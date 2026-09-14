/* meta.js —— 图片信息页（重写版）
 *
 * 重写的原因，以及和上一版最大的不同：
 *
 * 上一版的瓶颈不在解析，而在**怎么拿到这张图**。原来的链路是
 *
 *     松手 → WebView2 的 drop 事件 → pywebview 序列化整个事件对象
 *          → Python 侧读 dataTransfer.files[i].pywebviewFullPath
 *          → 把路径 emit 回前端 → 前端再请求后端按路径读文件
 *
 * 界面上显示的「解析 1ms」是真的，但它只覆盖最后那一小段；前面等路径的时间
 * 完全没被算进去，而那才是十几秒的所在。
 *
 * 这一版换掉根本设计：drop 事件就在 JS 里处理，不下沉到 Python。
 *
 *   预览 —— URL.createObjectURL(file)。浏览器直接引用自己手里的 File，
 *           零拷贝、零编码、不过桥，也不用再往 web/_preview 建硬链接。
 *   元数据 —— 在 JS 里把 PNG 文本块 / EXIF 抠出来（imgcontainer.js），
 *           只把这几 KB 文本送给后端做完整解析。图片本体一个字节都不过桥。
 *   路径 —— 不再是必需品。只有「写回保存」和「隐写 PNG / 同名 txt」
 *           这两类操作真的需要磁盘路径。
 *
 * 计时也重做了：这一版从**松手那一刻**开始计，分段记录并单独显示总计，
 * 不会再出现"每段都是 1ms 但总共 15 秒"。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let cur = { path: "", name: "", objectUrl: null, rawText: "", refsCount: 0 };

  // 加载序号。连着拖好几张时，先发出去的可能后回来，序号对不上一律丢弃。
  let seq = 0;

  let missingRows = [];
  let downloading = false;
  let missingProg = null;
  let marks = null;

  const STATE_TEXT = {
    local: { text: "本地已有", cls: "ok" },
    no_hash: { text: "无哈希", cls: "" },
    querying: { text: "查询中…", cls: "" },
    found: { text: "可下载", cls: "ok" },
    not_found: { text: "未找到", cls: "bad" },
    downloading: { text: "下载中…", cls: "" },
    done: { text: "已下载", cls: "ok" },
    failed: { text: "下载失败", cls: "bad" },
  };

  /* ================= 计时 ================= */

  function markStart() { marks = { t0: performance.now(), steps: [] }; }

  function mark(label) {
    if (!marks) return;
    const now = performance.now();
    const prev = marks.steps.length ? marks.steps[marks.steps.length - 1].at : marks.t0;
    marks.steps.push({ label: label, at: now, dur: now - prev });
  }

  function renderTiming(extra) {
    const el = $("#meta-timing");
    if (!el || !marks) return;
    const total = performance.now() - marks.t0;
    const parts = marks.steps.map((s) => `${s.label} ${s.dur.toFixed(0)}ms`);
    if (extra) parts.push(extra);
    // 总计单独标出来。分段之和跟总计对不上就说明还有没被计到的环节，
    // 上一版栽的正是这个跟头。
    el.innerHTML = App.esc(parts.join("　")) +
      `　<span style="color:var(--accent)">总计 ${total.toFixed(0)}ms</span>`;
  }

  /* ================= 页面重置 ================= */

  function resetPage(placeholder) {
    const drop = $("#meta-drop");
    drop.querySelectorAll("img").forEach((img) => {
      img.removeAttribute("src");
      img.remove();
    });
    // objectURL 不 revoke 的话，浏览器会一直押着这个 File 的整块内存不放
    if (cur.objectUrl) {
      try { URL.revokeObjectURL(cur.objectUrl); } catch (e) { /* 忽略 */ }
      cur.objectUrl = null;
    }
    drop.innerHTML = `<span>${App.esc(placeholder || "把图片拖到这里")}</span>`;

    cur.path = ""; cur.name = ""; cur.rawText = ""; cur.refsCount = 0;

    $("#meta-file").textContent = "";
    const tm = $("#meta-timing"); if (tm) tm.textContent = "";
    const src = $("#meta-source"); src.innerHTML = ""; src.hidden = true;

    $("#meta-prompt").value = "";
    $("#meta-neg").value = "";
    $("#meta-settings tbody").innerHTML = "";
    $("#meta-chars").innerHTML = ""; $("#meta-chars-card").hidden = true;
    $("#meta-loras").innerHTML = ""; $("#meta-lora-card").hidden = true;
    $("#meta-civitai").innerHTML = ""; $("#meta-civitai-card").hidden = true;

    $("#meta-missing tbody").innerHTML = "";
    missingRows = [];
    downloading = false;
    if (missingProg) missingProg.hide();

    $("#meta-scan").disabled = true;
    $("#meta-download").disabled = true;
    ["#meta-copy-prompt", "#meta-copy-neg", "#meta-copy-all"].forEach((s) => ($(s).disabled = true));
  }

  /* ================= 主流程：从 File 对象打开 ================= */

  async function openFile(file, knownPath) {
    const my = ++seq;
    markStart();
    resetPage("读取中…");

    // 1. 预览：直接引用浏览器手里的 File。不读盘、不编码、不过桥。
    cur.objectUrl = URL.createObjectURL(file);
    cur.name = file.name || "";
    cur.path = knownPath || "";
    const drop = $("#meta-drop");
    drop.innerHTML = "";
    const img = new Image();
    img.alt = "";
    img.onerror = () => { drop.innerHTML = "<span>（无法预览这个格式）</span>"; };
    img.src = cur.objectUrl;
    drop.appendChild(img);
    $("#meta-file").textContent = knownPath || file.name;
    mark("预览");

    // 2. 读字节。大图这一步才是真正的耗时大头，而且完全在浏览器内部，
    //    没有任何跨进程通信。
    let buf;
    try {
      buf = await file.arrayBuffer();
    } catch (e) {
      if (my === seq) { resetPage("（读取失败）"); App.toast("读取文件失败：" + e.message, "error"); }
      return;
    }
    if (my !== seq) return;
    mark("读字节");

    // 3. 就地抠出元数据
    const container = window.ImgContainer.readContainer(buf);
    mark("抠元数据");

    if (!container || container.deferred) {
      // 认不出的格式，或者遇到压缩文本块（罕见）—— 交给后端按路径重来一次
      if (cur.path) { deepParse(cur.path, my); }
      else if (my === seq) { applyEmpty("这个格式前端读不了，用下面的「选择图片」按钮打开可以走完整解析"); }
      return;
    }

    // 4. 送后端解析。过桥的只有抠出来的文本，通常几 KB。
    let r;
    try {
      r = await App.api.meta_parse(container, file.name || "", file.size || 0, cur.path || "");
    } catch (e) {
      if (my === seq) { resetPage("（解析失败）"); App.toast("解析失败：" + e.message, "error"); }
      return;
    }
    if (my !== seq) return;
    mark("后端解析");

    applyResult(r);
    renderTiming(`后端内部 ${(r.timing && r.timing.parse) || 0}ms`);

    // 5. 前端什么都没读出来，而我们手上又有路径 —— 再走一次完整解析，
    //    补上隐写 PNG 和同名 txt 这两条需要读原文件的路径。兜底，不是主路径。
    if (!r.has_meta && cur.path) deepParse(cur.path, my, true);
  }

  async function deepParse(path, my, silent) {
    try {
      const r = await App.api.meta_deep(path);
      if (my !== seq || !r || !r.ok) return;
      if (silent && !r.has_meta) return;   // 兜底也没读出来，别把已有结果盖掉
      applyResult(r);
      mark("深度解析");
      renderTiming("含隐写/sidecar 扫描");
    } catch (e) { /* 兜底失败就算了，主结果已经在屏幕上 */ }
  }

  function applyEmpty(msg) {
    $("#meta-prompt").value = "（没有读到生成参数）\n\n" + msg;
    ["#meta-copy-prompt", "#meta-copy-neg", "#meta-copy-all"].forEach((s) => ($(s).disabled = false));
    renderTiming();
  }

  function applyResult(r) {
    cur.rawText = r.raw_text || "";
    cur.refsCount = r.refs_count || 0;
    if (r.path) cur.path = r.path;

    renderBadge(r);
    $("#meta-prompt").value = r.prompt || "";
    $("#meta-neg").value = r.negative || "";
    $("#meta-settings tbody").innerHTML = (r.rows || [])
      .map(([k, v]) => `<tr><td class="dim" style="white-space:nowrap">${App.esc(k)}</td><td>${App.esc(v)}</td></tr>`)
      .join("");
    renderCharacters(r.characters);
    renderLoras(r.loras);
    renderCivitai(r.civitai);
    missingRows = [];
    renderMissing();

    $("#meta-scan").disabled = cur.refsCount === 0;
    ["#meta-copy-prompt", "#meta-copy-neg", "#meta-copy-all"].forEach((s) => ($(s).disabled = false));
  }

  /* ================= 按路径打开（选择图片按钮） ================= */

  async function openPath(path) {
    const my = ++seq;
    markStart();
    resetPage("读取中…");
    cur.path = path;
    $("#meta-file").textContent = path;

    let r;
    try {
      r = await App.api.meta_load(path);
    } catch (e) {
      if (my === seq) { resetPage("（读取失败）"); App.toast("读取失败：" + e.message, "error"); }
      return;
    }
    if (my !== seq) return;
    mark("后端解析");
    if (!r || !r.ok) {
      resetPage("（读取失败）");
      App.toast((r && r.error) || "读取失败", "error");
      return;
    }
    applyResult(r);

    // 走这条路时浏览器手里没有 File，只能让后端给个可引用的地址
    const drop = $("#meta-drop");
    drop.innerHTML = "";
    if (r.preview) {
      const img = new Image();
      img.alt = "";
      img.onerror = () => { drop.innerHTML = "<span>（无法预览）</span>"; };
      img.src = r.preview;
      drop.appendChild(img);
    } else {
      drop.innerHTML = "<span>（无法预览这个格式）</span>";
    }
    mark("预览");
    renderTiming();
  }

  /* ================= 渲染各区块 ================= */

  function renderBadge(r) {
    const box = $("#meta-source");
    const bits = [`<span class="src-badge" data-tone="${App.esc(r.tone || "plain")}">${App.esc(r.source || "未知")}</span>`];
    if (r.file_info) bits.push(`<span class="dim">${App.esc(r.file_info)}</span>`);
    if (r.from_sidecar) bits.push(`<span class="dim">内容来自同名 .txt，不是图片里读出来的</span>`);
    box.innerHTML = bits.join("　");
    box.hidden = false;
  }

  function renderCharacters(chars) {
    const card = $("#meta-chars-card"), box = $("#meta-chars");
    if (!chars || !chars.length) { card.hidden = true; box.innerHTML = ""; return; }
    card.hidden = false;
    box.innerHTML = chars.map((c, i) => {
      const pos = c.center
        ? `<span class="dim">位置 ${(+c.center.x).toFixed(2)}, ${(+c.center.y).toFixed(2)}</span>` : "";
      const neg = (c.negative_prompt || "").trim()
        ? `<div class="char-neg">负面：${App.esc(c.negative_prompt)}</div>` : "";
      return `<div class="char-slot">
        <div class="char-head">角色 ${i + 1}${pos ? "　" + pos : ""}
          <button class="btn btn-xs" data-char-copy="${i}">复制</button></div>
        <div class="char-body">${App.esc(c.prompt || "（空）")}</div>${neg}</div>`;
    }).join("");
    box.querySelectorAll("[data-char-copy]").forEach((b) =>
      b.addEventListener("click", () => App.copy(chars[+b.dataset.charCopy].prompt || "", "已复制角色提示词")));
  }

  function renderLoras(loras) {
    const card = $("#meta-lora-card"), box = $("#meta-loras");
    if (!loras || !loras.length) { card.hidden = true; box.innerHTML = ""; return; }
    card.hidden = false;
    box.innerHTML = loras.map((l, i) => {
      const w = (l.weight == null) ? "" : `<span class="dim">强度 ${l.weight}</span>`;
      const h = l.hash ? `<span class="dim">${App.esc(l.hash)}</span>` : `<span class="dim">无哈希</span>`;
      return `<div class="tw-row"><span class="tw-text">${App.esc(l.name)}</span>${w}　${h}
        <button class="btn btn-xs" data-lora-copy="${i}">复制调用</button></div>`;
    }).join("");
    box.querySelectorAll("[data-lora-copy]").forEach((b) =>
      b.addEventListener("click", () => {
        const l = loras[+b.dataset.loraCopy];
        const name = String(l.name || "").replace(/\.[^.]+$/, "");
        App.copy(`<lora:${name}:${l.weight == null ? 1 : l.weight}>`, "已复制 LoRA 调用格式");
      }));
  }

  function renderCivitai(items) {
    const card = $("#meta-civitai-card"), box = $("#meta-civitai");
    if (!items || !items.length) { card.hidden = true; box.innerHTML = ""; return; }
    card.hidden = false;
    box.innerHTML = items.map((c) => {
      const w = (c.weight == null) ? "" : `<span class="dim">强度 ${c.weight}</span>`;
      const type = c.model_type ? `<span class="dim">${App.esc(c.model_type)}</span>` : "";
      const ver = c.version_name ? `　<span class="dim">${App.esc(c.version_name)}</span>` : "";
      return `<div class="tw-row"><span class="tw-text">${App.esc(c.model_name || "")}${ver}</span>${type}　${w}
        <button class="btn btn-xs" data-civ-url="${App.esc(c.civitai_url || "")}">打开页面</button></div>`;
    }).join("");
    box.querySelectorAll("[data-civ-url]").forEach((b) =>
      b.addEventListener("click", () => b.dataset.civUrl && App.api.open_url(b.dataset.civUrl)));
  }

  /* ================= 缺失模型检测 ================= */

  function stateHtml(row) {
    const s = STATE_TEXT[row.state] || { text: row.state, cls: "" };
    let extra = "";
    if (row.state === "found" || row.state === "done") {
      extra = `<div class="dim" style="font-size:11px">${App.esc(row.file_name || "")}` +
        (row.size_mb ? `（${row.size_mb} MB）` : "") + `</div>`;
    } else if (row.error) {
      extra = `<div class="dim" style="font-size:11px">${App.esc(row.error)}</div>`;
    }
    const action = row.state === "no_hash"
      ? ` <button class="btn btn-xs" data-search="${row.idx}">按名字搜索</button>` : "";
    return `<span${s.cls ? ` style="color:var(--${s.cls})"` : ""}>${s.text}</span>${action}${extra}`;
  }

  function renderMissing() {
    const tb = $("#meta-missing tbody");
    if (!missingRows.length) { tb.innerHTML = ""; updateDownloadBtn(); return; }
    tb.innerHTML = missingRows.map((r) => {
      const canDl = ["found", "downloading", "done", "failed"].indexOf(r.state) >= 0;
      const chk = canDl
        ? `<input type="checkbox" data-idx="${r.idx}" ${r.checked ? "checked" : ""} ${(r.state === "downloading" || r.state === "done") ? "disabled" : ""}>`
        : "";
      return `<tr data-idx="${r.idx}"><td>${chk}</td><td class="dim">${App.esc(r.role)}</td>
        <td>${App.esc(r.name)}</td><td>${stateHtml(r)}</td></tr>`;
    }).join("");
    tb.querySelectorAll("input[type=checkbox]").forEach((c) =>
      c.addEventListener("change", () => {
        const row = missingRows.find((r) => r.idx === +c.dataset.idx);
        if (row) row.checked = c.checked;
        updateDownloadBtn();
      }));
    tb.querySelectorAll("[data-search]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); nameSearch(+b.dataset.search); }));
    updateDownloadBtn();
  }

  function updateDownloadBtn() {
    $("#meta-download").disabled = downloading || !missingRows.some((r) => r.checked && r.state === "found");
  }

  async function nameSearch(idx) {
    const row = missingRows.find((r) => r.idx === idx);
    if (!row) return;
    row.state = "querying";
    renderMissing();
    try {
      const r = await App.api.meta_name_search(idx);
      if (r && r.ok === false) { row.state = "no_hash"; renderMissing(); App.toast(r.error || "搜索失败", "error"); }
    } catch (e) { row.state = "no_hash"; renderMissing(); App.toast("搜索失败：" + e.message, "error"); }
  }

  /* ================= 入口绑定 ================= */

  App.pages.meta = {
    init() {
      missingProg = App.progress($("#meta-missing-progress"));

      $("#meta-pick").addEventListener("click", async () => {
        const r = await App.api.choose_images(false);
        if (r && r.ok && r.paths && r.paths.length) openPath(r.paths[0]);
      });

      // 拖放：全程留在 JS 里。这是这次重写的核心 ——
      // 不再把事件下沉到 Python 去换一个磁盘路径。
      const drop = $("#meta-drop");
      ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        drop.classList.add("dragover");
      }));
      ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
        e.preventDefault(); e.stopPropagation();
        drop.classList.remove("dragover");
      }));
      drop.addEventListener("drop", (e) => {
        e.preventDefault(); e.stopPropagation();
        const f = (e.dataTransfer.files || [])[0];
        if (!f) return;
        // pywebviewFullPath 少数情况下前端也能直接看到，有就顺手带上
        //（写回保存要用），没有完全不影响读图
        openFile(f, f.pywebviewFullPath || f.path || "");
      });

      // Ctrl+V 粘贴截图
      document.addEventListener("paste", (e) => {
        if (App.currentPage !== "meta") return;
        const t = e.target;
        if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
        const items = (e.clipboardData && e.clipboardData.items) || [];
        for (const it of items) {
          if (it.kind === "file" && (it.type || "").indexOf("image/") === 0) {
            const f = it.getAsFile();
            if (f) { e.preventDefault(); openFile(f, ""); return; }
          }
        }
      });

      $("#meta-scan").addEventListener("click", async () => {
        try {
          const r = await App.api.meta_scan_missing();
          if (!r.ok) { App.toast(r.error || "检测失败", "error"); return; }
          missingRows = (r.rows || []).map((row) => Object.assign({}, row, { checked: false }));
          renderMissing();
          if (r.querying > 0) missingProg.set(0, `正在按哈希查询 ${r.querying} 个模型…`);
          else missingProg.hide();
        } catch (e) { App.toast("检测失败：" + e.message, "error"); }
      });

      $("#meta-download").addEventListener("click", async () => {
        const indexes = missingRows.filter((r) => r.checked && r.state === "found").map((r) => r.idx);
        if (!indexes.length) return;
        downloading = true; updateDownloadBtn();
        try {
          const r = await App.api.meta_download(indexes);
          if (r && r.ok === false) { App.toast(r.error || "无法开始下载", "error"); downloading = false; updateDownloadBtn(); }
        } catch (e) { App.toast("无法开始下载：" + e.message, "error"); downloading = false; updateDownloadBtn(); }
      });

      $("#meta-copy-prompt").addEventListener("click", () => App.copy($("#meta-prompt").value, "已复制正向提示词"));
      $("#meta-copy-neg").addEventListener("click", () => App.copy($("#meta-neg").value, "已复制负向提示词"));
      $("#meta-copy-all").addEventListener("click", () => App.copy(cur.rawText, "已复制全部原始参数"));

      /* ---- 后端推来的事件 ---- */
      App.on("meta", "missing_row", (e) => {
        const row = missingRows.find((r) => r.idx === e.idx);
        if (!row) return;
        Object.assign(row, { state: e.state, file_name: e.file_name, size_mb: e.size_mb, error: e.error });
        if (e.state === "found") row.checked = true;
        renderMissing();
      });
      App.on("meta", "scan_done", (e) => {
        missingProg.hide();
        App.toast(e.has_downloadable ? "检测完成，已自动勾选可下载项" : "检测完成，没有可自动下载的模型",
          e.has_downloadable ? "ok" : "");
      });
      App.on("meta", "name_result", async (e) => {
        const row = missingRows.find((r) => r.idx === e.idx);
        if (!row) return;
        if (!e.ok || !(e.candidates || []).length) {
          row.state = "no_hash"; renderMissing();
          App.toast(e.error || "Civitai 上没有搜到同名模型", "error");
          return;
        }
        const v = await App.modal(
          `为「${row.name}」选择匹配的模型`,
          `<div class="dim" style="margin-bottom:8px">名字搜索不保证精确匹配，请确认后再下载：</div>
           <div class="choice-list">` +
          e.candidates.map((c, i) => `<div class="choice-item" data-i="${i}">${App.esc(c.label)}</div>`).join("") +
          `</div>`,
          [{ id: "cancel", label: "取消" }],
          { onOpen(body, finish) {
              body.querySelectorAll(".choice-item").forEach((el) =>
                el.addEventListener("click", () => finish("pick:" + el.dataset.i)));
            } });
        if (!v || v.indexOf("pick:") !== 0) { row.state = "no_hash"; renderMissing(); return; }
        try {
          const r = await App.api.meta_choose_candidate(e.idx, +v.slice(5));
          if (r && r.ok) Object.assign(row, { state: "found", file_name: r.file_name, size_mb: r.size_mb, checked: true });
          else { row.state = "no_hash"; App.toast((r && r.error) || "选择失败", "error"); }
        } catch (err) { row.state = "no_hash"; App.toast("选择失败：" + err.message, "error"); }
        renderMissing();
      });
      App.on("meta", "dl_progress", (e) => {
        const row = missingRows.find((r) => r.idx === e.idx);
        if (row && row.state !== "downloading") { row.state = "downloading"; renderMissing(); }
        const pct = e.total > 0 ? (e.downloaded / e.total * 100) : 0;
        missingProg.set(pct, `下载中：${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}`);
      });
      App.on("meta", "dl_item", (e) => {
        const row = missingRows.find((r) => r.idx === e.idx);
        if (!row) return;
        if (e.ok) { row.state = "done"; row.error = null; } else { row.state = "failed"; row.error = e.error; }
        renderMissing();
      });
      App.on("meta", "dl_overall", (e) =>
        missingProg.set(e.total > 0 ? (e.done / e.total * 100) : 0, `总体进度 ${e.done}/${e.total}`));
      App.on("meta", "dl_done", () => {
        downloading = false; missingProg.hide(); updateDownloadBtn();
        App.toast("缺失模型下载完成", "ok");
      });

      resetPage();
    },

    // 保留这条：别处仍可能按路径推图进来
    onDropped(paths) { if (paths && paths.length) openPath(paths[0]); },
  };
})();
