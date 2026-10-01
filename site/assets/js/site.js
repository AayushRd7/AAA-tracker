/* AAA Tracker — public site (no dependencies, ~2KB) */
(function () {
  'use strict';

  // ---- mobile navigation ----
  var toggle = document.querySelector('.nav-toggle');
  var nav = document.getElementById('site-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    nav.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') {
        nav.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // ---- reveal on scroll ----
  var reveals = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && reveals.length) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          io.unobserve(entry.target);
        }
      });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    reveals.forEach(function (el) { io.observe(el); });
  } else {
    reveals.forEach(function (el) { el.classList.add('in'); });
  }

  // ---- hero mock: a light, deterministic "live" tick ----
  var tick = document.querySelector('[data-ticker]');
  var start = Date.now();
  function rnd(seed, i) {
    var x = Math.sin(seed * 9301 + i * 49297) * 233280;
    return x - Math.floor(x);
  }
  if (tick) {
    setInterval(function () {
      var t = Math.floor((Date.now() - start) / 1800);
      tick.textContent = (3 + Math.floor(rnd(7, t) * 6)) + ' conversions · ' +
        (180 + Math.floor(rnd(11, t) * 260)) + ' clicks · last ' + (2 + Math.floor(rnd(3, t) * 8)) + 's';
    }, 1800);
  }

  // ---- current year ----
  document.querySelectorAll('[data-year]').forEach(function (el) {
    el.textContent = String(new Date().getFullYear());
  });

  // ---- FAQ schema is authored per page; nothing to do here ----
})();
