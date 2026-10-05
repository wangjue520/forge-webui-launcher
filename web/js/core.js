/* core.js — 应用核心：事件路由、页面导航、配置绑定、模态框、通知、日志、工具函数 */
(function () {
  "use strict";

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const App = {
    api: null,           // window.pywebview.api
    cfg: {},             // 当前配置
    cmdArgs: "",         // 当前命令行参数预览
    state: null,         // get_state() 完整结果
    handlers: {},        // 事件路由表：handlers[scope][type] = fn(evt)
    pages: {},           // 页面模块注册表
    ready: false,

    on(scope, type, fn) {
      (this.handlers[scope] = this.handlers[scope] || {})[type] = fn;
    },

    // 后端事件入口：window.evaluate_js("window.App && window.App.onEvent(<json>)")
    onEvent(evt) {
      try {
        const h = (App.handlers[evt.scope] || {})[evt.type];
        if (h) h(evt);
      } catch (e) {
        console.error("事件处理出错", evt, e);
      }
    },
  };
  window.App = App;

  /* ---------- 工具 ---------- */
  App.esc = function (s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  App.copy = async function (text, tip) {
    let done = false;
    try { await navigator.clipboard.writeText(text); done = true; } catch (e) { /* 兜底 */ }
    if (!done) {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { done = document.execCommand("copy"); } catch (e) { /* 忽略 */ }
      ta.remove();
    }
    App.toast(done ? (tip || "已复制到剪贴板") : "复制失败", done ? "ok" : "error");
  };

  App.toast = function (msg, kind, ms) {
    const box = $("#toasts");
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 350);
    }, ms || 3200);
  };

  App.fmtBytes = function (n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 2 : 1)) + " " + u[i];
  };

  /* ---------- 界面缩放（Ctrl+滚轮，全窗口生效，本地记住） ---------- */
  // 触摸板双指捏合在 Chromium 里就是 ctrl+wheel，一并覆盖。
  // 缩放挂在 documentElement 上对整个窗口（含所有页面）生效；
  // 每次缩放立即写 localStorage，下次启动直接恢复。
  const ZOOM_KEY = "ui-zoom", ZOOM_MIN = 0.5, ZOOM_MAX = 2.0, ZOOM_STEP = 0.1;
  App.zoom = 1;

  function applyZoom(z, save) {
    z = Math.round(Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z)) * 100) / 100;
    App.zoom = z;
    document.documentElement.style.zoom = z;
    if (save) { try { localStorage.setItem(ZOOM_KEY, String(z)); } catch (e) { /* 隐私模式等 */ } }
  }

  (function initZoom() {
    let saved = 1;
    try { saved = parseFloat(localStorage.getItem(ZOOM_KEY)) || 1; } catch (e) {}
    applyZoom(saved, false);

    // 触控板一次捏合会连发很多小 delta，攒到一格滚轮的量再跳一档
    let acc = 0;
    document.addEventListener("wheel", (e) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      acc += e.deltaY;
      if (Math.abs(acc) < 60) return;
      applyZoom(App.zoom + (acc > 0 ? -ZOOM_STEP : ZOOM_STEP), true);
      acc = 0;
    }, { passive: false });
  })();

  /* ---------- 模态框（Promise 化） ---------- */
  // buttons: [{id, label, kind: "primary"|"accent"|"danger"|""}] —— resolve(id)；Esc/无按钮时不自动关闭
  // 单例 DOM：新弹窗打开时，没关闭的旧弹窗按 "cancel" 自动完结——
  // 否则旧弹窗的 Promise 永远悬挂，后台 _ask 线程会一直等回答（部署卡死）
  let modalActiveFinish = null;
  App.modal = function (title, bodyHtml, buttons, opts) {
    opts = opts || {};
    if (modalActiveFinish) {
      const stale = modalActiveFinish;
      modalActiveFinish = null;
      try { stale("cancel"); } catch (e) { /* 已完结的忽略 */ }
    }
    return new Promise((resolve) => {
      const mask = $("#modal-mask");
      $("#modal-title").textContent = title;
      const body = $("#modal-body");
      body.innerHTML = bodyHtml;
      const btnBox = $("#modal-buttons");
      btnBox.innerHTML = "";
      let finished = false;
      const finish = (val) => {
        if (finished) return;
        finished = true;
        if (modalActiveFinish === finish) modalActiveFinish = null;
        mask.hidden = true;
        if (opts.onClose) opts.onClose(body, val);
        resolve(val);
      };
      modalActiveFinish = finish;
      (buttons || [{ id: "ok", label: "确定", kind: "primary" }]).forEach((b) => {
        const el = document.createElement("button");
        el.className = "btn" + (b.kind ? " btn-" + b.kind : "");
        el.textContent = b.label;
        el.addEventListener("click", () => finish(b.id));
        btnBox.appendChild(el);
      });
      mask.hidden = false;
      const first = btnBox.querySelector(".btn-primary, .btn-accent");
      if (first) first.focus();
      if (opts.onOpen) opts.onOpen(body, finish);
    });
  };

  App.confirm = function (title, text, okLabel, danger) {
    return App.modal(title, App.esc(text), [
      { id: "ok", label: okLabel || "确定", kind: danger ? "danger" : "primary" },
      { id: "cancel", label: "取消" },
    ]).then((v) => v === "ok");
  };

  /* ---------- 日志 ---------- */
  App.makeLogger = function (el, maxLines) {
    const max = maxLines || 800;
    let count = 0;
    return function (text) {
      const nearBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
      el.textContent += (count ? "\n" : "") + text;
      count++;
      if (count > max) {
        const lines = el.textContent.split("\n");
        el.textContent = lines.slice(lines.length - max).join("\n");
        count = max;
      }
      if (nearBottom) el.scrollTop = el.scrollHeight;
    };
  };

  /* ---------- 进度条 ---------- */
  App.progress = function (el) {
    const bar = el.querySelector(".progress-bar");
    const text = el.querySelector(".progress-text");
    return {
      show() { el.hidden = false; },
      hide() { el.hidden = true; },
      set(pct, label) {
        el.hidden = false;
        pct = Math.max(0, Math.min(100, pct || 0));
        bar.style.width = pct + "%";
        if (label != null) text.textContent = label;
      },
    };
  };

  /* ---------- 顶部加载条 ---------- */
  // 纯装饰。真实的加载现在是毫秒级，进度条如果如实反映，会是一道几乎看不见的
  // 闪光——反而让人怀疑"到底有没有在干活"。所以这里走一条固定 3 秒的节奏曲线。
  //
  // 让它像真的，关键是**别匀速**。匀速一眼就是假的。真实加载的观感是
  // 一顿一冲：握手很快 → 传大块时稳住 → 偶尔卡一下 → 收尾突然窜完。
  // 下面用一串不等距的关键点描述这个节奏，段与段之间速度差好几倍，
  // 再给每段配不同的缓动，加一点随机抖动让每次都不完全一样。
  App.topbar = (function () {
    const el = () => $("#loadbar");
    const fill = () => $("#loadbar .topbar-fill");

    // [到达时刻(ms), 目标百分比, 缓动]
    //  out  = 先快后慢（冲一下然后顶住，像连上了但还在等数据）
    //  in   = 先慢后快（憋一会儿然后窜出去）
    //  lin  = 匀速（长时间稳定传输）
    const KEYS = [
      [0,    0,  "lin"],
      [140,  22, "out"],   // 开头猛冲一下：握手总是很快
      [430,  27, "out"],   // 立刻顶住，这个反差是"真实感"的主要来源
      [760,  31, "lin"],   // 磨一会儿
      [1150, 55, "in"],    // 窜一大截
      [1420, 58, "out"],   // 又卡住
      [1900, 71, "lin"],
      [2180, 74, "out"],   // 再顿一下
      [2600, 90, "in"],    // 收尾前的冲刺
      [3000, 97, "out"],   // 停在 97，剩下 3% 留给 done()
    ];

    const EASE = {
      lin: (t) => t,
      out: (t) => 1 - Math.pow(1 - t, 2.2),
      in: (t) => Math.pow(t, 2.0),
    };

    let raf = 0, t0 = 0, cur = 0, jitter = [], finishing = false;

    function setPct(p) {
      cur = p;
      const f = fill();
      if (f) f.style.width = p.toFixed(2) + "%";
    }

    function valueAt(ms) {
      for (let i = 1; i < KEYS.length; i++) {
        const [t1, p1] = KEYS[i], [t0k, p0] = KEYS[i - 1];
        if (ms <= t1) {
          const k = (ms - t0k) / (t1 - t0k || 1);
          return p0 + (p1 - p0) * EASE[KEYS[i][2]](k) + (jitter[i] || 0);
        }
      }
      return KEYS[KEYS.length - 1][1];
    }

    function tick() {
      const ms = performance.now() - t0;
      setPct(Math.min(99.4, valueAt(ms)));
      if (ms < KEYS[KEYS.length - 1][0]) {
        raf = requestAnimationFrame(tick);
      } else {
        // 曲线跑完还没人收尾：以越来越慢的速度朝 99.4 蠕动。
        // 这是真实加载条最有说服力的一个细节——它永远不会自己到头。
        raf = requestAnimationFrame(function creep() {
          setPct(cur + (99.4 - cur) * 0.012);
          raf = requestAnimationFrame(creep);
        });
      }
    }

    function start() {
      cancelAnimationFrame(raf);
      finishing = false;
      // 每次给关键点加 ±1.2% 的随机偏移，避免两次播放一模一样
      jitter = KEYS.map((_, i) => (i === 0 || i === KEYS.length - 1) ? 0 : (Math.random() - 0.5) * 2.4);
      const box = el();
      if (!box) return;
      box.classList.add("on");
      setPct(0);
      t0 = performance.now();
      raf = requestAnimationFrame(tick);
    }

    function done() {
      if (finishing) return;
      finishing = true;
      cancelAnimationFrame(raf);
      const box = el();
      if (!box) return;
      // 收尾要快：最后一段猛地窜到 100，然后整条淡出
      const from = cur, s = performance.now();
      (function rush() {
        const k = Math.min(1, (performance.now() - s) / 200);
        setPct(from + (100 - from) * (1 - Math.pow(1 - k, 3)));
        if (k < 1) { requestAnimationFrame(rush); return; }
        box.classList.remove("on");
        setTimeout(() => { if (finishing) setPct(0); }, 320);
      })();
    }

    // 装饰用：完整播完 3 秒节奏再收尾
    function run(ms) {
      start();
      setTimeout(done, ms || 3000);
    }

    return { start, done, run };
  })();

  /* ---------- 全局状态 pill ---------- */
  App.setGlobalStatus = function (state, text) {
    const pill = $("#global-status");
    pill.dataset.state = state;
    $("#global-status-text").textContent = text;
  };

  /* ---------- 页面导航 ---------- */
  const PAGE_TITLES = {
    launch: "一键启动",
    instances: "实例管理",
    settings: "高级选项",
    launcher: "启动器设置",
    deploy: "环境部署",
    civitai: "模型下载 · Civitai / liblib",
    models: "模型管理",
    outputs: "输出管理",
    extensions: "常用插件",
    wd14: "WD14 标签反推",
    meta: "图片信息",
  };

  // 页头上方的英文编号（终末地风格的等宽标注）
  const PAGE_CODES = {
    launch: "LAUNCH", instances: "INSTANCES", settings: "SETTINGS", launcher: "LAUNCHER", deploy: "DEPLOY",
    civitai: "DOWNLOAD", models: "MODELS", outputs: "OUTPUTS", extensions: "EXTENSIONS",
    wd14: "WD14 TAGGER", meta: "IMAGE META",
  };

  App.showPage = function (name) {
    const prev = App.currentPage;
    if (prev && prev !== name && App.pages[prev] && App.pages[prev].onHide) {
      try { App.pages[prev].onHide(); } catch (e) { console.error(e); }
    }
    App.currentPage = name;
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === name));
    $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
    $("#page-title").textContent = PAGE_TITLES[name] || name;
    const code = $("#page-code");
    if (code) {
      // 编号跟侧栏上看到的一致（隐藏的页面不占号）
      const visible = $$(".nav-item").filter((b) => !b.hidden).map((b) => b.dataset.page);
      const idx = visible.indexOf(name);
      code.textContent = String(idx + 1).padStart(2, "0") + " // " + (PAGE_CODES[name] || name.toUpperCase());
    }
    const mod = App.pages[name];
    if (mod && mod.onShow) mod.onShow();
  };

  function initNav() {
    $$(".nav-item").forEach((b) =>
      b.addEventListener("click", () => App.showPage(b.dataset.page)));
  }

  /* ---------- 配置绑定（data-cfg 控件） ---------- */
  function controlValue(el) {
    if (el.type === "checkbox") return el.checked;
    return el.value;
  }
  function setControlValue(el, v) {
    if (el.type === "checkbox") el.checked = !!v;
    else el.value = v == null ? "" : String(v);
  }
  App.setControlValue = setControlValue;

  function bindConfigControls() {
    $$("[data-cfg]").forEach((el) => {
      // 高级选项页的控件是按实例分别保存的，由 settings.js 自己绑定
      // （可以编辑非当前实例），不走这里的全局保存
      if (el.closest("#page-settings")) return;
      const key = el.dataset.cfg;
      el.addEventListener("change", async () => {
        App.cfg[key] = controlValue(el);
        try {
          const r = await App.api.update_config({ [key]: App.cfg[key] });
          if (r && r.cmd_args != null) App.refreshCmdPreview(r.cmd_args);
        } catch (e) {
          console.error(e);
          App.toast("配置保存失败：" + e.message, "error");
        }
      });
    });
  }

  App.refreshConfigControls = function () {
    $$("[data-cfg]").forEach((el) => setControlValue(el, App.cfg[el.dataset.cfg]));
  };

  App.refreshCmdPreview = function (cmd) {
    App.cmdArgs = cmd || "";
    $("#launch-cmd-preview").textContent = cmd && cmd.trim() ? cmd : "（无）";
  };

  /* ---------- 文件拖放 ----------
   * 网页拿不到拖入文件的磁盘路径。WebView2 下把 File 列表交给原生侧
   * （postMessageWithAdditionalObjects），pywebview 会记下每个文件的完整路径，
   * 再按文件名向 Python 要回来（api.drop_files）。
   * 不再用 pywebview 的 DOM 事件转发：它会把整个事件连同整棵 DOM 树序列化，
   * 拖动时每次 dragover 都要几百毫秒、几 MB，界面直接卡死。 */
  function bindFileDrop() {
    // ---- 临时诊断：定位拖放问题用，定位完删掉 ----
    const dbg = (m) => { try { App.api.drop_debug(m); } catch (e) {} };
    let dragSeen = false;
    // 捕获阶段、不做任何过滤的原始记录：区分「事件根本没到」和「被 hasFiles 过滤」
    let overLogged = false;
    ["dragenter", "dragover", "drop"].forEach((ev) =>
      window.addEventListener(ev, (e) => {
        if (ev === "dragenter") overLogged = false;
        if (ev === "dragover") { if (overLogged) return; overLogged = true; }
        dbg(`原始 ${ev} types=${e.dataTransfer ? Array.from(e.dataTransfer.types || []).join(",") : "无 dataTransfer"}`);
      }, true));
    const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
    const allow = (e) => {
      if (!hasFiles(e)) return;
      if (!dragSeen) { dragSeen = true; dbg(`dragenter page=${App.currentPage} types=${Array.from(e.dataTransfer.types || []).join(",")}`); }
      e.preventDefault();                       // 不 preventDefault 浏览器就不允许放下（还会把文件当网页打开）
      e.dataTransfer.dropEffect = App.pages[App.currentPage] && App.pages[App.currentPage].onDropped ? "copy" : "none";
    };
    document.addEventListener("dragenter", allow);
    document.addEventListener("dragover", allow);
    document.addEventListener("drop", async (e) => {
      const wasDragging = dragSeen; dragSeen = false;
      if (!hasFiles(e)) { if (wasDragging) dbg("drop 事件但 hasFiles=false"); return; }
      e.preventDefault();
      const files = e.dataTransfer.files;
      dbg(`drop page=${App.currentPage} files=${files ? files.length : "null"} names=${files ? Array.from(files, (f) => f.name).join("|") : ""}`);
      if (!files || !files.length) return;
      const mod = App.pages[App.currentPage];
      if (!mod || !mod.onDropped) { App.toast("这个页面不接收拖入的文件", ""); return; }
      // 页面侧反馈一旦抛错，后面路径查询和错误提示就全没了——包起来让错误可见
      try { if (mod.onDropStart) mod.onDropStart(files.length); }
      catch (err) { dbg("onDropStart 抛错: " + err.message); App.toast("拖放处理出错：" + err.message, "error", 5000); }
      const wv = window.chrome && window.chrome.webview;
      dbg(`chrome.webview=${!!wv} postMessageWithAdditionalObjects=${!!(wv && wv.postMessageWithAdditionalObjects)}`);
      if (wv && wv.postMessageWithAdditionalObjects) {
        // FileList 按 WebIDL 转成 sequence 有兼容风险，显式转成数组再发
        try { wv.postMessageWithAdditionalObjects("FilesDropped", Array.from(files)); dbg("postMessageWithAdditionalObjects 已发"); }
        catch (err) { dbg("postMessageWithAdditionalObjects 抛错: " + err.message); }
      }
      const names = Array.from(files, (f) => f.name);
      let r = null;
      try { r = await App.api.drop_files(names); } catch (err) { r = { ok: false, error: err.message }; }
      dbg(`drop_files 返回 ${JSON.stringify(r)}`);
      if (r && r.legacy) {                      // 旧通道：路径稍后通过 app/dropped 送过来，进度由页面自己再显示
        if (mod.onDropFail) mod.onDropFail();
        return;
      }
      const paths = (r && r.paths) || [];
      if (!paths.length) {
        if (mod.onDropFail) mod.onDropFail();
        App.toast("没能读到拖入文件的路径" + (r && r.error ? "：" + r.error : "（请从资源管理器里直接拖文件或文件夹）"), "error", 5000);
        return;
      }
      dbg(`onDropped paths=${paths.join("|")}`);
      mod.onDropped(paths);
    });
  }

  /* ---------- 全局事件 ---------- */
  function registerGlobalEvents() {
    App.on("app", "error", (e) => App.toast(e.text || "发生未知错误", "error", 5000));

    // 文件拖放的兜底通道：pywebview 内部结构变了时，Python 侧用旧的 DOM 事件拿路径再推到这里
    App.on("app", "dropped", (e) => {
      const mod = App.pages[App.currentPage];
      if (mod && mod.onDropped) mod.onDropped(e.paths || []);
    });
    bindFileDrop();

    App.on("app", "shortcuts_created", () =>
      App.toast("已在桌面和启动器文件夹里创建「WWY 启动器」快捷方式，以后双击它就能打开", "ok", 6000));

    // 根目录为空/失效（新用户、启动器从别的电脑复制过来）时后端会自动找本机的安装并选上
    App.on("app", "installs_detected", (e) => {
      const more = e.count > 1 ? `（一共找到 ${e.count} 个，可在启动页切换）` : "";
      App.toast(`已自动选择这台电脑上的 ${e.label}：${e.picked}${more}`, "ok", 8000);
      setTimeout(() => {
        try { sessionStorage.setItem("skip-splash", "1"); sessionStorage.setItem("return-page", App.currentPage || "launch"); } catch (err) {}
        location.reload();
      }, 1500);
    });

    App.on("app", "confirm_exit", async (e) => {
      // 后端会带上正在运行的实例名单；没有（旧后端）就从本地状态算
      let names = (e && e.names) || [];
      if (!names.length) {
        names = ((App.instances && App.instances.instances) || [])
          .filter((i) => i.status && i.status.running)
          .map((i) => i.name).filter(Boolean);
      }
      const what = names.length ? names.map((n) => "「" + n + "」").join("、") : "当前实例";
      const v = await App.modal(
        names.length > 1 ? "多个实例正在运行" : `${what}正在运行`,
        `${what}还在后台运行中。直接退出启动器而不结束，会造成端口被占用、显存不释放等问题。`,
        [
          { id: "kill", label: names.length > 1 ? "全部结束并退出" : "结束并退出", kind: "danger" },
          { id: "exit", label: "不结束，直接退出" },
          { id: "cancel", label: "取消" },
        ]
      );
      if (v === "cancel") return;
      try { await App.api.exit_app(v === "kill"); } catch (e) { console.error(e); }
    });
  }

  /* ---------- 启动 ---------- */
  let bootStarted = false;  // pywebviewready 与 400ms 兜底可能先后触发，boot 必须幂等
  async function boot() {
    if (bootStarted) return;
    bootStarted = true;
    App.api = window.pywebview.api;
    initNav();
    bindConfigControls();
    registerGlobalEvents();

    try {
      App.state = await App.api.get_state();
    } catch (e) {
      bootStarted = false;  // 初始化失败时允许兜底逻辑重试
      App.toast("后端初始化失败：" + e.message, "error", 8000);
      console.error(e);
      if (window.Splash) window.Splash.ready();
      return;
    }
    App.cfg = App.state.config || {};
    if (App.state.new_machine) {
      let shown = false;
      try { shown = sessionStorage.getItem("new-machine-toast") === "1"; sessionStorage.setItem("new-machine-toast", "1"); } catch (e) {}
      if (!shown) App.toast("检测到启动器是从别的电脑复制过来的：已清除原电脑上的目录、模型库和 Civitai/liblib 登录信息，"
        + "界面设置保留。正在这台电脑上查找 WebUI / ComfyUI …", "ok", 10000);
    }
    App.refreshConfigControls();
    App.refreshCmdPreview(App.state.cmd_args);
    // 界面风格：后端配置为准，同步进 localStorage（开屏动画下次启动读它）
    if (window.WWYThemes) window.WWYThemes.apply(App.cfg.ui_theme || "terminal", true);

    // 初始化各页面模块
    for (const [name, mod] of Object.entries(App.pages)) {
      try { if (mod.init) await mod.init(App.state); }
      catch (e) { console.error("页面初始化失败: " + name, e); }
    }

    // 内置 HTTP 服务器偶发丢请求（见 webview_main.py 的 backlog 补丁），个别
    // js 没加载上时对应页面的按钮会完全没反应。缺模块就自动刷新一次——此时
    // 浏览器缓存已热，第二次几乎必好；用 sessionStorage 保证只刷一次不死循环
    const EXPECTED_PAGES = ["launch", "settings", "launcher", "deploy", "civitai", "models", "extensions", "wd14", "meta",
                            "instances", "outputs"];
    const missing = EXPECTED_PAGES.filter((p) => !App.pages[p]);
    if (missing.length) {
      console.error("页面模块未加载完整，自动刷新一次:", missing);
      if (!sessionStorage.getItem("reloaded-for-missing-modules")) {
        sessionStorage.setItem("reloaded-for-missing-modules", "1");
        location.reload();
        return;
      }
      App.toast("部分页面加载失败（" + missing.join("、") + "），建议重启启动器", "error", 8000);
    }
    sessionStorage.removeItem("reloaded-for-missing-modules");

    if (App.applyChrome) App.applyChrome(App.state);
    // 切换实例后会重载页面，回到切换前所在的页面
    let startPage = new URLSearchParams(location.search).get("page");
    try { startPage = sessionStorage.getItem("return-page") || startPage; sessionStorage.removeItem("return-page"); } catch (e) {}
    const navBtn = $(`.nav-item[data-page="${startPage}"]`);
    App.showPage(PAGE_TITLES[startPage] && navBtn && !navBtn.hidden ? startPage : "launch");
    App.ready = true;
    if (window.Splash) window.Splash.ready();
  }

  window.addEventListener("pywebviewready", boot);
  // 预览/mock 环境兜底
  window.addEventListener("DOMContentLoaded", () => {
    setTimeout(() => { if (!App.ready && window.pywebview && !App.api) boot(); }, 400);
  });
})();
