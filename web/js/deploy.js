/* deploy.js — 环境部署页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let log = null;
  let prog = null;
  let running = false;

  function setRunning(v) {
    running = v;
    $("#deploy-start").disabled = v;
    $("#deploy-cancel").disabled = !v;
  }

  async function envDetect() {
    const g = $("#deploy-git-status"), p = $("#deploy-python-status");
    g.textContent = "检测中…"; p.textContent = "检测中…";
    g.dataset.status = p.dataset.status = "";
    try {
      const r = await App.api.deploy_env_detect();
      g.textContent = r.git.text; g.dataset.status = r.git.found ? "ok" : "bad";
      p.textContent = r.python.text; p.dataset.status = r.python.found ? "ok" : "bad";
    } catch (e) {
      g.textContent = p.textContent = "检测失败：" + e.message;
      g.dataset.status = p.dataset.status = "bad";
    }
  }

  async function checkDir() {
    const el = $("#deploy-dir-status");
    const target = $("#deploy-target").value;
    try {
      const r = await App.api.deploy_check_dir(target);
      el.textContent = r.message;
      el.dataset.status = r.status || "";
    } catch (e) { el.textContent = "检测失败：" + e.message; el.dataset.status = "bad"; }
  }

  /* ---------- 随 ComfyUI 一起装的节点 ---------- */
  let nodeCatalogLoaded = false;
  async function loadNodeCatalog() {
    if (nodeCatalogLoaded || !App.api.comfy_node_catalog) return;
    try {
      const r = await App.api.comfy_node_catalog();
      if (!r || r.ok === false) return;
      const sel = new Set(r.selected || []);
      const row = (it) => `<label class="chk-row"><input type="checkbox" data-node="${App.esc(it.id)}"${sel.has(it.id) ? " checked" : ""}><i></i>` +
        `<span><span class="ext-name">${App.esc(it.name)}</span><div class="ext-desc">${App.esc(it.desc)}</div></span></label>`;
      const groups = r.groups || {};
      const core = r.items.filter((i) => i.group === "core");
      const rest = r.items.filter((i) => i.group !== "core");
      $("#deploy-nodes").innerHTML = core.map(row).join("");
      let html = "", g = null;
      rest.forEach((it) => {
        if (it.group !== g) { g = it.group; html += `<div class="deploy-nodes-group">${App.esc(groups[g] || g)}</div>`; }
        html += row(it);
      });
      $("#deploy-nodes-more").innerHTML = html;
      // 上次勾过「更多」里的节点就默认展开，免得用户以为没装
      if (rest.some((i) => sel.has(i.id))) $(".deploy-nodes-more").open = true;
      nodeCatalogLoaded = true;
    } catch (e) { console.error(e); }
  }
  function syncNodesCard() {
    const comfy = $("#deploy-branch").value === "comfyui";
    $("#deploy-nodes-card").hidden = !comfy;
    // 「环境诊断」按 webui.bat/venv 结构检测，是 Forge 专属逻辑，ComfyUI 部署时用不上
    $("#deploy-diag-card").hidden = comfy;
    if (comfy) loadNodeCatalog();
  }
  function selectedNodes() {
    if (!nodeCatalogLoaded) return null;   // 没加载出来就交给后端用默认推荐
    return Array.from(document.querySelectorAll("#deploy-nodes-card input[data-node]:checked")).map((el) => el.dataset.node);
  }

  async function onStart() {
    if (running) return;
    // 整个函数包一层兜底：预检查之后的任何异常（弹窗、返回值解析等）都必须
    // 给出可见反馈，不能让按钮"点了没反应"
    $("#deploy-start").disabled = true;
    try {
      const target = $("#deploy-target").value.trim();
      const branch = $("#deploy-branch").value;
      const usePortable = $("#deploy-portable").checked;

      let pre;
      try { pre = await App.api.deploy_precheck(target, branch, usePortable); }
      catch (e) { App.toast("检查失败：" + e.message, "error"); return; }
      if (!pre || typeof pre !== "object") {
        App.toast("检查失败：后端未返回有效结果", "error");
        return;
      }

      const issues = Array.isArray(pre.issues) ? pre.issues : [];
      const errors = issues.filter((i) => i.level === "error");
      if (errors.length || pre.ok === false) {
        const text = errors.length
          ? errors.map((i) => i.text).join("\n\n")
          : "预检查未通过，请检查安装目录和网络后重试。";
        await App.modal("无法开始部署", App.esc(text),
          [{ id: "ok", label: "知道了", kind: "primary" }]);
        return;
      }
      const warns = issues.filter((i) => i.level === "warn");
      if (warns.length) {
        const v = await App.modal("部署前提醒", App.esc(warns.map((i) => i.text).join("\n\n")), [
          { id: "go", label: "仍然继续部署", kind: "primary" },
          { id: "cancel", label: "取消" },
        ]);
        if (v !== "go") return;
      } else if (!pre.already_installed) {
        const v = await App.modal("确认开始部署",
          App.esc(`即将把 ${branch === "comfyui" ? "ComfyUI" : "Forge WebUI"} 部署到：\n${target}\n\n过程中会自动下载便携环境、源码和 torch 等依赖（视网速可能十几分钟以上），期间可以随时取消。` +
            (branch === "comfyui" && (selectedNodes() || []).length ? `\n\n另外会安装勾选的 ${selectedNodes().length} 个常用节点及其依赖。` : "")),
          [
            { id: "go", label: "开始部署", kind: "primary" },
            { id: "cancel", label: "取消" },
          ]);
        if (v !== "go") return;
      }

      try {
        const r = branch === "comfyui"
          ? await App.api.deploy_start(target, branch, usePortable, selectedNodes())
          : await App.api.deploy_start(target, branch, usePortable);
        if (r && r.ok === false) App.toast(r.error || "启动部署失败", "error");
      } catch (e) { App.toast("启动部署失败：" + e.message, "error"); }
    } catch (e) {
      console.error(e);
      App.toast("操作失败：" + ((e && e.message) || e), "error");
    } finally {
      if (!running) $("#deploy-start").disabled = false;
    }
  }

  async function venvCheck() {
    const target = $("#deploy-target").value.trim();
    const branch = $("#deploy-branch").value;
    const out = $("#deploy-diag-result");
    out.textContent = "检测中…"; out.dataset.status = "";
    try {
      const r = await App.api.deploy_venv_check(target, branch);
      if (!r.ok) { out.textContent = r.error; out.dataset.status = "bad"; return; }
      if (!r.mismatch) {
        out.textContent = r.detail || "venv 版本正常，无需处理。";
        out.dataset.status = "ok";
        return;
      }
      // 版本不一致：询问是否删除重建
      const v = await App.modal("检测到 venv 版本不一致", App.esc(r.detail), [
        { id: "del", label: "删除 venv（下次启动自动重建）", kind: "danger" },
        { id: "keep", label: "先不管" },
      ]);
      if (v === "del") {
        const d = await App.api.deploy_venv_delete(target);
        if (d && d.ok) { out.textContent = "已删除 venv，下次启动时会自动重建。"; out.dataset.status = "ok"; }
        else { out.textContent = "删除失败"; out.dataset.status = "bad"; }
      } else {
        out.textContent = r.detail; out.dataset.status = "warn";
      }
    } catch (e) { out.textContent = "检测失败：" + e.message; out.dataset.status = "bad"; }
  }

  async function patchHashlib() {
    const target = $("#deploy-target").value.trim();
    const out = $("#deploy-diag-result");
    out.textContent = "写入中…"; out.dataset.status = "";
    try {
      const r = await App.api.deploy_patch_hashlib(target);
      if (r.ok) { out.textContent = r.message; out.dataset.status = "ok"; }
      else { out.textContent = r.error; out.dataset.status = "bad"; }
    } catch (e) { out.textContent = "失败：" + e.message; out.dataset.status = "bad"; }
  }

  App.pages.deploy = {
    init(state) {
      log = App.makeLogger($("#deploy-log"));
      prog = App.progress($("#deploy-progress"));
      prog.hide();

      const sel = $("#deploy-branch");
      (state.deploy_branches || []).forEach((b) => {
        const o = document.createElement("option");
        o.value = b.key; o.textContent = b.label;
        sel.appendChild(o);
      });
      sel.value = ["classic", "comfyui"].includes(App.cfg.webui_branch) ? App.cfg.webui_branch : "neo2";
      sel.addEventListener("change", syncNodesCard);
      syncNodesCard();

      if (App.cfg.webui_root) {
        $("#deploy-target").value = App.cfg.webui_root;
        checkDir();
      }

      $("#deploy-redetect").addEventListener("click", envDetect);
      $("#deploy-browse").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择安装目录", $("#deploy-target").value);
        if (r && r.ok && r.path) { $("#deploy-target").value = r.path; checkDir(); }
      });
      $("#deploy-check").addEventListener("click", checkDir);
      $("#deploy-target").addEventListener("change", checkDir);
      $("#deploy-start").addEventListener("click", onStart);
      $("#deploy-cancel").addEventListener("click", () => App.api.deploy_cancel());
      $("#deploy-log-clear").addEventListener("click", () => { $("#deploy-log").textContent = ""; });
      $("#deploy-venv-check").addEventListener("click", venvCheck);
      $("#deploy-patch-hashlib").addEventListener("click", patchHashlib);

      envDetect();

      App.on("deploy", "log", (e) => log(e.text));
      App.on("deploy", "progress", (e) => {
        if (!e.total) { prog.hide(); return; }  // 总量未知/下载间过渡时不显示假进度
        const pct = e.downloaded / e.total * 100;
        prog.set(pct, `${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}（${pct.toFixed(1)}%）`);
      });
      App.on("deploy", "state", (e) => {
        setRunning(!!e.running);
        if (e.running) { prog.hide(); prog.set(0, ""); }
      });
      App.on("deploy", "ask", async (e) => {
        const v = await App.modal(e.title, App.esc(e.body), e.buttons || [{ id: "ok", label: "确定", kind: "primary" }]);
        try { await App.api.answer(e.ask_id, v); } catch (err) { console.error(err); }
      });
      App.on("deploy", "done", async (e) => {
        setRunning(false);
        prog.hide();
        App.toast(`部署完成：${e.target}`, "ok", 5000);
        // 后端已把部署结果登记成实例（当前实例 或 新实例）
        let st = null;
        try {
          st = await App.api.get_state();
          App.state = st; App.cfg = st.config || {};
          App.refreshConfigControls();
          App.refreshCmdPreview(st.cmd_args);
          if (App.applyChrome) App.applyChrome(st);
        } catch (err) { console.error(err); }
        $("#deploy-target").value = e.target;
        const name = e.branch === "comfyui" ? "ComfyUI" : "Forge WebUI";
        const other = st && st.instances && st.instances.instances.find((i) =>
          i.root && i.root.toLowerCase() === String(e.target).toLowerCase() && !i.active);
        if (other) {
          const v = await App.modal("部署完成",
            App.esc(`${name} 已部署到：\n${e.target}\n\n它被添加为新实例「${other.name}」。要切换过去并启动吗？`),
            [{ id: "go", label: "切换到新实例", kind: "primary" }, { id: "stay", label: "留在这里" }]);
          if (v === "go" && App.switchInstance) {
            try { sessionStorage.setItem("return-page", "launch"); } catch (err) {}
            App.switchInstance(other.id);
          }
          return;
        }
        await App.modal("部署完成",
          App.esc(`${name} 已部署到：\n${e.target}\n\n现在可以直接去「一键启动」页启动了。`),
          [{ id: "go", label: "去一键启动", kind: "primary" }, { id: "stay", label: "留在这里" }]
        ).then((v) => { if (v === "go") App.showPage("launch"); });
      });
      App.on("deploy", "cancelled", () => { setRunning(false); prog.hide(); App.toast("已取消部署"); });
      App.on("deploy", "error", (e) => {
        setRunning(false); prog.hide();
        App.modal(e.title || "部署出错", App.esc(e.text || ""), [{ id: "ok", label: "知道了", kind: "primary" }]);
      });
    },
  };
})();
