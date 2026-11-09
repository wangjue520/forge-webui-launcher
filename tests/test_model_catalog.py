"""
模型下载页「常用模型一键下载」的测试：目录数据完整、按显卡挑默认精度、文件放哪、批量下载流程。
不联网：下载和 HF 文件列表都换成假的。运行：python -m unittest tests.test_model_catalog
"""
import os as _os, sys as _sys  # app_dir_for_tests：被测模块在 app/ 子目录里
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import model_catalog as mc     # noqa: E402
import webview_api as wa       # noqa: E402

GB = 1024 ** 3
FMTS = {"bf16", "fp16", "fp32", "fp8", "int8", "mxfp8", "nvfp4", "fp4", "w4a8", "w6a8", "gguf"}
ROLES = {None, "DiffusionModel", "Checkpoint", "TextEncoder", "VAE", "LoRA", "ControlNet"}


def ver_of(mid, vid=None):
    m = mc.find_model(mid)
    return m, (mc.find_version(m, vid) if vid else mc.default_version(m))


def picked(mid, vid, ctx, comp="main"):
    m, v = ver_of(mid, vid)
    return mc.pick_defaults(v, ctx, m.get("ram_factor", 1.0))[comp]


class CatalogDataTests(unittest.TestCase):
    def test_structure(self):
        cat = mc.load(force=True)
        group_ids = {g["id"] for g in cat["groups"]}
        ids = [m["id"] for m in cat["models"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 20)
        for m in cat["models"]:
            self.assertIn(m["group"], group_ids, m["id"])
            self.assertTrue(m["ui"], m["id"])
            self.assertEqual(sum(1 for v in m["versions"] if v.get("default")), 1, m["id"])
            vids = [v["id"] for v in m["versions"]]
            self.assertEqual(len(vids), len(set(vids)), m["id"])
            for v in m["versions"]:
                cids = [c["id"] for c in v["components"]]
                self.assertEqual(len(cids), len(set(cids)), f"{m['id']}/{v['id']}")
                self.assertTrue(any(c["required"] for c in v["components"]), f"{m['id']}/{v['id']}")
                for c in v["components"]:
                    where = f"{m['id']}/{v['id']}/{c['id']}"
                    self.assertIn(c.get("role"), ROLES, where)
                    self.assertTrue(c.get("role") or c.get("folder"), where)
                    oids = [o["id"] for o in c["options"]]
                    self.assertTrue(oids, where)
                    self.assertEqual(len(oids), len(set(oids)), where)
                    for o in c["options"]:
                        self.assertIn(o["fmt"], FMTS, where)
                        self.assertGreater(o["size"], 0, where)
                        self.assertTrue(o["file"] and "/" not in o["file"] and "\\" not in o["file"], where)
                        self.assertTrue(o["src"], where)
                        for s in o["src"]:
                            self.assertTrue(s.get("hf") and s.get("path") or s.get("civitai"), where)
                        if o["fmt"] == "gguf":
                            self.assertTrue(o["file"].endswith(".gguf"), where)

    def test_anima_bundle(self):
        """作者要的：选 Anima 一键下本体 + VAE + 文本编码器"""
        m, v = ver_of("anima")
        req = {c["id"]: c for c in v["components"] if c["required"]}
        self.assertEqual(set(req), {"main", "te", "vae"})
        self.assertEqual(req["main"]["role"], "DiffusionModel")
        self.assertEqual(req["te"]["options"][0]["file"], "qwen_3_06b_base.safetensors")
        self.assertEqual(req["vae"]["options"][0]["file"], "qwen_image_vae.safetensors")
        self.assertGreaterEqual(len(m["versions"]), 3)

    def test_same_file_same_size_everywhere(self):
        """同名文件（比如多个模型共用的 VAE / 文本编码器）大小要一致，不然一个模型下好了另一个会说「大小不对」"""
        sizes = {}
        for m in mc.load()["models"]:
            for v in m["versions"]:
                for c in v["components"]:
                    for o in c["options"]:
                        sizes.setdefault(o["file"], set()).add(o["size"])
        for name, s in sizes.items():
            self.assertLessEqual(max(s) - min(s), mc.SIZE_TOLERANCE, name)


class DefaultPickTests(unittest.TestCase):
    def test_arch_preference(self):
        # 30 系：int8 有硬件加速，fp8 没有；40 系起反过来
        self.assertEqual(picked("krea2", None, mc.make_ctx("neo", 24, 64, 8.6)), "int8")
        self.assertEqual(picked("krea2", None, mc.make_ctx("comfyui", 24, 64, 8.9)), "fp8")

    def test_fits_vram(self):
        ctx = mc.make_ctx("comfyui", 8, 32, 8.6, gguf_node=True)
        m, v = ver_of("flux1")
        oid = mc.pick_defaults(v, ctx)["main"]
        opt = next(o for o in v["components"][0]["options"] if o["id"] == oid)
        self.assertLessEqual(opt["size"], mc.main_budget(8) * GB)
        self.assertEqual(picked("anima", None, ctx), "bf16")      # 2B 模型 8GB 也直接用原版

    def test_blackwell_only_not_default(self):
        for vram in (8, 12, 24):
            ctx = mc.make_ctx("comfyui", vram, 64, 8.6)
            for m in mc.load()["models"]:
                for v in m["versions"]:
                    for cid, oid in mc.pick_defaults(v, ctx).items():
                        comp = next(c for c in v["components"] if c["id"] == cid)
                        opt = next(o for o in comp["options"] if o["id"] == oid)
                        usable = [o for o in comp["options"] if not mc.option_issue(o, ctx)]
                        if usable:
                            self.assertNotIn(opt["fmt"], mc.BLACKWELL_FMTS, f"{m['id']}/{v['id']}/{cid}")

    def test_gguf_needs_node_in_comfy(self):
        no_node = mc.make_ctx("comfyui", 8, 32, 8.6, gguf_node=False)
        self.assertNotEqual(mc.option_issue({"fmt": "gguf"}, no_node), "")
        self.assertNotIn("gguf", picked("flux1", None, no_node))
        # 9B 只有 GGUF：没装插件也只能选 GGUF（界面上会提示先装插件）
        self.assertTrue(picked("flux2_klein", "9b", no_node).startswith("gguf-"))

    def test_old_ui_cannot_use_new_quant(self):
        ctx = mc.make_ctx("classic", 12, 32, 8.6)
        self.assertNotEqual(mc.option_issue({"fmt": "int8"}, ctx), "")
        self.assertEqual(mc.option_issue({"fmt": "fp8"}, ctx), "")

    def test_ram_step_down(self):
        # 内存很小时文本编码器先降档
        big = mc.make_ctx("comfyui", 24, 128, 8.9)
        small = mc.make_ctx("comfyui", 24, 8, 8.9)
        self.assertEqual(picked("moss", None, big, "te"), "bf16")
        self.assertNotEqual(picked("moss", None, small, "te"), "bf16")

    def test_h3_follows_h3_card(self):
        for ram, q in ((64, "Q4_K"), (32, "Q2_K")):
            view = mc.public_view(mc.make_ctx("h3", 24, ram, 8.6))
            h3 = next(m for m in view["models"] if m["id"] == "minimax_h3")
            self.assertTrue(h3["supported"])
            self.assertEqual(h3["versions"][0]["defaults"]["fl2va"], f"gguf-{q}")
        # ComfyUI 上不偏向 GGUF
        view = mc.public_view(mc.make_ctx("comfyui", 24, 64, 8.9))
        h3 = next(m for m in view["models"] if m["id"] == "minimax_h3")
        self.assertFalse(h3["versions"][0]["defaults"]["fl2va"].startswith("gguf"))

    def test_support_flags(self):
        view = mc.public_view(mc.make_ctx("classic", 12, 32, 8.6))
        sup = {m["id"]: m["supported"] for m in view["models"]}
        self.assertFalse(sup["anima"])
        self.assertTrue(sup["wai"])
        self.assertTrue(sup["flux1"])
        view = mc.public_view(mc.make_ctx("neo", 12, 32, 8.6))
        wan = next(m for m in view["models"] if m["id"] == "wan22")
        by_id = {v["id"]: v["supported"] for v in wan["versions"]}
        self.assertEqual(by_id, {"i2v": True, "t2v": True, "ti2v5b": False})   # Neo 不支持 5B
        # 界面不需要下载地址 / 哈希
        self.assertNotIn("src", wan["versions"][0]["components"][0]["options"][0])

    def test_instance_kind(self):
        self.assertEqual(mc.instance_kind({"webui_branch": "comfyui"}), "comfyui")
        self.assertEqual(mc.instance_kind({"webui_branch": "neo2", "webui_variant": "h3"}), "h3")
        self.assertEqual(mc.instance_kind({"webui_branch": "classic"}), "classic")
        self.assertEqual(mc.instance_kind({"webui_branch": "neo2"}), "neo")
        self.assertEqual(mc.instance_kind({"webui_branch": "neo2", "webui_root": "x"}, lambda r: True), "a1111")

    def test_sm_from_name(self):
        self.assertEqual(mc.sm_from_name("NVIDIA GeForce RTX 3090"), 8.6)
        self.assertEqual(mc.sm_from_name("NVIDIA GeForce RTX 4060 Laptop GPU"), 8.9)
        self.assertEqual(mc.sm_from_name("NVIDIA GeForce RTX 5090"), 12.0)
        self.assertIsNone(mc.sm_from_name("AMD Radeon"))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_existing_file_tolerance(self):
        p = os.path.join(self.tmp, "flux2-vae.safetensors")
        with open(p, "wb") as f:
            f.write(b"\0" * 5000)
        self.assertEqual(mc.existing_file([self.tmp], "flux2-vae.safetensors", 5000 + 2264), (p, True))
        self.assertEqual(mc.existing_file([self.tmp], "flux2-vae.safetensors", 5000 + 2264, tolerance=0), (p, False))
        self.assertEqual(mc.existing_file([self.tmp], "flux2-vae.safetensors", 5000 + 5 * 1024 * 1024), (p, False))
        self.assertEqual(mc.existing_file([self.tmp], "nope.safetensors", 1), (None, False))

    def test_source_urls(self):
        opt = {"src": [{"civitai": 123}, {"hf": "a/b", "path": "dir/x y.safetensors"}]}
        cn = mc.source_urls(opt, ["https://hf-mirror.com", "https://huggingface.co"], use_mirror=True)
        self.assertEqual([u for _h, u, _c in cn], [
            "https://hf-mirror.com/a/b/resolve/main/dir/x%20y.safetensors",
            "https://huggingface.co/a/b/resolve/main/dir/x%20y.safetensors",
            "https://civitai.com/api/download/models/123"])
        glob = mc.source_urls(opt, ["https://huggingface.co", "https://hf-mirror.com"], use_mirror=False)
        self.assertEqual([c for _h, _u, c in glob], [False, True, False])


class RoutingTests(unittest.TestCase):
    """组件 → 放进哪个文件夹（复用 _role_dest：开共享库进库，没开按实例类型的目录名）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc_route_")
        self.lib = os.path.join(self.tmp, "lib")
        os.makedirs(os.path.join(self.lib, "checkpoints"))
        self.neo = os.path.join(self.tmp, "neo")
        os.makedirs(os.path.join(self.neo, "modules"))
        with open(os.path.join(self.neo, "modules", "cmd_args.py"), "w") as f:
            f.write('parser.add_argument("--ckpt-dirs")\nparser.add_argument("--text-encoder-dirs")\n')
        self.comfy = os.path.join(self.tmp, "comfy")
        os.makedirs(os.path.join(self.comfy, "models", "diffusion_models"))
        os.makedirs(os.path.join(self.comfy, "custom_nodes", "ComfyUI-GGUF"))
        os.makedirs(os.path.join(self.comfy, "comfy"))
        with open(os.path.join(self.comfy, "main.py"), "w") as f:
            f.write("# comfy\n")
        wa.ml._FLAG_CACHE.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def api(self, branch, root, lib_on):
        cfg = {"webui_branch": branch, "webui_root": root, "model_library_enabled": lib_on,
               "model_library_path": self.lib}
        return types.SimpleNamespace(cfg=cfg)

    def rel(self, p):
        return os.path.relpath(p, self.tmp).replace(os.sep, "/")

    def test_dirs(self):
        neo_lib = self.api("neo2", self.neo, True)
        dest, disp, look = wa._catalog_dirs(neo_lib, "DiffusionModel")
        self.assertEqual(self.rel(dest), "lib/diffusion_models")
        self.assertIn("lib/checkpoints", [self.rel(x) for x in look])     # 旧版放错的位置也算已下载
        self.assertEqual(self.rel(wa._catalog_dirs(neo_lib, "TextEncoder")[0]), "lib/text_encoders")
        self.assertEqual(self.rel(wa._catalog_dirs(self.api("neo2", self.neo, False), "DiffusionModel")[0]),
                         "neo/models/Stable-diffusion")
        self.assertEqual(self.rel(wa._catalog_dirs(self.api("neo2", self.neo, False), "TextEncoder")[0]),
                         "neo/models/text_encoder")
        comfy = self.api("comfyui", self.comfy, False)
        self.assertEqual(self.rel(wa._catalog_dirs(comfy, "DiffusionModel")[0]), "comfy/models/diffusion_models")
        self.assertEqual(self.rel(wa._catalog_dirs(comfy, "TextEncoder")[0]), "comfy/models/text_encoders")
        self.assertEqual(self.rel(wa._catalog_dirs(comfy, "Checkpoint")[0]), "comfy/models/checkpoints")
        # 库里没有的分类（model_patches）始终放实例自己的目录
        self.assertEqual(self.rel(wa._catalog_dirs(neo_lib, None, "models/model_patches")[0]),
                         "neo/models/model_patches")
        self.assertTrue(wa._comfy_has_gguf(comfy.cfg))

    def test_download_validation(self):
        a = self.api("neo2", "", False)
        a._mc_thread = None
        r = wa._api_model_catalog_download(a, [{"model": "anima", "version": "aesthetic-v1.1", "comp": "vae", "option": "bf16"}])
        self.assertFalse(r["ok"])                                          # 没实例目录也没开库
        a = self.api("neo2", self.neo, False)
        a._mc_thread = None
        r = wa._api_model_catalog_download(a, [{"model": "anima", "version": "nope", "comp": "vae", "option": "bf16"}])
        self.assertFalse(r["ok"])

    def test_batch_download(self):
        """已存在的跳过；第一条线路出错换下一条；完成后发 done"""
        events = []
        a = self.api("neo2", self.neo, True)
        a._emit = lambda scope, typ, **kw: events.append((scope, typ, kw))
        have = os.path.join(self.lib, "VAE")
        os.makedirs(have)
        with open(os.path.join(have, "qwen_image_vae.safetensors"), "wb") as f:
            f.write(b"\0" * 100)
        plan = []
        for cid in ("main", "vae"):
            m, v, c, o = mc.resolve("anima", "aesthetic-v1.1", cid, "bf16")
            dest, disp, look = wa._catalog_dirs(a, c["role"])
            plan.append({"id": cid, "label": c["label"], "name": o["file"], "size": o["size"] if cid == "main" else 100,
                         "sha": None, "srcs": o["src"], "dest": dest, "where": disp, "look": look,
                         "tolerance": mc.SIZE_TOLERANCE, "relocate": cid == "main"})
        calls = []

        def fake_download(info, dest, api_key=None, progress_cb=None, cancel_flag=None, **kw):
            calls.append(info["downloadUrl"])
            if len(calls) == 1:
                raise OSError("connection reset")
            os.makedirs(dest, exist_ok=True)
            p = os.path.join(dest, info["name"])
            with open(p, "wb") as f:
                f.write(b"x")
            progress_cb(1, 1)
            return p, None

        with mock.patch.object(wa.cd, "download_file", fake_download), \
                mock.patch.object(wa.h3m, "fetch_tree", lambda repo, bases, timeout=20: None), \
                mock.patch("mirror_manager.resolve_mode", lambda cfg, log=None: False):
            wa._batch_model_download(a, plan, "mcdl", "[模型下载]", lambda: False)
        done = [kw for s, t, kw in events if t == "done"]
        self.assertEqual(done, [{"ok": True, "downloaded": 1, "skipped": 1}])
        self.assertEqual(len(calls), 2)                                    # 第一条线路失败后换了一条
        self.assertTrue(calls[1].startswith("https://hf-mirror.com/circlestone-labs/Anima/resolve/main/"))
        self.assertTrue(os.path.isfile(os.path.join(self.lib, "diffusion_models", "anima-aesthetic-v1.1.safetensors")))
        states = [(kw["id"], kw["state"]) for s, t, kw in events if t == "item"]
        self.assertIn(("vae", "ok"), states)
        self.assertIn(("main", "ok"), states)


if __name__ == "__main__":
    unittest.main()
