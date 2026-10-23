/* trainer.js — 「一键启动」页上的 LoRA 训练器卡片：启动 / 停止 / 打开，日志进运行日志（后端见 app/lora_trainer.py）
   训练器在「环境部署」页的程序下拉框里部署；部署好之前这张卡片不出现。 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let st = null;          // trainer_status() 的结果
  let timer = null;
  const STATE = { stopped: ["idle", "未启动"], starting: ["starting", "启动中"], ready: ["ready", "运行中"] };

  function render() {
    const box = $("#launch-trainer");
    const i = (st && st.info) || {};
    box.hidden = !(i.installed || (st && st.deploying));
    if (box.hidden) return;
    const run = st.run || {};
    const [pill, text] = STATE[run.state] || STATE.stopped;
    const port = run.state !== "stopped" && run.port ? ` :${run.port}` : "";
    let btns;
    if (st.deploying) btns = '<span class="hint" style="margin:0">部署中…</span>';
    else if (run.state !== "stopped") {
      btns = `<button class="btn btn-xs" data-act="open"${run.url ? "" : " disabled"}>打开</button><button class="btn btn-xs" data-act="stop">停止</button>`;
    } else {
      btns = '<button class="btn btn-xs btn-primary" data-act="start">启动</button>'
        + (i.ready ? "" : '<button class="btn btn-xs" data-act="deploy" title="训练环境（PyTorch / 训练引擎）还没装好">补齐环境</button>');
    }
    box.innerHTML = `
      <div class="inst-card" data-id="lora-trainer">
        <div class="ic-head">
          <span class="ic-kind" data-kind="trainer">LoRA 训练器</span>
          <b class="ic-name">WWY LoRA 训练器</b>
          ${i.version ? `<em class="ic-cur">V${App.esc(i.version)}</em>` : ""}
        </div>
        <div class="ic-root" title="${App.esc(st.root)}">${App.esc(st.root)}</div>
        <div class="ic-foot">
          <span class="status-pill" data-state="${pill}"><span class="dot"></span><span>${text}${port}</span></span>
          <span class="ic-btns">${btns}</span>
        </div>
      </div>`;
    box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => action(b.dataset.act)));
  }

  async function refresh() {
    try {
      const r = await App.api.trainer_status();
      if (r && r.ok) { st = r; render(); }
    } catch (e) { console.error(e); }
  }

  async function action(act) {
    try {
      if (act === "deploy") {
        App.showPage("deploy");
        if (App.pages.deploy.selectTrainer) App.pages.deploy.selectTrainer();
        return;
      }
      if (act === "open") {
        const r = await App.api.trainer_open();
        if (r && r.ok === false) App.toast(r.error, "error");
        return;
      }
      if (act === "stop") {
        if (!(await App.confirm("停止 LoRA 训练器", "正在训练的任务会被中断（已经保存的中间 LoRA 会保留）。确定停止吗？", "停止", true))) return;
        await App.api.trainer_stop();
      }
      if (act === "start") {
        st.run = Object.assign({}, st.run, { state: "starting" });   // 先给反馈，后端状态事件随后到
        render();
        const r = await App.api.trainer_start();
        if (!r || r.ok === false) App.toast((r && r.error) || "启动失败", "error", 6000);
        else if (r.already) App.toast("训练器已经在运行了", "ok");
      }
    } catch (e) { App.toast("操作失败：" + e.message, "error"); }
    refresh();
  }

  // 不是页面，只是挂在「一键启动」页上的一张卡片；放进 App.pages 只为了随启动器一起 init
  App.pages.trainercard = {
    init() {
      App.on("trainer", "log", (e) => {
        const text = (e.text || "").replace(/\n$/, "");
        if (App.launchLog) App.launchLog("trainer", "LoRA 训练器", text);
      });
      App.on("trainer", "state", (e) => { if (st) { st.run = e; render(); } else refresh(); });
      App.on("trainer", "changed", refresh);
      refresh();
      // 训练器自己点「重启训练器」、或者在启动器外面被关掉时，卡片状态跟着变
      clearInterval(timer);
      timer = setInterval(() => { if (App.currentPage === "launch" && !document.hidden) refresh(); }, 5000);
    },
  };
})();
