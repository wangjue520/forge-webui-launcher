/* settings.js — 「高级选项」页（按实例分别设置）+「启动器设置」页（全局） */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  let schema = null;
  let building = false; // 重建下拉期间不触发 change 保存

  /* 高级选项页正在编辑哪个实例（可以不是「当前实例」） */
  let editIid = "";
  let editCfg = {};       // 正在编辑的实例的配置（全局键 + 该实例的实例键）

  // ComfyUI 的显存/精度参数跟新版 Neo 同名，共用一套下拉
  function isNeo2() { return editCfg.webui_branch === "neo2" || editCfg.webui_branch === "comfyui"; }
  function isNeo() { return editCfg.webui_branch === "neo" || editCfg.webui_branch === "neo2"; }

  function controlValue(el) {
    return el.type === "checkbox" ? el.checked : el.value;
  }

  function fillSelect(el, items, cfgKey) {
    el.innerHTML = "";
    (items || []).forEach((it) => {
      const o = document.createElement("option");
      o.value = it.key;
      o.textContent = it.label;
      el.appendChild(o);
    });
    const v = editCfg[cfgKey];
    el.value = v != null ? String(v) : (items[0] ? items[0].key : "");
    if (el.selectedIndex < 0 && items.length) el.selectedIndex = 0;
  }

  function rebuildBranchDependent() {
    building = true;
    try {
      const neo2 = isNeo2();
      const comfy = editCfg.webui_branch === "comfyui";
      fillSelect($("#s-vram"), comfy ? schema.vram_comfy : (neo2 ? schema.vram_neo2 : schema.vram_legacy), "vram_mode");
      fillSelect($("#s-precision"), neo2 ? schema.precision_neo2 : schema.precision_legacy, "precision_mode");
      if (neo2) {
        fillSelect($("#s-unet"), schema.unet_neo2, "unet_precision");
        fillSelect($("#s-vae"), schema.vae_neo2, "vae_precision");
        fillSelect($("#s-textenc"), schema.text_enc_neo2, "text_enc_precision");
        fillSelect($("#s-attention"), schema.attention_neo2, "attention_impl");
      }
      // 分支显隐
      $$("[data-branch-show]").forEach((el) => {
        el.style.display = el.dataset.branchShow.split(",").includes(editCfg.webui_branch) ? "" : "none";
      });
      $$("[data-branch-hide]").forEach((el) => {
        el.style.display = el.dataset.branchHide.split(",").includes(editCfg.webui_branch) ? "none" : "";
      });
      $$("[data-branch-neo]").forEach((el) => {
        el.style.display = isNeo() ? "" : "none";
      });
    } finally {
      building = false;
    }
  }

  /* ---------- 高级选项页：按实例加载 / 保存 ---------- */
  function fillControls() {
    $$("#page-settings [data-cfg]").forEach((el) => {
      const v = editCfg[el.dataset.cfg];
      if (el.type === "checkbox") el.checked = !!v;
      else el.value = v == null ? "" : String(v);
    });
  }

  function editingIsActive() { return editIid && editIid === App.instances.active; }

  function syncSelector() {
    const card = $("#s-instance-card");
    const sel = $("#s-instance");
    const ins = App.instances || {};
    const list = ins.instances || [];
    card.hidden = !ins.multi;
    sel.innerHTML = list.map((i) =>
      `<option value="${App.esc(i.id)}">${App.esc(i.name)} · ${App.esc(i.kind_label)}</option>`).join("");
    if (!list.find((i) => i.id === editIid)) editIid = ins.active || (list[0] && list[0].id) || "";
    sel.value = editIid;
    const cur = list.find((i) => i.id === editIid);
    $("#s-instance-hint").textContent = cur
      ? (editingIsActive()
        ? `正在设置「${cur.name}」——这就是当前实例，改动保存后立即生效`
        : `正在设置「${cur.name}」——不是当前实例，改动只存到它名下，下次启动它时生效`)
      : "";
  }

  async function loadInstance() {
    if (!editIid) return;
    try {
      const r = await App.api.instance_config_get(editIid);
      if (!r || r.ok === false) return;
      editCfg = r.config || {};
    } catch (e) {
      // 后端太老没有这个接口时退化为当前实例配置（单实例场景无感知）
      editCfg = App.cfg || {};
    }
    building = true;
    try {
      fillControls();
      $("#s-branch").value = editCfg.webui_branch || "neo2";
    } finally {
      building = false;
    }
    rebuildBranchDependent();
  }

  async function saveKey(el) {
    const key = el.dataset.cfg;
    const val = controlValue(el);
    editCfg[key] = val;
    if (editingIsActive()) App.cfg[key] = val;
    try {
      const r = await App.api.instance_config_update(editIid, { [key]: val });
      if (r && r.ok === false) { App.toast(r.error || "配置保存失败", "error"); return; }
      // 改的是当前实例才刷新启动页的参数预览；改别的实例不动预览
      if (r && r.cmd_args != null && editingIsActive()) App.refreshCmdPreview(r.cmd_args);
    } catch (e) {
      console.error(e);
      App.toast("配置保存失败：" + e.message, "error");
    }
  }

  /* ---------- 启动器设置页：镜像检测 ---------- */
  async function redetectMirror() {
    const el = $("#s-mirror-status");
    el.dataset.status = "";
    el.textContent = "正在检测网络环境…";
    try { await App.api.redetect_network(); } // 结果走 settings/mirror_status 事件
    catch (e) { el.textContent = "检测失败：" + e.message; el.dataset.status = "bad"; }
  }

  /* ---------- 启动器设置页：启动器自更新 ---------- */
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
      this._doneSeq = this._checkSeq;  // 喂狗：结果到了，超时看门狗不再触发
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
      // 看门狗：后端任何异常路径（事件丢失/线程死掉）都会让"正在检查"挂到天荒地老，
      // 60 秒没收到 update_info 就按失败显示，让用户能重试而不是干等
      const mySeq = (this._checkSeq = (this._checkSeq || 0) + 1);
      setTimeout(() => {
        if (this._checkSeq === mySeq && this._doneSeq !== mySeq) {
          $("#up-remote").textContent = "检查超时";
          this.setStatus("检查超时：60 秒没有收到结果，请检查网络后重试", true);
        }
      }, 60000);
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
      const anyRunning = ((App.instances && App.instances.instances) || [])
        .some((i) => i.status && i.status.running);
      if (anyRunning || (App.state && App.state.launch && App.state.launch.running)) {
        App.toast("还有实例在运行，请先停止再重启启动器", "error");
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
          App.toast("发现启动器新版本 V" + e.remote.version + "，可在 启动器设置 → 启动器更新 一键升级", "ok", 6000);
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

  /* =============== 高级选项（按实例） =============== */
  App.pages.settings = {
    async init(state) {
      schema = state.settings_schema || {};

      // 分支下拉
      const branchSel = $("#s-branch");
      (state.settings_branches || []).forEach((b) => {
        const o = document.createElement("option");
        o.value = b.key;
        o.textContent = b.label;
        branchSel.appendChild(o);
      });

      // 实例选择器：选谁就在改谁（不动侧栏的「当前实例」）
      $("#s-instance").addEventListener("change", async (e) => {
        editIid = e.target.value;
        syncSelector();
        await loadInstance();
      });

      // 每个控件单独保存到「正在编辑的实例」
      $$("#page-settings [data-cfg]").forEach((el) => {
        el.addEventListener("change", async () => {
          if (building) return;
          await saveKey(el);
          // 分支变化会影响参数生成和显隐，顺手重建（core 的全局绑定不覆盖本页）
          if (el.id === "s-branch") rebuildBranchDependent();
        });
      });

      editIid = (App.state.instances && App.state.instances.active) || "";
      syncSelector();
      await loadInstance();
    },

    async onShow() {
      // 实例可能被增删/改名过，选择器和编辑目标都重新对齐一次
      syncSelector();
      await loadInstance();
    },
  };

  /* =============== 启动器设置（全局） =============== */
  App.pages.launcher = {
    init(state) {
      const ms = state.mirror_status;
      if (ms) $("#s-mirror-status").textContent = (typeof ms === "string") ? ms : (ms.text || "");
      $("#s-mirror-redetect").addEventListener("click", redetectMirror);

      App.on("settings", "mirror_status", (e) => {
        const el = $("#s-mirror-status");
        el.textContent = e.text || "";
        el.dataset.status = e.ok ? "ok" : "";
      });

      // 界面风格：选项来自 themes.js 注册表；切换立即生效（保存由 core 的
      // data-cfg 全局绑定负责，这里只负责即时预览 + 写 localStorage）
      const themeSel = $("#s-ui-theme");
      if (window.WWYThemes && themeSel) {
        themeSel.innerHTML = "";
        window.WWYThemes.list().forEach((t) => {
          const o = document.createElement("option");
          o.value = t.id;
          o.textContent = t.label;
          themeSel.appendChild(o);
        });
        themeSel.value = App.cfg.ui_theme || "terminal";
        themeSel.addEventListener("change", () => window.WWYThemes.apply(themeSel.value, true));
      }

      Updater.init(state);

      // 多实例开关
      const multi = $("#s-multi");
      const syncMulti = () => {
        const ins = App.instances || {};
        multi.checked = !!ins.multi;
        multi.disabled = (ins.instances || []).length >= 2;
        $("#s-multi-hint").textContent = multi.disabled
          ? `已有 ${(ins.instances || []).length} 个实例，多实例功能自动开启。移除到只剩一个实例后才能关掉。`
          : "";
      };
      syncMulti();
      multi.addEventListener("change", async () => {
        try {
          const r = await App.api.set_multi_ui(multi.checked);
          if (r && r.ok !== false && App.applyChrome) {
            App.applyChrome({ instances: r, hidden_pages: App.hiddenPages });
            if (multi.checked) App.toast("已显示多实例功能：侧栏出现了「实例管理」", "ok");
          }
        } catch (e) { App.toast("设置失败：" + e.message, "error"); }
        syncMulti();
      });
      App.pages.launcher.syncMulti = syncMulti;

      // 功能开关：把用不上的页面从侧栏藏起来
      const PAGES = [["settings", "高级选项"], ["deploy", "环境部署"], ["civitai", "模型下载"],
        ["models", "模型管理"], ["outputs", "输出管理"], ["extensions", "常用插件"],
        ["wd14", "WD14 反推"], ["meta", "图片信息"]];
      const box = $("#s-pages");
      const hidden = new Set(state.hidden_pages || []);
      box.innerHTML = PAGES.map(([k, label]) =>
        `<label class="chk-row"><input type="checkbox" data-page-toggle="${k}"${hidden.has(k) ? "" : " checked"}><i></i><span>${label}</span></label>`).join("");
      box.querySelectorAll("[data-page-toggle]").forEach((cb) => cb.addEventListener("change", async () => {
        const pages = Array.from(box.querySelectorAll("[data-page-toggle]"))
          .filter((x) => !x.checked).map((x) => x.dataset.pageToggle);
        try {
          const r = await App.api.set_hidden_pages(pages);
          if (r && r.ok !== false && App.applyChrome) {
            App.applyChrome({ instances: App.instances, hidden_pages: r.hidden_pages });
          }
        } catch (e) { App.toast("设置失败：" + e.message, "error"); }
      }));
    },

    onShow() { if (App.pages.launcher.syncMulti) App.pages.launcher.syncMulti(); },
  };
})();
