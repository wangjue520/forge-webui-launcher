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

  function setDownloading(v) {
    downloading = v;
    $("#cv-fetch").disabled = v;
    updateDownloadBtn();
  }
  function updateDownloadBtn() {
    // 不再要求先点「获取信息」：直接点下载会自动先获取
    $("#cv-download").disabled = downloading;
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
    const isLb = info.source === "liblib";
    $("#cv-info-card").hidden = false;
    const rows = [
      ["来源", isLb ? "liblib（哩布哩布）" : "Civitai"],
      ["模型", info.model_name],
      ["类型", info.model_type + (folder ? `（建议放入 ${folder}）` : "")],
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
        `　<span style="color:var(--text-faint)">${App.fmtBytes((f.sizeKB || 0) * 1024)}</span></span>`;
      label.addEventListener("click", () => {
        selectedFile = i;
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

    if (dest) $("#cv-dest").value = dest;
    updateDownloadBtn();
  }

  App.pages.civitai = {
    init(state) {
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
        if (r && r.ok && r.path) $("#cv-dest").value = r.path;
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
        } else if (e.ok) {
          App.toast("下载完成：" + e.path, "ok", 5000);
        } else {
          App.toast(e.error || "下载失败", "error", 5000);
        }
      });
    },
  };
})();
