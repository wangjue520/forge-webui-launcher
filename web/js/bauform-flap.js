/* bauform-flap.js — 「构型」风格的机械翻牌数字（老式机场 / 车站时刻表那种 split-flap）
 *
 * 每个字符是一块翻牌：上下两半，中间一道铰链缝。数字变化时不是直接跳到新值，
 * 而是像真的翻牌机一样按 0→1→2…→9→0 的顺序一片一片翻过去，直到停在目标数字
 * （01 → 06 会依次翻过 2 3 4 5；59 → 00 秒的十位会翻 6 7 8 9 0）。
 * 每块牌各自追自己的目标，所以目标在翻动途中又变了也没关系（开屏的百分比计数就是这样一直追着跑）。
 * 非数字字符（: . # 等）不翻，直接换，做成窄牌或无底板的分隔符。
 *
 * 用法：
 *   var f = BFFlap.create(容器元素, { speed: 70, cls: "额外 class" });
 *   f.set("06");            // 翻到 06
 *   f.set("15:04:09");      // 长度变了会重建牌位（新牌从 0 开始翻）
 *   f.jump("00");           // 不翻，直接显示
 * 一次翻页 = 上半片向下倒（旧字）+ 下半片落下（新字），时长 = speed；样式在 themes/bauform-base.css
 */
(function () {
  "use strict";
  var DIG = "0123456789";
  var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;

  function isDigit(c) { return DIG.indexOf(c) >= 0 && c.length === 1; }
  // 非数字字符的牌宽：字母 / 符号宽一点，标点窄一点
  function sepCls(c) {
    if (isDigit(c)) return "";
    if (/[A-Za-z%°@&#]/.test(c)) return " bfl-sep bfl-wide";
    if (c === " ") return " bfl-sep bfl-sp";
    return " bfl-sep";
  }

  function Cell(parent, ch, speed) {
    var n = document.createElement("span");
    n.className = "bfl-cell" + sepCls(ch);
    n.innerHTML =
      '<span class="bfl-h bfl-top"><i></i></span><span class="bfl-h bfl-bot"><i></i></span>' +
      '<span class="bfl-h bfl-ftop"><i></i></span><span class="bfl-h bfl-fbot"><i></i></span>';
    parent.appendChild(n);
    var q = n.querySelectorAll("i");
    this.el = n; this.top = q[0]; this.bot = q[1]; this.ftop = q[2]; this.fbot = q[3];
    this.cur = ch; this.target = ch; this.speed = speed; this.timer = 0;
    this.paint(ch, ch);
  }
  Cell.prototype.paint = function (oldC, newC) {
    this.top.textContent = newC;     // 上半：新字（被倒下的旧片挡着）
    this.bot.textContent = oldC;     // 下半：旧字（等新片落下盖住）
    this.ftop.textContent = oldC;    // 倒下的上半片：旧字
    this.fbot.textContent = newC;    // 落下的下半片：新字
  };
  Cell.prototype.setTarget = function (ch) {
    this.target = ch;
    if (!isDigit(ch) || !isDigit(this.cur) || reduce) { this.show(ch); return; }
    if (!this.timer) this.step();
  };
  Cell.prototype.show = function (ch) {
    clearTimeout(this.timer); this.timer = 0;
    this.cur = ch; this.target = ch;
    this.el.classList.remove("go");
    this.el.className = "bfl-cell" + sepCls(ch);
    this.paint(ch, ch);
  };
  Cell.prototype.step = function () {
    var self = this;
    if (this.cur === this.target) { this.timer = 0; this.el.classList.remove("go"); return; }
    var next = DIG[(DIG.indexOf(this.cur) + 1) % 10];
    // 还要翻好几片时中间几片快一点，最后两片放慢（真翻牌机停下前的节奏）
    var rem = (DIG.indexOf(this.target) - DIG.indexOf(this.cur) + 10) % 10;
    var dur = rem > 2 ? Math.max(60, Math.round(this.speed * .55)) : this.speed;
    this.paint(this.cur, next);
    this.el.style.setProperty("--bfl-t", dur + "ms");
    this.el.classList.remove("go"); void this.el.offsetWidth; this.el.classList.add("go");
    this.cur = next;
    this.timer = setTimeout(function () {
      self.bot.textContent = next;           // 落定：下半换成新字
      self.step();
    }, dur);
  };

  function create(host, opt) {
    opt = opt || {};
    var speed = opt.speed || 70;
    host.classList.add("bfl");
    if (opt.cls) host.className += " " + opt.cls;
    var cells = [], text = "";
    function build(t, from) {
      cells.forEach(function (c) { clearTimeout(c.timer); });
      host.innerHTML = ""; cells = [];
      for (var i = 0; i < t.length; i++) {
        var start = from && isDigit(t[i]) ? "0" : t[i];
        cells.push(new Cell(host, start, speed));
      }
      host.setAttribute("aria-label", t);
    }
    function pattern(t) { return t.replace(/\d/g, "0"); }
    var api = {
      set: function (t) {
        t = String(t);
        if (t.length !== cells.length || pattern(t) !== pattern(text)) build(t, true);
        text = t;
        host.setAttribute("aria-label", t);
        for (var i = 0; i < t.length; i++) cells[i].setTarget(t[i]);
      },
      jump: function (t) {
        t = String(t); text = t;
        if (t.length !== cells.length) build(t, false);
        for (var i = 0; i < t.length; i++) cells[i].show(t[i]);
        host.setAttribute("aria-label", t);
      },
      // 原地转一整圈再停回原数字（悬停反馈）
      rattle: function (ms) {
        if (reduce) return;
        cells.forEach(function (c) {
          if (!isDigit(c.target) || c.timer) return;
          if (ms) c.speed = ms;
          c.cur = DIG[(DIG.indexOf(c.target) + 1) % 10];
          c.step();
        });
        setTimeout(function () { cells.forEach(function (c) { c.speed = speed; }); }, 900);
      },
      speed: function (ms) { speed = ms; cells.forEach(function (c) { c.speed = ms; }); },
      destroy: function () { cells.forEach(function (c) { clearTimeout(c.timer); }); host.innerHTML = ""; host.classList.remove("bfl"); },
    };
    if (opt.text != null) api.jump(opt.text);
    return api;
  }

  window.BFFlap = { create: create };
})();
