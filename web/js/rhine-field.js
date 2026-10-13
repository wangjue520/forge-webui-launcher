/* rhine-field.js — 「莱茵 · 分析终端」的三维档案阵列（手写 WebGL2 实例化渲染，不依赖任何库，离线可用）
 *
 *  场景：一望无际的奶白色档案板，一列列竖着插在地上，高低起伏；波峰沿队列缓慢传递。
 *        每个页面 = 中间那一列里的一份档案；选中的档案升起，侧面亮出一条黄铜书脊。
 *  光照：半球环境光 + 一盏暖色主光；侧面越靠近地面越暗（接触阴影），
 *        前一块板比自己高时整体压暗（假投影）；远处雾化进纸色，像无限延伸。
 *  相机：detail（读档：档案出现在中间装饰区）/ overview（装饰全屏：相机在档案之间滑行）；
 *        拖拽可以绕着转，松手后慢慢回正。用裁剪空间平移把焦点放到屏幕任意 x 上。
 *  接口：window.RhineField(canvas) → { select, setMode, setFocus, setFiles, anchor, plateQuad, drag, pointer, peek,
 *        intro, setLow, resize, destroy }；plateQuad() 给出选中档案正面「标签区」四个角的屏幕坐标，
 *        rhine.js 用它把档案信息（HTML）透视贴到板面上
 *        WebGL2 不可用时返回 null（rhine.js 会退回纯纸面背景）。
 */
