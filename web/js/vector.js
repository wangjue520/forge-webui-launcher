/* vector.js — 「矢量突破」风格专属的界面元素（只在 vector 风格下存在，切走自动拆掉、原样还原）
 *
 *  1. 页头大字排版块（#vb-mast）—— 照 PV 左上角那组字排：
 *       [标签列]  WWY LAUNCHER            ← 两行青色大字，字距 -0.065em、行距 0.74 互相咬合
 *       [标签列]  MODELS “06”             ← 第二行 = 当前页英文名 + 弯引号编号
 *       [标签]    模型管理                ← 墨黑中文页名（真正要读的）
 *                 LOCAL / COMFYUI / IDLE  ← 压在下面的一行小字：本机或远程 + 当前实例 + 运行状态
 *     内容跟着 core.js 写进 #page-code（"06 // MODELS"）和 #page-title 的文字走，
 *     用 MutationObserver 监听，不改 core.js 的导航逻辑。
 *     标签列和小字都是真实数据（版本号、总页数、实例和状态），不放编出来的英文。
 *  2. 页头右侧：●●● NODE 胶囊（实例数，亮几颗 = 几个在运行）、三行小字（日期 / 版本 / 运行数），
 *     时钟沿用原来的 #topbar-clock
 *  3. 侧栏品牌：WWY 大字 + 启动器
 *  4. 背景：烘焙好的远 / 中 / 近三张立体场景图层（翻页时分层视差）；缺图时退回
 *     实时场景（js/vector-scene.js：方块 + 软球 + 景深）与淡等高环，
 *     侧栏 / 页头 / 卡片的毛玻璃透出的就是它；前景：右缘虚线箭头阵列、准星；
 *     会融合的水滴团放在页头空地里
 *  纯视觉，不碰业务逻辑；样式见 themes/vector.css。
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var NS = "http://www.w3.org/2000/svg";
  var built = null;      // { deco, mast, node, note, brandHTML, obs }

  function isVector() { return root.dataset.uiTheme === "vector"; }
  function $(id) { return document.getElementById(id); }

  function svg(tag, attrs, parent) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function el(tag, cls, text, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }

  // 固定种子：每次打开位置一样，界面不会「每次长得不一样」
  var seed = 7;
  function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }

  /* ---------------- 背景装饰 ---------------- */
  function buildRings() {
    var s = svg("svg", { "class": "vb-rings", viewBox: "0 0 940 940", "aria-hidden": "true" });
    var defs = svg("defs", {}, s);
    var f = svg("filter", { id: "vb-wobble", x: "-10%", y: "-10%", width: "120%", height: "120%" }, defs);
    svg("feTurbulence", { type: "fractalNoise", baseFrequency: "0.0055", numOctaves: "2", seed: "4", result: "n" }, f);
    svg("feDisplacementMap", { "in": "SourceGraphic", in2: "n", scale: "80", xChannelSelector: "R", yChannelSelector: "G" }, f);
    var g = svg("g", { "class": "vb-ring-g", filter: "url(#vb-wobble)", fill: "none", stroke: "#1ec8cc" }, s);
    for (var i = 1; i <= 28; i++) {
      var major = i % 5 === 0;
      svg("circle", {
        cx: 470, cy: 470, r: 20 + i * 16,
        "stroke-width": major ? 1.6 : .9,
        "stroke-opacity": major ? .8 : .42,
        "stroke-dasharray": i > 24 ? "3 4" : "",
      }, g);
    }
    return s;
  }

  // 页头软球簇：纯 CSS 渐变小球（不用 SVG 滤镜——滤镜每帧重算会卡），每颗只做合成器位移
  function buildBlobs() {
    var c = el("div", "vbm-cluster");
    // [相对簇中心的 x, y, 直径]，按 200×96 设计
    var B = [[-6, 2, 44], [-30, -14, 28], [20, -18, 26], [26, 14, 34], [-26, 20, 24], [-52, 4, 15],
             [2, -34, 16], [52, -4, 15], [-78, -22, 9], [74, 26, 8], [-62, 34, 7], [88, -30, 6]];
    B.forEach(function (b) {
      var n = el("i", null, null, c);
      n.style.setProperty("--l", b[0]); n.style.setProperty("--t", b[1]); n.style.setProperty("--s", b[2]);
      n.style.setProperty("--x", ((rnd() - .5) * 8).toFixed(1) + "px");
      n.style.setProperty("--y", (-3 - rnd() * 6).toFixed(1) + "px");
      n.style.setProperty("--p", (4.5 + rnd() * 4).toFixed(1) + "s");
      n.style.setProperty("--d", (-rnd() * 6).toFixed(1) + "s");
    });
    [[4, -8, 18], [-20, 6, 11]].forEach(function (b) {
      var n = el("i", "ring", null, c);
      n.style.setProperty("--l", b[0]); n.style.setProperty("--t", b[1]); n.style.setProperty("--s", b[2]);
      n.style.setProperty("--p", "7s");
    });
    return c;
  }

  // 背景层：铺满窗口、在侧栏和内容后面 —— 立体场景 + 淡等高环
  //   默认用烘焙好的三张透明图层（media/vector-bg-*.webp，tools/render_vector_backdrop.py 生成），
  //   运行时不再实时渲染 3D；图片缺失 / 地址带 ?bg=live 时退回实时场景
  function buildLive(layer) {
    layer.appendChild(buildRings());
    if (window.VBScene) layer.appendChild(VBScene.build(VBScene.MAIN, { stagger: .07 }));
  }
  function buildBaked(layer) {
    var box = el("div", "vb3d vb-baked", null, layer);
    var failed = false;
    ["far", "mid", "front"].forEach(function (name) {
      var img = el("img", "vbl " + name, null, box);
      img.alt = ""; img.decoding = "async";
      img.onerror = function () {
        if (failed) return;
        failed = true;
        box.remove();
        buildLive(layer);
      };
      img.src = "media/vector-bg-" + name + ".webp";
    });
  }
  function buildDeco() {
    var layer = el("div");
    layer.id = "vb-deco";
    layer.setAttribute("aria-hidden", "true");
    if (/[?&]bg=live\b/.test(location.search)) buildLive(layer); else buildBaked(layer);
    var lay = $("layout");
    document.body.insertBefore(layer, lay || document.body.firstChild);
    return layer;
  }
  // 前景层：右缘虚线箭头阵列 + 准星（不挡点击）
  function buildFront() {
    var f = el("div");
    f.id = "vb-front";
    f.setAttribute("aria-hidden", "true");
    el("div", "vb-arrows", null, f);
    el("i", "vb-cross c1", null, f);
    el("i", "vb-cross c2", null, f);
    document.body.appendChild(f);
    return f;
  }

  /* ---------------- 页头大字排版块 ---------------- */
  function buildMast(topbar) {
    var m = el("div");
    m.id = "vb-mast";
    m.setAttribute("aria-hidden", "true");   // 屏幕阅读器继续读原来的 #page-title
    var en = el("div", "vbm-en", null, m);
    el("span", "vb-lab vbm-ver", "Launcher", en);         // 拿到版本号后换成 V3.1.xxx
    el("div", "vb-big vbm-l1", "WWY LAUNCHER", en);
    el("span", "vb-lab vbm-of", "Page", en);              // 「OF 10」：一共几页（侧栏里看得见的）
    var l2 = el("div", "vb-big vbm-l2", null, en);
    el("span", "vbm-word", "", l2);
    var q = el("span", "vbm-q", null, l2);
    q.appendChild(document.createTextNode("“"));
    el("i", "vbm-num", "00", q);
    q.appendChild(document.createTextNode("”"));

    var cn = el("div", "vbm-cn", null, m);
    el("span", "vb-lab", "Module", cn);
    el("div", "vbm-title", "", cn);
    el("div", "vbm-under", "LOCAL", cn);                  // 本机 / 远程主机 + 当前实例 + 运行状态（见 syncLive）
    // 水滴团放进页头的弹性空地里：页名长、窗口窄时自动缩小 / 让位，不会压到文字
    var bw = el("div", "vbm-blobs", null, m);
    bw.appendChild(buildBlobs());
    topbar.insertBefore(m, $("topbar-extra"));
    return m;
  }

  // 编号换页时乱码滚两下再落定（PV 里数字跳变的感觉）
  var numTimer = null;
  function rollNum(node, final) {
    clearInterval(numTimer);
    var n = 0;
    numTimer = setInterval(function () {
      if (++n > 6) { clearInterval(numTimer); node.textContent = final; return; }
      node.textContent = String((Math.random() * 100) | 0).padStart(2, "0");
    }, 38);
  }

  function syncMast(animate) {
    if (!built) return;
    var m = built.mast;
    var code = ($("page-code") && $("page-code").textContent) || "";
    var title = ($("page-title") && $("page-title").textContent) || "";
    var parts = code.split("//");
    var num = (parts[0] || "").trim() || "00";
    var word = (parts[1] || code).trim();
    var t = title.split(" · ");
    m.querySelector(".vbm-word").textContent = word;
    var tn = m.querySelector(".vbm-title");
    tn.textContent = t[0];
    if (t[1]) el("small", null, t.slice(1).join(" · "), tn);
    // 翻页视差：背景场景按页码整体滑一段，近处的方块滑得多、远处的少（只在翻页时动，空闲零开销）
    var sc = built.deco && built.deco.querySelector(".vb3d");
    var px = -((parseInt(num, 10) || 1) - 1) * 6;
    if (sc) sc.style.setProperty("--px", px + "px");
    var rg = built.deco && built.deco.querySelector(".vb-rings");
    if (rg) rg.style.transform = "translate3d(" + (px * .35).toFixed(1) + "px, 0, 0)";
    var numNode = m.querySelector(".vbm-num");
    if (animate) rollNum(numNode, num); else numNode.textContent = num;
    if (animate) {
      m.classList.remove("vbm-in");
      void m.offsetWidth;   // 重新触发动画
      m.classList.add("vbm-in");
      kick();
    }
    syncLive();
  }

  /* 装饰元素只在「换页」这一下动：右缘箭头阵列、侧栏箭头、页头软球各走一小段后停住，
     不再一直循环（每个动效对应一次操作）。减少动态效果时 CSS 直接关掉 */
  var kickTimer = 0;
  function kick() {
    clearTimeout(kickTimer);
    root.classList.remove("vb-kick");
    void root.offsetWidth;
    root.classList.add("vb-kick");
    kickTimer = setTimeout(function () { root.classList.remove("vb-kick"); }, 1500);
  }

  /* 页头里原来写死的装饰字（TERMINAL / NUMBERS / TRIAL FROM LOCAL NODE / NODE01 / 三行英文说明）
     换成真实数据：版本、总页数、本机 / 远程 + 当前实例 + 运行状态、实例数和运行中的个数、日期 */
  var STATE_EN = { idle: "IDLE", stopped: "STOPPED", starting: "STARTING", ready: "RUNNING", running: "RUNNING" };
  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }
  function syncLive() {
    if (!built) return;
    var m = built.mast;
    var ver = "";
    try { ver = String((App.state && App.state.launcher && App.state.launcher.version) || "").replace(/^v/i, ""); } catch (e) {}
    setText(m.querySelector(".vbm-ver"), ver ? "V" + ver : "Launcher");
    var pages = document.querySelectorAll(".nav-item:not([hidden])").length;
    setText(m.querySelector(".vbm-of"), pages ? "Of " + pad2(pages) : "Page");

    var list = (window.App && App.instances && App.instances.instances) || [];
    var activeId = window.App && App.instances && App.instances.active;
    var cur = null, running = 0;
    list.forEach(function (i) {
      var st = (i.status && i.status.state) || "idle";
      if (st === "running" || st === "ready" || st === "starting") running++;
      if (i.id === activeId) cur = i;
    });
    var remote = document.body.classList.contains("remote-mode");
    var where = remote ? "REMOTE " + (($("link-banner-name") || {}).textContent || "").split("（")[0].trim() : "LOCAL";
    var gs = $("global-status");
    var gsState = (gs && gs.dataset.state) || "idle";
    var state = STATE_EN[gsState] || "IDLE";
    // 有实例在运行（任一实例 running / ready，或侧栏状态是运行中）→ 深色
    var anyRunning = gsState === "running" || gsState === "ready" || list.some(function (i) {
      var st = i.status && i.status.state;
      return st === "running" || st === "ready";
    });
    setDark(anyRunning);
    if (anyRunning && !running) running = 1;   // 实例列表还没到时，至少按侧栏状态亮一颗
    var parts = [where.trim()];
    if (cur && cur.name) parts.push(String(cur.name).toUpperCase());
    parts.push(state);
    setText(m.querySelector(".vbm-under"), parts.join(" / "));

    if (built.node) {
      setText(built.node.querySelector("b"), "NODE" + pad2(list.length || 1));
      built.node.style.setProperty("--on", String(Math.min(3, running)));
      built.node.title = list.length + " 个实例，" + running + " 个在运行";
    }
    if (built.note) {
      var d = new Date();
      var lines = [d.getFullYear() + "." + pad2(d.getMonth() + 1) + "." + pad2(d.getDate()),
                   ver ? "Version " + ver : "Version -",
                   running + " of " + (list.length || 1) + " running"];
      var s = built.note.querySelector("span");
      var html = lines.join("<br>");
      if (s && s.innerHTML !== html) s.innerHTML = html;
    }
  }

  /* 深色：有实例在运行时整套换成深色（样式见 themes/vector.css「三期」）。
     第一次判定（刚打开、刚切到这个风格）直接切；之后状态变化时，新配色从侧栏的运行状态处
     以圆形扩散揭开（约 3000px/s，240–600ms）。开屏还在 / 系统要求减少动态效果 / 不支持
     View Transitions 时直接切换。 */
  var reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  function setDark(on) {
    if (!built) return;
    var cur = root.classList.contains("vb-dark");
    if (built.darkKnown && cur === on) return;
    var first = !built.darkKnown;
    built.darkKnown = true;
    if (cur === on) return;
    var apply = function () { root.classList.toggle("vb-dark", on); };
    if (first || reduceMotion || $("splash") || !document.startViewTransition) { apply(); return; }
    var pill = $("global-status");
    var r = pill ? pill.getBoundingClientRect() : { left: 0, top: innerHeight, width: 0, height: 0 };
    var x = r.left + r.width / 2, y = r.top + r.height / 2;
    var rad = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    var dur = Math.max(240, Math.min(600, rad / 3));
    root.classList.add("vb-theming");
    var vt;
    try { vt = document.startViewTransition(apply); } catch (e) { root.classList.remove("vb-theming"); apply(); return; }
    vt.ready.then(function () {
      root.animate({ clipPath: ["circle(0px at " + x + "px " + y + "px)", "circle(" + rad + "px at " + x + "px " + y + "px)"] },
        { duration: dur, easing: "cubic-bezier(.7, 0, .2, 1)", pseudoElement: "::view-transition-new(root)" });
    }).catch(function () {});
    vt.finished.then(function () { root.classList.remove("vb-theming"); }, function () { root.classList.remove("vb-theming"); });
  }

  function buildExtra() {
    var extra = $("topbar-extra");
    if (!extra) return {};
    var node = el("div", "vbm-node");
    el("i", null, null, node);
    el("b", null, "NODE01", node);
    extra.insertBefore(node, extra.firstChild);
    var note = el("div", "vbm-note");
    el("span", null, null, note);
    extra.appendChild(note);
    return { node: node, note: note };
  }

  /* ---------------- 侧栏品牌 ---------------- */
  function buildBrand() {
    var name = document.querySelector(".brand-name");
    if (!name) return null;
    var html = name.innerHTML;
    var txt = name.textContent.trim();          // "WWY 启动器"
    var sp = txt.indexOf(" ");
    name.textContent = sp > 0 ? txt.slice(0, sp) : txt;
    if (sp > 0) el("span", "vb-cn", txt.slice(sp + 1), name);
    return html;
  }

  /* ---------------- 装 / 拆 ---------------- */
  function build() {
    var main = $("main"), topbar = $("topbar");
    if (!main || !topbar) return;
    built = { deco: buildDeco(), front: buildFront(), mast: buildMast(topbar) };
    var ex = buildExtra();
    built.node = ex.node; built.note = ex.note;
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
      var n = $(id);
      if (n) built.obs.observe(n, { childList: true, characterData: true, subtree: true });
    });
    // 运行状态 / 实例数会在后台变：低频刷一下（只在文字真的变了才改 DOM）；
    // 侧栏状态一变就立刻刷（深色切换不用等下一轮）
    built.live = setInterval(syncLive, 1500);
    var gs = $("global-status");
    built.gsObs = new MutationObserver(function () { syncLive(); });
    if (gs) built.gsObs.observe(gs, { attributes: true, attributeFilter: ["data-state"] });
  }

  function teardown() {
    if (!built) return;
    built.obs.disconnect();
    if (built.gsObs) built.gsObs.disconnect();
    root.classList.remove("vb-dark", "vb-theming");
    clearInterval(built.live);
    clearInterval(numTimer);
    clearTimeout(kickTimer);
    root.classList.remove("vb-kick");
    [built.deco, built.front, built.mast, built.node, built.note].forEach(function (n) { if (n && n.parentNode) n.remove(); });
    var name = document.querySelector(".brand-name");
    if (name && built.brandHTML != null) name.innerHTML = built.brandHTML;
    built = null;
  }

  // 开屏期间 <html> 带 vb-splash（主界面场景按住不飘）；万一开屏被别的途径移除，兜底解除
  setTimeout(function () { if (!$("splash")) root.classList.remove("vb-splash", "vb-splash-hide"); }, 10000);

  function sync() {
    if (isVector()) { if (!built || !built.deco.isConnected) { teardown(); build(); } }
    else teardown();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync);
  else sync();
  window.addEventListener("wwy-theme", function () { setTimeout(sync, 0); });
})();
