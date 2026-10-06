/* bauform-xray.js — 「构型 · 机能包豪斯」风格的 X 光爆炸装配图生成器（开屏和主界面背景共用）
 *
 * 参考 PV 里那种「机械零件的 X 光透视 + 从中心径向炸开」的画面，这里完全程序化生成，
 * 不用任何图片素材：
 *   · 中心是一个轮毂（同心环 + 刻度 + 辐条）
 *   · 若干条「装配臂」沿径向排开，每条臂由几段零件串起来：
 *       圆柱（两侧更亮 = X 光里边缘更厚）、环组、弹簧、法兰（带孔）、桁架、锥段、细杆
 *   · 全部用 "lighter" 叠加绘制：零件重叠处更亮，就是 X 光片的质感
 * 性能：每条臂先画进一张离屏小图（sprite），动画时只做 drawImage + 旋转平移，
 *       开屏 60fps 也只有十几次 drawImage；主界面背景只画一次静态图。
 *
 * 接口：
 *   BPXray.model(seed)                         生成一套装配（同一个种子结果相同）
 *   BPXray.draw(ctx, model, W, H, opt)         画一帧；opt.p = 炸开进度 0..1，opt.rot 整体旋转（弧度），
 *                                              opt.cx/cy 中心（像素），opt.scale 缩放，opt.blur 动态拖影强度
 */
