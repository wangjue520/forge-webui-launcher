# -*- coding: utf-8 -*-
"""
报错诊断（AI 层）：规则认不出、或用户想深挖时，把日志片段 + 环境信息交给大模型，
让它用只读工具自己再查几步（列目录、读文件、搜代码、看 pip 包、核对模型文件、测网址），
最后用 finish 交一份诊断：原因、操作步骤、推荐的修复动作。

Key 由用户自己填（启动器设置 → 报错诊断）。两种接口格式：
  openai     DeepSeek / 通义千问 / 各种中转（/chat/completions + tools）
  anthropic  Claude（/v1/messages + tools）
推荐的修复动作只能是 diagnose.ACTIONS 里的，由 diagnose.validate_action 校验后才变成按钮，
AI 自己什么都不执行。发出去的日志和文件内容先经 diagnose.redact 脱敏。
"""
import fnmatch
import json
import os
import re
import subprocess
import time

import diagnose as dg

KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnose_knowledge.md")

PROVIDERS = {
    "deepseek": {"label": "DeepSeek（国内直连，默认）", "format": "openai", "base": "https://api.deepseek.com",
                 "model": "deepseek-v4-flash", "key_url": "https://platform.deepseek.com/api_keys"},
    "qwen": {"label": "通义千问（阿里云百炼，国内直连）", "format": "openai",
             "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",
             "key_url": "https://bailian.console.aliyun.com/?apiKey=1"},
    "claude": {"label": "Claude（Anthropic，国内需要代理）", "format": "anthropic", "base": "https://api.anthropic.com",
               "model": "claude-sonnet-5-5", "key_url": "https://console.anthropic.com/settings/keys"},
    "custom": {"label": "自定义（OpenAI 兼容接口 / 中转）", "format": "openai", "base": "", "model": "", "key_url": ""},
}
DEFAULT_PROVIDER = "deepseek"
MAX_STEPS = 8
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def provider_list():
    return [{"id": k, **v} for k, v in PROVIDERS.items()]


def resolve(cfg):
    """配置 → (format, base, model, key)；没填 Key 返回 None"""
    pid = cfg.get("diag_llm_provider") or DEFAULT_PROVIDER
    p = PROVIDERS.get(pid) or PROVIDERS[DEFAULT_PROVIDER]
    key = (cfg.get("diag_llm_api_key") or "").strip()
    base = (cfg.get("diag_llm_base_url") or "").strip() or p["base"]
    model = (cfg.get("diag_llm_model") or "").strip() or p["model"]
    fmt = cfg.get("diag_llm_format") or p["format"] if pid == "custom" else p["format"]
    if not key or not base or not model:
        return None
    return fmt, base.rstrip("/"), model, key


class LlmError(Exception):
    pass


# ============================================================
# 接口
# ============================================================

