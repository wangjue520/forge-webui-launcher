/* dissoc.js — 「离解 · 黑白构成」风格专属的界面元素和动效（只在 dissoc 风格下存在，切走自动拆掉、原样还原）
 *
 *  1. 页头（#dx-mast）：自转的四叶 X + 两行超大英文（当前页英文名 / 编号，翻页时逐字「解码」落定）
 *     + 墨黑中文页名 + 一行小字；右侧加一个一直在走的 TIME AXIS 时间码
 *  2. 侧栏品牌：WWY 大字 + 启动器
 *  3. 背景（#dx-deco）：右下大号四叶 X（慢转、随鼠标轻微视差）、几道斜向细线、散落的 ✦ / + 和会跳数的标注
 *  4. 前景（#dx-front，不挡点击）：主区域里跟随鼠标的十字准线 + 坐标读数；每隔一阵从上往下扫过的扫描线；
 *     点击处爆开一个小 X
 *  5. 翻页：页面里的卡片被一道扫描光束依次「扫」出来
 *  内容跟着 core.js 写进 #page-code（"06 // MODELS"）和 #page-title 的文字走（MutationObserver），
 *  不碰导航和业务逻辑；样式见 themes/dissoc.css。系统开了「减少动态效果」时只留静态装饰。
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var NS = "http://www.w3.org/2000/svg";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var built = null;

  // 四叶 X（四片弯刀形叶片 + 中心黄绿点）、四角星 ✦
  var X_SVG = '<svg viewBox="-50 -50 100 100" aria-hidden="true"><g fill="currentColor">' +
    '<path d="M0 0C6-14 20-30 40-40C30-20 14-6 0 0Z"/><path d="M0 0C14 6 30 20 40 40C20 30 6 14 0 0Z"/>' +
    '<path d="M0 0C-6 14-20 30-40 40C-30 20-14 6 0 0Z"/><path d="M0 0C-14-6-30-20-40-40C-20-30-6-14 0 0Z"/></g>' +
    '<circle r="4.5" fill="#c8f53c"/></svg>';
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

  /* ---------------- 背景 ---------------- */
  function buildDeco() {
    var d = el("div");
    d.id = "dx-deco";
    d.setAttribute("aria-hidden", "true");
    el("div", "dx-bigx", null, d).innerHTML = X_SVG;
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("class", "dx-diag");
    s.setAttribute("preserveAspectRatio", "none");
    s.setAttribute("viewBox", "0 0 1000 1000");
    [[120, 0, 1000, 760], [0, 260, 820, 1000], [380, 0, 1000, 420]].forEach(function (p) {
      var l = document.createElementNS(NS, "line");
      l.setAttribute("x1", p[0]); l.setAttribute("y1", p[1]); l.setAttribute("x2", p[2]); l.setAttribute("y2", p[3]);
      l.setAttribute("vector-effect", "non-scaling-stroke");
      s.appendChild(l);
    });
    d.appendChild(s);
    // 散落的标注（位置固定，每次打开都一样）
    var tags = [
      { l: "31%", t: "24%", k: "CONVOLUTION", f: function () { return (0.6 + Math.random() * .05).toFixed(4); } },
      { l: "78%", t: "16%", k: "MESSAGE", f: function () { return "0x" + ((Math.random() * 0xffff) | 0).toString(16).toUpperCase().padStart(4, "0"); } },
      { l: "54%", t: "88%", k: "DISSOCIATE", f: function () { return pad((Math.random() * 100) | 0) + " / 100"; } },
    ];
    built.tags = tags.map(function (t) {
      var n = el("div", "dx-tag", null, d);
      n.style.left = t.l; n.style.top = t.t;
      el("span", "dx-lab", t.k, n);
      var v = el("span", "v", t.f(), n);
      return { node: v, f: t.f };
    });
    [["44%", "12%"], ["90%", "62%"], ["27%", "72%"]].forEach(function (p, i) {
      var st = el("i", "dx-star", null, d);
      st.innerHTML = STAR_SVG;
      st.style.left = p[0]; st.style.top = p[1]; st.style.animationDelay = (-i * 1.5) + "s";
    });
    [["62%", "34%"], ["36%", "52%"], ["84%", "84%"], ["70%", "8%"]].forEach(function (p) {
      var c = el("i", "dx-plus", null, d);
      c.style.left = p[0]; c.style.top = p[1];
    });
    document.body.insertBefore(d, $("layout") || document.body.firstChild);
    return d;
  }

  /* ---------------- 前景：准线 / 扫描线 / 点击 ---------------- */
  function buildFront() {
    var f = el("div");
    f.id = "dx-front";
    f.setAttribute("aria-hidden", "true");
    built.hx = el("i", "dx-hx", null, f);
    built.vx = el("i", "dx-vx", null, f);
    built.read = el("span", "dx-read", "", f);
    built.sweep = el("i", "dx-sweep", null, f);
    document.body.appendChild(f);
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
    var e = pending;
    if (!built || !e) return;
    var main = $("main");
    var inMain = main && main.contains(e.target) && !document.querySelector("#modal-mask:not([hidden])");
    built.front.classList.toggle("on", !!inMain);
    // 背景大 X 和网格跟着鼠标轻微反向移动（视差）
    var w = window.innerWidth, h = window.innerHeight;
    var dx = (e.clientX / w - .5), dy = (e.clientY / h - .5);
    built.bigx.style.setProperty("--mx", (-dx * 26).toFixed(1) + "px");
    built.bigx.style.setProperty("--my", (-dy * 20).toFixed(1) + "px");
    if (main) {
      main.style.setProperty("--dx-gx", (-dx * 10).toFixed(1) + "px");
      main.style.setProperty("--dx-gy", (-dy * 8).toFixed(1) + "px");
    }
    if (!inMain) return;
    var r = main.getBoundingClientRect();
    built.hx.style.transform = "translate3d(0," + e.clientY + "px,0)";
    built.vx.style.transform = "translate3d(" + e.clientX + "px,0,0)";
    built.hx.style.left = r.left + "px";
    var x = Math.round(e.clientX - r.left), y = Math.round(e.clientY - r.top);
    built.read.textContent = "X " + pad(x, 4) + "  Y " + pad(y, 4);
    var rx = e.clientX + 14, ry = e.clientY + 14;
    if (rx + 110 > w) rx = e.clientX - 118;
    if (ry + 24 > h) ry = e.clientY - 30;
    built.read.style.transform = "translate3d(" + rx + "px," + ry + "px,0)";
  }
  function onLeave() { if (built) built.front.classList.remove("on"); }

  function onClick(e) {
    if (!built || reduce || e.button !== 0) return;
    var b = el("i", "dx-burst", null, built.front);
    b.innerHTML = X_SVG;
    b.style.left = e.clientX + "px"; b.style.top = e.clientY + "px";
    setTimeout(function () { b.remove(); }, 600);
  }

  var sweepN = 0;
  function sweep() {
    if (!built || reduce || document.hidden) return;
    var sb = $("sidebar");
    built.sweep.style.setProperty("--sl", (sb ? sb.getBoundingClientRect().right : 236) + "px");
    built.sweep.setAttribute("data-t", "SCANNING · " + pad(++sweepN % 100));
    built.sweep.classList.remove("go");
    void built.sweep.offsetWidth;
    built.sweep.classList.add("go");
  }

  /* ---------------- 页头 ---------------- */
  function buildMast(topbar) {
    var m = el("div");
    m.id = "dx-mast";
    m.setAttribute("aria-hidden", "true");   // 屏幕阅读器继续读原来的 #page-title
    el("div", "dxm-x", null, m).innerHTML = X_SVG;
    var en = el("div", "dxm-en", null, m);
    var labs = el("div", "dxm-labs dx-lab", null, en);
    labs.innerHTML = "<span>Time axis</span><span>Convolution</span><span>WWY · <b>LAUNCHER</b></span>";
    el("div", "dxm-big", null, en);
    var cn = el("div", "dxm-cn", null, m);
    el("div", "dxm-title", "", cn);
    el("div", "dxm-quote", "You observe the world. The world observes you.", cn);
    topbar.insertBefore(m, $("topbar-extra"));
    return m;
  }

  // 大字逐字「解码」：每个字先跳几个乱码，按顺序落定
  var decodeTimer = null;
  function decode(box, text, animate) {
    clearInterval(decodeTimer);
    box.innerHTML = "";
    var word = el("span", null, null, box);
    var spans = text.split("").map(function (c, i) {
      var g = el("i", null, c === " " ? " " : c, word);
      g.style.setProperty("--i", i);
      return g;
    });
    var num = el("span", "dxm-num", built.num, box);
    if (!animate || reduce) return;
    var t = 0, n = spans.length;
    decodeTimer = setInterval(function () {
      t++;
      var settled = 0;
      spans.forEach(function (g, i) {
        var c = text[i];
        if (c === " ") { settled++; return; }
        if (t > 3 + i * 1.4) { g.textContent = c; settled++; }
        else g.textContent = GLYPHS[(Math.random() * GLYPHS.length) | 0];
      });
      num.textContent = t > 6 + n ? built.num : pad((Math.random() * 100) | 0);
      if (settled >= n && t > 6 + n) clearInterval(decodeTimer);
    }, 34);
  }

  function syncMast(animate) {
    if (!built) return;
    var code = ($("page-code") && $("page-code").textContent) || "";
    var title = ($("page-title") && $("page-title").textContent) || "";
    var parts = code.split("//");
    built.num = (parts[0] || "").trim() || "00";
    var word = (parts[1] || code).trim() || "WWY";
    var m = built.mast;
    decode(m.querySelector(".dxm-big"), word, animate);
    var tn = m.querySelector(".dxm-title"), t = title.split(" · ");
    tn.textContent = t[0];
    if (t[1]) el("small", null, t.slice(1).join(" · "), tn);
    if (animate) {
      m.classList.remove("dx-in");
      void m.offsetWidth;
      m.classList.add("dx-in");
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
    tc.appendChild(document.createTextNode("TIME AXIS"));
    built.tcv = el("b", null, "00:00:00:00", tc);
    extra.insertBefore(tc, extra.firstChild);
    return tc;
  }

  // 时间码（打开以来，12 帧 / 秒）和背景标注的读数
  var t0 = performance.now(), tick = null, n = 0;
  function onTick() {
    if (!built || document.hidden) return;
    var ms = performance.now() - t0, s = Math.floor(ms / 1000);
    built.tcv.textContent = pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60) + ":" + pad(Math.floor(ms / 1000 * 12) % 12);
    if (++n % 18 === 0) built.tags.forEach(function (t) { t.node.textContent = t.f(); });
  }

  /* ---------------- 侧栏品牌 ---------------- */
  function buildBrand() {
    var name = document.querySelector(".brand-name");
    if (!name) return null;
    var html = name.innerHTML;
    var txt = name.textContent.trim();
    var sp = txt.indexOf(" ");
    name.textContent = sp > 0 ? txt.slice(0, sp) : txt;
    if (sp > 0) el("span", "dx-cn", txt.slice(sp + 1), name);
    return html;
  }

  /* ---------------- 装 / 拆 ---------------- */
  var sweepTimer = null;
  function build() {
    var topbar = $("topbar");
    if (!$("main") || !topbar) return;
    built = {};
    built.deco = buildDeco();
    built.bigx = built.deco.querySelector(".dx-bigx");
    built.front = buildFront();
    built.mast = buildMast(topbar);
    built.extra = buildExtra();
    built.brandHTML = buildBrand();
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
    window.addEventListener("mousemove", onMove, { passive: true });
    document.addEventListener("mouseleave", onLeave);
    window.addEventListener("mousedown", onClick, true);
    tick = setInterval(onTick, 83);
    if (!reduce) sweepTimer = setInterval(sweep, 11000);
  }

  function teardown() {
    if (!built) return;
    built.obs.disconnect();
    clearInterval(decodeTimer); clearInterval(tick); clearInterval(sweepTimer); clearTimeout(scanTimer);
    window.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseleave", onLeave);
    window.removeEventListener("mousedown", onClick, true);
    document.querySelectorAll(".dx-scan").forEach(unscan);
    [built.deco, built.front, built.mast, built.extra].forEach(function (nd) { if (nd && nd.parentNode) nd.remove(); });
    var name = document.querySelector(".brand-name");
    if (name && built.brandHTML != null) name.innerHTML = built.brandHTML;
    var main = $("main");
    if (main) { main.style.removeProperty("--dx-gx"); main.style.removeProperty("--dx-gy"); }
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
