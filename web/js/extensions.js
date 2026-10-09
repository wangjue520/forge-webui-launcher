/* extensions.js — 常用插件页
 *
 * 顶部「安装到」明确显示目标实例（类型 + 名字 + 实际安装目录）；有多个实例时
 * 可以直接在这里切换安装目标，不用先去切当前实例。
 * Forge：git clone 到 extensions/，依赖由 WebUI 启动时自己装。
 * ComfyUI：clone 到 custom_nodes/ 并用该实例的 Python 装好依赖（ComfyUI 不会自己装），
 *          所以实例运行中不允许装。
 * 已装的插件（包括用户自己装的、不在清单里的）都能看版本、检查更新、换版本 / 回滚。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  const INTRO_FORGE = "勾选想装的扩展，点「安装选中项」会依次 git clone 到上面这个实例的 extensions/ 目录。已经装过的（包括你以前自己在 WebUI 里装的）显示「已安装」并自动跳过。部分扩展（ADetailer、WD14 Tagger 等）会按实例的分支自动装对应的兼容版本。已装的插件可以「检查插件更新」后一键更新，也可以点「版本…」换成任意历史版本或回滚。装好 / 更新后重启 WebUI 生效。";
  const INTRO_COMFY = "ComfyUI 的节点和 WebUI 扩展不通用，这里是 ComfyUI 专用清单。安装时会 clone 到上面这个实例的 custom_nodes/，并用它自己的 Python 把节点依赖一起装好（ComfyUI 不会自动装依赖）；更新 / 换版本后也会自动重装依赖。「推荐」那组在「环境部署」新装 ComfyUI 时会默认一起装上。更多节点可以在 ComfyUI 网页里用 Manager 搜索安装。";

  let log = null;
  let items = [];
  let others = [];
  let installing = false;
  let targetId = "";        // 空 = 当前实例
  let last = null;          // 上一次 ext_list 的结果

  function setInstalling(v) {
    installing = v;
    updateButtons();
    $("#ext-refresh").disabled = v;
    $("#ext-cancel").disabled = !v;
    $("#ext-target-sel").disabled = v;
    document.querySelectorAll("#ext-list [data-act]").forEach((b) => { b.disabled = v; });
  }

  function comfyBlocked() {
    return !!(last && last.comfy && last.target && last.target.running);
  }

  function allRepos() {
    const out = [];
    items.forEach((it) => (it.repos || []).forEach((r) => out.push(r)));
    others.forEach((r) => out.push(r));
    return out;
  }

  function updatable() {
    return allRepos().filter((r) => r.is_git && !r.pinned && r.update && r.update.behind);
  }

  function updateButtons() {
    const ok = !!(last && last.has_root);
    $("#ext-install").disabled = installing || comfyBlocked() || !ok;
    $("#ext-pick-default").disabled = installing;
    $("#ext-check-upd").disabled = installing || !ok || !allRepos().some((r) => r.is_git);
    $("#ext-open-dir").disabled = !ok;
    const up = updatable();
    const btn = $("#ext-update-all");
    btn.hidden = !up.length;
    btn.textContent = `全部更新（${up.length}）`;
    btn.disabled = installing || comfyBlocked();
  }

  /* 一个已装目录的版本行 */
  function repoHtml(r, showFolder) {
    const u = r.update || {};
    let ver;
    if (!r.is_git) ver = '<span class="ext-ver-na">不是 git 安装的（zip 解压 / 注册表版），没法在这里管理版本</span>';
    else if (r.pinned) ver = `固定在 <b>${App.esc(r.short)}</b>`;
    else ver = `${App.esc(r.branch || "")} @ <b>${App.esc(r.short)}</b>`;
    let st = "";
    if (r.disabled) st += '<span class="ext-status ext-off">已停用</span>';
    if (u.error) st += `<span class="ext-upd-err" title="${App.esc(u.error)}">检查失败</span>`;
    else if (u.behind) st += `<span class="ext-status ext-upd">有更新 · ${u.behind} 个提交 · ${App.esc(u.latest_date || "")}</span>`;
    else if (u.behind === 0) st += '<span class="ext-upd-ok">已是最新</span>';
    else if (u.checked && r.pinned) st += `<span class="ext-upd-ok">最新 ${App.esc(u.latest_short || "")}</span>`;
    const rel = App.esc(r.rel);
    const btns = r.is_git
      ? (u.behind && !r.pinned ? `<button class="btn btn-xs btn-accent" data-act="update" data-rel="${rel}">更新</button>` : "") +
        `<button class="btn btn-xs" data-act="versions" data-rel="${rel}" data-name="${App.esc(r.folder)}">版本…</button>`
      : "";
    return `<div class="ext-ver">` +
      `<span class="ext-ver-txt">${showFolder ? `<span class="ext-folder" title="${App.esc(r.remote || "")}">${App.esc(r.folder)}</span>` : ""}${ver}${st}</span>` +
      `<span class="ext-ver-btns">${btns}<button class="btn btn-xs" data-act="open" data-rel="${rel}" title="打开插件目录">目录</button></span></div>`;
  }

  function itemHtml(it) {
    const tags = (it.installed ? '<span class="ext-status">已安装</span>' : "") +
      (!it.installed && it.default ? '<span class="ext-status ext-rec">推荐</span>' : "") +
      (it.author ? '<span class="ext-status ext-rec">作者自制</span>' : "");
    const repos = it.repos || [];
    const variant = it.variant_hint
      ? `<div class="ext-variant">${App.esc(it.variant_hint)}` +
        (it.can_replace ? ` <button class="btn btn-xs" data-act="replace" data-id="${App.esc(it.id)}">换成推荐版本</button>` : "") + "</div>"
      : "";
    // 装在别的文件夹名下（自己装的 / zip 下载的 / 另一个分支的）时把文件夹名标出来
    const want = String(it.folder || "").toLowerCase();
    const showFolder = repos.length > 1 || repos.some((r) => r.folder.toLowerCase() !== want);
    return `<input type="checkbox" data-id="${App.esc(it.id)}" ${it.installed ? "disabled" : ""}><i></i>` +
      `<span class="ext-body"><span class="ext-name">${App.esc(it.name)}</span>${tags}` +
      `<div class="ext-desc">${App.esc(it.desc || "")}</div>` +
      repos.map((r) => repoHtml(r, showFolder)).join("") + variant + `</span>`;
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
    if (others.length) {
      const h = document.createElement("div");
      h.className = "ext-group";
      h.textContent = `其他已装插件（${others.length}）· 不在上面清单里的，也能在这里更新 / 换版本`;
      box.appendChild(h);
      others.forEach((r) => {
        const el = document.createElement("div");
        el.className = "ext-item ext-other installed";
        el.innerHTML = `<span class="ext-body">${repoHtml(r, true)}</span>`;
        box.appendChild(el);
      });
    }
    if (installing) setInstalling(true);
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
    const msgs = [];
    if (r.comfy && t.running) {
      msgs.push(`「${t.name}」正在运行：安装 / 更新节点要往它的 Python 环境里装依赖，运行中会因为文件被占用而失败。请先在「一键启动」页停止它。`);
    }
    if (!r.comfy && r.disable_all && r.disable_all !== "none") {
      msgs.push(`WebUI 里设置了「停用${r.disable_all === "all" ? "全部" : "所有第三方"}扩展」，装好的扩展暂时不会加载（WebUI 的 Extensions 页可以改回来）。`);
    }
    warn.textContent = msgs.join("\n");
    warn.hidden = !msgs.length;
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
      others = (r && r.others) || [];
      renderTarget(last);
      if (r && r.has_root === false) {
        $("#ext-list").innerHTML = '<div class="hint" style="padding:10px">先在「一键启动」页给这个实例设置正确的根目录，才能检测/安装</div>';
      } else {
        render();
      }
      updateButtons();
    } catch (e) { App.toast("读取插件状态失败：" + e.message, "error"); }
  }

  function findRepo(rel) {
    return allRepos().find((r) => r.rel === rel);
  }

  async function switchRepos(rels, target, what) {
    const comfy = !!(last && last.comfy);
    const ok = await App.confirm("确认" + (what || "更新"),
      (rels.length > 1 ? `将更新 ${rels.length} 个插件到最新版（固定了版本的会跳过）。` : `${rels[0]}：${what}`) +
      "\n\n切换前的版本会记下来，「版本…」里随时能回去；你改过的插件文件会先备份。" +
      (comfy ? "\n节点换版本后会自动重装依赖。" : "\n重启 WebUI 后生效。"), "开始");
    if (!ok) return;
    try {
      const r = await App.api.ext_switch(rels, target || null, targetId || null);
      if (r && r.ok === false) App.toast(r.error || "操作失败", "error", 6000);
    } catch (e) { App.toast("操作失败：" + e.message, "error"); }
  }

  async function openVersions(rel, name) {
    let pick = null;
    let loaded = false;
    const v = await App.modal(`「${name}」的版本`, '<div class="hint">正在联网获取版本列表…（走 GitHub 加速，一般几秒）</div>', [
      { id: "switch", label: "切换到所选版本", kind: "primary" },
      { id: "cancel", label: "关闭" },
    ], {
      async onOpen(body) {
        let r;
        try { r = await App.api.ext_repo_versions(rel, targetId || null); }
        catch (e) { r = { ok: false, error: e.message }; }
        if (!body.isConnected) return;
        if (!r || r.ok === false) { body.innerHTML = `<div class="hint" data-status="bad">${App.esc((r && r.error) || "获取失败")}</div>`; return; }
        const l = r.local || {};
        const head = `<div class="kv-table">` +
          `<div class="k">当前</div><div class="v"><b>${App.esc(l.tag || (l.branch ? l.branch + " @ " + l.short : "固定 " + (l.short || "")))}</b>　${App.esc(l.date || "")}　${App.esc(l.subject || "")}</div>` +
          `<div class="k">仓库</div><div class="v">${App.esc(l.remote || "")}</div>` +
          ((l.dirty || []).length ? `<div class="k">本地修改</div><div class="v">${l.dirty.length} 个文件（切换时会备份并尝试合并）</div>` : "") +
          `</div>`;
        if (!r.remote || !r.remote.ok) {
          body.innerHTML = head + `<div class="hint" data-status="bad">${App.esc((r.remote && r.remote.error) || "获取远程版本失败")}</div>` +
            ((r.history || []).length ? '<div id="ext-ver-pick"></div>' : "");
          if ((r.history || []).length) {
            pick = App.verPicker(body.querySelector("#ext-ver-pick"), { history: r.history, current: l.commit });
            loaded = true;
          }
          return;
        }
        const c = r.remote;
        const st = c.behind === 0 ? '<div class="hint" data-status="ok">已经是最新版</div>'
          : `<div class="hint" data-status="warn">落后 ${c.behind == null ? "40+" : c.behind} 个提交</div>`;
        body.innerHTML = head + st + '<div id="ext-ver-pick" class="ext-ver-pick"></div>';
        pick = App.verPicker(body.querySelector("#ext-ver-pick"), Object.assign({}, c, { history: r.history }));
        loaded = true;
        refresh();
      },
    });
    if (v !== "switch") return;
    const t = loaded && pick && pick.selected();
    if (!t) { App.toast("先在列表里选一个版本", "error"); return; }
    switchRepos([rel], t, "切换到 " + (t.label || t.name || (t.commit || "").slice(0, 7)));
  }

  App.pages.extensions = {
    init() {
      log = App.makeLogger($("#ext-log"), 600);

      $("#ext-refresh").addEventListener("click", refresh);
      $("#ext-log-clear").addEventListener("click", () => { $("#ext-log").textContent = ""; });
      $("#ext-cancel").addEventListener("click", () => App.api.ext_cancel());
      $("#ext-target-sel").addEventListener("change", (e) => { targetId = e.target.value; refresh(); });
      $("#ext-open-dir").addEventListener("click", () => App.api.ext_open_dir(null, targetId || null));
      $("#ext-pick-default").addEventListener("click", () => {
        const rec = new Set(items.filter((i) => i.default).map((i) => i.id));
        document.querySelectorAll('#ext-list input[type="checkbox"]:not(:disabled)').forEach((el) => {
          el.checked = rec.has(el.dataset.id);
        });
      });
      $("#ext-check-upd").addEventListener("click", async () => {
        try {
          const r = await App.api.ext_check_updates(targetId || null);
          if (r && r.ok === false) App.toast(r.error || "检查失败", "error", 6000);
        } catch (e) { App.toast("检查失败：" + e.message, "error"); }
      });
      $("#ext-update-all").addEventListener("click", () => {
        const rels = updatable().map((r) => r.rel);
        if (rels.length) switchRepos(rels, null, "全部更新");
      });

      // 插件行里的按钮：在 <label> 里面，必须拦住默认行为，否则点按钮会去勾复选框
      $("#ext-list").addEventListener("click", async (e) => {
        const b = e.target.closest("[data-act]");
        if (!b) return;
        e.preventDefault();
        e.stopPropagation();
        if (installing) return;
        const act = b.dataset.act;
        if (act === "open") App.api.ext_open_dir(b.dataset.rel, targetId || null);
        else if (act === "versions") openVersions(b.dataset.rel, b.dataset.name);
        else if (act === "update") {
          const r = findRepo(b.dataset.rel);
          switchRepos([b.dataset.rel], null, `更新到最新（${(r && r.update && r.update.latest_short) || ""}）`);
        } else if (act === "replace") {
          const it = items.find((i) => i.id === b.dataset.id);
          const ok = await App.confirm("换成推荐版本",
            `「${it ? it.name : ""}」现在装的不是当前分支推荐的版本。\n\n旧版本的文件夹会整个挪到启动器的 launcher_data\\version_backup\\replaced\\ 下（想回去可以挪回来），然后安装推荐版本。` +
            "\n\nWebUI 正在运行的话请先停止。", "开始替换");
          if (!ok) return;
          try {
            const r = await App.api.ext_replace(b.dataset.id, targetId || null);
            if (r && r.ok === false) App.toast(r.error || "替换失败", "error", 6000);
          } catch (err) { App.toast("替换失败：" + err.message, "error"); }
          refresh();
        }
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
      // 批量检查更新时逐个回填，不用等全部查完
      App.on("ext", "update_status", (e) => {
        const r = findRepo(e.rel);
        if (r) { r.update = e.update || {}; render(); updateButtons(); }
      });

      refresh();
    },
    onShow() { if (!installing) refresh(); },
  };
})();
