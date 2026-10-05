# -*- coding: utf-8 -*-
"""
进程管理（V3）：每个实例一个 InstanceRunner，各自管理进程树、端口、日志和就绪判定。

V2 的单实例启动逻辑原样搬到了这里（踩过的坑、代次保护都保留），只是把
「全局唯一的 _launch_* 状态」变成了「每个实例一份」。在此基础上加了：
  - ComfyUI 实例：python main.py 直接启动，按 "To see the GUI go to:" 判就绪
  - 多实例同时运行时自动分配不冲突的端口（WebUI 从 7860、ComfyUI 从 8188 起）
  - 所有事件都带 iid，前端按实例分发

同时放着几个进程工具函数（杀进程树 / 查端口占用 / 试运行可执行文件 /
webui-user.bat 写死变量检测），webview_api 从这里导入。
"""
import os
import re
import subprocess
import threading
import time
import traceback
import webbrowser

import config_manager as cm

WEBUI_ENTRY_SCRIPT = "webui.bat"

# subprocess.CREATE_NO_WINDOW，避免拉起 cmd/git/netstat 时闪黑框
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_URL_RE = re.compile(r"Running on local URL:\s*(http://\S+)")
_COMFY_URL_RE = re.compile(r"To see the GUI go to:\s*(https?://\S+)")
_BIND_ERROR_RE = re.compile(r"error while attempting to bind on address", re.IGNORECASE)


# 启动阶段的致命错误 → 给用户的说明（按出现顺序匹配，命中第一个）
_FATAL_HINTS = (
    (("please update your gpu driver", "driver on your system is too old", "cuda driver version is insufficient"),
     "[启动器] 启动失败：显卡驱动太旧，带不动环境里装的 torch（新版 Forge Neo / ComfyUI 默认装 CUDA 13 的 torch，"
     "需要 580 以上的驱动）。\n"
     "  · 启动器每次启动前会读取驱动版本并自动换装兼容的 torch；还看到这条说明没读到驱动信息"
     "（nvidia-smi 不可用），或者附加参数/系统环境变量里自己设了 TORCH_COMMAND\n"
     "  · 最省事的办法：把显卡驱动升级到 580 以上，然后再启动"),
    (("couldn't install pytorch", "couldn't install torch"),
     "[启动器] 启动失败：torch 下载/安装没成功。往上翻能看到 pip 的具体报错，常见原因：\n"
     "  · 网络：国内直连 download.pytorch.org 很容易失败，到「设置」把网络加速改成「总是使用国内镜像」再启动\n"
     "  · 版本：这个 torch 版本没有当前 Python 版本能用的安装包（日志里会有 No matching distribution）"),
    (("no kernel image is available",),
     "[启动器] 启动失败：环境里的 torch 不支持这张显卡的架构（常见于 GTX 9xx/10xx 等老卡配新版 torch），"
     "需要安装老卡专用的 torch 版本"),
    (("pytorch is not able to access any compute device", "torch is not able to use gpu"),
     "[启动器] 启动失败：torch 找不到可用的显卡。请确认装了 NVIDIA 驱动；没有 N 卡的话需要在附加参数里加 --use-cpu all"),
)


def _driver_issue_forge(root, cfg):
    """WebUI：已装的 torch，或者还没装时 WebUI 要装的 torch，驱动带不动 → 启动前提示升级驱动"""
    try:
        import cuda_compat as cc
        import torch_bootstrap as tb
        py = (cfg.get("custom_python_path") or "").strip() or cm.detect_bundled_python(root) or ""
        _ver, tag = cc.installed_torch_tag(cc.forge_site_packages(root, py))
        if not tag:
            _specs, tag = tb._parse_torch_spec(root)
        if not tag:
            return None
        auto = "TORCH_COMMAND" not in os.environ
        return cc.driver_issue(tag, "这个 WebUI ", auto)
    except Exception:
        return None


def _driver_issue_comfy(root, cfg, comfy_dir):
    """ComfyUI：看它的 Python 环境里装的 torch"""
    try:
        import cuda_compat as cc
        py = cm.comfy_python(cfg, root).strip('"')
        cands = [os.path.join(os.path.dirname(py), "Lib", "site-packages")] if py else []
        for v in ("venv", ".venv"):
            cands.append(os.path.join(comfy_dir, v, "Lib", "site-packages"))
        _ver, tag = cc.installed_torch_tag([c for c in cands if os.path.isdir(c)])
        return cc.driver_issue(tag, "ComfyUI ", False) if tag else None
    except Exception:
        return None