class Chat:
    """一次对话。send() 返回 (文本, [(调用id, 工具名, 参数dict)])，并自动把回复记进历史"""

    def __init__(self, fmt, base, model, key, system, tools, timeout=120, post=None):
        self.fmt, self.base, self.model, self.key = fmt, base, model, key
        self.system, self.tools, self.timeout = system, tools, timeout
        self.messages = []
        self._post = post or self._requests_post

    @staticmethod
    def _requests_post(url, headers, body, timeout):
        import requests
        r = requests.post(url, headers=headers, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = None
        return r.status_code, data, r.text[:500]

    def user(self, text):
        self.messages.append({"role": "user", "content": text})

    def tool_results(self, results):
        """results: [(调用id, 文本)]"""
        if self.fmt == "anthropic":
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": cid, "content": out} for cid, out in results]})
        else:
            for cid, out in results:
                self.messages.append({"role": "tool", "tool_call_id": cid, "content": out})

    def send(self, max_tokens=4096):
        if self.fmt == "anthropic":
            url = self.base + ("/messages" if self.base.endswith("/v1") else "/v1/messages")
            headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            body = {"model": self.model, "max_tokens": max_tokens, "system": self.system,
                    "messages": self.messages, "temperature": 0.2}
            if self.tools:
                body["tools"] = [{"name": n, "description": d, "input_schema": s} for n, d, s in self.tools]
        else:
            url = self.base + "/chat/completions"
            headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
            body = {"model": self.model, "max_tokens": max_tokens, "temperature": 0.2,
                    "messages": [{"role": "system", "content": self.system}] + self.messages}
            if self.tools:
                body["tools"] = [{"type": "function", "function": {"name": n, "description": d, "parameters": s}}
                                 for n, d, s in self.tools]
        try:
            status, data, raw = self._post(url, headers, body, self.timeout)
        except Exception as e:
            raise LlmError(_net_error_text(e)) from e
        if status != 200 or not isinstance(data, dict):
            raise LlmError(_http_error_text(status, data, raw))
        if self.fmt == "anthropic":
            blocks = data.get("content") or []
            self.messages.append({"role": "assistant", "content": blocks})
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [(b["id"], b["name"], b.get("input") or {}) for b in blocks if b.get("type") == "tool_use"]
            return text, calls
        msg = ((data.get("choices") or [{}])[0].get("message")) or {}
        keep = {k: v for k, v in msg.items() if v is not None and k in ("role", "content", "tool_calls",
                                                                          "reasoning_content")}
        keep["role"] = "assistant"
        keep.setdefault("content", "")
        self.messages.append(keep)
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": fn.get("arguments")}
            calls.append((c.get("id"), fn.get("name"), args if isinstance(args, dict) else {}))
        return msg.get("content") or "", calls


def _net_error_text(e):
    s = str(e)
    if "ProxyError" in type(e).__name__ or "proxy" in s.lower():
        return f"连不上代理：{s[:200]}"
    if "Timeout" in type(e).__name__:
        return "请求超时（模型接口没响应；Claude 在国内需要开梯子）"
    return f"连不上模型接口：{s[:200]}"


def _http_error_text(status, data, raw):
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or data.get("message") or ""
    msg = msg or raw
    if status in (401, 403):
        return f"API Key 无效或没有权限（{status}）：{msg}"
    if status == 402:
        return f"账户余额不足（402）：{msg}"
    if status == 404:
        return f"接口地址或模型名不对（404）：{msg}"
    if status == 429:
        return f"请求太频繁或额度用完（429）：{msg}"
    return f"模型接口返回错误 {status}：{msg}"


# ============================================================
# 只读工具
# ============================================================

def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


_S = {"type": "string"}
_I = {"type": "integer"}

TOOL_SPECS = [
    ("list_dir", "列出文件夹内容（名字、大小、是否文件夹）。只能看允许的目录。",
     _obj({"path": _S, "pattern": {"type": "string", "description": "可选，文件名通配符，如 *.py"}}, ["path"])),
    ("read_file", "读文本文件的一段（按行）。二进制文件和模型权重读不了。",
     _obj({"path": _S, "start_line": _I, "max_lines": {"type": "integer", "description": "默认 200，最多 400"}},
          ["path"])),
    ("search_files", "在文件夹里按正则搜索文本文件内容，返回 文件:行号:内容。默认跳过 .git / venv / models。",
     _obj({"path": _S, "pattern": _S, "glob": {"type": "string", "description": "文件名通配符，默认 *"}},
          ["path", "pattern"])),
    ("get_log", "读日志。source 取值见系统说明里的「日志来源」。",
     _obj({"source": _S, "tail": {"type": "integer", "description": "最后多少行，默认 300"},
           "grep": {"type": "string", "description": "可选，只要包含这个正则的行（带前后 3 行）"}}, ["source"])),
    ("python_packages", "查某个实例的 Python 环境里装的包版本。names 为空则列出全部。",
     _obj({"iid": _S, "names": {"type": "array", "items": _S}}, ["iid"])),
    ("check_model_file", "核对模型文件（safetensors / ckpt / gguf）是否完整。", _obj({"path": _S}, ["path"])),
    ("test_url", "从这台电脑（按启动器当前的代理设置）访问一个网址，看能不能连通、耗时多少。",
     _obj({"url": _S}, ["url"])),
    ("instance_info", "某个实例的详细信息：类型、目录、启动参数、torch、已装的节点 / 扩展。", _obj({"iid": _S}, ["iid"])),
    ("search_model", "按文件名找模型的下载地址：ComfyUI-Manager 模型表、Civitai（按名字搜，exact=文件名完全一致）、"
                     "HuggingFace 仓库（只按仓库名搜，要再用 hf_files 看仓库里的文件）。",
     _obj({"name": {"type": "string", "description": "模型文件名，如 anima_baseV10.safetensors"},
           "folder": {"type": "string", "description": "可选，ComfyUI 模型目录名：checkpoints / loras / vae / "
                                                       "diffusion_models / text_encoders / controlnet / upscale_models"}},
          ["name"])),
    ("hf_files", "列出 HuggingFace 仓库里的模型文件和直接下载地址。", _obj({"repo": {"type": "string",
                                                                       "description": "仓库名，如 Comfy-Org/x"}},
                                                                     ["repo"])),
    ("finish", "诊断结束时调用，交最终结论。",
     _obj({"summary": {"type": "string", "description": "一句话结论"},
           "cause": {"type": "string", "description": "原因分析，引用日志 / 文件里的证据"},
           "steps": {"type": "array", "items": _S, "description": "用户要做的操作步骤，具体到启动器哪个页面哪个按钮"},
           "actions": {"type": "array", "description": "推荐的一键修复（只能用动作白名单里的）",
                       "items": _obj({"action": _S, "params": {"type": "object"}, "label": _S}, ["action"])},
           "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
           "verdict": {"type": "string", "enum": ["fixed", "continue", "need_user"],
                       "description": "只在「AI 接管」模式填：fixed=已经没问题了，continue=执行这轮 actions 后重启再看，"
                                      "need_user=要用户自己动手（写在 steps 里）"}},
          ["summary", "cause", "steps"])),
]

_SKIP_DIRS = {".git", "__pycache__", "node_modules", "venv", ".venv", "models", "outputs", "output", "input"}


class Tools:
    """AI 能用的只读工具。roots 之外的路径一律拒绝（日志里原样出现过的文件也放行）"""

    def __init__(self, api, ctx, log_text=""):
        self.api, self.ctx = api, ctx
        self.roots = allowed_roots(api)
        self.mentioned = {os.path.normcase(os.path.abspath(p)) for p in
                          re.findall(r"[A-Za-z]:[\\/][^\r\n\"'<>|*?:]+\.\w{1,12}", (log_text or "").replace("\\\\", "\\"))}

    def _ok_path(self, path):
        if not path:
            return None, "缺少 path"
        p = os.path.abspath(os.path.expandvars(str(path).replace("%USERPROFILE%", os.path.expanduser("~"))))
        if any(dg._under(p, r) for r in self.roots) or os.path.normcase(p) in self.mentioned:
            return p, None
        return None, "这个路径不在允许读取的范围里（允许的目录见系统说明）"

    def call(self, name, args):
        fn = getattr(self, "t_" + (name or ""), None)
        if not fn:
            return f"没有这个工具：{name}"
        try:
            out = fn(**{k: v for k, v in (args or {}).items() if not k.startswith("_")})
        except TypeError as e:
            return f"参数不对：{e}"
        except Exception as e:
            return f"工具出错：{e}"
        return dg.redact(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))[:20000]

    def t_list_dir(self, path, pattern=""):
        p, err = self._ok_path(path)
        if err:
            return err
        if not os.path.isdir(p):
            return "不是文件夹"
        rows = []
        for name in sorted(os.listdir(p), key=str.lower):
            if pattern and not fnmatch.fnmatch(name.lower(), pattern.lower()):
                continue
            fp = os.path.join(p, name)
            if os.path.isdir(fp):
                rows.append(f"[目录] {name}")
            else:
                try:
                    rows.append(f"{name}  {dg._mb(os.path.getsize(fp))}")
                except OSError:
                    rows.append(name)
            if len(rows) >= 300:
                rows.append("……（只列前 300 项）")
                break
        return "\n".join(rows) or "（空）"

    def t_read_file(self, path, start_line=1, max_lines=200):
        p, err = self._ok_path(path)
        if err:
            return err
        if p.lower().endswith(dg.MODEL_EXTS):
            return "模型权重是二进制文件，用 check_model_file 核对"
        if os.path.normcase(os.path.basename(p)) == "launcher_config.json":
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.dumps(dg.redact_config(json.load(f)), ensure_ascii=False, indent=1)[:20000]
            except (OSError, ValueError) as e:
                return f"读不了：{e}"
        try:
            with open(p, "rb") as f:
                raw = f.read(2_000_000)
        except OSError as e:
            return f"读不了：{e}"
        if b"\x00" in raw[:4096]:
            return "二进制文件，读不了"
        lines = raw.decode("utf-8", "replace").splitlines()
        s = max(1, int(start_line or 1))
        n = min(400, max(1, int(max_lines or 200)))
        part = lines[s - 1:s - 1 + n]
        head = f"（共 {len(lines)} 行，显示 {s}-{s + len(part) - 1}）\n"
        return head + "\n".join(f"{s + i}: {l[:500]}" for i, l in enumerate(part))

    def t_search_files(self, path, pattern, glob="*"):
        p, err = self._ok_path(path)
        if err:
            return err
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"正则写错了：{e}"
        hits, files = [], 0
        walk = [(p, [], [os.path.basename(p)])] if os.path.isfile(p) else os.walk(p)
        for d, dirs, names in walk:
            if os.path.isfile(p):
                d = os.path.dirname(p)
            dirs[:] = [x for x in dirs if x.lower() not in _SKIP_DIRS]
            for n in names:
                if not fnmatch.fnmatch(n.lower(), (glob or "*").lower()) or n.lower().endswith(dg.MODEL_EXTS):
                    continue
                fp = os.path.join(d, n)
                files += 1
                if files > 3000:
                    hits.append("……（文件太多，只搜了前 3000 个）")
                    return "\n".join(hits)
                try:
                    if os.path.getsize(fp) > 2_000_000:
                        continue
                    with open(fp, "rb") as f:
                        raw = f.read()
                except OSError:
                    continue
                if b"\x00" in raw[:2048]:
                    continue
                for i, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(fp, p if os.path.isdir(p) else d)}:{i}: {line.strip()[:300]}")
                        if len(hits) >= 80:
                            hits.append("……（只显示前 80 处）")
                            return "\n".join(hits)
        return "\n".join(hits) or "没搜到"

    def t_get_log(self, source, tail=300, grep=""):
        text = log_source_text(self.api, source)
        if text is None:
            return "没有这个日志来源"
        lines = text.splitlines()
        if grep:
            try:
                rx = re.compile(grep, re.IGNORECASE)
            except re.error as e:
                return f"正则写错了：{e}"
            keep = set()
            for i, l in enumerate(lines):
                if rx.search(l):
                    keep.update(range(max(0, i - 3), min(len(lines), i + 4)))
            lines = [lines[i] for i in sorted(keep)]
        n = min(800, max(1, int(tail or 300)))
        return "\n".join(l[:600] for l in lines[-n:]) or "（空）"

    def t_python_packages(self, iid, names=None):
        py = instance_python(self.api, iid)
        if not py:
            return "找不到这个实例用的 Python"
        code = ("import json,sys,importlib.metadata as m\n"
                "names=json.loads(sys.argv[1])\n"
                "if names:\n"
                "  out={}\n"
                "  for n in names:\n"
                "    try: out[n]=m.version(n)\n"
                "    except Exception: out[n]=None\n"
                "else:\n"
                "  out=sorted('%s==%s'%(d.metadata['Name'],d.version) for d in m.distributions())\n"
                "print(json.dumps({'python':sys.version.split()[0],'packages':out}))")
        try:
            r = subprocess.run([py, "-s", "-c", code, json.dumps(list(names or []))], capture_output=True,
                               timeout=60, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            return f"运行 Python 失败：{e}"
        import config_manager as cm
        out = cm.decode_process_output(r.stdout or b"").strip()
        return out or ("失败：" + cm.decode_process_output(r.stderr or b"")[-800:])

    def t_check_model_file(self, path):
        p, err = self._ok_path(path)
        if err:
            return err
        state, why = dg.check_model_file(p)
        page = dg.civitai_page(p)
        return f"状态：{state}；{why}" + (f"；来源页面：{page}" if page else "")

    def t_test_url(self, url):
        if not re.match(r"^https?://", str(url or "")):
            return "只支持 http(s) 网址"
        import requests
        t0 = time.time()
        try:
            r = requests.get(url, timeout=10, stream=True, headers={"User-Agent": "Mozilla/5.0"})
            r.close()
            return f"HTTP {r.status_code}，耗时 {time.time() - t0:.1f} 秒"
        except Exception as e:
            return f"失败（{time.time() - t0:.1f} 秒）：{type(e).__name__}: {str(e)[:300]}"

    def t_instance_info(self, iid):
        return instance_info(self.api, iid)

    def _hf_base(self):
        try:
            import mirror_manager as mm
            return mm.HF_MIRROR if mm.resolve_mode(self.api.cfg) else "https://huggingface.co"
        except Exception:
            return "https://huggingface.co"

    def t_search_model(self, name, folder=""):
        import requests
        import civitai_downloader as cd
        stem = os.path.splitext(os.path.basename(str(name).replace("\\", "/")))[0]
        out = {}
        if self.ctx.get("comfy_dir"):
            hit = dg.model_list_lookup(dg.load_model_list(self.ctx["comfy_dir"]), name)
            if hit:
                out["ComfyUI-Manager 模型表"] = {k: hit.get(k) for k in ("name", "filename", "url", "save_path", "size",
                                                                     "base")}
        try:
            cands, _f = cd.search_models_smart(name, dg.FOLDER_ROLES.get(folder or "", ""), "",
                                               (self.api.cfg.get("civitai_api_key") or "").strip() or None, limit=5)
            out["Civitai"] = [{"模型": c.get("model_name"), "版本": c.get("version_name"), "底模": c.get("base_model"),
                               "类型": c.get("model_type"), "文件": (c.get("match_file") or {}).get("name"),
                               "大小MB": round(((c.get("match_file") or {}).get("sizeKB") or 0) / 1024),
                               "下载地址": (c.get("match_file") or {}).get("downloadUrl"), "exact": c.get("exact"),
                               "页面": f"https://civitai.com/models/{c.get('model_id')}"} for c in cands] or "没搜到"
        except Exception as e:
            out["Civitai"] = f"搜索失败：{str(e)[:200]}"
        base = self._hf_base()
        try:
            r = requests.get(f"{base}/api/models", params={"search": stem[:60], "limit": 8}, timeout=15)
            out["HuggingFace 仓库（" + base + "）"] = [m.get("id") or m.get("modelId") for m in r.json()][:8] \
                if r.status_code == 200 else f"HTTP {r.status_code}"
        except Exception as e:
            out["HuggingFace"] = f"搜索失败：{str(e)[:200]}"
        return out

    def t_hf_files(self, repo):
        import requests
        repo = str(repo or "").strip().strip("/")
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo) or any(x.strip(".") == "" for x in repo.split("/")):
            return "仓库名格式应为 作者/仓库"
        base = self._hf_base()
        try:
            r = requests.get(f"{base}/api/models/{repo}", timeout=15)
        except Exception as e:
            return f"访问失败：{str(e)[:200]}"
        if r.status_code != 200:
            return f"HTTP {r.status_code}（仓库不存在或需要登录授权）"
        files = [s.get("rfilename") for s in r.json().get("siblings") or []]
        exts = dg.MODEL_EXTS + (".onnx",)
        rows = [f"{f} → https://huggingface.co/{repo}/resolve/main/{f}" for f in files
                if f and f.lower().endswith(exts)]
        return "\n".join(rows[:150]) or f"仓库里没有模型文件（共 {len(files)} 个文件）"

    def t_finish(self, **kw):
        return "ok"


