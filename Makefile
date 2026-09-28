# Docker Compose invocation: modern Docker ships the `docker compose` plugin,
# older installs ship the standalone `docker-compose` binary. Detect once.
COMPOSE := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; elif command -v docker-compose >/dev/null 2>&1; then echo "docker-compose"; else echo "docker-compose"; fi)

.PHONY: check env preclean reset install install-local install-no-sll install-prod-domain install-db \
        generate-local-cert certificate stop start restart update logs reload-nginx \
        seed-demo-data start-http restart-nginx clear-logs

# Preflight: fail with an actionable message instead of "No such file or directory".
check:
	@command -v docker >/dev/null 2>&1 || { \
		echo "✗ Docker is not installed. See https://docs.docker.com/engine/install/"; exit 1; }
	@docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1 || { \
		echo "✗ Docker Compose not found (neither 'docker compose' nor 'docker-compose')."; \
		echo ""; \
		echo "  Recommended — install the Compose plugin:"; \
		echo "    Debian/Ubuntu : sudo apt-get update && sudo apt-get install -y docker-compose-plugin"; \
		echo "    RHEL/Fedora   : sudo dnf install -y docker-compose-plugin"; \
		echo "    Docs          : https://docs.docker.com/compose/install/linux/"; \
		echo ""; \
		echo "  Alternative — standalone v2 binary:"; \
		echo "    sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose"; \
		echo "    sudo chmod +x /usr/local/bin/docker-compose"; \
		exit 1; }
	@echo "→ compose: $(COMPOSE)"

# First-run configuration: create .env from .env.example. An existing .env is never
# touched. Placeholder credentials are replaced with generated ones ONLY when no
# database volume exists yet — a database keeps the password it was first created
# with, so rotating it out from under an existing volume would break the stack.
env:
	@if [ -f .env ]; then echo "→ .env present (left unchanged)"; \
	elif [ -f .env.example ]; then \
		cp .env.example .env; \
		if docker volume ls --format '{{.Name}}' | grep -qx 'aaa-tracker_postgres_data' \
		   || docker volume ls --format '{{.Name}}' | grep -qx 'postgres_data'; then \
			echo "⚠️  A database volume already exists but .env is missing."; \
			echo "    That volume keeps the credentials it was first created with, so the"; \
			echo "    values from .env.example are kept to match it."; \
			echo "    To start clean with fresh credentials instead: make reset && make install"; \
			echo "    (make reset DELETES all tracking data)."; \
		else \
			gen() { openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }; \
			sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$$(gen)|" .env; \
			sed -i.bak "s|^CLICKHOUSE_PASSWORD=.*|CLICKHOUSE_PASSWORD=$$(gen)|" .env; \
			sed -i.bak "s|^JWT_SECRET=.*|JWT_SECRET=$$(gen)|" .env; \
			rm -f .env.bak; \
			echo "→ created .env from .env.example with generated secrets"; \
		fi; \
	else \
		echo "✗ No .env and no .env.example — cannot configure the stack."; exit 1; \
	fi

# A previous attempt (or a deleted checkout) leaves containers holding the fixed
# names this stack uses, so `compose up` fails with "container name is already in
# use". Stop the current project, then force-remove ONLY the container names this
# compose file defines. Data lives in named volumes and is never touched here.
preclean: check
	@$(COMPOSE) down --remove-orphans >/dev/null 2>&1 || true; \
	names=$$(grep -h 'container_name:' docker-compose.yml docker-compose.prod.yml 2>/dev/null | awk '{print $$2}' | sort -u); \
	stale=""; \
	for n in $$names; do \
		if docker ps -a --format '{{.Names}}' | grep -qx "$$n"; then stale="$$stale $$n"; fi; \
	done; \
	if [ -n "$$stale" ]; then \
		echo "→ clearing leftover containers from a previous install:"; \
		for n in $$stale; do echo "    $$n"; done; \
		docker rm -f $$stale >/dev/null 2>&1 || true; \
		echo "  (data volumes preserved — no clicks or conversions are lost)"; \
	fi

install-db:
	docker exec tracker_backend pip install --no-cache-dir -r /app/install/requirements.txt
	docker exec tracker_backend python3 /app/install/install.py

install-no-sll: check env preclean
	cp nginx/nginx.dev.conf nginx/default.conf
	$(MAKE) generate-local-cert
	$(COMPOSE) --compatibility up --build -d --remove-orphans
	$(MAKE) install-db

install-prod-domain: check env preclean
	cp nginx/nginx.prod.conf nginx/default.conf
	$(COMPOSE) --compatibility up --build -d --remove-orphans
	$(MAKE) certificate
	$(MAKE) install-db

install: check env preclean
	cp nginx/nginx.dev.conf nginx/default.conf
	$(MAKE) generate-local-cert
	$(COMPOSE) --compatibility up --build -d --remove-orphans
	$(MAKE) install-db

install-local: check env preclean
	cp nginx/nginx.dev.conf nginx/default.conf
	$(MAKE) generate-local-cert
	$(COMPOSE) --compatibility up --build -d --remove-orphans
	$(MAKE) install-db

generate-local-cert:
	mkdir -p ssl
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout ssl/selfsigned.key \
		-out ssl/selfsigned.crt \
		-subj "/C=US/ST=Local/L=Local/O=Dev/OU=Dev/CN=localhost"

certificate:
	docker exec tracker_nginx certbot --nginx

stop: check
	$(COMPOSE) down

start: check env preclean
	$(COMPOSE) --compatibility up --build -d --remove-orphans

restart: check env preclean
	$(COMPOSE) down && $(COMPOSE) --compatibility up --build -d --remove-orphans

update: check env preclean
	git pull
	$(MAKE) restart

logs: check
	$(COMPOSE) logs -f

# DESTRUCTIVE: removes this tracker's containers AND data volumes (all clicks,
# conversions, settings). Use it to start from scratch, not to upgrade.
reset: check
	@echo "⚠️  Removing containers AND data volumes for this tracker…"
	$(COMPOSE) down --volumes --remove-orphans || true
	@names=$$(grep -h 'container_name:' docker-compose.yml docker-compose.prod.yml 2>/dev/null | awk '{print $$2}' | sort -u); \
	for n in $$names; do docker rm -f "$$n" >/dev/null 2>&1 || true; done; \
	echo "→ reset complete — run 'make install' for a clean setup"

reload-nginx:
	docker exec tracker_nginx nginx -s reload

seed-demo-data:
	docker exec -it tracker_frontend python3 /app/scripts/seed_demo.py

start-http: check env preclean
	$(COMPOSE) up -d nginx backend frontend

restart-nginx: check
	$(COMPOSE) restart nginx

clear-logs: check
	@echo "Stopping containers..."
	$(COMPOSE) down
	@echo "Truncating logs..."
	sudo truncate -s 0 /var/lib/docker/containers/*/*-json.log || true
	@echo "Clearing logs..."
	$(COMPOSE) logs --no-color > /dev/null 2>&1 || true
	@docker system prune -f --volumes || true
	@echo "Logs cleared (via prune)."
	rm -f logs/*.log || true
	@echo "Log files removed"
	@echo "Starting containers..."
	$(COMPOSE) --compatibility up --build -d