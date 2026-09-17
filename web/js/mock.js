/* mock.js — 无 pywebview 环境（如直接双击 html 或截图预览）时的桩实现。
   只在 window.pywebview 不存在时生效，真实运行时被自动跳过。 */
(function () {
  if (window.pywebview) return;

  const ok = (v) => new Promise((r) => setTimeout(() => r(v), 60));

  const mockConfig = {
    webui_root: "F:\\sd-webui-forge-aki",
    custom_python_path: "",
    custom_git_path: "",
    webui_branch: "classic",
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
      { label: "FP16 --vae-in-fp16", key: "fp16" },
      { label: "FP32 --vae-in-fp32", key: "fp32" },
      { label: "BF16 --vae-in-bf16", key: "bf16" },
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

  window.pywebview = {
    api: {
      get_state: () => ok({
        config: mockConfig,
        cmd_args: "--xformers",
        is_windows: true,
        launch: { state: "stopped", text: "尚未启动", url: null },
        launcher: { version: "2.0.8", commit: "e04e5a6" },
        mirror_status: "当前：已检测为国内网络，下载走镜像加速",
        settings_schema: schema,
        deploy_branches: [
          { label: "经典版（classic，推荐）", key: "classic" },
          { label: "Neo 2.0（新架构）", key: "neo2" },
        ],
        settings_branches: [
          { label: "经典版（classic）", key: "classic" },
          { label: "Neo", key: "neo" },
          { label: "Neo 2.0", key: "neo2" },
        ],
      }),
      update_config: () => ok({ ok: true, cmd_args: "--xformers" }),
      launch_env_detect: () => ok({ python: "F:\\sd-webui-forge-aki\\python\\python.exe（便携版）", git: "F:\\sd-webui-forge-aki\\git\\cmd\\git.exe（便携版）" }),
      launch_precheck: () => ok({ ok: true, issues: [] }),
      choose_directory: () => ok({ ok: true, path: "F:\\some\\folder" }),
      choose_images: () => ok({ ok: true, paths: [] }),
      models_categories: () => ok({ ok: true, root: "F:\\sd-webui-forge-aki\\models", categories: [
        { label: "Stable-diffusion", is_lora: false }, { label: "Lora", is_lora: true },
        { label: "VAE", is_lora: false }, { label: "ControlNet", is_lora: false },
        { label: "embeddings", is_lora: false }, { label: "hypernetworks", is_lora: false },
        { label: "upscaler", is_lora: false }] }),
      models_list: () => ok({ ok: true, is_lora: false, files: [
        { path: "F:\\m\\v1-5.safetensors", rel: "v1-5.safetensors", base: "SD 1.5", size: 2140000000, size_text: "1.99 GB", mtime_text: "2025-11-02 14:20" },
        { path: "F:\\m\\pony.safetensors", rel: "ponyDiffusionV6.safetensors", base: "Pony", size: 6780000000, size_text: "6.32 GB", mtime_text: "2025-12-18 09:41" },
      ], total: 2, no_info: 1, is_lora: false }),
      ext_list: () => ok({ ok: true, has_root: true, items: [
        { name: "adetailer", desc: "After Detailer：脸部/手部自动修复", installed: true },
        { name: "tagger", desc: "WD14 Tagger 扩展", installed: false },
      ] }),
      wd14_models: () => ok({ ok: true, model_ready: false, models: [
        { key: "wd-vit-tagger-v3", label: "wd-vit-tagger-v3（默认，速度快，~380MB）", cached: true,
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
        ok: true, path: "F:\\outputs\\txt2img\\00012-1234567890.png",
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
              model_name: "F.1-黑神话悟空-Lora",
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
            folder: "models/Lora", dest: "F:\\sd-webui-forge-aki\\models\\Lora",
          } : {
            scope: "civitai", type: "info", ok: true,
            info: { source: "civitai", model_name: "示例模型", model_type: "LORA", version_name: "v1.0", base_model: "SDXL 1.0",
                    page_url: "https://civitai.com/models/12345?modelVersionId=67890",
                    files: [{ name: "example.safetensors", sizeKB: 233472, primary: true }] },
            folder: "models/Lora", dest: "F:\\sd-webui-forge-aki\\models\\Lora",
          };
          window.App && window.App.onEvent(evt);
        }, 200);
        return ok({ ok: true });
      },
      settings_verify_liblib_token: (t) => t ? ok({ ok: true, nickname: "预览用户" }) : ok({ ok: true, nickname: "" }),
      deploy_env_detect: () => ok({ ok: true,
        git: { found: true, text: "已检测到 (git version 2.47.0.windows.1)" },
        python: { found: false, text: "未检测到 Python，请先安装：https://www.python.org/downloads/" } }),
      deploy_check_dir: () => ok({ ok: true, status: "ok", message: "检测到该目录已经是一个 WebUI 安装（存在 webui.bat），可直接点击部署来补齐/更新虚拟环境依赖" }),
      models_detail: () => ok({ ok: true, preview: null,
        file: { name: "ponyDiffusionV6.safetensors", size_text: "6.32 GB", mtime_text: "2025-12-18 09:41" },
        civitai: { modelName: "Pony Diffusion V6 XL", modelType: "Checkpoint", versionName: "V6", baseModel: "Pony", sha256_short: "67ab2fd684ec...",
          modelId: 257749, versionId: 290640, liblibUuid: "", liblibVersionUuid: "",
          trainedWords: ["score_9, score_8_up, score_7_up", "anthro pony"], trainedWordsSource: "civitai" },
        safetensors: { kind: "Checkpoint", arch: "SDXL", note: "UNet 结构符合 SDXL 特征", train_rows: [["ss_resolution", "1024x1024"]], tags: ["anime", "score_9", "score_8_up"] } }),
      models_set_trained_words: (p, w) => ok({ ok: true, count: (w || []).length }),
      models_bind_liblib: () => ok({ ok: true, modelName: "示例 LoRA", words: 2 }),
      open_output_folder: (which) => ok({ ok: true, path: "F:\\sd-webui-forge-aki\\outputs" }),
      output_dirs_info: () => ok({ ok: true, root: "F:\\sd-webui-forge-aki\\outputs",
        txt2img: "F:\\sd-webui-forge-aki\\outputs\\txt2img-images",
        img2img: "F:\\sd-webui-forge-aki\\outputs\\img2img-images",
        date_subdir: true, exists: { root: true, txt2img: true, img2img: true } }),
      wd14_add_clipboard_image: () => ok({ ok: true, path: "F:\\launcher\\clipboard_inbox\\paste_20260909.png" }),
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

  // 让 pywebviewready 事件在预览环境也能触发
  window.addEventListener("DOMContentLoaded", () => {
    setTimeout(() => window.dispatchEvent(new Event("pywebviewready")), 100);
  });
})();
