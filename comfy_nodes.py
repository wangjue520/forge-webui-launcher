# -*- coding: utf-8 -*-
"""
ComfyUI 常用节点：目录表 + 安装器（部署新实例时自动装、「常用插件」页手动装共用）。

选择依据（2026-10）：ComfyUI-Manager 节点库 github-stats.json 的星数 / 最近更新，
加上几篇 2026 年的「必装节点」盘点；偏向二次元出图 + 常见视频工作流会用到的、
依赖轻且仍在维护的包。默认勾选（default=True）的会随「环境部署」一起装好。

和 Forge 扩展不同，ComfyUI 不会自动装节点的 Python 依赖（ComfyUI-Manager
装节点时才会帮你装），所以这里每装一个节点都会：
  1. git clone（需要子模块的带 --recursive；国内走 GitHub 加速代理，用
     url.insteadOf 改写，子模块地址也一起走代理）
  2. pip install -r requirements.txt（用该实例自己的 Python；跳过 torch 系，
     防止节点把 CUDA 版 torch 换成 CPU 版；git+https://github.com 依赖同样走代理）
  3. 有 install.py 的再跑一次（Manager 的做法，Impact Pack 靠它准备模型/依赖）
单个节点失败只记日志、继续装下一个，不影响整体。

ComfyUI-Manager 现在已经并进 ComfyUI 本体：pip 装 manager_requirements.txt，
启动时加 --enable-manager（启动参数由 config_manager.build_comfy_args 自动加）。
老版本 ComfyUI 没有 manager_requirements.txt 时退回老办法 clone 到 custom_nodes。
"""
import os
import re
import shutil
import tempfile

MANAGER_ID = "comfyui-manager"
MANAGER_LEGACY_URL = "https://github.com/Comfy-Org/ComfyUI-Manager.git"
MANAGER_LEGACY_FOLDER = "comfyui-manager"

