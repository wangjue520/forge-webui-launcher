"""
amd_rocm.py — AMD 显卡（ROCm / TheRock）环境支持

做法照 CS1o 的教程（Forge Neo README 里给 A 卡用户指的就是它）：
  https://github.com/CS1o/Stable-Diffusion-Info/wiki/Webui-Installation-Guides#amd-forge-neo-with-rocm
  1. 卸掉 CUDA 版 torch（以及残留的 rocm 包）
  2. 从 AMD 的 wheel 源按显卡架构装 torch：
       pip install --index-url https://stable.repo.amd.com/rocm/whl-next/
           "torch[device-gfx1100]" "torchvision[device-gfx1100]" torchaudio "rocm[devel]"
  3. rocm-sdk init
  4. 环境变量 TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1、FLASH_ATTENTION_TRITON_AMD_ENABLE=TRUE
  5. Forge 参数 --cuda-stream --use-pytorch-cross-attention --disable-smart-memory --pin-shared-memory；
     ComfyUI 参数 --use-pytorch-cross-attention --bf16-vae --disable-smart-memory
ROCm 版 torch 也走 torch.cuda 这套接口（HIP），所以 Forge / ComfyUI 本身不用改。

这里只放纯逻辑（型号表、命令、参数），真正执行命令在 webview_api 里。
"""

import json
import os
import re
import subprocess

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

ROCM_INDEX = "https://stable.repo.amd.com/rocm/whl-next/"
ROCM_NIGHTLY_INDEX = "https://nightly.repo.amd.com/rocm/whl-next/"

# (gfx 架构, 显示名, 是否实验性) —— 顺序就是下拉框顺序；
# 实验性 = 教程说「支持」但没给安装命令的架构，装不上时提示用户
GFX_TARGETS = [
    ("gfx1201", "RX 9070 / 9070 XT、AI PRO R9700", False),
    ("gfx1200", "RX 9060 / 9060 XT", False),
    ("gfx1100", "RX 7900 XTX / 7900 XT / 7900 GRE、PRO W7900 / W7800", False),
    ("gfx1101", "RX 7800 XT / 7700 XT、PRO W7700", False),
    ("gfx1102", "RX 7600 / 7600 XT / 7650 GRE / 7700S", False),
    ("gfx1030", "RX 6950 XT / 6900 XT / 6800 XT / 6800、PRO W6800", False),
    ("gfx1031", "RX 6750 XT / 6700 XT / 6700、6800M", False),
    ("gfx1032", "RX 6650 XT / 6600 XT / 6600、6800S / 6700S", False),
    ("gfx1034", "RX 6500 XT / 6400", False),
    ("gfx1035", "Radeon 680M / 660M 核显", False),
    ("gfx1010", "RX 5700 / 5700 XT / 5600", False),
    ("gfx1151", "Ryzen AI Max（Radeon 8060S / 8050S）", True),
    ("gfx1103", "Radeon 780M / 760M 核显", True),
]
GFX_IDS = [g for g, _l, _e in GFX_TARGETS]

# 型号 → 架构。顺序有讲究：7700S 是 gfx1102（不是 7700 的 1101），
# 6800S/6700S 是 gfx1032、6800M 是 gfx1031，所以带后缀的先判断
_GFX_RULES = [
    (r"\b9070\b|R9700|R9600", "gfx1201"),
    (r"\b9060\b", "gfx1200"),
    (r"\b7700S\b|\b7600\b|\b7650\b|\b7400\b|W7600|W7500", "gfx1102"),
    (r"\b7900\b|W7900|W7800", "gfx1100"),
    (r"\b7800\b|\b7700\b|W7700|V710", "gfx1101"),
    (r"\b6800S\b|\b6700S\b|\b66[05]0\b|W6600", "gfx1032"),
    (r"\b6800M\b|\b67[05]0\b", "gfx1031"),
    (r"\b69[05]0\b|\b6800\b|W6800|V620", "gfx1030"),
    (r"\b6500\b|\b6400\b", "gfx1034"),
    (r"\b680M\b|\b660M\b", "gfx1035"),
    (r"\b5700\b|\b5600\b", "gfx1010"),
    (r"80[456]0S|Ryzen AI Max", "gfx1151"),
    (r"\b7[468]0M\b", "gfx1103"),
]

