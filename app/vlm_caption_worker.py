# -*- coding: utf-8 -*-
"""
自然语言反推的常驻子进程（跑在训练器的训练环境或启动器的 vlm_venv 里，需要 torch / transformers）。

模型加载一次就一直留在显存里，连续反推不用每次重新加载；启动器闲置一段时间 / 发现在跑图时直接结束这个进程，
显存就还回去了。模型推理部分照搬训练器 workers/tag_worker.py 的 VLM（自动选精度、成批生成、显存不够减半重来、
修 ToriiGate 停不下来的停止符），两边打出来的标一样。

用 -I 启动（不看用户的 PYTHONPATH 等），所以不 import 启动器的任何模块；提示词由启动器拼好传进来。

通信：stdin 每行一个 JSON 命令，stdout 以「@@{json}」行回报，其余输出是日志。
  {"cmd": "load", "dir": 模型目录, "key": "toriigate", "quant": "auto", "size_gb": 10, "batch": 0}
      → {"ev": "loaded", "quant": "bf16", "bs": 4} / {"ev": "load_failed", "error": ...}
  {"cmd": "run", "items": [{"file", "system", "user"}], "max_tokens": 512, "temperature": 0.6, "cancel_file": 路径}
      → 每张 {"ev": "item", "file", "text"} 或 {"ev": "item", "file", "error"}，最后 {"ev": "done", "cancelled"}
      取消：启动器建出 cancel_file，这一批生成完就停（不会打断正在生成的那一批）
  {"cmd": "quit"}（stdin 关了也退出）

命令只在主线程空闲时按顺序读，不能开线程一直阻塞读 stdin：Windows 上有线程在管道上同步读的时候，
别的线程加载 DLL（import numpy / torch）会一直卡到管道里来了新数据（实测卡住加载模型，直到下一条命令才放行），
所以取消走文件。
"""
import json
import os
import sys
import threading
import traceback
import warnings

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
warnings.filterwarnings("ignore", message=r".*MatMul8bitLt.*")

MAX_PIXELS = 1024 * 1024
_out_lock = threading.Lock()


def emit(**kw):
    with _out_lock:
        print("@@" + json.dumps(kw, ensure_ascii=False), flush=True)


def log(*a):
    with _out_lock:
        print(*a, flush=True)


def vram_free_gb():
    import torch
    free, _total = torch.cuda.mem_get_info()
    return free / 2 ** 30


def auto_quant(size_gb):
    """按剩余显存选 bf16 / 4bit（不自动选 8bit：bitsandbytes 的 8bit 比 4bit 慢 2–3 倍，质量差别很小）。"""
    free = vram_free_gb()
    q = "bf16" if free >= size_gb * 1.05 + 1.5 else "4bit"
    log(f"精度自动选择：可用显存 {free:.1f} GB，模型 bf16 约 {size_gb:g} GB → 用 {q}"
        + ("" if q == "bf16" else "（显存不够放原精度，量化后质量略降）"))
    if q == "4bit" and free < size_gb * 0.35 + 1.2:
        log(f"⚠ 显存可能连 4bit 都放不下（约需 {size_gb * 0.35 + 1.2:.1f} GB），可以换小一点的模型（Qwen3-VL-4B）、"
            "关掉正在跑图的 WebUI / ComfyUI，或者改用云端 API")
    return q


def pick_batch(want):
    if want:
        return max(1, int(want))
    free = vram_free_gb()
    bs = max(1, min(8, int(free - 1.5)))
    log(f"成批生成：模型加载后还剩 {free:.1f} GB 显存 → 一次 {bs} 张")
    return bs


class VLM:
    def __init__(self, d, key, quant="auto", size_gb=17, batch=0):
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError(f"这个环境里的 PyTorch（{torch.__version__}）用不了显卡：显卡驱动太旧、装成了 CPU 版，"
                               "或者不是 NVIDIA 显卡。可以改用云端 API。")
        import transformers
        from transformers import AutoProcessor
        self.torch = torch
        self.key = key
        kw = {"dtype": torch.bfloat16, "device_map": "cuda"}
        q = quant or "auto"
        if q == "auto":
            q = auto_quant(size_gb)
        if q in ("4bit", "8bit"):
            from transformers import BitsAndBytesConfig
            kw["quantization_config"] = (BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16,
                                                            bnb_4bit_quant_type="nf4") if q == "4bit"
                                         else BitsAndBytesConfig(load_in_8bit=True))
        self.quant = q
        log("加载模型到显卡…")
        model, errs = None, []
        for cls_name in ("AutoModelForImageTextToText", "AutoModelForVision2Seq", "LlavaForConditionalGeneration"):
            cls = getattr(transformers, cls_name, None)
            if cls is None:
                continue
            try:
                model = cls.from_pretrained(d, **kw)
                break
            except Exception as e:
                errs.append(f"{cls_name}: {e}")
        if model is None:
            raise RuntimeError("模型加载失败：\n" + "\n".join(errs))
        self.model = model.eval()
        pkw = {"min_pixels": 256 * 32 * 32, "max_pixels": 1024 * 1024} if key == "toriigate" else {}
        self.proc = AutoProcessor.from_pretrained(d, **pkw)
        # 停止符：ToriiGate 0.5 没带 generation_config.json，只认 <|endoftext|>，可每句话以 <|im_end|> 结尾——
        # 写完描述也停不下来，每张图都生成满 max_tokens
        tok = getattr(self.proc, "tokenizer", self.proc)
        ids = [model.generation_config.eos_token_id, getattr(tok, "eos_token_id", None)]
        ids = [i for x in ids for i in (x if isinstance(x, list) else [x]) if i is not None]
        vocab = tok.get_vocab()
        for t in ("<|im_end|>", "<|endoftext|>", "<|eot_id|>", "<end_of_turn>"):
            if t in vocab:
                ids.append(tok.convert_tokens_to_ids(t))
        self.eos = sorted(set(ids)) or None
        tok.padding_side = "left"   # 成批生成：短的提示词在左边补齐
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token
        self.pad = tok.pad_token_id
        self.bs = pick_batch(batch)

    def prompt(self, system, user):
        msgs = [{"role": "system", "content": [{"type": "text", "text": system}]},
                {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": user}]}]
        if self.key == "joycaption":   # JoyCaption 的对话模板自己插图片占位符（官方用法）
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return self.proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)

    @staticmethod
    def prep(img):
        from PIL import Image
        img = img.convert("RGB")
        # 大图先缩到约 100 万像素：Qwen-VL 系列按原图大小切视觉 token，大图会把显存撑爆、Windows 偷偷借内存，每张几分钟
        if img.width * img.height > MAX_PIXELS:
            s = (MAX_PIXELS / (img.width * img.height)) ** 0.5
            img = img.resize((max(28, int(img.width * s)), max(28, int(img.height * s))), Image.LANCZOS)
        return img

    def batch(self, imgs, prompts, max_tokens, temperature):
        torch = self.torch
        inputs = self.proc(text=prompts, images=list(imgs), return_tensors="pt", padding=True).to(self.model.device)
        if "pixel_values" in inputs:
            inputs["pixel_values"] = inputs["pixel_values"].to(torch.bfloat16)
        out = None
        try:
            with torch.no_grad():
                out = self.model.generate(**inputs, max_new_tokens=int(max_tokens or 512), do_sample=temperature > 0,
                                          temperature=temperature or None, top_p=0.9, repetition_penalty=1.05,
                                          eos_token_id=self.eos, pad_token_id=self.pad)
            res = self.proc.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)
        finally:
            del inputs
            out = None
            torch.cuda.empty_cache()
        return [(r.split("</think>", 1)[1] if "</think>" in r else r).strip() for r in res]


