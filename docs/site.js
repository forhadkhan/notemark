(function () {
  'use strict';
  var root = document.documentElement;
  var KEY = 'notemark-theme';
  var reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  var dark = window.matchMedia && matchMedia('(prefers-color-scheme: dark)');

  function stored() {
    try { var v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null; } catch (e) { return null; }
  }
  function save(v) {
    try { localStorage.setItem(KEY, v); } catch (e) { /* storage unavailable */ }
  }
  function current() { return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light'; }

  // Theme -----------------------------------------------------------------
  var toggle = document.getElementById('theme-toggle');
  function syncToggle() {
    toggle.setAttribute('aria-label', current() === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }
  // The variant that is hidden stays lazy until the theme is about to change.
  function warm(theme) {
    document.querySelectorAll('img.t-' + theme).forEach(function (img) { img.loading = 'eager'; });
  }
  function setTheme(next) {
    warm(next);
    function apply() { root.setAttribute('data-theme', next); syncToggle(); }
    if (reduce) { apply(); return; }
    if (document.startViewTransition) {
      document.startViewTransition(apply);
    } else {
      root.classList.add('theme-fade');
      apply();
      setTimeout(function () { root.classList.remove('theme-fade'); }, 450);
    }
  }
  var other = function () { return current() === 'dark' ? 'light' : 'dark'; };
  ['pointerenter', 'focus', 'touchstart'].forEach(function (ev) {
    toggle.addEventListener(ev, function () { warm(other()); }, { passive: true, once: true });
  });
  toggle.addEventListener('click', function () {
    var next = other();
    save(next);
    setTheme(next);
  });
  if (dark && dark.addEventListener) {
    dark.addEventListener('change', function (e) {
      if (!stored()) setTheme(e.matches ? 'dark' : 'light');
    });
  }
  syncToggle();

  // Tabs (install methods, screenshot views) ------------------------------
  document.querySelectorAll('[data-tabs]').forEach(function (box) {
    var list = box.querySelector('[role="tablist"]');
    var tabs = Array.prototype.slice.call(box.querySelectorAll('[role="tab"]'));
    function select(tab, focus) {
      tabs.forEach(function (t, i) {
        var on = t === tab;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
        if (on) list.style.setProperty('--i', i);
      });
      if (focus) tab.focus();
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(t); });
      t.addEventListener('keydown', function (e) {
        var n = null;
        if (e.key === 'ArrowRight') n = tabs[(i + 1) % tabs.length];
        else if (e.key === 'ArrowLeft') n = tabs[(i - 1 + tabs.length) % tabs.length];
        else if (e.key === 'Home') n = tabs[0];
        else if (e.key === 'End') n = tabs[tabs.length - 1];
        if (n) { e.preventDefault(); select(n, true); }
      });
    });
  });

  // Copy buttons ----------------------------------------------------------
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
  document.querySelectorAll('.code .copy').forEach(function (btn) {
    var base = btn.getAttribute('aria-label');
    var timer;
    btn.addEventListener('click', function () {
      var text = btn.parentNode.querySelector('code').textContent;
      function done(ok) {
        if (!ok) return;
        btn.classList.add('is-copied');
        btn.setAttribute('aria-label', 'Copied');
        clearTimeout(timer);
        timer = setTimeout(function () {
          btn.classList.remove('is-copied');
          btn.setAttribute('aria-label', base);
        }, 2000);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(fallbackCopy(text)); });
      } else {
        done(fallbackCopy(text));
      }
    });
  });

  // Mobile menu -----------------------------------------------------------
  var header = document.getElementById('nav');
  var menu = document.getElementById('menu-btn');
  function setMenu(open) {
    header.classList.toggle('open', open);
    menu.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  menu.addEventListener('click', function () { setMenu(!header.classList.contains('open')); });
  document.getElementById('site-nav').addEventListener('click', function (e) {
    if (e.target.closest('a')) setMenu(false);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && header.classList.contains('open')) { setMenu(false); menu.focus(); }
  });

  // Reveal on scroll, section highlight ----------------------------------
  var reveal = Array.prototype.slice.call(document.querySelectorAll('.rv'));
  if ('IntersectionObserver' in window && !reduce) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); }
      });
    }, { threshold: 0.08, rootMargin: '0px 0px -5% 0px' });
    reveal.forEach(function (el) { io.observe(el); });
  } else {
    reveal.forEach(function (el) { el.classList.add('in'); });
  }

  if ('IntersectionObserver' in window) {
    var links = {};
    document.querySelectorAll('#site-nav a').forEach(function (a) { links[a.getAttribute('href').slice(1)] = a; });
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        Object.keys(links).forEach(function (k) { links[k].removeAttribute('aria-current'); });
        if (links[en.target.id]) links[en.target.id].setAttribute('aria-current', 'true');
      });
    }, { rootMargin: '-45% 0px -50% 0px' });
    document.querySelectorAll('main section[id]').forEach(function (s) { spy.observe(s); });
  }

  // Hero parallax (transform only) ---------------------------------------
  var par = document.getElementById('parallax');
  if (par && !reduce) {
    var ticking = false;
    window.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(function () {
        var y = Math.min(window.scrollY, 900);
        par.style.transform = 'translate3d(0,' + (y * 0.07).toFixed(1) + 'px,0)';
        ticking = false;
      });
    }, { passive: true });
  }
})();
