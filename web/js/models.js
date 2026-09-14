/* models.js — 模型管理页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let categories = [];     // [{label, is_lora}]
  let curCat = -1;
  let curIsLora = false;
  let files = [];          // 当前类别全部文件
  let selected = null;     // 当前选中行数据
  let sortKey = "rel";
  let sortAsc = true;
  let batchRunning = false;

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
      </tr>`).join("");
    $("#md-count").textContent = `共 ${files.length} 个文件` +
      (rows.length !== files.length ? `，筛选后 ${rows.length} 个` : "");
    tbody().querySelectorAll("tr").forEach((tr) =>
      tr.addEventListener("click", () => selectRow(tr.dataset.path, tr)));
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

  async function loadCat(idx) {
    curCat = idx;
    $$("#md-cats .cat-item").forEach((el, i) => el.classList.toggle("active", i === idx));
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
      renderBaseOptions();
      renderTable();
      $("#md-root").textContent = r.no_info > 0
        ? `其中 ${r.no_info} 个没有 Civitai 信息` : "";
    } catch (e) {
      $("#md-count").textContent = "读取失败：" + e.message;
    }
  }

  function setBatchRunning(v) {
    batchRunning = v;
    $("#md-batch").disabled = v;
    $("#md-batch-cancel").disabled = !v;
  }

  App.pages.models = {
    init() {
      const reloadAll = () => {
        // 刷新分类列表（根目录可能刚设置/更换过），再刷新当前类别
        App.api.models_categories().then((r) => {
          categories = (r && r.categories) || [];
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
      $("#md-filter").addEventListener("input", renderTable);
      $("#md-base").addEventListener("change", renderTable);

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

    onShow() {
      // 切回本页时刷新分类（根目录可能刚在别的页面改过）
      if (App.pages.models.reloadAll) App.pages.models.reloadAll();
    },
  };
})();
