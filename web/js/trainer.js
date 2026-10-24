/* trainer.js — LoRA 训练器的两张卡片（后端见 app/lora_trainer.py）：
   「一键启动」页顶部的小卡片：启动 / 停止 / 打开，日志进运行日志；
   「实例管理」页实例列表下面的一行：和实例一样能换目录、启动 / 停止、移除。
   训练器在「环境部署」页的程序下拉框里部署，已经装好的用「添加已有安装」加进来或自动找到。 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let st = null;          // trainer_status() 的结果
  let timer = null;
  const STATE = { stopped: ["idle", "未启动"], starting: ["starting", "启动中"], ready: ["ready", "运行中"] };

  function view() {
    const i = (st && st.info) || {};
    const run = (st && st.run) || {};
    const on = !!run.state && run.state !== "stopped";
    // 在跑的可能是另一个文件夹里的训练器（比如部署新的那份时，旧的还开着）：显示实际在跑的那份
    const other = on && !!run.root && run.root.toLowerCase() !== ((st && st.root) || "").toLowerCase();
    const [pill, text] = STATE[run.state] || STATE.stopped;
    return {
      i, run, on, other,
      show: !!(i.installed || (st && st.deploying) || on),
      root: other ? run.root : ((st && st.root) || ""),
      version: i.version && !other ? `<em class="ic-cur">V${App.esc(i.version)}</em>` : "",
      pill: `<span class="status-pill" data-state="${pill}"><span class="dot"></span><span>${text}${on && run.port ? ` :${run.port}` : ""}</span></span>`,
    };
  }

  function buttons(v, size) {
    if (st.deploying) return '<span class="hint" style="margin:0">部署中…</span>';
    if (v.on) {
      return `<button class="btn ${size}" data-act="open"${v.run.url ? "" : " disabled"}>打开</button><button class="btn ${size}" data-act="stop">停止</button>`;
    }
    return `<button class="btn ${size} btn-primary" data-act="start">启动</button>`
      + (v.i.ready ? "" : `<button class="btn ${size}" data-act="deploy" title="训练环境（PyTorch / 训练引擎）还没装好">补齐环境</button>`);
  }

  function bind(box) {
    box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => action(b.dataset.act)));
  }

  function renderLaunch(v) {
    const box = $("#launch-trainer");
    box.hidden = !v.show;
    if (box.hidden) return;
    box.innerHTML = `
      <div class="inst-card" data-id="lora-trainer">
        <div class="ic-head">
          <span class="ic-kind" data-kind="trainer">LoRA 训练器</span>
          <b class="ic-name">WWY LoRA 训练器</b>
          ${v.version}
        </div>
        <div class="ic-root" title="${App.esc(v.root)}">${App.esc(v.root)}</div>
        <div class="ic-foot">
          ${v.pill}
          <span class="ic-btns">${buttons(v, "btn-xs")}</span>
        </div>
      </div>`;
    bind(box);
  }

  function renderRow(v) {
    const box = $("#inst-trainer");
    if (!box) return;
    if (!v.show) {
      box.innerHTML = '<div class="hint hint-block">LoRA 训练器：没有找到。已经装过的点下面「添加已有安装」选训练器的文件夹；'
        + "还没装的点「部署新的实例…」，在程序下拉框里选 WWY LoRA 训练器。</div>";
      return;
    }
    const port = v.on && v.run.port ? String(v.run.port) : "";
    box.innerHTML = `
      <div class="card inst-row" data-id="lora-trainer">
        <div class="ir-grid">
          <span class="ic-kind" data-kind="trainer">LoRA 训练器</span>
          <b class="ir-name">WWY LoRA 训练器</b>
          ${v.version}
          ${v.pill}
        </div>
        <div class="form-grid">
          <label>根目录</label>
          <div class="row"><input type="text" value="${App.esc(v.root)}" readonly><button class="btn btn-sm" data-act="root">更换…</button></div>
          <label>端口</label>
          <input type="text" value="${App.esc(port)}" readonly placeholder="用训练器自己「训练器设置」里的端口（默认 28100，被占用会自动换）">
        </div>
        <div class="action-row">
          ${buttons(v, "btn-sm")}
          <button class="btn btn-sm btn-danger-text" data-act="remove">移除</button>
          <span class="hint">移除只是从启动器里去掉，不会删除任何文件</span>
        </div>
      </div>`;
    bind(box);
  }

  function render() {
    const v = view();
    renderLaunch(v);
    renderRow(v);
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
      if (act === "root") {
        const c = await App.api.choose_directory("选择 WWY LoRA 训练器的文件夹", (st && st.root) || "");
        if (!c || !c.ok || !c.path) return;
        const r = await App.api.trainer_set_root(c.path);
        if (r && r.ok === false) App.toast(r.error, "error", 6000);
        else App.toast("已更换训练器目录", "ok");
      }
      if (act === "remove") {
        if (!(await App.confirm("移除 LoRA 训练器", `从启动器里移除训练器？\n\n只是不再管理它，${(st && st.root) || "训练器文件夹"} 里的文件一个都不会删。之后要用再点「添加已有安装」选它的文件夹。`, "移除", true))) return;
        const r = await App.api.trainer_remove();
        if (r && r.ok === false) { App.toast(r.error, "error"); return; }
        App.toast("已移除", "ok");
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

  // 不是页面，只是挂在「一键启动」和「实例管理」页上的卡片；放进 App.pages 只为了随启动器一起 init
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
      timer = setInterval(() => {
        if ((App.currentPage === "launch" || App.currentPage === "instances") && !document.hidden) refresh();
      }, 5000);
    },
  };
})();
