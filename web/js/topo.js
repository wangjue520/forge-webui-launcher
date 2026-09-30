/* 等高线地形生成器（终末地主界面的地形图元素）
 * 值噪声 + marching squares 画到 canvas 上。开屏动画和主界面背景共用：
 * 同一次启动用同一个种子，按 canvas 在窗口里的位置取样，所以开屏撤场后
 * 主界面背景里的地形跟开屏那张是连在一起的。
 *
 *   Topo.draw(canvas, { alpha: 1 })   // alpha 整体淡化倍率
 */
(function () {
  var SEED = (Date.now() & 0xffff) || 1;
  var G = 64, lat = null;

  function buildLattice() {
    var seed = SEED;
    function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
    lat = new Float32Array(G * G);
    for (var i = 0; i < lat.length; i++) lat[i] = rnd();
  }
  function sm(t) { return t * t * (3 - 2 * t); }
  function L(a, b) { return lat[(a & 63) + (b & 63) * G]; }
  function vnoise(x, y) {
    var xi = Math.floor(x), yi = Math.floor(y), xf = sm(x - xi), yf = sm(y - yi);
    var a = L(xi, yi), b = L(xi + 1, yi), c = L(xi, yi + 1), d = L(xi + 1, yi + 1);
    return a + (b - a) * xf + (c - a) * yf + (a - b - c + d) * xf * yf;
  }
  function field(x, y) {
    return vnoise(x / 300, y / 300) + 0.5 * vnoise(x / 140 + 17, y / 140 + 9) + 0.22 * vnoise(x / 60 + 3, y / 60 + 41);
  }
  // 用整个窗口的取值范围定等高线高度，不同大小的 canvas 线条才能对得上
  var LO = 0.15, HI = 1.55, LEVELS = 16;

  function draw(cv, opts) {
    if (!cv || !cv.getContext) return;
    opts = opts || {};
    if (!lat) buildLattice();
    var alpha = opts.alpha == null ? 1 : opts.alpha;
    var r = cv.getBoundingClientRect();
    var W = Math.max(1, Math.round(r.width || window.innerWidth));
    var H = Math.max(1, Math.round(r.height || window.innerHeight));
    var ox = r.left || 0, oy = r.top || 0;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    var cell = 9, nx = Math.ceil(W / cell) + 1, ny = Math.ceil(H / cell) + 1;
    var f = new Float32Array(nx * ny);
    for (var y = 0; y < ny; y++) for (var x = 0; x < nx; x++) f[x + y * nx] = field(x * cell + ox, y * cell + oy);

    for (var k = 1; k <= LEVELS; k++) {
      var lv = LO + (HI - LO) * k / (LEVELS + 1);
      var major = k % 4 === 0, top = k >= LEVELS - 1;
      ctx.beginPath();
      for (var cy = 0; cy < ny - 1; cy++) for (var cx = 0; cx < nx - 1; cx++) {
        var a = f[cx + cy * nx], b = f[cx + 1 + cy * nx], c = f[cx + 1 + (cy + 1) * nx], d = f[cx + (cy + 1) * nx];
        var idx = (a > lv ? 8 : 0) | (b > lv ? 4 : 0) | (c > lv ? 2 : 0) | (d > lv ? 1 : 0);
        if (idx === 0 || idx === 15) continue;
        var X = cx * cell, Y = cy * cell;
        var pT = [X + cell * (lv - a) / (b - a), Y];
        var pR = [X + cell, Y + cell * (lv - b) / (c - b)];
        var pB = [X + cell * (lv - d) / (c - d), Y + cell];
        var pL = [X, Y + cell * (lv - a) / (d - a)];
        var segs;
        switch (idx) {
          case 1: case 14: segs = [pL, pB]; break;
          case 2: case 13: segs = [pB, pR]; break;
          case 3: case 12: segs = [pL, pR]; break;
          case 4: case 11: segs = [pT, pR]; break;
          case 6: case 9:  segs = [pT, pB]; break;
          case 7: case 8:  segs = [pL, pT]; break;
          case 5:  segs = [pL, pT, pB, pR]; break;
          case 10: segs = [pL, pB, pT, pR]; break;
        }
        for (var s = 0; s < segs.length; s += 2) {
          ctx.moveTo(segs[s][0], segs[s][1]); ctx.lineTo(segs[s + 1][0], segs[s + 1][1]);
        }
      }
      ctx.setLineDash(top ? [3, 3] : []);
      ctx.lineWidth = major ? 1.15 : 0.7;
      ctx.strokeStyle = "rgba(18,19,22," + ((top ? .5 : major ? .26 : .12) * alpha) + ")";
      ctx.stroke();
    }
  }

  window.Topo = { draw: draw };
})();