def allowed_roots(api):
    import config_manager as cm
    roots = [dg.APP_DIR]
    for inst in api.cfg.get("instances") or []:
        r = (inst.get("webui_root") or "").strip()
        if r:
            roots.append(r)
            if cm.is_comfy(inst):
                cd = cm.comfy_layout(r)[0]
                if cd:
                    roots.append(cd)
    for k in ("model_library_path", "trainer_root"):
        v = (api.cfg.get(k) or "").strip()
        if v:
            roots.append(v)
    return [r for r in dict.fromkeys(roots) if r and os.path.isdir(r)]


def log_sources(api):
    out = {"launcher": "启动器各页面的任务日志 + 内部错误（合在一起）", "launcher_file": "启动器进程的 launcher.log"}
    for inst in api.cfg.get("instances") or []:
        out[f"instance:{inst.get('id')}"] = f"实例「{inst.get('name') or inst.get('id')}」的运行日志"
    for k in dg.LOGS.keys():
        if not k.startswith("inst:"):
            out[k] = f"{k} 页面 / 任务的日志"
    return out


def log_source_text(api, source):
    source = str(source or "")
    if source.startswith("instance:"):
        return dg.LOGS.tail(dg.inst_key(source.split(":", 1)[1]))
    if source == "launcher_file":
        return dg.read_launcher_log()
    if source == "launcher":
        return launcher_text(api)
    if source in dg.LOGS.keys():
        return dg.LOGS.tail(source)
    return None


