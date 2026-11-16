# -*- coding: utf-8 -*-
"""
整合包：制作（从装好的实例导出配方 .txt / .zip）和部署（按配方装出一样的环境）。
配方格式和纯函数在 pack_recipe.py；这里是联网、跑 git / pip 的流程。

和 lora_trainer.py 一样，函数第一个参数 api = LauncherApi：复用它的事件推送、
带取消 / 超时的命令执行、GitHub 加速和 pip 镜像选择。

部署 = 先走「环境部署」原本的流程（便携环境 → 源码 → torch 和依赖），
收尾登记实例之前调 after_deploy()：
  本体钉到配方里的提交 → torch 对齐 → 插件 / 节点逐个还原（git 钉提交 / 官方节点库 / 压缩包）
  → pip 包按配方对齐 → 下底模。单个插件 / 包失败只记下来，最后一起报告，不算部署失败。
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback
import zipfile

import config_manager as cm
import pack_recipe as pr
import version_manager as vm

SCOPE = "pack"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
UNKNOWN_MAX_FILE = 300 * 1024 * 1024   # 打进压缩包的插件里，超过这个大小的单个文件（多半是模型）不放
# 训练器的可选引擎：engines/ 下的文件夹 → envs/ 下的环境名
TRAINER_EXTRA = {"diffsynth-studio": "diffsynth", "ai-toolkit": "ai-toolkit"}


def _wa():
    import webview_api   # 延迟导入：webview_api 导入时还要 import 本模块
    return webview_api


class PackCancelled(Exception):
    pass


# ================================================================ 公共小工具

def _capture(args, cwd=None, env=None, timeout=300):
    """跑命令拿输出（中文 Windows 的输出按字节读再解码）。返回 (退出码, 文本)"""
    try:
        p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)
    return p.returncode, cm.decode_process_output(p.stdout or b"") + cm.decode_process_output(p.stderr or b"")


def pip_freeze(py, env=None):
    """(packages, skipped)；Python 不存在 / pip 跑不起来返回 ({}, [])"""
    if not py or not os.path.isfile(py):
        return {}, []
    rc, out = _capture([py, "-s", "-m", "pip", "freeze", "--all", "--disable-pip-version-check"],
                       env=env, timeout=300)
    if rc != 0:
        return {}, []
    return pr.parse_freeze(out)


def python_version(py, env=None):
    rc, out = _capture([py, "-c", "import sys;print(sys.version.split()[0])"], env=env, timeout=60)
    return out.strip().splitlines()[-1] if rc == 0 and out.strip() else ""


def _deploy_key(cfg):
    if cm.is_comfy(cfg):
        return "comfyui"
    if cfg.get("webui_branch") == "classic":
        return "classic"
    return "neo2h3" if cfg.get("webui_variant") == "h3" else "neo2"


def _gpu_names():
    try:
        return "、".join(_wa()._nvidia_gpu_names())
    except Exception:
        return ""


# ================================================================ 制作：可导出的东西

def sources(api):
    """「制作整合包」能选的：每个实例 + 装好的训练器"""
    wa = _wa()
    out = []
    for it in wa._instances_payload(api)["instances"]:
        cfg = cm.instance_cfg(api.cfg, it["id"]) or {}
        ok = bool(it["root"]) and not it["missing"]
        reason = "" if ok else "目录不存在"
        if ok and not cm.is_comfy(cfg) and cm.is_a1111(it["root"]):
            ok, reason = False, "A1111 原版 WebUI 不能用启动器部署，做不了整合包"
        out.append({"id": "inst:" + it["id"], "name": it["name"], "label": it["kind_label"], "root": it["root"],
                    "ok": ok, "reason": reason, "running": bool((it.get("status") or {}).get("running"))})
    root = wa._trainer_root(api)
    if root:
        import lora_trainer as lt
        info = lt.detect(root)
        if info.get("installed"):
            out.append({"id": "trainer", "name": "WWY LoRA 训练器", "label": "LoRA 训练器", "root": root,
                        "ok": bool(info.get("ready", True)), "reason": "" if info.get("ready", True) else "训练环境还没装好",
                        "running": False})
    return out


def _resolve(api, sid):
    """来源 id → 导出要用的上下文"""
    wa = _wa()
    if sid == "trainer":
        import lora_trainer as lt
        root = wa._trainer_root(api)
        if not root or not lt.is_trainer(root):
            raise pr.RecipeError("没找到装好的 LoRA 训练器")
        envs = [("", lt.env_python_of(root))]
        envs_dir = os.path.join(root, "envs")
        if os.path.isdir(envs_dir):
            for n in sorted(os.listdir(envs_dir)):
                py = os.path.join(envs_dir, n, "Scripts", "python.exe")
                if not os.path.isfile(py):
                    py = os.path.join(envs_dir, n, "bin", "python")
                if os.path.isfile(py):
                    envs.append(("envs/" + n, py))
        git = os.path.join(root, "git", "cmd", "git.exe")
        return {"kind": "lora_trainer", "name": "WWY LoRA 训练器", "root": root, "repo": root, "cfg": api.cfg,
                "plugins_dir": "", "comfy_dir": "", "git": git if os.path.isfile(git) else wa._git_for(api.cfg, root),
                "env": os.environ.copy(), "envs": envs, "official": lt.REPO}
    if not str(sid).startswith("inst:"):
        raise pr.RecipeError("请选择要做成整合包的实例")
    iid = sid[5:]
    cfg, info = wa._ver_target(api, iid)
    if not info.get("id") or info["id"] != iid:
        raise pr.RecipeError("这个实例已经不在了，刷新一下再选")
    root = info["root"]
    if not root or not os.path.isdir(root):
        raise pr.RecipeError(f"实例目录不存在：{root}")
    kind = _deploy_key(cfg)
    if kind != "comfyui" and cm.is_a1111(root):
        raise pr.RecipeError("A1111 原版 WebUI 不能用启动器部署，做不了整合包")
    py = cm.comfy_python(cfg, root) if kind == "comfyui" else wa._forge_env_python(cfg, root)
    return {"kind": kind, "name": info["name"], "root": root, "repo": info["repo"], "cfg": cfg,
            "plugins_dir": info["target_dir"], "comfy_dir": info.get("comfy_dir") or "",
            "git": wa._git_for(cfg, root), "env": wa._ext_env(cfg, info), "envs": [("", py)],
            "official": info["official_url"], "official_branch": info["official_branch"]}


def model_dirs(api, ctx):
    """[(分类键, 目录, 显示名)]：实例自己的底模目录 + 共享模型库（训练器：它的 models/）"""
    if ctx["kind"] == "lora_trainer":
        d = os.path.join(ctx["root"], "models")
        return [("trainer", d, "训练器 models")] if os.path.isdir(d) else []
    import model_library as ml
    out = []
    inst = ml.instance_model_dirs(ctx["cfg"], cm.comfy_layout)
    for k in pr.BASE_MODEL_KEYS:
        if k in inst:
            out.append((k, inst[k], os.path.relpath(inst[k], ctx["root"])))
    lib = _wa()._library_path(api)
    if lib:
        for k in pr.BASE_MODEL_KEYS:
            for d in ml.library_dir_variants(lib, k):
                out.append((k, d, f"共享模型库/{os.path.basename(d)}"))
    seen, uniq = set(), []
    for k, d, disp in out:
        n = os.path.normcase(os.path.abspath(d))
        if n not in seen:
            seen.add(n)
            uniq.append((k, d, disp))
    return uniq


def list_models(api, sid):
    ctx = _resolve(api, sid)
    items = []
    for key, d, disp in model_dirs(api, ctx):
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [x for x in dirnames if not x.startswith(".")]
            for fn in filenames:
                if not fn.lower().endswith(pr.MODEL_EXTS):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                if size < 1024 * 1024:
                    continue
                rel = os.path.relpath(full, d).replace("\\", "/")
                items.append({"path": full, "key": key, "name": rel, "where": disp, "size": size})
    items.sort(key=lambda x: (pr.BASE_MODEL_KEYS.index(x["key"]) if x["key"] in pr.BASE_MODEL_KEYS else 9,
                              x["name"].lower()))
    return items


# ================================================================ 制作：流程

def export_flow(api, sid, opts):
    """后台线程里跑。opts: name, out_dir, models（要带的模型文件路径）, check_remote（联网核对插件地址）"""
    log = lambda t: api._emit(SCOPE, "log", text=t + "\n")
    step = lambda pct, text: api._emit(SCOPE, "step", pct=pct, text=text)
    cancelled = lambda: bool(getattr(api, "_pack_cancel", False))
    try:
        ctx = _resolve(api, sid)
        ctx["sid"] = sid
        name = (opts.get("name") or "").strip() or ctx["name"]
        rec = pr.new_recipe(ctx["kind"], name)
        h = rec["head"]
        h["label"] = pr.KIND_LABELS[ctx["kind"]]
        try:
            import updater
            h["launcher"] = updater.get_local_info(api.cfg).get("version", "")
        except Exception:
            pass
        h["gpu"] = _gpu_names()
        log(f"[整合包] 开始制作：{name}（{h['label']}，{ctx['root']}）")

        step(5, "读取本体版本")
        git, env = ctx["git"], ctx["env"]
        rec["main"] = _export_main(ctx, git, env, log)

        packs = []   # (压缩包里的路径, 源路径, 只放这些相对路径 or None)
        if ctx["kind"] == "lora_trainer":
            step(15, "读取训练引擎")
            rec["engines"] = _export_engines(ctx, git, env, log)
        else:
            step(15, "检查插件 / 节点")
            rec["plugins"], packs = _export_plugins(api, ctx, git, env, log, bool(opts.get("check_remote", True)),
                                                    cancelled, step)
        if cancelled():
            raise PackCancelled()

        step(55, "记录 pip 包版本")
        for env_name, py in ctx["envs"]:
            pkgs, skipped = pip_freeze(py, env)
            if not pkgs:
                log(f"[整合包] 读不到 {env_name or '主环境'} 的 pip 包（Python：{py or '没找到'}）")
                continue
            rec["pip"][env_name] = pkgs
            if skipped:
                rec["pip_skipped"][env_name] = skipped
            if not env_name:
                h["python"] = python_version(py, env)
                t = pr.torch_specs(pkgs).get("torch")
                if t:
                    h["torch"] = t
                    if ctx["kind"] == "lora_trainer":
                        h["torch_variant"] = pr.torch_tag(t)
            log(f"[整合包] {env_name or '主环境'}：{len(pkgs)} 个 pip 包"
                + (f"（{len(skipped)} 个本地安装的跳过）" if skipped else ""))
        if cancelled():
            raise PackCancelled()

        chosen = [p for p in (opts.get("models") or []) if isinstance(p, str)]
        if chosen:
            step(65, "核对底模的下载来源")
            rec["models"] = _export_models(api, ctx, chosen, log, cancelled, step)
        if cancelled():
            raise PackCancelled()

        step(92, "写出整合包")
        out_dir = (opts.get("out_dir") or "").strip() or os.path.join(_wa().APP_DIR, "整合包")
        os.makedirs(out_dir, exist_ok=True)
        base = "".join(c for c in name if c not in '\\/:*?"<>|').strip() or "整合包"
        stamp = time.strftime("%Y%m%d-%H%M")
        text = pr.dump(rec)
        if packs:
            path = os.path.join(out_dir, f"{base}_{stamp}.zip")
            total = 0
            big_all = []
            with zipfile.ZipFile(path + ".part", "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.writestr(pr.RECIPE_NAME, text.encode("utf-8"))
                for arc, src, files in packs:
                    n, size, big = pr.add_tree(z, src, arc, files, UNKNOWN_MAX_FILE)
                    total += size
                    big_all += [(arc, r, s) for r, s in big]
                    log(f"[整合包] 打包 {arc}：{n} 个文件，{size / 1024 ** 2:.1f} MB")
            os.replace(path + ".part", path)
            for arc, rel, size in big_all:
                log(f"[整合包] 注意：{arc}/{rel}（{size / 1024 ** 3:.2f} GB）太大没放进压缩包，"
                    "部署的人要自己补上这个文件")
        else:
            path = os.path.join(out_dir, f"{base}_{stamp}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        s = pr.summary(rec, path if packs else None)
        log(f"[整合包] 完成：{path}")
        step(100, "完成")
        # 完成弹窗分开说：查不到来源、连整个文件夹打包的；有 git 地址、只打包了本地改动的
        api._emit(SCOPE, "done", ok=True, path=path, zip=bool(packs), summary=s,
                  packed=[p["folder"] for p in rec["plugins"] if p.get("zip")],
                  overlay=[p["folder"] for p in rec["plugins"] if p.get("overlay") and not p.get("zip")])
    except PackCancelled:
        log("[整合包] 已取消")
        api._emit(SCOPE, "done", ok=False, cancelled=True)
    except pr.RecipeError as e:
        log(f"[整合包] {e}")
        api._emit(SCOPE, "done", ok=False, error=str(e))
    except Exception as e:
        log(f"[整合包] 出错：{e}\n{traceback.format_exc()[-1500:]}")
        api._emit(SCOPE, "done", ok=False, error=f"制作失败：{e}")
    finally:
        api._pack_busy = False


def _export_main(ctx, git, env, log):
    repo = ctx["repo"]
    info = vm.local_info(git, repo, env) if git and repo else {"is_git": False}
    if not info.get("is_git") or not info.get("commit"):
        log("[整合包] 本体不是 git 安装的（比如解压的整合包），记不了具体版本：部署时会装官方最新版。"
            "想钉住版本可以先在「版本管理」里点「接管为 Git 管理」")
        return {"url": ctx.get("official", "")}
    m = {"url": info.get("remote") or ctx.get("official", ""), "commit": info["commit"],
         "branch": info.get("branch", ""), "label": vm.version_label(info)}
    if info.get("dirty"):
        log(f"[整合包] 本体有 {len(info['dirty'])} 个文件被改过（多半是配置文件），整合包里不带这些改动")
    if not _commit_on_remote(git, repo, info["commit"], env):
        log("[整合包] 注意：本体当前的提交不在远程仓库上（本地自己提交的？），部署时会退回分支的最新版")
        m["note"] = "本地提交"
    log(f"[整合包] 本体：{m['url']} @ {m['label']}")
    return m


def _commit_on_remote(git, repo, sha, env):
    """提交在任意一个远程分支上 = 别人拉得到。仓库里一个远程分支都没有时没法判断，按「在」算"""
    rc, refs = vm.capture(git, repo, ["for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes"], env)
    if rc != 0 or not refs.strip():
        return True
    rc, out = vm.capture(git, repo, ["branch", "-r", "--contains", sha], env, 60)
    return rc != 0 or bool(out.strip())   # 命令本身失败（浅克隆缺历史）时也按「在」算


def _loose_files(ctx, git, env):
    """ComfyUI custom_nodes 根目录下的单文件节点（xxx.py），去掉 ComfyUI 仓库自带的那几个"""
    if ctx["kind"] != "comfyui" or not ctx["plugins_dir"] or not os.path.isdir(ctx["plugins_dir"]):
        return []
    tracked = set()
    if git and ctx["comfy_dir"] and vm.git_dir_of(ctx["comfy_dir"]):
        rc, out = vm.capture(git, ctx["comfy_dir"], ["ls-files", "custom_nodes"], env)
        if rc == 0:
            tracked = {os.path.basename(x).lower() for x in out.splitlines()}
    return [fn for fn in sorted(os.listdir(ctx["plugins_dir"]), key=str.lower)
            if fn.lower().endswith(".py") and fn.lower() not in tracked
            and os.path.isfile(os.path.join(ctx["plugins_dir"], fn))]


def _is_empty_dir(path):
    """空文件夹（或只有 __pycache__ 之类）：不是插件，不记"""
    for dirpath, dirnames, filenames in os.walk(path, followlinks=True):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        if filenames:
            return False
    return True


def _skip_plugin(ctx, folder):
    """启动器自己会放的东西不进整合包（部署时启动器照样会放）"""
    import comfy_bridge
    return ctx["kind"] == "comfyui" and folder.lower() == comfy_bridge.NAME.lower()


def _export_plugins(api, ctx, git, env, log, check_remote, cancelled, step):
    wa = _wa()
    base = ctx["plugins_dir"]
    scanned = vm.scan_repos(base) if base else []
    disabled_names = set()
    if ctx["kind"] != "comfyui":
        disabled_names, _ = wa._webui_disabled_exts(ctx["root"])
    sources = wa._gh_sources(ctx["cfg"]) if check_remote else []
    if check_remote and git and not vm.rank_sources(git, base, "https://github.com/comfyanonymous/ComfyUI.git",
                                                    sources, env, None, timeout=20):
        log("[整合包] GitHub 直连和加速地址都连不上，这次不联网核对插件地址（只按本地信息判断）")
        check_remote = False
    plugins, packs = [], []
    jobs, seen = [], {}
    # 启用的排前面：同一个插件既有启用的、又有停用的旧副本（xxx 和 xxx.disabled）时只记启用的
    for r in sorted(scanned, key=lambda r: bool(r["disabled"])):
        folder = r["folder"]
        clean = folder[:-len(".disabled")] if folder.lower().endswith(".disabled") else folder
        if _skip_plugin(ctx, clean) or not pr.safe_folder(clean) or _is_empty_dir(r["path"]):
            continue
        if clean.lower() in seen:
            log(f"[整合包] {folder}：和 {seen[clean.lower()]} 是同一个插件的另一份（停用的旧副本），不记")
            continue
        seen[clean.lower()] = folder
        p = {"folder": clean, "disabled": bool(r["disabled"] or clean.lower() in disabled_names)}
        jobs.append((r, p))
    jobs.sort(key=lambda j: j[1]["folder"].lower())
    for i, (r, p) in enumerate(jobs):
        if cancelled():
            raise PackCancelled()
        step(15 + int(38 * i / max(1, len(jobs))), f"检查插件 / 节点（{i + 1}/{len(jobs)}）{p['folder']}")
        arc = f"{pr.PLUGIN_DIR}/{p['folder']}"
        if r["is_git"] and r["remote"] and r["commit"]:
            p.update(git=r["remote"], commit=r["commit"])
            if r.get("branch"):
                p["branch"] = r["branch"]
            reason = ""
            if not _commit_on_remote(git, r["path"], r["commit"], env):
                reason = "当前提交只在本地（没推到远程）"
            elif check_remote and not vm.rank_sources(git, base, r["remote"], sources, env, None, timeout=15):
                reason = "远程仓库连不上（私有仓库 / 已删除，或者网络不通）"
            if reason:
                p["zip"] = arc
                p["note"] = reason
                packs.append((arc, r["path"], None))
                log(f"[整合包] {p['folder']}：{reason}，连文件一起打包")
            else:
                dirty = vm.dirty_files(git, r["path"], env)
                if dirty:
                    p["overlay"] = arc
                    packs.append((arc, r["path"], dirty))
                    log(f"[整合包] {p['folder']}：本地改过 {len(dirty)} 个文件，改动一起打包")
        else:
            cnr = pr.read_cnr(r["path"])
            ok = False
            if cnr:
                p.update(cnr=cnr[0], version=cnr[1])
                ok = not check_remote or _cnr_url(cnr[0], cnr[1]) is not None
                if not ok:
                    log(f"[整合包] {p['folder']}：官方节点库里查不到 {cnr[0]} {cnr[1]}，连文件一起打包")
            if not ok:
                p["zip"] = arc
                if not cnr:
                    p["note"] = "没有 git 地址" if not r["is_git"] else "git 仓库没有远程地址"
                    log(f"[整合包] {p['folder']}：{p['note']}，查不到来源，连文件一起打包")
                packs.append((arc, r["path"], None))
        plugins.append(p)
    for fn in _loose_files(ctx, git, env):
        arc = f"{pr.PLUGIN_DIR}/{fn}"
        plugins.append({"folder": fn, "zip": arc, "note": "单文件节点"})
        packs.append((arc, os.path.join(base, fn), None))
        log(f"[整合包] {fn}：单文件节点，一起打包")
    n_git = sum(1 for p in plugins if p.get("git") and not p.get("zip"))
    n_cnr = sum(1 for p in plugins if p.get("cnr") and not p.get("zip"))
    n_zip = sum(1 for p in plugins if p.get("zip"))
    log(f"[整合包] 插件 / 节点 {len(plugins)} 个：git {n_git} 个、官方节点库 {n_cnr} 个、打包 {n_zip} 个")
    return plugins, packs


def _cnr_url(node_id, version, timeout=20):
    """官方节点库里这个版本的下载地址；查不到 / 连不上返回 None"""
    try:
        import requests
        r = requests.get(pr.cnr_install_api(node_id, version), timeout=timeout)
        if r.status_code != 200:
            return None
        url = (r.json() or {}).get("downloadUrl")
        return url or None
    except Exception:
        return None


def _export_engines(ctx, git, env, log):
    out = []
    d = os.path.join(ctx["root"], "engines")
    if not os.path.isdir(d):
        return out
    for r in vm.scan_repos(d):
        name = r["folder"]
        e = {"name": name}
        if r["is_git"] and r["remote"] and r["commit"]:
            e.update(git=r["remote"], commit=r["commit"])
            if r.get("branch"):
                e["branch"] = r["branch"]
        extra = TRAINER_EXTRA.get(name.lower())
        if extra:
            e["extra"] = extra
        out.append(e)
        log(f"[整合包] 训练引擎 {name}：{(r.get('commit') or '不是 git')[:10]}"
            + ("（可选引擎）" if extra else ""))
    return out


def _sha256_of(api, path, cancelled, on_pct=None):
    wa = _wa()
    st = os.stat(path)
    entry = wa._hash_cache_get(api, path)
    if entry and wa.cache_entry_valid(entry, st) and entry.get("sha256"):
        return entry["sha256"]
    digest = wa.compute_sha256(path, progress_cb=on_pct, cancel_flag=cancelled)
    if digest:
        new = dict(entry or {}, size=st.st_size, mtime=int(st.st_mtime), sha256=digest)
        wa._hash_cache_set(api, path, new)
    return digest


def _catalog_by_sha():
    """常用模型目录里所有文件：sha256 → hf 来源（仓库:路径）"""
    out = {}
    try:
        import model_catalog as mc
        for m in mc.load().get("models", []):
            for v in m.get("versions", []):
                for c in v.get("components", []):
                    for o in c.get("options", []):
                        sha = (o.get("sha256") or "").lower()
                        hf = next((s for s in o.get("src") or [] if s.get("hf")), None)
                        if sha and hf:
                            out[sha] = f"{hf['hf']}:{hf['path']}"
    except Exception:
        pass
    return out


def _export_models(api, ctx, chosen, log, cancelled, step):
    wa = _wa()
    by_path = {os.path.normcase(os.path.abspath(m["path"])): m for m in list_models(api, ctx["sid"])}
    catalog = _catalog_by_sha()
    api_key = (api.cfg.get("civitai_api_key") or "").strip() or None
    out = []
    for i, path in enumerate(chosen):
        if cancelled():
            raise PackCancelled()
        m = by_path.get(os.path.normcase(os.path.abspath(path)))
        if not m:
            log(f"[整合包] 跳过（不在这个实例的底模目录里）：{path}")
            continue
        pct0 = 65 + int(25 * i / max(1, len(chosen)))
        step(pct0, f"算哈希 {m['name']}")
        digest = _sha256_of(api, path, cancelled,
                            lambda p, m=m, pct0=pct0: step(pct0, f"算哈希 {m['name']}（{int(p)}%）"))
        if cancelled():
            raise PackCancelled()
        md = {"key": m["key"], "name": m["name"], "size": m["size"], "sha256": digest}
        side = wa.read_sidecar(path)
        if side.get("versionId"):
            md["civitai"] = str(side["versionId"])
        if side.get("liblibUuid"):
            md["liblib"] = side["liblibUuid"] + ("/" + side["liblibVersionUuid"] if side.get("liblibVersionUuid") else "")
        if digest and digest.lower() in catalog:
            md["hf"] = catalog[digest.lower()]
        if digest and not md.get("civitai"):
            step(pct0, f"到 C 站查 {m['name']}")
            try:
                data, code = wa.query_by_hash(digest, api_key)
                if data and data.get("id"):
                    md["civitai"] = str(data["id"])
                    f = next((f for f in data.get("files") or []
                              if ((f.get("hashes") or {}).get("SHA256") or "").lower() == digest.lower()), None)
                    if f and f.get("downloadUrl"):
                        md["civitai_url"] = f["downloadUrl"]
            except Exception as e:
                log(f"[整合包] C 站查询 {m['name']} 失败：{e}")
        if digest and not md.get("liblib"):
            try:
                import liblib_client as lc
                lb = lc.query_by_hash(digest)
                if lb and lb.get("model_uuid"):
                    md["liblib"] = lb["model_uuid"] + ("/" + lb["version_uuid"] if lb.get("version_uuid") else "")
            except Exception:
                pass
        where = [w for w, k in (("C 站", "civitai"), ("常用模型目录", "hf"), ("LiblibAI", "liblib")) if md.get(k)]
        log(f"[整合包] 底模 {m['name']}：" + ("、".join(where) + " 能下" if where else "哪儿都查不到，部署的人得自己放"))
        out.append(md)
    return out


# ================================================================ 部署：读配方

def load(path):
    rec, zpath = pr.read_source(path)
    s = pr.summary(rec, zpath)
    s["path"] = path
    return rec, zpath, s


def start(api, path, target, model_choice, use_portable=True, gpu_backend=None, amd_gfx=None):
    """
    「部署整合包」：读配方 → 记到 api._deploy_pack → 走原本的部署入口。
    model_choice: {"分类/文件名": "civitai" | "hf" | "liblib" | ""}（空 = 不下）
    """
    wa = _wa()
    if api._deploy_running:
        return {"ok": False, "error": "已经有部署任务在进行，等它结束再来"}
    rec, zpath = pr.read_source(path)
    kind = rec["head"]["kind"]
    pack = {"recipe": rec, "zip": zpath, "models": model_choice or {}, "report": {}}
    api._deploy_pack = pack
    try:
        if kind == "lora_trainer":
            # 训练器的 PyTorch 按 CUDA 版本选（cu126 / cu128 / cu130）：显卡 / 驱动带得动就和原机一样，否则按这台电脑自动选
            tag, _, _ = torch_target(rec, api.cfg)
            r = wa._api_trainer_deploy(api, target, tag if tag in _trainer_variants() else "auto")
        else:
            # ComfyUI 的节点全按配方装：部署流程看到 _deploy_pack 就跳过默认推荐的那一套（不改用户记住的勾选）
            r = wa._api_deploy_start(api, target, kind, use_portable, None, gpu_backend, amd_gfx)
    except Exception:
        api._deploy_pack = None
        raise
    if not r.get("ok"):
        api._deploy_pack = None
    else:
        api._emit("deploy", "log", text=f"[整合包] 按整合包部署：{rec['head'].get('name')}（{path}）\n"
                                         "         先装好程序本体，收尾时再按整合包还原版本、插件和 pip 包\n")
    return r


def _trainer_variants():
    import lora_trainer as lt
    return lt.TORCH_VARIANTS


# ================================================================ 部署：还原

class _Ctx:
    def __init__(self, api, kind, root, log, env):
        wa = _wa()
        self.api, self.kind, self.root, self.log = api, kind, root, log
        self.cfg = api._deploy_cfg
        self.env = env
        self.cancelled = api._deploy_cancel.is_set
        self.sources = wa._gh_sources(self.cfg, lambda m: log(m + "\n"))
        self.mirrors = wa._pip_mirror_argsets(self.cfg, lambda m: log(m + "\n"))
        if kind == "comfyui":
            import comfy_nodes
            self.comfy_dir = cm.comfy_layout(root)[0] or root
            self.repo = self.comfy_dir
            self.plugins_dir = os.path.join(self.comfy_dir, "custom_nodes")
            self.py = cm.comfy_python(self.cfg, root)
            self.env = comfy_nodes.node_env(self.comfy_dir, env)   # 和装节点时一样（SAM-2 不编译 CUDA 扩展等）
        elif kind == "lora_trainer":
            import lora_trainer as lt
            self.comfy_dir = ""
            self.repo = root
            self.plugins_dir = ""
            self.py = lt.env_python_of(root)
        else:
            self.comfy_dir = ""
            self.repo = root
            self.plugins_dir = os.path.join(root, "extensions")
            self.py = wa._forge_env_python(self.cfg, root)
        self.git = wa._git_for(self.cfg, root) or "git"

    def run(self, program, args, cwd, env=None, timeout=None):
        return self.api._deploy_run_cmd(program, args, cwd, self.log, env=env or self.env, timeout=timeout)

    def check(self):
        if self.cancelled():
            raise _wa()._DeployCancelled()


def after_deploy(api, kind, root, log, env):
    """部署流程收尾前调用（在部署线程里）。没有整合包任务时什么都不做"""
    pack = getattr(api, "_deploy_pack", None)
    if not pack:
        return
    rec, zpath = pack["recipe"], pack["zip"]
    report = pack["report"]
    c = _Ctx(api, kind, root, lambda t: log(t if t.endswith("\n") else t + "\n"), env)
    L = c.log
    L("\n[整合包] ===== 按整合包还原 =====")
    want_py = (rec.get("head") or {}).get("python", "")
    have_py = python_version(c.py, c.env) if c.py else ""
    if want_py and have_py and want_py.split(".")[:2] != have_py.split(".")[:2]:
        report["python"] = (f"整合包是在 Python {want_py} 上做的，这里是 {have_py}："
                            "有的包可能没有这个 Python 版本的安装包，对齐时会跳过")
        L("[整合包] 注意：" + report["python"])
    _apply_main(c, rec.get("main") or {}, report)
    c.check()
    if kind != "lora_trainer":
        _apply_torch(c, rec, report)
        c.check()
        _apply_plugins(c, rec.get("plugins") or [], zpath, report)
        c.check()
    else:
        _apply_engines(c, rec.get("engines") or [], report)
        c.check()
    for env_name, pkgs in (rec.get("pip") or {}).items():
        py = c.py if not env_name else _env_python(root, env_name)
        if not py:
            if env_name:
                report.setdefault("pip_failed", []).append(f"{env_name}（环境没装好）")
            continue
        _apply_pip(c, py, pkgs, env_name, report)
        c.check()
    if pack.get("models"):
        _apply_models(c, rec.get("models") or [], pack["models"], report)
    _final_report(c, report)


def _env_python(root, env_name):
    d = os.path.join(root, *env_name.split("/"))
    for p in (os.path.join(d, "Scripts", "python.exe"), os.path.join(d, "bin", "python")):
        if os.path.isfile(p):
            return p
    return ""


def _apply_main(c, main, report):
    sha = main.get("commit") or ""
    if not sha or main.get("note") == "本地提交":
        if main.get("url"):
            c.log("[整合包] 整合包没记本体的具体版本，保持刚装好的最新版")
        return
    if not vm.git_dir_of(c.repo):
        report["main"] = "本体不是 git 仓库，没法切版本"
        return
    url = main.get("url") or ""
    cur = vm.read_remote_url(c.repo)
    if url and vm.repo_slug(url) and vm.repo_slug(cur) and vm.repo_slug(url) != vm.repo_slug(cur):
        c.log(f"[整合包] 本体用的是另一个仓库：{url}")
        vm.capture(c.git, c.repo, ["config", "remote.origin.url", vm.normalize_url(url)], c.env)
    c.log(f"[整合包] 本体切到整合包里的版本：{main.get('label') or sha[:10]}")
    run = lambda p, a, cwd, e, t: c.run(p, a, cwd, e, t)
    res = vm.switch(run, c.git, c.repo, {"kind": "commit", "commit": sha}, url or cur, c.sources, c.log, c.env,
                    keep_files=vm.WEBUI_KEEP_FILES if c.kind not in ("comfyui", "lora_trainer") else (),
                    cancelled=c.cancelled)
    if not res.get("ok"):
        report["main"] = f"本体没切到 {sha[:10]}：{res.get('error')}"
        c.log(f"[整合包] {report['main']}（继续用刚装好的版本）")
        return
    branch = main.get("branch")
    if branch:   # 回到分支上（和原来一样能一键更新），分支指向这个提交
        vm.capture(c.git, c.repo, ["checkout", "-B", branch], c.env, 120)
        vm.capture(c.git, c.repo, ["branch", f"--set-upstream-to=origin/{branch}"], c.env)
    if c.kind == "comfyui":
        import comfy_nodes
        comfy_nodes.sync_comfy_requirements(c.comfy_dir, c.py, lambda p, a, cwd, e, t: c.run(p, a, cwd, e, t),
                                            c.log, c.env, c.cancelled, c.sources, c.mirrors)


def _gpu_sm():
    try:
        return _wa()._gpu_identity().get("sm")
    except Exception:
        return None


def torch_target(rec, cfg):
    """
    整合包里的 torch 在这台电脑上能不能照装：(CUDA tag, ["torch==2.9.1", …], "") 或 (None, None, 不能装的原因)。
    整合包没记 torch 时原因为空串。
    """
    want = pr.torch_specs((rec.get("pip") or {}).get("", {}))
    if not want.get("torch"):
        return None, None, ""
    tag = pr.torch_tag(want["torch"])
    if not tag.startswith("cu") or (cfg or {}).get("gpu_backend") == "rocm":
        return None, None, f"整合包是 {want['torch']}，这台电脑的显卡环境不同"
    sm = _gpu_sm()
    try:
        num = int(tag[2:])
    except ValueError:
        num = 0
    if sm and sm >= 12.0 and num < 128:
        return None, None, f"整合包的 torch {want['torch']} 不支持 50 系显卡"
    try:
        import cuda_compat as cc
        if cc.supported_tag(tag, cc.driver_cuda_version()) != tag:
            return None, None, f"显卡驱动带不动 {tag}（升级驱动后可以重新部署）"
    except Exception:
        pass
    return tag, [f"{k}=={pr.base_version(v)}" for k, v in want.items()], ""


def deploy_torch(api, log):
    """ComfyUI 部署装 torch 那一步问一下：按整合包部署、显卡带得动时返回 (tag, specs)，否则 None"""
    pack = getattr(api, "_deploy_pack", None)
    if not pack:
        return None
    tag, specs, why = torch_target(pack["recipe"], api._deploy_cfg)
    if tag:
        log(f"[整合包] torch 直接装整合包里的版本：{' '.join(specs)}（{tag}）\n")
        return tag, specs
    if why:
        log(f"[整合包] {why}，torch 按这台电脑自动选\n")
    return None


def _apply_torch(c, rec, report):
    """torch 三件套对齐到整合包的版本（显卡 / 驱动带不动那个 CUDA 版本时保持现在的）"""
    want = pr.torch_specs((rec.get("pip") or {}).get("", {}))
    if not want.get("torch") or not c.py:
        return
    have = pr.torch_specs(pip_freeze(c.py, c.env)[0])
    if have.get("torch") == want["torch"] and all(have.get(k) == v for k, v in want.items()):
        c.log(f"[整合包] torch 已经是 {want['torch']}")
        return
    tag, specs, why = torch_target(rec, c.cfg)
    if not tag:
        report["torch"] = f"{why}，保持 {have.get('torch') or '现有版本'}"
        c.log("[整合包] " + report["torch"])
        return
    try:
        import mirror_manager as mm
        bases = mm.torch_bases(c.cfg, lambda m: c.log(m + "\n"))
    except Exception:
        bases = [_wa().mm_official()]
    for b in bases:
        c.check()
        c.log(f"[整合包] 安装整合包里的 torch：{' '.join(specs)}（{b}/{tag}）")
        if c.run(c.py, ["-s", "-m", "pip", "install", "--disable-pip-version-check"] + specs
                 + ["--index-url", f"{b}/{tag}"], c.repo, timeout=5400) == 0:
            return
    report["torch"] = f"torch {want['torch']} 没装上，保持 {have.get('torch')}"
    c.log("[整合包] " + report["torch"])


def _clone(c, url, dest):
    """git clone（测速选源，一条线路失败换下一条），带子模块。成功返回 True"""
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    ranked = vm.rank_sources(c.git, parent, url, c.sources, c.env, lambda t: c.log(t)) or [""]
    for prefix in ranked:
        c.check()
        args = (["-c", f"url.{prefix}.insteadOf=https://github.com/"] if prefix else []) + \
            ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30",
             "clone", "--recursive", "--shallow-submodules", url, dest]
        c.log(f"[整合包] 下载（{vm.source_label(prefix)}）...")
        if c.run(c.git, args, parent, timeout=1200) == 0 and os.path.isdir(dest):
            vm.capture(c.git, dest, ["config", "remote.origin.url", vm.normalize_url(url)], c.env)
            return True
        shutil.rmtree(dest, ignore_errors=True)
    return False


def _pin(c, dest, p):
    run = lambda prog, a, cwd, e, t: c.run(prog, a, cwd, e, t)
    res = vm.switch(run, c.git, dest, {"kind": "commit", "commit": p["commit"]}, p["git"], c.sources,
                    lambda t: c.log(t), c.env, reapply=False, cancelled=c.cancelled)
    if not res.get("ok"):
        return res.get("error") or "切换版本失败"
    if p.get("branch"):
        vm.capture(c.git, dest, ["checkout", "-B", p["branch"]], c.env, 120)
        vm.capture(c.git, dest, ["branch", f"--set-upstream-to=origin/{p['branch']}"], c.env)
    return ""


def _install_cnr(c, p, dest):
    url = _cnr_url(p["cnr"], p["version"])
    if not url:
        return f"官方节点库里查不到 {p['cnr']} {p['version']}"
    tmp = tempfile.mkdtemp(prefix="wwy_cnr_")
    try:
        import requests
        zp = os.path.join(tmp, "node.zip")
        with requests.get(url, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            with open(zp, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if c.cancelled():
                        return "已取消"
                    f.write(chunk)
        os.makedirs(dest, exist_ok=True)
        names = []
        with zipfile.ZipFile(zp) as z:
            base = os.path.abspath(dest)
            for info in z.infolist():
                t = os.path.abspath(os.path.join(base, *info.filename.replace("\\", "/").split("/")))
                if not t.startswith(base + os.sep):
                    continue
                if info.is_dir():
                    os.makedirs(t, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(t), exist_ok=True)
                with z.open(info) as s, open(t, "wb") as o:
                    shutil.copyfileobj(s, o)
                names.append(info.filename)
        # Manager 靠 .tracking 认出这是节点库装的（以后能在 Manager 里更新 / 切版本）
        with open(os.path.join(dest, ".tracking"), "w", encoding="utf-8") as f:
            f.write("\n".join(names) + "\n")
        return ""
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return f"从官方节点库下载失败：{e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _apply_plugins(c, plugins, zpath, report):
    if not plugins:
        return
    wa = _wa()
    comfy = c.kind == "comfyui"
    os.makedirs(c.plugins_dir, exist_ok=True)
    have = {}
    for r in vm.scan_repos(c.plugins_dir):
        f = r["folder"]
        have[(f[:-len(".disabled")] if f.lower().endswith(".disabled") else f).lower()] = r
    ok, failed = [], []
    c.log(f"\n[整合包] 还原插件 / 节点：{len(plugins)} 个（单个失败不影响其它）")
    for i, p in enumerate(plugins):
        c.check()
        folder = p["folder"]
        dest = os.path.join(c.plugins_dir, folder)
        c.log(f"\n[整合包] ({i + 1}/{len(plugins)}) {folder}")
        err, same = "", False
        existing = have.get(folder.lower())   # 目录里已经有它（可能是停用的 xxx.disabled）
        try:
            if p.get("zip"):
                if not zpath or not pr.zip_members(zpath, p["zip"]) and not _zip_has_file(zpath, p["zip"]):
                    err = "整合包压缩包里没有它的文件（只拿到了 .txt？）"
                else:
                    if existing:   # 写进已有的那个文件夹（停用的就写进 xxx.disabled），不另建一份
                        dest = existing["path"]
                        c.log("[整合包] 已经有这个插件，按整合包里的文件覆盖")
                    n = pr.extract_prefix(zpath, p["zip"], dest)
                    c.log(f"[整合包] 从压缩包还原 {n} 个文件")
            elif p.get("cnr"):
                if existing and pr.read_cnr(existing["path"]) == (p["cnr"], p["version"]):
                    dest, same = existing["path"], True
                    c.log("[整合包] 已经装好同一个版本，跳过")
                else:
                    if existing:
                        _move_aside(existing["path"], c.log)
                    c.log(f"[整合包] 从 ComfyUI 官方节点库下载 {p['cnr']} {p['version']}")
                    err = _install_cnr(c, p, dest)
            elif p.get("git"):
                if existing and existing["is_git"] and _same_repo(existing["remote"], p["git"]):
                    dest = existing["path"]
                    if existing["commit"] == p.get("commit"):
                        same = not p.get("overlay")
                        c.log("[整合包] 已经装好同一个版本，跳过")
                    elif p.get("commit"):
                        err = _pin(c, dest, p)
                else:
                    if existing:
                        _move_aside(existing["path"], c.log)
                    if not _clone(c, p["git"], dest):
                        err = "下载失败（GitHub 直连和加速地址都没成功）"
                    elif p.get("commit"):
                        err = _pin(c, dest, p)
                if not err and p.get("overlay"):
                    if zpath and pr.zip_members(zpath, p["overlay"]):
                        n = pr.extract_prefix(zpath, p["overlay"], dest)
                        c.log(f"[整合包] 覆盖上原来本地改过的 {n} 个文件")
                    else:
                        report.setdefault("notes", []).append(f"{folder}：原来本地改过的文件不在压缩包里，用的是官方原版")
            else:
                err = "整合包里没写来源"
            if not err and not same and comfy and os.path.isdir(dest) and c.py:
                import comfy_nodes
                bad = comfy_nodes.install_node_deps(dest, c.comfy_dir, c.py,
                                                    lambda prog, a, cwd, e, t: c.run(prog, a, cwd, e, t),
                                                    c.log, c.env, c.cancelled, c.sources, c.mirrors)
                if bad:
                    report.setdefault("notes", []).append(f"{folder}：依赖没装全（{'、'.join(bad)}），pip 对齐时会再试")
            if not err:
                _set_enabled(c, folder, dest, not p.get("disabled"))
        except _wa()._DeployCancelled:
            raise
        except Exception as e:
            err = f"出错：{e}"
        if err:
            failed.append((folder, err))
            c.log(f"[整合包] {folder} 没装上：{err}")
        else:
            ok.append(folder)
    report["plugins_ok"] = ok
    report["plugins_failed"] = failed
    if comfy:
        try:
            import comfy_nodes
            comfy_nodes.fix_rgthree_compat(c.comfy_dir, lambda m: c.log(m.replace("[启动器]", "[整合包]")))
        except Exception:
            pass


def _same_repo(a, b):
    """两个仓库地址是不是同一个（Windows 路径的反斜杠 / 正斜杠、.git 后缀、大小写都不算区别）"""
    slug = lambda u: vm.repo_slug((u or "").replace("\\", "/"))
    return bool(slug(a)) and slug(a) == slug(b)


def _set_enabled(c, folder, path, enabled):
    """
    插件的启用 / 停用和整合包一致。
    ComfyUI 靠文件夹名：停用 = xxx.disabled（也可能在 .disabled/ 子文件夹里），启用 = xxx；
    WebUI 靠 config.json 的 disabled_extensions。
    """
    if c.kind != "comfyui":
        if not enabled:
            _wa()._webui_set_ext_disabled(c.root, folder, True)
            c.log("[整合包] 原来是停用状态，照样停用")
        return
    if not os.path.exists(path):
        return
    want = os.path.join(c.plugins_dir, folder + ("" if enabled else ".disabled"))
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(want)):
        if not enabled:
            c.log("[整合包] 原来是停用状态，照样停用")
        return
    if os.path.exists(want):   # 启用和停用的两份都在：不动，免得覆盖掉一份
        c.log(f"[整合包] 注意：{os.path.basename(path)} 和 {os.path.basename(want)} 两份都在，请手动删掉不要的那份")
        return
    os.rename(path, want)
    c.log("[整合包] 原来是停用状态，照样停用" if not enabled else "[整合包] 原来是启用状态，已启用")


def _zip_has_file(zpath, arc):
    try:
        with zipfile.ZipFile(zpath) as z:
            return arc in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _move_aside(path, log):
    """部署目录里已经有同名但来源不同的插件：挪到 xxx.old-时间 保留，不删"""
    new = f"{path}.old-{time.strftime('%Y%m%d%H%M%S')}"
    try:
        os.rename(path, new)
        log(f"[整合包] 原来的同名插件挪到了 {os.path.basename(new)}（没删）")
    except OSError as e:
        raise RuntimeError(f"原来的同名文件夹挪不开：{e}")


def _apply_engines(c, engines, report):
    """训练器：主引擎钉提交；可选引擎（DiffSynth / AI Toolkit）先用训练器自己的安装流程装上再钉提交"""
    ok, failed = [], []
    for e in engines:
        c.check()
        name = e.get("name", "")
        d = os.path.join(c.root, "engines", name)
        if e.get("extra") and not os.path.isdir(d):
            c.log(f"\n[整合包] 安装可选训练引擎 {name}（独立环境，按训练器自己的流程装）")
            err = _trainer_extra_install(c, e["extra"])
            if err:
                failed.append((name, err))
                c.log(f"[整合包] {name} 没装上：{err}")
                continue
        if not e.get("commit"):
            ok.append(name)
            continue
        if not vm.git_dir_of(d):
            failed.append((name, "引擎目录不是 git 仓库，没法切版本"))
            continue
        _, cur = vm.read_head(d)
        if cur == e["commit"]:
            ok.append(name)
            continue
        c.log(f"[整合包] 训练引擎 {name} 切到 {e['commit'][:10]}")
        err = _pin(c, d, {"git": e.get("git") or vm.read_remote_url(d), "commit": e["commit"], "branch": e.get("branch")})
        (failed.append((name, err)) if err else ok.append(name))
    report["plugins_ok"] = ok
    report["plugins_failed"] = failed


_EXTRA_DRIVER = r"""
import sys, os
sys.path.insert(0, os.getcwd())
import jobs, extra_engines
class J(jobs.Job):
    def write(self, line):
        line = str(line).rstrip("\r\n")
        if line:
            print(line, flush=True)
    def set_progress(self, v, text=""):
        if text:
            print("[%3d%%] %s" % (int(v * 100), text), flush=True)
