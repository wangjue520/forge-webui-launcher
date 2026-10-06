/* bauform.js — 「构型 · 机能包豪斯」风格专属的界面元素与动效（只在 bauform 风格下存在，切走自动拆掉、原样还原）
 *
 *  1. 背景（#bp-bg，放在 #main 最底层）
 *       · X 光爆炸装配图（js/bauform-xray.js 程序生成，只画一次），整体极慢旋转 + 跟随鼠标的轻微视差
 *       · 包豪斯构成辅助线：两道巨大的弧（左右「括号」）、两条横向发丝线、准星十字、小标签，进场时描线
 *       · 底部半调网点（全部静态，不做周期性扫动）
 *  2. 页头大字排版（#bp-mast）：「01」切线字编号 + 英文页名（WWY Bauform，逐字上浮）+ 中文页名
 *     内容跟着 core.js 写进 #page-code / #page-title 的文字走（MutationObserver），不改导航逻辑
 *  3. 翻页：背景装配图「拧」一格、页头小字解码 / 大字升起，新页面卡片错峰上浮、顶边扫光（不做整屏转场）
 *  4. 侧栏 = 翻牌时刻表：每项一片翻牌，编号是小翻牌（悬停转一圈），选中牌从上翻下、旧牌向下翻走；
 *     性能监控数值也是翻牌；品牌处是包豪斯三基本形标志
 *  5. 页头右侧：鼠标坐标读数（机能风 HUD 小字）
 *  纯视觉，不碰业务逻辑；样式见 themes/bauform.css。
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var NS = "http://www.w3.org/2000/svg";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var B = null;   // 已构建的元素与监听器

  function isBP() { return root.dataset.uiTheme === "bauform"; }
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, parent, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }
  function svg(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function pad(n, w) { n = String(n); while (n.length < w) n = "0" + n; return n; }

  /* ---------------- 背景 ---------------- */
  var model = null;
  function drawXray() {
    if (!B || !window.BPXray) return;
    var cv = B.xr, wrap = B.xrWrap;
    var w = wrap.clientWidth, h = wrap.clientHeight;
    // 主题样式表可能比脚本晚到：没套上样式时容器不是正方形，这时画了会被拉伸变扁，
    // 等 ResizeObserver 报告真正的尺寸再画
    if (!w || !h || Math.abs(w - h) > 2) return;
    var dpr = Math.min(1.5, window.devicePixelRatio || 1);
    var W = Math.round(w * dpr), H = Math.round(h * dpr);
    if (cv.width === W && cv.height === H) return;
    cv.width = W; cv.height = H;
    if (!model) model = window.BPXray.model(20261006);
    var c = cv.getContext("2d");
    c.clearRect(0, 0, W, H);
    window.BPXray.draw(c, model, W, H, { p: 1, scale: Math.min(W, H) / 1250 });
  }

  function buildGuides() {
    var g = B.guides, main = $("main");
    var W = main.clientWidth, H = main.clientHeight;
    if (!W || !H) return;
    g.setAttribute("viewBox", "0 0 " + W + " " + H);
    while (g.firstChild) g.removeChild(g.firstChild);
    var y1 = Math.round(H * .3), y2 = Math.round(H * .74);
    function path(d, cls, delay) {
      var p = svg("path", { d: d, class: cls }, g);
      if (!reduce && cls.indexOf("draw") >= 0) {
        var len = Math.ceil(p.getTotalLength ? p.getTotalLength() : 2000);
        p.style.setProperty("--len", len);
        p.style.animationDelay = (delay || 0) + "s";
      }
      return p;
    }
    // 横向发丝线（PV 里的「宽银幕」上下边线）
    path("M0 " + y1 + "H" + W, "g-hair draw", .1);
    path("M0 " + y2 + "H" + W, "g-hair draw", .25);
    // 一对巨大的弧：圆心在内容区中间，只露出左右两段，像括号
    var cx = W * .56, cy = H * .52, R = Math.max(H * .78, W * .42);
    var a = Math.asin(Math.min(.98, (H * .62) / R));
    function arcPts(sign) {
      var x0 = cx + sign * R * Math.cos(a), yA = cy - R * Math.sin(a), yB = cy + R * Math.sin(a);
      return "M" + x0 + " " + yA + "A" + R + " " + R + " 0 0 " + (sign > 0 ? 1 : 0) + " " + x0 + " " + yB;
    }
    path(arcPts(-1), "g-arc draw", .3);
    path(arcPts(1), "g-arc draw", .45);
    // 右侧装配图外圈的点划弧
    var xr = B.xrWrap.getBoundingClientRect(), mr = main.getBoundingClientRect();
    var xcx = xr.left - mr.left + xr.width / 2, xcy = xr.top - mr.top + xr.height / 2, xrr = xr.width * .36;
    path("M" + (xcx - xrr) + " " + xcy + "A" + xrr + " " + xrr + " 0 0 1 " + (xcx + xrr) + " " + xcy, "g-arc hi draw", .6);
    // 红色短发丝线 + 准星
    path("M" + (W * .62) + " " + y2 + "H" + (W * .62 + 64), "g-red draw", .9);
    function cross(x, y) {
      svg("path", { d: "M" + (x - 6) + " " + y + "H" + (x + 6) + "M" + x + " " + (y - 6) + "V" + (y + 6), class: "g-cross" }, g);
    }
    var lx = cx - R * Math.cos(Math.asin(Math.min(.98, Math.abs(y1 - cy) / R)));
    cross(Math.max(24, lx), y1); cross(W - 36, y2); cross(W * .62, y1);
    function lab(x, y, t) { var n = svg("text", { x: x, y: y, class: "g-lab" }, g); n.textContent = t; return n; }
    B.secLab = lab(W - 150, y1 - 8, "SECTION / " + (B.lastNum || "01"));
    lab(W * .62 + 72, y2 + 3, "ARC " + (R / H).toFixed(2) + "R");
    lab(Math.max(24, lx) + 12, y1 + 16, "LAYER — INVERSE");
  }

  /* ---------------- 页头大字 ---------------- */
  function setMast(first) {
    if (!B) return;
    var codeEl = $("page-code"), titleEl = $("page-title");
    var code = (codeEl && codeEl.textContent) || "01 // LAUNCH";
    var m = /^(\d+)\s*\/\/\s*(.+)$/.exec(code.trim());
    var num = m ? m[1] : "00", word = m ? m[2] : code;
    var cn = (titleEl && titleEl.textContent) || "";
    if (!first && num === B.lastNum && word === B.lastWord) return;
    var changed = !first && B.lastNum != null;
    B.lastNum = num; B.lastWord = word;

    if (B.numFlap) B.numFlap.set(num); else B.num.textContent = num;
    B.kicker.innerHTML = "";
    el("i", "", B.kicker);
    B.kicker.appendChild(document.createTextNode("SECTION "));
    el("b", "", B.kicker, num);
    decode(el("span", "", B.kicker), " — BAUFORM · WWY LAUNCHER");
    B.word.innerHTML = "";
    word.split("").forEach(function (ch, i) {
      var s = el("span", ch === " " ? "sp" : "", B.word, ch === " " ? " " : ch);
      s.style.setProperty("--i", i);
    });
    // 中文页名去掉「 · Civitai / liblib」之类的英文尾巴（英文已经在大字里了）
    var c2 = cn.split(" · ")[0];
    B.cn.textContent = c2;
    B.cn.style.animation = "none"; void B.cn.offsetWidth; B.cn.style.animation = "";
    var tb = $("topbar");
    tb.classList.remove("bp-swap"); void tb.offsetWidth; tb.classList.add("bp-swap");
    if (changed) onPageChange(num);
    placeNavlight(changed);
  }

  /* ---------------- 翻页：不盖整屏，动作分散到各处 ----------------
   * · 背景装配图整体「拧」一格（像机构转动换挡），辅助线里的 SECTION 标签跟着换号
   * · 页头小字解码、大字逐字升起、编号乱码定格（setMast 里）
   * · 新页面卡片错峰上浮，顶边扫过一道光（CSS） */
  var step = 0;
  function onPageChange(num) {
    if (reduce || !B) return;
    step += 1;
    B.xrStep.style.transform = "rotate(" + (step * 24) + "deg)";
    if (B.secLab) B.secLab.textContent = "SECTION / " + num;
  }
  function decode(node, text) {
    if (reduce) { node.textContent = text; return; }
    var t0 = performance.now(), dur = 520, CH = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/·—";
    (function tick(now) {
      var p = Math.min(1, (now - t0) / dur), n = Math.floor(text.length * p), out = text.slice(0, n);
      for (var i = n; i < Math.min(text.length, n + 6); i++) out += text[i] === " " ? " " : CH[(Math.random() * CH.length) | 0];
      node.textContent = out;
      if (p < 1) requestAnimationFrame(tick); else node.textContent = text;
    })(t0);
  }

  /* ---------------- 侧栏滑块 ---------------- */
  function placeNavlight(flip) {
    if (!B) return;
    var act = document.querySelector("#nav .nav-item.active");
    var nl = B.navlight;
    if (!act || act.hidden) { nl.style.opacity = "0"; B.nlTop = null; return; }
    nl.style.opacity = "1";
    var top = act.offsetTop, h = act.offsetHeight;
    var moved = B.nlTop != null && B.nlTop !== top;
    // 旧位置留一张「幽灵牌」向下翻走
    if (flip && moved && !reduce) {
      var g = el("div", "bp-navghost", null);
      g.innerHTML = '<i class="nl-face"></i>';
      g.style.transform = "translateY(" + B.nlTop + "px)"; g.style.height = B.nlH + "px";
      nl.parentNode.insertBefore(g, nl);
      setTimeout(function () { g.remove(); }, 320);
    }
    nl.style.transform = "translateY(" + top + "px)";
    nl.style.height = h + "px";
    B.nlTop = top; B.nlH = h;
    if (flip && !reduce) { nl.classList.remove("flip"); void nl.offsetWidth; nl.classList.add("flip"); }
  }

  /* ---------------- 侧栏编号翻牌 + 性能数值翻牌 ---------------- */
  function navNums(flip) {
    if (!B || !window.BFFlap) return;
    var k = 0;
    document.querySelectorAll("#nav .nav-item").forEach(function (it) {
      if (it.hidden) return;
      k += 1;
      var t = pad(k, 2);
      if (!it._bfNum) {
        var sp = el("span", "bp-navnum", it);
        it._bfNum = window.BFFlap.create(sp, { speed: 170, text: t });
        it._bfEl = sp;
        it.addEventListener("mouseenter", function () { if (isBP() && it._bfNum) it._bfNum.rattle(70); });
      } else if (flip) it._bfNum.set(t); else it._bfNum.jump(t);
    });
  }
  // 当前显示的导航项（按顺序）。只比数量会漏掉「一项出现、另一项同时隐藏」的情况
  function visibleSig() {
    return Array.prototype.filter.call(document.querySelectorAll("#nav .nav-item"), function (n) { return !n.hidden; })
      .map(function (n) { return n.dataset.page; }).join(",");
  }
  function perfFlaps() {
    if (!B || !window.BFFlap) return;
    document.querySelectorAll("#perf-mon .pm-val").forEach(function (v) {
      var txt = v.textContent;
      if (!v._bf) {
        var host = el("span", "bp-pmflap", null);
        v.parentNode.insertBefore(host, v.nextSibling);
        v._bf = window.BFFlap.create(host, { speed: 160, text: txt });
        v._bfEl = host; v._bfTxt = txt;
        B.pmHosts.push(v);
      } else if (txt !== v._bfTxt) { v._bfTxt = txt; v._bf.set(txt); }
    });
  }

  /* ---------------- 品牌 ---------------- */
  function emblem() {
    // 包豪斯三基本形：石板蓝方块 / 奶油色圆 / 红三角，叠一个准星
    return '<svg viewBox="0 0 42 42" aria-hidden="true">' +
      '<rect class="em-sq" x="0" y="8" width="26" height="26" fill="#4a5257"/>' +
      '<circle class="em-ci" cx="27" cy="17" r="13" fill="#ebe4d2" style="mix-blend-mode:normal"/>' +
      '<path class="em-tr" d="M14 42 L30 22 L42 42Z" fill="#e5482f"/>' +
      '<path d="M27 4v26M14 17h26" stroke="#0a0d10" stroke-width="1.2"/>' +
      '<circle cx="27" cy="17" r="3" fill="none" stroke="#0a0d10" stroke-width="1.2"/>' +
      '</svg>';
  }

  /* ---------------- 构建 / 拆除 ---------------- */
  function build() {
    if (B) return;
    var main = $("main"), topbar = $("topbar"), nav = $("nav");
    if (!main || !topbar || !nav) return;
    B = {};

    // 背景
    var bg = el("div", "", null); bg.id = "bp-bg";
    B.xrWrap = el("div", "bp-xr-wrap", bg);
    B.xrStep = el("div", "bp-xr-step", B.xrWrap);
    B.xr = el("canvas", "bp-xr", B.xrStep);
    B.guides = svg("svg", { class: "bp-guides", preserveAspectRatio: "none" }, bg);
    el("div", "bp-halftone", bg);
    main.insertBefore(bg, main.firstChild);
    B.bg = bg;

    // 页头
    var mast = el("div", "", null); mast.id = "bp-mast";
    B.num = el("div", "bp-num", mast);
    B.numFlap = window.BFFlap ? window.BFFlap.create(B.num, { speed: 210, text: "00" }) : null;
    var tx = el("div", "bp-mast-text", mast);
    B.kicker = el("div", "bp-kicker", tx);
    var row = el("div", "bp-title-row", tx);
    B.word = el("div", "bp-word", row);
    B.cn = el("div", "bp-cn", row);
    var head = topbar.querySelector(".page-head");
    topbar.insertBefore(mast, head ? head.nextSibling : topbar.firstChild);
    B.mast = mast;

    // 页头时钟换成翻牌（原来的 #topbar-clock 数字由 deco.js 每秒写入，这里只是盖住不显示）
    var clk = document.querySelector(".topbar-clock");
    if (clk && window.BFFlap) {
      B.clockEl = el("span", "bp-clock-flap", null);
      clk.insertBefore(B.clockEl, clk.firstChild);
      B.clockFlap = window.BFFlap.create(B.clockEl, { speed: 300 });
      var tick = function () {
        var d = new Date();
        B.clockFlap.set(pad(d.getHours(), 2) + ":" + pad(d.getMinutes(), 2) + ":" + pad(d.getSeconds(), 2));
      };
      var d0 = new Date();
      B.clockFlap.jump(pad(d0.getHours(), 2) + ":" + pad(d0.getMinutes(), 2) + ":" + pad(d0.getSeconds(), 2));
      B.clockT = setInterval(tick, 1000);
    }

    var extra = $("topbar-extra");
    if (extra) {
      B.coord = el("div", "bp-coord", null);
      B.coord.innerHTML = "X <b>0000</b> Y <b>0000</b><br>GRID 48 · SEC 04";
      extra.insertBefore(B.coord, extra.firstChild);
    }

    // 侧栏
    B.navlight = el("div", "", null); B.navlight.id = "bp-navlight";
    B.navlight.innerHTML = '<i class="nl-face"></i>';
    nav.insertBefore(B.navlight, nav.firstChild);
    var logo = document.querySelector(".brand-logo"), name = document.querySelector(".brand-name");
    if (logo) { B.logoHTML = logo.innerHTML; logo.innerHTML = emblem(); }
    if (name) {
      B.nameHTML = name.innerHTML;
      name.innerHTML = "WWY<span class=\"bp-cn\">启动器</span>";
    }

    // 监听：页码 / 标题文字变化 → 更新大字 + 转场；导航项显隐 → 重新定位滑块
    B.obs = new MutationObserver(function () { setMast(false); });
    ["page-code", "page-title"].forEach(function (id) {
      var n = $(id); if (n) B.obs.observe(n, { childList: true, characterData: true, subtree: true });
    });
    B.navObs = new MutationObserver(function (recs) {
      // 只关心导航项本身的显隐 / 选中变化（翻牌内部的改动不算）
      var hit = recs.some(function (r) { return r.target.classList && r.target.classList.contains("nav-item"); });
      if (!hit) return;
      var vis = visibleSig();
      if (vis !== B.navVis) { B.navVis = vis; navNums(true); }
      placeNavlight(false);
    });
    B.navObs.observe(nav, { attributes: true, subtree: true, attributeFilter: ["hidden", "class"] });

    // 鼠标：坐标读数 + 背景视差（rAF 节流）
    var mx = 0, my = 0, pending = false;
    B.onMove = function (e) {
      mx = e.clientX; my = e.clientY;
      if (pending) return; pending = true;
      requestAnimationFrame(function () {
        pending = false;
        if (!B) return;
        if (B.coord) {
          var bs = B.coord.querySelectorAll("b");
          bs[0].textContent = pad(Math.round(mx), 4); bs[1].textContent = pad(Math.round(my), 4);
        }
        if (!reduce) {
          var px = (mx / innerWidth - .5) * -18, py = (my / innerHeight - .5) * -14;
          B.xrWrap.style.setProperty("--bp-px", px.toFixed(1) + "px");
          B.xrWrap.style.setProperty("--bp-py", py.toFixed(1) + "px");
        }
      });
    };
    window.addEventListener("mousemove", B.onMove, { passive: true });
    B.onResize = function () {
      clearTimeout(B.rt);
      B.rt = setTimeout(function () { drawXray(); buildGuides(); placeNavlight(false); }, 160);
    };
    window.addEventListener("resize", B.onResize);
    if (window.ResizeObserver) {
      B.ro = new ResizeObserver(function () { B.onResize(); });
      B.ro.observe(B.xrWrap); B.ro.observe(main);
    }

    navNums(false);
    B.navVis = visibleSig();
    B.pmHosts = [];
    var pm = $("perf-mon");
    if (pm) {
      perfFlaps();
      B.pmObs = new MutationObserver(function (recs) {
        if (recs.every(function (r) { var t = r.target.nodeType === 1 ? r.target : r.target.parentNode; return t && t.closest && t.closest(".bfl"); })) return;
        perfFlaps();
      });
      B.pmObs.observe(pm, { childList: true, subtree: true, characterData: true });
    }

    setMast(true);
    requestAnimationFrame(function () { drawXray(); buildGuides(); placeNavlight(false); });
    // 字体加载完后导航项高度可能变，重新定位一次
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { placeNavlight(false); });
  }

  function teardown() {
    if (!B) return;
    try { B.obs.disconnect(); B.navObs.disconnect(); if (B.pmObs) B.pmObs.disconnect(); } catch (e) {}
    document.querySelectorAll("#nav .nav-item").forEach(function (it) {
      if (it._bfNum) { it._bfNum.destroy(); it._bfEl.remove(); it._bfNum = null; it._bfEl = null; }
    });
    (B.pmHosts || []).forEach(function (v) { if (v._bf) { v._bf.destroy(); v._bfEl.remove(); v._bf = null; } });
    window.removeEventListener("mousemove", B.onMove);
    window.removeEventListener("resize", B.onResize);
    if (B.ro) B.ro.disconnect();
    clearInterval(B.clockT);
    [B.bg, B.mast, B.navlight, B.coord, B.clockEl].forEach(function (n) { if (n && n.parentNode) n.parentNode.removeChild(n); });
    var logo = document.querySelector(".brand-logo"), name = document.querySelector(".brand-name");
    if (logo && B.logoHTML != null) logo.innerHTML = B.logoHTML;
    if (name && B.nameHTML != null) name.innerHTML = B.nameHTML;
    root.classList.remove("bp-intro");
    B = null;
  }

  function sync() { if (isBP()) build(); else teardown(); }
  window.addEventListener("wwy-theme", sync);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync); else sync();

  // 开屏撤场时调用：主界面整体入场
  window.BPUI = {
    intro: function () {
      if (!isBP() || reduce) return;
      root.classList.add("bp-intro");
      setTimeout(function () { root.classList.remove("bp-intro"); }, 1600);
      if (B) { setMast(true); }
    },
  };
})();