(function () {
  "use strict";

  var CREAM = "236,229,211";
  var SLATE = "165,172,177";   // 构造线：中性灰

  function rng(seed) {
    var s = seed >>> 0 || 1;
    return function () {
      s ^= s << 13; s >>>= 0; s ^= s >> 17; s ^= s << 5; s >>>= 0;
      return s / 4294967296;
    };
  }

  /* ---------------- 零件绘制（局部坐标：轴向 = +x，横向 = ±y） ---------------- */
  function xrayFill(c, x, y, w, h, a) {
    var g = c.createLinearGradient(0, y, 0, y + h);
    g.addColorStop(0, "rgba(" + CREAM + "," + (a * 1.0) + ")");
    g.addColorStop(0.18, "rgba(" + CREAM + "," + (a * 0.35) + ")");
    g.addColorStop(0.5, "rgba(" + CREAM + "," + (a * 0.12) + ")");
    g.addColorStop(0.82, "rgba(" + CREAM + "," + (a * 0.35) + ")");
    g.addColorStop(1, "rgba(" + CREAM + "," + (a * 1.0) + ")");
    c.fillStyle = g;
    c.fillRect(x, y, w, h);
  }
  function line(c, x0, y0, x1, y1) { c.beginPath(); c.moveTo(x0, y0); c.lineTo(x1, y1); c.stroke(); }

  var PARTS = {
    cyl: function (c, p, R) {
      xrayFill(c, 0, -p.h, p.l, p.h * 2, 0.62);
      c.strokeStyle = "rgba(" + CREAM + ",.75)"; c.lineWidth = 1.2;
      c.strokeRect(0, -p.h, p.l, p.h * 2);
      c.strokeStyle = "rgba(" + CREAM + ",.35)"; c.lineWidth = 0.8;
      line(c, 0, -p.h * 0.35, p.l, -p.h * 0.35); line(c, 0, p.h * 0.35, p.l, p.h * 0.35);
      var n = 1 + Math.floor(R() * 4);
      for (var i = 1; i <= n; i++) { var x = p.l * i / (n + 1); line(c, x, -p.h, x, p.h); }
      // 端部倒角
      c.strokeStyle = "rgba(" + CREAM + ",.55)";
      line(c, 0, -p.h * .7, p.h * .3, -p.h); line(c, 0, p.h * .7, p.h * .3, p.h);
    },
    rings: function (c, p, R) {
      var n = 4 + Math.floor(R() * 7), s = p.l / n;
      c.lineWidth = 1.1;
      for (var i = 0; i < n; i++) {
        var x = s * (i + .5);
        c.strokeStyle = "rgba(" + CREAM + "," + (0.35 + R() * 0.45) + ")";
        c.beginPath(); c.ellipse(x, 0, Math.max(2, s * .32), p.h, 0, 0, Math.PI * 2); c.stroke();
        c.fillStyle = "rgba(" + CREAM + ",.06)"; c.fill();
      }
      c.strokeStyle = "rgba(" + CREAM + ",.3)"; c.lineWidth = .8;
      line(c, 0, -p.h * .25, p.l, -p.h * .25); line(c, 0, p.h * .25, p.l, p.h * .25);
    },
    spring: function (c, p, R) {
      var turns = 6 + Math.floor(R() * 10);
      c.strokeStyle = "rgba(" + CREAM + ",.7)"; c.lineWidth = 1.3;
      c.beginPath();
      for (var i = 0; i <= turns * 16; i++) {
        var t = i / (turns * 16), x = t * p.l, y = Math.sin(t * turns * Math.PI * 2) * p.h;
        if (i) c.lineTo(x, y); else c.moveTo(x, y);
      }
      c.stroke();
      c.strokeStyle = "rgba(" + CREAM + ",.25)"; c.lineWidth = 1;
      c.beginPath();
      for (var j = 0; j <= turns * 16; j++) {
        var u = j / (turns * 16), xx = u * p.l + p.l / turns / 2, yy = Math.sin(u * turns * Math.PI * 2 + Math.PI) * p.h * .82;
        if (xx > p.l) break;
        if (j) c.lineTo(xx, yy); else c.moveTo(xx, yy);
      }
      c.stroke();
      xrayFill(c, 0, -p.h * .3, p.l, p.h * .6, .25);
    },
    flange: function (c, p, R) {
      xrayFill(c, 0, -p.h, p.l, p.h * 2, .7);
      c.strokeStyle = "rgba(" + CREAM + ",.85)"; c.lineWidth = 1.4; c.strokeRect(0, -p.h, p.l, p.h * 2);
      var holes = 3 + Math.floor(R() * 4);
      c.strokeStyle = "rgba(" + CREAM + ",.6)"; c.lineWidth = 1;
      for (var i = 0; i < holes; i++) {
        var y = -p.h + p.h * 2 * (i + .5) / holes;
        c.beginPath(); c.ellipse(p.l / 2, y, p.l * .22, Math.min(p.h / holes * .5, p.l * .3), 0, 0, Math.PI * 2); c.stroke();
      }
    },
    truss: function (c, p, R) {
      var n = 6 + Math.floor(R() * 10), s = p.l / n;
      c.strokeStyle = "rgba(" + CREAM + ",.75)"; c.lineWidth = 1.3;
      line(c, 0, -p.h, p.l, -p.h); line(c, 0, p.h, p.l, p.h);
      c.strokeStyle = "rgba(" + CREAM + ",.45)"; c.lineWidth = 1;
      c.beginPath();
      for (var i = 0; i <= n; i++) { c.moveTo(i * s, -p.h); c.lineTo(i * s, p.h); if (i < n) { c.moveTo(i * s, -p.h); c.lineTo((i + 1) * s, p.h); } }
      c.stroke();
      xrayFill(c, 0, -p.h, p.l, p.h * 2, .18);
    },
    cone: function (c, p) {
      var h2 = p.h * .45;
      c.beginPath(); c.moveTo(0, -p.h); c.lineTo(p.l, -h2); c.lineTo(p.l, h2); c.lineTo(0, p.h); c.closePath();
      c.save(); c.clip(); xrayFill(c, 0, -p.h, p.l, p.h * 2, .55); c.restore();
      c.strokeStyle = "rgba(" + CREAM + ",.75)"; c.lineWidth = 1.2; c.stroke();
      c.strokeStyle = "rgba(" + CREAM + ",.3)"; line(c, 0, 0, p.l, 0);
    },
    rod: function (c, p) {
      var h = Math.max(2, p.h * .22);
      xrayFill(c, 0, -h, p.l, h * 2, .6);
      c.strokeStyle = "rgba(" + CREAM + ",.7)"; c.lineWidth = 1; c.strokeRect(0, -h, p.l, h * 2);
      c.strokeStyle = "rgba(" + CREAM + ",.35)";
      for (var x = 8; x < p.l; x += 14) line(c, x, -h * 2.2, x, -h);
    },
    bolt: function (c, p) {
      xrayFill(c, 0, -p.h, p.l * .35, p.h * 2, .8);
      c.strokeStyle = "rgba(" + CREAM + ",.8)"; c.lineWidth = 1.2; c.strokeRect(0, -p.h, p.l * .35, p.h * 2);
      var hh = p.h * .45; c.beginPath();
      for (var x = p.l * .35; x < p.l; x += 5) { c.moveTo(x, -hh); c.lineTo(x + 2.5, hh); }
      c.strokeStyle = "rgba(" + CREAM + ",.5)"; c.lineWidth = .9; c.stroke();
      c.strokeRect(p.l * .35, -hh, p.l * .65, hh * 2);
    },
  };
  var KINDS = ["cyl", "cyl", "rings", "spring", "flange", "truss", "cone", "rod", "bolt", "rings", "cyl"];

  /* ---------------- 模型 ---------------- */
  function model(seed) {
    var R = rng(seed || 7);
    var arms = [], n = 10 + Math.floor(R() * 3);
    var base = R() * Math.PI * 2;
    for (var i = 0; i < n; i++) {
      var ang = base + (i / n) * Math.PI * 2 + (R() - .5) * .35;
      var parts = [], len = 0, h0 = 22 + R() * 34, k = 3 + Math.floor(R() * 4);
      for (var j = 0; j < k; j++) {
        var kind = KINDS[Math.floor(R() * KINDS.length)];
        var h = Math.max(8, h0 * (.45 + R() * .9));
        var l = kind === "flange" ? 10 + R() * 18 : kind === "bolt" ? 50 + R() * 70 : 60 + R() * 210;
        parts.push({ kind: kind, l: l, h: h, gap: 6 + R() * 26, seed: Math.floor(R() * 1e9) });
        len += l + parts[parts.length - 1].gap;
      }
      var arm = { ang: ang, off: 0, parts: parts, len: len, hmax: h0 * 1.4, r0: 120 + R() * 60, r1: 260 + R() * 360, spin: (R() - .5) * .2, sprite: null };
      arms.push(arm);
      // 一半的臂旁边并排一条细一点的副臂（PV 里零件常常成组平行排列）
      if (R() < .55) {
        var tw = [], tl = 0, th = h0 * (.35 + R() * .3), tk = 2 + Math.floor(R() * 3);
        for (var q = 0; q < tk; q++) {
          var kd = KINDS[Math.floor(R() * KINDS.length)];
          var ll = kd === "flange" ? 8 + R() * 12 : 50 + R() * 160;
          tw.push({ kind: kd, l: ll, h: Math.max(6, th * (.6 + R() * .6)), gap: 8 + R() * 30, seed: Math.floor(R() * 1e9) });
          tl += ll + tw[tw.length - 1].gap;
        }
        arms.push({ ang: ang, off: (R() < .5 ? -1 : 1) * (h0 * 1.4 + th * 1.4 + 6), parts: tw, len: tl, hmax: th * 1.3,
          r0: arm.r0 + 40, r1: arm.r1 + 60 + R() * 140, spin: arm.spin * 1.6, sprite: null });
      }
    }
    return { arms: arms, hub: { r: 110, seed: Math.floor(R() * 1e9) }, seed: seed, lines: makeLines(R) };
  }
  function makeLines(R) {
    var out = [];
    for (var i = 0; i < 26; i++) out.push({ a: R() * Math.PI * 2, w: R() < .2 ? 1.4 : .7, al: .08 + R() * .2 });
    return out;
  }

  // 辉光：把模糊过的自己用 lighter 叠回去一层（X 光片高亮处的光晕）
  function bloom(cv) {
    try {
      var tmp = document.createElement("canvas"); tmp.width = cv.width; tmp.height = cv.height;
      var t = tmp.getContext("2d"); t.filter = "blur(" + Math.round(cv.width > 0 ? 5 * (window.devicePixelRatio || 1) : 5) + "px)";
      t.drawImage(cv, 0, 0);
      var c = cv.getContext("2d"); c.save(); c.setTransform(1, 0, 0, 1, 0, 0);
      c.globalCompositeOperation = "lighter"; c.globalAlpha = .75; c.drawImage(tmp, 0, 0); c.restore();
    } catch (e) { /* 不支持 filter 就没有辉光 */ }
  }

  function armSprite(arm) {
    if (arm.sprite) return arm.sprite;
    var pad = 6, H = Math.ceil(arm.hmax * 2.2 + pad * 2), W = Math.ceil(arm.len + pad * 2);
    var cv = document.createElement("canvas");
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = Math.ceil(W * dpr); cv.height = Math.ceil(H * dpr);
    var c = cv.getContext("2d");
    c.scale(dpr, dpr);
    c.globalCompositeOperation = "lighter";
    c.translate(pad, H / 2);
    // 轴心线
    c.strokeStyle = "rgba(" + SLATE + ",.35)"; c.lineWidth = .8; c.setLineDash([10, 4, 2, 4]);
    line(c, 0, 0, arm.len, 0); c.setLineDash([]);
    var x = 0;
    for (var i = 0; i < arm.parts.length; i++) {
      var p = arm.parts[i];
      c.save(); c.translate(x, 0); PARTS[p.kind](c, p, rng(p.seed)); c.restore();
      x += p.l + p.gap;
    }
    bloom(cv);
    arm.sprite = { cv: cv, w: W, h: H, pad: pad };
    return arm.sprite;
  }

  function hubSprite(m) {
    if (m.hub.sprite) return m.hub.sprite;
    var r = m.hub.r, S = Math.ceil(r * 2 + 20), cv = document.createElement("canvas");
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.width = cv.height = Math.ceil(S * dpr);
    var c = cv.getContext("2d"); c.scale(dpr, dpr); c.translate(S / 2, S / 2);
    c.globalCompositeOperation = "lighter";
    var R = rng(m.hub.seed);
    var g = c.createRadialGradient(0, 0, 0, 0, 0, r);
    g.addColorStop(0, "rgba(" + CREAM + ",.9)"); g.addColorStop(.18, "rgba(" + CREAM + ",.35)");
    g.addColorStop(.5, "rgba(" + CREAM + ",.08)"); g.addColorStop(1, "rgba(" + CREAM + ",0)");
    c.fillStyle = g; c.beginPath(); c.arc(0, 0, r, 0, Math.PI * 2); c.fill();
    [r * .98, r * .74, r * .5, r * .3, r * .16].forEach(function (rr, i) {
      c.strokeStyle = "rgba(" + CREAM + "," + (i === 1 ? .7 : .4) + ")"; c.lineWidth = i === 1 ? 1.6 : 1;
      c.beginPath(); c.arc(0, 0, rr, 0, Math.PI * 2); c.stroke();
    });
    c.strokeStyle = "rgba(" + CREAM + ",.55)"; c.lineWidth = 1;
    for (var i = 0; i < 48; i++) {
      var a = i / 48 * Math.PI * 2, l = i % 4 ? .06 : .12;
      line(c, Math.cos(a) * r * .74, Math.sin(a) * r * .74, Math.cos(a) * r * (.74 + l), Math.sin(a) * r * (.74 + l));
    }
    var spokes = 6 + Math.floor(R() * 6);
    c.strokeStyle = "rgba(" + CREAM + ",.45)";
    for (var k = 0; k < spokes; k++) {
      var b = k / spokes * Math.PI * 2;
      c.save(); c.rotate(b); c.strokeRect(r * .18, -4, r * .3, 8); c.restore();
    }
    bloom(cv);
    m.hub.sprite = { cv: cv, s: S };
    return m.hub.sprite;
  }

  function ease(t) { return 1 - Math.pow(1 - t, 4); }

  /* ---------------- 画一帧 ---------------- */
  function draw(ctx, m, W, H, opt) {
    opt = opt || {};
    var p = opt.p == null ? 1 : opt.p, rot = opt.rot || 0;
    var cx = opt.cx == null ? W / 2 : opt.cx, cy = opt.cy == null ? H / 2 : opt.cy;
    var sc = opt.scale || Math.min(W, H) / 1100;
    var blur = opt.blur || 0, alpha = opt.alpha == null ? 1 : opt.alpha;
    ctx.save();
    ctx.globalCompositeOperation = "lighter";
    ctx.translate(cx, cy); ctx.rotate(rot); ctx.scale(sc, sc);
    // 构造线：从中心射向画面外
    var reach = Math.hypot(W, H) / sc;
    var lp = Math.min(1, p * 1.6);
    for (var i = 0; i < m.lines.length; i++) {
      var L = m.lines[i];
      ctx.strokeStyle = "rgba(" + SLATE + "," + (L.al * alpha) + ")"; ctx.lineWidth = L.w / sc;
      line(ctx, 0, 0, Math.cos(L.a) * reach * lp, Math.sin(L.a) * reach * lp);
    }
    ctx.strokeStyle = "rgba(" + SLATE + "," + (.28 * alpha) + ")"; ctx.lineWidth = 1 / sc;
    ctx.beginPath(); ctx.arc(0, 0, 330, 0, Math.PI * 2 * Math.min(1, p * 1.3)); ctx.stroke();
    ctx.beginPath(); ctx.arc(0, 0, 560, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.min(1, p * 1.1)); ctx.stroke();

    var ep = ease(Math.min(1, p));
    for (var a = 0; a < m.arms.length; a++) {
      var arm = m.arms[a], sp = armSprite(arm);
      var r = arm.r0 + (arm.r1 - arm.r0) * ep;
      ctx.save();
      ctx.rotate(arm.ang + arm.spin * (1 - ep));
      ctx.globalAlpha = alpha * Math.min(1, p * 3);
      ctx.drawImage(sp.cv, r - sp.pad, arm.off - sp.h / 2, sp.w, sp.h);
      if (blur > 0) {   // 动态拖影：沿轴向往回拖几张淡的副本
        for (var k = 1; k <= 3; k++) {
          ctx.globalAlpha = alpha * blur * (0.32 / k);
          ctx.drawImage(sp.cv, r - sp.pad - k * 46 * blur, arm.off - sp.h / 2, sp.w + k * 30 * blur, sp.h);
        }
      }
      ctx.restore();
    }
    var hs = hubSprite(m);
    ctx.globalAlpha = alpha;
    ctx.rotate(-rot * 2);
    ctx.drawImage(hs.cv, -hs.s / 2, -hs.s / 2, hs.s, hs.s);
    ctx.restore();
  }

  window.BPXray = { model: model, draw: draw };
})();
