#Requires -Version 3.0
# 解除启动器运行环境里文件的"来自网络"标记（Zone.Identifier 备用数据流）。
# 供 启动WWY启动器.bat 的 :check_clr 调用。
# 为什么需要：用资源管理器解压网上下载的 zip、或从别人那里拷来整个目录时，
# 文件会带上这个标记；.NET Framework 拒绝加载带标记的程序集，pywebview 依赖的
# pythonnet 就会报 "Failed to resolve Python.Runtime.Loader.Initialize"，界面起不来。
# 只处理 .venv 和便携 python 两个目录（其它目录与界面能否启动无关，全扫太慢）。
param([Parameter(Mandatory = $true)][string]$LauncherDir)
$ErrorActionPreference = 'SilentlyContinue'
foreach ($sub in @('.venv', 'python')) {
    $dir = Join-Path $LauncherDir $sub
    if (Test-Path -LiteralPath $dir) {
        Get-ChildItem -LiteralPath $dir -Recurse -File -Force | Unblock-File
    }
}
exit 0
