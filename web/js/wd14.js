/* wd14.js — WD14 标签反推页
   图片列表就是结果列表：每张图一行，缩略图 + 状态 + 反推出的标签，单张复制 / 移除。
   「开始反推」只跑还没出结果的图；模型已下载但没加载时顺手先加载，不用先点一次「加载模型」。 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  const MAX_ROWS = 400;     // 一次最多画这么多行，再多只显示数量（几千张的文件夹也不卡）

  let items = [];           // { path, url, state: pending|queued|running|done|failed, tags, error }
  const byPath = new Map(); // 路径（统一大小写/分隔符）-> item
  let modelReady = false;
  let loading = false;
  let tagging = false;
  let runAfterLoad = false; // 点了「开始反推」时模型还没加载：加载完接着跑
  let runQueued = false;    // 反推过程中又加了图：这一轮结束后自动接着跑
  let batch = 0;            // 本轮张数
  let batchDone = 0;
  let modelProg = null;
  let tagProg = null;
  let modelUrls = {};       // model_key -> 仓库页面地址（手动下载用）
  const modelInfo = {};     // key -> { label, cached }

  const norm = (p) => String(p || "").replace(/\//g, "\\").toLowerCase();
  const baseName = (p) => String(p).split(/[\\/]/).pop();
  const STATE_TEXT = { pending: "待反推", queued: "排队中", running: "反推中…", done: "完成", failed: "失败" };

  function saveCfg(key, value) {
    if (App.cfg) App.cfg[key] = value;
    App.api.update_config({ [key]: value }).catch(() => {});
  }

  const selectedCached = () => {
    const info = modelInfo[$("#wd-model").value];
    return !!(info && info.cached);
  };
  const todo = () => items.filter((it) => it.state === "pending" || it.state === "failed");

  /* ---------- 按钮 / 摘要 ---------- */
  function refreshControls() {
    const left = todo().length;
    const done = items.filter((it) => it.state === "done").length;
    const failed = items.filter((it) => it.state === "failed").length;
    const pending = left - failed;
    const busy = loading || tagging;
    const run = $("#wd-run");
    run.disabled = busy || !items.length;
    $("#wd-run-label").textContent = loading && runAfterLoad ? "正在加载模型…"
      : tagging ? "反推中…"
      : !items.length ? "开始反推"
      : pending ? `开始反推（${left} 张）`
      : failed ? `重试失败的（${failed} 张）` : `重新反推全部（${items.length} 张）`;
    $("#wd-cancel").disabled = !busy;
    $("#wd-load").disabled = busy;
    $("#wd-model").disabled = busy;
    $("#wd-clear").disabled = tagging || !items.length;
    $("#wd-copy-all").disabled = !done;
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

  // 按钮文字跟着选中模型的缓存状态走：已在本地 =「加载模型」，没有 =「导入 / 下载…」
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

  /* ---------- 列表 ---------- */
  function tagsHtml(it) {
    if (it.state === "failed") return `<div class="wd-tags wd-err">${App.esc(it.error || "处理失败")}</div>`;
    if (it.state !== "done") {
      const t = it.state === "running" ? "正在反推…" : it.state === "queued" ? "排队中…" : "等待反推";
      return `<div class="wd-tags wd-wait">${t}</div>`;
    }
    const tags = (it.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
    if (!tags.length) return '<div class="wd-tags wd-wait">没有超过阈值的标签（可以把阈值调低一点再试）</div>';
    return '<div class="wd-tags">' +
      tags.map((t) => `<span class="wd-tag" title="点击复制这个标签">${App.esc(t)}</span>`).join('<span class="wd-sep">, </span>') +
      "</div>";
  }

  function rowHtml(it, i) {
    const thumb = it.url
      ? `<img loading="lazy" decoding="async" alt="" src="${App.esc(it.url)}">`
      : "无预览";
    return `<div class="wd-item s-${it.state}" data-i="${i}">` +
      `<div class="wd-thumb" title="用系统看图工具打开">${thumb}</div>` +
      `<div class="wd-body"><div class="wd-head">` +
      `<span class="wd-name" title="${App.esc(it.path)}">${App.esc(baseName(it.path))}</span>` +
      `<span class="wd-state">${STATE_TEXT[it.state] || ""}${it.state === "done" && it.txt ? " · 已存 .txt" : ""}</span>` +
      `<span class="wd-acts">` +
      (it.state === "done" ? '<button class="btn btn-xs" data-act="copy">复制</button>' : "") +
      (tagging ? "" : '<button class="btn btn-xs" data-act="remove">移除</button>') +
      `</span></div>${tagsHtml(it)}</div></div>`;
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
      const it = { path, url: (x && x.url) || "", state: "pending", tags: "", error: "" };
      items.push(it);
      byPath.set(norm(path), it);
      added++;
    });
    return added;
  }

  async function addPaths(paths) {
    if (!paths || !paths.length) return;
    try {
      const r = await App.api.wd14_expand(paths);
      const added = addItems(r.items || r.paths || []);
      renderList();
      if (!added) { App.toast("没有找到新的图片文件"); return; }
      if ($("#wd-auto-run").checked && (modelReady || selectedCached())) {
        if (tagging) runQueued = true;
        else if (loading) { runAfterLoad = true; refreshControls(); }
        else startRun();
      } else if (added > 1 || items.length > 1) {
        App.toast(`已添加 ${added} 张图片（共 ${items.length} 张）`, "ok");
      }
    } catch (e) { App.toast("展开路径失败：" + e.message, "error"); }
  }

  /* ---------- 模型 ---------- */
  // 导入自己下好的模型：直接选文件（选文件夹的窗口里看不到文件，容易选错、以为没反应）
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
    if (tagging || loading || !items.length) return;
    if (!modelReady) { runAfterLoad = true; refreshControls(); await loadModel(); return; }
    let targets = todo();
    if (!targets.length) targets = items.slice();   // 全部完成了：改了阈值想重来
    targets.forEach((it) => { it.state = "queued"; it.error = ""; });
    targets[0].state = "running";
    batch = targets.length; batchDone = 0;
    tagging = true; runQueued = false;
    renderList();
    tagProg.set(0, `反推中 0/${batch}`);
    try {
      const r = await App.api.wd14_tag(
        targets.map((it) => it.path), $("#wd-general").value / 100, $("#wd-character").value / 100,
        $("#wd-include-rating").checked, $("#wd-save-txt").checked);
      if (r && r.ok === false) throw new Error(r.error || "无法开始反推");
    } catch (e) {
      App.toast(e.message, "error");
      endRun();
      tagProg.hide();
    }
  }

  // 结束一轮：没轮到的退回「待反推」，下次接着跑
  function endRun() {
    tagging = false;
    items.forEach((it) => { if (it.state === "queued" || it.state === "running") it.state = "pending"; });
    renderList();
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
    const text = done.length === 1 ? done[0].tags
      : done.map((it) => `【${baseName(it.path)}】\n${it.tags}`).join("\n\n");
    App.copy(text, `已复制 ${done.length} 张图的标签`);
  }

  App.pages.wd14 = {
    async init() {
      modelProg = App.progress($("#wd-model-progress"));
      tagProg = App.progress($("#wd-tag-progress"));
      modelProg.hide();
      tagProg.hide();
      const cfg = App.cfg || {};

      // 模型列表
      try {
        const r = await App.api.wd14_models();
        const sel = $("#wd-model");
        (r.models || []).forEach((m) => {
          const o = document.createElement("option");
          o.value = m.key;
          o.textContent = m.label + (m.cached ? "（已缓存）" : "");
          sel.appendChild(o);
          modelUrls[m.key] = m.urls || [];
          modelInfo[m.key] = { label: m.label, cached: !!m.cached };
        });
        // 记住上次选的模型（列表里还有这个模型才恢复）
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
        if (tagging) App.api.wd14_cancel();
        else if (loading) { runAfterLoad = false; App.api.wd14_cancel(); refreshControls(); }
      });

      // 行内操作：复制 / 移除 / 点标签复制单个 / 点缩略图用系统看图工具打开
      const box = $("#wd-images");
      box.addEventListener("click", (e) => {
        const row = e.target.closest(".wd-item");
        if (!row) return;
        const it = items[+row.dataset.i];
        if (!it) return;
        const act = e.target.closest("[data-act]");
        if (act && act.dataset.act === "copy") { App.copy(it.tags, "已复制标签"); return; }
        if (act && act.dataset.act === "remove") {
          items.splice(items.indexOf(it), 1);
          byPath.delete(norm(it.path));
          renderList();
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
        tagProg.set(total > 0 ? n / total * 100 : 0, `反推中 ${n}/${total}`);
      });
      App.on("wd14", "tag_item", (e) => {
        const it = byPath.get(norm(e.path));
        if (!it) return;   // 反推过程中被移除了
        it.state = "done"; it.tags = e.tags || ""; it.txt = !!e.txt;
        advance(it);
      });
      App.on("wd14", "tag_item_failed", (e) => {
        const it = byPath.get(norm(e.path));
        if (!it) return;
        it.state = "failed"; it.error = e.error || "处理失败";
        advance(it);
      });
      App.on("wd14", "tag_done", () => {
        const cancelled = batchDone < batch;
        endRun();
        const failed = items.filter((it) => it.state === "failed").length;
        tagProg.set(100, cancelled ? `已取消 · 完成 ${batchDone}/${batch}`
          : `完成 ${batch} 张` + (failed ? ` · 失败 ${failed}` : ""));
        if (runQueued && !cancelled && todo().some((it) => it.state === "pending")) { startRun(); return; }
        if (batch > 1 && !cancelled) App.toast(`反推完成，共 ${batch} 张` + (failed ? `，${failed} 张失败` : ""), failed ? "error" : "ok");
      });
      App.on("wd14", "tag_failed", (e) => {
        endRun(); tagProg.hide();
        App.modal("反推失败", `<pre class="modal-pre">${App.esc(e.msg || "")}</pre>`,
          [{ id: "ok", label: "知道了", kind: "primary" }]);
      });

      renderList();
    },

    onDropped(paths) {
      addPaths(paths);
    },
  };
})();