ROCM_ENV = {
    "TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL": "1",
    "FLASH_ATTENTION_TRITON_AMD_ENABLE": "TRUE",
}
FORGE_ARGS = ["--cuda-stream", "--use-pytorch-cross-attention", "--disable-smart-memory",
              "--pin-shared-memory"]
COMFY_ARGS = ["--use-pytorch-cross-attention", "--bf16-vae", "--disable-smart-memory"]
# 只对 N 卡有意义（或在 A 卡上直接报错）的参数：ROCm 环境启动时剔掉
NVIDIA_ONLY_ARGS = {"--xformers", "--sage", "--flash", "--cuda-malloc", "--pynvml",
                    "--use-sage-attention", "--use-flash-attention", "--use-ck-attention"}

# 切换环境时要卸掉的包：torch 三件套 + CUDA 专用的 xformers；rocm* 另外按已装列表找
UNINSTALL_BASE = ["torch", "torchvision", "torchaudio", "xformers"]


def is_rocm(cfg):
    return (cfg or {}).get("gpu_backend") == "rocm"


def gfx_label(gfx):
    for g, label, exp in GFX_TARGETS:
        if g == gfx:
            return f"{label}（{g}{'，实验性' if exp else ''}）"
    return gfx or "未选择"


def guess_gfx(name):
    """显卡名 → gfx 架构；认不出返回 None"""
    for pat, gfx in _GFX_RULES:
        if re.search(pat, name or "", re.IGNORECASE):
            return gfx
    return None


def detect_amd_gpus():
    """本机的 AMD 显卡 [{name, gfx}]（Windows 用 WMI 查；查不到返回空列表）"""
    if os.name != "nt":
        return []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=_NO_WINDOW)
        names = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    except Exception:
        return []
    out = []
    for n in names:
        if re.search(r"\bAMD\b|Radeon", n, re.IGNORECASE):
            out.append({"name": n, "gfx": guess_gfx(n)})
    # 独显排前面：核显（Radeon(TM) Graphics / 7x0M）通常不是要用的那块
    out.sort(key=lambda g: g["gfx"] in (None, "gfx1035", "gfx1103"))
    return out


# 网络抖动时 pip 默认只重试 5 次、15 秒超时，几 GB 的包很容易半途放弃；放宽
PIP_NET_ARGS = ["--retries", "10", "--timeout", "120"]


def root_requirements(gfx):
    """要装的顶层包（照教程）"""
    return [f"torch[device-{gfx}]", f"torchvision[device-{gfx}]", "torchaudio", "rocm[devel]"]


def pip_common_args(nightly=False, cache_dir=None):
    """
    AMD 源专用的 pip 参数。注意只用 --index-url 指向 AMD 源，绝不加国内镜像当
    --extra-index-url：pip 会在所有源里挑版本号最高的，PyPI 上的 torch（CPU 版）
    版本号比 AMD 的新，加了就会装成 CPU 版。--isolated 同理：忽略用户 pip.ini /
    环境变量里可能配着的 extra-index-url（缓存目录因此要显式传）。
    """
    args = ["--isolated", "--index-url", ROCM_NIGHTLY_INDEX if nightly else ROCM_INDEX]
    if nightly:
        args.append("--pre")
    if cache_dir:
        args += ["--cache-dir", cache_dir]
    return args + PIP_NET_ARGS


def pip_install_args(gfx, nightly=False, cache_dir=None):
    """python -m pip 后面的参数（列表形式，给启动器自己跑命令用）"""
    return ["install"] + pip_common_args(nightly, cache_dir) + root_requirements(gfx)


def rocm_sdk_exe(python_exe):
    """python 对应的 rocm-sdk.exe：venv 里和 python.exe 同目录，便携/系统 Python 在 Scripts 下"""
    d = os.path.dirname((python_exe or "").strip('"'))
    for c in (os.path.join(d, "rocm-sdk.exe"), os.path.join(d, "Scripts", "rocm-sdk.exe"),
              os.path.join(d, "rocm-sdk"), os.path.join(d, "bin", "rocm-sdk")):
        if os.path.isfile(c):
            return c
    return os.path.join(d, "Scripts", "rocm-sdk.exe")


