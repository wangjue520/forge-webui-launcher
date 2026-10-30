# WWY 启动器

Windows 桌面程序（pywebview），代码在启动器目录的 `app/`（Python 后端）和 `web/`（界面）。
- 配置：`launcher_config.json`（全局键 + `instances` 列表，每个实例有 id / name / webui_root / webui_branch 和各自的高级选项）
- 启动器自己的日志：`launcher_data/launcher.log`；其他缓存在 `launcher_data/`
- 后端入口 `app/webview_api.py`（LauncherApi），实例启动 `app/process_manager.py`（InstanceRunner），
  启动参数拼接 `app/config_manager.py`（build_commandline_args / build_comfy_args），镜像 `app/mirror_manager.py`，
  代理 `app/proxy_manager.py` + 自动换路 `app/net_guard.py`，ComfyUI 节点 `app/comfy_nodes.py`，
  版本管理 `app/version_manager.py`，torch / 驱动兼容 `app/cuda_compat.py`、`app/torch_bootstrap.py`
- 启动器日志行前缀：`[启动器]` 启动流程，`[网络]` 代理换路，`[插件]` `[节点]` 装插件，`[版本]` 版本管理，`[诊断]` 本诊断

## 界面页面（侧栏）
- 一键启动（launch）：选根目录、启动 / 停止、运行日志、「诊断」按钮。启动前会预检：中文路径、驱动太旧、
  残留进程占端口（可「结束并启动」）、webui-user.bat 里写死的 PYTHON/GIT（可「清空并启动」）、venv 与 Python 版本不一致（可重建 venv）
- 实例管理（instances）：多实例模式下管理多个 WebUI / ComfyUI，并排启动，端口自动错开
- 高级选项（settings）：按实例设置显存模式、精度（全局 / UNet / VAE / 文本编码器）、注意力、端口、--listen、附加参数 extra_args
- 启动器设置（launcher）：启动器更新、多实例开关、下载加速（mirror_mode：auto / always / never）、网络代理、报错诊断 AI
- 环境部署（deploy）：部署新的 Forge Neo / ComfyUI（便携 Python + Git）、显卡（N 卡 CUDA / A 卡 ROCm）、
  环境诊断、重建 venv、「版本管理…」入口
- 版本管理（versions）：WebUI / ComfyUI 本体检查更新、切换版本 / 回滚、换仓库
- 模型下载（civitai）：输入 Civitai / liblib 网址下载，Civitai API Key、liblib token；H3 视频模型一键下载
- 模型管理（models）：按类别浏览模型、哈希查 Civitai 信息、共享模型库（多个实例共用一套模型）
- 常用插件（extensions）：给当前实例装 / 停用 / 更新 / 换版本 WebUI 扩展或 ComfyUI 节点
- 输出管理、WD14 反推、图片信息（读 PNG 生成参数、缺失模型检测）

## 网络（用户大多在国内）
- HuggingFace、GitHub、Civitai、download.pytorch.org、PyPI 直连经常超时
- 启动器的「下载加速」：pip 走清华等 PyPI 镜像，GitHub 走加速代理，HF 设 HF_ENDPOINT=hf-mirror.com（启动的子进程也继承）。
  mirror_mode=auto 时首次联网检测一次并缓存，结果可能过时；always 强制走镜像
- Civitai 没有国内镜像，只能开梯子；启动器设置里的「网络代理」填梯子的 HTTP 端口（Clash 7890、v2rayN 10809……），
  会写进 HTTP(S)_PROXY，WebUI / ComfyUI 子进程要重启才用上
- 「Unable to connect to proxy / ProxyError / Tunnel connection failed」= 配的代理端口没开（梯子没开 / 端口变了 / 系统代理残留）。
  启动器自己的请求会自动换路（net_guard），子进程不会
- ADetailer 的模型启动器会提前下载（adetailer_models），日志里有对应提示

## Forge 系 WebUI
- 分支 webui_branch：neo2 = Forge Neo（Haoming02/sd-webui-forge-classic 的 neo 分支，新参数体系，和 ComfyUI 同名：
  --fp32-vae / --bf16-vae / --cpu-vae、--fp16-unet、--lowvram、--force-fp32 等）；neo / classic = 旧 Forge（--vae-in-fp32、
  --always-low-vram 等）；A1111 原版（秋叶整合包）还认 --no-half-vae / --no-half / --medvram
- 目录：webui.bat、launch.py、modules/、backend/（Forge）、extensions/、extensions-builtin/、models/Stable-diffusion|Lora|VAE、
  venv/（依赖）、outputs/、config.json（WebUI 设置，disabled_extensions）、ui-config.json
