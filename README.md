# Forge WebUI 启动器（pywebview 版）

面向不懂 Python 的用户：界面美观、开箱即用、出错有中文提示。
界面用 HTML/CSS/JS 实现（WebView2 渲染），后端逻辑全部保留在原 Python 模块里。

## 功能特性

- **零环境要求**：不需要预装 Python/Git，双击 `start一键启动.bat` 全自动引导
- **一键更新**：启动器本体可在「高级选项 → 启动器更新」一键升级（git 增量拉取 / ZIP 覆盖自动选择），配置和已下载环境全部保留，版本号随更新自动递增
- **一键部署**：默认部署社区持续维护的 Forge **Neo 分支**（推荐），也支持官方 Classic 分支，便携 Python + Git + 源码 + 依赖全自动
- **国内网络优化**：按 IP 归属地自动判断——国内走 GitHub 加速代理、PyPI 清华/阿里/腾讯镜像、torch 专用镜像、HF 镜像，国外直连官方源，失败自动切换
- **启动管理**：一键启动/停止、实时日志、就绪自动开浏览器、端口占用/残留进程预检
- **模型工具**：Civitai / 哩布哩布链接下载、模型管理与哈希查询、批量补全信息
- **WD14 反推**：图片自动打标签，独立环境不污染 Forge
- **图片信息**：读取/编辑 A1111、ComfyUI 图片生成参数，可发回 WebUI
- **常用插件**一键安装（已按分支适配兼容版本）

## 📖 使用教程

**新手请直接看 [教程.md](教程.md)**——从"一台什么都没装的电脑"到"画出第一张图"的完整图文流程，以及常见问题 FAQ。

快速上手：

```bat
git clone https://github.com/wangjue520/forge-webui-launcher.git
cd forge-webui-launcher
start一键启动.bat
```

（或直接下载 ZIP 解压到**纯英文路径**后双击 `start一键启动.bat`）

## 运行环境要求

- Windows 10 / 11（依赖系统自带的 **Edge WebView2 运行时**；近几年的系统都预装了。
  极少数精简版系统没有的话，装一下微软官方的 WebView2 Runtime 即可：
  https://developer.microsoft.com/microsoft-edge/webview2/）
- Python 3.10+。**没有装 Python 也没关系**：双击启动时会自动下载一个免安装的
  便携版 Python 3.13 到启动器目录（python-build-standalone，uv 同款官方构建），
  全程无需手动操作、不需要管理员权限、不影响系统里已有环境。
  由 uv 等工具托管的 Python 会禁止 pip 直接安装依赖（报
  `externally-managed-environment`）；启动 bat 的依赖一律装进项目内的
  `.venv` 虚拟环境，不碰系统 Python，天然不受这个限制。

## 安装依赖

正常双击 `start一键启动.bat` 即可——它会自动在项目目录创建 `.venv` 虚拟环境
并把依赖装进去（不碰系统 Python，因此 uv 托管 / PEP 668 的 Python 也能用）。

手动安装的话同样建议走虚拟环境：

```bat
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

依赖只有两个：`pywebview`（界面）、`requests`（Civitai 下载/查询）。

## 启动

双击 `start一键启动.bat`，或：

```
python webview_main.py
```

## 更新启动器

不用再重新下载整个压缩包：打开「高级选项」页顶部的 **启动器更新** 卡片，
点「检查更新 → 立即更新」，完成后重启启动器即可。git 克隆安装的走增量
拉取（只下载变化的提交），ZIP 安装的走整包覆盖；两种方式都会保留你的
配置（`launcher_config.json`）、已下载的便携 Python/Git、WD14 环境和模型
缓存。版本号规则为 `2.0.提交数`，每次更新自动递增并写入项目根目录的
`version.json`。维护者打包发布前执行一次
`python updater.py --write-version` 可让 ZIP 用户拿到准确的初始版本号。

## 目录结构

| 文件 | 说明 |
|---|---|
| `webview_main.py` | 入口：创建窗口、处理关闭前确认 |
| `webview_api.py` | JS ↔ Python 桥接层：所有页面逻辑、子进程管理、事件推送 |
| `web/` | 前端（index.html / style.css / js/） |
| `config_manager.py` | 配置读写、命令行参数生成、便携环境检测 |
| `portable_env.py` | 便携版 Python/Git 下载、venv 检测、hashlib 补丁 |
| `updater.py` | 启动器自更新：版本号管理、GitHub 检查、git/ZIP 两种增量升级 |
| `version.json` | 当前版本号（每次更新自动重写；无 .git 的 ZIP 安装以此为准） |
| `bootstrap_python.ps1` | 系统没有 Python 时由启动 bat 调用，自动下载便携 Python 3.13 |
| `mirror_manager.py` | 国内镜像加速（GitHub 代理 / PyPI / HF 镜像） |
| `civitai_downloader.py` | Civitai 解析、下载、按哈希查询 |
| `safetensors_meta.py` | 模型文件头部元数据解析 |
| `image_meta_core.py` | PNG/JPG/WebP 生成参数提取（A1111 / ComfyUI） |
| `wd14_*.py` | WD14 反推：模型下载、独立 venv、推理 CLI |

## 从 PyQt 版迁移的注意事项

1. **依赖变了**：不再需要 PyQt5/PyQt6，改为 `pywebview + requests`。
2. **入口变了**：启动目标是 `webview_main.py`（不是原来的 main.py）。
3. **配置文件完全兼容**：`config.json` 的键没有改动，老配置直接可用。
4. **WebView2**：用户机器上基本都有；没有的话上面链接装一下即可。
5. 图片预览走 base64 内嵌，不再有 file:// 权限问题；预览图上限 15MB。

## 前端联调小技巧

用浏览器打开 `web/index.html?mock=1` 可以预览界面：`js/mock.js` 会注入一套
标注了"演示数据"的假数据，页面底部有常驻提示条。**不带 `?mock=1` 时 mock 不会
启用**——这是故意的：pywebview 桥接是异步注入的，靠"加载时还没有桥接对象"来
自动启用 mock 会和真实注入赛跑，慢机器上会把假数据当真。正式运行下若 2 秒后
桥接仍未建立，只显示"未能连接后端"提示条。支持 `?page=xxx` 直接定位到某个页面，
例如 `web/index.html?mock=1&page=models`。
