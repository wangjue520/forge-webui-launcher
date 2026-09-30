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
        $("#ext-list").innerHTML = '<div class="hint" style="padding:10px">先在「一键启动」页设置根目录，才能检测/安装插件</div>';
        return;
      }
      const hb = document.querySelector("#page-extensions .hint-block");
      if (hb) {
        if (!hb.dataset.forgeText) hb.dataset.forgeText = hb.textContent;
        hb.textContent = r && r.comfy
          ? "当前实例是 ComfyUI：勾选想装的节点，点「安装选中项」会依次 git clone 到 custom_nodes/ 目录下，已装过的自动跳过。节点的 Python 依赖会在 ComfyUI 下次启动时由节点自己安装；更多节点建议装好 ComfyUI-Manager 后在网页里搜索安装。"
          : hb.dataset.forgeText;
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
