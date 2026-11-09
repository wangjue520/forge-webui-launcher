"""
build_model_catalog.py — 生成「模型下载 → 常用模型一键下载」用的目录 app/model_catalog.json

下面的 SPEC 只写「有哪些模型、每个模型有哪些版本、每个版本要哪几个文件、每个文件有哪些精度 / 量化」，
文件大小和 sha256 由本脚本联网去 HuggingFace（和 Civitai）查出来填进去，同时检查每个路径都真实存在：
    python tools/build_model_catalog.py            # 生成 / 刷新 app/model_catalog.json
    python tools/build_model_catalog.py --check    # 只检查，不写文件

加新模型：照着下面的写法在 SPEC 里加一条，再跑一次本脚本。
  - 只用不需要登录的仓库（Comfy-Org 重新打包的、unsloth / QuantStack / city96 的 GGUF 等）：
    官方仓库很多要先登录同意协议（gated），启动器匿名下载会 401
  - gguf_auto(仓库, "文件名-{q}.gguf") 会按 Q8 / Q6 / Q5 / Q4 / Q3 / Q2 各挑一个仓库里实际有的档位
  - 一个文件既在 Civitai 又在 HF（别人转存）时写 civ(版本号) + hf(...)：以 Civitai 的 sha256 为准，
    HF 那份哈希对不上会被丢掉，对得上就作为国内能走镜像的线路
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "app", "model_catalog.json")
UA = {"User-Agent": "WWY-Launcher-CatalogBuilder/1.0"}


# ---------------------------------------------------------------- 写 SPEC 用的小工具

def hf(repo, path):
    return {"hf": repo, "path": path}


def civ(version_id):
    return {"civitai": int(version_id)}


def o(fmt, *src, label=None, quant=None, file=None, id=None, note=""):
    """一个可选的文件（某种精度 / 量化）。fmt：bf16 fp16 fp32 fp8 int8 mxfp8 nvfp4 fp4 w4a8 w6a8 gguf"""
    return {"fmt": fmt, "src": list(src), "label": label, "quant": quant, "file": file, "id": id, "note": note}


def gguf_auto(repo, tmpl, levels=(8, 6, 5, 4, 3, 2), extra=()):
    """按档位自动挑仓库里实际存在的 GGUF；extra 里写额外要的档位名（比如 "BF16"）"""
    return {"gguf_auto": repo, "tmpl": tmpl, "levels": list(levels), "extra": list(extra)}


def c(id, label, role, opts, required=True, default=None, desc="", folder=None, link=None):
    """一个组件（主模型 / 文本编码器 / VAE / LoRA…），opts 里放可选的精度。
    link：同一个 link 的可选组件一起勾 / 一起取消（Wan 的高噪、低噪加速 LoRA 要成对用）"""
    return {"id": id, "label": label, "role": role, "options": opts, "required": required,
            "default": required if default is None else default, "desc": desc, "folder": folder, "link": link}


def v(id, label, comps, note="", default=False, ui=None):
    return {"id": id, "label": label, "note": note, "default": default, "ui": ui, "components": comps}


def m(id, name, group, desc, versions, ui, tips="", license="", link="", ram_factor=None):
    """ram_factor：运算时比文件大小多吃多少内存（挑默认精度时用，H3 这种视频模型要大一些）"""
    return {"id": id, "name": name, "group": group, "desc": desc, "tips": tips, "license": license,
            "link": link, "ui": ui, "versions": versions, "ram_factor": ram_factor}


GGUF_LEVELS = {
    8: ("Q8_0",),
    6: ("Q6_K", "UD-Q6_K_XL"),
    5: ("Q5_K_M", "Q5_K_S", "Q5_0", "UD-Q5_K_XL"),
    4: ("Q4_K_M", "Q4_K", "Q4_K_S", "Q4_0", "UD-Q4_K_XL"),
    3: ("Q3_K_M", "Q3_K", "Q3_K_S", "Q3_K_L", "UD-Q3_K_XL"),
    2: ("Q2_K", "Q2_K_M", "UD-Q2_K_XL"),
}

ALL_UI = ["comfyui", "neo", "classic", "a1111"]
NEO_COMFY = ["comfyui", "neo"]

# ---------------------------------------------------------------- 常用的共享组件

QWEN_IMAGE_VAE = c("vae", "VAE（Qwen-Image VAE）", "VAE",
                   [o("bf16", hf("Comfy-Org/Qwen-Image", "split_files/vae/qwen_image_vae.safetensors"), label="原版")])
FLUX_AE = c("vae", "VAE（FLUX ae）", "VAE",
            [o("bf16", hf("Comfy-Org/Lumina-Image-2", "split_files/vae/ae.safetensors"), label="原版")])
FLUX2_VAE = c("vae", "VAE（FLUX.2 VAE）", "VAE",
              [o("bf16", hf("Comfy-Org/Flux-2-Klein-4B", "split_files/vae/flux2-vae.safetensors"), label="原版")])
SDXL_VAE = c("vae", "SDXL VAE（fp16 修正版）", "VAE",
             [o("fp16", hf("madebyollin/sdxl-vae-fp16-fix", "sdxl_vae.safetensors"), label="fp16 fix")],
             required=False, default=False, desc="可选：大模型已内置 VAE，出图发灰 / 出黑图时再换这个")
FLUX_CLIP_L = c("clip_l", "文本编码器 CLIP-L", "TextEncoder",
                [o("fp16", hf("comfyanonymous/flux_text_encoders", "clip_l.safetensors"), label="原版")])
FLUX_T5 = c("t5", "文本编码器 T5-XXL", "TextEncoder", [
    o("fp16", hf("comfyanonymous/flux_text_encoders", "t5xxl_fp16.safetensors"), label="fp16 原版"),
    o("fp8", hf("comfyanonymous/flux_text_encoders", "t5xxl_fp8_e4m3fn_scaled.safetensors"),
      label="fp8 scaled", id="fp8_scaled"),
    o("fp8", hf("comfyanonymous/flux_text_encoders", "t5xxl_fp8_e4m3fn.safetensors"), label="fp8"),
    gguf_auto("city96/t5-v1_1-xxl-encoder-gguf", "t5-v1_1-xxl-encoder-{q}.gguf", levels=(8, 6, 5, 4, 3)),
])
QWEN25VL_TE = c("te", "文本编码器 Qwen2.5-VL 7B", "TextEncoder", [
    o("bf16", hf("Comfy-Org/Qwen-Image", "split_files/text_encoders/qwen_2.5_vl_7b.safetensors")),
    o("fp8", hf("Comfy-Org/Qwen-Image", "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors")),
    o("nvfp4", hf("Comfy-Org/Qwen-Image", "split_files/text_encoders/qwen_2.5_vl_7b_nvfp4.safetensors")),
])
UMT5_TE = c("te", "文本编码器 umT5-XXL", "TextEncoder", [
    o("fp16", hf("Comfy-Org/Wan-2.2", "split_files/text_encoders/umt5_xxl_fp16.safetensors")),
    o("fp8", hf("Comfy-Org/Wan-2.2", "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors")),
    gguf_auto("city96/umt5-xxl-encoder-gguf", "umt5-xxl-encoder-{q}.gguf", levels=(8, 6, 5, 4, 3)),
])


def sdxl_ckpt(*opts):
    return c("main", "大模型（SDXL 单文件）", "Checkpoint", list(opts),
             desc="VAE 和文本编码器都已经打包在里面，下这一个就能出图")


# ---------------------------------------------------------------- 模型清单

def anima_main(name, fp8=None, mxfp8=None, nvfp4=None, gguf=None):
    opts = [o("bf16", hf("circlestone-labs/Anima", f"split_files/diffusion_models/{name}.safetensors"),
              label="bf16 原版")]
    if fp8:
        opts.append(o("fp8", hf("Bedovyy/Anima-FP8", fp8)))
    if mxfp8:
        opts.append(o("mxfp8", hf("Bedovyy/Anima-FP8", mxfp8)))
    if nvfp4:
        opts.append(o("nvfp4", hf("Bedovyy/Anima-FP8", nvfp4), label="nvfp4 mixed"))
    if gguf:
        opts.append(gguf)
    return c("main", "主模型（DiT）", "DiffusionModel", opts)


def anima_version(id, label, main, note="", default=False):
    return v(id, label, [
        main,
        c("te", "文本编码器 Qwen3 0.6B", "TextEncoder",
          [o("bf16", hf("circlestone-labs/Anima", "split_files/text_encoders/qwen_3_06b_base.safetensors"))]),
        c("vae", "VAE（Qwen-Image VAE）", "VAE",
          [o("bf16", hf("circlestone-labs/Anima", "split_files/vae/qwen_image_vae.safetensors"), label="原版")]),
        c("turbo_lora", "Turbo 加速 LoRA", "LoRA",
          [o("bf16", hf("circlestone-labs/Anima-Official-LoRAs", "anima-turbo-lora-v0.2.safetensors"), label="v0.2")],
          required=False, default=False, desc="可选：配 Base / Aesthetic 用，少步数出图"),
        c("boost_lora", "高分辨率画质增强 LoRA", "LoRA",
          [o("bf16", hf("circlestone-labs/Anima-Official-LoRAs", "anima-highres-aesthetic-boost.safetensors"),
             label="官方")],
          required=False, default=False, desc="可选：官方出的高分辨率画质增强"),
    ], note=note, default=default)


ANIMA = m("anima", "Anima", "anime",
          "CircleStone Labs 与 Comfy Org 合作的 2B 二次元模型（Cosmos 架构），Danbooru 标签、自然语言都认，画师标签前加 @",
          [
              anima_version("aesthetic-v1.1", "Aesthetic v1.1",
                            anima_main("anima-aesthetic-v1.1", fp8="anima-aesthetic-v1.1-fp8.safetensors"),
                            note="画质微调版，出图推荐", default=True),
              anima_version("turbo-v1.1", "Turbo v1.1",
                            anima_main("anima-turbo-v1.1", fp8="anima-turbo-v1.1-fp8.safetensors",
                                       gguf=gguf_auto("vanes430/Anima-Turbo-V1.1-GGUF", "anima-turbo-v1.1-{q}.gguf",
                                                      levels=(8, 6, 5, 4, 3))),
                            note="蒸馏加速版：8–12 步、CFG 1"),
              anima_version("base-v1.0", "Base v1.0",
                            anima_main("anima-base-v1.0", fp8="anima-base-v1.0-fp8.safetensors",
                                       mxfp8="anima-base-v1.0-mxfp8.safetensors",
                                       gguf=gguf_auto("Abiray/Anima-base-v1.0-GGUF", "anima-base-v1.0-{q}.gguf",
                                                      levels=(8, 6, 5, 4, 3))),
                            note="未经画质微调的底模，练 LoRA 用这个"),
              anima_version("aesthetic-v1.0", "Aesthetic v1.0", anima_main("anima-aesthetic-v1.0"), note="旧版"),
              anima_version("aesthetic-v1.0b", "Aesthetic v1.0b", anima_main("anima-aesthetic-v1.0b"),
                            note="旧版：只做了画质微调、没合并风格稳定 LoRA"),
              anima_version("turbo-v1.0", "Turbo v1.0",
                            anima_main("anima-turbo-v1.0", fp8="anima-turbo-v1.0-fp8.safetensors"), note="旧版"),
              anima_version("preview3-base", "Preview 3 Base",
                            anima_main("anima-preview3-base", fp8="anima-preview3-base-fp8.safetensors",
                                       mxfp8="anima-preview3-base-mxfp8.safetensors",
                                       nvfp4="anima-preview3-base-nvfp4mixed.safetensors",
                                       gguf=gguf_auto("Bedovyy/Anima-GGUF", "anima-preview3-base-{q}.gguf",
                                                      levels=(8, 6, 5, 4))),
                            note="预览版"),
              anima_version("preview2", "Preview 2",
                            anima_main("anima-preview2", fp8="anima-preview2-fp8.safetensors",
                                       mxfp8="anima-preview2-mxfp8.safetensors",
                                       nvfp4="anima-preview2-nvfp4mixed.safetensors",
                                       gguf=gguf_auto("Bedovyy/Anima-GGUF", "anima-preview2-{q}.gguf",
                                                      levels=(8, 6, 5, 4))),
                            note="预览版"),
              anima_version("preview", "Preview",
                            anima_main("anima-preview", fp8="anima-preview-fp8.safetensors",
                                       nvfp4="anima-preview-nvfp4mixed.safetensors",
                                       gguf=gguf_auto("Bedovyy/Anima-GGUF", "anima-preview-{q}.gguf",
                                                      levels=(8, 6, 5, 4, 3))),
                            note="最早的预览版"),
          ],
          ui=NEO_COMFY,
          tips="Aesthetic / Base：30–50 步、CFG 4–5；Turbo：8–12 步、CFG 1。分辨率 512²–1536²",
          license="CircleStone Labs 非商用许可（生成的图片可以商用）",
          link="https://huggingface.co/circlestone-labs/Anima")

WAI = m("wai", "WAI-illustrious-SDXL", "anime",
        "Civitai 上最热门的 Illustrious 二次元融合模型（SDXL），VAE / 文本编码器已内置，下一个文件就能出图",
        [
            v("v17", "v17.0", [sdxl_ckpt(o("fp16", civ(2883731),
                                           hf("LyliaEngine/waiIllustriousSDXL_v170", "waiIllustriousSDXL_v170.safetensors"),
                                           file="waiIllustriousSDXL_v170.safetensors", label="fp16 原版")),
                               SDXL_VAE], note="最新版", default=True),
            v("v16", "v16.0", [sdxl_ckpt(o("fp16", civ(2514310),
                                           hf("Ine007/waiIllustriousSDXL_v160", "waiIllustriousSDXL_v160.safetensors"),
                                           file="waiIllustriousSDXL_v160.safetensors", label="fp16 原版")),
                               SDXL_VAE]),
        ],
        ui=ALL_UI, tips="SDXL 单文件大模型；提示词用 Danbooru 标签",
        link="https://civitai.com/models/827184")

NOOBAI = m("noobai", "NoobAI-XL", "anime",
           "Laxhar 在 Illustrious 上用 Danbooru + e621 续训的二次元 SDXL，角色和画师认得多",
           [
               v("eps-1.1", "ε-pred v1.1", [sdxl_ckpt(o("fp16", hf("Laxhar/noobai-XL-1.1", "NoobAI-XL-v1.1.safetensors"),
                                                        label="fp16 原版")), SDXL_VAE],
                 note="普通（ε）预测版，兼容性最好", default=True),
               v("vpred-1.0", "V-pred v1.0", [sdxl_ckpt(o("fp16", hf("Laxhar/noobai-XL-Vpred-1.0",
                                                                    "NoobAI-XL-Vpred-v1.0.safetensors"),
                                                          label="fp16 原版")), SDXL_VAE],
                 note="v-prediction 版：色彩更好，要 Forge Neo / 新版 ComfyUI 才能自动识别"),
           ],
           ui=ALL_UI, license="fair-ai-public-license-1.0-sd",
           link="https://huggingface.co/Laxhar/noobai-XL-1.1")

ILLUSTRIOUS = m("illustrious", "Illustrious-XL", "anime",
                "OnomaAI 的二次元 SDXL 底模，WAI / NoobAI 等融合模型都基于它；练 LoRA 常用 v0.1",
                [
                    v("v0.1", "v0.1", [sdxl_ckpt(o("fp16", hf("OnomaAIResearch/Illustrious-xl-early-release-v0",
                                                             "Illustrious-XL-v0.1.safetensors"), label="fp16 原版")),
                                       SDXL_VAE], note="大多数 Illustrious LoRA 基于它训练", default=True),
                    v("v1.0", "v1.0", [sdxl_ckpt(o("fp16", hf("OnomaAIResearch/Illustrious-XL-v1.0",
                                                             "Illustrious-XL-v1.0.safetensors"), label="fp16 原版")),
                                       SDXL_VAE]),
                    v("v2.0", "v2.0", [sdxl_ckpt(o("fp16", hf("OnomaAIResearch/Illustrious-XL-v2.0",
                                                             "Illustrious-XL-v2.0.safetensors"), label="fp16 原版")),
                                       SDXL_VAE]),
                ],
                ui=ALL_UI, link="https://huggingface.co/OnomaAIResearch/Illustrious-XL-v2.0")

PONY = m("pony", "Pony Diffusion V6 XL", "anime",
         "老牌 SDXL 二次元 / 卡通底模，提示词开头要写 score_9, score_8_up, score_7_up 这类质量词",
         [v("v6", "V6", [sdxl_ckpt(o("fp16", civ(290640),
                                     hf("LyliaEngine/Pony_Diffusion_V6_XL",
                                        "ponyDiffusionV6XL_v6StartWithThisOne.safetensors"),
                                     file="ponyDiffusionV6XL_v6StartWithThisOne.safetensors", label="fp16 原版")),
                         SDXL_VAE], default=True)],
         ui=ALL_UI, link="https://civitai.com/models/257749")

ANIMAGINE = m("animagine", "Animagine XL 4.0", "anime",
              "Cagliostro Lab 的二次元 SDXL；opt 版是官方优化过画质的版本",
              [
                  v("opt", "4.0 Opt", [sdxl_ckpt(o("fp16", hf("cagliostrolab/animagine-xl-4.0",
                                                              "animagine-xl-4.0-opt.safetensors"), label="fp16 原版")),
                                       SDXL_VAE], note="推荐", default=True),
                  v("base", "4.0", [sdxl_ckpt(o("fp16", hf("cagliostrolab/animagine-xl-4.0",
                                                          "animagine-xl-4.0.safetensors"), label="fp16 原版")),
                                    SDXL_VAE]),
              ],
              ui=ALL_UI, license="openrail++", link="https://huggingface.co/cagliostrolab/animagine-xl-4.0")

NETAYUME = m("netayume", "NetaYume Lumina", "anime",
             "基于 Lumina-Image 2.0 的二次元模型（Gemma 2B 文本编码器），自然语言理解比 SDXL 好",
             [v("v4", "v4", [
                 c("main", "主模型（DiT）", "DiffusionModel",
                   [o("bf16", hf("duongve/NetaYume-Lumina-Image-2.0", "Unet/v4/NetaYumev4_unet.safetensors"),
                      label="bf16 原版")]),
                 c("te", "文本编码器 Gemma 2 2B", "TextEncoder",
                   [o("fp16", hf("Comfy-Org/Lumina-Image-2", "split_files/text_encoders/gemma_2_2b_fp16.safetensors"))]),
                 FLUX_AE,
             ], default=True)],
             ui=NEO_COMFY, license="Apache 2.0", link="https://huggingface.co/duongve/NetaYume-Lumina-Image-2.0")


def zimage_version(id, label, prefix, comfy_repo, gguf_repo, nvfp4=False, note="", default=False):
    d = "split_files/diffusion_models/"
    opts = [o("bf16", hf(comfy_repo, f"{d}{prefix}_bf16.safetensors")),
            o("int8", hf(comfy_repo, f"{d}{prefix}_int8_convrot.safetensors"))]
    if nvfp4:
        opts.append(o("nvfp4", hf(comfy_repo, f"{d}{prefix}_nvfp4.safetensors")))
    opts.append(gguf_auto(gguf_repo, prefix.replace("_", "-") + "-{q}.gguf"))
    t = "split_files/text_encoders/"
    return v(id, label, [
        c("main", "主模型（DiT）", "DiffusionModel", opts),
        c("te", "文本编码器 Qwen3 4B", "TextEncoder", [
            o("bf16", hf("Comfy-Org/Z-Image-Turbo", t + "qwen_3_4b.safetensors")),
            o("fp8", hf("Comfy-Org/Z-Image-Turbo", t + "qwen_3_4b_fp8_mixed.safetensors"), label="fp8 mixed"),
            o("fp4", hf("Comfy-Org/Z-Image-Turbo", t + "qwen_3_4b_fp4_mixed.safetensors"), label="fp4 mixed"),
        ]),
        c("vae", "VAE（FLUX ae）", "VAE",
          [o("bf16", hf("Comfy-Org/Z-Image-Turbo", "split_files/vae/ae.safetensors"), label="原版")]),
    ], note=note, default=default)


ZIMAGE = m("zimage", "Z-Image", "image",
           "通义 MAI 的 6B 模型，写实人像和中英文字都强；Turbo 版 8 步就能出图",
           [
               zimage_version("turbo", "Turbo", "z_image_turbo", "Comfy-Org/Z-Image-Turbo",
                              "unsloth/Z-Image-Turbo-GGUF", nvfp4=True, note="8–9 步、CFG 1，出图推荐", default=True),
               zimage_version("base", "Base", "z_image", "Comfy-Org/Z-Image", "unsloth/Z-Image-GGUF",
                              note="底模：多样性高、适合练 LoRA，要 28 步以上、CFG 3–5"),
           ],
           ui=NEO_COMFY, license="Apache 2.0", link="https://huggingface.co/Tongyi-MAI/Z-Image-Turbo")


def krea2_version(id, label, kind, extra_main=(), gguf=None, extras=(), note="", default=False):
    d = "diffusion_models/"
    opts = [o("bf16", hf("Comfy-Org/Krea-2", f"{d}krea2_{kind}_bf16.safetensors")),
            o("fp8", hf("Comfy-Org/Krea-2", f"{d}krea2_{kind}_fp8_scaled.safetensors"), label="fp8 scaled"),
            o("int8", hf("Comfy-Org/Krea-2", f"{d}krea2_{kind}_int8_convrot.safetensors"))]
    opts += list(extra_main)
    if gguf:
        opts.append(gguf)
    t = "text_encoders/"
    return v(id, label, [
        c("main", "主模型（DiT）", "DiffusionModel", opts),
        c("te", "文本编码器 Qwen3-VL 4B", "TextEncoder", [
            o("bf16", hf("Comfy-Org/Krea-2", t + "qwen3vl_4b_bf16.safetensors")),
            o("fp8", hf("Comfy-Org/Krea-2", t + "qwen3vl_4b_fp8_scaled.safetensors"), label="fp8 scaled"),
            o("int8", hf("Comfy-Org/Krea-2", t + "qwen3vl_4b_int8_convrot.safetensors")),
        ]),
        c("vae", "VAE（Qwen-Image VAE）", "VAE",
          [o("bf16", hf("Comfy-Org/Krea-2", "vae/qwen_image_vae.safetensors"), label="原版")]),
        *extras,
    ], note=note, default=default)


KREA2 = m("krea2", "Krea 2", "image",
          "Krea 的写实 / 审美向模型（Qwen3-VL 4B 文本编码器），出图质感好",
          [
              krea2_version("turbo", "Turbo", "turbo",
                            extra_main=(o("mxfp8", hf("Comfy-Org/Krea-2", "diffusion_models/krea2_turbo_mxfp8.safetensors")),
                                        o("nvfp4", hf("Comfy-Org/Krea-2", "diffusion_models/krea2_turbo_nvfp4.safetensors"))),
                            gguf=gguf_auto("vantagewithai/Krea-2-Turbo-GGUF", "krea2_turbo-{q}.gguf"),
                            note="8 步、CFG 1，出图推荐", default=True),
              krea2_version("raw", "Raw", "raw",
                            gguf=gguf_auto("vantagewithai/Krea-2-Raw-GGUF", "krea2_raw-{q}.gguf"),
                            extras=(c("turbo_lora", "Turbo 加速 LoRA", "LoRA",
                                      [o("bf16", hf("Comfy-Org/Krea-2",
                                                    "loras/krea2_turbo_lora_rank_64_bf16.safetensors"),
                                         label="rank 64")],
                                      required=False, default=False, desc="可选：让 Raw 也能 8 步出图"),),
                            note="原始版：约 28 步、CFG 4–5.5，适合练 LoRA"),
          ],
          ui=NEO_COMFY, license="Krea 2 社区许可", link="https://huggingface.co/Comfy-Org/Krea-2")


def moss_version(id, label, kind, note="", default=False):
    up = "SFT" if kind == "sft" else "Base"
    return v(id, label, [
        c("main", "主模型（DiT）", "DiffusionModel", [
            o("bf16", hf("Comfy-Org/MOSS-Image", f"diffusion_models/moss_image_{kind}_bf16.safetensors")),
            o("int8", hf("Comfy-Org/MOSS-Image", f"diffusion_models/moss_image_{kind}_int8_convrot.safetensors")),
            gguf_auto(f"unsloth/MOSS-Image-{up}-GGUF", f"MOSS-Image-{up}-{{q}}.gguf"),
        ]),
        c("te", "文本编码器 Qwen3.5 9B", "TextEncoder", [
            o("bf16", hf("Comfy-Org/MOSS-Image", "text_encoders/qwen3.5_9b_bf16.safetensors")),
            o("int8", hf("Comfy-Org/MOSS-Image", "text_encoders/qwen3.5_9b_int8_convrot.safetensors")),
            o("w4a8", hf("Comfy-Org/MOSS-Image", "text_encoders/qwen3.5_9b_w4a8_gptq.safetensors"), label="w4a8 GPTQ"),
        ]),
        c("vae", "VAE（FLUX.2 VAE）", "VAE",
          [o("bf16", hf("Comfy-Org/MOSS-Image", "vae/flux2-vae.safetensors"), label="原版")]),
        c("edit_lora", "改图 LoRA", "LoRA",
          [o("bf16", hf("Comfy-Org/MOSS-Image", "loras/moss_image_edit_lora_rank_64_bf16.safetensors"), label="rank 64")],
          required=False, default=False, desc="可选：让 MOSS 能参考输入图改图"),
        c("control_lora", "颜色 / 深度控制 LoRA", "LoRA",
          [o("bf16", hf("Comfy-Org/MOSS-Image", "loras/moss_image_color_depth_control.safetensors"), label="官方")],
          required=False, default=False, desc="可选：按色块 / 深度图控制构图"),
    ], note=note, default=default)


MOSS = m("moss", "MOSS-Image", "image",
         "复旦 OpenMOSS 的 5B 文生图模型，中文提示词、文字渲染、JSON 布局控制都支持，Apache 2.0 可商用",
         [moss_version("sft", "SFT", "sft", note="出图推荐", default=True),
          moss_version("base", "Base", "base", note="底模，练 LoRA 用这个")],
         ui=NEO_COMFY, license="Apache 2.0", link="https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT")


def qi21_version(id, label, kind, extras=(), note="", default=False):
    pre = "qwen_image_2.1" + ("_turbo" if kind == "turbo" else "")
    gg = "unsloth/Qwen-Image-2.1-Turbo-GGUF" if kind == "turbo" else "unsloth/Qwen-Image-2.1-GGUF"
    return v(id, label, [
        c("main", "主模型（DiT）", "DiffusionModel", [
            o("bf16", hf("Comfy-Org/Qwen-Image-2.1", f"diffusion_models/{pre}_bf16.safetensors")),
            o("int8", hf("Comfy-Org/Qwen-Image-2.1", f"diffusion_models/{pre}_int8_convrot.safetensors")),
            gguf_auto(gg, pre.replace("_", "-") + "-{q}.gguf"),
        ]),
        c("te", "文本编码器 Qwen3-VL 8B", "TextEncoder", [
            o("bf16", hf("Comfy-Org/Qwen-Image-2.1", "text_encoders/qwen3vl_8b_bf16.safetensors")),
            o("int8", hf("Comfy-Org/Qwen-Image-2.1", "text_encoders/qwen3vl_8b_int8_convrot.safetensors")),
            o("w4a8", hf("Comfy-Org/Qwen-Image-2.1", "text_encoders/qwen3vl_8b_w4a8.safetensors")),
        ]),
        c("vae", "VAE（Qwen-Image 2.1 VAE）", "VAE",
          [o("bf16", hf("Comfy-Org/Qwen-Image-2.1", "vae/qwen_image_2.1_vae_bf16.safetensors"), label="原版")]),
        *extras,
    ], note=note, default=default)


QWEN_IMAGE_21 = m("qwen_image_21", "Qwen-Image 2.1", "image",
                  "通义千问 7B 图像模型：文生图、改图（最多 10 张参考图）、透明背景都在一个模型里",
                  [
                      qi21_version("std", "标准版", "std", extras=(
                          c("turbo_lora", "加速 LoRA", "LoRA", [
                              o("bf16", hf("Comfy-Org/Qwen-Image-2.1",
                                           "loras/qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors"),
                                label="Turbo LoRA（8 步）", id="turbo8"),
                              o("bf16", hf("Comfy-Org/Qwen-Image-2.1",
                                           "loras/p_q_image_2.1_6_steps_v0.2_comfyui_bf16.safetensors"),
                                label="P-Q 6 步 v0.2", id="pq6"),
                              o("bf16", hf("Comfy-Org/Qwen-Image-2.1",
                                           "loras/p_q_image_2.1_5_steps_v0.1_comfyui_bf16.safetensors"),
                                label="P-Q 5 步 v0.1", id="pq5"),
                          ], required=False, default=False, desc="可选：CFG 1、5–8 步出图"),),
                                   note="约 30–50 步", default=True),
                      qi21_version("turbo", "Turbo", "turbo", note="蒸馏版：8 步、CFG 1"),
                  ],
                  ui=NEO_COMFY, license="Qwen Research 许可（非商用）",
                  link="https://huggingface.co/Qwen/Qwen-Image-2.1")


def lightning(repo, steps_paths, desc="可选：配合 CFG 1、4 / 8 步出图"):
    return c("lightning", "Lightning 加速 LoRA", "LoRA",
             [o("bf16", hf(repo, p), label=lab, id=i) for i, lab, p in steps_paths],
             required=False, default=False, desc=desc)


QWEN_IMAGE = m("qwen_image", "Qwen-Image", "image",
               "通义千问 20B 图像模型，中文文字渲染很强；2512 是 2025 年 12 月的更新版",
               [
                   v("2512", "2512", [
                       c("main", "主模型（DiT）", "DiffusionModel", [
                           o("bf16", hf("Comfy-Org/Qwen-Image", "split_files/diffusion_models/qwen_image_2512_bf16.safetensors")),
                           o("fp8", hf("Comfy-Org/Qwen-Image", "split_files/diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors")),
                           gguf_auto("unsloth/Qwen-Image-2512-GGUF", "qwen-image-2512-{q}.gguf"),
                       ]),
                       QWEN25VL_TE, QWEN_IMAGE_VAE,
                       lightning("lightx2v/Qwen-Image-2512-Lightning", [
                           ("8steps", "8 步 v1.0", "Qwen-Image-2512-Lightning-8steps-V1.0-bf16.safetensors"),
                           ("4steps", "4 步 v1.0", "Qwen-Image-2512-Lightning-4steps-V1.0-bf16.safetensors")]),
                   ], note="推荐", default=True),
                   v("2508", "初版（2508）", [
                       c("main", "主模型（DiT）", "DiffusionModel", [
                           o("bf16", hf("Comfy-Org/Qwen-Image", "split_files/diffusion_models/qwen_image_bf16.safetensors")),
                           o("fp8", hf("Comfy-Org/Qwen-Image", "split_files/diffusion_models/qwen_image_fp8_e4m3fn.safetensors")),
                           o("nvfp4", hf("Comfy-Org/Qwen-Image", "split_files/diffusion_models/qwen_image_nvfp4.safetensors")),
                           gguf_auto("QuantStack/Qwen-Image-GGUF", "Qwen_Image-{q}.gguf"),
                       ]),
                       QWEN25VL_TE, QWEN_IMAGE_VAE,
                       lightning("lightx2v/Qwen-Image-Lightning", [
                           ("8steps", "8 步 v2.0", "Qwen-Image-Lightning-8steps-V2.0-bf16.safetensors"),
                           ("4steps", "4 步 v2.0", "Qwen-Image-Lightning-4steps-V2.0-bf16.safetensors")]),
                   ]),
               ],
               ui=NEO_COMFY, license="Apache 2.0", tips="20–50 步、CFG 2.5–4；挂 Lightning LoRA 时 4 / 8 步、CFG 1",
               link="https://huggingface.co/Qwen/Qwen-Image-2512")


def klein_version(id, label, size, base, note="", default=False):
    nm = f"flux-2-klein-{'base-' if base else ''}{size}"
    gg_repo = f"unsloth/FLUX.2-klein-{'base-' if base else ''}{size.upper()}-GGUF"
    if size == "4b":
        main_opts = [o("bf16", hf("Comfy-Org/Flux-2-Klein-4B", f"split_files/diffusion_models/{nm}.safetensors")),
                     gguf_auto(gg_repo, nm + "-{q}.gguf")]
        te = c("te", "文本编码器 Qwen3 4B", "TextEncoder", [
            o("bf16", hf("Comfy-Org/Flux-2-Klein-4B", "split_files/text_encoders/qwen_3_4b.safetensors")),
            o("fp4", hf("Comfy-Org/Flux-2-Klein-4B", "split_files/text_encoders/qwen_3_4b_fp4_flux2.safetensors")),
        ])
    else:
        # 9B 的官方仓库要登录同意协议，只有 unsloth 的 GGUF（含 BF16 无损档）能匿名下载
        main_opts = [gguf_auto(gg_repo, nm + "-{q}.gguf", extra=("BF16",))]
        te = c("te", "文本编码器 Qwen3 8B", "TextEncoder", [
            o("bf16", hf("Comfy-Org/Flux-2-Klein-9B", "split_files/text_encoders/qwen_3_8b.safetensors")),
            o("fp8", hf("Comfy-Org/Flux-2-Klein-9B", "split_files/text_encoders/qwen_3_8b_fp8mixed.safetensors"),
              label="fp8 mixed"),
            o("fp4", hf("Comfy-Org/Flux-2-Klein-9B", "split_files/text_encoders/qwen_3_8b_fp4mixed.safetensors"),
              label="fp4 mixed"),
        ])
    return v(id, label, [c("main", "主模型（DiT）", "DiffusionModel", main_opts), te, FLUX2_VAE],
             note=note, default=default)


FLUX2_KLEIN = m("flux2_klein", "FLUX.2 [klein]", "image",
                "Black Forest Labs 的小尺寸 FLUX.2，文生图 + 多图参考改图；4B 版 Apache 2.0 可商用，9B 版非商用",
                [
                    klein_version("4b", "4B", "4b", False, note="蒸馏版：4 步、CFG 1", default=True),
                    klein_version("9b", "9B", "9b", False, note="蒸馏版，画质更好（非商用，只有 GGUF）"),
                    klein_version("4b-base", "4B Base", "4b", True, note="底模：25–50 步，练 LoRA 用"),
                    klein_version("9b-base", "9B Base", "9b", True, note="底模（非商用，只有 GGUF）"),
                ],
                ui=NEO_COMFY, link="https://huggingface.co/black-forest-labs/FLUX.2-klein-4B")


def flux1_version(id, label, main_opts, note="", default=False):
    return v(id, label, [c("main", "主模型（DiT）", "DiffusionModel", main_opts), FLUX_CLIP_L, FLUX_T5, FLUX_AE],
             note=note, default=default)


FLUX1 = m("flux1", "FLUX.1", "image",
          "Black Forest Labs 的 12B 模型，LoRA / ControlNet 生态最全；Krea dev 是和 Krea 合作的写实审美版",
          [
              flux1_version("dev", "dev", [
                  o("bf16", hf("Comfy-Org/Flux-1-Dev", "flux1-dev.safetensors")),
                  o("fp8", hf("Kijai/flux-fp8", "flux1-dev-fp8-e4m3fn.safetensors")),
                  gguf_auto("city96/FLUX.1-dev-gguf", "flux1-dev-{q}.gguf"),
              ], note="20–30 步、Distilled CFG 3.5", default=True),
              flux1_version("krea-dev", "Krea dev", [
                  o("fp8", hf("Comfy-Org/Flux-1-Krea-Dev", "split_files/diffusion_models/flux1-krea-dev_fp8_scaled.safetensors"),
                    label="fp8 scaled"),
                  gguf_auto("QuantStack/FLUX.1-Krea-dev-GGUF", "flux1-krea-dev-{q}.gguf"),
              ], note="写实质感更好，去掉了「塑料感」"),
              flux1_version("schnell", "schnell", [
                  o("bf16", hf("Comfy-Org/Flux-1-Schnell", "flux1-schnell.safetensors")),
                  o("fp8", hf("Kijai/flux-fp8", "flux1-schnell-fp8-e4m3fn.safetensors")),
                  gguf_auto("city96/FLUX.1-schnell-gguf", "flux1-schnell-{q}.gguf"),
              ], note="4 步快速版，Apache 2.0 可商用"),
          ],
          ui=["comfyui", "neo", "classic"], license="dev / Krea dev 非商用；schnell 为 Apache 2.0",
          link="https://huggingface.co/black-forest-labs/FLUX.1-dev")

FLUX2_DEV = m("flux2_dev", "FLUX.2 [dev]", "image",
              "FLUX.2 的 32B 完整版（Mistral 24B 文本编码器），画质顶级但很吃配置：建议 24GB 显存 + 64GB 内存",
              [v("dev", "dev", [
                  c("main", "主模型（DiT）", "DiffusionModel", [
                      o("fp8", hf("Comfy-Org/Flux-2-Dev", "split_files/diffusion_models/flux2_dev_fp8mixed.safetensors"),
                        label="fp8 mixed"),
                      gguf_auto("unsloth/FLUX.2-dev-GGUF", "flux2-dev-{q}.gguf"),
                  ]),
                  c("te", "文本编码器 Mistral Small 3.1 24B", "TextEncoder", [
                      o("bf16", hf("Comfy-Org/Flux-2-Dev", "split_files/text_encoders/mistral_3_small_flux2_bf16.safetensors")),
                      o("fp8", hf("Comfy-Org/Flux-2-Dev", "split_files/text_encoders/mistral_3_small_flux2_fp8.safetensors")),
                      o("fp4", hf("Comfy-Org/Flux-2-Dev", "split_files/text_encoders/mistral_3_small_flux2_fp4_mixed.safetensors"),
                        label="fp4 mixed"),
                  ]),
                  c("vae", "VAE（FLUX.2 VAE）", "VAE",
                    [o("bf16", hf("Comfy-Org/Flux-2-Dev", "split_files/vae/flux2-vae.safetensors"), label="原版")]),
                  c("turbo_lora", "Turbo 加速 LoRA", "LoRA", [
                      o("bf16", hf("Comfy-Org/Flux-2-Dev", "split_files/loras/Flux2TurboComfyv2.safetensors"),
                        label="v2", id="v2"),
                      o("bf16", hf("Comfy-Org/Flux-2-Dev", "split_files/loras/Flux_2-Turbo-LoRA_comfyui.safetensors"),
                        label="v1", id="v1"),
                  ], required=False, default=False, desc="可选：少步数出图"),
              ], default=True)],
              ui=["comfyui"], license="FLUX 非商用许可", link="https://huggingface.co/black-forest-labs/FLUX.2-dev")

ERNIE = m("ernie_image", "ERNIE-Image", "image",
          "百度 8B 文生图（Apache 2.0），长文本、海报、多格漫画排版很强",
          [
              v("std", "标准版", [
                  c("main", "主模型（DiT）", "DiffusionModel",
                    [o("bf16", hf("Comfy-Org/ERNIE-Image", "diffusion_models/ernie-image.safetensors"))]),
                  c("te", "文本编码器 Ministral 3B", "TextEncoder",
                    [o("bf16", hf("Comfy-Org/ERNIE-Image", "text_encoders/ministral-3-3b.safetensors"))]),
                  c("vae", "VAE（FLUX.2 VAE）", "VAE",
                    [o("bf16", hf("Comfy-Org/ERNIE-Image", "vae/flux2-vae.safetensors"), label="原版")]),
                  c("pe", "提示词扩写模型", "TextEncoder",
                    [o("bf16", hf("Comfy-Org/ERNIE-Image", "text_encoders/ernie-image-prompt-enhancer.safetensors"))],
                    required=False, default=False, desc="可选：把短提示词自动扩写成详细描述（ComfyUI 工作流里用）"),
              ], note="约 50 步", default=True),
              v("turbo", "Turbo", [
                  c("main", "主模型（DiT）", "DiffusionModel",
                    [o("bf16", hf("Comfy-Org/ERNIE-Image", "diffusion_models/ernie-image-turbo.safetensors"))]),
                  c("te", "文本编码器 Ministral 3B", "TextEncoder",
                    [o("bf16", hf("Comfy-Org/ERNIE-Image", "text_encoders/ministral-3-3b.safetensors"))]),
                  c("vae", "VAE（FLUX.2 VAE）", "VAE",
                    [o("bf16", hf("Comfy-Org/ERNIE-Image", "vae/flux2-vae.safetensors"), label="原版")]),
              ], note="8 步快速版"),
          ],
          ui=NEO_COMFY, license="Apache 2.0", link="https://huggingface.co/baidu/ERNIE-Image")

CHROMA = m("chroma", "Chroma1-HD", "image",
           "lodestones 在 FLUX.1 schnell 上重训的 8.9B 模型（Apache 2.0），风格跨度大、内容限制少",
           [v("hd", "HD", [
               c("main", "主模型（DiT）", "DiffusionModel", [
                   o("bf16", hf("Comfy-Org/Chroma1-HD", "split_files/diffusion_models/Chroma1-HD.safetensors")),
                   o("fp8", hf("Comfy-Org/Chroma1-HD", "split_files/diffusion_models/Chroma1-HD-fp8mixed.safetensors"),
                     label="fp8 mixed"),
               ]),
               FLUX_T5, FLUX_AE,
           ], default=True)],
           ui=NEO_COMFY, license="Apache 2.0", tips="约 40 步、CFG 3–5，只用 T5 一个文本编码器",
           link="https://huggingface.co/lodestones/Chroma1-HD")


def qedit_version(id, label, main_opts, light, note="", default=False):
    return v(id, label, [c("main", "主模型（DiT）", "DiffusionModel", main_opts), QWEN25VL_TE, QWEN_IMAGE_VAE, light],
             note=note, default=default)


QWEN_EDIT = m("qwen_image_edit", "Qwen-Image-Edit", "edit",
              "通义千问 20B 改图模型：换背景、换衣服、多图融合、改文字；2511 是最新版",
              [
                  qedit_version("2511", "2511", [
                      o("bf16", hf("Comfy-Org/Qwen-Image-Edit", "split_files/diffusion_models/qwen_image_edit_2511_bf16.safetensors")),
                      o("fp8", hf("Comfy-Org/Qwen-Image-Edit", "split_files/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors"),
                        label="fp8 mixed"),
                      o("int8", hf("Comfy-Org/Qwen-Image-Edit", "split_files/diffusion_models/qwen_image_edit_2511_int8_convrot.safetensors")),
                      gguf_auto("unsloth/Qwen-Image-Edit-2511-GGUF", "qwen-image-edit-2511-{q}.gguf"),
                  ], lightning("lightx2v/Qwen-Image-Edit-2511-Lightning", [
                      ("4steps", "4 步 v1.0", "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"),
                      ("8steps", "8 步 v1.0", "Qwen-Image-Edit-2511-Lightning-8steps-V1.0-bf16.safetensors")]),
                      note="推荐", default=True),
                  qedit_version("2509", "2509", [
                      o("bf16", hf("Comfy-Org/Qwen-Image-Edit", "split_files/diffusion_models/qwen_image_edit_2509_bf16.safetensors")),
                      o("fp8", hf("Comfy-Org/Qwen-Image-Edit", "split_files/diffusion_models/qwen_image_edit_2509_fp8_e4m3fn.safetensors")),
                      o("int8", hf("Comfy-Org/Qwen-Image-Edit", "split_files/diffusion_models/qwen_image_edit_2509_int8_convrot.safetensors")),
                      gguf_auto("QuantStack/Qwen-Image-Edit-2509-GGUF", "Qwen-Image-Edit-2509-{q}.gguf"),
                  ], lightning("lightx2v/Qwen-Image-Lightning", [
                      ("4steps", "4 步 v1.0", "Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors"),
                      ("8steps", "8 步 v1.0", "Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-8steps-V1.0-bf16.safetensors")])),
              ],
              ui=NEO_COMFY, license="Apache 2.0", link="https://huggingface.co/Qwen/Qwen-Image-Edit-2511")

FLUX_KONTEXT = m("flux1_kontext", "FLUX.1 Kontext", "edit",
                 "FLUX.1 的指令改图版（12B）：上传一张图，用一句话说要怎么改",
                 [flux1_version("dev", "dev", [
                     o("fp8", hf("Comfy-Org/Flux-1-Kontext-Dev",
                                 "split_files/diffusion_models/flux1-dev-kontext_fp8_scaled.safetensors"), label="fp8 scaled"),
                     gguf_auto("QuantStack/FLUX.1-Kontext-dev-GGUF", "flux1-kontext-dev-{q}.gguf"),
                 ], default=True)],
                 ui=NEO_COMFY, license="FLUX.1 dev 非商用许可",
                 link="https://huggingface.co/black-forest-labs/FLUX.1-Kontext-dev")


def wan_pair(kind, gguf_repo, gguf_kind, lora_ver):
    d = "split_files/diffusion_models/"
    comps = []
    for noise, cn in (("high", "高噪"), ("low", "低噪")):
        Noise = noise.capitalize() + "Noise"
        comps.append(c(f"{noise}_noise", f"主模型 · {cn}（{noise} noise）", "DiffusionModel", [
            o("fp16", hf("Comfy-Org/Wan-2.2", f"{d}wan2.2_{kind}_{noise}_noise_14B_fp16.safetensors")),
            o("fp8", hf("Comfy-Org/Wan-2.2", f"{d}wan2.2_{kind}_{noise}_noise_14B_fp8_scaled.safetensors"),
              label="fp8 scaled"),
            gguf_auto(gguf_repo, f"{Noise}/Wan2.2-{gguf_kind}-A14B-{Noise}-{{q}}.gguf"),
        ], desc="14B 要高噪 + 低噪两个模型配合，两个都要下"))
    comps += [UMT5_TE, c("vae", "VAE（Wan 2.1 VAE）", "VAE",
                         [o("bf16", hf("Comfy-Org/Wan-2.2", "split_files/vae/wan_2.1_vae.safetensors"), label="原版")])]
    for noise, cn in (("high", "高噪"), ("low", "低噪")):
        comps.append(c(f"lora_{noise}", f"lightx2v 4 步加速 LoRA · {cn}", "LoRA", [
            o("bf16", hf("Comfy-Org/Wan-2.2",
                         f"split_files/loras/wan2.2_{kind}_lightx2v_4steps_lora_{lora_ver}_{noise}_noise.safetensors"),
              label=lora_ver)], required=False, default=False, link="lightx2v",
            desc="可选：4 步出视频（高噪、低噪要一起用，勾一个另一个也会勾上）"))
    return comps


WAN22 = m("wan22", "Wan 2.2", "video",
          "阿里通义万相 2.2：14B 文生 / 图生视频（高噪 + 低噪两个模型），5B 小模型显存要求低",
          [
              v("i2v", "图生视频 14B（I2V A14B）", wan_pair("i2v", "QuantStack/Wan2.2-I2V-A14B-GGUF", "I2V", "v1"),
                note="上传首帧图生成视频", default=True),
              v("t2v", "文生视频 14B（T2V A14B）", wan_pair("t2v", "QuantStack/Wan2.2-T2V-A14B-GGUF", "T2V", "v1.1")),
              v("ti2v5b", "文 / 图生视频 5B（TI2V 5B）", [
                  c("main", "主模型（DiT）", "DiffusionModel", [
                      o("fp16", hf("Comfy-Org/Wan-2.2", "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors")),
                      gguf_auto("QuantStack/Wan2.2-TI2V-5B-GGUF", "Wan2.2-TI2V-5B-{q}.gguf"),
                  ]),
                  UMT5_TE,
                  c("vae", "VAE（Wan 2.2 VAE）", "VAE",
                    [o("bf16", hf("Comfy-Org/Wan-2.2", "split_files/vae/wan2.2_vae.safetensors"), label="原版")]),
              ], note="显存小也能跑（Forge Neo 不支持 5B）", ui=["comfyui"]),
          ],
          ui=NEO_COMFY, license="Apache 2.0", tips="Forge Neo 用「Refiner」切换高噪 / 低噪模型；挂 lightx2v LoRA 时 4 步、CFG 1",
          link="https://huggingface.co/Wan-AI/Wan2.2-I2V-A14B")


def h3_main(kind):
    d = "diffusion_models/"
    return [
        o("bf16", hf("Comfy-Org/MiniMax-H3", f"{d}minimax_h3_{kind}_pruned_bf16.safetensors")),
        o("fp8", hf("Comfy-Org/MiniMax-H3", f"{d}minimax_h3_{kind}_pruned_fp8_scaled.safetensors"), label="fp8 scaled"),
        o("int8", hf("Comfy-Org/MiniMax-H3", f"{d}minimax_h3_{kind}_pruned_int8_convrot.safetensors")),
        o("w6a8", hf("Comfy-Org/MiniMax-H3", f"{d}minimax_h3_{kind}_pruned_w6a8.safetensors")),
        gguf_auto("unsloth/MiniMax-H3-GGUF", f"minimax_h3_{kind}_pruned-{{q}}.gguf"),
    ]


H3 = m("minimax_h3", "MiniMax H3", "video",
       "MiniMax 开源的音视频同出模型：文 / 图生视频、首尾帧、参考图生视频，自带立体声音轨",
       [v("h3", "H3", [
           c("fl2va", "主模型 FL2VA", "DiffusionModel", h3_main("fl2va"),
             desc="文生视频 / 图生视频 / 首尾帧 / ControlNet"),
           c("te", "文本编码器 Qwen3-VL 32B", "TextEncoder", [
               o("bf16", hf("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors")),
               o("int8", hf("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors")),
               o("nvfp4", hf("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"),
                 label="nvfp4 AWQ"),
               gguf_auto("unsloth/MiniMax-H3-GGUF", "qwen3vl_32b_minimax_h3-{q}.gguf"),
           ]),
           c("vae_video", "视频 VAE", "VAE", [
               o("fp16", hf("unsloth/MiniMax-H3-GGUF", "vae/minimax_h3_video_vae_fp16.safetensors")),
               o("int8", hf("Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_int8_convrot.safetensors")),
           ]),
           c("vae_audio", "音频 VAE", "VAE",
             [o("fp32", hf("unsloth/MiniMax-H3-GGUF", "vae/minimax_h3_audio_vae_fp32.safetensors"))]),
           c("turbo_fl2v", "Turbo LoRA · FL2V", "LoRA", [
               o("bf16", hf("lightx2v/Minimax-h3-Turbo",
                            "minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"), label="8 步 v1.0", id="8step"),
               o("bf16", hf("Comfy-Org/MiniMax-H3",
                            "loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"), label="4 步 v1.0",
                 id="4step"),
           ], required=False, default=False, desc="可选：少步数出片，速度快很多"),
           c("ref2va", "主模型 Ref2VA", "DiffusionModel", h3_main("ref2va"), required=False, default=False,
             desc="可选：参考图 / 参考视频 / 参考音频生视频才需要"),
           c("turbo_ref2v", "Turbo LoRA · Ref2V", "LoRA", [
               o("bf16", hf("lightx2v/Minimax-h3-Turbo",
                            "minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"), label="8 步 v1.0", id="8step"),
           ], required=False, default=False, desc="可选：配合 Ref2VA 用"),
           c("controlnet", "ControlNet Union 2.0", None, [
               o("int8", hf("Comfy-Org/MiniMax-H3",
                            "model_patches/minimax_h3_fun_controlnet_union_2.0_pruned_int8_convrot.safetensors")),
               o("bf16", hf("Comfy-Org/MiniMax-H3",
                            "model_patches/minimax_h3_fun_controlnet_union_2.0_pruned_bf16.safetensors")),
           ], required=False, default=False, folder="models/model_patches",
             desc="可选：姿态 / 深度 / 线稿控制、视频局部重绘"),
       ], default=True)],
       ui=["comfyui", "h3"], license="MiniMax H3 社区许可", ram_factor=1.5,
       tips="Forge Neo 要用 H3 版（环境部署里选 Forge Neo H3）；官方 Neo 不支持 H3",
       link="https://huggingface.co/Comfy-Org/MiniMax-H3")

SDXL_BASE = m("sdxl", "SDXL 1.0", "classic", "Stability AI 的 SDXL 官方底模",
              [v("base", "Base 1.0", [sdxl_ckpt(o("fp16", hf("stabilityai/stable-diffusion-xl-base-1.0",
                                                          "sd_xl_base_1.0.safetensors"), label="fp16 原版")),
                                      SDXL_VAE], default=True)],
              ui=ALL_UI, license="openrail++", link="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0")

SD15 = m("sd15", "Stable Diffusion 1.5", "classic", "SD 1.5 官方底模，老插件 / ControlNet 生态最全，显存要求最低",
         [v("emaonly", "pruned-emaonly", [c("main", "大模型（SD1.5 单文件）", "Checkpoint", [
             o("fp16", hf("Comfy-Org/stable-diffusion-1.5", "v1-5-pruned-emaonly-fp16.safetensors"), label="fp16"),
             o("fp32", hf("Comfy-Org/stable-diffusion-1.5", "v1-5-pruned-emaonly.safetensors"), label="fp32 原版"),
         ], desc="VAE 和文本编码器都已经打包在里面")], default=True)],
         ui=ALL_UI, license="CreativeML OpenRAIL-M", link="https://huggingface.co/Comfy-Org/stable-diffusion-1.5")

SPEC = {
    "groups": [
        {"id": "anime", "label": "二次元"},
        {"id": "image", "label": "通用出图"},
        {"id": "edit", "label": "改图"},
        {"id": "video", "label": "视频"},
        {"id": "classic", "label": "SDXL / SD1.5 底模"},
    ],
    "models": [ANIMA, WAI, NOOBAI, ILLUSTRIOUS, PONY, ANIMAGINE, NETAYUME,
               ZIMAGE, KREA2, MOSS, QWEN_IMAGE_21, QWEN_IMAGE, FLUX2_KLEIN, FLUX1, FLUX2_DEV, ERNIE, CHROMA,
               QWEN_EDIT, FLUX_KONTEXT,
               WAN22, H3,
               SDXL_BASE, SD15],
}


# ---------------------------------------------------------------- 联网补大小 / 哈希

def fetch_tree(repo):
    for attempt in range(3):
        try:
            r = requests.get(f"https://huggingface.co/api/models/{repo}/tree/main?recursive=true",
                             timeout=60, headers=UA)
            if r.status_code == 200:
                return {e["path"]: ((e.get("lfs") or {}).get("size") or e.get("size") or 0,
                                    (e.get("lfs") or {}).get("oid"))
                        for e in r.json() if e.get("type") == "file"}
            if r.status_code in (401, 403, 404):
                raise SystemExit(f"仓库 {repo} 打不开（HTTP {r.status_code}，可能要登录或已删除）")
        except requests.RequestException:
            pass
        time.sleep(2)
    raise SystemExit(f"查不到 {repo} 的文件列表")


def fetch_civitai(version_id):
    r = requests.get(f"https://civitai.com/api/v1/model-versions/{version_id}", timeout=60, headers=UA)
    r.raise_for_status()
    d = r.json()
    f = next((x for x in d.get("files", []) if x.get("primary")), (d.get("files") or [None])[0])
    if not f:
        raise SystemExit(f"Civitai 版本 {version_id} 没有文件")
    return {"name": f.get("name"), "size": int(round((f.get("sizeKB") or 0) * 1024)),
            "sha256": ((f.get("hashes") or {}).get("SHA256") or "").lower() or None}


def _iter_option_lists(spec):
    for mod in spec["models"]:
        for ver in mod["versions"]:
            for comp in ver["components"]:
                yield mod, ver, comp


def build(spec):
    repos = set()
    for _m, _v, comp in _iter_option_lists(spec):
        for opt in comp["options"]:
            if "gguf_auto" in opt:
                repos.add(opt["gguf_auto"])
            else:
                repos.update(s["hf"] for s in opt["src"] if "hf" in s)
    print(f"查询 {len(repos)} 个 HuggingFace 仓库 …")
    with ThreadPoolExecutor(8) as ex:
        trees = dict(zip(sorted(repos), ex.map(fetch_tree, sorted(repos))))
    civ_cache = {}
    problems = []

    def expand_gguf(spec_opt):
        repo, tmpl = spec_opt["gguf_auto"], spec_opt["tmpl"]
        tree = trees[repo]
        rx = re.compile("^" + re.escape(tmpl).replace(re.escape("{q}"), "(?P<q>[A-Za-z0-9_-]+?)") + "$")
        have = {}
        for path in tree:
            mm = rx.match(path)
            if mm:
                have[mm.group("q")] = path
        out = []
        for q in spec_opt["extra"]:
            if q in have:
                out.append(o("gguf", hf(repo, have[q]), quant=q, label=f"GGUF {q}"))
            else:
                problems.append(f"{repo}: 没有 GGUF 档位 {q}")
        for lv in spec_opt["levels"]:
            q = next((x for x in GGUF_LEVELS[lv] if x in have), None)
            if q:
                out.append(o("gguf", hf(repo, have[q]), quant=q, label=f"GGUF {q}"))
        if not out:
            problems.append(f"{repo}: 按 {tmpl} 一个 GGUF 都没找到")
        return out

    out_models = []
    for mod in spec["models"]:
        mo = {k: mod[k] for k in ("id", "name", "group", "desc", "tips", "license", "link", "ui")}
        if mod.get("ram_factor"):
            mo["ram_factor"] = mod["ram_factor"]
        mo["versions"] = []
        for ver in mod["versions"]:
            vo = {k: ver[k] for k in ("id", "label", "note", "default") if ver.get(k) not in (None, "", False)}
            if ver.get("ui"):
                vo["ui"] = ver["ui"]
            vo["components"] = []
            for comp in ver["components"]:
                co = {k: comp[k] for k in ("id", "label", "role", "required", "default", "desc", "folder", "link")
                      if comp.get(k) not in (None, "")}
                co["required"] = bool(comp["required"])
                co["default"] = bool(comp["default"])
                opts = []
                for so in comp["options"]:
                    opts.extend(expand_gguf(so) if "gguf_auto" in so else [so])
                co["options"] = []
                seen = set()
                for so in opts:
                    oo = {"fmt": so["fmt"]}
                    if so.get("quant"):
                        oo["quant"] = so["quant"]
                    oid = so.get("id") or (f"gguf-{so['quant']}" if so["fmt"] == "gguf" else so["fmt"])
                    if oid in seen:
                        problems.append(f"{mod['id']}/{ver['id']}/{comp['id']}: 选项 id 重复 {oid}（给它写 id=）")
                    seen.add(oid)
                    oo["id"] = oid
                    oo["label"] = so.get("label") or (so["fmt"] + (" 原版" if so["fmt"] in ("bf16", "fp16") else ""))
                    if so.get("note"):
                        oo["note"] = so["note"]
                    srcs, size, sha = [], None, None
                    for s in so["src"]:
                        if "civitai" in s:
                            vid = s["civitai"]
                            if vid not in civ_cache:
                                civ_cache[vid] = fetch_civitai(vid)
                            ci = civ_cache[vid]
                            sha = sha or ci["sha256"]
                            size = size or ci["size"]
                            srcs.append({"civitai": vid})
                            continue
                        tree = trees[s["hf"]]
                        if s["path"] not in tree:
                            problems.append(f"{s['hf']} 里没有 {s['path']}")
                            continue
                        fsize, foid = tree[s["path"]]
                        if sha and foid and foid.lower() != sha:
                            problems.append(f"{s['hf']}/{s['path']} 和 Civitai 的文件不一样（sha256 不同），没用这条线路")
                            continue
                        size = fsize if fsize else size     # HF 给的是精确字节数，优先
                        sha = sha or (foid.lower() if foid else None)
                        srcs.append({"hf": s["hf"], "path": s["path"]})
                    if not srcs:
                        continue
                    first_hf = next((x for x in srcs if "hf" in x), None)
                    oo["file"] = so.get("file") or os.path.basename(first_hf["path"] if first_hf else "")
                    oo["size"] = int(size or 0)
                    if sha:
                        oo["sha256"] = sha
                    oo["src"] = srcs
                    co["options"].append(oo)
                if not co["options"]:
                    problems.append(f"{mod['id']}/{ver['id']}/{comp['id']}: 没有可用的文件")
                vo["components"].append(co)
            mo["versions"].append(vo)
        if not any(x.get("default") for x in mo["versions"]):
            mo["versions"][0]["default"] = True
        out_models.append(mo)

    ids = [x["id"] for x in out_models]
    if len(ids) != len(set(ids)):
        problems.append("模型 id 有重复")
    return {"version": 1, "updated": time.strftime("%Y-%m-%d"), "groups": spec["groups"], "models": out_models}, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    args = ap.parse_args()
    data, problems = build(SPEC)
    for p in problems:
        print("！", p)
    n_files = sum(len(cp["options"]) for mo in data["models"] for vo in mo["versions"] for cp in vo["components"])
    print(f"{len(data['models'])} 个模型，{n_files} 个可选文件")
    if problems:
        print("有问题，没写文件")
        sys.exit(1)
    if not args.check:
        with open(OUT, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("已写入", OUT)


if __name__ == "__main__":
    main()
