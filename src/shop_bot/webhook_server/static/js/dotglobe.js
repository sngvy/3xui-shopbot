/* Фон панели: вращающийся глобус из точек с пунктирными дугами.
   Цвета берутся из CSS-переменных --ln и --ln2 элемента .bg, поэтому
   следуют теме панели (data-bs-theme). При переключении темы цвета
   перечитываются, перезапускать глобус не нужно. */
(function(){
  'use strict';
  var c = document.getElementById('gl');
  if (!c) return;
  var x = c.getContext('2d');
  var bg = document.querySelector('.bg');
  var ln = '#5B27F5', ln2 = '#D9006C';
  var W, H, Rr, dpr = Math.min(window.devicePixelRatio || 1, 2);

  function readColors(){
    try {
      var cs = getComputedStyle(bg);
      ln = cs.getPropertyValue('--ln').trim() || ln;
      ln2 = cs.getPropertyValue('--ln2').trim() || ln2;
    } catch (e) {}
  }
  readColors();

  function size(){
    W = window.innerWidth; H = window.innerHeight;
    c.width = W * dpr; c.height = H * dpr;
    c.style.width = W + 'px'; c.style.height = H + 'px';
    x.setTransform(dpr, 0, 0, dpr, 0, 0);
    Rr = Math.min(Math.max(W * .62, H * .3), 560);
  }
  size();
  window.addEventListener('resize', size);

  /* Точки на сфере (спираль Фибоначчи) */
  var P = [], n = 1100, i, g = Math.PI * (3 - Math.sqrt(5));
  for (i = 0; i < n; i++) {
    var y = 1 - 2 * (i + .5) / n, r = Math.sqrt(1 - y * y), t = g * i;
    P.push([Math.cos(t) * r, y, Math.sin(t) * r]);
  }
  /* Узлы (широта, долгота) и рёбра между ними */
  var C = [[.7,.3],[.2,2.2],[-.4,1.1],[.5,-1.2],[-.3,-2.4],[.1,0],[.85,-.2]],
      E = [[0,1],[1,2],[2,4],[3,0],[5,3],[6,3],[4,5],[0,6]];
  function ll(la, lo){ return [Math.cos(la) * Math.cos(lo), Math.sin(la), Math.cos(la) * Math.sin(lo)]; }

  var tilt = .42, rot = 0, T = 0;
  function pr(p, rot){
    var cy = Math.cos(rot), sy = Math.sin(rot),
        X = p[0] * cy + p[2] * sy, Z = -p[0] * sy + p[2] * cy,
        ct = Math.cos(tilt), st = Math.sin(tilt),
        Y = p[1] * ct - Z * st, Z2 = p[1] * st + Z * ct;
    return [X, Y, Z2];
  }

  function frame(){
    x.clearRect(0, 0, W, H);
    var cx = W * .5, cy = H * .44, k;
    x.fillStyle = ln;
    for (i = 0; i < n; i++) {
      var q = pr(P[i], rot), a = q[2] > 0 ? .4 + .6 * q[2] : .12;
      x.globalAlpha = a;
      x.beginPath();
      x.arc(cx + q[0] * Rr, cy - q[1] * Rr, q[2] > 0 ? 2.1 : 1.2, 0, 7);
      x.fill();
    }
    x.globalAlpha = .9; x.strokeStyle = ln2; x.lineWidth = 2;
    x.setLineDash([1, 7]); x.lineCap = 'round';
    E.forEach(function(e, ei){
      var a = ll(C[e[0]][0], C[e[0]][1]), b = ll(C[e[1]][0], C[e[1]][1]),
          d = Math.acos(Math.min(1, a[0]*b[0] + a[1]*b[1] + a[2]*b[2])), pts = [];
      for (k = 0; k <= 40; k++) {
        var t = k / 40, s1 = Math.sin((1 - t) * d) / Math.sin(d), s2 = Math.sin(t * d) / Math.sin(d),
            h = 1 + .35 * Math.sin(Math.PI * t) * Math.min(1, d),
            p = [(a[0]*s1 + b[0]*s2) * h, (a[1]*s1 + b[1]*s2) * h, (a[2]*s1 + b[2]*s2) * h];
        pts.push(pr(p, rot));
      }
      x.beginPath();
      var on = false;
      pts.forEach(function(q, m){
        var px = cx + q[0] * Rr, py = cy - q[1] * Rr;
        if (q[2] > -.1) { on ? x.lineTo(px, py) : x.moveTo(px, py); on = true; } else on = false;
      });
      x.stroke();
      /* Бегущая точка по дуге */
      var tt = ((T * .12 + ei * .37) % 1), m = Math.floor(tt * 40), q = pts[m];
      if (q && q[2] > -.1) {
        x.setLineDash([]); x.globalAlpha = 1; x.fillStyle = ln2;
        x.beginPath(); x.arc(cx + q[0] * Rr, cy - q[1] * Rr, 5, 0, 7); x.fill();
        x.fillStyle = ln; x.setLineDash([1, 7]); x.globalAlpha = .9;
      }
    });
    x.setLineDash([]);
    C.forEach(function(p){
      var q = pr(ll(p[0], p[1]), rot);
      if (q[2] > 0) {
        x.globalAlpha = 1; x.fillStyle = ln2;
        x.beginPath(); x.arc(cx + q[0] * Rr, cy - q[1] * Rr, 6.5, 0, 7); x.fill();
        x.fillStyle = ln;
      }
    });
    x.globalAlpha = 1;
  }

  /* Смена темы: перечитываем цвета и перерисовываем (в режиме без анимации
     кадр иначе остался бы старым цветом) */
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  try {
    new MutationObserver(function(){ readColors(); if (reduce) frame(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme'] });
  } catch (e) {}

  function loop(){ T += .004; rot += .000875; frame(); requestAnimationFrame(loop); }
  if (reduce) { rot = .6; frame(); } else loop();
})();
