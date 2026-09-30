/* instances.js — 多实例：侧栏切换器、启动页实例卡片、实例管理页、共享模型库、文件移动记录
 *
 * 单实例用户看不到这里的任何东西：实例数 < 2 且没手动打开多实例模式时，
 * 切换器、实例卡片、实例管理页全部隐藏（见 applyMulti）。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  App.instances = { instances: [], active: "", multi: false };
  App.isComfy = () => (App.cfg && App.cfg.webui_branch) === "comfyui";
  App.kindName = () => (App.isComfy() ? "ComfyUI" : "WebUI");

  const STATE_TEXT = { idle: "未启动", starting: "启动中", ready: "运行中", running: "运行中", stopped: "已停止" };

  function inst(id) { return App.instances.instances.find((i) => i.id === id); }
  function active() { return inst(App.instances.active) || App.instances.instances[0]; }
  function isRunning(i) { return !!(i && i.status && i.status.running); }

  /* ---------- 外观：功能开关 / 多实例显隐 / 按实例类型改文案 ---------- */
  function applyHidden(pages) {
    App.hiddenPages = pages || [];
    $$(".nav-item").forEach((b) => {
      if (b.classList.contains("nav-multi")) return;
      b.hidden = b.dataset.page !== "launch" && App.hiddenPages.includes(b.dataset.page);
    });
    applyMulti();  // 再叠多实例规则（环境部署收进实例管理页等）
  }

  function applyMulti() {
    const m = !!App.instances.multi;
    $$(".nav-multi").forEach((b) => { b.hidden = !m; });
    $("#inst-switch").hidden = !m;
    $("#launch-instances").hidden = !m;
    const og = $("#of-inst-group");
    if (og) og.hidden = !m;
    // 多实例时「环境部署」从侧栏收进「实例管理」页：部署新实例的入口统一在
    // 实例管理里（「部署新的实例…」按钮会跳到部署页），侧栏不再单列一个。
    // 页面本身保留；回到单实例时恢复显示。
    const deployNav = document.querySelector('.nav-item[data-page="deploy"]');
    if (deployNav && m) deployNav.hidden = true;
    if (!m && App.currentPage === "instances") App.showPage("launch");
  }

  function applyKindTexts() {
    const comfy = App.isComfy();
    $("#launch-root-title").textContent = comfy ? "ComfyUI 根目录" : "WebUI 根目录";
    $("#launch-root-hint").textContent = comfy
      ? "包含 main.py 的文件夹；官方便携包选外层的 ComfyUI_windows_portable 也可以"
      : "包含 webui.bat 的那个文件夹";
    $$("[data-forge-only]").forEach((el) => { el.hidden = comfy; });
    const ah = $("#launch-args-hint");
    if (ah) ah.textContent = comfy ? "传给 ComfyUI 的 main.py，由「高级选项」页生成" : "将注入 COMMANDLINE_ARGS 环境变量，由「高级选项」页生成";
  }

  App.applyChrome = function (state) {
    setInstances((state && state.instances) || App.instances);
    applyHidden((state && state.hidden_pages) || []);
    applyKindTexts();
  };

  function setInstances(p) {
    if (!p || !Array.isArray(p.instances)) return;
    App.instances = p;
    renderSwitcher();
    renderLaunchCards();
    renderList();
    applyMulti();
    updateGlobalStatus();
  }

  async function refresh() {
    try {
      const r = await App.api.instances_list();
      if (!r || r.ok === false) return;
      const wasActive = App.instances.active;
      setInstances(r);
      // 当前实例被删掉了（后端已经切到别的实例）→ 重载让各页面换成新实例的配置
      if (wasActive && r.active !== wasActive) reloadKeepPage();
    } catch (e) { console.error(e); }
  }
  App.refreshInstances = refresh;

  function reloadKeepPage() {
    try {
      sessionStorage.setItem("skip-splash", "1");
      sessionStorage.setItem("return-page", App.currentPage || "launch");
    } catch (e) {}
    location.reload();
  }

  async function switchTo(id) {
    if (!id || id === App.instances.active) return;
    try {
      const r = await App.api.instance_switch(id);
      if (r && r.ok === false) { App.toast(r.error || "切换失败", "error"); return; }
      reloadKeepPage();
    } catch (e) { App.toast("切换失败：" + e.message, "error"); }
  }
  App.switchInstance = switchTo;

  /* ---------- 全局状态：多实例时显示「N 个实例运行中」 ---------- */
  function updateGlobalStatus() {
    if (!App.instances.multi) return;   // 单实例由 launch.js 直接设置
    const list = App.instances.instances;
    const run = list.filter((i) => i.status && (i.status.state === "ready" || i.status.state === "running"));
    const starting = list.filter((i) => i.status && i.status.state === "starting");
    if (run.length) App.setGlobalStatus("ready", `${run.length} 个实例运行中`);
    else if (starting.length) App.setGlobalStatus("starting", `${starting.length} 个实例启动中`);
    else App.setGlobalStatus("idle", "没有实例在运行");
  }

  /* ---------- 侧栏切换器 ---------- */
  function renderSwitcher() {
    const sel = $("#inst-switch-select");
    sel.innerHTML = App.instances.instances.map((i) =>
      `<option value="${App.esc(i.id)}">${App.esc(i.name)} · ${App.esc(i.kind_label)}</option>`).join("");
    sel.value = App.instances.active;
    const a = active();
    $("#inst-switch-dot").dataset.state = (a && a.status && a.status.state) || "idle";
  }

  /* ---------- 启动页：所有实例的状态卡片 ---------- */
  function statusPill(i) {
    const st = (i.status && i.status.state) || "idle";
    const port = i.status && i.status.port ? ` :${i.status.port}` : "";
    return `<span class="status-pill" data-state="${st}"><span class="dot"></span><span>${STATE_TEXT[st] || st}${port}</span></span>`;
  }

  function renderLaunchCards() {
    const box = $("#launch-instances");
    box.innerHTML = App.instances.instances.map((i) => `
      <div class="inst-card${i.active ? " active" : ""}" data-id="${App.esc(i.id)}">
        <div class="ic-head">
          <span class="ic-kind" data-kind="${i.kind}">${App.esc(i.kind_label)}</span>
          <b class="ic-name">${App.esc(i.name)}</b>
          ${i.active ? '<em class="ic-cur">当前</em>' : ""}
        </div>
        <div class="ic-root" title="${App.esc(i.root)}">${App.esc(i.root || "（未设置目录）")}</div>
        <div class="ic-foot">
          ${statusPill(i)}
          <span class="ic-btns">
            ${isRunning(i)
              ? `<button class="btn btn-xs" data-act="open"${i.status.url ? "" : " disabled"}>打开</button><button class="btn btn-xs" data-act="stop">停止</button>`
              : `<button class="btn btn-xs btn-primary" data-act="start"${i.root ? "" : " disabled"}>启动</button>`}
            ${i.active ? "" : '<button class="btn btn-xs" data-act="switch">设为当前</button>'}
          </span>
        </div>
      </div>`).join("");
    box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () =>
      cardAction(b.dataset.act, b.closest("[data-id]").dataset.id)));
  }

  async function cardAction(act, id) {
    const i = inst(id);
    if (!i) return;
    if (act === "switch") return switchTo(id);
    if (act === "open" && i.status.url) return App.api.open_url(i.status.url);
    if (act === "stop") {
      try { await App.api.launch_stop(id); } catch (e) { App.toast("停止失败：" + e.message, "error"); }
      return;
    }
    if (act === "start" && App.pages.launch && App.pages.launch.startInstance) {
      const others = App.instances.instances.filter((x) => x.id !== id && isRunning(x));
      if (others.length) {
        const v = await App.modal("同时运行多个实例",
          `已经有 ${others.map((x) => "「" + App.esc(x.name) + "」").join("、")} 在运行。` +
          "同一块显卡上再启动一个实例，两个都会占显存，大模型很容易爆显存。\n\n端口会自动错开，不会冲突。",
          [{ id: "go", label: "仍然启动", kind: "primary" }, { id: "cancel", label: "取消" }]);
        if (v !== "go") return;
      }
      // 立刻把卡片标成启动中：预检/确认弹窗期间后端还没发状态事件，
      // 没反馈的话用户会以为点了没反应
      i.status = Object.assign({}, i.status, { iid: id, state: "starting", text: "启动前检查…" });
      renderLaunchCards();
      renderList();
      await App.pages.launch.startInstance(id, i.root);
      refresh();  // 对齐后端真实状态（取消/失败时退回原状）
    }
  }

  /* ---------- 实例管理页 ---------- */
  function renderList() {
    const box = $("#inst-list");
    if (!box) return;
    box.innerHTML = App.instances.instances.map((i) => `
      <div class="card inst-row" data-id="${App.esc(i.id)}">
        <div class="ir-grid">
          <span class="ic-kind" data-kind="${i.kind}">${App.esc(i.kind_label)}</span>
          <input type="text" class="ir-name" value="${App.esc(i.name)}" title="实例名称，可以直接改">
          ${statusPill(i)}
          ${i.active ? '<em class="ic-cur">当前实例</em>' : `<button class="btn btn-xs" data-act="switch">设为当前</button>`}
        </div>
        <div class="form-grid">
          <label>根目录</label>
          <div class="row"><input type="text" class="ir-root" value="${App.esc(i.root)}" readonly><button class="btn btn-sm" data-act="root">更换…</button></div>
          <label>端口</label>
          <input type="text" class="ir-port" value="${App.esc(i.port)}" placeholder="留空自动（${i.kind === "comfyui" ? "8188" : "7860"} 起，多开时自动错开）">
        </div>
        <div class="action-row">
          ${isRunning(i)
            ? '<button class="btn btn-sm" data-act="stop">停止</button>'
            : `<button class="btn btn-sm btn-primary" data-act="start"${i.root ? "" : " disabled"}>启动</button>`}
          <button class="btn btn-sm btn-danger-text" data-act="remove"${App.instances.instances.length <= 1 ? " disabled" : ""}>移除实例</button>
          <span class="hint">移除只是从启动器里去掉，不会删除任何文件</span>
        </div>
      </div>`).join("");

    box.querySelectorAll(".inst-row").forEach((row) => {
      const id = row.dataset.id;
      row.querySelector(".ir-name").addEventListener("change", (e) => update(id, { name: e.target.value }));
      row.querySelector(".ir-port").addEventListener("change", (e) => update(id, { port: e.target.value }));
      row.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => rowAction(b.dataset.act, id)));
    });
  }

  async function update(id, changes) {
    try {
      const r = await App.api.instance_update(id, changes);
      if (r && r.ok === false) { App.toast(r.error || "保存失败", "error"); refresh(); return; }
      setInstances(r);
      App.toast("已保存", "ok", 1500);
    } catch (e) { App.toast("保存失败：" + e.message, "error"); }
  }

  async function rowAction(act, id) {
    const i = inst(id);
    if (!i) return;
    if (act === "switch") return switchTo(id);
    if (act === "start" || act === "stop") return cardAction(act, id);
    if (act === "root") {
      const r = await App.api.choose_directory("选择实例根目录", i.root || "");
      if (r && r.ok && r.path) update(id, { webui_root: r.path });
      return;
    }
    if (act === "remove") {
      const yes = await App.confirm("移除实例",
        `从启动器里移除「${i.name}」？\n\n只是不再管理它，${i.root || "该目录"} 里的文件一个都不会删。`, "移除", true);
      if (!yes) return;
      try {
        const r = await App.api.instance_remove(id);
        if (r && r.ok === false) { App.toast(r.error || "移除失败", "error"); return; }
        App.toast("已移除", "ok");
        refresh();
      } catch (e) { App.toast("移除失败：" + e.message, "error"); }
    }
  }

  async function addInstance() {
    const r = await App.api.choose_directory("选择已有的 ComfyUI 或 WebUI 安装目录", "");
    if (!r || !r.ok || !r.path) return;
    try {
      const a = await App.api.instance_add(r.path);
      if (a && a.ok === false) { App.toast(a.error || "添加失败", "error", 6000); return; }
      const added = (a.instances || []).find((x) => x.id === a.id);
      App.toast(`已添加实例「${added ? added.name : ""}」`, "ok");
      setInstances(a);
    } catch (e) { App.toast("添加失败：" + e.message, "error"); }
  }

  /* ---------- 共享模型库 ---------- */
  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function renderLibrary(st) {
    if (!st) return;
    $("#lib-enabled").checked = !!st.enabled;
    $("#lib-path").value = st.path || "";
    $("#lib-merge").disabled = !st.enabled;
    $("#lib-status").textContent = st.enabled
      ? `已开启：所有实例启动时都会挂载 ${st.path}（正在运行的实例需要重启后生效）`
      : "未开启：每个实例用自己 models 目录里的模型";
    const jl = $("#journal-list");
    jl.innerHTML = (st.journals || []).length
      ? st.journals.map((j) => `
          <div class="journal-row${j.undone ? " undone" : ""}">
            <span class="jr-time">${fmtTime(j.time)}</span>
            <span class="jr-title">${App.esc(j.title)}</span>
            <span class="jr-count">${j.count} 个文件</span>
            ${j.undone ? '<em class="jr-undone">已撤销</em>' : `<button class="btn btn-xs" data-undo="${App.esc(j.id)}">撤销</button>`}
          </div>`).join("")
      : '<div class="hint">还没有任何文件移动记录</div>';
    jl.querySelectorAll("[data-undo]").forEach((b) => b.addEventListener("click", () => undoJournal(b.dataset.undo)));
  }

  async function loadLibrary() {
    try { renderLibrary(await App.api.library_status()); } catch (e) { console.error(e); }
  }
  App.reloadLibrary = loadLibrary;

  async function undoJournal(id) {
    const yes = await App.confirm("撤销文件移动", "把这一批移动过的文件全部搬回原来的位置？\n（被判定为重复、已放进回收站的文件需要你自己去回收站还原）");
    if (!yes) return;
    try {
      const r = await App.api.journal_undo(id);
      if (r && r.ok === false) { App.toast(r.error || "撤销失败", "error", 6000); return; }
      App.toast(r.already ? "这一批已经撤销过了" :
        `已还原 ${r.restored} 个文件` + (r.failed && r.failed.length ? `，${r.failed.length} 个失败（${r.failed[0].name}：${r.failed[0].error}）` : "")
        + (r.trashed ? `；另有 ${r.trashed} 个重复文件在回收站里` : ""), r.failed && r.failed.length ? "error" : "ok", 6000);
      loadLibrary();
      if (App.pages.models && App.pages.models.reloadAll) App.pages.models.reloadAll();
    } catch (e) { App.toast("撤销失败：" + e.message, "error"); }
  }

  async function setLibrary(path, enabled) {
    try {
      const r = await App.api.library_set(path, enabled);
      if (r && r.ok === false) { App.toast(r.error || "设置失败", "error", 6000); loadLibrary(); return; }
      renderLibrary(r);
      App.toast(enabled ? "共享模型库已开启" + (r.note ? "，" + r.note : "") : "已关闭共享模型库", "ok", 5000);
      if (App.pages.models && App.pages.models.reloadAll) App.pages.models.reloadAll();
    } catch (e) { App.toast("设置失败：" + e.message, "error"); }
  }

  async function mergePreview() {
    let p;
    try { p = await App.api.library_merge_plan(); }
    catch (e) { App.toast("生成预览失败：" + e.message, "error"); return; }
    if (!p || p.ok === false) { App.toast((p && p.error) || "生成预览失败", "error"); return; }
    if (!p.count) { App.toast("各实例里没有需要合并的模型", "ok"); return; }
    const rows = (p.by_instance || []).map((b) =>
      `<tr><td>${App.esc(b.name)}</td><td>${b.move}</td><td>${b.dup}</td></tr>`).join("");
    const sample = (p.sample || []).map((s) =>
      `<div class="pv-line">${s.dup ? '<em class="pv-dup">重复→回收站</em>' : ""}${App.esc(s.from)}</div>`).join("");
    const v = await App.modal("合并各实例已有模型",
      `<div>要移动 <b>${p.count}</b> 个文件（${App.esc(p.total_text)}），其中重复可省下 <b>${App.esc(p.dup_text)}</b>。</div>` +
      (p.cross ? `<div class="pv-warn">有 ${App.esc(p.cross_text)} 在别的磁盘上，跨盘移动是真复制，会比较慢。</div>` : "") +
      `<table class="pv-table"><tr><th>实例</th><th>移入模型库</th><th>重复</th></tr>${rows}</table>` +
      `<div class="pv-list">${sample}${p.count > (p.sample || []).length ? `<div class="pv-line">…… 共 ${p.count} 个</div>` : ""}</div>` +
      `<div class="hint">执行前请先停止所有实例。完成后可以在下方「文件移动记录」里撤销。</div>`,
      [{ id: "go", label: "开始合并", kind: "primary" }, { id: "cancel", label: "取消" }]);
    if (v !== "go") return;
    try {
      const r = await App.api.library_merge_start();
      if (r && r.ok === false) { App.toast(r.error || "无法开始", "error", 6000); return; }
      App.progress($("#lib-progress")).set(0, "准备中…");
    } catch (e) { App.toast("无法开始：" + e.message, "error"); }
  }

  App.pages.instances = {
    init(state) {
      $("#inst-switch-select").addEventListener("change", (e) => switchTo(e.target.value));
      $("#inst-add").addEventListener("click", addInstance);
      $("#inst-deploy").addEventListener("click", () => App.showPage("deploy"));

      $("#lib-enabled").addEventListener("change", async (e) => {
        let path = $("#lib-path").value.trim();
        if (e.target.checked && !path) {
          const r = await App.api.choose_directory("选择共享模型库文件夹", "");
          if (!r || !r.ok || !r.path) { e.target.checked = false; return; }
          path = r.path;
        }
        setLibrary(path, e.target.checked);
      });
      $("#lib-browse").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择共享模型库文件夹", $("#lib-path").value || "");
        if (r && r.ok && r.path) setLibrary(r.path, $("#lib-enabled").checked || true);
      });
      $("#lib-merge").addEventListener("click", mergePreview);

      App.on("instances", "status", (e) => {
        const i = inst(e.iid);
        if (!i) return;
        i.status = { iid: e.iid, running: e.running, state: e.state, url: e.url, text: e.text, port: e.port };
        renderSwitcher();
        renderLaunchCards();
        renderList();
        updateGlobalStatus();
      });
      App.on("instances", "changed", () => refresh());
      App.on("library", "progress", (e) =>
        App.progress($("#lib-progress")).set(e.n ? e.i * 100 / e.n : 0, `${e.i}/${e.n} · ${e.name}`));
      App.on("library", "merge_done", (e) => {
        App.progress($("#lib-progress")).hide();
        App.toast(`合并完成：移入 ${e.moved} 个，重复进回收站 ${e.trashed} 个` +
          (e.failed && e.failed.length ? `，失败 ${e.failed.length} 个（${e.failed[0].name}）` : ""),
          e.failed && e.failed.length ? "error" : "ok", 7000);
        loadLibrary();
        if (App.pages.models && App.pages.models.reloadAll) App.pages.models.reloadAll();
      });

      App.applyChrome(state);
      loadLibrary();
    },
    onShow() { refresh(); loadLibrary(); },
  };
})();
