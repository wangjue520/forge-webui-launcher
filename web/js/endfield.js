/* 终末地（默认风格 terminal）的界面动效：纯视觉，不碰业务逻辑；样式见 themes/endfield.css
 *
 *   页头右侧：日期写进 .topbar-clock 的 data-date（时间仍由 js/deco.js 每秒更新）
 *   换页：页名从左擦入（280ms，mech），英文编号乱码后落定（360ms）
 *   开屏撤场时 EFUI.intro()：侧栏导航、页头、当前页的卡片依次亮起（整段 < 1 秒）
 * 只在当前风格是 terminal 时生效；系统开了「减少动态效果」时什么也不做（CSS 里同样关掉）。
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  var GLYPHS = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789";

  function mine() { return root.dataset.uiTheme === "terminal"; }
  function pad(n) { return (n < 10 ? "0" : "") + n; }

  /* ---------- 页头日期 ---------- */
  var clockBox = document.querySelector(".topbar-clock");
  function stampDate() {
    if (!clockBox) return;
    var d = new Date();
    clockBox.setAttribute("data-date", d.getFullYear() + "." + pad(d.getMonth() + 1) + "." + pad(d.getDate()));
  }
  stampDate();
  setInterval(stampDate, 60000);

  /* ---------- 换页 ---------- */
  function scramble(node, text, dur) {
    var t1 = performance.now();
    node.__efText = text;
    (function tick(now) {
      if (node.__efText !== text) return;          // 又换页了，让新的那次接手
      var p = Math.min(1, (now - t1) / dur), out = "";
      for (var i = 0; i < text.length; i++) {
        var ch = text[i];
        out += ((i + 1) / text.length <= p || /[\s/]/.test(ch)) ? ch : GLYPHS[(Math.random() * GLYPHS.length) | 0];
      }
      if (node.textContent !== out) { node.__efSelf = true; node.textContent = out; }
      if (p < 1) requestAnimationFrame(tick);
    })(t1);
  }

  var title = document.getElementById("page-title");
  var code = document.getElementById("page-code");
  if (title && window.MutationObserver) {
    new MutationObserver(function () {
      if (!mine() || reduce) return;
      title.classList.remove("ef-swap");
      void title.offsetWidth;                       // 重启动画
      title.classList.add("ef-swap");
    }).observe(title, { childList: true, characterData: true, subtree: true });
  }
  if (code && window.MutationObserver) {
    new MutationObserver(function () {
      if (code.__efSelf) { code.__efSelf = false; return; }
      if (!mine() || reduce) return;
      scramble(code, code.textContent, 360);
    }).observe(code, { childList: true, characterData: true, subtree: true });
  }

  /* ---------- 开屏之后的入场 ---------- */
  var introTimer = 0;
  window.EFUI = {
    intro: function () {
      if (!mine() || reduce) return;
      clearTimeout(introTimer);
      root.classList.remove("ef-intro");
      void root.offsetWidth;
      root.classList.add("ef-intro");
      introTimer = setTimeout(function () { root.classList.remove("ef-intro"); }, 1200);
    },
  };
})();
