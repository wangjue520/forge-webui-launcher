/* perfmon.js — 侧栏性能监控（所有主题通用）
 *
 *  5 项指标：GPU 温度 / GPU 占用 / 显存温度 / 显存占用 / 系统内存占用
 *  每项一行：名称 + 当前值 + 最近约 60 秒的 sparkline（内联 SVG）。
 *  以前用 canvas 画：WebView2 的 canvas 走 GPU，生图 / 训练把显卡占满、驱动重置之后，
 *  画布偶尔整块变成白底、只剩一条灰线，要重开窗口才好（群友截图）。SVG 不依赖 GPU 画布，颜色也直接用 CSS 变量。
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

  var SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, parent) {
    var n = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { n.setAttribute(k, attrs[k]); });
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
      m.cv = svgEl("svg", { "class": "pm-spark", "aria-hidden": "true" }, row);
      svgEl("line", { "class": "pm-grid" }, m.cv);
      m.areaEl = svgEl("path", { "class": "pm-area" }, m.cv);
      m.lineEl = svgEl("path", { "class": "pm-ln" }, m.cv);
      m.dotEl = svgEl("rect", { "class": "pm-dot", width: 3, height: 3 }, m.cv);
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
    var round = (getComputedStyle(panel).getPropertyValue("--pm-dot") || "").trim() === "round";
    METRICS.forEach(function (m) { if (!m.row.hidden) draw(m, round); });
  }

  function f1(n) { return Math.round(n * 10) / 10; }

  // 坐标直接用 CSS 像素（viewBox = 实际宽高），线宽、端点不会被拉伸
  function draw(m, round) {
    var cv = m.cv, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.setAttribute("viewBox", "0 0 " + w + " " + h);
    var mid = Math.round(h / 2) + .5;
    var grid = cv.firstChild;
    grid.setAttribute("x1", 0); grid.setAttribute("x2", w);
    grid.setAttribute("y1", mid); grid.setAttribute("y2", mid);

    var hist = m.hist, n = hist.length;
    var step = (w - 3) / (N - 1), off = N - n, segs = [], cur = null;
    for (var i = 0; i < n; i++) {
      var val = hist[i];
      if (val == null) { cur = null; continue; }      // N/A 处断开曲线
      var t = (val - m.lo) / (m.hi - m.lo);
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      if (!cur) { cur = []; segs.push(cur); }
      cur.push([f1((off + i) * step + 1), f1(1.5 + (1 - t) * (h - 3))]);
    }
    var area = "", line = "";
    segs.forEach(function (s) {
      var pts = s.map(function (p) { return p[0] + " " + p[1]; });
      line += "M" + pts.join("L");
      area += "M" + s[0][0] + " " + h + "L" + pts.join("L") + "L" + s[s.length - 1][0] + " " + h + "Z";
    });
    m.areaEl.setAttribute("d", area);
    m.lineEl.setAttribute("d", line);
    var last = n ? hist[n - 1] : null;
    if (last != null && segs.length) {
      var s = segs[segs.length - 1], p = s[s.length - 1];
      m.dotEl.setAttribute("x", p[0] - 1.5); m.dotEl.setAttribute("y", p[1] - 1.5);
      m.dotEl.setAttribute("rx", round ? 1.5 : 0);
      m.dotEl.style.display = "";
    } else {
      m.dotEl.style.display = "none";
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