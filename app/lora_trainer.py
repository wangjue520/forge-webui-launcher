# -*- coding: utf-8 -*-
"""
WWY LoRA 训练器（https://github.com/wangjue520/wwy-lora-trainer）的一键部署与一键启动。

训练器是独立的程序（自己的网页界面 + 训练环境），目录结构和启动器部署出来的实例一样：
  <训练器目录>/python/python.exe   便携 Python（训练器自己的 bat 也认这个位置）
  <训练器目录>/git/cmd/git.exe     便携 Git（训练器装训练引擎时优先用它）
  <训练器目录>/env/                训练环境（PyTorch + sd-scripts + musubi-tuner），由训练器的
                                   scripts/setup_env.py 安装，和在训练器界面里点「安装训练环境」是同一套代码
部署：便携 Python / Git（复用启动器部署的下载与校验）→ git 拉源码（GitHub 加速候选地址轮换）
      → 按启动器的网络设置写训练器配置 → 跑 setup_env.py 装训练环境。
启动：用便携 Python 跑 app/main.py --no-browser，日志进「一键启动」页的运行日志，就绪后打开浏览器。
"""
import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.request

import config_manager as cm
from process_manager import find_listening_pid, kill_process_tree

LAUNCHER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 启动器根目录（代码在 app/ 里）
SHARED_PIP_CACHE = os.path.join(LAUNCHER_DIR, "launcher_data", "cache", "pip")    # 和 WebUI / ComfyUI 部署共用

REPO = "https://github.com/wangjue520/wwy-lora-trainer"
REF = "main"
DEFAULT_PORT = 28100
DIR_NAME = "WWY-LoRA-Trainer"
SCOPE = "trainer"          # 运行时的事件（一键启动页的训练器卡片和运行日志）
DEPLOY_SCOPE = "deploy"    # 部署进度和日志直接进「环境部署」页，和部署 WebUI / ComfyUI 一样
TORCH_VARIANTS = ("cu126", "cu128", "cu130")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# 部署失败 / 中断后目录里可能留下的东西：这些之外还有别的文件就不往里装（免得 git checkout 覆盖用户文件）
_LEFTOVERS = {"python", "git", ".git", ".launcher_tmp"}
_READY_RE = re.compile(r"(?:本机访问|LoRA 训练器已启动|训练器已经在运行)：\s*(https?://\S+)")


# ============================================================
# 目录检测
# ============================================================
def python_of(root):
    for p in (os.path.join(root, "python", "python.exe"), os.path.join(root, "python", "bin", "python")):
        if os.path.isfile(p):
            return p
    return ""


def env_python_of(root):
    for p in (os.path.join(root, "env", "Scripts", "python.exe"), os.path.join(root, "env", "bin", "python")):
        if os.path.isfile(p):
            return p
    return ""


def is_trainer(root):
    return bool(root) and os.path.isfile(os.path.join(root, "app", "main.py")) \
        and os.path.isfile(os.path.join(root, "app", "envsetup.py")) and os.path.isdir(os.path.join(root, "web"))


def detect(root):
    """训练器目录的状态：源码 / 便携 Python / 训练环境（PyTorch + 两个训练引擎）/ 版本。"""
    root = (root or "").strip().strip('"')
    d = {"root": root, "exists": bool(root) and os.path.isdir(root), "installed": is_trainer(root),
         "python": python_of(root) if root else "", "env": bool(root) and bool(env_python_of(root)),
         "engines": False, "torch": "", "cuda": "", "version": "", "deployable": True, "reason": ""}
    if not root:
        d.update(deployable=False, reason="还没有选择训练器目录")
        return d
    if d["installed"]:
        try:
            with open(os.path.join(root, "version.json"), encoding="utf-8") as f:
                d["version"] = json.load(f).get("version") or ""
        except (OSError, ValueError):
            pass
        d["engines"] = all(os.path.isdir(os.path.join(root, "engines", n)) for n in ("sd-scripts", "musubi-tuner"))
        try:
            with open(os.path.join(root, "env", "lora_trainer_env.json"), encoding="utf-8") as f:
                info = json.load(f)
            d["torch"], d["cuda"] = info.get("torch") or "", info.get("cuda") or ""
        except (OSError, ValueError):
            pass
    elif d["exists"]:
        try:
            extra = [x for x in os.listdir(root) if x.lower() not in _LEFTOVERS]
        except OSError as e:
            extra = [str(e)]
        if extra:
            d.update(deployable=False, reason="这个文件夹里已经有别的文件，请选一个空文件夹，或已经装好的训练器文件夹")
    d["ready"] = d["installed"] and d["env"] and d["engines"] and bool(d["python"])
    return d


