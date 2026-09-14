/* wd14.js — WD14 标签反推页 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  let images = [];          // 待处理图片绝对路径
  let modelReady = false;
  let loading = false;
  let tagging = false;
  let modelProg = null;
  let tagProg = null;
  let resultLog = null;
  let resultCount = 0;
  let modelUrls = {};       // model_key -> 仓库页面地址（手动下载用）

  function refreshRunBtn() {
    $("#wd-run").disabled = !modelReady || loading || tagging || !images.length;
    $("#wd-cancel").disabled = !tagging;
    $("#wd-load").disabled = loading || tagging;
  }

  function renderImageList() {
    const box = $("#wd-images");
    if (!images.length) {
      box.innerHTML = '<div class="drop-empty">把图片 / 文件夹拖到这里，或直接 Ctrl+V 粘贴剪贴板里的图片</div>';
    } else {
      box.innerHTML = images.map((p) => `<div class="item">${App.esc(p)}</div>`).join("");
    }
    refreshRunBtn();
  }

  async function addPaths(paths) {
    if (!paths || !paths.length) return;
    try {
      const r = await App.api.wd14_expand(paths);
      const news = (r.paths || []).filter((p) => !images.includes(p));
      images = images.concat(news);
      renderImageList();
      if (news.length) App.toast(`已添加 ${news.length} 张图片（共 ${images.length} 张）`, "ok");
      else App.toast("没有找到新的图片文件");
    } catch (e) { App.toast("展开路径失败：" + e.message, "error"); }
  }

  App.pages.wd14 = {
    async init() {
      modelProg = App.progress($("#wd-model-progress"));
      tagProg = App.progress($("#wd-tag-progress"));
      modelProg.hide();
      tagProg.hide();
      resultLog = App.makeLogger($("#wd-result"), 500);

      // 模型列表
      try {
        const r = await App.api.wd14_models();
        const sel = $("#wd-model");
        (r.models || []).forEach((m) => {
          const o = document.createElement("option");
          o.value = m.key;
          o.textContent = m.label + (m.cached ? "（已缓存）" : "");
          sel.appendChild(o);
          modelUrls[m.key] = m.urls || [];
        });
        if (r.model_ready) {
          modelReady = true;
          $("#wd-model-status").textContent = "模型已就绪";
          $("#wd-model-status").dataset.status = "ok";
        }
      } catch (e) { App.toast("读取模型列表失败：" + e.message, "error"); }

      // 滑块标签
      const gen = $("#wd-general"), chr = $("#wd-character");
      gen.addEventListener("input", () => {
        $("#wd-general-label").textContent = `常规标签阈值（${(gen.value / 100).toFixed(2)}）`;
      });
      chr.addEventListener("input", () => {
        $("#wd-character-label").textContent = `角色标签阈值（${(chr.value / 100).toFixed(2)}）`;
      });

      $("#wd-load").addEventListener("click", async () => {
        loading = true; refreshRunBtn();
        $("#wd-model-status").textContent = "加载中…";
        $("#wd-model-status").dataset.status = "";
        try {
          const r = await App.api.wd14_load_model($("#wd-model").value);
          if (r && r.ok === false) {
            App.toast(r.error || "加载失败", "error");
            loading = false; refreshRunBtn();
          }
        } catch (e) {
          App.toast("加载失败：" + e.message, "error");
          loading = false; refreshRunBtn();
        }
      });

      // 下载失败时的退路：自己去网页下好，再把文件夹导进来
      $("#wd-open-repo").addEventListener("click", () => {
        const urls = modelUrls[$("#wd-model").value] || [];
        if (!urls.length) { App.toast("没有这个模型的下载地址", "error"); return; }
        App.modal("手动下载模型",
          `<div class="dim" style="margin-bottom:10px">在下面的页面里找到 <b>model.onnx</b> 和 <b>selected_tags.csv</b>，` +
          `两个都下载到同一个文件夹里，然后回来点「手动导入模型…」。<br><br>` +
          `国内一般第一个能打开；model.onnx 体积较大，建议用下载工具拖。</div>` +
          `<div class="choice-list">` +
          urls.map((u, i) => `<div class="choice-item" data-i="${i}">${App.esc(u)}</div>`).join("") +
          `</div>`,
          [{ id: "cancel", label: "关闭" }],
          {
            onOpen(body) {
              body.querySelectorAll(".choice-item").forEach((el) =>
                el.addEventListener("click", () => App.api.open_url(urls[+el.dataset.i])));
            },
          });
      });

      $("#wd-import").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择放着 model.onnx 和 selected_tags.csv 的文件夹", "");
        if (!r || !r.ok || !r.path) return;
        loading = true; refreshRunBtn();
        $("#wd-model-status").textContent = "导入中…";
        $("#wd-model-status").dataset.status = "";
        try {
          const res = await App.api.wd14_import_model($("#wd-model").value, r.path);
          if (res && res.ok === false) {
            loading = false; refreshRunBtn();
            $("#wd-model-status").textContent = "导入失败";
            $("#wd-model-status").dataset.status = "bad";
            App.modal("导入失败", App.esc(res.error || ""), [{ id: "ok", label: "知道了", kind: "primary" }]);
          }
        } catch (e) {
          loading = false; refreshRunBtn();
          App.toast("导入失败：" + e.message, "error");
        }
      });

      $("#wd-pick-files").addEventListener("click", async () => {
        const r = await App.api.choose_images(true);
        if (r && r.ok && r.paths && r.paths.length) addPaths(r.paths);
      });
      $("#wd-pick-folder").addEventListener("click", async () => {
        const r = await App.api.choose_directory("选择图片文件夹", App.cfg.webui_root || "");
        if (r && r.ok && r.path) addPaths([r.path]);
      });
      $("#wd-clear").addEventListener("click", () => { images = []; renderImageList(); });

      // 拖放：完整路径由 Python 侧 pywebview DOM 事件拿到后经 app/dropped → onDropped 进来；
      // 这里仅作为少数能拿到 .path 的平台的兜底
      const drop = $("#wd-images");
      ["dragover", "dragenter"].forEach((ev) => drop.addEventListener(ev, (e) => {
        e.preventDefault(); drop.classList.add("dragover");
      }));
      ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
        e.preventDefault(); drop.classList.remove("dragover");
      }));
      drop.addEventListener("drop", (e) => {
        const paths = Array.from(e.dataTransfer.files || []).map((f) => f.path).filter(Boolean);
        if (paths.length) addPaths(paths);
      });

      // 剪贴板粘贴：Ctrl+V 把截图/复制的图片直接加进列表。
      // 剪贴板里的 File 没有磁盘路径，读成 base64 交给 Python 落成临时文件。
      const abToB64 = (buf) => {
        const bytes = new Uint8Array(buf);
        let s = "";
        for (let i = 0; i < bytes.length; i += 0x8000) {
          s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
        }
        return btoa(s);
      };
      document.addEventListener("paste", (e) => {
        if (App.currentPage !== "wd14") return;
        const t = e.target;
        if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        const files = [];
        for (const it of items) {
          if (it.kind === "file" && (it.type || "").startsWith("image/")) {
            const f = it.getAsFile();
            if (f) files.push(f);
          }
        }
        if (!files.length) return;
        e.preventDefault();
        files.forEach(async (f) => {
          try {
            const b64 = abToB64(await f.arrayBuffer());
            const r = await App.api.wd14_add_clipboard_image(b64, f.name || "");
            if (r && r.ok && r.path) addPaths([r.path]);
            else App.toast((r && r.error) || "粘贴图片失败", "error");
          } catch (err) { App.toast("粘贴图片失败：" + err.message, "error"); }
        });
      });

      $("#wd-run").addEventListener("click", async () => {
        tagging = true; refreshRunBtn();
        resultCount = 0;
        tagProg.set(0, "准备中…");
        try {
          const r = await App.api.wd14_tag(
            images, gen.value / 100, chr.value / 100,
            $("#wd-include-rating").checked, $("#wd-save-txt").checked);
          if (r && r.ok === false) {
            App.toast(r.error || "无法开始反推", "error");
            tagging = false; refreshRunBtn(); tagProg.hide();
          }
        } catch (e) {
          App.toast("无法开始反推：" + e.message, "error");
          tagging = false; refreshRunBtn(); tagProg.hide();
        }
      });
      $("#wd-cancel").addEventListener("click", () => App.api.wd14_cancel());
      $("#wd-result-clear").addEventListener("click", () => { $("#wd-result").textContent = ""; });

      App.on("wd14", "model_log", (e) => {
        $("#wd-model-status").textContent = e.text;
      });
      App.on("wd14", "model_progress", (e) => {
        const pct = e.total > 0 ? (e.downloaded / e.total * 100) : 0;
        modelProg.set(pct, `${e.name}：${App.fmtBytes(e.downloaded)} / ${App.fmtBytes(e.total)}`);
      });
      App.on("wd14", "model_ready", () => {
        loading = false; modelReady = true;
        modelProg.hide();
        $("#wd-model-status").textContent = "模型已就绪";
        $("#wd-model-status").dataset.status = "ok";
        App.toast("模型加载完成", "ok");
        refreshRunBtn();
      });
      App.on("wd14", "model_failed", (e) => {
        loading = false; modelReady = false;
        modelProg.hide();
        $("#wd-model-status").textContent = "加载失败";
        $("#wd-model-status").dataset.status = "bad";
        App.modal("模型加载失败",
          `<pre class="modal-pre">${App.esc(e.msg || "")}</pre>`,
          [{ id: "ok", label: "知道了", kind: "primary" }]);
        refreshRunBtn();
      });

      App.on("wd14", "tag_log", (e) => resultLog("[日志] " + e.text));
      App.on("wd14", "tag_progress", (e) => {
        const pct = e.total > 0 ? (e.done / e.total * 100) : 0;
        tagProg.set(pct, `反推中 ${e.done}/${e.total}`);
      });
      App.on("wd14", "tag_item", (e) => {
        resultCount++;
        const name = e.path.split(/[\\/]/).pop();
        resultLog(`【${name}】\n${e.tags}\n`);
      });
      App.on("wd14", "tag_done", () => {
        tagging = false; refreshRunBtn();
        tagProg.set(100, `完成，共处理 ${resultCount} 张`);
        App.toast(`反推完成，共 ${resultCount} 张`, "ok");
      });
      App.on("wd14", "tag_failed", (e) => {
        tagging = false; refreshRunBtn(); tagProg.hide();
        App.modal("反推失败", App.esc(e.msg || ""), [{ id: "ok", label: "知道了", kind: "primary" }]);
      });

      renderImageList();
    },

    onDropped(paths) {
      addPaths(paths);
    },
  };
})();
