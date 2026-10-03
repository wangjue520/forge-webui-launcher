# create_shortcuts.ps1 —— 给启动器的 bat 生成带图标的快捷方式
#
# 背景：.bat 批处理文件本身无法携带图标（Windows 只给 .exe / .lnk 显示自定义
# 图标），所以"给 bat 加图标"的正确做法是在 bat 旁边放一个指向它的 .lnk
# 快捷方式，图标设在快捷方式上。双击快捷方式与双击 bat 完全等效。
#
# 启动器只有一个启动 bat：启动WWY启动器.bat（放在文件夹旁边也能用，会自动
# 转交本体）。图标文件在 assets\wwy_launcher_icon.ico。
#
# 用法：
#   powershell -File create_shortcuts.ps1              # bat 旁边 + 桌面都建
#   powershell -File create_shortcuts.ps1 -DesktopOnly # 只在桌面建一个
#
# 重复运行是安全的（覆盖同名快捷方式），不会报错中断启动流程。
param(
    [switch]$DesktopOnly
)

$ErrorActionPreference = "SilentlyContinue"

$LauncherDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$IconPath    = Join-Path $LauncherDir "assets\wwy_launcher_icon.ico"
$EntryBat    = Join-Path $LauncherDir "启动WWY启动器.bat"
if (-not (Test-Path $IconPath)) { exit 0 }   # 没有图标就什么都不做
if (-not (Test-Path $EntryBat)) { exit 0 }

$ws = New-Object -ComObject WScript.Shell

function New-LauncherShortcut {
    param([string]$LnkPath)
    $s = $ws.CreateShortcut($LnkPath)
    $s.TargetPath       = $EntryBat
    $s.WorkingDirectory = $LauncherDir
    $s.IconLocation     = "$IconPath,0"
    $s.Description      = "WWY 启动器"
    $s.WindowStyle      = 1
    $s.Save()
}

if (-not $DesktopOnly) {
    # bat 旁边放一个同名快捷方式，在文件夹里直接显示图标
    New-LauncherShortcut (Join-Path $LauncherDir "启动WWY启动器.lnk")
}

# 桌面：双击 bat 首次运行时会自动建一个（bat 里控制只建一次）
$Desktop = [Environment]::GetFolderPath("Desktop")
New-LauncherShortcut (Join-Path $Desktop "WWY 启动器.lnk")

exit 0
