/* rhine-sfx.js — 「莱茵 · 分析终端」界面音效（Web Audio 现场合成，不带音频文件，不放音乐）
 *
 * 声音设计：像敲在亚克力 / 玻璃上的轻响——正弦为主、带一点点非谐泛音的钟片，
 * 经过一个现场生成的小房间混响；气流类声音用很柔的低通噪声、慢起慢收。
 * 不用方波 / 锯齿波，总线上再压一道 8kHz 低通，不会刺耳。
 *
 *  tick(i)  悬停档案板：五声音阶上的一颗极轻的钟片音（i = 第几块板，划过去像一串风铃）
 *  press    按下：短促圆润的「嗒」
 *  type     开屏逐字输入：很轻的高音点
 *  pull     抽出档案：一口柔和的气流 + 两音钟片
 *  rise     档案升起：低频缓缓上扬的「嗡」
 *  open / close  面板展开 / 收起：一阵很淡的气流
 *  scan     权限扫描：一道柔和的上行滑音
 *  confirm  授权通过：两声清亮的钟片
 *  welcome  欢迎页：一个温暖的长和弦铺底（只有一个和弦，不是音乐）
 *  开关存在 localStorage（rhine-sfx = "0" 关）；AudioContext 第一次用到时才建，
 *  被浏览器挂起时等第一次点击再恢复。
 */
(function () {
  "use strict";
  var KEY = "rhine-sfx";
  var ctx = null, bus = null, wet = null, noiseBuf = null, lastAt = {};
  // 五声音阶（D 大调五声：D E F# A B），两个八度
  var PENTA = [1174.7, 1318.5, 1480, 1760, 1975.5, 2349.3, 2637, 2960, 3520, 3951];

  function enabled() { try { return localStorage.getItem(KEY) !== "0"; } catch (e) { return true; } }
  function setEnabled(v) { try { localStorage.setItem(KEY, v ? "1" : "0"); } catch (e) {} }

  function ac() {
    if (ctx) { if (ctx.state === "suspended") ctx.resume(); return ctx; }
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    ctx = new AC();
    var master = ctx.createGain(); master.gain.value = .55;
    var lp = ctx.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 8000; lp.Q.value = .5;
    var comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -20; comp.knee.value = 18; comp.ratio.value = 3; comp.attack.value = .004; comp.release.value = .2;
    master.connect(lp); lp.connect(comp); comp.connect(ctx.destination);
    // 小房间混响：指数衰减的噪声脉冲
    var conv = ctx.createConvolver(), len = Math.round(ctx.sampleRate * 1.6);
    var ir = ctx.createBuffer(2, len, ctx.sampleRate);
    for (var ch = 0; ch < 2; ch++) {
      var d = ir.getChannelData(ch);
      for (var i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 3.2);
    }
    conv.buffer = ir;
    wet = ctx.createGain(); wet.gain.value = .32;
    var wetLp = ctx.createBiquadFilter(); wetLp.type = "lowpass"; wetLp.frequency.value = 4200;
    wet.connect(conv); conv.connect(wetLp); wetLp.connect(master);
    bus = ctx.createGain(); bus.gain.value = 1;
    bus.connect(master); bus.connect(wet);
    var nl = ctx.sampleRate * 2;
    noiseBuf = ctx.createBuffer(1, nl, ctx.sampleRate);
    var nd = noiseBuf.getChannelData(0), b0 = 0;
    for (var k = 0; k < nl; k++) { b0 = .985 * b0 + .015 * (Math.random() * 2 - 1); nd[k] = b0 * 6; }   // 偏粉的柔和噪声
    return ctx;
  }
  ["pointerdown", "keydown"].forEach(function (ev) {
    window.addEventListener(ev, function () { if (ctx && ctx.state === "suspended") ctx.resume(); }, true);
  });

  // 一颗正弦音：缓起（不咔哒）+ 指数衰减
  function sine(f, t, peak, attack, dec, glideTo, type) {
    var o = ctx.createOscillator(), g = ctx.createGain();
    o.type = type || "sine";
    o.frequency.setValueAtTime(f, t);
    if (glideTo) o.frequency.exponentialRampToValueAtTime(glideTo, t + attack + dec * .8);
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(peak, t + attack);
    g.gain.exponentialRampToValueAtTime(.0001, t + attack + dec);
    o.connect(g); g.connect(bus);
    o.start(t); o.stop(t + attack + dec + .05);
  }
  // 钟片：基音 + 一点点非谐泛音
  function bell(f, t, peak, dec) {
    sine(f, t, peak, .004, dec);
    sine(f * 2.756, t, peak * .16, .003, dec * .35);
    sine(f * 5.404, t, peak * .05, .002, dec * .18);
  }
  function air(t, dur, f0, f1, peak, attack) {
    var s = ctx.createBufferSource(), f = ctx.createBiquadFilter(), g = ctx.createGain();
    s.buffer = noiseBuf; f.type = "bandpass"; f.Q.value = .6;
    f.frequency.setValueAtTime(f0, t); f.frequency.exponentialRampToValueAtTime(f1, t + dur);
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(peak, t + (attack || dur * .35));
    g.gain.exponentialRampToValueAtTime(.0001, t + dur);
    s.connect(f); f.connect(g); g.connect(bus);
    s.start(t, Math.random()); s.stop(t + dur + .05);
  }

  var S = {
    tick: function (t, i) {
      var f = PENTA[((i | 0) % PENTA.length + PENTA.length) % PENTA.length];
      bell(f, t, .035, .55);
    },
    press: function (t) { sine(880, t, .07, .003, .09, 700); sine(1760, t, .015, .002, .05); },
    type: function (t) { sine(2600 + Math.random() * 500, t, .018, .002, .035); },
    pull: function (t) {
      air(t, .55, 500, 2200, .05, .16);
      bell(1318.5, t + .12, .045, .9);
      bell(1975.5, t + .2, .035, 1.1);
    },
    rise: function (t) { sine(98, t, .09, .18, .7, 147); sine(196, t, .025, .2, .6, 294); },
    open: function (t) { air(t, .5, 380, 1500, .035, .2); },
    close: function (t) { air(t, .45, 1400, 360, .03, .12); },
    scan: function (t) { sine(587, t, .035, .25, 1.0, 1175); sine(880, t + .05, .015, .25, .9, 1760); },
    confirm: function (t) { bell(1760, t, .06, 1.1); bell(2637, t + .12, .05, 1.4); },
    welcome: function (t) {
      [220, 277.2, 329.6, 440].forEach(function (f, k) { sine(f, t + k * .04, .028, .5, 2.4, null, "triangle"); });
      sine(110, t, .05, .4, 2.2);
      bell(1760, t + .35, .03, 1.6);
    },
  };
  var GAP = { tick: 35, type: 22, press: 70, pull: 250, rise: 300, open: 350, close: 350 };

  window.RhineSfx = {
    play: function (name, delay, arg) {
      if (!enabled() || !S[name]) return;
      if (!gate(name, GAP[name] || 90)) return;
      if (!ac()) return;
      try { S[name](ctx.currentTime + .01 + (delay || 0) / 1000, arg); } catch (e) {}
    },
    enabled: enabled,
    toggle: function () { var v = !enabled(); setEnabled(v); if (v) { ac(); this.play("confirm"); } return v; },
  };
  function gate(name, ms) {
    var now = performance.now();
    if (lastAt[name] && now - lastAt[name] < ms) return false;
    lastAt[name] = now; return true;
  }
})();
