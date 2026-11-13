/* rhine-field.js — 「莱茵 · 分析终端」的三维档案阵列（手写 WebGL2 实例化渲染，不依赖任何库，离线可用）
 *
 *  场景：一望无际的奶白色档案盒，一列列竖着插在地上，高低起伏。
 *        每个页面 = 中间那一列里的一份档案；选中的档案升起，侧面亮出一条黄铜书脊。
 *  光照：环境光 + 一盏主光 + 选中档案处的一盏暖色点光；侧面越靠近地面越暗（接触阴影），
 *        前一块板比自己高时整体压暗（假投影）；远处雾化进底色，像无限延伸。
 *  明暗：档案盒本身的颜色不变（都是奶白），浅色 / 深色只换「灯」——
 *        浅色 = 明亮的环境光 + 暖主光，点光只是一点暖意；
 *        深色 = 环境光压到很暗的冷色，选中档案自己发亮，暖琥珀点光把周围几块照出来，
 *        其余的沉在深蓝黑里（参考 Rhine Music Database 的深色场景）。setTone() 在两组灯之间渐变，不重建场景。
 *  相机：detail（读档：档案出现在中间装饰区）/ overview（装饰全屏：相机在档案之间滑行）；
 *        拖拽可以绕着转，松手后慢慢回正。用裁剪空间平移把焦点放到屏幕任意 x 上。
 *  性能：按需渲染。没有常驻动画——波纹、抬升、相机、视差、明暗渐变全都静止后就停掉 requestAnimationFrame，
 *        画面留在最后一帧（GPU / CPU 占用归零）；有输入或状态变化时由 wake() 重新开始。
 *        窗口隐藏、被功能页整块盖住（setPaused）时同样不画。启动器要常驻在跑图的机器上，不能和出图抢显卡。
 *  接口：window.RhineField(canvas, { onFrame }) → { select, setMode, setFocus, setFiles, setTone, anchor, plateQuad,
 *        drag, pointer, peek, intro, setLow, setPaused, resize, wake, awake, frames, destroy }；
 *        plateQuad() 给出选中档案正面「标签区」四个角的屏幕坐标，rhine.js 用它把档案信息（HTML）透视贴到板面上；
 *        onFrame 在每画完一帧后调用（rhine.js 在里面更新贴图位置，不用自己再开一个每帧循环）。
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

  // kind：0 = 普通档案盒；1 ~ 1.9 = 档案列里的一份（小数部分 = 抬起的程度，选中的那份接近 1.9）；3 = 黄铜书脊
  var FS = [
    "#version 300 es",
    "precision highp float;",
    "in vec3 vN; in vec3 vW; in vec3 vL; flat in vec4 vB; in vec2 vS;",
    "uniform vec3 uEye; uniform vec3 uLight; uniform vec3 uFog; uniform float uFogD; uniform float uFocus;",
    "uniform vec3 uFocusP;",
    "uniform vec3 uAmb; uniform vec3 uSun; uniform vec3 uGlow; uniform vec3 uGlowP; uniform vec4 uEmis;",
    "out vec4 o;",
    "void main(){",
    "  vec3 n = normalize(vN);",
    "  float kind = vB.z;",
    "  float spine = step(2.5, kind);",
    "  float sel = (1.0 - spine)*clamp((kind - 1.0)/0.9, 0.0, 1.0);",
    "  vec3 alb = vec3(0.962, 0.944, 0.898);",
    "  if (spine > 0.5) alb = vec3(0.905, 0.730, 0.400);",          // 黄铜书脊
    "  else if (kind > 0.5) alb = vec3(0.968, 0.954, 0.918);",      // 档案列：稍亮
    "  vec3 L = normalize(uLight);",
    "  float dif = max(dot(n, L), 0.0);",
    "  float hemi = mix(0.70, 1.0, n.y*0.5 + 0.5);",
    "  float ao = 1.0;",
    "  if (n.y < 0.5) ao = mix(0.50, 1.0, pow(smoothstep(0.0, 0.95, vL.y), 0.8));",   // 越近地面越暗
    "  float shade = 1.0 - 0.22*vB.y;",
    "  vec3 lit = (uAmb*hemi + uSun*dif)*ao*shade;",
    // 选中档案处的暖色点光：平方衰减，朝向它的面更亮
    "  vec3 gv = uGlowP - vW;",
    "  float gd2 = dot(gv, gv);",
    "  float att = 1.0/(1.0 + gd2*0.30);",
    "  float gn = max(dot(n, gv*inversesqrt(gd2 + 0.0001)), 0.0);",
    "  lit += uGlow*att*att*(0.22 + 0.78*gn)*mix(0.55, 1.0, ao);",
    "  vec3 col = alb*lit;",
    // 质感（都是着色器里算出来的，不加几何）：
    //   侧面：两条竖棱和上沿各一道窄窄的倒角受光 / 背光，盒子不再像刀切出来的薄片；
    //   朝外的窄端面（±x）：上方一个凹下去的卡口，像磁带盒的锁扣
    "  if (n.y < 0.5) {",
    "    float u = abs(n.x) > 0.5 ? vL.z : vL.x;",                     // 沿面宽方向 -0.5 ~ 0.5
    "    float wid = abs(n.x) > 0.5 ? vS.y : vS.x;",                   // 这个面的实际宽度
    "    float topd = (1.0 - vL.y)*vW.y/max(vL.y, 0.001);",           // 离上沿多远（世界单位）
    "    float ed = (0.5 - abs(u))*wid;",                              // 离竖棱多远
    "    float bev = (1.0 - smoothstep(0.0, 0.022, ed)) + (1.0 - smoothstep(0.0, 0.02, topd));",
    "    col *= 1.0 + 0.10*min(bev, 1.0);",
    "    if (abs(n.x) > 0.5) {",
    "      vec2 q = vec2(u*wid, topd);",
    "      float notch = smoothstep(0.046, 0.036, abs(q.x))*smoothstep(0.055, 0.065, q.y)*smoothstep(0.165, 0.155, q.y);",
    "      col *= 1.0 - 0.15*notch;",
    "    }",
    "  }",
    // 顶面边缘一圈细亮线：像注塑件倒角上的高光（亮度跟着环境光走，深色里不会变成一圈白边）
    "  if (n.y > 0.5) {",
    "    vec2 e = (0.5 - abs(vL.xz))*vS;",
    "    float d = min(e.x, e.y);",
    "    col += (1.0 - smoothstep(0.0, 0.03, d))*0.1*(uAmb + uGlow*att*att*0.6);",
    "  }",
    "  vec3 V = normalize(uEye - vW);",
    "  vec3 H = normalize(L + V);",
    "  col += pow(max(dot(n, H), 0.0), 40.0)*(spine > 0.5 ? 0.9 : 0.16)*uSun;",
    "  if (spine > 0.5) col += vec3(0.06, 0.04, 0.0)*(uAmb.g + uGlow.g);",
    // 深色：选中的那份自己发亮（uEmis.a = 强度，浅色为 0）
    "  col = mix(col, uEmis.rgb*mix(0.82, 1.0, hemi)*mix(0.72, 1.0, ao), uEmis.a*smoothstep(0.15, 0.9, sel));",
    "  float dist = length(vW - uEye);",
    "  float f = 1.0 - exp(-pow(dist*uFogD, 2.0));",
    // 景深：离焦点越远越往底色里化
    "  float fd = length(vW.xz - uFocusP.xz);",
    "  f = max(f, smoothstep(uFocus, uFocus*3.6, fd)*0.32);",
    "  col = mix(col, uFog, clamp(f, 0.0, 1.0));",
    "  o = vec4(col, 1.0);",
    "}"
  ].join("\n");

  /* ---------- 两组灯：[浅色, 深色]，setTone 在两者之间插值 ----------
     fog 必须和 themes/rhine.css 的 --rh-paper 一致（浅 #f1f0e7 / 深 #181d1c），画布边缘才能化进页面底色 */
  var PAL = {
    fog:  [[0.945, 0.941, 0.906], [0.094, 0.114, 0.110]],
    amb:  [[0.700, 0.698, 0.688], [0.112, 0.132, 0.146]],
    sun:  [[0.380, 0.357, 0.319], [0.045, 0.054, 0.066]],
    glow: [[0.110, 0.088, 0.050], [2.300, 1.560, 0.720]],
    emis: [[1.000, 0.900, 0.690, 0.0], [1.000, 0.895, 0.680, 0.86]],
  };

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

  window.RhineField = function (canvas, opts) {
    opts = opts || {};
    // low-power：双显卡的机器上尽量用核显，把独显（和它的显存）留给出图；单显卡的机器上没有区别
    var gl = canvas.getContext("webgl2", { antialias: true, alpha: false, powerPreference: "low-power" });
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
    ["uVP", "uShift", "uEye", "uLight", "uFog", "uFogD", "uFocus", "uFocusP", "uAmb", "uSun", "uGlow", "uGlowP", "uEmis"]
      .forEach(function (k) { U[k] = gl.getUniformLocation(prog, k); });

    /* ---------- 阵列布局 ---------- */
    // 档案盒比早先的薄板略厚、间隙略大，更像一盒盒磁带
    var C = 45, R = 76, PX = 1.04, PZ = 0.30, W = 0.95, T = 0.238;
    var MID = (C / 2) | 0, ROW0 = 26, ROWSTEP = 2;     // 档案列 = 中间那列；第 i 份在 ROW0 + i*ROWSTEP 行
    var N = C * R, NI = N + 1;                         // +1 = 黄铜书脊
    var LIFT = 1.7;                                    // 选中的档案抬起多高
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
    var tone = 0, toneT = 0;                 // 0 = 浅色，1 = 深色
    var glowP = [0, 1.2, 0], glowInit = false;
    var low = false, paused = false, frameN = 0, raf = 0, alive = true;
    var asleep = true, still = 0, drawn = 0; // 按需渲染：asleep = 当前没有排下一帧
    var VPm = null, eye = [0, 0, 0], dpr = 1, Wd = 1, Hd = 1;

    function rowOf(i) { return Math.min(R - 3, ROW0 + i * ROWSTEP); }
    function keyOf(i) { return MID * R + rowOf(i); }
    function posOf(i) { return [(MID - C / 2 + .5) * PX, (rowOf(i) - R / 2 + .5) * PZ]; }

    // 有东西要动了：没在画就重新开始画
    function wake() {
      still = 0;
      if (!alive || !asleep) return;
      asleep = false;
      raf = requestAnimationFrame(frame);
    }

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, low ? 1 : 1.5);
      Wd = canvas.clientWidth || window.innerWidth; Hd = canvas.clientHeight || window.innerHeight;
      canvas.width = Math.round(Wd * dpr); canvas.height = Math.round(Hd * dpr);
      wake();
    }
    resize();

    function setMode(m) {
      mode = m;
      if (m === "overview") { camT.az = .78; camT.el = .42; camT.dist = 9.2; }
      else { camT.az = .7; camT.el = .46; camT.dist = 10.5; }
      for (var i = 0; i < files; i++) liftT[keyOf(i)] = 0;
      liftT[keyOf(sel)] = LIFT;
      wake();
    }
    function select(i, wave) {
      var prev = sel;
      sel = Math.max(0, Math.min(files - 1, i));
      liftT[keyOf(prev)] = 0;
      liftT[keyOf(sel)] = LIFT;
      if (wave !== false && !reduce) waves.push({ c: MID, r: rowOf(sel), t0: performance.now(), amp: .55 });
      if (waves.length > 4) waves.shift();
      wake();
    }
    setMode("detail"); select(0, false);

    function mix3(name, n) {
      var a = PAL[name][0], b = PAL[name][1], o = [];
      for (var i = 0; i < n; i++) o.push(a[i] + (b[i] - a[i]) * tone);
      return o;
    }

    function frame(now) {
      if (!alive) return;
      // 窗口隐藏 / 被功能页盖住：不画，也不再排下一帧（visibilitychange / setPaused(false) 会 wake）
      if (document.hidden || paused) { asleep = true; return; }
      if (low && (frameN++ & 1)) { raf = requestAnimationFrame(frame); return; }   // 装饰区很窄时半帧率
      var move = 0;   // 这一帧里最大的变化量：连续几帧都几乎没变就停画
      function track(d) { d = Math.abs(d); if (d > move) move = d; }

      // 开场：高度从 0 涨起来（从选中档案向外扩散）
      if (introT >= 0) { rise = Math.min(1, (now - introT) / 2200); if (rise >= 1) introT = -1; move = 1; }
      var p0 = posOf(sel);

      // 波纹：走远、衰减到看不出来就丢掉
      var j;
      for (j = waves.length - 1; j >= 0; j--) if (now - waves[j].t0 > 3200) waves.splice(j, 1);   // 3 秒后波峰早已跑出雾外
      if (waves.length) move = 1;

      // 明暗渐变
      if (tone !== toneT) {
        tone += (toneT - tone) * .09;
        if (Math.abs(toneT - tone) < .002) tone = toneT;
        move = 1;
      }

      // 高度 / 抬升 / 投影
      var i, c, r, k, nw = waves.length;
      var rising = introT >= 0 || rise < 1;
      for (c = 0; c < C; c++) for (r = 0; r < R; r++) {
        k = c * R + r;
        var wv = 0;
        for (j = 0; j < nw; j++) {
          var w = waves[j], age = (now - w.t0) / 1000, dd = Math.hypot((c - w.c) * PX, (r - w.r) * PZ * 1.6);
          var front = age * 7.5;
          wv += w.amp * Math.exp(-Math.pow(dd - front, 2) / 1.6) * Math.exp(-age * .55);
        }
        var target = hBase[k] + wv;
        if (rising) {
          var x = (c - C / 2 + .5) * PX, z = (r - R / 2 + .5) * PZ;
          var dd0 = Math.hypot(x - p0[0], z - p0[1]);
          var local = Math.max(0, Math.min(1, rise * 2.2 - dd0 / 14));
          target *= local * local * (3 - 2 * local);
        }
        var dh = target - hCur[k], dl = liftT[k] - lift[k];
        if (dh > .0008 || dh < -.0008) { hCur[k] += dh * .14; if (dh > move) move = dh; else if (-dh > move) move = -dh; } else hCur[k] = target;
        if (dl > .0008 || dl < -.0008) { lift[k] += dl * .075; if (dl > move) move = dl; else if (-dl > move) move = -dl; } else lift[k] = liftT[k];
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
        // 档案列里的每一份：1 + 抬起程度 × 0.9（着色器据此决定深色里亮不亮）
        inst[j + 6] = (c === MID && r >= ROW0 && r < ROW0 + files * ROWSTEP && (r - ROW0) % ROWSTEP === 0)
          ? 1 + .9 * Math.max(0, Math.min(1, lift[k] / LIFT)) : 0;
        inst[j + 7] = 0;
      }
      // 黄铜书脊：贴在选中档案的 +x 端面上
      k = keyOf(sel); j = N * 8;
      var hs = hCur[k] + lift[k];
      inst[j] = p0[0] + W / 2 + .012; inst[j + 1] = p0[1];
      inst[j + 2] = Math.max(.001, hs * .94); inst[j + 3] = .03;
      inst[j + 4] = T * 1.04; inst[j + 5] = 0; inst[j + 6] = 3; inst[j + 7] = hs * .03;

      // 相机：阻尼跟随
      if (!dragging && now - lastDrag > 900) { dragYaw *= .96; dragPitch *= .96; if (Math.abs(dragYaw) < .0005) dragYaw = 0; if (Math.abs(dragPitch) < .0005) dragPitch = 0; }
      if (dragging || dragYaw || dragPitch) move = 1;   // 松手后那 0.9 秒相机不动，但回正还没做完，不能停
      var e = .055, d;
      d = (parXT - parX) * .04; parX += d; track(d * 4);
      d = (parYT - parY) * .04; parY += d; track(d * 4);
      d = (camT.az + dragYaw + parX * .09 - cam.az) * e; cam.az += d; track(d * 20);
      d = (Math.max(.12, Math.min(1.3, camT.el + dragPitch + parY * .05)) - cam.el) * e; cam.el += d; track(d * 20);
      var distT = camT.dist;
      if (rising) distT += (1 - rise) * 16;
      d = (distT - cam.dist) * e; cam.dist += d; track(d * 2);
      var tyT = Math.max(1.1, hs * .62);
      d = (p0[0] - cam.tx) * e; cam.tx += d; track(d * 2);
      d = (p0[1] - cam.tz) * e; cam.tz += d; track(d * 2);
      d = (tyT - cam.ty) * e; cam.ty += d; track(d * 2);
      var sx = (focusX * 2 - 1); d = (sx - cam.shift) * .08; cam.shift += d; track(d * 12);

      // 点光跟着选中的档案滑过去（切换档案时光沿着队列移动，而不是瞬间跳过去）
      var gT = [p0[0] - .1, Math.max(.9, hs * .58), p0[1] + .42];
      if (!glowInit) { glowP = gT.slice(); glowInit = true; }
      for (i = 0; i < 3; i++) { d = (gT[i] - glowP[i]) * .1; glowP[i] += d; track(d * 2); }

      eye = [cam.tx + cam.dist * Math.cos(cam.el) * Math.sin(cam.az),
             cam.ty + cam.dist * Math.sin(cam.el),
             cam.tz + cam.dist * Math.cos(cam.el) * Math.cos(cam.az)];
      VPm = mul(persp(28 * Math.PI / 180, Wd / Hd, .1, 120), lookAt(eye, [cam.tx, cam.ty, cam.tz], [0, 1, 0]));

      var fog = mix3("fog", 3);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(fog[0], fog[1], fog[2], 1);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.enable(gl.DEPTH_TEST); gl.enable(gl.CULL_FACE);
      gl.useProgram(prog);
      gl.uniformMatrix4fv(U.uVP, false, VPm);
      gl.uniform1f(U.uShift, cam.shift);
      gl.uniform3fv(U.uEye, eye);
      gl.uniform3f(U.uLight, -.45, .9, .55);
      gl.uniform3fv(U.uFog, fog);
      gl.uniform3fv(U.uAmb, mix3("amb", 3));
      gl.uniform3fv(U.uSun, mix3("sun", 3));
      gl.uniform3fv(U.uGlow, mix3("glow", 3));
      gl.uniform3fv(U.uGlowP, glowP);
      gl.uniform4fv(U.uEmis, mix3("emis", 4));
      gl.uniform1f(U.uFogD, (mode === "overview" ? .042 : .05) * (1 + .18 * tone));
      gl.uniform1f(U.uFocus, mode === "overview" ? 9 : 6);
      gl.uniform3f(U.uFocusP, p0[0], 0, p0[1]);
      gl.bindVertexArray(vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, ibuf);
      gl.bufferSubData(gl.ARRAY_BUFFER, 0, inst);
      gl.drawElementsInstanced(gl.TRIANGLES, geo.i.length, gl.UNSIGNED_SHORT, 0, NI);
      gl.bindVertexArray(null);
      drawn++;
      if (opts.onFrame) { try { opts.onFrame(); } catch (err) { console.warn(err); } }

      // 全都停稳了（连续几帧变化量都在千分之几以内）就不再排下一帧
      still = move < .004 ? still + 1 : 0;
      if (still >= 4) { asleep = true; return; }
      raf = requestAnimationFrame(frame);
    }
    asleep = false;
    raf = requestAnimationFrame(frame);

    function onVis() { if (!document.hidden) wake(); }
    document.addEventListener("visibilitychange", onVis);

    // 世界坐标 → 屏幕坐标（含焦点平移）
    function proj(x, y, z) {
      var o = [0, 0, 0, 0];
      for (var r = 0; r < 4; r++) o[r] = VPm[r] * x + VPm[4 + r] * y + VPm[8 + r] * z + VPm[12 + r];
      if (o[3] <= 0) return null;
      return { x: ((o[0] / o[3] + cam.shift) * .5 + .5) * Wd, y: (1 - (o[1] / o[3] * .5 + .5)) * Hd };
    }

    return {
      setFiles: function (n) { files = Math.max(1, Math.min(18, n)); wake(); },
      select: select,
      setMode: setMode,
      setFocus: function (fx) { if (fx !== focusX) { focusX = fx; wake(); } },
      // 明暗：dark = true 换成深色那组灯；instant = 不做渐变（刚建好场景时用）
      setTone: function (dark, instant) {
        toneT = dark ? 1 : 0;
        if (instant) tone = toneT;
        wake();
      },
      // 鼠标视差：nx / ny ∈ [-1, 1]（变化很小就不唤醒）
      pointer: function (nx, ny) {
        if (reduce) return;
        if (Math.abs(nx - parXT) < .004 && Math.abs(ny - parYT) < .004) return;
        parXT = nx; parYT = ny; wake();
      },
      // 悬停预览：让第 i 份档案微微升起并泛起一圈波纹（-1 = 取消）
      peek: function (i) {
        if (peekI >= 0 && peekI !== sel) liftT[keyOf(peekI)] = 0;
        peekI = i;
        wake();
        if (i < 0 || i >= files || i === sel) return;
        liftT[keyOf(i)] = mode === "overview" ? .7 : .55;
        if (!reduce) { waves.push({ c: MID, r: rowOf(i), t0: performance.now(), amp: .22 }); if (waves.length > 4) waves.shift(); }
      },
      // 被功能页整块盖住时停画（画面停在最后一帧，揭开时接着动）
      setPaused: function (v) { v = !!v; if (paused === v) return; paused = v; if (!v) wake(); },
      setLow: function (v) { if (low !== v) { low = v; resize(); } },
      // 选中档案顶面中心的屏幕坐标
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
        wake();
      },
      intro: function () { introT = performance.now(); rise = 0; cam.dist = camT.dist + 16; wake(); },
      resize: resize,
      wake: wake,
      // 诊断用：现在是不是还在逐帧画、一共画了多少帧
      awake: function () { return !asleep; },
      frames: function () { return drawn; },
      destroy: function () {
        alive = false; cancelAnimationFrame(raf);
        document.removeEventListener("visibilitychange", onVis);
        var ext = gl.getExtension("WEBGL_lose_context"); if (ext) ext.loseContext();
      },
    };
  };
})();
