/* AAA Tracker — public site. No dependencies. */
(function () {
  'use strict';

  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---- sticky header: shrinks as you scroll ---- */
  var header = document.querySelector('.site-header');
  if (header) {
    var SHO = function () { return window.scrollY || window.pageYOffset || 0; };
    var stick = function () {
      var y = SHO();
      header.classList.toggle('is-stuck', y > 8);
      header.style.setProperty('--shrink', Math.min(1, y / 140).toFixed(3));
    };
    stick();
    window.addEventListener('scroll', stick, { passive: true });
  }

  /* ---- bands that expand to full-bleed as they travel up the viewport ---- */
  var bleeders = [].slice.call(document.querySelectorAll('[data-bleed]'));
  if (bleeders.length && !reduced) {
    var root = document.documentElement;
    var vw = 0;
    var measure = function () {
      vw = root.clientWidth;
      root.style.setProperty('--vw', vw + 'px');
    };
    var expand = function () {
      var vh = window.innerHeight;
      var start = vh * 0.92;   /* band top at the fold — still a panel   */
      var end = vh * 0.34;     /* band top well inside — fully bled      */
      for (var i = 0; i < bleeders.length; i++) {
        var el = bleeders[i];
        var t = (start - el.getBoundingClientRect().top) / (start - end);
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        el.style.setProperty('--expand', (t * t * (3 - 2 * t)).toFixed(4));
      }
    };
    var queued = false;
    var onScroll = function () {
      if (queued) return;
      queued = true;
      requestAnimationFrame(function () { queued = false; expand(); });
    };
    measure();
    expand();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', function () { measure(); expand(); });
  }

  /* ---- mobile navigation ---- */
  var toggle = document.querySelector('.nav-toggle');
  var nav = document.getElementById('site-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    nav.addEventListener('click', function (e) {
      if (e.target.closest('a')) {
        nav.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && nav.classList.contains('open')) {
        nav.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
        toggle.focus();
      }
    });
  }

  /* ---- animated counters ---- */
  function countUp(el) {
    var to = parseFloat(el.getAttribute('data-count'));
    if (isNaN(to)) return;
    var dec = (el.getAttribute('data-count').split('.')[1] || '').length;
    var prefix = el.getAttribute('data-prefix') || '';
    var suffix = el.getAttribute('data-suffix') || '';
    if (reduced) { el.textContent = prefix + to.toFixed(dec) + suffix; return; }
    var start = performance.now(), dur = 1100;
    (function step(now) {
      var t = Math.min(1, (now - start) / dur);
      var eased = 1 - Math.pow(1 - t, 3);
      var v = to * eased;
      el.textContent = prefix + v.toFixed(dec).replace(/\B(?=(\d{3})+(?!\d))/g, ',') + suffix;
      if (t < 1) requestAnimationFrame(step);
    })(start);
  }

  /* ---- meters: fill when they scroll into view ---- */
  function fillMeter(el) {
    var w = el.getAttribute('data-meter') || '0';
    el.style.width = w.indexOf('%') > -1 ? w : w + '%';
  }

  /* ---- reveal on scroll ---- */
  var watched = document.querySelectorAll('.reveal, [data-count], .meter__fill[data-meter]');
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var el = entry.target;
        if (el.classList.contains('reveal')) el.classList.add('in');
        if (el.hasAttribute('data-count')) countUp(el);
        if (el.hasAttribute('data-meter')) fillMeter(el);
        io.unobserve(el);
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    watched.forEach(function (el) {
      if (el.hasAttribute('data-count')) el.textContent = el.getAttribute('data-prefix') || '';
      io.observe(el);
    });
  } else {
    document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
    document.querySelectorAll('[data-count]').forEach(countUp);
    document.querySelectorAll('.meter__fill[data-meter]').forEach(fillMeter);
  }

  /* ---- hero demo video ----
     Nineteen seconds of muted footage on a loop. It must not start for anyone
     who has asked for reduced motion, and it needs a working pause control:
     WCAG 2.2.2 covers motion that begins on its own and runs past five seconds,
     and a muted autoplay video shows no browser controls to stop it. */
  document.querySelectorAll('[data-demo-video]').forEach(function (v) {
    var wrap = v.closest('.hero-demo');
    var btn = wrap && wrap.querySelector('[data-demo-toggle]');

    if (reduced) {
      v.removeAttribute('autoplay');
      v.pause();
    } else if (v.paused) {
      var started = v.play();
      // Autoplay can still be refused; the poster then just stays put.
      if (started && started.catch) started.catch(function () {});
    }

    if (!btn) return;
    var sync = function () {
      var playing = !v.paused && !v.ended;
      btn.setAttribute('aria-pressed', playing ? 'false' : 'true');
      btn.setAttribute('aria-label', playing ? 'Pause the demo video' : 'Play the demo video');
    };
    btn.addEventListener('click', function () {
      if (v.paused) {
        var again = v.play();
        if (again && again.catch) again.catch(function () {});
      } else {
        v.pause();
      }
    });
    ['play', 'pause', 'ended'].forEach(function (ev) { v.addEventListener(ev, sync); });
    sync();
  });

  /* ---- tabs ---- */
  document.querySelectorAll('[data-tabs]').forEach(function (root) {
    var btns = Array.prototype.slice.call(root.querySelectorAll('.tabs__btn'));
    var panes = Array.prototype.slice.call(root.querySelectorAll('.tabpane'));
    if (!btns.length) return;
    function select(i, focus) {
      btns.forEach(function (b, j) {
        var on = i === j;
        b.setAttribute('aria-selected', on ? 'true' : 'false');
        b.tabIndex = on ? 0 : -1;
      });
      panes.forEach(function (p, j) { p.classList.toggle('is-on', i === j); });
      /* meters inside a pane that was display:none have no transition — set at once */
      panes[i].querySelectorAll('.meter__fill[data-meter]').forEach(fillMeter);
      if (focus) btns[i].focus();
    }
    btns.forEach(function (b, i) {
      b.addEventListener('click', function () { select(i); });
      b.addEventListener('keydown', function (e) {
        var n = null;
        if (e.key === 'ArrowRight') n = (i + 1) % btns.length;
        if (e.key === 'ArrowLeft') n = (i - 1 + btns.length) % btns.length;
        if (e.key === 'Home') n = 0;
        if (e.key === 'End') n = btns.length - 1;
        if (n !== null) { e.preventDefault(); select(n, true); }
      });
    });
    var initial = btns.findIndex(function (b) { return b.getAttribute('aria-selected') === 'true'; });
    select(initial < 0 ? 0 : initial);
  });

  /* ---- contact form: compose an email, no backend required ---- */
  var form = document.querySelector('[data-mailto]');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var to = form.getAttribute('data-mailto');
      var get = function (name) {
        var el = form.elements[name];
        return el ? String(el.value || '').trim() : '';
      };
      var subject = 'AAA Tracker — ' + (get('topic') || 'Enquiry') + ' — ' + (get('name') || 'website');
      var body = [
        'Name: ' + get('name'),
        'Email: ' + get('email'),
        'Topic: ' + get('topic'),
        'Company: ' + get('company'),
        '',
        get('message')
      ].join('\n');
      window.location.href = 'mailto:' + to +
        '?subject=' + encodeURIComponent(subject) +
        '&body=' + encodeURIComponent(body);
      var done = form.querySelector('[data-mailto-done]');
      if (done) done.hidden = false;
    });
  }

  /* ---- current year ---- */
  document.querySelectorAll('[data-year]').forEach(function (el) {
    el.textContent = String(new Date().getFullYear());
  });
})();
