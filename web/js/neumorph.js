/* neumorph.js — 「新拟物」风格的动效与音效，只在 neu-* 风格下生效，其它风格什么都不做：
 *   · 机械滚轮数字（下面）
 *   · 翻页：旧页压平 → 新页浮起，配沉闷的「咚」
 *   · 拨杆开关拨动时清脆的「咔哒」，点任意按钮有薄膜键盘的「噗」
 *
 * 机械滚轮数字（里程表 / 计数器那种数字鼓）：
 * 业务代码照常往元素里写 textContent；这里用 MutationObserver 接住新文字，
 * 把每个数字换成一个竖向的数字鼓（0-9 循环三遍的长条），从旧数字滚到新数字：
 *   · 数值变大往上滚（9 → 0 时进位那一位会滚过一整格），变小往下滚
 *   · 原来没有的数字位（位数变多 / 第一次出现）从 0 滚起
 *   · 非数字字符原样显示
 *
 * 只挑「偶尔变一下」的数字（ROLL 列表）。显卡 / 显存占用、进度百分比、
 * 下载剩余量这类每秒都在变的不接——一直滚反而看不清，也费性能。
 */
(function () {
  "use strict";
  var ROLL = ["#out-count", "#out-sel-count", "#md-count"];
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var on = false, obs = null, busy = false;

  function isNeu(t) { return !!t && /^neu-/.test(t.id); }

  // 文字切成「数字串 / 其它」两类片段
  function runs(text) { return text.match(/\d+|\D+/g) || []; }
  function nums(text) { return (text.match(/\d+/g) || []); }

  function render(el, text) {
    var prev = el._nmText;
    el._nmText = text;
    if (!on || reduce || prev == null || !/\d/.test(text)) { el.textContent = text; return; }
    var oldNums = nums(prev), k = 0;
    var frag = document.createDocumentFragment(), cols = [];
    runs(text).forEach(function (r) {
      if (!/^\d/.test(r)) { frag.appendChild(document.createTextNode(r)); return; }
      var old = oldNums[k++] || "";
      var up = old === "" || Number(r) >= Number(old);
      var wrap = document.createElement("span");
      wrap.className = "nm-num"; wrap.setAttribute("aria-hidden", "true");
      for (var i = 0; i < r.length; i++) {
        // 右对齐比较：个位对个位
        var oi = old.length - (r.length - i);
        var from = oi >= 0 ? +old[oi] : 0, to = +r[i];
        var a = from + 10, b;
        if (up) b = to >= from ? to + 10 : to + 20;
        else b = to <= from ? to + 10 : to;
        var col = document.createElement("span"), strip = document.createElement("span");
        col.className = "nm-col"; strip.className = "nm-strip";
        strip.textContent = "012345678901234567890123456789";
        strip.style.transform = "translateY(" + (-a) + "em)";
        // 越靠左的位滚得越慢一点，像真的齿轮带动
        strip.style.transitionDuration = (520 + (r.length - 1 - i) * 90) + "ms";
        col.appendChild(strip); wrap.appendChild(col);
        if (a !== b) cols.push([strip, b]);
      }
      frag.appendChild(wrap);
    });
    el.textContent = "";
    el.appendChild(frag);
    el.setAttribute("aria-label", text);
    if (!cols.length) return;
    void el.offsetWidth;
    cols.forEach(function (c) { c[0].style.transform = "translateY(" + (-c[1]) + "em)"; });
  }

  function onMut(recs) {
    if (busy) return;
    var seen = [];
    recs.forEach(function (r) {
      var el = r.target.nodeType === 1 ? r.target : r.target.parentNode;
      while (el && !el._nmRoll) el = el.parentNode;
      if (el && seen.indexOf(el) < 0) seen.push(el);
    });
    busy = true;
    seen.forEach(function (el) {
      // 业务代码写进来的是纯文字；自己渲染出来的数字鼓不算新内容
      if (el.querySelector(".nm-num") && el.getAttribute("aria-label") === el._nmText) return;
      render(el, el.textContent);
    });
    obs.takeRecords();
    busy = false;
  }

  function start() {
    if (on) return;
    on = true;
    obs = obs || new MutationObserver(onMut);
    ROLL.forEach(function (sel) {
      var el = document.querySelector(sel);
      if (!el) return;
      el._nmRoll = true; el._nmText = el.textContent;     // 现有内容不滚，下次变化才滚
      obs.observe(el, { childList: true, characterData: true, subtree: true });
    });
  }

  function stop() {
    if (!on) return;
    on = false;
    if (obs) obs.disconnect();
    ROLL.forEach(function (sel) {
      var el = document.querySelector(sel);
      if (!el || !el._nmRoll) return;
      el._nmRoll = false;
      if (el._nmText != null) el.textContent = el._nmText;   // 换回纯文字
      el.removeAttribute("aria-label");
    });
  }

  function sync(t) { if (isNeu(t)) start(); else stop(); }
  window.addEventListener("wwy-theme", function (e) { sync(e.detail); });

  /* ---------- 音效（Web Audio 现场合成，不带音频文件） ----------
   * 翻页：沉闷的「咚」——低频正弦往下掉音 + 一点低通过的噪声，像软垫被压下去
   * 开关：清脆的「咔哒」——带通过的高频噪声瞬态 + 短促高音，紧跟一声更轻的回弹
   * AudioContext 第一次用到时才建（要在用户点击之后，WebView2 才允许出声） */
  var actx = null, noise = null, master = null;
  function audio() {
    if (!actx) {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try { actx = new AC(); } catch (e) { return null; }
      master = actx.createGain(); master.gain.value = .4; master.connect(actx.destination);
      noise = actx.createBuffer(1, Math.round(actx.sampleRate * .3), actx.sampleRate);
      var d = noise.getChannelData(0);
      for (var i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    if (actx.state === "suspended") actx.resume();
    return actx;
  }
  function env(g, t, peak, attack, decay) {
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(peak, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
  }
  function burst(t, filterType, freq, q, peak, decay) {
    var src = actx.createBufferSource(), f = actx.createBiquadFilter(), g = actx.createGain();
    src.buffer = noise; f.type = filterType; f.frequency.value = freq; f.Q.value = q;
    env(g, t, peak, .002, decay);
    src.connect(f); f.connect(g); g.connect(master);
    src.start(t); src.stop(t + decay + .02);
  }
  function tone(t, type, f0, f1, peak, attack, decay, dest) {
    var o = actx.createOscillator(), g = actx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(f0, t);
    if (f1) o.frequency.exponentialRampToValueAtTime(f1, t + attack + decay);
    env(g, t, peak, attack, decay);
    o.connect(g); g.connect(dest || master);
    o.start(t); o.stop(t + attack + decay + .02);
  }
  function playThud() {
    if (!audio()) return;
    var t = actx.currentTime + .005;
    var lp = actx.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 700; lp.connect(master);
    tone(t, "sine", 140, 52, .9, .006, .26, lp);
    tone(t, "triangle", 90, 45, .25, .004, .14, lp);
    burst(t, "lowpass", 380, .7, .35, .09);
  }
  function playClick(onState) {
    if (!audio()) return;
    var t = actx.currentTime + .003;
    burst(t, "bandpass", onState ? 4200 : 3400, 1.4, .7, .018);
    tone(t, "square", onState ? 2600 : 2000, 0, .07, .001, .03);
    burst(t + .022, "highpass", 5200, .8, .22, .012);      // 拨杆落到底的回弹
  }

  // 薄膜键盘：按下是闷闷的「噗」（橡胶碗塌下去），松开是更轻的回弹
  function playKey(down) {
    if (!audio()) return;
    var t = actx.currentTime + .002;
    if (down) {
      burst(t, "bandpass", 1150 + Math.random() * 250, 1.1, .5, .028);   // 每次音高略有不同，像真键盘
      tone(t, "sine", 210, 120, .32, .002, .045);
      burst(t + .004, "lowpass", 600, .7, .18, .03);
    } else {
      burst(t, "bandpass", 1700 + Math.random() * 300, 1.4, .16, .016);
    }
  }
  // 「任意按钮」= 所有 button / .btn / 胶囊等可点块；导航（有翻页声）和开关（有咔哒声）除外
  var KEY_SEL = "button, .btn, .chip, .choice-item, .radio-row, .cat-item, .facet, .col-item";
  function keyTarget(e) {
    var el = e.target && e.target.closest && e.target.closest(KEY_SEL);
    if (!on || !el || el.disabled || el.closest(".nav-item, .chk-row")) return null;
    return el;
  }
  var keyHeld = null;
  document.addEventListener("pointerdown", function (e) {
    if (e.button !== 0) return;
    var el = keyTarget(e);
    if (el) { keyHeld = el; playKey(true); }
  }, true);
  document.addEventListener("pointerup", function () {
    if (keyHeld) { keyHeld = null; playKey(false); }
  }, true);
  // 键盘回车 / 空格触发的点击（detail 为 0）也出声
  document.addEventListener("click", function (e) {
    if (e.detail === 0 && keyTarget(e)) playKey(true);
  }, true);

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!on || !el || !el.closest || !el.closest(".chk-row")) return;
    if (el.type === "checkbox") playClick(el.checked);
    else if (el.type === "radio") playKey(true);          // 列表里点选一项：按键声，不是开关声
  }, true);

  /* ---------- 翻页：旧页压平 → 换页 → 新页浮起 ----------
   * 包一层 App.showPage（导航点击、其它代码跳页都走它）。样式见 themes/neumorph.css
   * 的 .nm-sink / .nm-flat：阴影颜色是注册过的可动画属性，整页的凸起凹陷一起变化。
   * 压平期间又点了别的页：只记下最后的目标，压平结束直接去那一页。 */
  var SINK_MS = 110, RISE_MS = 300;
  var pending = null, sinkTimer = 0, riseTimer = 0;

  function pageEl(name) { return document.getElementById("page-" + name); }

  function wrapShowPage() {
    var App = window.App;
    if (!App || !App.showPage || App.showPage._nm) return;
    var orig = App.showPage;
    var wrapped = function (name) {
      var cur = App.currentPage;
      if (!on || reduce || !cur || cur === name || !pageEl(cur) || !pageEl(name)) {
        if (sinkTimer) { clearTimeout(sinkTimer); sinkTimer = 0; var s = document.querySelector(".page.nm-sink"); if (s) s.classList.remove("nm-sink"); }
        return orig.apply(this, arguments);
      }
      // 侧栏选中状态马上跟手，内容等旧页压平再换
      document.querySelectorAll(".nav-item").forEach(function (b) { b.classList.toggle("active", b.dataset.page === name); });
      if (pending !== name || !sinkTimer) playThud();
      pending = name;
      if (sinkTimer) return;
      pageEl(cur).classList.add("nm-sink");
      sinkTimer = setTimeout(function () {
        sinkTimer = 0;
        var target = pending, old = pageEl(cur), neu = pageEl(target);
        old.classList.remove("nm-sink");
        clearTimeout(riseTimer);
        document.querySelectorAll(".page.nm-flat").forEach(function (p) { p.classList.remove("nm-flat"); });
        neu.classList.add("nm-flat");
        orig.call(App, target);
        void neu.offsetWidth;                        // 先以压平状态排一次版，再去掉 class 才有过渡
        requestAnimationFrame(function () {
          neu.classList.remove("nm-flat");
        });
        riseTimer = setTimeout(function () { neu.classList.remove("nm-flat"); }, RISE_MS);
      }, SINK_MS);
    };
    wrapped._nm = true;
    App.showPage = wrapped;
  }

  function init() { wrapShowPage(); sync(window.WWYThemes && window.WWYThemes.current()); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