def forge_torch_command(gfx, scripts_dir):
    """
    给 Forge 的 TORCH_COMMAND：Forge 执行的是 `"<python>" -m <TORCH_COMMAND>`（shell=True），
    所以开头必须是 pip；装完紧接着 rocm-sdk init（scripts_dir 是 rocm-sdk.exe 所在目录）。
    """
    pkgs = " ".join(f'"{p}"' if "[" in p else p for p in root_requirements(gfx))
    cmd = f"pip install {' '.join(pip_common_args())} {pkgs}"
    if scripts_dir:
        cmd += f' && "{os.path.join(scripts_dir, "rocm-sdk.exe")}" init'
    return cmd


def apply_args(args, extra):
    """args（列表）里剔掉 N 卡专用参数，再补上 extra 里还没有的（保持顺序、去重）"""
    out = []
    for a in args:
        if a in NVIDIA_ONLY_ARGS:
            continue
        out.append(a)
    for a in extra:
        if a not in out:
            out.append(a)
    return out


def apply_args_str(arg_str, extra):
    """COMMANDLINE_ARGS 字符串版：按空白切开逐个处理（带引号的路径参数原样保留）"""
    parts = re.findall(r'"[^"]*"|\S+', arg_str or "")
    return " ".join(apply_args(parts, extra))


# ------------------------------------------------------------ 环境探测 ----

_PROBE = ("import json,torch;print(json.dumps({'v':torch.__version__,"
          "'hip':getattr(torch.version,'hip',None),'cuda':torch.version.cuda,"
          "'ok':bool(torch.cuda.is_available()),"
          "'name':(torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')}))")