# group: core = 推荐（部署时默认装） / extra = 按需 / author = 作者自制
CATALOG = [
    # ---------- 推荐 ----------
    {"id": MANAGER_ID, "name": "ComfyUI-Manager（节点管理器）", "group": "core", "default": True,
     "desc": "在 ComfyUI 网页里搜索 / 安装 / 更新其它节点，打开别人的工作流时一键补装缺失节点。"
             "现已内置进 ComfyUI，这里装好依赖后启动器会自动加 --enable-manager 启用。"
             "新版界面的入口：左侧栏「节点」面板就是节点管理器（老版那个 Manager 按钮没了），"
             "也可以在命令面板里搜 Manager（拼图图标）；装了以后记得刷新一下网页。"
             "想要老版 Manager 界面的话，在高级选项的额外参数里加 --enable-manager-legacy-ui 重启即可。",
     "url": "", "folder": ""},
    {"id": "rgthree-comfy", "name": "rgthree-comfy", "group": "core", "default": True,
     "desc": "工作流整理神器：多 LoRA 堆叠加载、分组一键开关、种子控制、显示中间值，大工作流必备。",
     "url": "https://github.com/rgthree/rgthree-comfy.git", "folder": "rgthree-comfy"},
    {"id": "custom-scripts", "name": "ComfyUI-Custom-Scripts（pysssss）", "group": "core", "default": True,
     "desc": "提示词里输 tag / LoRA 名自动补全、图片预览增强、工作流小工具。",
     "url": "https://github.com/pythongosssss/ComfyUI-Custom-Scripts.git", "folder": "ComfyUI-Custom-Scripts"},
    {"id": "impact-pack", "name": "ComfyUI-Impact-Pack", "group": "core", "default": True,
     "desc": "FaceDetailer 等检测 + 局部重绘节点，相当于 WebUI 的 ADetailer（修脸、修手）。依赖较多，首次安装会久一点。",
     "url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack.git", "folder": "ComfyUI-Impact-Pack"},
    {"id": "impact-subpack", "name": "ComfyUI-Impact-Subpack", "group": "core", "default": True,
     "desc": "Impact Pack 的 YOLO 检测器（UltralyticsDetectorProvider）——FaceDetailer 用 face_yolov8 这类模型检测脸必须装它。",
     "url": "https://github.com/ltdrdata/ComfyUI-Impact-Subpack.git", "folder": "ComfyUI-Impact-Subpack"},
    {"id": "ultimate-sd-upscale", "name": "ComfyUI_UltimateSDUpscale", "group": "core", "default": True,
     "desc": "分块放大重绘（WebUI 里的 Ultimate SD Upscale 脚本），大图高清化常用。",
     "url": "https://github.com/ssitu/ComfyUI_UltimateSDUpscale.git", "folder": "ComfyUI_UltimateSDUpscale",
     "recursive": True},
    {"id": "videohelpersuite", "name": "ComfyUI-VideoHelperSuite", "group": "core", "default": True,
     "desc": "视频读取 / 拆帧 / 合成 mp4、gif，Wan 等视频工作流基本都要用到。",
     "url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git", "folder": "ComfyUI-VideoHelperSuite"},
    {"id": "kjnodes", "name": "ComfyUI-KJNodes", "group": "core", "default": True,
     "desc": "kijai 的实用节点合集（尺寸计算、遮罩、批处理、视频辅助），大量网传工作流依赖它。",
     "url": "https://github.com/kijai/ComfyUI-KJNodes.git", "folder": "ComfyUI-KJNodes"},
    # ---------- 按需 ----------
    {"id": "controlnet-aux", "name": "comfyui_controlnet_aux", "group": "extra",
     "desc": "ControlNet 预处理器（线稿、深度、OpenPose 姿势等 50+ 种）。预处理模型在第一次使用时下载。",
     "url": "https://github.com/Fannovel16/comfyui_controlnet_aux.git", "folder": "comfyui_controlnet_aux"},
    {"id": "inpaint-cropandstitch", "name": "ComfyUI-Inpaint-CropAndStitch", "group": "extra",
     "desc": "局部重绘时只裁出遮罩附近放大重绘再贴回，相当于 WebUI 的「仅蒙版区域」，小区域修图更清晰。",
     "url": "https://github.com/lquesada/ComfyUI-Inpaint-CropAndStitch.git", "folder": "ComfyUI-Inpaint-CropAndStitch"},
    {"id": "wd14-tagger", "name": "ComfyUI-WD14-Tagger", "group": "extra",
     "desc": "在工作流里用 WD14 反推图片的 danbooru tag。",
     "url": "https://github.com/pythongosssss/ComfyUI-WD14-Tagger.git", "folder": "ComfyUI-WD14-Tagger"},
    {"id": "image-saver", "name": "ComfyUI-Image-Saver", "group": "extra",
     "desc": "按 WebUI 格式把生成参数写进图片——传 C 站能识别模型 / LoRA，「输出管理」「图片信息」也能读得更全。",
     "url": "https://github.com/alexopus/ComfyUI-Image-Saver.git", "folder": "ComfyUI-Image-Saver"},
    {"id": "gguf", "name": "ComfyUI-GGUF", "group": "extra",
     "desc": "加载 GGUF 量化模型（Flux / Wan / Qwen 等的小显存版本）。",
     "url": "https://github.com/city96/ComfyUI-GGUF.git", "folder": "ComfyUI-GGUF"},
    {"id": "easy-use", "name": "ComfyUI-Easy-Use", "group": "extra",
     "desc": "国内很流行的简化节点包（一体化加载器 / 采样器），很多国内分享的工作流会用。依赖较多。",
     "url": "https://github.com/yolain/ComfyUI-Easy-Use.git", "folder": "ComfyUI-Easy-Use", "recursive": True},
    {"id": "crystools", "name": "ComfyUI-Crystools", "group": "extra",
     "desc": "网页顶部实时显示 CPU / 显卡 / 显存占用和出图进度。",
     "url": "https://github.com/crystian/ComfyUI-Crystools.git", "folder": "ComfyUI-Crystools"},
    {"id": "ipadapter-plus", "name": "ComfyUI_IPAdapter_plus", "group": "extra",
     "desc": "用参考图控制画风 / 人物（IPAdapter）。作者已停止更新，SD1.5 / SDXL 仍可正常使用。",
     "url": "https://github.com/cubiq/ComfyUI_IPAdapter_plus.git", "folder": "ComfyUI_IPAdapter_plus"},
    {"id": "lora-manager", "name": "ComfyUI-Lora-Manager", "group": "extra",
     "desc": "带预览图的 LoRA 浏览器，可直接看 Civitai 信息并把 LoRA 拖进工作流。",
     "url": "https://github.com/willmiao/ComfyUI-Lora-Manager.git", "folder": "ComfyUI-Lora-Manager"},
    # ---------- 作者自制 ----------
    {"id": "neo-sampler", "name": "ComfyUI-Neo-Sampler", "group": "author",
     "desc": "把 Forge Neo 的采样流程搬进 ComfyUI：同样的参数出和 Neo 一样的图（文本编码 / 初始噪声 / 采样器全复刻）。",
     "url": "https://github.com/wangjue520/ComfyUI-Neo-Sampler.git", "folder": "ComfyUI-Neo-Sampler"},
    {"id": "inpaint-webui-style", "name": "ComfyUI-Inpaint-WebUI-Style", "group": "author",
     "desc": "WebUI 风格的局部重绘：蒙版羽化、四种蒙版内容模式、只改蒙版区域不偏色。",
     "url": "https://github.com/wangjue520/ComfyUI-Inpaint-WebUI-Style.git", "folder": "ComfyUI-Inpaint-WebUI-Style"},
]

