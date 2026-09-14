/* launch.js — 一键启动页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let log = null;
  let launchUrl = null;

  function setStatus(state, text) {
    const pill = $("#launch-status");
    pill.dataset.state = state;
    $("#launch-status-text").textContent = text;
    App.setGlobalStatus(state, state === "idle" ? "WebUI 未启动" : text);
  }

  function setRunningUI(running) {
    $("#launch-start").disabled = running;
    $("#launch-stop").disabled = !running;
    $("#launch-open-browser").disabled = !launchUrl;
  }

  async function refreshEnvHint() {
    const root = App.cfg.webui_root || "";
    const hint = $("#launch-env-hint");
    if (!root.trim()) { hint.textContent = ""; return; }
    try {
      const r = await App.api.launch_env_detect(root);
      const parts = [r.python, r.git].filter(Boolean);
      hint.textContent = parts.join("　|　");
    } catch (e) { console.error(e); }
  }

  async function onStart() {
    const root = ($("#launch-root").value || "").trim();

    let pre;
    try { pre = await App.api.launch_precheck(root); }
    catch (e) { App.toast("启动前检查失败：" + e.message, "error"); return; }

    let fixOverrides = false, killPid = 0;

    const errors = (pre.issues || []).filter((i) => i.level === "error");
    if (errors.length) {
      await App.modal("无法启动", App.esc(errors.map((i) => i.text).join("\n\n")),
        [{ id: "ok", label: "知道了", kind: "primary" }]);
      return;
    }

    for (const issue of (pre.issues || []).filter((i) => i.level === "warn")) {
      if (issue.id === "user_bat_overrides") {
        const v = await App.modal("检测到配置冲突", App.esc(issue.text), [
          { id: "fix", label: "清空并启动", kind: "primary" },
          { id: "anyway", label: "保持原样直接启动" },
          { id: "cancel", label: "取消" },
        ]);
        if (v === "cancel") return;
        if (v === "fix") fixOverrides = true;
      } else if (issue.id === "port_occupied") {
        const v = await App.modal("检测到残留进程", App.esc(issue.text), [
          { id: "kill", label: "结束并启动", kind: "danger" },
          { id: "anyway", label: "直接启动（自动换端口）" },
          { id: "cancel", label: "取消" },
        ]);
        if (v === "cancel") return;
        if (v === "kill") killPid = issue.pids || issue.pid || 0;
      } else {
        // 通用警告（中文/空格路径等）：确认后继续
        const v = await App.modal("启动前提醒", App.esc(issue.text), [
          { id: "go", label: "仍然启动", kind: "primary" },
          { id: "cancel", label: "取消" },
        ]);
        if (v !== "go") return;
      }
    }

    try {
      const r = await App.api.launch_start(root, fixOverrides, killPid);
      if (r && r.ok === false) App.toast(r.error || "启动失败", "error");
    } catch (e) { App.toast("启动失败：" + e.message, "error"); }
  }

  async function onStop() {
    try { await App.api.launch_stop(); }
    catch (e) { App.toast("停止失败：" + e.message, "error"); }
  }

  // 出图目录的实际位置是从 WebUI 的 config.json 算出来的（官方默认 outputs，
  // 也有整合包/改过设置的是 output 或别的盘）。这里顺带按实际情况决定
  // 「当天」那两个按钮要不要显示 —— 没开「按日期分目录」的话它们没有意义。
  async function refreshOutputButtons() {
    try {
      const r = await App.api.output_dirs_info();
      if (!r || !r.ok) return;
      $$("[data-open-output]").forEach((btn) => {
        const isToday = btn.dataset.openOutput.endsWith("_today");
        btn.style.display = (isToday && !r.date_subdir) ? "none" : "";
      });
      const hint = $("#launch-output-hint");
      if (hint) {
        hint.textContent = r.exists && r.exists.txt2img
          ? r.txt2img
          : `${r.txt2img}（还没跑出过图，目录尚未创建）`;
      }
    } catch (e) { /* 拿不到就保持原样，不影响按钮可用 */ }
  }

  // 输出目录按钮组（输出目录/文生图/当天文生图/图生图/当天图生图）
  $$("[data-open-output]").forEach((btn) => btn.addEventListener("click", async () => {
    try {
      const r = await App.api.open_output_folder(btn.dataset.openOutput);
      if (r && r.ok === false) App.toast(r.error || "无法打开目录", "error", 5000);
    } catch (e) { App.toast("无法打开目录：" + e.message, "error"); }
  }));

  App.pages.launch = {
    init(state) {
      log = App.makeLogger($("#launch-log"));

      $("#launch-browse").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择 WebUI 根目录", App.cfg.webui_root || "");
        if (r && r.ok && r.path) {
          $("#launch-root").value = r.path;
          $("#launch-root").dispatchEvent(new Event("change"));
          refreshEnvHint();
          refreshOutputButtons();
        }
      });

      $("#launch-start").addEventListener("click", onStart);
      $("#launch-stop").addEventListener("click", onStop);
      $("#launch-log-clear").addEventListener("click", () => { $("#launch-log").textContent = ""; });
      $("#launch-open-browser").addEventListener("click", () => {
        if (launchUrl) App.api.open_url(launchUrl);
      });
      $("#launch-root").addEventListener("change", () => { refreshEnvHint(); refreshOutputButtons(); });

      // 恢复状态（比如重载页面时 WebUI 还在跑）
      const ls = state.launch || {};
      launchUrl = ls.url || null;
      setStatus(ls.state || "idle", ls.text || "尚未启动");
      setRunningUI(!!ls.running);
      if (App.cfg.webui_root) { refreshEnvHint(); refreshOutputButtons(); }

      App.on("launch", "log", (e) => log(e.text));
      App.on("launch", "status", (e) => {
        launchUrl = e.url || null;
        setStatus(e.state, e.text);
        $("#launch-open-browser").disabled = !launchUrl;
      });
      App.on("launch", "state", (e) => setRunningUI(!!e.running));
    },

    onShow() {
      // 每次切回启动页都同步一次状态，防止错过事件
      App.api.launch_status().then((ls) => {
        if (!ls) return;
        launchUrl = ls.url || null;
        setStatus(ls.state || "idle", ls.text || "尚未启动");
        setRunningUI(!!ls.running);
      }).catch(() => {});
    },
  };
})();
