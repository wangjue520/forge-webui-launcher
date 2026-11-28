# -*- coding: utf-8 -*-
"""
Python 包变化记录：每次环境可能变的时候拍一次「装了哪些包、什么版本」，跟上一次比，有变化就记一条。
只记录、不回退——报错多半是新装的东西引起的，而用户装新东西是为了用新功能，退回去等于白装；
记录交给报错诊断（diagnose_llm）当线索：「哪次操作之后多了 / 换了哪些包」，AI 据此找让新旧都能用的办法。

什么时候拍：
  · 启动器跑 pip install / uninstall 的前后（插件管理、版本管理、诊断、部署、整合包都经过
    webview_api._stream_cmd / _deploy_run_cmd）。操作前那次专门收「两次记录之间别人改的」
    （手动 pip、ComfyUI-Manager、WebUI 自己装的），这样每条变化都记在真正的来源下面
  · 实例启动前、就绪后（WebUI 启动时会自己装 / 升级依赖）、进程结束后（运行期间 ComfyUI-Manager 装的）
每个 Python 环境一个文件：launcher_data/pip_history/<路径哈希>.json
  {"python", "latest": {包名: 版本}, "latest_time", "entries": [{"time","reason","added","removed","changed"}]}
第一次只记基准（"baseline": true，包数），不把几百个包列进变化里。
这里的函数都不抛异常：记不下来不能影响装包、启动这些正事。
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_DIR = os.path.join(APP_DIR, "launcher_data", "pip_history")
MAX_ENTRIES = 80
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

# 只读包名和版本，比 pip freeze 快得多（不导入 pip）
_LIST_CODE = ("import json,importlib.metadata as m\n"
              "out={}\n"
              "for d in m.distributions():\n"
              "  try:\n"
              "    n=d.metadata['Name']\n"
              "  except Exception:\n"
              "    n=None\n"
              "  if n: out[n]=d.version\n"
              "print(json.dumps(out))")


def canon(name):
    """包名规范化（PEP 503）：大小写、_ . - 都算一样"""
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


def _key(py):
    return hashlib.sha1(os.path.normcase(os.path.abspath(py)).encode("utf-8")).hexdigest()[:16]


def _path(py):
    return os.path.join(HISTORY_DIR, _key(py) + ".json")


def _lock(py):
    k = _key(py)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(k, threading.Lock())


def list_packages(py, env=None, timeout=60):
    """{规范包名: 版本}；跑不起来返回 None"""
    if not py or not os.path.isfile(py):
        return None
    try:
        r = subprocess.run([py, "-s", "-c", _LIST_CODE], capture_output=True, timeout=timeout,
                           env=env, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads((r.stdout or b"").decode("utf-8", errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if not isinstance(data, dict):
        return None
    out = {}
    for n, v in data.items():
        c = canon(n)
        if c and c not in out:       # 同名多份（残留的 dist-info）只认第一个，和 import 时一致
            out[c] = str(v)
    return out


def diff(old, new):
    added = {n: v for n, v in new.items() if n not in old}
    removed = {n: v for n, v in old.items() if n not in new}
    changed = {n: [old[n], v] for n, v in new.items() if n in old and old[n] != v}
    return {"added": added, "removed": removed, "changed": changed}


def _empty(d):
    return not (d["added"] or d["removed"] or d["changed"])


def load(py):
    try:
        with open(_path(py), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(py, data):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    p = _path(py)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def record(py, reason, env=None, packages=None):
    """拍一次，跟上一次比。返回这次记下的那条（没变化 / 失败返回 None）"""
    try:
        py = os.path.abspath(str(py or "").strip().strip('"'))
        if not py or not os.path.isfile(py):
            return None
        with _lock(py):
            pk = packages if packages is not None else list_packages(py, env)
            if pk is None:
                return None
            data = load(py)
            old = data.get("latest")
            entries = [e for e in data.get("entries") or [] if isinstance(e, dict)]
            if not isinstance(old, dict):
                entry = {"time": _now(), "reason": "第一次记录（之后每次变化都跟它比）", "baseline": True,
                         "count": len(pk)}
            else:
                d = diff(old, pk)
                if _empty(d):
                    return None
                entry = {"time": _now(), "reason": str(reason or "")[:300], **d}
            entries.append(entry)
            data.update(python=py, latest=pk, latest_time=entry["time"], entries=entries[-MAX_ENTRIES:])
            _save(py, data)
            return entry
    except Exception as e:      # 记不下来不能影响正事
        print(f"[pip 记录] 失败: {e}")
        return None


def record_async(py, reason, env=None):
    if not py:
        return None
    t = threading.Thread(target=record, args=(py, reason, env), daemon=True, name="pip-history")
    t.start()
    return t


def history(py, limit=20):
    """最近的记录，新的在前"""
    if not py:
        return []
    entries = [e for e in load(py).get("entries") or [] if isinstance(e, dict)]
    return entries[::-1][:max(1, int(limit or 20))]


# ---------------------------------------------------------------- pip 命令 ----

_OPT_WITH_VALUE = {"-i", "--index-url", "--extra-index-url", "-c", "--constraint", "-f", "--find-links",
                   "--trusted-host", "--cache-dir", "--target", "-t", "--prefix", "--root", "--platform",
                   "--python-version", "--implementation", "--abi", "--progress-bar", "--timeout", "--retries",
                   "--proxy", "--log", "--src", "--upgrade-strategy", "-e", "--editable", "--config-settings",
                   "-C", "--global-option", "--report"}


def pip_verb(program, args):
    """这条命令是不是会改环境的 pip：返回 install / uninstall，不是返回空"""
    a = [str(x) for x in (args or [])]
    prog = os.path.basename(str(program or "")).lower()
    if prog.startswith("pip"):
        rest = a
    elif "pip" in a and a.index("pip") > 0 and a[a.index("pip") - 1] == "-m":
        rest = a[a.index("pip") + 1:]
    else:
        return ""
    for x in rest:
        if x in ("install", "uninstall"):
            if x == "install" and ("--dry-run" in rest or "--download" in rest or "-d" in rest):
                return ""
            return x
        if not x.startswith("-"):
            return ""
    return ""


def describe(args, cwd=""):
    """pip 命令 → 一句人话：装了 / 卸了什么（不带镜像地址这些）"""
    a = [str(x) for x in (args or [])]
    try:
        rest = a[a.index("pip") + 1:] if "pip" in a else a
    except ValueError:
        rest = a
    verb = next((x for x in rest if x in ("install", "uninstall")), "install")
    pkgs, reqs, skip = [], [], False
    after = rest[rest.index(verb) + 1:] if verb in rest else rest
    for i, x in enumerate(after):
        if skip:
            skip = False
            continue
        if x in ("-r", "--requirement"):
            if i + 1 < len(after):
                reqs.append(after[i + 1])
            skip = True
            continue
        if x in _OPT_WITH_VALUE:
            skip = True
            continue
        if x.startswith("-"):
            continue
        if x.lower().endswith(".whl"):
            x = os.path.basename(x)
        pkgs.append(x)
    parts = []
    where = os.path.basename(os.path.normpath(cwd)) if cwd else ""
    for r in reqs:
        base = os.path.basename(r)
        owner = os.path.basename(os.path.dirname(os.path.abspath(os.path.join(cwd or "", r))))
        parts.append(f"{owner or where}/{base} 里的依赖")
    if pkgs:
        shown = pkgs[:8]
        parts.append(" ".join(shown) + (f" 等 {len(pkgs)} 个" if len(pkgs) > 8 else ""))
    what = "；".join(parts) or "（命令里没写包名）"
    return ("卸载 " if verb == "uninstall" else "安装 ") + what


class around_pip:
    """with around_pip(py, args, cwd, "插件管理"): 跑 pip
    进去前拍一次（收两次记录之间别处的改动），出来后拍一次（这次命令的改动，记在这条命令名下）"""

    def __init__(self, program, args, cwd="", source="", env=None):
        self.verb = pip_verb(program, args)
        prog = str(program or "")
        self.py = prog if self.verb and not os.path.basename(prog).lower().startswith("pip") else ""
        self.args, self.cwd, self.source, self.env = args, cwd, source, env

    def __enter__(self):
        if self.py:
            record(self.py, "两次记录之间的变化（不是启动器装的：手动 pip、ComfyUI-Manager、WebUI 自己装的等）",
                   self.env)
        return self

    def __exit__(self, *exc):
        if self.py:
            src = f"{self.source}：" if self.source else ""
            record(self.py, src + describe(self.args, self.cwd), self.env)
        return False


# ---------------------------------------------------------------- 实例 ----

def instance_python(api, iid, strict=False):
    """实例用的 Python。strict：只要「装包的那个环境」——Forge 还没建 venv 时，
    配置里的 python 只是拿来建 venv 的基础解释器，不算"""
    import config_manager as cm
    cfg = cm.instance_cfg(api.cfg, iid) or {}
    root = (cfg.get("webui_root") or "").strip()
    if not root:
        return ""
    if cm.is_comfy(cfg):
        return (cm.comfy_python(cfg, root) or "").strip('"')
    for c in (os.path.join(root, "venv", "Scripts", "python.exe"), os.path.join(root, "venv", "bin", "python")):
        if os.path.isfile(c):
            return c
    bundled = (cm.detect_bundled_python(root) or "").strip('"')
    if strict:
        return bundled          # 秋叶这类整合包：自带的 python 就是环境本身
    return ((cfg.get("custom_python_path") or "").strip() or bundled).strip('"')


def record_instance_async(api, iid, reason):
    try:
        py = instance_python(api, iid, strict=True)
    except Exception:
        py = ""
    if py and os.path.isfile(py):
        return record_async(py, reason)
    return None


# ---------------------------------------------------------------- 给 AI 看的文字 ----

def _fmt_pk(d, limit):
    items = [f"{n} {v}" for n, v in sorted(d.items())]
    return "、".join(items[:limit]) + (f" 等 {len(items)} 个" if len(items) > limit else "")


def entry_text(e, limit=12):
    if e.get("baseline"):
        return f"[{e.get('time')}] {e.get('reason')}（当时共 {e.get('count')} 个包）"
    parts = []
    if e.get("added"):
        parts.append("新装：" + _fmt_pk(e["added"], limit))
    if e.get("changed"):
        ch = [f"{n} {o}→{v}" for n, (o, v) in sorted(e["changed"].items())]
        parts.append("版本变了：" + "、".join(ch[:limit]) + (f" 等 {len(ch)} 个" if len(ch) > limit else ""))
    if e.get("removed"):
        parts.append("卸掉了：" + _fmt_pk(e["removed"], limit))
    return f"[{e.get('time')}] {e.get('reason')}：" + "；".join(parts)


def summary_text(py, limit=8):
    """最近几次变化（新的在前），给 AI 的环境信息用；没记录返回空串"""
    rows = history(py, limit)
    if not rows:
        return ""
    return "\n".join(entry_text(e, 8) for e in rows)