def launcher_text(api):
    """启动器自己的诊断材料：各任务日志（部署 / 插件 / 下载 / 版本……）+ 内部错误"""
    parts = []
    for k in dg.LOGS.keys():
        if k.startswith("inst:"):
            continue
        t = dg.LOGS.tail(k, 400)
        if t.strip():
            parts.append(f"===== {k} =====\n{t}")
    return "\n".join(parts)


def instance_python(api, iid):
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
    return ((cfg.get("custom_python_path") or "").strip() or cm.detect_bundled_python(root) or "").strip('"')


def instance_info(api, iid):
    import config_manager as cm
    import version_manager as vm
    cfg = cm.instance_cfg(api.cfg, iid)
    if cfg is None:
        return "实例不存在"
    ctx = dg.instance_ctx(api, iid)
    root = ctx["root"]
    r = api._runners.get(iid) if hasattr(api, "_runners") else None
    info = {"id": iid, "名字": ctx["name"], "类型": cm.kind_label(cfg.get("webui_branch"), cfg.get("webui_variant")),
            "webui_branch": cfg.get("webui_branch"), "A1111 原版": ctx["a1111"], "根目录": root,
            "ComfyUI 代码目录": ctx["comfy_dir"], "Python": instance_python(api, iid),
            "运行状态": r.status() if r else "未启动过",
            "启动参数": cm.build_commandline_args(cfg, root or None),
            "附加参数 extra_args": cfg.get("extra_args", ""), "显卡后端": cfg.get("gpu_backend") or "cuda"}
    try:
        import cuda_compat as cc
        if ctx["comfy_dir"]:
            py = info["Python"]
            sps = [os.path.join(os.path.dirname(py), "Lib", "site-packages")] if py else []
        else:
            sps = cc.forge_site_packages(root, info["Python"])
        ver, tag = cc.installed_torch_tag([s for s in sps if os.path.isdir(s)])
        info["torch"] = f"{ver} ({tag})" if ver else "没找到"
    except Exception:
        pass
    plug = os.path.join(ctx["comfy_dir"], "custom_nodes") if ctx["comfy_dir"] else os.path.join(root, "extensions")
    try:
        info["已装的节点/扩展"] = [r["folder"] + ("（已停用）" if r["disabled"] else "") for r in vm.scan_repos(plug)]
    except Exception:
        pass
    return info