def settings_port(root):
    """训练器自己设置里的端口（data/settings.json 的 port，没设过是 28100）"""
    try:
        with open(os.path.join(root, "data", "settings.json"), encoding="utf-8") as f:
            return int(json.load(f).get("port") or DEFAULT_PORT)
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_PORT


def candidate_roots(launcher_dir, cfg):
    """自动找训练器的位置：启动器文件夹旁边、上一级文件夹旁边、各实例文件夹旁边。"""
    parents = [os.path.dirname(launcher_dir), os.path.dirname(os.path.dirname(launcher_dir))]
    for inst in cfg.get("instances") or []:
        r = (inst.get("webui_root") or "").strip()
        if r:
            parents.append(os.path.dirname(os.path.abspath(r)))
    out, seen = [], set()
    for p in parents:
        if not p:
            continue
        for name in (DIR_NAME, "wwy-lora-trainer", "LoRA-Trainer", "lora-trainer"):
            c = os.path.join(p, name)
            k = os.path.normcase(os.path.abspath(c))
            if k not in seen:
                seen.add(k)
                out.append(c)
    return out


def default_root(launcher_dir, cfg):
    for c in candidate_roots(launcher_dir, cfg):
        if is_trainer(c):
            return c
    return os.path.join(os.path.dirname(launcher_dir), DIR_NAME)


def recommend_torch(gpu_names):
    """GTX 9xx / 10xx、TITAN X/V 等老卡新版 CUDA 已经不支持，只能用 cu126；其余用训练器默认的 cu128。"""
    legacy = re.compile(r"GTX?\s*(9\d\d|10\d\d)\b|TITAN\s+X|TITAN\s+V\b|Tesla\s+[PM]\d+", re.IGNORECASE)
    return "cu126" if any(legacy.search(n or "") for n in gpu_names or []) else "cu128"


def clean_env(extra=None):
    """跑训练器 / 训练器脚本用的环境：剥掉会把 Python / pip / git 带偏的变量（和启动 WebUI 时同一份清单）。"""
    env = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku in cm._ENV_STRIP_EXACT or any(ku.startswith(p) for p in cm._ENV_STRIP_PREFIX):
            continue
        env[k] = v
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    env.update(extra or {})
    return env


