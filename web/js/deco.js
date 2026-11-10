/* 主界面装饰：页头时钟（纯视觉，不碰业务逻辑） */
(function () {
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