(function () {
  "use strict";

  var VS = [
    "#version 300 es",
    "layout(location=0) in vec3 aPos;",
    "layout(location=1) in vec3 aNrm;",
    "layout(location=2) in vec4 iA;",   // x, z, height, width(x)
    "layout(location=3) in vec4 iB;",   // thick(z), shade, kind, lift(y)
    "uniform mat4 uVP; uniform float uShift;",
    "out vec3 vN; out vec3 vW; out vec3 vL; flat out vec4 vB; out vec2 vS;",
    "void main(){",
    "  vec3 p = vec3(iA.x + aPos.x*iA.w, iB.w + aPos.y*iA.z, iA.y + aPos.z*iB.x);",
    "  vW = p; vN = aNrm; vL = aPos; vB = iB; vS = vec2(iA.w, iB.x);",
    "  gl_Position = uVP*vec4(p,1.0);",
    "  gl_Position.x += uShift*gl_Position.w;",
    "}"
  ].join("\n");

  var FS = [
    "#version 300 es",
    "precision highp float;",
    "in vec3 vN; in vec3 vW; in vec3 vL; flat in vec4 vB; in vec2 vS;",
    "uniform vec3 uEye; uniform vec3 uLight; uniform vec3 uFog; uniform float uFogD; uniform float uFocus;",
    "uniform vec3 uFocusP;",
    "out vec4 o;",
    "void main(){",
    "  vec3 n = normalize(vN);",
    "  float kind = vB.z;",
    "  vec3 base = vec3(0.958, 0.940, 0.905);",
    "  if (kind > 1.5) base = vec3(0.905, 0.760, 0.470);",          // 黄铜书脊
    "  else if (kind > 0.5) base = vec3(0.962, 0.950, 0.925);",     // 档案列：稍亮
    "  vec3 L = normalize(uLight);",
    "  float dif = max(dot(n, L), 0.0);",
    "  float hemi = mix(0.70, 1.0, n.y*0.5 + 0.5);",
    "  float ao = 1.0;",
    "  if (n.y < 0.5) ao = mix(0.50, 1.0, pow(smoothstep(0.0, 0.95, vL.y), 0.8));",   // 越近地面越暗
    "  float shade = 1.0 - 0.22*vB.y;",
    "  vec3 col = base*(0.70*hemi + 0.38*dif*vec3(1.0, 0.94, 0.84))*ao*shade;",
    // 顶面边缘一圈细亮线：像注塑件倒角上的高光
    "  if (n.y > 0.5) {",
    "    vec2 e = (0.5 - abs(vL.xz))*vS;",
    "    float d = min(e.x, e.y);",
    "    col += (1.0 - smoothstep(0.0, 0.03, d))*0.07;",
    "  }",
    "  vec3 V = normalize(uEye - vW);",
    "  vec3 H = normalize(L + V);",
    "  col += pow(max(dot(n, H), 0.0), 40.0)*(kind > 1.5 ? 0.35 : 0.06);",
    "  if (kind > 1.5) col += vec3(0.06, 0.04, 0.0);",
    "  float dist = length(vW - uEye);",
    "  float f = 1.0 - exp(-pow(dist*uFogD, 2.0));",
    // 景深：离焦点越远越往纸色里化（真正的模糊由 CSS 叠层做）
    "  float fd = length(vW.xz - uFocusP.xz);",
    "  f = max(f, smoothstep(uFocus, uFocus*3.6, fd)*0.32);",
    "  col = mix(col, uFog, clamp(f, 0.0, 1.0));",
    "  o = vec4(col, 1.0);",
    "}"
  ].join("\n");

  /* ---------- 小型矩阵工具（列主序） ---------- */
  function persp(fovy, asp, n, f) {
    var t = 1 / Math.tan(fovy / 2), nf = 1 / (n - f);
    return [t / asp, 0, 0, 0, 0, t, 0, 0, 0, 0, (f + n) * nf, -1, 0, 0, 2 * f * n * nf, 0];
  }
  function lookAt(e, c, u) {
    var zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2], l = Math.hypot(zx, zy, zz);
    zx /= l; zy /= l; zz /= l;
    var xx = u[1] * zz - u[2] * zy, xy = u[2] * zx - u[0] * zz, xz = u[0] * zy - u[1] * zx;
    l = Math.hypot(xx, xy, xz); xx /= l; xy /= l; xz /= l;
    var yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
    return [xx, yx, zx, 0, xy, yy, zy, 0, xz, yz, zz, 0,
      -(xx * e[0] + xy * e[1] + xz * e[2]), -(yx * e[0] + yy * e[1] + yz * e[2]), -(zx * e[0] + zy * e[1] + zz * e[2]), 1];
  }
  function mul(a, b) {
    var o = new Array(16);
    for (var c = 0; c < 4; c++) for (var r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
    return o;
  }

  function boxGeo() {
    // 单位盒：x/z ∈ [-0.5, 0.5]，y ∈ [0, 1]（底面不画，看不到）
    var F = [
      [[0, 1, 0], [[-.5, 1, -.5], [.5, 1, -.5], [.5, 1, .5], [-.5, 1, .5]]],
      [[0, 0, 1], [[-.5, 0, .5], [.5, 0, .5], [.5, 1, .5], [-.5, 1, .5]]],
      [[0, 0, -1], [[.5, 0, -.5], [-.5, 0, -.5], [-.5, 1, -.5], [.5, 1, -.5]]],
      [[1, 0, 0], [[.5, 0, .5], [.5, 0, -.5], [.5, 1, -.5], [.5, 1, .5]]],
      [[-1, 0, 0], [[-.5, 0, -.5], [-.5, 0, .5], [-.5, 1, .5], [-.5, 1, -.5]]],
    ];
    var v = [], idx = [];
    F.forEach(function (f, i) {
      f[1].forEach(function (p) { v.push(p[0], p[1], p[2], f[0][0], f[0][1], f[0][2]); });
      var b = i * 4; idx.push(b, b + 1, b + 2, b, b + 2, b + 3);
    });
    return { v: new Float32Array(v), i: new Uint16Array(idx) };
  }

  window.RhineField = function (canvas) {
    var gl = canvas.getContext("webgl2", { antialias: true, alpha: false, powerPreference: "high-performance" });
    if (!gl) return null;
    function sh(type, src) {
      var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    }
    var prog = gl.createProgram();
    try {
      gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS));
      gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    } catch (e) { console.warn("[rhine] WebGL 着色器失败，退回纸面背景", e); return null; }
    var U = {};
    ["uVP", "uShift", "uEye", "uLight", "uFog", "uFogD", "uFocus", "uFocusP"].forEach(function (k) { U[k] = gl.getUniformLocation(prog, k); });

    /* ---------- 阵列布局 ---------- */
    var C = 45, R = 76, PX = 1.04, PZ = 0.28, W = 0.95, T = 0.215;
    var MID = (C / 2) | 0, ROW0 = 26, ROWSTEP = 2;     // 档案列 = 中间那列；第 i 份在 ROW0 + i*ROWSTEP 行
    var N = C * R, NI = N + 1;                         // +1 = 黄铜书脊
    var inst = new Float32Array(NI * 8);
    var hBase = new Float32Array(N), hCur = new Float32Array(N), lift = new Float32Array(N), liftT = new Float32Array(N);
    for (var c = 0; c < C; c++) for (var r = 0; r < R; r++) {
      var k = c * R + r;
      // 平滑起伏 + 轻微台阶感
      var h = .7 + .45 * Math.sin(c * .31 + r * .07) * Math.cos(r * .06 - c * .17) + .16 * Math.sin(c * .9 - r * .21);
      h += c === MID ? .18 : 0;
      hBase[k] = Math.max(.25, Math.round(h * 20) / 20);
      hCur[k] = 0;
    }

    var geo = boxGeo();
    var vao = gl.createVertexArray(); gl.bindVertexArray(vao);
    var vb = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, vb); gl.bufferData(gl.ARRAY_BUFFER, geo.v, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 24, 0);
    gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 3, gl.FLOAT, false, 24, 12);
    var ib = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, geo.i, gl.STATIC_DRAW);
    var ibuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, ibuf); gl.bufferData(gl.ARRAY_BUFFER, inst.byteLength, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(2); gl.vertexAttribPointer(2, 4, gl.FLOAT, false, 32, 0); gl.vertexAttribDivisor(2, 1);
    gl.enableVertexAttribArray(3); gl.vertexAttribPointer(3, 4, gl.FLOAT, false, 32, 16); gl.vertexAttribDivisor(3, 1);
    gl.bindVertexArray(null);

    /* ---------- 状态 ---------- */
    var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var files = 10, sel = 0, mode = "detail", focusX = .5;
    var waves = [];                          // { c, r, t0, amp }
    var cam = { az: .62, el: .44, dist: 6.6, tx: 0, ty: 1, tz: 0, shift: 0 };
    var camT = { az: .62, el: .44, dist: 6.6 };
    var dragYaw = 0, dragPitch = 0, dragging = false, lastDrag = 0;
    var parX = 0, parY = 0, parXT = 0, parYT = 0, peekI = -1;
    var introT = -1, rise = 1;
    var low = false, paused = false, frameN = 0, raf = 0, alive = true;
    var VPm = null, eye = [0, 0, 0], dpr = 1, Wd = 1, Hd = 1;

    function rowOf(i) { return Math.min(R - 3, ROW0 + i * ROWSTEP); }
    function keyOf(i) { return MID * R + rowOf(i); }
    function posOf(i) { return [(MID - C / 2 + .5) * PX, (rowOf(i) - R / 2 + .5) * PZ]; }

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, low ? 1 : 1.5);
      Wd = canvas.clientWidth || window.innerWidth; Hd = canvas.clientHeight || window.innerHeight;
      canvas.width = Math.round(Wd * dpr); canvas.height = Math.round(Hd * dpr);
    }
    resize();

    function setMode(m) {
      mode = m;
      if (m === "overview") { camT.az = .78; camT.el = .42; camT.dist = 9.2; }
      else { camT.az = .7; camT.el = .46; camT.dist = 10.5; }
      for (var i = 0; i < files; i++) liftT[keyOf(i)] = 0;
      liftT[keyOf(sel)] = 1.7;
    }
    function select(i, wave) {
      var prev = sel;
      sel = Math.max(0, Math.min(files - 1, i));
      liftT[keyOf(prev)] = 0;
      liftT[keyOf(sel)] = 1.7;
      if (wave !== false && !reduce) waves.push({ c: MID, r: rowOf(sel), t0: performance.now(), amp: .55 });
      if (waves.length > 4) waves.shift();
    }
    setMode("detail"); select(0, false);

    var t0 = performance.now();
    function frame(now) {
      if (!alive) return;
      raf = requestAnimationFrame(frame);
      if (document.hidden || paused) return;
      if (low && (frameN++ & 1)) return;   // 内容铺满时半帧率
      var t = (now - t0) / 1000;

      // 开场：高度从 0 涨起来（从选中档案向外扩散）
      if (introT >= 0) { rise = Math.min(1, (now - introT) / 2200); if (rise >= 1) introT = -1; }
      var p0 = posOf(sel);

      // 高度 / 抬升 / 投影
      var i, c, r, k, j;
      for (c = 0; c < C; c++) for (r = 0; r < R; r++) {
        k = c * R + r;
        var x = (c - C / 2 + .5) * PX, z = (r - R / 2 + .5) * PZ;
        var amb = reduce ? 0 : .09 * Math.sin(r * .34 - t * 1.1 + c * .52) + .05 * Math.sin(c * .8 + t * .7);
        var wv = 0;
        for (j = 0; j < waves.length; j++) {
          var w = waves[j], age = (now - w.t0) / 1000, dd = Math.hypot((c - w.c) * PX, (r - w.r) * PZ * 1.6);
          var front = age * 7.5;
          wv += w.amp * Math.exp(-Math.pow(dd - front, 2) / 1.6) * Math.exp(-age * .55);
        }
        var target = hBase[k] + amb + wv;
        if (introT >= 0 || rise < 1) {
          var dd0 = Math.hypot(x - p0[0], z - p0[1]);
          var local = Math.max(0, Math.min(1, rise * 2.2 - dd0 / 14));
          target *= local * local * (3 - 2 * local);
        }
        hCur[k] += (target - hCur[k]) * .14;
        lift[k] += (liftT[k] - lift[k]) * .075;
      }
      for (c = 0; c < C; c++) for (r = 0; r < R; r++) {
        k = c * R + r;
        var hh = hCur[k] + lift[k];
        var front2 = r + 1 < R ? hCur[k + 1] + lift[k + 1] : 0;   // 光从 +z 来：前一块板挡光
        var side = c + 1 < C ? hCur[k + R] + lift[k + R] : 0;
        var shade = Math.max(0, Math.min(1, (front2 - hh) * 1.4)) * .8 + Math.max(0, Math.min(1, (side - hh) * .9)) * .35;
        j = k * 8;
        inst[j] = (c - C / 2 + .5) * PX; inst[j + 1] = (r - R / 2 + .5) * PZ;
        inst[j + 2] = Math.max(.001, hh); inst[j + 3] = W;
        inst[j + 4] = T; inst[j + 5] = Math.min(1, shade);
        inst[j + 6] = (c === MID && r >= ROW0 && r < ROW0 + files * ROWSTEP && (r - ROW0) % ROWSTEP === 0) ? 1 : 0;
        inst[j + 7] = 0;
      }
      // 黄铜书脊：贴在选中档案的 +x 端面上
      k = keyOf(sel); j = N * 8;
      var hs = hCur[k] + lift[k];
      inst[j] = p0[0] + W / 2 + .012; inst[j + 1] = p0[1];
      inst[j + 2] = Math.max(.001, hs * .94); inst[j + 3] = .03;
      inst[j + 4] = T * 1.04; inst[j + 5] = 0; inst[j + 6] = 2; inst[j + 7] = hs * .03;

      // 相机：阻尼跟随
      if (!dragging && now - lastDrag > 900) { dragYaw *= .96; dragPitch *= .96; }
      var e = .055;
      parX += (parXT - parX) * .04; parY += (parYT - parY) * .04;
      cam.az += (camT.az + dragYaw + parX * .09 - cam.az) * e;
      cam.el += (Math.max(.12, Math.min(1.3, camT.el + dragPitch + parY * .05)) - cam.el) * e;
      var distT = camT.dist;
      if (introT >= 0 || rise < 1) distT += (1 - rise) * 16;
      cam.dist += (distT - cam.dist) * e;
      var tyT = Math.max(1.1, hs * .62);
      cam.tx += (p0[0] - cam.tx) * e; cam.tz += (p0[1] - cam.tz) * e; cam.ty += (tyT - cam.ty) * e;
      var sx = (focusX * 2 - 1); cam.shift += (sx - cam.shift) * .08;

      eye = [cam.tx + cam.dist * Math.cos(cam.el) * Math.sin(cam.az),
             cam.ty + cam.dist * Math.sin(cam.el),
             cam.tz + cam.dist * Math.cos(cam.el) * Math.cos(cam.az)];
      VPm = mul(persp(28 * Math.PI / 180, Wd / Hd, .1, 120), lookAt(eye, [cam.tx, cam.ty, cam.tz], [0, 1, 0]));

      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(.909, .898, .882, 1);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST); gl.enable(gl.CULL_FACE);
      gl.useProgram(prog);
      gl.uniformMatrix4fv(U.uVP, false, VPm);
      gl.uniform1f(U.uShift, cam.shift);
      gl.uniform3fv(U.uEye, eye);
      gl.uniform3f(U.uLight, -.45, .9, .55);
      gl.uniform3f(U.uFog, .909, .898, .882);
      gl.uniform1f(U.uFogD, mode === "overview" ? .042 : .05);
      gl.uniform1f(U.uFocus, mode === "overview" ? 9 : 6);
      gl.uniform3f(U.uFocusP, p0[0], 0, p0[1]);
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, ibuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, inst);
      gl.drawElementsInstanced(gl.TRIANGLES, geo.i.length, gl.UNSIGNED_SHORT, 0, NI);
      gl.bindVertexArray(null);
    }
    raf = requestAnimationFrame(frame);

    // 世界坐标 → 屏幕坐标（含焦点平移）
    function proj(x, y, z) {
      var o = [0, 0, 0, 0];
      for (var r = 0; r < 4; r++) o[r] = VPm[r] * x + VPm[4 + r] * y + VPm[8 + r] * z + VPm[12 + r];
      if (o[3] <= 0) return null;
      return { x: ((o[0] / o[3] + cam.shift) * .5 + .5) * Wd, y: (1 - (o[1] / o[3] * .5 + .5)) * Hd };
    }

    return {
      setFiles: function (n) { files = Math.max(1, Math.min(18, n)); },
      select: select,
      setMode: setMode,
      setFocus: function (fx) { focusX = fx; },
      // 鼠标视差：nx / ny ∈ [-1, 1]
      pointer: function (nx, ny) { if (!reduce) { parXT = nx; parYT = ny; } },
      // 悬停预览：让第 i 份档案微微升起并泛起一圈波纹（-1 = 取消）
      peek: function (i) {
        if (peekI >= 0 && peekI !== sel) liftT[keyOf(peekI)] = 0;
        peekI = i;
        if (i < 0 || i >= files || i === sel) return;
        liftT[keyOf(i)] = mode === "overview" ? .7 : .55;
        if (!reduce) { waves.push({ c: MID, r: rowOf(i), t0: performance.now(), amp: .22 }); if (waves.length > 4) waves.shift(); }
      },
      // 被功能页整块盖住时停画（画面停在最后一帧，揭开时接着动）
      setPaused: function (v) { paused = !!v; },
      // 被功能页整块盖住时停画（画面停在最后一帧，揭开时接着动）
      setPaused: function (v) { paused = !!v; },
      // 被功能页整块盖住时停画（画面停在最后一帧，揭开时接着动）
      setPaused: function (v) { paused = !!v; },
      setLow: function (v) { if (low !== v) { low = v; resize(); } },
      // 选中档案顶面中心的屏幕坐标（给 HTML 叠层的引线、景深焦点用）
      anchor: function () {
        if (!VPm) return null;
        var k = keyOf(sel), p = posOf(sel);
        return proj(p[0], hCur[k] + lift[k], p[1]);
      },
      // 选中档案朝向相机那一面的「标签区」（板子上半截，露在周围档案上面的那部分）
      plateQuad: function () {
        if (!VPm) return null;
        var k = keyOf(sel), p = posOf(sel), top = hCur[k] + lift[k];
        var fz = Math.cos(cam.az) >= 0 ? 1 : -1;          // 相机在 +z 一侧就贴 +z 面
        var z = p[1] + fz * (T / 2 + .003);
        var x0 = p[0] - W / 2 + .025, x1 = p[0] + W / 2 - .025;
        if (fz < 0) { var t = x0; x0 = x1; x1 = t; }
        var LH = Math.max(.5, Math.min(1.62, top - .2)), yt = top - .05, yb = yt - LH;
        var a = proj(x0, yt, z), b = proj(x1, yt, z), c = proj(x0, yb, z), d = proj(x1, yb, z);
        if (!a || !b || !c || !d) return null;
        return { tl: a, tr: b, bl: c, br: d, aspect: LH / Math.abs(x1 - x0) };
      },
      drag: function (dx, dy, on) {
        dragging = !!on; lastDrag = performance.now();
        dragYaw -= dx * .004; dragPitch += dy * .003;
        dragPitch = Math.max(-.3, Math.min(.6, dragPitch));
      },
      intro: function () { introT = performance.now(); rise = 0; cam.dist = camT.dist + 16; },
      resize: resize,
      destroy: function () {
        alive = false; cancelAnimationFrame(raf);
        var ext = gl.getExtension("WEBGL_lose_context"); if (ext) ext.loseContext();
      },
    };
  };
})();
