/* link.js — 局域网 / 广域网联动
 *
 * 「启动器设置」页的联动卡片：本机当主机（开关、密钥、端口）、输入密钥连接远程主机。
 * 「一键启动」页的远程实例卡片：连过的主机在不在线、上面的实例运行到哪了，点「控制」进入远程模式。
 * 远程模式（core.js 把 App.api 换成转发到主机的代理）：侧栏提示正在控制哪台、返回本机，
 * 上传 / 下载到本机的进度条，主机那边启动好的 WebUI 在这台电脑的浏览器里打开。
 */
(function () {
  "use strict";
  const App = window.App;
  const $ = (s) => document.querySelector(s);

  const STATE_TEXT = { idle: "未启动", starting: "启动中", ready: "运行中", running: "运行中", stopped: "已停止", error: "出错" };
  let info = null;            // link_info() 的结果
  let peerStatus = {};        // link_peers_status() 的结果：pid -> {online, name, instances…}
  let statusBusy = false;

  function reloadKeepPage() {
    try { sessionStorage.setItem("return-page", App.currentPage || "launch"); } catch (e) {}
    location.reload();
  }

  async function enterRemote(pid) {
    App.toast("正在连接主机…", "", 2500);
    let r;
    try { r = await App.api.link_enter(pid); } catch (e) { r = { ok: false, error: e.message }; }
    if (!r || !r.ok) { App.toast("连不上：" + ((r && r.error) || "未知错误"), "error", 7000); return; }
    try { sessionStorage.setItem("return-page", "launch"); } catch (e) {}
    location.reload();
  }

  async function leaveRemote() {
    try { await window.pywebview.api.link_leave(); } catch (e) { /* 照样刷新 */ }
    reloadKeepPage();
  }

  /* ---------- 启动器设置页：联动卡片 ---------- */
  function renderHost() {
    const h = info.host;
    $("#link-host-on").checked = h.enabled;
    $("#link-host-box").hidden = !h.enabled;
    $("#link-key").textContent = h.key || "—";
    if (document.activeElement !== $("#link-port")) $("#link-port").value = h.port;
    $("#link-ips").textContent = h.ips.length ? h.ips.map((ip) => `${ip}:${h.port}`).join("　") : "没找到局域网地址";
    $("#link-clients").textContent = h.clients.length
      ? h.clients.map((c) => (c.name ? `${c.name}（${c.ip}）` : c.ip)).join("、") : "暂无";
    let err = h.error || "";
    if (!err && h.enabled && h.running && !h.discovery) err = "局域网发现用的 UDP 7898 端口被占用了：远程连接时需要手动填这台的地址";
    $("#link-host-err").hidden = !err;
    $("#link-host-err").textContent = err;
  }

  function renderPeers() {
    const box = $("#link-peers");
    box.innerHTML = info.peers.map((p) => `
      <div class="link-peer" data-pid="${App.esc(p.id)}">
        <b>${App.esc(p.name)}</b><span class="lp-addr">${App.esc(p.addr)}:${p.port}</span>
        <span class="ic-btns">
          <button class="btn btn-xs btn-primary" data-act="control">控制</button>
          <button class="btn btn-xs" data-act="remove">删除</button>
        </span>
      </div>`).join("");
    box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () =>
      peerAction(b.dataset.act, b.closest("[data-pid]").dataset.pid)));
  }

  async function peerAction(act, pid) {
    if (act === "control") return enterRemote(pid);
    if (act === "remove") {
      const p = info.peers.find((x) => x.id === pid);
      if (!(await App.confirm("删除远程主机", `不再记住「${p ? p.name : pid}」？以后要再输入一次密钥才能连。`, "删除", true))) return;
      apply(await App.api.link_peer_remove(pid));
      delete peerStatus[pid];
      renderRemoteCards();
    }
  }

  function apply(r) {
    if (!r) return;
    if (r.host) { info = r; renderHost(); renderPeers(); renderRemoteCards(); }
    if (r.ok === false && r.error) App.toast(r.error, "error", 7000);
  }

  async function loadInfo() {
    try { apply(await App.api.link_info()); } catch (e) { console.error(e); }
  }

  async function connect() {
    const key = $("#link-in-key").value.trim();
    const addr = $("#link-in-addr").value.trim();
    if (!key) { App.toast("先填主机上显示的密钥", "error"); return; }
    const btn = $("#link-connect");
    btn.disabled = true;
    $("#link-connect-hint").textContent = addr ? "正在连接…" : "正在局域网里查找主机…";
    let r;
    try { r = await App.api.link_connect(key, addr); } catch (e) { r = { ok: false, error: e.message }; }
    btn.disabled = false;
    $("#link-connect-hint").textContent = "";
    if (!r || !r.ok) { App.toast((r && r.error) || "连接失败", "error", 9000); return; }
    apply(r);
    $("#link-in-key").value = "";
    if (r.warn) App.toast(r.warn, "", 8000);
    refreshPeerStatus();
    const v = await App.modal("已连接", `已连接到主机「${App.esc(r.peer.name)}」（${App.esc(r.peer.addr)}:${r.peer.port}）。<br>` +
      "以后在「一键启动」页的远程实例卡片上点「控制」就能操作它。现在就切过去吗？",
      [{ id: "go", label: "控制这台主机", kind: "primary" }, { id: "later", label: "稍后" }]);
    if (v === "go") enterRemote(r.peer.id);
  }

  /* ---------- 一键启动页：远程实例卡片 ---------- */
  function pill(st) {
    const s = (st && st.state) || "idle";
    return `<span class="status-pill" data-state="${s}"><span class="dot"></span><span>${STATE_TEXT[s] || s}</span></span>`;
  }

  function renderRemoteCards() {
    const box = $("#launch-remote");
    if (!box || !info) return;
    if (!info.peers.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    box.innerHTML = info.peers.map((p) => {
      const st = peerStatus[p.id];
      const online = st && st.online;
      const insts = (online && st.instances) || [];
      const rows = insts.map((i) => `
        <div class="ic-inst">
          <span class="ic-kind" data-kind="${i.kind}">${App.esc(i.kind_label)}</span>
          <span title="${App.esc(i.name)}">${App.esc(i.name)}</span>
          ${pill(i.status)}
          ${i.status && i.status.url ? `<button class="btn btn-xs" data-act="open" data-url="${App.esc(i.status.url)}">打开</button>` : ""}
        </div>`).join("");
      const stText = !st ? "检查中…" : online ? "在线" : "离线";
      return `
      <div class="inst-card remote" data-pid="${App.esc(p.id)}">
        <div class="ic-head">
          <span class="ic-kind">远程</span>
          <b class="ic-name">${App.esc((st && st.name) || p.name)}</b>
          <em class="ic-cur">${stText}</em>
        </div>
        <div class="ic-root" title="${App.esc(st && st.error ? st.error : "")}">${App.esc(p.addr)}:${p.port}${st && !online && st.error ? " · " + App.esc(st.error) : ""}</div>
        ${rows ? `<div class="ic-insts">${rows}</div>` : ""}
        <div class="ic-foot">
          <span class="ic-btns"><button class="btn btn-xs btn-primary" data-act="control"${st && !online ? " disabled" : ""}>控制这台主机</button></span>
        </div>
      </div>`;
    }).join("") + `<div class="inst-card remote-add" data-act="add">＋ 连接远程主机</div>`;
    box.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => {
      const act = b.dataset.act;
      if (act === "add") { App.showPage("launcher"); setTimeout(() => $("#link-in-key").focus(), 50); return; }
      if (act === "open") { App.api.open_url(b.dataset.url); return; }
      if (act === "control") enterRemote(b.closest("[data-pid]").dataset.pid);
    }));
  }

  async function refreshPeerStatus() {
    if (statusBusy || App.remote || !info || !info.peers.length) return;
    statusBusy = true;
    try {
      const r = await App.api.link_peers_status();
      if (r && r.status) { peerStatus = r.status; renderRemoteCards(); }
    } catch (e) { console.error(e); }
    statusBusy = false;
  }

  /* ---------- 远程模式 ---------- */
  function applyRemoteChrome() {
    const m = App.remote;
    $("#link-banner").hidden = !m;
    if (!m || !m.peer) return;
    $("#link-banner-name").textContent = `${m.peer.name}（${m.peer.addr}）`;
    $("#link-banner-dot").dataset.state = "ready";
    document.title = `WWY 启动器 · 远程控制 ${m.peer.name}`;
  }

  function showXfer(e) {
    const box = $("#link-xfer");
    box.hidden = false;
    $("#link-xfer-title").textContent = e.dir === "up" ? "上传到主机" : "下载到本机";
    $("#link-xfer-bar").style.width = Math.max(0, Math.min(100, e.pct || 0)) + "%";
    $("#link-xfer-text").textContent = `${e.i}/${e.n}  ${e.name}  ${Math.round(e.pct || 0)}%`;
  }

  async function xferDone(e) {
    $("#link-xfer").hidden = true;
    if (e.dir === "up") {
      if (!e.ok) App.toast("上传到主机失败：" + (e.error || ""), "error", 8000);
      return;
    }
    if (e.cancelled) { App.toast("已取消下载", ""); return; }
    const fails = e.failed || [];
    const body = `已下载 ${e.done} 个文件到：<br><code>${App.esc(e.dest)}</code>` +
      (fails.length ? `<br><br>${fails.length} 个失败：<br>` + fails.slice(0, 8).map((f) => App.esc(`${f.name}：${f.error}`)).join("<br>") : "");
    const v = await App.modal(fails.length ? "下载完成（有失败）" : "下载完成", body,
      [{ id: "open", label: "打开文件夹", kind: "primary" }, { id: "ok", label: "关闭" }]);
    if (v === "open") { try { await window.pywebview.api.reveal_in_explorer(e.dest); } catch (err) { /* 忽略 */ } }
  }

  function bindEvents() {
    App.on("link", "transfer", showXfer);
    App.on("link", "transfer_done", xferDone);
    App.on("link", "status", (e) => {
      if (!App.remote) return;
      $("#link-banner-dot").dataset.state = e.online ? "ready" : "offline";
      if (e.online) App.toast("已连上主机", "ok");
      else App.toast("和主机的连接断了，正在重连…" + (e.text ? "\n" + e.text : ""), "error", 6000);
    });
    App.on("link", "host_restarted", () => {
      if (!App.remote) return;
      App.toast("主机的启动器重启过了，正在刷新界面…", "", 3000);
      setTimeout(reloadKeepPage, 1200);
    });
    // 远程这边点的启动：主机启动好之后在这台电脑的浏览器里打开（地址已经换成本机转发端口）
    App.on("launch", "open_remote", (e) => { if (App.remote && e.url) App.api.open_url(e.url); });
  }

  App.pages.link = {
    async init() {
      bindEvents();
      applyRemoteChrome();
      $("#link-leave").addEventListener("click", leaveRemote);
      $("#link-xfer-cancel").addEventListener("click", () => App.api.link_transfer_cancel());
      let left = "";
      try { left = sessionStorage.getItem("link-left") || ""; sessionStorage.removeItem("link-left"); } catch (e) {}
      if (left) App.toast(left, "error", 9000);
      if (App.remote || !App.api.link_info) return;

      $("#link-host-on").addEventListener("change", async (e) => {
        const on = e.target.checked;
        apply(await App.api.link_host_set(on, null));
        if (on && info && info.host.running) App.toast("联动已打开。Windows 弹出防火墙提示时请点「允许」", "ok", 6000);
      });
      $("#link-key-copy").addEventListener("click", () => info && info.host.key && App.copy(info.host.key, "密钥已复制"));
      $("#link-key-regen").addEventListener("click", async () => {
        if (!(await App.confirm("换新密钥", "换了之后，之前连过这台的电脑都要重新输入新密钥。继续？", "换新密钥"))) return;
        apply(await App.api.link_host_regen());
      });
      $("#link-port-save").addEventListener("click", async () => {
        apply(await App.api.link_host_set(null, $("#link-port").value.trim()));
      });
      $("#link-firewall").addEventListener("click", async () => {
        const r = await App.api.link_firewall();
        App.toast(r && r.ok ? "已在 Windows 防火墙里放行联动端口" : "放行失败：" + ((r && r.error) || ""), r && r.ok ? "ok" : "error", 6000);
      });
      $("#link-connect").addEventListener("click", connect);
      $("#link-in-key").addEventListener("keydown", (e) => { if (e.key === "Enter") connect(); });
      $("#link-in-addr").addEventListener("keydown", (e) => { if (e.key === "Enter") connect(); });

      await loadInfo();
      refreshPeerStatus();
      // 一键启动页开着时每 8 秒看一眼远程主机；设置页开着时刷新「已连接」的远程列表
      setInterval(() => {
        if (document.hidden) return;
        if (App.currentPage === "launch") refreshPeerStatus();
        else if (App.currentPage === "launcher") loadInfo();
      }, 8000);
    },
  };
})();