def environment(api, ctx):
    """发给 AI 的环境信息（已脱敏）"""
    import platform
    env = {"系统": f"{platform.system()} {platform.release()} ({platform.version()})"}
    try:
        import updater
        env["启动器版本"] = updater.get_local_info(api.cfg).get("version")
    except Exception:
        pass
    try:
        import cuda_compat as cc
        env["显卡"] = cc.gpu_names()
        drv = cc.driver_cuda_version()
        env["驱动支持的 CUDA"] = cc.fmt_cuda(drv) if drv else "读不到（可能不是 N 卡，或 nvidia-smi 不可用）"
    except Exception:
        pass
    try:
        import proxy_manager as pm
        env["启动器网络代理"] = pm.describe(api.cfg)
    except Exception:
        pass
    try:
        env["国内镜像"] = api._mirror_status_text()
    except Exception:
        pass
    env["实例列表"] = [{"id": i.get("id"), "名字": i.get("name"), "branch": i.get("webui_branch"),
                      "根目录": i.get("webui_root")} for i in api.cfg.get("instances") or []]
    env["当前实例"] = api.cfg.get("active_instance")
    if ctx.get("iid"):
        env["诊断对象"] = instance_info(api, ctx["iid"])
    if ctx.get("comfy_dir"):
        wf = os.path.join(ctx["comfy_dir"], "user", "wwy_bridge", "current_workflow.json")
        if os.path.isfile(wf):
            env["ComfyUI 网页里最近打开的工作流（桥接插件存的，可用 read_file 读）"] = wf
    return dg.redact(json.dumps(env, ensure_ascii=False, indent=1, default=str))


