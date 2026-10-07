# WWY 启动器（ComfyUI / Forge WebUI）

面向不懂 Python 的用户：界面美观、开箱即用、出错有中文提示。
一个启动器同时管理 **ComfyUI** 和 **Forge WebUI**（Neo / H3 视频版 / Classic），**N 卡、A 卡都支持**，支持多实例同时运行；只用一个的话界面和以前一样简单。

> ## 📖 [新手图文教程（点这里）](docs/README.md)
>
> 每个功能都有一步一步的教程：[安装部署](docs/01-安装与部署.md) · [启动出图](docs/02-启动与出图.md) · [C 站怎么下模型](docs/03-模型下载-C站与liblib.md) · [要下哪些模型](docs/04-要下哪些模型.md) · [Anima 使用教程](docs/05-Anima使用教程.md) · [常见问题](docs/09-常见问题.md)

> **English TL;DR**: A beginner-proof launcher for ComfyUI & Forge WebUI (NVIDIA CUDA and AMD ROCm). Download the ZIP, extract to a pure-English path, double-click `启动WWY启动器.bat`, deploy with one click, download a checkpoint, press Launch. Full UI is in Chinese.

---

## 功能特性

- **零环境要求**：不需要预装 Python / Git，双击 bat 全自动引导（便携 Python + 独立 `.venv`，不碰系统 Python），首次运行自动建桌面快捷方式
- **一键部署**：Forge Neo（推荐）/ Neo · H3 视频版 / Classic / ComfyUI，便携 Python + Git + 源码 + torch 预下载全自动，断点续传，部署心跳显示实时网速
- **N 卡 / A 卡**：A 卡自动装 AMD ROCm 版 torch 并配好参数；换显卡不用重装，一键把已有环境在 N 卡 ⇄ A 卡之间切换；N 卡旧驱动自动换兼容的 torch
- **一个启动器管多个后端**：ComfyUI 和 Forge 都支持，自动识别安装类型，已有整合包直接用
- **多实例**：登记 / 部署多个实例、同时运行（端口自动错开）、一键切换
- **共享模型库**：所有实例共用一份模型，合并按内容去重、可撤销
- **模型下载**：Civitai / 哩布哩布链接一键下载，按类型自动放对文件夹，断点续传 + SHA256 校验，自动写封面和触发词；H3 视频模型一键配齐
- **模型管理**：分类浏览、哈希查询补全信息、拖拽上传、LoRA 按底模 / 类型自动整理（同步修正 ComfyUI 工作流路径，可撤销）
- **输出管理器**：所有实例的出图 / 视频统一相册，按时间、角色、LoRA、模型、tag、来源实例筛选，收藏夹、多选删除
- **图片信息**：读取 A1111 / Forge / ComfyUI / NovelAI 等来源的生成参数，**支持视频**（MP4 / MKV / WebM），检测并一键补齐缺失模型
- **WD14 反推**：图片自动打标签，独立环境不污染主环境
- **常用插件一键安装**：按分支自动适配兼容版本；ComfyUI 节点连依赖一起装
- **启动管理**：一键启动 / 停止（整棵进程树干净退出）、实时日志、就绪自动开浏览器、端口占用 / 残留进程预检、启动失败给中文原因
- **国内网络优化**：自动判断走 GitHub 加速 / PyPI 镜像 / HF 镜像，失败自动回退官方源；经代理下载的程序强制哈希 + 签名校验
- **多套界面风格**：终末地、构型 BAUFORM、极简、液态玻璃、矢量突破，带开屏动画

---

## 快速上手（五步）

> 详细图文版：[01 · 安装与部署](docs/01-安装与部署.md) → [03 · 模型下载](docs/03-模型下载-C站与liblib.md) → [02 · 启动与出图](docs/02-启动与出图.md)

