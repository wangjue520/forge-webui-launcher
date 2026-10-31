# -*- coding: utf-8 -*-
"""
自然语言反推的运行环境（PyTorch + transformers + bitsandbytes，约 4–5 GB）。

两边不重复装：装了 WWY LoRA 训练器、训练环境（<训练器>/env）也装好了，就直接用训练器的环境
（训练器自己的自然语言打标就跑在里面，依赖齐全）；没有训练器，或者训练器后来删了，启动器再自己建一个
独立的 vlm_venv（和 wd14_venv 一样跟其它 Python 环境隔离）。

自建环境的 PyTorch 跟训练器钉同一版（2.11.0），按显卡选 cu126 / cu128；torch 的 wheel 用启动器部署
WebUI 时那套断点续传下载（torch_bootstrap），缓存在 launcher_data/cache/wheels 和各实例共用。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from collections import deque

import config_manager as cm

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(APP_DIR, "vlm_venv")
TORCH_VERSION, TORCHVISION_VERSION = "2.11.0", "0.26.0"   # 和训练器 envsetup.py 一样
PACKAGES = ["transformers>=4.57", "accelerate", "bitsandbytes", "safetensors", "pillow", "numpy"]
NEED = ("torch", "transformers", "accelerate", "bitsandbytes", "PIL")   # site-packages 里要有的包目录
# 装完以后真 import 一遍（只在安装后跑：训练环境里冷启动 import torch + transformers 实测要一两分钟，平时不能每次都查）
CHECK = ("import torch, transformers, accelerate, bitsandbytes, PIL; "
         "import sys; sys.exit(0 if torch.cuda.is_available() else 3)")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class VenvError(Exception):
    pass


def venv_python_path():
    if os.name == "nt":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def trainer_env_python(trainer_root):
    root = (trainer_root or "").strip()
    if not root:
        return ""
    for p in (os.path.join(root, "env", "Scripts", "python.exe"), os.path.join(root, "env", "bin", "python")):
        if os.path.isfile(p):
            return p
    return ""


def site_packages(py):
    """venv 的 site-packages（Windows：<venv>/Lib/site-packages；Linux：<venv>/lib/pythonX.Y/site-packages）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(py)))
    sp = os.path.join(root, "Lib", "site-packages")
    if os.path.isdir(sp):
        return sp
    lib = os.path.join(root, "lib")
    try:
        for n in sorted(os.listdir(lib)):
            if n.startswith("python") and os.path.isdir(os.path.join(lib, n, "site-packages")):
                return os.path.join(lib, n, "site-packages")
    except OSError:
        pass
    return ""


def env_ok(py):
    """这个 Python 的环境里有没有跑自然语言模型要的包。只看包目录在不在（瞬间出结果）；
    包坏了 / 显卡用不了的话，加载模型那一步会报出具体原因。"""
    if not py or not os.path.isfile(py):
        return False
    sp = site_packages(py)
    return bool(sp) and all(os.path.isdir(os.path.join(sp, n)) for n in NEED)


def import_ok(py, runner=None):
    """真 import 一遍并确认能用显卡（慢，只在装完环境后跑）。"""
    runner = runner or subprocess.run
    try:
        return runner([py, "-I", "-c", CHECK], capture_output=True, timeout=600, creationflags=_NO_WINDOW).returncode == 0
    except Exception:
        return False


def pick(trainer_root):
    """现在能用的环境：("trainer" / "launcher", python)；都不能用返回 ("", "")。训练器的优先。"""
    t = trainer_env_python(trainer_root)
    if t and env_ok(t):
        return "trainer", t
    v = venv_python_path()
    if env_ok(v):
        return "launcher", v
    return "", ""


def status(trainer_root):
    """界面用的简单状态（不跑 import 检查，只看文件在不在，免得打开页面就卡几秒）。"""
    t = trainer_env_python(trainer_root)
    v = venv_python_path()
    return {"trainer_env": t, "launcher_env": v if os.path.isfile(v) else "",
            "source": "trainer" if t else ("launcher" if os.path.isfile(v) else "")}


def _run_streamed(cmd, log_cb=None, env=None, tail_lines=15):
    tail = deque(maxlen=tail_lines)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, creationflags=_NO_WINDOW)
    for raw in p.stdout:
        line = cm.decode_process_output(raw).rstrip("\r\n")
        if line:
            tail.append(line)
            if log_cb:
                log_cb(line)
    p.wait()
    return p.returncode, list(tail)


