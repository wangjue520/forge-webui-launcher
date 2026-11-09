# -*- coding: utf-8 -*-
"""
启动器文件夹整个搬了位置（整合包解压到别的盘 / 别的目录）后，修好启动器自己的 .venv。

venv 不能直接搬：.venv\\pyvenv.cfg 里的 home 写的是建 venv 时 Python 的绝对路径，
.venv\\Scripts\\python.exe 启动时按它去找 Python，路径不对就直接起不来，bat 会以为
.venv 坏了、删掉重建、重新联网装依赖。启动器自带的便携 Python（python\\）本身可以随便搬，
所以只要把 pyvenv.cfg 里的路径改成它现在的位置就行。

由「启动WWY启动器.bat」在检查 .venv 之前用 python\\python.exe -I 调用，只用标准库。
用法：python.exe -I venv_relocate.py <启动器目录>
"""
import os
import sys


def relocate(launcher_dir):
    """返回 True = 改过；False = 不需要改 / 没法改"""
    launcher_dir = os.path.abspath(launcher_dir)
    py_dir = os.path.join(launcher_dir, "python")
    py_exe = os.path.join(py_dir, "python.exe")
    venv_dir = os.path.join(launcher_dir, ".venv")
    cfg_path = os.path.join(venv_dir, "pyvenv.cfg")
    if not (os.path.isfile(py_exe) and os.path.isfile(cfg_path)):
        return False
    with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    values = {}
    for line in lines:
        k, sep, v = line.partition("=")
        if sep:
            values[k.strip().lower()] = v.strip()
    home = values.get("home", "")
    if home and os.path.normcase(os.path.normpath(home)) == os.path.normcase(py_dir):
        return False
    if home and os.path.isfile(os.path.join(home, "python.exe")):
        # 原来那个 Python 还在（venv 本来就不是用自带 Python 建的）：别动
        return False
    new = {"home": py_dir, "executable": py_exe,
           "command": f"{py_exe} -m venv {venv_dir}"}
    out, seen = [], set()
    for line in lines:
        k, sep, _v = line.partition("=")
        key = k.strip().lower()
        if sep and key in new:
            out.append(f"{key} = {new[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key in ("home", "executable", "command"):
        if key not in seen and (key == "home" or key in values):
            out.append(f"{key} = {new[key]}")
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, cfg_path)
    return True


if __name__ == "__main__":
    try:
        changed = relocate(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        if changed:
            print("[启动器] 启动器换了位置，已把 .venv 指到现在的 python 目录")
    except Exception as e:      # 修不好也别挡启动：bat 后面还会检测 .venv、坏了自动重建
        print(f"[启动器] 修正 .venv 路径失败：{e}")
    sys.exit(0)
