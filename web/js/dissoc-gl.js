/* dissoc-gl.js — 「离解 · 黑白构成」的 WebGL 粒子开屏（约 3 秒；dissoc-splash.js 先试它，跑不起来再用 CSS 版）
 *
 *   0.0  黑色虚空，几千个粒子在三维空间里漂浮；下方一张透视网格地面亮起，镜头缓缓推进
 *   0.3  离解 → 复合：粒子边绕轴旋转边汇聚，拼成立体的四叶 X
 *   1.35 核心闪出黄绿光，一圈粒子像冲击波一样扩散出去
 *   1.5  一部分粒子从 X 上剥离，沿弧线飞向右边，重新拼成「WWY / LAUNCHER」
 *   2.3  标注、取景框、副标题、时间码、进度条出现（DOM，清晰的字）
 *   收尾  粒子冲向镜头，白光从 X 核心扩散开，露出主界面
 * 纯 WebGL1，不依赖任何库（启动器离线也要能跑）。window.DissocGL(host, opts) 返回控制器，WebGL 不可用时返回 null。
 */
(function () {
  "use strict";

  var BLADES = ["M0 0C6-14 20-30 40-40C30-20 14-6 0 0Z", "M0 0C14 6 30 20 40 40C20 30 6 14 0 0Z",
                "M0 0C-6 14-20 30-40 40C-30 20-14 6 0 0Z", "M0 0C-14-6-30-20-40-40C-20-30-6-14 0 0Z"];

  var VS = [
    "attribute vec3 aP0; attribute vec3 aP1; attribute vec3 aP2; attribute vec4 aR;",
    "uniform float uT; uniform float uE; uniform mat4 uProj; uniform vec3 uCam; uniform float uRotY; uniform float uRotX; uniform float uPx;",
    "varying vec4 vCol;",
    "float ease(float x){ x = clamp(x, 0.0, 1.0); return x < 0.5 ? 4.0 * x * x * x : 1.0 - pow(-2.0 * x + 2.0, 3.0) / 2.0; }",
    "void main(){",
    "  float role = aR.w;",
    // 汇聚：先绕 y 轴旋转着靠近（离解 → 复合）
    "  float a = ease((uT - 0.3 - aR.x * 0.4) / 0.95);",
    "  vec3 sw = vec3(sin(uT * 1.3 + aR.x * 20.0), cos(uT * 1.1 + aR.x * 13.0), sin(uT * 0.9 + aR.x * 7.0)) * 0.4 * (1.0 - a);",
    "  vec3 p0 = aP0 + sw;",
    "  float ang = (1.0 - a) * (2.4 + aR.x * 2.0);",
    "  vec3 pr = vec3(p0.x * cos(ang) - p0.z * sin(ang), p0.y, p0.x * sin(ang) + p0.z * cos(ang));",
    "  vec3 pos = mix(pr, aP1, a);",
    // 分裂：沿二次贝塞尔弧线飞到最终位置（中途往镜头方向鼓出去）
    "  float b = ease((uT - 1.5 - aR.x * 0.3) / 0.8);",
    "  if (b > 0.0) {",
    "    vec3 mid = mix(aP1, aP2, 0.5) + vec3(0.0, (aR.x - 0.5) * 1.2, 1.4 + aR.x * 1.6);",
    "    pos = mix(mix(aP1, mid, b), mix(mid, aP2, b), b);",
    "  }",
    "  pos.y += sin(uT * 2.2 + aR.x * 40.0) * 0.012;",
    // 收尾：冲向镜头
    "  float e = uE * uE;",
    "  pos.z += e * (6.0 + aR.x * 14.0); pos.xy *= 1.0 + e * 0.8;",
    // 镜头
    "  float cy = cos(uRotY), sy = sin(uRotY), cx = cos(uRotX), sx = sin(uRotX);",
    "  vec3 r = vec3(pos.x * cy + pos.z * sy, pos.y, -pos.x * sy + pos.z * cy);",
    "  r = vec3(r.x, r.y * cx - r.z * sx, r.y * sx + r.z * cx);",
    "  vec3 v = r - uCam;",
    "  gl_Position = uProj * vec4(v, 1.0);",
    "  gl_PointSize = clamp(aR.y * uPx * 7.0 / max(0.6, -v.z), 1.0, 48.0);",   // 镜头距离 7 时就是 aR.y × uPx 像素
    // 颜色：大部分纸白，少量黄绿和灰；冲击波粒子扩散后淡出
    "  vec3 col = aR.z > 0.9 ? vec3(0.78, 0.96, 0.24) : (aR.z > 0.72 ? vec3(0.55, 0.56, 0.57) : vec3(0.96, 0.96, 0.95));",
    "  float al = role > 2.5 ? 0.5 : 0.95;",
    "  if (role > 1.5) { al *= (1.0 - smoothstep(0.0, 1.0, b)) * step(0.01, a); col = vec3(0.78, 0.96, 0.24); }",
    "  al *= 1.0 - e;",
    "  al *= smoothstep(0.0, 0.35, uT + aR.x * 0.2);",
    "  vCol = vec4(col, al);",
    "}"].join("\n");
  var FS = [
    "precision mediump float; varying vec4 vCol;",
    "void main(){ float d = length(gl_PointCoord - 0.5); float a = smoothstep(0.5, 0.15, d); gl_FragColor = vec4(vCol.rgb, vCol.a * a); }"].join("\n");
  var VS_GRID = [
    "attribute vec3 aP; uniform mat4 uProj; uniform vec3 uCam; uniform float uRotY; uniform float uRotX; uniform float uA; varying float vA;",
    "void main(){",
    "  float cy = cos(uRotY), sy = sin(uRotY), cx = cos(uRotX), sx = sin(uRotX);",
    "  vec3 r = vec3(aP.x * cy + aP.z * sy, aP.y, -aP.x * sy + aP.z * cy);",
    "  r = vec3(r.x, r.y * cx - r.z * sx, r.y * sx + r.z * cx);",
    "  vec3 v = r - uCam; gl_Position = uProj * vec4(v, 1.0);",
    "  vA = uA * clamp(1.0 - (-v.z - 3.0) / 22.0, 0.0, 1.0);",
    "}"].join("\n");
  var FS_GRID = "precision mediump float; varying float vA; void main(){ gl_FragColor = vec4(0.96, 0.96, 0.95, vA * 0.16); }";

  function compile(gl, type, src) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  function program(gl, vs, fs) {
    var p = gl.createProgram();
    gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
    gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }
  function perspective(fov, aspect, near, far) {
    var f = 1 / Math.tan(fov / 2), nf = 1 / (near - far);
    return new Float32Array([f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0]);
  }

  // 在离屏画布上画出形状，取实心像素当粒子目标点（返回 [x, y] 归一化到 0..1）
  function samplePixels(w, h, draw, step) {
    var c = document.createElement("canvas");
    c.width = w; c.height = h;
    var g = c.getContext("2d");
    draw(g);
    var d = g.getImageData(0, 0, w, h).data, pts = [];
    for (var y = 0; y < h; y += step) for (var x = 0; x < w; x += step) if (d[(y * w + x) * 4 + 3] > 120) pts.push([x / w, y / h]);
    return pts;
  }

  window.DissocGL = function (host, opts) {
    opts = opts || {};
    var cv = document.createElement("canvas");
    cv.className = "dxg-canvas";
    var gl = null;
    try { gl = cv.getContext("webgl", { antialias: true, alpha: false, premultipliedAlpha: false }) || cv.getContext("experimental-webgl"); } catch (e) { gl = null; }
    if (!gl) return null;
    var progP, progG;
    try { progP = program(gl, VS, FS); progG = program(gl, VS_GRID, FS_GRID); } catch (e) { return null; }
    host.appendChild(cv);

    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var W = 0, H = 0, aspect = 1, layoutK = 1;
    function resize() {
      W = host.clientWidth || window.innerWidth; H = host.clientHeight || window.innerHeight;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.width = W + "px"; cv.style.height = H + "px";
      aspect = W / H;
      gl.viewport(0, 0, cv.width, cv.height);
    }
    resize();

    // 构图：最终镜头在 z=7 正对着画面，可见宽度按窗口比例算；排版总宽 ~8.6，放不下就整体缩小
    var FOV = 50 * Math.PI / 180, CAM_Z = 7;
    var visH = 2 * CAM_Z * Math.tan(FOV / 2), visW = visH * aspect;
    layoutK = Math.min(1, visW * 0.9 / 8.6);
    var XC = [-3.05 * layoutK, 0.1 * layoutK], XR = 1.35 * layoutK;      // 四叶 X 的中心和半径
    var TX0 = -1.35 * layoutK, TW = 5.6 * layoutK;                       // 大字左边和宽度

    // ---------- 采样目标形状 ----------
    var xPts = samplePixels(200, 200, function (g) {
      g.translate(100, 100); g.scale(2, 2); g.fillStyle = "#000";
      BLADES.forEach(function (d) { g.fill(new Path2D(d)); });
      g.beginPath(); g.arc(0, 0, 6, 0, Math.PI * 2); g.fill();
    }, 2);
    var TXT_W = 1100, TXT_H = 420;
    var FONT = '"WWY Grotesk", "Arial Black", "Helvetica Neue", Arial, sans-serif';
    function sampleText() {
      return samplePixels(TXT_W, TXT_H, function (g) {
        g.fillStyle = "#000"; g.textBaseline = "alphabetic";
        g.font = "700 230px " + FONT; g.fillText("WWY", 0, 200);
        g.font = "700 150px " + FONT; g.fillText("LAUNCHER", 0, 380);
      }, 3);
    }
    var tPts = sampleText();
    var textAspect = TXT_H / TXT_W;

    // ---------- 粒子 ----------
    var N_X = Math.min(3200, xPts.length * 2), N_T = Math.min(5200, tPts.length), N_R = 900, N_F = 1400;
    var N = N_X + N_T + N_R + N_F;
    var P0 = new Float32Array(N * 3), P1 = new Float32Array(N * 3), P2 = new Float32Array(N * 3), R = new Float32Array(N * 4);
    var seed = 7;
    function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
    function xTarget() {
      var p = xPts[(rnd() * xPts.length) | 0];
      return [XC[0] + (p[0] - .5) * 2 * XR + (rnd() - .5) * .02, XC[1] - (p[1] - .5) * 2 * XR + (rnd() - .5) * .02, (rnd() - .5) * .22];
    }
    // 字形上随机取一点（不能按顺序取：采样点是逐行排的，按顺序只会落在上面那行字上）
    function textTarget(pts) {
      var tp = pts[(rnd() * pts.length) | 0];
      return [TX0 + tp[0] * TW + (rnd() - .5) * .01, (.5 - tp[1]) * TW * textAspect + .05 * layoutK, (rnd() - .5) * .06];
    }
    // 中心 X（汇聚时所有粒子先拼在画面中央，再整体左移）
    function xCenterTarget() {
      var p = xPts[(rnd() * xPts.length) | 0], s = 1.9 * layoutK;
      return [(p[0] - .5) * 2 * s, -(p[1] - .5) * 2 * s + .1, (rnd() - .5) * .3];
    }
    for (var i = 0; i < N; i++) {
      // 起点：三维球壳里的散点（离解）
      var th = rnd() * Math.PI * 2, ph = Math.acos(2 * rnd() - 1), rr = 4 + rnd() * 9;
      P0[i * 3] = Math.sin(ph) * Math.cos(th) * rr * 1.4;
      P0[i * 3 + 1] = Math.cos(ph) * rr * .8;
      P0[i * 3 + 2] = Math.sin(ph) * Math.sin(th) * rr - 2;
      var role = i < N_X ? 0 : i < N_X + N_T ? 1 : i < N_X + N_T + N_R ? 2 : 3;
      var c1 = xCenterTarget(), c2;
      if (role === 0) c2 = xTarget();
      else if (role === 1) {
        c2 = textTarget(tPts);
      } else if (role === 2) {      // 冲击波：从核心扩散成一圈
        var ra = rnd() * Math.PI * 2, rr2 = 4.5 + rnd() * 2;
        c1 = [Math.cos(ra) * .05, Math.sin(ra) * .05 + .1, 0];
        c2 = [Math.cos(ra) * rr2, Math.sin(ra) * rr2 * .9, (rnd() - .5) * 1.5];
      } else {                       // 背景尘埃：一直飘在远处
        c1 = [P0[i * 3] * .6, P0[i * 3 + 1] * .6, P0[i * 3 + 2] * .5 - 6];
        c2 = [c1[0] * 1.1, c1[1] * 1.1, c1[2]];
      }
      P1.set(c1, i * 3); P2.set(c2, i * 3);
      R[i * 4] = rnd();                                   // 延迟 / 随机相位
      R[i * 4 + 1] = role === 3 ? 1.2 + rnd() * 1.6 : role === 1 ? 2.1 + rnd() * .8 : 2.4 + rnd() * 1.4;   // 点大小
      R[i * 4 + 2] = role === 1 ? rnd() * .8 : rnd();     // 颜色：字以白为主
      R[i * 4 + 3] = role;
    }
    function buf(data, prog, name, size) {
      var b = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, b);
      gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      return { b: b, loc: gl.getAttribLocation(prog, name), size: size };
    }
    var attrs = [buf(P0, progP, "aP0", 3), buf(P1, progP, "aP1", 3), buf(P2, progP, "aP2", 3), buf(R, progP, "aR", 4)];
    // 开屏时品牌字体可能还没加载完，先用后备字体采样；字体到了且粒子还没飞去拼字（1.4 秒前），按真字形重采一次
    if (document.fonts && !document.fonts.check("700 150px " + FONT)) {
      document.fonts.load("700 150px " + FONT).then(function () {
        if (dead || (t0 && performance.now() - t0 > 1400)) return;
        var pts = sampleText();
        if (!pts.length) return;
        for (var k = N_X; k < N_X + N_T; k++) P2.set(textTarget(pts), k * 3);
        gl.bindBuffer(gl.ARRAY_BUFFER, attrs[2].b);
        gl.bufferSubData(gl.ARRAY_BUFFER, 0, P2);
      }).catch(function () {});
    }
    // 网格地面
    var gl_lines = [];
    for (var gx = -14; gx <= 14; gx += 1) gl_lines.push(gx, -2.6, -24, gx, -2.6, 6);
    for (var gz = -24; gz <= 6; gz += 1) gl_lines.push(-14, -2.6, gz, 14, -2.6, gz);
    var gridBuf = buf(new Float32Array(gl_lines), progG, "aP", 3), gridN = gl_lines.length / 3;

    var U = {}, UG = {};
    ["uT", "uE", "uProj", "uCam", "uRotY", "uRotX", "uPx"].forEach(function (n) { U[n] = gl.getUniformLocation(progP, n); });
    ["uProj", "uCam", "uRotY", "uRotX", "uA"].forEach(function (n) { UG[n] = gl.getUniformLocation(progG, n); });

    function ease(x) { x = Math.max(0, Math.min(1, x)); return x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; }
    var t0 = 0, exitT = -1, raf = 0, dead = false;
    function camAt(t) {
      var k = ease(t / 1.7);
      return { z: 10.5 - (10.5 - CAM_Z) * k, ry: .55 * (1 - k), rx: .22 * (1 - k) + .0 };
    }
    // 最终镜头下，世界坐标 → 屏幕像素（DOM 标注对齐粒子大字用）
    function project(x, y) {
      var f = 1 / Math.tan(FOV / 2), d = CAM_Z;
      return [W / 2 + (x * f / aspect / d) * W / 2, H / 2 - (y * f / d) * H / 2];
    }

    function frame(now) {
      if (dead || frozen) return;
      if (!t0) t0 = now;
      draw((now - t0) / 1000, exitT >= 0 ? Math.min(1, (now - exitT) / 650) : 0);
      raf = requestAnimationFrame(frame);
    }
    var frozen = false;
    function draw(t, e) {
      var cam = camAt(t);
      var proj = perspective(FOV, aspect, .1, 100);
      gl.clearColor(.02, .02, .024, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND);
      // 网格地面
      gl.useProgram(progG);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      gl.uniformMatrix4fv(UG.uProj, false, proj);
      gl.uniform3f(UG.uCam, 0, .4, cam.z);
      gl.uniform1f(UG.uRotY, cam.ry); gl.uniform1f(UG.uRotX, cam.rx);
      gl.uniform1f(UG.uA, Math.min(1, t / .6) * (1 - e));
      gl.bindBuffer(gl.ARRAY_BUFFER, gridBuf.b);
      gl.enableVertexAttribArray(gridBuf.loc);
      gl.vertexAttribPointer(gridBuf.loc, 3, gl.FLOAT, false, 0, 0);
      gl.drawArrays(gl.LINES, 0, gridN);
      gl.disableVertexAttribArray(gridBuf.loc);
      // 粒子（加色混合）
      gl.useProgram(progP);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.uniform1f(U.uT, t); gl.uniform1f(U.uE, e);
      gl.uniformMatrix4fv(U.uProj, false, proj);
      gl.uniform3f(U.uCam, 0, 0, cam.z);
      gl.uniform1f(U.uRotY, cam.ry); gl.uniform1f(U.uRotX, cam.rx);
      gl.uniform1f(U.uPx, dpr * Math.min(W, H) / 900);
      attrs.forEach(function (a) {
        gl.bindBuffer(gl.ARRAY_BUFFER, a.b);
        gl.enableVertexAttribArray(a.loc);
        gl.vertexAttribPointer(a.loc, a.size, gl.FLOAT, false, 0, 0);
      });
      gl.drawArrays(gl.POINTS, 0, N);
      attrs.forEach(function (a) { gl.disableVertexAttribArray(a.loc); });
      if (opts.onFrame) opts.onFrame(t, e);
    }
    raf = requestAnimationFrame(frame);
    window.addEventListener("resize", resize);

    var core = project(XC[0], XC[1]);
    var txtTL = project(TX0, (.5 - 0) * TW * textAspect + .05 * layoutK);
    var txtBR = project(TX0 + TW, (.5 - 1) * TW * textAspect + .05 * layoutK);
    return {
      core: core,                 // 四叶 X 核心在屏幕上的位置（收尾白光从这里扩散）
      text: { left: txtTL[0], top: txtTL[1], right: txtBR[0], bottom: txtBR[1] },
      exit: function () { if (exitT < 0) exitT = performance.now(); },
      freeze: function (t, e) { frozen = true; draw(t, e || 0); },   // 调试用：定格到第 t 秒
      destroy: function () {
        dead = true;
        cancelAnimationFrame(raf);
        window.removeEventListener("resize", resize);
        var ext = gl.getExtension("WEBGL_lose_context");
        if (ext) ext.loseContext();
      },
    };
  };
})();
