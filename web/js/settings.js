/* settings.js — 高级选项页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let schema = null;
  let building = false; // 重建下拉期间不触发 change 保存

  function isNeo2() { return App.cfg.webui_branch === "neo2"; }
  function isNeo() { return App.cfg.webui_branch === "neo" || App.cfg.webui_branch === "neo2"; }

  function fillSelect(el, items, cfgKey) {
    el.innerHTML = "";
    (items || []).forEach((it) => {
      const o = document.createElement("option");
      o.value = it.key;
      o.textContent = it.label;
      el.appendChild(o);
    });
    const v = App.cfg[cfgKey];
    el.value = v != null ? String(v) : (items[0] ? items[0].key : "");
    if (el.selectedIndex < 0 && items.length) el.selectedIndex = 0;
  }

  function rebuildBranchDependent() {
    building = true;
    try {
      const neo2 = isNeo2();
      fillSelect($("#s-vram"), neo2 ? schema.vram_neo2 : schema.vram_legacy, "vram_mode");
      fillSelect($("#s-precision"), neo2 ? schema.precision_neo2 : schema.precision_legacy, "precision_mode");
      if (neo2) {
        fillSelect($("#s-unet"), schema.unet_neo2, "unet_precision");
        fillSelect($("#s-vae"), schema.vae_neo2, "vae_precision");
        fillSelect($("#s-textenc"), schema.text_enc_neo2, "text_enc_precision");
        fillSelect($("#s-attention"), schema.attention_neo2, "attention_impl");
      }
      // 分支显隐
      $$("[data-branch-show]").forEach((el) => {
        el.style.display = el.dataset.branchShow.split(",").includes(App.cfg.webui_branch) ? "" : "none";
      });
      $$("[data-branch-hide]").forEach((el) => {
        el.style.display = el.dataset.branchHide.split(",").includes(App.cfg.webui_branch) ? "none" : "";
      });
      $$("[data-branch-neo]").forEach((el) => {
        el.style.display = isNeo() ? "" : "none";
      });
    } finally {
      building = false;
    }
  }

  async function redetectMirror() {
    const el = $("#s-mirror-status");
    el.dataset.status = "";
    el.textContent = "正在检测网络环境…";
    try { await App.api.redetect_network(); } // 结果走 settings/mirror_status 事件
    catch (e) { el.textContent = "检测失败：" + e.message; el.dataset.status = "bad"; }
  }

  /* ---------- 启动器自更新 ---------- */
  const Updater = {
    lastInfo: null,   // 最近一次检查结果（update_info 事件）
    busy: false,

    setStatus(text, bad) {
      const el = $("#up-status");
      el.textContent = text || "";
      el.style.color = bad ? "var(--danger, #e26060)" : "";
    },

    fillCurrent(ver, commit) {
      $("#up-current").textContent = "V" + (ver || "未知") + (commit ? "（" + commit + "）" : "");
      const side = $("#app-version");
      if (side && ver) side.textContent = "V" + ver;
    },

    renderInfo(e) {
      this.lastInfo = e;
      if (!e.ok) {
        $("#up-remote").textContent = "检查失败";
        this.setStatus(e.error || "检查更新失败", true);
        $("#up-do").disabled = true;
        return;
      }
      this.fillCurrent(e.current.version, e.current.commit);
      const r = e.remote || {};
      $("#up-remote").textContent =
        "V" + (r.version || "?") + (r.date ? " · " + r.date : "") + (r.message ? " · " + r.message : "");
      $("#up-do").disabled = !e.has_update;
      this.setStatus(e.has_update ? "发现新版本，点「立即更新」一键升级（配置和已下载的环境都会保留）"
                                  : "已经是最新版本");
    },

    async check(silent) {
      if (!silent) { this.setStatus("正在检查更新…"); $("#up-remote").textContent = "检查中…"; }
      try { await App.api.launcher_check_update(); } // 结果走 launcher/update_info 事件
      catch (e) { this.setStatus("检查失败：" + e.message, true); }
    },

    async run() {
      if (this.busy) return;
      this.busy = true;
      $("#up-do").disabled = true;
      $("#up-check").disabled = true;
      $("#up-restart").hidden = true;
      const prog = App.progress($("#up-progress"));
      prog.hide();
      this.setStatus("正在更新…");
      try { await App.api.launcher_update(); } // 进度/结果走 launcher/* 事件
      catch (e) {
        this.setStatus("更新失败：" + e.message, true);
        this.busy = false;
        $("#up-do").disabled = false;
        $("#up-check").disabled = false;
      }
    },

    async restart() {
      if (App.state && App.state.launch && App.state.launch.running) {
        App.toast("WebUI 还在运行，请先停止再重启启动器", "error");
        return;
      }
      const yes = await App.confirm("重启启动器", "更新已下载完成，现在重启启动器让新版本生效？");
      if (!yes) return;
      try {
        const r = await App.api.launcher_restart();
        if (r && r.ok === false) App.toast(r.error || "重启失败", "error");
      } catch (e) { App.toast("重启失败：" + e.message, "error"); }
    },

    init(state) {
      const lv = state.launcher || {};
      this.fillCurrent(lv.version, lv.commit);

      $("#up-check").addEventListener("click", () => this.check(false));
      $("#up-do").addEventListener("click", () => this.run());
      $("#up-restart").addEventListener("click", () => this.restart());

      App.on("launcher", "update_info", (e) => {
        this.renderInfo(e);
        // 启动时的静默检查：有更新就提示一次，把用户引到这个卡片
        if (e.ok && e.has_update && !this.busy) {
          App.toast("发现启动器新版本 V" + e.remote.version + "，可在 高级选项 → 启动器更新 一键升级", "ok", 6000);
        }
      });
      App.on("launcher", "log", (e) => this.setStatus(e.text || ""));
      App.on("launcher", "update_progress", (e) => {
        App.progress($("#up-progress")).set(e.pct, e.label || "");
      });
      App.on("launcher", "update_done", (e) => {
        this.busy = false;
        $("#up-check").disabled = false;
        App.progress($("#up-progress")).hide();
        if (!e.ok) {
          this.setStatus("更新失败：" + (e.error || "未知错误"), true);
          $("#up-do").disabled = false;
          return;
        }
        this.fillCurrent(e.version, e.commit);
        $("#up-remote").textContent = "V" + e.version + "（当前）";
        this.setStatus("更新完成！新版本 V" + e.version + " 已就绪，重启启动器后生效。");
        $("#up-restart").hidden = false;
        App.toast("更新完成，重启后生效", "ok");
      });

      // 启动后自动静默检查一次（结果只填卡片 + toast，不打扰）
      this.check(true);
    },
  };

  App.pages.settings = {
    init(state) {
      schema = state.settings_schema || {};

      // 分支下拉
      const branchSel = $("#s-branch");
      (state.settings_branches || []).forEach((b) => {
        const o = document.createElement("option");
        o.value = b.key;
        o.textContent = b.label;
        branchSel.appendChild(o);
      });
      branchSel.value = App.cfg.webui_branch || "neo2";

      rebuildBranchDependent();

      // 分支切换后重建相关下拉并显隐
      branchSel.addEventListener("change", () => rebuildBranchDependent());

      // 重建期间不保存配置：屏蔽 data-cfg 的 change 里因程序性赋值引发的误触发
      $$("#page-settings [data-cfg]").forEach((el) => {
        el.addEventListener("change", () => {
          if (building) return;
          // 分支变化会影响参数生成，顺手刷新预览（core 里已处理保存）
          if (el.id === "s-branch") rebuildBranchDependent();
        });
      });

      const ms = state.mirror_status;
      if (ms) $("#s-mirror-status").textContent = (typeof ms === "string") ? ms : (ms.text || "");
      $("#s-mirror-redetect").addEventListener("click", redetectMirror);

      App.on("settings", "mirror_status", (e) => {
        const el = $("#s-mirror-status");
        el.textContent = e.text || "";
        el.dataset.status = e.ok ? "ok" : "";
      });

      Updater.init(state);
    },
  };
})();