### 〇、准备

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10（1803+）/ 11，64 位 |
| 显卡 | NVIDIA 6GB+ 显存体验较好，4GB 可跑；AMD RX 5000 ~ 9000 系列（ROCm） |
| 内存 | 16GB 起，32GB 推荐 |
| 硬盘 | 至少预留 **30GB**，推荐 SSD |
| 软件 | **什么都不用装** |

### 一、下载并解压

仓库主页 → 绿色 **Code** → **Download ZIP**，解压到**纯英文路径**（如 `D:\AI\`）。
❌ 不要放桌面、不要有中文 / 空格 / `(1)` 这类括号、不要在压缩包里直接运行。

### 二、双击 `启动WWY启动器.bat`

第一次自动下载便携 Python 并装好依赖（1~3 分钟），之后弹出启动器窗口，桌面会多一个「WWY 启动器」快捷方式，以后双击它即可。

### 三、部署（只需一次）

侧栏 **「环境部署」** → 程序选 **Neo 版（推荐）** → 显卡选 N 卡 / A 卡 → 「浏览…」选一个空的纯英文目录（如 `D:\AI\forge`）→ **「开始部署」**。
装依赖要十几分钟到一小时，看日志里的「当前网速」判断有没有在下载；断了再点一次会**断点续传**。

> 已有秋叶整合包等现成安装：跳过部署，「一键启动」页「根目录」直接选它。

### 四、下载一个大模型

去 [Civitai](https://civitai.com) 或 [哩布哩布](https://www.liblib.art) 复制模型页链接 → 启动器 **「模型下载」** 粘贴 →「获取信息」→「下载并放入对应文件夹」。
不知道下哪个：看 [要下哪些模型](docs/04-要下哪些模型.md)。

### 五、启动出图

**「一键启动」** → 浏览器自动打开 → 左上角选模型 → 写提示词 → Generate。
用完先在启动器点 **「停止」** 再关窗口。参数怎么填看 [启动与出图](docs/02-启动与出图.md)。

---

## 教程目录

| 教程 | 内容 |
|---|---|
| [01 · 安装与部署](docs/01-安装与部署.md) | 下载解压、首次运行、部署 Forge / ComfyUI、A 卡、已有整合包、N 卡 ⇄ A 卡切换、环境诊断 |
| [02 · 启动与出图](docs/02-启动与出图.md) | 一键启动页、出第一张图、参数和提示词怎么填、ComfyUI 入门、网页汉化、高级选项每一项 |
| [03 · 模型下载：C 站和 liblib](docs/03-模型下载-C站与liblib.md) | C 站怎么找模型、看懂模型页、API Key、liblib usertoken、复刻别人的图、HuggingFace 文件 |
| [04 · 要下哪些模型](docs/04-要下哪些模型.md) | 模型种类、按显卡选模型、入门包、模型放哪个文件夹、兼容性规则 |
| [05 · Anima 使用教程](docs/05-Anima使用教程.md) | 版本选择、三个文件下载与放置、Forge Neo / ComfyUI 出图、提示词写法 |
| [06 · 模型管理与共享模型库](docs/06-模型管理与共享模型库.md) | 模型管理页、拖拽上传、补全信息、LoRA 自动整理、多实例、共享模型库 |
| [07 · 输出管理、图片信息、WD14 反推](docs/07-输出管理-图片信息-WD14反推.md) | 出图相册、读取参数、缺失模型检测、自动打标签 |
| [08 · 插件、启动器设置、界面风格](docs/08-插件-启动器设置-界面风格.md) | 插件清单、启动器更新、界面风格、下载加速 |
| [09 · 常见问题](docs/09-常见问题.md) | 部署、启动、A 卡、出图、模型的各种报错 |

---

## 进阶：配置文件与目录结构

`launcher_config.json` 首次运行自动生成（实例表、当前实例、界面风格、全局设置都在里面），全部能在界面上改，不需要手改。

```
forge-webui-launcher/
├─ 启动WWY启动器.bat    ← 启动入口（也可以复制到文件夹旁边用；自动准备 Python + 依赖）
├─ .venv/               ← 启动器自己的运行环境（首次自动创建，删了自动重建）
├─ python/              ← 自动下载的便携 Python 3.13（没有才建）
├─ launcher_data/       ← 运行期数据：输出索引、缩略图、下载缓存、移动记录（更新时保留）
├─ docs/                ← 图文教程
├─ webview_main.py      ← 入口：创建窗口、关闭前确认
├─ webview_api.py       ← JS ↔ Python 桥接层：所有页面逻辑、事件推送
├─ process_manager.py   ← 每个实例一个启动器（进程树/端口/就绪判定/启动失败诊断）
├─ model_library.py     ← 拖拽上传、共享模型库合并去重、LoRA 整理、移动记录与撤销
├─ output_index.py      ← 输出管理器：SQLite 索引、角色识别、收藏夹、本地文件服务
├─ config_manager.py    ← 配置读写、实例管理、命令行参数生成、环境检测
├─ portable_env.py      ← 便携 Python/Git 下载、venv 检测、hashlib 补丁
├─ torch_bootstrap.py / cuda_compat.py ← torch 预下载、按驱动挑兼容的 CUDA 版本
├─ amd_rocm.py          ← A 卡：型号识别、ROCm torch 选版与配套、启动参数
├─ h3_models.py         ← H3 视频模型清单与一键下载
├─ netspeed.py          ← 部署心跳里的实时网速
├─ updater.py           ← 启动器自更新（版本号管理、GitHub 检查、增量升级）
├─ version.json         ← 当前版本号（每次更新自动重写）
├─ mirror_manager.py    ← 国内镜像加速
├─ civitai_downloader.py / liblib_client.py / safetensors_meta.py / image_meta_core.py
├─ meta_engine/         ← 图片 / 视频元数据读写引擎
├─ wd14_*.py            ← WD14 反推（独立 venv）
├─ tools/               ← 字体生成、开屏视频 / 背景离线渲染脚本
├─ tests/               ← 单元测试
└─ web/                 ← 前端（index.html / style.css / splash.css / js/ / themes/ / fonts/）
```

**前端联调**：浏览器打开 `web/index.html?mock=1` 可预览界面（注入标注了"演示数据"的假数据，底部有常驻提示条；不带 `?mock=1` 不会启用 mock）。支持 `?page=xxx` 直接定位页面、`?theme=xxx` 预览指定界面风格。

**界面风格开发**：新风格做成 `web/themes/xxx.css` 加注册表一条即可（见 `web/js/themes.js` 开头注释），开屏动画也变体化。液态玻璃复用极简的结构规则（`base: "minimal"`，选择器写 `[data-ui-base="minimal"]`），只叠一层材质与动效（`themes/liquid.css` + `js/liquid.js`，字体在 `web/fonts/`）。

矢量突破的开屏视频和背景图层由 `tools/render_vector_splash.py`、`tools/render_vector_backdrop.py` 离线生成（Playwright 逐帧截图 + ffmpeg 编码，假时钟驱动，每帧精确 1/60 秒），改了开屏 / 背景的 CSS 后重新跑一遍即可；只改了叠加层位置可以加 `--meta-only` 秒出。

构型 BAUFORM 的拉丁字体（WWY Bauform，MIT）由 `tools/build_bauform_font.py` 生成，翻牌组件在 `web/js/bauform-flap.js`。

**依赖**：只有 `pywebview`（界面）和 `requests`（下载/查询），全部装进项目内 `.venv`，不碰系统 Python（因此 uv 托管 / PEP 668 的 Python 也能用）。

---

## 反馈与贡献

- Bug / 建议：[GitHub Issues](https://github.com/wangjue520/forge-webui-launcher/issues)（附日志最后 30 行截图最容易解决）
- 欢迎 PR：代码风格跟随现有文件，大改动请先开 Issue 讨论
