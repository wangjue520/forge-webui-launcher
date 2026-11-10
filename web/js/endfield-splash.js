/* 终末地开屏（默认风格 terminal；样式在 themes/endfield-splash.css，始终加载）
 *
 * 版面（展示页的写法）：
 *   顶边三色条；左上一行字段（日期 / 时间 / 版本，都是真实数据）
 *   左边大字标 WWY（实心）+ LAUNCHER（空心），下面一句黄色马克笔说明
 *   右边切角画框 + 斜线纹影子，框里是三步启动检查（编号、名称、状态）和 24 格进度条，
 *   框下压一块墨色铭牌，写当前在做什么
 *
 * 时间轴（ms；整段入场 ≤ 1 秒，"减少动态效果" 时全部直接到位）：
 *     0  纸面被斜切快门从左推进来（360，mech）
 *    80  三色条扫过（600，ease）
 *   120  字标实心行推入（620，mech）；260 空心行推入
 *   160  左上字段乱码后落定（600，linear）
 *   200  画框通电：从上往下展开（520，mech）
 *   420  三行检查逐行亮起（320，snap，每行晚 38）
 *   480  马克笔划过（420，mech）
 * 进度跟真实启动走：后端就绪前最多涨到 ~90%，core.js 调 Splash.ready() 后冲到 100%。
 * 撤场：黄色块带墨色亮边从左擦入盖住（280，mech）→ 开屏移除 → 黄块向右退出（220，mech）露出主界面，
 *       同时通知 js/endfield.js 播主界面入场。就绪后点击任意处可直接进入。
 */
