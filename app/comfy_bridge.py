"""
往 ComfyUI 的 custom_nodes 里放「WWY 启动器桥接插件」（源文件在 assets/comfyui/wwy_launcher_bridge）。

新版 ComfyUI 网页发现缺节点 / 缺模型直接拦着不让运行，后端日志里没有任何记录；桥接插件在
网页里检查工作流，把结果打成一行 [WWY诊断] 日志，报错诊断就能接住（见 diagnose.py 的 comfy_bridge 规则）。

启动 ComfyUI 和部署 ComfyUI 时调用 ensure()：没有就放进去，启动器更新后内容变了就覆盖。
用户停用了（文件夹改名成 .disabled，ComfyUI-Manager 停用也是这么做的）就不再放回去。

WebUI / Forge 也有一个同名的桥接扩展（源文件在 assets/webui/wwy_launcher_bridge），
启动 WebUI 时 ensure_webui() 放进 extensions：图片信息页 / 输出管理器「发送到 WebUI」靠它把参数送进网页。
"""
import os
import shutil

NAME = "wwy_launcher_bridge"
_ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
SRC = os.path.join(_ASSETS, "comfyui", NAME)
WEBUI_SRC = os.path.join(_ASSETS, "webui", NAME)


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


def _status_in(parent, src, missing_label):
    if not os.path.isdir(parent):
        return missing_label
    if os.path.isdir(os.path.join(parent, NAME + ".disabled")):
        return "disabled"
    dst = os.path.join(parent, NAME)
    if not os.path.isdir(dst):
        return "missing"
    if all(_same(os.path.join(src, f), os.path.join(dst, f)) for f in _files(src)):
        return "ok"
    return "outdated"


def _copy_into(parent, src):
    dst = os.path.join(parent, NAME)
    for f in _files(src):
        os.makedirs(os.path.dirname(os.path.join(dst, f)), exist_ok=True)
        shutil.copyfile(os.path.join(src, f), os.path.join(dst, f))


def status(comfy_dir):
    """"missing" / "outdated" / "ok" / "disabled" / "no_comfy" """
    if not comfy_dir:
        return "no_comfy"
    return _status_in(os.path.join(comfy_dir, "custom_nodes"), SRC, "no_comfy")


def webui_status(webui_root):
    """"missing" / "outdated" / "ok" / "disabled" / "no_webui" """
    if not webui_root or not os.path.isdir(webui_root):
        return "no_webui"
    if not os.path.isdir(os.path.join(webui_root, "extensions")):
        return "missing"                # 刚装好的 WebUI 可能还没有 extensions 文件夹，放的时候顺带建
    return _status_in(os.path.join(webui_root, "extensions"), WEBUI_SRC, "no_webui")


def ensure_webui(webui_root, log=None):
    """WebUI 版的 ensure()：放进 extensions / 更新。返回值同 ensure()"""
    st = webui_status(webui_root)
    if st not in ("missing", "outdated") or not os.path.isdir(WEBUI_SRC):
        return st
    try:
        _copy_into(os.path.join(webui_root, "extensions"), WEBUI_SRC)
    except OSError as e:
        if log:
            log(f"[启动器] 放置启动器桥接扩展失败（不影响使用，只是「发送到 WebUI」用不了）：{e}\n")
        return "error"
    if log:
        if st == "missing":
            log("[启动器] 已放入启动器桥接扩展 extensions\\wwy_launcher_bridge：图片信息 / 输出管理里"
                "「发送到 WebUI」靠它把参数送进网页（不加界面、不改设置）\n")
        else:
            log("[启动器] 启动器桥接扩展已更新\n")
    return "installed" if st == "missing" else "updated"


def ensure(comfy_dir, log=None):
    """需要时放进去 / 更新。返回 status() 的值（放好了返回 "installed" / "updated"），出错返回 "error"。"""
    st = status(comfy_dir)
    if st not in ("missing", "outdated") or not os.path.isdir(SRC):
        return st
    try:
        _copy_into(os.path.join(comfy_dir, "custom_nodes"), SRC)
    except OSError as e:
        if log:
            log(f"[启动器] 放置启动器桥接插件失败（不影响使用，只是报错诊断看不到网页里的缺节点 / 缺模型）：{e}\n")
        return "error"
    if log:
        if st == "missing":
            log("[启动器] 已放入启动器桥接插件 custom_nodes\\wwy_launcher_bridge：工作流缺节点 / 缺模型时，"
                "启动器的「诊断」能看到并一键处理；「发送到 ComfyUI」也靠它（不加任何节点）\n")
        else:
            log("[启动器] 启动器桥接插件已更新\n")
    return "installed" if st == "missing" else "updated"
