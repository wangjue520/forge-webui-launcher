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

  async function onStart() {
    const target = $("#deploy-target").value.trim();
    const branch = $("#deploy-branch").value;
    const usePortable = $("#deploy-portable").checked;

    let pre;
    try { pre = await App.api.deploy_precheck(target, branch, usePortable); }
    catch (e) { App.toast("检查失败：" + e.message, "error"); return; }

    const issues = pre.issues || [];
    const errors = issues.filter((i) => i.level === "error");
    if (errors.length) {
      await App.modal("无法开始部署", App.esc(errors.map((i) => i.text).join("\n\n")),
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
        App.esc(`即将把 Forge WebUI 部署到：\n${target}\n\n过程中会自动下载便携环境、源码和 torch 等依赖（视网速可能十几分钟以上），期间可以随时取消。`),
        [
          { id: "go", label: "开始部署", kind: "primary" },
          { id: "cancel", label: "取消" },
        ]);
      if (v !== "go") return;
    }

    try {
      const r = await App.api.deploy_start(target, branch, usePortable);
      if (r && r.ok === false) App.toast(r.error || "启动部署失败", "error");
    } catch (e) { App.toast("启动部署失败：" + e.message, "error"); }
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
      sel.value = (App.cfg.webui_branch === "neo2") ? "neo2" : "classic";

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
        // 后端已写回配置：刷新本地状态并跳转到启动页
        try {
          const st = await App.api.get_state();
          App.state = st; App.cfg = st.config || {};
          App.refreshConfigControls();
          App.refreshCmdPreview(st.cmd_args);
        } catch (err) { console.error(err); }
        $("#deploy-target").value = e.target;
        await App.modal("部署完成",
          App.esc(`Forge WebUI 已部署到：\n${e.target}\n\n分支：${e.branch}\n\n现在可以直接去「一键启动」页启动了。`),
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