extra_engines.install(J("pack", "pack"), sys.argv[1])
"""


def _trainer_extra_install(c, name):
    import lora_trainer as lt
    py = lt.python_of(c.root)
    app_dir = os.path.join(c.root, "app")
    if not py or not os.path.isfile(os.path.join(app_dir, "extra_engines.py")):
        return "这个版本的训练器没有可选引擎的安装程序"
    rc = c.run(py, lt.PY_FLAGS + ["-c", _EXTRA_DRIVER, name], app_dir, env=lt.clean_env(), timeout=7200)
    return "" if rc == 0 else f"安装失败（退出码 {rc}），可以之后在训练器的「设置 → 可选训练引擎」里重装"


def _apply_pip(c, py, recorded, env_name, report):
    installed, _ = pip_freeze(py, c.env)
    todo = pr.pip_plan(recorded, installed)
    label = env_name or "主环境"
    if not todo:
        c.log(f"[整合包] pip 包（{label}）：版本都和整合包一致")
        return
    c.log(f"\n[整合包] pip 包（{label}）：{len(todo)} 个版本不同或没装，按整合包对齐（--no-deps，不连带升级别的包）")
    if _pip_batch(c, py, todo) == 0:
        c.log(f"[整合包] pip 包（{label}）已对齐")
        return
    c.log("[整合包] 整批装不上（有的包在这台电脑上装不了），对半拆开找出是哪几个 ...")
    bad = _pip_bisect(c, py, todo)
    if bad:
        report.setdefault("pip_failed", []).extend(f"{label}：{s}" for s in bad)
        c.log(f"[整合包] 这些包没对齐（{len(bad)} 个）：{'、'.join(bad[:30])}" + (" ……" if len(bad) > 30 else ""))
    else:
        c.log(f"[整合包] pip 包（{label}）已对齐")


def _pip_batch(c, py, specs, sources=None):
    """一批包写成临时 requirements 装（命令行太长 Windows 会报错）"""
    fd, req = tempfile.mkstemp(prefix="wwy_pack_", suffix=".txt")
    os.close(fd)
    try:
        with open(req, "w", encoding="utf-8") as f:
            f.write("\n".join(specs) + "\n")
        return _pip(c, py, ["-r", req], sources)
    finally:
        try:
            os.remove(req)
        except OSError:
            pass


def _pip_bisect(c, py, specs):
    """
    整批失败时对半拆开装，能装的那半直接装上，找出真正装不上的包：几百个包里只有几个坏的时
    只要十几轮，不用一个个装。拆的时候只用最快的一个源；剩下单个装不上的再把所有源都试一遍。
    返回最后还是装不上的包。
    """
    first = [c.mirrors[0]] if c.mirrors else [[]]
    bad, stack = [], [list(specs)]
    while stack:
        c.check()
        group = stack.pop()
        if _pip_batch(c, py, group, first) == 0:
            continue
        if len(group) == 1:
            if _pip(c, py, group) != 0:   # 换所有源再试一次（单个镜像缺包 / 同步慢）
                bad.append(group[0])
            continue
        mid = len(group) // 2
        stack += [group[mid:], group[:mid]]
    return bad


def _pip(c, py, args, sources=None):
    base = ["-s", "-m", "pip", "install", "--disable-pip-version-check", "--no-deps"]
    # 要现场编译的包（git+ 直链等）的 setup.py 常用默认编码读 README：中文 Windows 是 GBK，读 UTF-8 文件直接报错
    env = dict(c.env or os.environ, PYTHONUTF8="1")
    rc = 1
    for extra in (sources if sources is not None else [list(a) for a in c.mirrors] + [[]]):
        c.check()
        rc = c.run(py, base + list(args) + list(extra), os.path.dirname(py), env=env, timeout=3600)
        if rc == 0:
            break
    return rc


def _model_dest(c, key, name):
    """模型下到哪：开了共享模型库放库里，否则放新实例自己的目录"""
    import model_library as ml
    if c.kind == "lora_trainer":
        return os.path.join(c.root, "models", *os.path.dirname(name).split("/")) if "/" in name \
            else os.path.join(c.root, "models")
    sub = os.path.dirname(name)
    lib = _wa()._library_path(c.api)
    if lib and key in pr.BASE_MODEL_KEYS:
        if key == "diffusion_models":
            key = ml.dit_library_key(lib, c.cfg)
        d = ml.library_dir(lib, key)
    else:
        if key == "diffusion_models" and c.kind == "classic":
            key = "checkpoints"   # Classic 不认 diffusion_models 目录
        cat = next((x for x in ml.LIBRARY_CATEGORIES if x[0] == key), None)
        if not cat:
            return ""
        base = c.comfy_dir if c.kind == "comfyui" else c.root
        rel = cat[4] if c.kind == "comfyui" else (cat[3][0] if cat[3] else "")
        if not rel:
            return ""
        d = os.path.join(base, *rel.split("/"))
    return os.path.join(d, *sub.split("/")) if sub else d


def _model_urls(c, md, src):
    """[(说明, 地址, 额外请求头, 是否 C 站)]"""
    import model_catalog as mc
    out = []
    if src == "civitai" and md.get("civitai"):
        url = md.get("civitai_url") or mc.CIVITAI_DOWNLOAD.format(vid=int(md["civitai"]))
        out.append(("C 站", url, None, True))
    elif src == "hf" and md.get("hf") and ":" in md["hf"]:
        import h3_models as h3m
        import mirror_manager as mm
        repo, path = md["hf"].split(":", 1)
        use_mirror = mm.resolve_mode(c.cfg, lambda m: c.log(m + "\n"))
        for host, url, _ in mc.source_urls({"src": [{"hf": repo, "path": path}]}, h3m.base_order(use_mirror), use_mirror):
            out.append((host, url, None, False))
    elif src == "liblib" and md.get("liblib"):
        import liblib_client as lc
        token = (c.cfg.get("liblib_token") or c.api.cfg.get("liblib_token") or "").strip()
        if not token:
            raise RuntimeError("LiblibAI 下载要登录：先在「模型下载」页填上 liblib usertoken")
        mu, _, vu = md["liblib"].partition("/")
        info = lc.fetch_download_info(mu, vu or None)
        ver = info["versions"][info["chosen"]]
        if not ver.get("download_url"):
            raise RuntimeError("LiblibAI 没给这个版本的下载地址（会员 / 独家模型常见）")
        out.append(("LiblibAI", ver["download_url"], lc.download_headers(token), False))
    return out


def _apply_models(c, models, choice, report):
    import civitai_downloader as cd
    import model_catalog as mc
    todo = [(md, choice.get(f"{md['key']}/{md['name']}")) for md in models]
    todo = [(md, s) for md, s in todo if s]
    if not todo:
        return
    api_key = (c.api.cfg.get("civitai_api_key") or "").strip() or None
    ok, have, failed = [], [], []
    c.log(f"\n[整合包] 下载底模：{len(todo)} 个")
    for i, (md, src) in enumerate(todo):
        c.check()
        dest = _model_dest(c, md["key"], md["name"])
        fname = os.path.basename(md["name"])
        if not dest:
            failed.append((md["name"], "不知道放哪个文件夹"))
            continue
        found, size_ok = mc.existing_file([dest], fname, md.get("size") or None)
        if found and size_ok:
            c.log(f"[整合包] 已经有了，跳过：{found}")
            have.append(md["name"])
            continue
        c.log(f"[整合包] ({i + 1}/{len(todo)}) {fname}（{(md.get('size') or 0) / 1024 ** 3:.2f} GB）→ {dest}")
        os.makedirs(dest, exist_ok=True)
        err = "没有可用的下载地址"
        try:
            urls = _model_urls(c, md, src)
        except Exception as e:
            urls, err = [], str(e)
        done = False
        for host, url, headers, is_civ in urls:
            c.check()
            info = {"name": fname, "downloadUrl": url, "sizeKB": (md.get("size") or 0) // 1024,
                    "hashes": {"SHA256": md["sha256"]} if md.get("sha256") else {}}
            last = [0.0]

            def prog(d, t):
                now = time.monotonic()
                if now - last[0] >= 0.5 or (t and d >= t):
                    last[0] = now
                    c.api._emit("deploy", "progress", downloaded=d, total=t or md.get("size") or 0)
            try:
                kw = {"extra_headers": headers} if headers else {}
                final, hash_ok = cd.download_file(info, dest, api_key if is_civ else None, progress_cb=prog,
                                                  cancel_flag=c.cancelled, **kw)
                if hash_ok is False:
                    err = f"哈希校验没通过（文件另存为 {final}）"
                    break
                c.log(f"[整合包] 下好了：{final}" + ("（sha256 校验通过）" if hash_ok else ""))
                done = True
                break
            except Exception as e:
                err = str(e)
                if c.cancelled():
                    break
                c.log(f"[整合包] 从 {host} 下载出错：{e}；换下一条线路（已下的部分会续传）")
        c.api._emit("deploy", "progress", downloaded=0, total=0)
        if done:
            ok.append(md["name"])
        else:
            failed.append((md["name"], err))
            c.log(f"[整合包] {fname} 没下好：{err}")
    report["models_ok"] = ok
    report["models_have"] = have
    report["models_failed"] = failed


def _final_report(c, report):
    L = c.log
    L("\n[整合包] ===== 还原结果 =====")
    if report.get("python"):
        L("[整合包] Python：" + report["python"])
    if report.get("main"):
        L("[整合包] 本体：" + report["main"])
    if report.get("torch"):
        L("[整合包] torch：" + report["torch"])
    if "plugins_ok" in report:
        L(f"[整合包] 插件 / 引擎：装好 {len(report['plugins_ok'])} 个，没装上 {len(report.get('plugins_failed') or [])} 个")
        for name, why in report.get("plugins_failed") or []:
            L(f"         · {name}：{why}")
    for n in report.get("notes") or []:
        L("[整合包] " + n)
    bad = report.get("pip_failed") or []
    L(f"[整合包] pip 包：{'全部对齐' if not bad else f'{len(bad)} 个没对齐：'}")
    for b in bad[:50]:
        L(f"         · {b}")
    if "models_ok" in report:
        L(f"[整合包] 底模：下好 {len(report['models_ok'])} 个，本来就有 {len(report.get('models_have') or [])} 个，"
          f"没下好 {len(report.get('models_failed') or [])} 个")
        for name, why in report.get("models_failed") or []:
            L(f"         · {name}：{why}")
    report["kind"] = c.kind   # 完成弹窗按它说「插件」还是「训练引擎」
    c.api._emit("deploy", "pack_report", report=_jsonable(report))


def _jsonable(o):
    return json.loads(json.dumps(o, ensure_ascii=False, default=str))


def finish(api):
    """部署线程结束（成功 / 失败 / 取消）时清掉整合包任务"""
    api._deploy_pack = None


def export_start(api, sid, opts):
    if getattr(api, "_pack_busy", False):
        return {"ok": False, "error": "已经在制作整合包了，等它做完"}
    api._pack_busy = True
    api._pack_cancel = False
    try:
        api._spawn(lambda: export_flow(api, sid, opts or {}), name="pack-export")
    except Exception:
        api._pack_busy = False
        raise
    return {"ok": True}