def knowledge():
    try:
        with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def system_prompt(api, ctx):
    roots = "\n".join(f"- {r}" for r in allowed_roots(api))
    sources = "\n".join(f"- {k}：{v}" for k, v in log_sources(api).items())
    return (
        "你是 WWY 启动器内置的报错诊断助手。WWY 启动器是一个 Windows 桌面程序，用来部署、启动、管理"
        " Stable Diffusion WebUI（Forge Neo / Forge Classic / A1111）和 ComfyUI。用户大多在中国大陆，"
        "不太懂技术。你的任务：根据日志和环境信息找出报错的真正原因，给出用户照着就能做的解决办法。\n\n"
        "工作方式：\n"
        "1. 先看给你的日志片段和环境信息；信息不够就用工具查（读文件、搜代码、看 pip 包、核对模型、测网址），"
        "最多查 6 步，别漫无目的地翻。\n"
        "2. 工具全是只读的，你不能直接改任何东西。要修就在 finish 的 actions 里推荐白名单动作，用户点了才执行。"
        "白名单外的操作写进 steps 让用户手动做。\n"
        "3. 最后必须调用 finish 交结论。用中文，口语化，别堆术语；steps 要具体到启动器的哪个页面、哪个按钮，"
        "或者 WebUI / ComfyUI 网页里的位置。拿不准就说拿不准，并说明还需要什么信息。\n"
        "4. 不要建议用户重装整个系统、乱删 venv 之类的大动作，除非证据充分；涉及 torch 版本的问题优先用启动器"
        "「环境部署」页的功能，不要让用户手动 pip install torch。\n\n"
        f"允许读取的目录：\n{roots}\n（日志里出现过的具体文件路径也可以读）\n\n"
        f"日志来源（get_log 的 source）：\n{sources}\n\n"
        f"修复动作白名单（finish.actions 里的 action / params）：\n{dg.actions_doc()}\n\n"
        "===== 启动器 / WebUI / ComfyUI 知识 =====\n" + knowledge())


