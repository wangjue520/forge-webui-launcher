"""
「图片反推」页的自然语言反推（app/vlm_caption_core.py、vlm_venv_manager.py、vlm_caption_service.py）的回归测试。

核心：提示词 / 拼接 / 拒答判断和训练器一致；模型目录两边互相找得到（训练器的优先、下了一半接着下）；
     仓库下载换源、sha256 校验、写训练器认得的 .lora_complete。
环境：训练器的训练环境能用就用，不能用再用启动器自己的 vlm_venv。
服务：用一个假的模型进程（和 vlm_caption_worker.py 同样的 stdin / stdout 协议）跑准备 → 反推 → 释放，
     以及闲置 / 跑图时自动释放显存、云端 API 的拒答处理。
运行：python -m unittest tests.test_vlm_caption
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

import vlm_caption_core as vc  # noqa: E402
import vlm_caption_service as vcs  # noqa: E402
import vlm_venv_manager as vv  # noqa: E402


def _touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def _model_dir(d, marker=True, shards=("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors")):
    _touch(os.path.join(d, "config.json"), "{}")
    _touch(os.path.join(d, "model.safetensors.index.json"),
           json.dumps({"weight_map": {f"w{i}": s for i, s in enumerate(shards)}}))
    for s in shards:
        _touch(os.path.join(d, s), b"w" * 16)
    if marker:
        _touch(os.path.join(d, vc.MARK), json.dumps({"v": 2, "repo": "x", "sha256": {}}))
    return d


def _make_trainer(root):
    _touch(os.path.join(root, "app", "main.py"), "")
    return root


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vlm_test_")
        self.launcher_models = os.path.join(self.tmp, "launcher", "vlm_models")
        p = mock.patch.object(vc, "LAUNCHER_MODELS", self.launcher_models)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ============================================================
# 提示词 / 拼接 / 拒答（和训练器一致）
# ============================================================
class PromptTest(unittest.TestCase):
    def test_toriigate_uses_its_own_format_and_tags(self):
        system, user = vc.build_prompt("toriigate", {"torii_format": "short", "use_names": False}, ["1girl", "smile"])
        self.assertEqual(system, vc.TORII_SYSTEM)
        self.assertIn(vc.TORII_FORMATS["short"], user)
        self.assertIn("[1girl, smile]", user)
        self.assertIn("Avoid to guess names", user)

    def test_other_models_use_preset_and_reference_tags(self):
        system, user = vc.build_prompt("qwen3vl4b", {"preset": "moss"}, ["1girl"])
        self.assertEqual(system, vc.DEFAULT_SYSTEM)
        self.assertTrue(user.startswith(vc.PROMPT_PRESETS["moss"]))
        self.assertIn("Reference tags (may contain mistakes): 1girl", user)
        _, user = vc.build_prompt("qwen3vl4b", {"preset": "short"})
        self.assertEqual(user, vc.PROMPT_PRESETS["short"])

    def test_custom_prompt_and_empty_custom_falls_back(self):
        self.assertEqual(vc.user_prompt({"preset": "custom", "prompt": " my words "}), "my words")
        self.assertEqual(vc.user_prompt({"preset": "custom", "prompt": ""}), vc.PROMPT_PRESETS["detailed"])
        self.assertEqual(vc.user_prompt({"preset": "nope"}), vc.PROMPT_PRESETS["detailed"])

    def test_every_preset_has_a_label(self):
        keys = {k for k, _ in vc.PROMPT_LABELS}
        self.assertEqual(keys - {"custom"}, set(vc.PROMPT_PRESETS))
        self.assertIn(vc.DEFAULT_MODEL, vc.VLM_MODELS)
        self.assertEqual(vc.DEFAULT_MODEL, "toriigate")

    def test_clean_and_compose(self):
        self.assertEqual(vc.clean_text("**Caption:** A *cat*\n\non a mat."), "A cat on a mat.")
        self.assertEqual(vc.clean_text("<think>hmm</think>\nA dog."), "A dog.")
        self.assertEqual(vc.compose("", "A cat."), "A cat.")
        self.assertEqual(vc.compose("1girl, smile", "A girl smiles."), "1girl, smile. A girl smiles.")
        self.assertEqual(vc.compose("1girl", ""), "1girl")


class RefusalTest(unittest.TestCase):
    def test_refusals(self):
        self.assertTrue(vc.refusal_reason("I'm sorry, but I can't help with that."))
        self.assertTrue(vc.refusal_reason("抱歉，我无法描述这张图片。"))
        self.assertTrue(vc.refusal_reason("Here is a general description without explicit details: a woman."))
        self.assertTrue(vc.refusal_reason("", "content_filter"))
        self.assertTrue(vc.refusal_reason("ok", None, "policy"))
        self.assertTrue(vc.refusal_reason("   "))

    def test_normal_text_and_quoted_text_are_not_refusals(self):
        self.assertEqual(vc.refusal_reason("A girl with long hair stands in a field."), "")
        self.assertEqual(vc.refusal_reason('A sign reads "I\'m sorry, I can\'t help" in red letters.'), "")


# ============================================================
# 模型放在哪：两边互相找
# ============================================================
class LocateTest(Tmp):
    def test_found_in_trainer_or_launcher(self):
        trainer = _make_trainer(os.path.join(self.tmp, "trainer"))
        name = vc.model_dirname(vc.VLM_MODELS["toriigate"]["repo"])
        self.assertEqual(vc.find_model("toriigate", trainer), ("", ""))
        _model_dir(os.path.join(self.launcher_models, name))
        d, where = vc.find_model("toriigate", trainer)
        self.assertEqual(where, "launcher")
        _model_dir(os.path.join(trainer, "models", "taggers", name))
        d, where = vc.find_model("toriigate", trainer)
        self.assertEqual((d, where), (os.path.join(trainer, "models", "taggers", name), "trainer"))

    def test_not_a_trainer_dir_is_ignored(self):
        fake = os.path.join(self.tmp, "not_trainer")
        name = vc.model_dirname(vc.VLM_MODELS["qwen3vl4b"]["repo"])
        _model_dir(os.path.join(fake, "models", "taggers", name))
        self.assertEqual(vc.find_model("qwen3vl4b", fake), ("", ""))
        self.assertEqual(vc.trainer_models_dir(fake), "")

    def test_download_goes_to_trainer_if_installed_and_resumes_partial(self):
        trainer = _make_trainer(os.path.join(self.tmp, "trainer"))
        name = vc.model_dirname(vc.VLM_MODELS["joycaption"]["repo"])
        self.assertEqual(vc.download_dir("joycaption", trainer), os.path.join(trainer, "models", "taggers", name))
        self.assertEqual(vc.download_dir("joycaption", ""), os.path.join(self.launcher_models, name))
        # 启动器这边已经下了一半：接着在启动器这边下
        _touch(os.path.join(self.launcher_models, name, "config.json"), "{}")
        self.assertEqual(vc.download_dir("joycaption", trainer), os.path.join(self.launcher_models, name))

    def test_is_complete(self):
        d = os.path.join(self.tmp, "m")
        self.assertFalse(vc.is_complete(d))
        _model_dir(d, marker=False)
        self.assertTrue(vc.is_complete(d))   # 训练器用 huggingface_hub 下的：没有标记，分片齐了就算
        os.remove(os.path.join(d, "model-00002-of-00002.safetensors"))
        self.assertFalse(vc.is_complete(d))
        d2 = _model_dir(os.path.join(self.tmp, "m2"))
        self.assertTrue(vc.is_complete(d2))
        _touch(os.path.join(d2, "model-00001-of-00002.safetensors.part"))
        self.assertFalse(vc.is_complete(d2))   # 还有下了一半的文件
        d3 = os.path.join(self.tmp, "m3")
        _touch(os.path.join(d3, "config.json"), "{}")
        _touch(os.path.join(d3, "model.safetensors"))
        self.assertTrue(vc.is_complete(d3))

    def test_model_list_reports_where(self):
        trainer = _make_trainer(os.path.join(self.tmp, "trainer"))
        _model_dir(os.path.join(trainer, "models", "taggers", vc.model_dirname(vc.VLM_MODELS["toriigate"]["repo"])))
        rows = {r["key"]: r for r in vc.model_list(trainer)}
        self.assertTrue(rows["toriigate"]["downloaded"])
        self.assertEqual(rows["toriigate"]["where"], "trainer")
        self.assertFalse(rows["qwen3vl8b"]["downloaded"])


# ============================================================
# 下载
# ============================================================
class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise IOError(f"HTTP {self.status_code}")

    def json(self):
        return self.data


class DownloadTest(Tmp):
    def setUp(self):
        super().setUp()
        self.files = {"config.json": b'{"a": 1}', "model.safetensors": b"W" * 1000, "README.md": b"readme",
                      "pytorch_model.bin": b"old", ".gitattributes": b"x"}
        sha = lambda b: hashlib.sha256(b).hexdigest()
        self.listing = [{"type": "file", "path": p, "size": len(b),
                         **({"lfs": {"oid": sha(b)}} if p.endswith((".safetensors", ".bin")) else {})}
                        for p, b in self.files.items()]
        self.calls = []

    def fetch_ok(self, bad_hosts=()):
        def fetch(url, dest, progress, log, cancel, name):
            self.calls.append(url)
            data = self.files[url.rsplit("/", 1)[-1]]
            if any(h in url for h in bad_hosts):
                data = b"X" * len(data)   # 线路把文件弄坏了：大小对、哈希不对
            with open(dest, "wb") as f:
                f.write(data)
            if progress:
                progress(name, len(data), len(data))
        return fetch

    def test_filters_and_writes_trainer_marker(self):
        local = os.path.join(self.tmp, "repo")
        prog = []
        vc.download_repo("a/b", local, progress_cb=lambda d, t, n: prog.append((d, t)),
                         fetch_one=self.fetch_ok(), get=lambda url: _Resp(self.listing))
        self.assertEqual(sorted(os.listdir(local)), sorted(["config.json", "model.safetensors", vc.MARK]))
        with open(os.path.join(local, vc.MARK), encoding="utf-8") as f:
            mark = json.load(f)
        self.assertEqual(mark["v"], 2)   # 训练器 downloader.is_verified 要 v >= 2 才认
        self.assertEqual(mark["sha256"]["model.safetensors"], hashlib.sha256(self.files["model.safetensors"]).hexdigest())
        self.assertTrue(vc.is_complete(local))
        self.assertEqual(prog[-1][0], prog[-1][1])
        # 再下一次：都对上了，不再下载
        self.calls.clear()
        vc.download_repo("a/b", local, fetch_one=self.fetch_ok(), get=lambda url: _Resp(self.listing))
        self.assertEqual(self.calls, [])

    def test_bad_sha_switches_source(self):
        local = os.path.join(self.tmp, "repo")
        with mock.patch.object(vc, "source_order", return_value=["hf-mirror", "modelscope", "hf"]):
            vc.download_repo("a/b", local, fetch_one=self.fetch_ok(bad_hosts=("hf-mirror",)), get=lambda url: _Resp(self.listing))
        big = [u for u in self.calls if u.endswith("model.safetensors")]
        self.assertIn("hf-mirror.com", big[0])
        self.assertIn("modelscope.cn", big[1])
        with open(os.path.join(local, "model.safetensors"), "rb") as f:
            self.assertEqual(f.read(), self.files["model.safetensors"])

    def test_listing_falls_back_to_modelscope_format(self):
        ms = {"Data": {"Files": [{"Path": "config.json", "Size": 8, "Sha256": "", "Type": "blob"},
                                 {"Path": "sub", "Type": "tree"}]}}

        def get(url):
            if "hf-mirror" in url:
                raise IOError("down")
            return _Resp(ms)
        files = vc.list_repo_files("a/b", ["hf-mirror", "modelscope", "hf"], get=get)
        self.assertEqual(files, [("config.json", 8, "")])

    def test_all_sources_fail(self):
        def fetch(*a):
            raise IOError("reset")
        with mock.patch.object(vc.time, "sleep"):
            with self.assertRaises(vc.CaptionError) as cm:
                vc.download_repo("a/b", os.path.join(self.tmp, "r"), fetch_one=fetch, get=lambda url: _Resp(self.listing))
        self.assertIn("所有下载源都试过了", str(cm.exception))

    def test_cancel(self):
        with self.assertRaises(vc.CaptionError):
            vc.download_repo("a/b", os.path.join(self.tmp, "r"), cancel_flag=lambda: True,
                             fetch_one=self.fetch_ok(), get=lambda url: _Resp(self.listing))


# ============================================================
# 云端 API
# ============================================================
class ApiTest(Tmp):
    def setUp(self):
        super().setUp()
        self.img = os.path.join(self.tmp, "a.jpg")
        _touch(self.img, b"\xff\xd8jpeg")
        self.api = {"base_url": "https://x/v1", "model": "m", "api_key": "k"}

    def test_ok_and_request_shape(self):
        seen = {}

        def post(api, body, timeout):
            seen.update(body)
            return {"choices": [{"message": {"content": "<think>x</think>A cat."}, "finish_reason": "stop"}]}
        self.assertEqual(vc.api_call(self.api, self.img, "describe", 300, post=post), "A cat.")
        url = seen["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(seen["max_tokens"], 300)

    def test_refusal_and_blocked(self):
        post = lambda a, b, t: {"choices": [{"message": {"content": "I'm sorry, I can't describe this image."}}]}
        with self.assertRaises(vc.Refused):
            vc.api_call(self.api, self.img, "d", 100, post=post)

        def blocked(a, b, t):
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error": "PROHIBITED_CONTENT safety"}'))
        with self.assertRaises(vc.Refused):
            vc.api_call(self.api, self.img, "d", 100, post=blocked)

    def test_error_text_and_test_api(self):
        def unauthorized(a, b, t):
            raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"bad key"))
        r = vc.test_api(self.api, post=unauthorized)
        self.assertFalse(r["ok"])
        self.assertIn("API Key 不对", r["message"])
        self.assertTrue(vc.test_api(self.api, post=lambda a, b, t: {"choices": []})["ok"])
        self.assertFalse(vc.test_api({"base_url": "", "model": ""})["ok"])


# ============================================================
# 运行环境：训练器优先
# ============================================================
class EnvTest(Tmp):
    def setUp(self):
        super().setUp()
        self.trainer = os.path.join(self.tmp, "trainer")
        self.tpy = os.path.join(self.trainer, "env", "Scripts", "python.exe")
        self.vpy = os.path.join(self.tmp, "vlm_venv", "Scripts", "python.exe")
        p = mock.patch.object(vv, "venv_python_path", return_value=self.vpy)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def install(py, pkgs=vv.NEED):
        _touch(py)
        for n in pkgs:
            _touch(os.path.join(os.path.dirname(os.path.dirname(py)), "Lib", "site-packages", n, "__init__.py"))

    def test_trainer_env_first(self):
        self.install(self.tpy)
        self.install(self.vpy)
        self.assertEqual(vv.pick(self.trainer), ("trainer", self.tpy))

    def test_falls_back_to_launcher_venv_when_trainer_env_broken_or_gone(self):
        self.install(self.tpy, ("torch", "PIL"))   # 训练环境缺 transformers / bitsandbytes
        self.assertEqual(vv.pick(self.trainer), ("", ""))
        self.install(self.vpy)
        self.assertEqual(vv.pick(self.trainer), ("launcher", self.vpy))
        shutil.rmtree(self.trainer)   # 训练器删了
        self.assertEqual(vv.pick(self.trainer), ("launcher", self.vpy))
        self.assertEqual(vv.pick(""), ("launcher", self.vpy))

    def test_site_packages_linux_layout(self):
        py = os.path.join(self.tmp, "lin", "bin", "python")
        _touch(py)
        sp = os.path.join(self.tmp, "lin", "lib", "python3.12", "site-packages")
        os.makedirs(sp)
        self.assertEqual(vv.site_packages(py), sp)

    def test_import_ok_runs_check(self):
        class R:
            returncode = 3
        self.assertFalse(vv.import_ok(self.vpy, runner=lambda cmd, **kw: R()))
        R.returncode = 0
        self.assertTrue(vv.import_ok(self.vpy, runner=lambda cmd, **kw: R()))

    def test_status_does_not_run_python(self):
        _touch(self.tpy)
        s = vv.status(self.trainer)
        self.assertEqual(s["source"], "trainer")
        self.assertEqual(s["trainer_env"], self.tpy)

    def test_ensure_uses_existing_without_installing(self):
        with mock.patch.object(vv, "pick", return_value=("trainer", self.tpy)), \
                mock.patch.object(vv, "_install_torch") as it:
            self.assertEqual(vv.ensure(self.trainer, {}), ("trainer", self.tpy))
            it.assert_not_called()


# ============================================================
# 真的模型进程：启动器没了要跟着退出（不加载模型，不需要 torch）
# ============================================================
class WorkerExitTest(unittest.TestCase):
    WORKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "vlm_caption_worker.py")

    def spawn(self, parent_pid):
        import subprocess
        env = dict(os.environ, WWY_PARENT_PID=str(parent_pid))
        p = subprocess.Popen([sys.executable, "-u", self.WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, env=env)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.stdin.close(), p.stdout.close()))
        self.assertIn(b'"hello"', p.stdout.readline())
        return p

    def test_exits_when_launcher_process_ends(self):
        import subprocess
        fake_launcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
        p = self.spawn(fake_launcher.pid)
        fake_launcher.wait()
        p.wait(10)   # stdin 还开着，没读到命令也要退出

    def test_quit_and_eof(self):
        p = self.spawn(os.getpid())
        p.stdin.write(b'{"cmd": "quit"}\n')
        p.stdin.flush()
        self.assertEqual(p.wait(10), 0)
        p2 = self.spawn(os.getpid())
        p2.stdin.close()   # 管道断了：跟着退出
        p2.wait(10)


# ============================================================
# 服务：假的模型进程
# ============================================================
FAKE_WORKER = r'''
import json, sys, time, os
def emit(**k):
    print("@@" + json.dumps(k), flush=True)
emit(ev="hello", pid=os.getpid())
for line in sys.stdin:
    c = json.loads(line)
    if c["cmd"] == "quit":
        break
    if c["cmd"] == "load":
        if "BADMODEL" in c["dir"]:
            emit(ev="load_failed", error="CUDA out of memory")
        else:
            print("loading " + c["key"], flush=True)
            emit(ev="loaded", quant="4bit" if c["quant"] == "auto" else c["quant"], bs=2)
    elif c["cmd"] == "run":
        cancelled = False
        for it in c["items"]:
            if os.path.exists(c["cancel_file"]):
                cancelled = True
                break
            if "slow" in it["file"]:
                time.sleep(0.5)
            if "broken" in it["file"]:
                emit(ev="item", file=it["file"], error="读图失败")
            else:
                emit(ev="item", file=it["file"], text="Caption: A picture. [" + it["user"][:40].replace("\n", " ") + "]")
        emit(ev="done", cancelled=cancelled)
'''


class ServiceTest(Tmp):
    def setUp(self):
        super().setUp()
        self.trainer = _make_trainer(os.path.join(self.tmp, "trainer"))
        self.mdir = _model_dir(os.path.join(self.trainer, "models", "taggers",
                                            vc.model_dirname(vc.VLM_MODELS["toriigate"]["repo"])))
        self.worker = os.path.join(self.tmp, "fake_worker.py")
        _touch(self.worker, FAKE_WORKER)
        self.events = []
        self.cond = threading.Condition()
        self.gpu = [None]
        p = mock.patch.object(vv, "ensure", return_value=("trainer", sys.executable))
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(vv, "pick", return_value=("trainer", sys.executable))
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(vcs, "WATCH_SEC", 3600)   # 测试里手动调 check_idle
        p3.start()
        self.addCleanup(p3.stop)
        self.svc = vcs.CaptionService(self.emit, lambda: {}, lambda: self.trainer, gpu_util=lambda: self.gpu[0],
                                      worker_cmd=lambda py: [sys.executable, "-u", self.worker])
        self.addCleanup(lambda: (setattr(self.svc, "running", False), self.svc.release("cleanup")))
        self.imgs = []
        for n in ("a.png", "b.png", "broken.png"):
            p = os.path.join(self.tmp, "imgs", n)
            _touch(p, b"img")
            self.imgs.append(p)

    def emit(self, scope, event, **kw):
        self.assertEqual(scope, "caption")
        with self.cond:
            self.events.append((event, kw))
            self.cond.notify_all()

    def wait_for(self, event, timeout=20):
        end = time.time() + timeout
        with self.cond:
            while True:
                for e, kw in self.events:
                    if e == event:
                        return kw
                left = end - time.time()
                if left <= 0:
                    self.fail(f"没等到 {event}：{self.events}")
                self.cond.wait(left)

    def of(self, event):
        return [kw for e, kw in self.events if e == event]

    def prepare(self):
        self.assertTrue(self.svc.prepare("toriigate")["ok"])
        return self.wait_for("ready")

    def test_prepare_uses_trainer_model_and_runs(self):
        ready = self.prepare()
        self.assertEqual((ready["key"], ready["quant"], ready["source"], ready["dir"]), ("toriigate", "4bit", "trainer", self.mdir))
        self.assertTrue(any("训练器" in kw["text"] for kw in self.of("prep_log")))
        # 已经加载了同一个模型：直接就绪，不重启进程
        pid = self.svc.proc.pid
        self.svc.prepare("toriigate")
        self.assertEqual(self.svc.proc.pid, pid)

        tags = {self.imgs[0]: "1girl, smile"}
        r = self.svc.run(self.imgs, {"source": "local", "tags": tags, "feed_tags": True, "keep_tags": True,
                                     "save_txt": True, "prompt": {"torii_format": "long"}})
        self.assertTrue(r["ok"])
        done = self.wait_for("done")
        self.assertEqual((done["failed"], done["total"], done["cancelled"]), (1, 3, False))
        items = {kw["path"]: kw for kw in self.of("item")}
        a = items[self.imgs[0]]
        self.assertTrue(a["caption"].startswith("1girl, smile. A picture."))   # 标签. 句子
        self.assertIn("Captioning format", a["text"])   # ToriiGate 的提示词传进了模型进程
        self.assertTrue(a["txt"])
        with open(os.path.splitext(self.imgs[0])[0] + ".txt", encoding="utf-8") as f:
            self.assertEqual(f.read(), a["caption"])
        self.assertFalse(items[self.imgs[1]]["caption"].startswith("1girl"))
        self.assertIn("读图失败", items[self.imgs[2]]["error"])
        self.assertFalse(self.svc.running)

    def test_cancel_stops_between_items(self):
        self.prepare()
        slow = []
        for k in range(4):
            p = os.path.join(self.tmp, "imgs", f"slow_{k}.png")
            _touch(p, b"img")
            slow.append(p)
        self.svc.run(slow, {"source": "local", "prompt": {}})
        self.wait_for("item")
        self.svc.cancel_all()
        done = self.wait_for("done")
        self.assertTrue(done["cancelled"])
        self.assertLess(len(self.of("item")), 4)
        self.assertFalse(os.path.exists(self.svc.cancel_file))
        # 取消之后还能接着用
        self.events.clear()
        self.svc.run(slow[:1], {"source": "local", "prompt": {}})
        self.assertFalse(self.wait_for("done")["cancelled"])

    def test_run_requires_loaded_model(self):
        r = self.svc.run(self.imgs, {"source": "local"})
        self.assertFalse(r["ok"])
        self.assertIn("加载模型", r["error"])

    def test_missing_model_without_download(self):
        self.svc.preparing = True
        with self.assertRaises(vc.CaptionError):
            self.svc._load("qwen3vl8b", "auto", download=False)

    def test_load_failure_reports_vram_hint(self):
        bad = _model_dir(os.path.join(self.tmp, "BADMODEL"))
        with mock.patch.object(vc, "find_model", return_value=(bad, "launcher")):
            self.svc.prepare("toriigate")
            kw = self.wait_for("prep_failed")
        self.assertIn("显存不够", kw["msg"])
        self.assertIsNone(self.svc.loaded)
        self.assertFalse(self.svc.preparing)

    def test_release_and_auto_release(self):
        self.prepare()
        proc = self.svc.proc
        # 闲置太久
        self.assertEqual(self.svc.check_idle(now=time.time() + vcs.IDLE_SEC + 1), "idle")
        proc.wait(10)
        self.assertIsNone(self.svc.loaded)
        self.assertTrue(any("闲置" in kw["reason"] for kw in self.of("released")))

        # 在跑图：显卡持续很忙
        self.events.clear()
        self.prepare()
        self.gpu[0] = 60                                     # 平时浏览器、视频也会占一些，不算
        for _ in range(vcs.GPU_BUSY_HITS + 2):
            self.assertEqual(self.svc.check_idle(), "")
        self.gpu[0] = 95
        for _ in range(vcs.GPU_BUSY_HITS - 1):
            self.assertEqual(self.svc.check_idle(), "")      # 短暂的忙不算
        self.assertEqual(self.svc.check_idle(), "gpu")
        self.assertIsNone(self.svc.loaded)
        self.assertTrue(any("跑图" in kw["reason"] for kw in self.of("released")))

        # 正在反推时不释放
        self.events.clear()
        self.gpu[0] = 95
        self.prepare()
        self.svc.running = True
        self.assertEqual(self.svc.check_idle(now=time.time() + vcs.IDLE_SEC * 2), "")
        self.assertEqual(self.svc.check_idle(), "")
        self.assertFalse(self.svc.release()["ok"])
        self.svc.running = False
        self.assertTrue(self.svc.release()["ok"])
        self.assertIsNone(self.svc.loaded)

    def test_gpu_unknown_only_idle(self):
        self.prepare()
        self.gpu[0] = None
        for _ in range(5):
            self.assertEqual(self.svc.check_idle(), "")
        self.assertIsNotNone(self.svc.loaded)

    def test_api_run_with_refusal_and_fatal_error(self):
        def fake_call(api, path, prompt, max_tokens):
            if path.endswith("b.png"):
                raise vc.Refused("模型拒答：I'm sorry")
            return "A photo. " + ("[tags]" if "Reference tags" in prompt else "")
        api = {"base_url": "https://x/v1", "model": "m", "api_key": "k", "concurrency": 2, "fallback_local": False}
        with mock.patch.object(vc, "api_call", side_effect=fake_call):
            r = self.svc.run(self.imgs[:2], {"source": "api", "api": api, "tags": {self.imgs[0]: "cat"}, "feed_tags": True,
                                             "keep_tags": False})
            self.assertTrue(r["ok"])
            done = self.wait_for("done")
        self.assertEqual(done["failed"], 1)
        items = {kw["path"]: kw for kw in self.of("item")}
        self.assertEqual(items[self.imgs[0]]["caption"], "A photo. [tags]")   # 关了保留标签：只有句子
        self.assertTrue(items[self.imgs[1]]["refused"])

        # Key 不对：整批停下来
        self.events.clear()

        def unauthorized(*a):
            raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"bad key"))
        with mock.patch.object(vc, "api_call", side_effect=unauthorized):
            self.svc.run(self.imgs[:2], {"source": "api", "api": dict(api, concurrency=1)})
            kw = self.wait_for("failed")
        self.assertIn("API Key 不对", kw["msg"])

    def test_api_refusal_falls_back_to_downloaded_local_model(self):
        api = {"base_url": "https://x/v1", "model": "m", "concurrency": 1, "fallback_local": True}
        with mock.patch.object(vc, "api_call", side_effect=vc.Refused("模型拒答")):
            self.svc.run(self.imgs[:2], {"source": "api", "api": api, "prompt": {}})
            done = self.wait_for("done", 30)
        self.assertEqual(done["failed"], 0)
        self.assertEqual(self.of("ready")[0]["key"], "toriigate")   # 自动加载了已经下好的本地模型
        self.assertEqual(len([kw for kw in self.of("item") if not kw.get("error")]), 2)

    def test_api_requires_address(self):
        r = self.svc.run(self.imgs, {"source": "api", "api": {"base_url": "", "model": ""}})
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
