# -*- coding: utf-8 -*-
"""
WD14 反推功能的独立虚拟环境管理。

问题背景：一开始 WD14 反推直接在启动器自身的 Python 进程里 import onnxruntime，
这个进程往往就是用户系统里那个装了一堆别的 AI 工具（webui本体、mediapipe、
opencv、datasets...）的全局 Python。这些工具对 numpy/protobuf 版本的要求
互相打架，装 onnxruntime 时很容易把 numpy 升级/降级到跟其他工具不兼容的
版本，改一个坏一个。

解决办法：WD14 反推用到的 onnxruntime/numpy/pillow 全部装进一个跟系统环境
完全隔离的独立 venv（wd14_venv 文件夹）里，实际推理通过子进程调用这个 venv
的 python 来跑，主程序（启动器本体）自始至终都不 import onnxruntime，
两边环境互不影响，不会再出现"装好了这个坏了那个"的连锁反应。
"""
import os
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(APP_DIR, "wd14_venv")

# 特意钉死 numpy<2 ——这是实际踩过的坑：onnxruntime 的 C 扩展在 numpy 2.x
# 环境下有时会初始化失败，报一个没有任何消息内容的裸 ImportError，
# 很难排查，锁定版本范围直接避开这个问题。
DEPENDENCIES = ["numpy<2", "pillow", "onnxruntime", "requests"]


class VenvError(Exception):
    pass


def venv_python_path():
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def is_venv_present():
    return os.path.exists(venv_python_path())


def _run_streamed(cmd, log_cb=None):
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    for line in process.stdout:
        line = line.rstrip("\n")
        if line and log_cb:
            log_cb(line)
    process.wait()
    return process.returncode


def _check_deps_importable():
    py = venv_python_path()
    result = subprocess.run(
        [py, "-c", "import onnxruntime, numpy, PIL, requests"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def create_venv(log_cb=None):
    if is_venv_present():
        return
    base_python = sys.executable
    if log_cb:
        log_cb(f"正在创建独立虚拟环境 (使用 {base_python}) ...")
    rc = _run_streamed([base_python, "-m", "venv", VENV_DIR], log_cb=log_cb)
    if rc != 0 or not is_venv_present():
        raise VenvError("创建虚拟环境失败，请检查系统 Python 是否完整（是否包含 venv 模块）")


def install_dependencies(log_cb=None, cfg=None):
    py = venv_python_path()
    if log_cb:
        log_cb(f"正在隔离环境中安装依赖: {', '.join(DEPENDENCIES)} ...")
    base = [py, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location"]

    # 国内网络下依次尝试各个 PyPI 镜像，全部失败再用默认源兜底
    attempts = [[]]
    if cfg is not None:
        try:
            import mirror_manager as mm
            if mm.resolve_mode(cfg, log_cb):
                attempts = [mm.pip_index_args(True, i) for i in range(len(mm.PYPI_MIRRORS))] + [[]]
        except Exception:
            pass

    for i, extra in enumerate(attempts):
        if log_cb and i > 0:
            log_cb("上一个源失败，换用下一个源重试 ...")
        rc = _run_streamed(base + extra + DEPENDENCIES, log_cb=log_cb)
        if rc == 0:
            return
    raise VenvError("隔离环境依赖安装失败，请检查网络连接")


def ensure_ready(log_cb=None, force_reinstall=False, cfg=None):
    """确保独立 venv 存在且依赖齐全，返回 venv 的 python 路径"""
    if not is_venv_present():
        create_venv(log_cb)
        install_dependencies(log_cb, cfg)
    elif force_reinstall or not _check_deps_importable():
        if log_cb:
            log_cb("检测到隔离环境依赖不完整，重新安装 ...")
        install_dependencies(log_cb, cfg)
    else:
        if log_cb:
            log_cb("独立虚拟环境已就绪，跳过重复安装")
    return venv_python_path()
