/* perfmon.js — 侧栏性能监控（所有主题通用）
 *
 *  5 项指标：GPU 温度 / GPU 占用 / 显存温度 / 显存占用 / 系统内存占用
 *  每项一行：名称 + 当前值 + 最近约 60 秒的 sparkline（纯 canvas 手写）。
 *
 *  数据：前端就绪后调 api.perf_start()（后端起采样线程、返回已有历史），
 *        之后每秒收到 perf/sample 事件。
 *  布局：#perf-mon 占满侧栏导航与状态栏之间的空白；放不下时按优先级减行，
 *        少于 2 行就整体隐藏（ResizeObserver 驱动）。
 *  配色：读 #perf-mon 上的 CSS 变量（--pm-line / --pm-fill / --pm-grid / --pm-hot / --pm-bg / --pm-dot），
 *        各主题在自己的样式里覆盖即可。
 */
(function () {
  "use strict";
  var App = window.App;
  var panel = document.getElementById("perf-mon");
  if (!App || !panel) return;

  var N = 60;   // 曲线上的点数（≈ 60 秒）

  function gib(mib) { return (mib / 1024).toFixed(mib >= 10 * 1024 ? 0 : 1); }
  function na() { return { v: null, text: "N/A" }; }
  function temp(v) { return v == null ? na() : { v: v, text: Math.round(v) + "°C" }; }
  function usage(used, total) {
    if (used == null || !total) return na();
    var p = used / total * 100;
    return { v: p, text: Math.round(p) + "% · " + gib(used) + "/" + gib(total) + "G" };
  }

  // lo/hi：曲线纵轴范围；warn：当前值超过它就显示警示色
  var METRICS = [
    { key: "gpu_temp", label: "GPU 温度", lo: 20, hi: 100, warn: 83, gpu: true, pick: function (s) { return temp(s.gpu_temp); } },
    { key: "gpu_util", label: "GPU 占用", lo: 0, hi: 100, warn: 101, gpu: true,
      pick: function (s) { return s.gpu_util == null ? na() : { v: s.gpu_util, text: Math.round(s.gpu_util) + "%" }; } },
    { key: "mem_temp", label: "显存温度", lo: 20, hi: 110, warn: 95, gpu: true, pick: function (s) { return temp(s.mem_temp); } },
    { key: "vram", label: "显存占用", lo: 0, hi: 100, warn: 92, gpu: true, pick: function (s) { return usage(s.vram_used, s.vram_total); } },
    { key: "ram", label: "内存占用", lo: 0, hi: 100, warn: 90, pick: function (s) { return usage(s.ram_used, s.ram_total); } },
  ];
  // 空间不够时先藏谁（显存温度很多驱动本来就是 N/A，最先让位）
  var HIDE_ORDER = [2, 4, 0, 3, 1];

  var box = null, raf = 0, lastAt = 0, started = false;

  function el(tag, cls, parent) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (parent) parent.appendChild(n);
    return n;
  }

  function build() {
    box = el("div", "pm-box", panel);
    var head = el("div", "pm-title", box);
    head.innerHTML = '<span class="pm-t">PERF <b>//</b> 性能监控</span><i class="pm-live"></i>';
    METRICS.forEach(function (m) {
      var row = el("div", "pm-row na", box);
      row.dataset.key = m.key;
      var h = el("div", "pm-head", row);
      el("span", "pm-label", h).textContent = m.label;
      m.valEl = el("b", "pm-val", h);
      m.valEl.textContent = "--";
      m.cv = el("canvas", "pm-spark", row);
      m.row = row;
      m.hist = [];
    });
  }

  /* ---------- 放得下几行 ---------- */
  function fit() {
    if (!box) return;
    var cs = getComputedStyle(panel);
    var avail = panel.clientHeight - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0);
    panel.classList.remove("pm-collapsed");
    METRICS.forEach(function (m) { m.row.hidden = false; });
    var shown = METRICS.length, k = 0;
    while (shown > 0 && box.offsetHeight > avail && k < HIDE_ORDER.length) {
      METRICS[HIDE_ORDER[k++]].row.hidden = true;
      shown--;
    }
    panel.classList.toggle("pm-collapsed", shown < 2);
    schedule();
  }

  /* ---------- 绘制 ---------- */
  function schedule() { if (!raf) raf = requestAnimationFrame(render); }

  function render() {
    raf = 0;
    if (!box || document.hidden || panel.classList.contains("pm-collapsed") || panel.classList.contains("pm-off")) return;
    var cs = getComputedStyle(panel);
    function v(name, def) { return (cs.getPropertyValue(name) || "").trim() || def; }
    var col = {
      line: v("--pm-line", "#fff200"), fill: v("--pm-fill", "rgba(255,242,0,.12)"),
      grid: v("--pm-grid", "rgba(255,255,255,.08)"), hot: v("--pm-hot", "#ff5a4e"),
      round: v("--pm-dot", "square") === "round",
    };
    METRICS.forEach(function (m) { if (!m.row.hidden) draw(m, col); });
  }

  function draw(m, col) {
    var cv = m.cv, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    // 后备缓冲按实际屏幕像素算（兼容界面缩放和高 DPI），不然会糊
    var r = cv.getBoundingClientRect();
    var k = (r.width / w || 1) * (window.devicePixelRatio || 1);
    var W = Math.max(1, Math.round(w * k)), H = Math.max(1, Math.round(h * k));
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; }
    var g = cv.getContext("2d");
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, W, H);
    g.setTransform(k, 0, 0, k, 0, 0);

    // 中线（虚线网格）
    g.strokeStyle = col.grid; g.lineWidth = 1; g.setLineDash([2, 3]);
    g.beginPath(); g.moveTo(0, Math.round(h / 2) + .5); g.lineTo(w, Math.round(h / 2) + .5); g.stroke();
    g.setLineDash([]);

    var hist = m.hist, n = hist.length;
    if (!n) return;
    var step = (w - 3) / (N - 1), off = N - n, segs = [], cur = null;
    for (var i = 0; i < n; i++) {
      var val = hist[i];
      if (val == null) { cur = null; continue; }      // N/A 处断开曲线
      var t = (val - m.lo) / (m.hi - m.lo);
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      if (!cur) { cur = []; segs.push(cur); }
      cur.push([(off + i) * step + 1, 1.5 + (1 - t) * (h - 3)]);
    }
    var last = hist[n - 1];
    var line = last != null && last >= m.warn ? col.hot : col.line;
    g.lineWidth = 1.3; g.lineJoin = "round"; g.lineCap = "round";
    segs.forEach(function (s) {
      // 面积
      g.beginPath();
      g.moveTo(s[0][0], h);
      s.forEach(function (p) { g.lineTo(p[0], p[1]); });
      g.lineTo(s[s.length - 1][0], h);
      g.closePath();
      g.fillStyle = col.fill; g.fill();
      // 折线
      g.beginPath();
      s.forEach(function (p, j) { if (j) g.lineTo(p[0], p[1]); else g.moveTo(p[0], p[1]); });
      g.strokeStyle = line; g.stroke();
    });
    // 最新一点
    if (last != null && segs.length) {
      var s = segs[segs.length - 1], p = s[s.length - 1];
      g.fillStyle = line;
      if (col.round) { g.beginPath(); g.arc(p[0], p[1], 2, 0, Math.PI * 2); g.fill(); }
      else g.fillRect(p[0] - 1.5, p[1] - 1.5, 3, 3);
    }
  }

  /* ---------- 数据 ---------- */
  function push(s, quiet) {
    if (!s || !box) return;
    lastAt = Date.now();
    box.classList.remove("stale");
    box.classList.toggle("pm-nogpu", !s.gpu);
    METRICS.forEach(function (m) {
      var p = m.pick(s);
      m.hist.push(p.v);
      if (m.hist.length > N) m.hist.shift();
      m.valEl.textContent = p.text;
      m.row.classList.toggle("na", p.v == null);
      m.row.classList.toggle("hot", p.v != null && p.v >= m.warn);
      m.row.title = p.v != null ? "" :
        (m.gpu && !s.gpu ? "未检测到 NVIDIA 显卡或 nvidia-smi，GPU 指标不可用"
          : m.key === "mem_temp" ? "当前显卡 / 驱动不提供显存温度（nvidia-smi 返回 N/A）" : "暂时读不到该指标");
    });
    if (!quiet) schedule();
  }

  function start() {
    if (started) return;
    var api = App.api;
    if (!api || typeof api.perf_start !== "function") { panel.classList.add("pm-off"); return; }
    started = true;
    Promise.resolve(api.perf_start()).then(function (r) {
      if (r && r.history && r.history.length) {
        METRICS.forEach(function (m) { m.hist = []; });
        r.history.forEach(function (s) { push(s, true); });
        schedule();
      }
    }).catch(function () { started = false; });
  }

  App.on("perf", "sample", function (e) { push(e.sample); });

  build();
  fit();
  if (window.ResizeObserver) new ResizeObserver(fit).observe(panel);
  else window.addEventListener("resize", fit);
  // 切主题：样式表异步加载，过一会儿再量一次、重画一次
  window.addEventListener("wwy-theme", function () { setTimeout(fit, 30); setTimeout(fit, 400); });
  document.addEventListener("visibilitychange", schedule);

  // 等 core.js 的 boot 完成（App.api 已注入、后端已应答）再启动采样
  var waitTimer = setInterval(function () {
    if (App.ready) { clearInterval(waitTimer); start(); fit(); }
  }, 300);
  // 超过 5 秒没收到新采样：实时指示灯熄灭（后端卡住 / 窗口刚恢复）
  setInterval(function () {
    if (started && lastAt && Date.now() - lastAt > 5000) box.classList.add("stale");
  }, 2000);
})();