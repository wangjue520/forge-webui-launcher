/* liquid.js — 液态玻璃风格的动效（只在 liquid-* 风格下运行，切走自动停）
 *
 *  1. 流动壁纸：几条带明暗的「绸带」缓慢起伏，边缘清晰——
 *     玻璃要有东西可「折射」，糊成一片的背景只能看出毛玻璃
 *  2. 边缘折射：给每块浮在上层的玻璃（侧栏、页头胶囊、提示条）按它自己的
 *     尺寸生成一张圆角矩形位移图：中间完全透明不变形，只在边缘一圈把背后的
 *     画面往里「弯」，三个颜色通道弯的程度略有不同（色散）。
 *     WebView2 / Chrome 支持 backdrop-filter: url(#滤镜)；不支持时退回普通模糊
 *  3. 侧栏透镜：选中项变化时，一颗玻璃胶囊用弹簧动画「流」到新位置，
 *     始终在文字下面，文字本身不动、不变粗（之前浮到文字上折射会让字抽动）
 *  4. 大标题：页面往下滚时，大标题缩进顶部的玻璃胶囊里（iOS 导航栏的行为）
 *  5. 指针高光：鼠标靠近玻璃时，玻璃边缘那圈高光跟着走
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var on = false;

  function isLiquid() { return /^liquid/.test(root.dataset.uiTheme || ""); }
  function isDark() { return root.dataset.uiTheme === "liquid-dark"; }

  /* ================= 1. 流动壁纸 ================= */
  // 浅色：高亮度的粉彩（深色字放在上面也清楚）；深色：低亮度的深色绸带
  var PAL = {
    light: {
      bg: ["#f6f7fb", "#e9eef8"],
      bands: [["#bcd6ff", "#8fb8ff"], ["#d9ccff", "#b9a6ff"], ["#ffd3e2", "#ffb3cb"], ["#c4efe6", "#97dccd"], ["#ffe2c2", "#ffc996"]],
      edge: "rgba(255,255,255,.85)", shade: "rgba(60,72,140,.22)",
    },
    dark: {
      bg: ["#040508", "#0a0d16"],
      bands: [["#16306e", "#0d1d48"], ["#2f1a63", "#1d0f42"], ["#5a1940", "#3a0f29"], ["#0b4a4a", "#062f30"], ["#55300c", "#381e06"]],
      edge: "rgba(255,255,255,.22)", shade: "rgba(0,0,0,.55)",
    },
  };
  var canvas = null, ctx = null, raf = 0, lastT = 0, t = 40 + Math.random() * 200;
  var CW = 0, CH = 0, SCALE = .5;
  var bands = [];

  function makeBands() {
    bands = [];
    for (var i = 0; i < 5; i++) {
      bands.push({
        y: .16 + i * .19,                               // 基线（占画面高度比例）
        a1: .05 + Math.random() * .04, f1: 1.2 + Math.random() * 1.1, s1: .05 + Math.random() * .05, p1: Math.random() * 6.28,
        a2: .02 + Math.random() * .025, f2: 2.6 + Math.random() * 1.8, s2: .07 + Math.random() * .06, p2: Math.random() * 6.28,
      });
    }
  }
  function sizeCanvas() {
    if (!canvas) return;
    var w = Math.max(320, Math.round(innerWidth * SCALE)), h = Math.max(200, Math.round(innerHeight * SCALE));
    if (w !== CW || h !== CH) { CW = canvas.width = w; CH = canvas.height = h; }
  }
  function curveY(b, x, W, H) {
    var u = x / W;
    return H * (b.y + b.a1 * Math.sin(u * b.f1 * 3.1416 + t * b.s1 + b.p1) + b.a2 * Math.sin(u * b.f2 * 3.1416 - t * b.s2 + b.p2));
  }
  function render(dt) {
    t += dt;
    var pal = isDark() ? PAL.dark : PAL.light, W = CW, H = CH;
    var g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, pal.bg[0]); g.addColorStop(1, pal.bg[1]);
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    // 绸带整体斜一点，更像壁纸
    ctx.save();
    ctx.translate(W / 2, H / 2); ctx.rotate(-.16); ctx.translate(-W * .62, -H * .62);
    var SW = W * 1.24, SH = H * 1.24, step = Math.max(8, SW / 64);
    for (var i = 0; i < bands.length; i++) {
      var b = bands[i], c = pal.bands[i];
      ctx.beginPath();
      ctx.moveTo(0, SH + 40);
      for (var x = 0; x <= SW + step; x += step) ctx.lineTo(x, curveY(b, x, SW, SH));
      ctx.lineTo(SW + step, SH + 40); ctx.closePath();
      var top = SH * (b.y - .1), gg = ctx.createLinearGradient(0, top, 0, top + SH * .42);
      gg.addColorStop(0, c[0]); gg.addColorStop(1, c[1]);
      // 每条绸带压在上一条上面，带一点投影 → 层次和清晰的边缘
      ctx.shadowColor = pal.shade; ctx.shadowBlur = 22 * SCALE * 2; ctx.shadowOffsetY = -4;
      ctx.fillStyle = gg; ctx.fill();
      ctx.shadowColor = "transparent"; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;
      // 边缘一道细高光
      ctx.beginPath();
      for (var x2 = 0; x2 <= SW + step; x2 += step) { var yy = curveY(b, x2, SW, SH) + 1; if (x2 === 0) ctx.moveTo(x2, yy); else ctx.lineTo(x2, yy); }
      ctx.strokeStyle = pal.edge; ctx.lineWidth = 1.2; ctx.stroke();
    }
    ctx.restore();
  }
  function loop(now) {
    raf = 0;
    if (!on) return;
    if (!document.hidden && now - lastT > 40) {          // 约 25fps，壁纸动得很慢，够了
      render(Math.min(.1, (now - lastT) / 1000));
      lastT = now;
    }
    raf = requestAnimationFrame(loop);
  }
  function startBg() {
    if (!canvas) {
      canvas = document.createElement("canvas");
      canvas.id = "lg-bg";
      canvas.setAttribute("aria-hidden", "true");
      document.body.insertBefore(canvas, document.body.firstChild);
      ctx = canvas.getContext("2d");
      makeBands();
      addEventListener("resize", function () { if (on) { sizeCanvas(); render(0); } });
    }
    canvas.hidden = false;
    sizeCanvas();
    lastT = performance.now();
    render(0);
    if (!reduce && !raf) raf = requestAnimationFrame(loop);
  }
  function stopBg() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    if (canvas) canvas.hidden = true;   // 切到别的风格后 liquid.css 不在了，必须自己藏起来
  }

  /* ================= 2. 边缘折射 ================= */
  var NS = "http://www.w3.org/2000/svg";
  var svgHost = null, uid = 0;
  var canRefract = !!(window.CSS && CSS.supports && CSS.supports("backdrop-filter", "url(#a)")) &&
    /Chrome|Edg/.test(navigator.userAgent);
  var glassList = [];        // { el, opt, id, filter, w, h }
  var ro = window.ResizeObserver ? new ResizeObserver(function (ents) {
    for (var i = 0; i < ents.length; i++) {
      var g = findGlass(ents[i].target);
      if (g) scheduleMap(g);
    }
  }) : null;

  function findGlass(el) { for (var i = 0; i < glassList.length; i++) if (glassList[i].el === el) return glassList[i]; return null; }

  function ensureSvg() {
    if (svgHost && svgHost.isConnected) return svgHost;
    svgHost = document.createElementNS(NS, "svg");
    svgHost.setAttribute("width", "0"); svgHost.setAttribute("height", "0");
    svgHost.setAttribute("aria-hidden", "true");
    svgHost.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;pointer-events:none";
    document.body.appendChild(svgHost);
    return svgHost;
  }

  // 圆角矩形位移图：R=横向、G=纵向，128 为不动。
  // 距边缘 b 像素以内往里取样：取样位置 g(v) = v + c·(1-v)²（v = 到边缘距离 / b）
  //   · g 单调递增 → 画面只被「拉伸」不会对折，不会出现接缝
  //   · 边缘处斜率最小（1-2c ≈ 0.36）→ 越靠边放大得越厉害，像凸透镜的厚边；
  //     c 不能太大，否则边缘一圈被放大成一条纯色，看起来像画了个边框
  //   · v=1 处斜率回到 1 → 和中间不变形的部分平滑衔接
  // 返回 { url, max }：max 是最大位移像素，feDisplacementMap 的 scale = 2·max
  var C_EDGE = .32;
  function makeMap(w, h, r, bezel) {
    var c = document.createElement("canvas");
    c.width = w; c.height = h;
    var g = c.getContext("2d"), im = g.createImageData(w, h), d = im.data;
    var hx = w / 2, hy = h / 2, rr = Math.min(r, hx, hy), b = Math.min(bezel, rr), max = b * C_EDGE;
    for (var y = 0; y < h; y++) {
      var py = y + .5 - hy, ay = Math.abs(py), qy = ay - (hy - rr);
      for (var x = 0; x < w; x++) {
        var px = x + .5 - hx, ax = Math.abs(px), qx = ax - (hx - rr);
        var dist, nx, ny;                               // dist: 到边缘的距离（里面为正）；n: 向外的法线
        if (qx > 0 && qy > 0) {
          var l = Math.sqrt(qx * qx + qy * qy) || 1;
          dist = rr - l; nx = qx / l; ny = qy / l;
        } else if (qx > qy) { dist = rr - qx; nx = 1; ny = 0; }
        else { dist = rr - qy; nx = 0; ny = 1; }
        nx *= px < 0 ? -1 : 1; ny *= py < 0 ? -1 : 1;
        var i = (y * w + x) * 4, m = 0;
        if (dist < b) {
          var v = Math.max(0, dist) / b, k = 1 - v;
          m = k * k;                                    // = 位移 / max
        }
        d[i] = 128 - nx * m * 127; d[i + 1] = 128 - ny * m * 127; d[i + 2] = 128; d[i + 3] = 255;
      }
    }
    g.putImageData(im, 0, 0);
    return { url: c.toDataURL(), max: max };
  }

  function buildFilter(gl, w, h) {
    var o = gl.opt, mp = makeMap(w, h, o.radius, o.bezel), url = mp.url, s = 2 * mp.max;
    var f = gl.filter;
    if (!f) {
      f = gl.filter = document.createElementNS(NS, "filter");
      f.setAttribute("id", gl.id);
      f.setAttribute("color-interpolation-filters", "sRGB");
      f.setAttribute("filterUnits", "userSpaceOnUse");
      f.setAttribute("primitiveUnits", "userSpaceOnUse");
      ensureSvg().appendChild(f);
    }
    f.setAttribute("x", "0"); f.setAttribute("y", "0"); f.setAttribute("width", w); f.setAttribute("height", h);
    // 轻微模糊（不是毛玻璃，只是去掉锯齿）→ 三个通道各自按略不同的强度位移 → 合成（色散）
    f.innerHTML =
      '<feGaussianBlur in="SourceGraphic" stdDeviation="' + o.blur + '" result="s"/>' +
      '<feImage href="' + url + '" x="0" y="0" width="' + w + '" height="' + h + '" preserveAspectRatio="none" result="m"/>' +
      '<feDisplacementMap in="s" in2="m" scale="' + s + '" xChannelSelector="R" yChannelSelector="G" result="d1"/>' +
      '<feColorMatrix in="d1" type="matrix" values="1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0" result="r"/>' +
      '<feDisplacementMap in="s" in2="m" scale="' + (s * .9).toFixed(1) + '" xChannelSelector="R" yChannelSelector="G" result="d2"/>' +
      '<feColorMatrix in="d2" type="matrix" values="0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 1 0" result="g"/>' +
      '<feDisplacementMap in="s" in2="m" scale="' + (s * .8).toFixed(1) + '" xChannelSelector="R" yChannelSelector="G" result="d3"/>' +
      '<feColorMatrix in="d3" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 1 0" result="b"/>' +
      '<feBlend in="r" in2="g" mode="screen" result="rg"/>' +
      '<feBlend in="rg" in2="b" mode="screen"/>';
    gl.w = w; gl.h = h;
    var bf = "url(#" + gl.id + ") " + o.post;
    gl.el.style.setProperty("-webkit-backdrop-filter", bf);
    gl.el.style.setProperty("backdrop-filter", bf);
  }

  var mapTimer = 0, mapQueue = [];
  function scheduleMap(gl) {
    if (mapQueue.indexOf(gl) < 0) mapQueue.push(gl);
    // 尺寸在动画里连续变化时不必每帧重算，停下来 120ms 后再算（期间旧图被拉伸着用，看不出来）
    clearTimeout(mapTimer);
    mapTimer = setTimeout(flushMaps, gl.filter ? 120 : 0);
  }
  function flushMaps() {
    var q = mapQueue; mapQueue = [];
    for (var i = 0; i < q.length; i++) {
      var gl = q[i];
      if (!gl.el.isConnected || !on) continue;
      var w = Math.round(gl.el.offsetWidth), h = Math.round(gl.el.offsetHeight);
      if (w < 8 || h < 8 || (w === gl.w && h === gl.h)) continue;
      buildFilter(gl, w, h);
    }
  }

  function glassify(el, opt) {
    if (!el || !canRefract || findGlass(el)) return;
    var gl = { el: el, opt: opt, id: "lg-f" + (++uid), filter: null, w: 0, h: 0 };
    glassList.push(gl);
    el.classList.add("lg-refract");
    if (ro) ro.observe(el);
    scheduleMap(gl);
  }
  function unglassify(gl) {
    if (ro) ro.unobserve(gl.el);
    gl.el.classList.remove("lg-refract");
    gl.el.style.removeProperty("backdrop-filter");
    gl.el.style.removeProperty("-webkit-backdrop-filter");
    if (gl.filter) gl.filter.remove();
    var k = glassList.indexOf(gl); if (k >= 0) glassList.splice(k, 1);
  }
  function unglassAll() { while (glassList.length) unglassify(glassList[0]); }

  // 每块玻璃的参数：radius 与 CSS 的圆角一致；bezel（弯折带宽度）越宽，边缘弯得越明显（不超过圆角）
  var OPT = {
    sidebar: { radius: 34, bezel: 34, blur: .6, post: "saturate(1.7) brightness(1.03)" },
    bar: { radius: 26, bezel: 26, blur: 1.6, post: "saturate(1.8) brightness(1.04)" },
    toast: { radius: 22, bezel: 20, blur: .5, post: "saturate(1.6)" },
  };
  function glassAll() {
    glassify(document.getElementById("sidebar"), OPT.sidebar);
    glassify(ensureTopGlass(), OPT.bar);
    document.querySelectorAll("#toasts .toast").forEach(function (t) { glassify(t, OPT.toast); });
  }

  // 提示条是动态加的，加进来就给它玻璃，移走就回收滤镜
  var toastObs = null;
  function watchToasts() {
    var box = document.getElementById("toasts");
    if (!box || toastObs) return;
    toastObs = new MutationObserver(function () {
      if (!on) return;
      box.querySelectorAll(".toast").forEach(function (t) { glassify(t, OPT.toast); });
      for (var i = glassList.length - 1; i >= 0; i--) if (!glassList[i].el.isConnected) unglassify(glassList[i]);
    });
    toastObs.observe(box, { childList: true });
  }

  /* ================= 3. 侧栏透镜 ================= */
  var lens = null, nav = null, lensRaf = 0;
  var S = { y: 0, h: 38, vy: 0, vh: 0, ty: 0, th: 38, inited: false };

  function ensureLens() {
    nav = document.getElementById("nav");
    if (!nav) return false;
    if (!lens) {
      lens = document.createElement("div");
      lens.id = "lg-lens";
      nav.insertBefore(lens, nav.firstChild);
    }
    return true;
  }
  function measure() {
    var a = nav && nav.querySelector(".nav-item.active");
    if (!a || a.hidden || a.offsetParent === null) { if (lens.classList.contains("on")) lens.classList.remove("on"); return false; }
    S.ty = a.offsetTop; S.th = a.offsetHeight;
    if (!lens.classList.contains("on")) lens.classList.add("on");
    if (!S.inited) { S.y = S.ty; S.h = S.th; S.inited = true; drawLens(0); }
    return true;
  }
  function drawLens(v) {
    // 速度越大拉得越长、越窄（液滴被拖动），停下时回弹；只动透镜自己，文字不受影响
    var st = Math.min(.35, Math.abs(v) / 3000);
    var sy = 1 + st, sx = 1 - st * .25;
    lens.style.height = S.h.toFixed(1) + "px";
    lens.style.transform = "translate3d(0," + S.y.toFixed(2) + "px,0) scale(" + sx.toFixed(3) + "," + sy.toFixed(3) + ")";
  }
  function stepLens(prev) {
    lensRaf = 0;
    var now = performance.now(), dt = Math.min(.032, (now - prev) / 1000);
    var k = 180, c = 20;                       // 刚度 / 阻尼：略欠阻尼，到位时轻轻回弹
    var ay = k * (S.ty - S.y) - c * S.vy, ah = k * (S.th - S.h) - c * S.vh;
    S.vy += ay * dt; S.y += S.vy * dt;
    S.vh += ah * dt; S.h += S.vh * dt;
    drawLens(S.vy);
    if (Math.abs(S.ty - S.y) > .3 || Math.abs(S.vy) > 2 || Math.abs(S.th - S.h) > .3) {
      lensRaf = requestAnimationFrame(function () { stepLens(now); });
    } else {
      S.y = S.ty; S.h = S.th; S.vy = S.vh = 0; drawLens(0);
    }
  }
  function moveLens() {
    if (!on || !ensureLens() || !measure()) return;
    if (reduce) { S.y = S.ty; S.h = S.th; drawLens(0); return; }
    if (!lensRaf) { var t0 = performance.now(); lensRaf = requestAnimationFrame(function () { stepLens(t0); }); }
  }
  var navObserver = null;
  function watchNav() {
    if (navObserver || !ensureLens()) return;
    // 只关心导航按钮自己的变化；透镜本身的变化不算（否则自己触发自己，死循环）
    navObserver = new MutationObserver(function (recs) {
      // 换页时滚动位置可能变了（新页面更短），顺便更新大标题状态
      for (var i = 0; i < recs.length; i++) if (recs[i].target !== lens) { moveLens(); setTimeout(onScroll, 0); return; }
    });
    navObserver.observe(nav, { subtree: true, attributes: true, attributeFilter: ["class", "hidden"] });
    window.addEventListener("resize", function () { S.inited = false; moveLens(); });
  }

  /* ================= 4. 大标题 → 顶部玻璃胶囊 ================= */
  var topGlass = null, content = null, mainEl = null;
  function ensureTopGlass() {
    var bar = document.getElementById("topbar");
    if (!bar) return null;
    if (!topGlass) {
      topGlass = document.createElement("div");
      topGlass.id = "lg-topglass";
      topGlass.setAttribute("aria-hidden", "true");
      bar.insertBefore(topGlass, bar.firstChild);
    }
    return topGlass;
  }
  function onScroll() {
    if (!on || !content) return;
    var c = content.scrollTop > 14;
    if (c !== mainEl.classList.contains("lg-condensed")) mainEl.classList.toggle("lg-condensed", c);
  }
  function watchScroll() {
    content = document.getElementById("content"); mainEl = document.getElementById("main");
    if (!content || !mainEl || content.dataset.lgScroll) return;
    content.dataset.lgScroll = "1";
    content.addEventListener("scroll", onScroll, { passive: true });
  }

  /* ================= 5. 指针高光（只亮玻璃边缘那一圈） ================= */
  var GLASS = "#sidebar, #topbar, .toast";
  var hot = null, pend = null, pRaf = 0;
  function onMove(e) {
    if (!on) return;
    pend = e;
    if (!pRaf) pRaf = requestAnimationFrame(applyMove);
  }
  function applyMove() {
    pRaf = 0;
    var e = pend; if (!e) return;
    var el = e.target && e.target.closest ? e.target.closest(GLASS) : null;
    if (el && el.id === "topbar") el = topGlass;
    if (hot && hot !== el) hot.classList.remove("lg-hot");
    hot = el;
    if (!el) return;
    var r = el.getBoundingClientRect();
    el.style.setProperty("--lg-mx", (e.clientX - r.left).toFixed(0) + "px");
    el.style.setProperty("--lg-my", (e.clientY - r.top).toFixed(0) + "px");
    if (!el.classList.contains("lg-hot")) el.classList.add("lg-hot");
  }

  /* ================= 开关 ================= */
  function sync() {
    var want = isLiquid();
    if (want === on) { if (on) { moveLens(); render(0); } return; }
    on = want;
    if (on) {
      startBg();
      watchNav();
      watchScroll();
      watchToasts();
      glassAll();
      if (lens) lens.hidden = false;
      if (topGlass) topGlass.hidden = false;
      S.inited = false;
      setTimeout(function () { moveLens(); onScroll(); }, 0);
    } else {
      stopBg();
      unglassAll();
      if (lens) lens.hidden = true;
      if (topGlass) topGlass.hidden = true;
      if (mainEl) mainEl.classList.remove("lg-condensed");
      if (hot) hot.classList.remove("lg-hot");
    }
  }

  document.addEventListener("pointermove", onMove, { passive: true });
  document.addEventListener("visibilitychange", function () {
    if (on && !document.hidden && !raf && !reduce) { lastT = performance.now(); raf = requestAnimationFrame(loop); }
  });
  window.addEventListener("wwy-theme", function () { setTimeout(sync, 0); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync);
  else sync();
})();
