/* themes.js — 界面风格注册表（必须在 splash.js 之前加载：开屏动画也读它）
 *
 * 以后新增一套风格的步骤：
 *   1. 资源放进 web/，比如 web/themes/xxx.css；新开屏动画在 js/splash.js
 *      里加一个变体实现（见 splash.js 里的 VARIANTS 注释）
 *   2. 在下面 THEMES 里加一条：
 *        { id: "xxx", label: "显示名", css: "themes/xxx.css", splash: "xxx" }
 *      css 留空 = 界面沿用默认样式、只换开屏；splash: "none" = 跳过开屏直接进界面
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
    { id: "minimal-light", label: "极简 · 浅色（无开屏动画）", css: "themes/minimal.css", splash: "none" },
    { id: "minimal-dark", label: "极简 · 深色（无开屏动画）", css: "themes/minimal.css", splash: "none" },
  ];
  var KEY = "ui-theme";
  // 旧版本只叫 "minimal"，自动归到浅色
  var ALIAS = { minimal: "minimal-light" };

  function find(id) {
    id = ALIAS[id] || id;
    for (var i = 0; i < THEMES.length; i++) if (THEMES[i].id === id) return THEMES[i];
    return THEMES[0];
  }

  function currentId() {
    // 优先读启动器生成的 js/current_theme.js（localStorage 按 origin 存，
    // pywebview 每次启动端口都变，跨启动拿不到，只当兜底）
    if (window.WWY_THEME_ID) return window.WWY_THEME_ID;
    try { return localStorage.getItem(KEY) || THEMES[0].id; } catch (e) { return THEMES[0].id; }
  }

  function apply(id, save) {
    var t = find(id);
    document.documentElement.dataset.uiTheme = t.id;
    var link = document.getElementById("ui-theme-css");
    if (t.css) {
      if (!link) {
        link = document.createElement("link");
        link.id = "ui-theme-css";
        link.rel = "stylesheet";
        document.head.appendChild(link);
      }
      link.href = t.css;
    } else if (link) {
      link.remove();
    }
    if (save) { try { localStorage.setItem(KEY, t.id); } catch (e) { /* 隐私模式等 */ } }
    return t;
  }

  window.WWYThemes = {
    list: function () { return THEMES.slice(); },
    current: function () { return find(currentId()); },
    apply: apply,
  };

  // 立刻应用本地记住的风格：开屏动画比 core.js 先启动，等不到后端配置
  apply(currentId(), false);
})();
