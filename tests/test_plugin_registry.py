import io
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import plugin_registry as pr  # noqa: E402


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


PYPROJECT = '[project]\nname = "ComfyUI-Foo"\nversion = "{v}"\n\n[tool.comfy]\nPublisherId = "me"\n'


class CnrTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmp, "custom_nodes", "comfyui-foo")
        os.makedirs(os.path.dirname(self.dest))

    def read(self, *parts):
        with open(os.path.join(self.dest, *parts), encoding="utf-8") as f:
            return f.read()

    def test_install_then_switch_keeps_user_files(self):
        z1 = make_zip({"pyproject.toml": PYPROJECT.format(v="1.0.0"), "__init__.py": "v1",
                       "old_only.py": "x", "web/a.js": "1"})
        pr.install_zip(z1, self.dest)
        c = pr.read_cnr(self.dest)
        self.assertEqual(c["id"], "comfyui-foo")
        self.assertEqual(c["version"], "1.0.0")
        self.assertTrue(c["tracked"])
        # 用户自己放的文件
        with open(os.path.join(self.dest, "my_config.json"), "w") as f:
            f.write("{}")
        z2 = make_zip({"pyproject.toml": PYPROJECT.format(v="1.2.0"), "__init__.py": "v2", "web/a.js": "2"})
        backup = os.path.join(self.tmp, "bk")
        pr.install_zip(z2, self.dest, backup_dir=backup)
        self.assertEqual(pr.read_cnr(self.dest)["version"], "1.2.0")
        self.assertEqual(self.read("__init__.py"), "v2")
        self.assertEqual(self.read("my_config.json"), "{}")
        self.assertFalse(os.path.exists(os.path.join(self.dest, "old_only.py")))
        self.assertTrue(os.path.isfile(os.path.join(backup, "comfyui-foo", "old_only.py")))
        self.assertFalse([n for n in os.listdir(os.path.dirname(self.dest)) if n.startswith(".wwy-stage")])

    def test_zip_slip_ignored(self):
        z = make_zip({"../evil.py": "x", "ok.py": "y", "pyproject.toml": PYPROJECT.format(v="1")})
        pr.install_zip(z, self.dest)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "custom_nodes", "evil.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "ok.py")))

    def test_git_or_plain_folder_not_cnr(self):
        os.makedirs(self.dest)
        self.assertIsNone(pr.read_cnr(self.dest))
        with open(os.path.join(self.dest, "pyproject.toml"), "w") as f:
            f.write('[project]\nname = "x"\nversion = "1"\n')
        self.assertIsNone(pr.read_cnr(self.dest))          # 没有 .tracking 也没有 [tool.comfy]
        self.assertIsNone(pr.read_cnr(self.dest, is_git=True))


class MiscTest(unittest.TestCase):
    def test_versions(self):
        self.assertTrue(pr.is_newer("1.10.0", "1.9.3"))
        self.assertFalse(pr.is_newer("1.5.0", "1.5.0"))
        self.assertTrue(pr.is_newer("1.0.1", ""))

    def test_registry_versions_sorted(self):
        data = [{"version": "1.9.0", "downloadUrl": "a"}, {"version": "1.10.0", "downloadUrl": "b"},
                {"version": "1.11.0", "downloadUrl": "c", "deprecated": True}]
        vs = pr.registry_versions(lambda u: data, "x")
        self.assertEqual([v["version"] for v in vs], ["1.11.0", "1.10.0", "1.9.0"])
        self.assertEqual(pr.registry_download_url(lambda u: data, "x"), ("b", "1.10.0"))
        self.assertEqual(pr.registry_download_url(lambda u: data, "x", "1.9.0"), ("a", "1.9.0"))

    def test_git_url(self):
        self.assertEqual(pr.normalize_git_url("https://github.com/a/b/tree/main/x"), "https://github.com/a/b")
        self.assertEqual(pr.normalize_git_url(" https://github.com/a/b.git "), "https://github.com/a/b")
        self.assertIsNone(pr.normalize_git_url("adetailer"))
        self.assertEqual(pr.repo_folder("https://gitee.com/a/My-Ext.git"), "My-Ext")

    def test_search(self):
        items = [{"name": "sd-webui-controlnet", "desc": "ControlNet", "tags": [], "url": "u1"},
                 {"name": "adetailer", "desc": "auto detect face", "tags": ["face"], "url": "u2"},
                 {"name": "x", "desc": "works with adetailer", "tags": [], "url": "u3"}]
        self.assertEqual([i["name"] for i in pr.search_items(items, "adetailer")], ["adetailer", "x"])
        self.assertEqual(pr.search_items(items, "face")[0]["name"], "adetailer")


if __name__ == "__main__":
    unittest.main()