def write_settings(root, launcher_cfg, log):
    """按启动器的网络设置预先配好训练器（data/settings.json）。训练器里已经设过的不覆盖。"""
    data_dir = os.path.join(root, "data")
    path = os.path.join(data_dir, "settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    changed = []
    mode = launcher_cfg.get("mirror_mode") or "auto"
    if "network" not in s and mode in ("always", "never"):
        s["network"] = "cn" if mode == "always" else "global"
        changed.append("网络：" + ("国内（镜像加速）" if mode == "always" else "国外（直连）"))
    proxy = (launcher_cfg.get("proxy_url") or "").strip()
    if launcher_cfg.get("proxy_enabled") and proxy and not s.get("proxy_url"):
        s["proxy_url"] = proxy if "://" in proxy else "http://" + proxy
        changed.append(f"代理：{s['proxy_url']}")
    if not changed:
        return
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    log("[训练器] 已按启动器的设置配置训练器：" + "；".join(changed) + "\n")


# ============================================================
# 部署
# ============================================================
class _Cancelled(Exception):
    pass


def deploy_flow(api, root, torch_variant):
    """在部署线程里跑。api = LauncherApi：复用它的便携环境下载、带取消 / 超时的命令执行和事件推送。"""
    log = lambda t: api._emit(DEPLOY_SCOPE, "log", text=t)
    progress = lambda d, t: api._emit(DEPLOY_SCOPE, "progress", downloaded=d, total=t)
    step = lambda pct, text: api._emit(DEPLOY_SCOPE, "step", pct=pct, text=text)
    ok = False
    try:
        os.makedirs(root, exist_ok=True)
        env = cm.build_subprocess_env(api._deploy_cfg, root)
        tmp_dir = os.path.join(root, ".launcher_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        env.update(TEMP=tmp_dir, TMP=tmp_dir, PIP_CACHE_DIR=SHARED_PIP_CACHE)   # 部署第二个程序时 PyTorch 等大包不用重下

        # ---- 1. 便携 Python / Git ----
        step(2, "准备便携 Python / Git")
        need_python = not python_of(root)
        bundled_git = os.path.join(root, "git", "cmd", "git.exe")
        need_git = os.name == "nt" and not os.path.isfile(bundled_git)
        if need_python or need_git:
            log(f"[训练器] 准备便携环境（Python 3.13：{'需要下载' if need_python else '已有'}，"
                f"Git：{'需要下载' if need_git else '已有'}）\n")
            api._deploy_portable_env(root, "lora_trainer", need_python, need_git, log, progress)
            progress(0, 0)
        py = python_of(root)
        if not py:
            raise RuntimeError("便携 Python 没有准备好，请重试")
        git = bundled_git if os.path.isfile(bundled_git) else "git"
        if api._deploy_cancel.is_set():
            raise _Cancelled()

        # ---- 2. 源码 ----
        step(10, "下载训练器源码")
        if is_trainer(root):
            log("[训练器] 训练器源码已经在了，跳过下载（要更新请在训练器界面里点「一键更新」）\n")
        else:
            _fetch_source(api, root, git, env, log)
        if api._deploy_cancel.is_set():
            raise _Cancelled()

        # ---- 3. 配置 ----
        write_settings(root, api._deploy_cfg, log)

        # ---- 4. 训练环境 ----
        step(15, "安装训练环境（PyTorch + 训练引擎，约 4 GB）")
        log("\n[训练器] 安装训练环境：PyTorch（约 2.7 GB）+ sd-scripts / musubi-tuner 训练引擎 + 反推依赖。\n"
            "         下载量大，10 MB/s 网速大约 10 分钟；已经装好的部分会自动跳过。\n")
        args = ["-I", os.path.join(root, "scripts", "setup_env.py")]
        if torch_variant in TORCH_VARIANTS:
            args += ["--torch", torch_variant]
        rc = _run_setup(api, py, args, root, clean_env({k: env[k] for k in ("TEMP", "TMP", "PIP_CACHE_DIR")}), log, step)
        if api._deploy_cancel.is_set():
            raise _Cancelled()
        if rc != 0:
            raise RuntimeError(f"训练环境没有装好（退出码 {rc}），详见上面的日志。网络问题的话重新点「一键部署」会接着装")
        ok = True
        step(100, "部署完成")
        log("\n[训练器] 部署完成，到「一键启动」页点训练器卡片上的「启动」就能用了\n")
    except Exception as e:
        if isinstance(e, _Cancelled) or api._deploy_cancel.is_set():   # 取消时正在跑的命令被结束，报错是取消造成的
            log("\n[训练器] 已取消部署。再点「一键部署」会接着装，已经下好的部分不用重下\n")
            return
        log(f"\n[训练器] 部署失败：{e}\n")
        if not isinstance(e, RuntimeError):
            log(traceback.format_exc()[-1500:] + "\n")
        api._emit(DEPLOY_SCOPE, "error", title="训练器部署失败", text=str(e))
    finally:
        api._deploy_running = False
        api._trainer_deploying = False
        api._emit(DEPLOY_SCOPE, "state", running=False)
        api._emit(DEPLOY_SCOPE, "trainer_done", ok=ok, target=root)
        api._emit(SCOPE, "changed")   # 一键启动页的训练器卡片跟着刷新


def _fetch_source(api, root, git, env, log):
    """git init → fetch（GitHub 加速候选地址逐个试）→ checkout。中断后重跑可以接着来。"""
    candidates = [REPO]
    try:
        import mirror_manager as mm
        if mm.resolve_github_mode(api._deploy_cfg, log):
            mirrored = [mm.github_url(REPO, True, i) for i in range(len(mm.GITHUB_PROXIES))]
            candidates = [u for u in mirrored if u != REPO] + [REPO]
            log(f"[网络] 使用 GitHub 加速（{len(candidates)} 个候选地址，失败自动切换）\n")
    except Exception:
        pass
    parent = os.path.dirname(os.path.abspath(root)) or "."
    for args in (["init", root], ["-C", root, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"]):
        if api._deploy_run_cmd(git, args, parent, log, env=env) != 0:
            raise RuntimeError("git 初始化失败，详见日志")
    for u in candidates:
        if api._deploy_cancel.is_set():
            raise _Cancelled()
        if api._deploy_run_cmd(git, ["-C", root, "config", "remote.origin.url", u], parent, log, env=env) != 0:
            raise RuntimeError("设置 git 远程地址失败，详见日志")
        rc = api._deploy_run_cmd(git, ["-C", root, "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=120",
                                       "fetch", "--depth", "1", "origin", REF], parent, log, env=env, timeout=900)
        if rc == 0:
            break
        if api._deploy_cancel.is_set():
            raise _Cancelled()
        log(f"\n[训练器] 从这个地址拉取失败（退出码 {rc}），换下一个地址 ...\n")
    else:
        raise RuntimeError("从 GitHub 和所有加速代理拉取训练器源码都失败了，请检查网络 / 代理后重新点「一键部署」")
    if api._deploy_run_cmd(git, ["-C", root, "checkout", "-f", "-B", REF, f"origin/{REF}"], parent, log, env=env) != 0:
        raise RuntimeError("检出训练器源码失败，详见日志")
    # 远程地址改回 GitHub 原地址：训练器自己的「一键更新」走 git pull，加速代理随时可能失效
    api._deploy_run_cmd(git, ["-C", root, "config", "remote.origin.url", REPO + ".git"], parent, log, env=env)
    if not is_trainer(root):
        raise RuntimeError("源码下载完了，但没找到训练器的程序文件，请重试")


def _run_setup(api, py, args, cwd, env, log, step):
    """跑训练器的 setup_env.py：日志里的「[ 35%] …」换算成部署进度（训练环境这一步占 15% → 99%）。"""
    pct_re = re.compile(r"^\s*\[\s*(\d+)%\]\s*(.*)$", re.M)

    def on_log(text):
        log(text)
        for m in pct_re.finditer(text or ""):
            step(15 + int(m.group(1)) * 84 // 100, m.group(2).strip() or "安装训练环境")
    return api._deploy_run_cmd(py, args, cwd, on_log, env=env)


# ============================================================
# 启动 / 停止
# ============================================================
def probe(url, timeout=1.5):
    """这个地址上是不是训练器（首页里有「LoRA」）。"""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/index.html", timeout=timeout) as r:
            return b"LoRA" in r.read(4000)
    except Exception:
        return False


class TrainerRunner:
    """启动器里只有一个训练器进程。训练器自己点「重启训练器」时会另起一个进程、旧的退出，
    这时进程不归启动器管了，但端口上还是训练器：状态按端口算，停止时按端口找进程。"""

    def __init__(self, api):
        self.api = api
        self.proc = None
        self.url = ""
        self.port = DEFAULT_PORT
        self.state = "stopped"   # stopped / starting / ready
        self.stopped_at = 0.0
        self.lock = threading.Lock()

    def _emit_state(self):
        self.api._emit(SCOPE, "state", **self.status())

    def status(self):
        alive = bool(self.proc and self.proc.poll() is None)
        url = self.url or f"http://127.0.0.1:{self.port}/"
        if alive:
            state = self.state
        elif time.time() - self.stopped_at > 5 and probe(url, 0.8):   # 刚停止时端口还没关，别误判成在运行
            state = "ready"   # 训练器自己重启过，或者是在启动器外面打开的
        else:
            state = "stopped"
        return {"state": state, "url": url if state == "ready" else "", "managed": alive, "port": self.port}

    def running(self):
        return self.status()["state"] != "stopped"

    def managed(self):
        return bool(self.proc and self.proc.poll() is None)

    def start(self, root, open_browser, port=None):
        """port 不传：用训练器自己设置里的端口（被占用时训练器会自动换一个，地址从它的输出里认）"""
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return {"ok": False, "error": "训练器已经在运行"}
            d = detect(root)
            if not d["installed"] or not d["python"]:
                return {"ok": False, "need_deploy": True, "error": "这个文件夹里还没有部署训练器，先点「一键部署」"}
            self.port = int(port or settings_port(root))
            url = f"http://127.0.0.1:{self.port}/"
            if probe(url):
                self.url, self.state = url, "ready"
                if open_browser:
                    self.api.open_url(url)
                return {"ok": True, "already": True, "url": url}
            cmd = [d["python"], "-I", os.path.join(root, "app", "main.py"), "--no-browser"] + (["--port", str(port)] if port else [])
            env = clean_env({"WWY_LAUNCHER_DIR": LAUNCHER_DIR})
            self.api._emit(SCOPE, "log", text=f"[训练器] 启动：{' '.join(cmd)}\n")
            try:
                kw = {"creationflags": _NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                self.proc = subprocess.Popen(cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, **kw)
            except OSError as e:
                return {"ok": False, "error": f"启动失败：{e}"}
            self.url, self.state = "", "starting"
        self._emit_state()
        threading.Thread(target=self._reader, args=(self.proc, open_browser), daemon=True, name="trainer-log").start()
        threading.Thread(target=self._watch_port, args=(self.proc, open_browser), daemon=True, name="trainer-port").start()
        return {"ok": True}

    def _ready(self, proc, url, open_browser):
        with self.lock:
            if proc is not self.proc or self.state == "ready":
                return
            self.url, self.state = url, "ready"
            m = re.search(r":(\d+)/?$", url)
            if m:
                self.port = int(m.group(1))
        self._emit_state()
        if open_browser:
            self.api.open_url(url)

    def _reader(self, proc, open_browser):
        buf = b""
        try:
            while True:
                chunk = proc.stdout.read1(4096)
                if not chunk:
                    break
                buf += chunk
                if b"\n" not in buf:
                    continue
                text, buf = buf.rsplit(b"\n", 1)
                text = cm.decode_process_output(text)
                self.api._emit(SCOPE, "log", text=text)
                m = _READY_RE.search(text)
                if m and "<" not in m.group(1):
                    self._ready(proc, m.group(1).rstrip("/") + "/", open_browser)
        except Exception:
            pass
        if buf.strip():
            self.api._emit(SCOPE, "log", text=cm.decode_process_output(buf))
        rc = proc.wait()
        with self.lock:
            if proc is self.proc:
                self.state = "stopped"
        self.api._emit(SCOPE, "log", text=f"[训练器] 训练器进程已退出（退出码 {rc}）\n")
        self._emit_state()

    def _watch_port(self, proc, open_browser):
        """日志里没认出地址（比如输出被缓冲）时的兜底：端口能打开就算就绪。"""
        url = f"http://127.0.0.1:{self.port}/"
        for _ in range(240):
            if proc.poll() is not None or self.state == "ready":
                return
            if probe(url):
                self._ready(proc, url, open_browser)
                return
            time.sleep(0.5)

    def stop(self):
        with self.lock:
            proc = self.proc
        if proc and proc.poll() is None:
            kill_process_tree(proc.pid)
        else:   # 不是启动器开的（训练器自己重启过）：按端口找到它
            pid = find_listening_pid(self.port)
            if pid:
                kill_process_tree(pid)
        with self.lock:
            self.state = "stopped"
            self.url = ""
            self.stopped_at = time.time()
        self._emit_state()
        return {"ok": True}
