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
})();
