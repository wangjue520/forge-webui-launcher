/* catalog.js — 模型下载页「常用模型一键下载」：选模型 → 选版本 → 每个文件选精度，
   主模型 / 文本编码器 / VAE 一次下好、各自放进对应文件夹（目录数据在 app/model_catalog.json） */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  const S = {
    info: null,          // model_catalog_info 的结果
    group: "",           // 当前分类（空 = 全部）
    query: "",
    model: null,         // 当前选中的模型
    version: null,       // 当前选中的版本
    files: {},           // model_catalog_state：「版本/组件/精度」→ {state, part}
    where: {},           // 「版本/组件」→ 显示用位置
    diskFree: null,
    choice: {},          // 用户在这个版本里手动选过的精度：组件 id → 精度 id
    checked: {},         // 可选组件的勾选：组件 id → bool
    running: false,
    active: null,        // 正在下载的 {model, version}
    live: {},            // 下载中各组件的状态：组件 id → state（只对 active 那个版本有效）
    liveErr: {},
    inited: false, log: null, prog: null,
  };
  const GB = (n) => (n / 1073741824).toFixed(n >= 10 * 1073741824 ? 1 : 2) + " GB";
  const STATE_TEXT = { ok: "已下载", part: "下了一部分，会续传", diff: "已有同名文件但大小不对，会重新下载",
    downloading: "下载中…", error: "失败" };

  /* ---------- 数据 ---------- */
  function modelById(id) { return S.info && S.info.models.find((m) => m.id === id); }
  function curVer() {
    return S.model && (S.model.versions.find((v) => v.id === S.version) || S.model.versions[0]);
  }
  function fileState(vid, cid, oid) { return S.files[`${vid}/${cid}/${oid}`] || {}; }
  function isActive() {
    return S.running && S.active && S.model && S.active.model === S.model.id && S.active.version === S.version;
  }
  // 当前选中的精度：用户选过的 > 已经下好的（推荐的那个优先）> 按显卡推荐的
  function pickOption(ver, comp) {
    const want = S.choice[comp.id];
    if (want && comp.options.some((o) => o.id === want)) return comp.options.find((o) => o.id === want);
    const rec = ver.defaults[comp.id];
    const have = comp.options.filter((o) => fileState(ver.id, comp.id, o.id).state === "ok");
    if (have.length) return have.find((o) => o.id === rec) || have[0];
    return comp.options.find((o) => o.id === rec) || comp.options[0];
  }
  function isChecked(ver, comp) {
    if (comp.required) return true;
    if (comp.id in S.checked) return S.checked[comp.id];
    return comp.default || comp.options.some((o) => fileState(ver.id, comp.id, o.id).state === "ok");
  }

  /* ---------- 模型列表 ---------- */
  function renderGroups() {
    const groups = [{ id: "", label: "全部" }].concat(S.info.groups);
    $("#mc-groups").innerHTML = groups.map((g) =>
      `<button class="chip${g.id === S.group ? " on" : ""}" data-group="${App.esc(g.id)}">${App.esc(g.label)}</button>`).join("");
  }
  function matches(m) {
    if (S.group && m.group !== S.group) return false;
    const q = S.query.trim().toLowerCase();
    if (!q) return true;
    return (m.name + " " + m.desc + " " + m.versions.map((v) => v.label).join(" ")).toLowerCase().includes(q);
  }
  function chipHtml(m) {
    const on = S.model && S.model.id === m.id;
    const tip = m.supported ? m.desc : `当前实例用不了：${m.desc}`;
    return `<button class="chip mc-chip${on ? " on" : ""}${m.supported ? "" : " mc-off"}" data-model="${App.esc(m.id)}"` +
      ` title="${App.esc(tip)}">${App.esc(m.name)}</button>`;
  }
  function renderModels() {
    const box = $("#mc-models");
    const list = S.info.models.filter(matches);
    if (!list.length) { box.innerHTML = `<div class="hint">没有找到「${App.esc(S.query)}」相关的模型</div>`; return; }
    if (S.group || S.query.trim()) {
      box.innerHTML = `<div class="chips">${list.map(chipHtml).join("")}</div>`;
      return;
    }
    // 「全部」按分类分组显示
    box.innerHTML = S.info.groups.map((g) => {
      const ms = list.filter((m) => m.group === g.id);
      return ms.length ? `<div class="mc-group"><span class="mc-gtitle">${App.esc(g.label)}</span>` +
        `<div class="chips">${ms.map(chipHtml).join("")}</div></div>` : "";
    }).join("");
  }

  /* ---------- 选中的模型 ---------- */
  async function selectModel(id, keepVersion) {
    const m = modelById(id);
    if (!m) return;
    const changed = !S.model || S.model.id !== id;
    S.model = m;
    if (changed || !keepVersion || !m.versions.some((v) => v.id === S.version)) {
      S.version = (m.versions.find((v) => v.default) || m.versions[0]).id;
    }
    if (changed) { S.choice = {}; S.checked = {}; }
    if (S.running && S.active && S.active.model === id) S.version = S.active.version;
    renderModels();
    $("#mc-detail").hidden = false;
    renderDetail();
    await loadState();
  }
  async function loadState() {
    if (!S.model || !App.api.model_catalog_state) return;
    const id = S.model.id;
    let r;
    try { r = await App.api.model_catalog_state(id); } catch (e) { return; }
    if (!r || !r.ok || !S.model || S.model.id !== id) return;
    S.files = r.files || {};
    S.where = r.where || {};
    S.diskFree = r.disk_free;
    renderDetail();
  }

  function hwText(ver) {
    const i = S.info;
    const parts = [];
    if (i.gpu) parts.push(i.gpu.replace(/^NVIDIA\s+(GeForce\s+)?/i, "") + (i.vram_gb ? ` ${i.vram_gb} GB 显存` : ""));
    else parts.push("没检测到 NVIDIA 显卡（按 12GB 显存估）");
    if (i.ram_gb) parts.push(`内存 ${Math.round(i.ram_gb)} GB`);
    const inst = i.kind_label ? `当前实例：${i.kind_label}` : "";
    let text = [inst, parts.join(" · ")].filter(Boolean).join("　");
    let status = "";
    if (!ver.supported) {
      text += `。${i.kind_label || "当前实例"} 用不了 ${S.model.name}${ver.ui.length ? "（支持：" + ver.ui.join(" / ") + "）" : ""}` +
        (i.library ? "；文件会放进共享模型库，换到支持的实例就能用" : "；可以先在顶部切换到支持的实例再下载");
      status = "warn";
    } else {
      text += "。默认精度已按显卡挑好（标「推荐」的），想要画质更好或更省显存可以自己换";
    }
    if (!i.library && !i.has_root) {
      text = "还没有可用的实例目录：先在「一键启动」选好 WebUI / ComfyUI 的目录，或者在「模型管理」开启共享模型库";
      status = "bad";
    }
    return { text, status };
  }

  function optionText(ver, comp, o) {
    const st = fileState(ver.id, comp.id, o.id).state;
    let t = `${o.label}　${GB(o.size)}`;
    if (o.id === ver.defaults[comp.id] && comp.options.length > 1) t += "　推荐";
    if (o.note) t += "　" + o.note;
    if (o.issue) t += `　（${o.issue}）`;
    if (st === "ok") t += "　✓";
    return t;
  }

  function rowHtml(ver, comp) {
    const o = pickOption(ver, comp);
    const sel = comp.options.length > 1
      ? `<select data-mc="${App.esc(comp.id)}">` + comp.options.map((x) =>
        `<option value="${App.esc(x.id)}"${x.id === o.id ? " selected" : ""}>${App.esc(optionText(ver, comp, x))}</option>`).join("") +
        `</select>`
      : `<span class="mc-one">${App.esc(o.label)}</span>`;
    const checked = isChecked(ver, comp);
    return `<div class="h3-row mc-row" data-cid="${App.esc(comp.id)}">` +
      `<label class="chk-row${comp.required ? " locked" : ""}"><input type="checkbox" data-mc="${App.esc(comp.id)}"` +
      `${checked ? " checked" : ""}${comp.required ? " disabled" : ""}><i></i>` +
      `<span><span class="ext-name">${App.esc(comp.label)}</span>${comp.required ? '<em class="tag">必需</em>' : ""}` +
      (comp.desc ? `<div class="ext-desc">${App.esc(comp.desc)}</div>` : "") + `</span></label>` +
      sel + `<span class="h3-size"></span>` +
      `<div class="h3-sub"><span class="h3-name"></span><span class="mc-where"></span>` +
      `<span class="h3-state"></span><span class="mc-issue"></span></div></div>`;
  }

  function renderRow(ver, comp) {
    const row = document.querySelector(`#mc-list .mc-row[data-cid="${CSS.escape(comp.id)}"]`);
    if (!row) return;
    const o = pickOption(ver, comp);
    const fs = fileState(ver.id, comp.id, o.id);
    let st = fs.state || "";
    let err = "";
    if (isActive() && S.live[comp.id]) { st = S.live[comp.id]; err = S.liveErr[comp.id] || ""; }
    const sEl = row.querySelector(".h3-state");
    sEl.dataset.state = st;
    sEl.textContent = st === "error" && err ? "失败：" + err : (STATE_TEXT[st] || "");
    row.querySelector(".h3-size").textContent = GB(o.size);
    row.querySelector(".h3-name").textContent = o.file;
    const where = S.where[`${ver.id}/${comp.id}`];
    row.querySelector(".mc-where").textContent = where ? "→ " + where : "";
    let issue = o.issue || "";
    if (!issue && o.fmt === "gguf" && S.info.kind === "comfyui" && !S.info.gguf_node) issue = "要先装 ComfyUI-GGUF 插件";
    const iEl = row.querySelector(".mc-issue");
    iEl.textContent = issue ? "⚠ " + issue : "";
    row.classList.toggle("mc-unchecked", !isChecked(ver, comp));
  }

  function renderDetail() {
    const m = S.model;
    if (!m) return;
    const ver = curVer();
    $("#mc-name").textContent = m.name;
    $("#mc-ui").textContent = ver.ui.length ? "支持：" + ver.ui.join(" / ") : "";
    $("#mc-desc").textContent = m.desc;
    const extra = [m.tips, m.license ? "许可：" + m.license : ""].filter(Boolean).join("　·　");
    $("#mc-tips").textContent = extra;
    $("#mc-tips").hidden = !extra;
    $("#mc-link").hidden = !m.link;
    const vs = $("#mc-version");
    vs.innerHTML = m.versions.map((v) =>
      `<option value="${App.esc(v.id)}"${v.id === ver.id ? " selected" : ""}>${App.esc(v.label + (v.note ? "　—　" + v.note : ""))}` +
      `${v.supported ? "" : "（当前实例用不了）"}</option>`).join("");
    $("#mc-version-row").hidden = m.versions.length < 2;
    const hw = hwText(ver);
    $("#mc-hw").textContent = hw.text;
    $("#mc-hw").dataset.status = hw.status;
    $("#mc-list").innerHTML = ver.components.map((c) => rowHtml(ver, c)).join("");
    ver.components.forEach((c) => renderRow(ver, c));
    lockInputs();
    renderTotal();
  }

  function selection() {
    const ver = curVer();
    return ver.components.filter((c) => isChecked(ver, c)).map((c) => ({ comp: c, opt: pickOption(ver, c) }));
  }

  function renderTotal() {
    if (!S.model) return;
    const ver = curVer();
    let need = 0, n = 0, gguf = false;
    const seen = new Set();
    selection().forEach(({ comp, opt }) => {
      const fs = fileState(ver.id, comp.id, opt.id);
      if (fs.state === "ok" || seen.has(opt.file)) return;
      seen.add(opt.file);
      n++;
      need += Math.max(0, opt.size - (fs.part || 0));
      if (opt.fmt === "gguf") gguf = true;
    });
    let t = n ? `还要下载 ${n} 个文件，共 ${GB(need)}` : "选中的文件都已经下好了";
    if (n && S.diskFree != null) t += `　·　模型盘剩余 ${GB(S.diskFree)}`;
    if (n && gguf && S.info.kind === "comfyui" && !S.info.gguf_node) t += "　·　选了 GGUF：ComfyUI 要先在「常用插件」装 ComfyUI-GGUF";
    const el = $("#mc-total");
    el.textContent = t;
    el.dataset.status = n && S.diskFree != null && S.diskFree < need + 2 * 1073741824 ? "bad" : "";
    $("#mc-start").disabled = S.running || !n;
  }

  function lockInputs() {
    const lock = isActive();
    document.querySelectorAll("#mc-list input, #mc-list select").forEach((el) => {
      if (el.type === "checkbox" && el.closest(".locked")) return;
      el.disabled = lock;
    });
    $("#mc-version").disabled = lock;
  }

  function setRunning(v) {
    S.running = v;
    $("#mc-cancel").disabled = !v;
    $("#mc-start").innerHTML = v ? '<svg class="btn-icon"><use href="#i-download"/></svg>下载中…'
      : '<svg class="btn-icon"><use href="#i-download"/></svg>下载选中的文件';
    if (!v) { S.live = {}; S.liveErr = {}; }
    lockInputs();
    renderTotal();
  }

  async function start() {
    if (S.running || !S.model) return;
    const ver = curVer();
    const sel = selection();
    if (!sel.length) return;
    if (!ver.supported) {
      const ok = await App.confirm("当前实例用不了这个模型",
        `${S.info.kind_label || "当前实例"} 用不了 ${S.model.name}${ver.ui.length ? "（支持：" + ver.ui.join(" / ") + "）" : ""}。\n\n` +
        (S.info.library ? "文件会放进共享模型库，之后切换到支持的实例就能直接用。" : "文件会放进当前实例的模型文件夹。") +
        "\n\n还要下载吗？", "仍然下载");
      if (!ok) return;
    }
    const payload = sel.map(({ comp, opt }) => ({ model: S.model.id, version: ver.id, comp: comp.id, option: opt.id }));
    S.active = { model: S.model.id, version: ver.id };
    S.live = {}; S.liveErr = {};
    setRunning(true);
    try {
      const r = await App.api.model_catalog_download(payload);
      if (r && r.ok === false) { App.toast(r.error || "无法开始下载", "error", 6000); setRunning(false); }
    } catch (e) { App.toast("无法开始下载：" + e.message, "error"); setRunning(false); }
  }

  /* ---------- 抽屉：默认收起，点标题展开 ---------- */
  function setOpen(open) {
    $("#mc-card").classList.toggle("mc-collapsed", !open);
    $("#mc-body").hidden = !open;
    $("#mc-toggle").setAttribute("aria-expanded", open ? "true" : "false");
    $("#mc-fold").textContent = open ? "收起 ▴" : "展开 ▾";
  }
  function isOpen() { return !$("#mc-body").hidden; }

  /* ---------- 刷新 / 初始化 ---------- */
  async function refresh() {
    if (!App.api.model_catalog_info) return;
    let r;
    try { r = await App.api.model_catalog_info(); } catch (e) { return; }
    const card = $("#mc-card");
    if (!r || !r.ok) {
      card.hidden = !r;
      if (r) $("#mc-models").innerHTML = `<div class="hint" data-status="bad">${App.esc(r.error || "读不了模型目录")}</div>`;
      return;
    }
    card.hidden = false;
    S.info = r;
    if (r.running && r.active) {
      S.active = r.active;
      if (!S.running) setRunning(true);
      setOpen(true);                       // 正在下载：展开，进度条看得到
    }
    renderGroups();
    renderModels();
    const keep = S.model && modelById(S.model.id);
    const first = (r.running && r.active && r.active.model) || (keep && S.model.id) ||
      (r.models.find((m) => m.supported) || r.models[0]).id;
    await selectModel(first, true);
  }

  function init() {
    if (S.inited) return;
    S.inited = true;
    S.log = App.pageLog("mcdl", "常用模型下载");
    S.prog = App.progress($("#mc-progress"));
    S.prog.hide();

    setOpen(false);
    $("#mc-toggle").addEventListener("click", () => setOpen(!isOpen()));
    $("#mc-toggle").addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(!isOpen()); }
    });
    $("#mc-groups").addEventListener("click", (e) => {
      const b = e.target.closest("[data-group]");
      if (!b) return;
      S.group = b.dataset.group;
      renderGroups();
      renderModels();
    });
    $("#mc-search").addEventListener("input", (e) => { S.query = e.target.value; renderModels(); });
    $("#mc-models").addEventListener("click", (e) => {
      const b = e.target.closest("[data-model]");
      if (b) selectModel(b.dataset.model);
    });
    $("#mc-version").addEventListener("change", (e) => {
      S.version = e.target.value;
      S.choice = {};
      S.checked = {};
      renderDetail();
    });
    $("#mc-list").addEventListener("change", (e) => {
      const el = e.target.closest("[data-mc]");
      if (!el || !S.model) return;
      const ver = curVer();
      const comp = ver.components.find((c) => c.id === el.dataset.mc);
      if (!comp) return;
      if (el.tagName === "SELECT") {
        S.choice[comp.id] = el.value;
        renderRow(ver, comp);
      } else {
        // 成对用的（Wan 高噪 + 低噪加速 LoRA）一起勾 / 一起取消
        const group = comp.link ? ver.components.filter((c) => c.link === comp.link && !c.required) : [comp];
        group.forEach((c) => {
          S.checked[c.id] = el.checked;
          const box = document.querySelector(`#mc-list input[data-mc="${CSS.escape(c.id)}"]`);
          if (box) box.checked = el.checked;
          renderRow(ver, c);
        });
      }
      renderTotal();
    });
    $("#mc-link").addEventListener("click", () => { if (S.model && S.model.link) App.api.open_url(S.model.link); });
    $("#mc-start").addEventListener("click", start);
    $("#mc-cancel").addEventListener("click", () => App.api.model_catalog_cancel());

    App.on("mcdl", "log", (e) => S.log(e.text));
    App.on("mcdl", "item", (e) => {
      S.live[e.id] = e.state;
      if (e.error) S.liveErr[e.id] = e.error;
      if (e.state === "ok" && S.active) {
        // 下好了：本地也记成已下载，不用等重新查询
        const m = modelById(S.active.model);
        const ver = m && m.versions.find((v) => v.id === S.active.version);
        const comp = ver && ver.components.find((c) => c.id === e.id);
        if (comp && S.model && S.model.id === m.id && S.version === ver.id) {
          const o = pickOption(ver, comp);
          S.files[`${ver.id}/${comp.id}/${o.id}`] = { state: "ok", part: 0 };
        }
      }
      if (isActive()) {
        const ver = curVer();
        const comp = ver.components.find((c) => c.id === e.id);
        if (comp) renderRow(ver, comp);
      }
    });
    App.on("mcdl", "progress", (e) => {
      const all = e.all_total > 0 ? e.all_done / e.all_total * 100 : 0;
      const cur = e.total > 0 ? e.downloaded / e.total * 100 : 0;
      S.prog.set(all, `总进度 ${all.toFixed(1)}%　·　(${e.index}/${e.count}) ${e.name}　${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}（${cur.toFixed(1)}%）`);
    });
    App.on("mcdl", "done", (e) => {
      const name = S.active && modelById(S.active.model) ? modelById(S.active.model).name : "模型";
      setRunning(false);
      S.active = null;
      S.prog.hide();
      if (e.ok) {
        App.toast(e.downloaded ? `${name} 已下载 ${e.downloaded} 个文件` + (e.skipped ? `（另有 ${e.skipped} 个已存在，跳过）` : "") +
          "，已放进对应文件夹" : `${name} 选中的文件都已经在了`, "ok", 7000);
      } else if (e.cancelled) {
        App.toast("已取消，已下载的部分保留，下次点下载会接着下", "ok", 5000);
      } else {
        App.toast(e.error || "下载失败", "error", 8000);
      }
      loadState();
    });
  }

  App.catalog = { init, refresh };
})();
