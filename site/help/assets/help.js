/* ============================================================================
   AAA Tracker — help centre behaviour (progressive enhancement)

   Hub:     loads search-index.json and filters as you type.
   Article: highlights the active section in the on-page table of contents.

   The hub is fully usable without this file: every category and article is
   server-rendered in index.html.
   ========================================================================= */
(function () {
  "use strict";

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function escRe(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  /* Wrap every query term in <mark>, escaping the source first. */
  function highlight(text, terms) {
    if (!terms.length) return esc(text);
    var re = new RegExp("(" + terms.map(escRe).join("|") + ")", "gi");
    return esc(text).replace(re, "<mark>$1</mark>");
  }

  function snippet(text, terms) {
    if (!text) return "";
    var lower = text.toLowerCase();
    var idx = -1;
    for (var i = 0; i < terms.length; i++) {
      var at = lower.indexOf(terms[i]);
      if (at !== -1 && (idx === -1 || at < idx)) idx = at;
    }
    if (idx === -1) return text.slice(0, 180) + (text.length > 180 ? "…" : "");
    var start = Math.max(0, idx - 50);
    var end = Math.min(text.length, start + 200);
    return (start > 0 ? "…" : "") + text.slice(start, end) + (end < text.length ? "…" : "");
  }

  /* ── hub search ──────────────────────────────────────────────────────── */
  function initSearch() {
    var input = document.getElementById("help-search");
    var results = document.getElementById("help-results");
    var form = document.getElementById("help-search-form");
    if (!input || !results || !form) return;

    var resultsSection = results.closest(".help-results-section");
    var categories = document.getElementById("help-categories");
    var popular = document.getElementById("help-popular");

    var index = [];
    var fetched = false;
    var active = -1;

    function load() {
      if (fetched) return Promise.resolve();
      fetched = true;
      return fetch("/help/search-index.json")
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (data) { index = Array.isArray(data) ? data : []; })
        .catch(function () { index = []; });
    }

    function matches(item, terms) {
      var hay = (item.title + " " + item.description + " " +
                 (item.tags || []).join(" ") + " " + item.category + " " +
                 item.text).toLowerCase();
      return terms.every(function (t) { return hay.indexOf(t) !== -1; });
    }

    function score(item, terms) {
      var title = item.title.toLowerCase();
      var tags = (item.tags || []).join(" ").toLowerCase();
      var s = 0;
      terms.forEach(function (t) {
        if (title.indexOf(t) === 0) s += 12;
        else if (title.indexOf(t) !== -1) s += 6;
        if (tags.indexOf(t) !== -1) s += 3;
      });
      return s;
    }

    function setActive(n) {
      var links = results.querySelectorAll(".help-result");
      if (!links.length) return;
      active = (n + links.length) % links.length;
      for (var i = 0; i < links.length; i++) {
        links[i].classList.toggle("is-active", i === active);
      }
      links[active].scrollIntoView({ block: "nearest" });
    }

    function show(on) {
      if (resultsSection) resultsSection.classList.toggle("is-on", on);
      results.hidden = !on;
      if (categories) categories.hidden = on;
      if (popular) popular.hidden = on;
    }

    function render(terms) {
      active = -1;
      if (!terms.length) { show(false); results.innerHTML = ""; return; }

      var hits = index.filter(function (it) { return matches(it, terms); });
      hits.sort(function (a, b) {
        return score(b, terms) - score(a, terms) ||
               a.title.localeCompare(b.title);
      });
      hits = hits.slice(0, 20);
      show(true);

      if (!hits.length) {
        results.innerHTML =
          '<p class="help-noresults">No results for “' + esc(terms.join(" ")) +
          '”. Try a shorter phrase, or <a href="/contact">contact support</a>.</p>';
        return;
      }

      var html = '<p class="help-results__count">' + hits.length +
        " result" + (hits.length === 1 ? "" : "s") + "</p>";
      hits.forEach(function (it) {
        html += '<a class="help-result" href="/help/' + esc(it.slug) + '">' +
          '<span class="help-result__top">' +
          '<span class="help-result__cat">' + esc(it.category) + "</span>" +
          '<span class="help-result__title">' + highlight(it.title, terms) + "</span>" +
          "</span>" +
          '<span class="help-result__snippet">' +
          highlight(snippet(it.description + " — " + it.text, terms), terms) +
          "</span></a>";
      });
      results.innerHTML = html;
    }

    function onInput() {
      var terms = input.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
      load().then(function () { render(terms); });
    }

    input.addEventListener("input", onInput);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var first = results.querySelector(".help-result");
      if (first) window.location.href = first.getAttribute("href");
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
      else if (e.key === "Enter" && active !== -1) {
        e.preventDefault();
        var link = results.querySelectorAll(".help-result")[active];
        if (link) window.location.href = link.getAttribute("href");
      } else if (e.key === "Escape") {
        input.value = "";
        render([]);
      }
    });

    // Deep link: /help/?q=postback
    var q = new URLSearchParams(window.location.search).get("q");
    if (q) { input.value = q; onInput(); }
  }

  /* ── article: table-of-contents scrollspy ────────────────────────────── */
  function initToc() {
    var toc = document.querySelector(".help-toc");
    if (!toc) return;
    var links = Array.prototype.slice.call(toc.querySelectorAll('a[href^="#"]'));
    if (!links.length || !("IntersectionObserver" in window)) return;

    var byId = {};
    links.forEach(function (a) { byId[a.getAttribute("href").slice(1)] = a; });

    var targets = links.map(function (a) {
      return document.getElementById(a.getAttribute("href").slice(1));
    }).filter(Boolean);
    if (!targets.length) return;

    var visible = {};
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { visible[entry.target.id] = entry.isIntersecting; });
      var current = null;
      for (var i = 0; i < targets.length; i++) {
        if (visible[targets[i].id]) { current = targets[i].id; break; }
      }
      links.forEach(function (a) { a.classList.remove("is-active"); });
      if (current && byId[current]) byId[current].classList.add("is-active");
    }, { rootMargin: "-30% 0px -60% 0px", threshold: 0 });

    targets.forEach(function (t) { observer.observe(t); });
  }

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () { initSearch(); initToc(); });
})();
