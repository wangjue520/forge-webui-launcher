/* extensions.js — 常用插件页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let log = null;
  let items = [];
  let installing = false;

  function setInstalling(v) {
    installing = v;
    $("#ext-install").disabled = v;
    $("#ext-refresh").disabled = v;
    $("#ext-cancel").disabled = !v;
  }

  function render() {
    const box = $("#ext-list");
    box.innerHTML = "";
    items.forEach((it) => {
      const el = document.createElement("label");
      el.className = "chk-row ext-item" + (it.installed ? " installed" : "");
      el.innerHTML =
        `<input type="checkbox" data-name="${App.esc(it.name)}" ${it.installed ? "disabled" : ""}><i></i>` +
        `<span><span class="ext-name">${App.esc(it.name)}</span>` +
        (it.installed ? '<span class="ext-status">已安装</span>' : "") +
        `<div class="ext-desc">${App.esc(it.desc || "")}</div></span>`;
      box.appendChild(el);
    });
  }

  async function refresh() {
    try {
      const r = await App.api.ext_list();
      items = (r && r.items) || [];
      if (r && r.has_root === false) {
        $("#ext-list").innerHTML = '<div class="hint" style="padding:10px">先在「一键启动」页设置 WebUI 根目录，才能检测/安装扩展</div>';
        return;
      }
      render();
    } catch (e) { App.toast("读取插件状态失败：" + e.message, "error"); }
  }

  App.pages.extensions = {
    init() {
      log = App.makeLogger($("#ext-log"), 400);

      $("#ext-refresh").addEventListener("click", refresh);
      $("#ext-log-clear").addEventListener("click", () => { $("#ext-log").textContent = ""; });
      $("#ext-cancel").addEventListener("click", () => App.api.ext_cancel());

      $("#ext-install").addEventListener("click", async () => {
        const names = Array.from(document.querySelectorAll('#ext-list input[type="checkbox"]:checked'))
          .map((el) => el.dataset.name);
        if (!names.length) { App.toast("先勾选要安装的扩展", "error"); return; }
        try {
          const r = await App.api.ext_install(names);
          if (r && r.ok === false) App.toast(r.error || "安装失败", "error");
        } catch (e) { App.toast("安装失败：" + e.message, "error"); }
      });

      App.on("ext", "log", (e) => log(e.text));
      App.on("ext", "state", (e) => setInstalling(!!e.running));
      App.on("ext", "done", () => { setInstalling(false); refresh(); });

      refresh();
    },
    onShow() { if (!installing) refresh(); },
  };
})();
