/* outputs.js — 输出管理器
 *
 * 数据：后端 SQLite 索引（output_index.py），这里只拿 id 列表，缩略图/原图走后端起的
 * 本地文件服务（127.0.0.1 随机端口 + token）。
 * 缩略图：先请求缓存；没有就由浏览器加载原图（视频截一帧）画到 canvas，压成 webp
 * 传回后端缓存——不需要 Pillow / ffmpeg。
 * 网格是虚拟滚动：几万张图也只渲染屏幕上那几十个格子。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const CELL = 168, GAP = 8, THUMB = 320;
  let base = "", token = "";
  let ids = [], kinds = "", missing = new Set(), vers = [];
  let filters = { q: "", kind: "", iid: "", collection: "", chars: [], tags: [], lora: "", model: "", sort: "new" };
  let timeDays = "";
  let collections = [];
  let charFacet = [];
  let selectMode = false;
  const selected = new Set();
  let detailIdx = -1;
  let inited = false, visible = false, scanning = false;
  let autoTimer = 0, queryTimer = 0;

  const url = (route, id) => `${base}/${route}/${id}?t=${token}`;
  // 缩略图带版本号：同一个 id 的文件内容变了，URL 也变，不会读到浏览器缓存里的旧图
  const thumbUrl = (i) => url("t", ids[i]) + (vers[i] ? `&v=${vers[i]}` : "");

  /* ---------- 查询 ---------- */
  function currentFilters() {
    const f = Object.assign({}, filters);
    if (timeDays) {
      const d = new Date();
      if (timeDays === "1") d.setHours(0, 0, 0, 0);
      else d.setTime(d.getTime() - Number(timeDays) * 86400000);
      f.since = d.getTime() / 1000;
    }
    Object.keys(f).forEach((k) => { if (f[k] === "" || (Array.isArray(f[k]) && !f[k].length)) delete f[k]; });
    return f;
  }

  function scheduleQuery(ms) {
    clearTimeout(queryTimer);
    queryTimer = setTimeout(runQuery, ms == null ? 200 : ms);
  }

  async function runQuery() {
    const f = currentFilters();
    try {
      const [r, fc] = await Promise.all([App.api.outputs_query(f), App.api.outputs_facets(f)]);
      ids = (r && r.ids) || [];
      kinds = (r && r.kinds) || "";
      missing = new Set((r && r.missing) || []);
      vers = (r && r.vers) || [];
      renderFacets(fc || {});
      layout(true);
      const kindTxt = fc && fc.kinds ? Object.entries(fc.kinds).map(([k, n]) =>
        `${{ image: "图片", video: "视频", other: "其他" }[k] || k} ${n}`).join(" · ") : "";
      $("#out-count").textContent = `${ids.length} 个文件` + (kindTxt ? `（${kindTxt}）` : "");
      showEmpty();
    } catch (e) { App.toast("查询失败：" + e.message, "error"); }
  }

  function showEmpty() {
    const box = $("#out-empty");
    if (ids.length) { box.hidden = true; return; }
    box.hidden = false;
    const anyFilter = Object.keys(currentFilters()).some((k) => k !== "sort");
    box.innerHTML = anyFilter
      ? "<b>没有符合条件的文件</b><span>换个筛选条件试试，或点左下角「清空筛选」</span>"
      : scanning ? "<b>正在建立索引…</b><span>第一次扫描需要读取每张图的生成参数，图多的话要等一会儿</span>"
        : "<b>还没有输出</b><span>各实例出图后会自动出现在这里（只收录实例自己的输出目录）</span>";
  }

  /* ---------- 侧边筛选 ---------- */
  function renderFacets(fc) {
    charFacet = fc.chars || [];
    renderChars();
    fillSelect($("#of-lora"), "全部 LoRA", fc.loras || [], filters.lora);
    fillSelect($("#of-model"), "全部模型", fc.models || [], filters.model);
    $("#of-tag-dl").innerHTML = (fc.tags || []).slice(0, 300).map(([t, n]) =>
      `<option value="${App.esc(t)}">${n}</option>`).join("");
  }

  function fillSelect(sel, allLabel, rows, cur) {
    const opts = rows.map(([v, n]) => `<option value="${App.esc(v)}">${App.esc(v)}（${n}）</option>`);
    if (cur && !rows.some(([v]) => v === cur)) opts.unshift(`<option value="${App.esc(cur)}">${App.esc(cur)}</option>`);
    sel.innerHTML = `<option value="">${allLabel}</option>` + opts.join("");
    sel.value = cur || "";
  }

  function renderChars() {
    const kw = ($("#of-char-q").value || "").trim().toLowerCase().replace(/\s+/g, "_");
    const rows = charFacet.filter(([t]) => !kw || t.includes(kw));
    const sel = new Set(filters.chars);
    // 已选的角色即使当前结果里没有也要显示，方便取消
    filters.chars.forEach((c) => { if (!rows.some(([t]) => t === c)) rows.unshift([c, 0]); });
    $("#of-chars").innerHTML = rows.slice(0, 80).map(([t, n]) =>
      `<button class="facet${sel.has(t) ? " on" : ""}" data-char="${App.esc(t)}"><span>${App.esc(t.replace(/_/g, " "))}</span><em>${n || ""}</em></button>`
    ).join("") || '<div class="of-empty">没有识别到角色</div>';
    $$("#of-chars [data-char]").forEach((b) => b.addEventListener("click", () => toggleArr("chars", b.dataset.char)));
  }

  function toggleArr(key, v) {
    const arr = filters[key];
    const i = arr.indexOf(v);
    if (i >= 0) arr.splice(i, 1); else arr.push(v);
    if (key === "tags") renderTagChips();
    scheduleQuery(0);
  }

  function renderTagChips() {
    $("#of-tags").innerHTML = filters.tags.map((t) =>
      `<button class="chip on" data-tag="${App.esc(t)}">${App.esc(t)} ✕</button>`).join("");
    $$("#of-tags [data-tag]").forEach((b) => b.addEventListener("click", () => toggleArr("tags", b.dataset.tag)));
  }

  /* ---------- 收藏夹 ---------- */
  function renderCollections() {
    const box = $("#of-cols");
    const cur = String(filters.collection || "");
    box.innerHTML = `<button class="col-item${cur ? "" : " on"}" data-col=""><svg class="btn-icon"><use href="#i-grid"/></svg><span>全部输出</span></button>` +
      collections.map((c) => `
        <div class="col-item${cur === String(c.id) ? " on" : ""}" data-col="${c.id}">
          <svg class="btn-icon"><use href="#i-star"/></svg><span class="col-name">${App.esc(c.name)}</span><em>${c.count}</em>
          <span class="col-ops"><button title="改名" data-op="rename">✎</button><button title="导出到文件夹" data-op="export">⤓</button><button title="删除收藏夹" data-op="delete">✕</button></span>
        </div>`).join("");
    box.querySelectorAll("[data-col]").forEach((el) => el.addEventListener("click", (e) => {
      const op = e.target.closest("[data-op]");
      if (op) { e.stopPropagation(); collectionOp(op.dataset.op, Number(el.dataset.col)); return; }
      filters.collection = el.dataset.col;
      renderCollections();
      scheduleQuery(0);
    }));
    const selCol = $("#out-sel-col");
    selCol.innerHTML = collections.length
      ? collections.map((c) => `<option value="${c.id}">${App.esc(c.name)}</option>`).join("")
      : '<option value="">（先新建一个收藏夹）</option>';
    if (detailIdx >= 0) renderDetailCollections();
  }

  async function reloadCollections() {
    try { collections = ((await App.api.outputs_collections()) || {}).collections || []; } catch (e) { collections = []; }
    renderCollections();
  }

  async function askName(title, value) {
    let name = null;
    const v = await App.modal(title, `<input type="text" id="col-name-in" maxlength="40" value="${App.esc(value || "")}" placeholder="收藏夹名字">`,
      [{ id: "ok", label: "确定", kind: "primary" }, { id: "cancel", label: "取消" }],
      {
        onOpen(body, finish) {
          const inp = body.querySelector("#col-name-in");
          inp.focus(); inp.select();
          inp.addEventListener("keydown", (e) => { if (e.key === "Enter") finish("ok"); });
        },
        onClose(body) { name = (body.querySelector("#col-name-in") || {}).value; },
      });
    return v === "ok" ? (name || "").trim() : null;
  }

  async function collectionOp(op, cid) {
    const c = collections.find((x) => x.id === cid);
    try {
      if (op === "new") {
        const name = await askName("新建收藏夹", "");
        if (!name) return null;
        const r = await App.api.outputs_collection_op("create", null, name);
        if (r && r.ok === false) { App.toast(r.error, "error"); return null; }
        collections = r.collections; renderCollections();
        return r.id;
      }
      if (!c) return null;
      if (op === "rename") {
        const name = await askName("收藏夹改名", c.name);
        if (!name) return null;
        const r = await App.api.outputs_collection_op("rename", cid, name);
        if (r && r.ok === false) { App.toast(r.error, "error"); return null; }
        collections = r.collections; renderCollections();
      } else if (op === "delete") {
        if (!await App.confirm("删除收藏夹", `删除收藏夹「${c.name}」？\n只删除收藏记录，图片文件本身不受影响。`, "删除", true)) return null;
        const r = await App.api.outputs_collection_op("delete", cid);
        collections = r.collections;
        if (String(filters.collection) === String(cid)) filters.collection = "";
        renderCollections(); scheduleQuery(0);
      } else if (op === "export") {
        const d = await App.api.choose_directory(`把「${c.name}」导出到…`, "");
        if (!d || !d.ok || !d.path) return null;
        const r = await App.api.outputs_collection_export(cid, d.path);
        if (r && r.ok === false) App.toast(r.error, "error");
        else App.toast(`正在复制 ${r.count} 个文件…`, "", 3000);
      }
    } catch (e) { App.toast("操作失败：" + e.message, "error"); }
    return null;
  }

  /* ---------- 虚拟滚动网格 ---------- */
  let cols = 1;
  const cellPool = new Map();   // 当前渲染着的格子：索引 → 元素

  function layout(reset) {
    const wrap = $("#out-scroll");
    const w = wrap.clientWidth - 4;
    cols = Math.max(1, Math.floor((w + GAP) / (CELL + GAP)));
    const rows = Math.ceil(ids.length / cols);
    $("#out-grid").style.height = Math.max(0, rows * (CELL + GAP) - GAP) + "px";
    if (reset) {
      cellPool.forEach((el) => el.remove());
      cellPool.clear();
      wrap.scrollTop = 0;
    }
    renderVisible();
  }

  function renderVisible() {
    const wrap = $("#out-scroll");
    const grid = $("#out-grid");
    const rowH = CELL + GAP;
    const first = Math.max(0, Math.floor(wrap.scrollTop / rowH) - 2);
    const last = Math.min(Math.ceil(ids.length / cols), Math.ceil((wrap.scrollTop + wrap.clientHeight) / rowH) + 2);
    const want = new Set();
    for (let r = first; r < last; r++) {
      for (let c = 0; c < cols; c++) {
        const i = r * cols + c;
        if (i >= ids.length) break;
        want.add(i);
        let el = cellPool.get(i);
        if (!el) {
          el = makeCell(i);
          grid.appendChild(el);
          cellPool.set(i, el);
        }
        el.style.transform = `translate(${c * (CELL + GAP)}px, ${r * rowH}px)`;
      }
    }
    cellPool.forEach((el, i) => { if (!want.has(i)) { el.remove(); cellPool.delete(i); } });
  }

  function makeCell(i) {
    const id = ids[i], kind = { i: "image", v: "video", o: "other" }[kinds[i]] || "other";
    const el = document.createElement("div");
    el.className = "oc" + (selected.has(id) ? " sel" : "") + (missing.has(id) ? " missing" : "");
    el.dataset.i = i;
    el.style.width = el.style.height = CELL + "px";
    el.innerHTML = (kind === "video" ? '<span class="oc-badge">▶ 视频</span>' : "") +
      (missing.has(id) ? '<span class="oc-badge oc-missing">已失效</span>' : "") +
      '<span class="oc-check"></span>';
    if (kind === "other") {
      el.insertAdjacentHTML("afterbegin", '<div class="oc-other">FILE</div>');
    } else if (!missing.has(id)) {
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "";
      img.src = thumbUrl(i);
      img.onerror = () => { img.onerror = null; makeThumb(id, kind, img); };
      el.prepend(img);
    }
    el.addEventListener("click", (e) => onCellClick(i, e));
    return el;
  }

  /* ---------- 缩略图：浏览器画好传回后端缓存 ---------- */
  const thumbQueue = [];
  let thumbActive = 0;

  function makeThumb(id, kind, img) {
    thumbQueue.push({ id, kind, img });
    pumpThumbs();
  }

  function pumpThumbs() {
    while (thumbActive < 4 && thumbQueue.length) {
      const job = thumbQueue.shift();
      if (!job.img.isConnected) continue;      // 已经滚出屏幕了
      thumbActive++;
      const done = () => { thumbActive--; pumpThumbs(); };
      (job.kind === "video" ? thumbFromVideo(job) : thumbFromImage(job)).then(done, done);
    }
  }

  function drawAndUpload(id, source, sw, sh, img) {
    const scale = Math.min(1, THUMB / Math.max(sw, sh));
    const cv = document.createElement("canvas");
    cv.width = Math.max(1, Math.round(sw * scale));
    cv.height = Math.max(1, Math.round(sh * scale));
    cv.getContext("2d").drawImage(source, 0, 0, cv.width, cv.height);
    return new Promise((resolve) => cv.toBlob((blob) => {
      if (!blob) { resolve(); return; }
      img.src = URL.createObjectURL(blob);
      fetch(url("t", id), { method: "POST", body: blob, headers: { "Content-Type": "image/webp" } })
        .catch(() => {}).finally(resolve);
    }, "image/webp", 0.82));
  }

  function thumbFromImage({ id, img }) {
    return new Promise((resolve) => {
      const src = new Image();
      src.crossOrigin = "anonymous";
      src.onload = () => drawAndUpload(id, src, src.naturalWidth, src.naturalHeight, img).then(resolve);
      src.onerror = () => { img.replaceWith(Object.assign(document.createElement("div"), { className: "oc-other", textContent: "无法预览" })); resolve(); };
      src.src = url("f", id);
    });
  }

  function thumbFromVideo({ id, img }) {
    return new Promise((resolve) => {
      const v = document.createElement("video");
      v.crossOrigin = "anonymous";
      v.muted = true;
      v.preload = "metadata";
      let finished = false;
      const end = () => { if (!finished) { finished = true; v.removeAttribute("src"); v.load(); resolve(); } };
      v.onloadeddata = () => { v.currentTime = Math.min(0.5, (v.duration || 1) / 3); };
      v.onseeked = () => drawAndUpload(id, v, v.videoWidth, v.videoHeight, img).then(end);
      v.onerror = () => {
        img.replaceWith(Object.assign(document.createElement("div"), { className: "oc-other", textContent: "▶ 视频" }));
        end();
      };
      setTimeout(end, 15000);
      v.src = url("f", id);
    });
  }

  /* ---------- 选择 / 详情 ---------- */
  function onCellClick(i, e) {
    const id = ids[i];
    if (selectMode || e.ctrlKey || e.metaKey) {
      if (!selectMode) setSelectMode(true);
      if (selected.has(id)) selected.delete(id); else selected.add(id);
      const el = cellPool.get(i);
      if (el) el.classList.toggle("sel", selected.has(id));
      updateSelBar();
      return;
    }
    openDetail(i);
  }

  function setSelectMode(v) {
    selectMode = v;
    $("#out-select-mode").classList.toggle("btn-accent", v);
    $("#out-select-mode").textContent = v ? "退出多选" : "多选";
    $("#page-outputs").classList.toggle("selecting", v);
    if (!v) {
      selected.clear();
      cellPool.forEach((el) => el.classList.remove("sel"));
    }
    updateSelBar();
  }

  function updateSelBar() {
    $("#out-selbar").hidden = !selectMode;
    $("#out-sel-count").textContent = `已选 ${selected.size} 个`;
  }

  let detail = null;

  async function openDetail(i) {
    detailIdx = i;
    const id = ids[i];
    const box = $("#out-detail");
    box.hidden = false;
    $("#od-title").textContent = "读取中…";
    let d;
    try { d = await App.api.outputs_detail(id); } catch (e) { d = { ok: false, error: e.message }; }
    if (detailIdx !== i) return;       // 读的时候用户已经翻到别的了
    if (!d || d.ok === false) { $("#od-title").textContent = (d && d.error) || "读取失败"; return; }
    detail = d;
    $("#od-title").textContent = d.name;
    const media = $("#od-media");
    if (!d.exists) media.innerHTML = '<div class="od-missing">文件已不在原来的位置（被移动或删除）</div>';
    else if (d.kind === "video") media.innerHTML = `<video src="${url("f", id)}" controls autoplay loop muted></video>`;
    else if (d.kind === "image") media.innerHTML = `<img src="${url("f", id)}" alt="">`;
    else media.innerHTML = `<div class="od-missing">${App.esc(d.name)}<br>这种文件不能在这里预览，点「打开」用系统程序打开</div>`;
    const mediaImg = media.querySelector("img");
    if (mediaImg) {   // 点图片放大浏览
      mediaImg.title = "点击放大";
      mediaImg.addEventListener("click", () => lbShow(url("f", id)));
    }

    const kv = (k, v) => v ? `<span class="k">${k}</span><span class="v">${App.esc(v)}</span>` : "";
    const chips = (arr, attr) => arr.map((t) =>
      `<button class="chip" ${attr}="${App.esc(t)}">${App.esc(t.replace(/_/g, " "))}</button>`).join("");
    $("#od-body").innerHTML =
      `<div class="kv-table">` +
      kv("时间", d.time_text) + kv("尺寸", d.width && d.height ? `${d.width} × ${d.height}` : "") +
      kv("大小", d.size_text) + (App.instances && App.instances.multi ? kv("来源实例", d.instance_name) : "") +
      kv("模型", d.model) + kv("种子", d.seed) + kv("采样器", d.sampler) + kv("来源", d.source) +
      `</div>` +
      (d.chars.length ? `<div class="od-sect">角色</div><div class="chips">${chips(d.chars, "data-fchar")}</div>` : "") +
      (d.loras.length ? `<div class="od-sect">LoRA</div><div class="chips">${d.loras.map((l) =>
        `<button class="chip" data-flora="${App.esc(l.name)}">${App.esc(l.name)}${l.weight ? " · " + App.esc(l.weight) : ""}</button>`).join("")}</div>` : "") +
      (d.prompt ? `<div class="od-sect">正向提示词<button class="btn btn-xs" data-copy="prompt">复制</button></div><div class="od-text">${App.esc(d.prompt)}</div>` : "") +
      (d.negative ? `<div class="od-sect">反向提示词<button class="btn btn-xs" data-copy="negative">复制</button></div><div class="od-text dim">${App.esc(d.negative)}</div>` : "") +
      (d.tags.length ? `<div class="od-sect">tag（点击加入筛选）</div><div class="chips">${chips(d.tags.slice(0, 80), "data-ftag")}</div>` : "") +
      (!d.prompt && d.kind === "image" ? '<div class="hint">这张图里没有读到生成参数</div>' : "") +
      `<div class="od-path">${App.esc(d.path)}</div>`;
    $$("#od-body [data-fchar]").forEach((b) => b.addEventListener("click", () => {
      if (!filters.chars.includes(b.dataset.fchar)) toggleArr("chars", b.dataset.fchar);
    }));
    $$("#od-body [data-flora]").forEach((b) => b.addEventListener("click", () => {
      filters.lora = b.dataset.flora; scheduleQuery(0);
    }));
    $$("#od-body [data-ftag]").forEach((b) => b.addEventListener("click", () => {
      if (!filters.tags.includes(b.dataset.ftag)) toggleArr("tags", b.dataset.ftag);
    }));
    $$("#od-body [data-copy]").forEach((b) => b.addEventListener("click", () =>
      App.copy(detail[b.dataset.copy] || "", "已复制")));
    renderDetailCollections();
    $("#od-prev").disabled = i <= 0;
    $("#od-next").disabled = i >= ids.length - 1;
  }

  function renderDetailCollections() {
    if (!detail) return;
    const on = new Set(detail.collections || []);
    $("#od-cols").innerHTML = '<span class="od-cols-label">收藏到</span>' +
      collections.map((c) => `<button class="chip${on.has(c.id) ? " on" : ""}" data-cid="${c.id}">★ ${App.esc(c.name)}</button>`).join("") +
      '<button class="chip" data-cid="new">＋ 新收藏夹</button>';
    $$("#od-cols [data-cid]").forEach((b) => b.addEventListener("click", async () => {
      let cid = b.dataset.cid;
      let want = true;
      if (cid === "new") {
        cid = await collectionOp("new");
        if (!cid) return;
      } else {
        cid = Number(cid);
        want = !on.has(cid);
      }
      const r = await App.api.outputs_collection_op("set", cid, "", [detail.id], want);
      if (r && r.ok !== false) {
        collections = r.collections;
        detail.collections = want ? [...(detail.collections || []), cid] : (detail.collections || []).filter((x) => x !== cid);
        renderCollections();
      }
    }));
  }

  function closeDetail() {
    lbHide();
    $("#out-detail").hidden = true;
    const v = $("#od-media video");
    if (v) v.pause();
    detailIdx = -1;
    detail = null;
  }

  /* ---------- 图片放大浏览 ----------
   * 详情里点图片进入；滚轮以光标为中心缩放，拖动平移，
   * 双击在「适应窗口 / 实际大小」间切换，点空白处或 Esc 退出。 */
  const lb = { scale: 1, x: 0, y: 0, natW: 0, natH: 0 };

  function lbApply() {
    const img = $("#lb-img");
    img.style.width = Math.round(lb.natW * lb.scale) + "px";
    img.style.transform = `translate(calc(-50% + ${lb.x}px), calc(-50% + ${lb.y}px))`;
    $("#lb-pct").textContent = Math.round(lb.scale * 100) + "%";
  }

  function lbFit() {
    const r = $("#lb-stage").getBoundingClientRect();
    if (lb.natW && lb.natH) lb.scale = Math.min(r.width / lb.natW, r.height / lb.natH, 1);
    lb.x = lb.y = 0;
    lbApply();
  }

  function lbShow(src) {
    const img = $("#lb-img");
    $("#lightbox").hidden = false;
    img.onload = () => { lb.natW = img.naturalWidth; lb.natH = img.naturalHeight; lbFit(); };
    img.src = src;
  }

  function lbHide() {
    const box = $("#lightbox");
    if (box.hidden) return;
    box.hidden = true;
    $("#lb-img").removeAttribute("src");
  }

  function lbZoom(k, cx, cy) {
    const r = $("#lb-stage").getBoundingClientRect();
    if (cx == null) { cx = r.width / 2; cy = r.height / 2; }
    const ns = Math.min(20, Math.max(0.02, lb.scale * k));
    // 保持光标（或中心）下的那个图点不动
    const px = cx - r.width / 2 - lb.x, py = cy - r.height / 2 - lb.y;
    lb.x = cx - r.width / 2 - px * ns / lb.scale;
    lb.y = cy - r.height / 2 - py * ns / lb.scale;
    lb.scale = ns;
    lbApply();
  }

  function bindLightbox() {
    const stage = $("#lb-stage");
    stage.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = stage.getBoundingClientRect();
      lbZoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    let pan = null;
    stage.addEventListener("pointerdown", (e) => {
      pan = { x: e.clientX, y: e.clientY, ox: lb.x, oy: lb.y, moved: false };
      stage.classList.add("panning");
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", (e) => {
      if (!pan) return;
      const dx = e.clientX - pan.x, dy = e.clientY - pan.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) pan.moved = true;
      lb.x = pan.ox + dx; lb.y = pan.oy + dy;
      lbApply();
    });
    stage.addEventListener("pointerup", (e) => {
      const wasPan = pan && pan.moved;
      pan = null;
      stage.classList.remove("panning");
      if (!wasPan && e.target === stage) lbHide();   // 点图片外的空白处退出
    });
    stage.addEventListener("dblclick", () => {
      if (Math.abs(lb.scale - 1) < 0.01) lbFit();
      else { lb.scale = 1; lb.x = lb.y = 0; lbApply(); }
    });
    $("#lb-in").addEventListener("click", () => lbZoom(1.25));
    $("#lb-out").addEventListener("click", () => lbZoom(1 / 1.25));
    $("#lb-fit").addEventListener("click", lbFit);
    $("#lb-actual").addEventListener("click", () => { lb.scale = 1; lb.x = lb.y = 0; lbApply(); });
    $("#lb-close").addEventListener("click", lbHide);
  }

  async function deleteIds(list) {
    if (!list.length) return;
    const yes = await App.confirm("删除文件", `把 ${list.length} 个文件放进回收站？\n（可以在系统回收站里还原）`, "删除", true);
    if (!yes) return;
    try {
      const r = await App.api.outputs_delete(list);
      App.toast(`已删除 ${r.deleted} 个` + (r.failed && r.failed.length ? `，${r.failed.length} 个删除失败（可能被占用）` : ""),
        r.failed && r.failed.length ? "error" : "ok");
      closeDetail();
      setSelectMode(false);
      reloadCollections();
      scheduleQuery(0);
    } catch (e) { App.toast("删除失败：" + e.message, "error"); }
  }

  /* ---------- 扫描 ---------- */
  async function scan() {
    try {
      const r = await App.api.outputs_scan();
      if (r && r.ok !== false && !r.busy) { scanning = true; $("#out-scan-hint").textContent = "正在扫描…"; }
    } catch (e) { console.error(e); }
  }

  function autoScanTick() {
    clearTimeout(autoTimer);
    autoTimer = setTimeout(async () => {
      if (!visible) return;
      const running = App.instances && App.instances.instances.some((i) => i.status && i.status.running);
      if (running && !scanning) scan();   // 有实例在出图时，每 20 秒收一次新图
      autoTimer = 0;
      autoScanTick();
    }, 20000);
  }

  async function ensureInit() {
    if (inited) return true;
    try {
      const r = await App.api.outputs_info();
      if (!r || r.ok === false) { App.toast((r && r.error) || "输出管理初始化失败", "error"); return false; }
      base = r.base; token = r.token;
      collections = r.collections || [];
      const sel = $("#of-inst");
      sel.innerHTML = '<option value="">全部实例</option>' +
        (r.instances || []).map((i) => `<option value="${App.esc(i.id)}">${App.esc(i.name)}</option>`).join("");
      $("#of-inst-group").hidden = !r.multi;
      if (!r.dirs || !r.dirs.length) $("#out-scan-hint").textContent = "还没有设置任何实例的根目录";
      inited = true;
      renderCollections();
      return true;
    } catch (e) { App.toast("输出管理初始化失败：" + e.message, "error"); return false; }
  }

  App.pages.outputs = {
    init() {
      $("#out-scroll").addEventListener("scroll", () => requestAnimationFrame(renderVisible), { passive: true });
      window.addEventListener("resize", () => { if (visible) layout(false); });

      $("#of-q").addEventListener("input", () => { filters.q = $("#of-q").value; scheduleQuery(350); });
      $("#of-time").addEventListener("change", () => { timeDays = $("#of-time").value; scheduleQuery(0); });
      $$("#of-kind .chip").forEach((b) => b.addEventListener("click", () => {
        $$("#of-kind .chip").forEach((x) => x.classList.toggle("on", x === b));
        filters.kind = b.dataset.kind; scheduleQuery(0);
      }));
      $("#of-inst").addEventListener("change", () => { filters.iid = $("#of-inst").value; scheduleQuery(0); });
      $("#of-lora").addEventListener("change", () => { filters.lora = $("#of-lora").value; scheduleQuery(0); });
      $("#of-model").addEventListener("change", () => { filters.model = $("#of-model").value; scheduleQuery(0); });
      $("#of-char-q").addEventListener("input", renderChars);
      $("#of-tag-in").addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        const t = e.target.value.trim().toLowerCase().replace(/\s+/g, "_");
        e.target.value = "";
        if (t && !filters.tags.includes(t)) toggleArr("tags", t);
      });
      $("#of-tag-in").addEventListener("change", (e) => {    // 从下拉建议里点选
        const t = e.target.value.trim();
        if (t && Array.from($("#of-tag-dl").options).some((o) => o.value === t)) {
          e.target.value = "";
          if (!filters.tags.includes(t)) toggleArr("tags", t);
        }
      });
      $("#of-reset").addEventListener("click", () => {
        filters = { q: "", kind: "", iid: "", collection: "", chars: [], tags: [], lora: "", model: "", sort: filters.sort };
        timeDays = "";
        $("#of-q").value = ""; $("#of-time").value = ""; $("#of-inst").value = ""; $("#of-char-q").value = "";
        $$("#of-kind .chip").forEach((x) => x.classList.toggle("on", !x.dataset.kind));
        renderTagChips(); renderCollections(); scheduleQuery(0);
      });
      $("#of-col-new").addEventListener("click", () => collectionOp("new"));
      $("#out-sort").addEventListener("change", () => { filters.sort = $("#out-sort").value; scheduleQuery(0); });
      $("#out-rescan").addEventListener("click", scan);
      $("#out-select-mode").addEventListener("click", () => setSelectMode(!selectMode));
      $("#out-sel-clear").addEventListener("click", () => setSelectMode(false));
      $("#out-sel-del").addEventListener("click", () => deleteIds(Array.from(selected)));
      $("#out-sel-add").addEventListener("click", async () => {
        let cid = Number($("#out-sel-col").value);
        if (!cid) { cid = await collectionOp("new"); if (!cid) return; }
        if (!selected.size) { App.toast("先点选要收藏的图", "error"); return; }
        const r = await App.api.outputs_collection_op("set", cid, "", Array.from(selected), true);
        if (r && r.ok !== false) {
          collections = r.collections; renderCollections();
          App.toast(`已加入收藏夹（${selected.size} 个）`, "ok");
          setSelectMode(false);
        }
      });

      $("#od-close").addEventListener("click", closeDetail);
      $("#od-prev").addEventListener("click", () => { if (detailIdx > 0) openDetail(detailIdx - 1); });
      $("#od-next").addEventListener("click", () => { if (detailIdx < ids.length - 1) openDetail(detailIdx + 1); });
      $("#od-open").addEventListener("click", () => detail && App.api.outputs_open(detail.id));
      $("#od-reveal").addEventListener("click", () => detail && App.api.outputs_reveal(detail.id));
      $("#od-del").addEventListener("click", () => detail && deleteIds([detail.id]));
      $("#od-meta").addEventListener("click", () => {
        if (!detail || !App.pages.meta) return;
        const p = detail.path;
        closeDetail();
        App.showPage("meta");
        if (App.pages.meta.onDropped) App.pages.meta.onDropped([p]);
      });
      document.addEventListener("keydown", (e) => {
        if (App.currentPage !== "outputs" || $("#out-detail").hidden) return;
        if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
        if (!$("#lightbox").hidden) {   // 放大浏览开着时，Esc 只退放大
          if (e.key === "Escape") lbHide();
          return;
        }
        if (e.key === "Escape") closeDetail();
        else if (e.key === "ArrowLeft") $("#od-prev").click();
        else if (e.key === "ArrowRight") $("#od-next").click();
      });

      // 点详情抽屉外任意处退出（点网格里的图是切换/选择，不算退出；放大浏览层和弹窗也不算）
      document.addEventListener("click", (e) => {
        if (App.currentPage !== "outputs" || $("#out-detail").hidden) return;
        if (e.target.closest("#out-detail, #lightbox, #modal-mask, .oc")) return;
        closeDetail();
      });

      bindLightbox();

      App.on("outputs", "scan_progress", (e) => {
        scanning = true;
        $("#out-scan-hint").textContent = `正在索引 ${e.done}/${e.total}`;
      });
      App.on("outputs", "scan_done", (e) => {
        scanning = false;
        const bits = [];
        if (e.added) bits.push(`新增 ${e.added}`);
        if (e.relinked) bits.push(`找回改名的 ${e.relinked}`);
        if (e.missing) bits.push(`${e.missing} 个收藏已失效`);
        $("#out-scan-hint").textContent = bits.length ? "索引已更新：" + bits.join("，") : "";
        if (e.removed) bits.push(`移除已删除的 ${e.removed}`);
        if (e.added || e.updated || e.missing || e.relinked || e.removed || !ids.length) { reloadCollections(); scheduleQuery(0); }
      });
      App.on("outputs", "export_done", (e) =>
        App.toast(`导出完成：复制了 ${e.copied} 个文件到 ${e.dest}` + (e.failed ? `，${e.failed} 个失败` : ""), e.failed ? "error" : "ok", 6000));
    },

    async onShow() {
      visible = true;
      if (!(await ensureInit())) return;
      layout(false);
      scheduleQuery(0);
      scan();
      autoScanTick();
    },

    onHide() { visible = false; clearTimeout(autoTimer); closeDetail(); },
  };

})();
