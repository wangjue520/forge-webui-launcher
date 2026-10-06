/* 开屏动画 v3 —— 明日方舟：终末地风格
 *
 * 时间轴（ms）：
 *   60   p-on     黄色菱形点亮
 *   230  p-line   点拉成横向发丝线
 *   520  p-open   浅灰纸面从线处上下展开：等高线 / 网格 / 刻度尺 / 角标
 *   780  p-orbit  轨道环描边 + 刻度环旋转
 *   1020 p-title  黄色色块擦过，WWY 启动器 乱码解码出现
 *   1380 p-info   副标题 / 版本号 / 启动日志 / 分段进度条
 * 撤场：页面就绪且至少播到 MIN_MS 后，黑条 + 黄条从左擦入盖住，再从右退出露出主界面。
 * 进度条跟真实启动走：就绪前最多涨到 ~90%，就绪后冲到 100% 再撤场；就绪后点击可跳过。
 * core.js 的 boot() 完成时调用 window.Splash.ready()（接口与旧版一致）。
 * 其它变体：liquidSplash（液态玻璃）、vectorSplash（矢量突破），都在本文件末尾。
 */
(function () {
  var el = document.getElementById("splash");
  if (!el) return;
  // 切换实例导致的重载不再播开屏动画
  var skip = false;
  try { skip = sessionStorage.getItem("skip-splash") === "1"; sessionStorage.removeItem("skip-splash"); } catch (e) {}
  if (skip) {
    el.remove();
    window.Splash = { ready: function () {} };
    return;
  }

  /* 开屏变体由界面风格决定（见 themes.js）：splash: "none" 的风格直接进界面。
     VARIANTS：目前只有 terminal 一套实现（就是下面这套终末地动画）；以后新
     风格自带开屏时，在这里按变体名分发到各自的时间轴实现即可。 */
  var variant = (window.WWYThemes && window.WWYThemes.current().splash) || "terminal";
  if (variant === "liquid") {
    liquidSplash(el);
    return;
  }
  if (variant === "vector") {
    // 默认播放预渲染的视频（media/vector-splash.mp4，tools/render_vector_splash.py 生成）；
    // 视频缺失 / 解码失败 / 用户开了「减少动态效果」/ 渲染工具自己在录制时（?splash=live），走实时版
    var live = /[?&]splash=live\b/.test(location.search) || !window.VB_SPLASH ||
      (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    if (live) vectorSplash(el); else vectorVideoSplash(el);
    return;
  }
  if (variant === "bauform") {
    bauformSplash(el);
    return;
  }
  if (variant === "none") {
    el.remove();
    window.Splash = { ready: function () {} };
    return;
  }

  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var K = reduce ? 0.35 : 1;          // 减少动态效果时整体压缩时间轴
  var MIN_MS = 2300 * K;              // 最短展示时长（整段动画播完）
  var MAX_MS = 9000;                  // 兜底：页面初始化异常也强制关
  var t0 = performance.now();
  var bootReady = false, closing = false, closed = false;
  var timers = [];

  function $(s) { return el.querySelector(s); }
  function later(fn, ms) { timers.push(setTimeout(fn, ms)); }

  /* ---------- 相位 ---------- */
  [[60, "p-on"], [230, "p-line"], [520, "p-open"], [780, "p-orbit"],
   [1020, "p-title"], [1380, "p-info"]].forEach(function (p) {
    later(function () { el.classList.add(p[1]); }, p[0] * K);
  });

  /* ---------- 等高线地形（js/topo.js；主界面背景用同一个种子，撤场后地形无缝衔接） ---------- */
  function drawTopo() {
    if (window.Topo) window.Topo.draw($(".sp-topo"));
  }
  try { drawTopo(); } catch (e) { console.error(e); }

  /* ---------- 乱码解码文字 ---------- */
  var GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789/#▲▼";
  function scramble(node, finalText, dur, done) {
    var start = performance.now();
    (function tick(now) {
      if (closed) return;
      var p = Math.min(1, (now - start) / dur), out = "";
      for (var i = 0; i < finalText.length; i++) {
        var ch = finalText[i];
        var lock = (i + 1) / finalText.length <= p;
        out += (lock || ch === " " || ch === ".") ? ch : GLYPHS[(Math.random() * GLYPHS.length) | 0];
      }
      node.textContent = out;
      if (p < 1) requestAnimationFrame(tick); else if (done) done();
    })(start);
  }
  later(function () {
    el.querySelectorAll(".sp-title [data-final]").forEach(function (n) {
      scramble(n, n.getAttribute("data-final"), 520 * K);
    });
  }, 1020 * K + 200 * K);

  // 版本号：未知时持续乱码，拿到真实版本后解码锁定
  var verNode = $(".sp-ver-num"), verLocked = false;
  function currentVersion() {
    try { return (window.App && App.state && App.state.launcher && App.state.launcher.version) || ""; }
    catch (e) { return ""; }
  }
  var verTimer = setInterval(function () {
    if (verLocked || !verNode) return;
    var v = currentVersion();
    if (v) {
      verLocked = true; clearInterval(verTimer);
      scramble(verNode, v, 380);
      return;
    }
    var s = "";
    for (var i = 0; i < 6; i++) s += (i === 1 || i === 3) ? "." : "0123456789"[(Math.random() * 10) | 0];
    verNode.textContent = s;
  }, 70);

  /* ---------- 启动日志 ---------- */
  var rows = el.querySelectorAll(".sp-log-row");
  [1460, 1640, 1820].forEach(function (t, i) {
    later(function () { if (rows[i]) rows[i].classList.add("show"); }, t * K);
  });
  function markBridgeOk() {
    var b = $(".sp-log-bridge");
    if (b) { b.classList.add("ok"); var em = b.querySelector("em"); if (em) em.textContent = "OK"; }
  }

  /* ---------- 时钟 + 分段进度条 ---------- */
  var clock = $(".sp-clock"), pctEl = $(".sp-pct"), segBox = $(".sp-segs");
  var SEGS = 48, segs = [];
  if (segBox) {
    for (var i = 0; i < SEGS; i++) segs.push(segBox.appendChild(document.createElement("i")));
  }
  function pad(n, w) { n = String(n); while (n.length < w) n = "0" + n; return n; }
  var pct = 0, INFO_AT = 1380 * K;

  function frame(now) {
    if (closed) return;
    var d = new Date();
    if (clock) clock.textContent = pad(d.getHours(), 2) + ":" + pad(d.getMinutes(), 2) + ":" +
      pad(d.getSeconds(), 2) + "." + pad(Math.floor(d.getMilliseconds() / 10), 2);

    // 进度 = min(时间爬升, 就绪上限)：启动再快也完整走一遍，没就绪就卡在 90% 附近等
    var el_ms = now - t0, ramp = Math.max(0, (el_ms - INFO_AT) / (900 * K)) * 100;
    var cap = bootReady ? 100 : 90 * (1 - Math.exp(-(el_ms - INFO_AT) / 800));
    var target = Math.max(0, Math.min(ramp, cap));
    pct += (target - pct) * 0.3;
    if (bootReady && target >= 100 && 100 - pct < 0.5) pct = 100;

    var shown = Math.floor(pct);
    if (pctEl) pctEl.textContent = pad(shown, 3);
    var on = Math.round(pct / 100 * SEGS);
    for (var j = 0; j < segs.length; j++) {
      segs[j].className = j < on - 1 ? "on" : j === on - 1 ? "head" : "";
    }
    if (pct >= 100) tryClose();
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  /* ---------- 撤场 ---------- */
  function tryClose(force) {
    if (closing || !bootReady) return;
    if (!force && (pct < 100 || performance.now() - t0 < MIN_MS)) return;
    closing = true;
    timers.forEach(clearTimeout);
    // 被跳过时相位可能没播完，补齐 class 保证版面完整
    ["p-on", "p-line", "p-open", "p-orbit", "p-title", "p-info"].forEach(function (c) { el.classList.add(c); });
    el.classList.add("p-out1");
    setTimeout(function () {
      el.classList.add("p-out2");
      setTimeout(finish, 480 * (reduce ? 0.5 : 1));
    }, 380 * (reduce ? 0.5 : 1));
  }
  function finish() {
    if (closed) return;
    closed = true;
    clearInterval(verTimer);
    el.remove();
  }

  el.addEventListener("click", function () { if (bootReady) tryClose(true); });

  window.Splash = {
    ready: function () {
      if (bootReady) return;
      bootReady = true;
      markBridgeOk();
      el.classList.add("sp-ready");
    },
  };
  setTimeout(function () { if (!bootReady) window.Splash.ready(); tryClose(true); }, MAX_MS);

  /* ---------- 构型 · 机能包豪斯开屏 ----------
   * 参考 PV 的节奏（只借语言）：黑场 → 中线红色发丝线拉开 → 宽银幕上下边线 + 准星 →
   * X 光零件从中心径向炸开（带拖影）、左右两道巨弧描线、散落的单词逐个闪现 →
   * 石板蓝色块从右擦入垫到零件下面（零件变成「蓝底白片」）→ 大字 WWY / LAUNCHER 逐字上浮 →
   * 进度走完后：大字上飞、色块向左依次抽走，露出主界面并触发主界面入场动画（BPUI.intro）。
   * 时间轴（ms，减少动态效果时整体 ×0.35）：
   *   80 b-line   260 b-frame   420 b-burst   1500 b-slab   1700 b-title   2050 b-info
   * 画面全部 DOM + 一个 canvas（零件是预渲染的 sprite，每帧只做十几次 drawImage）。 */
  function bauformSplash(host) {
    var reduceB = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var KB = reduceB ? 0.35 : 1;
    var MIN = 3000 * KB, MAX = 9000, start = performance.now();
    var ready = false, leaving = false, done = false, tms = [];
    function at(fn, ms) { tms.push(setTimeout(fn, ms * KB)); }
    function chars(t, cls) {
      return '<span class="' + cls + '">' + t.split("").map(function (c, i) {
        return '<b style="--i:' + i + '">' + (c === " " ? "&nbsp;" : c) + "</b>";
      }).join("") + "</span>";
    }
    var WORDS = [["EVERY", 18, 30], ["FRAME", 22, 37], ["-", 31, 54], ["STARTS", 56, 22], ["AS", 63, 29],
                 ["A", 74, 40], ["BAUFORM", 70, 47], ["-", 52, 45], ["TONIGHT", 42, 64]];
    host.className = "sp-bp";
    host.innerHTML =
      '<div class="bps-slabs"><i></i><i></i><i></i></div>' +
      '<canvas class="bps-xr"></canvas>' +
      '<div class="bps-flash"></div>' +
      '<svg class="bps-guides" preserveAspectRatio="none" viewBox="0 0 1000 1000" aria-hidden="true">' +
        '<path class="g-arc g-arc-l" pathLength="1" d="M230 60 A640 640 0 0 0 230 940"/>' +
        '<path class="g-arc g-arc-r" pathLength="1" d="M770 60 A640 640 0 0 1 770 940"/>' +
      '</svg>' +
      '<div class="bps-mid"></div><div class="bps-frame f1"></div><div class="bps-frame f2"></div>' +
      '<div class="bps-cross c1"></div><div class="bps-cross c2"></div><div class="bps-cross c3"></div><div class="bps-cross c4"></div>' +
      '<div class="bps-words">' + WORDS.map(function (w, i) {
        return '<span style="left:' + w[1] + '%;top:' + w[2] + '%;--i:' + i + '">' + w[0] + "</span>";
      }).join("") + "</div>" +
      '<div class="bps-hud tl"><b>WWY</b> / 启动器<span>BAUFORM — BOOT SEQUENCE</span></div>' +
      '<div class="bps-hud tr"><span class="bps-clock">00:00:00</span><span>SECTION 04 · LAYER INVERSE</span></div>' +
      '<div class="bps-title">' +
        '<div class="bps-kick"><i></i>LAUNCHER<em>COMFYUI · WEBUI</em></div>' +
        '<div class="bps-big">' + chars("WWY", "l1") + chars("LAUNCHER", "l2") + "</div>" +
        '<div class="bps-sub"><span>AI 绘图启动器</span><span class="bps-dots">( (&#9632;( )&#9632;) )</span><em class="bps-ver">#-.-.-</em></div>' +
      "</div>" +
      '<div class="bps-strip"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>' +
      '<div class="bps-bar"><span class="lab">INITIALIZING<b>初始化</b></span><span class="trk"><i></i></span><span class="pct">000</span></div>' +
      '<div class="bps-skip">CLICK TO SKIP</div>';

    var cv = host.querySelector(".bps-xr"), ctx = cv.getContext("2d");
    var m = window.BPXray ? window.BPXray.model(20261006) : null;
    var W = 0, H = 0, dpr = Math.min(1.5, window.devicePixelRatio || 1);
    function size() {
      W = Math.round(innerWidth * dpr); H = Math.round(innerHeight * dpr);
      if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
    }
    size();
    window.addEventListener("resize", size);

    [[80, "b-line"], [260, "b-frame"], [420, "b-burst"], [1500, "b-slab"], [1700, "b-title"], [2050, "b-info"]].forEach(function (p) {
      at(function () { host.classList.add(p[1]); }, p[0]);
    });
    requestAnimationFrame(function () { host.classList.add("b-on"); });

    var pctEl = host.querySelector(".pct"), trk = host.querySelector(".trk i"), clock = host.querySelector(".bps-clock");
    var verEl = host.querySelector(".bps-ver"), verLocked = false, pct = 0;
    // 数字全部是机械翻牌（js/bauform-flap.js）：百分比、时钟、版本号
    var F = window.BFFlap, pctF = null, clockF = null, verF = null, lastPct = -1, lastClock = "";
    if (F) {
      pctF = F.create(pctEl, { speed: 110, text: "000" });
      clockF = F.create(clock, { speed: 300, text: "00:00:00" });
      verF = F.create(verEl, { speed: 150, text: "#0.0.00" });
    }
    function pad2(n) { return (n < 10 ? "0" : "") + n; }
    function ver() {
      try { return (window.App && App.state && App.state.launcher && App.state.launcher.version) || ""; } catch (e) { return ""; }
    }
    var BURST = 420 * KB, DUR = 1500 * KB;
    function frame(now) {
      if (done) return;
      var t = now - start;
      // 零件
      if (m && t > BURST) {
        var p = Math.min(1, (t - BURST) / DUR);
        var drift = (t - BURST) / 1000;
        ctx.clearRect(0, 0, W, H);
        var sc = Math.min(W, H) / 1050 * (0.92 + 0.1 * Math.min(1, drift / 6)) * (leaving ? 1 + (now - leaving) / 900 : 1);
        window.BPXray.draw(ctx, m, W, H, {
          p: p, rot: -0.35 + drift * 0.045, scale: sc, blur: Math.pow(1 - p, 2) * 1.2,
          alpha: leaving ? Math.max(0, 1 - (now - leaving) / 520) : 1,
          cx: W * .62, cy: H * .5,
        });
      }
      // 进度：就绪前渐近 88%，就绪后冲到 100
      var cap = ready ? 100 : 88 * (1 - Math.exp(-t / 1100));
      pct += (cap - pct) * (ready ? .14 : .08);
      if (ready && pct > 99.4) pct = 100;
      var ip = Math.floor(pct);
      if (ip !== lastPct) {
        lastPct = ip;
        var ps = (ip < 10 ? "00" : ip < 100 ? "0" : "") + ip;
        if (pctF) pctF.set(ps); else if (pctEl) pctEl.textContent = ps;
      }
      if (trk) trk.style.transform = "scaleX(" + (pct / 100).toFixed(3) + ")";
      var d = new Date();
      var cs = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
      if (cs !== lastClock) {
        // 第一次直接显示当前时间，之后每秒翻一格
        if (clockF) { if (lastClock) clockF.set(cs); else clockF.jump(cs); } else if (clock) clock.textContent = cs;
        lastClock = cs;
      }
      if (verEl && !verLocked) {
        var v = ver();
        if (v) { verLocked = true; if (verF) verF.set("#" + v); else verEl.textContent = "#" + v; }
      }
      if (ready && pct >= 100 && t >= MIN) leave();
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    function leave() {
      if (leaving) return;
      leaving = performance.now();
      tms.forEach(clearTimeout);
      ["b-on", "b-line", "b-frame", "b-burst", "b-slab", "b-title", "b-info"].forEach(function (c) { host.classList.add(c); });
      host.classList.add("b-out");
      setTimeout(function () {
        if (window.BPUI) window.BPUI.intro();
        host.classList.add("b-out2");
      }, (reduceB ? 60 : 260));
      setTimeout(function () {
        done = true;
        window.removeEventListener("resize", size);
        host.remove();
      }, reduceB ? 400 : 1300);
    }
    host.addEventListener("click", function () { if (ready) { pct = 100; start = Math.min(start, performance.now() - MIN); leave(); } });
    window.Splash = { ready: function () { ready = true; host.classList.add("b-ready"); } };
    setTimeout(function () { ready = true; leave(); }, MAX);
  }

  /* ---------- 液态玻璃开屏 ----------
   * 背景色团流动 → 一滴玻璃从中心弹出 → 横向流开成胶囊，露出图标和名字 →
   * 胶囊下面一条细进度 → 就绪后玻璃放大、化开，露出同样背景的主界面。 */
  function liquidSplash(host) {
    var reduceM = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var MIN = reduceM ? 500 : 1500, MAX = 9000, start = performance.now();
    var ready = false, leaving = false, done = false;
    host.className = "sp-liquid";
    host.innerHTML =
      '<div class="lq-bg"><i class="lq-b b1"></i><i class="lq-b b2"></i><i class="lq-b b3"></i><i class="lq-b b4"></i></div>' +
      '<div class="lq-drop"><div class="lq-logo">W</div>' +
      '<div class="lq-text"><b>WWY 启动器</b><span>AI 绘图启动器</span></div></div>' +
      '<div class="lq-bar"><i></i></div>';
    requestAnimationFrame(function () { host.classList.add("lq-in"); });
    setTimeout(function () { host.classList.add("lq-open"); }, reduceM ? 0 : 520);
    var bar = host.querySelector(".lq-bar i"), pct = 0;
    function tick(now) {
      if (done) return;
      var cap = ready ? 100 : 88 * (1 - Math.exp(-(now - start) / 900));
      pct += (cap - pct) * .12;
      if (bar) bar.style.transform = "scaleX(" + (pct / 100).toFixed(3) + ")";
      if (ready && pct > 99 && now - start >= MIN) leave();
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    function leave() {
      if (leaving) return;
      leaving = true;
      host.classList.add("lq-out");
      setTimeout(function () { done = true; host.remove(); }, reduceM ? 150 : 620);
    }
    host.addEventListener("click", function () { if (ready) leave(); });
    window.Splash = { ready: function () { ready = true; } };
    setTimeout(function () { ready = true; leave(); }, MAX);
  }

  /* ---------- 矢量突破开屏 · 视频版（默认） ----------
   * 画面是离线逐帧渲染好的视频（实时版 vectorSplash 的同一套编排，60fps），
   * 这里只负责：播放、叠加不进视频的内容（版本号）、等待后端、撤场。
   * 撤场 / 叠加层都只动 transform / opacity，整个开屏只有「一个视频图层」在动，不会卡。
   *   视频播完 + 后端就绪 → 撤场；后端还没好 → 停在最后一帧，日志卡里 SYNC-03 那格盖上「WAITING」；
   *   就绪后点击可跳过；2.6 秒内播不起来（缺文件 / 解码失败）→ 换成实时版 */
  function vectorVideoSplash(host) {
    var M = window.VB_SPLASH;
    var root = document.documentElement;
    var MAX = 9000, start = performance.now();
    var ready = false, ended = false, leaving = false, done = false, fellBack = false, playing = false;
    root.classList.add("vb-splash");
    // 播放期间主界面不绘制（只排版）：视频底下不再有一整页毛玻璃 / 大图在栅格化，抢显卡和主线程
    root.classList.add("vb-splash-hide");

    /* 撤场丝滑的关键：主界面要用的资源全部趁视频播放时提前备齐。
       不提前的话，reveal 那一帧要同时做「整页毛玻璃栅格化 + 三张背景图解码 +
       大字字体下载排版」，再强的机器也要卡一下——卡顿全攒在撤场开头。
       字体不预热还会在白光散开后看到一次大字换字体的闪烁（font-display: swap）。 */
    var fontReady = [];
    try {
      if (document.fonts && document.fonts.load) {
        fontReady = [
          document.fonts.load('400 100px "WWY Grotesk"'),
          document.fonts.load('700 100px "WWY Grotesk"'),
          document.fonts.load('400 32px "WWY Sans SC"'),
          document.fonts.load('700 32px "WWY Sans SC"'),
        ];
      }
    } catch (e) {}
    ["far", "mid", "front"].forEach(function (n) {
      var im = new Image(); im.src = "media/vector-bg-" + n + ".webp";   // 只图进缓存，不上屏
    });

    function pos(r, extra) {
      return "left:" + (r.x * 100).toFixed(3) + "%;top:" + (r.y * 100).toFixed(3) + "%;width:" + (r.w * 100).toFixed(3) +
        "%;height:" + (r.h * 100).toFixed(3) + "%;" + (extra || "");
    }
    host.className = "sp-vector-video";
    host.innerHTML =
      '<div class="vv-box" style="--ar:' + M.aspect + ';--big:' + (M.bigFont * 100).toFixed(3) + 'cqw;--small:' + (M.underFont * 100).toFixed(3) + 'cqw">' +
        '<img class="vv-poster" src="media/vector-splash-first.jpg" alt="">' +
        '<video class="vv-video" muted playsinline preload="auto">' +
          '<source src="media/vector-splash.mp4" type="video/mp4"><source src="media/vector-splash.webm" type="video/webm"></video>' +
        (M.num ? '<div class="vv-num" style="' + pos(M.num) + '"><b>-</b></div>' : '') +
        (M.ver ? '<div class="vv-ver" style="' + pos(M.ver, "width:auto;") + '"> · V<b>-.-.-</b></div>' : '') +
        (M.bridge ? '<div class="vv-bridge" style="' + pos(M.bridge) + '"><i></i>WAITING</div>' : '') +
        '<div class="vv-skip">CLICK TO SKIP</div>' +
      '</div>' +
      '<div class="vv-veil"></div>';
    var video = host.querySelector(".vv-video");
    var numB = host.querySelector(".vv-num b"), verB = host.querySelector(".vv-ver b");

    // 版本号：拿到就填（引号里是主版本号 V3 → “3”）
    var vT = setInterval(function () {
      var v = "";
      try { v = (window.App && App.state && App.state.launcher && App.state.launcher.version) || ""; } catch (e) {}
      if (!v) return;
      clearInterval(vT);
      v = String(v).replace(/^v/i, "");
      if (numB) numB.textContent = v.split(".")[0] || "-";
      if (verB) verB.textContent = v;
    }, 60);

    // 叠加层按视频时间出场（与视频里大字逐字升起同一时刻、同一缓动）
    function tick() {
      if (done || fellBack) return;
      var t = video.currentTime || 0;
      if (t >= M.numAt) host.classList.add("vv-num-in");
      if (t >= M.verAt) host.classList.add("vv-ver-in");
      if (!ended) requestAnimationFrame(tick);
    }

    function fallback() {
      if (fellBack || playing || leaving) return;
      fellBack = true;
      clearInterval(vT);
      root.classList.remove("vb-splash", "vb-splash-hide");
      vectorSplash(host);
      if (ready && window.Splash) window.Splash.ready();
    }
    video.addEventListener("playing", function () {
      playing = true;
      host.classList.add("vv-playing");
      requestAnimationFrame(tick);
    });
    video.addEventListener("ended", function () {
      ended = true;
      revealUI();
      host.classList.add("vv-num-in", "vv-ver-in", "vv-ended");
      if (!ready) host.classList.add("vv-waiting");
      leave();
    });
    video.addEventListener("error", fallback);
    var srcs = host.querySelectorAll(".vv-video source");
    if (srcs.length) srcs[srcs.length - 1].addEventListener("error", fallback);   // 所有格式都放不了
    /* 起播时机：页面刚打开那几百毫秒主线程在解析脚本、排版主界面，这时开播最容易掉开头几帧。
       所以先停在海报（= 视频第一帧，画面完全一样），等「页面 load 完 + 视频缓冲够 + 再空两帧」
       再开播；最多等 700ms。 */
    // 整体加快 1/3：播放速率 4/3，时长缩到 3/4。版本号等叠加层按视频时间出场，
    // 跟着一起提速、对齐不变；想再快/慢只调这一个数（1.5 = 时长缩 1/3）
    video.playbackRate = 4 / 3;
    var started = false;
    function startPlay() {
      if (started || fellBack) return;
      started = true;
      var p = video.play();
      if (p && p.catch) p.catch(fallback);
    }
    function settle(cb) { requestAnimationFrame(function () { requestAnimationFrame(cb); }); }
    var loaded = document.readyState === "complete", buffered = false;
    function maybeStart() { if (loaded && buffered) settle(startPlay); }
    if (!loaded) window.addEventListener("load", function () { loaded = true; maybeStart(); });
    video.addEventListener("canplaythrough", function () { buffered = true; maybeStart(); });
    if (video.readyState >= 4) buffered = true;
    maybeStart();
    setTimeout(startPlay, 700);
    setTimeout(fallback, 2600);

    // 主界面在视频结束时才恢复绘制：整页毛玻璃 + 大图的首次栅格化这一下重活，
    // 藏在静止的最后一帧后面（静止画面上多停一帧察觉不到）；字体 / 图片在开播前
    // 已预热，这口气比不预热小得多。leave 会等资源就绪 + 画出两帧后才撤场，
    // 栅格化再慢也只会推迟撤场，不会卡进撤场动画里。
    var revealed = false;
    var assetsReady = null;   // 背景图解码 + 字体就绪的 Promise（leave 时等它）
    function revealUI() {
      if (revealed) return;
      revealed = true;
      root.classList.remove("vb-splash-hide");
      var jobs = fontReady.slice();
      try {
        document.querySelectorAll("#vb-deco img").forEach(function (im) {
          if (im.decode) jobs.push(im.decode().catch(function () {}));
        });
      } catch (e) {}
      // 兜底 600ms：个别资源慢也不拖住撤场，只是回到原来的体验
      assetsReady = Promise.race([
        Promise.all(jobs).catch(function () {}),
        new Promise(function (res) { setTimeout(res, 600); }),
      ]);
    }

    function leave(force) {
      if (leaving || fellBack || !ready) return;
      if (!force && !ended) return;
      leaving = true;
      if (!revealed) { revealUI(); }
      // 等「资源备齐 + 主界面画出两帧」再撤场：撤场动画不和栅格化 / 解码 / 换字体抢时间
      assetsReady.then(function () { settle(exit); });
    }
    function exit() {
      clearInterval(vT);
      // 点击跳过进来的：视频还在播。冻在当前帧再撤场——撤场期间少一路视频解码
      // 抢资源，而且定格推近的观感比「画面还在动就放大」更稳
      if (!ended) { try { video.pause(); } catch (e) {} }
      host.classList.remove("vv-waiting");
      host.classList.add("vv-num-in", "vv-ver-in", "vv-out0");     // 预备：轻轻吸一口气
      // 主界面场景在 out0 就开始飘入：此刻开屏还完全不透明地盖着，入场动画的
      // 启动开销（几十个小球 / 方块同时开跑）被这一百多毫秒吸收掉；白光掀开时
      // 场景已经在动，不会在「掀帘子」的同一帧才扎堆启动
      root.classList.remove("vb-splash", "vb-splash-hide");
      setTimeout(function () {
        host.classList.add("vv-out1");                              // 推近 + 白光
        setTimeout(function () {
          host.classList.add("vv-out2");                            // 整体淡出
          setTimeout(function () { done = true; try { video.pause(); } catch (e) {} host.remove(); }, 420);
        }, 480);
      }, 170);
    }
    host.addEventListener("click", function () { if (ready && !fellBack) leave(true); });
    window.Splash = {
      ready: function () {
        if (ready) return;
        ready = true;
        host.classList.add("vv-ready");
        leave();
      },
    };
    setTimeout(function () {
      if (fellBack) return;
      if (!ready) window.Splash.ready();
      leave(true);
    }, MAX);
  }

  /* ---------- 矢量突破开屏 ----------
   * 性能原则（上一版卡顿的原因：SVG 湍流 / 粘滞 / 投影滤镜每帧重算、字距动画每帧重排）：
   *   所有运动只动 transform / opacity，交给合成器在 GPU 上跑，主线程忙着初始化也不掉帧；
   *   带滤镜的东西（等高环、景深模糊、光晕）内容保持静止，只栅格化一次；
   *   进度弧只在整数百分比变化时才重画。
   *
   * 编排（ms；相位 class 加在 #splash 上，样式见 splash.css 末尾）——快进慢出、有先后、有回弹。
   * 前 200ms 只做静态淡入：这段时间页面在解析脚本、首次排版，主线程最忙，不安排要紧的动作。
   *   0     vs-1  纸面网点、四角准星旋入；镜头从 1.07 倍拉远，约 1.7 秒减速停稳
   *   200   vs-2  等高环由小放大浮现，两道波纹扩散；立体方块 / 软球分层飘入（远的慢、近的快）
   *   440   vs-3  三条青色光条从左侧「射」进来后急刹（expo-out），折返箭头描出、箭头头弹出
   *   700   vs-4  软球簇一颗颗弹出（带过冲回弹），之后各自轻轻浮动
   *   840   vs-5  立体唱片带旋转从右侧滑入减速停稳，正弦波描出，光晕呼吸；外圈青弧 = 真实进度
   *   1060  vs-6  大字逐字从下方升起（错峰 22ms），整行同时从略宽收紧到紧排
   *   1380  vs-7  玻璃日志卡浮起，同步日志逐行出现
   * 撤场：先轻轻「吸气」缩一下（预备动作），再整体推近 + 泛起白光淡出，露出主界面；
   *       主界面的立体场景在这一刻才开始飘入，衔接成一个连续镜头。
   * 进度规则与终末地开屏一致：就绪前最多涨到 ~90%，就绪后冲到 100% 再撤场；就绪后点击可跳过。 */
  function vectorSplash(host) {
    var reduceV = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var KV = reduceV ? 0.35 : 1;
    var MIN = 2700 * KV, MAX = 9000, start = performance.now();
    var ready = false, leaving = false, done = false, tms = [];
    function at(fn, ms) { tms.push(setTimeout(fn, ms * KV)); }
    var root = document.documentElement;
    root.classList.add("vb-splash");          // 主界面的场景先按住不动，等撤场时再飘入

    // 大字拆成单字，逐字升起
    function chars(text, from) {
      var o = "";
      for (var j = 0; j < text.length; j++) {
        var c = text[j] === " " ? " " : text[j];
        o += '<span class="ch" style="--i:' + (from + j) + '">' + c + '</span>';
      }
      return o;
    }

    var rings = "", i;
    for (i = 1; i <= 20; i++) {
      rings += '<circle cx="400" cy="400" r="' + (24 + i * 18) + '"' + (i % 5 === 0 ? ' class="mj"' : '') + '/>';
    }
    // 软球簇：[x%, y%, 直径(vh)]，相对簇容器
    var BL = [[50, 50, 9.5], [36, 38, 6], [62, 34, 5.4], [66, 60, 7], [40, 64, 5.8], [26, 54, 3.6],
              [51, 22, 4], [78, 48, 3.4], [18, 30, 2.4], [84, 72, 2.4], [14, 72, 1.8], [88, 18, 1.8]];
    var balls = "";
    BL.forEach(function (b, k) {
      balls += '<i class="vs-ball" style="left:' + b[0] + '%;top:' + b[1] + '%;--s:' + b[2] + 'vh;--d:' + (k * 0.04).toFixed(2) +
        's;--bp:' + (3.6 + (k % 4) * .7).toFixed(1) + 's;--by:' + (-4 - (k % 3) * 2) + 'px"><b></b></i>';
    });
    var arc = 2 * Math.PI * 214;     // 进度弧周长
    var ticks = "";
    for (i = 0; i < 72; i++) ticks += '<line x1="250" y1="14" x2="250" y2="' + (i % 6 ? 22 : 30) + '" transform="rotate(' + i * 5 + ' 250 250)"/>';

    host.className = "sp-vector";
    host.innerHTML =
      '<div class="vs-dots"></div>' +
      '<div class="vs-stage">' +
        '<div class="vs-far">' +
          '<div class="vs-rings-wrap"><svg class="vs-rings" viewBox="0 0 800 800" aria-hidden="true"><defs>' +
            '<filter id="vs-wob" x="-10%" y="-10%" width="120%" height="120%"><feTurbulence type="fractalNoise" baseFrequency="0.006" numOctaves="2" seed="9" result="n"/>' +
            '<feDisplacementMap in="SourceGraphic" in2="n" scale="70" xChannelSelector="R" yChannelSelector="G"/></filter></defs>' +
            '<g filter="url(#vs-wob)">' + rings + '</g></svg>' +
            '<i class="vs-ripple r1"></i><i class="vs-ripple r2"></i></div>' +
        '</div>' +
        '<div class="vs-mid">' +
          '<i class="vs-arrows vs-arrows-l"></i><i class="vs-arrows vs-arrows-r"></i>' +
          '<div class="vs-bars"><i class="vs-bar b1"></i><i class="vs-bar b2"></i><i class="vs-bar b3"></i></div>' +
          '<svg class="vs-bend" viewBox="0 0 320 120" aria-hidden="true">' +
            '<path pathLength="1" d="M-40 14 H226 Q248 14 236 36 L206 84 Q196 100 218 90 L262 64"/>' +
            '<path class="hd" d="M256 54 L286 54 L270 80z"/></svg>' +
          '<div class="vs-balls">' + balls + '</div>' +
        '</div>' +
        '<div class="vs-disc">' +
          '<div class="vs-disc-shadow"></div>' +
          '<svg class="vs-disc-body" viewBox="0 0 500 500" aria-hidden="true"><defs>' +
            '<radialGradient id="vs-dk" cx=".36" cy=".28" r=".85"><stop offset="0" stop-color="#465456"/>' +
            '<stop offset=".5" stop-color="#1b2426"/><stop offset="1" stop-color="#0b1112"/></radialGradient>' +
            '<radialGradient id="vs-in" cx=".42" cy=".36" r=".8"><stop offset="0" stop-color="#20292b"/><stop offset="1" stop-color="#0e1415"/></radialGradient>' +
            '<linearGradient id="vs-sheen" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".3"/>' +
            '<stop offset=".42" stop-color="#fff" stop-opacity="0"/></linearGradient></defs>' +
            '<circle cx="250" cy="250" r="246" fill="url(#vs-dk)"/>' +
            '<circle cx="250" cy="250" r="214" fill="none" stroke="rgba(255,255,255,.07)" stroke-width="20"/>' +
            '<circle cx="250" cy="250" r="128" fill="url(#vs-in)" stroke="rgba(255,255,255,.14)" stroke-width="1.5"/>' +
            '<circle cx="250" cy="250" r="244" fill="url(#vs-sheen)"/></svg>' +
          '<svg class="vs-ticks" viewBox="0 0 500 500" aria-hidden="true"><g>' + ticks + '</g></svg>' +
          '<svg class="vs-arcsvg" viewBox="0 0 500 500" aria-hidden="true"><circle class="arc" cx="250" cy="250" r="214" style="stroke-dasharray:' +
            arc.toFixed(1) + ';stroke-dashoffset:' + arc.toFixed(1) + '"/></svg>' +
          '<div class="vs-glow"></div>' +
          '<svg class="vs-wave" viewBox="0 0 500 500" aria-hidden="true">' +
            '<path pathLength="1" d="M128 300 C168 300 178 160 218 160 S268 340 308 340 S350 210 370 190"/>' +
            '<circle class="hub" cx="250" cy="250" r="9"/><circle class="hub2" cx="250" cy="250" r="15"/></svg>' +
          '<div class="vs-pct"><span>LOAD</span><b>000</b></div>' +
        '</div>' +
        /* 左上角大字排版（结构照 PV：标签列 | 两行紧排咬合大字 + 引号编号 | 标签 | 大字 + 压在下面的小字） */
        '<div class="vs-type">' +
          '<div class="vs-node"><i></i>NODE01</div>' +
          '<span class="vs-lab r1">Terminal</span><div class="vs-big r1"><span class="vs-line">' + chars("WWY", 0) + '</span></div>' +
          '<span class="vs-lab r2">Numbers</span><div class="vs-big r2"><span class="vs-line">' + chars("LAUNCHER", 3) +
            '<span class="ch vs-q" style="--i:11">“<b class="vs-num">-</b>”</span></span></div>' +
          '<span class="vs-lab r3">Trial from</span><div class="vs-big r3"><span class="vs-line">' + chars("LOCAL", 12) + '</span></div>' +
          '<div class="vs-under">COMFYUI · WEBUI<span class="vs-verw"> · V<b class="vs-ver">-.-.-</b></span></div>' +
          '<div class="vs-cn">启动器</div>' +
        '</div>' +
        '<div class="vs-log">' +
          '<div class="row"><span>SYNC-01</span>UI.MODULES<i></i><em>CONNECTED</em></div>' +
          '<div class="row"><span>SYNC-02</span>WEBVIEW2 RUNTIME<i></i><em>CONNECTED</em></div>' +
          '<div class="row vs-bridge"><span>SYNC-03</span>PYTHON BRIDGE<i></i><em>WAITING</em></div>' +
          '<div class="vs-note"><i></i><span>THE CURRENT TERMINAL RECORD HAS BEEN SYNCHRONIZED,<br>AND ALL DATA WILL BE ARCHIVED LOCALLY.<br>PLEASE STAND BY WHILE THE BRIDGE IS ESTABLISHED.</span></div>' +
        '</div>' +
      '</div>' +
      '<i class="vs-cross x1"></i><i class="vs-cross x2"></i><i class="vs-cross x3"></i><i class="vs-cross x4"></i>' +
      '<div class="vs-skip">CLICK TO SKIP</div>' +
      '<div class="vs-veil"></div>';

    function $v(s) { return host.querySelector(s); }
    // 立体方块 + 软球（与主界面同一套，见 js/vector-scene.js；live = 开屏里持续漂浮）
    if (window.VBScene) {
      var sc = VBScene.build(VBScene.SPLASH, { stagger: .11, cls: "vs-scene", live: true });
      sc.classList.add("vs-hold");
      $v(".vs-far").appendChild(sc);
      at(function () { sc.classList.remove("vs-hold"); }, 200);
    }
    [[0, "vs-1"], [200, "vs-2"], [440, "vs-3"], [700, "vs-4"], [840, "vs-5"], [1060, "vs-6"], [1380, "vs-7"]].forEach(function (p) {
      at(function () { host.classList.add(p[1]); }, p[0]);
    });
    host.querySelectorAll(".vs-log .row").forEach(function (row, k) {
      at(function () { row.classList.add("show"); }, 1500 + k * 140);
    });

    // 乱码解码（只改小字的文字，不碰大字排版）
    var GL = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#<>/";
    function scr(node, text, dur) {
      var t1 = performance.now();
      (function tk(now) {
        if (done) return;
        var p = Math.min(1, (now - t1) / dur), o = "";
        for (var j = 0; j < text.length; j++) {
          o += ((j + 1) / text.length <= p || text[j] === ".") ? text[j] : GL[(Math.random() * GL.length) | 0];
        }
        node.textContent = o;
        if (p < 1) requestAnimationFrame(tk);
      })(t1);
    }

    // 版本号：拿到后解码锁定；引号里显示主版本号（V3 → “3”）
    var verN = $v(".vs-ver"), numN = $v(".vs-num"), vLocked = false;
    var vT = setInterval(function () {
      if (vLocked) return;
      var v = "";
      try { v = (window.App && App.state && App.state.launcher && App.state.launcher.version) || ""; } catch (e) {}
      if (v) {
        vLocked = true; clearInterval(vT);
        if (verN) scr(verN, String(v).replace(/^v/i, ""), 360);
        if (numN) numN.textContent = String(v).replace(/^v/i, "").split(".")[0] || "-";
        return;
      }
      if (numN) numN.textContent = String((Math.random() * 10) | 0);
      if (verN) verN.textContent = ((Math.random() * 10) | 0) + "." + ((Math.random() * 10) | 0) + "." + ((Math.random() * 10) | 0);
    }, 90);

    // 进度：唱片外圈的青色弧（只在整数百分比变化时重画）
    var arcEl = $v(".arc"), pctEl = $v(".vs-pct b"), pct = 0, shown = -1, INFO = 840 * KV;
    function frame(now) {
      if (done) return;
      var e = now - start;
      var ramp = Math.max(0, (e - INFO) / (1100 * KV)) * 100;
      var cap = ready ? 100 : 90 * (1 - Math.exp(-Math.max(0, e - INFO) / 800));
      var target = Math.max(0, Math.min(ramp, cap));
      pct += (target - pct) * 0.2;
      if (ready && target >= 100 && 100 - pct < 0.5) pct = 100;
      var n = Math.floor(pct);
      if (n !== shown) {
        shown = n;
        if (arcEl) arcEl.style.strokeDashoffset = (arc * (1 - pct / 100)).toFixed(1);
        if (pctEl) pctEl.textContent = (n < 10 ? "00" : n < 100 ? "0" : "") + n;
      }
      if (pct >= 100) leave();
      if (!leaving) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    function leave(force) {
      if (leaving || !ready) return;
      if (!force && (pct < 100 || performance.now() - start < MIN)) return;
      leaving = true;
      tms.forEach(clearTimeout);
      ["vs-1", "vs-2", "vs-3", "vs-4", "vs-5", "vs-6", "vs-7"].forEach(function (c) { host.classList.add(c); });
      host.querySelectorAll(".vs-log .row").forEach(function (r) { r.classList.add("show"); });
      if (arcEl) arcEl.style.strokeDashoffset = "0";
      if (pctEl) pctEl.textContent = "100";
      host.classList.add("vs-out0");                       // 预备：轻轻吸一口气
      setTimeout(function () {
        host.classList.add("vs-out1");                     // 推近 + 白光
        root.classList.remove("vb-splash");                // 主界面的场景这时开始飘入
        setTimeout(function () {
          host.classList.add("vs-out2");                   // 整体淡出
          setTimeout(function () { done = true; clearInterval(vT); host.remove(); }, reduceV ? 150 : 420);
        }, reduceV ? 150 : 480);
      }, reduceV ? 60 : 170);
    }
    host.addEventListener("click", function () { if (ready) leave(true); });
    window.Splash = {
      ready: function () {
        if (ready) return;
        ready = true;
        var b = $v(".vs-bridge");
        if (b) { b.classList.add("ok"); var em = b.querySelector("em"); if (em) em.textContent = "CONNECTED"; }
        host.classList.add("vs-ready");
      },
    };
    setTimeout(function () { if (!ready) window.Splash.ready(); leave(true); }, MAX);
  }
})();
