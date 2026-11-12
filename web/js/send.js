/* send.js —— 「发送到 WebUI / 发送到 ComfyUI」和 LoRA 调用格式，图片信息页、输出管理器、模型管理共用
 *
 *   App.sendTo("webui" | "comfy", src, btn)
 *       src = { from: "meta" }              图片信息页当前这张
 *           | { from: "output", id }        输出管理器的一条记录
 *           | { path }                      任意文件
 *     好几个同类实例都在运行时弹框让用户挑；转换时有对不上的地方（ComfyUI 节点 WebUI 套不上、
 *     ComfyUI 里缺模型等）弹框列出来，不会悄悄吞掉。
 *
 *   App.loraStem / App.loraTag / App.loraTags
 *     <lora:Anima\画风\xxx:1> WebUI 认不出，只要文件名 <lora:xxx:1>
 */
(function () {
  "use strict";
  const App = window.App;

  const EXT = /\.(safetensors|ckpt|pt|pth|bin|gguf|sft)$/i;

  App.loraStem = function (name) {
    const n = String(name == null ? "" : name).replace(/\\/g, "/").replace(/\/+$/, "");
    return n.slice(n.lastIndexOf("/") + 1).replace(EXT, "");
  };

  App.loraTag = function (name, weight) {
    const w = weight == null || weight === "" || isNaN(+weight) ? 1 : +weight;
    return `<lora:${App.loraStem(name)}:${w}>`;
  };

  App.loraTags = function (loras) {
    const seen = new Set();
    const out = [];
    (loras || []).forEach((l) => {
      const name = typeof l === "string" ? l : l && l.name;
      const stem = App.loraStem(name);
      if (!stem || seen.has(stem.toLowerCase())) return;
      seen.add(stem.toLowerCase());
      out.push(App.loraTag(stem, typeof l === "string" ? 1 : l.weight));
    });
    return out.join(", ");
  };

  const LABEL = { webui: "WebUI", comfy: "ComfyUI" };
  let busy = false;

  async function chooseTarget(kind, list) {
    const body = `<div class="dim" style="margin-bottom:8px">有 ${list.length} 个 ${LABEL[kind]} 在运行，发送到哪一个？</div>`;
    const btns = list.map((t) => ({ id: t.id, label: t.name, kind: t.active ? "primary" : "" }))
      .concat([{ id: "cancel", label: "取消" }]);
    const v = await App.modal(`发送到 ${LABEL[kind]}`, body, btns);
    return v && v !== "cancel" ? v : null;
  }

  function showNotes(kind, r) {
    const notes = r.notes || [];
    const missing = r.missing || [];
    if (!notes.length && !missing.length) return;
    const li = (arr) => arr.map((x) => `<li>${App.esc(x)}</li>`).join("");
    let html = `<div style="margin-bottom:6px">${App.esc(r.text || "已发送")}</div>`;
    if (notes.length) html += `<div class="dim" style="margin:8px 0 4px">转换时有几处需要留意：</div><ul class="send-notes">${li(notes)}</ul>`;
    if (missing.length && kind === "comfy") {
      html += `<div class="dim" style="margin:8px 0 4px">ComfyUI 里没找到这些模型（节点里先填了原名）：</div><ul class="send-notes">${li(missing)}</ul>` +
        `<div class="dim">可以在本页「检测缺失模型」一键下载，或用启动器的「诊断」处理；下好后在 ComfyUI 里按 R 刷新。</div>`;
    }
    if (kind === "comfy" && r.mode === "built") html += `<div class="dim" style="margin-top:8px">这些说明也写在了工作流左边的「转换说明」节点里。</div>`;
    App.modal(`已发送到 ${LABEL[kind]}`, html, [{ id: "ok", label: "知道了", kind: "primary" }]);
  }

  App.sendTo = async function (kind, src, btn, iid) {
    if (busy) return;
    busy = true;
    const old = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "发送中…"; }
    let r;
    try {
      r = await App.api.send_to(kind, src, iid || null, "txt2img");
    } catch (e) {
      r = { ok: false, error: e.message };
    } finally {
      busy = false;
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
    if (r && r.choose) {
      const pick = await chooseTarget(kind, r.choose);
      if (pick) return App.sendTo(kind, src, btn, pick);
      return;
    }
    if (!r || r.ok === false) {
      App.toast((r && r.error) || "发送失败", "error", 7000);
      if (r && r.not_running && App.showPage) {
        const v = await App.modal(`${LABEL[kind]} 没在运行`, `<div>${App.esc(r.error)}</div>`,
          [{ id: "go", label: "去一键启动", kind: "primary" }, { id: "cancel", label: "取消" }]);
        if (v === "go") App.showPage("launch");
      }
      return;
    }
    App.toast(r.text || "已发送", "ok", 5000);
    showNotes(kind, r);
  };
})();
