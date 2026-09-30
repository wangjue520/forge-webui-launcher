/* 主界面装饰：背景等高线 + 页头时钟（纯视觉，不碰业务逻辑） */
(function () {
  var cv = document.getElementById("bg-topo");
  var timer = 0;
  function redraw() {
    if (!cv || !window.Topo) return;
    try { window.Topo.draw(cv, { alpha: 0.9 }); } catch (e) { console.error(e); }
  }
  redraw();
  window.addEventListener("resize", function () {
    clearTimeout(timer);
    timer = setTimeout(redraw, 180);
  });

  var clock = document.getElementById("topbar-clock");
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    if (!clock) return;
    var d = new Date();
    clock.textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  tick();
  setInterval(tick, 1000);
})();
