#Requires -Version 3.0
# 【注意】本文件必须保存为「UTF-8 带 BOM」（同 bootstrap_python.ps1：PowerShell 5.1 读无 BOM 的中文脚本会乱码）。
#
# 首次启动部署启动器自己的运行环境（便携 Python + 依赖）前先测速，自动选最快的下载源。
# 由 启动WWY启动器.bat 的 :speedtest 调用，只在需要联网下载时跑一次。
#
#   1. 同时测各 PyPI 源（官方 + 国内镜像，下同一个 numpy wheel 的前 2MB）和
#      各 GitHub 线路（直连 + 加速代理，下 MinGit 发布包的前 2MB，-NeedGithub 时才测）
#   2. PyPI 源按速度从快到慢写进 -OutFile（每行「索引地址|显示名」），bat 按这个顺序装依赖
#   3. 所有源都很慢（按最快的源估算下载要超过 15 分钟）：
#        先提示开启或关闭梯子（代理）后按回车重新测速；
#        还是慢就问「预计部署时间过长，是否继续部署？（Y/N）」——选 Y 照常慢慢部署，选 N 退出
#
# 退出码：0 继续部署；2 用户选了 N（不部署了）。测速本身出问题一律按 0 处理，不挡启动。
param(
    [Parameter(Mandatory = $true)][string]$OutFile,
    [switch]$NeedGithub,
    # 预计下载超过多少秒算「很慢」（测试时可以调小）
    [int]$SlowSeconds = 900
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# 与 mirror_manager.PYPI_MIRRORS / GITHUB_PROXIES 保持一致（tests/test_source_speed.py 会核对）
$pypiSources = @(
    @{ Name = '官方源';   Index = 'https://pypi.org/simple';                       Files = 'https://files.pythonhosted.org/' },
    @{ Name = '清华大学'; Index = 'https://pypi.tuna.tsinghua.edu.cn/simple';      Files = 'https://pypi.tuna.tsinghua.edu.cn/' },
    @{ Name = '阿里云';   Index = 'https://mirrors.aliyun.com/pypi/simple';        Files = 'https://mirrors.aliyun.com/pypi/' },
    @{ Name = '腾讯云';   Index = 'https://mirrors.cloud.tencent.com/pypi/simple'; Files = 'https://mirrors.cloud.tencent.com/pypi/' }
)
$pypiProbePath = 'packages/cb/3b/d58c12eafcb298d4e6d0d40216866ab15f59e55d148a5658bb3132311fcf/numpy-2.2.6-cp313-cp313-win_amd64.whl'
$githubProbe = 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/MinGit-2.56.0.4-64-bit.zip'
$githubProxies = @('https://ghfast.top/', 'https://gh-proxy.com/', 'https://ghproxy.net/', 'https://gh.llkk.cc/', 'https://gh-proxy.org/')

# 首次启动要下的量：便携 Python 约 30MB（GitHub），依赖约 80MB（PyPI）
$planGithub = 30MB
$planPypi = 80MB

$sysCurl = Join-Path $env:SystemRoot 'System32\curl.exe'
if (-not (Test-Path $sysCurl)) {
    # 没有 curl（Win10 1803 以前）：不测了，bat 按地区选源
    exit 0
}

function Measure-Urls([string[]]$urls) {
    # 同时测，各下最多 2MB / 6 秒，返回同顺序的速度（字节/秒），连不上为 0
    $probes = @()
    foreach ($u in $urls) {
        $out = [IO.Path]::GetTempFileName()
        $err = [IO.Path]::GetTempFileName()
        $curlArgs = '-sL --connect-timeout 5 --max-time 8 -r 0-2097151 -o NUL -w "%{http_code} %{speed_download}" "' + $u + '"'
        $proc = Start-Process -FilePath $sysCurl -ArgumentList $curlArgs -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $probes += @{ Url = $u; Proc = $proc; Out = $out; Err = $err }
    }
    $speeds = @()
    foreach ($p in $probes) {
        if (-not $p.Proc.WaitForExit(11000)) {
            try { $p.Proc.Kill() } catch { }
            $p.Proc.WaitForExit(2000) | Out-Null
        }
        $speed = 0.0
        $parts = ("" + (Get-Content -Path $p.Out -Raw -ErrorAction SilentlyContinue)).Trim() -split '\s+'
        if ($parts.Count -eq 2 -and ($parts[0] -eq '200' -or $parts[0] -eq '206')) {
            [double]::TryParse($parts[1], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$speed) | Out-Null
        }
        Remove-Item $p.Out, $p.Err -Force -ErrorAction SilentlyContinue
        $speeds += $speed
    }
    return , $speeds
}

function Format-Speed([double]$bps) {
    if ($bps -ge 1MB) { return ('{0:N1} MB/s' -f ($bps / 1MB)) }
    return ('{0:N0} KB/s' -f ($bps / 1KB))
}

function Format-Duration([double]$sec) {
    if ([double]::IsInfinity($sec)) { return '无法估计（有一类源全部连不上）' }
    $min = [int][Math]::Ceiling($sec / 60)
    if ($min -lt 1) { return '不到 1 分钟' }
    if ($min -lt 60) { return ("$min 分钟") }
    $h = [int][Math]::Floor($min / 60); $m = $min % 60
    if ($m -eq 0) { return ("$h 小时") }
    return ("$h 小时 $m 分钟")
}

function Invoke-SpeedTest {
    Write-Host ''
    Write-Host '  正在测各下载源的速度（约 10 秒），之后自动选最快的 ...'
    $urls = @()
    foreach ($s in $pypiSources) { $urls += ($s.Files + $pypiProbePath) }
    $pypiSpeeds = Measure-Urls $urls
    $pypi = @()
    for ($i = 0; $i -lt $pypiSources.Count; $i++) {
        $pypi += New-Object PSObject -Property @{ Name = $pypiSources[$i].Name; Index = $pypiSources[$i].Index; Speed = [double]$pypiSpeeds[$i]; Order = $i }
    }
    $pypi = @($pypi | Sort-Object -Property @{ Expression = 'Speed'; Descending = $true }, @{ Expression = 'Order'; Descending = $false })
    $line = ($pypi | ForEach-Object { if ($_.Speed -gt 0) { $_.Name + ' ' + (Format-Speed $_.Speed) } else { $_.Name + ' 不通' } }) -join '，'
    Write-Host ('    PyPI（依赖包）：' + $line)

    $githubBest = $null
    if ($NeedGithub) {
        $gurls = @($githubProbe)
        foreach ($p in $githubProxies) { $gurls += ($p + $githubProbe) }
        $gs = Measure-Urls $gurls
        $names = @('GitHub 直连') + @($githubProxies | ForEach-Object { $_.Split('/')[2] })
        $parts = @()
        $best = 0.0
        for ($i = 0; $i -lt $gurls.Count; $i++) {
            if ($gs[$i] -gt $best) { $best = [double]$gs[$i] }
            if ($gs[$i] -gt 0) { $parts += ($names[$i] + ' ' + (Format-Speed $gs[$i])) } else { $parts += ($names[$i] + ' 不通') }
        }
        Write-Host ('    GitHub（便携 Python）：' + ($parts -join '，'))
        $githubBest = $best
    }

    $est = 0.0
    $pb = [double]$pypi[0].Speed
    if ($pb -le 0) { $est = [double]::PositiveInfinity } else { $est += $planPypi / $pb }
    if ($NeedGithub) {
        if ($githubBest -le 0) { $est = [double]::PositiveInfinity } else { $est += $planGithub / $githubBest }
    }
    Write-Host ('    按最快的源估算，下载约需 ' + (Format-Duration $est))
    return @{ Pypi = $pypi; Estimate = $est }
}

function Save-Order($result) {
    # 能连上的按速度排在前面；一个都连不上就按原顺序全写上（让 pip 自己再试一遍）
    $lines = @()
    foreach ($s in $result.Pypi) { if ($s.Speed -gt 0) { $lines += ($s.Index + '|' + $s.Name) } }
    if ($lines.Count -eq 0) { foreach ($s in $pypiSources) { $lines += ($s.Index + '|' + $s.Name) } }
    try {
        $dir = Split-Path -Parent $OutFile
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        # bat 的 for /f 按系统编码读，显示名用 GBK（936）写才不乱码
        [IO.File]::WriteAllLines($OutFile, [string[]]$lines, [Text.Encoding]::GetEncoding(936))
    }
    catch { }
}

function Read-YesNo([string]$prompt) {
    # 没有可交互的控制台（输入被重定向）时按「是」处理：宁可慢慢部署，也不直接拒绝
    try { if ([Console]::IsInputRedirected) { Write-Host ($prompt + ' Y（无法交互，默认继续）'); return $true } } catch { }
    $empty = 0
    while ($true) {
        $a = ("" + (Read-Host $prompt)).Trim().ToUpper()
        if ($a -eq 'Y' -or $a -eq 'YES' -or $a -eq '是') { return $true }
        if ($a -eq 'N' -or $a -eq 'NO' -or $a -eq '否') { return $false }
        if (-not $a) { $empty++; if ($empty -ge 5) { return $true } }
        Write-Host '    请输入 Y 或 N'
    }
}

try {
    $r = Invoke-SpeedTest
    Save-Order $r
    if ($r.Estimate -le $slowSeconds) { exit 0 }

    Write-Host ''
    Write-Host '  [!] 所有下载源都很慢。'
    $px = $null
    try { $px = [Net.WebRequest]::GetSystemWebProxy().GetProxy([Uri]'https://pypi.org/') } catch { }
    if (($px -and $px.Host -ne 'pypi.org') -or $env:HTTPS_PROXY -or $env:HTTP_PROXY) {
        Write-Host '      检测到开着代理（梯子）：可以试试先关掉梯子（国内镜像走梯子常常更慢）。'
    }
    else {
        Write-Host '      可以试试开启梯子（代理）；梯子开的是 TUN / 虚拟网卡模式的话，也可以关掉试试。'
    }
    $ans = ''
    try { if ([Console]::IsInputRedirected) { $ans = 'S' } } catch { }
    if ($ans -ne 'S') { $ans = ("" + (Read-Host '      切换好后直接按回车重新测速（输入 S 跳过）')).Trim().ToUpper() }
    if ($ans -ne 'S') {
        $r = Invoke-SpeedTest
        Save-Order $r
        if ($r.Estimate -le $slowSeconds) {
            Write-Host '  [√] 重新测速后速度正常，继续部署'
            exit 0
        }
    }
    Write-Host ''
    Write-Host ('  按现在的速度，光下载就要约 ' + (Format-Duration $r.Estimate) + '。')
    if (Read-YesNo '  预计部署时间过长，是否继续部署？（Y/N）') {
        Write-Host '  好的，继续部署（会慢一些，请耐心等待；中途断了会自动换源重试）'
        exit 0
    }
    exit 2
}
catch {
    Write-Host ('  [测速] 出错，按原来的规则选源：' + $_)
    exit 0
}
