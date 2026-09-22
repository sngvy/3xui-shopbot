/* =============================================================================
   Material 3 Expressive — индикатор загрузки
   Семь фигур (soft burst, 9-угольное «печенье», пятиугольник, капсула,
   4-лепестковая, овал, 7-угольная) генерируются с одинаковым числом точек,
   поэтому браузер плавно интерполирует одну в другую. Нормализация по
   площади удерживает оптический вес постоянным на всём морфинге.

   Скрипт также сам заменяет спиннеры Bootstrap (.spinner-border) и
   Font Awesome (.fa-spinner) — включая те, что страницы вставляют
   динамически через innerHTML.
   ========================================================================== */
(function (global) {
  'use strict';

  function m3LoaderPaths() {
    var N = 72, TAU = Math.PI * 2;
    function map(f) { var a = [], i; for (i = 0; i < N; i++) { a.push(f(i / N * TAU)); } return a; }
    function cookie(n, amp, ph) {
      return map(function (t) { var r = 1 + amp * Math.cos(n * (t - ph)); return [r * Math.cos(t), r * Math.sin(t)]; });
    }
    function oval(a, b) { return map(function (t) { return [a * Math.cos(t), b * Math.sin(t)]; }); }
    function pill(A, B) {
      var h = A - B;
      return map(function (t) {
        var cx = Math.cos(t), cy = Math.sin(t), lo = 0, hi = 4, m, x, y, px, k;
        for (k = 0; k < 32; k++) {
          m = (lo + hi) / 2; px = cx * m; y = cy * m; x = Math.min(Math.max(px, -h), h);
          if (Math.sqrt((px - x) * (px - x) + y * y) <= B) { lo = m; } else { hi = m; }
        }
        return [cx * lo, cy * lo];
      });
    }
    function poly(n, ph, sm) {
      var seg = TAU / n, i, j, k = [], s = 0, o = [], p = map(function (t) {
        var phi = ((t - ph) % seg + seg) % seg - seg / 2,
            r = Math.cos(Math.PI / n) / Math.cos(phi);
        return [r * Math.cos(t), r * Math.sin(t)];
      });
      for (i = -sm; i <= sm; i++) { var w = Math.exp(-Math.pow(i / sm * 2, 2)); k.push(w); s += w; }
      for (i = 0; i < N; i++) {
        var ax = 0, ay = 0;
        for (j = -sm; j <= sm; j++) {
          var q = p[(i + j + N * 2) % N], w2 = k[j + sm];
          ax += q[0] * w2; ay += q[1] * w2;
        }
        o.push([ax / s, ay / s]);
      }
      return o;
    }

    var S = [cookie(10, .14, 0), cookie(9, .16, .3), poly(5, -Math.PI / 2, 4),
             pill(1.35, .72), cookie(4, .3, .5), oval(1.22, .86), cookie(7, .21, .2)],
        i, j, max = 0;

    for (i = 0; i < S.length; i++) {
      var p = S[i], a = 0;
      for (j = 0; j < N; j++) { var q = p[j], r = p[(j + 1) % N]; a += q[0] * r[1] - r[0] * q[1]; }
      var sc = Math.sqrt(Math.PI / Math.abs(a / 2));
      for (j = 0; j < N; j++) {
        p[j][0] *= sc; p[j][1] *= sc;
        var d = Math.sqrt(p[j][0] * p[j][0] + p[j][1] * p[j][1]);
        if (d > max) { max = d; }
      }
    }
    var g = 23 / max;
    return S.map(function (p) {
      var d = 'M', j2;
      for (j2 = 0; j2 < N; j2++) {
        d += (j2 ? 'L' : '') + (24 + p[j2][0] * g).toFixed(1) + ' ' + (24 + p[j2][1] * g).toFixed(1);
      }
      return d + 'Z';
    });
  }

  var CACHE = null;
  function paths() {
    if (!CACHE) { try { CACHE = m3LoaderPaths(); } catch (e) { CACHE = []; } }
    return CACHE;
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';

  /* Создаёт готовый <svg> с анимацией морфинга. */
  function createLoader(cls) {
    var P = paths();
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 48 48');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'm3-loader ' + (cls || ''));

    var path = document.createElementNS(SVG_NS, 'path');
    if (P.length) { path.setAttribute('d', P[0]); }
    svg.appendChild(path);

    if (P.length) {
      var keys = [], splines = [], n = P.length, i;
      for (i = 0; i <= n; i++) { keys.push((i / n).toFixed(4)); }
      for (i = 0; i < n; i++) { splines.push('0.4 0 0.2 1'); }

      var anim = document.createElementNS(SVG_NS, 'animate');
      anim.setAttribute('attributeName', 'd');
      anim.setAttribute('dur', '4.9s');
      anim.setAttribute('repeatCount', 'indefinite');
      anim.setAttribute('calcMode', 'spline');
      anim.setAttribute('keyTimes', keys.join(';'));
      anim.setAttribute('keySplines', splines.join(';'));
      anim.setAttribute('values', P.concat([P[0]]).join(';'));
      path.appendChild(anim);
    }

    if (global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      if (svg.pauseAnimations) { svg.pauseAnimations(); }
    }
    return svg;
  }

  /* Заменяет один спиннер на морфирующую фигуру. */
  function upgrade(el) {
    if (!el || el.getAttribute('data-m3-done')) { return; }
    el.setAttribute('data-m3-done', '1');
    var inline = el.classList.contains('spinner-border-sm') ||
                 el.tagName.toLowerCase() === 'i' ||
                 el.classList.contains('fa-spinner');
    var svg = createLoader(inline ? 'm3-loader-inline' : '');
    if (el.parentNode) { el.parentNode.replaceChild(svg, el); }
  }

  var SEL = '.spinner-border, .fa-spinner, .m3-loader-slot';

  function upgradeAll(root) {
    var scope = root || document;
    if (!scope.querySelectorAll) { return; }
    var list = scope.querySelectorAll(SEL);
    for (var i = 0; i < list.length; i++) { upgrade(list[i]); }
    /* сам корень тоже может быть спиннером */
    if (scope.matches && scope.matches(SEL)) { upgrade(scope); }
  }

  /* Страницы монитора подставляют спиннеры через innerHTML уже после
     загрузки — следим за деревом и подменяем их на лету. */
  function observe() {
    if (!global.MutationObserver || !document.body) { return; }
    var mo = new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) {
          if (added[j].nodeType === 1) { upgradeAll(added[j]); }
        }
      }
    });
    mo.observe(document.body, { childList: true, subtree: true });
  }

  function init() {
    upgradeAll(document);
    observe();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  global.M3Loader = {
    create: createLoader,
    upgradeAll: upgradeAll,
    paths: paths
  };
})(window);
