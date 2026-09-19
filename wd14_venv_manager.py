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
import shutil
import subprocess
import sys
from collections import deque

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(APP_DIR, "wd14_venv")

# 特意钉死 numpy 版本范围——这是实际踩过的坑：onnxruntime 的 C 扩展在 numpy 2.x
# 环境下有时会初始化失败，报一个没有任何消息内容的裸 ImportError，
# 很难排查，锁定版本范围直接避开这个问题。
# 但注意另一个方向的坑：numpy 1.x 最高只出到 cp312 的预编译包，
# Python 3.13+（bootstrap_python.ps1 下载的便携 Python 就是 3.13）装 numpy<2
# 只能退回去从源码编译，用户机器上没有编译器，所有镜像源都会以同样的方式失败，
# 表现为"换源/开关代理都没用"。Python 3.13 能装的 onnxruntime（1.19+）
# 已经修复了 numpy 2 兼容问题，所以 3.13+ 直接用 numpy 2.x。
DEPENDENCIES_COMMON = ["pillow", "onnxruntime", "requests"]


def _venv_py_version():
    """venv 里 Python 的 (major, minor)。查不到就退回启动器自身的版本。"""
    try:
        r = subprocess.run(
            [venv_python_path(), "-c",
             "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            major, minor = r.stdout.strip().split(".")[:2]
            return int(major), int(minor)
    except Exception:
        pass
    return sys.version_info[:2]


def dependency_specs():
    ver = _venv_py_version()
    numpy_spec = "numpy>=2,<3" if ver >= (3, 13) else "numpy<2"
    return [numpy_spec] + DEPENDENCIES_COMMON


class VenvError(Exception):
    pass


def venv_python_path():
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def is_venv_present():
    return os.path.exists(venv_python_path())


def _run_streamed(cmd, log_cb=None, tail_lines=15):
    """跑子过程并把输出逐行推到日志；返回 (返回码, 输出尾部几行)。
    尾部留给报错用——"安装失败"四个字用户没法排查，真正的原因在 pip 输出里。"""
    tail = deque(maxlen=tail_lines)
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    for line in process.stdout:
        line = line.rstrip("\n")
        if line:
            tail.append(line)
            if log_cb:
                log_cb(line)
    process.wait()
    return process.returncode, list(tail)


def _check_deps_importable():
    py = venv_python_path()
    result = subprocess.run(
        [py, "-c", "import onnxruntime, numpy, PIL, requests"],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _pip_available():
    """venv 里的 pip 是否可用。创建过程被中断的 venv 会有 python.exe 没 pip，
    只检查解释器存在会永远卡在"装依赖必失败"的状态，必须识别出来重建。"""
    try:
        r = subprocess.run([venv_python_path(), "-m", "pip", "--version"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def create_venv(log_cb=None):
    if is_venv_present():
        return
    base_python = sys.executable
    if log_cb:
        log_cb(f"正在创建独立虚拟环境 (使用 {base_python}) ...")
    rc, _ = _run_streamed([base_python, "-m", "venv", VENV_DIR], log_cb=log_cb)
    if rc != 0 or not is_venv_present():
        raise VenvError("创建虚拟环境失败，请检查系统 Python 是否完整（是否包含 venv 模块）")


def install_dependencies(log_cb=None, cfg=None):
    py = venv_python_path()
    deps = dependency_specs()
    if log_cb:
        log_cb(f"正在隔离环境中安装依赖: {', '.join(deps)} ...")
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

    last_tail = []
    for i, extra in enumerate(attempts):
        if log_cb and i > 0:
            log_cb("上一个源失败，换用下一个源重试 ...")
        rc, last_tail = _run_streamed(base + extra + deps, log_cb=log_cb)
        if rc == 0:
            return
    detail = "\n".join(last_tail[-8:])
    raise VenvError(
        "隔离环境依赖安装失败。pip 最后的输出：\n" + (detail or "（无输出）") +
        "\n\n排查建议：\n"
        "· 如果上面是超时/连接重置/SSLError：换网络，或开/关代理后重试\n"
        "· 如果是 No matching distribution / 需要编译：Python 版本太新，"
        "该版本的预编译包还没出，请到「关于」里反馈\n"
        "· 如果在用代理：确认代理允许命令行程序走（TUN/增强模式），"
        "或给终端设置 HTTP_PROXY/HTTPS_PROXY 环境变量")


def ensure_ready(log_cb=None, force_reinstall=False, cfg=None):
    """确保独立 venv 存在且依赖齐全，返回 venv 的 python 路径"""
    if is_venv_present() and not _pip_available():
        if log_cb:
            log_cb("检测到隔离环境不完整（缺少 pip，可能上次创建被中断），重建 ...")
        shutil.rmtree(VENV_DIR, ignore_errors=True)
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