def _fatal_hint_for(text):
    low = (text or "").lower()
    for keys, hint in _FATAL_HINTS:
        if any(k in low for k in keys):
            return hint
    return ""

# 第三方整合包（秋叶系等）会在 webui-user.bat 里写死 set PYTHON=/GIT=...
_OVERRIDE_VAR_PATTERN = re.compile(
    r"^\s*set\s+(PYTHON|GIT|VENV_DIR|COMMANDLINE_ARGS)\s*=\s*(.*)$",
    re.IGNORECASE,
)

FORGE_PORT_BASE = 7860
COMFY_PORT_BASE = 8188


# ============================================================
# 进程工具
# ============================================================

def kill_process_tree(pid):
    """杀掉整棵进程树（cmd.exe 只是壳，真正占端口的 python.exe 是孙进程）。
    返回是否杀成功；taskkill 本身也加超时，避免杀进程的动作自己卡死。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, creationflags=_NO_WINDOW, timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
            return True
        except Exception:
            return False


def _probe_executable(cmd, timeout=15):
    """
    试运行一个可执行文件，返回 (能否运行, 失败原因)。
    “文件存在”不等于“能运行”——解压不完整、缺 DLL、被杀软拦截
    都会让 exe 一跑就挂，等到启动中途才暴雷就很难看懂。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except FileNotFoundError:
        return False, "文件不存在"
    except subprocess.TimeoutExpired:
        return False, "执行超时"
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        tail = [l for l in ((r.stderr or "") + "\n" + (r.stdout or "")).splitlines() if l.strip()]
        detail = f"返回码 {r.returncode}"
        if tail:
            detail += f"，输出: {tail[-1].strip()[:200]}"
        return False, detail
    return True, ""


def find_listening_pid(port):
    """返回正在 LISTEN 指定端口的进程 PID，找不到返回 None（仅 Windows）"""
    if os.name != "nt" or not port:
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-2].upper() == "LISTENING":
            if parts[1].endswith(f":{port}"):
                try:
                    return int(parts[-1])
                except ValueError:
                    continue
    return None


def port_in_use(port):
    """端口是否已被监听（跨平台，用 bind 试探）"""
    import socket
    if find_listening_pid(str(port)):
        return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", int(port)))
            return False
        except OSError:
            return True


def process_name_of(pid):
    """查询 PID 对应的进程名（仅 Windows），查不到返回空字符串"""
    if os.name != "nt":
        return ""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        ).stdout.strip()
        if out.startswith('"'):
            return out.split('","')[0].strip('"')
    except Exception:
        pass
    return ""


