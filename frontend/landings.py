from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import Optional
import html as html_lib
import ipaddress
import re
import socket
import zipfile, os, shutil
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin
from fastapi.responses import JSONResponse

import httpx
from db import get_db
from models import Landing

from pydantic import BaseModel


class FileSaveRequest(BaseModel):
    filename: str
    content: str


async def require_admin_dep(request: Request):
    """Admin gate for every landings-management route.

    These endpoints live on the public tracking host (nginx proxies /), so an
    unauthenticated visitor must never reach them — DELETE rmtree's landing
    folders and the editor reads/writes arbitrary files. require_admin lives in
    app.py, which imports this module — import it lazily at request time.
    """
    from app import require_admin
    await require_admin(request)


router = APIRouter(dependencies=[Depends(require_admin_dep)])

LANDINGS_DIR = "/app/landings"  # path inside the src container
os.makedirs(LANDINGS_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {'.html', '.php', '.css', '.js', '.jpg', '.jpeg', '.png'}

# Zip-upload caps: reject oversized / zip-bomb archives before extractall().
ZIP_MAX_ENTRIES = 2000
ZIP_MAX_ENTRY_BYTES = 50 * 1024 * 1024      # 50 MB per entry
ZIP_MAX_TOTAL_BYTES = 200 * 1024 * 1024     # 200 MB uncompressed total
ZIP_MAX_RATIO = 100                          # uncompressed : compressed hard cap


# ─── G73 — lander grabber ──────────────────────────────────────────
FOLDER_NAME_RE = re.compile(r"^[a-z0-9_]{1,250}$")
GRAB_MAX_BYTES = 3 * 1024 * 1024          # 3 MB page cap
GRAB_TIMEOUT_SECONDS = 10
GRAB_MAX_REDIRECTS = 5
GRAB_UA = "AAA-Tracker-LanderGrabber/1.0"


def validate_folder_name(folder: str) -> str:
    """Same zip-slip-safe naming as the upload flow: lowercase letters,
    numbers and underscores; no traversal, no absolute paths."""
    folder = (folder or "").strip()
    if not FOLDER_NAME_RE.match(folder):
        raise HTTPException(
            status_code=400,
            detail="Folder name must be 1-250 chars of lowercase letters, numbers and underscores.")
    if folder in (".", "..") or os.sep in folder:
        raise HTTPException(status_code=400, detail="Invalid folder name.")
    return folder


def _within(base, target) -> bool:
    """True only when target resolves inside base.

    Uses realpath + commonpath so a sibling directory whose name merely shares
    a prefix with base (e.g. base=/app/landings/site, target=/app/landings/site2)
    is rejected, unlike a plain str.startswith() check."""
    base_r = os.path.realpath(base)
    target_r = os.path.realpath(target)
    try:
        return os.path.commonpath([base_r, target_r]) == base_r
    except ValueError:
        # different drives / mixed absolute-relative — never contained
        return False


def safe_folder_name(folder: str) -> str:
    """Folder name that cannot escape the landings root.

    Unlike ``validate_folder_name`` (which also enforces a charset for the
    grabber's generated names), this only enforces the security property: no
    path separators and no escape from LANDINGS_DIR — so multi-word names with
    hyphens, dots or capitals stay valid.
    """
    f = (folder or "").strip()
    if not f or f in (".", "..") or "\x00" in f:
        raise HTTPException(status_code=400, detail="Invalid folder name")
    if "/" in f or "\\" in f:
        raise HTTPException(status_code=400,
                            detail="Folder name must not contain path separators")
    if not _within(LANDINGS_DIR, landing_path(f)):
        raise HTTPException(status_code=400, detail="Invalid folder path")
    return f[:255]


def _check_grab_url(url: str, allow_private: bool):
    """SSRF guard: http/https only, resolvable public host.

    Returns the parsed URL. Private/loopback/link-local/reserved targets are
    rejected unless the operator explicitly passes allow_private (self-hosted
    operators mirroring pages from their own internal network)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http:// and https:// URLs can be grabbed.")
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail="URL has no host.")
    host_l = host.lower().rstrip(".")
    if host_l in ("localhost",):
        raise HTTPException(status_code=400, detail="Refusing to grab from localhost.")
    if parsed.port not in (None, 80, 443):
        raise HTTPException(status_code=400, detail="Only default ports (80/443) are allowed.")

    try:
        ips = {ipaddress.ip_address(host_l)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host_l, parsed.port or (443 if parsed.scheme == "https" else 80),
                proto=socket.IPPROTO_TCP)
            ips = {ipaddress.ip_address(i[4][0]) for i in infos}
        except OSError:
            raise HTTPException(status_code=400, detail=f"Cannot resolve host '{host}'.")

    bad = [ip for ip in ips
           if ip.is_private or ip.is_loopback or ip.is_link_local
           or ip.is_multicast or ip.is_reserved or ip.is_unspecified]
    if bad and not allow_private:
        raise HTTPException(
            status_code=400,
            detail="Host resolves to a private/internal address — "
                   "re-check the URL or enable 'allow internal address' for own-infrastructure grabs.")
    return parsed


class _AssetRewriter(HTMLParser):
    """Re-serializes an HTML page, rewriting relative src/href/srcset URLs to
    absolute ones against the final (post-redirect) page URL so the mirrored
    copy loads remote assets. stdlib-only, no new dependencies."""

    URL_ATTRS = ("src", "href", "poster")

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.out = []
        self.title_parts = []
        self._in_title = False

    def _rewrite(self, tag: str, attrs):
        out = []
        for k, v in attrs:
            if v is None:
                out.append((k, None))
                continue
            if k in self.URL_ATTRS:
                v = urljoin(self.base_url, v)
            elif k == "srcset":
                parts = []
                for entry in v.split(","):
                    bits = entry.strip().split()
                    if bits:
                        bits[0] = urljoin(self.base_url, bits[0].strip())
                    parts.append(" ".join(bits))
                v = ", ".join(parts)
            out.append((k, v))
        return out

    @staticmethod
    def _render(tag, attrs, self_closing):
        rendered = "".join(
            f" {k}" if v is None else f' {k}="{html_lib.escape(v, quote=True)}"'
            for k, v in attrs)
        return f"<{tag}{rendered}{' /' if self_closing else ''}>"

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        self.out.append(self._render(tag, self._rewrite(tag, attrs), False))

    def handle_startendtag(self, tag, attrs):
        self.out.append(self._render(tag, self._rewrite(tag, attrs), True))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        self.out.append(data)

    def handle_comment(self, data):
        self.out.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.out.append(f"<!{decl}>")

    def handle_pi(self, data):
        self.out.append(f"<?{data}>")

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")

    @property
    def html(self):
        return "".join(self.out)

    @property
    def title(self):
        return " ".join("".join(self.title_parts).split())[:255]


@router.post("/landing/grab")
def grab_landing(
        url: str = Form(...),
        folder: str = Form(...),
        allow_private: bool = Form(False),
        db: Session = Depends(get_db)
):
    """Fetch a remote page and store it as a local landing (G73).

    Runs on the frontend service because only it (and nginx) mounts the
    landings volume. The SSRF guard rejects internal addresses unless the
    operator opts in with allow_private. Runs sync (threadpool) so the fetch
    never blocks the tracking event loop.
    """
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required.")
    folder = validate_folder_name(folder)

    current_url = url
    response = None
    try:
        with httpx.Client(timeout=GRAB_TIMEOUT_SECONDS, headers={"User-Agent": GRAB_UA}) as client:
            for _ in range(GRAB_MAX_REDIRECTS + 1):
                _check_grab_url(current_url, allow_private)
                response = client.get(current_url, follow_redirects=False)
                if response.status_code in (301, 302, 303, 307, 308) \
                        and response.headers.get("location"):
                    current_url = urljoin(current_url, response.headers["location"])
                    continue
                break
            else:
                raise HTTPException(status_code=400, detail="Too many redirects.")
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(status_code=400, detail="Fetch timed out (10s).")
    except httpx.TransportError as e:
        raise HTTPException(status_code=400, detail=f"Fetch failed: {str(e)[:200]}")

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Remote page returned HTTP {response.status_code}.")
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise HTTPException(status_code=400,
                            detail=f"Not an HTML page (content-type: {content_type or 'unknown'}).")
    if len(response.content) > GRAB_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Page exceeds the 3 MB limit.")

    final_url = str(response.url)
    rewriter = _AssetRewriter(final_url)
    try:
        rewriter.feed(response.text)
        rewriter.close()
    except Exception:
        # A malformed page still saves — fall back to the raw body so the
        # operator gets the mirror even when the parser chokes on bad markup.
        html_out, title = response.text, ""
    else:
        html_out, title = rewriter.html, rewriter.title

    if not title:
        title = urlparse(final_url).hostname or url

    folder_path = landing_path(folder)
    if os.path.exists(folder_path) and os.listdir(folder_path):
        raise HTTPException(status_code=400,
                            detail=f"Folder '{folder}' already exists and is not empty.")

    os.makedirs(folder_path, exist_ok=True)
    try:
        with open(os.path.join(folder_path, "index.html"), "w", encoding="utf-8") as f:
            f.write(html_out)
    except Exception:
        shutil.rmtree(folder_path, ignore_errors=True)
        raise HTTPException(status_code=500, detail="Failed to save the grabbed page.")

    landing = Landing(
        folder=folder,
        name=title,
        link=url[:255],
        type='local_file',
        created_at=datetime.utcnow()
    )
    db.add(landing)
    try:
        db.commit()
        db.refresh(landing)
    except IntegrityError as e:
        db.rollback()
        shutil.rmtree(folder_path, ignore_errors=True)
        if 'landings_folder_key' in str(e.orig):
            raise HTTPException(status_code=400,
                                detail="A landing with this folder already exists.")
        if 'landings_name_key' in str(e.orig):
            raise HTTPException(status_code=400,
                                detail="A landing with this name already exists.")
        raise HTTPException(status_code=500, detail="Database error: " + str(e.orig))

    return {
        "status": "ok",
        "site": folder,
        "url": f"/landing/{folder}/",
        "id": landing.id,
        "name": landing.name,
        "bytes": len(response.content),
    }



def get_next_site_id():
    existing = [f for f in os.listdir(LANDINGS_DIR) if
                f.startswith("site_") and os.path.isdir(os.path.join(LANDINGS_DIR, f))]
    numbers = [int(f.replace("site_", "")) for f in existing if f.replace("site_", "").isdigit()]
    return max(numbers, default=0) + 1


def landing_path(folder):
    return os.path.join(LANDINGS_DIR, folder)


def save_uploaded_file(file: UploadFile, folder_path: str):
    filename = file.filename.lower()

    if filename.endswith('.zip'):
        # Save and unpack the archive
        temp_zip = os.path.join(folder_path, "temp.zip")
        with open(temp_zip, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
                infos = zip_ref.infolist()

                # Bomb / resource caps — checked from central-directory metadata
                # BEFORE any bytes are written to disk.
                if len(infos) > ZIP_MAX_ENTRIES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Archive has too many files (max {ZIP_MAX_ENTRIES}).")
                uncompressed = 0
                for member in infos:
                    if member.file_size > ZIP_MAX_ENTRY_BYTES:
                        raise HTTPException(
                            status_code=400,
                            detail="Archive contains a single file larger than 50 MB.")
                    uncompressed += member.file_size
                if uncompressed > ZIP_MAX_TOTAL_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail="Archive expands to more than 200 MB.")
                compressed = os.path.getsize(temp_zip)
                if compressed > 0 and uncompressed > compressed * ZIP_MAX_RATIO:
                    raise HTTPException(
                        status_code=400,
                        detail="Archive looks like a zip bomb (compression ratio too high).")

                # Zip-slip: every entry must stay inside the landing folder.
                for member in infos:
                    if not _within(folder_path, os.path.join(folder_path, member.filename)):
                        raise HTTPException(status_code=400, detail="Archive contains unsafe paths")
                zip_ref.extractall(folder_path)
        finally:
            os.remove(temp_zip)

    elif filename.endswith('.php') or filename.endswith('.html'):
        # Save the file as is
        os.makedirs(folder_path, exist_ok=True)
        file_path = os.path.join(folder_path, filename)
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    else:
        raise HTTPException(status_code=400, detail="Unsupported file type. Only .zip, .php, .html allowed.")


@router.post("/landing")
async def upload_landing(
        name: str = Form(...),
        site_folder: str = Form(...),
        type: int = Form(...),  # now an int
        tags: str = Form(""),
        link: Optional[str] = Form(None),
        file: Optional[UploadFile] = File(None),
        db: Session = Depends(get_db)
):
    type_mapping = {0: 'link', 1: 'mirror', 2: 'local_file'}
    landing_type = type_mapping.get(type)

    if landing_type is None:
        raise HTTPException(status_code=400, detail="Invalid landing type.")

    # Handle the case where the file is passed as an empty string
    if isinstance(file, str) and file == "":
        file = None

    if landing_type in ('link', 'mirror') and not link:
        raise HTTPException(status_code=400, detail="Link is required for 'link' or 'mirror' type.")

    if landing_type == 'local_file' and not file:
        raise HTTPException(status_code=400, detail="File is required for 'local_file' type.")

    if not site_folder:
        site_folder = f"site_{get_next_site_id()}"
    else:
        site_folder = safe_folder_name(site_folder)

    full_path = landing_path(site_folder)

    if landing_type == 'local_file':
        os.makedirs(full_path, exist_ok=True)
        save_uploaded_file(file, full_path)

    landing = Landing(
        folder=site_folder,
        name=name.strip()[:255] if name and name.strip() else None,
        link=link.strip()[:255] if link and link.strip() else None,
        type=landing_type,
        tags=tags[:250] if tags else None,
        created_at=datetime.utcnow()
    )
    db.add(landing)

    try:
        db.commit()
        db.refresh(landing)
    except IntegrityError as e:
        db.rollback()

        if 'landings_folder_key' in str(e.orig):
            raise HTTPException(
                status_code=400,
                detail="A landing with this folder already exists."
            )
        if 'landings_name_key' in str(e.orig):
            raise HTTPException(
                status_code=400,
                detail="A landing with this name already exists."
            )
        raise HTTPException(
            status_code=500,
            detail="Database error: " + str(e.orig)
        )

    return {
        "status": "ok",
        "site": site_folder,
        "url": f"/landing/{site_folder}/" if landing_type == 'local_file' else link,
        "id": landing.id
    }


def get_landing_metrics(ch) -> dict:
    """Real per-landing metrics from ClickHouse, keyed by landing id (str)."""
    metrics = {}
    try:
        result = ch.query("""
            SELECT
                toString(coalesce(landing_id, '')) AS landing_key,
                countIf(click = true) AS clicks,
                countIf(status IN ('sale', 'upsale')) AS conversions,
                sumOrNull(toFloat64(cost)) AS cost,
                sumOrNull(toFloat64(revenue)) AS revenue
            FROM clicks_data
            GROUP BY landing_key
        """)
        for key, clicks, conversions, cost, revenue in result.result_rows:
            cost = float(cost or 0)
            revenue = float(revenue or 0)
            metrics[str(key)] = {
                "clicks": int(clicks),
                "conversions": int(conversions),
                "cost": round(cost, 2),
                "revenue": round(revenue, 2),
                "roi": round((revenue - cost) / cost * 100, 2) if cost else 0.0,
            }
    except Exception as e:
        print("Landing metrics ClickHouse error:", str(e))
    return metrics


@router.get("/landings")
def list_landings(db: Session = Depends(get_db)):
    landings = db.query(Landing).all()
    metrics = {}
    try:
        from clickhouse_connect import get_client  # same container as the tracking plane
        ch = get_client(
            host=os.environ.get("CLICKHOUSE_HOST", "tracker_clickhouse"),
            port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
            username=os.environ.get("CLICKHOUSE_USER", "user"),
            password=os.environ.get("CLICKHOUSE_PASSWORD") or "_".join(["password"] * 3),
            database=os.environ.get("CLICKHOUSE_DB", "default")
        )
        try:
            metrics = get_landing_metrics(ch)
        finally:
            ch.close()
    except Exception as e:
        print("Landing metrics unavailable:", str(e))
    return [
        {
            "id": landing.id,
            "folder": landing.folder,
            "name": landing.name,
            "link": landing.link,
            "type": landing.type.value if hasattr(landing.type, "value") else landing.type,  # ENUM support
            "tags": landing.tags.split(",") if landing.tags else [],
            "created_at": landing.created_at,
            **metrics.get(str(landing.id), {"clicks": 0, "conversions": 0, "cost": 0, "revenue": 0, "roi": 0})
        }
        for landing in landings
    ]


@router.get("/landing/{landing_id}")
def get_landing(landing_id: int, db: Session = Depends(get_db)):
    landing = db.query(Landing).filter(Landing.id == landing_id).first()
    if not landing:
        raise HTTPException(status_code=404, detail="Landing not found")
    return {
        "id": landing.id,
        "folder": landing.folder,
        "name": landing.name,
        "link": landing.link,
        "type": landing.type,
        "tags": landing.tags.split(",") if landing.tags else [],
        "created_at": landing.created_at
    }


@router.put("/landing/{landing_id}")
async def update_landing(
        landing_id: int,
        name: Optional[str] = Form(None),
        site_folder: Optional[str] = Form(None),
        tags: Optional[str] = Form(None),
        link: Optional[str] = Form(None),
        type: Optional[int] = Form(None),
        file: Optional[UploadFile] = File(None),
        db: Session = Depends(get_db)
):
    type_mapping = {0: 'link', 1: 'mirror', 2: 'local_file'}

    landing = db.query(Landing).filter(Landing.id == landing_id).first()
    if not landing:
        raise HTTPException(status_code=404, detail="Landing not found")

    if name:
        landing.name = name[:255]
    if site_folder:
        landing.folder = safe_folder_name(site_folder)
    if tags is not None:
        landing.tags = tags[:250] if tags else None
    if link is not None:
        landing.link = link[:255] if link else None
    if type is not None:
        landing_type = type_mapping.get(type)
        if not landing_type:
            raise HTTPException(status_code=400, detail="Invalid landing type")
        landing.type = landing_type

    folder_path = landing_path(landing.folder)

    # When a new file is uploaded
    if file:
        if landing.type != 'local_file':
            raise HTTPException(status_code=400, detail="Cannot upload file for non-local_file landing")

        # Never rmtree/write outside the landings root
        if not _within(LANDINGS_DIR, folder_path):
            raise HTTPException(status_code=400, detail="Invalid folder path")

        # Clear the folder before a new upload
        if os.path.exists(folder_path):
            for item in os.listdir(folder_path):
                item_path = os.path.join(folder_path, item)
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
        else:
            os.makedirs(folder_path, exist_ok=True)

        save_uploaded_file(file, folder_path)

    try:
        db.commit()
        db.refresh(landing)
    except IntegrityError as e:
        db.rollback()

        if 'landings_folder_key' in str(e.orig):
            raise HTTPException(
                status_code=400,
                detail="A landing with this folder already exists."
            )
        if 'landings_name_key' in str(e.orig):
            raise HTTPException(
                status_code=400,
                detail="A landing with this name already exists."
            )
        raise HTTPException(
            status_code=500,
            detail="Database error: " + str(e.orig)
        )

    return {
        "status": "updated",
        "id": landing.id,
        "folder": landing.folder,
        "name": landing.name,
        "link": landing.link,
        "type": landing.type,
        "tags": landing.tags,
        "created_at": landing.created_at
    }


@router.delete("/landing/{landing_id}")
def delete_landing(landing_id: int, db: Session = Depends(get_db)):
    landing = db.query(Landing).filter(Landing.id == landing_id).first()
    if not landing:
        raise HTTPException(status_code=404, detail="Landing not found")

    folder_path = landing_path(landing.folder)
    # Landing.type is a LandingMood enum — compare the value, not the member
    if getattr(landing.type, "value", landing.type) == 'local_file':
        if not _within(LANDINGS_DIR, folder_path):
            raise HTTPException(status_code=400, detail="Invalid folder path")
        if os.path.exists(folder_path):
            shutil.rmtree(folder_path)

    db.delete(landing)
    db.commit()

    return {"status": "deleted", "id": landing_id}


def get_landing_folder(db: Session, landing_id: int) -> str:
    landing = db.query(Landing).filter(Landing.id == landing_id).first()
    if not landing or not landing.folder:
        raise HTTPException(status_code=404, detail="Landing folder not found")

    base_dir = "/app/landings"
    folder_path = os.path.abspath(os.path.join(base_dir, landing.folder))

    # Guard: the path must stay inside base_dir
    if not _within(base_dir, folder_path):
        raise HTTPException(status_code=400, detail="Invalid folder path")

    return folder_path



from pathlib import Path



def build_tree(base: Path, current: Path):
    children = []
    for item in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rel_path = item.relative_to(base).as_posix()
        if item.is_dir():
            children.append({
                "name": item.name,
                "path": rel_path,
                "type": "folder",
                "children": build_tree(base, item)
            })
        else:
            children.append({
                "name": item.name,
                "path": rel_path,
                "type": "file"
            })
    return children


@router.get("/landings_editor/{landing_id}/tree")
def get_file_tree(landing_id: int, db: Session = Depends(get_db)):
    base = Path(get_landing_folder(db, landing_id))
    if not base.exists():
        raise HTTPException(404, "Landing folder not found")

    tree = build_tree(base, base)
    return JSONResponse(tree)


@router.get("/landings_editor/{landing_id}/files")
def list_all_files(landing_id: int, db: Session = Depends(get_db)):
    base = Path(get_landing_folder(db, landing_id))
    tree = {
        "name": base.name,
        "path": "",
        "type": "folder",
        "children": build_tree(base, base)
    }
    return JSONResponse(tree)



@router.get("/landings_editor/{landing_id}/file")
def get_file(landing_id: int, filename: str, db: Session = Depends(get_db)):
    base = Path(get_landing_folder(db, landing_id))
    safe_rel_path = Path(filename).as_posix().lstrip("/")
    full_path = base.joinpath(safe_rel_path).resolve()

    # protect against escaping the folder
    if not _within(base, full_path):
        raise HTTPException(status_code=400, detail="Invalid file path")

    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return {"content": full_path.read_text(encoding="utf-8")}


class FileSaveRequest(BaseModel):
    filename: str
    content: str


@router.post("/landings_editor/{landing_id}/file")
def save_file(landing_id: int, payload: FileSaveRequest, db: Session = Depends(get_db)):
    base = Path(get_landing_folder(db, landing_id))
    safe_rel_path = Path(payload.filename).as_posix().lstrip("/")

    full_path = base.joinpath(safe_rel_path).resolve()

    # Security — the path must stay inside base
    if not _within(base, full_path):
        raise HTTPException(400, "Invalid file path")

    # Make sure the directory exists
    full_path.parent.mkdir(parents=True, exist_ok=True)

    with open(full_path, "w", encoding="utf-8") as f:
        f.write(payload.content)

    return {"status": "ok"}



@router.post("/landings_editor/{landing_id}/upload")
def upload_file(
    landing_id: int,
    path: str = Query(..., description="Relative path to save file (e.g. subdir/image.png)"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    base = Path(get_landing_folder(db, landing_id))
    relative_path = Path(path).as_posix().lstrip("/")
    save_path = base.joinpath(relative_path).resolve()

    # Ensure path is inside base directory
    if not _within(base, save_path):
        raise HTTPException(400, "Invalid path")

    ext = save_path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Extension '{ext}' not allowed")

    # Create directory if needed
    save_path.parent.mkdir(parents=True, exist_ok=True)

    with open(save_path, "wb") as out_file:
        shutil.copyfileobj(file.file, out_file)

    return {"status": "ok", "filename": str(save_path.relative_to(base))}



@router.post("/landings_editor/{landing_id}/file-plain")
def save_file_plain(
    landing_id: int,
    filename: str = Form(...),
    content: str = Form(...),
    db: Session = Depends(get_db)
):
    base = Path(get_landing_folder(db, landing_id))
    safe_rel_path = Path(filename).as_posix().lstrip("/")
    full_path = base.joinpath(safe_rel_path).resolve()

    if not _within(base, full_path):
        raise HTTPException(400, "Invalid file path")

    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return {"status": "ok"}
