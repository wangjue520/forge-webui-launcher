/* rhine-splash.js — 「莱茵 · 分析终端」开屏（必须在 splash.js 之前加载，splash.js 按变体名分发过来）
 *
 * 按 3 秒设计，取《明日方舟》特别映像「莱茵生命：访问」开场里最有辨识度的几个镜头，串成一条线
 * （徽记与文字是自己的，不用官方标志）。同一时刻只有一个焦点：
 *
 *   0.00  纸面亮起，背景两道极淡的巨大徽记曲线
 *   0.08  笔刷一笔画出徽记（粗笔触、圆头），笔尖经过时下方的 W W Y · L A B 逐个亮起
 *   0.60  左上角品牌打字出现——位置、字号和主界面侧栏的品牌完全一致，之后一直留在原地
 *   0.70  徽记中心的点与黄铜方块弹出
 *   1.00  徽记缩小、下移到欢迎文字下方（FLIP），同时打出 WELCOME TO
 *   1.26  公司名先以普通黑字打出，随后一块黑色色块从左往右扫过，把它反成黑底白字
 *   1.80  INTERNAL DATABASE ——（后端还没就绪就停在这里，色块后面一个闪烁光标）
 *   1.95  过曝：白光泛起，中间文字出现青 / 红色散并糊开
 *   2.20  主界面在白光后面就位：档案阵列带着横向动态模糊涨起来（window.RhineUI.intro）
 *   2.95  白光退尽，开屏移除；左上角品牌无缝落在侧栏同一位置
 * 就绪后点击可直接跳到过曝。接口同 splash.js：window.Splash.ready()。音效见 rhine-sfx.js。
 */
