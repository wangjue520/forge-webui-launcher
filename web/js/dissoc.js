/* dissoc.js — 「离解 · 黑白构成」风格专属的版面和动效（只在 dissoc 风格下存在，切走自动拆掉、原样还原）
 *
 * 这套风格的版面和别的风格不一样（别的都是「左侧竖排导航 + 页头 + 卡片」）：
 *  1. 左侧收成一条窄黑带：四叶 X 徽记 + 竖排大字 WWY / LAUNCHER + 性能监控
 *  2. 导航搬到顶部，排成一条横向「时间轴」（#dx-axis）：编号 + 中文页名挂在一根细线上，
 *     当前页由一个四叶 X 在轴上滑过去标出；实例切换 / 远程联动的条幅放在轴的右端
 *  3. 页头是海报式的：自转的四叶 X + 超大英文页名 / 编号（翻页时逐字「解码」）+ 一个写着中文页名的信息框；
 *     右侧是一直在走的 TIME AXIS 时间码、时钟和运行状态（原侧栏底部的状态按钮搬过来）
 *  4. 背景（#dx-deco）：右下角一个描边的巨型页码、黑带边缘露出半个大四叶 X、几道斜向细线、少量标注
 *  5. 动效：翻页时卡片被扫描光束依次扫出；主区域里跟随鼠标的十字准线 + 坐标（在卡片下面，不压字）；
 *     每隔一阵一道扫描线扫过（同样在卡片下面）；点击处爆开一个小 X
 * 搬动的节点都记下原来的位置，切走风格时放回去；不碰导航和业务逻辑（按钮还是原来那些元素）。
 * 样式见 themes/dissoc.css。系统开了「减少动态效果」时只留静态版面。
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var NS = "http://www.w3.org/2000/svg";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var built = null;

  var X_SVG = '<svg viewBox="-50 -50 100 100" aria-hidden="true"><g fill="currentColor">' +
    '<path d="M0 0C6-14 20-30 40-40C30-20 14-6 0 0Z"/><path d="M0 0C14 6 30 20 40 40C20 30 6 14 0 0Z"/>' +
    '<path d="M0 0C-6 14-20 30-40 40C-30 20-14 6 0 0Z"/><path d="M0 0C-14-6-30-20-40-40C-20-30-6-14 0 0Z"/></g>' +
    '<circle class="dx-core" r="4.5"/></svg>';
  var STAR_SVG = '<svg viewBox="-50 -50 100 100" aria-hidden="true"><path fill="currentColor" ' +
    'd="M0-50C4-12 12-4 50 0C12 4 4 12 0 50C-4 12-12 4-50 0C-12-4-4-12 0-50Z"/></svg>';
  var GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789/#+-";

  function isMine() { return root.dataset.uiTheme === "dissoc"; }
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }
  function pad(n, w) { return String(n).padStart(w || 2, "0"); }

  // 搬家：记下原位置，拆的时候放回去
  function move(node, parent, before) {
    if (!node || !parent) return;
    built.moved.push({ node: node, parent: node.parentNode, next: node.nextSibling });
    parent.insertBefore(node, before || null);
  }
  function restoreMoved() {
    for (var i = built.moved.length - 1; i >= 0; i--) {
      var m = built.moved[i];
      if (m.parent) m.parent.insertBefore(m.node, m.next && m.next.parentNode === m.parent ? m.next : null);
    }
  }

  /* ---------------- 背景 ---------------- */
  function buildDeco() {
    var d = el("div");
    d.id = "dx-deco";
    d.setAttribute("aria-hidden", "true");
    el("div", "dx-fade", null, d);
    el("div", "dx-bigx", null, d).innerHTML = X_SVG;
    built.bignum = el("div", "dx-bignum", "01", d);
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("class", "dx-diag");
    s.setAttribute("preserveAspectRatio", "none");
    s.setAttribute("viewBox", "0 0 1000 1000");
    [[180, 0, 1000, 640], [0, 380, 760, 1000]].forEach(function (p) {
      var l = document.createElementNS(NS, "line");
      l.setAttribute("x1", p[0]); l.setAttribute("y1", p[1]); l.setAttribute("x2", p[2]); l.setAttribute("y2", p[3]);
      l.setAttribute("vector-effect", "non-scaling-stroke");
      s.appendChild(l);
    });
    d.appendChild(s);
    var tags = [
      { l: "62%", t: "31%", k: "Convolution", f: function () { return (0.6 + Math.random() * .05).toFixed(4); } },
      { l: "88%", t: "57%", k: "Dissociate", f: function () { return pad((Math.random() * 100) | 0) + " / 100"; } },
    ];
    built.tags = tags.map(function (t) {
      var n = el("div", "dx-tag", null, d);
      n.style.left = t.l; n.style.top = t.t;
      el("span", "dx-lab", t.k, n);
      return { node: el("span", "v", t.f(), n), f: t.f };
    });
    [["56%", "22%"], ["93%", "40%"]].forEach(function (p, i) {
      var st = el("i", "dx-star", null, d);
      st.innerHTML = STAR_SVG;
      st.style.left = p[0]; st.style.top = p[1]; st.style.animationDelay = (-i * 2.3) + "s";
    });
    document.body.insertBefore(d, $("layout") || document.body.firstChild);
    return d;
  }

  /* ---------------- 前景：准线 / 扫描线（在内容下面）+ 点击爆点（在最上面） ---------------- */
  function buildFront() {
    var f = el("div");
    f.id = "dx-front";
    f.setAttribute("aria-hidden", "true");
    built.hx = el("i", "dx-hx", null, f);
    built.vx = el("i", "dx-vx", null, f);
    built.read = el("span", "dx-read", "", f);
    built.sweep = el("i", "dx-sweep", null, f);
    document.body.insertBefore(f, $("layout") || document.body.firstChild);
    var b = el("div");
    b.id = "dx-burst";
    b.setAttribute("aria-hidden", "true");
    document.body.appendChild(b);
    built.burst = b;
    return f;
  }

  var pending = null, raf = 0;
  function onMove(e) {
    if (!built || reduce) return;
    pending = e;
    if (!raf) raf = requestAnimationFrame(applyMove);
  }
  function applyMove() {
    raf = 0;
    var e = pending, main = $("main");
    if (!built || !e || !main) return;
    var r = main.getBoundingClientRect();
    var inMain = e.clientX > r.left && e.clientY > r.top && !document.querySelector("#modal-mask:not([hidden])");
    built.front.classList.toggle("on", inMain);
    var w = window.innerWidth, h = window.innerHeight;
    var dx = e.clientX / w - .5, dy = e.clientY / h - .5;
    built.deco.style.setProperty("--mx", (-dx * 24).toFixed(1) + "px");
    built.deco.style.setProperty("--my", (-dy * 18).toFixed(1) + "px");
    if (!inMain) return;
    built.hx.style.transform = "translate3d(0," + e.clientY + "px,0)";
    built.vx.style.transform = "translate3d(" + e.clientX + "px,0,0)";
    built.read.textContent = "X " + pad(Math.round(e.clientX - r.left), 4) + "   Y " + pad(Math.round(e.clientY - r.top), 4);
    var rx = e.clientX + 12, ry = e.clientY - 22;
    if (rx + 120 > w) rx = e.clientX - 124;
    if (ry < r.top) ry = e.clientY + 10;
    built.read.style.transform = "translate3d(" + rx + "px," + ry + "px,0)";
  }
  function onLeave() { if (built) built.front.classList.remove("on"); }

  function onClick(e) {
    if (!built || reduce || e.button !== 0) return;
    var b = el("i", "dx-pop", null, built.burst);
    b.innerHTML = X_SVG;
    b.style.left = e.clientX + "px"; b.style.top = e.clientY + "px";
    setTimeout(function () { b.remove(); }, 600);
  }

  var sweepN = 0;
  function sweep() {
    if (!built || reduce || document.hidden) return;
    built.sweep.setAttribute("data-t", "SCANNING · " + pad(++sweepN % 100));
    built.sweep.classList.remove("go");
    void built.sweep.offsetWidth;
    built.sweep.classList.add("go");
  }

  /* ---------------- 顶部时间轴（导航） ---------------- */
  function buildAxis(main) {
    var a = el("div");
    a.id = "dx-axis";
    var head = el("div", "dxa-head", null, a);
    el("span", "dx-lab", "Time axis", head);
    el("b", null, "WWY", head);
    var track = el("div", "dxa-track", null, a);
    built.marker = el("i", "dxa-x", null, track);
    built.marker.innerHTML = X_SVG;
    move($("nav"), track);
    var side = el("div", "dxa-side", null, a);
    move($("inst-switch"), side);
    move($("link-banner"), side);
    main.insertBefore(a, main.firstChild);
    return a;
  }
  // 四叶 X 滑到当前页那一格的刻度上
  function placeMarker() {
    if (!built || !built.marker) return;
    var act = document.querySelector("#nav .nav-item.active");
    var track = built.marker.parentNode;
    if (!act || !track) { built.marker.style.opacity = "0"; return; }
    var tr = track.getBoundingClientRect(), r = act.getBoundingClientRect();
    built.marker.style.opacity = "1";
    built.marker.style.transform = "translate3d(" + (r.left - tr.left + r.width / 2).toFixed(1) + "px,0,0)";
  }

  /* ---------------- 左侧窄黑带 ---------------- */
  function buildRail(sidebar) {
    var w = el("div", "dx-rail-word");
    w.setAttribute("aria-hidden", "true");
    el("b", null, "WWY", w);
    el("span", null, "LAUNCHER", w);
    var brand = sidebar.querySelector(".brand");
    sidebar.insertBefore(w, brand ? brand.nextSibling : sidebar.firstChild);
    return w;
  }

  /* ---------------- 页头 ---------------- */
  function buildMast(topbar) {
    var m = el("div");
    m.id = "dx-mast";
    m.setAttribute("aria-hidden", "true");   // 屏幕阅读器继续读原来的 #page-title
    el("div", "dxm-x", null, m).innerHTML = X_SVG;
    var en = el("div", "dxm-en", null, m);
    var labs = el("div", "dxm-labs", null, en);
    labs.innerHTML = '<span class="dx-lab">Module</span><span class="dx-lab dxm-code"></span>';
    el("div", "dxm-big", null, en);
    var box = el("div", "dxm-box", null, m);
    el("span", "dx-lab", "Message", box);
    el("div", "dxm-title", "", box);
    el("div", "dxm-quote", "You observe the world. The world observes you.", box);
    topbar.insertBefore(m, $("topbar-extra"));
    return m;
  }

  var decodeTimer = null;
  function decode(box, text, num, animate) {
    clearInterval(decodeTimer);
    box.innerHTML = "";
    var word = el("span", "dxm-word", null, box);
    var spans = text.split("").map(function (c, i) {
      var g = el("i", null, c === " " ? " " : c, word);
      g.style.setProperty("--i", i);
      return g;
    });
    var numEl = el("span", "dxm-num", num, box);
    if (!animate || reduce) return;
    var t = 0, n = spans.length;
    decodeTimer = setInterval(function () {
      t++;
      spans.forEach(function (g, i) {
        if (text[i] === " ") return;
        g.textContent = t > 3 + i * 1.3 ? text[i] : GLYPHS[(Math.random() * GLYPHS.length) | 0];
      });
      numEl.textContent = t > 6 + n ? num : pad((Math.random() * 100) | 0);
      if (t > 6 + n) clearInterval(decodeTimer);
    }, 34);
  }

  function syncMast(animate) {
    if (!built) return;
    var code = ($("page-code") && $("page-code").textContent) || "";
    var title = ($("page-title") && $("page-title").textContent) || "";
    var parts = code.split("//");
    var num = (parts[0] || "").trim() || "00";
    var word = (parts[1] || code).trim() || "WWY";
    var m = built.mast;
    decode(m.querySelector(".dxm-big"), word, num, animate);
    m.querySelector(".dxm-code").textContent = "No." + num + " — " + word;
    var tn = m.querySelector(".dxm-title"), t = title.split(" · ");
    tn.textContent = t[0];
    if (t[1]) el("small", null, t.slice(1).join(" · "), tn);
    built.bignum.textContent = num;
    placeMarker();
    if (animate) {
      [m, built.bignum].forEach(function (n) { n.classList.remove("dx-in"); void n.offsetWidth; n.classList.add("dx-in"); });
      scanPage();
    }
  }

  // 翻页：当前页的各块被一道光束依次扫出来；扫完把类去掉，免得 clip-path 裁到下拉框 / 阴影
  var scanTimer = null;
  function scanPage() {
    if (reduce) return;
    clearTimeout(scanTimer);
    document.querySelectorAll(".dx-scan").forEach(unscan);
    var page = document.querySelector(".page.active");
    if (!page) return;
    var k = 0;
    Array.prototype.forEach.call(page.children, function (c) {
      if (c.hidden || c.offsetParent === null || k > 8) return;
      c.classList.add("dx-scan");
      c.style.setProperty("--k", k++);
      el("i", "dx-beam", null, c);
    });
    scanTimer = setTimeout(function () { document.querySelectorAll(".dx-scan").forEach(unscan); }, 70 * k + 900);
  }
  function unscan(c) {
    c.classList.remove("dx-scan");
    c.style.removeProperty("--k");
    var b = c.querySelector(":scope > .dx-beam");
    if (b) b.remove();
  }

  function buildExtra() {
    var extra = $("topbar-extra");
    if (!extra) return null;
    var tc = el("div", "dx-tc");
    el("span", "dx-lab", "Time axis", tc);
    built.tcv = el("b", null, "00:00:00:00", tc);
    extra.insertBefore(tc, extra.firstChild);
    move(document.querySelector(".sidebar-footer"), extra);
    return tc;
  }

  var t0 = performance.now(), tick = null, n = 0;
  function onTick() {
    if (!built || document.hidden) return;
    var ms = performance.now() - t0, s = Math.floor(ms / 1000);
    built.tcv.textContent = pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60) + ":" + pad(Math.floor(ms / 1000 * 12) % 12);
    if (++n % 18 === 0) built.tags.forEach(function (t) { t.node.textContent = t.f(); });
  }

  /* ---------------- 装 / 拆 ---------------- */
  var sweepTimer = null, ro = null;
  function build() {
    var main = $("main"), topbar = $("topbar"), sidebar = $("sidebar");
    if (!main || !topbar || !sidebar) return;
    built = { moved: [] };
    built.deco = buildDeco();
    built.front = buildFront();
    built.axis = buildAxis(main);
    built.rail = buildRail(sidebar);
    built.mast = buildMast(topbar);
    built.extra = buildExtra();
    syncMast(true);
    var last = "";
    built.obs = new MutationObserver(function () {
      var key = ($("page-code") || {}).textContent + "|" + ($("page-title") || {}).textContent;
      if (key === last) return;
      last = key;
      syncMast(true);
    });
    ["page-code", "page-title"].forEach(function (id) {
      var nd = $(id);
      if (nd) built.obs.observe(nd, { childList: true, characterData: true, subtree: true });
    });
    // 导航项显示 / 隐藏（多实例、远程模式）或窗口变宽窄时，X 重新对准
    built.navObs = new MutationObserver(placeMarker);
    built.navObs.observe($("nav"), { attributes: true, subtree: true, attributeFilter: ["class", "hidden"] });
    if (window.ResizeObserver) { ro = new ResizeObserver(placeMarker); ro.observe(built.axis); }
    window.addEventListener("mousemove", onMove, { passive: true });
    document.addEventListener("mouseleave", onLeave);
    window.addEventListener("mousedown", onClick, true);
    tick = setInterval(onTick, 83);
    if (!reduce) sweepTimer = setInterval(sweep, 11000);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(placeMarker);
  }

  function teardown() {
    if (!built) return;
    built.obs.disconnect();
    built.navObs.disconnect();
    if (ro) { ro.disconnect(); ro = null; }
    clearInterval(decodeTimer); clearInterval(tick); clearInterval(sweepTimer); clearTimeout(scanTimer);
    window.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseleave", onLeave);
    window.removeEventListener("mousedown", onClick, true);
    document.querySelectorAll(".dx-scan").forEach(unscan);
    restoreMoved();
    [built.deco, built.front, built.burst, built.axis, built.rail, built.mast, built.extra].forEach(function (nd) {
      if (nd && nd.parentNode) nd.remove();
    });
    built = null;
  }

  function sync() {
    if (isMine()) { if (!built || !built.deco.isConnected) { teardown(); build(); } }
    else teardown();
  }

  // 开屏结束时调：主界面从一条横线里展开，随后扫一遍当前页
  window.DissocUI = {
    intro: function () {
      if (!isMine()) return;
      root.classList.add("dx-intro");
      setTimeout(function () { root.classList.remove("dx-intro"); syncMast(true); }, 800);
    },
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync);
  else sync();
  window.addEventListener("wwy-theme", function () { setTimeout(sync, 0); });
})();
