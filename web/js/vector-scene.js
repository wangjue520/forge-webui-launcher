/* vector-scene.js — 「矢量突破」风格的立体背景场景（主界面 js/vector.js 与开屏 js/splash.js 共用）
 *
 * 照 PV 画面做：漂浮的浅青色立体方块（板状盒子，带接缝线）+ 半透明软球，
 * 远近分层，离焦的用模糊模拟景深；整体缓慢漂浮 / 微转。
 * 纯 CSS 3D（每个方块 6 个面），不用 WebGL，WebView2 上开销很小。
 * 样式见 themes/vector-scene.css（index.html 里静态引入，开屏第一帧就能用）。
 *
 * 用法：VBScene.build(preset) → 返回一个 .vb3d 元素，调用方自己插到页面里。
 *   item 字段：
 *     t: "box" | "ball"
 *     x, y: 位置（容器的百分比，指物体中心）
 *     w, h, d: 盒子宽 / 高 / 厚（px）；球用 r（半径 px）
 *     rx, ry, rz: 盒子初始朝向（度）
 *     z: 景深（0 = 焦平面，越大越虚）
 *     o: 不透明度；f: 漂浮幅度（px）；p: 漂浮周期（秒）
 */
(function () {
  "use strict";

  function div(cls, parent) {
    var n = document.createElement("div");
    n.className = cls;
    if (parent) parent.appendChild(n);
    return n;
  }

  // 固定种子：每次打开都一样，不会「每次长得不一样」
  function rng(seed) {
    var s = seed || 11;
    return function () { s = (s * 16807) % 2147483647; return s / 2147483647; };
  }

  function box(it, host) {
    var w = it.w || 160, h = it.h || it.w || 160, d = it.d || 50;
    var c = div("vb3-box", host);
    c.style.width = w + "px"; c.style.height = h + "px";
    c.style.setProperty("--rx", (it.rx || 0) + "deg");
    c.style.setProperty("--ry", (it.ry || 0) + "deg");
    c.style.setProperty("--rz", (it.rz || 0) + "deg");
    var faces = [
      ["fr", w, h, "translateZ(" + d / 2 + "px)"],
      ["bk", w, h, "rotateY(180deg) translateZ(" + d / 2 + "px)"],
      ["rt", d, h, "rotateY(90deg) translateZ(" + w / 2 + "px)"],
      ["lt", d, h, "rotateY(-90deg) translateZ(" + w / 2 + "px)"],
      ["tp", w, d, "rotateX(90deg) translateZ(" + h / 2 + "px)"],
      ["bt", w, d, "rotateX(-90deg) translateZ(" + h / 2 + "px)"],
    ];
    faces.forEach(function (f) {
      var e = div("vb3-f " + f[0], c);
      e.style.width = f[1] + "px"; e.style.height = f[2] + "px";
      e.style.left = (w - f[1]) / 2 + "px"; e.style.top = (h - f[2]) / 2 + "px";
      e.style.transform = f[3];
    });
    return c;
  }

  /* 性能要点（开屏卡顿就是这里没拆好）：
   *   .vb3-it    定位 + 入场动画（只动 transform / opacity）+ 视差位移（--px）
   *   .vb3-float 漂浮（只动 transform，独立合成层）
   *   .vb3-blur  景深模糊（filter，内容静止 → 只栅格化一次，之后由合成器搬运）
   *   物体本身   焦平面上（z=0）的方块才缓慢转动；离焦的不转，否则模糊每帧重算
   * opts.live = true 时才有常驻漂浮 / 转动（开屏用）；主界面默认静止，
   * 只在翻页时整体做一次视差滑动，空闲时零开销（毛玻璃也不用每帧重算背景）。 */
  function build(preset, opts) {
    opts = opts || {};
    var R = rng(opts.seed);
    var root = div("vb3d" + (opts.live ? " vb3-live" : "") + (opts.cls ? " " + opts.cls : ""));
    root.setAttribute("aria-hidden", "true");
    div("vb3-haze", root);
    preset.forEach(function (it, i) {
      var z = it.z || 0;
      var wrap = div("vb3-it" + (it.t === "ball" ? " is-ball" : " is-box"), root);
      wrap.setAttribute("data-layer", it.front ? "front" : (z >= 2 ? "far" : "mid"));   // 烘焙背景图时按层拆分
      wrap.style.left = it.x + "%";
      wrap.style.top = it.y + "%";
      wrap.style.zIndex = String(10 + Math.round((it.front ? 20 : 0) - z));
      wrap.style.setProperty("--dz", (it.front ? 1.6 : 1 / (1 + z * .35)).toFixed(2));   // 视差系数：近处动得多
      wrap.style.setProperty("--k", (opts.stagger ? i * opts.stagger : 0).toFixed(3) + "s");
      if (it.o != null) wrap.style.setProperty("--o", it.o);
      var fl = div("vb3-float", wrap);
      fl.style.setProperty("--fy", (it.f != null ? it.f : 10) + "px");
      fl.style.setProperty("--fp", (it.p || (9 + R() * 6)).toFixed(1) + "s");
      fl.style.setProperty("--fd", (-R() * 8).toFixed(1) + "s");
      var bl = div("vb3-blur", fl);
      if (z) bl.style.filter = "blur(" + (z * 1.6).toFixed(1) + "px)";
      if (it.t === "ball") {
        var b = div("vb3-ball", bl);
        var r = it.r || 30;
        b.style.width = b.style.height = r * 2 + "px";
      } else {
        var bx = box(it, bl);
        if (!z) bx.classList.add("spin");
        bx.style.setProperty("--fd", (-R() * 8).toFixed(1) + "s");
      }
    });
    return root;
  }

  /* 预设：主界面（铺满窗口的固定背景层，内容是毛玻璃卡片，透过来是虚化的色块） */
  var MAIN = [
    // 远景（虚）
    { t: "box", x: 46, y: 16, w: 150, h: 150, d: 46, rx: -26, ry: 38, rz: 6, z: 4, o: .55 },
    { t: "box", x: 14, y: 30, w: 110, h: 110, d: 36, rx: 18, ry: -30, rz: -12, z: 3, o: .7 },
    { t: "ball", x: 30, y: 12, r: 26, z: 3, o: .7 },
    // 焦平面（清楚，主要在右侧和下方，卡片后面透出来）
    { t: "box", x: 70, y: 56, w: 280, h: 260, d: 74, rx: -24, ry: 36, rz: 9, z: 0, f: 14 },
    { t: "box", x: 36, y: 78, w: 200, h: 190, d: 56, rx: 30, ry: -26, rz: -16, z: 1, f: 12 },
    { t: "box", x: 93, y: 28, w: 96, h: 96, d: 34, rx: -18, ry: -40, rz: 20, z: 0, f: 8 },
    { t: "ball", x: 80, y: 30, r: 46, z: 0, f: 16 },
    { t: "ball", x: 86, y: 40, r: 26, z: 0, f: 12 },
    { t: "ball", x: 76, y: 41, r: 18, z: 1, f: 10 },
    { t: "ball", x: 55, y: 44, r: 34, z: 1, f: 14 },
    { t: "ball", x: 60, y: 36, r: 14, z: 0, f: 8 },
    { t: "ball", x: 8, y: 66, r: 30, z: 2, o: .85 },
    // 前景（很虚，压在边角，营造纵深）
    { t: "box", x: 100, y: 96, w: 360, h: 340, d: 90, rx: 22, ry: -34, rz: -8, z: 8, o: .75, front: true },
    { t: "ball", x: 2, y: 100, r: 90, z: 9, o: .6, front: true },
  ];

  /* 预设：开屏（大方块从画面两侧飘着，中间留给唱片和大字） */
  var SPLASH = [
    { t: "box", x: 40, y: 12, w: 170, h: 170, d: 52, rx: -26, ry: 38, rz: 6, z: 4, o: .6 },
    { t: "box", x: 12, y: 60, w: 210, h: 200, d: 60, rx: 26, ry: -30, rz: -14, z: 2, o: .9 },
    { t: "box", x: 58, y: 86, w: 260, h: 240, d: 70, rx: -20, ry: 34, rz: 10, z: 1 },
    { t: "box", x: 30, y: 104, w: 380, h: 340, d: 100, rx: 18, ry: -24, rz: -6, z: 9, o: .7, front: true },
    { t: "ball", x: 66, y: 16, r: 30, z: 2, o: .8 },
    { t: "ball", x: 4, y: 20, r: 70, z: 8, o: .6, front: true },
  ];

  window.VBScene = { build: build, MAIN: MAIN, SPLASH: SPLASH };
})();
