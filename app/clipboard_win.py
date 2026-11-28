# -*- coding: utf-8 -*-
"""
把文件放进 Windows 剪贴板（输出管理「复制图片」/ Ctrl+C）：
  · 文件列表（CF_HDROP）：在资源管理器里 Ctrl+V 粘贴出文件，QQ / 微信里粘贴成文件
  · 只有一张图时再放一份位图：聊天框、PS、画图里能直接粘贴成图片
用系统自带的 PowerShell + .NET（System.Windows.Forms.Clipboard，必须 STA），不需要装 pywin32。
路径通过 UTF-8 的临时 JSON 文件传过去，中文路径、空格、引号都不用转义。
"""
import json
import os
import subprocess
import tempfile

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_PS = r"""
param([string]$ListFile)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$req = Get-Content -LiteralPath $ListFile -Raw -Encoding UTF8 | ConvertFrom-Json
$files = New-Object System.Collections.Specialized.StringCollection
foreach ($p in $req.paths) { [void]$files.Add([string]$p) }
$data = New-Object System.Windows.Forms.DataObject
$data.SetFileDropList($files)
$img = $null
if ($req.image) {
  try { $img = [System.Drawing.Image]::FromFile([string]$req.image); $data.SetImage($img) } catch { }
}
$done = $false; $why = ''
for ($i = 0; $i -lt 5; $i++) {
  try { [System.Windows.Forms.Clipboard]::SetDataObject($data, $true); $done = $true; break }
  catch { $why = $_.Exception.Message; Start-Sleep -Milliseconds 150 }    # 别的程序正占着剪贴板：等一下再试
}
if ($img) { $img.Dispose() }
# 五次都没写进去就是失败：以前照样输出 ok，界面提示「已复制」，粘贴出来却是空的
if (-not $done) { Write-Output ("fail " + $why); exit 3 }
Write-Output ("ok " + $files.Count)
"""

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif")     # System.Drawing 读得了的（webp 读不了，只放文件）


def copy_files(paths, timeout=30):
    """返回 (ok, 说明)"""
    paths = [os.path.abspath(p) for p in paths or [] if p and os.path.isfile(p)]
    if not paths:
        return False, "文件已经不在了"
    if os.name != "nt":
        return False, "只支持 Windows"
    image = paths[0] if len(paths) == 1 and paths[0].lower().endswith(IMAGE_EXTS) else ""
    fd, lst = tempfile.mkstemp(prefix="wwy_clip_", suffix=".json")
    fd2, ps1 = tempfile.mkstemp(prefix="wwy_clip_", suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"paths": paths, "image": image}, f, ensure_ascii=False)
        with os.fdopen(fd2, "w", encoding="utf-8-sig") as f:      # 带 BOM：PowerShell 5.1 才按 UTF-8 读脚本
            f.write(_PS)
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                            "-File", ps1, lst], capture_output=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        if r.returncode == 0 and out.startswith("ok"):
            return True, f"已复制 {len(paths)} 个文件" + ("（也能直接粘贴成图片）" if image else "")
        if out.startswith("fail"):
            return False, "复制到剪贴板失败：剪贴板正被别的程序占用，稍后再试" + \
                (f"（{out[4:].strip()[:200]}）" if out[4:].strip() else "")
        err = (r.stderr or b"").decode("utf-8", "replace").strip() or out
        return False, "复制到剪贴板失败：" + (err[-300:] or f"返回码 {r.returncode}")
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"复制到剪贴板失败：{e}"
    finally:
        for p in (lst, ps1):
            try:
                os.remove(p)
            except OSError:
                pass
