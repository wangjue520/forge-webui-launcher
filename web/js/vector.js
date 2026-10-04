/* vector.js — 「矢量突破」风格专属的界面元素（只在 vector 风格下存在，切走自动拆掉、原样还原）
 *
 *  1. 页头大字排版块（#vb-mast）—— 照 PV 左上角那组字排：
 *       [标签列]  WWY LAUNCHER            ← 两行青色大字，字距 -0.065em、行距 0.74 互相咬合
 *       [标签列]  MODELS “06”             ← 第二行 = 当前页英文名 + 弯引号编号
 *       [标签]    模型管理                ← 墨黑中文页名（真正要读的）
 *                 TRIAL FROM LOCAL NODE   ← 压在下面的一行青色小字
 *     内容跟着 core.js 写进 #page-code（"06 // MODELS"）和 #page-title 的文字走，
 *     用 MutationObserver 监听，不改 core.js 的导航逻辑。
 *  2. 页头右侧：●●● NODE 胶囊、三行说明小字（时钟沿用原来的 #topbar-clock）
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
    el("span", "vb-lab", "Terminal", en);
    el("div", "vb-big vbm-l1", "WWY LAUNCHER", en);
    el("span", "vb-lab", "Numbers", en);
    var l2 = el("div", "vb-big vbm-l2", null, en);
    el("span", "vbm-word", "", l2);
    var q = el("span", "vbm-q", null, l2);
    q.appendChild(document.createTextNode("“"));
    el("i", "vbm-num", "00", q);
    q.appendChild(document.createTextNode("”"));

    var cn = el("div", "vbm-cn", null, m);
    el("span", "vb-lab", "Module", cn);
    el("div", "vbm-title", "", cn);
    el("div", "vbm-under", "TRIAL FROM LOCAL NODE", cn);
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
    // 同步到收起态的灵动岛胶囊（只放中文页名，去掉 " · " 后面的副标题）
    syncIsland(t[0], animate);
    var numNode = m.querySelector(".vbm-num");
    if (animate) rollNum(numNode, num); else numNode.textContent = num;
    if (animate) {
      m.classList.remove("vbm-in");
      void m.offsetWidth;   // 重新触发动画
      m.classList.add("vbm-in");
    }
  }

  function buildExtra() {
    var extra = $("topbar-extra");
    if (!extra) return {};
    var node = el("div", "vbm-node");
    el("i", null, null, node);
    node.appendChild(document.createTextNode("NODE01"));
    extra.insertBefore(node, extra.firstChild);
    var note = el("div", "vbm-note");
    var s = el("span", null, null, note);
    s.innerHTML = "The current module has been synchronized.<br>All data will be archived locally.<br>Please stand by for unknown changes ahead.";
    extra.appendChild(note);
    return { node: node, note: note };
  }

  /* ---------------- 收起态：居中的「灵动岛」小胶囊 ----------------
   *  顶栏收起时，它从顶部中央鼓出来，只显示当前页名；点一下平滑回顶。
   *  宽度不能从 auto 过渡，所以内部包一层 max-content 的 .vbi-in，
   *  用 ResizeObserver 实测它的宽度写到胶囊上，CSS 再对 width 做回弹过渡 ——
   *  换页时页名变长变短，胶囊也会像灵动岛一样平滑伸缩。
   *  入场三段式（见 startIntro）：先鼓出一个较长的空胶囊 → 回弹缩短到实测宽度 → 文字浮现。
   *  入场期间 st.hold = true：实测宽度照常记录，但先不写到胶囊上。 */
  function buildIsland(main, topbar) {
    var b = el("button", "vb-island");
    b.id = "vb-island";
    b.type = "button";
    b.tabIndex = -1;                       // 展开态不进 Tab 序列，收起时再打开
    b.setAttribute("aria-hidden", "true");
    b.title = "回到顶部";
    var w = el("span", "vbi-in", null, b);
    el("i", "vbi-dot", null, w);
    el("span", "vbi-title", "", w);
    b.addEventListener("click", function () {
      var sc = $("content");
      if (!sc) return;
      var reduceMotion = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
      sc.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    });
    main.insertBefore(b, topbar.nextSibling);
    // 用 offsetWidth（布局宽度，不受胶囊自身 scale 形变影响）；宽度没变就不重复写，避免无谓地重启过渡。
    // 宽度过渡只在收起态生效（见 vector.css）：展开态（胶囊隐藏）时改宽度瞬间到位，
    // 下次鼓出来时已经是正确宽度，不会和鼓出动画叠在一起产生跳变
    var st = { lastW: -1, hold: false };
    // force === true：宽度没变也强制写一次（入场缩短那一步要用）
    var fit = function (force) {
      if (!w.isConnected) return;
      var px = w.offsetWidth + 2;   // +2 = 左右描边
      if (px === st.lastW && force !== true) return;
      st.lastW = px;
      if (!st.hold) b.style.width = px + "px";
    };
    var ro = null;
    if (window.ResizeObserver) { ro = new ResizeObserver(function () { fit(); }); ro.observe(w); }
    fit();
    return { island: b, ro: ro, fit: fit, st: st };
  }

  // 胶囊入场编排：较长的空胶囊鼓出来 → 缩短到页名宽度 → 文字浮现
  var ISL_START_MIN = 240;    // 初始空胶囊的最小宽度
  var ISL_START_PAD = 72;     // 初始宽度至少比最终宽度多这么多，保证「明显更长」
  var INTRO_SHRINK = 360;     // 写入实测宽度的时刻（胶囊已过鼓出的过冲顶点）
  var INTRO_TEXT = 600;       // 摘掉 vbi-intro、文字浮现的时刻（缩短的回弹已大半完成）
  var introTimers = [];
  function clearIntro() {
    introTimers.forEach(clearTimeout);
    introTimers = [];
    if (!built || !built.island) return;
    built.island.classList.remove("vbi-intro");
    if (built.islandSt && built.islandSt.hold) {
      built.islandSt.hold = false;
      built.islandFit(true);
    }
  }
  // 必须在给 #main 挂 vb-collapsed 之前调用：展开态下胶囊宽度不过渡，初始宽度瞬间到位，
  // 切到收起态后宽度过渡才生效，缩短那一步就会带回弹
  function startIntro() {
    clearIntro();
    if (!built || !built.island) return;
    var b = built.island, st = built.islandSt;
    built.islandFit();                          // 确保 lastW 是最新实测值
    var fw = st.lastW > 0 ? st.lastW : b.offsetWidth;
    st.hold = true;
    b.classList.add("vbi-intro");               // 内层文字先隐藏 → 空胶囊
    b.style.width = Math.max(ISL_START_MIN, fw + ISL_START_PAD) + "px";
    void b.offsetWidth;                         // 在展开态（width 0s）下先落定，避免和收起态的宽度过渡叠在一起
    introTimers.push(setTimeout(function () {
      if (!built || !built.island) return;
      built.islandSt.hold = false;
      built.islandFit(true);                    // 写入实测宽度 → CSS 回弹缩短
    }, INTRO_SHRINK));
    introTimers.push(setTimeout(function () {
      if (built && built.island) built.island.classList.remove("vbi-intro");   // 文字浮现
    }, INTRO_TEXT));
  }

  function syncIsland(title, animate) {
    if (!built || !built.island) return;
    var tn = built.island.querySelector(".vbi-title");
    if (tn.textContent === title) return;
    tn.textContent = title;
    if (!built.islandRO) built.islandFit();   // 没有 ResizeObserver 时手动量一次
    // 只有胶囊正显示着（收起态）才播换字动画；展开态胶囊是隐藏的，直接换字，免得下次鼓出时还在播半截动画
    var shown = mainEl && mainEl.classList.contains("vb-collapsed");
    tn.classList.remove("vbi-swap");
    if (animate && shown) {
      void tn.offsetWidth;   // 重新触发换字动画
      tn.classList.add("vbi-swap");
    }
  }

  // 收起 / 展开时切换胶囊的可聚焦状态
  function setIsland(collapsed) {
    if (!built || !built.island) return;
    var b = built.island;
    b.tabIndex = collapsed ? 0 : -1;
    b.setAttribute("aria-hidden", collapsed ? "false" : "true");
    if (!collapsed && document.activeElement === b) b.blur();
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

  /* ---------------- 顶栏随滚动收起（移植自 liquid.js 的大标题收起） ----------------
   *  liquid 的做法：页头浮在内容上方，#content 往下滚就给 #main 加类收起，
   *  回到顶部附近再去掉。vector 的顶栏原本在文档流里，这里同样改成浮层（见 vector.css），
   *  内容顶部留出实测的顶栏高度（--vb-top-h），收起/展开都不改变布局。 */
  var scroller = null, mainEl = null, barRO = null;
  // 滞后（hysteresis）：下滚超过 COLLAPSE_AT 才收起，回到 EXPAND_AT 以下才展开，
  // 中间这段来回滚不会反复切换
  var COLLAPSE_AT = 40, EXPAND_AT = 8;
  // 切换后锁定一段时间，期间不反转方向；锁定结束后按当时的滚动位置再判定一次，保证最终状态正确。
  // 锁定时长必须 ≥ 该方向所有动画（vector.css 的 keyframes / 过渡 + 上面 startIntro 的分段计时）
  // 「delay + 时长」的最大值：
  //   收起：顶栏抹除 .5s；胶囊鼓出 .06s + .62s = .68s；缩短 .36s + .6s = .96s；
  //         文字浮现 .6s + .4s = 1.0s；落定闪光 .64s + .34s = .98s → 1020ms
  //   展开：胶囊吸走 .04s + .4s = .44s；顶栏显现 .06s + .5s = .56s；
  //         辉光线淡入 .5s + .26s = .76s → 780ms
  var LOCK_COLLAPSE = 1020, LOCK_EXPAND = 780;
  var lockUntil = 0, lockTimer = 0, animTimer = 0;
  var ANIM_CLS = ["vb-anim-collapse", "vb-anim-expand"];
  // 切换瞬间挂上动画类（重触发 keyframes），锁定时长到点后摘掉。
  // 用计时器而不是 animationend：多个元素各自触发，计时器更可靠；
  // keyframes 终点 = 静止态，摘类不会跳
  function playAnim(collapsed) {
    if (!mainEl) return;
    clearTimeout(animTimer);
    animTimer = 0;
    mainEl.classList.remove(ANIM_CLS[0], ANIM_CLS[1]);
    if (prefersReduce()) return;       // 减弱动效：只走 CSS 里的简单淡入淡出
    mainEl.classList.add(collapsed ? ANIM_CLS[0] : ANIM_CLS[1]);
    animTimer = setTimeout(function () {
      animTimer = 0;
      if (mainEl) mainEl.classList.remove(ANIM_CLS[0], ANIM_CLS[1]);
    }, collapsed ? LOCK_COLLAPSE : LOCK_EXPAND);
  }
  function prefersReduce() {
    return !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  function recheckLater(ms) {
    clearTimeout(lockTimer);
    lockTimer = setTimeout(function () { lockTimer = 0; onScroll(); }, ms + 16);
  }
  function onScroll() {
    if (!built || !scroller || !mainEl) return;
    var cur = mainEl.classList.contains("vb-collapsed");
    var top = scroller.scrollTop;
    var want = cur ? top >= EXPAND_AT : top > COLLAPSE_AT;
    if (want === cur) return;
    var now = Date.now();
    if (now < lockUntil) { recheckLater(lockUntil - now); return; }   // 过渡中：先不反转，结束后再看
    // 收起且允许动效：先布置空胶囊（必须在挂 vb-collapsed 之前）；其余情况清掉残留的入场状态
    if (want && !prefersReduce()) startIntro(); else clearIntro();
    mainEl.classList.toggle("vb-collapsed", want);
    setIsland(want);
    playAnim(want);
    lockUntil = prefersReduce() ? 0 : now + (want ? LOCK_COLLAPSE : LOCK_EXPAND);
  }
  function syncBarH() {
    var bar = $("topbar");
    if (bar && mainEl) mainEl.style.setProperty("--vb-top-h", bar.offsetHeight + "px");
  }
  function watchScroll() {
    scroller = $("content"); mainEl = $("main");
    var bar = $("topbar");
    if (!scroller || !mainEl) return;
    scroller.addEventListener("scroll", onScroll, { passive: true });
    if (window.ResizeObserver && bar) { barRO = new ResizeObserver(syncBarH); barRO.observe(bar); }
    syncBarH();
    onScroll();
  }
  function unwatchScroll() {
    if (scroller) scroller.removeEventListener("scroll", onScroll);
    if (barRO) barRO.disconnect();
    barRO = null;
    clearTimeout(lockTimer);
    clearTimeout(animTimer);
    lockTimer = 0; lockUntil = 0; animTimer = 0;
    if (mainEl) {
      mainEl.classList.remove("vb-collapsed", ANIM_CLS[0], ANIM_CLS[1]);
      mainEl.style.removeProperty("--vb-top-h");
    }
    scroller = mainEl = null;
  }

  /* ---------------- 装 / 拆 ---------------- */
  function build() {
    var main = $("main"), topbar = $("topbar");
    if (!main || !topbar) return;
    built = { deco: buildDeco(), front: buildFront(), mast: buildMast(topbar) };
    // 灵动岛胶囊要在 syncMast 之前建好，首次同步时就能写入页名
    var isl = buildIsland(main, topbar);
    built.island = isl.island; built.islandRO = isl.ro; built.islandFit = isl.fit; built.islandSt = isl.st;
    var ex = buildExtra();
    built.node = ex.node; built.note = ex.note;
    built.brandHTML = buildBrand();
    syncMast(true);
    var last = "";
    built.obs = new MutationObserver(function () {
      // 换页后新页面可能更短、滚动位置被夹回顶部，顺便更新顶栏收起状态（同 liquid）
      setTimeout(onScroll, 0);
      var key = ($("page-code") || {}).textContent + "|" + ($("page-title") || {}).textContent;
      if (key === last) return;
      last = key;
      syncMast(true);
    });
    ["page-code", "page-title"].forEach(function (id) {
      var n = $(id);
      if (n) built.obs.observe(n, { childList: true, characterData: true, subtree: true });
    });
    watchScroll();
  }

  function teardown() {
    if (!built) return;
    built.obs.disconnect();
    clearInterval(numTimer);
    clearIntro();
    unwatchScroll();
    if (built.islandRO) built.islandRO.disconnect();
    [built.deco, built.front, built.mast, built.node, built.note, built.island].forEach(function (n) { if (n && n.parentNode) n.remove(); });
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