def _pip_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_") and k not in ("PYTHONHOME", "PYTHONPATH")}
    env.update(PIP_CONFIG_FILE=os.devnull, PIP_RETRIES="5", PIP_TIMEOUT="60", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return env


def _install_torch(py, tag, cfg, log, progress, cancel):
    """把 torch / torchvision 的 wheel 断点续传下好，再本地装进 vlm_venv。"""
    import mirror_manager as mm
    import portable_env as pe
    import torch_bootstrap as tb
    pytag, _ = tb._python_tag(py, log)
    if not pytag:
        raise VenvError("读不到独立环境的 Python 版本")
    official = f"{mm.PYTORCH_OFFICIAL_WHL}/{tag}"
    mirrors = [f"{b}/{tag}" for b in mm.PYTORCH_MIRROR_BASES]
    try:
        use_mirror = mm.resolve_mode(cfg)
    except Exception:
        use_mirror = True
    bases = mirrors + [official] if use_mirror else [official] + mirrors
    cache = os.path.join(APP_DIR, "launcher_data", "cache", "wheels")
    os.makedirs(cache, exist_ok=True)
    specs = [("torch", f"{TORCH_VERSION}+{tag}"), ("torchvision", f"{TORCHVISION_VERSION}+{tag}")]
    last = None
    for base in bases:
        try:
            wheels = []
            for name, ver in specs:
                url, sha = tb._resolve_wheel(base, name, ver, pytag, log)
                if not url:
                    raise VenvError(f"{base} 里没有 {name}=={ver}（{pytag}）")
                fname = url.rsplit("/", 1)[-1].replace("%2B", "+").replace("%2b", "+")
                dest = os.path.join(cache, fname)
                if not (os.path.exists(dest) and tb._sha256_ok(dest, sha)):
                    log(f"下载 {fname} …")
                    pe.download_file(url, dest, progress_cb=progress, cancel_flag=cancel, log_cb=log)
                    if not tb._sha256_ok(dest, sha):
                        for x in (dest, dest + ".part"):
                            try:
                                os.remove(x)
                            except OSError:
                                pass
                        raise VenvError(f"{fname} 下载后 sha256 校验不符")
                wheels.append(dest)
            log("安装 PyTorch …")
            rc, tail = _run_streamed([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location"]
                                     + (mm.pip_index_args(True, 0) if use_mirror else []) + wheels, log, _pip_env())
            if rc == 0:
                return
            last = "\n".join(tail[-6:])
        except Exception as e:
            if "取消" in str(e):
                raise VenvError("已取消")
            last = str(e)
            log(f"  {base} 失败：{last[:200]}，换下一个源")
    raise VenvError(f"PyTorch 安装失败：{last}")


def _install_packages(py, tag, cfg, log):
    import mirror_manager as mm
    try:
        use_mirror = mm.resolve_mode(cfg)
    except Exception:
        use_mirror = True
    attempts = [mm.pip_index_args(True, i) for i in range(len(mm.PYPI_MIRRORS))] + [[]] if use_mirror else [[]]
    # 钉住 torch：bitsandbytes / accelerate 有时会顺手把 torch 换成 PyPI 上的 CPU 版
    fd, cons = tempfile.mkstemp(suffix=".txt", prefix="vlm_pin_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"torch=={TORCH_VERSION}+{tag}\ntorchvision=={TORCHVISION_VERSION}+{tag}\n")
    try:
        last = []
        for i, extra in enumerate(attempts):
            if i:
                log("上一个源失败，换下一个源重试 …")
            rc, last = _run_streamed([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
                                      "-c", cons] + extra + PACKAGES, log, _pip_env())
            if rc == 0:
                return
        raise VenvError("依赖安装失败。pip 最后的输出：\n" + ("\n".join(last[-8:]) or "（无输出）"))
    finally:
        try:
            os.remove(cons)
        except OSError:
            pass


def ensure(trainer_root, cfg, torch_tag="cu128", log_cb=None, progress_cb=None, cancel_flag=None):
    """准备好能用的环境，返回 (来源, python)。训练器的能用就用训练器的；否则建 / 修启动器自己的 vlm_venv。"""
    log = log_cb or (lambda t: None)
    src, py = pick(trainer_root)
    if py:
        log("使用 LoRA 训练器的训练环境（不另外安装）" if src == "trainer" else "使用启动器的自然语言反推环境")
        return src, py
    if os.name != "nt":
        raise VenvError("启动器自建环境目前只支持 Windows；可以先装 LoRA 训练器，或者用云端 API")
    if trainer_env_python(trainer_root):
        log("训练器的训练环境用不了（缺依赖或不能用显卡），改用启动器自己的独立环境")
    try:
        import cuda_compat as cc
        tag = cc.effective_tag(torch_tag)
    except Exception:
        tag = torch_tag
    py = venv_python_path()
    if not os.path.isfile(py):
        log(f"创建独立环境 vlm_venv（PyTorch {TORCH_VERSION}+{tag}，约 4–5 GB，只需装一次）…")
        rc, tail = _run_streamed([sys.executable, "-m", "venv", VENV_DIR], log)
        if rc != 0 or not os.path.isfile(py):
            shutil.rmtree(VENV_DIR, ignore_errors=True)
            raise VenvError("创建独立环境失败：" + "\n".join(tail[-4:]))
    if cancel_flag and cancel_flag():
        raise VenvError("已取消")
    _install_torch(py, tag, cfg, log, progress_cb, cancel_flag)
    if cancel_flag and cancel_flag():
        raise VenvError("已取消")
    log("安装 transformers / accelerate / bitsandbytes …")
    _install_packages(py, tag, cfg, log)
    log("检查环境能不能用显卡 …")
    if not import_ok(py):
        raise VenvError("独立环境装好了，但 PyTorch 用不了显卡（显卡驱动太旧，或者不是 NVIDIA 显卡）。可以改用云端 API。")
    log("独立环境准备好了")
    return "launcher", py
