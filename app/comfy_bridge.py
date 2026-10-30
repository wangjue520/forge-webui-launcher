"""
往 ComfyUI 的 custom_nodes 里放「WWY 启动器桥接插件」（源文件在 assets/comfyui/wwy_launcher_bridge）。

新版 ComfyUI 网页发现缺节点 / 缺模型直接拦着不让运行，后端日志里没有任何记录；桥接插件在
网页里检查工作流，把结果打成一行 [WWY诊断] 日志，报错诊断就能接住（见 diagnose.py 的 comfy_bridge 规则）。

启动 ComfyUI 和部署 ComfyUI 时调用 ensure()：没有就放进去，启动器更新后内容变了就覆盖。
用户停用了（文件夹改名成 .disabled，ComfyUI-Manager 停用也是这么做的）就不再放回去。
"""
import os
import shutil

NAME = "wwy_launcher_bridge"
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "comfyui", NAME)


def _files(root):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in fns:
            if not fn.endswith((".pyc", ".pyo")):
                out.append(os.path.relpath(os.path.join(dp, fn), root))
    return sorted(out)


def _same(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def status(comfy_dir):
    """"missing" / "outdated" / "ok" / "disabled" / "no_comfy" """
    cn = os.path.join(comfy_dir or "", "custom_nodes")
    if not comfy_dir or not os.path.isdir(cn):
        return "no_comfy"
    if os.path.isdir(os.path.join(cn, NAME + ".disabled")):
        return "disabled"
    dst = os.path.join(cn, NAME)
    if not os.path.isdir(dst):
        return "missing"
    if all(_same(os.path.join(SRC, f), os.path.join(dst, f)) for f in _files(SRC)):
        return "ok"
    return "outdated"


def ensure(comfy_dir, log=None):
    """需要时放进去 / 更新。返回 status() 的值（放好了返回 "installed" / "updated"），出错返回 "error"。"""
    st = status(comfy_dir)
    if st not in ("missing", "outdated") or not os.path.isdir(SRC):
        return st
    dst = os.path.join(comfy_dir, "custom_nodes", NAME)
    try:
        for f in _files(SRC):
            os.makedirs(os.path.dirname(os.path.join(dst, f)), exist_ok=True)
            shutil.copyfile(os.path.join(SRC, f), os.path.join(dst, f))
    except OSError as e:
        if log:
            log(f"[启动器] 放置启动器桥接插件失败（不影响使用，只是报错诊断看不到网页里的缺节点 / 缺模型）：{e}\n")
        return "error"
    if log:
        if st == "missing":
            log("[启动器] 已放入启动器桥接插件 custom_nodes\\wwy_launcher_bridge：工作流缺节点 / 缺模型时，"
                "启动器的「诊断」能看到并一键处理（不加任何节点）\n")
        else:
            log("[启动器] 启动器桥接插件已更新\n")
    return "installed" if st == "missing" else "updated"
