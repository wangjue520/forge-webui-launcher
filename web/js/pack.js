/* pack.js — 环境部署页的「整合包」卡片：制作（实例 → .txt / .zip）和部署（按整合包装出一样的环境）
   后端：app/pack_manager.py（pack_* 接口）。制作的进度在本卡片里，部署走部署页原本的进度 / 日志 / 完成弹窗。 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);
  const esc = (s) => App.esc(s == null ? "" : String(s));
  const SRC_LABEL = { civitai: "C 站", hf: "常用模型目录", liblib: "LiblibAI" };
  const SRC_ORDER = ["hf", "civitai", "liblib"];   // 默认先走 HF / 国内镜像，再 C 站、Liblib
  const OUT_KEY = "pack-out-dir";   // 记住上次「保存到」的位置
  const lastOutDir = () => { try { return localStorage.getItem(OUT_KEY) || ""; } catch (e) { return ""; } };
  let log = null;
  let prog = null;
  let busy = false;

  function setBusy(v) {
    busy = v;
    $("#pack-export").disabled = v;
    $("#pack-cancel").hidden = !v;
    if (!v) prog.hide();
  }

  /* ---------- 制作 ---------- */
  function modelRows(items) {
    if (!items.length) return '<div class="hint">这个实例的底模目录里没有模型文件</div>';
    let html = "", key = null;
    items.forEach((m, i) => {
      if (m.key !== key) { key = m.key; html += `<div class="pk-group">${esc(m.where)}</div>`; }
      html += `<label class="chk-row pk-model"><input type="checkbox" data-mi="${i}"><i></i>` +
        `<span class="pk-name">${esc(m.name)}</span><span class="pk-size">${esc(App.fmtBytes(m.size))}</span></label>`;
    });
    return html;
  }

  async function openExport() {
    if (busy) return;
    let r;
    try { r = await App.api.pack_sources(); } catch (e) { App.toast("读取实例失败：" + e.message, "error"); return; }
    if (!r || r.ok === false) { App.toast((r && r.error) || "读取实例失败", "error"); return; }
    const items = r.items || [];
    if (!items.some((s) => s.ok)) {
      await App.modal("制作整合包", esc("还没有能做成整合包的实例：先在「一键启动」或「实例管理」里添加装好的 WebUI / ComfyUI，或者部署好 LoRA 训练器。"),
        [{ id: "ok", label: "知道了", kind: "primary" }]);
      return;
    }
    const first = items.find((s) => s.ok);
    const opts = items.map((s) => `<option value="${esc(s.id)}"${s.ok ? "" : " disabled"}${s === first ? " selected" : ""}>` +
      `${esc(s.name)}（${esc(s.label)}）${s.ok ? "" : " — " + esc(s.reason)}</option>`).join("");
    let models = [];
    const body = '<div class="pk-form">' +
      `<label class="pk-label">要做成整合包的程序</label><select id="pk-src">${opts}</select>` +
      '<div class="hint" id="pk-src-hint"></div>' +
      '<label class="pk-label">整合包名字</label><input type="text" id="pk-name" placeholder="比如：我的 ComfyUI 视频工作流">' +
      '<label class="chk-row"><input type="checkbox" id="pk-check" checked><i></i><span>联网核对插件地址（连不上的私有仓库也连文件一起打包；网络不好时会慢一点）</span></label>' +
      '<label class="chk-row"><input type="checkbox" id="pk-with-models"><i></i><span>带上底模（主模型 / 文本编码器 / VAE）：记下哈希和下载方式，部署的人可以选从 C 站、常用模型目录或 LiblibAI 下</span></label>' +
      '<div id="pk-models" class="pk-models" hidden></div>' +
      '<label class="pk-label">保存到</label>' +
      `<div class="row"><input type="text" id="pk-out" value="${esc(lastOutDir() || r.out_dir || "")}"><button class="btn" id="pk-out-browse">浏览…</button></div>` +
      "</div>";
    const v = await App.modal("制作整合包", body, [{ id: "go", label: "开始制作", kind: "primary" }, { id: "cancel", label: "取消" }], {
      onOpen(el) {
        const src = el.querySelector("#pk-src"), name = el.querySelector("#pk-name"), hint = el.querySelector("#pk-src-hint");
        const box = el.querySelector("#pk-models"), withM = el.querySelector("#pk-with-models");
        const sync = () => {
          const s = items.find((x) => x.id === src.value) || {};
          name.value = s.name || "";
          hint.textContent = (s.root || "") + (s.running ? "　（正在运行也能制作，只读不改）" : "");
          if (withM.checked) loadModels();
        };
        const loadModels = async () => {
          box.hidden = !withM.checked;
          if (!withM.checked) return;
          box.innerHTML = '<div class="hint">读取模型列表…</div>';
          try {
            const m = await App.api.pack_models(src.value);
            if (!m || m.ok === false) { box.innerHTML = `<div class="hint" data-status="bad">${esc((m && m.error) || "读取失败")}</div>`; return; }
            models = m.items || [];
            box.innerHTML = modelRows(models) + (models.length ? '<div class="hint">勾选的模型会算一遍 sha256（大文件要一会儿），再去 C 站 / LiblibAI / 常用模型目录查下载地址；模型文件本身不放进整合包。</div>' : "");
          } catch (e) { box.innerHTML = `<div class="hint" data-status="bad">${esc(e.message)}</div>`; }
        };
        src.addEventListener("change", sync);
        withM.addEventListener("change", loadModels);
        el.querySelector("#pk-out-browse").addEventListener("click", async () => {
          const d = await App.api.choose_directory("整合包保存到", el.querySelector("#pk-out").value);
          if (d && d.ok && d.path) el.querySelector("#pk-out").value = d.path;
        });
        sync();
      },
      onClose(el, val) {
        if (val !== "go") return;
        openExport.args = {
          sid: el.querySelector("#pk-src").value,
          opts: {
            name: el.querySelector("#pk-name").value.trim(),
            out_dir: el.querySelector("#pk-out").value.trim(),
            check_remote: el.querySelector("#pk-check").checked,
            models: el.querySelector("#pk-with-models").checked
              ? Array.from(el.querySelectorAll("input[data-mi]:checked")).map((c) => models[+c.dataset.mi].path) : [],
          },
        };
      },
    });
    if (v !== "go" || !openExport.args) return;
    const a = openExport.args;
    openExport.args = null;
    try { if (a.opts.out_dir) localStorage.setItem(OUT_KEY, a.opts.out_dir); } catch (e) { /* 存不了就下次用默认位置 */ }
    try {
      const s = await App.api.pack_export(a.sid, a.opts);
      if (s && s.ok === false) { App.toast(s.error || "没能开始", "error", 6000); return; }
      setBusy(true);
      prog.set(1, "开始制作");
      log("[整合包] 开始制作（进度在「环境部署」页的整合包卡片上）");
    } catch (e) { App.toast("没能开始：" + e.message, "error"); }
  }

  async function exportDone(e) {
    setBusy(false);
    if (e.cancelled) { App.toast("已取消制作整合包"); return; }
    if (!e.ok) { App.modal("整合包没做成", esc(e.error || "出错了，详情见运行日志"), [{ id: "ok", label: "知道了", kind: "primary" }]); return; }
    const s = e.summary || {}, p = s.plugins || {};
    const lines = [`已保存：${e.path}`, ""];
    lines.push(`程序：${s.label}　${(s.main && s.main.label) || "（本体不是 git 安装，部署时用最新版）"}`);
    if (s.kind !== "lora_trainer") lines.push(`插件 / 节点：git ${p.git || 0} 个、官方节点库 ${p.cnr || 0} 个、连文件打包 ${p.zip || 0} 个`);
    else lines.push(`训练引擎：${(s.engines || []).join("、") || "无"}`);
    lines.push("pip 包：" + Object.entries(s.pip || {}).map(([k, n]) => `${k} ${n} 个`).join("、"));
    if ((s.models || []).length) lines.push(`底模：${s.models.length} 个（只记了下载方式，模型文件本身不在整合包里）`);
    if (e.zip) {
      const packed = e.packed || [], overlay = e.overlay || [];
      lines.push("");
      if (packed.length) lines.push(`有 ${packed.length} 个插件查不到来源，已连文件一起打包：${packed.join("、")}`);
      if (overlay.length) lines.push(`有 ${overlay.length} 个插件本地改过文件，改过的文件也一起打包了（装好后覆盖上去）：${overlay.join("、")}`);
      lines.push("把这个 .zip 发给别人就行（里面已经有整合包.txt）。");
    } else if (s.kind === "lora_trainer") lines.push("", "训练器和训练引擎都能从网上装回来，整合包就是这一个 .txt 文件。");
    else lines.push("", "所有插件都能从网上装回来，整合包就是这一个 .txt 文件。");
    if (s.main && s.main.note === "本地提交") {
      lines.push("", "注意：本体现在的版本只在这台电脑上（没推到远程仓库），别人部署时拿不到这个版本，会装分支的最新版。");
    }
    const v = await App.modal("整合包做好了", esc(lines.join("\n")),
      [{ id: "open", label: "打开所在文件夹", kind: "primary" }, { id: "ok", label: "好" }]);
    if (v === "open") App.api.reveal_in_explorer(e.path);
  }

  /* ---------- 部署 ---------- */
  function summaryHtml(s) {
    const p = s.plugins || {};
    const rows = [
      ["程序", `${s.label}${s.main && s.main.label ? "　" + s.main.label : "（最新版）"}`],
      ["原机", [s.gpu, s.python && "Python " + s.python, s.torch && "torch " + s.torch].filter(Boolean).join("　") || "—"],
    ];
    if (s.kind === "lora_trainer") rows.push(["训练引擎", (s.engines || []).join("、") || "—"]);
    else rows.push(["插件 / 节点", `git ${p.git || 0}　官方节点库 ${p.cnr || 0}　压缩包里 ${p.zip || 0}` + (p.missing ? `　缺 ${p.missing}` : "")]);
    rows.push(["pip 包", Object.entries(s.pip || {}).map(([k, n]) => `${k} ${n} 个`).join("、") || "—"]);
    if (s.created) rows.push(["制作时间", s.created]);
    let html = '<div class="pk-sum">' + rows.map(([k, v]) => `<span class="pk-k">${esc(k)}</span><span>${esc(v)}</span>`).join("") + "</div>";
    if (s.main && s.main.note === "本地提交") {
      html += `<div class="pk-warn">${esc("本体的这个版本只在做整合包的那台电脑上（没推到远程仓库），这里会装分支的最新版；其余部分照样按整合包对齐。")}</div>`;
    }
    if ((s.missing || []).length) {
      html += `<div class="pk-warn">${esc((s.zip ? "压缩包里少了这些插件的文件，装不上：" : "这些插件的文件在同名 .zip 里，只有 .txt 时装不上（要部署 .zip 才行）：") + s.missing.join("、"))}</div>`;
    }
    return html;
  }

  function modelChoiceHtml(models) {
    if (!models.length) return "";
    let html = '<label class="pk-label">底模（选从哪儿下，不想下的选「不下载」）</label><div class="pk-models">';
    models.forEach((m, i) => {
      const avail = SRC_ORDER.filter((k) => m[k]);
      const opts = avail.map((k, j) => `<option value="${k}"${j === 0 ? " selected" : ""}>${SRC_LABEL[k]}</option>`).join("") +
        `<option value=""${avail.length ? "" : " selected"}>${avail.length ? "不下载" : "查不到，自己放"}</option>`;
      html += `<div class="pk-mrow"><span class="pk-name" title="${esc(m.key + "/" + m.name)}">${esc(m.name)}</span>` +
        `<span class="pk-size">${m.size ? esc(App.fmtBytes(m.size)) : ""}</span><select data-mk="${i}"${avail.length ? "" : " disabled"}>${opts}</select></div>`;
    });
    return html + '</div><div class="hint">LiblibAI 下载要先在「模型下载」页填 usertoken；C 站有的模型要 API Key（「启动器设置」里填）。</div>';
  }

  async function precheck(target, kind, portable, gpu) {
    let pre;
    try { pre = await App.api.deploy_precheck(target, kind, portable, gpu.backend, gpu.gfx); }
    catch (e) { App.toast("检查失败：" + e.message, "error"); return false; }
    const issues = (pre && pre.issues) || [];
    const errors = issues.filter((i) => i.level === "error");
    if (errors.length || (pre && pre.ok === false)) {
      await App.modal("无法开始部署", esc(errors.map((i) => i.text).join("\n\n") || "预检查没通过，请检查安装目录"),
        [{ id: "ok", label: "知道了", kind: "primary" }]);
      return false;
    }
    const warns = issues.filter((i) => i.level === "warn");
    if (warns.length) {
      const v = await App.modal("部署前提醒", esc(warns.map((i) => i.text).join("\n\n")),
        [{ id: "go", label: "仍然继续部署", kind: "primary" }, { id: "cancel", label: "取消" }]);
      return v === "go";
    }
    return true;
  }

  async function openDeploy() {
    if (App.deployRunning && App.deployRunning()) { App.toast("已经有部署任务在进行，等它结束再来", "error"); return; }
    let f;
    try { f = await App.api.pack_choose_file(); } catch (e) { App.toast("选文件失败：" + e.message, "error"); return; }
    const path = f && f.paths && f.paths[0];
    if (!path) return;
    let r;
    try { r = await App.api.pack_load(path); } catch (e) { App.toast("读取失败：" + e.message, "error"); return; }
    if (!r || r.ok === false) {
      await App.modal("打不开这个整合包", esc((r && r.error) || "读取失败"), [{ id: "ok", label: "知道了", kind: "primary" }]);
      return;
    }
    const s = r.summary;
    const trainer = s.kind === "lora_trainer";
    // 安装目录不预填：预填现在这份训练器的目录的话，直接点「开始部署」就会按整合包改掉正在用的训练器
    const defTarget = "";
    const body = '<div class="pk-form">' + `<div class="pk-title">${esc(s.name || "整合包")}</div>` + summaryHtml(s) +
      '<label class="pk-label">安装目录</label>' +
      `<div class="row"><input type="text" id="pk-target" value="${esc(defTarget)}" placeholder="${trainer ? "选一个空文件夹，装一份新的训练器" : "选一个空文件夹，装好后自动添加为新实例"}"><button class="btn" id="pk-target-browse">浏览…</button></div>` +
      '<div class="hint" id="pk-target-tip" data-status="bad" hidden>先选安装目录（点「浏览…」，或者直接填路径）</div>' +
      modelChoiceHtml(s.models || []) +
      (trainer ? "" : '<div class="hint">显卡类型和便携环境按本页下面「显卡」「虚拟环境依赖」的选择来。</div>') +
      "</div>";
    let picked = null;
    const v = await App.modal("部署整合包", body, [{ id: "go", label: "开始部署", kind: "primary" }, { id: "cancel", label: "取消" }], {
      onOpen(el) {
        const target = el.querySelector("#pk-target"), tip = el.querySelector("#pk-target-tip");
        el.querySelector("#pk-target-browse").addEventListener("click", async () => {
          const d = await App.api.choose_directory("整合包装到哪个文件夹", target.value);
          if (d && d.ok && d.path) { target.value = d.path; tip.hidden = true; }
        });
        target.addEventListener("input", () => { tip.hidden = true; });
        // 没选安装目录就点「开始部署」：留在对话框里提示，别关掉（关掉的话文件和底模来源都得重选）
        const go = Array.from(document.querySelectorAll("#modal-buttons .btn")).find((b) => b.textContent === "开始部署");
        if (go) go.addEventListener("click", (ev) => {
          if (target.value.trim()) return;
          ev.stopImmediatePropagation();
          tip.hidden = false;
          target.focus();
        }, true);
      },
      onClose(el, val) {
        if (val !== "go") return;
        const models = {};
        el.querySelectorAll("select[data-mk]").forEach((sel) => {
          const m = s.models[+sel.dataset.mk];
          models[m.key + "/" + m.name] = sel.value;
        });
        picked = { target: el.querySelector("#pk-target").value.trim(), models };
      },
    });
    if (v !== "go" || !picked) return;
    if (!picked.target) { App.toast("请先选安装目录", "error"); return; }
    const gpu = App.deployGpu ? App.deployGpu() : { backend: "", gfx: "" };
    const portable = !!($("#deploy-portable") && $("#deploy-portable").checked);
    if (!trainer && !(await precheck(picked.target, s.kind, portable, gpu))) return;
    try {
      const res = await App.api.pack_deploy(path, picked.target, picked.models, portable, gpu.backend, gpu.gfx);
      if (res && res.ok === false) { App.toast(res.error || "部署没能开始", "error", 6000); return; }
      App.toast("开始按整合包部署，进度见本页下方和运行日志", "ok", 5000);
    } catch (e) { App.toast("部署没能开始：" + e.message, "error"); }
  }

  /* 部署完成弹窗里带上整合包的还原结果（deploy.js 的完成弹窗来取） */
  let lastReport = null;
  App.takePackReport = function () {
    const r = lastReport;
    lastReport = null;
    if (!r) return "";
    const out = [];
    if (r.python) out.push("Python：" + r.python);
    if (r.main) out.push("本体：" + r.main);
    if (r.torch) out.push("torch：" + r.torch);
    if (r.plugins_ok) {
      const bad = r.plugins_failed || [];
      out.push(`插件：装好 ${r.plugins_ok.length} 个` + (bad.length ? `，没装上 ${bad.length} 个：` + bad.map((x) => x[0]).join("、") : ""));
    }
    const pf = r.pip_failed || [];
    out.push(pf.length ? `pip 包：${pf.length} 个没对齐（多半是这台电脑上装不了的包，详情见运行日志）` : "pip 包：全部和整合包一致");
    if (r.models_ok) {
      const bad = r.models_failed || [], have = r.models_have || [];
      const parts = [];
      if (r.models_ok.length) parts.push(`下好 ${r.models_ok.length} 个`);
      if (have.length) parts.push(`本来就有 ${have.length} 个（跳过）`);
      if (bad.length) parts.push(`没下好 ${bad.length} 个：` + bad.map((x) => x[0]).join("、"));
      out.push("底模：" + (parts.join("，") || "没有要下的"));
    }
    return "\n\n整合包还原结果：\n" + out.map((x) => "· " + x).join("\n");
  };

  App.pages.pack = {
    init() {
      log = App.pageLog("pack", "整合包");
      prog = App.progress($("#pack-progress"));
      prog.hide();
      $("#pack-export").addEventListener("click", () => openExport().catch((e) => App.toast("出错了：" + e.message, "error")));
      $("#pack-deploy").addEventListener("click", () => openDeploy().catch((e) => App.toast("出错了：" + e.message, "error")));
      $("#pack-cancel").addEventListener("click", () => App.api.pack_cancel());
      App.on("pack", "log", (e) => log(e.text));
      App.on("pack", "step", (e) => { if (busy) prog.set(e.pct, e.text); });
      App.on("pack", "done", exportDone);
      App.on("deploy", "pack_report", (e) => { lastReport = e.report || null; });
      if (App.api.pack_sources) {
        App.api.pack_sources().then((r) => { if (r && r.busy) setBusy(true); }).catch(() => {});
      }
    },
  };
})();
