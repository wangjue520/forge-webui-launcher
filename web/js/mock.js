/* mock.js — 演示数据桩，仅在显式预览（地址带 ?mock=1）且没有 pywebview 时生效。

   为什么不能靠"加载时没有 window.pywebview"就自动启用：
   pywebview 的桥接对象是异步注入的，注入比页面脚本慢半拍的机器上，
   mock 会抢先接管整个界面，把假数据当成真实检测结果展示（真实案例：
   一台没有 F 盘的电脑上显示"F:\ 下已检测到 WebUI 安装"）。
   所以正式运行时这里什么都不做——真连不上后端只给提示条，绝不喂假数据。 */
(function () {
  if (window.pywebview) return;

  function showBanner(text) {
    const bar = document.createElement("div");
    bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:99999;" +
      "background:#7f1d1d;color:#fecaca;padding:8px 14px;font-size:13px;" +
      "text-align:center;line-height:1.5;box-shadow:0 -2px 8px rgba(0,0,0,.4);";
    bar.textContent = text;
    document.body.appendChild(bar);
  }

  if (!/[?&]mock=1\b/.test(location.search)) {
    // 非预览：2 秒后桥接还没来才是真失败（桥接注入本身慢一点很正常）
    window.addEventListener("DOMContentLoaded", () => {
      setTimeout(() => {
        if (window.pywebview) return;
        showBanner("未能连接到启动器后端（pywebview 桥接未建立）。请关闭本窗口，" +
                   "通过 启动WWY启动器.bat 重新打开；反复出现请检查 WebView2 运行时是否正常。");
      }, 2000);
    });
    return;
  }

  const ok = (v) => new Promise((r) => setTimeout(() => r(v), 60));

  const mockConfig = {
    ui_theme: (/[?&]theme=([\w-]+)/.exec(location.search) || [])[1] || "terminal",
    webui_root: "",
    custom_python_path: "",
    custom_git_path: "",
    webui_branch: "comfyui",
    use_portable_env: true,
    gpu_device_id: "",
    vram_mode: "auto",
    precision_mode: "auto",
    always_offload_from_vram: false,
    cuda_malloc: false,
    install_xformers: false,
    install_flash: false,
    install_sage: false,
    enable_listen: false,
    enable_api: false,
    enable_share: false,
    enable_insecure_extension_access: false,
    autolaunch: false,
    auto_open_browser_on_ready: true,
    skip_python_version_check: false,
    no_hashing: false,
    theme: "auto",
    port: "",
    extra_args: "",
    info_reserved_vram_gb: 4,
    info_shared_vram_fallback: false,
    info_model_hash_calc: true,
    civitai_api_key: "",
    civitai_last_dir: "",
    liblib_token: "",
    mirror_mode: "auto",
    mirror_detected: false,
    github_mirror_detected: false,
    reserve_vram_gb: "",
    unet_precision: "auto",
    vae_precision: "auto",
    text_enc_precision: "auto",
    attention_impl: "auto",
    fast_fp16: false,
    pin_shared_memory: false,
    expandable_segments: false,
  };

  const schema = {
    vram_legacy: [
      { label: "自动（默认）", key: "auto" },
      { label: "高显存（≥8GB）--always-high-vram", key: "high" },
      { label: "中等显存（6GB）--always-med-vram", key: "med" },
      { label: "低显存（4GB）--always-low-vram", key: "low" },
      { label: "超低显存 --always-no-vram", key: "novram" },
      { label: "纯 CPU --always-cpu", key: "cpu" },
    ],
    vram_neo2: [
      { label: "自动（默认）", key: "auto" },
      { label: "高显存 --gpu-only", key: "gpu_only" },
      { label: "中等 --normalvram", key: "normal" },
      { label: "低 --lowvram", key: "low" },
      { label: "超低 --novram", key: "novram" },
      { label: "纯 CPU --cpu", key: "cpu" },
    ],
    vram_comfy: [
      { label: "由 ComfyUI 自动管理（推荐，不加任何参数）", key: "auto" },
      { label: "高显存 --gpu-only", key: "gpu_only" },
      { label: "中等 --normalvram", key: "normal" },
      { label: "低 --lowvram", key: "low" },
      { label: "超低 --novram", key: "novram" },
      { label: "纯 CPU --cpu", key: "cpu" },
    ],
    precision_legacy: [
      { label: "自动（默认）", key: "auto" },
      { label: "全 FP16 --all-in-fp16", key: "fp16" },
      { label: "全 FP32 --all-in-fp32", key: "fp32" },
      { label: "全 BF16 --all-in-bf16", key: "bf16" },
      { label: "FP8 --all-in-fp8", key: "fp8" },
    ],
    precision_neo2: [
      { label: "自动（默认）", key: "auto" },
      { label: "强制 FP16 --force-fp16", key: "fp16" },
      { label: "强制 FP32 --force-fp32", key: "fp32" },
      { label: "强制 BF16 --force-bf16", key: "bf16" },
    ],
    unet_neo2: [
      { label: "自动", key: "auto" },
      { label: "FP16 --unet-in-fp16", key: "fp16" },
      { label: "FP32 --unet-in-fp32", key: "fp32" },
      { label: "BF16 --unet-in-bf16", key: "bf16" },
      { label: "FP8 --unet-in-fp8", key: "fp8" },
    ],
    vae_neo2: [
      { label: "自动", key: "auto" },
      { label: "FP32 --vae-in-fp32", key: "fp32" },
      { label: "BF16 --vae-in-bf16", key: "bf16" },
      { label: "FP16 --vae-in-fp16", key: "fp16" },
    ],
    text_enc_neo2: [
      { label: "自动", key: "auto" },
      { label: "FP16 --te-in-fp16", key: "fp16" },
      { label: "FP32 --te-in-fp32", key: "fp32" },
      { label: "BF16 --te-in-bf16", key: "bf16" },
      { label: "FP8 --te-in-fp8", key: "fp8" },
    ],
    attention_neo2: [
      { label: "自动", key: "auto" },
      { label: "SDP --attention-sdp", key: "sdp" },
      { label: "Quad --attention-quad", key: "quad" },
      { label: "Pytorch --attention-pytorch", key: "pytorch" },
    ],
  };

  // 预览模式下所有"检测/查询"类接口都返回空或明确的演示提示，
  // 绝不伪造"检测到安装/检测到便携版"这类会被当真的结论。
  window.pywebview = {
    api: {
      get_state: () => ok({
        config: mockConfig,
        cmd_args: "",
        is_windows: true,
        launch: { state: "stopped", text: "尚未启动", url: null },
        launcher: { version: "0.0.0", commit: "preview" },
        mirror_status: "（预览模式，无真实检测结果）",
        settings_schema: schema,
        deploy_branches: [
          { label: "Neo 版（Haoming02 社区维护分支，推荐）", key: "neo2" },
          { label: "Neo · H3 视频版（Neo + MiniMax-H3 视频生成，新分支）", key: "neo2h3" },
          { label: "常规版 / Classic（lllyasviel 官方仓库）", key: "classic" },
          { label: "ComfyUI（官方仓库）", key: "comfyui" },
        ],
        settings_branches: [
          { label: "Neo 版（新版参数，推荐）", key: "neo2" },
          { label: "Neo 版（旧版参数）", key: "neo" },
          { label: "常规版 / Classic", key: "classic" },
          { label: "ComfyUI", key: "comfyui" },
        ],
      }),
      update_config: () => ok({ ok: true, cmd_args: "" }),
      deploy_gpu_detect: () => {
        const amd = /[?&]amd=1/.test(location.search);
        const T = [["gfx1201", "RX 9070 / 9070 XT、AI PRO R9700"], ["gfx1200", "RX 9060 / 9060 XT"],
          ["gfx1100", "RX 7900 XTX / 7900 XT / 7900 GRE、PRO W7900 / W7800"], ["gfx1101", "RX 7800 XT / 7700 XT、PRO W7700"],
          ["gfx1102", "RX 7600 / 7600 XT / 7650 GRE / 7700S"], ["gfx1030", "RX 6950 XT / 6900 XT / 6800 XT / 6800、PRO W6800"],
          ["gfx1031", "RX 6750 XT / 6700 XT / 6700、6800M"], ["gfx1032", "RX 6650 XT / 6600 XT / 6600、6800S / 6700S"],
          ["gfx1034", "RX 6500 XT / 6400"], ["gfx1035", "Radeon 680M / 660M 核显"], ["gfx1010", "RX 5700 / 5700 XT / 5600"],
          ["gfx1151", "Ryzen AI Max（Radeon 8060S / 8050S）", true], ["gfx1103", "Radeon 780M / 760M 核显", true]];
        return ok({ ok: true, nvidia: amd ? [] : ["NVIDIA GeForce RTX 3090"],
          amd: amd ? [{ name: "AMD Radeon RX 7900 XTX", gfx: "gfx1100" }] : [],
          targets: T.map(([gfx, label, experimental]) => ({ gfx, label, experimental: !!experimental })),
          suggest: amd ? { backend: "rocm", gfx: "gfx1100" } : { backend: "", gfx: "" } });
      },
      gpu_switch: (target, backend, gfx) => {
        const ev = (type, d) => window.App.onEvent(Object.assign({ scope: "deploy", type }, d));
        ev("state", { running: true });
        ev("log", { text: `\n[环境] 把 ${target} 的环境切换为：${backend === "rocm" ? "AMD " + gfx : "NVIDIA（CUDA）"}\n` });
        setTimeout(() => { ev("log", { text: "[环境] 切换完成！\n" }); ev("state", { running: false }); ev("switch_done", { ok: true, backend, gfx, target }); }, 1500);
        return ok({ ok: true });
      },
      h3_models_info: () => {
        if (!/[?&]h3=1/.test(location.search)) return ok({ ok: true, is_h3: false });
        const G = 1073741824;
        const qf = (base, sizes, notes, state) => Object.keys(sizes).map((q) => ({ q, note: notes[q] || "",
          name: base.replace("{q}", q), size: sizes[q] * G, state: state && state[q] || "", part: 0 }));
        const one = (name, gb, state) => [{ q: null, note: "", name, size: gb * G, state: state || "", part: 0 }];
        const qn = { Q2_K: "最省内存，画质有损", Q4_K: "推荐", Q8_0: "接近原版，很吃内存", Q4_K_M: "推荐", Q2_K_M: "省约 5GB 内存" };
        const fl = { Q2_K: 6.26, Q3_K: 8.16, Q4_K: 10.64, Q5_0: 12.97, Q6_K: 15.45, Q8_0: 19.97 };
        return ok({ ok: true, is_h3: true, root: "D:/AI/forge-neo-h3", ram_gb: 63.9, vram_gb: 24, min_vram_gb: 12, ram_q4_gb: 33,
          quant_defaults: { fl2va: "Q4_K", ref2va: "Q4_K", te: "Q4_K_M" }, disk_free: 812 * G, running: false,
          items: [
            { id: "fl2va", label: "主模型 FL2VA", desc: "文生视频 / 图生视频 / 首尾帧 / ControlNet", required: true, default: true, where: "共享模型库/checkpoints", quant_default: "Q4_K", files: qf("minimax_h3_fl2va_pruned-{q}.gguf", fl, qn) },
            { id: "te", label: "文本编码器 Qwen3-VL-32B", desc: "必需，所有模式都要", required: true, default: true, where: "共享模型库/text_encoders", quant_default: "Q4_K_M", files: qf("qwen3vl_32b_minimax_h3-{q}.gguf", { Q4_K_M: 16.97, Q2_K_M: 12.2 }, qn) },
            { id: "vae_video", label: "视频 VAE", desc: "必需", required: true, default: true, where: "共享模型库/VAE", files: one("minimax_h3_video_vae_fp16.safetensors", 4.85, "ok") },
            { id: "vae_audio", label: "音频 VAE", desc: "必需（H3 同时生成立体声音轨）", required: true, default: true, where: "共享模型库/VAE", files: one("minimax_h3_audio_vae_fp32.safetensors", 0.56, "ok") },
            { id: "turbo_fl2v", label: "Turbo LoRA · FL2V 8 步", desc: "可选，强烈推荐：8 步出片，速度快好几倍", required: false, default: true, where: "共享模型库/loras", files: one("minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors", 1.82, "part") },
            { id: "ref2va", label: "主模型 Ref2VA", desc: "可选：参考图 / 参考视频 / 参考音频生视频才需要", required: false, default: false, where: "共享模型库/checkpoints", quant_default: "Q4_K", files: qf("minimax_h3_ref2va_pruned-{q}.gguf", fl, qn) },
            { id: "turbo_ref2v", label: "Turbo LoRA · Ref2V 8 步", desc: "可选：配合 Ref2VA 用", required: false, default: false, where: "共享模型库/loras", files: one("minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors", 1.82) },
            { id: "controlnet", label: "ControlNet Union 2.0（int8）", desc: "可选：上传普通视频做姿态 / 深度 / 线稿控制、视频局部重绘", required: false, default: false, where: "models/model_patches", files: one("minimax_h3_fun_controlnet_union_2.0_pruned_int8_convrot.safetensors", 4.22) },
          ] });
      },
      h3_models_download: (sel) => {
        let pct = 0;
        const t = setInterval(() => {
          pct += 7;
          const total = 10.64 * 1073741824;
          window.App.onEvent({ scope: "h3dl", type: "progress", id: "fl2va", name: "minimax_h3_fl2va_pruned-Q4_K.gguf", index: 1, count: sel.length,
            downloaded: total * Math.min(pct, 100) / 100, total, all_done: total * Math.min(pct, 100) / 100, all_total: total * 2.8 });
          if (pct >= 100) { clearInterval(t); window.App.onEvent({ scope: "h3dl", type: "item", id: "fl2va", state: "ok" }); }
        }, 300);
        window.App.onEvent({ scope: "h3dl", type: "item", id: "fl2va", state: "downloading" });
        window.App.onEvent({ scope: "h3dl", type: "log", text: "[H3] (1/3) minimax_h3_fl2va_pruned-Q4_K.gguf → 共享模型库/checkpoints（10.64 GB）\n" });
        return ok({ ok: true });
      },
      h3_models_cancel: () => { window.App.onEvent({ scope: "h3dl", type: "done", ok: false, cancelled: true }); return ok({ ok: true }); },
      instance_config_get: () => ok({ ok: true, iid: "demo", config: mockConfig, cmd_args: "" }),
      instance_config_update: () => ok({ ok: true, cmd_args: "" }),
      launch_env_detect: () => ok({ python: "", git: "" }),
      launch_precheck: () => ok({ ok: false, issues: [{ level: "error", text: "预览模式：未连接后端，无法检测" }] }),
      choose_directory: () => ok({ ok: true, path: "" }),
      choose_images: () => ok({ ok: true, paths: [] }),
      models_categories: () => ok({ ok: true, root: "", categories: [
        { label: "Stable-diffusion", is_lora: false, path: "D:\\AI\\webui\\models\\Stable-diffusion（演示）" },
        { label: "Lora", is_lora: true, path: "D:\\AI\\webui\\models\\Lora（演示）" },
        { label: "VAE", is_lora: false, path: "D:\\AI\\webui\\models\\VAE（演示）" },
        { label: "ControlNet", is_lora: false, path: "" },
        { label: "embeddings", is_lora: false, path: "" }, { label: "hypernetworks", is_lora: false, path: "" },
        { label: "upscaler", is_lora: false, path: "" }] }),
      // 拖拽上传演示：路径里带 lora 的当作 LoRA；开始后用定时器模拟进度事件
      models_import_plan: (cat, paths) => {
        const lora = (paths || []).filter((p) => /lora/i.test(p)).length;
        return ok({ ok: true, target_label: cat === 1 ? "Lora" : "Stable-diffusion", target_dir: "D:\\AI\\webui\\models（演示）",
          count: paths.length, to_copy: paths.length, skip: 0, rename: [], ignored: [], ignored_count: 0,
          counts: { lora, full: paths.length - lora }, total_text: "1.2 GB", no_space: false, free_text: "",
          suggest_index: (cat !== 1 && lora === paths.length) ? 1 : null, suggest_label: "Lora" });
      },
      models_import_start: (cat, paths) => {
        let pct = 0;
        const t = setInterval(() => {
          pct = Math.min(100, pct + 7);
          window.App.onEvent({ scope: "models", type: "import_progress", i: 1, n: paths.length, name: "演示模型.safetensors", pct });
          if (pct >= 100) {
            clearInterval(t);
            window.App.onEvent({ scope: "models", type: "import_done", cat_index: cat, copied: paths.length,
              renamed: 0, skipped: 0, failed: [], cancelled: false, last_dest: "" });
          }
        }, 120);
        return ok({ ok: true, total_text: "1.2 GB" });
      },
      models_list: () => ok({ ok: true, is_lora: false, files: [
        { path: "", rel: "（演示数据）v1-5.safetensors", base: "SD 1.5", size: 2140000000, size_text: "1.99 GB", mtime_text: "2025-11-02 14:20" },
        { path: "", rel: "（演示数据）ponyDiffusionV6.safetensors", base: "Pony", size: 6780000000, size_text: "6.32 GB", mtime_text: "2025-12-18 09:41" },
      ], total: 2, no_info: 1, is_lora: false }),
      ext_list: () => ok({ ok: true, has_root: true, comfy: false,
        target: { id: "demo", name: "Forge Neo（演示）", kind: "forge", kind_label: "Forge Neo", root: "D:\\demo", target_dir: "D:\\demo\\extensions", running: false },
        items: [
        { id: "ADetailer（演示）", name: "ADetailer（演示）", desc: "脸部/手部自动修复", installed: true, folder: "ADetailer-Neo",
          repos: [{ folder: "adetailer", rel: "adetailer", is_git: true, branch: "main", short: "a1b2c3d", pinned: false, remote: "https://github.com/Bing-su/adetailer.git", disabled: false,
            update: { behind: 3, latest_short: "e4f5a6b", latest_date: "2026-09-30 12:00" } }],
          variant_hint: "装的是 Classic 版本，Neo 下可能报错，建议换成 Neo 专用版", can_replace: true },
        { id: "中文标签补全（演示）", name: "中文标签补全（zh Tag Autocomplete）", desc: "打中文出英文标签", installed: false, author: true, folder: "sd-webui-zh-tag-autocomplete", repos: [] },
        { id: "Prompt All-in-One（演示）", name: "Prompt All-in-One（演示）", desc: "提示词输入框全家桶", installed: true, folder: "sd-webui-prompt-all-in-one-neo",
          repos: [{ folder: "sd-webui-prompt-all-in-one-neo", rel: "sd-webui-prompt-all-in-one-neo", is_git: true, branch: "", short: "9f8e7d6", pinned: true, remote: "", disabled: false, update: {} }] },
      ], others: [
        { folder: "sd-webui-my-thing-main", rel: "sd-webui-my-thing-main", is_git: false, branch: "", short: "", pinned: false, remote: "", disabled: true, update: {} },
        { folder: "sd-webui-other", rel: "sd-webui-other", is_git: true, branch: "master", short: "1234567", pinned: false, remote: "https://github.com/x/sd-webui-other.git", disabled: false, update: { behind: 0 } },
      ] }),
      ext_repo_versions: () => ok({ ok: true, local: { is_git: true, branch: "main", short: "a1b2c3d", commit: "a1b2c3d", date: "2026-08-01 10:00", subject: "（演示）fix", remote: "https://github.com/Bing-su/adetailer.git", dirty: [] },
        remote: { ok: true, branch: "main", behind: 2, current: "a1b2c3d",
          commits: [{ commit: "e4f5a6b", short: "e4f5a6b", date: "2026-09-30 12:00", subject: "（演示）新功能" }, { commit: "c0ffee1", short: "c0ffee1", date: "2026-09-20 12:00", subject: "（演示）修 bug" }, { commit: "a1b2c3d", short: "a1b2c3d", date: "2026-08-01 10:00", subject: "（演示）fix" }],
          tags: [{ name: "v25.3.0", commit: "e4f5a6b0" }, { name: "v25.2.0", commit: "0000000" }], branches: [{ name: "main", commit: "e4f5a6b" }] },
        history: [] }),
      ext_check_updates: () => ok({ ok: true }),
      ext_switch: () => ok({ ok: true }),
      ext_open_dir: () => ok({ ok: true }),
      ver_info: () => ok({ ok: true, has_root: true, has_git: true, running: false, official: true, busy: false,
        target: { id: "demo", name: "Forge Neo（演示）", kind: "forge", kind_label: "Forge Neo", repo: "D:\\demo" },
        local: { is_git: true, branch: "neo", short: "1a2b3c4", commit: "1a2b3c4", date: "2026-09-01 08:00", subject: "（演示）update", label: "neo @ 1a2b3c4", remote: "https://github.com/Haoming02/sd-webui-forge-classic.git", dirty: ["webui-user.bat"] },
        history: [{ commit: "0ff1ce0", label: "neo @ 0ff1ce0", time: "2026-08-20 21:00" }],
        check: { ok: true, branch: "neo", behind: 5, checked: "2026-10-09 09:00", current: "1a2b3c4",
          latest: { short: "9z9z9z9", date: "2026-10-08 18:00", subject: "（演示）最新提交" },
          commits: [{ commit: "9z9z9z9", short: "9z9z9z9", date: "2026-10-08 18:00", subject: "（演示）最新提交" }, { commit: "1a2b3c4", short: "1a2b3c4", date: "2026-09-01 08:00", subject: "（演示）update" }],
          tags: [], branches: [{ name: "neo", commit: "9z9z9z9" }, { name: "classic", commit: "abc" }] } }),
      ver_check: () => ok({ ok: true }),
      comfy_node_catalog: () => ok({ ok: true, selected: ["comfyui-manager", "rgthree-comfy"], groups: { core: "推荐（部署 ComfyUI 时默认安装）", extra: "按需安装" },
        items: [
          { id: "comfyui-manager", name: "ComfyUI-Manager（节点管理器）", desc: "（演示）节点管理器", group: "core", default: true },
          { id: "rgthree-comfy", name: "rgthree-comfy", desc: "（演示）工作流整理", group: "core", default: true },
          { id: "gguf", name: "ComfyUI-GGUF", desc: "（演示）GGUF 量化模型", group: "extra", default: false },
        ] }),
      wd14_models: () => ok({ ok: true, model_ready: false, models: [
        { key: "wd-vit-tagger-v3", label: "wd-vit-tagger-v3（默认，速度快，~380MB）", cached: false,
          urls: ["https://hf-mirror.com/SmilingWolf/wd-vit-tagger-v3/tree/main",
                 "https://huggingface.co/SmilingWolf/wd-vit-tagger-v3/tree/main"] },
        { key: "wd-eva02-large-tagger-v3", label: "wd-eva02-large-tagger-v3（精度更高）", cached: false,
          urls: ["https://hf-mirror.com/SmilingWolf/wd-eva02-large-tagger-v3/tree/main",
                 "https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3/tree/main"] },
      ] }),
      wd14_import_model: () => ok({ ok: true }),
      meta_preview: () => ok({ ok: true, preview: null }),
      meta_parse: () => ok({ ok: true, source: "WebUI (Forge)", tone: "webui",
        file_info: "1408×960　·　PNG　·　1512 KB", has_meta: true, from_sidecar: false,
        prompt: "（预览环境假数据）masterpiece, 1girl", negative: "lowres",
        rows: [["步数","28"],["采样器","DPM++ 2M"],["种子","1234567890"]],
        loras: [], characters: [], civitai: [], raw_text: "", refs_count: 0,
        timing: { parse: 0.4, mode: "前端直读" } }),
      meta_deep: () => ok({ ok: true, has_meta: false }),
      reveal_image: () => ok({ ok: true }),
      meta_load: () => ok({
        ok: true, path: "（预览环境假数据）example.png",
        preview: null, preview_size: 2 * 1048576, source: "NovelAI", tone: "nai",
        file_info: "832×1216　·　PNG　·　1420 KB", has_meta: true, from_sidecar: false,
        prompt: "2girls, school uniform, cherry blossoms, best quality",
        negative: "lowres, bad anatomy",
        rows: [["采样器", "k_euler_ancestral"], ["步数", "28"], ["CFG", "6"],
               ["种子", "3847562910"], ["尺寸", "832x1216"]],
        loras: [{ name: "handDrawnStyle.safetensors", weight: 0.7, hash: null }],
        characters: [
          { index: 0, prompt: "girl, blue hair, smile", negative_prompt: "", center: { x: 0.3, y: 0.5 } },
          { index: 1, prompt: "girl, red ribbon", negative_prompt: "blurry", center: { x: 0.7, y: 0.5 } },
        ],
        civitai: [{ model_name: "Detail Tweaker", version_name: "v1.0", weight: 0.8,
                    model_type: "lora", civitai_url: "https://civitai.com/models/58390?modelVersionId=87153" }],
        raw_text: "（预览环境的假数据）", refs_count: 2,
        timing: { parse: 0.6, preview: 1.2, config: 0, total: 2.1, preview_mode: "硬链接" },
      }),
      civitai_fetch: (text) => {
        // 模拟后端：异步发 info 事件（与真实运行时的 civitai/info 一致）
        setTimeout(() => {
          const isLb = (text || "").toLowerCase().includes("liblib");
          const evt = isLb ? {
            scope: "civitai", type: "info", ok: true,
            info: {
              source: "liblib",
              model_name: "（演示数据）F.1-黑神话悟空-Lora",
              model_type: "LoRA（按文件大小猜测）",
              version_name: "FLUX-黑神话悟空-lora",
              base_model: "FLUX.1",
              page_url: "https://www.liblib.art/modelinfo/318995332c8d4f6fb47cc40792c579de",
              trigger_words: ["wukong"],
              vip_used: false, exclusive: false,
              files: [
                { name: "flux_wukong.safetensors", version_name: "FLUX-黑神话悟空-lora", sizeKB: 167940, primary: true, unavailable: false },
                { name: "flux_wukong_v2.safetensors", version_name: "FLUX-黑神话悟空-lora v2", sizeKB: 245760, primary: false, unavailable: false },
              ],
            },
            folder: "models/Lora", dest: "（预览模式：请先设置 WebUI 根目录）",
          } : {
            scope: "civitai", type: "info", ok: true,
            info: { source: "civitai", model_name: "示例模型（演示数据）", model_type: "LORA", version_name: "v1.0", base_model: "SDXL 1.0",
                    page_url: "https://civitai.com/models/12345?modelVersionId=67890",
                    files: [{ name: "example.safetensors", sizeKB: 233472, primary: true }] },
            folder: "models/Lora", dest: "（预览模式：请先设置 WebUI 根目录）",
          };
          window.App && window.App.onEvent(evt);
        }, 200);
        return ok({ ok: true });
      },
      settings_verify_liblib_token: (t) => t ? ok({ ok: true, nickname: "预览用户" }) : ok({ ok: true, nickname: "" }),
      deploy_env_detect: () => ok({ ok: true,
        git: { found: false, text: "（预览模式，无真实检测结果）" },
        python: { found: false, text: "（预览模式，无真实检测结果）" } }),
      deploy_check_dir: () => ok({ ok: true, status: "warn", message: "预览模式：未连接后端，无法检测目录" }),
      models_detail: () => ok({ ok: true, preview: null,
        file: { name: "ponyDiffusionV6.safetensors（演示）", size_text: "6.32 GB", mtime_text: "2025-12-18 09:41" },
        civitai: { modelName: "Pony Diffusion V6 XL", modelType: "Checkpoint", versionName: "V6", baseModel: "Pony", sha256_short: "67ab2fd684ec...",
          modelId: 257749, versionId: 290640, liblibUuid: "", liblibVersionUuid: "",
          trainedWords: ["score_9, score_8_up, score_7_up", "anthro pony"], trainedWordsSource: "civitai" },
        safetensors: { kind: "Checkpoint", arch: "SDXL", note: "UNet 结构符合 SDXL 特征", train_rows: [["ss_resolution", "1024x1024"]], tags: ["anime", "score_9", "score_8_up"] } }),
      models_set_trained_words: (p, w) => ok({ ok: true, count: (w || []).length }),
      models_bind_liblib: () => ok({ ok: true, modelName: "示例 LoRA（演示）", words: 2 }),
      open_output_folder: (which) => ok({ ok: false, error: "预览模式：未连接后端" }),
      output_dirs_info: () => ok({ ok: true, root: "",
        txt2img: "", img2img: "",
        date_subdir: true, exists: { root: false, txt2img: false, img2img: false } }),
      wd14_add_clipboard_image: () => ok({ ok: false, error: "预览模式：未连接后端" }),
      instances_list: () => ok({ ok: true, instances: [{ id: "demo", name: "ComfyUI", branch: "comfyui", kind: "comfyui",
        kind_label: "ComfyUI", root: "", port: "", active: true, status: { state: "idle", running: false } }],
        active: "demo", multi: false, multi_manual: false }),
      library_status: () => ok({ ok: true, enabled: false, path: "", exists: false, journals: [] }),
      outputs_info: () => ok({ ok: false, error: "预览模式：输出管理需要连接后端" }),
      _noop: () => ok({}),
    },
  };

  // 未定义的方法统一返回空对象，避免预览时报错
  const api = window.pywebview.api;
  window.pywebview.api = new Proxy(api, {
    get(target, prop) {
      if (prop in target) return target[prop];
      return () => ok({});
    },
  });

  // 让 pywebviewready 事件在预览环境也能触发；同时常驻一条明显的
  // "预览模式"提示条——演示数据绝不能被当成真实检测结果
  window.addEventListener("DOMContentLoaded", () => {
    setTimeout(() => window.dispatchEvent(new Event("pywebviewready")), 100);
    showBanner("预览模式（?mock=1）：未连接到启动器后端，页面上的路径、" +
               "检测结果均为演示数据，不代表这台电脑的真实情况。");
  });
})();
