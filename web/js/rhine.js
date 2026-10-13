/* rhine.js — 「莱茵 · 分析终端」风格：整个界面就是一座档案库（只在 rhine 风格下存在，切走自动拆掉、原样还原）
 *
 *  三个区域：左 = 目录（#sidebar，一块块浮动的亚克力档案板）
 *            中 = 装饰（三维档案阵列 js/rhine-field.js；当前档案升起，档案信息直接「印」在板面上）
 *            右 = 功能页（原来的 #main，内容不变）
 *  功能页：宽度固定（内容只排一次版），平时整块往右平移、只露出一部分；鼠标进入时平移回来盖住装饰，
 *          回到目录 / 装饰区再让位——只有 transform 在动，不重排、不卡。窗口够宽时整块直接放得下，
 *          装饰区永久保留，不再延伸。
 *          抬头浮在内容上方，往下滚时折叠成一行（淡入一条通栏底色），内容从它下面滚过、位置不受影响。
 *  装饰全屏（<html>.rh-ov）：点中间装饰区进入，目录和功能页都收起来；滚轮 / 拖动 / 方向键切换档案，
 *          点升起的那块档案板（或 Enter）进入对应功能页；Esc 返回。
 *  索引：Ctrl+K / 右上「ARCHIVE INDEX」打开档案索引（可搜索的页面列表）。
 *  内容跟着 core.js 写进 #page-title 的文字走（MutationObserver），不改导航逻辑。
 *  音效：js/rhine-sfx.js（合成音效，不放音乐），底栏可开关。
 *  纯视觉，不碰业务逻辑；样式见 themes/rhine.css，开屏见 js/rhine-splash.js。
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var built = null;

  var META = {
    launch:     { en: "LAUNCH CONTROL",       dept: "运行控制科", col: "核心档案" },
    instances:  { en: "INSTANCE REGISTRY",    dept: "实例登记处", col: "多实例档案" },
    settings:   { en: "RUNTIME PARAMETERS",   dept: "参数工程科", col: "启动参数" },
    launcher:   { en: "SYSTEM CONFIGURATION", dept: "系统维护科", col: "终端配置" },
    deploy:     { en: "ENVIRONMENT DEPLOY",   dept: "环境构建科", col: "部署记录" },
    civitai:    { en: "REMOTE ACQUISITION",   dept: "远程采集科", col: "外部资源" },
    models:     { en: "MODEL ARCHIVE",        dept: "模型档案室", col: "权重样本" },
    outputs:    { en: "OUTPUT GALLERY",       dept: "成果陈列室", col: "生成成果" },
    extensions: { en: "EXTENSION MODULES",    dept: "扩展工程科", col: "功能模块" },
    wd14:       { en: "TAG INFERENCE",        dept: "图像解析科", col: "反推标注" },
    meta:       { en: "IMAGE FORENSICS",      dept: "图像鉴定科", col: "元数据" },
  };
  var WIDE = {};                          // 需要强制铺满的页面（目前没有：模型 / 输出管理也跟其他页一样，悬停时才延伸）
  var PLATE_W = 300;                      // 板面标签的 CSS 宽度（再透视变换贴到板上）

  function isRhine() { return root.dataset.uiTheme === "rhine"; }
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, html, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    if (parent) parent.appendChild(n);
    return n;
  }
  function sfx(n, d, a) { if (window.RhineSfx) RhineSfx.play(n, d, a); }
  function pad(n) { return String(n).padStart(2, "0"); }
  function fileNo(i) { return "X-" + String(i + 1).padStart(3, "0"); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function isOv() { return root.classList.contains("rh-ov"); }

  /* ---------- 乱码解码 ---------- */
  // 只用拉丁字母和数字：「—」「#」「/」在西文字体里没有，会回退到中文字体，行高变大，整行跟着上下跳
  var GLYPH = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789";
  var noDecode = false;   // 开屏交接那一次翻页：直接出真字，白光后面不跑乱码
  function decode(node, text, ms) {
    if (!node) return;
    if (node._rhDec) cancelAnimationFrame(node._rhDec);
    if (reduce || noDecode) { node.textContent = text; return; }
    var t0 = performance.now(), dur = ms || 520;
    (function step(now) {
      var p = Math.min(1, (now - t0) / dur), out = "";
      for (var i = 0; i < text.length; i++) {
        var c = text[i];
        out += (c === " " || p * text.length * 1.3 > i + 2) ? c : GLYPH[(Math.random() * GLYPH.length) | 0];
      }
      node.textContent = out;
      if (p < 1) node._rhDec = requestAnimationFrame(step); else node.textContent = text;
    })(t0);
  }
  function replay(node, cls) {
    if (!node) return;
    node.classList.remove(cls); void node.offsetWidth; node.classList.add(cls);
  }

  /* ---------- 透视贴图：把 w×h 的矩形映射到屏幕上任意四边形（matrix3d 单应变换） ---------- */
  function adj(m) {
    return [m[4] * m[8] - m[5] * m[7], m[2] * m[7] - m[1] * m[8], m[1] * m[5] - m[2] * m[4],
            m[5] * m[6] - m[3] * m[8], m[0] * m[8] - m[2] * m[6], m[2] * m[3] - m[0] * m[5],
            m[3] * m[7] - m[4] * m[6], m[1] * m[6] - m[0] * m[7], m[0] * m[4] - m[1] * m[3]];
  }
  function mm(a, b) {
    var c = new Array(9);
    for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++) {
      c[3 * i + j] = a[3 * i] * b[j] + a[3 * i + 1] * b[3 + j] + a[3 * i + 2] * b[6 + j];
    }
    return c;
  }
  function mv(m, v) { return [m[0] * v[0] + m[1] * v[1] + m[2] * v[2], m[3] * v[0] + m[4] * v[1] + m[5] * v[2], m[6] * v[0] + m[7] * v[1] + m[8] * v[2]]; }
  function basis(p) {
    var m = [p[0], p[2], p[4], p[1], p[3], p[5], 1, 1, 1];
    var v = mv(adj(m), [p[6], p[7], 1]);
    return mm(m, [v[0], 0, 0, 0, v[1], 0, 0, 0, v[2]]);
  }
  function quadMatrix(w, h, q) {
    // 源：左上 / 右上 / 左下 / 右下
    var s = basis([0, 0, w, 0, 0, h, w, h]);
    var d = basis([q.tl.x, q.tl.y, q.tr.x, q.tr.y, q.bl.x, q.bl.y, q.br.x, q.br.y]);
    var t = mm(d, adj(s));
    for (var i = 0; i < 9; i++) t[i] /= t[8];
    return "matrix3d(" + [t[0], t[3], 0, t[6], t[1], t[4], 0, t[7], 0, 0, 1, 0, t[2], t[5], 0, t[8]].map(function (x) { return x.toFixed(6); }).join(",") + ")";
  }

  /* ---------- 页面列表（与目录一致：隐藏的不算） ---------- */
  function pages() {
    return Array.prototype.filter.call(document.querySelectorAll("#nav .nav-item"), function (b) { return !b.hidden; })
      .map(function (b) { return { id: b.dataset.page, cn: (b.querySelector("span") || b).textContent.trim() }; });
  }
  function curPage() { return (window.App && App.currentPage) || "launch"; }

  /* ================= 搭建 ================= */
  function build() {
    if (built) return;
    var b = built = { obs: [], off: [] };
    function on(target, ev, fn, opt) { target.addEventListener(ev, fn, opt); b.off.push(function () { target.removeEventListener(ev, fn, opt); }); }

    /* 世界：画布 + 景深叠层 */
    b.world = el("div", null, '<canvas id="rh-gl"></canvas><div class="rh-dof"></div><div class="rh-veil"></div>');
    b.world.id = "rh-world";
    document.body.insertBefore(b.world, document.body.firstChild);
    try { b.field = window.RhineField ? RhineField(b.world.querySelector("canvas")) : null; } catch (e) { console.warn(e); b.field = null; }
    if (!b.field) root.classList.add("rh-nogl");

    /* 装饰区：拖拽 / 点击面 */
    b.zone = el("div", null, '<div class="rz-hint"><span>CLICK TO EXPAND</span><i></i><span>DRAG TO INSPECT</span></div>');
    b.zone.id = "rh-zone";
    document.body.appendChild(b.zone);

    /* 印在升起档案板上的信息（透视贴图） */
    b.plate = el("div", "rh-plate",
      '<div class="rp-top"><b>WWY LAB, LLC.</b><em>INFO</em><span class="rp-mark"><i></i><i></i></span></div>' +
      '<div class="rp-no">NO.001</div>' +
      '<div class="rp-k"><span>INTERNAL DATABASE</span><i>/</i><span class="rp-col">核心档案</span></div>' +
      '<div class="rp-ring"><i></i></div>' +
      '<div class="rp-en">LAUNCH CONTROL</div>' +
      '<div class="rp-cn">一键启动</div>' +
      '<div class="rp-bot"><span class="rp-dept">运行控制科</span><span class="rp-bar"></span></div>' +
      '<div class="rp-go">ACCESS FILE <span>→</span></div>');
    b.plateLayer = el("div", null, null);
    b.plateLayer.id = "rh-plates";
    b.plateLayer.appendChild(b.plate);
    document.body.appendChild(b.plateLayer);

    /* 装饰全屏 HUD：左上返回、底部选择器、提示 */
    b.ov = el("div", null,
      '<button type="button" class="rh-ovback"><span>←</span>RETURN<kbd>ESC</kbd></button>' +
      '<div class="rh-ovbrand"><b>WWY LAB</b><span>SYNTHESIZE INFORMATION</span><p>ANALYSIS <b>OS</b></p></div>' +
      '<div class="rh-sel">' +
        '<div class="rs-k">ARCHIVE / SELECT</div>' +
        '<div class="rs-n"><b class="rs-i">01</b><i>/</i><span class="rs-t">10</span></div>' +
        '<div class="rs-ctl"><button type="button" class="rs-up">↑</button><div class="rs-ticks"></div><button type="button" class="rs-dn">↓</button></div>' +
      '</div>' +
      '<div class="rh-hint"><span><kbd>WHEEL</kbd><kbd>DRAG</kbd> 切换档案</span><span><kbd>CLICK</kbd> 点档案板进入</span><span><kbd>ESC</kbd> 返回</span></div>');
    b.ov.id = "rh-ov";
    document.body.appendChild(b.ov);

    /* 底栏 */
    b.foot = el("div", null,
      '<span><i class="rh-light"></i>SESSION AUTHORIZED</span>' +
      '<span class="rf-user">WWY LAB<i>/</i><b class="rf-clock">00:00:00</b></span>' +
      '<button type="button" class="rf-sfx">SOUND <b>ON</b></button>' +
      '<span class="rf-pow">POWERED BY <b>WWY LAB</b><i class="rf-bar"></i></span>');
    b.foot.id = "rh-foot";
    document.body.appendChild(b.foot);

    /* 档案索引 */
    b.idx = el("div", null,
      '<div class="ri-box">' +
        '<div class="ri-top"><span>WWY LAB / ARCHIVE DIRECTORY</span><button type="button" class="ri-close">CLOSE <i>✕</i></button></div>' +
        '<h2>ARCHIVE INDEX <small>档案索引</small></h2>' +
        '<label class="ri-search"><i></i><input type="text" placeholder="输入档案编号、名称或部门…" spellcheck="false"><kbd>ESC</kbd></label>' +
        '<div class="ri-head"><span>FILE NO.</span><span>TITLE / 档案</span><span>DEPARTMENT / 部门</span><span>ACCESS</span></div>' +
        '<div class="ri-list"></div>' +
        '<div class="ri-bottom"><span class="ri-count">00 RECORDS FOUND</span><span>INTERNAL DATABASE <i>●</i> CONNECTED</span></div>' +
      '</div>');
    b.idx.id = "rh-index";
    b.idx.hidden = true;
    document.body.appendChild(b.idx);

    /* 目录：品牌 + 抬头 */
    var brand = document.querySelector("#sidebar .brand");
    if (brand) {
      b.brand = el("div", "rh-brand",
        '<div class="rb-h">WWY LAB</div><div class="rb-sub">SYNTHESIZE INFORMATION</div>' +
        '<div class="rb-os">ANALYSIS <b>OS</b></div>', brand);
    }
    b.navHead = el("div", "rh-navhead", '<span>ARCHIVE / FILES</span><b class="rh-navcount">10</b>');
    var nav = $("nav");
    if (nav) nav.parentNode.insertBefore(b.navHead, nav);

    /* 功能页抬头 */
    var top = $("topbar");
    if (top) {
      b.phNav = el("div", "rh-ph-nav",
        '<button type="button" class="rh-back"><span>←</span>ARCHIVE VIEW<kbd>ESC</kbd></button>' +
        '<span class="rh-mini"><b class="rmi-no">X-001</b><i></i><em>未启动</em></span>' +
        '<button type="button" class="rh-find"><i class="rh-glass"></i>ARCHIVE INDEX<kbd>CTRL K</kbd></button>');
      b.kick = el("div", "rh-kick", '<span>FILE <b class="rk-no">X-001</b></span><i></i><span>REFERENCE AREA</span>');
      b.en = el("div", "rh-en", "LAUNCH CONTROL");
      b.meta = el("div", "rh-meta",
        '<div><span>DEPARTMENT / 部门</span><b class="rm-dept">运行控制科</b></div>' +
        '<div><span>COLLECTION / 编目</span><b class="rm-col">核心档案</b></div>' +
        '<div><span>RELATED / 关联节点</span><b class="rm-rel">LOCAL NODE</b></div>' +
        '<div><span>STATUS / 状态</span><b class="rm-st"><i></i><em>未启动</em></b></div>');
      top.insertBefore(b.phNav, top.firstChild);
      top.insertBefore(b.kick, top.querySelector(".page-head"));
      top.insertBefore(b.en, top.querySelector(".page-head"));
      top.appendChild(b.meta);
    }
    b.meas = el("div", "rh-meas", null, document.body);

    /* ---------- 按钮 ---------- */
    b.phNav.querySelector(".rh-back").addEventListener("click", function () { setOverview(true); });
    b.phNav.querySelector(".rh-find").addEventListener("click", openIndex);
    b.ov.querySelector(".rh-ovback").addEventListener("click", function () { setOverview(false); });
    b.ov.querySelector(".rs-up").addEventListener("click", function () { moveSel(-1); });
    b.ov.querySelector(".rs-dn").addEventListener("click", function () { moveSel(1); });
    b.foot.querySelector(".rf-sfx").addEventListener("click", function () { syncSfx(window.RhineSfx ? RhineSfx.toggle() : false); });
    syncSfx(window.RhineSfx ? RhineSfx.enabled() : false);
    b.idx.querySelector(".ri-close").addEventListener("click", closeIndex);
    b.idx.addEventListener("click", function (e) { if (e.target === b.idx) closeIndex(); });
    b.idx.querySelector("input").addEventListener("input", renderIndex);

    /* ---------- 装饰区：点一下全屏；拖动 = 读档时转相机 / 全屏时切档案 ---------- */
    var drag = null;
    function down(e) {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, moved: 0, onPlate: !!e.target.closest(".rh-plate"), t: performance.now() };
      e.currentTarget.setPointerCapture(e.pointerId);
      root.classList.add("rh-dragging");
    }
    function move(e) {
      if (!drag) return;
      drag.moved = Math.max(drag.moved, Math.hypot(e.clientX - drag.x, e.clientY - drag.y));
      if (drag.moved > 6 && b.field) b.field.drag(e.movementX || 0, isOv() ? 0 : (e.movementY || 0), true);
    }
    function up(e) {
      if (!drag) return;
      var d = drag; drag = null;
      root.classList.remove("rh-dragging");
      if (b.field) b.field.drag(0, 0, false);
      var dx = e.clientX - d.x, dy = e.clientY - d.y;
      if (d.moved < 6) {                                   // 点击
        if (isOv()) { if (d.onPlate) openSel(); }
        else setOverview(true);
        return;
      }
      // 全屏时：甩一下切换档案（左 / 上 = 下一份）
      if (isOv() && Math.max(Math.abs(dx), Math.abs(dy)) > 50) {
        var main = Math.abs(dx) > Math.abs(dy) ? dx : dy;
        moveSel(main < 0 ? 1 : -1);
      }
    }
    [b.zone, b.plate].forEach(function (t) {
      on(t, "pointerdown", down);
      on(t, "pointermove", move);
      on(t, "pointerup", up);
      on(t, "pointercancel", function () { drag = null; root.classList.remove("rh-dragging"); if (b.field) b.field.drag(0, 0, false); });
    });
    // 全屏时滚轮切档案（攒够一格才切，触控板细碎滚动不会连跳）
    var wheelAcc = 0, wheelAt = 0;
    on(window, "wheel", function (e) {
      if (!isOv() || !b.idx.hidden) return;
      e.preventDefault();
      var now = performance.now();
      if (now - wheelAt > 400) wheelAcc = 0;
      wheelAcc += Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      if (Math.abs(wheelAcc) >= 60 && now - wheelAt > 230) {
        moveSel(wheelAcc > 0 ? 1 : -1);
        wheelAcc = 0; wheelAt = now;
      }
    }, { passive: false });

    on(window, "keydown", onKey, true);
    on(window, "resize", function () { if (b.field) b.field.resize(); layout(); measureHead(); measureFold(); });

    // 悬停 / 按下：音效 + 黄铜涟漪
    on(document, "pointerover", function (e) {
      var t = e.target.closest && e.target.closest(".rs-ticks button, .ri-row");
      if (t && t !== b.lastHover) { b.lastHover = t; sfx("tick", 0, Array.prototype.indexOf.call(t.parentNode.children, t) + 3); }
    });
    on(document, "pointerdown", function (e) {
      var btn = e.target.closest && e.target.closest(".btn, .rh-back, .rh-find, .rh-ovback, .rs-ctl button, .chip, .cat-item");
      if (!btn || btn.disabled) return;
      sfx("press");
      if (reduce) return;
      var r = btn.getBoundingClientRect(), d = Math.max(r.width, r.height) * 2.4;
      if (getComputedStyle(btn).position === "static") btn.style.position = "relative";
      var rp = el("span", "rh-ripple", null, btn);
      rp.style.cssText = "left:" + (e.clientX - r.left - d / 2) + "px;top:" + (e.clientY - r.top - d / 2) + "px;width:" + d + "px;height:" + d + "px";
      setTimeout(function () { rp.remove(); }, 750);
    }, true);

    /* ---------- 目录档案板：跟手倾斜、悬停预览 ---------- */
    function navIndex(item) { var k = -1; (b.list || pages()).forEach(function (p, j) { if (p.id === item.dataset.page) k = j; }); return k; }
    if (nav) {
      on(nav, "pointermove", function (e) {
        var it = e.target.closest(".nav-item"); if (!it) return;
        var r = it.getBoundingClientRect();
        var x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
        it.style.setProperty("--mx", (x * 100).toFixed(1) + "%");
        it.style.setProperty("--tx", ((x - .5) * 10).toFixed(2) + "deg");
        it.style.setProperty("--ty", ((.5 - y) * 16).toFixed(2) + "deg");
        if (it !== b.hoverItem) {
          if (b.hoverItem) resetTilt(b.hoverItem);
          b.hoverItem = it;
          var k = navIndex(it);
          sfx("tick", 0, k);
          if (b.field) b.field.peek(k);
        }
      });
      on(nav, "pointerleave", function () {
        if (b.hoverItem) resetTilt(b.hoverItem);
        b.hoverItem = null;
        if (b.field) b.field.peek(-1);
      });
    }

    /* ---------- 功能页：进入就延伸，回到目录 / 装饰区就让位（只切 class，全程无重排） ---------- */
    var mainEl = $("main"), sbEl = $("sidebar");
    function setFocusMode(v) {
      clearTimeout(b.focusT);
      b.focusT = setTimeout(function () {
        if (isOv() || root.classList.contains("rh-focus") === v) return;
        if (!v && overlayOpen()) return;
        root.classList.toggle("rh-focus", v);
        if (b.slide > 1) sfx(v ? "open" : "close");     // 窗口够宽、功能页本来就整块露着时不出声
        syncPause();
        syncPause();
        syncPause();
      }, v ? 90 : 90);
    }
    if (mainEl) on(mainEl, "pointerenter", function () { setFocusMode(true); });
    if (sbEl) on(sbEl, "pointerenter", function () { setFocusMode(false); });
    on(b.zone, "pointerenter", function () { setFocusMode(false); });
    on(b.plate, "pointerenter", function () { setFocusMode(false); });

    /* ---------- 抬头折叠：面板里任何一块在滚都算（scroll 不冒泡，用捕获） ---------- */
    if (mainEl) on(mainEl, "scroll", function (e) { foldCheck(e.target); }, { capture: true, passive: true });
    // 抬头展开时的高度 → 内容区顶部留白（抬头浮在内容上方，折叠时内容不动）
    if (top && window.ResizeObserver) {
      b.ro = new ResizeObserver(measureHead);
      b.ro.observe(top);
    }

    /* ---------- 鼠标视差 ---------- */
    on(window, "pointermove", function (e) {
      if (b.field) b.field.pointer(e.clientX / window.innerWidth * 2 - 1, e.clientY / window.innerHeight * 2 - 1);
    }, { passive: true });

    /* 翻页：跟着 #page-title */
    var title = $("page-title");
    if (title) {
      var mo = new MutationObserver(function () { turn(); });
      mo.observe(title, { childList: true, characterData: true, subtree: true });
      b.obs.push(mo);
    }
    if (nav) {
      var no = new MutationObserver(function () { syncFiles(); });
      no.observe(nav, { attributes: true, subtree: true, attributeFilter: ["hidden"] });
      b.obs.push(no);
    }
    var gs = $("global-status");
    if (gs) {
      var so = new MutationObserver(syncStatus);
      so.observe(gs, { attributes: true, childList: true, characterData: true, subtree: true });
      b.obs.push(so);
    }
    var isel = $("inst-switch-select");
    if (isel) on(isel, "change", syncStatus);

    syncFiles();
    syncStatus();
    layout();
    measureFold();
    turn(true);
    measureHead();
    b.loop = requestAnimationFrame(tick);
    b.clockT = setInterval(function () {
      var d = new Date(), c = b.foot.querySelector(".rf-clock");
      if (c) c.textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    }, 1000);
  }

  function syncSfx(v) {
    if (!built) return;
    var x = built.foot.querySelector(".rf-sfx b"); if (x) x.textContent = v ? "ON" : "OFF";
    built.foot.querySelector(".rf-sfx").classList.toggle("off", !v);
  }

  function syncFiles() {
    var b = built; if (!b) return;
    var list = pages();
    b.list = list;
    if (b.field) b.field.setFiles(list.length);
    var c = b.navHead.querySelector(".rh-navcount"); if (c) c.textContent = pad(list.length);
    var t = b.ov.querySelector(".rs-t"); if (t) t.textContent = pad(list.length);
    var ticks = b.ov.querySelector(".rs-ticks");
    ticks.innerHTML = "";
    list.forEach(function (p, i) {
      var x = el("button", null, null, ticks);
      x.type = "button"; x.title = fileNo(i) + " " + p.cn;
      x.addEventListener("click", function () { setSel(i); });
    });
    Array.prototype.forEach.call(document.querySelectorAll("#nav .nav-item"), function (n) {
      var i = -1; list.forEach(function (p, k) { if (p.id === n.dataset.page) i = k; });
      n.dataset.rhNo = i >= 0 ? fileNo(i) : "";
      n.style.setProperty("--n", Math.max(0, i));
    });
  }

  function syncStatus() {
    var b = built; if (!b || !b.meta) return;
    var gs = $("global-status"), txt = $("global-status-text");
    var st = b.meta.querySelector(".rm-st");
    st.dataset.state = gs ? gs.dataset.state || "idle" : "idle";
    st.querySelector("em").textContent = txt ? txt.textContent : "—";
    var mini = b.phNav && b.phNav.querySelector(".rh-mini");
    if (mini) { mini.dataset.state = st.dataset.state; mini.querySelector("em").textContent = txt ? txt.textContent : "—"; }
    var isel = $("inst-switch-select"), sw = $("inst-switch");
    var rel = (sw && !sw.hidden && isel && isel.selectedOptions[0]) ? isel.selectedOptions[0].textContent : "LOCAL NODE";
    b.meta.querySelector(".rm-rel").textContent = rel;
  }

  // 依赖尺寸的页面（输出网格等）重新量一次
  function kickResize() {
    var b = built; if (!b) return;
    clearTimeout(b.rsT);
    b.rsT = setTimeout(function () { window.dispatchEvent(new Event("resize")); }, 60);
  }

  /* ---------- 抬头高度 → 内容顶部留白（只在展开且不在过渡中时量） ---------- */
  function measureHead() {
    var b = built, top = $("topbar"); if (!b || !top) return;
    if (top.classList.contains("rh-fold") || b.unfolding) return;
    var h = Math.ceil(top.getBoundingClientRect().height);
    if (h > 40 && h !== b.headH) { b.headH = h; root.style.setProperty("--rh-head-h", h + "px"); }
  }

  /* ---------- 布局：装饰区 = 目录右边到功能页左边（功能页按「不延伸」时的宽度算，延伸不挪相机） ---------- */
  function layout() {
    var b = built; if (!b) return;
    var W = window.innerWidth;
    var sb = $("sidebar");
    var ov = isOv();
    var left = ov ? 0 : (sb ? sb.getBoundingClientRect().width : 236);
    var pw = b.meas ? b.meas.getBoundingClientRect().width : W * .55;
    var main = $("main");
    b.slide = main ? Math.max(0, main.getBoundingClientRect().width - pw) : 0;
    root.classList.toggle("rh-roomy", b.slide < 1);
    var right = ov ? W : W - pw;
    root.style.setProperty("--rh-zl", left + "px");
    root.style.setProperty("--rh-zr", (W - right) + "px");
    if (b.field) {
      b.field.setFocus(ov ? .5 : (left + right) / 2 / W);
      b.field.setLow(!ov && right - left < 120);
    }
  }

  /* ---------- 每帧：板面信息贴图 + 景深焦点 ---------- */
  function tick() {
    var b = built; if (!b) return;
    b.loop = requestAnimationFrame(tick);
    if (!b.field || document.hidden) return;
    var a = b.field.anchor();
    if (a) {
      root.style.setProperty("--rh-fx", a.x.toFixed(1) + "px");
      root.style.setProperty("--rh-fy", a.y.toFixed(1) + "px");
    }
    var q = b.field.plateQuad();
    if (!q) { b.plate.style.visibility = "hidden"; return; }
    b.plate.style.visibility = "";
    var h = Math.round(PLATE_W * q.aspect);
    if (h !== b.plateH) { b.plateH = h; b.plate.style.height = h + "px"; b.plate.classList.toggle("short", h < 300); }
    b.plate.style.transform = quadMatrix(PLATE_W, h, q);
  }

  // 3D 阵列被功能页完全盖住（延伸状态且装饰区不留）时暂停渲染；平移动画走完再停，揭开时立刻恢复
  function syncPause() {
    var b = built; if (!b || !b.field) return;
    var covered = root.classList.contains("rh-focus") && b.slide > 1 && !isOv();
    clearTimeout(b.pauseT);
    if (!covered) b.field.setPaused(false);
    else b.pauseT = setTimeout(function () { b.field.setPaused(true); }, 500);
  }
  // 3D 阵列被功能页完全盖住（延伸状态且装饰区不留）时暂停渲染；平移动画走完再停，揭开时立刻恢复
  function syncPause() {
    var b = built; if (!b || !b.field) return;
    var covered = root.classList.contains("rh-focus") && b.slide > 1 && !isOv();
    clearTimeout(b.pauseT);
    if (!covered) b.field.setPaused(false);
    else b.pauseT = setTimeout(function () { b.field.setPaused(true); }, 500);
  }
  // 3D 阵列被功能页完全盖住（延伸状态且装饰区不留）时暂停渲染；平移动画走完再停，揭开时立刻恢复
  function syncPause() {
    var b = built; if (!b || !b.field) return;
    var covered = root.classList.contains("rh-focus") && b.slide > 1 && !isOv();
    clearTimeout(b.pauseT);
    if (!covered) b.field.setPaused(false);
    else b.pauseT = setTimeout(function () { b.field.setPaused(true); }, 500);
  }
  function overlayOpen() {
    return ["out-detail", "lightbox", "modal-mask"].some(function (id) { var n = $(id); return n && !n.hidden; });
  }
  function resetTilt(it) { ["--mx", "--tx", "--ty"].forEach(function (k) { it.style.removeProperty(k); }); }
  // 模型管理 / 输出管理：自带滚动区，抬头始终是紧凑版，不随里面的滚动折叠 / 展开
  var COMPACT = { models: 1, outputs: 1 };
  function foldCheck(src) {
    var b = built, c = (src && src.nodeType === 1) ? src : $("content"), top = $("topbar"); if (!b || !c || !top) return;
    if (COMPACT[curPage()]) { setFold(top, true); top.classList.add("rh-scrolled"); return; }
    if (c.id !== "content") return;          // 只有 #content 自己的滚动才折叠抬头
    var y = c.scrollTop, f = top.classList.contains("rh-fold");
    top.classList.toggle("rh-scrolled", y > 2);
    if (!f && y > 56) setFold(top, true);
    else if (f && y < 8) {
      measureHeadLater();
      top.classList.remove("rh-fold");
    }
  }
  // 抬头这一帧的变化不走过渡（翻页时用：内容区边距是同一帧切过去的，抬头也要跟上）
  function snap(top) {
    if (!top) return;
    top.classList.add("rh-snap");
    requestAnimationFrame(function () { requestAnimationFrame(function () { top.classList.remove("rh-snap"); }); });
  }
  // 预先量出折叠后的抬头高度（临时加 class、同步量、立刻撤掉，不会闪），模型 / 输出页第一次打开就是准的
  function measureFold() {
    var b = built, top = $("topbar"); if (!b || !top || top.classList.contains("rh-fold")) return;
    top.classList.add("rh-snap", "rh-fold");
    var h = Math.ceil(top.getBoundingClientRect().height);
    top.classList.remove("rh-fold");
    void top.offsetWidth;
    top.classList.remove("rh-snap");
    if (h > 40) { b.foldH = h; root.style.setProperty("--rh-fold-h", h + "px"); }
  }
  function setFold(top, v) {
    var b = built; if (!b) return;
    if (top.classList.contains("rh-fold") === v) return;
    if (!v) { measureHeadLater(); top.classList.remove("rh-fold"); return; }
    top.classList.add("rh-fold");
    clearTimeout(b.foldT);
    b.foldT = setTimeout(function () {   // 折叠过渡走完量一次真实高度
      if (!top.classList.contains("rh-fold")) return;
      var h = Math.ceil(top.getBoundingClientRect().height);
      if (h > 40 && h !== b.foldH) { b.foldH = h; root.style.setProperty("--rh-fold-h", h + "px"); kickResize(); }
    }, 620);
  }
  function measureHeadLater() {
    var b = built; if (!b) return;
    b.unfolding = true;
    clearTimeout(b.unfoldT);
    b.unfoldT = setTimeout(function () { b.unfolding = false; measureHead(); }, 650);
  }

  /* ---------- 板面信息 ---------- */
  function fillPlate(i, swap) {
    var b = built; if (!b) return;
    var list = b.list || pages(), p = list[i] || { id: "launch", cn: "" }, m = META[p.id] || { en: p.id.toUpperCase() };
    var put = function () {
      decode(b.plate.querySelector(".rp-no"), "NO." + String(i + 1).padStart(3, "0"), 460);
      decode(b.plate.querySelector(".rp-en"), m.en || "", 620);
      b.plate.querySelector(".rp-cn").textContent = p.cn;
      b.plate.querySelector(".rp-col").textContent = m.col || "—";
      b.plate.querySelector(".rp-dept").textContent = (m.dept || "—") + " · " + fileNo(i);
      b.plate.title = p.cn;
    };
    if (!swap || reduce) { put(); return; }
    b.plate.classList.add("swap");
    clearTimeout(b.plateT);
    b.plateT = setTimeout(function () { put(); b.plate.classList.remove("swap"); }, 260);
  }

  /* ---------- 翻页 ---------- */
  function turn(first) {
    var b = built; if (!b) return;
    var page = curPage();
    if (page === b.page && !first) return;
    var list = b.list || pages();
    var i = 0; list.forEach(function (p, k) { if (p.id === page) i = k; });
    var m = META[page] || { en: page.toUpperCase(), dept: "—", col: "—" };
    b.page = page; b.sel = i;

    root.classList.toggle("rh-wide", !!WIDE[page]);
    if (b.field) b.field.select(i, !first);
    decode(b.kick.querySelector(".rk-no"), fileNo(i), 420);
    decode(b.en, m.en, 640);
    b.meta.querySelector(".rm-dept").textContent = m.dept;
    b.meta.querySelector(".rm-col").textContent = m.col;
    var mi = b.phNav.querySelector(".rmi-no"); if (mi) mi.textContent = fileNo(i);
    replay($("topbar"), "rh-turn");
    var pg = $("page-" + page);
    if (pg) {
      Array.prototype.forEach.call(pg.children, function (c, k) { c.style.setProperty("--i", Math.min(k, 8)); });
      replay(pg, "rh-in");
      clearTimeout(b.inT);
      b.inT = setTimeout(function () { pg.classList.remove("rh-in"); }, root.classList.contains("rh-enter") ? 2500 : 1700);
    }
    b.ovSel = i;
    syncSel(i);
    fillPlate(i, !first);
    if (isOv() && !first) setOverview(false);
    else if (!first) { sfx("pull"); sfx("rise", 240); }
    var tb = $("topbar");
    snap(tb);
    if (!COMPACT[page] && tb) { tb.classList.toggle("rh-scrolled", ($("content") || {}).scrollTop > 2); if (($("content") || {}).scrollTop < 8) setFold(tb, false); }
    foldCheck($("content"));
    layout();
    // 整宽 ↔ 普通页面切换：内容列宽度会变，等过渡走完再让各页重新量一次尺寸
    if (b.wasWide !== !!WIDE[page]) {
      b.wasWide = !!WIDE[page];
      setTimeout(function () { layout(); measureHead(); kickResize(); }, 40);
    }
  }

  /* ---------- 装饰全屏 ---------- */
  function syncSel(i) {
    var b = built;
    decode(b.ov.querySelector(".rs-i"), pad(i + 1), 300);
    Array.prototype.forEach.call(b.ov.querySelectorAll(".rs-ticks button"), function (x, k) { x.classList.toggle("on", k === i); });
  }
  function setSel(i) {
    var b = built; if (!b) return;
    var n = (b.list || pages()).length;
    i = (i + n) % n;
    if (i === b.ovSel) return;
    b.ovSel = i;
    syncSel(i);
    fillPlate(i, true);
    if (b.field) b.field.select(i, true);
    sfx("tick", 0, i); sfx("rise", 120);
  }
  function moveSel(d) { setSel(built.ovSel + d); }
  function openSel() {
    var b = built, p = (b.list || pages())[b.ovSel];
    if (!p) return;
    if (p.id === curPage()) setOverview(false);
    else if (window.App && App.showPage) App.showPage(p.id);
  }
  function setOverview(v) {
    var b = built; if (!b) return;
    if (isOv() === v) return;
    root.classList.remove("rh-focus");
    root.classList.toggle("rh-ov", v);
    kickResize();
    if (!v && b.ovSel !== b.sel) { b.ovSel = b.sel; syncSel(b.sel); fillPlate(b.sel, true); }
    syncPause();
    syncPause();
    syncPause();
    if (b.field) {
      b.field.setMode(v ? "overview" : "detail");
      b.field.select(v ? b.ovSel : b.sel, false);
    }
    sfx(v ? "close" : "open");
    if (!v) sfx("rise", 240);
    layout();
  }

  /* ---------- 档案索引 ---------- */
  function openIndex() {
    var b = built; if (!b) return;
    b.idx.hidden = false;
    replay(b.idx, "in");
    var inp = b.idx.querySelector("input"); inp.value = ""; renderIndex();
    setTimeout(function () { inp.focus(); }, 30);
    sfx("open");
  }
  function closeIndex() { var b = built; if (!b || b.idx.hidden) return; b.idx.hidden = true; sfx("close"); }
  function renderIndex() {
    var b = built, q = b.idx.querySelector("input").value.trim().toLowerCase();
    var listEl = b.idx.querySelector(".ri-list"); listEl.innerHTML = "";
    var n = 0;
    (b.list || pages()).forEach(function (p, i) {
      var m = META[p.id] || {};
      var hay = (fileNo(i) + " " + p.cn + " " + (m.en || "") + " " + (m.dept || "")).toLowerCase();
      if (q && hay.indexOf(q) < 0) return;
      n++;
      var row = el("button", "ri-row" + (p.id === curPage() ? " cur" : ""),
        '<b>' + fileNo(i) + '</b><span><strong>' + esc(p.cn) + '</strong><small>' + esc(m.en || "") + '</small></span>' +
        '<em>' + esc(m.dept || "—") + '</em><i>' + (p.id === curPage() ? "OPEN" : "AUTHORIZED") + ' <u>↗</u></i>', listEl);
      row.type = "button";
      row.addEventListener("click", function () {
        closeIndex();
        if (p.id === curPage()) setOverview(false); else App.showPage(p.id);
      });
    });
    b.idx.querySelector(".ri-count").textContent = pad(n) + " RECORDS FOUND";
  }

  /* ---------- 键盘 ---------- */
  function typing(e) {
    var t = e.target;
    return t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
  }
  function onKey(e) {
    var b = built; if (!b) return;
    var modal = $("modal-mask");
    if (modal && !modal.hidden) return;
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) { e.preventDefault(); if (b.idx.hidden) openIndex(); else closeIndex(); return; }
    if (!b.idx.hidden) {
      if (e.key === "Escape") { e.preventDefault(); closeIndex(); }
      if (e.key === "Enter") { var r = b.idx.querySelector(".ri-row"); if (r) { e.preventDefault(); r.click(); } }
      return;
    }
    if (typing(e)) return;
    var ov = isOv();
    if (e.key === "Escape") { e.preventDefault(); setOverview(!ov); return; }
    if (!ov) return;
    if (e.key === "ArrowUp" || e.key === "ArrowLeft") { e.preventDefault(); moveSel(-1); }
    else if (e.key === "ArrowDown" || e.key === "ArrowRight") { e.preventDefault(); moveSel(1); }
    else if (e.key === "Enter") { e.preventDefault(); openSel(); }
  }

  /* ================= 拆除 ================= */
  function teardown() {
    var b = built; if (!b) return;
    built = null;
    b.obs.forEach(function (o) { o.disconnect(); });
    if (b.ro) b.ro.disconnect();
    b.off.forEach(function (f) { f(); });
    cancelAnimationFrame(b.loop); clearInterval(b.clockT);
    [b.inT, b.plateT, b.focusT, b.rsT, b.unfoldT, b.foldT, b.pauseT].forEach(clearTimeout);
    if (b.field) b.field.destroy();
    [b.world, b.zone, b.plateLayer, b.ov, b.foot, b.idx, b.brand, b.navHead, b.phNav, b.kick, b.en, b.meta, b.meas].forEach(function (n) { if (n) n.remove(); });
    Array.prototype.forEach.call(document.querySelectorAll(".rh-ripple"), function (n) { n.remove(); });
    clearTimeout(b.enterT);
    root.classList.remove("rh-enter", "rh-ov", "rh-wide", "rh-nogl", "rh-intro", "rh-focus", "rh-dragging", "rh-roomy");
    var tb = $("topbar"); if (tb) tb.classList.remove("rh-fold", "rh-turn", "rh-scrolled");
    ["--rh-zl", "--rh-zr", "--rh-fx", "--rh-fy", "--rh-head-h", "--rh-fold-h"].forEach(function (k) { root.style.removeProperty(k); });
    Array.prototype.forEach.call(document.querySelectorAll("#nav .nav-item"), function (n) { delete n.dataset.rhNo; n.style.removeProperty("--n"); resetTilt(n); });
  }

  function sync() { if (isRhine()) build(); else teardown(); }
  window.addEventListener("wwy-theme", function () { setTimeout(sync, 0); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", sync); else sync();

  // 开屏撤场时调用：阵列从地面涨起、相机飞入，目录和功能页稍后进场
  window.RhineUI = {
    intro: function () {
      var b = built; if (!b) return;
      root.classList.add("rh-intro");
      if (b.field) b.field.intro();
      // 开屏的白光还盖着时就开始收拢：模糊退去、目录滑入、功能页进场
      // 同一帧里：模糊退去、目录滑入、功能页进场、卡片开始浮入（分两帧做会先露出内容再藏起来重播，闪一下）
      setTimeout(function () {
        if (!reduce) root.classList.add("rh-enter");
        b.page = null; noDecode = true; turn(); noDecode = false;
        root.classList.remove("rh-intro"); sfx("open");
        measureHead();
        clearTimeout(b.enterT);
        b.enterT = setTimeout(function () { root.classList.remove("rh-enter"); }, 2600);
      }, reduce ? 30 : 140);
    },
  };
})();
