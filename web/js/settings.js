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
      branchSel.value = App.cfg.webui_branch || "classic";

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
    },
  };
})();
