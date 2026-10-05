# -*- coding: utf-8 -*-
"""
在这台电脑上找 WebUI / ComfyUI 的安装目录（给「换了电脑」「第一次用」时自动填根目录用）。

只看各个盘根目录往下 3 层、外加 桌面 / 下载 / 文档 往下 3 层，秋叶整合包这种
F:\\sd-forge-aki\\sd-webui-forge-aki-v1.0\\sd-webui-forge-aki 正好在第 3 层。
系统目录、模型/输出/扩展这类大目录一律不进，总时长有上限，找到多少算多少。
"""
import os
import string
import time

import config_manager as cm

MAX_DEPTH = 3
TIME_BUDGET = 4.0     # 秒

_SKIP = {
    "windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
    "system volume information", "recovery", "perflogs", "appdata", "$windows.~bt", "$windows.~ws",
    "msocache", "intel", "amd", "nvidia", "drivers", "config.msi", "onedrivetemp",
    # 安装目录内部 / 体积大的目录：进去也不会有另一个安装
    "node_modules", ".git", "venv", ".venv", "python", "python_embeded", "git", "site-packages",
    "models", "outputs", "output", "extensions", "extensions-builtin", "custom_nodes", "__pycache__",
    "repositories", "embeddings", "input", "temp", "tmp", "cache", ".cache", "steamapps",
}


def _roots():
    out = []
    if os.name == "nt":
        for letter in string.ascii_uppercase[2:]:          # C: 往后
            d = f"{letter}:\\"
            if os.path.isdir(d):
                out.append(d)
        home = os.path.expanduser("~")
        for sub in ("Desktop", "Downloads", "Documents", "桌面"):
            p = os.path.join(home, sub)
            if os.path.isdir(p):
                out.append(p)
    else:
        out.append(os.path.expanduser("~"))
    return out


def _branch_of(root, kind):
    if kind == "comfyui":
        return "comfyui"
    return guess_forge_branch(root)


def guess_forge_branch(root):
    """Neo（新参数体系 neo2）还是 Classic：先看 git 远程，没有 .git（整合包）就看源码特征"""
    try:
        with open(os.path.join(root, ".git", "config"), "r", encoding="utf-8", errors="replace") as f:
            txt = f.read().lower()
        if "sd-webui-forge-classic" in txt:
            return "neo2"
        if "stable-diffusion-webui-forge" in txt:
            return "classic"
    except OSError:
        pass
    # Neo 才有 --sage / --flash 这类参数；lllyasviel 原版 Forge（秋叶 forge 整合包多是它）没有
    try:
        with open(os.path.join(root, "backend", "args.py"), "r", encoding="utf-8", errors="replace") as f:
            if "--sage" in f.read():
                return "neo2"
        return "classic"
    except OSError:
        pass
    return "neo2" if os.path.isfile(os.path.join(root, "pyproject.toml")) else "classic"


def find_installs(budget=TIME_BUDGET):
    """返回 [{"path", "kind": "forge"/"comfyui", "branch", "label"}]，按路径排序、去重"""
    t_end = time.monotonic() + budget
    found, seen = [], set()

    def visit(d, depth):
        if time.monotonic() > t_end:
            return
        kind = cm.detect_kind(d)
        if kind:
            real = os.path.normcase(os.path.abspath(cm.comfy_layout(d)[0] if kind == "comfyui" else d))
            # ComfyUI 便携包外层和里层 ComfyUI 是同一个安装，只记一次（记用户更习惯选的外层）
            if real not in seen:
                seen.add(real)
                branch = _branch_of(d, kind)
                found.append({"path": d, "kind": kind, "branch": branch,
                              "label": cm.KIND_LABELS.get(branch, "WebUI")})
            return                                          # 安装目录里面不再往下找
        if depth >= MAX_DEPTH:
            return
        try:
            with os.scandir(d) as it:
                subs = [e.path for e in it
                        if e.is_dir(follow_symlinks=False) and e.name.lower() not in _SKIP
                        and not e.name.startswith((".", "$"))]
        except OSError:
            return
        for sub in subs:
            visit(sub, depth + 1)

    for r in _roots():
        visit(r, 0)
    found.sort(key=lambda x: x["path"].lower())
    return found
