/* dissoc-splash.js — 「离解 · 黑白构成」开屏（必须在 splash.js 之前加载，splash.js 按变体名分发过来）
 *
 * 按「离解 → 复合」编排，约 3 秒：
 *   0.00  黑屏正中一个黄绿点，向两边拉成一条线
 *   0.35  白纸从这条线上下裂开，露出网格
 *   0.60  散落在四周的碎片和四片叶子一起飞回中心，拼成四叶 X（叶子边飞边转）
 *   1.30  中心黄绿点亮起，一圈光环扩散；X 一边缩小一边移到左侧
 *   1.50  「WWY」三个字母从上下不同高度落下并解码，「LAUNCHER」字距从很松收紧
 *   1.85  四角取景框画出来，标注 / 时间码 / 副标题擦出
 *   2.10  一块反色板（差值混合）从左扫到右，扫过的地方黑白互换
 *   2.50  信息框打字，✦ 闪一下（后端还没就绪就停在这里，光标闪）
 *   收尾  画面抖一下（错位切片），压成一条横线后消失，主界面从同一条线里展开（window.DissocUI.intro）
 * 就绪后点击可直接收尾。接口同 splash.js：window.Splash.ready()。样式在 splash.css 末尾。
 */
(function () {
  "use strict";

  var BLADES = ["M0 0C6-14 20-30 40-40C30-20 14-6 0 0Z", "M0 0C14 6 30 20 40 40C20 30 6 14 0 0Z",
                "M0 0C-6 14-20 30-40 40C-30 20-14 6 0 0Z", "M0 0C-14-6-30-20-40-40C-20-30-6-14 0 0Z"];
  var STAR = '<svg viewBox="-50 -50 100 100" aria-hidden="true"><path fill="currentColor" ' +
    'd="M0-50C4-12 12-4 50 0C12 4 4 12 0 50C-4 12-12 4-50 0C-12-4-4-12 0-50Z"/></svg>';
  var GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789/#+-";
  var MSG = "Dissociation complete. Recombining…";

  // WebGL 粒子版（js/dissoc-gl.js 渲染粒子，这里只管叠在上面的清晰文字、闪光和收尾）；跑不起来返回 false
  function glSplash(host) {
    if (!window.DissocGL) return false;
    var MAX = 9000, ready = false, minDone = false, out = false, tms = [];
    host.className = "sp-dissoc sp-dissoc-gl";
    host.style.background = "#050506";
    var ctl = window.DissocGL(host);
    if (!ctl) { host.className = ""; host.innerHTML = ""; return false; }
    host.dxgl = ctl;   // 调试时在控制台定格画面用（host.dxgl.freeze(秒)）
    var ui = document.createElement("div");
    ui.className = "dxg-ui";
    ui.innerHTML =
      '<div class="dxg-scan"></div>' +
      '<div class="dxg-tl"><span>Dissociation</span><b class="dxg-pct">000</b><span>Recombination</span></div>' +
      '<div class="dxg-brk"><i></i><i></i><i></i><i></i></div>' +
      '<div class="dxg-sub">AI IMAGE GENERATION LAUNCHER — SYSTEM 3.1</div>' +
      '<div class="dxg-tc"><span>Time axis</span><b>00:00:00:00</b></div>' +
      '<div class="dxg-bar"><i></i></div>' +
      '<div class="dxg-flash"></div><div class="dxg-reveal"></div>' +
      '<button class="dxs-skip dxg-skip" type="button">SKIP ›</button>';
    host.appendChild(ui);
    // 副标题贴在粒子大字下面（按最终镜头算出来的屏幕位置）
    var sub = ui.querySelector(".dxg-sub");
    sub.style.left = ctl.text.left + "px";
    sub.style.top = (ctl.text.bottom + 18) + "px";
    var rv = ui.querySelector(".dxg-reveal");
    rv.style.setProperty("--cx", ctl.core[0] + "px");
    rv.style.setProperty("--cy", ctl.core[1] + "px");
    var pct = ui.querySelector(".dxg-pct"), tc = ui.querySelector(".dxg-tc b"), bar = ui.querySelector(".dxg-bar i");
    function pad(n, w) { return String(n).padStart(w || 2, "0"); }
    var t0 = performance.now(), iv = setInterval(function () {
      var ms = performance.now() - t0, s = ms / 1000;
      var p = Math.min(100, Math.round(s / 2.9 * 100));
      pct.textContent = pad(p, 3);
      bar.style.transform = "scaleX(" + (p / 100) + ")";
      tc.textContent = "00:00:" + pad(Math.floor(s) % 60) + ":" + pad(Math.floor(s * 12) % 12);
    }, 50);
    function at(ms, fn) { tms.push(setTimeout(fn, ms)); }
    function add(c) { host.classList.add(c); }
    at(60, function () { add("g-on"); });
    at(1350, function () { add("g-flash"); });
    at(2250, function () { add("g-info"); });
    at(2950, function () { minDone = true; if (ready) leave(); else add("g-hold"); });
    function leave() {
      if (out) return;
      out = true;
      tms.forEach(clearTimeout);
      add("g-out");                 // 粒子冲向镜头，白光从 X 核心扩散
      ctl.exit();
      setTimeout(function () { if (window.DissocUI) window.DissocUI.intro("soft"); }, 420);
      setTimeout(function () { clearInterval(iv); ctl.destroy(); host.remove(); }, 820);
    }
    ui.querySelector(".dxg-skip").addEventListener("click", function (e) { e.stopPropagation(); if (ready) leave(); });
    host.addEventListener("click", function () { if (ready) leave(); });
    window.Splash = { ready: function () { ready = true; add("r-ready"); if (minDone) leave(); } };
    setTimeout(function () { ready = true; if (minDone) leave(); }, MAX);
    return true;
  }

  window.DissocSplash = function (host) {
    var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduce && glSplash(host)) return;
    var K = reduce ? .3 : 1;
    var MAX = 9000;
    var ready = false, minDone = false, out = false, done = false, tms = [], ivs = [];
    host.className = "sp-dissoc";
    host.style.background = "#050506";

    // 固定种子：每次打开碎片飞来的方向一样
    var seed = 11;
    function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
    function word(t, cls, jump) {
      return '<div class="' + cls + '">' + t.split("").map(function (c, i) {
        var dy = jump ? ((i % 2 ? -1 : 1) * (60 + rnd() * 80)).toFixed(0) + "%" : "0";
        return '<i style="--i:' + i + ";--dy:" + dy + '" data-c="' + c + '">' + c + "</i>";
      }).join("") + "</div>";
    }
    var frags = "";
    for (var k = 0; k < 34; k++) {
      var a = rnd() * Math.PI * 2, r0 = 45 + rnd() * 40, r1 = rnd() * 6;
      var kind = k % 3 === 0 ? "bar" : k % 3 === 1 ? "sq" : "ln";
      frags += '<i class="' + kind + '" style="--x0:' + (Math.cos(a) * r0).toFixed(1) + "vw;--y0:" + (Math.sin(a) * r0 * .7).toFixed(1) +
        "vh;--x1:" + (Math.cos(a) * r1).toFixed(1) + "vmin;--y1:" + (Math.sin(a) * r1).toFixed(1) + "vmin;--r0:" +
        ((rnd() - .5) * 540).toFixed(0) + "deg;--d:" + (rnd() * .25).toFixed(2) + 's"></i>';
    }
    var blades = BLADES.map(function (d, i) {
      var a = (i * 90 + 45 + (rnd() - .5) * 40) * Math.PI / 180;
      return '<path class="bl" d="' + d + '" style="--bx:' + (Math.cos(a) * 520).toFixed(0) + "px;--by:" + (Math.sin(a) * 380).toFixed(0) +
        "px;--br:" + ((i % 2 ? 1 : -1) * (220 + rnd() * 140)).toFixed(0) + "deg;--d:" + (i * .06).toFixed(2) + 's"/>';
    }).join("");

    host.innerHTML =
      '<div class="dxs-paper"><i class="dxs-major m1"></i><i class="dxs-major m2"></i><i class="dxs-major m3"></i></div>' +
      '<i class="dxs-hline"></i><i class="dxs-dot"></i>' +
      '<div class="dxs-frags">' + frags + "</div>" +
      '<div class="dxs-xwrap"><i class="dxs-ring"></i><svg viewBox="-50 -50 100 100" aria-hidden="true">' + blades +
        '<circle class="dxs-core" r="5"/></svg></div>' +
      '<div class="dxs-brk"><i class="tl"></i><i class="tr"></i><i class="bl"></i><i class="br"></i></div>' +
      '<div class="dxs-labs"><span>Time axis</span><span>Convolution</span><span>Dissociate · <b>01</b></span></div>' +
      '<div class="dxs-title">' + word("WWY", "dxs-l1", true) + word("LAUNCHER", "dxs-l2", false) + "</div>" +
      '<div class="dxs-sub">AI IMAGE GENERATION LAUNCHER — SYSTEM 3.1</div>' +
      '<div class="dxs-msg"><span class="dxs-lab">Message</span><p><span class="dxs-typed"></span><i class="dxs-cur"></i></p></div>' +
      '<i class="dxs-star">' + STAR + "</i>" +
      '<div class="dxs-tc"><span>Time axis</span><b>00:00:00:00</b></div>' +
      '<i class="dxs-invert"></i>' +
      '<button class="dxs-skip" type="button">SKIP ›</button>';

    function q(s) { return host.querySelector(s); }
    function at(ms, fn) { tms.push(setTimeout(fn, ms * K)); }
    function add(c) { host.classList.add(c); }
    function pad(n) { return String(n).padStart(2, "0"); }
    function decode(sel, per, lead) {
      var gs = host.querySelectorAll(sel + " i"), t = 0;
      var iv = setInterval(function () {
        t++;
        var left = 0;
        Array.prototype.forEach.call(gs, function (g, i) {
          if (t > lead + i * per) g.textContent = g.dataset.c;
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

    // 等页面脚本解析完再开始（最多等 600ms），免得第一段动画被主线程卡住
    requestAnimationFrame(function () { add("p-dot"); });
    var started = false;
    function begin() { if (started) return; started = true; timeline(); }
    if (document.readyState === "complete") setTimeout(begin, 0);
    else { window.addEventListener("load", function () { setTimeout(begin, 30); }); setTimeout(begin, 600); }
    function timeline() {
      at(60, function () { add("p-line"); });
      at(350, function () { add("p-open"); });
      at(600, function () { add("p-frag"); });
      at(1300, function () { add("p-core"); });
      at(1420, function () { add("p-move"); });
      at(1520, function () { add("p-title"); decode(".dxs-l1", 3, 4); decode(".dxs-l2", 1.2, 6); });
      at(1850, function () { add("p-info"); });
      at(2100, function () { add("p-invert"); });
      at(2500, function () { add("p-msg"); type(q(".dxs-typed"), MSG, 16); });
      at(3000, function () { minDone = true; if (ready) leave(false); else add("p-hold"); });
    }

    function leave(fast) {
      if (out) return;
      out = true;
      tms.forEach(clearTimeout); tms = [];
      if (fast) {
        Array.prototype.forEach.call(host.querySelectorAll(".dxs-title i"), function (g) { g.textContent = g.dataset.c; });
        q(".dxs-typed").textContent = MSG;
        ["p-dot", "p-line", "p-open", "p-frag", "p-core", "p-move", "p-title", "p-info", "p-msg"].forEach(add);
      }
      host.classList.remove("p-hold");
      add("p-glitch");                                   // 错位抖动
      setTimeout(function () { add("p-out"); }, 260 * K);  // 压成一条横线
      setTimeout(function () { if (window.DissocUI) window.DissocUI.intro(); }, 560 * K);
      setTimeout(function () {
        done = true;
        ivs.forEach(clearInterval);
        host.remove();
      }, 980 * K);
    }

    q(".dxs-skip").addEventListener("click", function (e) { e.stopPropagation(); if (ready) leave(true); });
    host.addEventListener("click", function () { if (ready) leave(true); });
    window.Splash = { ready: function () { ready = true; add("r-ready"); if (minDone) leave(false); } };
    setTimeout(function () { ready = true; if (minDone) leave(false); }, MAX);
  };
})();
