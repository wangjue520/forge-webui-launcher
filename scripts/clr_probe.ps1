#Requires -Version 3.0
# 找出 pythonnet 加载失败的真正原因。供 启动WWY启动器.bat 的 :check_clr 在自动修复无效后调用。
#
# 为什么需要：pythonnet 靠 clr_loader 的 ClrLoader.dll 把 Python.Runtime.dll 载进 .NET Framework，
# ClrLoader 的发行版把异常整个吞掉（Print 在 Release 编译里是空函数），Python 那边只剩一句
# "Failed to resolve Python.Runtime.Loader.Initialize"——文件被杀毒软件删了、被智能应用控制拦了、
# 放在网络盘上、.NET 坏了，全是这一句，用户截图里看不出任何东西。
# 这里在 PowerShell 5.1（同样跑在 .NET Framework 4.x 上）里按 ClrLoader 的步骤重走一遍：
#   AssemblyName.GetAssemblyName -> AppDomain.Load -> GetType('Python.Runtime.Loader')
#   -> 按 int Initialize(IntPtr, int) 取函数（ClrLoader 绑的 EntryPoint 委托就是这个签名）
# 把真实的 .NET 异常和对应的处理办法打出来。
param([Parameter(Mandatory = $true)][string]$LauncherDir)
$ErrorActionPreference = 'Stop'

function Say([string]$text, [string]$color = 'Gray') { Write-Host "      $text" -ForegroundColor $color }

function Advice([string]$text) {
    Write-Host ''
    Write-Host '    → 处理办法：' -ForegroundColor Yellow
    foreach ($line in $text -split "`n") { Write-Host "      $line" -ForegroundColor Yellow }
}

# 异常链里最里层、最能说明问题的那个（TargetInvocation 之类的包装层跳过）
function Inner($e) {
    while ($e.InnerException -and ($e -is [System.Reflection.TargetInvocationException] -or
            $e -is [System.Management.Automation.MethodInvocationException] -or
            $e -is [System.TypeInitializationException])) { $e = $e.InnerException }
    return $e
}

function Explain($e, [string]$dll) {
    $e = Inner $e
    $hr = '0x{0:X8}' -f $e.HResult
    Say ("{0}（{1}）" -f $e.GetType().FullName, $hr) 'Red'
    Say $e.Message 'Red'
    if ($e -is [System.Reflection.ReflectionTypeLoadException]) {
        foreach ($le in $e.LoaderExceptions | Select-Object -First 3) { Say ("  · " + $le.Message) 'Red' }
    }
    $fileName = ''
    if ($e -is [System.IO.FileNotFoundException] -or $e -is [System.IO.FileLoadException]) { $fileName = [string]$e.FileName }

    switch -Regex ($hr) {
        '0x800711C7|0x800711CA|0x800711C9' {
            Advice ("这个组件被 Windows 的「智能应用控制」或应用控制策略拦截了。`n" +
                "打开「Windows 安全中心 → 应用和浏览器控制 → 智能应用控制」，看是否为「开启」；`n" +
                "公司电脑可能是管理员下发的策略，需要找管理员放行。")
            return
        }
        '0x80131515' {
            Advice (".NET 把这个文件当成「来自网络」的程序集，拒绝加载。`n" +
                "启动器会在 python 文件夹里写 python.exe.config / pythonw.exe.config 放行它；`n" +
                "走到这一步说明没写成功：检查 python 文件夹能不能写入（右键 bat →「以管理员身份运行」试试），`n" +
                "或者把启动器整个文件夹放到本机硬盘的普通目录（不要放网络盘 / 网盘同步目录）。")
            return
        }
        '0x80070005' {
            Advice ("读取被拒绝，多半是杀毒软件（360 / 火绒 / 电脑管家 / Windows Defender 等）拦截。`n" +
                "把启动器文件夹加入杀毒软件的信任区 / 白名单，再重新双击「启动WWY启动器.bat」。")
            return
        }
    }
    if ($e -is [System.BadImageFormatException]) {
        Advice ("文件已损坏（可能下载不完整，或被杀毒软件改动过）。`n" +
            "删除启动器目录下的 .venv 文件夹，再重新双击「启动WWY启动器.bat」重新安装。")
        return
    }
    if ($e -is [System.IO.FileNotFoundException] -or $e -is [System.IO.FileLoadException]) {
        if ($fileName -and $fileName -notmatch 'Python\.Runtime') {
            Advice (".NET Framework 缺少组件「$fileName」，系统里的 .NET 不完整（常见于精简版系统）。`n" +
                "安装或修复 .NET Framework 4.8，装完重启电脑：`n" +
                "https://dotnet.microsoft.com/download/dotnet-framework/net48")
        } else {
            Advice ("文件读不出来，多半被杀毒软件隔离或拦截了。`n" +
                "到杀毒软件的隔离区恢复它，并把启动器文件夹加入信任区，再重新双击「启动WWY启动器.bat」。")
        }
        return
    }
    if ($e -is [System.TypeLoadException] -or $e -is [System.MissingMethodException] -or $e -is [System.ArgumentException]) {
        Advice ("pythonnet 和 clr_loader 的版本对不上。`n" +
            "删除启动器目录下的 .venv 文件夹，再重新双击「启动WWY启动器.bat」重新安装。")
        return
    }
    Advice ("请把这个窗口截图反馈给作者。")
}