def _find_webui_user_bat_overrides(root):
    """返回 [(变量名, 值, 行号), ...]，跳过被注释掉的行"""
    bat_path = os.path.join(root, "webui-user.bat")
    if not os.path.exists(bat_path):
        return []
    hits = []
    try:
        with open(bat_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                stripped = line.strip()
                if stripped.upper().startswith(("REM", "::")):
                    continue
                m = _OVERRIDE_VAR_PATTERN.match(line)
                if m and m.group(2).strip():
                    hits.append((m.group(1).upper(), m.group(2).strip(), i))
    except OSError:
        return []
    return hits


def _clear_webui_user_bat_overrides(root):
    """清空 webui-user.bat 里生效的变量赋值（保留行本身），先备份 .bak"""
    bat_path = os.path.join(root, "webui-user.bat")
    backup_path = bat_path + ".bak"
    with open(bat_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if not os.path.exists(backup_path):
        with open(backup_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith(("REM", "::")):
            new_lines.append(line)
            continue
        m = _OVERRIDE_VAR_PATTERN.match(line)
        if m and m.group(2).strip():
            new_lines.append(f"set {m.group(1).upper()}=\n")
        else:
            new_lines.append(line)

    with open(bat_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _non_ascii_or_space_issues(root, what):
    issues = []
    try:
        root.encode("ascii")
    except UnicodeEncodeError:
        issues.append({
            "level": "warn", "id": "non_ascii_path",
            "text": f"{what}根目录：\n{root}\n\n包含中文（或其他非英文字符）。"
                    "启动器本身没问题，但 torch / gradio / 部分扩展在中文路径下"
                    "有各自的历史 bug，出问题时很难排查。\n\n"
                    "强烈建议把整个文件夹移到纯英文路径（例如 F:\\ai）再启动。",
        })
    if " " in root:
        issues.append({
            "level": "warn", "id": "space_in_path",
            "text": f"{what}根目录：\n{root}\n\n包含空格。虽然多数情况下能跑，"
                    "但部分扩展/依赖对带空格的路径处理得不好。\n"
                    "建议换成不含空格的路径（例如 F:\\ai）。",
        })
    return issues


# ============================================================
# 每个实例一个 Runner
# ============================================================

class InstanceRunner:
    """
    一个实例的启动/停止/就绪判定。api 需要提供：
      api._emit(scope, type, **data)      推事件
      api._spawn(fn, name)                起后台线程
      api.instance_cfg(iid)               该实例的有效配置（全局键 + 实例键）
      api._runner_ports_in_use(except_iid) 其他实例正在用的端口集合
      api._instance_launch_extras(iid, cfg) 启动前给 cfg 追加模型库参数等（可选）
    """

    def __init__(self, api, iid):
        self.api = api
        self.iid = iid
        self.proc = None
        self.url = None
        self.status_text = "尚未启动"
        self.state = "idle"          # idle / starting / ready / stopped
        self.output_tail = ""
        self.fatal_hint = ""         # 本轮输出里认出的致命错误（给出可操作的说明）
        # 就绪判定的并发保护（V2 踩过的两个坑，保留原注释要点）：
        #  1) 日志解析线程和端口探测线程会同时判定「就绪」，各开一次浏览器
        #  2) 点「停止」后正在被杀的进程还没释放端口，被端口探测当成「刚就绪」
        # 解法：代次（generation）标记每一轮启动，启动/停止各 +1，监视线程
        # 代次对不上就立刻收工；真正的「开浏览器」收敛到 _mark_ready 一个入口。
        self.lock = threading.Lock()
        self.gen = 0
        self.opened = False
        self.watch_ports = []
        self.pre_pids = set()
        self.port = None             # 本轮实际使用/分配的端口（能确定时）

    # ---------------------------------------------------------- 基础 ----
    @property
    def cfg(self):
        return self.api.instance_cfg(self.iid) or {}

    def is_comfy(self):
        return cm.is_comfy(self.cfg)

    def label(self):
        return "ComfyUI" if self.is_comfy() else "WebUI"

    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def emit(self, type_, **data):
        self.api._emit("launch", type_, iid=self.iid, **data)

    def log(self, text):
        self.emit("log", text=text)

    def status(self):
        return {"iid": self.iid, "running": self.running(), "state": self.state,
                "url": self.url, "text": self.status_text, "port": self.port}

    def set_status(self, state, text, url=None):
        self.state = state
        self.status_text = text
        if url is not None:
            self.url = url
        self.emit("status", state=state, text=text, url=self.url)
        self.api._emit("instances", "status", **self.status())

    def gen_alive(self, gen):
        return gen == self.gen

    def _auto_open(self, cfg):
        # 用户若已开了 --autolaunch（程序自己开浏览器），启动器就不再重复打开
        return bool(cfg.get("auto_open_browser_on_ready", True)) and not bool(cfg.get("autolaunch"))

    def _mark_ready(self, gen, url, text, auto_open):
        with self.lock:
            if gen != self.gen or self.opened:
                return False
            self.opened = True
            self.url = url
            pm = re.search(r":(\d+)", url.split("//", 1)[-1])
            if pm:
                self.port = int(pm.group(1))
        self.set_status("ready", text, url=url)
        if auto_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return True

    # ---------------------------------------------------------- 端口 ----
    def _base_port(self):
        return COMFY_PORT_BASE if self.is_comfy() else FORGE_PORT_BASE

    def _pick_port(self, cfg):
        """
        多个实例同时跑时必须各用各的端口，否则端口探测会把别的实例当成自己。
        用户在设置里填了端口就用用户的；没填且别的实例在跑时，从基准端口往上找空闲的。
        单实例场景保持 V2 行为（不强行指定，让程序自己用默认端口）。
        """
        user = str(cfg.get("port", "")).strip()
        if user.isdigit():
            return int(user), False
        taken = self.api._runner_ports_in_use(self.iid)
        if not taken:
            return None, False
        p = self._base_port()
        while p < self._base_port() + 100:
            if p not in taken and not port_in_use(p):
                return p, True
            p += 1
        return None, False

    # ---------------------------------------------------------- 环境探测 ----
    def env_detect(self, root):
        root = (root or "").strip()
        out = {"python": "", "git": ""}
        if not root or not os.path.isdir(root):
            return {"ok": True, **out}
        cfg = self.cfg
        if cm.is_comfy(cfg):
            comfy_dir, py = cm.comfy_layout(root)
            if not comfy_dir:
                out["python"] = "该目录不像是 ComfyUI（没找到 main.py）"
            elif not (cfg.get("custom_python_path") or "").strip():
                out["python"] = f"检测到 ComfyUI 自带 Python: {py}" if py \
                    else "未检测到 ComfyUI 自带 Python / venv，将使用系统 python"
            return {"ok": True, **out}
        if not (cfg.get("custom_python_path") or "").strip():
            found = cm.detect_bundled_python(root)
            out["python"] = f"检测到便携版 Python: {found}" if found else "未检测到便携版 Python，将使用系统 python"
        if not (cfg.get("custom_git_path") or "").strip():
            found = cm.detect_bundled_git(root)
            out["git"] = f"检测到便携版 Git: {found}" if found else "未检测到便携版 Git，将使用系统 git"
        return {"ok": True, **out}

    # ---------------------------------------------------------- 预检 ----
    def precheck(self, root):
        root = (root or "").strip()
        cfg = self.cfg
        if cm.is_comfy(cfg):
            return self._precheck_comfy(root, cfg)
        return self._precheck_forge(root, cfg)

    def _leftover_issue(self, cfg, what):
        cfg_port = str(cfg.get("port", "")).strip()
        base = self._base_port()
        scan_ports = [int(cfg_port)] if cfg_port.isdigit() else list(range(base, base + 10))
        mine = self.api._runner_ports_in_use(None)   # 启动器自己管着的端口不算残留
        leftovers, seen = [], set()
        for p in scan_ports:
            if p in mine:
                continue
            pid = find_listening_pid(str(p))
            if pid and pid not in seen:
                seen.add(pid)
                leftovers.append((p, pid))
        if not leftovers:
            return None
        detail = "\n".join(
            f"  端口 {p}: {process_name_of(pid) or '未知进程'} (PID {pid})" for p, pid in leftovers)
        return {
            "level": "warn", "id": "port_occupied",
            "pid": leftovers[0][1], "pids": [pid for _, pid in leftovers],
            "text": f"检测到 {len(leftovers)} 个残留进程还占着端口：\n\n{detail}\n\n"
                    f"这通常是之前没退干净的 {what} 实例，还占着显存和内存，"
                    "而且会干扰启动器的就绪检测（把旧进程误判成刚启动成功的那个）。\n"
                    "选择「结束并启动」会把它们全部结束；"
                    "选择「直接启动」会自动换用空闲端口。",
        }

    def _precheck_comfy(self, root, cfg):
        issues = []
        comfy_dir, _py = cm.comfy_layout(root)
        if not root or not comfy_dir:
            issues.append({"level": "error",
                           "text": "请先设置正确的 ComfyUI 根目录（应包含 main.py；"
                                   "官方便携包选外层的 ComfyUI_windows_portable 也可以）"})
            return {"ok": False, "issues": issues}
        issues += _non_ascii_or_space_issues(root, "ComfyUI ")
        drv = _driver_issue_comfy(root, cfg, comfy_dir)
        if drv:
            issues.append(drv)
        py = cm.comfy_python(cfg, root)
        if py:
            ok, why = _probe_executable([py.strip('"'), "--version"])
            if not ok:
                issues.append({"level": "error",
                               "text": f"ComfyUI 的 Python 无法运行：\n{py}\n\n原因: {why}\n\n"
                                       "常见情况是整合包解压不完整或被杀毒软件拦截，建议重新解压。"})
        if cfg.get("enable_listen"):
            issues.append({"level": "warn", "id": "listen",
                           "text": "当前开启了局域网访问（--listen）。同一局域网里的任何人都能打开你的 "
                                   "ComfyUI 并执行工作流/安装节点。只是自己用的话建议关掉。"})
        left = self._leftover_issue(cfg, "ComfyUI")
        if left:
            issues.append(left)
        return {"ok": not any(i["level"] == "error" for i in issues), "issues": issues}

    def _precheck_forge(self, root, cfg):
        issues = []
        if not root or not os.path.isdir(root):
            issues.append({"level": "error", "text": "请先设置正确的 WebUI 根目录"})
            return {"ok": False, "issues": issues}

        entry = os.path.join(root, WEBUI_ENTRY_SCRIPT)
        if not os.path.exists(entry):
            issues.append({
                "level": "error",
                "text": f"该目录下没有找到 {WEBUI_ENTRY_SCRIPT}。\n"
                        "如果你还没安装，去「环境部署」页先部署一个；如果目录没错，"
                        "确认一下这是不是 WebUI 的根目录（应该和 webui.py 在同一层）。",
            })
            return {"ok": False, "issues": issues}

        issues += _non_ascii_or_space_issues(root, "WebUI ")
        drv = _driver_issue_forge(root, cfg)
        if drv:
            issues.append(drv)

        # 试运行 python / git：路径存在 ≠ 能执行（解压不完整、缺文件、被杀软拦截）
        py_exe = (cfg.get("custom_python_path") or "").strip() or cm.detect_bundled_python(root)
        if py_exe:
            ok, why = _probe_executable([py_exe.strip('"'), "--version"])
            if not ok:
                issues.append({
                    "level": "error",
                    "text": f"便携版 Python 无法运行：\n{py_exe}\n\n原因: {why}\n\n"
                            "常见情况是整合包解压不完整或文件被杀毒软件损坏，"
                            "建议重新解压整合包（解压前关掉杀毒/ Defender 实时保护，"
                            "或把目录加进排除列表）。",
                })
        git_exe = (cfg.get("custom_git_path") or "").strip() or cm.detect_bundled_git(root)
        if git_exe:
            ok, why = _probe_executable([git_exe.strip('"'), "version"])
            if not ok:
                issues.append({
                    "level": "error",
                    "text": f"便携版 Git 无法运行：\n{git_exe}\n\n原因: {why}\n\n"
                            "Git 损坏会让 Forge 启动到一半报 Bad git executable 然后退出。\n"
                            "常见原因：整合包解压不完整（git 目录缺文件）、文件被杀毒软件拦截。\n"
                            "解决办法：重新解压整合包；或者把根目录下的 git 文件夹改名/删除，"
                            "启动器会改用系统里已安装的 Git（前提是系统装过）。",
                })

        if cfg.get("enable_listen") and cfg.get("enable_insecure_extension_access"):
            issues.append({
                "level": "warn", "id": "listen_insecure_combo",
                "text": "当前同时开启了 --listen（允许局域网访问）和\n"
                        "--enable-insecure-extension-access（允许网页安装扩展）。\n\n"
                        "这个组合意味着：同一局域网里的任何人都能从网页给你的 WebUI "
                        "装扩展——而扩展就是任意 Python 代码，等于把整台电脑交出去。\n\n"
                        "建议到「高级选项」至少关掉其中一个：\n"
                        "  · 只是自己用 → 关掉 --listen（仅本机访问）\n"
                        "  · 确需局域网访问 → 关掉 --enable-insecure-extension-access",
            })

        overrides = _find_webui_user_bat_overrides(root)
        if overrides:
            detail = "\n".join(f"  第 {ln} 行: set {name}={val}" for name, val, ln in overrides)
            issues.append({
                "level": "warn", "id": "user_bat_overrides",
                "text": "检测到 webui-user.bat 里写死了以下变量：\n\n" + detail + "\n\n"
                        "这几行是无条件执行的 set，会把启动器注入的路径/参数强行覆盖回去，"
                        "很可能导致启动失败（尤其是换过电脑、改过安装路径之后）。\n"
                        "选择「清空并启动」会自动清空这几行（原文件备份为 webui-user.bat.bak）。",
            })

        left = self._leftover_issue(cfg, "WebUI")
        if left:
            issues.append(left)
        return {"ok": True, "issues": issues}

    # ---------------------------------------------------------- 启动 ----
    def start(self, root, fix_overrides=False, kill_pid=0):
        if self.running():
            return {"ok": False, "error": f"{self.label()} 已在运行中"}
        # 先把上一轮代次作废：上一轮崩掉后它的读取线程可能正要做收尾
        # （gen 校验 + 清 self.proc），若等新进程起好再 +1，它会穿过这个
        # 窗口把新进程的状态误清掉，新读取线程就会对着 None 调 wait()
        with self.lock:
            self.gen += 1
        root = (root or "").strip()
        cfg = dict(self.cfg)
        comfy = cm.is_comfy(cfg)
        if comfy:
            comfy_dir, _ = cm.comfy_layout(root)
            if not comfy_dir:
                return {"ok": False, "error": "ComfyUI 根目录无效（没找到 main.py）"}
        else:
            if not root or not os.path.isdir(root):
                return {"ok": False, "error": "WebUI 根目录无效"}
            if not os.path.exists(os.path.join(root, WEBUI_ENTRY_SCRIPT)):
                return {"ok": False, "error": f"目录下没有 {WEBUI_ENTRY_SCRIPT}"}

        self.api._instance_set_root(self.iid, root)
        log = self.log

        if kill_pid:
            pids = kill_pid if isinstance(kill_pid, (list, tuple)) else [kill_pid]
            for pid in pids:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    continue
                pname = process_name_of(pid) or "未知进程"
                kill_process_tree(pid)
                log(f"[启动器] 已结束占用端口的残留进程 {pname} (PID {pid})\n")

        # 多实例同时运行：自动分配端口
        port, auto_port = self._pick_port(cfg)
        if auto_port:
            cfg["port"] = str(port)
            log(f"[启动器] 其他实例正在运行，自动分配端口 {port}\n")
        self.port = port

        # 模型库等附加参数（ComfyUI 的 extra_model_paths、WebUI 的 --lora-dir 等）
        try:
            self.api._instance_launch_extras(self.iid, cfg, log)
        except Exception as e:
            log(f"[启动器] 附加模型库参数失败（不影响启动）: {e}\n")

        if comfy:
            res = self._spawn_comfy(root, cfg, log)
        else:
            res = self._spawn_forge(root, cfg, log, fix_overrides)
        if not res.get("ok"):
            return res

        base = self._base_port()
        self.watch_ports = [port] if port else (
            [int(cfg["port"])] if str(cfg.get("port", "")).strip().isdigit()
            else list(range(base, base + 10)))
        # 启动前给这些端口的现有监听者拍快照——残留实例不能被当成本次就绪
        pre = set()
        for p in self.watch_ports:
            pid = find_listening_pid(str(p))
            if pid:
                pre.add(pid)
        self.pre_pids = pre

        with self.lock:
            self.gen += 1
            gen = self.gen
            self.opened = False
            self.url = None

        self.output_tail = ""
        self.fatal_hint = ""
        self.set_status("starting", f"启动中，等待 {self.label()} 输出监听地址...", url=None)
        self.emit("state", running=True)
        auto_open = self._auto_open(cfg)
        self.api._spawn(lambda: self._reader(gen, auto_open, comfy), name=f"launch-reader-{self.iid}")
        self.api._spawn(lambda: self._port_watcher(gen, auto_open), name=f"launch-port-{self.iid}")
        return {"ok": True, "port": port}

    def _popen(self, cmd, cwd, env):
        kw = {}
        if os.name != "nt":
            kw["start_new_session"] = True
        return subprocess.Popen(
            cmd, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, **kw)

    def _spawn_comfy(self, root, cfg, log):
        comfy_dir, _ = cm.comfy_layout(root)
        py = cm.comfy_python(cfg, root)
        if not py:
            py = "python"
            log("[启动器] 没在实例目录里检测到自带的 Python，临时用系统 PATH 里的 python。"
                "如果启动报 No module named ...，多半是解释器不对：在「一键启动」页的"
                "「Python 解释器」里填整合包自带的 python.exe 完整路径即可\n")
        args = cm.build_comfy_args(cfg)
        cmd = [py.strip('"'), "-s", cm.COMFY_ENTRY] + args
        env = cm.build_comfy_env(cfg, root)
        log(f"[启动器] 正在启动 ComfyUI ...\n[启动器] 目录: {comfy_dir}\n")
        log(f"[启动器] 命令: {' '.join(cmd)}\n\n")
        try:
            self.proc = self._popen(cmd, comfy_dir, env)
        except OSError as e:
            return {"ok": False, "error": f"启动失败: {e}"}
        return {"ok": True}

    def _spawn_forge(self, root, cfg, log, fix_overrides):
        import shutil
        if fix_overrides:
            try:
                _clear_webui_user_bat_overrides(root)
                log("[启动器] 已清空 webui-user.bat 里写死的 PYTHON/GIT/VENV_DIR/"
                    "COMMANDLINE_ARGS（原文件备份为 webui-user.bat.bak）\n")
            except OSError as e:
                return {"ok": False, "error": f"无法修改 webui-user.bat：{e}"}

        # 先静默修复 venv\pyvenv.cfg 里过期的 home 路径，再判断 venv 是否残缺
        resolved_python = (cfg.get("custom_python_path") or "").strip() \
            or cm.detect_bundled_python(root) or ""
        if resolved_python:
            try:
                cm.sync_venv_pyvenv_cfg(root, resolved_python, lambda m: log(m + "\n"))
            except OSError as e:
                log(f"[启动器] 检查 venv pyvenv.cfg 时出错（不影响继续启动）: {e}\n")

        venv_dir = os.path.join(root, "venv")
        if cm.venv_is_definitely_broken(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)
            if not os.path.isdir(venv_dir):
                log(f"[启动器] 检测到残缺的 venv（缺 python.exe 或 pip），已删除: {venv_dir}\n")
            else:
                log(f"[启动器] 检测到残缺的 venv 但删除失败（可能被占用），"
                    f"如启动报 No module named pip 请手动删除: {venv_dir}\n")

        args_str = cm.build_commandline_args(cfg)
        notes = []
        env_overrides = cm.build_launch_env_overrides(cfg, root, notes)
        for n in notes:
            log(n + "\n")
        log(f"[启动器] 正在启动 {WEBUI_ENTRY_SCRIPT} ...\n")
        log(f"[启动器] COMMANDLINE_ARGS = {args_str}\n")
        for k, v in env_overrides.items():
            if k != "COMMANDLINE_ARGS":
                log(f"[启动器] {k} = {v}\n")
        log("\n")
        env = cm.build_subprocess_env(cfg, root)
        try:
            self.proc = self._popen(
                ["cmd.exe", "/c", WEBUI_ENTRY_SCRIPT] if os.name == "nt" else ["sh", "webui.sh"],
                root, env)
        except OSError as e:
            return {"ok": False, "error": f"启动失败: {e}"}
        return {"ok": True}

    # ---------------------------------------------------------- 监视线程 ----
    def _port_watcher(self, gen, auto_open):
        # 兜底：有些整合包把输出重定向到自己的 log 文件，管道里一个字节都没有，
        # 靠解析输出判断就绪会永远卡在「启动中」——发现 python 在监听就判定就绪
        ports = self.watch_ports or []
        pre_pids = self.pre_pids or set()
        skipped_logged = set()
        for _ in range(600):  # 每 2 秒一次，最长 20 分钟
            if not self.gen_alive(gen):
                return
            if not self.proc or self.proc.poll() is not None:
                return
            if self.url:
                return
            for p in ports:
                if not self.gen_alive(gen):
                    return
                pid = find_listening_pid(str(p))
                if not pid:
                    continue
                if pid in pre_pids:
                    if p not in skipped_logged:
                        skipped_logged.add(p)
                        self.log(f"[启动器] 端口 {p} 上有残留的旧进程 (PID {pid})，"
                                 "就绪判定将忽略它；建议下次启动前在预检弹窗里选「结束并启动」\n")
                    continue
                pname = (process_name_of(pid) or "").lower()
                if pname not in ("python.exe", "pythonw.exe", "python3.exe"):
                    continue
                url = f"http://127.0.0.1:{p}"
                if self._mark_ready(gen, url, f"已就绪（端口探测），实际访问地址: {url}", auto_open):
                    self.log(f"\n[启动器] 通过端口探测到 {self.label()} 已在监听: {url}\n")
                return
            time.sleep(2)

    def _reader(self, gen, auto_open, comfy):
        proc = self.proc
        if proc is None:
            # 与新一轮启动擦肩时 self.proc 可能已被重置，没有进程可读就收工
            return
        url_re = _COMFY_URL_RE if comfy else _URL_RE
        try:
            while True:
                # read1() 而非 read()：后者会攒满 4096 字节才返回，输出停顿时日志卡住不显示
                data = proc.stdout.read1(4096)
                if not data:
                    break
                text = cm.decode_process_output(data)
                self.log(text)

                combined = self.output_tail + text
                self.output_tail = combined[-500:]
                if not self.fatal_hint and not self.url:
                    self.fatal_hint = _fatal_hint_for(combined)

                if not comfy and _BIND_ERROR_RE.search(combined) and not self.url:
                    self.set_status("starting",
                                    "检测到端口被占用，Forge 正在自动尝试其他端口，请以后面出现的地址为准")

                m = url_re.search(combined)
                if m and not self.url:
                    url = m.group(1).rstrip("/.,").replace("0.0.0.0", "127.0.0.1")
                    self._mark_ready(gen, url, f"已就绪，实际访问地址: {url}", auto_open)
        except Exception:
            self.log("\n[启动器] 读取进程输出时出错:\n" + traceback.format_exc()[-800:] + "\n")
        finally:
            rc = proc.wait()
            # 程序自己发起的重启（ComfyUI-Manager 装/卸节点后的自动重启，新旧版
            # Manager 退出前都会打一行 "Restarting..."）是正常流程，不是崩溃。
            # 不依赖返回码：旧版 Manager 在 Windows 上用 os.execv 重启，老进程的
            # 退出码没有保证。注意这种重启后新 ComfyUI 进程可能已经脱管在后台
            # 继续跑着，提示里别说死「必须重启」。
            manager_restart = "restarting" in self.output_tail.lower()
            # 还没就绪就退出、且输出里认出了致命错误：webui.bat 结尾有 pause，
            # 按任意键后返回码是 0，不能凭返回码当成「正常退出」
            failed_start = bool(self.fatal_hint) and not self.url
            self.log(f"\n[启动器] 进程已结束，返回码: {rc}\n")
            if failed_start:
                self.log(self.fatal_hint + "\n")
                if "显卡驱动太旧" in self.fatal_hint:
                    try:
                        import cuda_compat as cc
                        url = cc.DRIVER_URL
                    except Exception:
                        url = ""
                    self.emit("driver_outdated", text=self.fatal_hint.replace("[启动器] ", ""), url=url)
            # 只收尾「自己这一代、自己这个进程」：gen 对不上，或 self.proc 已经
            # 换成新一轮启动的进程时，什么都不动——否则会把刚启动的进程状态清掉
            if self.gen_alive(gen) and self.proc is proc:
                self.proc = None
                self.url = None
                self.port = None
                if manager_restart:
                    self.log("[启动器] 这是程序自己的自动重启（ComfyUI-Manager 装/卸节点后就是这样重启的），"
                             "不是崩溃。如果界面还能正常打开就不用管——新进程可能已在后台运行；"
                             "打不开的话再点一次「启动」。\n")
                    self.set_status("stopped", "已正常退出（自动重启）")
                elif failed_start:
                    self.set_status("stopped", "启动失败（原因见运行日志末尾）")
                elif rc == 0:
                    self.set_status("stopped", "已正常退出")
                else:
                    self.set_status("stopped", f"已停止（返回码 {rc}）")
                self.emit("state", running=False)

    # ---------------------------------------------------------- 停止 ----
    def kill_tree(self):
        if self.proc and self.proc.poll() is None:
            kill_process_tree(self.proc.pid)
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:
                pass
            return True
        return False

    def stop(self):
        # 先作废代次，再动手杀进程（顺序反了会把正在退出的进程判成「刚就绪」）
        with self.lock:
            self.gen += 1
            self.opened = True
            self.url = None
        killed = self.kill_tree()
        if killed:
            self.log(f"\n[启动器] 已终止 {self.label()} 进程树（含子进程，端口已释放）\n")
        self.proc = None
        self.port = None
        self.set_status("stopped", "已停止")
        self.emit("state", running=False)
        return {"ok": True, "killed": killed}
