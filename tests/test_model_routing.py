"""
DiT 模型（Anima / Flux / Wan / MiniMax-H3）分类路由的回归测试：
识别（safetensors + GGUF）/ 共享库分类 / 模型下载路由 / 拖拽上传建议 / 合并与库内归位。

背景：ComfyUI 的 UNet / GGUF 加载器只扫 diffusion_models（和老的 unet），WebUI 则把
只有 DiT 的主模型和大模型放一起。共享模型库要同时喂两边，放错就在某一边「消失」。
用合成的假文件跑，不需要真模型。运行：python -m unittest tests.test_model_routing
"""
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest

import safetensors_meta as sm  # noqa: E402
import model_library as ml     # noqa: E402
import civitai_downloader as cd  # noqa: E402
import config_manager as cm    # noqa: E402
import webview_api as wa       # noqa: E402

FAIL = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(f"{name}: got={got!r} want={want!r}")


def write_st(path, keys, pad=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hdr = {k: {"dtype": "F16", "shape": [1], "data_offsets": [0, 2]} for k in keys}
    raw = json.dumps(hdr).encode()
    with open(path, "wb") as f:
        f.write(struct.pack("<Q", len(raw)) + raw + b"\0\0" + os.urandom(pad))
    return path


def gstr(s):
    b = s.encode()
    return struct.pack("<Q", len(b)) + b


def write_gguf(path, arch, tensors, vocab=50, pad=0, truncate=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kv = []
    kv.append(gstr("general.architecture") + struct.pack("<I", 8) + gstr(arch))
    kv.append(gstr("general.file_type") + struct.pack("<I", 4) + struct.pack("<I", 15))
    arr = struct.pack("<IQ", 8, vocab) + b"".join(gstr(f"tok{i}") for i in range(vocab))
    kv.append(gstr("tokenizer.ggml.tokens") + struct.pack("<I", 9) + arr)
    farr = struct.pack("<IQ", 6, 100) + b"\0" * 400
    kv.append(gstr("tokenizer.ggml.scores") + struct.pack("<I", 9) + farr)
    ti = b""
    for t in tensors:
        ti += gstr(t) + struct.pack("<I", 2) + struct.pack("<QQ", 4, 4) + struct.pack("<I", 1) + struct.pack("<Q", 0)
    data = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kv)) + b"".join(kv) + ti
    if truncate:
        data = data[: len(data) // 2]
    with open(path, "wb") as f:
        f.write(data + os.urandom(pad))
    return path


ANIMA = ["net.blocks.0.self_attn.q_proj.weight", "net.blocks.0.mlp.layer1.weight", "net.x_embedder.proj.1.weight"]
SDXL = ["model.diffusion_model.input_blocks.0.0.weight", "conditioner.embedders.1.model.ln_final.weight",
        "first_stage_model.decoder.conv_in.weight"]
FLUX_DIT = ["model.diffusion_model.double_blocks.0.img_attn.qkv.weight"]
LORA = ["lora_unet_down_blocks_0.lora_down.weight", "lora_unet_down_blocks_0.lora_up.weight"]
TE = ["model.embed_tokens.weight", "model.layers.0.self_attn.q_proj.weight"]
VAE = ["encoder.conv_in.weight", "decoder.conv_out.weight", "encoder.down.0.weight"]

def run_scenario(tmp):
    # ---------------- 1. 识别 ----------------
    d = os.path.join(tmp, "cls")
    check("anima dit", sm.classify_model_role(write_st(os.path.join(d, "anima.safetensors"), ANIMA)), "DiffusionModel")
    check("sdxl full", sm.classify_model_role(write_st(os.path.join(d, "sdxl.safetensors"), SDXL)), "Checkpoint")
    check("flux dit", sm.classify_model_role(write_st(os.path.join(d, "flux.safetensors"), FLUX_DIT)), "DiffusionModel")
    check("lora", sm.classify_model_role(write_st(os.path.join(d, "l.safetensors"), LORA)), "LoRA")
    check("te", sm.classify_model_role(write_st(os.path.join(d, "qwen.safetensors"), TE)), "TextEncoder")
    check("vae", sm.classify_model_role(write_st(os.path.join(d, "v.safetensors"), VAE)), "VAE")
    g_dit = write_gguf(os.path.join(d, "minimax_h3_fl2va_pruned-Q4_K.gguf"), "minimax_h3",
                       ["blocks.0.attn.qkv.weight", "blocks.0.ffn.w1.weight"])
    g_te = write_gguf(os.path.join(d, "qwen3vl_32b_minimax_h3-Q4_K_M.gguf"), "qwen3vl",
                      ["token_embd.weight", "blk.0.attn_q.weight"], vocab=5000)
    g_qi = write_gguf(os.path.join(d, "qwen-image-Q4.gguf"), "qwen_image", ["transformer_blocks.0.attn.to_q.weight"])
    g_te2 = write_gguf(os.path.join(d, "umt5.gguf"), "t5encoder", ["enc.blk.0.attn_q.weight"])
    g_bad = write_gguf(os.path.join(d, "broken.gguf"), "flux", ["double_blocks.0.x"], truncate=True)
    check("gguf dit", sm.classify_model_role(g_dit), "DiffusionModel")
    check("gguf te (llama.cpp names)", sm.classify_model_role(g_te), "TextEncoder")
    check("gguf qwen_image is dit", sm.classify_model_role(g_qi), "DiffusionModel")
    check("gguf t5 arch", sm.classify_model_role(g_te2), "TextEncoder")
    check("gguf truncated", sm.classify_model_role(g_bad), None)
    check("cd.classify_file delegates gguf", cd.classify_file(g_dit), "DiffusionModel")
    check("detect_kind dit", ml.detect_kind(os.path.join(d, "anima.safetensors")), "dit")
    check("detect_kind full", ml.detect_kind(os.path.join(d, "sdxl.safetensors")), "full")
    check("detect_kind gguf te", ml.detect_kind(g_te), "te")
    check("is_dit_base anima", cd.is_dit_base("Anima"), True)
    check("is_dit_base illustrious", cd.is_dit_base("Illustrious"), False)

    # ---------------- 2. 共享库分类 ----------------
    def fake_webui(root, flags):
        os.makedirs(os.path.join(root, "modules"), exist_ok=True)
        with open(os.path.join(root, "modules", "cmd_args.py"), "w") as f:
            f.write("\n".join(f'parser.add_argument("{x}")' for x in flags))
        return root

    lib = os.path.join(tmp, "lib")
    os.makedirs(lib)
    neo = fake_webui(os.path.join(tmp, "neo"), ["--ckpt-dirs", "--lora-dirs", "--vae-dirs", "--text-encoder-dirs"])
    classic = fake_webui(os.path.join(tmp, "classic"), ["--ckpt-dir", "--lora-dir", "--vae-dir"])
    comfy = os.path.join(tmp, "comfy")
    os.makedirs(os.path.join(comfy, "models", "diffusion_models"))
    os.makedirs(os.path.join(comfy, "models", "unet"))
    with open(os.path.join(comfy, "main.py"), "w") as f:
        f.write("# comfy\n")
    neo_cfg = {"webui_branch": "neo2", "webui_root": neo}
    classic_cfg = {"webui_branch": "classic", "webui_root": classic}
    comfy_cfg = {"webui_branch": "comfyui", "webui_root": comfy}
    ml._FLAG_CACHE.clear()
    check("dit key neo", ml.dit_library_key(lib, neo_cfg), "diffusion_models")
    check("dit key classic", ml.dit_library_key(lib, classic_cfg), "checkpoints")
    check("dit key comfy", ml.dit_library_key(lib, comfy_cfg), "diffusion_models")
    check("dit key no root", ml.dit_library_key(lib, {"webui_branch": "neo2"}), "checkpoints")
    check("dit key all w/ classic", ml.dit_library_key_all(lib, [neo_cfg, classic_cfg]), "checkpoints")

    # ---------------- 3. 下载路由 ----------------
    def api(cfg, lib_on):
        c = dict(cfg)
        c["model_library_enabled"] = lib_on
        c["model_library_path"] = lib
        return types.SimpleNamespace(cfg=c)

    def rel(p):
        return os.path.relpath(p, tmp).replace(os.sep, "/")

    check("role_dest dit neo+lib", rel(wa._role_dest(api(neo_cfg, True), "DiffusionModel", "models/Stable-diffusion")[0]),
          "lib/diffusion_models")
    check("role_dest dit classic+lib", rel(wa._role_dest(api(classic_cfg, True), "DiffusionModel", "x")[0]),
          "lib/checkpoints")
    check("role_dest dit neo no lib", rel(wa._role_dest(api(neo_cfg, False), "DiffusionModel", "x")[0]),
          "neo/models/Stable-diffusion")
    check("role_dest dit comfy no lib", rel(wa._role_dest(api(comfy_cfg, False), "DiffusionModel", "x")[0]),
          "comfy/models/diffusion_models")
    check("role_dest ckpt neo+lib", rel(wa._role_dest(api(neo_cfg, True), "Checkpoint", "x")[0]), "lib/checkpoints")

    h3_cfg = dict(neo_cfg, webui_variant="h3")
    os.makedirs(os.path.join(lib, "checkpoints"), exist_ok=True)   # 旧版放错的位置（存在时才会去找）
    fl2va = wa.h3m.CATALOG_BY_ID["fl2va"]
    dest, disp, look = wa._h3_dirs(api(h3_cfg, True), fl2va)
    check("h3 fl2va dest lib", rel(dest), "lib/diffusion_models")
    check("h3 looks legacy lib/checkpoints", "lib/checkpoints" in [rel(x) for x in look], True)
    check("h3 looks instance", "neo/models/Stable-diffusion" in [rel(x) for x in look], True)
    dest, _d, _l = wa._h3_dirs(api(h3_cfg, False), fl2va)
    check("h3 fl2va dest no lib", rel(dest), "neo/models/Stable-diffusion")
    dest, _d, _l = wa._h3_dirs(api(h3_cfg, True), wa.h3m.CATALOG_BY_ID["te"])
    check("h3 te dest lib", rel(dest), "lib/text_encoders")
    dest, _d, _l = wa._h3_dirs(api(h3_cfg, True), wa.h3m.CATALOG_BY_ID["controlnet"])
    check("h3 controlnet stays in instance", rel(dest), "neo/models/model_patches")

    # 旧位置的 H3 主模型：下载时挪到 diffusion_models
    old = write_gguf(os.path.join(lib, "checkpoints", "minimax_h3_fl2va_pruned-Q4_K.gguf"), "minimax_h3",
                     ["blocks.0.x"])
    with open(os.path.splitext(old)[0] + ".civitai.info", "w") as f:
        f.write("{}")
    logs = []
    new = wa._h3_relocate(api(h3_cfg, True), old, os.path.join(lib, "diffusion_models"), logs.append)
    check("h3 relocate moved", rel(new), "lib/diffusion_models/minimax_h3_fl2va_pruned-Q4_K.gguf")
    check("h3 relocate sidecar", os.path.exists(os.path.splitext(new)[0] + ".civitai.info"), True)
    inst_file = write_gguf(os.path.join(neo, "models", "Stable-diffusion", "a.gguf"), "flux", ["x"])
    check("h3 relocate leaves instance file", wa._h3_relocate(api(h3_cfg, True), inst_file,
                                                              os.path.join(lib, "diffusion_models"), logs.append), inst_file)
    os.remove(inst_file)
    shutil.rmtree(os.path.join(lib, "diffusion_models"))
    os.makedirs(os.path.join(lib, "checkpoints"), exist_ok=True)

    # ---------------- 4. 上传建议 ----------------
    def suggest(cfg, lib_on, cat_key, counts):
        a = api(cfg, lib_on)
        if lib_on:
            cats = [{"label": c["label"], "path": c["path"], "is_lora": c["is_lora"], "key": c["key"]}
                    for c in ml.library_categories(lib)]
        else:
            cats = [{"label": l, "path": "", "is_lora": isl, "key": ml.library_key_for_folder(r)}
                    for l, r, isl in wa.BASE_CATEGORIES]
        a._models_categories = cats
        cat = next(c for c in cats if c["key"] == cat_key)
        i, kind = wa._import_suggest(a, cat, dict({"lora": 0, "full": 0, "dit": 0, "te": 0, "vae": 0}, **counts))
        return (cats[i]["key"] if i is not None else None), kind

    check("lib: dit into checkpoints", suggest(neo_cfg, True, "checkpoints", {"dit": 2}), ("diffusion_models", "dit"))
    check("lib: full into diffusion", suggest(neo_cfg, True, "diffusion_models", {"full": 1}), ("checkpoints", "full"))
    check("lib: dit into diffusion ok", suggest(neo_cfg, True, "diffusion_models", {"dit": 1}), (None, ""))
    check("lib classic: dit into checkpoints ok", suggest(classic_cfg, True, "checkpoints", {"dit": 1}), (None, ""))
    check("lib: te into checkpoints", suggest(neo_cfg, True, "checkpoints", {"te": 1}), ("text_encoders", "te"))
    check("lib: lora into vae", suggest(neo_cfg, True, "vae", {"lora": 1}), ("loras", "lora"))
    check("lib: dit into vae (no nag)", suggest(neo_cfg, True, "vae", {"dit": 1}), (None, ""))
    check("lib: mixed dit+te", suggest(neo_cfg, True, "checkpoints", {"dit": 1, "te": 1}), (None, ""))
    check("webui: dit into Stable-diffusion ok", suggest(neo_cfg, False, "checkpoints", {"dit": 1}), (None, ""))
    check("webui: full into Lora", suggest(neo_cfg, False, "loras", {"full": 1}), ("checkpoints", "full"))

    # ---------------- 5. 合并 ----------------
    shutil.rmtree(lib)
    os.makedirs(lib)
    # 库里已有：checkpoints 里放错的 anima、正常的 sdxl
    lib_anima = write_st(os.path.join(lib, "checkpoints", "anima_lib.safetensors"), ANIMA, pad=4096)
    write_st(os.path.join(lib, "checkpoints", "sdxl_lib.safetensors"), SDXL, pad=100)
    # Neo：Stable-diffusion 里 sdxl + H3 gguf + anima(与库里那份重复) + 文本编码器
    write_st(os.path.join(neo, "models", "Stable-diffusion", "illu.safetensors"), SDXL, pad=200)
    write_gguf(os.path.join(neo, "models", "Stable-diffusion", "minimax_h3_fl2va_pruned-Q4_K.gguf"), "minimax_h3",
               ["blocks.0.x"], pad=300)
    shutil.copy2(lib_anima, os.path.join(neo, "models", "Stable-diffusion", "anima_copy.safetensors"))
    write_gguf(os.path.join(neo, "models", "text_encoder", "qwen3vl_32b_minimax_h3-Q4_K_M.gguf"), "qwen3vl",
               ["token_embd.weight"], pad=50)
    with open(os.path.join(neo, "models", "Stable-diffusion", "illu.preview.png"), "wb") as f:
        f.write(b"png")
    # Classic：Stable-diffusion 里的 flux DiT 必须留在 checkpoints（Classic 挂不上 diffusion_models）
    write_st(os.path.join(classic, "models", "Stable-diffusion", "flux_dev.safetensors"), FLUX_DIT, pad=500)
    # ComfyUI：diffusion_models + 老的 unet 目录都有东西；checkpoints 里放的 dit 不替用户改
    write_st(os.path.join(comfy, "models", "diffusion_models", "wan.safetensors"), ANIMA, pad=600)
    write_st(os.path.join(comfy, "models", "unet", "old_unet.safetensors"), FLUX_DIT, pad=700)
    write_st(os.path.join(comfy, "models", "checkpoints", "comfy_dit.safetensors"), ANIMA, pad=800)

    instances = [("n", "Neo", neo_cfg), ("c", "Classic", classic_cfg), ("y", "Comfy", comfy_cfg)]
    ml._FLAG_CACHE.clear()
    plan = ml.plan_merge(instances, lib, cm.comfy_layout)
    m = {os.path.basename(mv["src"]): (rel(mv["dst"]), mv["dup"], mv["recat"]) for mv in plan["moves"]}
    # 有 Classic 在：库内归位不做（Classic 读不到库里的 diffusion_models）
    check("merge: lib anima stays (classic present)", "anima_lib.safetensors" in m, False)
    check("merge: neo sdxl -> checkpoints", m["illu.safetensors"][:2], ("lib/checkpoints/illu.safetensors", False))
    check("merge: neo h3 gguf -> diffusion_models", m["minimax_h3_fl2va_pruned-Q4_K.gguf"],
          ("lib/diffusion_models/minimax_h3_fl2va_pruned-Q4_K.gguf", False, True))
    check("merge: neo anima dup of lib copy (classic present => lib copy stays in checkpoints)",
          m["anima_copy.safetensors"][:2], ("lib/checkpoints/anima_lib.safetensors", True))
    check("merge: neo te -> text_encoders", m["qwen3vl_32b_minimax_h3-Q4_K_M.gguf"][0],
          "lib/text_encoders/qwen3vl_32b_minimax_h3-Q4_K_M.gguf")
    check("merge: classic flux stays checkpoints", m["flux_dev.safetensors"][0], "lib/checkpoints/flux_dev.safetensors")
    check("merge: comfy unet merged", m["old_unet.safetensors"][0], "lib/diffusion_models/old_unet.safetensors")
    check("merge: comfy checkpoints dit untouched category", m["comfy_dit.safetensors"][0][:15], "lib/checkpoints")

    # 没有 Classic：库内归位 + 重复判定
    instances2 = [("n", "Neo", neo_cfg), ("y", "Comfy", comfy_cfg)]
    plan2 = ml.plan_merge(instances2, lib, cm.comfy_layout)
    m2 = {os.path.basename(mv["src"]): (rel(mv["dst"]), mv["dup"], mv["recat"], mv["iid"]) for mv in plan2["moves"]}
    check("merge2: lib anima relocated", m2["anima_lib.safetensors"],
          ("lib/diffusion_models/anima_lib.safetensors", False, True, ""))
    check("merge2: neo anima dup -> relocated lib copy", m2["anima_copy.safetensors"][:3],
          ("lib/diffusion_models/anima_lib.safetensors", True, True))
    check("merge2: lib sdxl not moved", "sdxl_lib.safetensors" in m2, False)
    check("merge2: recat count", plan2["recat"], 3)

    res = ml.run_merge(plan2, lib)
    check("run_merge no failures", res["failed"], [])
    check("file: lib anima in diffusion_models", os.path.exists(os.path.join(lib, "diffusion_models", "anima_lib.safetensors")), True)
    check("file: h3 in diffusion_models", os.path.exists(os.path.join(lib, "diffusion_models", "minimax_h3_fl2va_pruned-Q4_K.gguf")), True)
    check("file: preview followed", os.path.exists(os.path.join(lib, "checkpoints", "illu.preview.png")), True)
    check("file: neo stable-diffusion emptied", sorted(os.listdir(os.path.join(neo, "models", "Stable-diffusion"))), [])
    undo = ml.undo_journal(res["journal"])
    check("undo restored", undo["failed"], [])
    check("undo: lib anima back in checkpoints", os.path.exists(lib_anima), True)

    # ComfyUI yaml 会把 diffusion_models 挂成 unet + diffusion_models
    y = ml.comfy_yaml_text(lib)
    check("yaml has unet->diffusion_models", "unet: \"diffusion_models\"" in y, True)


class ModelRoutingTests(unittest.TestCase):
    def test_dit_routing(self):
        FAIL.clear()
        tmp = tempfile.mkdtemp(prefix="routing_")
        try:
            run_scenario(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(FAIL, [], "\n".join(FAIL))


if __name__ == "__main__":
    unittest.main()
