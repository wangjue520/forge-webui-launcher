/* extensions.js — 插件管理页
 *
 * 顶部「安装到」明确显示目标实例（类型 + 名字 + 实际安装目录）；有多个实例时
 * 可以直接在这里切换安装目标，不用先去切当前实例。
 * 「安装新插件」：搜索（ComfyUI 搜 Comfy 注册表，和 ComfyUI-Manager 同一个源；WebUI 搜 A1111 官方扩展索引），
 *   或者直接粘贴 GitHub 地址安装。
 * 下面分两栏：
 *   已安装 —— 这个实例里所有插件（推荐清单里的、自己装的、ComfyUI-Manager 装的都算），
 *             看版本、检查更新、换版本 / 回滚、停用、卸载。Manager 从注册表装的节点（zip，没有 .git）
 *             走注册表的版本号。
 *   推荐插件 —— 以前的「常用插件」清单，勾选后批量装。
 * Forge：git clone 到 extensions/，依赖由 WebUI 启动时自己装。
 * ComfyUI：装进 custom_nodes/ 并用该实例的 Python 装好依赖（ComfyUI 不会自己装），所以实例运行中不允许装。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  const INTRO_FORGE = "勾选想装的扩展，点「安装选中项」会依次 git clone 到上面这个实例的 extensions/ 目录。已经装过的（包括你以前自己在 WebUI 里装的）显示「已安装」并自动跳过。部分扩展（ADetailer、WD14 Tagger 等）会按实例的分支自动装对应的兼容版本。装好后重启 WebUI 生效。清单里没有的扩展，用上面的「安装新插件」搜索或粘贴地址。";
  const INTRO_COMFY = "ComfyUI 的节点和 WebUI 扩展不通用，这里是 ComfyUI 专用清单。安装时会 clone 到上面这个实例的 custom_nodes/，并用它自己的 Python 把节点依赖一起装好（ComfyUI 不会自动装依赖）。「推荐」那组在「环境部署」新装 ComfyUI 时会默认一起装上。清单里没有的节点，用上面的「安装新插件」搜索（和 ComfyUI-Manager 同一个注册表）或粘贴地址。";
  const INTRO_INSTALLED_FORGE = "这个实例里装的所有扩展：启动器装的、你在 WebUI「扩展」页自己装的都在这里。可以「检查插件更新」后一键更新，点「版本…」换成任意历史版本或回滚，也可以停用 / 卸载（卸载会放进回收站）。改完重启 WebUI 生效。";
  const INTRO_INSTALLED_COMFY = "这个实例里装的所有节点：启动器装的、ComfyUI-Manager 装的、你自己 clone 的都在这里。Manager 从注册表装的节点（标「注册表版」）按注册表的版本号更新 / 换版本，和 git 装的一样能回滚；换版本后会自动重装依赖。卸载会放进回收站。改完重启 ComfyUI 生效。";

  let log = null;
  let items = [];
  let others = [];
  let installing = false;
  let targetId = "";        // 空 = 当前实例
  let last = null;          // 上一次 ext_list 的结果
  let view = "";            // installed / catalog；空 = 还没定（有已装插件就先看已安装）
  let filterText = "";
  let results = null;       // 上一次搜索结果 {items, url, folder, source}

  function setInstalling(v) {
    installing = v;
    updateButtons();
    $("#ext-refresh").disabled = v;
    $("#ext-cancel").disabled = !v;
    $("#ext-target-sel").disabled = v;
    $("#ext-q-go").disabled = v;
    document.querySelectorAll("#ext-list [data-act], #ext-results [data-act]").forEach((b) => {
      if (!b.dataset.keep) b.disabled = v;
    });
  }

  function comfyBlocked() {
    return !!(last && last.comfy && last.target && last.target.running);
  }

  function allRepos() {
    const out = [];
    items.forEach((it) => (it.repos || []).forEach((r) => out.push(r)));
    others.forEach((r) => out.push(r));
    return out;
  }

  const manageable = (r) => r.is_git || !!r.cnr;

  function updatable() {
    return allRepos().filter((r) => r.update && r.update.behind && (r.cnr || (r.is_git && !r.pinned)));
  }

  function installedCount() {
    return items.filter((it) => (it.repos || []).length).length + others.length;
  }

  function updateButtons() {
    const ok = !!(last && last.has_root);
    const cat = view === "catalog";
    $("#ext-install").hidden = !cat;
    $("#ext-install").disabled = installing || comfyBlocked() || !ok;
    $("#ext-pick-default").hidden = !cat || !(last && last.comfy);
    $("#ext-pick-default").disabled = installing;
    $("#ext-check-upd").disabled = installing || !ok || !allRepos().some(manageable);
    $("#ext-open-dir").disabled = !ok;
    const up = updatable();
    const btn = $("#ext-update-all");
    btn.hidden = !up.length;
    btn.textContent = `全部更新（${up.length}）`;
    btn.disabled = installing || comfyBlocked();
  }

  /* 一个已装目录的版本行 */
  function repoHtml(r, showFolder) {
    const u = r.update || {};
    let ver;
    if (r.cnr) {
      ver = `<span class="ext-status ext-cnr" title="ComfyUI-Manager 从 Comfy 注册表装的（zip 包，没有 .git），按注册表的版本号管理">注册表版</span>` +
        `<b>${r.cnr.version ? "v" + App.esc(r.cnr.version) : "版本未知"}</b>`;
    } else if (!r.is_git) ver = '<span class="ext-ver-na">不是 git 装的（zip 解压的），没法在这里管理版本</span>';
    else if (r.pinned) ver = `固定在 <b>${App.esc(r.short)}</b>`;
    else ver = `${App.esc(r.branch || "")} @ <b>${App.esc(r.short)}</b>`;
    let st = "";
    if (r.disabled) st += '<span class="ext-status ext-off">已停用</span>';
    if (u.error) st += `<span class="ext-upd-err" title="${App.esc(u.error)}">检查失败</span>`;
    else if (u.behind && u.cnr) st += `<span class="ext-status ext-upd">有新版本 · v${App.esc(u.latest_short || "")} · ${App.esc(u.latest_date || "")}</span>`;
    else if (u.behind) st += `<span class="ext-status ext-upd">有更新 · ${u.behind} 个提交 · ${App.esc(u.latest_date || "")}</span>`;
    else if (u.behind === 0) st += '<span class="ext-upd-ok">已是最新</span>';
    else if (u.checked && r.pinned) st += `<span class="ext-upd-ok">最新 ${App.esc(u.latest_short || "")}</span>`;
    const rel = App.esc(r.rel);
    const canUpd = u.behind && (r.cnr || !r.pinned);
    const btns = manageable(r)
      ? (canUpd ? `<button class="btn btn-xs btn-accent" data-act="update" data-rel="${rel}">更新</button>` : "") +
        `<button class="btn btn-xs" data-act="versions" data-rel="${rel}" data-name="${App.esc(r.folder)}">版本…</button>`
      : "";
    const tgl = r.disabled
      ? `<button class="btn btn-xs btn-accent" data-act="enable" data-rel="${rel}" title="重新启用这个插件（重启实例后生效）">启用</button>`
      : `<button class="btn btn-xs" data-act="disable" data-rel="${rel}" title="暂时停用，不删除文件，随时能再启用（重启实例后生效）">停用</button>`;
    return `<div class="ext-ver${r.disabled ? " is-off" : ""}">` +
      `<span class="ext-ver-txt">${showFolder ? `<span class="ext-folder" title="${App.esc(r.remote || "")}">${App.esc(r.folder)}</span>` : ""}${ver}${st}</span>` +
      `<span class="ext-ver-btns">${tgl}${btns}<button class="btn btn-xs" data-act="open" data-rel="${rel}" title="打开插件目录">目录</button>` +
      `<button class="btn btn-xs" data-act="uninstall" data-rel="${rel}" data-name="${App.esc(r.folder)}" title="整个插件文件夹放进回收站">卸载</button></span></div>`;
  }

  function itemHtml(it, withCheck) {
    const tags = (withCheck && it.installed ? '<span class="ext-status">已安装</span>' : "") +
      (!it.installed && it.default ? '<span class="ext-status ext-rec">推荐</span>' : "") +
      (it.author ? '<span class="ext-status ext-rec">作者自制</span>' : "");
    const repos = it.repos || [];
    const variant = it.variant_hint
      ? `<div class="ext-variant">${App.esc(it.variant_hint)}` +
        (it.can_replace ? ` <button class="btn btn-xs" data-act="replace" data-id="${App.esc(it.id)}">换成推荐版本</button>` : "") + "</div>"
      : "";
    // 装在别的文件夹名下（自己装的 / zip 下载的 / 另一个分支的）时把文件夹名标出来
    const want = String(it.folder || "").toLowerCase();
    const showFolder = repos.length > 1 || repos.some((r) => r.folder.toLowerCase() !== want);
    return (withCheck ? `<input type="checkbox" data-id="${App.esc(it.id)}" ${it.installed ? "disabled" : ""}><i></i>` : "") +
      `<span class="ext-body"><span class="ext-name">${App.esc(it.name)}</span>${tags}` +
      `<div class="ext-desc">${App.esc(it.desc || "")}</div>` +
      repos.map((r) => repoHtml(r, showFolder)).join("") + variant + `</span>`;
  }

  function matchFilter(...texts) {
    const q = filterText.trim().toLowerCase();
    return !q || texts.some((t) => String(t || "").toLowerCase().includes(q));
  }

  function renderCatalog(box) {
    const groups = last && last.comfy ? (last.groups || {}) : null;
    let curGroup = null;
    items.forEach((it) => {
      if (groups && it.group !== curGroup) {
        curGroup = it.group;
        const h = document.createElement("div");
        h.className = "ext-group";
        h.textContent = groups[curGroup] || curGroup;
        box.appendChild(h);
      }
      const el = document.createElement("label");
      el.className = "chk-row ext-item" + (it.installed ? " installed" : "");
      el.innerHTML = itemHtml(it, true);
      box.appendChild(el);
    });
  }

  function renderInstalled(box) {
    let n = 0;
    const known = items.filter((it) => (it.repos || []).length &&
      matchFilter(it.name, it.desc, ...(it.repos || []).map((r) => r.folder)));
    known.forEach((it) => {
      const el = document.createElement("div");
      el.className = "ext-item ext-inst";
      el.innerHTML = itemHtml(it, false);
      box.appendChild(el);
      n++;
    });
    const rest = others.filter((r) => matchFilter(r.folder, r.remote, r.cnr && r.cnr.id));
    if (rest.length) {
      const h = document.createElement("div");
      h.className = "ext-group";
      h.textContent = known.length ? `其他插件（${rest.length}）· 自己装的 / Manager 装的` : `已装插件（${rest.length}）`;
      box.appendChild(h);
      rest.forEach((r) => {
        const el = document.createElement("div");
        el.className = "ext-item ext-other";
        el.innerHTML = `<span class="ext-body">${repoHtml(r, true)}</span>`;
        box.appendChild(el);
        n++;
      });
    }
    if (!n) {
      box.innerHTML = filterText.trim()
        ? `<div class="hint" style="padding:10px">没有名字里带「${App.esc(filterText.trim())}」的已装插件</div>`
        : '<div class="hint" style="padding:10px">这个实例还没有装插件。可以在「推荐插件」里勾选安装，或者用上面的「安装新插件」搜索。</div>';
    }
  }

  function render() {
    const box = $("#ext-list");
    box.innerHTML = "";
    const cnt = installedCount();
    $("#ext-n-installed").textContent = ` ${cnt}`;
    if (!view) view = cnt ? "installed" : "catalog";
    document.querySelectorAll("#ext-view [data-view]").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
    $("#ext-filter").hidden = view !== "installed";
    const comfy = !!(last && last.comfy);
    $("#ext-intro").textContent = view === "installed"
      ? (comfy ? INTRO_INSTALLED_COMFY : INTRO_INSTALLED_FORGE)
      : (comfy ? INTRO_COMFY : INTRO_FORGE);
    if (view === "installed") renderInstalled(box);
    else renderCatalog(box);
    if (installing) setInstalling(true);
    updateButtons();
  }

  function renderTarget(r) {
    const t = r.target || {};
    const kind = $("#ext-target-kind");
    kind.dataset.kind = t.kind || "";
    kind.textContent = t.kind_label || "—";
    $("#ext-target-name").textContent = t.name || "—";
    $("#ext-target-path").textContent = t.target_dir
      ? (r.comfy ? "节点目录：" : "扩展目录：") + t.target_dir
      : "（这个实例还没有设置正确的根目录）";
    $("#ext-target-hint").textContent = r.comfy
      ? "ComfyUI 实例 → 装 ComfyUI 节点"
      : "WebUI 实例 → 装 WebUI 扩展";
    $("#ext-find-hint").textContent = r.comfy
      ? "搜 Comfy 注册表（和 ComfyUI-Manager 同一个源，下载走注册表 CDN，不用连 GitHub）"
      : "搜 WebUI 官方扩展索引（和 WebUI「扩展 → 可下载」同一个列表）";

    // 多个实例时才出现切换框
    const list = (App.instances && App.instances.instances) || [];
    const sel = $("#ext-target-sel");
    if (list.length > 1) {
      sel.innerHTML = list.map((i) =>
        `<option value="${App.esc(i.id)}">${App.esc(i.name)}${i.name !== i.kind_label ? "（" + App.esc(i.kind_label) + "）" : ""}${i.active ? " · 当前实例" : ""}</option>`
      ).join("");
      sel.value = t.id || "";
      sel.hidden = false;
      $("#ext-target-name").hidden = true;
    } else {
      sel.hidden = true;
      $("#ext-target-name").hidden = false;
    }

    const warn = $("#ext-target-warn");
    const msgs = [];
    if (r.comfy && t.running) {
      msgs.push(`「${t.name}」正在运行：安装 / 更新节点要往它的 Python 环境里装依赖，运行中会因为文件被占用而失败。请先在「一键启动」页停止它。`);
    }
    if (!r.comfy && r.disable_all && r.disable_all !== "none") {
      msgs.push(`WebUI 里设置了「停用${r.disable_all === "all" ? "全部" : "所有第三方"}扩展」，装好的扩展暂时不会加载（WebUI 的 Extensions 页可以改回来）。`);
    }
    warn.textContent = msgs.join("\n");
    warn.hidden = !msgs.length;
  }

  async function refresh() {
    try {
      // 选中的目标实例被删了 → 回到当前实例
      const list = (App.instances && App.instances.instances) || [];
      if (targetId && !list.some((i) => i.id === targetId)) targetId = "";
      const r = await App.api.ext_list(targetId || null);
      const prevKind = last && last.target && last.target.id;
      last = r || {};
      items = (r && r.items) || [];
      others = (r && r.others) || [];
      // 换了目标实例：搜索结果的「已安装」标记不对了，清掉
      if (prevKind && last.target && prevKind !== last.target.id) { results = null; renderResults(); }
      // 后端每次读列表前都会按目录重新认一遍目标实例的类型。纠正的是当前实例时
      // instances.js 会收到 changed 事件并整页重载；别的实例在这里提示一下
      const t = last.target || {};
      if (t.kind_fixed && !t.active) {
        App.toast(`「${t.name}」按目录重新检测为 ${t.kind_label}，已自动更正`, "ok", 4000);
      }
      renderTarget(last);
      if (r && r.has_root === false) {
        $("#ext-list").innerHTML = '<div class="hint" style="padding:10px">先在「一键启动」页给这个实例设置正确的根目录，才能检测/安装</div>';
        updateButtons();
      } else {
        render();
      }
    } catch (e) { App.toast("读取插件状态失败：" + e.message, "error"); }
  }

  function findRepo(rel) {
    return allRepos().find((r) => r.rel === rel);
  }

  async function switchRepos(rels, target, what) {
    const comfy = !!(last && last.comfy);
    const ok = await App.confirm("确认" + (what || "更新"),
      (rels.length > 1 ? `将更新 ${rels.length} 个插件到最新版（固定了版本的会跳过）。` : `${rels[0]}：${what}`) +
      "\n\n切换前的版本会记下来 / 备份到启动器的 launcher_data\\version_backup，随时能回去；你改过的插件文件会先备份。" +
      (comfy ? "\n节点换版本后会自动重装依赖。" : "\n重启 WebUI 后生效。"), "开始");
    if (!ok) return;
    try {
      const r = await App.api.ext_switch(rels, target || null, targetId || null);
      if (r && r.ok === false) App.toast(r.error || "操作失败", "error", 6000);
    } catch (e) { App.toast("操作失败：" + e.message, "error"); }
  }

  /* 注册表版节点的「版本…」：注册表列出的版本号，选一个整包换过去 */
  function cnrVersionsHtml(r) {
    const cur = (r.cnr && r.cnr.version) || "";
    const head = `<div class="kv-table"><div class="k">当前</div><div class="v"><b>${cur ? "v" + App.esc(cur) : "版本未知"}</b>　注册表版（ComfyUI-Manager 装的）</div>` +
      `<div class="k">注册表 id</div><div class="v">${App.esc((r.cnr && r.cnr.id) || "")}</div></div>`;
    if (r.error || !(r.versions || []).length) {
      return head + `<div class="hint" data-status="bad">${App.esc(r.error || "注册表里没有这个节点的版本列表")}</div>`;
    }
    const latest = r.versions.find((v) => !v.deprecated) || r.versions[0];
    const st = latest.version === cur ? '<div class="hint" data-status="ok">已经是最新版</div>'
      : `<div class="hint" data-status="warn">最新是 v${App.esc(latest.version)}</div>`;
    return head + st + '<div class="ver-list">' + r.versions.map((v) => {
      const isCur = v.version === cur;
      return `<label class="radio-row ver-row${isCur ? " is-cur" : ""}"><input type="radio" name="ext-cnr-ver" value="${App.esc(v.version)}"${isCur ? " disabled" : ""}><span class="radio-dot"></span>` +
        `<span><span class="ver-name">v${App.esc(v.version)}</span>` +
        (isCur ? '<span class="ext-status">当前</span>' : "") + (v === latest && !isCur ? '<span class="ext-status ext-upd">最新</span>' : "") +
        (v.deprecated ? '<span class="ext-status ext-off">已弃用</span>' : "") +
        `<span class="ver-meta">${App.esc(v.date || "")}${v.changelog ? "　" + App.esc(v.changelog) : ""}</span></span></label>`;
    }).join("") + "</div>";
  }

  async function openVersions(rel, name) {
    let pick = null;
    let loaded = false;
    let waiting = true;
    let closed = false;
    let timer = null;
    let cnrPick = null;     // 注册表版：选中的版本号
    const v = await App.modal(`「${name}」的版本`,
      '<div class="hint">正在联网获取版本列表…（几个 GitHub 加速地址同时试，一般几秒）<span id="ext-ver-wait"></span></div>' +
      '<div class="hint">过程看「一键启动」页的运行日志；等太久可以点「关闭」，会同时中止联网。</div>', [
      { id: "switch", label: "切换到所选版本", kind: "primary" },
      { id: "cancel", label: "关闭" },
    ], {
      async onOpen(body) {
        const t0 = Date.now();
        timer = setInterval(() => {
          const el = body.querySelector("#ext-ver-wait");
          if (el) el.textContent = `　已等 ${Math.round((Date.now() - t0) / 1000)} 秒`;
        }, 1000);
        let r;
        try { r = await App.api.ext_repo_versions(rel, targetId || null); }
        catch (e) { r = { ok: false, error: e.message }; }
        waiting = false;
        clearInterval(timer);
        // 弹窗 DOM 是单例：关掉后 body 还在页面上，得自己记着关没关，不然会写进下一个弹窗
        if (closed) return;
        if (!r || r.ok === false) { body.innerHTML = `<div class="hint" data-status="bad">${App.esc((r && r.error) || "获取失败")}</div>`; return; }
        if (r.cnr) {
          body.innerHTML = cnrVersionsHtml(r);
          body.querySelectorAll('input[name="ext-cnr-ver"]').forEach((x) => x.addEventListener("change", () => {
            cnrPick = x.value;
            body.querySelectorAll(".ver-row").forEach((row) => row.classList.toggle("checked", row.contains(x)));
          }));
          loaded = true;
          return;
        }
        const l = r.local || {};
        if (!l.is_git) { body.innerHTML = '<div class="hint" data-status="bad">这个插件不是 git 装的，没有版本可选</div>'; return; }
        const head = `<div class="kv-table">` +
          `<div class="k">当前</div><div class="v"><b>${App.esc(l.tag || (l.branch ? l.branch + " @ " + l.short : "固定 " + (l.short || "")))}</b>　${App.esc(l.date || "")}　${App.esc(l.subject || "")}</div>` +
          `<div class="k">仓库</div><div class="v">${App.esc(l.remote || "")}</div>` +
          ((l.dirty || []).length ? `<div class="k">本地修改</div><div class="v">${l.dirty.length} 个文件（切换时会备份并尝试合并）</div>` : "") +
          `</div>`;
        if (!r.remote || !r.remote.ok) {
          body.innerHTML = head + `<div class="hint" data-status="bad">${App.esc((r.remote && r.remote.error) || "获取远程版本失败")}</div>` +
            ((r.history || []).length ? '<div id="ext-ver-pick"></div>' : "");
          if ((r.history || []).length) {
            pick = App.verPicker(body.querySelector("#ext-ver-pick"), { history: r.history, current: l.commit });
            loaded = true;
          }
          return;
        }
        const c = r.remote;
        const st = c.behind === 0 ? '<div class="hint" data-status="ok">已经是最新版</div>'
          : c.partial ? '<div class="hint" data-status="warn">有新版本。最近提交没拉下来，下面先列出版本标签和分支（网络好些时重开可看到提交列表）</div>'
          : `<div class="hint" data-status="warn">落后 ${c.behind == null ? "40+" : c.behind} 个提交</div>`;
        body.innerHTML = head + st + '<div id="ext-ver-pick" class="ext-ver-pick"></div>';
        pick = App.verPicker(body.querySelector("#ext-ver-pick"), Object.assign({}, c, { history: r.history }));
        loaded = true;
        refresh();
      },
      onClose() {
        closed = true;
        clearInterval(timer);
        // 还在联网就顺手中止，不然后台一直占着「插件任务」，别的操作都进不来
        if (waiting) App.api.ext_cancel().catch(() => {});
      },
    });
    if (v !== "switch") return;
    if (cnrPick) { switchRepos([rel], { kind: "cnr", version: cnrPick }, "换成 v" + cnrPick); return; }
    const t = loaded && pick && pick.selected();
    if (!t) { App.toast("先在列表里选一个版本", "error"); return; }
    switchRepos([rel], t, "切换到 " + (t.label || t.name || (t.commit || "").slice(0, 7)));
  }

  // 打开插件目录：后端打不开（目录不存在、没设根目录）时以前什么都不提示，看起来像按钮坏了
  async function openDir(rel) {
    try {
      const r = await App.api.ext_open_dir(rel || null, targetId || null);
      if (r && r.ok === false) App.toast(r.error || "打不开插件目录", "error", 6000);
    } catch (e) { App.toast("打不开插件目录：" + e.message, "error"); }
  }

  /* ---------- 安装新插件：搜索 / 按地址 ---------- */
  function fmtNum(n) {
    n = +n || 0;
    return n >= 10000 ? (n / 10000).toFixed(n >= 100000 ? 0 : 1) + " 万" : String(n);
  }

  function renderResults() {
    const box = $("#ext-results");
    if (!results) { box.innerHTML = ""; return; }
    if (results.url) {
      box.innerHTML = `<div class="ext-item ext-hit"><span class="ext-body"><span class="ext-name">${App.esc(results.folder || "")}</span>` +
        `<div class="ext-desc">按地址安装：${App.esc(results.url)}（git clone${last && last.comfy ? "，再用这个实例的 Python 装依赖" : ""}）</div></span>` +
        `<span class="ext-hit-btns"><button class="btn btn-xs btn-accent" data-act="url-install">安装</button></span></div>`;
      return;
    }
    const list = results.items || [];
    if (!list.length) {
      box.innerHTML = `<div class="hint">${App.esc(results.source || "")}里没搜到。换个关键词（英文名一般更准），或者直接粘贴插件的 GitHub 地址。</div>`;
      return;
    }
    box.innerHTML = `<div class="hint">${App.esc(results.source)}：${list.length} 个结果</div>` + list.map((it, i) => {
      const meta = [it.author, it.version ? "v" + it.version : "", it.downloads ? "下载 " + fmtNum(it.downloads) : "",
        it.stars ? "★ " + fmtNum(it.stars) : "", (it.tags || []).slice(0, 4).join(" / ")].filter(Boolean).join("　");
      return `<div class="ext-item ext-hit${it.installed ? " installed" : ""}"><span class="ext-body">` +
        `<span class="ext-name">${App.esc(it.name)}</span>${it.installed ? '<span class="ext-status">已安装</span>' : ""}` +
        (meta ? `<span class="ext-hit-meta">${App.esc(meta)}</span>` : "") +
        `<div class="ext-desc">${App.esc(it.desc || "")}</div></span>` +
        `<span class="ext-hit-btns">` +
        (it.url ? `<button class="btn btn-xs" data-act="hit-page" data-i="${i}" data-keep="1">主页</button>` : "") +
        `<button class="btn btn-xs btn-accent" data-act="hit-install" data-i="${i}"${it.installed ? " disabled data-keep=\"1\"" : ""}>${it.installed ? "已安装" : "安装"}</button>` +
        `</span></div>`;
    }).join("");
  }

  async function search() {
    const q = $("#ext-q").value.trim();
    if (!q) { App.toast("输入插件名字、关键词，或者粘贴 GitHub 地址", "error"); return; }
    const btn = $("#ext-q-go");
    btn.disabled = true;
    $("#ext-results").innerHTML = '<div class="hint">正在搜索…</div>';
    let r;
    try { r = await App.api.ext_search(q, targetId || null); } catch (e) { r = { ok: false, error: e.message }; }
    btn.disabled = installing;
    if (!r || r.ok === false) {
      results = null;
      $("#ext-results").innerHTML = `<div class="hint" data-status="bad">${App.esc((r && r.error) || "搜索失败")}</div>`;
      return;
    }
    results = r;
    renderResults();
    if (installing) setInstalling(true);
  }

  async function installHit(it) {
    const t = (last && last.target) || {};
    const comfy = !!(last && last.comfy);
    const ok = await App.confirm("安装插件",
      `把「${it.name}」装到：\n${t.name || ""}（${t.kind_label || ""}）\n${t.target_dir || ""}` +
      (comfy ? "\n\n从 Comfy 注册表下载（注册表下不了会改用 git 从源码仓库装），节点依赖会一起装好。" : "\n\n用 git clone 安装，重启 WebUI 后生效。") +
      "\n\n第三方插件的代码会在你电脑上运行，只装你信得过的。", "开始安装");
    if (!ok) return;
    try {
      const r = comfy && it.id
        ? await App.api.ext_install_registry(it.id, targetId || null, it.url || null, it.name)
        : await App.api.ext_install_url(it.url, targetId || null, it.name);
      if (r && r.ok === false) App.toast(r.error || "安装失败", "error", 6000);
    } catch (e) { App.toast("安装失败：" + e.message, "error"); }
  }

  App.pages.extensions = {
    init() {
      log = App.pageLog("ext", "插件管理");

      $("#ext-refresh").addEventListener("click", refresh);
      $("#ext-cancel").addEventListener("click", () => App.api.ext_cancel());
      $("#ext-target-sel").addEventListener("change", (e) => { targetId = e.target.value; refresh(); });
      $("#ext-open-dir").addEventListener("click", () => openDir(null));
      $("#ext-view").addEventListener("click", (e) => {
        const b = e.target.closest("[data-view]");
        if (!b || b.dataset.view === view) return;
        view = b.dataset.view;
        render();
      });
      $("#ext-filter").addEventListener("input", (e) => { filterText = e.target.value; render(); });
      $("#ext-q-go").addEventListener("click", search);
      $("#ext-q").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
      $("#ext-results").addEventListener("click", async (e) => {
        const b = e.target.closest("[data-act]");
        if (!b || !results) return;
        if (b.dataset.act === "hit-page") { App.api.open_url((results.items[+b.dataset.i] || {}).url); return; }
        if (installing) return;
        if (b.dataset.act === "url-install") installHit({ name: results.folder, url: results.url });
        else if (b.dataset.act === "hit-install") installHit(results.items[+b.dataset.i]);
      });
      $("#ext-pick-default").addEventListener("click", () => {
        const rec = new Set(items.filter((i) => i.default).map((i) => i.id));
        document.querySelectorAll('#ext-list input[type="checkbox"]:not(:disabled)').forEach((el) => {
          el.checked = rec.has(el.dataset.id);
        });
      });
      $("#ext-check-upd").addEventListener("click", async () => {
        try {
          const r = await App.api.ext_check_updates(targetId || null);
          if (r && r.ok === false) App.toast(r.error || "检查失败", "error", 6000);
        } catch (e) { App.toast("检查失败：" + e.message, "error"); }
      });
      $("#ext-update-all").addEventListener("click", () => {
        const rels = updatable().map((r) => r.rel);
        if (rels.length) switchRepos(rels, null, "全部更新");
      });

      // 插件行里的按钮：在 <label> 里面，必须拦住默认行为，否则点按钮会去勾复选框
      $("#ext-list").addEventListener("click", async (e) => {
        const b = e.target.closest("[data-act]");
        if (!b) return;
        e.preventDefault();
        e.stopPropagation();
        if (installing) return;
        const act = b.dataset.act;
        if (act === "open") openDir(b.dataset.rel);
        else if (act === "enable" || act === "disable") {
          const on = act === "enable";
          b.disabled = true;
          try {
            const r = await App.api.ext_toggle(b.dataset.rel, on, targetId || null);
            if (r && r.ok === false) App.toast(r.error || "操作失败", "error", 6000);
            else App.toast(`已${on ? "启用" : "停用"}，重启${last && last.comfy ? " ComfyUI" : " WebUI"} 后生效`, "ok", 4000);
          } catch (err) { App.toast("操作失败：" + err.message, "error"); }
          refresh();
        }
        else if (act === "uninstall") {
          const ok = await App.confirm("卸载插件",
            `卸载「${b.dataset.name}」？\n\n整个插件文件夹会放进回收站（想要回来可以在回收站还原），插件自己的设置 / 下载的模型如果放在它的文件夹里也会一起进回收站。` +
            `\n\n重启${last && last.comfy ? " ComfyUI" : " WebUI"} 后生效。`, "卸载", true);
          if (!ok) return;
          try {
            const r = await App.api.ext_uninstall(b.dataset.rel, targetId || null);
            if (r && r.ok === false) App.toast(r.error || "卸载失败", "error", 6000);
            else App.toast(`已卸载「${b.dataset.name}」（在回收站里）`, "ok", 4000);
          } catch (err) { App.toast("卸载失败：" + err.message, "error"); }
          refresh();
        }
        else if (act === "versions") openVersions(b.dataset.rel, b.dataset.name);
        else if (act === "update") {
          const r = findRepo(b.dataset.rel);
          const u = (r && r.update) || {};
          switchRepos([b.dataset.rel], null, r && r.cnr ? `更新到 v${u.latest_short || "最新"}` : `更新到最新（${u.latest_short || ""}）`);
        } else if (act === "replace") {
          const it = items.find((i) => i.id === b.dataset.id);
          const ok = await App.confirm("换成推荐版本",
            `「${it ? it.name : ""}」现在装的不是当前分支推荐的版本。\n\n旧版本的文件夹会整个挪到启动器的 launcher_data\\version_backup\\replaced\\ 下（想回去可以挪回来），然后安装推荐版本。` +
            "\n\nWebUI 正在运行的话请先停止。", "开始替换");
          if (!ok) return;
          try {
            const r = await App.api.ext_replace(b.dataset.id, targetId || null);
            if (r && r.ok === false) App.toast(r.error || "替换失败", "error", 6000);
          } catch (err) { App.toast("替换失败：" + err.message, "error"); }
          refresh();
        }
      });

      $("#ext-install").addEventListener("click", async () => {
        const ids = Array.from(document.querySelectorAll('#ext-list input[type="checkbox"]:checked'))
          .map((el) => el.dataset.id);
        if (!ids.length) { App.toast("先勾选要安装的项目", "error"); return; }
        const t = (last && last.target) || {};
        const ok = await App.confirm("确认安装",
          `将安装 ${ids.length} 项到：\n${t.name || ""}（${t.kind_label || ""}）\n${t.target_dir || ""}` +
          (last && last.comfy ? "\n\n节点的 Python 依赖会一起安装，视网速可能需要几分钟。" : ""),
          "开始安装");
        if (!ok) return;
        try {
          const r = await App.api.ext_install(ids, targetId || null);
          if (r && r.ok === false) App.toast(r.error || "安装失败", "error", 6000);
        } catch (e) { App.toast("安装失败：" + e.message, "error"); }
      });

      App.on("ext", "log", (e) => log(e.text));
      App.on("ext", "state", (e) => setInstalling(!!e.running));
      App.on("ext", "done", (e) => {
        setInstalling(false);
        refresh().then(() => {
          // 搜索结果里刚装好的那个标成「已安装」：重新搜一遍太慢，按列表里的文件夹名 / 注册表 id 对一下
          if (!results || !results.items) return;
          const ids = new Set(), keys = new Set();
          allRepos().forEach((r) => { keys.add(r.folder.toLowerCase()); if (r.cnr) ids.add(r.cnr.id.toLowerCase()); });
          results.items.forEach((it) => {
            const folder = String(it.url || "").replace(/\.git$/i, "").split("/").pop().toLowerCase();
            if ((it.id && (ids.has(it.id.toLowerCase()) || keys.has(it.id.toLowerCase()))) || (folder && keys.has(folder))) it.installed = true;
          });
          renderResults();
        });
        const failed = (e && e.failed) || [];
        if (e && e.error) App.toast("安装中途出错：" + e.error + "（详情见下方日志）", "error", 8000);
        else if (failed.length) App.toast(`${failed.length} 个没装上：${failed.join("、")}（原因见下方日志）`, "error", 8000);
      });
      // 批量检查更新时逐个回填，不用等全部查完
      App.on("ext", "update_status", (e) => {
        const r = findRepo(e.rel);
        if (r) { r.update = e.update || {}; render(); }
      });

      refresh();
    },
    onShow() { if (!installing) refresh(); },
  };
})();