- 启动器直接跑 webui.bat，COMMANDLINE_ARGS / PYTHON / GIT 由启动器注入；webui-user.bat 不会被执行（除非 webui.bat 里 call 了它）
- 启动参数是否被这个版本支持：看 backend/args.py 或 modules/cmd_args.py 里的 add_argument；不支持的启动器会自动去掉
- 扩展依赖在启动时由扩展的 install.py 安装；扩展报错一般会打印 `*** Error loading script: xxx.py` 和扩展路径
- 常见：VAE 出 NaN → 黑图（A tensor with all NaNs was produced in VAE）；显存不足 OOM；模型文件下载不完整 → SafetensorError

## ComfyUI
- 目录：main.py、comfy/、nodes.py、custom_nodes/（节点）、models/checkpoints|loras|vae|clip|unet|diffusion_models、
  user/default/（设置和工作流）、output/、extra_model_paths.yaml（启动器的共享模型库会写这个）
- 官方便携包：ComfyUI_windows_portable/python_embeded/python.exe + ComfyUI/；启动器用 `python -s main.py`
- 参数在 comfy/cli_args.py；启动器默认加 --enable-manager（ComfyUI-Manager 已内置进本体，依赖在 manager_requirements.txt）
- 节点导入失败：启动日志 `Cannot import <路径> module for custom nodes: <原因>`，结尾汇总里标 `(IMPORT FAILED)`；
  多半是没装 requirements.txt（手动 clone 的节点不会自动装依赖）或节点版本和 ComfyUI 不配套
- 工作流缺节点：提交时报 `Cannot execute because node X does not exist` / missing_node_type；
  ComfyUI-Manager 的 extension-node-map.json 记录了节点类型 → 节点仓库
- ComfyUI 不会自动装节点依赖，而且节点 requirements 里的 torch 绝不能照装（会把 CUDA 版换成 CPU 版），启动器装依赖时会跳过 torch 系
- ComfyUI-Manager 装 / 卸节点后会打印 Restarting... 自动重启，这不是崩溃
- 新版 ComfyUI 网页发现缺节点 / 缺模型会直接拦着不让运行，后端日志里什么都没有。所以启动器启动 / 部署 ComfyUI 时会放一个
  桥接插件 custom_nodes/wwy_launcher_bridge（不加节点）：网页每次加载工作流就检查一遍，打一行
  `[WWY诊断] 工作流「名字」缺 … {JSON}`（nodes = 缺的节点类型，models = 缺的模型：name 下拉框里的值、folder 模型目录、
  dirs 这个目录在本机的实际路径、url 工作流自带的下载地址），全齐了打「检查通过」。当前工作流存一份在
  user/wwy_bridge/current_workflow.json。用户把插件改名 .disabled 就是不想要，启动器不会放回去
- 缺模型：先确认是真没有，还是放错了目录 / 名字不一样（用 list_dir 看看对应 models 子目录和共享模型库）。
  要下载就用 search_model 找地址（Civitai 的 exact=true 才是文件名完全一致；HuggingFace 只能按仓库名搜，再用 hf_files
  看仓库里的文件），然后在 finish.actions 里推荐 download_model，dir 用发现里给的目录，name 和工作流里的文件名一致。
  只搜到近似结果时说清楚「可能不是同一个」，别硬推荐。官方模型（Comfy-Org 的 split_files 仓库等）优先 HuggingFace
- 下载好模型后要在 ComfyUI 网页里按 R（刷新节点定义）下拉框才会出现新文件

## torch / 显卡
- 新版 Forge Neo / ComfyUI 默认装 CUDA 13 的 torch，要 580 以上的驱动；启动器启动前会读驱动版本并换装兼容的 torch
- 老卡（GTX 9xx / 10xx）配新 torch 报 no kernel image is available，要 cu126 的 torch
- A 卡走 ROCm（gpu_backend=rocm），在「环境部署」页切换
- 不要让用户手动 pip install torch；用「环境部署」页的功能或删掉 venv 让 WebUI 重装

## Windows 常见坑
- 中文 / 带空格的路径：很多依赖有 bug，建议放纯英文路径（如 D:\ai）
- 虚拟内存太小（WinError 1455 / 页面文件太小）：系统设置里调大虚拟内存，只能用户自己改
- 杀毒软件删掉便携 Python / Git 的文件：表现为 exe 跑不起来、Bad git executable
- 中文系统命令行输出是 GBK，日志偶尔有乱码是正常的