def probe_torch(python_exe, env=None, timeout=180):
    """
    {'installed': bool, 'backend': 'rocm'/'cuda'/'cpu'/None, 'version', 'ok', 'name', 'error'}
    import torch 要几秒，ROCm 首次还会更久，超时给宽一点
    """
    try:
        r = subprocess.run([python_exe, "-c", _PROBE], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except Exception as e:
        return {"installed": False, "backend": None, "error": str(e)}
    line = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not line:
        err = (r.stderr or "").strip().splitlines()
        no_torch = any("No module named 'torch'" in x for x in err)
        return {"installed": not no_torch, "backend": None,
                "error": err[-1] if err else f"退出码 {r.returncode}"}
    try:
        d = json.loads(line[-1])
    except ValueError:
        return {"installed": True, "backend": None, "error": line[-1]}
    backend = "rocm" if d.get("hip") or "rocm" in str(d.get("v", "")).lower() else (
        "cuda" if d.get("cuda") else "cpu")
    return {"installed": True, "backend": backend, "version": d.get("v"), "ok": d.get("ok"),
            "name": d.get("name") or "", "error": ""}


def installed_rocm_packages(python_exe, env=None):
    """已装的 rocm* 包名（切换时一起卸掉，免得新旧版本混装）"""
    try:
        r = subprocess.run([python_exe, "-m", "pip", "list", "--format=json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=120, creationflags=_NO_WINDOW)
        pkgs = json.loads(r.stdout or "[]")
    except Exception:
        return []
    return [p["name"] for p in pkgs
            if str(p.get("name", "")).lower().startswith(("rocm", "_rocm"))]


# ------------------------------------------------------------ 预下载 ----
#
# pip 下载没有断点续传：rocm 的运行库 / 开发包动辄一两 GB，国内连 AMD 源一抖就从头再来。
# 所以启动器先自己读 AMD 源的索引，把 torch / rocm 这一族大包的 wheel 算出来，用
# portable_env 的断点续传下载到本地（sha256 校验），再让 pip 从本地文件安装；
# numpy 这类小依赖仍由 pip 从 AMD 源装。任何一步失败都退回普通 pip 安装，不比原来差。

FAMILY_PREFIXES = ("torch", "rocm", "_rocm", "triton")


class PlanError(Exception):
    pass


def _packaging():
    try:
        from packaging.requirements import Requirement
        from packaging.specifiers import SpecifierSet
        from packaging.utils import canonicalize_name, parse_wheel_filename
    except ImportError:   # 启动器环境里一定有 pip，借它自带的 packaging
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.specifiers import SpecifierSet
        from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
    return Requirement, SpecifierSet, canonicalize_name, parse_wheel_filename


def _in_family(name):
    n = name.lower().replace("_", "-")
    return n.startswith(tuple(p.replace("_", "-") for p in FAMILY_PREFIXES)) or n.startswith("-rocm")


class _HttpRangeFile:
    """只读、可 seek 的远程文件（HTTP Range）：给 zipfile 用来只读 wheel 里的 METADATA"""

    TAIL = 256 * 1024   # zip 的目录和 dist-info 都在文件末尾：一次取尾部 256KB，通常一两个请求就够

    def __init__(self, session, url, size):
        self.s, self.url, self.size, self.pos = session, url, size, 0
        self._cache = {}
        self._tail_start = max(0, size - self.TAIL)
        self._tail = None

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if not n:
            return b""
        if self.pos >= self._tail_start:
            if self._tail is None:
                self._tail = self._get(self._tail_start, self.size - 1)
            off = self.pos - self._tail_start
            data = self._tail[off:off + n]
            self.pos += len(data)
            return data
        key = (self.pos, n)
        if key not in self._cache:
            self._cache[key] = self._get(self.pos, self.pos + n - 1)
        data = self._cache[key]
        self.pos += len(data)
        return data

    def _get(self, a, b):
        r = self.s.get(self.url, headers={"Range": f"bytes={a}-{b}"}, timeout=60)
        if r.status_code != 206:
            raise PlanError(f"服务器不支持分段读取（HTTP {r.status_code}）")
        return r.content


class Planner:
    """解析 AMD 源（PEP 503/691 简单索引），算出 torch / rocm 一族要装的 wheel"""

    def __init__(self, index, pytag, prerelease=False, log=None, session=None):
        import requests
        self.index = index.rstrip("/") + "/"
        self.pytag = pytag                       # cp313 之类
        self.pre = prerelease
        self.log = log or (lambda m: None)
        self.s = session or requests.Session()
        self.s.headers["User-Agent"] = "WWYLauncher/1.0"
        self._links = {}
        (self.Requirement, self.SpecifierSet,
         self.canon, self.parse_wheel) = _packaging()
        ver = pytag[2:]
        self.env = {"python_version": f"{ver[0]}.{ver[1:]}", "python_full_version": f"{ver[0]}.{ver[1:]}.0",
                    "sys_platform": "win32", "platform_system": "Windows", "platform_machine": "AMD64",
                    "os_name": "nt", "implementation_name": "cpython",
                    "platform_python_implementation": "CPython", "extra": ""}

    # ---- 索引 ----
    def links(self, name):
        name = self.canon(name)
        if name in self._links:
            return self._links[name]
        from urllib.parse import urljoin, urldefrag
        url = self.index + name + "/"
        r = self.s.get(url, timeout=60, headers={
            "Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.1"})
        if r.status_code == 404:
            self._links[name] = []
            return []
        r.raise_for_status()
        out = []
        if "json" in (r.headers.get("content-type") or ""):
            for f in r.json().get("files", []):
                meta = f.get("core-metadata") or f.get("dist-info-metadata")
                out.append({"filename": f["filename"], "url": urljoin(r.url, f["url"]),
                            "sha256": (f.get("hashes") or {}).get("sha256"), "meta": bool(meta)})
        else:
            for m in re.finditer(r"<a\s+([^>]*)>([^<]+)</a>", r.text, re.IGNORECASE):
                attrs, text = m.group(1), m.group(2).strip()
                href = re.search(r'href="([^"]+)"', attrs)
                if not href:
                    continue
                full, frag = urldefrag(urljoin(r.url, href.group(1).replace("&amp;", "&")))
                sha = frag.split("=", 1)[1] if frag.startswith("sha256=") else None
                meta = "data-core-metadata" in attrs or "data-dist-info-metadata" in attrs
                out.append({"filename": text, "url": full, "sha256": sha, "meta": meta})
        self._links[name] = out
        return out

    def _compatible(self, link):
        fn = link["filename"]
        if not fn.endswith(".whl"):
            return None
        try:
            _n, ver, _b, tags = self.parse_wheel(fn)
        except Exception:
            return None
        ok = any(t.interpreter in (self.pytag, "py3", "py" + self.pytag[2:]) and
                 t.abi in (self.pytag, "abi3", "none") and t.platform in ("win_amd64", "any")
                 for t in tags)
        return ver if ok else None

    def best(self, name, spec):
        cands = []
        for link in self.links(name):
            ver = self._compatible(link)
            if ver is None or not spec.contains(ver, prereleases=True):
                continue
            cands.append((ver, link))
        if not cands:
            return None
        final = [c for c in cands if not c[0].is_prerelease]
        pool = cands if (self.pre or not final) else final
        pool.sort(key=lambda c: c[0], reverse=True)
        return pool[0]

    # ---- 元数据 ----
    def metadata(self, link):
        if link.get("meta"):
            r = self.s.get(link["url"] + ".metadata", timeout=60)
            if r.ok:
                return r.text
        import zipfile
        h = self.s.head(link["url"], timeout=60, allow_redirects=True)
        size = int(h.headers.get("content-length") or 0)
        if not size:
            raise PlanError(f"拿不到 {link['filename']} 的大小")
        f = _HttpRangeFile(self.s, h.url, size)
        with zipfile.ZipFile(f) as z:
            name = next((n for n in z.namelist()
                         if n.endswith(".dist-info/METADATA") and n.count("/") == 1), None)
            if not name:
                raise PlanError(f"{link['filename']} 里没有 METADATA")
            return z.read(name).decode("utf-8", "replace")

    def requires(self, text):
        return [line.split(":", 1)[1].strip() for line in text.splitlines()
                if line.lower().startswith("requires-dist:")]

    # ---- 解析 ----
    def plan(self, roots):
        """
        roots: ["torch[device-gfx1100]", ...] → [{name, version, filename, url, sha256}]
        只跟进 torch / rocm 一族（大包都在这里）；其余依赖交给 pip
        """
        specs, extras, chosen, done_extras = {}, {}, {}, {}
        queue = []
        for r in roots:
            req = self.Requirement(r)
            n = self.canon(req.name)
            specs[n] = specs.get(n, self.SpecifierSet("")) & req.specifier
            extras[n] = extras.get(n, set()) | set(req.extras)
            queue.append(n)
        steps = 0
        while queue:
            steps += 1
            if steps > 300:
                raise PlanError("依赖关系太复杂，放弃预下载")
            n = queue.pop(0)
            got = self.best(n, specs[n])
            if got is None:
                raise PlanError(f"AMD 源里没有适合这个 Python（{self.pytag}）的 {n}（要求 {specs[n] or '任意版本'}）")
            ver, link = got
            prev = chosen.get(n)
            if prev and prev[0] == ver and done_extras.get(n, set()) >= extras[n]:
                continue
            chosen[n] = (ver, link)
            done_extras[n] = set(extras[n])
            for line in self.requires(self.metadata(link)):
                req = self.Requirement(line)
                if req.marker is not None and not any(
                        req.marker.evaluate(dict(self.env, extra=e)) for e in ("",) + tuple(extras[n])):
                    continue
                d = self.canon(req.name)
                if not _in_family(d):
                    continue
                new_spec = specs.get(d, self.SpecifierSet("")) & req.specifier
                new_ex = extras.get(d, set()) | set(req.extras)
                if d not in chosen or str(new_spec) != str(specs.get(d)) or new_ex != extras.get(d):
                    specs[d], extras[d] = new_spec, new_ex
                    if d not in queue:
                        queue.append(d)
        return [{"name": n, "version": str(v), "filename": l["filename"], "url": l["url"],
                 "sha256": l["sha256"], "extras": sorted(extras.get(n, ()))}
                for n, (v, l) in chosen.items()]


def local_install_args(plan, paths):
    """从本地 wheel 安装：顶层包带上 extras（path[extra] 写法），其余直接给路径"""
    out = []
    for item, path in zip(plan, paths):
        ex = item.get("extras") or []
        out.append(f"{path}[{','.join(ex)}]" if ex else path)
    return out
