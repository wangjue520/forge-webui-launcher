# -*- coding: utf-8 -*-
"""
「图片反推」页的自然语言反推：管常驻的模型进程、本地 / 云端两种反推、显存自动释放。

本地：准备（找模型 / 下载 → 准备环境 → 起 vlm_caption_worker.py → 加载）一次，模型常驻显存，之后连续反推不用重载。
  显存什么时候还回去：闲置 IDLE_SEC 秒；发现在跑图（反推空闲时显卡占用率持续很高——WebUI / ComfyUI 在出图）；
  点「释放显存」；启动器退出。正在反推时不会释放。
云端：OpenAI 兼容接口并发请求；拒答的图可以交给已经下好的本地模型补上（和训练器一样）。

事件都发到 "caption" scope：
  prep_log / prep_progress / ready / prep_failed / released
  progress / item / done / failed
"""
import json
import os
import subprocess
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import config_manager as cm
import vlm_caption_core as vc
import vlm_venv_manager as vv

SCOPE = "caption"
IDLE_SEC = 600          # 闲置 10 分钟释放显存
GPU_BUSY_PCT = 80       # 反推空闲时显卡占用率超过这个数……（出图时是 95–100%；实测不出图时浏览器、视频等也能到 20–40%）
GPU_BUSY_HITS = 3       # ……连续这么多次（每 WATCH_SEC 秒查一次，约 15 秒）就当是在跑图
WATCH_SEC = 5
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlm_caption_worker.py")


def gpu_utilization():
    """所有 N 卡里最高的占用率（%）；没有 nvidia-smi 返回 None（AMD 等：只靠闲置时间释放）。"""
    try:
        import cuda_compat as cc
        exe = cc.find_nvidia_smi()
        if not exe:
            return None
        r = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, timeout=10, creationflags=_NO_WINDOW)
        vals = [int(x.strip()) for x in cm.decode_process_output(r.stdout).splitlines() if x.strip().isdigit()]
        return max(vals) if vals else None
    except Exception:
        return None


