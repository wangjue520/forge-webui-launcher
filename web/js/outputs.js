/* outputs.js — 输出管理器
 *
 * 数据：后端 SQLite 索引（output_index.py），这里只拿 id 列表，缩略图/原图走后端起的
 * 本地文件服务（127.0.0.1 随机端口 + token）。
 * 缩略图：先请求缓存；没有就由浏览器加载原图（视频截一帧）画到 canvas，压成 webp
 * 传回后端缓存——不需要 Pillow / ffmpeg。
 * 网格是虚拟滚动：几万张图也只渲染屏幕上那几十个格子。
 * 「莱茵 · 分析终端」风格下换成档案架视图：每个文件夹一排，没展开时是一叠斜插的亚克力档案板，
 * 点开后板子从堆里依次飞出平铺成网格；工具栏可以一键全部平铺（同样是虚拟滚动）。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const CELL = 168, GAP = 8, THUMB = 320;
  let base = "", token = "";
  let ids = [], kinds = "", missing = new Set(), vers = [];
  let folders = [], fidx = [];       // 所在文件夹（去重列表 + 每个文件的下标），档案架视图分排用
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
      ratios = (r && r.ratios) || [];
      names = (r && r.names) || [];
      folders = (r && r.folders) || [];
      fidx = (r && r.fidx) || [];
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
        <div class="col-item${cur === String(c.id) ? " on" : ""}" data-col="${c.id}"
          title="${c.copy_dir ? "收藏时复制一份到：" + App.esc(c.copy_dir) : "不转移：只记录，不复制文件"}">
          <svg class="btn-icon"><use href="#i-star"/></svg><span class="col-name">${App.esc(c.name)}${c.copy_dir ? ' <i class="col-copy">⇢ 复制</i>' : ""}</span><em>${c.count}</em>
          <span class="col-ops"><button title="设置：改名、收藏时要不要复制一份到文件夹" data-op="settings">⚙</button><button title="把收藏夹里的图全部复制一份到任意文件夹" data-op="export">⤓</button><button title="删除收藏夹（图片文件不受影响）" data-op="delete">✕</button></span>
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

  /** 新建 / 设置收藏夹：名字 + 收藏时「不转移（只记录）」还是「复制一份到某个文件夹」。
   *  返回 {name, copy_dir, copy_existing} 或 null（取消） */
  async function askCollection(title, c) {
    const local = !App.remote;      // 联动控制远程主机时，文件夹选的是这台电脑的，复制却在主机上做：不提供
    let out = null, dir = (c && c.copy_dir) || "";
    const isNew = !c;
    const html = `<div class="col-dlg">
      <label class="col-dlg-k">名字</label>
      <input type="text" id="col-name-in" maxlength="40" value="${App.esc((c && c.name) || "")}" placeholder="收藏夹名字">
      <label class="col-dlg-k">收藏的时候</label>
      <label class="chk-row"><input type="radio" name="col-mode" value="none"${dir ? "" : " checked"}><i></i><span>不转移：只记在收藏夹里，图片留在原来的位置</span></label>
      ${local ? `<label class="chk-row"><input type="radio" name="col-mode" value="copy"${dir ? " checked" : ""}><i></i><span>同时复制一份到文件夹（原图不动）</span></label>
      <div class="col-dlg-dir"><code id="col-dir-text">${App.esc(dir || "（还没选文件夹）")}</code><button class="btn btn-xs" id="col-dir-pick" type="button">选择文件夹…</button></div>
      ${isNew ? "" : `<label class="chk-row" id="col-copy-old-row"><input type="checkbox" id="col-copy-old"><i></i><span>把已经收藏的 ${c.count} 张也复制过去（同名同大小的会跳过）</span></label>`}` :
        `<div class="hint">正在控制远程主机：收藏夹只记录，不能复制到文件夹</div>`}
    </div>`;
    const v = await App.modal(title, html, [{ id: "ok", label: "确定", kind: "primary" }, { id: "cancel", label: "取消" }], {
      onOpen(body, finish) {
        const inp = body.querySelector("#col-name-in");
        inp.focus(); inp.select();
        inp.addEventListener("keydown", (e) => { if (e.key === "Enter") finish("ok"); });
        const pick = body.querySelector("#col-dir-pick");
        if (pick) pick.addEventListener("click", async () => {
          const d = await App.api.choose_directory("收藏时复制到哪个文件夹", dir);
          if (d && d.ok && d.path) {
            dir = d.path;
            body.querySelector("#col-dir-text").textContent = dir;
            body.querySelector('input[name="col-mode"][value="copy"]').checked = true;
          }
        });
      },
      onClose(body) {
        const mode = (body.querySelector('input[name="col-mode"]:checked') || {}).value;
        const old = body.querySelector("#col-copy-old");
        out = { name: ((body.querySelector("#col-name-in") || {}).value || "").trim(), mode,
          copy_dir: mode === "copy" ? dir : "", copy_existing: mode === "copy" && !!(old && old.checked) };
      },
    });
    if (v !== "ok" || !out) return null;
    if (!out.name) { App.toast("名字不能为空", "error"); return null; }
    if (out.mode === "copy" && !out.copy_dir) { App.toast("选了「复制一份到文件夹」，但还没选文件夹", "error", 5000); return null; }
    return out;
  }

  function copyToast(r, n) {
    if (r && r.copying) App.toast(`已加入收藏夹（${n} 个），正在复制 ${r.copying} 个到收藏夹的文件夹…`, "ok", 4000);
    else App.toast(`已加入收藏夹（${n} 个）`, "ok");
  }

  async function collectionOp(op, cid) {
    const c = collections.find((x) => x.id === cid);
    try {
      if (op === "new") {
        const o = await askCollection("新建收藏夹", null);
        if (!o) return null;
        const r = await App.api.outputs_collection_op("create", null, o.name, null, true, o.copy_dir);
        if (r && r.ok === false) { App.toast(r.error, "error", 6000); return null; }
        collections = r.collections; renderCollections();
        return r.id;
      }
      if (!c) return null;
      if (op === "settings") {
        const o = await askCollection("收藏夹设置", c);
        if (!o) return null;
        if (o.name !== c.name) {
          const r = await App.api.outputs_collection_op("rename", cid, o.name);
          if (r && r.ok === false) { App.toast(r.error, "error"); return null; }
          collections = r.collections;
        }
        if (o.copy_dir !== (c.copy_dir || "") || o.copy_existing) {
          const r = await App.api.outputs_collection_op("config", cid, "", null, true, o.copy_dir, o.copy_existing);
          if (r && r.ok === false) { App.toast(r.error, "error", 6000); return null; }
          collections = r.collections;
          App.toast(o.copy_dir ? (r.copying ? `之后收藏的会复制一份到 ${o.copy_dir}；已有的 ${r.copying} 张正在复制…` :
            `之后收藏的会复制一份到 ${o.copy_dir}`) : "改成不转移：之后收藏只记录，不复制文件", "ok", 5000);
        }
        renderCollections();
      } else if (op === "rename") {
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

  /* ---------- 虚拟滚动网格 ----------
   * 按原图长宽比排：每行等高（约 CELL），一张图多宽由它自己的比例决定，一行排满后整行缩放到正好铺满宽度
   * （最后一行不拉伸）。只渲染屏幕附近那几行。比例来自索引里的宽高（outputs_query 的 ratios），读不到的当方图 */
  let cols = 1;
  const cellPool = new Map();   // 当前渲染着的格子：索引 → 元素
  let ratios = [], names = [];
  let jRows = [], jPos = [];    // 每行 {y, h, a, b}（下标 a..b-1）；每张 {x, y, w, h}
  const RATIO_MIN = 0.4, RATIO_MAX = 2.6, ROW_GROW = 1.5;

  function justify(W) {
    jRows = []; jPos = new Array(ids.length);
    let row = [], sum = 0, y = 0;
    const flush = (last) => {
      if (!row.length) return;
      const gaps = GAP * (row.length - 1);
      let scale = (W - gaps) / sum;
      if (last) scale = Math.min(scale, 1);
      scale = Math.min(scale, ROW_GROW);          // 一行只有一两张很窄的竖图时别把行高撑得太离谱
      const h = Math.round(CELL * scale);
      let x = 0;
      row.forEach((i) => {
        const w = Math.max(24, Math.round(jR(i) * CELL * scale));
        jPos[i] = { x, y, w, h };
        x += w + GAP;
      });
      jRows.push({ y, h, a: row[0], b: row[row.length - 1] + 1 });
      y += h + GAP;
      row = []; sum = 0;
    };
    for (let i = 0; i < ids.length; i++) {
      row.push(i);
      sum += jR(i) * CELL;
      if (sum + GAP * (row.length - 1) >= W) flush(false);
    }
    flush(true);
    return Math.max(0, y - GAP);
  }

  function jR(i) {
    const r = Number(ratios[i]) || 1;
    return Math.min(RATIO_MAX, Math.max(RATIO_MIN, r));
  }

  function layout(reset) {
    if (shelfOn()) { shelfLayout(reset); return; }
    if (shelfWas) { reset = true; shelfWas = false; clearShelf(); cs = CELL; }
    const wrap = $("#out-scroll");
    const w = wrap.clientWidth - 4;
    cols = Math.max(1, Math.floor((w + GAP) / (CELL + GAP)));
    $("#out-grid").style.height = justify(Math.max(CELL, w)) + "px";
    if (reset) {
      cellPool.forEach((el) => el.remove());
      cellPool.clear();
      wrap.scrollTop = 0;
    }
    renderVisible();
  }

  function renderVisible() {
    if (shelfOn()) { shelfRender(); return; }
    const wrap = $("#out-scroll");
    const grid = $("#out-grid");
    const top = wrap.scrollTop - 2 * CELL, bot = wrap.scrollTop + wrap.clientHeight + 2 * CELL;
    let lo = 0, hi = jRows.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (jRows[m].y + jRows[m].h < top) lo = m + 1; else hi = m; }
    const want = new Set();
    for (let r = lo; r < jRows.length && jRows[r].y < bot; r++) {
      for (let i = jRows[r].a; i < jRows[r].b; i++) {
        const p = jPos[i];
        want.add(i);
        let el = cellPool.get(i);
        if (!el) {
          el = makeCell(i);
          grid.appendChild(el);
          cellPool.set(i, el);
        }
        if (el.style.width !== p.w + "px") el.style.width = p.w + "px";
        if (el.style.height !== p.h + "px") el.style.height = p.h + "px";
        el.style.transform = `translate(${p.x}px, ${p.y}px)`;
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
      img.decoding = "async";
      img.decoding = "async";
      img.decoding = "async";
      img.alt = "";
      img.draggable = false;          // 拖的是整个格子（原图），不是缩略图
      img.src = thumbUrl(i);
      img.onerror = () => { img.onerror = null; makeThumb(id, kind, img); };
      el.prepend(img);
    }
    if (!missing.has(id) && !App.remote) {
      el.draggable = true;
      el.title = "拖到桌面 / 文件夹 = 复制一份原图";
      el.addEventListener("dragstart", (e) => dragOut(e, i));
    }
    el.addEventListener("click", (e) => onCellClick(i, e));
    return el;
  }

  /* ---------- 档案架视图（莱茵风格）：每个文件夹一排亚克力档案板 ---------- */
  const SH = { HEAD: 48, STACK: 212, PAD: 20, STEP: 30, MAX: 48 };
  let groups = [], gOf = [], lines = [], shelfWas = false;
  const headPool = new Map();
  const openState = new Map();          // 文件夹路径 → 是否平铺（换筛选后保持）
  let shelfAll = false;
  try { shelfAll = localStorage.getItem("out-shelf-all") === "1"; } catch (e) {}
  let animG = -1, animUntil = 0, delayT = 0;
  let cs = CELL;                         // 档案架里的格子边长：按可用宽度撑满，不留右侧空白

  function shelfOn() { return document.documentElement.dataset.uiTheme === "rhine"; }

  function clearShelf() {
    headPool.forEach((el) => el.remove()); headPool.clear();
    cellPool.forEach((el) => el.remove()); cellPool.clear();
    $("#out-grid").classList.remove("shelf");
  }

  function buildGroups() {
    const map = new Map();
    groups = []; gOf = new Array(ids.length);
    for (let i = 0; i < ids.length; i++) {
      const k = fidx.length ? fidx[i] : 0;
      let g = map.get(k);
      if (!g) {
        const full = folders[k] || "";
        const seg = full.split(/[\\/]/).filter(Boolean);
        g = { key: full, full, label: seg.slice(-2).join(" / ") || "全部输出", items: [], gi: groups.length };
        map.set(k, g); groups.push(g);
      }
      gOf[i] = g.gi;
      g.items.push(i);
    }
    groups.forEach((g, n) => { g.open = openState.has(g.key) ? openState.get(g.key) : (shelfAll || n === 0); });
    syncShelfBtn();
  }

  function stackCount(g) {
    const w = $("#out-scroll").clientWidth - 4;
    return Math.min(g.items.length, SH.MAX, Math.max(1, Math.floor((w - cs - 40) / SH.STEP) + 1));
  }
  // 档案堆里第 k 张：往右依次退后，斜插着只露出一截
  function stackT(k, lineY) {
    return `translate(${k * SH.STEP}px, ${lineY + 18}px) perspective(900px) rotateY(-34deg) scale(.93)`;
  }
  function gridT(c, y) {
    return `translate(${c * (cs + GAP)}px, ${y}px)`;   // 平铺状态是纯 2D：不让每个格子各占一个 3D 合成层
  }

  function shelfLayout(reset) {
    const wrap = $("#out-scroll"), grid = $("#out-grid");
    const w = wrap.clientWidth - 4;
    cols = Math.max(1, Math.floor((w + GAP) / (CELL + GAP)));
    const ncs = Math.floor((w - GAP * (cols - 1)) / cols);
    if (ncs !== cs) { cs = ncs; cellPool.forEach((el) => { el.style.width = el.style.height = cs + "px"; }); }
    if (reset || !shelfWas) {
      clearShelf();
      buildGroups();
      if (reset) wrap.scrollTop = 0;
    }
    shelfWas = true;
    grid.classList.add("shelf");
    lines = [];
    let y = 0;
    groups.forEach((g, gi) => {
      g.y = y;
      lines.push({ y, h: SH.HEAD, g: gi, type: "head" }); y += SH.HEAD;
      if (g.open) {
        const rows = Math.ceil(g.items.length / cols);
        for (let r = 0; r < rows; r++) { lines.push({ y, h: cs + GAP, g: gi, type: "row", row: r }); y += cs + GAP; }
      } else { lines.push({ y, h: Math.max(SH.STACK, cs + 44), g: gi, type: "stack" }); y += Math.max(SH.STACK, cs + 44); }
      y += SH.PAD;
    });
    grid.style.height = y + "px";
    shelfRender();
  }

  function shelfRender() {
    const wrap = $("#out-scroll"), grid = $("#out-grid");
    if (!lines.length) return;
    const top = wrap.scrollTop - 300, bot = wrap.scrollTop + wrap.clientHeight + 300;
    let lo = 0, hi = lines.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (lines[mid].y + lines[mid].h < top) lo = mid + 1; else hi = mid; }
    const wantC = new Set(), wantH = new Set();
    for (let li = lo; li < lines.length && lines[li].y < bot; li++) {
      const L = lines[li], g = groups[L.g];
      if (L.type === "head") {
        wantH.add(L.g);
        let h = headPool.get(L.g);
        if (!h) { h = makeHead(L.g); h.style.transform = `translateY(${L.y}px)`; grid.appendChild(h); headPool.set(L.g, h); }
        h.style.transform = `translateY(${L.y}px)`;
        h.classList.toggle("open", g.open);
        h.querySelector(".oh-tg").textContent = g.open ? "收起" : "平铺";
      } else if (L.type === "stack") {
        const m = stackCount(g);
        for (let k = 0; k < m; k++) { const i = g.items[k]; wantC.add(i); place(i, stackT(k, L.y), true, k, L.g, L.y); }
      } else {
        for (let c = 0; c < cols; c++) {
          const k = L.row * cols + c;
          if (k >= g.items.length) break;
          const i = g.items[k]; wantC.add(i);
          place(i, gridT(c, L.y), false, k, L.g, g.y + SH.HEAD);
        }
      }
    }
    cellPool.forEach((el, i) => { if (!wantC.has(i)) { el.remove(); cellPool.delete(i); } });
    headPool.forEach((el, gi) => { if (!wantH.has(gi)) { el.remove(); headPool.delete(gi); } });
  }

  function place(i, tf, stacked, k, gi, stackY) {
    const grid = $("#out-grid");
    let el = cellPool.get(i);
    const animating = (animG === gi || animG === -2) && performance.now() < animUntil;
    const delay = Math.min(k, 36) * 16 + "ms";
    if (!el) {
      el = makeCell(i);
      el.style.width = el.style.height = cs + "px";
      el.classList.add("oc-plate");
      el.classList.toggle("oc-stacked", stacked);
      el.style.zIndex = stacked ? String(SH.MAX - k) : "";
      if (animating && !stacked) {
        // 新出现的格子：从档案堆的位置飞出来平铺
        el.style.transition = "none";
        el.style.transform = stackT(Math.min(k, SH.MAX - 1), stackY);
        el.style.opacity = "0";
        grid.appendChild(el); cellPool.set(i, el);
        void el.offsetWidth;
        el.style.transition = ""; el.style.transitionDelay = delay;
        el.style.transform = tf; el.style.opacity = "";
        return;
      }
      el.style.transform = tf;
      grid.appendChild(el); cellPool.set(i, el);
      return;
    }
    if (el.classList.contains("oc-stacked") !== stacked) {
      el.classList.toggle("oc-stacked", stacked);
      el.style.zIndex = stacked ? String(SH.MAX - k) : "";
      if (animating) el.style.transitionDelay = delay;
    }
    if (el.style.transform !== tf) el.style.transform = tf;
  }

  function makeHead(gi) {
    const g = groups[gi];
    const h = document.createElement("div");
    h.className = "oc-head";
    h.title = g.full;
    h.innerHTML = `<i></i><b>${App.esc(g.label)}</b><em>${g.items.length} FILES</em>` +
      `<span class="oh-path">${App.esc(g.full)}</span><span class="oh-tg"></span>`;
    h.addEventListener("click", () => toggleGroup(gi));
    return h;
  }

  function animate(gi) {
    animG = gi; animUntil = performance.now() + 1400;
    clearTimeout(delayT);
    delayT = setTimeout(() => cellPool.forEach((el) => { el.style.transitionDelay = ""; }), 1500);
    if (window.RhineSfx) window.RhineSfx.play("pull");
  }

  function toggleGroup(gi, v) {
    const g = groups[gi]; if (!g) return;
    g.open = v == null ? !g.open : v;
    openState.set(g.key, g.open);
    animate(gi);
    shelfLayout(false);
    syncShelfBtn();
  }

  function setAllOpen(v) {
    shelfAll = v;
    try { localStorage.setItem("out-shelf-all", v ? "1" : "0"); } catch (e) {}
    groups.forEach((g) => { g.open = v; openState.set(g.key, v); });
    animate(-2);
    shelfLayout(false);
    syncShelfBtn();
  }

  function syncShelfBtn() {
    let b = $("#out-shelf-all");
    const right = $(".out-bar-right");
    if (!b && right) {
      b = document.createElement("button");
      b.className = "btn btn-sm"; b.id = "out-shelf-all"; b.type = "button";
      b.addEventListener("click", () => setAllOpen(!(groups.length && groups.every((g) => g.open))));
      right.insertBefore(b, right.firstChild);
    }
    if (!b) return;
    b.hidden = !shelfOn();
    b.textContent = groups.length && groups.every((g) => g.open) ? "全部收起" : "全部平铺";
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
    const el0 = cellPool.get(i);
    if (shelfOn() && el0 && el0.classList.contains("oc-stacked")) { toggleGroup(gOf[i], true); return; }
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
    const c = $("#out-sel-cmp");
    if (c) c.disabled = selected.size < 2;
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
    if (mediaImg) {   // 点图片放大浏览；拖出去 = 复制一份原图
      mediaImg.title = App.remote ? "点击放大" : "点击放大；拖到桌面 / 文件夹 = 复制一份原图";
      mediaImg.addEventListener("click", () => lbShow(url("f", id)));
      if (!App.remote) mediaImg.addEventListener("dragstart", (e) => dragOut(e, i));
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
      (d.loras.length ? `<div class="od-sect">LoRA<button class="btn btn-xs" data-lora-all>全部复制调用</button></div><div class="chips">${d.loras.map((l) =>
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
    $$("#od-body [data-lora-all]").forEach((b) => b.addEventListener("click", () =>
      App.copy(App.loraTags(detail.loras), `已复制 ${detail.loras.length} 个 LoRA 的调用格式`)));
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
        if (r.copying) App.toast(`已收藏，正在复制一份到收藏夹的文件夹…`, "ok", 3000);
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

  /* ---------- 输出对比 ----------
   * 多选 2~4 张点「对比」。并排：每格按自己的尺寸适应窗口，缩放 / 平移是同步的（按相对位置），
   * 不同尺寸的图也对得上同一处；滑动对比：两张叠在一起，拖分割线看差别。
   * 下面是参数对照：不一样的行标出来，提示词按逗号分开，只在部分图里有的词高亮。 */
  const cmp = { items: [], mode: "side", z: 1, x: 0, y: 0, wipe: 50, a: 0, b: 1, diffOnly: false };
  const CMP_ROWS = [
    ["模型", (d) => d.model], ["LoRA", (d) => (d.loras || []).map((l) => l.name + (l.weight ? ":" + l.weight : "")).join("、")],
    ["种子", (d) => d.seed], ["采样器", (d) => d.sampler], ["调度器", (d) => d.scheduler], ["步数", (d) => d.steps],
    ["CFG", (d) => d.cfg], ["重绘幅度", (d) => d.denoise],
    ["尺寸", (d) => d.width && d.height ? `${d.width} × ${d.height}` : ""],
    ["正向提示词", (d) => d.prompt, true], ["反向提示词", (d) => d.negative, true],
    ["来源", (d) => d.source], ["来源实例", (d) => d.instance_name], ["时间", (d) => d.time_text],
  ];
  const LETTER = "ABCD";

  async function openCompare(list) {
    if (list.length < 2) { App.toast("至少选 2 张图才能对比", "error"); return; }
    if (list.length > 4) { App.toast("最多同时对比 4 张，先取前 4 张", "", 3000); list = list.slice(0, 4); }
    let r;
    try { r = await App.api.outputs_compare(list); } catch (e) { r = { ok: false, error: e.message }; }
    if (!r || r.ok === false) { App.toast((r && r.error) || "读取失败", "error"); return; }
    Object.assign(cmp, { items: r.items, z: 1, x: 0, y: 0, wipe: 50, a: 0, b: 1 });
    const opts = r.items.map((d, i) => `<option value="${i}">${LETTER[i]} · ${App.esc(d.name)}</option>`).join("");
    $("#cmp-a").innerHTML = opts; $("#cmp-b").innerHTML = opts;
    $("#cmp-a").value = "0"; $("#cmp-b").value = "1";
    $("#out-compare").hidden = false;
    setCmpMode(cmp.items.every((d) => d.kind === "image") ? cmp.mode : "side");
    renderCmpParams();
  }

  function closeCompare() {
    const box = $("#out-compare");
    if (box.hidden) return;
    box.hidden = true;
    $("#cmp-stage").innerHTML = "";
  }

  function setCmpMode(mode) {
    if (mode === "wipe" && !cmp.items.every((d) => d.kind === "image")) {
      App.toast("滑动对比只支持图片", "error"); mode = "side";
    }
    cmp.mode = mode;
    $$("#cmp-mode .chip").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    $("#cmp-pick").hidden = mode !== "wipe" || cmp.items.length < 3;
    renderCmpStage();
  }

  function mediaEl(d) {
    if (!d.exists) return Object.assign(document.createElement("div"), { className: "cmp-missing", textContent: "文件已不在了" });
    if (d.kind === "video") {
      const v = document.createElement("video");
      Object.assign(v, { src: url("f", d.id), muted: true, loop: true, autoplay: true, controls: true });
      return v;
    }
    const img = document.createElement("img");
    img.src = url("f", d.id); img.alt = ""; img.draggable = false;
    img.onload = () => cmpApply();
    return img;
  }

  function renderCmpStage() {
    const stage = $("#cmp-stage");
    stage.innerHTML = "";
    stage.className = "cmp-stage cmp-" + cmp.mode;
    if (cmp.mode === "wipe") {
      const a = cmp.items[cmp.a], b = cmp.items[cmp.b];
      const pane = document.createElement("div");
      pane.className = "cmp-pane";
      const under = mediaEl(b), over = mediaEl(a);
      under.classList.add("cmp-under"); over.classList.add("cmp-over");
      pane.append(under, over);
      pane.insertAdjacentHTML("beforeend", `<div class="cmp-line"><span>⇆</span></div>
        <span class="cmp-tag cmp-tag-l">${LETTER[cmp.a]} · ${App.esc(a.name)}</span>
        <span class="cmp-tag cmp-tag-r">${LETTER[cmp.b]} · ${App.esc(b.name)}</span>`);
      stage.appendChild(pane);
    } else {
      stage.style.setProperty("--n", cmp.items.length);
      cmp.items.forEach((d, i) => {
        const pane = document.createElement("div");
        pane.className = "cmp-pane";
        pane.appendChild(mediaEl(d));
        pane.insertAdjacentHTML("beforeend", `<span class="cmp-tag">${LETTER[i]} · ${App.esc(d.name)}</span>`);
        stage.appendChild(pane);
      });
    }
    cmpApply();
  }

  /** 按同步的 z（相对「适应窗口」的倍数）和平移，摆好每格里的图 */
  function cmpApply() {
    const panes = $$("#cmp-stage .cmp-pane");
    panes.forEach((pane) => {
      const r = pane.getBoundingClientRect();
      const line = pane.querySelector(".cmp-line");
      if (line) line.style.left = Math.max(0, Math.min(100, cmp.wipe)) + "%";
      const ref = pane.querySelector(cmp.mode === "wipe" ? "img.cmp-over" : "img");
      if (!ref || !ref.naturalWidth) return;
      const fit = Math.min(r.width / ref.naturalWidth, r.height / ref.naturalHeight);
      const w = ref.naturalWidth * fit * cmp.z, h = ref.naturalHeight * fit * cmp.z;
      // 平移按格子大小的比例存，几格大小不同也一起动
      const tx = (r.width - w) / 2 + cmp.x * r.width, ty = (r.height - h) / 2 + cmp.y * r.height;
      pane.querySelectorAll("img").forEach((img) => {
        // 每张图按自己的长宽比适应格子：滑动对比的两张图比例不同时，以前底下那张被拉成了上面那张的比例
        const nw = img.naturalWidth || ref.naturalWidth, nh = img.naturalHeight || ref.naturalHeight;
        const f = Math.min(r.width / nw, r.height / nh);
        const iw = nw * f * cmp.z, ih = nh * f * cmp.z;
        img.style.width = iw + "px"; img.style.height = ih + "px";
        img.style.transform = `translate(${(r.width - iw) / 2 + cmp.x * r.width}px, ${(r.height - ih) / 2 + cmp.y * r.height}px)`;
      });
      if (cmp.mode === "wipe") {
        // 裁切按图自己的宽度算：把分割线位置换成图内的比例
        const px = r.width * Math.max(0, Math.min(100, cmp.wipe)) / 100;
        const inImg = Math.max(0, Math.min(100, (px - tx) / w * 100));
        ref.style.clipPath = `inset(0 ${100 - inImg}% 0 0)`;
      }
    });
    const first = $("#cmp-stage img");
    const pct = first && first.naturalWidth ? Math.round(parseFloat(first.style.width) / first.naturalWidth * 100) : 0;
    $("#cmp-pct").textContent = Math.abs(cmp.z - 1) < 0.001 ? "适应" : (pct ? pct + "%" : "");
  }

  function cmpZoom(k, pane, cx, cy) {
    const r = pane.getBoundingClientRect();
    const nz = Math.min(40, Math.max(0.2, cmp.z * k));
    // 光标下那一点不动（按格子比例算，所有格子一起）
    const fx = (cx - r.left) / r.width - 0.5, fy = (cy - r.top) / r.height - 0.5;
    cmp.x = fx - (fx - cmp.x) * nz / cmp.z;
    cmp.y = fy - (fy - cmp.y) * nz / cmp.z;
    cmp.z = nz;
    cmpApply();
  }

  function cmpTokens(s) {
    return String(s || "").split(/[,，\n]/).map((t) => t.trim()).filter(Boolean);
  }

  function renderCmpParams() {
    const items = cmp.items;
    const head = `<tr><th></th>${items.map((d, i) => `<th>${LETTER[i]} · ${App.esc(d.name)}</th>`).join("")}</tr>`;
    const rows = CMP_ROWS.map(([label, get, isPrompt]) => {
      const vals = items.map((d) => String(get(d) || ""));
      if (vals.every((v) => !v)) return "";
      const differ = vals.some((v) => v !== vals[0]);
      if (cmp.diffOnly && !differ) return "";
      let cells;
      if (isPrompt && differ) {
        const sets = vals.map((v) => new Set(cmpTokens(v).map((t) => t.toLowerCase())));
        cells = vals.map((v) => cmpTokens(v).map((t) => {
          const common = sets.every((s) => s.has(t.toLowerCase()));
          return common ? App.esc(t) : `<mark>${App.esc(t)}</mark>`;
        }).join(", "));
      } else {
        cells = vals.map((v) => App.esc(v) || '<span class="cmp-none">—</span>');
      }
      return `<tr class="${differ ? "cmp-diff" : ""}${isPrompt ? " cmp-prompt" : ""}"><td>${label}</td>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`;
    }).join("");
    $("#cmp-params").innerHTML = `<table class="cmp-table">${head}${rows ||
      `<tr><td colspan="${items.length + 1}" class="cmp-none">${cmp.diffOnly ? "参数完全一样" : "这些图里没有读到生成参数"}</td></tr>`}</table>
      <div class="hint">标色的行是不一样的参数；提示词里高亮的词只在部分图里有。滚轮缩放、拖动平移，所有画面一起动；双击回到适应窗口。<br>滑动对比：没放大时按住任意处左右拖动分割线，放大后拖分割线本身（或按住 Shift 拖）。</div>`;
  }

  function bindCompare() {
    const stage = $("#cmp-stage");
    $$("#cmp-mode .chip").forEach((b) => b.addEventListener("click", () => setCmpMode(b.dataset.mode)));
    const pick = () => {
      let a = +$("#cmp-a").value, b = +$("#cmp-b").value;
      if (a === b) { b = (a + 1) % cmp.items.length; $("#cmp-b").value = String(b); }
      cmp.a = a; cmp.b = b; renderCmpStage();
    };
    $("#cmp-a").addEventListener("change", pick);
    $("#cmp-b").addEventListener("change", pick);
    $("#cmp-fit").addEventListener("click", () => { cmp.z = 1; cmp.x = cmp.y = 0; cmpApply(); });
    $("#cmp-actual").addEventListener("click", () => {
      const pane = $("#cmp-stage .cmp-pane"), img = $("#cmp-stage img");
      if (!pane || !img || !img.naturalWidth) return;
      const r = pane.getBoundingClientRect();
      cmp.z = 1 / Math.min(r.width / img.naturalWidth, r.height / img.naturalHeight);
      cmp.x = cmp.y = 0; cmpApply();
    });
    $("#cmp-diff-only").addEventListener("change", (e) => { cmp.diffOnly = e.target.checked; renderCmpParams(); });
    $("#cmp-close").addEventListener("click", closeCompare);
    stage.addEventListener("wheel", (e) => {
      const pane = e.target.closest(".cmp-pane");
      if (!pane || e.target.closest("video")) return;
      e.preventDefault();
      cmpZoom(e.deltaY < 0 ? 1.2 : 1 / 1.2, pane, e.clientX, e.clientY);
    }, { passive: false });
    let drag = null;
    stage.addEventListener("pointerdown", (e) => {
      const pane = e.target.closest(".cmp-pane");
      if (!pane || e.target.closest("video") || e.button !== 0) return;
      const r = pane.getBoundingClientRect();
      // 滑动对比：没放大时没什么可平移的，按哪儿都是拖分割线；放大后按住分割线附近（或 Shift）才拖线，别处是平移
      const unzoomed = Math.abs(cmp.z - 1) < 0.001 && !cmp.x && !cmp.y;
      const onLine = cmp.mode === "wipe" && (unzoomed || e.shiftKey ||
        Math.abs(e.clientX - (r.left + r.width * cmp.wipe / 100)) < 18);
      drag = { pane, r, x: e.clientX, y: e.clientY, ox: cmp.x, oy: cmp.y, wipe: onLine };
      stage.setPointerCapture(e.pointerId);
      stage.classList.add("panning");
      if (drag.wipe) { cmp.wipe = (e.clientX - r.left) / r.width * 100; cmpApply(); }
    });
    stage.addEventListener("pointermove", (e) => {
      if (!drag) return;
      if (drag.wipe) cmp.wipe = (e.clientX - drag.r.left) / drag.r.width * 100;
      else { cmp.x = drag.ox + (e.clientX - drag.x) / drag.r.width; cmp.y = drag.oy + (e.clientY - drag.y) / drag.r.height; }
      cmpApply();
    });
    const end = () => { drag = null; stage.classList.remove("panning"); };
    stage.addEventListener("pointerup", end);
    stage.addEventListener("pointercancel", end);
    stage.addEventListener("dblclick", () => { cmp.z = 1; cmp.x = cmp.y = 0; cmpApply(); });
    window.addEventListener("resize", () => { if (!$("#out-compare").hidden) cmpApply(); });
    document.addEventListener("keydown", (e) => {
      if ($("#out-compare").hidden) return;
      if (e.key === "Escape") { e.stopPropagation(); closeCompare(); }
    }, true);
  }

  /* ---------- 复制 / 拖出去 ----------
   * 复制：后端把文件放进系统剪贴板（文件列表 + 单张时再放一份位图），资源管理器、QQ / 微信、PS 里都能直接粘贴。
   * 拖出去：启动器窗口里交给后端发起 Windows 原生拖动（app/native_drag.py），拖到桌面 / 文件夹就是复制一份原图；
   * 在普通浏览器里（预览）退回 DownloadURL，Chrome 认这个。 */
  async function copyIds(list) {
    if (!list.length) { App.toast("先选中要复制的图", "error"); return; }
    if (App.remote) { App.toast("正在控制远程主机：图在主机上，先「下载到本机」再复制", "error", 5000); return; }
    let r;
    try { r = await App.api.outputs_copy(list); } catch (e) { r = { ok: false, error: e.message }; }
    App.toast(r && r.ok ? r.text + "，到文件夹或聊天软件里 Ctrl+V 粘贴" : (r && r.error) || "复制失败", r && r.ok ? "ok" : "error", 5000);
  }

  // 启动器窗口（WebView2）里浏览器自己的拖出会变成一次被拦掉的「下载」：改由后端发起 Windows 原生拖动
  const nativeDrag = () => !!(window.pywebview && !App.remote && !/[?&]mock=1\b/.test(location.search));

  function dragOut(e, i) {
    const id = ids[i];
    if (id == null || missing.has(id) || !e.dataTransfer) return;
    if (nativeDrag()) {
      e.preventDefault();
      // 多选时拖的是一张选中的图：把选中的全部一起拖出去
      const list = selectMode && selected.has(id) ? Array.from(selected) : [id];
      App.api.outputs_drag(list).then((r) => {
        if (!r || !r.ok) App.toast((r && r.error) || "拖出失败，请用「复制图片」再粘贴", "error", 5000);
      }, (err) => App.toast("拖出失败：" + err.message, "error", 5000));
      return;
    }
    const name = names[i] || (detail && detail.id === id && detail.name) || `output_${id}`;
    const ext = (name.match(/\.(\w+)$/) || [, "png"])[1].toLowerCase();
    const mime = { jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp", gif: "image/gif", mp4: "video/mp4",
      webm: "video/webm" }[ext] || "image/png";
    const src = url("f", id);
    e.dataTransfer.effectAllowed = "copy";
    e.dataTransfer.setData("DownloadURL", `${mime}:${name.includes(".") ? name : name + "." + ext}:${src}`);
    e.dataTransfer.setData("text/uri-list", src);
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
      // 一帧最多重排一次（滚动事件一帧里可能来好几个）
      let rvQueued = false;
      $("#out-scroll").addEventListener("scroll", () => {
        if (rvQueued) return;
        rvQueued = true;
        requestAnimationFrame(() => { rvQueued = false; renderVisible(); });
      }, { passive: true });
      window.addEventListener("resize", () => { if (visible) layout(false); });
      window.addEventListener("wwy-theme", () => setTimeout(() => { syncShelfBtn(); if (visible) layout(true); }, 0));

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
      $("#out-sel-cmp").addEventListener("click", () => openCompare(Array.from(selected)));
      $("#out-sel-copy").addEventListener("click", () => copyIds(Array.from(selected)));
      $("#od-copy").addEventListener("click", () => detail && copyIds([detail.id]));
      // Ctrl+C：详情开着复制这张，多选时复制选中的（输入框里、选中了文字时照常复制文字）
      document.addEventListener("keydown", (e) => {
        if (App.currentPage !== "outputs" || !(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== "c") return;
        if (e.target && /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
        if (String(window.getSelection() || "").trim()) return;
        if (!$("#out-compare").hidden || !$("#modal-mask").hidden) return;
        const list = !$("#out-detail").hidden && detail ? [detail.id] : (selectMode ? Array.from(selected) : []);
        if (!list.length) return;
        e.preventDefault();
        copyIds(list);
      });
      bindCompare();
      $("#out-sel-add").addEventListener("click", async () => {
        let cid = Number($("#out-sel-col").value);
        if (!cid) { cid = await collectionOp("new"); if (!cid) return; }
        if (!selected.size) { App.toast("先点选要收藏的图", "error"); return; }
        const r = await App.api.outputs_collection_op("set", cid, "", Array.from(selected), true);
        if (r && r.ok !== false) {
          collections = r.collections; renderCollections();
          copyToast(r, selected.size);
          setSelectMode(false);
        }
      });

      $("#od-close").addEventListener("click", closeDetail);
      $("#od-prev").addEventListener("click", () => { if (detailIdx > 0) openDetail(detailIdx - 1); });
      $("#od-next").addEventListener("click", () => { if (detailIdx < ids.length - 1) openDetail(detailIdx + 1); });
      $("#od-open").addEventListener("click", () => detail && App.api.outputs_open(detail.id));
      $("#od-reveal").addEventListener("click", () => detail && App.api.outputs_reveal(detail.id));
      // 联动：控制远程主机时把主机的出图下载到这台电脑
      const downloadIds = async (list) => {
        if (!list.length) { App.toast("先点选要下载的图", "error"); return; }
        const r = await App.api.link_download_outputs(list);
        if (r && r.ok === false && !r.cancelled) App.toast(r.error || "下载失败", "error", 6000);
      };
      $("#od-dl").addEventListener("click", () => detail && downloadIds([detail.id]));
      $("#out-sel-dl").addEventListener("click", () => downloadIds(Array.from(selected)));
      $("#od-del").addEventListener("click", () => detail && deleteIds([detail.id]));
      $("#od-send-webui").addEventListener("click", (e) =>
        detail && App.sendTo("webui", { from: "output", id: detail.id }, e.currentTarget));
      $("#od-send-comfy").addEventListener("click", (e) =>
        detail && App.sendTo("comfy", { from: "output", id: detail.id }, e.currentTarget));
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
        if (e.target.closest("#out-detail, #lightbox, #out-compare, #out-selbar, #modal-mask, .oc")) return;
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
        App.toast(e.error ? `复制失败：${e.error}` : `复制完成：${e.copied} 个文件到 ${e.dest}` +
          (e.skipped ? `，${e.skipped} 个已经在那里了（跳过）` : "") + (e.failed ? `，${e.failed} 个失败` : ""),
        e.failed || e.error ? "error" : "ok", 6000));
    },

    async onShow() {
      visible = true;
      if (!(await ensureInit())) return;
      layout(false);
      scheduleQuery(0);
      scan();
      autoScanTick();
    },

    onHide() { visible = false; clearTimeout(autoTimer); closeDetail(); closeCompare(); },
  };

})();
