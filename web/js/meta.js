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
  let fileProg = null;
  let downloadSpeed = { at: 0, bytes: 0, value: 0 };
  let dlResults = {};      // 这一轮下载每个模型的结果 idx → {ok, path, error, …}
  let marks = null;

  const STATE_TEXT = {
    local: { text: "本地已有", cls: "ok" },
    no_hash: { text: "无哈希", cls: "" },
    querying: { text: "查询中…", cls: "" },
    found: { text: "可下载", cls: "ok" },
    queued: { text: "排队中", cls: "" },
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
    drop.innerHTML = `<span>${App.esc(placeholder || "把图片或视频拖到这里")}</span>`;

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
    if (fileProg) fileProg.hide();
    $("#meta-download-cancel").hidden = true;
    $("#meta-dl-result").hidden = true;
    dlResults = {};

    $("#meta-scan").disabled = true;
    $("#meta-download").disabled = true;
    ["#meta-copy-prompt", "#meta-copy-neg", "#meta-copy-all"].forEach((s) => ($(s).disabled = true));
  }

  /* 预览区：图片用 <img>，视频用 <video>（静音循环自动播放，带控制条，想听声音点一下即可） */
  function showPreview(url, isVideo) {
    const drop = $("#meta-drop");
    drop.innerHTML = "";
    let el;
    if (isVideo) {
      el = document.createElement("video");
      el.muted = true; el.loop = true; el.autoplay = true; el.controls = true; el.playsInline = true;
      el.onerror = () => { drop.innerHTML = "<span>（这个视频格式无法预览，参数照常读取）</span>"; };
    } else {
      el = new Image();
      el.alt = "";
      el.onerror = () => { drop.innerHTML = "<span>（无法预览这个格式）</span>"; };
    }
    el.src = url;
    drop.appendChild(el);
  }

  /* ================= 主流程：从 File 对象打开 ================= */

  async function openFile(file, knownPath) {
    if (downloading) { App.toast("模型下载中，请先完成或取消下载再换图片", "", 4000); return; }
    const my = ++seq;
    markStart();
    resetPage("读取中…");

    // 1. 预览：直接引用浏览器手里的 File。不读盘、不编码、不过桥。
    cur.objectUrl = URL.createObjectURL(file);
    cur.name = file.name || "";
    cur.path = knownPath || "";
    const isVideo = window.ImgContainer.isVideoFile(file);
    showPreview(cur.objectUrl, isVideo);
    $("#meta-file").textContent = knownPath || file.name;
    mark("预览");

    let container;
    if (isVideo) {
      // 视频（Forge Neo / H3 / ComfyUI）：只按需读容器头和 moov / Tags 那几 KB，
      // 几百 MB 的视频也不会整个读进内存
      try { container = await window.ImgContainer.readVideo(file); }
      catch (e) { container = null; }
      if (my !== seq) return;
      mark("抠元数据");
    } else {
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
      container = window.ImgContainer.readContainer(buf);
      mark("抠元数据");
    }

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
    if (downloading) { App.toast("模型下载中，请先完成或取消下载再换图片", "", 4000); return; }
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
    if (r.preview) {
      showPreview(r.preview, /\.(mp4|m4v|mov|mkv|webm)$/i.test(r.preview.split("?")[0]));
    } else {
      $("#meta-drop").innerHTML = "<span>（无法预览这个格式）</span>";
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

  // 状态列只放一行短字；文件名、保存位置、出错原因放在这一行下面的整行说明里。
  // 以前全塞进窄窄的状态列，出错原因被挤成一个字一行，行高撑到好几百像素，
  // 名字和勾选框被顶出可视区——用户看不出是哪个模型、为什么失败（群友反馈视频里就是这样）
  function stateHtml(row) {
    const s = STATE_TEXT[row.state] || { text: row.state, cls: "" };
    let text = s.text;
    if (row.state === "downloading" && row.pct != null) text = `下载中 ${row.pct.toFixed(0)}%`;
    if (row.state === "failed" && row.need_key) text = "需要登录";
    if (row.state === "done" && row.warn) text = "已下载 · 请核对";
    return `<span${s.cls ? ` style="color:var(--${s.cls})"` : ""}>${App.esc(text)}</span>`;
  }

  function detailHtml(row) {
    const bits = [];
    if (row.state === "found" || row.state === "queued") {
      if (row.file_name) bits.push(`Civitai 文件：${App.esc(row.file_name)}` + (row.size_mb ? `（${row.size_mb} MB）` : ""));
    } else if (row.state === "done") {
      if (row.path) bits.push(`保存到：<span class="meta-path">${App.esc(row.path)}</span>` +
        ` <button class="btn btn-xs" data-reveal="${App.esc(row.path)}">定位文件</button>`);
      if (row.renamed_from) bits.push(`已按图里的名字保存（Civitai 上叫 ${App.esc(row.renamed_from)}），WebUI / ComfyUI 按这个名字就能找到`);
      if (row.warn) bits.push(`<span class="meta-warn">${App.esc(row.warn)}</span>`);
    } else if (row.state === "failed" || (row.state === "not_found" && row.error)) {
      bits.push(`<span class="meta-warn">${App.esc(row.error || "下载失败")}</span>` +
        (row.need_key ? ` <button class="btn btn-xs btn-accent" data-key="${row.idx}">填写 Civitai Key</button>` : "") +
        (row.state === "failed" ? ` <button class="btn btn-xs" data-retry="${row.idx}">重试</button>` : ""));
    }
    return bits.length ? `<tr class="meta-sub" data-sub="${row.idx}"><td></td><td colspan="3">${bits.join("<br>")}</td></tr>` : "";
  }

  function renderMissing() {
    const tb = $("#meta-missing tbody");
    if (!missingRows.length) { tb.innerHTML = ""; updateDownloadBtn(); return; }
    tb.innerHTML = missingRows.map((r) => {
      const canDl = ["found", "queued", "downloading", "done", "failed"].indexOf(r.state) >= 0;
      const locked = r.state === "downloading" || r.state === "done" || r.state === "queued";
      // 「按名字搜索」按钮放第一列：之前放在状态列里，行内容一宽状态列就被
      // 挤出可视区（容器横向滚动），用户根本看不到这个入口
      const chk = canDl
        ? `<input type="checkbox" data-idx="${r.idx}" ${r.checked ? "checked" : ""} ${locked ? "disabled" : ""}>`
        : (r.state === "no_hash" || r.state === "not_found"
          ? `<button class="btn btn-xs" data-search="${r.idx}">按名字搜索</button>` : "");
      return `<tr data-idx="${r.idx}"${r.state === "downloading" ? ' class="selected"' : ""}><td>${chk}</td><td class="dim">${App.esc(r.role)}</td>
        <td>${App.esc(r.name)}</td><td style="white-space:nowrap">${stateHtml(r)}</td></tr>` + detailHtml(r);
    }).join("");
    tb.querySelectorAll("input[type=checkbox]").forEach((c) =>
      c.addEventListener("change", () => {
        const row = missingRows.find((r) => r.idx === +c.dataset.idx);
        if (row) row.checked = c.checked;
        updateDownloadBtn();
      }));
    tb.querySelectorAll("[data-search]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); nameSearch(+b.dataset.search); }));
    tb.querySelectorAll("[data-reveal]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); App.api.reveal_in_explorer(b.dataset.reveal); }));
    tb.querySelectorAll("[data-key]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); askKeyAndRetry(); }));
    tb.querySelectorAll("[data-retry]").forEach((b) =>
      b.addEventListener("click", (e) => { e.stopPropagation(); startDownload([+b.dataset.retry]); }));
    updateDownloadBtn();
  }

  // 下载失败的勾上了也能再下（以前只认「可下载」，失败一次就再也点不了下载按钮）
  const RETRYABLE = ["found", "failed"];

  function updateDownloadBtn() {
    $("#meta-download").disabled = downloading ||
      !missingRows.some((r) => r.checked && RETRYABLE.indexOf(r.state) >= 0);
  }

  // 正在下的那一行滚进表格可视区：表格只有一小块，下第二个时第一个早滚出去了
  function revealRow(idx) {
    const tr = document.querySelector(`#meta-missing tr[data-idx="${idx}"]`);
    const wrap = tr && tr.closest(".table-wrap");
    if (!wrap) return;
    const top = tr.offsetTop - wrap.querySelector("thead").offsetHeight;
    if (top < wrap.scrollTop || top + tr.offsetHeight > wrap.scrollTop + wrap.clientHeight) wrap.scrollTop = Math.max(0, top - 8);
  }

  async function startDownload(indexes) {
    if (downloading || !indexes.length) return;
    downloading = true;
    missingRows.forEach((r) => {
      if (indexes.indexOf(r.idx) >= 0) Object.assign(r, { state: "queued", checked: true, error: null, need_key: false, pct: null });
    });
    renderMissing();
    $("#meta-scan").disabled = true; $("#meta-pick").disabled = true;
    const cancel = $("#meta-download-cancel");
    cancel.hidden = false; cancel.disabled = false; cancel.textContent = "取消下载";
    $("#meta-dl-result").hidden = true;
    downloadSpeed = { at: 0, bytes: 0, value: 0 };
    dlResults = {};
    missingProg.set(0, `准备下载 ${indexes.length} 个模型…`);
    const failedStart = (msg) => {
      App.toast(msg, "error"); downloading = false;
      missingRows.forEach((r) => { if (r.state === "queued") r.state = "found"; });
      renderMissing();
      cancel.hidden = true;
      $("#meta-scan").disabled = cur.refsCount === 0; $("#meta-pick").disabled = false;
      missingProg.hide(); fileProg.hide();
    };
    try {
      const r = await App.api.meta_download(indexes);
      if (r && r.ok === false) failedStart(r.error || "无法开始下载");
    } catch (e) { failedStart("无法开始下载：" + e.message); }
  }

  // 403 / 401：这个模型要登录才能下。图片信息页没有填 Key 的地方，以前提示「在下方填入 Key」
  // 用户在这页根本找不到。直接在这里弹框填，存好后把要登录的那几个重新下一遍
  async function askKeyAndRetry() {
    if (downloading) return;
    let key = "";
    const has = !!(App.cfg.civitai_api_key || "").trim();
    const v = await App.modal("填写 Civitai API Key",
      `<div class="dim" style="margin-bottom:8px">${has ? "已经填过 Key 但仍被拒绝：Key 可能失效了，换一个新的；" : ""}` +
      `这个模型的作者设置了登录后才能下载。到 Civitai 网站右上角头像 → Settings → API Keys 生成一个，粘贴到下面。` +
      `Key 只保存在本机配置里，和「模型下载」页填的是同一个。</div>
       <input type="password" id="meta-key-input" style="width:100%" placeholder="粘贴 Civitai API Key" autocomplete="off">`,
      [{ id: "ok", label: "保存并重试", kind: "primary" }, { id: "open", label: "打开 Civitai 生成 Key" }, { id: "cancel", label: "取消" }],
      { onOpen(body) { const i = body.querySelector("#meta-key-input"); if (i) i.focus(); },
        onClose(body, val) { const i = body.querySelector("#meta-key-input"); key = val === "ok" && i ? i.value.trim() : ""; } });
    if (v === "open") { App.api.open_url("https://civitai.com/user/account"); return; }
    if (v !== "ok" || !key) return;
    try {
      await App.api.update_config({ civitai_api_key: key });
      App.cfg.civitai_api_key = key;
      const box = $("#cv-apikey"); if (box) box.value = key;
    } catch (e) { App.toast("保存 Key 失败：" + e.message, "error"); return; }
    const idxs = missingRows.filter((r) => r.state === "failed" && r.need_key).map((r) => r.idx);
    App.toast("Key 已保存" + (idxs.length ? "，正在重新下载" : ""), "ok");
    if (idxs.length) startDownload(idxs);
  }

  // 一轮下载结束后的结果清单：表格太小、行会被滚走，结果单独列在进度条下面
  function renderDlResult(e) {
    const box = $("#meta-dl-result");
    const rows = Object.keys(dlResults).map((k) => {
      const r = dlResults[k];
      const row = missingRows.find((x) => x.idx === +k) || {};
      const name = App.esc(row.name || r.file_name || "模型");
      if (r.ok) {
        return `<div class="meta-res ok">✓ ${name} → <span class="meta-path">${App.esc(r.path || "")}</span>` +
          (r.path ? ` <button class="btn btn-xs" data-reveal="${App.esc(r.path)}">定位文件</button>` : "") +
          (r.warn ? `<div class="meta-warn">${App.esc(r.warn)}</div>` : "") + `</div>`;
      }
      return `<div class="meta-res bad">✗ ${name}：${App.esc(r.error || "下载失败")}` +
        (r.need_key ? ` <button class="btn btn-xs btn-accent" data-key="1">填写 Civitai Key</button>` : "") + `</div>`;
    });
    if (!rows.length) { box.hidden = true; return; }
    const okN = Object.values(dlResults).filter((r) => r.ok).length;
    const tail = okN ? `<div class="dim" style="margin-top:4px">WebUI 已经开着的话，在 LoRA / 模型列表上点一下「刷新」才看得到新模型；ComfyUI 按 R 刷新节点里的下拉框。</div>` : "";
    box.innerHTML = `<div class="meta-res-title">${e && e.cancelled ? "已取消" : "下载结果"}</div>` + rows.join("") + tail;
    box.hidden = false;
    box.querySelectorAll("[data-reveal]").forEach((b) =>
      b.addEventListener("click", () => App.api.reveal_in_explorer(b.dataset.reveal)));
    box.querySelectorAll("[data-key]").forEach((b) => b.addEventListener("click", askKeyAndRetry));
  }

  async function nameSearch(idx, allBases) {
    const row = missingRows.find((r) => r.idx === idx);
    if (!row) return;
    row._prevState = row.state;   // 记住来路（no_hash / not_found），失败或取消时还原
    row.state = "querying";
    renderMissing();
    try {
      const r = await App.api.meta_name_search(idx, !!allBases);
      if (r && r.ok === false) { row.state = row._prevState || "no_hash"; renderMissing(); App.toast(r.error || "搜索失败", "error"); }
    } catch (e) { row.state = row._prevState || "no_hash"; renderMissing(); App.toast("搜索失败：" + e.message, "error"); }
  }

  /* ================= 入口绑定 ================= */

  App.pages.meta = {
    init() {
      missingProg = App.progress($("#meta-missing-progress"));
      fileProg = App.progress($("#meta-file-progress"));

      $("#meta-pick").addEventListener("click", async () => {
        const r = await App.api.choose_images(false, true);
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

      $("#meta-download").addEventListener("click", () =>
        startDownload(missingRows.filter((r) => r.checked && RETRYABLE.indexOf(r.state) >= 0).map((r) => r.idx)));

      $("#meta-download-cancel").addEventListener("click", async () => {
        const btn = $("#meta-download-cancel");
        btn.disabled = true; btn.textContent = "取消中…";
        try {
          const r = await App.api.meta_download_cancel();
          if (r && r.ok === false) { App.toast(r.error || "无法取消", "error"); btn.disabled = false; btn.textContent = "取消下载"; }
        } catch (e) { App.toast("取消失败：" + e.message, "error"); btn.disabled = false; btn.textContent = "取消下载"; }
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
          row.state = row._prevState || "no_hash"; renderMissing();
          App.toast(e.error || "Civitai 上没有搜到同名模型", "error");
          return;
        }
        // 按这张图的底模过滤过：写明依据，并给「显示全部底模」的退路
        let note = "名字搜索不保证精确匹配，请确认后再下载。";
        if (e.filtered) note = `只显示 <b>${App.esc(e.base)}</b> 底模的结果（按${App.esc(e.base_why)}判断）。` + note;
        else if (e.base && !e.all_bases) note = `没有 ${App.esc(e.base)} 底模的结果，下面是其他底模的。` + note;
        const item = (c, i) =>
          `<div class="choice-item" data-i="${i}">` +
          `<div><b>${App.esc(c.model)}</b>${c.version ? " · " + App.esc(c.version) : ""}` +
          (c.exact ? ' <span class="ext-status">文件名完全一致</span>' : "") +
          (c.base_match ? ' <span class="ext-status ext-rec">同底模</span>' : "") + `</div>` +
          `<div class="dim" style="font-size:11.5px">${App.esc(c.type)} · ${App.esc(c.base)} · ${App.esc(c.file)}` +
          (c.size_mb ? `（${c.size_mb} MB）` : "") + `</div></div>`;
        const btns = [{ id: "cancel", label: "取消" }];
        if (e.filtered) btns.unshift({ id: "all", label: "显示全部底模" });
        const v = await App.modal(
          `为「${row.name}」选择匹配的模型`,
          `<div class="dim" style="margin-bottom:8px">${note}</div>
           <div class="choice-list">` + e.candidates.map(item).join("") + `</div>`,
          btns,
          { onOpen(body, finish) {
              body.querySelectorAll(".choice-item").forEach((el) =>
                el.addEventListener("click", () => finish("pick:" + el.dataset.i)));
            } });
        if (v === "all") { row.state = row._prevState || "no_hash"; nameSearch(e.idx, true); return; }
        if (!v || v.indexOf("pick:") !== 0) { row.state = row._prevState || "no_hash"; renderMissing(); return; }
        try {
          const r = await App.api.meta_choose_candidate(e.idx, +v.slice(5));
          if (r && r.ok) Object.assign(row, { state: "found", file_name: r.file_name, size_mb: r.size_mb, checked: true });
          else { row.state = row._prevState || "no_hash"; App.toast((r && r.error) || "选择失败", "error"); }
        } catch (err) { row.state = row._prevState || "no_hash"; App.toast("选择失败：" + err.message, "error"); }
        renderMissing();
      });
      App.on("meta", "dl_stage", (e) => {
        const row = missingRows.find((r) => r.idx === e.idx);
        if (row && row.state !== "downloading") { row.state = "downloading"; row.pct = null; renderMissing(); revealRow(row.idx); }
        if (e.stage === "download") fileProg.set(0, e.text || "正在连接…");
        else fileProg.set(100, e.text || "正在处理…");
      });
      App.on("meta", "dl_progress", (e) => {
        const row = missingRows.find((r) => r.idx === e.idx);
        const rowPct = e.total > 0 ? Math.min(100, e.downloaded / e.total * 100) : null;
        if (row && (row.state !== "downloading" || Math.floor(rowPct || 0) !== Math.floor(row.pct || 0))) {
          const first = row.state !== "downloading";
          row.state = "downloading"; row.pct = rowPct;
          // 整表重画只在百分比的整数变了时做（一秒最多几次），不随每条进度重画
          if (first) { renderMissing(); revealRow(row.idx); }
          else {
            const cell = document.querySelector(`#meta-missing tr[data-idx="${row.idx}"] td:last-child`);
            if (cell) cell.innerHTML = stateHtml(row); else renderMissing();
          }
        }
        const now = performance.now();
        if (downloadSpeed.idx !== e.idx) downloadSpeed = { at: 0, bytes: 0, value: 0, idx: e.idx };
        if (downloadSpeed.at && e.downloaded >= downloadSpeed.bytes) {
          const seconds = (now - downloadSpeed.at) / 1000;
          if (seconds > 0) {
            const instant = (e.downloaded - downloadSpeed.bytes) / seconds;
            downloadSpeed.value = downloadSpeed.value ? downloadSpeed.value * .65 + instant * .35 : instant;
          }
        }
        downloadSpeed.at = now; downloadSpeed.bytes = e.downloaded;
        const pct = e.total > 0 ? Math.min(100, e.downloaded / e.total * 100) : 0;
        const speed = downloadSpeed.value > 0 ? ` · ${App.fmtBytes(downloadSpeed.value)}/s` : "";
        fileProg.set(pct, `${e.name || "当前文件"} ${pct.toFixed(1)}% · ${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}${speed}`);
      });
      App.on("meta", "dl_item", (e) => {
        const row = missingRows.find((r) => r.idx === e.idx);
        if (!row) return;
        dlResults[e.idx] = e;
        if (e.ok) {
          Object.assign(row, { state: "done", error: null, need_key: false, path: e.path, warn: e.warn || "",
            renamed_from: e.renamed_from || "", checked: false,
            file_name: e.file_name || e.name, size_mb: e.size ? (e.size / 1048576).toFixed(1) : row.size_mb });
        } else {
          Object.assign(row, { state: "failed", error: e.error, need_key: !!e.need_key, checked: true });
        }
        renderMissing();
      });
      App.on("meta", "dl_log", (e) => { if (e.text) App.toast(e.text, "", 5000); });
      App.on("meta", "dl_overall", (e) => {
        if (e.bytes_total > 0) {
          const pct = Math.min(100, e.downloaded / e.bytes_total * 100);
          missingProg.set(pct, `总体 ${pct.toFixed(1)}% · ${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.bytes_total)} · ${e.done}/${e.total} 个`);
        } else {
          missingProg.set(e.total > 0 ? (e.done / e.total * 100) : 0, `总体进度 ${e.done}/${e.total} 个`);
        }
      });
      App.on("meta", "dl_done", (e) => {
        downloading = false; fileProg.hide();
        // 取消时还排着没轮到的，退回「可下载」
        missingRows.forEach((r) => { if (r.state === "queued" || r.state === "downloading") { r.state = "found"; r.pct = null; } });
        renderMissing();
        $("#meta-download-cancel").hidden = true;
        $("#meta-scan").disabled = cur.refsCount === 0; $("#meta-pick").disabled = false;
        const summary = `成功 ${e.success || 0} 个，失败 ${e.failed || 0} 个`;
        missingProg.set(100, (e.cancelled ? "已取消 · " : "下载结束 · ") + summary);
        renderDlResult(e);
        // 以前不管成没成功都弹「缺失模型下载完成」，失败的那个被当成下好了
        if (e.cancelled) App.toast("已取消下载；" + summary + "，未完成部分保留为 .part，下次可续传", "", 6000);
        else if (e.failed && !e.success) App.toast("下载失败：" + summary + "。原因见进度条下面的下载结果", "error", 8000);
        else if (e.failed) App.toast("部分下载失败：" + summary + "。原因见进度条下面的下载结果", "error", 8000);
        else App.toast(`已下载 ${e.success || 0} 个模型，保存位置见下载结果。WebUI 里要在模型列表上点「刷新」才看得到`, "ok", 8000);
      });

      resetPage();
    },

    // 保留这条：别处仍可能按路径推图进来
    onDropped(paths) { if (paths && paths.length) openPath(paths[0]); },
  };
})();
