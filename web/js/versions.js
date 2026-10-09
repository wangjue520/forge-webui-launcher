/* versions.js — 版本管理（WebUI / ComfyUI 本体）+ 版本选择列表（插件页的「版本…」弹窗共用）
 *
 * 不单独占侧栏：界面放在「环境部署」页顶部（#ver-section），多实例时也可以从
 * 「实例管理」每个实例的「版本管理…」按钮直接跳过来（App.openVersions）。
 * 还没有任何可用实例的新用户整块隐藏，不打扰第一次部署。
 *
 * 所有切换都是原地 git 检出：只换源码，venv / 模型 / 插件 / 输出不动，不用重装。
 * 每次切换前的版本由后端记进 launcher_data/version_history.json，「切换记录」一键回滚。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  /* ---------- 版本选择列表（共用） ----------
   * data: {commits, tags, branches, branch, current, history}
   * 返回 { selected() -> target | null }；target 直接传给后端 switch
   */
  App.verPicker = function (box, data, onPick) {
    data = data || {};
    const cur = data.current || "";
    const tabs = [];
    if ((data.commits || []).length) tabs.push(["commits", `最近提交（${data.branch || "当前分支"}）`]);
    if ((data.tags || []).length) tabs.push(["tags", `版本标签 ${data.tags.length}`]);
    if ((data.history || []).length) tabs.push(["history", `之前用过 ${data.history.length}`]);
    if ((data.branches || []).length > 1) tabs.push(["branches", `分支 ${data.branches.length}`]);
    let tab = tabs.length ? tabs[0][0] : "";
    let picked = null;

    function rowsHtml() {
      const row = (target, title, meta, isCur, extra) =>
        `<label class="chk-row ver-row${isCur ? " is-cur" : ""}"><input type="radio" name="ver-pick" value="${App.esc(JSON.stringify(target))}"${isCur ? " disabled" : ""}><i></i>` +
        `<span><b class="ver-name">${App.esc(title)}</b>${isCur ? '<span class="ext-status">当前</span>' : ""}${extra || ""}` +
        `<span class="ver-meta">${App.esc(meta)}</span></span></label>`;
      if (tab === "commits") {
        return data.commits.map((c, i) => row({ kind: "commit", commit: c.commit, label: c.short },
          c.short, `${c.date}  ${c.subject}`, c.commit === cur,
          i === 0 ? '<span class="ext-status ext-rec">最新</span>' : "")).join("");
      }
      if (tab === "tags") {
        return data.tags.map((t, i) => row({ kind: "tag", name: t.name, label: t.name },
          t.name, t.commit.slice(0, 7), t.commit === cur,
          i === 0 ? '<span class="ext-status ext-rec">最新版本号</span>' : "")).join("");
      }
      if (tab === "history") {
        return data.history.map((h) => row({ kind: "commit", commit: h.commit, label: h.label },
          h.label || h.commit.slice(0, 7), `${h.time} 切换前的版本 · ${h.commit.slice(0, 7)}`, h.commit === cur)).join("");
      }
      if (tab === "branches") {
        return '<div class="hint" data-status="warn" style="margin:4px 0 8px">换分支等于换成另一条开发线，参数和功能可能差别很大，不确定就别动。</div>' +
          data.branches.map((b) => row({ kind: "branch", name: b.name, label: b.name },
            b.name, b.commit.slice(0, 7) + (b.name === data.branch ? "  · 当前跟踪的分支" : ""), false)).join("");
      }
      return '<div class="hint">没有可选的版本</div>';
    }

    function render() {
      box.innerHTML =
        `<div class="ver-tabs">${tabs.map(([k, l]) =>
          `<button class="btn btn-xs${k === tab ? " btn-primary" : ""}" data-tab="${k}">${App.esc(l)}</button>`).join("")}</div>` +
        `<div class="ver-list">${rowsHtml()}</div>`;
      picked = null;
      if (onPick) onPick(null);
    }

    // 用属性赋值而不是 addEventListener：同一个容器反复渲染时不会叠出多份监听
    box.onclick = (e) => {
      const b = e.target.closest("[data-tab]");
      if (b) { tab = b.dataset.tab; render(); }
    };
    box.onchange = (e) => {
      if (e.target.name !== "ver-pick") return;
      try { picked = JSON.parse(e.target.value); } catch (err) { picked = null; }
      if (onPick) onPick(picked);
    };
    render();
    return { selected: () => picked };
  };

  /* ---------- 版本管理页 ---------- */
  let log = null;
  let targetId = "";
  let info = null;
  let picker = null;
  let busy = false;

  function setBusy(v) {
    busy = v;
    $("#ver-cancel").disabled = !v;
    $("#ver-target-sel").disabled = v;
    updateButtons();
  }

  function updateButtons() {
    const l = (info && info.local) || {};
    const ready = !!(info && info.has_root && info.has_git);
    const running = !!(info && info.running);
    $("#ver-update").disabled = busy || !ready || !l.is_git || running;
    $("#ver-check").disabled = busy || !ready || !l.is_git;
    $("#ver-adopt").hidden = !(info && info.has_root && !l.is_git);
    $("#ver-adopt").disabled = busy || !ready || running;
    $("#ver-apply").disabled = busy || running || !(picker && picker.selected());
    document.querySelectorAll("#ver-history [data-rollback]").forEach((b) => { b.disabled = busy || running; });
  }

  function renderTarget() {
    const t = (info && info.target) || {};
    const kind = $("#ver-target-kind");
    kind.dataset.kind = t.kind || "";
    kind.textContent = t.kind_label || "—";
    $("#ver-target-name").textContent = t.name || "—";
    $("#ver-target-path").textContent = t.repo ? "源码目录：" + t.repo : "（这个实例还没有设置正确的根目录）";
    $("#ver-target-hint").textContent = t.kind === "comfyui"
      ? "ComfyUI：切换后启动器会自动补装新版本需要的依赖"
      : "WebUI：切换后下次启动时 WebUI 会自己补装依赖";
    const list = (App.instances && App.instances.instances) || [];
    const sel = $("#ver-target-sel");
    if (list.length > 1) {
      sel.innerHTML = list.map((i) =>
        `<option value="${App.esc(i.id)}">${App.esc(i.name)}${i.name !== i.kind_label ? "（" + App.esc(i.kind_label) + "）" : ""}${i.active ? " · 当前实例" : ""}</option>`
      ).join("");
      sel.value = t.id || "";
      sel.hidden = false;
      $("#ver-target-name").hidden = true;
    } else {
      sel.hidden = true;
      $("#ver-target-name").hidden = false;
    }
    const warn = $("#ver-target-warn");
    const msgs = [];
    if (info && info.running) msgs.push(`「${t.name}」正在运行：切换版本前请先在「一键启动」页停止它（运行中的文件被占用会切换失败）。`);
    if (info && info.has_root && !info.has_git) msgs.push("没有找到可用的 Git：可以在「一键启动」页指定 git.exe，或在「环境部署」页部署便携环境。");
    if (info && info.official === false) msgs.push("这个仓库的远程地址不是启动器部署时用的官方仓库，更新会跟随它自己的远程地址。");
    warn.textContent = msgs.join("\n");
    warn.hidden = !msgs.length;
  }

  function renderCurrent() {
    const box = $("#ver-current");
    const st = $("#ver-status");
    st.removeAttribute("data-status");
    if (!info || !info.has_root) {
      box.innerHTML = "";
      st.textContent = "先在「一键启动」页给这个实例设置正确的根目录。";
      return;
    }
    const l = info.local || {};
    const kv = (k, v) => `<div class="k">${App.esc(k)}</div><div class="v">${v}</div>`;
    if (!l.is_git) {
      box.innerHTML = kv("安装方式", "整合包 / 解压安装（不是 git 仓库）") +
        (l.app_version ? kv("ComfyUI 版本号", App.esc(l.app_version)) : "");
      st.textContent = "这个目录不是用 git 装的，没法直接更新或选版本。点「接管为 Git 管理」后，启动器会把源码换成官方最新版并开始记录版本（改过的源码文件先备份，模型 / 插件 / venv 不动），以后就能在这里一键更新、切换、回滚。";
      return;
    }
    if (l.error) {
      box.innerHTML = kv("状态", App.esc(l.error));
      st.textContent = "";
      return;
    }
    const dirty = l.dirty || [];
    box.innerHTML =
      kv("仓库", App.esc(l.remote || "（没有远程地址）")) +
      kv("分支", l.branch ? App.esc(l.branch) : "固定在某个版本（不跟随分支更新，点「更新到最新」会回到默认分支）") +
      kv("版本", `<b>${App.esc(l.label || "")}</b>　${App.esc(l.date || "")}`) +
      kv("说明", App.esc(l.subject || "")) +
      (l.app_version ? kv("ComfyUI 版本号", App.esc(l.app_version)) : "") +
      (l.near_tag && !l.tag ? kv("最近的版本号", App.esc(l.near_tag)) : "") +
      kv("本地修改", dirty.length
        ? `${dirty.length} 个文件（切换时会备份并尝试合并）：${App.esc(dirty.slice(0, 6).join("、"))}${dirty.length > 6 ? " …" : ""}`
        : "无");
    const c = info.check;
    if (!c) {
      st.textContent = "还没检查更新。";
      $("#ver-check-hint").textContent = "";
    } else {
      $("#ver-check-hint").textContent = "检查于 " + (c.checked || "");
      if (c.behind === 0) {
        st.dataset.status = "ok";
        st.textContent = "已经是最新版。";
      } else {
        st.dataset.status = "warn";
        const lt = c.latest || {};
        st.textContent = `有新版本：比 ${c.branch} 分支最新落后 ${c.behind == null ? "40+" : c.behind} 个提交（最新 ${lt.short || ""} · ${lt.date || ""} · ${lt.subject || ""}）`;
      }
    }
  }

  function renderPicker() {
    const card = $("#ver-pick-card");
    const c = info && info.check;
    if (!c || !info.local || !info.local.is_git) { card.hidden = true; picker = null; return; }
    card.hidden = false;
    picker = App.verPicker($("#ver-pick"), Object.assign({}, c, { history: info.history || [] }), updateButtons);
  }

  function renderHistory() {
    const box = $("#ver-history");
    const h = (info && info.history) || [];
    const cur = (info && info.local && info.local.commit) || "";
    if (!h.length) { box.innerHTML = '<div class="hint">还没有切换过版本。</div>'; return; }
    box.innerHTML = h.map((x) =>
      `<div class="ver-hist-row"><span class="ver-name">${App.esc(x.label || x.commit.slice(0, 7))}</span>` +
      `<span class="ver-meta">${App.esc(x.time)} 切换前 · ${App.esc(x.commit.slice(0, 7))}</span>` +
      (x.commit === cur ? '<span class="ext-status">当前</span>'
        : `<button class="btn btn-xs" data-rollback="${App.esc(x.commit)}" data-label="${App.esc(x.label || "")}">回到这个版本</button>`) +
      `</div>`).join("");
  }

  async function refresh() {
    try {
      const list = (App.instances && App.instances.instances) || [];
      if (targetId && !list.some((i) => i.id === targetId)) targetId = "";
      info = await App.api.ver_info(targetId || null);
      $("#ver-section").hidden = !(info && info.has_root) && !list.some((i) => i.root);
      if (info && info.busy && !busy) setBusy(true);
      renderTarget();
      renderCurrent();
      renderPicker();
      renderHistory();
      updateButtons();
    } catch (e) { App.toast("读取版本信息失败：" + e.message, "error"); }
  }

  async function doCheck() {
    $("#ver-check").disabled = true;
    $("#ver-update").disabled = true;
    $("#ver-check").textContent = "正在检查…";
    try {
      const r = await App.api.ver_check(targetId || null);
      if (r && r.ok === false) App.toast(r.error || "检查失败", "error", 6000);
    } catch (e) { App.toast("检查失败：" + e.message, "error"); }
    $("#ver-check").textContent = "检查更新 / 选择版本";
    await refresh();
  }

  async function doSwitch(target, what) {
    const t = (info && info.target) || {};
    const comfy = t.kind === "comfyui";
    const branchWarn = target.kind === "branch" ? "\n\n注意：换分支相当于换一条开发线，启动参数和功能可能完全不同。" : "";
    const ok = await App.confirm("确认切换版本",
      `${t.name || ""}：${what}\n\n只替换源码，venv / 模型 / 插件 / 出图不会动；切换前的版本会记进「切换记录」，随时能回来。` +
      (comfy ? "\n切换后会自动补装新版本需要的依赖，可能要几分钟。" : "\n下次启动 WebUI 时它会自己补装依赖，第一次启动会慢一点。") +
      branchWarn, "开始切换");
    if (!ok) return;
    try {
      const r = await App.api.ver_switch(target, targetId || null);
      if (r && r.ok === false) App.toast(r.error || "切换失败", "error", 6000);
    } catch (e) { App.toast("切换失败：" + e.message, "error"); }
  }

  App.pages.versions = {
    init() {
      log = App.makeLogger($("#ver-log"), 800);
      $("#ver-log-clear").addEventListener("click", () => { $("#ver-log").textContent = ""; });
      $("#ver-target-sel").addEventListener("change", (e) => { targetId = e.target.value; refresh(); });
      $("#ver-check").addEventListener("click", doCheck);
      $("#ver-cancel").addEventListener("click", () => App.api.ver_cancel());
      $("#ver-update").addEventListener("click", () => doSwitch({ kind: "latest" }, "更新到所在分支的最新版"));
      $("#ver-apply").addEventListener("click", () => {
        const t = picker && picker.selected();
        if (t) doSwitch(t, "切换到 " + (t.label || t.name || (t.commit || "").slice(0, 7)));
      });
      $("#ver-history").addEventListener("click", (e) => {
        const b = e.target.closest("[data-rollback]");
        if (b) doSwitch({ kind: "commit", commit: b.dataset.rollback }, "回到 " + (b.dataset.label || b.dataset.rollback.slice(0, 7)));
      });
      $("#ver-adopt").addEventListener("click", async () => {
        const t = (info && info.target) || {};
        const ok = await App.confirm("接管为 Git 管理",
          `会在「${t.repo}」里初始化 git，并把源码换成官方仓库的最新版。\n\n` +
          "· 跟官方不一样的源码文件会先完整备份（启动器目录 launcher_data\\version_backup），webui-user.bat 会保留你的设置\n" +
          "· venv / python / 模型 / 插件 / 出图都不会动\n" +
          "· 整合包作者改过源码的话，这些改动会被官方版本替换（备份里还有）\n\n" +
          "接管完成后就能在这里一键更新、选版本、回滚。", "开始接管");
        if (!ok) return;
        try {
          const r = await App.api.ver_adopt(targetId || null);
          if (r && r.ok === false) App.toast(r.error || "接管失败", "error", 6000);
        } catch (e) { App.toast("接管失败：" + e.message, "error"); }
      });

      App.on("ver", "log", (e) => log(e.text));
      App.on("ver", "state", (e) => setBusy(!!e.running));
      App.on("ver", "done", async (e) => {
        setBusy(false);
        if (e.ok) {
          App.toast("版本切换完成", "ok");
          if (e.conflicts && e.conflicts.length && e.backup) {
            const v = await App.modal("有改过的文件没能自动合并",
              App.esc(`这些文件你改过，和新版本的内容冲突，已换成新版本：\n${e.conflicts.slice(0, 15).join("\n")}` +
                `${e.conflicts.length > 15 ? "\n……" : ""}\n\n原来的文件都在备份目录里。`).replace(/\n/g, "<br>"),
              [{ id: "open", label: "打开备份目录", kind: "primary" }, { id: "ok", label: "知道了" }]);
            if (v === "open") App.api.ver_open_backup(e.backup);
          }
        } else if (e.error) {
          App.toast(e.error, "error", 8000);
        }
        refresh();
      });
      // 跟着「环境部署」页一起刷新（本模块没有自己的页面）
      const deploy = App.pages.deploy;
      if (deploy) {
        const orig = deploy.onShow;
        deploy.onShow = function () {
          if (orig) orig.apply(this, arguments);
          if (!busy) refresh();
        };
      }
      refresh();
    },
  };

  /* 从别的页面（实例管理）直接打开某个实例的版本管理 */
  App.openVersions = function (iid) {
    targetId = iid || "";
    App.showPage("deploy");
    // showPage 里的 onShow 已经刷新过一次，这里只负责滚动到版本管理区块
    setTimeout(() => {
      const sec = $("#ver-section");
      if (sec && !sec.hidden) sec.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 80);
  };
})();
