/* diagnose.js — 报错诊断面板（后端 app/diagnose.py 规则 + app/diagnose_llm.py AI）
   入口：一键启动页日志框上的「诊断」（诊断实例）、启动器设置里的「诊断启动器」、Ctrl+Shift+D、
   启动器报错后后端自动推送（diag/auto）。面板是右侧抽屉，不挡操作。 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s, r) => (r || document).querySelector(s);
  const esc = (s) => App.esc(s);

  let target = null;       // 当前诊断对象 {type: "inst", iid} / {type: "launcher"}
  let report = null;       // 最近一次规则诊断结果
  let job = "";            // 正在跑的 AI 任务
  let aiSteps = [];
  let aiResult = null;
  let aiError = "";
  const badges = {};       // iid -> 发现的问题数

  const RISK_TIP = { low: "可撤销", confirm: "需要确认", danger: "需要确认", ui: "" };

  function sameTarget(a, b) {
    return a && b && a.type === b.type && (a.type === "launcher" || a.iid === b.iid);
  }

  /* ---------- 抽屉 ---------- */
  function drawer() { return $("#diag-drawer"); }

  async function fillTargets() {
    const sel = $("#dg-target");
    let info = null;
    try { info = await App.api.diag_info(); } catch (e) { /* 拿不到就只放当前对象 */ }
    const list = (info && info.targets) || [];
    sel.innerHTML = list.map((t, i) =>
      `<option value="${i}">${esc(t.type === "launcher" ? "启动器本身" : "实例：" + t.name)}</option>`).join("");
    sel._targets = list;
    const idx = list.findIndex((t) => sameTarget(t, target));
    if (idx >= 0) sel.value = String(idx);
  }

  async function open(t, preset) {
    target = t || { type: "inst", iid: (App.instances && App.instances.active) || App.cfg.active_instance };
    drawer().hidden = false;
    document.body.classList.add("diag-open");
    if (target.type === "inst" && target.iid) { badges[target.iid] = 0; renderBadge(); }
    fillTargets();
    if (preset) { report = preset; render(); return; }
    await run();
  }

  function close() {
    drawer().hidden = true;
    document.body.classList.remove("diag-open");
  }

  async function run() {
    report = null;
    if (!job) { aiSteps = []; aiResult = null; aiError = ""; }
    $("#dg-findings").innerHTML = `<div class="dg-empty">正在分析日志…</div>`;
    renderAi();
    try { report = await App.api.diag_run(target); }
    catch (e) { report = { ok: false, error: e.message }; }
    render();
  }

  /* ---------- 渲染 ---------- */
  function render() {
    const box = $("#dg-findings");
    if (!report || report.ok === false) {
      box.innerHTML = `<div class="dg-empty dg-bad">${esc((report && report.error) || "诊断失败")}</div>`;
      renderAi();
      return;
    }
    $("#dg-sub").textContent = `${report.name} · 看了 ${report.lines} 行日志`;
    const fs = report.findings || [];
    if (!fs.length) {
      box.innerHTML = `<div class="dg-empty">${report.lines ? "本地规则没认出已知的报错。" :
        "还没有日志：先启动一次，出错后再来诊断。"}${report.lines ? "可以让 AI 结合日志和本地文件再看看。" : ""}</div>`;
    } else {
      box.innerHTML = "";
      fs.forEach((f) => box.appendChild(findingCard(f)));
    }
    renderAi();
  }

  function findingCard(f) {
    const el = document.createElement("div");
    el.className = "dg-card";
    el.dataset.level = f.level || "error";
    el.innerHTML = `<div class="dg-card-title">${esc(f.title)}</div>
      <div class="dg-card-text">${esc(f.text)}</div>
      ${f.evidence ? `<div class="dg-evidence">${esc(f.evidence)}</div>` : ""}
      <div class="dg-actions"></div>`;
    const acts = $(".dg-actions", el);
    (f.actions || []).forEach((a) => acts.appendChild(actionEl(a)));
    if (!(f.actions || []).length) acts.remove();
    return el;
  }

  function actionEl(a) {
    const wrap = document.createElement("span");
    wrap.className = "dg-act";
    if (a.applied && !a.applied.undone) {
      wrap.innerHTML = `<span class="dg-applied">✓ 已自动处理：${esc(a.applied.text)}</span>`;
      if (a.applied.undo) wrap.appendChild(undoBtn(a.applied.undo, wrap));
      return wrap;
    }
    const btn = document.createElement("button");
    btn.className = "btn btn-xs" + (a.risk === "danger" ? " btn-danger" : (a.risk === "ui" ? "" : " btn-accent"));
    btn.textContent = a.label;
    if (RISK_TIP[a.risk]) btn.title = RISK_TIP[a.risk];
    btn.addEventListener("click", () => doAction(a, btn, wrap));
    wrap.appendChild(btn);
    if (a.error) {
      const e = document.createElement("span");
      e.className = "dg-act-err";
      e.textContent = "自动处理失败：" + a.error;
      wrap.appendChild(e);
    }
    return wrap;
  }

  function undoBtn(token, wrap) {
    const u = document.createElement("button");
    u.className = "btn btn-xs";
    u.textContent = "撤销";
    u.addEventListener("click", async () => {
      u.disabled = true;
      const r = await App.api.diag_undo(token);
      if (r && r.ok) {
        App.toast("已撤销", "ok");
        wrap.innerHTML = `<span class="dg-applied dg-undone">已撤销</span>`;
      } else {
        App.toast((r && r.error) || "撤销失败", "error");
        u.disabled = false;
      }
    });
    return u;
  }

  function uiAction(a) {
    const p = a.params || {};
    if (a.aid === "open_page") { close(); App.showPage(p.page); return; }
    if (a.aid === "open_url") { App.api.open_url(p.url); return; }
    if (a.aid === "civitai_redownload") {
      close();
      App.showPage("civitai");
      const inp = $("#cv-url");
      if (inp) { inp.value = p.url; const go = $("#cv-fetch"); if (go) go.click(); }
    }
  }

  async function doAction(a, btn, wrap) {
    if (a.risk === "ui") { uiAction(a); return; }
    if (a.risk === "confirm" || a.risk === "danger") {
      const ok = await App.confirm(a.risk === "danger" ? "确认操作" : "确认修复",
        a.label + (a.risk === "danger" ? "\n\n文件会移到回收站，需要时可以从回收站还原。" : ""),
        a.risk === "danger" ? "移到回收站" : "执行", a.risk === "danger");
      if (!ok) return;
    }
    btn.disabled = true;
    let r;
    try { r = await App.api.diag_fix(a.aid, a.params || {}); }
    catch (e) { r = { ok: false, error: e.message }; }
    if (!r || !r.ok) {
      btn.disabled = false;
      App.toast((r && r.error) || "执行失败", "error", 6000);
      return;
    }
    if (r.ui) { uiAction({ ...a, params: r.params }); btn.disabled = false; return; }
    App.toast(r.text || "已完成", "ok", 5000);
    wrap.innerHTML = `<span class="dg-applied">✓ ${esc(r.text || "已完成")}</span>`;
    if (r.undo) wrap.appendChild(undoBtn(r.undo, wrap));
    if (r.page) { const pg = r.page; setTimeout(() => { close(); App.showPage(pg); }, 600); }
  }

  /* ---------- AI ---------- */
  function renderAi() {
    const box = $("#dg-ai");
    const ready = report ? report.ai_ready : true;
    $("#dg-ai-model").textContent = report && report.ai_model ? report.ai_model : "";
    if (!ready && !job && !aiResult) {
      box.innerHTML = `<div class="dg-empty">还没设置 AI。到「启动器设置 → 报错诊断」选一个模型、填上自己的 API Key
        （默认 DeepSeek，国内直连，诊断一次大约几分钱）。</div>
        <div class="dg-actions"><button class="btn btn-xs btn-accent" id="dg-ai-setup">去设置</button></div>`;
      $("#dg-ai-setup").addEventListener("click", () => {
        close(); App.showPage("launcher");
        const c = $("#diag-settings-card"); if (c) c.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      return;
    }
    let html = "";
    if (job || aiSteps.length) {
      html += `<ol class="dg-steps">${aiSteps.map((s) => `<li>${esc(s)}</li>`).join("")}${job ? `<li class="dg-running">思考中…</li>` : ""}</ol>`;
    }
    if (aiError) html += `<div class="dg-empty dg-bad">${esc(aiError)}</div>`;
    box.innerHTML = html;
    if (aiResult) box.appendChild(aiCard(aiResult));
    const row = document.createElement("div");
    row.className = "dg-actions";
    if (job) {
      row.innerHTML = `<button class="btn btn-xs" id="dg-ai-stop">停止</button>`;
      $("#dg-ai-stop", row).addEventListener("click", () => App.api.diag_ai_cancel(job));
    } else {
      const has = report && (report.findings || []).length;
      row.innerHTML = `<button class="btn btn-xs btn-accent" id="dg-ai-go">${aiResult ? "让 AI 重新分析" :
        (has ? "让 AI 再深入看看" : "让 AI 分析")}</button>
        <span class="hint">会把脱敏后的日志片段和环境信息发给所选模型，AI 只能读文件、不能改</span>`;
      $("#dg-ai-go", row).addEventListener("click", startAi);
    }
    box.appendChild(row);
  }

  function aiCard(res) {
    const el = document.createElement("div");
    el.className = "dg-card dg-ai-card";
    const conf = { high: "把握较大", medium: "有一定把握", low: "把握不大" }[res.confidence] || "";
    el.innerHTML = `<div class="dg-card-title">${esc(res.summary || "AI 诊断结果")}${conf ? `<em class="dg-conf">${conf}</em>` : ""}</div>
      ${res.cause ? `<div class="dg-card-text">${esc(res.cause)}</div>` : ""}
      ${(res.steps || []).length ? `<ol class="dg-todo">${res.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>` : ""}
      <div class="dg-actions"></div>
      ${(res.dropped || []).length ? `<div class="hint">AI 还推荐了几个启动器做不了 / 不安全的操作，已忽略：${esc(res.dropped.join("；"))}</div>` : ""}`;
    const acts = $(".dg-actions", el);
    (res.actions || []).forEach((a) => acts.appendChild(actionEl(a)));
    if (!(res.actions || []).length) acts.remove();
    return el;
  }

  async function startAi() {
    aiSteps = []; aiResult = null; aiError = "";
    let r;
    try { r = await App.api.diag_ai(target); } catch (e) { r = { ok: false, error: e.message }; }
    if (!r || !r.ok) { aiError = (r && r.error) || "启动 AI 诊断失败"; renderAi(); return; }
    job = r.job;
    renderAi();
  }

  /* ---------- 报告 ---------- */
  function reportText() {
    const out = [`# WWY 启动器诊断报告（${report ? report.name : ""}）`];
    ((report && report.findings) || []).forEach((f) => {
      out.push(`\n## ${f.title}\n${f.text}`);
      if (f.evidence) out.push(`证据：${f.evidence}`);
    });
    if (aiResult) {
      out.push(`\n## AI：${aiResult.summary}\n${aiResult.cause || ""}`);
      (aiResult.steps || []).forEach((s, i) => out.push(`${i + 1}. ${s}`));
    }
    return out.join("\n");
  }

  function renderBadge() {
    const b = $("#launch-diag-badge");
    if (!b) return;
    const multi = App.instances && App.instances.multi;
    const active = (App.instances && App.instances.active) || App.cfg.active_instance;
    const n = multi || !active ? Object.values(badges).reduce((a, x) => a + (x || 0), 0) : (badges[active] || 0);
    b.hidden = !n;
    b.textContent = n;
  }

  /* ---------- 设置卡片 ---------- */
  let providers = [];
  function refreshProviderHints() {
    const pid = $("#dg-provider").value;
    const p = providers.find((x) => x.id === pid) || {};
    $("#dg-model").placeholder = p.model ? `留空用默认：${p.model}` : "必填，例如 deepseek-v4-flash";
    $("#dg-base").placeholder = p.base ? `留空用默认：${p.base}` : "必填，例如 https://api.xxx.com/v1";
    $("#dg-format-row").hidden = $("#dg-format-row-label").hidden = pid !== "custom";
    $("#dg-key-link").hidden = !p.key_url;
  }

  async function initSettings() {
    try { const info = await App.api.diag_info(); providers = (info && info.providers) || []; } catch (e) { providers = []; }
    if (providers.length) {
      const sel = $("#dg-provider");
      sel.innerHTML = providers.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("");
      sel.value = App.cfg.diag_llm_provider || "deepseek";
    }
    $("#dg-provider").addEventListener("change", refreshProviderHints);
    refreshProviderHints();
    $("#dg-key-link").addEventListener("click", () => {
      const p = providers.find((x) => x.id === $("#dg-provider").value);
      if (p && p.key_url) App.api.open_url(p.key_url);
    });
    $("#dg-test").addEventListener("click", async () => {
      const btn = $("#dg-test"), out = $("#dg-test-result");
      btn.disabled = true; out.hidden = false; out.textContent = "正在测试…"; out.dataset.ok = "";
      const settings = {};
      ["diag_llm_provider", "diag_llm_api_key", "diag_llm_model", "diag_llm_base_url", "diag_llm_format"].forEach((k) => {
        const el = document.querySelector(`[data-cfg="${k}"]`); if (el) settings[k] = el.value;
      });
      let r;
      try { r = await App.api.diag_llm_test(settings); } catch (e) { r = { ok: false, text: e.message }; }
      out.textContent = (r && r.text) || "测试失败";
      out.dataset.ok = r && r.ok ? "1" : "0";
      btn.disabled = false;
    });
    $("#dg-launcher-btn").addEventListener("click", () => open({ type: "launcher" }));
  }

  /* ---------- 界面异常上报（也算启动器报错） ---------- */
  let lastReport = 0;
  function reportJsError(msg) {
    const now = Date.now();
    if (now - lastReport < 10000 || !App.api || !App.api.diag_report_error) return;
    lastReport = now;
    try { App.api.diag_report_error(String(msg).slice(0, 2000)); } catch (e) { /* 忽略 */ }
  }
  window.addEventListener("error", (e) => {
    if (!e.filename || !/\/js\//.test(e.filename)) return;   // 只管启动器自己的脚本
    reportJsError(`${e.message}\n  at ${e.filename.split("/").pop()}:${e.lineno}:${e.colno}\n${(e.error && e.error.stack) || ""}`);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    if (r && /pywebview|evaluate_js/i.test(String(r.message || r))) return;
    reportJsError("未处理的 Promise 异常：" + ((r && (r.stack || r.message)) || r));
  });

  App.diagnose = { open, close, run };

  App.pages.diag = {
    init() {
      $("#dg-close").addEventListener("click", close);
      $("#dg-rerun").addEventListener("click", run);
      $("#dg-copy").addEventListener("click", () => App.copy(reportText(), "诊断报告已复制"));
      $("#dg-target").addEventListener("change", (e) => {
        const t = (e.target._targets || [])[+e.target.value];
        if (t) { target = t; if (!job) { aiSteps = []; aiResult = null; aiError = ""; } run(); }
      });
      const btn = $("#launch-diag");
      if (btn) btn.addEventListener("click", () =>
        open({ type: "inst", iid: (App.instances && App.instances.active) || App.cfg.active_instance }));
      document.addEventListener("keydown", (e) => {
        if (e.ctrlKey && e.shiftKey && (e.key === "D" || e.key === "d")) {
          e.preventDefault();
          drawer().hidden ? open({ type: "launcher" }) : close();
        } else if (e.key === "Escape" && !drawer().hidden && $("#modal-mask").hidden) {
          close();
        }
      });
      initSettings();

      App.on("diag", "found", (e) => {
        const iid = e.target && e.target.iid;
        if (!iid) return;
        badges[iid] = e.count || 0;
        renderBadge();
        if (!drawer().hidden && sameTarget(target, e.target) && !job) run();
      });
      App.on("diag", "auto", (e) => {
        if (!drawer().hidden && target && target.type !== "launcher" && job) return;  // 正在看实例的 AI 分析，别打断
        target = { type: "launcher" };
        if (e.job) { job = e.job; aiSteps = []; aiResult = null; aiError = ""; }
        App.toast("启动器遇到问题，已打开诊断面板", "error", 4000);
        open(target, e.report);
      });
      App.on("diag", "ai_step", (e) => {
        if (e.job !== job) return;
        aiSteps.push(e.text);
        if (!drawer().hidden) renderAi();
      });
      App.on("diag", "ai_done", (e) => {
        if (e.job !== job) return;
        job = ""; aiResult = e.result;
        renderAi();
      });
      App.on("diag", "ai_error", (e) => {
        if (e.job !== job) return;
        job = ""; aiError = e.error;
        renderAi();
      });
      App.on("diag", "cfg_changed", async () => {
        // 诊断改了设置：同步界面上的配置控件和启动参数预览
        try {
          const st = await App.api.get_state();
          if (st && st.config) {
            App.cfg = st.config; App.state = st;
            App.refreshConfigControls();
            App.refreshCmdPreview(st.cmd_args);
          }
        } catch (err) { /* 下次打开页面会同步 */ }
      });
    },
  };
})();
