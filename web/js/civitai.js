/* civitai.js — 模型下载页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let log = null;
  let prog = null;
  let currentInfo = null;   // civitai/info 事件里的 subset
  let selectedFile = 0;
  let downloading = false;

  let pendingFetch = null;    // 自动获取信息的 Promise 回调

  // 保存位置是不是用户自己改过：没改过就跟着选中的文件自动换（主模型 / 文本编码器 / VAE 各放各的）
  let destManual = false;

  function setDownloading(v) {
    downloading = v;
    $("#cv-fetch").disabled = v;
    updateDownloadBtn();
  }
  function updateDownloadBtn() {
    // 不再要求先点「获取信息」：直接点下载会自动先获取
    $("#cv-download").disabled = downloading;
    $("#cv-download-all").disabled = downloading;
  }

  function fetchInfo(text) {
    return new Promise((resolve) => {
      pendingFetch = resolve;
      App.api.civitai_fetch(text).then((r) => {
        if (r && r.ok === false) { pendingFetch = null; resolve(null); }
      }).catch(() => { pendingFetch = null; resolve(null); });
    });
  }

  function renderInfo(info, folder, dest) {
    currentInfo = info;
    destManual = false;
    const isLb = info.source === "liblib";
    const files = info.files || [];
    const multiRole = !isLb && new Set(files.map((f) => f.folder)).size > 1;
    $("#cv-info-card").hidden = false;
    const rows = [
      ["来源", isLb ? "liblib（哩布哩布）" : "Civitai"],
      ["模型", info.model_name],
      ["类型", info.model_type + (multiRole ? "（这个版本打包了多个文件，每个文件会按自己的类型放进对应文件夹）"
        : folder ? `（建议放入 ${folder}）` : "")],
      ["版本", info.version_name + (isLb && (info.files || []).length > 1 ? "（下方可切换其他版本）" : "")],
      ["基础模型", info.base_model || "未知"],
    ];
    if (isLb) {
      if (info.trigger_words && info.trigger_words.length)
        rows.push(["触发词", info.trigger_words.join("，")]);
      if (info.vip_used)
        rows.push(["注意", "会员专属模型：即使填了 usertoken 也可能因账号权限被拒绝下载"]);
      else if (info.exclusive)
        rows.push(["注意", "独家模型：可能只允许在线运行、不提供文件下载"]);
    }
    $("#cv-info").innerHTML = rows
      .map(([k, v]) => `<span class="k">${App.esc(k)}</span><span class="v">${App.esc(v)}</span>`)
      .join("");

    const box = $("#cv-files");
    box.innerHTML = "";
    (info.files || []).forEach((f, i) => {
      const label = document.createElement("label");
      label.className = "radio-row" + (i === 0 ? " checked" : "");
      // liblib：一个版本一个文件，选项实质是在「选版本」
      const title = isLb && f.version_name ? `${f.version_name} · ${f.name}` : f.name;
      label.innerHTML =
        `<input type="radio" name="cv-file" value="${i}"><span class="radio-dot"></span>` +
        `<span>${App.esc(title)}${f.primary ? '<span class="primary-tag">' + (isLb ? "当前版本" : "主文件") + '</span>' : ""}` +
        (f.unavailable ? '<span style="color:var(--accent)">　无直接下载地址</span>' : "") +
        `　<span style="color:var(--text-faint)">${App.fmtBytes((f.sizeKB || 0) * 1024)}</span>` +
        (!isLb && f.folder ? `<span class="cv-role">→ ${App.esc(f.role_label || "")} · ${App.esc(f.folder)}</span>` : "") +
        `</span>`;
      label.addEventListener("click", () => {
        selectedFile = i;
        if (!destManual && f.dest) $("#cv-dest").value = f.dest;
        box.querySelectorAll(".radio-row").forEach((r) => r.classList.remove("checked"));
        label.classList.add("checked");
        label.querySelector("input").checked = true;
      });
      box.appendChild(label);
    });
    // 默认选中主文件/当前版本，否则第一个
    const primary = (info.files || []).findIndex((f) => f.primary);
    selectedFile = primary >= 0 ? primary : 0;
    box.children[selectedFile] && box.children[selectedFile].click();

    // 信息卡底部：在浏览器打开模型页面（下载被拒时的手动下载入口）
    if (info.page_url) {
      const btn = document.createElement("button");
      btn.className = "btn";
      btn.style.marginTop = "10px";
      btn.textContent = isLb ? "在浏览器打开 liblib 模型页面（可手动下载）" : "在浏览器打开 Civitai 模型页面";
      btn.addEventListener("click", () => App.api.open_url(info.page_url));
      box.appendChild(btn);
    }

    if (dest && !(files[selectedFile] && files[selectedFile].dest)) $("#cv-dest").value = dest;
    $("#cv-download-all").hidden = isLb || files.length < 2;
    updateDownloadBtn();
  }

  /* ---------- H3 视频模型一键下载（当前实例是 Forge Neo H3 才显示） ---------- */
  const h3 = { info: null, running: false, log: null, prog: null, states: {}, inited: false };
  const GB = (n) => (n / 1073741824).toFixed(n >= 10 * 1073741824 ? 1 : 2) + " GB";
  const H3_STATE_TEXT = { ok: "已下载", part: "下了一部分，会续传", diff: "已有同名文件但大小不对，会重新下载",
    downloading: "下载中…", error: "失败" };

  function h3File(item) {
    const sel = document.querySelector(`#h3-list select[data-h3="${item.id}"]`);
    const q = sel ? sel.value : null;
    return item.files.find((f) => f.q === q) || item.files[0];
  }
  function h3Checked(id) {
    const el = document.querySelector(`#h3-list input[data-h3="${id}"]`);
    return !!(el && el.checked);
  }
  function h3RenderRow(item) {
    const f = h3File(item);
    const row = document.querySelector(`#h3-list .h3-row[data-id="${item.id}"]`);
    if (!row) return;
    const st = h3.states[item.id] || f.state;
    const sEl = row.querySelector(".h3-state");
    sEl.dataset.state = st || "";
    sEl.textContent = H3_STATE_TEXT[st] || "";
    if (st === "error" && h3.states[item.id + ":err"]) sEl.textContent = "失败：" + h3.states[item.id + ":err"];
    row.querySelector(".h3-size").textContent = GB(f.size);
    row.querySelector(".h3-name").textContent = f.name;
  }
  function h3Total() {
    if (!h3.info) return;
    let need = 0, n = 0;
    h3.info.items.forEach((it) => {
      if (!h3Checked(it.id)) return;
      const f = h3File(it);
      if (f.state === "ok") return;
      n++; need += Math.max(0, f.size - (f.part || 0));
    });
    const free = h3.info.disk_free;
    let t = n ? `还要下载 ${n} 个文件，共 ${GB(need)}` : "选中的模型都已经下好了";
    if (n && free != null) t += `　·　模型盘剩余 ${GB(free)}`;
    const el = $("#h3-total");
    el.textContent = t;
    el.dataset.status = n && free != null && free < need + 2 * 1073741824 ? "bad" : "";
    if (!h3.running) $("#h3-start").disabled = !n;
  }
  function h3Hw(info) {
    const el = $("#h3-hw");
    const parts = [];
    let status = "";
    if (info.vram_gb != null) {
      parts.push(`显卡 ${info.vram_gb} GB 显存`);
      if (info.vram_gb < info.min_vram_gb - 0.5) status = "bad";
    } else parts.push("没检测到 NVIDIA 显卡");
    if (info.ram_gb != null) parts.push(`内存 ${Math.round(info.ram_gb)} GB`);
    let tip = "";
    if (info.vram_gb != null && info.vram_gb < info.min_vram_gb - 0.5) {
      tip = `H3 至少要 ${info.min_vram_gb}GB 显存，这块卡大概率跑不动。`;
    } else if (info.ram_gb != null && info.ram_gb < 40) {
      tip = `Q4 一套约占 ${info.ram_q4_gb}GB 内存，你的内存偏紧，已默认选 Q2 档（画质稍差但能跑）。`;
      status = status || "warn";
    } else if (info.ram_gb != null && info.ram_gb < 60) {
      tip = "内存够跑 Q4，生成时尽量关掉其他占内存的程序。";
    }
    el.textContent = parts.join(" · ") + (tip ? "。" + tip : "");
    el.dataset.status = status;
  }
  function h3Render(info) {
    h3.info = info;
    const defaults = info.quant_defaults || {};
    $("#h3-list").innerHTML = info.items.map((it) => {
      const hasQ = it.files.length > 1;
      const have = it.files.find((f) => f.state === "ok");
      // 默认档位：已经下过哪一档就选哪一档，否则按内存给的建议
      const q = have && have.q ? have.q : (defaults[it.id] || it.quant_default);
      const sel = hasQ ? `<select data-h3="${it.id}">` + it.files.map((f) =>
        `<option value="${App.esc(f.q)}"${f.q === q ? " selected" : ""}>${App.esc(f.q)}　${GB(f.size)}` +
        `${f.note ? "　" + App.esc(f.note) : ""}${f.state === "ok" ? "　✓" : ""}</option>`).join("") + `</select>` : "<span></span>";
      const checked = it.required || it.default || !!have;
      return `<div class="h3-row" data-id="${it.id}">` +
        `<label class="chk-row${it.required ? " locked" : ""}"><input type="checkbox" data-h3="${it.id}"` +
        `${checked ? " checked" : ""}${it.required ? " disabled" : ""}><i></i>` +
        `<span><span class="ext-name">${App.esc(it.label)}</span>${it.required ? '<em class="tag">必需</em>' : ""}` +
        `<div class="ext-desc">${App.esc(it.desc)}</div></span></label>` +
        sel + `<span class="h3-size"></span>` +
        `<div class="h3-sub"><span class="h3-name"></span><span>→ ${App.esc(it.where)}</span><span class="h3-state"></span></div></div>`;
    }).join("");
    info.items.forEach(h3RenderRow);
    h3Hw(info);
    h3Total();
  }
  async function h3Refresh() {
    if (!App.api.h3_models_info || h3.running) return;
    let r;
    try { r = await App.api.h3_models_info(); } catch (e) { return; }
    const card = $("#h3-card");
    card.hidden = !(r && r.ok && r.is_h3);
    if (card.hidden) return;
    h3.states = {};
    h3Render(r);
    if (r.running) h3SetRunning(true);
  }
  function h3SetRunning(v) {
    h3.running = v;
    $("#h3-start").disabled = v;
    $("#h3-cancel").disabled = !v;
    document.querySelectorAll("#h3-list input, #h3-list select").forEach((el) => {
      if (el.type === "checkbox" && el.closest(".locked")) return;
      el.disabled = v;
    });
    if (!v) h3Total();
  }
  async function h3Start() {
    if (h3.running || !h3.info) return;
    const selection = h3.info.items.filter((it) => h3Checked(it.id))
      .map((it) => ({ id: it.id, q: h3File(it).q }));
    $("#h3-log-wrap").hidden = false;
    h3SetRunning(true);
    try {
      const r = await App.api.h3_models_download(selection);
      if (r && r.ok === false) { App.toast(r.error || "无法开始下载", "error", 5000); h3SetRunning(false); }
    } catch (e) { App.toast("无法开始下载：" + e.message, "error"); h3SetRunning(false); }
  }
  function h3Init() {
    if (h3.inited) return;
    h3.inited = true;
    h3.log = App.makeLogger($("#h3-log"), 300);
    h3.prog = App.progress($("#h3-progress"));
    h3.prog.hide();
    $("#h3-list").addEventListener("change", () => {
      if (h3.info) h3.info.items.forEach(h3RenderRow);
      h3Total();
    });
    $("#h3-start").addEventListener("click", h3Start);
    $("#h3-cancel").addEventListener("click", () => App.api.h3_models_cancel());
    $("#h3-log-clear").addEventListener("click", () => { $("#h3-log").textContent = ""; });
    App.on("h3dl", "log", (e) => h3.log(e.text));
    App.on("h3dl", "item", (e) => {
      h3.states[e.id] = e.state;
      if (e.error) h3.states[e.id + ":err"] = e.error;
      if (e.state === "ok" && h3.info) {
        const it = h3.info.items.find((x) => x.id === e.id);
        if (it) { const f = h3File(it); f.state = "ok"; f.part = 0; }
      }
      const it = h3.info && h3.info.items.find((x) => x.id === e.id);
      if (it) h3RenderRow(it);
    });
    App.on("h3dl", "progress", (e) => {
      const all = e.all_total > 0 ? e.all_done / e.all_total * 100 : 0;
      const cur = e.total > 0 ? e.downloaded / e.total * 100 : 0;
      h3.prog.set(all, `总进度 ${all.toFixed(1)}%　·　(${e.index}/${e.count}) ${e.name}　${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}（${cur.toFixed(1)}%）`);
    });
    App.on("h3dl", "done", (e) => {
      h3SetRunning(false);
      h3.prog.hide();
      if (e.ok) {
        App.toast(e.downloaded ? `H3 模型已下载 ${e.downloaded} 个` + (e.skipped ? `（另有 ${e.skipped} 个已存在跳过）` : "") +
          "。启动 WebUI 后顶部 UI Preset 选 h3 即可" : "选中的 H3 模型都已经在了", "ok", 7000);
      } else if (e.cancelled) {
        App.toast("已取消，已下载的部分保留，下次点下载会接着下", "ok", 5000);
      } else {
        App.toast(e.error || "下载失败", "error", 7000);
      }
      h3Refresh();
    });
  }

  App.pages.civitai = {
    onShow() { h3Refresh(); },
    init(state) {
      h3Init();
      h3Refresh();
      log = App.makeLogger($("#cv-log"), 400);
      prog = App.progress($("#cv-progress"));
      prog.hide();

      $("#cv-fetch").addEventListener("click", async () => {
        const text = $("#cv-url").value.trim();
        if (!text) { App.toast("请先粘贴 Civitai / liblib 链接或 ID", "error"); return; }
        try {
          const r = await App.api.civitai_fetch(text);
          if (r && r.ok === false) App.toast(r.error || "获取失败", "error");
        } catch (e) { App.toast("获取失败：" + e.message, "error"); }
      });
      $("#cv-url").addEventListener("keydown", (e) => {
        if (e.key === "Enter") $("#cv-fetch").click();
      });

      $("#cv-open-key").addEventListener("click", () => App.api.open_url("https://civitai.com/user/account"));

      // liblib usertoken：验证有效性后再保存（有效会回显昵称）
      $("#lb-verify").addEventListener("click", async () => {
        const token = $("#lb-token").value.trim();
        if (!token) {
          const r = await App.api.settings_verify_liblib_token("");
          if (r && r.ok) App.toast("已清除 liblib usertoken", "ok");
          return;
        }
        App.toast("正在验证 usertoken…");
        try {
          const r = await App.api.settings_verify_liblib_token(token);
          if (r && r.ok) {
            App.cfg.liblib_token = token;
            App.toast(`usertoken 有效，已保存（当前账号：${r.nickname || "已登录"}）`, "ok", 6000);
          } else {
            App.toast((r && r.error) || "usertoken 无效", "error", 6000);
          }
        } catch (e) { App.toast("验证失败：" + e.message, "error"); }
      });
      $("#lb-open").addEventListener("click", () => App.api.open_url("https://www.liblib.art/"));

      $("#cv-browse").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择保存位置", $("#cv-dest").value || App.cfg.webui_root || "");
        if (r && r.ok && r.path) { $("#cv-dest").value = r.path; destManual = true; }
      });
      $("#cv-dest").addEventListener("input", () => { destManual = true; });

      $("#cv-download-all").addEventListener("click", async () => {
        const files = (currentInfo && currentInfo.files) || [];
        const ok = await App.confirm("全部下载",
          `将依次下载这个版本的 ${files.length} 个文件，每个文件放进它自己类型对应的文件夹：\n\n` +
          files.map((f) => `· ${f.name}\n   → ${f.dest || "（无法自动确定，跳过）"}`).join("\n") +
          "\n\n下载完还会按文件内容核对一遍类型，网站标错了会自动挪到正确的文件夹。", "开始下载");
        if (!ok) return;
        setDownloading(true);
        try {
          const r = await App.api.civitai_download_all();
          if (r && r.ok === false) { App.toast(r.error || "下载失败", "error"); setDownloading(false); }
        } catch (e) { App.toast("下载失败：" + e.message, "error"); setDownloading(false); }
      });

      $("#cv-download").addEventListener("click", async () => {
        const dest = $("#cv-dest").value.trim();
        // 没点过「获取信息」就直接下载：自动先获取（按链接类型推断保存位置）
        if (!currentInfo) {
          const text = $("#cv-url").value.trim();
          if (!text) { App.toast("请先粘贴 Civitai / liblib 链接或 ID", "error"); return; }
          setDownloading(true);
          App.toast("正在自动获取模型信息…");
          const e = await fetchInfo(text);
          if (!e) { setDownloading(false); App.toast("获取模型信息失败，无法下载", "error", 5000); return; }
          // info 事件已渲染并填充了默认保存位置
          if (!$("#cv-dest").value.trim()) { setDownloading(false); App.toast("请先选择保存位置", "error"); return; }
        }
        if (!dest && !$("#cv-dest").value.trim()) { App.toast("请先选择保存位置", "error"); return; }
        setDownloading(true);
        try {
          const r = await App.api.civitai_download(selectedFile, $("#cv-dest").value.trim());
          if (r && r.ok === false) { App.toast(r.error || "下载失败", "error"); setDownloading(false); }
        } catch (e) { App.toast("下载失败：" + e.message, "error"); setDownloading(false); }
      });

      $("#cv-log-clear").addEventListener("click", () => { $("#cv-log").textContent = ""; });

      App.on("civitai", "log", (e) => log(e.text));
      App.on("civitai", "info", (e) => {
        if (pendingFetch) { const r = pendingFetch; pendingFetch = null; r(e.ok ? e : null); }
        if (!e.ok) { App.toast(e.error || "获取信息失败", "error", 5000); return; }
        renderInfo(e.info, e.folder, e.dest);
        App.toast(`已获取：${e.info.model_name} / ${e.info.version_name}`, "ok");
      });
      App.on("civitai", "progress", (e) => {
        const pct = e.total > 0 ? (e.downloaded / e.total * 100) : 0;
        prog.set(pct, `${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}（${pct.toFixed(1)}%）`);
      });
      App.on("civitai", "done", (e) => {
        setDownloading(false);
        prog.hide();
        if (e.ok && e.hash_ok === false) {
          // 校验失败：文件已被改名成 .broken，不会被当成正常模型加载
          App.toast("下载完成但哈希校验未通过！文件已另存为 .broken，请删除后重新下载：" + e.path, "error", 9000);
        } else if (e.ok && e.count) {
          App.toast(`全部下载完成：${e.count} 个文件，已各自放进对应文件夹`, "ok", 6000);
        } else if (e.ok) {
          App.toast("下载完成：" + e.path, "ok", 5000);
        } else {
          App.toast(e.error || "下载失败", "error", 5000);
        }
      });
    },
  };
})();
