/* models.js — 模型管理页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let categories = [];     // [{label, is_lora, path}]
  let curCat = -1;
  let curIsLora = false;
  let files = [];          // 当前类别全部文件
  let selected = null;     // 当前选中行数据
  let sortKey = "rel";
  let sortAsc = true;
  let batchRunning = false;
  let importing = false;   // 拖拽上传进行中
  let organizing = false;  // LoRA 整理进行中
  let lastJournal = "";    // 最近一次整理的记录（撤销用）
  let isLibrary = false;   // 当前管理的是共享模型库
  let pendingSelect = "";  // 上传完成后要选中的文件路径

  function tbody() { return $("#md-files tbody"); }

  function filtered() {
    const kw = ($("#md-filter").value || "").trim().toLowerCase();
    const base = $("#md-base").value;
    return files.filter((f) => {
      if (kw && !f.rel.toLowerCase().includes(kw)) return false;
      if (base && (f.base || "未知") !== base) return false;
      return true;
    });
  }

  function renderBaseOptions() {
    const sel = $("#md-base");
    const cur = sel.value;
    const bases = [...new Set(files.map((f) => f.base || "未知"))].sort();
    sel.innerHTML = '<option value="">全部</option>' +
      bases.map((b) => `<option value="${App.esc(b)}">${App.esc(b)}</option>`).join("");
    sel.value = cur;
    if (sel.selectedIndex < 0) sel.value = "";
  }

  function renderTable() {
    const rows = filtered();
    rows.sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      if (typeof va === "string") va = va.toLowerCase();
      if (typeof vb === "string") vb = vb.toLowerCase();
      if (va < vb) return sortAsc ? -1 : 1;
      if (va > vb) return sortAsc ? 1 : -1;
      return 0;
    });
    tbody().innerHTML = rows.map((f) =>
      `<tr data-path="${App.esc(f.path)}">
        <td>${App.esc(f.rel)}</td>
        <td class="dim">${App.esc(f.base || "未知")}</td>
        <td class="dim">${App.esc(f.size_text)}</td>
        <td class="dim">${App.esc(f.mtime_text)}</td>
        <td class="row-actions">` +
        `<button class="btn btn-xs" data-act="reveal" title="在资源管理器中定位">定位</button>` +
        `<button class="btn btn-xs" data-act="copy" title="复制文件名（不含后缀）">复制名</button>` +
        (curIsLora ? `<button class="btn btn-xs" data-act="lora" title="复制 &lt;lora:名字:1&gt; 调用格式">复制lora</button>` : "") +
        `</td>
      </tr>`).join("");
    $("#md-count").textContent = `共 ${files.length} 个文件` +
      (rows.length !== files.length ? `，筛选后 ${rows.length} 个` : "");
    tbody().querySelectorAll("tr").forEach((tr) => {
      tr.addEventListener("click", () => selectRow(tr.dataset.path, tr));
      tr.querySelectorAll("[data-act]").forEach((b) =>
        b.addEventListener("click", (e) => {
          e.stopPropagation();   // 不要触发整行选中/读详情
          rowAction(b.dataset.act, tr.dataset.path);
        }));
    });
  }

  function rowAction(act, path) {
    const f = files.find((x) => x.path === path);
    if (!f) return;
    const stem = f.rel.replace(/\.[^.]+$/, "");
    if (act === "reveal") App.api.reveal_in_explorer(f.path);
    else if (act === "copy") App.copy(stem, "已复制文件名");
    else if (act === "lora") App.copy(`<lora:${stem}:1>`, "已复制 LoRA 调用格式");
  }

  async function selectRow(path, tr) {
    tbody().querySelectorAll("tr").forEach((r) => r.classList.remove("selected"));
    if (tr) tr.classList.add("selected");
    selected = files.find((f) => f.path === path) || null;
    ["#md-open", "#md-copy-name", "#md-hash-lookup", "#md-delete"].forEach((s) => ($(s).disabled = !selected));
    $("#md-copy-lora").disabled = !selected || !curIsLora;

    const detail = $("#md-detail");
    detail.innerHTML = '<span class="dim">读取中…</span>';
    try {
      const d = await App.api.models_detail(path);
      renderDetail(d);
    } catch (e) {
      detail.innerHTML = `<span class="dim">读取失败：${App.esc(e.message)}</span>`;
    }
  }

  function kv(rows) {
    return `<div class="kv-table">` + rows.filter((r) => r[1] != null && r[1] !== "")
      .map(([k, v]) => `<span class="k">${App.esc(k)}</span><span class="v">${App.esc(v)}</span>`).join("") + `</div>`;
  }

  function renderDetail(d) {
    const box = $("#md-preview");
    box.innerHTML = d.preview ? `<img src="${d.preview}" alt="">` : "<span>没有预览图</span>";

    let html = "";
    if (d.file) {
      html += `<div class="sect">文件</div>` + kv([
        ["文件名", d.file.name], ["大小", d.file.size_text], ["修改时间", d.file.mtime_text],
      ]);
    }
    if (d.civitai) {
      if (d.civitai.parse_error) {
        html += `<div class="sect">模型站信息</div><div class="dim">.civitai.info 文件解析失败（可能已损坏）</div>`;
      } else {
        const c = d.civitai;
        // 模型站跳转按钮：有哪站的信息就显示哪站
        const links = [];
        if (c.modelId) {
          links.push(`<button class="btn btn-xs" data-open-url="https://civitai.com/models/${c.modelId}${c.versionId ? `?modelVersionId=${c.versionId}` : ""}">Civitai 页面</button>`);
        }
        if (c.liblibUuid) {
          links.push(`<button class="btn btn-xs" data-open-url="https://www.liblib.art/modelinfo/${c.liblibUuid}${c.liblibVersionUuid ? `?versionUuid=${c.liblibVersionUuid}` : ""}">liblib 页面</button>`);
        }
        html += `<div class="sect">模型站信息` +
          (links.length ? `<span class="sect-actions">${links.join("")}</span>` : "") + `</div>` + kv([
          ["模型", c.modelName], ["类型", c.modelType],
          ["版本", c.versionName], ["基础模型", c.baseModel],
          ["SHA256", c.sha256_short],
        ]);
      }
    } else {
      html += `<div class="sect">模型站信息</div><div class="dim">本地没有 .civitai.info——可点击下方「按哈希查询并补全信息」（会先查 Civitai，查不到再查 liblib），或在下方粘贴 liblib 链接手动绑定</div>`;
    }

    // ---- 触发词（多组）：站点爬取 + 手动输入 ----
    const words = (d.civitai && d.civitai.trainedWords) || [];
    const srcMap = { civitai: "来自 Civitai", liblib: "来自 liblib", manual: "手动输入" };
    const srcLabel = (d.civitai && srcMap[d.civitai.trainedWordsSource]) || "";
    html += `<div class="sect">触发词${srcLabel ? `<span class="card-title-hint">${srcLabel}</span>` : ""}</div>`;
    if (words.length) {
      html += words.map((w, i) =>
        `<div class="tw-row"><span class="tw-text">${App.esc(w)}</span>` +
        `<button class="btn btn-xs" data-tw-copy="${i}">复制</button></div>`).join("");
    } else {
      html += `<div class="dim">暂无触发词——哈希查询/绑定 liblib 会自动补全，也可以直接手动输入</div>`;
    }
    html += `<div class="tw-edit"><textarea id="md-tw-input" rows="3" placeholder="手动输入触发词，每行一组">${App.esc(words.join("\n"))}</textarea>` +
      `<button class="btn btn-xs" id="md-tw-save">保存</button></div>`;

    // ---- liblib 手动绑定（哈希查不到时的退路，也能补触发词）----
    html += `<div class="sect">liblib 绑定</div>` +
      `<div class="tw-edit"><input type="text" id="md-liblib-url" placeholder="粘贴 liblib 模型链接（https://www.liblib.art/modelinfo/…）">` +
      `<button class="btn btn-xs" id="md-liblib-bind">绑定</button></div>`;

    if (d.safetensors) {
      if (d.safetensors.unreadable) {
        html += `<div class="sect">Safetensors 元数据</div><div class="dim">头部无法读取（文件可能损坏，或不是标准 safetensors）</div>`;
      } else {
        html += `<div class="sect">Safetensors 元数据</div>` + kv([
          ["类型", d.safetensors.kind], ["架构", d.safetensors.arch],
        ]);
        if (d.safetensors.note) html += `<div class="dim">${App.esc(d.safetensors.note)}</div>`;
        if (d.safetensors.train_rows && d.safetensors.train_rows.length) {
          html += `<div class="sect">训练信息</div>` + kv(d.safetensors.train_rows);
        }
        if (d.safetensors.tags && d.safetensors.tags.length) {
          html += `<div class="sect">高频训练标签</div><div class="dim">${App.esc(d.safetensors.tags.join(", "))}</div>`;
        }
      }
    }
    $("#md-detail").innerHTML = html;
    bindDetailActions(d, words);
  }

  function bindDetailActions(d, words) {
    const box = $("#md-detail");
    box.querySelectorAll("[data-open-url]").forEach((b) =>
      b.addEventListener("click", () => App.api.open_url(b.dataset.openUrl)));
    box.querySelectorAll("[data-tw-copy]").forEach((b) =>
      b.addEventListener("click", () => App.copy(words[+b.dataset.twCopy] || "", "已复制触发词")));

    const saveBtn = $("#md-tw-save");
    if (saveBtn) saveBtn.addEventListener("click", async () => {
      if (!selected) return;
      const lines = $("#md-tw-input").value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      try {
        const r = await App.api.models_set_trained_words(selected.path, lines);
        if (r && r.ok) App.toast(`已保存 ${r.count} 组触发词（手动输入，不会被站点数据覆盖）`, "ok");
        else App.toast((r && r.error) || "保存失败", "error");
      } catch (e) { App.toast("保存失败：" + e.message, "error"); }
    });

    const bindBtn = $("#md-liblib-bind");
    if (bindBtn) bindBtn.addEventListener("click", async () => {
      if (!selected) return;
      const url = $("#md-liblib-url").value.trim();
      if (!url) { App.toast("请先粘贴 liblib 模型链接", "error"); return; }
      bindBtn.disabled = true;
      try {
        const r = await App.api.models_bind_liblib(selected.path, url);
        if (r && r.ok) {
          App.toast(`已绑定 liblib：${r.modelName}（补全 ${r.words} 组触发词）`, "ok", 4500);
          selectRow(selected.path, null);  // 重新渲染详情
        } else App.toast((r && r.error) || "绑定失败", "error", 5000);
      } catch (e) { App.toast("绑定失败：" + e.message, "error"); }
      finally { bindBtn.disabled = false; }
    });
  }

  function renderDropTarget() {
    const c = categories[curCat];
    $("#md-drop-target").innerHTML = c
      ? `<em>${App.esc(c.label)}</em>${isLibrary ? "<em>共享模型库</em>" : ""}${App.esc(c.path || "")}`
      : "请先在左侧选择一个分类";
  }

  async function loadCat(idx) {
    curCat = idx;
    $$("#md-cats .cat-item").forEach((el, i) => el.classList.toggle("active", i === idx));
    renderDropTarget();
    $("#md-files tbody").innerHTML = "";
    $("#md-count").textContent = "读取中…";
    $("#md-detail").innerHTML = "";
    $("#md-preview").innerHTML = "<span>选中文件后这里显示预览图</span>";
    selected = null;
    ["#md-open", "#md-copy-name", "#md-copy-lora", "#md-hash-lookup", "#md-delete"].forEach((s) => ($(s).disabled = true));
    try {
      const r = await App.api.models_list(idx);
      if (r && r.ok === false) { $("#md-count").textContent = r.error || "读取失败"; return; }
      files = r.files || [];
      curIsLora = !!r.is_lora;
      $("#md-organize").hidden = !curIsLora;
      $("#md-organize-undo").hidden = !curIsLora || !lastJournal;
      renderBaseOptions();
      renderTable();
      $("#md-root").textContent = r.no_info > 0
        ? `其中 ${r.no_info} 个没有 Civitai 信息` : "";
      if (pendingSelect) {
        const want = pendingSelect;
        pendingSelect = "";
        const tr = Array.from(tbody().querySelectorAll("tr")).find((t) => t.dataset.path === want);
        if (tr) { tr.scrollIntoView({ block: "center" }); selectRow(want, tr); }
      }
    } catch (e) {
      $("#md-count").textContent = "读取失败：" + e.message;
    }
  }

  function setBatchRunning(v) {
    batchRunning = v;
    $("#md-batch").disabled = v;
    $("#md-batch-cancel").disabled = !v;
  }

  /* ---------- 进度区（拖拽上传 / LoRA 整理共用） ---------- */
  let preparing = false;   // 已放下文件、还在读路径 / 分析文件（还没开始复制）
  function refreshBusy() {
    const on = importing || organizing || preparing;
    $("#page-models").classList.toggle("importing", on);
    $("#md-import-busy").hidden = !on;
    // 读取 / 分析阶段没法取消（很快，而且没有后台任务可停）
    $("#md-import-cancel").hidden = !(importing || organizing);
  }
  // 更新进度区；pct 为 null 时显示来回滑动的「不确定」进度条
  function busySet(o) {
    const setText = (sel, v) => { const el = $(sel); if (el) { el.textContent = v; } };
    if ("stage" in o) setText("#md-import-stage", o.stage || "");
    if ("name" in o) { setText("#md-import-name", o.name || ""); const el = $("#md-import-name"); if (el) el.title = o.name || ""; }
    if ("foot" in o) setText("#md-import-text", o.foot || "");
    if ("pct" in o) {
      const busy = $("#md-import-busy"), bar = $("#md-import-bar");
      if (!busy || !bar) return;
      const indet = o.pct == null;
      busy.querySelector(".db-progress").classList.toggle("indeterminate", indet);
      bar.style.width = indet ? "" : Math.max(0, Math.min(100, o.pct)) + "%";
      setText("#md-import-pct", indet ? "" : Math.floor(o.pct) + "%");
    }
  }
  function setPreparing(v, n) {
    preparing = v;
    if (v) busySet({ stage: "读取拖入的文件…", name: n ? `${n} 项` : "", pct: null, foot: "" });
    refreshBusy();
  }

  /* ---------- 拖拽上传 ---------- */
  // 复制速度：每 0.5 秒以上取一次样，指数平滑，算剩余时间
  const speed = { t: 0, done: 0, bps: 0 };
  function fmtBytes(n) { return App.fmtBytes ? App.fmtBytes(n) : (n / 1048576).toFixed(1) + " MB"; }
  function fmtEta(sec) {
    if (!isFinite(sec) || sec < 0) return "";
    if (sec < 60) return `剩余约 ${Math.max(1, Math.round(sec))} 秒`;
    if (sec < 3600) return `剩余约 ${Math.floor(sec / 60)} 分 ${Math.round(sec % 60)} 秒`;
    return `剩余约 ${Math.floor(sec / 3600)} 小时 ${Math.round((sec % 3600) / 60)} 分`;
  }
  function setImporting(v) {
    importing = v;
    if (v) {
      preparing = false;
      speed.t = performance.now(); speed.done = 0; speed.bps = 0;
      busySet({ stage: "开始复制…", name: "", pct: 0, foot: "" });
    }
    refreshBusy();
  }
  function onImportProgress(e) {
    if (!importing) return;
    const now = performance.now();
    if (e.done != null && now - speed.t >= 500) {
      const inst = (e.done - speed.done) * 1000 / (now - speed.t);
      speed.bps = speed.bps ? speed.bps * 0.6 + inst * 0.4 : inst;
      speed.t = now; speed.done = e.done;
    }
    const parts = [];
    if (e.total) parts.push(`${fmtBytes(e.done || 0)} / ${fmtBytes(e.total)}`);
    if (speed.bps > 0) {
      parts.push(`${fmtBytes(speed.bps)}/s`);
      if (e.total) parts.push(fmtEta((e.total - (e.done || 0)) / speed.bps));
    }
    busySet({ stage: e.n > 1 ? `复制中 ${e.i}/${e.n}` : "复制中", name: e.name || "", pct: e.pct || 0, foot: parts.join(" · ") });
  }

  /* ---------- LoRA 整理 ---------- */
  function setOrganizing(v) {
    organizing = v;
    $("#md-organize").disabled = v;
    if (v) busySet({ stage: "查询模型信息…", name: "", pct: null, foot: "" });
    refreshBusy();
  }

  async function startOrganize() {
    if (organizing || importing) return;
    try {
      const r = await App.api.lora_organize_scan();
      if (r && r.ok === false) { App.toast(r.error || "无法开始整理", "error"); return; }
      setOrganizing(true);
      App.toast("正在补全 LoRA 信息（没查过的会按哈希查 Civitai / liblib，数量多时需要一会儿）", "", 4000);
    } catch (e) { App.toast("无法开始整理：" + e.message, "error"); }
  }

  async function showOrganizePlan(e) {
    setOrganizing(false);
    if (e.cancelled) { App.toast("已取消整理", ""); return; }
    if (!e.count) { App.toast("LoRA 都已经在该在的文件夹里了，没有需要整理的", "ok"); return; }
    const groups = (e.groups || []).map((g) =>
      `<tr><td>${App.esc(g.sub)}</td><td>${g.count}</td></tr>`).join("");
    const sample = (e.sample || []).map((x) =>
      `<div class="pv-line">${App.esc(x.from)} <b>→</b> ${App.esc(x.to)}</div>`).join("");
    const v = await App.modal("整理 LoRA",
      `<div>将把 <b>${e.count}</b> 个 LoRA 按「${App.esc(e.template)}」分进子文件夹：</div>` +
      `<table class="pv-table"><tr><th>目标文件夹</th><th>数量</th></tr>${groups}</table>` +
      `<div class="pv-list">${sample}${e.count > (e.sample || []).length ? `<div class="pv-line">…… 共 ${e.count} 个</div>` : ""}</div>` +
      (e.running ? '<div class="pv-warn">有实例正在运行：请先停止所有实例再整理（LoRA 可能正被加载）。</div>' : "") +
      '<div class="hint">ComfyUI 已保存的工作流会同步改路径；图片里内嵌的工作流改不了，拖旧图进 ComfyUI 时需要重新选一下 LoRA。整理后可以一键撤销。</div>',
      [{ id: "go", label: "开始整理", kind: "primary" }, { id: "cancel", label: "取消" }]);
    if (v !== "go") return;
    try {
      const r = await App.api.lora_organize_apply();
      if (r && r.ok === false) { App.toast(r.error || "整理失败", "error", 6000); return; }
      setOrganizing(true);
    } catch (err) { App.toast("整理失败：" + err.message, "error"); }
  }

  async function undoOrganize() {
    if (!lastJournal) return;
    const yes = await App.confirm("撤销上次整理", "把上次整理移动过的 LoRA 全部搬回原来的位置，并恢复被改写的 ComfyUI 工作流？");
    if (!yes) return;
    try {
      const r = await App.api.journal_undo(lastJournal);
      if (r && r.ok === false) { App.toast(r.error || "撤销失败", "error", 6000); return; }
      App.toast(`已还原 ${r.restored} 个文件` + (r.failed && r.failed.length ? `，${r.failed.length} 个失败` : ""),
        r.failed && r.failed.length ? "error" : "ok", 5000);
      lastJournal = "";
      loadCat(curCat);
      if (App.reloadLibrary) App.reloadLibrary();
    } catch (e) { App.toast("撤销失败：" + e.message, "error"); }
  }

  async function handleDrop(paths) {
    if (!paths || !paths.length) { setPreparing(false); return; }
    if (importing) { App.toast("上一批模型还在复制，请等它完成或先取消", "error"); return; }
    if (organizing) { setPreparing(false); App.toast("正在整理 LoRA，请等它完成再拖入", "error"); return; }
    if (curCat < 0 || !categories[curCat]) { setPreparing(false); App.toast("请先在左侧选一个模型分类，再把文件拖进来", "error"); return; }
    setPreparing(true, paths.length);
    busySet({ stage: "分析文件…", name: paths.length > 1 ? `${paths.length} 项` : paths[0].split(/[\\/]/).pop() });
    let plan;
    try { plan = await App.api.models_import_plan(curCat, paths); }
    catch (e) { setPreparing(false); App.toast("读取拖入的文件失败：" + e.message, "error"); return; }
    setPreparing(false);   // 分析完了；下面可能弹确认框，确认后才真正开始复制
    if (!plan || plan.ok === false) { App.toast((plan && plan.error) || "读取拖入的文件失败", "error"); return; }

    if (!plan.count) {
      const ig = plan.ignored_count ? `（忽略了 ${plan.ignored_count} 个非模型文件：${plan.ignored.slice(0, 3).join("、")}${plan.ignored_count > 3 ? " 等" : ""}）` : "";
      App.toast("没有找到可上传的模型文件" + ig, "error", 5000);
      return;
    }
    if (!plan.to_copy) {
      App.toast(`这 ${plan.skip} 个模型「${plan.target_label}」里已经有了，跳过`, "ok", 4000);
      return;
    }
    if (plan.no_space) {
      App.modal("磁盘空间不足",
        `需要 ${App.esc(plan.total_text)}，目标磁盘只剩 ${App.esc(plan.free_text)}。\n\n目标文件夹：${App.esc(plan.target_dir)}`);
      return;
    }

    let target = curCat;
    if (plan.suggest_index != null) {
      const isLora = !!(plan.counts && plan.counts.lora);
      const kind = plan.suggest_kind || (isLora ? "LoRA" : "大模型（Checkpoint）");
      const v = await App.modal("放到哪个分类？",
        `拖进来的 ${plan.count} 个文件看起来是<b> ${App.esc(kind)} </b>，` +
        `但当前分类是「${App.esc(plan.target_label)}」。`,
        [
          { id: "suggest", label: `放到「${plan.suggest_label}」`, kind: "primary" },
          { id: "here", label: "仍然放这里" },
          { id: "cancel", label: "取消" },
        ]);
      if (v === "cancel" || !v) return;
      if (v === "suggest") target = plan.suggest_index;
    }

    try {
      const r = await App.api.models_import_start(target, paths);
      if (!r || r.ok === false) { App.toast((r && r.error) || "无法开始上传", "error", 5000); return; }
      setImporting(true);
      const where = categories[target] ? categories[target].label : "";
      App.toast(`开始上传到「${where}」，共 ${r.total_text}` +
        (plan.skip ? `（${plan.skip} 个已存在，跳过）` : ""), "", 3000);
    } catch (e) { App.toast("无法开始上传：" + e.message, "error"); }
  }

  // 拖着文件经过窗口时高亮提示条（真正的 drop 由 core.js bindFileDrop 统一接收，
  // 拿到路径后调本页的 onDropped）
  function bindDragHighlight() {
    const page = $("#page-models");
    let depth = 0;
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    const off = () => { depth = 0; page.classList.remove("drag-active"); };
    document.addEventListener("dragenter", (e) => {
      if (App.currentPage !== "models" || !hasFiles(e)) return;
      depth++;
      page.classList.add("drag-active");
    });
    document.addEventListener("dragleave", () => {
      if (depth > 0 && --depth === 0) page.classList.remove("drag-active");
    });
    document.addEventListener("drop", off);
    window.addEventListener("blur", off);
  }

  App.pages.models = {
    init() {
      const reloadAll = () => {
        // 刷新分类列表（根目录可能刚设置/更换过），再刷新当前类别
        App.api.models_categories().then((r) => {
          categories = (r && r.categories) || [];
          isLibrary = !!(r && r.library);
          const box = $("#md-cats");
          box.innerHTML = "";
          categories.forEach((c, i) => {
            const el = document.createElement("div");
            el.className = "cat-item";
            el.textContent = c.label;
            el.addEventListener("click", () => loadCat(i));
            box.appendChild(el);
          });
          if (categories.length) loadCat(Math.min(Math.max(curCat, 0), categories.length - 1));
        }).catch((e) => App.toast("读取模型目录失败：" + e.message, "error"));
      };
      App.pages.models.reloadAll = reloadAll;
      $("#md-refresh").addEventListener("click", reloadAll);

      bindDragHighlight();
      $("#md-import-cancel").addEventListener("click", () =>
        organizing ? App.api.lora_organize_cancel() : App.api.models_import_cancel());
      $("#md-organize").addEventListener("click", startOrganize);
      $("#md-organize-undo").addEventListener("click", undoOrganize);
      App.on("models", "organize_progress", (e) => {
        if (!organizing) return;
        busySet({ stage: `${e.stage || "整理"} ${e.i}/${e.n}`, name: e.name || "", pct: e.n ? e.i * 100 / e.n : null });
      });
      App.on("models", "organize_plan", showOrganizePlan);
      App.on("models", "organize_done", (e) => {
        if (!e.auto) setOrganizing(false);
        if (e.journal && e.moved) lastJournal = e.journal;
        const fail = e.failed && e.failed.length ? `，${e.failed.length} 个失败（${e.failed[0].name}：${e.failed[0].error}）` : "";
        const wf = e.workflows ? `，同步修改了 ${e.workflows} 个 ComfyUI 工作流` : "";
        if (e.auto) {
          const where = [...new Set(e.subs || [])].join("、");
          if (e.moved) App.toast(`新 LoRA 已自动归类到「${where}」${wf}`, "ok", 5000);
        } else {
          App.toast(`整理完成：移动了 ${e.moved} 个 LoRA${wf}${fail}`, fail ? "error" : "ok", 6000);
        }
        if (curCat >= 0) loadCat(curCat);
        if (App.reloadLibrary) App.reloadLibrary();
      });
      App.on("models", "import_scan", (e) => {
        if (!preparing) return;
        busySet({ stage: `分析文件 ${e.i}/${e.n}`, name: e.name || "", pct: e.n ? e.i * 100 / e.n : null });
      });
      App.on("models", "import_progress", onImportProgress);
      App.on("models", "import_done", (e) => {
        setImporting(false);
        const parts = [];
        if (e.copied) parts.push(`已上传 ${e.copied} 个`);
        if (e.renamed) parts.push(`其中 ${e.renamed} 个因重名自动改名`);
        if (e.skipped) parts.push(`跳过已存在的 ${e.skipped} 个`);
        if (e.failed && e.failed.length) parts.push(`失败 ${e.failed.length} 个（${e.failed[0].name}：${e.failed[0].error}）`);
        const text = (e.cancelled ? "已取消上传。" : "") + (parts.join("，") || "没有复制任何文件");
        App.toast(text, e.failed && e.failed.length ? "error" : "ok", 6000);
        if (e.last_dest) pendingSelect = e.last_dest;
        loadCat(e.cat_index);
      });
      $("#md-filter").addEventListener("input", renderTable);
      $("#md-base").addEventListener("change", renderTable);

      // 预览图可收起（窗口小或触发词多时给详情区腾地方），状态本地记住
      const store = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };
      const recall = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
      const pToggle = $("#md-preview-toggle");
      const applyPreviewCollapsed = () => {
        const collapsed = recall("md-preview-collapsed") === "1";
        $(".models-detail").classList.toggle("preview-collapsed", collapsed);
        pToggle.textContent = collapsed ? "展开" : "收起";
      };
      pToggle.addEventListener("click", () => {
        store("md-preview-collapsed",
          $(".models-detail").classList.contains("preview-collapsed") ? "0" : "1");
        applyPreviewCollapsed();
      });
      applyPreviewCollapsed();

      $$("#md-files th.sortable").forEach((th) => th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (sortKey === key) sortAsc = !sortAsc; else { sortKey = key; sortAsc = true; }
        $$("#md-files th").forEach((t) => t.classList.remove("sorted-asc", "sorted-desc"));
        th.classList.add(sortAsc ? "sorted-asc" : "sorted-desc");
        renderTable();
      }));

      $("#md-open").addEventListener("click", () => selected && App.api.reveal_in_explorer(selected.path));
      $("#md-copy-name").addEventListener("click", () => {
        if (!selected) return;
        App.copy(selected.rel.replace(/\.[^.]+$/, ""), "已复制文件名");
      });
      $("#md-copy-lora").addEventListener("click", () => {
        if (!selected) return;
        App.copy(`<lora:${selected.rel.replace(/\.[^.]+$/, "")}:1>`, "已复制 LoRA 调用格式");
      });

      $("#md-hash-lookup").addEventListener("click", async () => {
        if (!selected) return;
        try {
          const r = await App.api.models_hash_lookup(selected.path);
          if (r && r.ok === false) App.toast(r.error || "查询失败", "error");
        } catch (e) { App.toast("查询失败：" + e.message, "error"); }
      });

      $("#md-delete").addEventListener("click", async () => {
        if (!selected) return;
        const yes = await App.confirm("删除模型",
          `确定删除以下模型文件吗？\n\n${selected.path}\n\n会连同同名的预览图和 .civitai.info 一起删除，此操作不可恢复。`,
          "删除", true);
        if (!yes) return;
        try {
          const r = await App.api.models_delete(selected.path);
          if (r && r.ok) { App.toast("已删除", "ok"); loadCat(curCat); }
          else App.toast((r && r.error) || "删除失败", "error");
        } catch (e) { App.toast("删除失败：" + e.message, "error"); }
      });

      $("#md-batch").addEventListener("click", async () => {
        if (curCat < 0 || batchRunning) return;
        try {
          const r = await App.api.models_batch(curCat);
          if (r && r.ok === false) App.toast(r.error || "无法开始批量查询", "error");
          else setBatchRunning(true);
        } catch (e) { App.toast("无法开始批量查询：" + e.message, "error"); }
      });
      $("#md-batch-cancel").addEventListener("click", () => App.api.models_batch_cancel());

      App.on("models", "lookup_status", (e) => App.toast(e.text));
      App.on("models", "lookup_progress", () => {}); // 状态文字已足够
      App.on("models", "lookup_done", (e) => {
        if (e.ok) {
          const via = e.source === "liblib" ? "[liblib] " : "";
          App.toast(`已补全信息：${via}${e.info.modelName}（${e.info.baseModel || "未知基础模型"}）`, "ok", 4500);
          if (selected) pendingSelect = selected.path;   // 详情区跟着刷新，新补的预览图立刻可见
          if (curCat >= 0) loadCat(curCat);
        } else if (e.not_found) {
          App.toast("Civitai 和 liblib 上都没有这个哈希对应的模型（可能是本地训练/合并的，也可以在详情里粘贴 liblib 链接手动绑定）", "error", 6000);
        } else {
          App.toast("查询失败：" + (e.error || ""), "error", 5000);
        }
      });

      App.on("models", "batch_progress", (e) => {
        $("#md-count").textContent = `批量查询中 ${e.i}/${e.total}：${e.name}`;
      });
      App.on("models", "batch_item", (e) => { /* 进度文字已足够 */ });
      App.on("models", "batch_done", (e) => {
        setBatchRunning(false);
        App.toast(e.cancelled
          ? `已取消。补全 ${e.found} 个`
          : `批量查询完成：补全 ${e.found} 个，未收录 ${e.notfound} 个，失败 ${e.failed} 个`, "ok", 5000);
        if (curCat >= 0) loadCat(curCat);
      });

      // 首次加载分类列表
      reloadAll();
    },

    // core.js bindFileDrop：放下的一瞬间先给反馈（拿路径要一点时间），失败时收起
    onDropStart(n) { if (!importing && !organizing) setPreparing(true, n); },
    onDropFail() { setPreparing(false); },
    onDropped(paths) { handleDrop(paths); },

    onShow() {
      // 切回本页时刷新分类（根目录可能刚在别的页面改过）
      if (App.pages.models.reloadAll) App.pages.models.reloadAll();
    },
  };
})();