GROUP_LABELS = {"core": "推荐（部署 ComfyUI 时默认安装）", "extra": "按需安装", "author": "作者自制"}

# 节点 requirements 里这些包一律跳过：环境里已有 CUDA 版，被节点按 PyPI 默认
# 重装会变成 CPU 版（ComfyUI-Manager 也是这么处理的）
_SKIP_PKGS = {"torch", "torchvision", "torchaudio", "xformers"}


def find(node_id):
    for n in CATALOG:
        if n["id"] == node_id:
            return n
    return None


def default_ids():
    return [n["id"] for n in CATALOG if n.get("default")]


def custom_nodes_dir(comfy_dir):
    return os.path.join(comfy_dir, "custom_nodes")


def site_packages_dirs(py):
    """某个 python.exe 对应的 site-packages 候选（便携 / 整合包 / venv / Linux venv）"""
    if not py:
        return []
    d = os.path.dirname(os.path.abspath(py))
    cands = [os.path.join(d, "Lib", "site-packages"),
             os.path.join(os.path.dirname(d), "Lib", "site-packages")]
    for base in (d, os.path.dirname(d)):
        lib = os.path.join(base, "lib")
        if os.path.isdir(lib):
            for name in os.listdir(lib):
                if name.startswith("python"):
                    cands.append(os.path.join(lib, name, "site-packages"))
    return cands


def manager_builtin_supported(comfy_dir):
    return bool(comfy_dir) and os.path.isfile(os.path.join(comfy_dir, "manager_requirements.txt"))


def manager_pip_installed(py):
    return any(os.path.isdir(os.path.join(sp, "comfyui_manager")) for sp in site_packages_dirs(py))


def _node_keys(node):
    """一个目录项可能对应的 (仓库 slug 集合, 文件夹键集合)"""
    import version_manager as vm
    slugs, keys = set(), set()
    if node["url"]:
        slugs.add(vm.repo_slug(node["url"]))
        keys.add(vm.folder_key(vm.repo_name(node["url"])))
    for f in [node.get("folder")] + list(node.get("aliases") or []):
        if f:
            keys.add(vm.folder_key(f))
    return slugs, keys


