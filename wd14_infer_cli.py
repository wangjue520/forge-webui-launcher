# -*- coding: utf-8 -*-
"""
这个脚本运行在隔离的 wd14_venv 环境里（由 wd14_venv_manager 创建），
是唯一真正 import onnxruntime 的地方。启动器主程序通过子进程调用它，
自身从不 import onnxruntime，避免污染/被污染系统 Python 环境。

用法：
    <venv_python> wd14_infer_cli.py --model_dir <模型目录> --input_json <输入> --output_json <输出>

input_json 格式:
    {"images": [路径,...], "general_thresh": 0.35, "character_thresh": 0.85, "include_rating": false}
output_json 格式:
    [{"path":..., "ok": true, "tag_string":..., "rating": [name, prob] 或 null}, ...]

进度汇报：处理完每一张图后，会在 stdout 额外打印一行
    ##ITEM## {"path":..., "ok":..., ...}
（跟上面 output_json 里单个元素同结构），并立即 flush。父进程
（wd14_tagger_tab.py 的 TagWorker）按行读取这些输出，就能实时汇报
"已处理 X/总数"，而不用等全部处理完才有反应——之前的实现是父进程
一次性 communicate() 阻塞到子进程整个退出，界面上长时间只显示一句
"正在隔离环境中反推 N 张图片"，跟卡死没有区别，尤其是精度更高、速度
更慢的 eva02-large 模型批量跑几十上百张图时特别明显。
"""
import argparse
import json
import os
import sys
import traceback

# 这个脚本在 Windows 上是被父进程用 subprocess.Popen(..., encoding="utf-8")
# 起来的子进程——但那个 encoding 参数只决定"父进程怎么解码收到的字节"，
# 完全不影响"子进程自己用什么编码去写 stdout"。子进程自己的 sys.stdout
# 默认走的是系统控制台代码页（简体中文 Windows 是 GBK/CP936），一旦文件名
# 或路径里出现 GBK 编不了的字符（比如国内常见的 pixiv 图集文件名里混着
# ❤ 💙 🔥 这类 emoji），print() 直接 UnicodeEncodeError 崩溃退出，
# 且这个崩溃发生在处理任何一张图之前，反推整批全部失败。
# 显式把子进程自己的 stdout/stderr 锁定成 UTF-8，就不会再受运行环境的
# 系统代码页影响（Python 3.7+ 支持 reconfigure，errors="replace" 兜底，
# 真遇到 UTF-8 都编不出来的极端字符也不会让脚本崩溃）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wd14_tagger_core as wt  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir", required=True)
    parser.add_argument("--input_json", required=True)
    parser.add_argument("--output_json", required=True)
    args = parser.parse_args()

    with open(args.input_json, "r", encoding="utf-8") as f:
        payload = json.load(f)

    results = []
    try:
        tagger = wt.WD14Tagger(args.model_dir)
    except Exception as e:
        # 模型加载失败，所有图片统一标记失败并写出，不让整个子进程崩溃到没有输出
        err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        for path in payload.get("images", []):
            results.append({"path": path, "ok": False, "error": f"模型加载失败: {err}"})
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False)
        sys.exit(1)

    from PIL import Image

    total = len(payload.get("images", []))
    for idx, path in enumerate(payload.get("images", []), 1):
        try:
            img = Image.open(path)
            r = tagger.tag_image(
                img,
                payload.get("general_thresh", 0.35),
                payload.get("character_thresh", 0.85),
                payload.get("include_rating", False),
            )
            item = {
                "path": path,
                "ok": True,
                "tag_string": r["tag_string"],
                "rating": list(r["rating"]) if r.get("rating") else None,
            }
        except Exception as e:
            err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            item = {"path": path, "ok": False, "error": err}

        results.append(item)
        # 实时进度行：father 进程按行读取 stdout 解析这个前缀，
        # 界面上就能立刻显示 "已处理 idx/total"，不用等全部跑完。
        print("##ITEM##" + json.dumps({**item, "index": idx, "total": total}, ensure_ascii=False))
        sys.stdout.flush()

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 兜底：任何没预料到的崩溃也打印完整堆栈到 stderr，方便主程序捕获展示
        traceback.print_exc()
        sys.exit(1)
