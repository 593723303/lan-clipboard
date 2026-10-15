# 启动局域网剪贴板
# 用法: pwsh start.ps1 [-Port 8900] [-LocalOnly] [-NoBrowser] [-MaxFileMb 200] [-Inbox <目录>] [-NoInbox]

param(
    [int]$Port = 8900,
    [switch]$LocalOnly,   # 只给本机用，不对局域网开放
    [switch]$NoBrowser,
    [int]$MaxFileMb = 0,  # 单个文件上限，0 = 用服务端默认的 200 MB
    [string]$Inbox = "",  # 收到的文件照原名落到这个目录，留空 = 系统下载目录
    [switch]$NoInbox      # 不往收件箱落，只留在网页上
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 端口已经被占着，多半是上一次还开着，直接把浏览器指过去就行
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host "剪贴板已经在 $Port 端口运行中" -ForegroundColor Yellow
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Port" }
    return
}

# 手机连不上十有八九是防火墙没放行，提前说一声，省得对着页面干等
if (-not $LocalOnly) {
    $rule = Get-NetFirewallRule -DisplayName "局域网剪贴板" -ErrorAction SilentlyContinue
    if (-not $rule) {
        Write-Host "提示：手机第一次连不上的话，用管理员 PowerShell 跑一次下面这条放行规则 ——" -ForegroundColor Yellow
        Write-Host "  New-NetFirewallRule -DisplayName '局域网剪贴板' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private" -ForegroundColor DarkGray
        Write-Host ""
    }
}

$serverArgs = @("$scriptDir\server.py", "--port", $Port)
if ($LocalOnly) { $serverArgs += @("--host", "127.0.0.1") }
if ($NoBrowser) { $serverArgs += "--no-browser" }
if ($MaxFileMb -gt 0) { $serverArgs += @("--max-file-mb", $MaxFileMb) }
if ($NoInbox) { $serverArgs += "--no-inbox" }
elseif ($Inbox) { $serverArgs += @("--inbox", $Inbox) }

python @serverArgs