# ---------- 找文件 ----------
$site = Join-Path $LauncherDir '.venv\Lib\site-packages'
$dll = Join-Path $site 'pythonnet\runtime\Python.Runtime.dll'
$loader = Join-Path $site ('clr_loader\ffi\dlls\' + $(if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { 'x86' }) + '\ClrLoader.dll')
$dll = [System.IO.Path]::GetFullPath($dll)

# 智能应用控制（Windows 11）：0 关闭 / 1 开启 / 2 评估模式。只显示，不判断
try {
    $sac = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState).VerifiedAndReputablePolicyState
    $sacText = @{ 0 = '关闭'; 1 = '开启'; 2 = '评估模式' }[[int]$sac]
    if ($sacText) { Say "Windows 智能应用控制：$sacText" }
} catch { }

# 网络位置（映射的网络驱动器 / UNC 路径）：.NET Framework 默认不从这里加载程序集
try {
    $root = [System.IO.Path]::GetPathRoot($dll)
    if ($root.StartsWith('\\') -or ([System.IO.DriveInfo]::new($root).DriveType -eq 'Network')) {
        Say "启动器在网络位置上：$root" 'Red'
        Advice "把启动器整个文件夹复制到本机硬盘（比如 D:\WWY启动器），从那里重新双击「启动WWY启动器.bat」。"
        exit 1
    }
} catch { }

foreach ($f in @($dll, $loader)) {
    if (-not (Test-Path -LiteralPath $f)) {
        Say "找不到文件：$f" 'Red'
        Advice ("文件不见了，多半是被杀毒软件当成可疑文件删除 / 隔离了。`n" +
            "到杀毒软件的隔离区恢复它，并把启动器文件夹加入信任区；`n" +
            "或者删除 .venv 文件夹后重新双击「启动WWY启动器.bat」重新安装（装完马上又没了就是杀毒软件在删）。")
        exit 1
    }
}

# 能不能读（杀毒软件常见的拦法是不删文件、只拒绝读取）
try {
    $fs = [System.IO.File]::OpenRead($dll); $fs.Close()
} catch {
    Say "无法读取：$dll" 'Red'
    Explain $_.Exception $dll
    exit 1
}

# ---------- 按 ClrLoader 的步骤重走一遍 ----------
$step = ''
try {
    $step = '读取程序集信息'
    $name = [System.Reflection.AssemblyName]::GetAssemblyName($dll)
    $step = '加载 Python.Runtime.dll'
    $asm = [System.AppDomain]::CurrentDomain.Load($name)
    $step = '查找 Python.Runtime.Loader'
    $type = $asm.GetType('Python.Runtime.Loader', $true)
    $step = '查找 Initialize 入口'
    $m = $type.GetMethod('Initialize', [Type[]]@([IntPtr], [int]))
    if (-not $m -or $m.ReturnType -ne [int]) {
        throw [System.MissingMethodException]::new('Python.Runtime.Loader', 'Initialize(IntPtr, int)')
    }
} catch {
    Say "失败步骤：$step" 'Red'
    Explain $_.Exception $dll
    exit 1
}

# 依赖的系统程序集（netstandard 等）。.NET 不完整的精简版系统会缺
foreach ($ref in $asm.GetReferencedAssemblies()) {
    try { [void][System.Reflection.Assembly]::Load($ref) } catch {
        Say "缺少依赖：$($ref.FullName)" 'Red'
        Explain $_.Exception $dll
        exit 1
    }
}

Say '在 PowerShell 里能正常加载 Python.Runtime.dll，系统的 .NET 没有问题。' 'Green'
Advice ("问题出在 python.exe 进程里，最常见的是杀毒软件（360 / 火绒 / 电脑管家等）不让 python.exe 加载 .NET 组件。`n" +
    "把启动器文件夹加入杀毒软件的信任区 / 白名单，再重新双击「启动WWY启动器.bat」；`n" +
    "还不行请把这个窗口截图反馈给作者。")
exit 2
