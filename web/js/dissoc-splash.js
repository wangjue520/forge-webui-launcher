/* dissoc-splash.js — 「离解 · 黑白构成」开屏（必须在 splash.js 之前加载，splash.js 按变体名分发过来）
 *
 * 约 2.4 秒，同一时刻只有一个焦点：
 *   0.00  白纸亮起，细网格从左上角铺开，三道粗网格线依次划过
 *   0.30  左侧黑→白的大渐变从屏幕外推进来
 *   0.55  四叶 X 旋转着落到黑白交界处，中心黄绿点亮
 *   0.80  「WWY / LAUNCHER」逐字解码落定，左上标注和右下时间码开始跳
 *   1.40  信息框打出一行字，✦ 闪一下
 *   1.70  一道扫描线从上往下扫过（后端还没就绪就停在这里，信息框后面一个闪烁光标）
 *   收尾  整屏压成一条横线后消失，主界面从同一条线里展开（window.DissocUI.intro）
 * 就绪后点击可直接收尾。接口同 splash.js：window.Splash.ready()。样式在 splash.css 末尾。
 */
(function () {
  "use strict";

  var X_SVG = '<svg viewBox="-50 -50 100 100" aria-hidden="true"><g fill="currentColor">' +
    '<path d="M0 0C6-14 20-30 40-40C30-20 14-6 0 0Z"/><path d="M0 0C14 6 30 20 40 40C20 30 6 14 0 0Z"/>' +
    '<path d="M0 0C-6 14-20 30-40 40C-30 20-14 6 0 0Z"/><path d="M0 0C-14-6-30-20-40-40C-20-30-6-14 0 0Z"/></g>' +
    '<circle class="dxs-core" r="4.5"/></svg>';
  var STAR = '<svg viewBox="-50 -50 100 100" aria-hidden="true"><path fill="currentColor" ' +
    'd="M0-50C4-12 12-4 50 0C12 4 4 12 0 50C-4 12-12 4-50 0C-12-4-4-12 0-50Z"/></svg>';
  var GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789/#+-";

  window.DissocSplash = function (host) {
    var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var K = reduce ? .35 : 1;
    var MAX = 9000;
    var ready = false, minDone = false, out = false, done = false, tms = [], ivs = [];
    host.className = "sp-dissoc";
    host.style.background = "#f3f3f1";

    function word(t, cls) {
      return '<div class="' + cls + '">' + t.split("").map(function (c, i) {
        return '<i style="--i:' + i + '" data-c="' + c + '">' + c + "</i>";
      }).join("") + "</div>";
    }
    host.innerHTML =
      '<div class="dxs-grid"></div>' +
      '<i class="dxs-major m1"></i><i class="dxs-major m2"></i><i class="dxs-major m3"></i>' +
      '<div class="dxs-grad"></div>' +
      '<div class="dxs-x">' + X_SVG + "</div>" +
      '<div class="dxs-labs"><span>Time axis</span><span>Convolution</span><span>Dissociate · <b>01</b></span></div>' +
      '<div class="dxs-title">' + word("WWY", "dxs-l1") + word("LAUNCHER", "dxs-l2") + "</div>" +
      '<div class="dxs-sub">AI IMAGE GENERATION LAUNCHER — SYSTEM 3.1</div>' +
      '<div class="dxs-msg"><span class="dxs-lab">Message</span><p><span class="dxs-typed"></span><i class="dxs-cur"></i></p></div>' +
      '<div class="dxs-tc"><span>Time axis</span><b>00:00:00:00</b></div>' +
      '<i class="dxs-star">' + STAR + "</i>" +
      '<i class="dxs-scan"></i>' +
      '<button class="dxs-skip" type="button">SKIP ›</button>';

    function q(s) { return host.querySelector(s); }
    function at(ms, fn) { tms.push(setTimeout(fn, ms * K)); }
    function add(c) { host.classList.add(c); }
    function pad(n) { return String(n).padStart(2, "0"); }

    function decode(sel, per) {
      var gs = host.querySelectorAll(sel + " i"), t = 0;
      var iv = setInterval(function () {
        t++;
        var left = 0;
        Array.prototype.forEach.call(gs, function (g, i) {
          if (t > 2 + i * per) g.textContent = g.dataset.c;
          else { g.textContent = GLYPHS[(Math.random() * GLYPHS.length) | 0]; left++; }
        });
        if (!left) clearInterval(iv);
      }, 34);
      ivs.push(iv);
    }
    function type(node, text, per) {
      var i = 0;
      (function step() {
        if (done || out) return;
        node.textContent = text.slice(0, ++i);
        if (i < text.length) tms.push(setTimeout(step, per * K));
      })();
    }
    var t0 = performance.now(), tc = q(".dxs-tc b");
    ivs.push(setInterval(function () {
      var ms = performance.now() - t0, s = Math.floor(ms / 1000);
      tc.textContent = "00:00:" + pad(s % 60) + ":" + pad(Math.floor(ms / 1000 * 12) % 12);
    }, 83));

    // 时间轴等页面脚本解析完再开始（最多等 600ms），免得第一段动画被主线程卡住
    requestAnimationFrame(function () { add("p-on"); });
    var started = false;
    function begin() { if (started) return; started = true; timeline(); }
    if (document.readyState === "complete") setTimeout(begin, 0);
    else { window.addEventListener("load", function () { setTimeout(begin, 30); }); setTimeout(begin, 600); }
    function timeline() {
      at(40, function () { add("p-grid"); });
      at(300, function () { add("p-grad"); });
      at(550, function () { add("p-x"); });
      at(800, function () { add("p-title"); decode(".dxs-l1", 2); decode(".dxs-l2", 1.4); });
      at(1150, function () { add("p-sub"); });
      at(1400, function () { add("p-msg"); type(q(".dxs-typed"), "Welcome back. Dissociation complete.", 18); });
      at(1700, function () { add("p-scan"); });
      at(2350, function () { minDone = true; if (ready) leave(false); else add("p-hold"); });
    }

    function leave(fast) {
      if (out) return;
      out = true;
      tms.forEach(clearTimeout); tms = [];
      if (fast) {
        Array.prototype.forEach.call(host.querySelectorAll(".dxs-title i"), function (g) { g.textContent = g.dataset.c; });
        q(".dxs-typed").textContent = "Welcome back. Dissociation complete.";
        ["p-on", "p-grid", "p-grad", "p-x", "p-title", "p-sub", "p-msg"].forEach(add);
      }
      host.classList.remove("p-hold");
      add("p-out");                                   // 压成一条横线
      setTimeout(function () { if (window.DissocUI) window.DissocUI.intro(); }, 380 * K);
      setTimeout(function () {
        done = true;
        ivs.forEach(clearInterval);
        host.remove();
      }, 760 * K);
    }

    q(".dxs-skip").addEventListener("click", function (e) { e.stopPropagation(); if (ready) leave(true); });
    host.addEventListener("click", function () { if (ready) leave(true); });
    window.Splash = { ready: function () { ready = true; add("r-ready"); if (minDone) leave(false); } };
    setTimeout(function () { ready = true; if (minDone) leave(false); }, MAX);
  };
})();
