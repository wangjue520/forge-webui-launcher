"""
整合包（app/pack_recipe.py、app/pack_manager.py）的测试：配方文本往返、pip 对齐计划、官方节点库识别、
压缩包打包 / 解压、导出时插件分类、按配方还原插件（git 用本地仓库代替 GitHub，不联网）。
运行：python -m unittest tests.test_pack
"""
import os as _os, sys as _sys  # app_dir_for_tests：被测模块在 app/ 子目录里
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "app"))
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

import pack_recipe as pr       # noqa: E402
import pack_manager as pm      # noqa: E402
import version_manager as vm   # noqa: E402

GIT = shutil.which("git")


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class RecipeTextTest(unittest.TestCase):
    def sample(self):
        r = pr.new_recipe("comfyui", "我的 ComfyUI")
        r["head"].update(python="3.13.11", torch="2.9.1+cu130", gpu="RTX 3090")
        r["main"] = {"url": "https://github.com/Comfy-Org/ComfyUI.git", "branch": "master",
                     "commit": "a" * 40, "label": "master @ aaaaaaa"}
        r["plugins"] = [
            {"folder": "rgthree-comfy", "git": "https://github.com/rgthree/rgthree-comfy.git",
             "commit": "b" * 40, "branch": "main"},
            {"folder": "comfyui-kjnodes", "cnr": "comfyui-kjnodes", "version": "1.5.0", "disabled": True},
            {"folder": "本地节点", "zip": "plugins/本地节点", "note": "没有 git 地址"},
            {"folder": "x.py", "zip": "plugins/x.py", "note": "单文件节点"},
        ]
        r["pip"] = {"": {"numpy": "numpy==1.26.4", "torch": "torch==2.9.1+cu130",
                         "swarm": "swarm @ git+https://github.com/openai/swarm.git@6af0b4c"}}
        r["pip_skipped"] = {"": ["-e git+https://x#egg=y"]}
        r["models"] = [{"key": "checkpoints", "name": "sub/模型 A.safetensors", "size": 123, "sha256": "ab" * 32,
                        "civitai": "42", "hf": "Comfy-Org/x:split/a.safetensors"}]
        return r

    def test_round_trip(self):
        r = self.sample()
        back = pr.parse(pr.dump(r))
        self.assertEqual(back["head"]["kind"], "comfyui")
        self.assertEqual(back["head"]["name"], "我的 ComfyUI")
        self.assertEqual(back["head"]["torch"], "2.9.1+cu130")
        self.assertEqual(back["main"]["commit"], "a" * 40)
        self.assertEqual([p["folder"] for p in back["plugins"]], ["rgthree-comfy", "comfyui-kjnodes", "本地节点", "x.py"])
        self.assertTrue(back["plugins"][1]["disabled"])
        self.assertNotIn("disabled", back["plugins"][0])
        self.assertEqual(back["plugins"][2]["zip"], "plugins/本地节点")
        self.assertEqual(back["pip"][""]["swarm"], "swarm @ git+https://github.com/openai/swarm.git@6af0b4c")
        self.assertEqual(back["models"][0]["name"], "sub/模型 A.safetensors")
        self.assertEqual(back["models"][0]["size"], 123)
        self.assertEqual(back["models"][0]["hf"], "Comfy-Org/x:split/a.safetensors")

    def test_trainer_envs_and_engines(self):
        r = pr.new_recipe("lora_trainer")
        r["engines"] = [{"name": "sd-scripts", "git": "https://github.com/kohya-ss/sd-scripts", "commit": "c" * 40},
                        {"name": "DiffSynth-Studio", "git": "https://github.com/modelscope/DiffSynth-Studio",
                         "commit": "d" * 40, "extra": "diffsynth"}]
        r["pip"] = {"": {"torch": "torch==2.11.0+cu128"}, "envs/diffsynth": {"peft": "peft==0.17.0"}}
        back = pr.parse(pr.dump(r))
        self.assertEqual(back["engines"][1]["extra"], "diffsynth")
        self.assertEqual(back["pip"]["envs/diffsynth"], {"peft": "peft==0.17.0"})

    def test_rejects_unknown_and_bad_rows(self):
        with self.assertRaises(pr.RecipeError):
            pr.parse("随便一个文本文件\n")
        with self.assertRaises(pr.RecipeError):
            pr.parse("[整合包]\n程序 = comfyui\n格式 = 99\n")
        r = pr.parse("[整合包]\n程序 = neo2\n[插件]\n../evil | git=https://x\nok | git=https://y\n"
                     "[模型]\ncheckpoints/../../x.safetensors | size=1\ncheckpoints/a.safetensors | size=abc\n")
        self.assertEqual([p["folder"] for p in r["plugins"]], ["ok"])   # 带路径的文件夹名丢掉
        self.assertEqual([m["name"] for m in r["models"]], ["a.safetensors"])
        self.assertEqual(r["models"][0]["size"], 0)

    def test_hand_edited_gbk_file(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "a.txt")
            with open(p, "wb") as f:
                f.write("[整合包]\n程序 = classic\n名称 = 手写\n".encode("gbk"))
            rec, z = pr.read_source(p)
            self.assertEqual(rec["head"]["name"], "手写")
            self.assertIsNone(z)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class PipTest(unittest.TestCase):
    def test_parse_freeze(self):
        pkgs, skipped = pr.parse_freeze(
            "Numpy==1.26.4\n-e git+https://github.com/x/y@abc#egg=y\nfoo @ file:///C:/w/foo.whl\n"
            "bar @ git+https://github.com/a/bar@123\nweird line\nTyping_Extensions==4.12\n")
        self.assertEqual(pkgs, {"numpy": "Numpy==1.26.4", "bar": "bar @ git+https://github.com/a/bar@123",
                                "typing-extensions": "Typing_Extensions==4.12"})
        self.assertEqual(len(skipped), 3)
        # 启动器从自己的 wheel 缓存装的 torch：文件名里取版本号，按 == 记
        pkgs, skipped = pr.parse_freeze(
            "torch @ file:///F:/web%20ui%E5%90%AF/cache/wheels/torch-2.13.0%2Bcu130-cp313-cp313-win_amd64.whl#sha256=cf\n"
            "torchvision @ file:///F:/x/torchvision-0.28.0%2Bcu130-cp313-cp313-win_amd64.whl\n"
            "mylib @ file:///D:/src/mylib\n")
        self.assertEqual(pkgs, {"torch": "torch==2.13.0+cu130", "torchvision": "torchvision==0.28.0+cu130"})
        self.assertEqual(skipped, ["mylib @ file:///D:/src/mylib"])

    def test_plan(self):
        rec = {"numpy": "numpy==1.26.4", "pillow": "pillow==11.0.0", "torch": "torch==2.8.0+cu128",
               "xformers": "xformers==0.0.32", "pip": "pip==25.0", "new": "new==1.0",
               "bar": "bar @ git+https://github.com/a/bar@123", "same": "same==2.0"}
        have = {"numpy": "numpy==2.1.0", "pillow": "Pillow==11.0.0", "torch": "torch==2.9.1+cu130",
                "xformers": "xformers==0.0.32", "pip": "pip==24.0", "bar": "bar==0.1", "same": "same==2.0"}
        self.assertEqual(pr.pip_plan(rec, have), ["bar @ git+https://github.com/a/bar@123", "new==1.0", "numpy==1.26.4"])

    def test_torch_helpers(self):
        self.assertEqual(pr.torch_tag("2.8.0+cu128"), "cu128")
        self.assertEqual(pr.torch_tag("2.8.0"), "")
        self.assertEqual(pr.base_version("2.8.0+cu128"), "2.8.0")
        self.assertEqual(pr.torch_specs({"torch": "torch==2.8.0+cu128", "torchvision": "torchvision==0.23.0+cu128",
                                         "torchaudio": "torchaudio @ https://x"}),
                         {"torch": "2.8.0+cu128", "torchvision": "0.23.0+cu128"})


class CnrAndZipTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_read_cnr(self):
        n = os.path.join(self.d, "comfyui-kjnodes")
        os.makedirs(n)
        with open(os.path.join(n, "pyproject.toml"), "w", encoding="utf-8") as f:
            f.write('[project]\nname = "ComfyUI-KJNodes"\ndescription = "x"\nversion = "1.5.0"\n'
                    '[tool.comfy]\nPublisherId = "kijai"\nversion = "9.9"\n')
        self.assertIsNone(pr.read_cnr(n))   # 没有 .tracking = 不是节点库装的
        open(os.path.join(n, ".tracking"), "w").close()
        self.assertEqual(pr.read_cnr(n), ("comfyui-kjnodes", "1.5.0"))

    def test_add_tree_and_extract(self):
        src = os.path.join(self.d, "node")
        os.makedirs(os.path.join(src, "sub"))
        os.makedirs(os.path.join(src, "__pycache__"))
        os.makedirs(os.path.join(src, ".git"))
        for rel, size in (("a.py", 10), ("sub/b.txt", 5), ("__pycache__/c.pyc", 3), (".git/HEAD", 3), ("big.bin", 2000)):
            with open(os.path.join(src, *rel.split("/")), "wb") as f:
                f.write(b"x" * size)
        single = os.path.join(self.d, "one.py")
        with open(single, "w") as f:
            f.write("print(1)")
        zp = os.path.join(self.d, "p.zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr(pr.RECIPE_NAME, "[整合包]\n程序 = comfyui\n")
            n, total, big = pr.add_tree(z, src, "plugins/node", max_file=1000)
            self.assertEqual((n, total), (2, 15))
            self.assertEqual(big, [("big.bin", 2000)])
            pr.add_tree(z, src, "plugins/ov", files=["sub/b.txt"])
            pr.add_tree(z, single, "plugins/one.py")
            z.writestr("plugins/evil/../../escape.txt", "no")
        out = os.path.join(self.d, "out", "node")
        self.assertEqual(pr.extract_prefix(zp, "plugins/node", out), 2)
        self.assertTrue(os.path.isfile(os.path.join(out, "sub", "b.txt")))
        self.assertEqual(pr.extract_prefix(zp, "plugins/one.py", os.path.join(self.d, "out", "one.py")), 1)
        pr.extract_prefix(zp, "plugins/evil", os.path.join(self.d, "out", "evil"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "escape.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "out", "escape.txt")))
        rec, zpath = pr.read_source(zp)
        self.assertEqual(zpath, zp)
        rec["plugins"] = [{"folder": "node", "zip": "plugins/node"}, {"folder": "gone", "zip": "plugins/gone"},
                          {"folder": "g", "git": "https://x", "overlay": "plugins/ov"}]
        s = pr.summary(rec, zp)
        self.assertEqual(s["plugins"], {"git": 1, "cnr": 0, "zip": 1, "missing": 1})
        self.assertEqual(s["missing"], ["gone"])


@unittest.skipUnless(GIT, "需要 git")
class FlowTest(unittest.TestCase):
    """导出时插件分类 + 按配方还原插件：用本地仓库当「远程」，不联网"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        # 「远程」仓库
        self.upstream = os.path.join(self.d, "upstream")
        os.makedirs(self.upstream)
        _git(self.upstream, "init", "-q", "-b", "main")
        with open(os.path.join(self.upstream, "__init__.py"), "w") as f:
            f.write("v = 1\n")
        _git(self.upstream, "add", ".")
        _git(self.upstream, "commit", "-q", "-m", "one")
        self.c1 = subprocess.run(["git", "-C", self.upstream, "rev-parse", "HEAD"], capture_output=True,
                                 text=True).stdout.strip()
        with open(os.path.join(self.upstream, "__init__.py"), "w") as f:
            f.write("v = 2\n")
        _git(self.upstream, "commit", "-q", "-am", "two")
        # 插件目录：一个钉在第一个提交的 git 插件（还改了文件）、一个本地提交的、一个没有 git 的、一个空文件夹
        self.nodes = os.path.join(self.d, "custom_nodes")
        os.makedirs(self.nodes)
        a = os.path.join(self.nodes, "pinned")
        subprocess.run(["git", "clone", "-q", self.upstream, a], check=True, capture_output=True)
        _git(a, "checkout", "-q", self.c1)
        with open(os.path.join(a, "__init__.py"), "w") as f:
            f.write("v = 'local edit'\n")
        b = os.path.join(self.nodes, "localcommit")
        subprocess.run(["git", "clone", "-q", self.upstream, b], check=True, capture_output=True)
        with open(os.path.join(b, "x.py"), "w") as f:
            f.write("x\n")
        _git(b, "add", ".")
        _git(b, "commit", "-q", "-m", "local only")
        c = os.path.join(self.nodes, "plain.disabled")
        os.makedirs(c)
        with open(os.path.join(c, "__init__.py"), "w") as f:
            f.write("plain\n")
        os.makedirs(os.path.join(self.nodes, "empty"))
        with open(os.path.join(self.nodes, "single.py"), "w") as f:
            f.write("single\n")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_export_then_apply(self):
        logs = []
        ctx = {"kind": "comfyui", "plugins_dir": self.nodes, "comfy_dir": "", "root": self.d, "cfg": {}}
        api = object()
        with mock.patch.object(pm, "_wa") as wa:
            wa.return_value._gh_sources.return_value = [""]
            plugins, packs = pm._export_plugins(api, ctx, "git", None, logs.append, False, lambda: False,
                                                lambda *a: None)
        by = {p["folder"]: p for p in plugins}
        self.assertEqual(sorted(by), ["localcommit", "pinned", "plain", "single.py"])
        self.assertEqual(by["pinned"]["commit"], self.c1)
        self.assertEqual(by["pinned"]["overlay"], "plugins/pinned")
        self.assertEqual(by["localcommit"]["zip"], "plugins/localcommit")
        self.assertTrue(by["plain"]["disabled"])
        self.assertEqual(by["plain"]["zip"], "plugins/plain")
        self.assertEqual(by["single.py"]["zip"], "plugins/single.py")

        zp = os.path.join(self.d, "pack.zip")
        rec = pr.new_recipe("comfyui")
        rec["plugins"] = plugins
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr(pr.RECIPE_NAME, pr.dump(rec))
            for arc, src, files in packs:
                pr.add_tree(z, src, arc, files)
        rec, zpath = pr.read_source(zp)

        # 还原到新目录
        target = os.path.join(self.d, "new_nodes")
        os.makedirs(target)

        class C:
            kind = "comfyui"
            plugins_dir = target
            comfy_dir = ""
            py = ""
            root = self.d
            env = None
            sources = [""]
            mirrors = []
            git = "git"

            def log(self, t):
                logs.append(t)

            def run(self, program, args, cwd, env=None, timeout=None):
                return subprocess.run([program] + list(args), cwd=cwd, capture_output=True).returncode

            def check(self):
                pass

            def cancelled(self):
                return False
        c = C()
        report = {}
        with mock.patch.object(pm, "_wa") as wa:
            wa.return_value._DeployCancelled = RuntimeError
            # 「远程」换成本地仓库路径
            for p in rec["plugins"]:
                if p.get("git"):
                    p["git"] = self.upstream
            with mock.patch.object(vm, "rank_sources", return_value=[""]):
                pm._apply_plugins(c, rec["plugins"], zpath, report)
        self.assertEqual(report["plugins_failed"], [], logs)
        with open(os.path.join(target, "pinned", "__init__.py")) as f:
            self.assertEqual(f.read(), "v = 'local edit'\n")      # 钉在第一个提交 + 本地改动覆盖上去
        self.assertEqual(vm.read_head(os.path.join(target, "pinned"))[1], self.c1)
        self.assertTrue(os.path.isfile(os.path.join(target, "localcommit", "x.py")))   # 从压缩包还原
        self.assertTrue(os.path.isfile(os.path.join(target, "plain.disabled", "__init__.py")))   # 停用照旧
        self.assertTrue(os.path.isfile(os.path.join(target, "single.py")))

        # 再按同一个整合包部署到同一个目录：停用的插件不能变成 xxx.disabled.disabled，也不能多出一份启用的
        for p in rec["plugins"]:
            if p["folder"] == "localcommit":
                p["disabled"] = True
        report = {}
        with mock.patch.object(pm, "_wa") as wa:
            wa.return_value._DeployCancelled = RuntimeError
            with mock.patch.object(vm, "rank_sources", return_value=[""]):
                pm._apply_plugins(c, rec["plugins"], zpath, report)   # localcommit：启用 → 停用
                pm._apply_plugins(c, rec["plugins"], zpath, report)   # 两边都已经是停用的
        self.assertEqual(report["plugins_failed"], [], logs)
        names = sorted(os.listdir(target))
        self.assertEqual(names, ["localcommit.disabled", "pinned", "plain.disabled", "single.py"])


class PipBisectTest(unittest.TestCase):
    def test_finds_only_the_broken_ones(self):
        specs = [f"p{i}==1" for i in range(300)]
        broken = {"p7==1", "p250==1"}
        calls = []

        def fake_batch(c, py, group, sources=None):
            calls.append(len(group))
            return 1 if broken & set(group) else 0

        c = mock.Mock(mirrors=[["-i", "m1"], ["-i", "m2"]])
        with mock.patch.object(pm, "_pip_batch", side_effect=fake_batch), \
                mock.patch.object(pm, "_pip", side_effect=lambda c, py, args, sources=None: 1 if args[0] in broken else 0):
            bad = pm._pip_bisect(c, "py", specs)
        self.assertEqual(sorted(bad), sorted(broken))
        self.assertLess(len(calls), 40)   # 不是 300 次一个个装


class StartGuardTest(unittest.TestCase):
    def test_refuses_while_deploying(self):
        api = mock.Mock()
        api._deploy_running = True
        api._deploy_pack = "keep"
        r = pm.start(api, "x.txt", "D:/x", {})
        self.assertFalse(r["ok"])
        self.assertEqual(api._deploy_pack, "keep")   # 正在跑的部署的整合包不能被清掉

    def test_after_deploy_noop_without_pack(self):
        api = mock.Mock()
        api._deploy_pack = None
        pm.after_deploy(api, "comfyui", "D:/x", lambda t: None, {})   # 不报错、什么都不做
        api._emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
