/* themes.js — 界面风格注册表（必须在 splash.js 之前加载：开屏动画也读它）
 *
 * 以后新增一套风格的步骤：
 *   1. 资源放进 web/，比如 web/themes/xxx.css；新开屏动画在 js/splash.js
 *      里加一个变体实现（见 splash.js 里的 VARIANTS 注释）
 *   2. 在下面 THEMES 里加一条：
 *        { id: "xxx", label: "显示名", css: "themes/xxx.css", splash: "xxx" }
 *      css 留空 = 界面沿用默认样式、只换开屏；splash: "none" = 跳过开屏直接进界面；
 *      css 也可以是数组（按顺序叠加）；base: "minimal" = 复用极简那套结构规则
 *      切换风格时会派发 window 事件 "wwy-theme"（detail 为该风格条目）
 *   3. 界面里要覆盖的样式写 [data-ui-theme="xxx"] 选择器（style.css 是基底）
 *
 * 选择存在 launcher_config.json 的 ui_theme（全局设置，不属于任何实例），
 * 保存/启动时由后端生成 js/current_theme.js——开屏动画在后端应答之前、
 * 且 pywebview 每次启动端口都变（localStorage 跨启动拿不到），只能走文件。
 */
(function () {
  "use strict";
  var THEMES = [
    { id: "terminal", label: "终末地 · 工业终端（默认）", css: "", splash: "terminal" },
    // 构型 · BAUFORM（机能包豪斯，深色）：结构沿用默认（style.css），themes/bauform.css 全面改皮，
    // 动效 / 背景由 js/bauform.js 负责，数字翻牌是 js/bauform-flap.js，
    // 西文装饰字体是自制的 WWY Bauform（tools/build_bauform_font.py）
    { id: "bauform", label: "构型 BAUFORM（深色）", css: "themes/bauform.css", splash: "bauform" },
    // 「极简」本身已不再单独提供（被新拟物取代），themes/minimal.css 只作为下面几套的结构基底
    // 液态玻璃：结构沿用极简（base），再叠一层玻璃材质 + 流动背景（js/liquid.js）
    { id: "liquid-light", label: "液态玻璃 · 浅色", css: ["themes/minimal.css", "themes/liquid.css"], base: "minimal", splash: "liquid" },
    { id: "liquid-dark", label: "液态玻璃 · 深色", css: ["themes/minimal.css", "themes/liquid.css"], base: "minimal", splash: "liquid" },
    // 新拟物（Neumorphism）：结构沿用极简（base），themes/neumorph.css 换成同色凸起 / 凹陷的软阴影材质
    { id: "neu-light", label: "新拟物 · 浅色（无开屏动画）", css: ["themes/minimal.css", "themes/neumorph.css"], base: "minimal", splash: "none" },
    { id: "neu-dark", label: "新拟物 · 深色（无开屏动画）", css: ["themes/minimal.css", "themes/neumorph.css"], base: "minimal", splash: "none" },
    // 矢量突破：结构沿用终末地（不设 base），换成浅灰 + 青色 + 黑；背景装饰见 js/vector.js
    // （正常维护的风格，新功能同样要适配它）
    { id: "vector", label: "矢量突破 · 青色拟生态", css: "themes/vector.css", splash: "vector" },
    // 莱茵 · 分析终端：结构沿用终末地（不设 base），暖灰研究纸 + 墨色 + 黄铜；三维档案库背景见 js/rhine-field.js，
    // 界面编排 js/rhine.js，开屏 js/rhine-splash.js，音效 js/rhine-sfx.js
    { id: "rhine", label: "莱茵 · 分析终端（研究档案）", css: "themes/rhine.css", splash: "rhine" },
    // 离解 · 黑白构成：结构沿用终末地（不设 base），白纸细网格 + 黑白大渐变 + 荧光黄绿点缀；
    // 背景装饰和动效见 js/dissoc.js，开屏 js/dissoc-splash.js（样式在 splash.css 末尾）
    { id: "dissoc", label: "离解 · 黑白构成", css: "themes/dissoc.css", splash: "dissoc" },
  ];
  var KEY = "ui-theme";
  // 旧 id 的去处：极简（含更早只叫 "minimal" 的版本）已由新拟物取代；blueprint = 构型的内测名
  var ALIAS = { minimal: "neu-light", "minimal-light": "neu-light", "minimal-dark": "neu-dark", blueprint: "bauform" };

  function find(id) {
    id = ALIAS[id] || id;
    for (var i = 0; i < THEMES.length; i++) if (THEMES[i].id === id) return THEMES[i];
    return THEMES[0];
  }

  function currentId() {
    // 预览用：地址里带 ?theme=xxx 时优先（正式运行的地址不会带这个参数）
    var q = /[?&]theme=([\w-]+)/.exec(location.search);
    if (q) return q[1];
    // 优先读启动器生成的 js/current_theme.js（localStorage 按 origin 存，
    // pywebview 每次启动端口都变，跨启动拿不到，只当兜底）
    if (window.WWY_THEME_ID) return window.WWY_THEME_ID;
    try { return localStorage.getItem(KEY) || THEMES[0].id; } catch (e) { return THEMES[0].id; }
  }

  function apply(id, save) {
    var t = find(id);
    var root = document.documentElement;
    root.dataset.uiTheme = t.id;
    // base：结构共用的那一套（极简 / 液态玻璃都用 minimal 的结构规则，
    // 选择器写 [data-ui-base="minimal"]，配色各自出变量）
    if (t.base) root.dataset.uiBase = t.base; else delete root.dataset.uiBase;
    // css 可以是一个文件或按顺序叠加的多个文件
    var files = !t.css ? [] : (Array.isArray(t.css) ? t.css : [t.css]);
    var old = document.querySelectorAll("link[data-ui-theme-css]");
    for (var i = 0; i < Math.max(files.length, old.length); i++) {
      var link = old[i];
      if (i >= files.length) { if (link) link.remove(); continue; }
      if (!link) {
        link = document.createElement("link");
        link.rel = "stylesheet";
        link.setAttribute("data-ui-theme-css", "");
        document.head.appendChild(link);
      }
      if (link.getAttribute("href") !== files[i]) link.setAttribute("href", files[i]);
    }
    if (save) { try { localStorage.setItem(KEY, t.id); } catch (e) { /* 隐私模式等 */ } }
    try { window.dispatchEvent(new CustomEvent("wwy-theme", { detail: t })); } catch (e) {}
    return t;
  }

  window.WWYThemes = {
    list: function () { return THEMES.slice(); },
    current: function () { return find(currentId()); },
    find: find,   // 旧 id / 别名 → 实际的风格条目（设置页下拉框回填用）
    apply: apply,
  };

  // 立刻应用本地记住的风格：开屏动画比 core.js 先启动，等不到后端配置
  apply(currentId(), false);
})();
