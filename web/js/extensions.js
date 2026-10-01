/* extensions.js — 常用插件页
 *
 * 顶部「安装到」明确显示目标实例（类型 + 名字 + 实际安装目录）；有多个实例时
 * 可以直接在这里切换安装目标，不用先去切当前实例。
 * Forge：git clone 到 extensions/，依赖由 WebUI 启动时自己装。
 * ComfyUI：clone 到 custom_nodes/ 并用该实例的 Python 装好依赖（ComfyUI 不会自己装），
 *          所以实例运行中不允许装。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  const INTRO_FORGE = "勾选想装的扩展，点「安装选中项」会依次 git clone 到上面这个实例的 extensions/ 目录。已经装过的显示「已安装」并自动跳过。部分扩展（ADetailer、WD14 Tagger）会按实例的分支自动装对应的兼容版本。装好后重启 WebUI 生效。";
  const INTRO_COMFY = "ComfyUI 的节点和 WebUI 扩展不通用，这里是 ComfyUI 专用清单。安装时会 clone 到上面这个实例的 custom_nodes/，并用它自己的 Python 把节点依赖一起装好（ComfyUI 不会自动装依赖）。「推荐」那组在「环境部署」新装 ComfyUI 时会默认一起装上。更多节点可以在 ComfyUI 网页里用 Manager 搜索安装。";

  let log = null;
  let items = [];
  let installing = false;
  let targetId = "";        // 空 = 当前实例
  let last = null;          // 上一次 ext_list 的结果

  function setInstalling(v) {
    installing = v;
    updateButtons();
    $("#ext-refresh").disabled = v;
    $("#ext-cancel").disabled = !v;
    $("#ext-target-sel").disabled = v;
  }

  function updateButtons() {
    const blocked = !!(last && last.comfy && last.target && last.target.running);
    $("#ext-install").disabled = installing || blocked || !(last && last.has_root);
    $("#ext-pick-default").disabled = installing;
  }

  function itemHtml(it) {
    const tags = (it.installed ? '<span class="ext-status">已安装</span>' : "") +
      (!it.installed && it.default ? '<span class="ext-status ext-rec">推荐</span>' : "");
    return `<input type="checkbox" data-id="${App.esc(it.id)}" ${it.installed ? "disabled" : ""}><i></i>` +
      `<span><span class="ext-name">${App.esc(it.name)}</span>${tags}` +
      `<div class="ext-desc">${App.esc(it.desc || "")}</div></span>`;
  }

  function render() {
    const box = $("#ext-list");
    box.innerHTML = "";
    const groups = last && last.comfy ? (last.groups || {}) : null;
    let curGroup = null;
    items.forEach((it) => {
      if (groups && it.group !== curGroup) {
        curGroup = it.group;
        const h = document.createElement("div");
        h.className = "ext-group";
        h.textContent = groups[curGroup] || curGroup;
        box.appendChild(h);
      }
      const el = document.createElement("label");
      el.className = "chk-row ext-item" + (it.installed ? " installed" : "");
      el.innerHTML = itemHtml(it);
      box.appendChild(el);
    });
  }

  function renderTarget(r) {
    const t = r.target || {};
    const kind = $("#ext-target-kind");
    kind.dataset.kind = t.kind || "";
    kind.textContent = t.kind_label || "—";
    $("#ext-target-name").textContent = t.name || "—";
    $("#ext-target-path").textContent = t.target_dir
      ? (r.comfy ? "节点目录：" : "扩展目录：") + t.target_dir
      : "（这个实例还没有设置正确的根目录）";
    $("#ext-target-hint").textContent = r.comfy
      ? "ComfyUI 实例 → 装 ComfyUI 节点"
      : "WebUI 实例 → 装 WebUI 扩展";

    // 多个实例时才出现切换框
    const list = (App.instances && App.instances.instances) || [];
    const sel = $("#ext-target-sel");
    if (list.length > 1) {
      sel.innerHTML = list.map((i) =>
        `<option value="${App.esc(i.id)}">${App.esc(i.name)}${i.name !== i.kind_label ? "（" + App.esc(i.kind_label) + "）" : ""}${i.active ? " · 当前实例" : ""}</option>`
      ).join("");
      sel.value = t.id || "";
      sel.hidden = false;
      $("#ext-target-name").hidden = true;
    } else {
      sel.hidden = true;
      $("#ext-target-name").hidden = false;
    }

    const warn = $("#ext-target-warn");
    if (r.comfy && t.running) {
      warn.textContent = `「${t.name}」正在运行：安装节点要往它的 Python 环境里装依赖，运行中会因为文件被占用而失败。请先在「一键启动」页停止它。`;
      warn.hidden = false;
    } else {
      warn.hidden = true;
    }
    $("#ext-intro").textContent = r.comfy ? INTRO_COMFY : INTRO_FORGE;
    $("#ext-pick-default").hidden = !r.comfy;
  }

  async function refresh() {
    try {
      // 选中的目标实例被删了 → 回到当前实例
      const list = (App.instances && App.instances.instances) || [];
      if (targetId && !list.some((i) => i.id === targetId)) targetId = "";
      const r = await App.api.ext_list(targetId || null);
      last = r || {};
      items = (r && r.items) || [];
      renderTarget(last);
      if (r && r.has_root === false) {
        $("#ext-list").innerHTML = '<div class="hint" style="padding:10px">先在「一键启动」页给这个实例设置正确的根目录，才能检测/安装</div>';
      } else {
        render();
      }
      updateButtons();
    } catch (e) { App.toast("读取插件状态失败：" + e.message, "error"); }
  }

  App.pages.extensions = {
    init() {
      log = App.makeLogger($("#ext-log"), 600);

      $("#ext-refresh").addEventListener("click", refresh);
      $("#ext-log-clear").addEventListener("click", () => { $("#ext-log").textContent = ""; });
      $("#ext-cancel").addEventListener("click", () => App.api.ext_cancel());
      $("#ext-target-sel").addEventListener("change", (e) => { targetId = e.target.value; refresh(); });
      $("#ext-pick-default").addEventListener("click", () => {
        const rec = new Set(items.filter((i) => i.default).map((i) => i.id));
        document.querySelectorAll('#ext-list input[type="checkbox"]:not(:disabled)').forEach((el) => {
          el.checked = rec.has(el.dataset.id);
        });
      });

      $("#ext-install").addEventListener("click", async () => {
        const ids = Array.from(document.querySelectorAll('#ext-list input[type="checkbox"]:checked'))
          .map((el) => el.dataset.id);
        if (!ids.length) { App.toast("先勾选要安装的项目", "error"); return; }
        const t = (last && last.target) || {};
        const ok = await App.confirm("确认安装",
          `将安装 ${ids.length} 项到：\n${t.name || ""}（${t.kind_label || ""}）\n${t.target_dir || ""}` +
          (last && last.comfy ? "\n\n节点的 Python 依赖会一起安装，视网速可能需要几分钟。" : ""),
          "开始安装");
        if (!ok) return;
        try {
          const r = await App.api.ext_install(ids, targetId || null);
          if (r && r.ok === false) App.toast(r.error || "安装失败", "error", 6000);
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