def takeover_text(tk):
    """「AI 接管」模式的说明：这一轮第几轮、之前做过什么、实例现在什么状态"""
    hist = "\n".join(tk.get("history") or []) or "（这是第一轮，还没动过）"
    return (
        f"\n\n===== 现在是「AI 接管」模式（第 {tk['round']} 轮，最多 {tk['max']} 轮）=====\n"
        "用户把修复交给你了：你在 finish.actions 里给的动作会被直接执行，不再问用户（danger 级的删文件除外，"
        "restart_instance 也不用给，启动器每轮执行完都会自己重启实例）。然后启动器重启实例、等它起来，"
        "把新一轮的日志交给你看。\n"
        "- 日志里已经没有需要处理的报错（或剩下的是用户说不用管的）：verdict=fixed，actions 留空\n"
        "- 要用户自己动手（换驱动、手动下载要登录的模型、改系统设置……）：verdict=need_user，写清 steps\n"
        "- 否则 verdict=continue，给出这一轮要执行的动作；一次只做有把握的几步，别把不相干的东西都改一遍\n"
        "- 前几轮做过但没解决的办法不要原样再来一次，换思路；改过的设置如果证明没用，可以用同一个动作改回去\n"
        f"- 用户的说明（如果有）优先级最高，说不用管的问题就当没看见\n"
        f"实例当前状态：{tk.get('state') or '未知'}\n之前几轮：\n{hist}")