class Worker:
    def __init__(self):
        self.vlm = None
        self.cancel_file = ""

    def cancelled(self):
        return bool(self.cancel_file) and os.path.exists(self.cancel_file)

    @staticmethod
    def next_cmd():
        while True:
            line = sys.stdin.readline()
            if not line:
                return {"cmd": "quit"}   # 启动器关了 stdin（退出 / 崩溃）：跟着退出，不留孤儿进程占显存
            try:
                return json.loads(line)
            except ValueError:
                continue

    def load(self, c):
        try:
            self.vlm = None
            self.vlm = VLM(c["dir"], c.get("key") or "", c.get("quant") or "auto", float(c.get("size_gb") or 17),
                           int(c.get("batch") or 0))
            emit(ev="loaded", quant=self.vlm.quant, bs=self.vlm.bs)
        except Exception as e:
            traceback.print_exc()
            emit(ev="load_failed", error=f"{type(e).__name__}: {e}")

    def run(self, c):
        from PIL import Image
        self.cancel_file = c.get("cancel_file") or ""
        items = list(c.get("items") or [])
        mt = int(c.get("max_tokens") or 512)
        temp = float(c.get("temperature") if c.get("temperature") is not None else 0.6)
        vlm = self.vlm

        def gen(chunk):
            """生成一批。显存不够把以后每批的张数减半、这批拆开重来；一批里有张图出错就一张张单跑，找出是哪张。"""
            import torch
            try:
                texts = vlm.batch([x[1] for x in chunk], [x[2] for x in chunk], mt, temp)
            except torch.cuda.OutOfMemoryError as e:
                torch.cuda.empty_cache()
                if len(chunk) == 1:
                    return emit(ev="item", file=chunk[0][0], error="显存不够：" + str(e)[:200])
                vlm.bs = max(1, len(chunk) // 2)
                log(f"⚠ 显存不够一次生成 {len(chunk)} 张，改成一次 {vlm.bs} 张")
                for k in range(0, len(chunk), vlm.bs):
                    gen(chunk[k:k + vlm.bs])
                return
            except Exception as e:
                if len(chunk) == 1:
                    return emit(ev="item", file=chunk[0][0], error=f"{type(e).__name__}: {e}")
                log(f"⚠ 这一批出错（{e}），改成一张张生成")
                for x in chunk:
                    gen([x])
                return
            for x, t in zip(chunk, texts):
                emit(ev="item", file=x[0], text=t)

        i = 0
        while i < len(items) and not self.cancelled():
            chunk = []
            while i < len(items) and len(chunk) < vlm.bs:
                it = items[i]
                i += 1
                try:
                    img = Image.open(it["file"])
                    img.load()
                    chunk.append((it["file"], vlm.prep(img), vlm.prompt(it.get("system") or "", it.get("user") or "")))
                except Exception as e:
                    emit(ev="item", file=it.get("file"), error=f"读图失败：{type(e).__name__}: {e}")
            if chunk:
                gen(chunk)
        emit(ev="done", cancelled=self.cancelled())

    def main(self):
        emit(ev="hello", pid=os.getpid())
        while True:
            c = self.next_cmd()
            cmd = c.get("cmd")
            if cmd == "quit":
                break
            if cmd == "load":
                self.load(c)
            elif cmd == "run":
                if self.vlm is None:
                    emit(ev="done", cancelled=False, error="模型还没加载")
                    continue
                try:
                    self.run(c)
                except Exception as e:
                    traceback.print_exc()
                    emit(ev="done", cancelled=False, error=f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        Worker().main()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        os._exit(1)
    # 直接结束：正常退出要等 torch / CUDA 慢慢收尾（实测加载过模型后要几十秒），显存要尽快还回去
    sys.stdout.flush()
    os._exit(0)