def match_installed(node, scanned):
    """
    在 scan_repos() 的结果里找这个节点：git 远程地址是同一个仓库，或者文件夹名对得上
    （忽略大小写、.disabled、zip 解压自带的 -main/-master）。用户自己在 ComfyUI 里
    用 Manager / git clone / 下载 zip 装的都能认出来。
    """
    # 文件夹名对上但仓库不同（别人的分叉）也算装了：同名目录在，再 clone 也会失败
    slugs, keys = _node_keys(node)
    return [r for r in scanned if (r["slug"] and r["slug"] in slugs) or r["key"] in keys]


def is_installed(node, comfy_dir, py, scanned=None):
    if not comfy_dir:
        return False
    cn = custom_nodes_dir(comfy_dir)
    if node["id"] == MANAGER_ID:
        # 支持内置 Manager 的新版 ComfyUI 会无视甚至拒绝 custom_nodes 里的旧版
        # Manager（整合包常自带一个），那种情况下只有 pip 依赖装好才算真装上了
        if manager_builtin_supported(comfy_dir):
            return manager_pip_installed(py)
        return any(os.path.isdir(os.path.join(cn, f)) for f in ("comfyui-manager", "ComfyUI-Manager"))
    if scanned is None:
        import version_manager as vm
        scanned = vm.scan_repos(cn)
    return bool(match_installed(node, scanned))


def list_status(comfy_dir, py):
    import version_manager as vm
    scanned = vm.scan_repos(custom_nodes_dir(comfy_dir)) if comfy_dir else []
    out = []
    for n in CATALOG:
        hits = [] if n["id"] == MANAGER_ID else match_installed(n, scanned)
        installed = is_installed(n, comfy_dir, py, scanned)
        out.append({"id": n["id"], "name": n["name"], "desc": n["desc"], "group": n["group"],
                    "default": bool(n.get("default")), "installed": installed,
                    "folders": [h["folder"] for h in hits],
                    "disabled": bool(hits) and all(h["disabled"] for h in hits),
                    "url": n["url"], "folder": n["folder"]})
    return out, scanned


def _filtered_requirement_lines(req_path, gh_prefix):
    """读 requirements：去掉 torch 系，github 依赖按 gh_prefix 改写成代理地址。
    返回 (lines, has_gh)；读不到文件或没有有效内容时 lines 为空。"""
    try:
        with open(req_path, "r", encoding="utf-8", errors="replace") as f:
            raw_lines = f.read().splitlines()
    except OSError:
        return [], False
    keep = []
    has_gh = False
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~;\[\s@]", line, maxsplit=1)[0].strip().lower()
        if name in _SKIP_PKGS:
            continue
        if "https://github.com/" in line:
            has_gh = True
            if gh_prefix:
                line = line.replace("https://github.com/", gh_prefix)
        keep.append(line)
    return keep, has_gh