class CaptionService:
    def __init__(self, emit, get_cfg, trainer_root, torch_tag=lambda: "cu128", spawn=None,
                 gpu_util=gpu_utilization, worker_cmd=None):
        """emit(scope, event, **kw)；get_cfg() 当前配置；trainer_root() 训练器目录；
        worker_cmd(python) 测试用：换成假的模型进程。"""
        self._emit = emit
        self.get_cfg = get_cfg
        self.trainer_root = trainer_root
        self.torch_tag = torch_tag
        self.spawn = spawn or (lambda fn, name: threading.Thread(target=fn, name=name, daemon=True).start())
        self.gpu_util = gpu_util
        self.worker_cmd = worker_cmd or (lambda py: [py, "-I", "-X", "utf8", "-u", WORKER])
        self.lock = threading.RLock()
        self.proc = None
        self.loaded = None          # {"key", "quant", "bs", "source", "dir"}
        self.preparing = False
        self.running = False
        self.cancel = False
        self.last_used = time.time()
        self._events = None         # 当前在等的模型进程回报
        self._busy_hits = 0
        self._watch = None
        # 取消正在生成的一批：建出这个文件，模型进程每批之间看一眼（不能用 stdin，见 vlm_caption_worker.py 开头）
        self.cancel_file = os.path.join(tempfile.gettempdir(), f"wwy_vlm_cancel_{os.getpid()}_{id(self)}.flag")

    def emit(self, event, **kw):
        self._emit(SCOPE, event, **kw)

    # ---------------- 状态 ----------------
    def info(self):
        root = self.trainer_root()
        return {"models": vc.model_list(root), "default_model": vc.DEFAULT_MODEL,
                "prompts": vc.PROMPT_PRESETS, "prompt_labels": vc.PROMPT_LABELS, "api_presets": vc.API_PRESETS,
                "env": vv.status(root), "trainer_models_dir": vc.trainer_models_dir(root),
                "launcher_models_dir": vc.LAUNCHER_MODELS, "loaded": self.loaded, "preparing": self.preparing,
                "running": self.running, "idle_minutes": IDLE_SEC // 60}

    def busy(self):
        return self.preparing or self.running

    # ---------------- 模型进程 ----------------
    def _start_proc(self, py):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
                   HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",   # 模型都下好了，别让 transformers 去联网检查
                   WWY_PARENT_PID=str(os.getpid()))                # 启动器一退出，模型进程马上跟着退出
        for k in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(k, None)
        p = subprocess.Popen(self.worker_cmd(py), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=env, creationflags=_NO_WINDOW)
        self.proc = p
        if self._events is not None:
            self._events.owner = p
        threading.Thread(target=self._read, args=(p,), name="vlm-read", daemon=True).start()
        return p

    def _read(self, p):
        for raw in p.stdout:
            line = cm.decode_process_output(raw).rstrip("\r\n")
            if not line:
                continue
            if line.startswith("@@"):
                try:
                    ev = json.loads(line[2:])
                except ValueError:
                    ev = None
                if ev is not None:
                    q = self._events
                    if q is not None and q.owner is p:
                        q.append(ev)
                        q.cond.set()
                    continue
            self.emit("prep_log" if self.preparing else "log", text=line)
        try:
            p.stdout.close()
        except Exception:
            pass
        q = self._events
        if q is not None and q.owner is p:   # 换模型时旧进程退出，不要把它的「退出」当成新进程的
            q.append({"ev": "exit", "code": p.wait()})
            q.cond.set()
        with self.lock:
            if self.proc is p:
                self.proc = None
                if self.loaded and not self.preparing:
                    self.loaded = None
                    self.emit("released", reason="模型进程退出了")

    def _send(self, cmd):
        p = self.proc
        if not p or p.poll() is not None:
            raise vc.CaptionError("模型进程已经退出")
        p.stdin.write((json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8"))
        p.stdin.flush()

    def _wait(self, q, kinds, timeout=None):
        """等模型进程回报 kinds 里的一种（或者进程退出）。返回事件；中间的 item 事件交给 on_item。"""
        t0 = time.time()
        while True:
            while q:
                ev = q.pop(0)
                if ev.get("ev") in kinds or ev.get("ev") == "exit":
                    return ev
                if ev.get("ev") == "item" and q.on_item:
                    q.on_item(ev)
            q.cond.wait(1)
            q.cond.clear()
            if timeout and time.time() - t0 > timeout:
                return {"ev": "timeout"}

    def _new_events(self, on_item=None):
        q = _Events()
        q.on_item = on_item
        q.owner = self.proc
        self._events = q
        return q

    def release(self, reason="手动释放"):
        with self.lock:
            if self.running:
                return {"ok": False, "error": "正在反推，等它结束再释放"}
            p, self.proc = self.proc, None
            was = self.loaded
            self.loaded = None
        if p:
            self._stop_proc(p)
        if was:
            self.emit("released", reason=reason)
        return {"ok": True}

    @staticmethod
    def _stop_proc(p, wait=5):
        """让模型进程自己退出（显存随进程一起还回去），等不到就强制结束。"""
        if p.poll() is None:
            try:
                p.stdin.write(b'{"cmd": "quit"}\n')
                p.stdin.flush()
                p.wait(wait)
            except Exception:
                pass
            if p.poll() is None:
                try:
                    from process_manager import kill_process_tree
                    kill_process_tree(p.pid)
                except Exception:
                    p.kill()
                p.wait(10)
        try:
            p.stdin.close()
        except Exception:
            pass

    # ---------------- 显存自动释放 ----------------
    def _ensure_watch(self):
        if self._watch and self._watch.is_alive():
            return
        self._watch = threading.Thread(target=self._watch_loop, name="vlm-watch", daemon=True)
        self._watch.start()

    def _watch_loop(self):
        while self.loaded:
            time.sleep(WATCH_SEC)
            self.check_idle()

    def check_idle(self, now=None):
        """闲置太久 / 在跑图就释放。返回释放原因（没释放返回 ""）。"""
        if not self.loaded or self.busy():
            self._busy_hits = 0
            return ""
        now = now or time.time()
        if now - self.last_used >= IDLE_SEC:
            self.release(f"闲置 {IDLE_SEC // 60} 分钟，已释放显存")
            return "idle"
        u = self.gpu_util()
        # 反推空闲时自己不占显卡算力，这时显卡忙 = 别的程序在用（多半是 WebUI / ComfyUI 在出图）
        self._busy_hits = self._busy_hits + 1 if u is not None and u >= GPU_BUSY_PCT else 0
        if self._busy_hits >= GPU_BUSY_HITS:
            self._busy_hits = 0
            self.release("检测到显卡在跑图，已释放自然语言模型占的显存")
            return "gpu"
        return ""

    # ---------------- 准备（下载 + 环境 + 加载）----------------
    def prepare(self, key, quant="auto", download=True):
        if key not in vc.VLM_MODELS:
            return {"ok": False, "error": "未知的模型"}
        with self.lock:
            if self.busy():
                return {"ok": False, "error": "正在准备 / 反推，等它结束"}
            if self.loaded and self.loaded["key"] == key and quant in ("auto", self.loaded["quant"]) and self.proc:
                self.last_used = time.time()
                self.emit("ready", **self.loaded)
                return {"ok": True, "already": True}
            self.preparing = True
            self.cancel = False
        self.spawn(lambda: self._prepare(key, quant, download), "vlm-prepare")
        return {"ok": True}

    def _prepare(self, key, quant, download):
        try:
            self._load(key, quant, download)
            self.emit("ready", **self.loaded)
        except Exception as e:
            msg = str(e) or type(e).__name__
            if not isinstance(e, (vc.CaptionError, vv.VenvError)):
                msg = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            self.emit("prep_failed", msg=msg)
        finally:
            self.preparing = False

    def _load(self, key, quant, download=True):
        """同步地准备好 key 这个模型（在后台线程里调）。"""
        log = lambda t: self.emit("prep_log", text=t)
        cancelled = lambda: self.cancel
        root = self.trainer_root()
        info = vc.VLM_MODELS[key]
        d, where = vc.find_model(key, root)
        if d:
            log(f"使用{'训练器' if where == 'trainer' else '启动器'}里已下载的模型：{d}")
        elif not download:
            raise vc.CaptionError(f"{info['label']} 还没有下载")
        else:
            d = vc.download_dir(key, root)
            log(f"下载 {info['repo']}（约 {info['size_gb']} GB）到 {d}" +
                ("（训练器的模型目录，训练器也能直接用）" if vc.trainer_models_dir(root) and d.startswith(vc.trainer_models_dir(root))
                 else "（训练器装好后也会来这里找）"))
            vc.download_repo(info["repo"], d,
                             progress_cb=lambda done, total, name: self.emit("prep_progress", stage="model", name=name,
                                                                             downloaded=done, total=total),
                             log_cb=log, cancel_flag=cancelled, cfg=self.get_cfg())
        if self.cancel:
            raise vc.CaptionError("已取消")
        src, py = vv.ensure(root, self.get_cfg(), self.torch_tag(), log_cb=log,
                            progress_cb=lambda done, total: self.emit("prep_progress", stage="env", name="PyTorch",
                                                                      downloaded=done, total=total),
                            cancel_flag=cancelled)
        if self.cancel:
            raise vc.CaptionError("已取消")
        # 换模型 / 换精度：先把旧的放掉
        if self.proc:
            p, self.proc, self.loaded = self.proc, None, None
            self._stop_proc(p, wait=10)
        q = self._new_events()
        self._start_proc(py)
        hello = self._wait(q, ("hello",), timeout=120)
        if hello.get("ev") != "hello":
            raise vc.CaptionError("模型进程没有启动起来" + (f"（退出码 {hello.get('code')}）" if hello.get("ev") == "exit" else ""))
        log(f"加载 {info['label'].split('（')[0]} …（要先载入 PyTorch，第一次可能要一两分钟）")
        self._send({"cmd": "load", "dir": d, "key": key, "quant": quant or "auto", "size_gb": info["size_gb"]})
        ev = self._wait(q, ("loaded", "load_failed"))
        if ev.get("ev") != "loaded":
            self.release("加载失败")
            err = ev.get("error") or f"模型进程退出了（退出码 {ev.get('code')}）"
            if "out of memory" in err.lower() or "OutOfMemory" in err:
                err += "\n\n显存不够：可以把精度改成 4bit、换小一点的 Qwen3-VL-4B、关掉正在跑图的 WebUI / ComfyUI，或者改用云端 API。"
            raise vc.CaptionError("模型加载失败：" + err)
        self.loaded = {"key": key, "quant": ev.get("quant"), "bs": ev.get("bs"), "source": src, "dir": d,
                       "label": info["label"]}
        self.last_used = time.time()
        self._ensure_watch()

    def cancel_all(self):
        self.cancel = True
        if self.running:
            try:
                open(self.cancel_file, "w").close()
            except OSError:
                pass
        return {"ok": True}

    def _clear_cancel_file(self):
        try:
            os.remove(self.cancel_file)
        except OSError:
            pass

    # ---------------- 反推 ----------------
    def run(self, images, opts):
        """opts: source local/api，tags {路径: WD14 标签}（「标签 + 自然语言」），feed_tags，save_txt，
        prompt {preset, prompt, torii_format, use_names, max_tokens}，api {base_url, api_key, model, concurrency, fallback_local}"""
        images = [p for p in (images or []) if isinstance(p, str)]
        if not images:
            return {"ok": False, "error": "请先添加图片"}
        opts = opts or {}
        local = opts.get("source") != "api"
        with self.lock:
            if self.busy():
                return {"ok": False, "error": "正在准备 / 反推，等它结束"}
            if local and not (self.loaded and self.proc):
                return {"ok": False, "error": "请先加载模型"}
            if not local:
                a = opts.get("api") or {}
                if not a.get("base_url") or not a.get("model"):
                    return {"ok": False, "error": "请填写 API 地址和模型名"}
            self.running = True
            self.cancel = False
        self.spawn(lambda: self._run(images, opts, local), "vlm-run")
        return {"ok": True}

    def _finish_item(self, path, text, opts, state):
        tags = (opts.get("tags") or {}).get(path) or ""
        cap = vc.compose(tags if opts.get("keep_tags", True) else "", text)
        txt = False
        if opts.get("save_txt"):
            try:
                with open(os.path.splitext(path)[0] + ".txt", "w", encoding="utf-8") as f:
                    f.write(cap)
                txt = True
            except OSError as e:
                self.emit("log", text=f"保存 .txt 失败 {os.path.basename(path)}: {e}")
        with state["lock"]:
            state["done"] += 1
            state["seen"].add(path)
            self.emit("progress", done=state["done"], total=state["total"])
        self.emit("item", path=path, text=vc.clean_text(text), tags=tags, caption=cap, txt=txt)

    def _fail_item(self, path, error, state, refused=False):
        with state["lock"]:
            state["done"] += 1
            state["failed"] += 1
            state["seen"].add(path)
            self.emit("progress", done=state["done"], total=state["total"])
        self.emit("item", path=path, error=error, refused=refused)

    def _run(self, images, opts, local):
        state = {"lock": threading.Lock(), "done": 0, "total": len(images), "failed": 0, "seen": set()}
        try:
            if local:
                self._run_local(images, opts, state)
            else:
                self._run_api(images, opts, state)
            self.emit("done", cancelled=self.cancel, failed=state["failed"], total=state["total"])
        except Exception as e:
            msg = str(e) if isinstance(e, vc.CaptionError) else f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
            self.emit("failed", msg=msg)
        finally:
            self.running = False
            self.last_used = time.time()

    def _local_items(self, images, opts):
        key = self.loaded["key"]
        p = opts.get("prompt") or {}
        out = []
        for path in images:
            tags = vc.split_tags((opts.get("tags") or {}).get(path)) if opts.get("feed_tags", True) else None
            system, user = vc.build_prompt(key, p, tags)
            out.append({"file": path, "system": system, "user": user})
        return out

    def _run_local(self, images, opts, state, on_error=None):
        p = opts.get("prompt") or {}
        def on_item(ev):
            f = ev.get("file")
            if ev.get("error"):
                (on_error or (lambda f, e: self._fail_item(f, e, state)))(f, ev["error"])
            else:
                self._finish_item(f, ev.get("text") or "", opts, state)
            self.last_used = time.time()

        q = self._new_events(on_item)
        self._clear_cancel_file()
        if self.cancel:
            return
        self._send({"cmd": "run", "items": self._local_items(images, opts), "max_tokens": int(p.get("max_tokens") or 512),
                    "temperature": 0.6, "cancel_file": self.cancel_file})
        try:
            ev = self._wait(q, ("done",))
        finally:
            self._clear_cancel_file()
        if ev.get("ev") == "exit":
            raise vc.CaptionError(f"模型进程意外退出（退出码 {ev.get('code')}）。多半是显存 / 内存不够，"
                                  "可以把精度改成 4bit、换小一点的模型，或者改用云端 API。")
        if ev.get("error"):
            raise vc.CaptionError(ev["error"])

    def _run_api(self, images, opts, state):
        api = dict(opts.get("api") or {})
        p = opts.get("prompt") or {}
        prompt = vc.user_prompt(p)
        conc = max(1, min(16, int(api.get("concurrency") or 4)))
        refused = []
        fatal = {}
        lock = threading.Lock()

        def one(path):
            if self.cancel or fatal:
                return
            text = None
            user = prompt
            tags = (opts.get("tags") or {}).get(path)
            if tags and opts.get("feed_tags", True):
                user += "\nReference tags (may contain mistakes): " + tags
            for attempt in range(3):
                try:
                    text = vc.api_call(api, path, user, p.get("max_tokens"))
                    break
                except vc.Refused as e:   # 不重试（换个说法拒绝而已，还多花钱）
                    with lock:
                        refused.append((path, str(e)))
                    return
                except Exception as e:
                    msg = vc.api_error_zh(e)
                    if vc.is_fatal_api_error(e):
                        fatal["msg"] = msg
                        return self._fail_item(path, msg, state)
                    if attempt == 2 or getattr(e, "code", None) == 400:
                        return self._fail_item(path, msg, state)
                    time.sleep(2 * (attempt + 1))
            if text is not None:
                self._finish_item(path, text, opts, state)

        with ThreadPoolExecutor(conc) as ex:
            list(ex.map(one, images))
        if fatal:
            raise vc.CaptionError(fatal["msg"])
        if not refused:
            return
        refused.sort(key=lambda x: images.index(x[0]))
        why = dict(refused)
        if self.cancel or not api.get("fallback_local", True):
            for path, e in refused:
                self._fail_item(path, "API 拒答：" + e, state, refused=True)
            return
        key = self._fallback_model()
        if not key:
            for path, e in refused:
                self._fail_item(path, f"API 拒答：{e}（本地模型还没下载，切到「本地模型」可以补上）", state, refused=True)
            return
        self.emit("log", text=f"── {len(refused)} 张被 API 拒答，改用本地 {vc.VLM_MODELS[key]['label'].split('（')[0]} 反推这些图 ──")
        try:
            if not (self.loaded and self.proc and self.loaded["key"] == key):
                self.preparing = True
                try:
                    self._load(key, "auto", download=False)
                finally:
                    self.preparing = False
                self.emit("ready", **self.loaded)
            self._run_local([x[0] for x in refused], opts, state,
                            on_error=lambda f, e: self._fail_item(f, f"API 拒答：{why.get(f)}；本地模型也失败：{e}", state,
                                                                  refused=True))
        except Exception as e:
            self.emit("log", text=f"[失败] 本地模型没有补上拒答的图：{e}")
            for path, r in refused:
                if path not in state["seen"]:
                    self._fail_item(path, "API 拒答：" + r, state, refused=True)

    def _fallback_model(self):
        """补拒答用哪个本地模型：已经加载的；没有就第一个已经下好的（默认模型优先）。环境也得能用。"""
        if self.loaded and self.proc:
            return self.loaded["key"]
        root = self.trainer_root()
        if not vv.pick(root)[1]:
            return ""
        for key in [vc.DEFAULT_MODEL] + [k for k in vc.VLM_MODELS if k != vc.DEFAULT_MODEL]:
            if vc.find_model(key, root)[0]:
                return key
        return ""


class _Events(list):
    def __init__(self):
        super().__init__()
        self.cond = threading.Event()
        self.on_item = None
        self.owner = None
