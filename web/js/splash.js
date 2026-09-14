/* 开屏动画：点 → 线 → 标题 → 版本号（明日方舟终末地风格）
 * 总时长跟真实启动走：动画播完（~2.2s）且页面就绪后立即斜切撤场；
 * 启动慢则停在 V2.0 画面转加载条，就绪后立刻结束，不人为拖延。
 * core.js 的 boot() 完成时调用 window.Splash.ready()。
 */
(function () {
  var el = document.getElementById("splash");
  if (!el) return;
  var t0 = performance.now();
  var bootReady = false, closed = false;

  var MIN_MS = 2150;   // 播完四个相位所需的最短时长
  var MAX_MS = 8000;   // 兜底：页面初始化异常也强制关
  var phases = [
    [80,   "ph-dot"],     // 黄点亮出
    [420,  "ph-line"],    // 点拉成线 + 黄条带扫入
    [900,  "ph-title"],   // 故障闪切出 WWY NEO
    [1650, "ph-version"], // 硬切到 V2.0 + 加载条
  ];
  phases.forEach(function (p) {
    setTimeout(function () { if (!closed) el.classList.add(p[1]); }, p[0]);
  });

  // INITIALIZING 后的省略号滚动
  var loadTxt = el.querySelector(".sp-load-txt");
  var frames = ["", ".", "..", "..."], fi = 0;
  var dotTimer = setInterval(function () {
    fi = (fi + 1) % frames.length;
    if (loadTxt) loadTxt.textContent = "INITIALIZING" + frames[fi];
  }, 300);

  function tryClose() {
    if (closed || !bootReady) return;
    var wait = Math.max(0, MIN_MS - (performance.now() - t0));
    setTimeout(function () {
      if (closed) return;
      closed = true;
      clearInterval(dotTimer);
      el.classList.add("ph-out");
      var done = false;
      function fin() { if (!done) { done = true; el.remove(); } }
      el.addEventListener("transitionend", function h(e) {
        if (e.target === el) { el.removeEventListener("transitionend", h); fin(); }
      });
      setTimeout(fin, 900); // 兜底
    }, wait);
  }

  window.Splash = {
    ready: function () { bootReady = true; tryClose(); },
  };
  setTimeout(function () { bootReady = true; tryClose(); }, MAX_MS);
})();