def _write_temp_req(lines):
    fd, path = tempfile.mkstemp(prefix="node_req_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _torch_constraints(py, env):
    """
    把环境里现有的 torch 系版本写成 pip 约束文件。节点依赖里要是有包要求
    更新的 torch，pip 会去 PyPI 拉——Windows 上那是 CPU 版，装上就废了显卡。
    有了约束，pip 宁可报错（随后逐个安装时只跳过那一个包）也不会动 torch。
    """
    if not py or not os.path.isfile(py):
        return None
    code = ("import importlib.metadata as m\n"
            "for n in ('torch','torchvision','torchaudio','xformers'):\n"
            "    try: print(n + '==' + m.version(n))\n"
            "    except Exception: pass\n")
    try:
        import subprocess
        p = subprocess.run([py, "-s", "-c", code], capture_output=True, text=True, timeout=120, env=env,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        pins = [x.strip() for x in p.stdout.splitlines() if "==" in x]
    except Exception:
        return None
    if not pins:
        return None
    fd, path = tempfile.mkstemp(prefix="torch_pin_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(pins) + "\n")
    return path


class DepInstaller:
    """
    往某个 ComfyUI 的 Python 里装依赖（节点安装、节点换版本、ComfyUI 本体换版本共用）。
      run(program, args, cwd, env, timeout) -> 退出码（调用方负责流式日志 / 取消 / 超时）
      gh_proxies: GitHub 加速代理前缀列表（如 "https://ghfast.top"），按顺序尝试，最后直连
      pip_mirror_argsets: pip 镜像参数组列表（如 [["-i", 清华], ["-i", 阿里]]），
        按顺序尝试，官方源永远自动兜底
    用完调用 close() 删掉临时的 torch 约束文件。
    """

    def __init__(self, py, run, log, env=None, cancelled=lambda: False,
                 gh_proxies=(), pip_mirror_argsets=None):
        self.py, self.run, self.log, self.cancelled = py, run, log, cancelled
        self.env = env
        self.sources = [p.rstrip("/") + "/https://github.com/" for p in gh_proxies] + [""]
        self.mirrors = [list(a) for a in (pip_mirror_argsets or [])]
        self.pins = _torch_constraints(py, env)

    def close(self):
        if self.pins:
            try:
                os.remove(self.pins)
            except OSError:
                pass
            self.pins = None

    def ok(self):
        return bool(self.py and os.path.isfile(self.py))

    def pip(self, args, cwd, what):
        if not self.ok():
            return 1
        base = [self.py, "-s", "-m", "pip", "install", "--disable-pip-version-check"]
        if self.pins:
            base += ["-c", self.pins]
        # 逐个镜像试，官方源永远兜底：单个镜像缺包/同步延迟/限速时
        # 不至于直接整批失败（国内部署最常见的死因）
        attempts = self.mirrors + [[]]
        rc = 1
        for i, extra in enumerate(attempts):
            if self.cancelled():
                break
            if i > 0:
                self.log(f"[节点] {what}：这个源装不上，换下一个源重试 ...\n")
            rc = self.run(base[0], base[1:] + list(args) + extra, cwd, self.env, 3600)
            if rc == 0:
                break
        return rc

    def requirements(self, req, cwd, label):
        """装一份 requirements.txt（去掉 torch 系）。返回装不上的依赖列表"""
        lines, has_gh = _filtered_requirement_lines(req, "")
        if not lines:
            return []
        log, cancelled = self.log, self.cancelled
        # 含 git+ 依赖的：每个代理重生成一份改写后的清单逐个试；
        # 纯 PyPI 依赖的只有一份，失败直接进逐行安装
        prefixes = self.sources if has_gh else [""]
        for prefix in prefixes:
            if cancelled():
                return []
            attempt = _filtered_requirement_lines(req, prefix)[0] if prefix else lines
            tmp = _write_temp_req(attempt)
            try:
                log(f"[节点] 安装 {label} 的依赖 ...\n")
                if self.pip(["-r", tmp], cwd, label) == 0:
                    return []
                if prefix != prefixes[-1]:
                    log(f"[节点] {label}：经该地址安装失败，换下一个地址重试 ...\n")
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        if cancelled():
            return []
        # 整份 requirements 里只要有一行装不上（比如要现场编译的包），
        # pip 会整批放弃；退回逐行装，能装的尽量装上
        log(f"[节点] {label}：整体安装失败，改为逐个安装依赖 ...\n")
        bad = []
        for line in lines:
            if cancelled():
                break
            if self.pip([line], cwd, label) != 0:
                bad.append(line)
        if bad:
            log(f"[节点] {label}：这些依赖没装上：{'、'.join(bad)}\n")
        return bad

    def node_deps(self, node_dir, label):
        """装节点依赖（requirements.txt + install.py）。返回 None = 全部装好；否则返回失败原因列表"""
        problems = []
        req = os.path.join(node_dir, "requirements.txt")
        if os.path.isfile(req):
            problems.extend(self.requirements(req, node_dir, label))
        if self.cancelled():
            return problems or None
        inst_py = os.path.join(node_dir, "install.py")
        if os.path.isfile(inst_py) and self.ok():
            self.log(f"[节点] 运行 {label} 的 install.py ...\n")
            if self.run(self.py, ["-s", "install.py"], node_dir, self.env, 1800) != 0:
                problems.append("install.py 执行失败")
        return problems or None


def node_env(comfy_dir, env=None):
    """装节点 / 节点依赖时用的环境变量"""
    env = dict(env or os.environ)
    env.setdefault("COMFYUI_PATH", comfy_dir)
    env.setdefault("COMFYUI_MODEL_PATH", os.path.join(comfy_dir, "models"))
    env["SAM2_BUILD_CUDA"] = "0"           # Impact Pack 依赖的 sam2：不编译 CUDA 扩展（Windows 上没有编译器会失败）
    env["SAM2_BUILD_ALLOW_ERRORS"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def sync_comfy_requirements(comfy_dir, py, run, log, env=None, cancelled=lambda: False,
                            gh_proxies=(), pip_mirror_argsets=None):
    """
    ComfyUI 本体换版本后补装依赖：ComfyUI 启动时不会自己装（新版前端包
    comfyui-frontend-package 的版本号每个版本都会变，不装就是旧界面甚至打不开）。
    已经装了内置 Manager 的，manager_requirements.txt 也一起更新。返回装不上的依赖列表
    """
    dep = DepInstaller(py, run, log, node_env(comfy_dir, env), cancelled, gh_proxies, pip_mirror_argsets)
    try:
        if not dep.ok():
            log("[版本] 找不到这个 ComfyUI 用的 Python，依赖没法自动补装\n")
            return ["（找不到 Python）"]
        bad = dep.requirements(os.path.join(comfy_dir, "requirements.txt"), comfy_dir, "ComfyUI")
        mreq = os.path.join(comfy_dir, "manager_requirements.txt")
        if os.path.isfile(mreq) and manager_pip_installed(py) and not cancelled():
            bad += dep.requirements(mreq, comfy_dir, "ComfyUI-Manager")
        return bad
    finally:
        dep.close()


def install_node_deps(node_dir, comfy_dir, py, run, log, env=None, cancelled=lambda: False,
                      gh_proxies=(), pip_mirror_argsets=None):
    """节点换版本后重装它的依赖。返回 None = 全部装好；否则返回失败原因列表"""
    dep = DepInstaller(py, run, log, node_env(comfy_dir, env), cancelled, gh_proxies, pip_mirror_argsets)
    try:
        return dep.node_deps(node_dir, os.path.basename(node_dir))
    finally:
        dep.close()


def install(ids, comfy_dir, py, git_exe, run, log, env=None, cancelled=lambda: False,
            gh_proxies=(), pip_mirror_argsets=None):
    """
    装一组节点。参数含义见 DepInstaller。
    返回 {"ok": [...], "failed": [(id, 原因)], "skipped": [...]}
    """
    res = {"ok": [], "failed": [], "skipped": []}
    if not comfy_dir or not os.path.isdir(comfy_dir):
        res["failed"].append(("*", "找不到 ComfyUI 目录"))
        return res
    if not py or not os.path.isfile(py):
        log("[节点] 找不到这个 ComfyUI 用的 Python，节点依赖装不了（节点本身照样下载）\n")
    cn = custom_nodes_dir(comfy_dir)
    os.makedirs(cn, exist_ok=True)
    env = node_env(comfy_dir, env)
    dep = DepInstaller(py, run, log, env, cancelled, gh_proxies, pip_mirror_argsets)
    sources = dep.sources
    pip = dep.pip
    install_deps = dep.node_deps

    for nid in ids:
        if cancelled():
            break
        node = find(nid)
        if not node:
            continue
        if is_installed(node, comfy_dir, py):
            res["skipped"].append(nid)
            continue
        label = node["name"]
        log(f"\n[节点] ===== {label} =====\n")

        # ComfyUI-Manager：新版内置（pip），老版本 clone
        if nid == MANAGER_ID and manager_builtin_supported(comfy_dir):
            rc = pip(["-r", os.path.join(comfy_dir, "manager_requirements.txt")], comfy_dir, label)
            # pip 返回 0 不代表包真的进去了（比如被杀软删了文件），以实际检测到为准
            if rc == 0 and manager_pip_installed(py):
                # custom_nodes 里的旧版 Manager（整合包常自带）会跟内置版冲突，
                # 新版 ComfyUI 会报错拒载——改名停用（可逆：改回原名即恢复）
                for old in ("ComfyUI-Manager", "comfyui-manager"):
                    old_dir = os.path.join(cn, old)
                    if os.path.isdir(old_dir):
                        disabled = old_dir + ".disabled"
                        try:
                            if os.path.exists(disabled):
                                shutil.rmtree(disabled, ignore_errors=True)
                            os.rename(old_dir, disabled)
                            log(f"[节点] 已停用 custom_nodes 里的旧版 Manager（{old} → {old}.disabled），"
                                "避免和内置版冲突\n")
                        except OSError as e:
                            log(f"[节点] 旧版 Manager 文件夹改名失败（{e}），请手动删除 custom_nodes\\{old}\n")
                log("[节点] ComfyUI-Manager 已安装，启动 ComfyUI 时会自动加 --enable-manager 启用\n")
                res["ok"].append(nid)
            else:
                res["failed"].append((nid, "依赖安装失败" if rc != 0 else "装完但未检测到 comfyui_manager 包"))
            continue
        url = node["url"] or MANAGER_LEGACY_URL
        folder = node["folder"] or MANAGER_LEGACY_FOLDER
        target = os.path.join(cn, folder)

        cloned = False
        for prefix in sources:
            if cancelled():
                break
            args = []
            if prefix:
                args += ["-c", f"url.{prefix}.insteadOf=https://github.com/"]
            args += ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60",
                     "clone", "--depth", "1"]
            if node.get("recursive"):
                args += ["--recursive", "--shallow-submodules"]
            args += [url, target]
            log(f"[节点] 下载{'（经 ' + prefix.split('/https://')[0] + ' 加速）' if prefix else ''} ...\n")
            rc = run(git_exe, args, cn, env, 900)
            if rc == 0 and os.path.isdir(target):
                cloned = True
                break
            shutil.rmtree(target, ignore_errors=True)   # 半截的目录会让下一次 clone 直接报「目录已存在」
            if not cancelled():
                log(f"[节点] 这个地址下载失败（退出码 {rc}），换下一个 ...\n")
        if cancelled():
            shutil.rmtree(target, ignore_errors=True)
            break
        if not cloned:
            res["failed"].append((nid, "下载失败"))
            continue
        problems = install_deps(target, label)
        if problems is None:
            res["ok"].append(nid)
        else:
            # 节点文件已经在了，只是依赖没装全：ComfyUI 启动时该节点可能导入失败，
            # 之后可以用 Manager 的「修复」或重新点安装（先删掉文件夹）
            res["failed"].append((nid, "依赖没装全（节点已下载）：" + "、".join(problems)))
    dep.close()
    return res


def summary_text(res):
    parts = []
    if res["ok"]:
        parts.append(f"成功 {len(res['ok'])} 个")
    if res["skipped"]:
        parts.append(f"已装过跳过 {len(res['skipped'])} 个")
    if res["failed"]:
        names = "、".join((find(i) or {"name": i})["name"] + f"（{why}）" for i, why in res["failed"])
        parts.append(f"失败 {len(res['failed'])} 个：{names}")
    return "；".join(parts) or "没有需要安装的节点"
