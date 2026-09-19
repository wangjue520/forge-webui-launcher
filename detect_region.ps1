#Requires -Version 3.0
# 查询出口 IP 的国家代码（如 CN / US / JP），写到标准输出；查询失败不输出。
# 供 start一键启动.bat 的 :detect_region 调用，决定 pip 镜像源顺序：
# 国内 -> 镜像优先；国外 -> 官方源优先；查不到 -> 按国内处理（bat 里判断）。
# 为什么不内嵌在 bat 里：cmd 解析 for /f 的命令串时不认引号，PowerShell 代码
# 里的圆括号会被当成 for 语法的括号，报"此时不应有 xxx"——独立 ps1 没这个问题。
$ErrorActionPreference = 'SilentlyContinue'
$urls = @(
    'https://ipapi.co/country/',
    'http://ip-api.com/line/?fields=countryCode',
    'https://ipinfo.io/country'
)
foreach ($u in $urls) {
    try {
        $code = Invoke-RestMethod -Uri $u -TimeoutSec 5
        if ($code) {
            $code = "$code".Trim().ToUpper()
            if ($code -match '^[A-Z]{2}$') {
                Write-Output $code
                exit 0
            }
        }
    }
    catch { }
}
