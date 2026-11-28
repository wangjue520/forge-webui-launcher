"""
3.2 新功能的后端测试——尽量用真东西，不用假替身：
  - 真的 Python 虚拟环境 + 真的 pip（装 / 卸启动器目录里自带的 bottle wheel，不联网）：
    插件管理跑 pip 时记录包变化；实例真启动一个进程，运行期间装的包记在「这次运行期间」名下
  - 一键修复：模型从本机 HTTP 服务真下载；缺的两个节点从本机 git 仓库真 clone 成一个安装任务，节点依赖真 pip 装进虚拟环境
  - 输出管理：真的 SQLite 索引扫描真的 PNG（不同长宽比、带 A1111 参数）；收藏夹复制到文件夹 / 不转移；
    旧索引升级；对比读出步数 / CFG；复制到系统剪贴板再读回来
  - 模型收藏夹、版本号、教程 PDF
运行：python -m unittest tests.test_v32_backend（第一次要建虚拟环境，约半分钟）
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import http.server
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

import diagnose as dg  # noqa: E402
import model_favorites as mf  # noqa: E402
import output_index as oi  # noqa: E402
import pip_history as ph  # noqa: E402
import process_manager as pmgr  # noqa: E402
import updater  # noqa: E402
import webview_api as wa  # noqa: E402
from test_comfy_bridge import bridge_line  # noqa: E402
from test_diag_takeover import FakeRunner  # noqa: E402
from test_diagnose import _Base, _touch, fake_api  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WHEEL = os.path.join(ROOT, "bottle-0.13.4-py2.py3-none-any.whl")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_VENV = {}


def venv_python():
    """整个测试文件共用一个真的虚拟环境（建一次要十几秒）"""
    if "py" not in _VENV:
        d = tempfile.mkdtemp(prefix="wwy_venv_")
        subprocess.run([sys.executable, "-m", "venv", d], check=True, capture_output=True, creationflags=NO_WINDOW)
        _VENV["dir"] = d
        _VENV["py"] = os.path.join(d, "Scripts", "python.exe") if os.name == "nt" else os.path.join(d, "bin", "python")
    return _VENV["py"]


def tearDownModule():
    if "dir" in _VENV:
        shutil.rmtree(_VENV["dir"], ignore_errors=True)


def pip_has(py, name):
    r = subprocess.run([py, "-m", "pip", "show", name], capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def png(path, w, h, params=None, color=(200, 60, 60)):
    """真的 PNG（可带 A1111 的 parameters 文本块）"""
    raw = zlib.compress(b"".join(b"\x00" + bytes(color) * w for _ in range(h)))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    if params:
        data += chunk(b"tEXt", b"parameters\x00" + params.encode("latin-1"))
    data += chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


A1111 = ("1girl, solo\nNegative prompt: lowres\n"
         "Steps: 28, Sampler: Euler a, Schedule type: Karras, CFG scale: 6.5, Seed: 123, Size: {w}x{h}, Model: ponyV6")


class _Serve:
    """本机 HTTP 服务：一键修复「下载模型」从这里真下载"""

    def __init__(self, files):
        files = dict(files)

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = files.get(self.path.split("?")[0])
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, creationflags=NO_WINDOW)


# ============================================================ pip 记录（真 pip）

class PipRealTest(unittest.TestCase):
    def setUp(self):
        self.hdir = tempfile.mkdtemp()
        self.p = mock.patch.object(ph, "HISTORY_DIR", self.hdir)
        self.p.start()
        self.py = venv_python()
        subprocess.run([self.py, "-m", "pip", "uninstall", "-y", "bottle"], capture_output=True, creationflags=NO_WINDOW)

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.hdir, ignore_errors=True)

    def test_ext_pip_install_and_uninstall_recorded(self):
        api = fake_api({"instances": []})
        api._ext_cancel, api._ext_proc = False, None
        rc = wa._stream_cmd(api, "ext", self.py, ["-m", "pip", "install", "--no-index", "--no-deps", WHEEL],
                            ROOT, None, 300)
        self.assertEqual(rc, 0)
        self.assertTrue(pip_has(self.py, "bottle"))
        rows = ph.history(self.py)
        self.assertEqual(rows[0]["added"], {"bottle": "0.13.4"})
        self.assertEqual(rows[0]["reason"], "插件管理：安装 bottle-0.13.4-py2.py3-none-any.whl")
        self.assertTrue(rows[-1]["baseline"])                      # 第一次：只记基准
        wa._stream_cmd(api, "ver", self.py, ["-m", "pip", "uninstall", "-y", "bottle"], ROOT, None, 300)
        rows = ph.history(self.py)
        self.assertEqual(rows[0]["removed"], {"bottle": "0.13.4"})
        self.assertEqual(rows[0]["reason"], "版本管理：卸载 bottle")
        self.assertIn("pip", ph.load(self.py)["latest"])           # 记录的是真实环境的包列表

    def test_changes_between_runs_get_their_own_entry(self):
        """两次启动器操作之间别人装的（手动 pip）单独记一条，不混进下一次操作"""
        api = fake_api({"instances": []})
        api._ext_cancel, api._ext_proc = False, None
        ph.record(self.py, "x")                                        # 基准
        subprocess.run([self.py, "-m", "pip", "install", "--no-index", "--no-deps", WHEEL], check=True,
                       capture_output=True, creationflags=NO_WINDOW)  # 手动装
        wa._stream_cmd(api, "ext", self.py, ["-m", "pip", "uninstall", "-y", "bottle"], ROOT, None, 300)
        rows = ph.history(self.py)
        self.assertEqual(rows[0]["removed"], {"bottle": "0.13.4"})
        self.assertIn("不是启动器装的", rows[1]["reason"])
        self.assertEqual(rows[1]["added"], {"bottle": "0.13.4"})


class RunnerRealTest(_Base):
    """真启动一个「ComfyUI」进程：打出监听地址 → 就绪；运行期间自己 pip 装包（像 ComfyUI-Manager 那样）→ 退出"""

    def setUp(self):
        super().setUp()
        self.hdir = tempfile.mkdtemp()
        self.p = mock.patch.object(ph, "HISTORY_DIR", self.hdir)
        self.p.start()
        self.py = venv_python()
        subprocess.run([self.py, "-m", "pip", "uninstall", "-y", "bottle"], capture_output=True, creationflags=NO_WINDOW)
        with open(os.path.join(self.comfy, "main.py"), "w", encoding="utf-8") as f:
            f.write("import subprocess, sys, time\n"
                    "print('To see the GUI go to: http://127.0.0.1:8199', flush=True)\n"
                    "time.sleep(1.5)\n"
                    f"subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps', r'{WHEEL}'])\n"
                    "print('done', flush=True)\n")
        inst = self.api.cfg["instances"][2]
        inst["custom_python_path"] = self.py
        self.api.cfg["comfy_bridge"] = False
        self.api._emit = lambda scope, type_, **k: self.api.events.append((scope, type_, k))
        self.api._spawn = lambda fn, name=None: threading.Thread(target=fn, daemon=True).start()

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.hdir, ignore_errors=True)
        super().tearDown()

    def test_real_process_records(self):
        r = pmgr.InstanceRunner(self.api, "cf")
        with mock.patch.object(wa, "_library_launch_extras", lambda *a: None), \
                mock.patch.object(pmgr, "find_listening_pid", lambda port: None), \
                mock.patch.object(pmgr.webbrowser, "open", lambda url: None):
            res = r.start(self.comfy)
            self.assertTrue(res.get("ok"), res)
            t0 = time.time()
            while r.state != "ready" and time.time() - t0 < 30:
                time.sleep(0.2)
            self.assertEqual(r.state, "ready")
            while r.running() and time.time() - t0 < 120:
                time.sleep(0.3)
        t0 = time.time()
        while time.time() - t0 < 30 and not any((e.get("added") or {}).get("bottle") for e in ph.history(self.py)):
            time.sleep(0.3)
        rows = ph.history(self.py)
        hit = [e for e in rows if (e.get("added") or {}).get("bottle")]
        self.assertEqual(len(hit), 1, rows)
        self.assertIn("这次运行期间的变化", hit[0]["reason"])
        self.assertTrue(any(e.get("baseline") for e in rows))          # 启动前拍的基准


# ============================================================ 一键修复（真下载、真 git、真 pip）

class FixAllRealTest(_Base):
    def setUp(self):
        super().setUp()
        self.hdir = tempfile.mkdtemp()
        self.p = mock.patch.object(ph, "HISTORY_DIR", self.hdir)
        self.p.start()
        self.py = venv_python()
        subprocess.run([self.py, "-m", "pip", "uninstall", "-y", "bottle"], capture_output=True, creationflags=NO_WINDOW)
        self.api.cfg["instances"][2]["custom_python_path"] = self.py
        self.api.cfg["diag_llm_api_key"] = ""
        self.api._ext_thread, self.api._ext_proc, self.api._ext_cancel = None, None, False
        self.api._spawn = lambda fn, name=None: (lambda t: (t.start(), t)[1])(threading.Thread(target=fn, daemon=True))
        self.runner = FakeRunner(self.api, "cf", [])
        self.runner._running, self.runner.state = True, "ready"
        self.api._runner = lambda iid=None: self.runner
        # 两个本机 git 仓库当「缺的节点」：Alpha 带 requirements.txt（装 bottle），Beta 没依赖
        # （别叫 ComfyUI-Foo：_Base 已经在 custom_nodes 里放了一个，会被正确地当成「已安装」跳过）
        self.repos = tempfile.mkdtemp()
        self.urls = []
        for name, req in (("ComfyUI-Alpha", WHEEL + "\n"), ("ComfyUI-Beta", None)):
            work = os.path.join(self.repos, "work_" + name)
            _touch(os.path.join(work, "__init__.py"), "NODE_CLASS_MAPPINGS = {}\n")
            if req:
                _touch(os.path.join(work, "requirements.txt"), req)
            git("init", "-q", cwd=work)
            git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=work)
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init", cwd=work)
            bare = os.path.join(self.repos, name + ".git")
            git("clone", "-q", "--bare", work, bare)
            self.urls.append((bare[:-4].replace("\\", "/"), name))
        self.models = {"/a.safetensors": b"A" * 4096, "/b.safetensors": b"B" * 8192}
        self.srv = _Serve(self.models)

    def tearDown(self):
        self.srv.close()
        self.p.stop()
        shutil.rmtree(self.hdir, ignore_errors=True)
        shutil.rmtree(self.repos, ignore_errors=True)
        super().tearDown()

    def test_one_click_real(self):
        mdir = os.path.join(self.comfy, "models", "loras")
        models = [{"name": n, "folder": "loras", "node_type": "LoraLoader", "input": "lora_name", "dirs": [mdir],
                   "url": self.srv.base + "/" + n} for n in ("a.safetensors", "b.safetensors")]
        self.runner.log("[启动器] 正在启动 ComfyUI ...\n" + bridge_line(nodes=["FooNode", "BarNode"], models=models))
        repos = [(u, n, [n]) for u, n in self.urls]
        with mock.patch.object(dg, "load_model_list", lambda *a, **k: []), \
                mock.patch.object(dg, "load_node_map", lambda *a, **k: {"x": 1}), \
                mock.patch.object(dg, "resolve_node_types", lambda types, nm: (repos, [])), \
                mock.patch.object(wa, "_gh_sources", lambda cfg, log=None: [""]), \
                mock.patch.object(wa, "_pip_mirror_argsets", lambda cfg, log=None: []):
            job = wa._diag_new_job(self.api)
            wa._diag_fixall_run(self.api, job, {"type": "inst", "iid": "cf"}, "")
        done = [k for s, t, k in self.api.events if (s, t) == ("diag", "fixall_done")]
        self.assertEqual(len(done), 1)
        steps = [k["text"] for s, t, k in self.api.events if (s, t) == ("diag", "fixall_step")]
        ext_log = "".join(k.get("text", "") for s, t, k in self.api.events if (s, t) == ("ext", "log"))
        self.assertTrue(done[0]["ok"], (done[0], steps, ext_log[-3000:]))
        for n, body in self.models.items():                                   # 两个模型都真下载下来了
            with open(os.path.join(mdir, n.lstrip("/")), "rb") as f:
                self.assertEqual(f.read(), body)
        cn = os.path.join(self.comfy, "custom_nodes")
        self.assertIn("===== ComfyUI-Alpha =====", ext_log)                  # 两个节点在同一个安装任务里
        self.assertIn("===== ComfyUI-Beta =====", ext_log)
        self.assertTrue(os.path.isfile(os.path.join(cn, "ComfyUI-Alpha", "__init__.py")), ext_log[-2000:])
        self.assertTrue(os.path.isfile(os.path.join(cn, "ComfyUI-Beta", "__init__.py")))
        self.assertTrue(pip_has(self.py, "bottle"), ext_log[-2000:])          # 节点依赖真装进了虚拟环境
        self.assertEqual((self.runner.stops, self.runner.starts), (1, 1))     # 装之前停、装完启动
        rows = ph.history(self.py)                                            # 这次装依赖也记进了变化记录
        self.assertTrue(any((e.get("added") or {}).get("bottle") and e["reason"].startswith("插件管理")
                            for e in rows), rows)


# ============================================================ 输出管理（真 SQLite、真 PNG）

class OutputsRealTest(_Base):
    def setUp(self):
        super().setUp()
        self.data = tempfile.mkdtemp()
        self.patches = [mock.patch.object(oi, "DATA_DIR", self.data),
                        mock.patch.object(oi, "DB_PATH", os.path.join(self.data, "index.db")),
                        mock.patch.object(oi, "THUMB_DIR", os.path.join(self.data, "thumbs"))]
        for p in self.patches:
            p.start()
        self.out = os.path.join(self.comfy, "output")
        self.files = [png(os.path.join(self.out, "wide.png"), 96, 48, A1111.format(w=96, h=48)),
                      png(os.path.join(self.out, "tall.png"), 40, 80, A1111.format(w=40, h=80), (60, 60, 200)),
                      png(os.path.join(self.out, "square.png"), 64, 64)]
        self.src = mock.patch.object(wa, "_output_sources", lambda self_: [("cf", [self.out])])
        self.src.start()
        self.api._spawn = lambda fn, name=None: fn()          # 复制在后台线程做：测试里同步跑完
        self.api.events = []
        self.idx = wa._outidx(self.api)
        self.idx.scan([("cf", [self.out])])
        q = self.idx.query({})
        self.ids = dict(zip(q["names"], q["ids"]))
        self.q = q

    def tearDown(self):
        try:
            self.api._out_server.shutdown()
            self.idx.db.close()
        except Exception:
            pass
        self.src.stop()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.data, ignore_errors=True)
        super().tearDown()

    def test_query_ratios_and_names(self):
        ratio = dict(zip(self.q["names"], self.q["ratios"]))
        self.assertEqual(ratio, {"wide.png": 2.0, "tall.png": 0.5, "square.png": 1.0})

    def test_collection_copy_modes(self):
        dest = os.path.join(self.tmp, "收藏 复制")
        # 不能把「复制到」放进实例输出目录
        bad = wa._api_outputs_collection_op(self.api, "create", None, "坏", None, True, os.path.join(self.out, "fav"))
        self.assertFalse(bad["ok"])
        # 不转移：只记录
        keep = wa._api_outputs_collection_op(self.api, "create", None, "只记录")
        r = wa._api_outputs_collection_op(self.api, "set", keep["id"], "", [self.ids["wide.png"]], True)
        self.assertEqual(r["copying"], 0)
        self.assertFalse(os.path.exists(dest))
        # 复制一份
        c = wa._api_outputs_collection_op(self.api, "create", None, "复制", None, True, dest)
        self.assertEqual([x["copy_dir"] for x in c["collections"] if x["id"] == c["id"]], [dest])
        r = wa._api_outputs_collection_op(self.api, "set", c["id"], "", [self.ids["wide.png"], self.ids["tall.png"]], True)
        self.assertEqual(r["copying"], 2)
        self.assertEqual(sorted(os.listdir(dest)), ["tall.png", "wide.png"])
        self.assertTrue(os.path.isfile(self.files[0]))                             # 原图不动
        # 已经在收藏夹里的再加一次：不重复复制
        r = wa._api_outputs_collection_op(self.api, "set", c["id"], "", [self.ids["wide.png"]], True)
        self.assertEqual(r["copying"], 0)
        # 目标里已有同名但内容不同的：改名成 _1，不覆盖
        png(os.path.join(dest, "square.png"), 8, 8)
        wa._api_outputs_collection_op(self.api, "set", c["id"], "", [self.ids["square.png"]], True)
        self.assertIn("square_1.png", os.listdir(dest))
        # 「只记录」的改成复制、并把已收藏的也复制过去（同名同大小的跳过）
        dest2 = os.path.join(self.tmp, "第二个")
        shutil.copy2(self.files[0], os.path.join(self.tmp, "x"))
        r = wa._api_outputs_collection_op(self.api, "config", keep["id"], "", None, True, dest2, True)
        self.assertEqual(r["copying"], 1)
        self.assertEqual(os.listdir(dest2), ["wide.png"])
        r = wa._api_outputs_collection_op(self.api, "config", keep["id"], "", None, True, dest2, True)
        done = [k for s, t, k in self.api.events if (s, t) == ("outputs", "export_done")][-1]
        self.assertEqual((done["copied"], done["skipped"]), (0, 1))
        # 改回不转移
        r = wa._api_outputs_collection_op(self.api, "config", keep["id"], "", None, True, "")
        self.assertEqual([x["copy_dir"] for x in r["collections"] if x["id"] == keep["id"]], [""])

    def test_old_index_gets_copy_dir_column(self):
        db = os.path.join(self.data, "old.db")
        con = sqlite3.connect(db)
        con.executescript("CREATE TABLE collections(id INTEGER PRIMARY KEY, name TEXT UNIQUE, created REAL);"
                          "INSERT INTO collections(name, created) VALUES('旧的', 1);")
        con.commit()
        con.close()
        with mock.patch.object(oi, "DB_PATH", db):
            idx = oi.OutputIndex()
            self.assertEqual(idx.collections(), [{"id": 1, "name": "旧的", "count": 0, "copy_dir": ""}])
            idx.db.close()

    def test_compare_reads_sampler_params(self):
        r = wa._api_outputs_compare(self.api, [self.ids["wide.png"], self.ids["tall.png"]])
        self.assertTrue(r["ok"], r)
        a = r["items"][0]
        self.assertEqual((a["steps"], a["cfg"], a["seed"], a["model"]), ("28", "6.5", "123", "ponyV6"))
        self.assertEqual((a["width"], a["height"]), (96, 48))

    @unittest.skipUnless(os.name == "nt", "Windows 原生拖动")
    def test_drag_out_hands_real_file_list_to_windows(self):
        """拖出去：在窗口的界面线程上用 WinForms DoDragDrop 交出文件列表（CF_HDROP），而不是浏览器的「下载」"""
        import native_drag

        class FakeForm:            # 代替 pywebview 的 BrowserForm：记下要拖的东西，不真的进入拖动循环
            def __init__(self):
                self.dragged, self.effects = None, None

            def BeginInvoke(self, delegate):
                delegate.Invoke()

            def DoDragDrop(self, data, effects):
                self.dragged = [str(p) for p in data.GetFileDropList()]
                self.effects = str(effects)

        form = FakeForm()
        self.api._window = mock.Mock(native=form)
        r = wa._api_outputs_drag(self.api, [self.ids["wide.png"], self.ids["tall.png"]])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["count"], 2)
        self.assertEqual([os.path.normcase(p) for p in form.dragged],
                         [os.path.normcase(self.files[0]), os.path.normcase(self.files[1])])
        self.assertEqual(form.effects, "Copy")
        self.assertFalse(native_drag._BUSY[0])                       # 拖完解锁，下一次还能拖
        self.api._window = mock.Mock(native=None)                     # 没有原生窗口（远程 / 预览）：给出能看懂的失败
        r = wa._api_outputs_drag(self.api, [self.ids["wide.png"]])
        self.assertFalse(r["ok"])
        self.assertIn("复制图片", r["error"])
        self.assertFalse(wa._api_outputs_drag(self.api, [999999])["ok"])

    @unittest.skipUnless(os.name == "nt", "Windows 剪贴板")
    def test_copy_to_clipboard(self):
        r = wa._api_outputs_copy(self.api, [self.ids["wide.png"]])
        self.assertTrue(r["ok"], r)
        got = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-Command",
                              "Add-Type -AssemblyName System.Windows.Forms;"
                              "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                              "[System.Windows.Forms.Clipboard]::GetFileDropList() | % { $_ };"
                              "'IMG=' + [System.Windows.Forms.Clipboard]::ContainsImage()"],
                             capture_output=True, creationflags=NO_WINDOW).stdout.decode("utf-8", "replace")
        self.assertIn(os.path.normcase(self.files[0]), os.path.normcase(got))
        self.assertIn("IMG=True", got)
        r = wa._api_outputs_copy(self.api, [self.ids["wide.png"], self.ids["tall.png"]])
        self.assertTrue(r["ok"])
        self.assertIn("2 个", r["text"])
        self.assertFalse(wa._api_outputs_copy(self.api, [999999])["ok"])


# ============================================================ 模型收藏夹 / 版本号 / 教程

class ModelFavTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = mock.patch.object(mf, "PATH", os.path.join(self.d, "mc.json"))
        self.p.start()
        self.api = fake_api({"instances": []})

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_crud_never_touches_files(self):
        m = os.path.join(self.d, "Lora", "a.safetensors")
        _touch(m, "x")
        r = wa._api_models_collection_op(self.api, "create", None, "常用")
        self.assertTrue(r["ok"])
        self.assertFalse(wa._api_models_collection_op(self.api, "create", None, "常用")["ok"])   # 重名
        cid = r["id"]
        r = wa._api_models_collection_op(self.api, "set", cid, "", [m.upper()], True)            # 大小写不同也算同一个
        r = wa._api_models_collection_op(self.api, "set", cid, "", [m], True)
        self.assertEqual(r["collections"][0]["count"], 1)
        self.assertEqual(r["member"], {os.path.normcase(os.path.abspath(m)): [cid]})
        self.assertTrue(os.path.isfile(m))
        self.assertEqual(os.listdir(os.path.dirname(m)), ["a.safetensors"])                     # 没动文件
        os.remove(m)
        self.assertEqual(wa._api_models_collections(self.api)["collections"][0]["alive"], 0)
        r = wa._api_models_collection_op(self.api, "rename", cid, "改名")
        self.assertEqual(r["collections"][0]["name"], "改名")
        r = wa._api_models_collection_op(self.api, "set", cid, "", [m], False)
        self.assertEqual(r["collections"][0]["count"], 0)
        r = wa._api_models_collection_op(self.api, "delete", cid)
        self.assertEqual(r["collections"], [])
        self.assertFalse(wa._api_models_collection_op(self.api, "delete", cid)["ok"])


class VersionAndAboutTest(unittest.TestCase):
    def test_version_3_2(self):
        self.assertEqual(updater.version_of(221), "3.2.000")
        with open(os.path.join(ROOT, "changelog.json"), encoding="utf-8") as f:
            top = json.load(f)["entries"][0]
        self.assertEqual((top["version"], top["count"]), ("3.2.000", 221))
        self.assertEqual(updater.version_of(top["count"]), top["version"])

    def test_open_tutorial(self):
        api = fake_api({"instances": []})
        opened = []
        with mock.patch.object(wa.os, "startfile", opened.append, create=True):
            r = wa.LauncherApi.open_tutorial(api)
        self.assertTrue(r["ok"], r)
        self.assertTrue(opened[0].lower().endswith(".pdf") and os.path.isfile(opened[0]))


if __name__ == "__main__":
    unittest.main()