def run(api, ctx, log_text, findings=(), on_step=lambda t: None, cancelled=lambda: False, post=None, note="",
        takeover=None):
    """跑一次 agent。返回 {"summary","cause","steps","actions","confidence","verdict","rounds"}；出错抛 LlmError。
    note：用户自己写的问题描述 / 说明（可能日志里根本没报错，比如出图不对；也可能是「缺模型不用管」）
    takeover：AI 接管模式的 {"round","max","history","state"}，见 takeover_text"""
    conf = resolve(api.cfg)
    if not conf:
        raise LlmError("还没有设置 AI：到「启动器设置 → 报错诊断」填 API Key")
    fmt, base, model, key = conf
    tools = Tools(api, ctx, log_text)
    chat = Chat(fmt, base, model, key, system_prompt(api, ctx), TOOL_SPECS, post=post)
    what = "启动器本身" if ctx.get("kind") == "launcher" else f"实例「{ctx.get('name')}」（id={ctx.get('iid')}）"
    rule_txt = "\n".join(f"- {f['title']}：{f['evidence'][:200]}" for f in findings) or "（本地规则没认出来）"
    note = dg.redact(str(note or "").strip()[:2000])
    ask = f"用户的描述 / 说明（优先照这个来）：\n{note}\n\n" if note else ""
    rule_acts = "\n".join(f"  · {f['title']} → 规则建议：" + "；".join(
        f"{a['aid']} {json.dumps(a['params'], ensure_ascii=False)}" for a in f.get("actions") or [])
        for f in findings if takeover and f.get("actions"))
    chat.user(f"{ask}要诊断的对象：{what}\n\n环境信息：\n{environment(api, ctx)}\n\n"
              f"本地规则的识别结果：\n{rule_txt}\n" + (f"{dg.redact(rule_acts)}\n" if rule_acts else "") +
              f"\n日志片段（已脱敏）：\n```\n{dg.redact(dg.snippet(log_text, findings))}\n```" +
              (dg.redact(takeover_text(takeover)) if takeover else ""))
    on_step(f"正在请求 {model} ...")
    final_text = ""
    for rnd in range(1, MAX_STEPS + 2):
        if cancelled():
            raise LlmError("已取消")
        text, calls = chat.send()
        final_text = text or final_text
        if not calls:
            break
        results = []
        for cid, name, args in calls:
            if name == "finish":
                return _final(api, args, rnd, tools)
            on_step(_step_label(name, args))
            results.append((cid, tools.call(name, args) if rnd <= MAX_STEPS else
                            "查询次数用完了，请根据已有信息调用 finish 给结论"))
        chat.tool_results(results)
    if final_text.strip():
        return {"summary": final_text.strip()[:300], "cause": final_text.strip(), "steps": [], "actions": [],
                "confidence": "low", "verdict": "need_user", "rounds": MAX_STEPS}
    raise LlmError("AI 没有给出结论")


def _step_label(name, args):
    a = args or {}
    labels = {"list_dir": "查看目录", "read_file": "读取文件", "search_files": "搜索", "get_log": "查看日志",
              "python_packages": "查看 Python 包", "check_model_file": "核对模型文件", "test_url": "测试网址",
              "instance_info": "查看实例信息", "search_model": "搜索模型", "hf_files": "查看 HuggingFace 仓库"}
    target = a.get("path") or a.get("url") or a.get("source") or a.get("iid") or a.get("name") or a.get("repo") or ""
    extra = f"「{a['pattern']}」" if a.get("pattern") else ""
    return f"{labels.get(name, name)} {dg.redact(str(target))} {extra}".strip()


def _final(api, args, rounds, tools):
    acts, dropped = [], []
    for a in (args.get("actions") or [])[:6]:
        if not isinstance(a, dict):
            continue
        aid = a.get("action") or a.get("aid")
        params, err = dg.validate_action(api, aid, a.get("params") or {}, tools.roots)
        if err:
            dropped.append(f"{aid}：{err}")
            continue
        label = str(a.get("label") or dg.ACTIONS[aid]["desc"])[:40]
        acts.append({"aid": aid, "label": label, "params": params, "risk": dg.ACTIONS[aid]["risk"], "auto": False})
    steps = [str(s) for s in (args.get("steps") or []) if str(s).strip()]
    return {"summary": str(args.get("summary") or ""), "cause": str(args.get("cause") or ""), "steps": steps,
            "actions": acts, "dropped": dropped, "confidence": args.get("confidence") or "medium",
            "verdict": args.get("verdict") if args.get("verdict") in ("fixed", "continue", "need_user") else
            ("continue" if acts else "need_user"), "rounds": rounds}


def ping(cfg, post=None):
    """设置页「测试」：发一句话看通不通。返回 (ok, 说明)"""
    conf = resolve(cfg)
    if not conf:
        return False, "先填 API Key（自定义还要填接口地址和模型名）"
    fmt, base, model, key = conf
    chat = Chat(fmt, base, model, key, "你是测试助手。", [], timeout=30, post=post)
    chat.user("只回复两个字：正常")
    t0 = time.time()
    try:
        text, _ = chat.send(max_tokens=20)
    except LlmError as e:
        return False, str(e)
    return True, f"{model} 已连通（{time.time() - t0:.1f} 秒），回复：{text.strip()[:30]}"