(function () {
  "use strict";

  // 徽记：左边圆环、右边圆角方环，从中心交叉点出发一笔连成（viewBox 0 0 240 112）
  var MARK_PATH = "M120 54 C108 39 97 20 70 20 A34 34 0 0 0 70 88 C97 88 108 69 120 54 " +
                  "C132 39 141 22 152 22 L188 22 Q202 22 202 36 L202 72 Q202 86 188 86 L152 86 C141 86 132 69 120 54";

  window.RhineSplash = function (host) {
    var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    var K = reduce ? .35 : 1;
    var MAX = 9000;
    var ready = false, minDone = false, out = false, done = false, tms = [], rafs = [];
    host.className = "sp-rhine4";
    host.style.background = "#e9e6e1";

    var sub = "WWY·LAB".split("").map(function (c) { return "<span>" + c + "</span>"; }).join("");
    host.innerHTML =
      '<svg class="r4-bg" viewBox="0 0 240 112" aria-hidden="true"><path d="' + MARK_PATH + '"/></svg>' +
      '<svg class="r4-bg r4-bg2" viewBox="0 0 240 112" aria-hidden="true"><path d="' + MARK_PATH + '"/></svg>' +
      '<div class="r4-brand rh-brand"><div class="rb-h"></div><div class="rb-sub"></div><div class="rb-os"><span></span><b></b></div></div>' +
      '<div class="r4-stack">' +
        '<div class="r4-w1"><span></span></div>' +
        '<div class="r4-w2"><span class="n0"></span><span class="n1">WWY LAB, LLC.</span><i class="r4-cur"></i></div>' +
        '<div class="r4-w3">INTERNAL DATABASE</div>' +
        '<div class="r4-slot"></div>' +
      '</div>' +
      '<div class="r4-logo">' +
        '<svg viewBox="0 0 240 112" aria-hidden="true">' +
          '<path class="mk-path" d="' + MARK_PATH + '"/>' +
          '<circle class="mk-dot" cx="70" cy="54" r="7"/>' +
          '<rect class="mk-sq" x="162" y="46" width="16" height="16" transform="rotate(45 170 54)"/>' +
          '<circle class="mk-pen" r="7" cx="120" cy="54"/>' +
        '</svg>' +
        '<div class="r4-sub">' + sub + '</div>' +
      '</div>' +
      '<div class="r4-bloom"></div>' +
      '<button class="r4-skip" type="button">SKIP<span>›</span></button>';

    function q(s) { return host.querySelector(s); }
    function at(ms, fn) { tms.push(setTimeout(fn, ms * K)); }
    function add(c) { host.classList.add(c); }
    function sfx(n, d, a) { if (window.RhineSfx) RhineSfx.play(n, d, a); }
    function type(node, text, per, cb, quiet) {
      if (!node) return;
      var i = 0;
      (function step() {
        if (done || out) return;
        node.textContent = text.slice(0, ++i);
        if (!quiet && text[i - 1] !== " ") sfx("type");
        if (i < text.length) tms.push(setTimeout(step, per * K)); else if (cb) cb();
      })();
    }

    // 笔刷一笔画出徽记；笔尖每走过一段，下方字母亮一个
    function draw(ms) {
      var svg = q(".r4-logo svg"), path = svg.querySelector(".mk-path"), pen = svg.querySelector(".mk-pen");
      var letters = host.querySelectorAll(".r4-sub span"), L = path.getTotalLength(), lit = 0;
      path.style.strokeDasharray = L; path.style.strokeDashoffset = L;
      var t0 = performance.now();
      (function f(now) {
        if (done) return;
        var p = Math.min(1, (now - t0) / (ms * K));
        var e = 1 - Math.pow(1 - p, 2.2);
        path.style.strokeDashoffset = (L * (1 - e)).toFixed(1);
        var pt = path.getPointAtLength(L * e);
        pen.setAttribute("cx", pt.x.toFixed(1)); pen.setAttribute("cy", pt.y.toFixed(1));
        while (lit < letters.length && e > (lit + 1) / (letters.length + 1)) letters[lit++].classList.add("on");
        if (p < 1) rafs.push(requestAnimationFrame(f));
        else { for (; lit < letters.length; lit++) letters[lit].classList.add("on"); add("p-drawn"); sfx("tick", 0, 4); }
      })(t0);
    }

    // 徽记从屏幕中心挪到欢迎文字下面的位置（FLIP：量好目标再用 transform 过去）
    function settleLogo() {
      var logo = q(".r4-logo"), slot = q(".r4-slot");
      var a = logo.getBoundingClientRect(), b = slot.getBoundingClientRect();
      if (!a.width) return;
      var s = b.width / a.width;
      var dx = (b.left + b.width / 2) - (a.left + a.width / 2), dy = (b.top + b.height / 2) - (a.top + a.height / 2);
      logo.style.transform = "translate(-50%, -50%) translate(" + dx.toFixed(1) + "px," + dy.toFixed(1) + "px) scale(" + s.toFixed(3) + ")";
    }

    var bh = q(".r4-brand .rb-h"), bs = q(".r4-brand .rb-sub"), bo = q(".r4-brand .rb-os span"), bb = q(".r4-brand .rb-os b");

    // ---------- 时间轴 ----------
    // 开屏脚本比其他页面脚本先跑，那时主线程还在解析一大堆脚本，笔刷会卡在第一笔；
    // 所以纸面先亮，时间轴等页面加载完（最多等 700ms）再开始，保证 3 秒都是顺的
    requestAnimationFrame(function () { add("p-on"); });
    var started = false;
    function begin() { if (started) return; started = true; timeline(); }
    if (document.readyState === "complete") setTimeout(begin, 0);
    else { window.addEventListener("load", function () { setTimeout(begin, 30); }); setTimeout(begin, 700); }
    function timeline() {
    at(80, function () { add("p-draw"); sfx("rise"); draw(620); });
    at(600, function () {
      type(bh, "WWY LAB", 34, null, true);
      type(bs, "SYNTHESIZE INFORMATION", 9, null, true);
      type(bo, "ANALYSIS", 26, null, true);
    });
    at(820, function () { type(bb, "OS", 50, null, true); });
    at(940, function () { add("p-welcome"); settleLogo(); });     // 徽记先让位，文字再出来，不叠在一起
    at(1200, function () { type(q(".r4-w1 span"), "WELCOME TO", 18); });
    at(1390, function () { add("p-name"); type(q(".r4-w2 .n0"), "WWY LAB, LLC.", 16, null, true); });
    at(1640, function () { add("p-redact"); sfx("tick", 0, 6); });
    at(1840, function () { add("p-db"); });
    at(1950, function () { minDone = true; if (ready) leave(false); else add("p-hold"); });
    }

    // ---------- 过曝 → 主界面 ----------
    function leave(fast) {
      if (out) return;
      out = true;
      tms.forEach(clearTimeout); tms = [];
      if (fast) {      // 跳过：直接补齐画面再过曝
        bh.textContent = "WWY LAB"; bs.textContent = "SYNTHESIZE INFORMATION"; bo.textContent = "ANALYSIS"; bb.textContent = "OS";
        q(".r4-w1 span").textContent = "WELCOME TO"; q(".r4-w2 .n0").textContent = "WWY LAB, LLC.";
        Array.prototype.forEach.call(host.querySelectorAll(".r4-sub span"), function (s) { s.classList.add("on"); });
        ["p-on", "p-draw", "p-drawn", "p-welcome", "p-name", "p-redact", "p-db"].forEach(add);
        var path = q(".r4-logo .mk-path"); path.style.strokeDashoffset = "0";
        settleLogo();
      }
      host.classList.remove("p-hold");
      add("p-flash"); sfx("welcome");
      setTimeout(function () {
        add("p-out");
        if (window.RhineUI) window.RhineUI.intro();
      }, 250 * K);
      setTimeout(function () { done = true; rafs.forEach(cancelAnimationFrame); host.remove(); }, 1000 * K);
    }

    q(".r4-skip").addEventListener("click", function (e) { e.stopPropagation(); if (ready) leave(true); });
    host.addEventListener("click", function () { if (ready) leave(true); });
    window.Splash = { ready: function () { ready = true; add("r-ready"); if (minDone) leave(false); } };
    setTimeout(function () { ready = true; if (minDone) leave(false); }, MAX);
  };
})();