(function () {
  "use strict";

  var GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789";

  function pad(n, w) { n = String(n); while (n.length < (w || 2)) n = "0" + n; return n; }
  function dateText(d) { return d.getFullYear() + "." + pad(d.getMonth() + 1) + "." + pad(d.getDate()); }
  function timeText(d) { return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); }
  function version() {
    try { return String((window.App && App.state && App.state.launcher && App.state.launcher.version) || "").replace(/^v/i, ""); }
    catch (e) { return ""; }
  }

  window.EndfieldSplash = function (host) {
    var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var MIN = reduce ? 300 : 1500;   // 至少完整看一遍入场
    var MAX = 9000;                  // 兜底：初始化出错也不会一直挡着界面
    var start = performance.now();
    var ready = false, leaving = false, done = false;
    var now0 = new Date();

    host.className = "efs";
    host.setAttribute("role", "status");
    host.setAttribute("aria-label", "WWY 启动器正在启动");
    host.innerHTML =
      '<div class="efs-paper">' +
        '<div class="efs-tri"><i></i><i></i><i></i></div>' +
        '<div class="efs-top">' +
          '<span class="efs-eyebrow">// COMFYUI / WEBUI LAUNCHER</span>' +
          '<ul class="efs-facts">' +
            '<li data-fx="' + dateText(now0) + '">' + dateText(now0) + '</li>' +
            '<li class="efs-time">' + timeText(now0) + '</li>' +
            '<li>V<b class="efs-ver">-.-.---</b></li>' +
          '</ul>' +
        '</div>' +
        '<div class="efs-hero">' +
          '<h1 class="efs-word"><span class="solid">WWY</span><span class="hollow">LAUNCHER</span></h1>' +
          '<p class="efs-marker"><span>AI 绘图启动器</span></p>' +
          '<p class="efs-lede">部署、启动、模型、插件、出图，一个窗口管好 ComfyUI 和 WebUI。</p>' +
        '</div>' +
        '<div class="efs-frame">' +
          '<div class="efs-window">' +
            '<div class="efs-head">' +
              '<span class="efs-pair"><small>STARTUP</small><b>启动检查</b></span>' +
              '<span class="efs-count"><b class="efs-done">0</b> / 3</span>' +
            '</div>' +
            '<ol class="efs-rows">' +
              '<li style="--i:0"><span class="efs-tag">No.01</span><span class="nm">界面模块</span><span class="st">完成</span></li>' +
              '<li style="--i:1"><span class="efs-tag">No.02</span><span class="nm">WebView2 运行时</span><span class="st">完成</span></li>' +
              '<li style="--i:2" class="efs-bridge"><span class="efs-tag">No.03</span><span class="nm">Python 后台</span><span class="st">连接中</span></li>' +
            '</ol>' +
            '<div class="efs-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">' +
              '<span class="efs-eyebrow">LOADING / 加载中</span><span class="efs-pct">000%</span><i></i>' +
            '</div>' +
          '</div>' +
          '<p class="efs-plate">正在连接后台…</p>' +
        '</div>' +
        '<div class="efs-band"></div>' +
      '</div>' +
      '<div class="efs-wipe"></div>';

    function $(s) { return host.querySelector(s); }
    var timeEl = $(".efs-time"), verEl = $(".efs-ver"), pctEl = $(".efs-pct"), bar = $(".efs-progress");
    var plate = $(".efs-plate"), doneEl = $(".efs-done"), bridge = $(".efs-bridge");

    // 乱码后落定：逐位锁定，linear 节奏
    function scramble(node, text, dur) {
      if (reduce) { node.textContent = text; return; }
      var t1 = performance.now();
      (function tick(now) {
        if (done) return;
        var p = Math.min(1, (now - t1) / dur), out = "";
        for (var i = 0; i < text.length; i++) {
          var ch = text[i];
          out += ((i + 1) / text.length <= p || /[.:\s/]/.test(ch)) ? ch : GLYPHS[(Math.random() * GLYPHS.length) | 0];
        }
        node.textContent = out;
        if (p < 1) requestAnimationFrame(tick);
      })(t1);
    }

    // 已完成的检查数：前两步在脚本跑到这里时就已经是真的
    var doneCount = 0;
    function setDone(n) { doneCount = n; if (doneEl) doneEl.textContent = String(n); }

    // 开始入场
    requestAnimationFrame(function () {
      host.classList.add("efs-go");
      setTimeout(function () {
        if (done) return;
        host.querySelectorAll(".efs-facts [data-fx]").forEach(function (n) { scramble(n, n.getAttribute("data-fx"), 600); });
      }, reduce ? 0 : 160);
      setTimeout(function () { if (!done && doneCount < 2) setDone(2); }, reduce ? 0 : 520);
    });

    var pct = 0, shown = -1, lastClock = "", verLocked = false;
    function frame(now) {
      if (done) return;
      var t = now - start;
      // 时钟（真实时间）
      var cs = timeText(new Date());
      if (cs !== lastClock && timeEl) { timeEl.textContent = cs; lastClock = cs; }
      // 版本：拿到就落定
      if (!verLocked && verEl) {
        var v = version();
        if (v) { verLocked = true; scramble(verEl, v, 360); }
      }
      // 进度：就绪前渐近 90%，就绪后冲到 100
      var cap = ready ? 100 : 90 * (1 - Math.exp(-t / 700));
      pct += (cap - pct) * (ready ? 0.22 : 0.12);
      if (ready && pct > 99.5) pct = 100;
      var n = Math.floor(pct);
      if (n !== shown && bar) {
        shown = n;
        bar.style.setProperty("--p", (n / 100).toFixed(3));
        bar.setAttribute("aria-valuenow", String(n));
        if (pctEl) pctEl.textContent = pad(n, 3) + "%";
      }
      if (ready && pct >= 100 && t >= MIN) leave();
      if (!leaving) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);

    function leave() {
      if (leaving) return;
      leaving = true;
      host.classList.add("efs-go", "efs-out1");          // 补齐入场（被点击跳过时）+ 黄块擦入
      var t1 = reduce ? 0 : 290;
      setTimeout(function () {
        host.classList.add("efs-out2");                  // 纸面撤掉，黄块向右退出
        if (window.EFUI && window.EFUI.intro) window.EFUI.intro();
        setTimeout(function () { done = true; host.remove(); }, reduce ? 0 : 240);
      }, t1);
    }

    host.addEventListener("click", function () {
      if (!ready) return;
      pct = 100;
      start = Math.min(start, performance.now() - MIN);
    });

    window.Splash = {
      ready: function () {
        if (ready) return;
        ready = true;
        setDone(3);
        if (bridge) { bridge.classList.add("ok"); var st = bridge.querySelector(".st"); if (st) st.textContent = "完成"; }
        if (plate) plate.textContent = "已就绪，点击任意处进入";
        host.classList.add("efs-ready");
      },
    };
    setTimeout(function () { if (!ready) window.Splash.ready(); pct = 100; start = Math.min(start, performance.now() - MIN); }, MAX);
  };
})();
