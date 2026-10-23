/* trainer.js — LoRA 训练器页：一键部署、一键启动 / 停止、打开训练器界面（后端见 app/lora_trainer.py） */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let st = null;          // trainer_status() 的结果
  let deploying = false;
  let log = null, prog = null, timer = null;

  const RUN_TEXT = { stopped: ["未运行", ""], starting: ["启动中…", "warn"], ready: ["运行中", "ok"] };

  function setStatus(el, text, status) { el.textContent = text; el.dataset.status = status || ""; }

  function render() {
    if (!st) return;
    const i = st.info || {}, run = st.run || {};
    if (document.activeElement !== $("#tr-root")) $("#tr-root").value = st.root || "";
    if (document.activeElement !== $("#tr-port")) $("#tr-port").value = st.port || "";
    $("#tr-torch").value = st.torch || "auto";
    $("#tr-open-browser").checked = st.open_browser !== false;

    setStatus($("#tr-st-app"), i.installed ? `已安装${i.version ? " · V" + i.version : ""}` : "未部署", i.installed ? "ok" : "warn");
    setStatus($("#tr-st-python"), i.python ? "便携 Python 已就绪" : "未准备（部署时自动下载）", i.python ? "ok" : "warn");
    const envOk = i.env && i.engines;
    setStatus($("#tr-st-env"), envOk ? `已安装${i.torch ? " · PyTorch " + i.torch : ""}${i.cuda ? " · CUDA " + i.cuda : ""}`
      : (i.installed ? "训练环境还没装好（点「一键部署」补齐）" : "未安装"), envOk ? "ok" : "warn");
    const [rt, rs] = RUN_TEXT[run.state] || RUN_TEXT.stopped;
    setStatus($("#tr-st-run"), run.state === "ready" ? `${rt} · ${run.url}${run.managed ? "" : "（不是启动器开的，也可以在这里停止）"}` : rt, rs);

    const running = run.state !== "stopped";
    $("#tr-start").disabled = deploying || running;
    $("#tr-stop").disabled = !running;
    $("#tr-open").disabled = run.state !== "ready";
    $("#tr-deploy").disabled = deploying || running;
    $("#tr-deploy-cancel").disabled = !deploying;
    $("#tr-root").disabled = $("#tr-browse").disabled = deploying;
    $("#tr-deploy").lastChild.textContent = i.ready ? "检查并补齐环境" : (i.installed ? "继续部署" : "一键部署");

    const hint = $("#tr-root-hint");
    if (i.installed) setStatus(hint, i.ready ? "这里已经装好了训练器，可以直接启动" : "这里有训练器，但训练环境还没装好：点「一键部署」接着装", i.ready ? "ok" : "warn");
    else if (!i.deployable) setStatus(hint, i.reason || "这个文件夹不能用", "bad");
    else setStatus(hint, i.exists ? "空文件夹（或上次没部署完），可以部署到这里" : "文件夹还不存在，部署时自动创建", "");
    $("#tr-torch-hint").textContent = `这台电脑推荐：${st.torch_recommend === "cu126" ? "CUDA 12.6（老显卡）" : "CUDA 12.8"}。`
      + (i.env ? "训练环境已经装好时选「自动」不会重装 PyTorch；要换版本就选具体的版本再部署。" : "");
  }

  async function refresh() {
    try {
      const r = await App.api.trainer_status();
      if (r && r.ok) { st = r; if (r.deploying !== deploying) { deploying = r.deploying; } render(); }
    } catch (e) { console.error(e); }
  }

  async function save(changes) {
    try {
      const r = await App.api.trainer_set(changes);
      if (r && r.ok === false) { App.toast(r.error, "error"); }
      else if (r) { st = r; render(); }
    } catch (e) { App.toast("保存失败：" + e.message, "error"); }
  }

  async function deploy() {
    const i = (st && st.info) || {};
    if (!i.installed && !(await App.confirm("部署 LoRA 训练器",
      `将把 WWY LoRA 训练器部署到：\n${$("#tr-root").value}\n\n需要下载约 4~5 GB（便携 Python、训练器源码、PyTorch、训练引擎），按网速大约 10~30 分钟。开始吗？`, "开始部署"))) return;
    try {
      const r = await App.api.trainer_deploy($("#tr-root").value.trim(), $("#tr-torch").value);
      if (!r || r.ok === false) { App.toast((r && r.error) || "部署没能开始", "error", 6000); return; }
      deploying = true; prog.set(1, "准备中…"); render();
    } catch (e) { App.toast("部署失败：" + e.message, "error"); }
  }

  async function start() {
    $("#tr-start").disabled = true;
    try {
      const r = await App.api.trainer_start();
      if (!r || r.ok === false) {
        if (r && r.need_deploy) {
          if (await App.confirm("还没有部署训练器", (r.error || "") + "\n\n现在就部署吗？", "一键部署")) deploy();
        } else App.toast((r && r.error) || "启动失败", "error", 6000);
      } else if (r.already) App.toast("训练器已经在运行了", "ok");
      else App.toast("正在启动训练器…", "ok");
    } catch (e) { App.toast("启动失败：" + e.message, "error"); }
    refresh();
  }

  App.pages.trainer = {
    init() {
      log = App.makeLogger($("#tr-log"), 1500);
      prog = App.progress($("#tr-progress"));
      prog.hide();

      $("#tr-start").addEventListener("click", start);
      $("#tr-stop").addEventListener("click", async () => {
        if (!(await App.confirm("停止训练器", "正在训练的任务会被中断（已经保存的中间 LoRA 会保留）。确定停止吗？", "停止", true))) return;
        try { await App.api.trainer_stop(); } catch (e) { App.toast("停止失败：" + e.message, "error"); }
        refresh();
      });
      $("#tr-open").addEventListener("click", async () => {
        const r = await App.api.trainer_open();
        if (r && r.ok === false) App.toast(r.error, "error");
      });
      $("#tr-deploy").addEventListener("click", deploy);
      $("#tr-deploy-cancel").addEventListener("click", async () => {
        if (!(await App.confirm("取消部署", "确定取消吗？已经下载好的部分会保留，下次接着装。", "取消部署", true))) return;
        const r = await App.api.trainer_deploy_cancel();
        if (r && r.ok === false) App.toast(r.error, "error");
      });
      $("#tr-browse").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择 LoRA 训练器的安装目录", $("#tr-root").value);
        if (r && r.path) { $("#tr-root").value = r.path; save({ root: r.path }); }
      });
      $("#tr-root").addEventListener("change", () => save({ root: $("#tr-root").value.trim() }));
      $("#tr-port").addEventListener("change", () => save({ port: $("#tr-port").value.trim() }));
      $("#tr-torch").addEventListener("change", () => save({ torch: $("#tr-torch").value }));
      $("#tr-open-browser").addEventListener("change", () => save({ open_browser: $("#tr-open-browser").checked }));
      $("#tr-open-dir").addEventListener("click", async () => {
        const r = await App.api.trainer_open_dir();
        if (r && r.ok === false) App.toast(r.error, "error");
      });
      $("#tr-log-clear").addEventListener("click", () => { $("#tr-log").textContent = ""; log = App.makeLogger($("#tr-log"), 1500); });

      App.on("trainer", "log", (e) => log((e.text || "").replace(/\n$/, "")));
      App.on("trainer", "state", (e) => { if (st) { st.run = e; render(); } });
      App.on("trainer", "deploy_state", (e) => { deploying = !!e.running; render(); });
      App.on("trainer", "step", (e) => prog.set(e.pct, e.text));
      App.on("trainer", "progress", (e) => {
        if (e.total) prog.set(Math.min(9, 2 + 7 * e.downloaded / e.total), `下载便携环境 ${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}`);
      });
      App.on("trainer", "error", (e) => App.modal(e.title || "出错了", App.esc(e.text || ""), [{ id: "ok", label: "知道了", kind: "primary" }]));
      App.on("trainer", "deploy_done", async (e) => {
        deploying = false;
        await refresh();
        if (e.ok) {
          prog.set(100, "部署完成");
          if (await App.confirm("部署完成", "LoRA 训练器已经装好了，现在启动吗？", "启动训练器")) start();
        } else prog.hide();
      });
      refresh();
    },
    onShow() {
      refresh();
      clearInterval(timer);
      timer = setInterval(refresh, 4000);   // 训练器在别处被关掉 / 自己重启时，状态跟着变
    },
    onHide() { clearInterval(timer); },
  };
})();
