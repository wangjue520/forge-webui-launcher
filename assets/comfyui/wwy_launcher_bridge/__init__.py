"""
WWY 启动器桥接插件（由启动器自动放进 custom_nodes，启动器更新时一起更新）

新版 ComfyUI 网页发现工作流缺节点 / 缺模型时直接拦下、不让运行，后端日志里什么都没有，
启动器的报错诊断就看不到。这个插件的网页部分（web/wwy_bridge.js）在每次加载工作流时
自己检查一遍，结果发到这里，打成一行 [WWY诊断] 日志，启动器读日志接住。

· 不加任何节点；只有一个 POST /wwy_bridge/report 接口
· 当前工作流另存一份到 user/wwy_bridge/current_workflow.json，给启动器的 AI 诊断读
· 不想要：在 ComfyUI-Manager 里停用，或把文件夹改名成 wwy_launcher_bridge.disabled（启动器不会再放回来）
"""
import json
import os

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

VERSION = 1

# 下拉框里的模型找不到对应目录时，按输入名猜
_INPUT_FOLDERS = {
    "ckpt_name": "checkpoints", "lora_name": "loras", "vae_name": "vae", "unet_name": "diffusion_models",
    "clip_name": "text_encoders", "clip_name1": "text_encoders", "clip_name2": "text_encoders",
    "clip_name3": "text_encoders", "clip_name4": "text_encoders", "control_net_name": "controlnet",
    "style_model_name": "style_models", "clip_vision_name": "clip_vision", "gligen_name": "gligen",
    "hypernetwork_name": "hypernetworks", "model_name": "upscale_models", "photomaker_model_name": "photomaker",
}

_last = {"sig": None}


def _combo_values(node_type, input_name):
    import nodes
    cls = nodes.NODE_CLASS_MAPPINGS.get(node_type)
    if cls is None:
        return None
    spec = cls.INPUT_TYPES()
    for sect in ("required", "optional"):
        item = (spec.get(sect) or {}).get(input_name)
        if not item:
            continue
        if isinstance(item[0], (list, tuple)):
            return list(item[0])
        if item[0] == "COMBO" and len(item) > 1 and isinstance(item[1], dict):
            return list(item[1].get("options") or [])
    return None


def _folder_for(node_type, input_name):
    """这个下拉框列的是哪个模型目录：拿节点的选项列表跟 folder_paths 各目录的文件列表比"""
    import folder_paths
    try:
        values = _combo_values(node_type, input_name)
    except Exception:
        values = None
    guess = _INPUT_FOLDERS.get(input_name)
    if values:
        want = set(values)
        best, best_score = None, 0.0
        for name in list(folder_paths.folder_names_and_paths):
            try:
                have = set(folder_paths.get_filename_list(name))
            except Exception:
                continue
            if not have:
                continue
            score = len(want & have) / len(want | have)
            if score > best_score or (score == best_score and name == guess):
                best, best_score = name, score
        if best and best_score >= 0.6:
            return best
    return guess


def _folder_dirs(folder):
    import folder_paths
    try:
        return [p for p in folder_paths.get_folder_paths(folder) if p]
    except Exception:
        return []


def _save_workflow(data):
    try:
        import folder_paths
        d = os.path.join(folder_paths.get_user_directory(), "wwy_bridge")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "current_workflow.json"), "w", encoding="utf-8") as f:
            json.dump({"name": data.get("workflow_name") or "", "workflow": data.get("workflow")}, f,
                      ensure_ascii=False, indent=1)
    except Exception:
        pass


def handle_report(data):
    """网页发来的检查结果 → 需要时打一行日志。返回打出来的那行（没打返回 None）"""
    nodes_missing = sorted({str(n.get("type") if isinstance(n, dict) else n) for n in data.get("missing_nodes") or []})
    nodes_info = {}
    for n in data.get("missing_nodes") or []:
        if isinstance(n, dict) and n.get("cnr_id"):
            nodes_info[str(n.get("type"))] = str(n["cnr_id"])
    models = []
    for m in data.get("missing_models") or []:
        name = str(m.get("name") or "")
        if not name:
            continue
        folder = m.get("directory") or _folder_for(str(m.get("node_type") or ""), str(m.get("input") or ""))
        models.append({"name": name, "node_type": m.get("node_type"), "node_title": m.get("node_title"),
                       "input": m.get("input"), "folder": folder, "dirs": _folder_dirs(folder) if folder else [],
                       "url": m.get("url") or ""})
    _save_workflow(data)
    report = {"wf": str(data.get("workflow_name") or ""), "nodes": nodes_missing, "cnr": nodes_info,
              "models": models}
    sig = json.dumps([nodes_missing, sorted(m["name"] for m in models)], ensure_ascii=False)
    first = _last["sig"] is None
    if sig == _last["sig"] or (first and not nodes_missing and not models):
        _last["sig"] = sig
        return None
    _last["sig"] = sig
    if nodes_missing or models:
        parts = ([f"{len(nodes_missing)} 个节点"] if nodes_missing else []) + \
                ([f"{len(models)} 个模型"] if models else [])
        head = f"工作流「{report['wf']}」缺 {'、'.join(parts)}，运行会被网页拦下或在那个节点报错（启动器的「诊断」里能一键处理）"
    else:
        head = f"工作流「{report['wf']}」检查通过"
    line = f"[WWY诊断] {head} {json.dumps(report, ensure_ascii=False)}"
    print(line, flush=True)
    return line


try:
    from aiohttp import web
    from server import PromptServer

    @PromptServer.instance.routes.post("/wwy_bridge/report")
    async def _report(request):
        try:
            data = await request.json()
            handle_report(data if isinstance(data, dict) else {})
        except Exception as e:
            print(f"[WWY诊断] 桥接插件处理检查结果出错：{e}", flush=True)
        return web.json_response({"ok": True, "version": VERSION})
except ImportError:         # 不在 ComfyUI 里（单测）
    pass
except Exception as e:      # 接口变了：网页那边发不出来而已，不影响 ComfyUI
    print(f"[WWY诊断] 桥接插件没能注册接口：{e}", flush=True)
