/* wd14.js — 图片反推页：WD14 标签 / 自然语言描述 / 标签 + 自然语言
   图片列表就是结果列表：每张图一行，缩略图 + 状态 + 反推结果，单张复制 / 移除。
   「开始反推」只跑还没出结果（或结果不是当前模式）的图；要用的模型已下载但没加载时顺手先加载。
   自然语言：本地模型（和 LoRA 训练器共用模型和环境）或云端 API，后端在 vlm_caption_service.py。
   「标签 + 自然语言」分两步：先 WD14 出标签，再把标签交给自然语言模型参考，结果写成「标签. 句子」。 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  const MAX_ROWS = 400;     // 一次最多画这么多行，再多只显示数量（几千张的文件夹也不卡）

  let items = [];           // { path, url, state: pending|queued|running|done|failed, tags, text, caption, mode, error }
  const byPath = new Map(); // 路径（统一大小写/分隔符）-> item
  let mode = "tags";        // tags / text / hybrid
  let modelReady = false;   // WD14
  let loading = false;      // WD14 模型加载中
  let tagging = false;      // 一轮反推进行中（两步模式下两步都算）
  let phase = "";           // 当前这一轮在跑哪一步：tags（WD14）/ text（自然语言）
  let runAfterLoad = false; // 点了「开始反推」时模型还没加载：加载完接着跑
  let runQueued = false;    // 反推过程中又加了图：这一轮结束后自动接着跑
  let targets = [];         // 本轮的图
  let batch = 0;            // 本轮张数
  let batchDone = 0;
  let tagsDone = 0;         // 两步模式第一步完成的张数
  let modelProg = null;
  let tagProg = null;
  let capProg = null;
  let view = "tags";        // 标签显示成 标签块 / 一段文本
  let modelUrls = {};       // model_key -> 仓库页面地址（手动下载用）
  const modelInfo = {};     // key -> { label, cached }

  // 自然语言
  let cap = { models: {}, prompts: {}, presets: [], loaded: null, env: {}, idle: 10 };
  let capPreparing = false;
  let capSource = "local";

  const norm = (p) => String(p || "").replace(/\//g, "\\").toLowerCase();
  const baseName = (p) => String(p).split(/[\\/]/).pop();
  const STATE_TEXT = { pending: "待反推", queued: "排队中", running: "反推中…", done: "完成", failed: "失败" };
  const MODE_HINT = {
    tags: "WD14 反推 Danbooru 标签，速度快，CPU 就能跑",
    text: "用视觉大模型写一段英文描述（Anima / Krea2 / MOSS 这类自然语言底模用）",
    hybrid: "先出 WD14 标签，再把标签交给自然语言模型参考，结果是「标签. 描述」",
  };
  const needTags = () => mode !== "text";
  const needText = () => mode !== "tags";
  const useLocal = () => capSource !== "api";

  function saveCfg(key, value) {
    if (App.cfg) App.cfg[key] = value;
    App.api.update_config({ [key]: value }).catch(() => {});
  }

  const selectedCached = () => {
    const info = modelInfo[$("#wd-model").value];
    return !!(info && info.cached);
  };
  const capKey = () => $("#cap-model").value;
  const capReady = () => !!(cap.loaded && cap.loaded.key === capKey());
  const capDownloaded = () => !!(cap.models[capKey()] || {}).downloaded;
  const isTodo = (it) => it.state === "pending" || it.state === "failed" || (it.state === "done" && it.mode !== mode);
  const todo = () => items.filter(isTodo);
  const busy = () => loading || tagging || capPreparing;

  /* ---------- 按钮 / 摘要 ---------- */
  function refreshControls() {
    const left = todo().length;
    const done = items.filter((it) => it.state === "done").length;
    const failed = items.filter((it) => it.state === "failed").length;
    const pending = left - failed;
    const run = $("#wd-run");
    run.disabled = busy() || !items.length;
    $("#wd-run-label").textContent = (loading || capPreparing) && runAfterLoad ? "正在加载模型…"
      : tagging ? (phase === "tags" && mode === "hybrid" ? "反推标签中…" : "反推中…")
      : !items.length ? "开始反推"
      : pending ? `开始反推（${left} 张）`
      : failed ? `重试失败的（${failed} 张）` : `重新反推全部（${items.length} 张）`;
    $("#wd-cancel").disabled = !busy();
    $("#wd-load").disabled = busy();
    $("#wd-model").disabled = busy();
    $("#wd-clear").disabled = tagging || !items.length;
    $("#wd-copy-all").disabled = !done;
    document.querySelectorAll("#cap-mode .chip, #cap-source .chip").forEach((b) => { b.disabled = busy(); });
    $("#cap-model").disabled = busy();
    $("#cap-quant").disabled = busy();
    $("#cap-load").disabled = busy() || capReady();
    $("#cap-load").textContent = capReady() ? "已加载" : capDownloaded() ? "加载模型" : "下载并加载…";
    $("#cap-release").disabled = busy() || !cap.loaded;
    $("#wd-summary").textContent = items.length
      ? `共 ${items.length} 张 · 完成 ${done}` + (pending ? ` · 待反推 ${pending}` : "") + (failed ? ` · 失败 ${failed}` : "")
      : "";
  }

  function setModelStatus(text, status) {
    const el = $("#wd-model-status");
    el.textContent = text;
    el.dataset.status = status || "";
  }

  function idleModelStatus() {
    if (modelReady) setModelStatus("模型已就绪", "ok");
    else if (selectedCached()) setModelStatus("已下载 · 开始反推时自动加载");
    else setModelStatus("还没下载");
  }

  function updateLoadLabel() {
    $("#wd-load").textContent = selectedCached() ? "加载模型" : "导入 / 下载…";
  }

  function markCached(key) {
    if (!modelInfo[key] || modelInfo[key].cached) return;
    modelInfo[key].cached = true;
    const opt = $("#wd-model").querySelector(`option[value="${key}"]`);
    if (opt) opt.textContent = modelInfo[key].label + "（已缓存）";
    updateLoadLabel();
  }

  /* ---------- 模式 / 自然语言设置 ---------- */
  function applyMode() {
    $("#cap-mode").querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    $("#cap-mode-hint").textContent = MODE_HINT[mode];
    document.querySelectorAll("#page-wd14 [data-modes]").forEach((el) => {
      el.hidden = !el.dataset.modes.split(" ").includes(mode);
    });
    applySource();
    renderList();
  }

  function applySource() {
    $("#cap-source").querySelectorAll("[data-src]").forEach((b) => b.classList.toggle("on", b.dataset.src === capSource));
    $("#cap-local").hidden = !useLocal();
    $("#cap-api").hidden = useLocal();
    const torii = useLocal() && capKey() === "toriigate";
    $("#cap-prompt-generic").hidden = torii;
    $("#cap-prompt-torii").hidden = !torii;
    $("#cap-use-names-row").hidden = !torii;
    capStatus();
  }

  function capStatus(text, status) {
    const el = $("#cap-status");
    if (text == null) {
      if (!useLocal()) { text = $("#cap-api-key").value ? "云端 API" : "填上 API Key 就能用"; status = ""; }
      else if (capReady()) {
        const l = cap.loaded;
        text = `已加载 · ${l.quant || ""}${l.source === "trainer" ? " · 用训练器的环境" : ""} · 闲置 ${cap.idle} 分钟或跑图时自动释放显存`;
        status = "ok";
      } else if (capDownloaded()) {
        const m = cap.models[capKey()];
        text = `已下载（${m.where === "trainer" ? "训练器里" : "启动器里"}）· 开始反推时自动加载`;
        status = "";
      } else { text = "还没下载"; status = ""; }
    }
    el.textContent = text;
    el.dataset.status = status || "";
  }

  function capAbout() {
    const m = cap.models[capKey()] || {};
    const where = m.downloaded ? `已下载：${m.dir}`
      : cap.trainer_models_dir ? `第一次使用会下载到 LoRA 训练器的模型目录（训练器也能直接用）：${cap.trainer_models_dir}`
      : `第一次使用会下载到 ${cap.launcher_models_dir}（以后装了 LoRA 训练器，训练器也会来这里找，不用重下）`;
    const env = cap.env.trainer_env ? "运行环境用 LoRA 训练器的训练环境，不另外安装。"
      : cap.env.launcher_env ? "运行环境：启动器的 vlm_venv。"
      : "第一次使用会建一个独立的运行环境 vlm_venv（PyTorch + transformers，约 4–5 GB，只装一次；装了 LoRA 训练器就直接用训练器的环境）。";
    $("#cap-local-about").innerHTML = App.esc(`${m.label || ""}，约 ${m.size_gb || "?"} GB。${where}`) + "<br>" + App.esc(env) +
      "<br>" + App.esc(`模型加载后常驻显存，连续反推不用重新加载；闲置 ${cap.idle} 分钟、或者发现显卡在跑图（WebUI / ComfyUI 出图）时自动释放，也可以点「释放显存」。`) +
      "<br>精度选「自动」时按剩余显存在 bf16 / 4bit 之间选；显存不够可以换 Qwen3-VL-4B，或者改用云端 API。";
  }

  function capModelLabel(m) {
    return `${m.label} · 约 ${m.size_gb} GB` + (m.downloaded ? (m.where === "trainer" ? " · 训练器已下载" : " · 已下载") : "");
  }

  function renderCapModels() {
    const sel = $("#cap-model");
    const cur = sel.value;
    sel.innerHTML = Object.values(cap.models).map((m) => `<option value="${App.esc(m.key)}">${App.esc(capModelLabel(m))}</option>`).join("");
    if (cur && cap.models[cur]) sel.value = cur;
  }

  function promptOpts() {
    return {
      preset: $("#cap-preset").value, prompt: $("#cap-prompt").value,
      torii_format: $("#cap-torii-format").value, use_names: $("#cap-use-names").checked,
      max_tokens: parseInt($("#cap-max-tokens").value, 10) || 512,
    };
  }

  function apiOpts() {
    return {
      base_url: $("#cap-api-url").value.trim(), model: $("#cap-api-model").value.trim(),
      api_key: $("#cap-api-key").value.trim(), concurrency: parseInt($("#cap-api-conc").value, 10) || 4,
      fallback_local: $("#cap-api-fallback").checked,
    };
  }

  async function refreshCapInfo() {
    try {
      const r = await App.api.caption_info();
      if (!r || r.ok === false) throw new Error((r && r.error) || "读取失败");
      cap.models = {};
      (r.models || []).forEach((m) => { cap.models[m.key] = m; });
      cap.prompts = r.prompts || {};
      cap.presets = r.prompt_labels || [];
      cap.api_presets = r.api_presets || [];
      cap.loaded = r.loaded || null;
      cap.env = r.env || {};
      cap.idle = r.idle_minutes || 10;
      cap.trainer_models_dir = r.trainer_models_dir || "";
      cap.launcher_models_dir = r.launcher_models_dir || "";
      cap.default_model = r.default_model || "toriigate";
      renderCapModels();
    } catch (e) { App.toast("读取自然语言模型列表失败：" + e.message, "error"); }
  }

  // 准备本地自然语言模型：没下载先确认（体积大）
  async function capLoad() {
    const key = capKey();
    const m = cap.models[key] || {};
    if (!m.downloaded) {
      const v = await App.modal("下载自然语言模型",
        App.esc(`「${m.label}」还不在本地，要下载约 ${m.size_gb} GB。\n\n` +
          (cap.trainer_models_dir ? `会下到 LoRA 训练器的模型目录，训练器也能直接用：\n${cap.trainer_models_dir}`
            : `会下到 ${cap.launcher_models_dir}，以后装了 LoRA 训练器也能直接用。`) +
          "\n\n依次尝试 hf-mirror、魔搭和 HuggingFace 官方，断了接着下。" +
          (cap.env.trainer_env || cap.env.launcher_env ? "" : "\n第一次使用还会安装运行环境（PyTorch 等，约 4–5 GB）。") +
          "\n\n不想下载可以切到「云端 API」。").replace(/\n/g, "<br>"),
        [{ id: "go", label: "开始下载", kind: "primary" }, { id: "cancel", label: "取消" }]);
      if (v !== "go") {
        runAfterLoad = false;
        if (tagging) { endRun(); tagProg.hide(); }
        refreshControls();
        return;
      }
    }
    capPreparing = true; refreshControls();
    capStatus("准备中…");
    capProg.hide();
    try {
      const r = await App.api.caption_prepare(key, $("#cap-quant").value);
      if (r && r.ok === false) throw new Error(r.error || "加载失败");
    } catch (e) {
      capPreparing = false; runAfterLoad = false;
      if (tagging) { endRun(); tagProg.hide(); }
      App.toast("加载失败：" + e.message, "error");
      refreshControls(); capStatus();
    }
  }

  /* ---------- 列表 ---------- */
  function tagsBlock(tagStr) {
    const tags = (tagStr || "").split(",").map((t) => t.trim()).filter(Boolean);
    if (!tags.length) return "";
    if (view === "text") return `<div class="wd-text" title="点击全选">${App.esc(tags.join(", "))}</div>`;
    return '<div class="wd-tags">' +
      tags.map((t) => `<span class="wd-tag" title="点击复制这个标签">${App.esc(t)}</span>`).join('<span class="wd-sep">, </span>') +
      "</div>";
  }

  function resultHtml(it) {
    if (it.state === "failed") return `<div class="wd-tags wd-err">${App.esc(it.error || "处理失败")}</div>`;
    if (it.state !== "done") {
      const waitText = tagging && phase === "tags" && mode === "hybrid" && it.tags && it.state === "queued";
      const t = it.state === "running" ? "正在反推…" : waitText ? "标签好了，等描述…" : it.state === "queued" ? "排队中…" : "等待反推";
      return (tagging && mode === "hybrid" && it.state !== "pending" ? tagsBlock(it.tags) : "") + `<div class="wd-tags wd-wait">${t}</div>`;
    }
    let html = "";
    if (it.mode === "tags" || (it.mode === "hybrid" && it.keepTags !== false)) html += tagsBlock(it.tags);
    if (it.mode !== "tags" && it.text) html += `<div class="wd-text wd-desc" title="点击全选">${App.esc(it.text)}</div>`;
    if (!html) {
      return it.mode === "tags"
        ? '<div class="wd-tags wd-wait">没有超过阈值的标签（可以把阈值调低一点再试）</div>'
        : '<div class="wd-tags wd-wait">模型没有给出内容</div>';
    }
    return html;
  }

  function rowHtml(it, i) {
    const thumb = it.url
      ? `<img loading="lazy" decoding="async" alt="" src="${App.esc(it.url)}">`
      : "无预览";
    const doneNote = it.state === "done"
      ? (it.mode !== mode ? `（${{ tags: "标签", text: "自然语言", hybrid: "标签+自然语言" }[it.mode]}）` : "") + (it.txt ? " · 已存 .txt" : "")
      : "";
    return `<div class="wd-item s-${it.state}" data-i="${i}">` +
      `<div class="wd-thumb" title="用系统看图工具打开">${thumb}</div>` +
      `<div class="wd-body"><div class="wd-head">` +
      `<span class="wd-name" title="${App.esc(it.path)}">${App.esc(baseName(it.path))}</span>` +
      `<span class="wd-state">${STATE_TEXT[it.state] || ""}${doneNote}</span>` +
      `<span class="wd-acts">` +
      (it.state === "done" ? '<button class="btn btn-xs" data-act="copy">复制</button>' : "") +
      (tagging ? "" : '<button class="btn btn-xs" data-act="remove">移除</button>') +
      `</span></div>${resultHtml(it)}</div></div>`;
  }

  function renderList() {
    const box = $("#wd-images");
    if (!items.length) {
      box.innerHTML = '<div class="drop-empty"><b>把图片 / 文件夹拖到这里</b>' +
        "<span>或者 Ctrl+V 粘贴剪贴板里的图片，也可以点上面的按钮选择</span></div>";
    } else {
      const shown = items.slice(0, MAX_ROWS);
      box.innerHTML = shown.map(rowHtml).join("") +
        (items.length > MAX_ROWS ? `<div class="wd-more">另外 ${items.length - MAX_ROWS} 张没有列出来（同样会反推，结果见各自的 .txt）</div>` : "");
    }
    refreshControls();
  }

  function renderItem(it) {
    const i = items.indexOf(it);
    const old = i >= 0 && i < MAX_ROWS && $("#wd-images").querySelector(`.wd-item[data-i="${i}"]`);
    if (old) old.outerHTML = rowHtml(it, i);
    refreshControls();
  }

  function addItems(list) {
    let added = 0;
    (list || []).forEach((x) => {
      const path = typeof x === "string" ? x : x.path;
      if (!path || byPath.has(norm(path))) return;
      const it = { path, url: (x && x.url) || "", state: "pending", tags: "", text: "", caption: "", mode: "", error: "" };
      items.push(it);
      byPath.set(norm(path), it);
      added++;
    });
    return added;
  }

  // 自动开始：要用的模型都已经在本地（或者用 API）才自动跑，不然等用户点（第一次要下载好几 GB）
  const canAutoRun = () => (!needTags() || modelReady || selectedCached())
    && (!needText() || !useLocal() || capReady() || capDownloaded());

  async function addPaths(paths) {
    if (!paths || !paths.length) return;
    try {
      const r = await App.api.wd14_expand(paths);
      const added = addItems(r.items || r.paths || []);
      renderList();
      if (!added) { App.toast("没有找到新的图片文件"); return; }
      if ($("#wd-auto-run").checked && canAutoRun()) {
        if (tagging) runQueued = true;
        else if (loading || capPreparing) { runAfterLoad = true; refreshControls(); }
        else startRun();
      } else if (added > 1 || items.length > 1) {
        App.toast(`已添加 ${added} 张图片（共 ${items.length} 张）`, "ok");
      }
    } catch (e) { App.toast("展开路径失败：" + e.message, "error"); }
  }

  /* ---------- WD14 模型 ---------- */
  async function doImport() {
    const r = await App.api.choose_files(["WD14 模型文件 (*.onnx;*.csv)", "所有文件 (*.*)"], true);
    if (!r || !r.ok) { if (r && r.error) App.toast("打开文件选择失败：" + r.error, "error"); return; }
    if (!r.paths || !r.paths.length) return;   // 取消了
    loading = true; refreshControls();
    setModelStatus("导入中…");
    try {
      const res = await App.api.wd14_import_model($("#wd-model").value, r.paths);
      if (res && res.ok === false) {
        loading = false; runAfterLoad = false; refreshControls();
        setModelStatus("导入失败", "bad");
        App.modal("导入失败", App.esc(res.error || ""), [{ id: "ok", label: "知道了", kind: "primary" }]);
        return;
      }
      markCached($("#wd-model").value);
      App.toast("模型文件已导入，正在准备独立运行环境（首次需要下载约几十 MB）…", "ok");
    } catch (e) {
      loading = false; runAfterLoad = false; refreshControls();
      App.toast("导入失败：" + e.message, "error");
    }
  }

  // 准备模型：已缓存直接加载；没下载先问「导入」还是「下载」，下载再二次确认
  async function loadModel() {
    const key = $("#wd-model").value;
    if (!selectedCached()) {
      const label = (modelInfo[key] && modelInfo[key].label) || key;
      const v = await App.modal("模型还没有下载",
        App.esc(`「${label}」还不在本地。可以导入自己下好的 model.onnx 和 selected_tags.csv，也可以在线下载。`),
        [{ id: "import", label: "从本地导入…", kind: "primary" },
         { id: "download", label: "在线下载" },
         { id: "cancel", label: "取消" }]);
      if (v === "import") { await doImport(); return; }
      if (v !== "download") { runAfterLoad = false; refreshControls(); return; }
      const ok = await App.modal("确认下载",
        App.esc(`将下载「${label}」。\n\n会依次尝试国内镜像和 HuggingFace 官方源，支持断点续传；` +
                "首次使用还会安装独立运行环境（约几十 MB）。确定开始下载吗？"),
        [{ id: "go", label: "开始下载", kind: "primary" }, { id: "cancel", label: "取消" }]);
      if (ok !== "go") { runAfterLoad = false; refreshControls(); return; }
    }
    loading = true; refreshControls();
    setModelStatus("加载中…");
    try {
      const r = await App.api.wd14_load_model(key);
      if (r && r.ok === false) throw new Error(r.error || "加载失败");
    } catch (e) {
      App.toast("加载失败：" + e.message, "error");
      loading = false; runAfterLoad = false; refreshControls(); idleModelStatus();
    }
  }

  /* ---------- 反推 ---------- */
  async function startRun() {
    if (busy() || !items.length) return;
    // 要用的模型还没加载：先加载，加载完（ready 事件）再回到这里
    if (needTags() && !modelReady) { runAfterLoad = true; refreshControls(); await loadModel(); return; }
    if (needText() && useLocal() && !capReady()) { runAfterLoad = true; refreshControls(); await capLoad(); return; }
    if (needText() && !useLocal()) {
      const a = apiOpts();
      if (!a.base_url || !a.model) { App.toast("请先填好 API 的接口地址和模型名", "error"); return; }
    }
    targets = todo();
    if (!targets.length) targets = items.slice();   // 全部完成了：改了参数想重来
    targets.forEach((it) => { it.state = "queued"; it.error = ""; if (mode === "hybrid") it.tags = ""; });
    targets[0].state = "running";
    batch = targets.length; batchDone = 0; tagsDone = 0;
    tagging = true; runQueued = false;
    renderList();
    if (needTags()) await runTags(); else await runText();
  }

  const saveTxt = () => $("#wd-save-txt").checked && items.length > 1;   // 单张图只看结果，不落 .txt

  async function runTags() {
    phase = "tags";
    tagProg.set(0, mode === "hybrid" ? `标签 0/${batch}` : `反推中 0/${batch}`);
    try {
      const r = await App.api.wd14_tag(
        targets.map((it) => it.path), $("#wd-general").value / 100, $("#wd-character").value / 100,
        $("#wd-include-rating").checked,
        mode === "tags" && saveTxt());   // 两步模式最后由自然语言那步写 .txt
      if (r && r.ok === false) throw new Error(r.error || "无法开始反推");
    } catch (e) {
      App.toast(e.message, "error");
      endRun();
      tagProg.hide();
    }
  }

  async function runText() {
    phase = "text";
    const list = targets.filter((it) => it.state === "queued" || it.state === "running");
    if (!list.length) { finishRun(false); return; }
    // 两步之间模型被自动释放了（闲置 / 跑图）：重新加载再接着跑
    if (useLocal() && !capReady()) { runAfterLoad = true; refreshControls(); await capLoad(); return; }
    list.forEach((it, i) => { it.state = i ? "queued" : "running"; });
    renderList();
    tagProg.set(0, `${mode === "hybrid" ? "描述" : "反推中"} 0/${list.length}`);
    const tags = {};
    if (mode === "hybrid") list.forEach((it) => { if (it.tags) tags[it.path] = it.tags; });
    try {
      const r = await App.api.caption_run(list.map((it) => it.path), {
        source: capSource, tags, feed_tags: $("#cap-feed-tags").checked, keep_tags: $("#cap-keep-tags").checked,
        save_txt: saveTxt(), prompt: promptOpts(), api: useLocal() ? null : apiOpts(),
      });
      if (r && r.ok === false) {
        if (/加载模型/.test(r.error || "") && useLocal()) { cap.loaded = null; runAfterLoad = true; await capLoad(); return; }
        throw new Error(r.error || "无法开始反推");
      }
    } catch (e) {
      App.toast(e.message, "error");
      endRun();
      tagProg.hide();
    }
  }

  // 结束一轮：没轮到的退回「待反推」，下次接着跑
  function endRun() {
    tagging = false; phase = "";
    items.forEach((it) => { if (it.state === "queued" || it.state === "running") it.state = "pending"; });
    renderList();
  }

  function finishRun(cancelled) {
    endRun();
    const failed = items.filter((it) => it.state === "failed").length;
    tagProg.set(100, cancelled ? `已取消 · 完成 ${batchDone}/${batch}`
      : `完成 ${batch} 张` + (failed ? ` · 失败 ${failed}` : ""));
    if (runQueued && !cancelled && todo().some((it) => it.state === "pending")) { startRun(); return; }
    if (batch > 1 && !cancelled) App.toast(`反推完成，共 ${batch} 张` + (failed ? `，${failed} 张失败` : ""), failed ? "error" : "ok");
  }

  function advance(it) {
    batchDone++;
    const next = items.find((x) => x.state === "queued");
    if (next) { next.state = "running"; renderItem(next); }
    renderItem(it);
  }

  function copyAll() {
    const done = items.filter((it) => it.state === "done");
    if (!done.length) return;
    const text = done.length === 1 ? done[0].caption
      : done.map((it) => `【${baseName(it.path)}】\n${it.caption}`).join("\n\n");
    App.copy(text, `已复制 ${done.length} 张图的结果`);
  }

  /* ---------- 初始化 ---------- */
  async function initCaption(cfg) {
    capProg = App.progress($("#cap-progress"));
    capProg.hide();
    await refreshCapInfo();
    const sel = $("#cap-model");
    sel.value = cap.models[cfg.caption_model] ? cfg.caption_model : (cap.default_model || "toriigate");
    sel.addEventListener("change", () => { saveCfg("caption_model", sel.value); applySource(); capAbout(); refreshControls(); });
    const quant = $("#cap-quant");
    quant.value = cfg.caption_quant || "auto";
    quant.addEventListener("change", () => { saveCfg("caption_quant", quant.value); });

    // 提示词模板：选预设就显示当前版本的预设文字，改了文字自动变成「自定义」
    const pre = $("#cap-preset"), ta = $("#cap-prompt");
    pre.innerHTML = cap.presets.map(([k, label]) => `<option value="${App.esc(k)}">${App.esc(label)}</option>`).join("");
    pre.value = cfg.caption_preset && cap.presets.some(([k]) => k === cfg.caption_preset) ? cfg.caption_preset : "detailed";
    const showPrompt = () => { ta.value = pre.value === "custom" ? (App.cfg.caption_prompt || "") : (cap.prompts[pre.value] || ""); };
    showPrompt();
    pre.addEventListener("change", () => { saveCfg("caption_preset", pre.value); showPrompt(); });
    ta.addEventListener("change", () => {
      if (pre.value !== "custom") { pre.value = "custom"; saveCfg("caption_preset", "custom"); }
      saveCfg("caption_prompt", ta.value);
    });
    const tf = $("#cap-torii-format");
    if (cfg.caption_torii_format) tf.value = cfg.caption_torii_format;
    tf.addEventListener("change", () => saveCfg("caption_torii_format", tf.value));
    const mt = $("#cap-max-tokens");
    mt.value = cfg.caption_max_tokens || 512;
    mt.addEventListener("change", () => saveCfg("caption_max_tokens", parseInt(mt.value, 10) || 512));
    [["#cap-use-names", "caption_use_names"], ["#cap-feed-tags", "caption_feed_tags"], ["#cap-keep-tags", "caption_keep_tags"],
     ["#cap-api-fallback", "caption_api_fallback"]].forEach(([id, key]) => {
      const el = $(id);
      el.checked = typeof cfg[key] === "boolean" ? cfg[key] : true;
      el.addEventListener("change", () => saveCfg(key, el.checked));
    });

    // 云端 API
    const ap = $("#cap-api-preset");
    ap.innerHTML = (cap.api_presets || []).map((x) => `<option>${App.esc(x.name)}</option>`).join("") + "<option>自定义</option>";
    ap.value = cfg.caption_api_preset || "Google Gemini";
    ap.addEventListener("change", () => {
      const x = (cap.api_presets || []).find((p) => p.name === ap.value);
      saveCfg("caption_api_preset", ap.value);
      if (x) {
        $("#cap-api-url").value = x.base_url; $("#cap-api-model").value = x.model;
        saveCfg("caption_api_base_url", x.base_url); saveCfg("caption_api_model", x.model);
      }
    });
    [["#cap-api-url", "caption_api_base_url"], ["#cap-api-model", "caption_api_model"], ["#cap-api-key", "caption_api_key"]]
      .forEach(([id, key]) => {
        const el = $(id);
        el.value = cfg[key] || "";
        el.addEventListener("change", () => {
          saveCfg(key, el.value.trim());
          if (key !== "caption_api_key" && ap.value !== "自定义") { ap.value = "自定义"; saveCfg("caption_api_preset", "自定义"); }
          capStatus();
        });
      });
    // 地址 / 模型名还空着（老配置）：按选中的服务商填上
    const cur = (cap.api_presets || []).find((p) => p.name === ap.value);
    if (cur && !$("#cap-api-url").value) { $("#cap-api-url").value = cur.base_url; $("#cap-api-model").value = cur.model; }
    const conc = $("#cap-api-conc");
    conc.value = cfg.caption_api_concurrency || 4;
    conc.addEventListener("change", () => saveCfg("caption_api_concurrency", Math.max(1, Math.min(16, parseInt(conc.value, 10) || 4))));
    $("#cap-api-test").addEventListener("click", async () => {
      const msg = $("#cap-api-test-msg");
      msg.textContent = "测试中…"; msg.className = "hint";
      try {
        const r = await App.api.caption_test_api(apiOpts());
        msg.textContent = r.message || (r.ok ? "连接成功" : "失败");
        msg.className = "hint " + (r.ok ? "ok" : "bad");
      } catch (e) { msg.textContent = e.message; msg.className = "hint bad"; }
    });

    capSource = cfg.caption_source === "api" ? "api" : "local";
    $("#cap-source").addEventListener("click", (e) => {
      const b = e.target.closest("[data-src]");
      if (!b || b.dataset.src === capSource || busy()) return;
      capSource = b.dataset.src;
      saveCfg("caption_source", capSource);
      applySource(); refreshControls();
    });

    $("#cap-load").addEventListener("click", () => { if (!capReady()) capLoad(); });
    $("#cap-release").addEventListener("click", async () => {
      const r = await App.api.caption_release();
      if (r && r.ok === false) App.toast(r.error || "释放失败", "error");
    });
    $("#cap-open-repo").addEventListener("click", () => {
      const m = cap.models[capKey()] || {};
      const urls = m.urls || [];
      App.modal("手动下载模型",
        `<div class="dim" style="margin-bottom:10px">把仓库里的文件（*.safetensors、*.json、tokenizer 等）全部下到<br>` +
        `<b>${App.esc((cap.trainer_models_dir || cap.launcher_models_dir) + "\\" + (m.repo || "").replace("/", "__"))}</b><br>` +
        `再回来点「加载模型」。在线下载一般更省事（自动换源、断点续传、校验文件）。</div>` +
        `<div class="choice-list">` + urls.map((u, i) => `<div class="choice-item" data-i="${i}">${App.esc(u)}</div>`).join("") + `</div>`,
        [{ id: "cancel", label: "关闭" }],
        { onOpen(body) { body.querySelectorAll(".choice-item").forEach((el) => el.addEventListener("click", () => App.api.open_url(urls[+el.dataset.i]))); } });
    });

    App.on("caption", "prep_log", (e) => { capStatus(String(e.text || "").slice(0, 120)); });
    App.on("caption", "log", App.pageLog("wd14", "自然语言反推"));
    App.on("caption", "prep_progress", (e) => {
      const pct = e.total > 0 ? (e.downloaded / e.total * 100) : 0;
      capProg.set(pct, `${e.stage === "env" ? "运行环境 · " : ""}${e.name || ""}：${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}`);
    });
    App.on("caption", "ready", async (e) => {
      capPreparing = false;
      capProg.hide();
      await refreshCapInfo();
      cap.loaded = e;
      $("#cap-model").value = e.key;
      capAbout(); applySource();
      if (runAfterLoad) {
        runAfterLoad = false; refreshControls();
        if (tagging && phase === "text") runText(); else if (!tagging) startRun();
      } else { App.toast("自然语言模型加载完成", "ok"); refreshControls(); }
    });
    App.on("caption", "prep_failed", (e) => {
      capPreparing = false; runAfterLoad = false;
      capProg.hide();
      if (tagging) { endRun(); tagProg.hide(); }
      refreshControls(); capStatus("加载失败", "bad");
      if (e.msg === "已取消" || e.msg === "下载已取消") return;
      App.modal("自然语言模型准备失败", `<pre class="modal-pre">${App.esc(e.msg || "")}</pre>`,
        [{ id: "ok", label: "知道了", kind: "primary" }]);
    });
    App.on("caption", "released", (e) => {
      cap.loaded = null;
      refreshControls(); capStatus();
      if (e.reason) App.toast(e.reason);
    });
    App.on("caption", "progress", (e) => {
      tagProg.set(e.total > 0 ? e.done / e.total * 100 : 0, `${mode === "hybrid" ? "描述" : "反推中"} ${e.done}/${e.total}`);
    });
    App.on("caption", "item", (e) => {
      const it = byPath.get(norm(e.path));
      if (!it) return;   // 反推过程中被移除了
      if (e.error) { it.state = "failed"; it.error = e.error; }
      else {
        it.state = "done"; it.mode = mode; it.text = e.text || ""; it.caption = e.caption || "";
        it.keepTags = $("#cap-keep-tags").checked; it.txt = !!e.txt;
        if (mode === "text") it.tags = "";
      }
      advance(it);
    });
    App.on("caption", "done", (e) => { finishRun(!!e.cancelled); });
    App.on("caption", "failed", (e) => {
      endRun(); tagProg.hide();
      App.modal("自然语言反推失败", `<pre class="modal-pre">${App.esc(e.msg || "")}</pre>`,
        [{ id: "ok", label: "知道了", kind: "primary" }]);
    });

    capAbout();
  }

  App.pages.wd14 = {
    async init() {
      modelProg = App.progress($("#wd-model-progress"));
      tagProg = App.progress($("#wd-tag-progress"));
      modelProg.hide();
      tagProg.hide();
      const cfg = App.cfg || {};
      mode = ["tags", "text", "hybrid"].includes(cfg.caption_mode) ? cfg.caption_mode : "tags";

      // 模型列表
      try {
        const r = await App.api.wd14_models();
        const sel = $("#wd-model");
        (r.models || []).forEach((m) => {
          const o = document.createElement("option");
          o.value = m.key;
          o.textContent = m.label + (m.cached ? (m.where === "trainer" ? "（训练器已下载）" : "（已缓存）") : "");
          sel.appendChild(o);
          modelUrls[m.key] = m.urls || [];
          modelInfo[m.key] = { label: m.label, cached: !!m.cached };
        });
        if (cfg.wd14_model && modelInfo[cfg.wd14_model]) sel.value = cfg.wd14_model;
        // 换了模型就得重新加载（后端只记着当前加载的那一个）
        sel.addEventListener("change", () => {
          modelReady = false;
          updateLoadLabel(); idleModelStatus();
          saveCfg("wd14_model", sel.value);
        });
        updateLoadLabel();
        modelReady = !!r.model_ready;
        idleModelStatus();
      } catch (e) { App.toast("读取模型列表失败：" + e.message, "error"); }

      // 参数：改了就记住
      const sliders = [["#wd-general", "wd14_general"], ["#wd-character", "wd14_character"]];
      sliders.forEach(([id, key]) => {
        const el = $(id), out = $(id + "-val");
        if (typeof cfg[key] === "number") el.value = Math.round(cfg[key] * 100);
        const show = () => { out.textContent = (el.value / 100).toFixed(2); };
        show();
        el.addEventListener("input", show);
        el.addEventListener("change", () => saveCfg(key, el.value / 100));
      });
      [["#wd-auto-run", "wd14_auto_run"], ["#wd-save-txt", "wd14_save_txt"], ["#wd-include-rating", "wd14_include_rating"]]
        .forEach(([id, key]) => {
          const el = $(id);
          if (typeof cfg[key] === "boolean") el.checked = cfg[key];
          el.addEventListener("change", () => saveCfg(key, el.checked));
        });

      // 标签显示方式：标签块（点一下复制单个） / 文本（整段可选中复制）
      const setView = (v) => {
        view = v === "text" ? "text" : "tags";
        $("#wd-view").querySelectorAll("[data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
      };
      setView(cfg.wd14_view);
      $("#wd-view").addEventListener("click", (e) => {
        const b = e.target.closest("[data-view]");
        if (!b || b.dataset.view === view) return;
        setView(b.dataset.view);
        saveCfg("wd14_view", view);
        renderList();
      });

      $("#cap-mode").addEventListener("click", (e) => {
        const b = e.target.closest("[data-mode]");
        if (!b || b.dataset.mode === mode || busy()) return;
        mode = b.dataset.mode;
        saveCfg("caption_mode", mode);
        applyMode();
      });

      $("#wd-load").addEventListener("click", loadModel);

      // 下载失败时的退路：自己去网页下好，再把文件导进来
      $("#wd-open-repo").addEventListener("click", () => {
        const urls = modelUrls[$("#wd-model").value] || [];
        if (!urls.length) { App.toast("没有这个模型的下载地址", "error"); return; }
        App.modal("手动下载模型",
          `<div class="dim" style="margin-bottom:10px">在下面的页面里找到 <b>model.onnx</b> 和 <b>selected_tags.csv</b>，` +
          `两个都下载下来，然后回来点「从本地导入…」，按住 Ctrl 同时选中这两个文件。<br><br>` +
          `国内一般第一个能打开；model.onnx 体积较大，建议用下载工具拖。</div>` +
          `<div class="choice-list">` +
          urls.map((u, i) => `<div class="choice-item" data-i="${i}">${App.esc(u)}</div>`).join("") +
          `</div>`,
          [{ id: "cancel", label: "关闭" }],
          {
            onOpen(body) {
              body.querySelectorAll(".choice-item").forEach((el) =>
                el.addEventListener("click", () => App.api.open_url(urls[+el.dataset.i])));
            },
          });
      });

      $("#wd-import").addEventListener("click", doImport);

      $("#wd-pick-files").addEventListener("click", async () => {
        const r = await App.api.choose_images(true);
        if (r && r.ok && r.paths && r.paths.length) addPaths(r.paths);
      });
      $("#wd-pick-folder").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择图片文件夹", App.cfg.webui_root || "");
        if (r && r.ok && r.path) addPaths([r.path]);
      });
      $("#wd-clear").addEventListener("click", () => {
        items = []; byPath.clear(); tagProg.hide(); renderList();
      });
      $("#wd-copy-all").addEventListener("click", copyAll);
      $("#wd-run").addEventListener("click", startRun);
      $("#wd-cancel").addEventListener("click", () => {
        runQueued = false;
        if (capPreparing) { runAfterLoad = false; App.api.caption_cancel(); }
        if (loading) { runAfterLoad = false; App.api.wd14_cancel(); }
        if (tagging && !capPreparing) {
          if (phase === "text") App.api.caption_cancel(); else App.api.wd14_cancel();
        }
        refreshControls();
      });

      // 行内操作：复制 / 移除 / 点标签复制单个 / 点缩略图用系统看图工具打开
      const box = $("#wd-images");
      box.addEventListener("click", (e) => {
        const row = e.target.closest(".wd-item");
        if (!row) return;
        const it = items[+row.dataset.i];
        if (!it) return;
        const act = e.target.closest("[data-act]");
        if (act && act.dataset.act === "copy") { App.copy(it.caption, "已复制"); return; }
        if (act && act.dataset.act === "remove") {
          items.splice(items.indexOf(it), 1);
          byPath.delete(norm(it.path));
          renderList();
          return;
        }
        const text = e.target.closest(".wd-text");
        if (text) {
          if (String(window.getSelection() || "")) return;   // 已经在拖选一部分，别打断
          window.getSelection().selectAllChildren(text);
          return;
        }
        const tag = e.target.closest(".wd-tag");
        if (tag) {
          if (String(window.getSelection() || "")) return;   // 在拖选文字，别打断
          App.copy(tag.textContent, `已复制「${tag.textContent}」`);
          return;
        }
        if (e.target.closest(".wd-thumb")) {
          App.api.reveal_image(it.path).then((r) => {
            if (r && r.ok === false) App.toast(r.error || "打开失败", "error");
          });
        }
      });

      // 拖放：完整路径由 core.js bindFileDrop 拿到后调用 onDropped；
      // 这里仅作为少数能拿到 .path 的平台的兜底
      ["dragover", "dragenter"].forEach((ev) => box.addEventListener(ev, (e) => {
        e.preventDefault(); box.classList.add("dragover");
      }));
      ["dragleave", "drop"].forEach((ev) => box.addEventListener(ev, (e) => {
        e.preventDefault(); box.classList.remove("dragover");
      }));
      box.addEventListener("drop", (e) => {
        const paths = Array.from(e.dataTransfer.files || []).map((f) => f.path).filter(Boolean);
        if (paths.length) addPaths(paths);
      });

      // 剪贴板粘贴：Ctrl+V 把截图/复制的图片直接加进列表。
      // 剪贴板里的 File 没有磁盘路径，读成 base64 交给 Python 落成临时文件。
      const abToB64 = (buf) => {
        const bytes = new Uint8Array(buf);
        let s = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
          s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }
        return btoa(s);
      };
      document.addEventListener("paste", (e) => {
        if (App.currentPage !== "wd14") return;
        const t = e.target;
        if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
        const clip = e.clipboardData && e.clipboardData.items;
        if (!clip) return;
        const files = [];
        for (const it of clip) {
          if (it.kind === "file" && (it.type || "").startsWith("image/")) {
            const f = it.getAsFile();
            if (f) files.push(f);
          }
        }
        if (!files.length) return;
        e.preventDefault();
        files.forEach(async (f) => {
          try {
            const b64 = abToB64(await f.arrayBuffer());
            const r = await App.api.wd14_add_clipboard_image(b64, f.name || "");
            if (r && r.ok && r.path) addPaths([r.path]);
            else App.toast((r && r.error) || "粘贴图片失败", "error");
          } catch (err) { App.toast("粘贴图片失败：" + err.message, "error"); }
        });
      });

      App.on("wd14", "model_log", (e) => setModelStatus(e.text));
      App.on("wd14", "model_progress", (e) => {
        const pct = e.total > 0 ? (e.downloaded / e.total * 100) : 0;
        modelProg.set(pct, `${e.name}：${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}`);
      });
      App.on("wd14", "model_ready", () => {
        markCached($("#wd-model").value);
        loading = false; modelReady = true;
        modelProg.hide();
        idleModelStatus();
        if (runAfterLoad) { runAfterLoad = false; startRun(); }
        else { App.toast("模型加载完成", "ok"); refreshControls(); }
      });
      App.on("wd14", "model_failed", (e) => {
        loading = false; modelReady = false; runAfterLoad = false;
        modelProg.hide();
        setModelStatus("加载失败", "bad");
        refreshControls();
        if (e.msg === "已取消") return;
        App.modal("模型加载失败",
          `<pre class="modal-pre">${App.esc(e.msg || "")}</pre>`,
          [{ id: "ok", label: "知道了", kind: "primary" }]);
      });

      App.on("wd14", "tag_log", App.pageLog("wd14", "WD14 反推"));
      App.on("wd14", "tag_progress", (e) => {
        const total = Math.min(e.total || batch, batch) || e.total;
        const n = Math.min(e.done, total);
        tagProg.set(total > 0 ? n / total * 100 : 0, `${mode === "hybrid" ? "标签" : "反推中"} ${n}/${total}`);
      });
      App.on("wd14", "tag_item", (e) => {
        const it = byPath.get(norm(e.path));
        if (!it) return;   // 反推过程中被移除了
        if (phase === "tags" && mode === "hybrid") {   // 两步模式：标签好了，等自然语言那步
          tagsDone++;
          it.tags = e.tags || "";
          it.state = "queued";
          const next = targets.find((x) => x.state === "queued" && x !== it && tagsDone < batch && !x.tags);
          if (next) { next.state = "running"; renderItem(next); }
          renderItem(it);
          return;
        }
        it.tags = e.tags || "";
        it.state = "done"; it.mode = "tags"; it.caption = it.tags; it.text = ""; it.txt = !!e.txt;
        advance(it);
      });
      App.on("wd14", "tag_item_failed", (e) => {
        const it = byPath.get(norm(e.path));
        if (!it) return;
        if (phase === "tags" && mode === "hybrid") {   // 标签没出来也照样写描述（只是没有标签参考）
          tagsDone++;
          it.state = "queued"; it.tags = "";
          renderItem(it);
          return;
        }
        it.state = "failed"; it.error = e.error || "处理失败";
        advance(it);
      });
      App.on("wd14", "tag_done", () => {
        if (phase === "tags" && mode === "hybrid") {
          if (tagsDone < batch) { finishRun(true); return; }   // 第一步被取消了
          runText();
          return;
        }
        finishRun(batchDone < batch);
      });
      App.on("wd14", "tag_failed", (e) => {
        endRun(); tagProg.hide();
        App.modal("反推失败", `<pre class="modal-pre">${App.esc(e.msg || "")}</pre>`,
          [{ id: "ok", label: "知道了", kind: "primary" }]);
      });

      await initCaption(cfg);
      applyMode();
    },

    onDropped(paths) {
      addPaths(paths);
    },
  };
})();
