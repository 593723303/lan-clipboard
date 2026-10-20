<#
.SYNOPSIS
    局域网剪贴板 —— 桌面快捷方式一键安装/卸载

.DESCRIPTION
    在桌面创建「局域网剪贴板」图标，双击即启动服务并打开网页。
    路径自适应：脚本放在哪，快捷方式就指向哪，换机器 clone 下来直接可用。

.EXAMPLE
    pwsh install.ps1
    pwsh install.ps1 -Uninstall
#>
param(
    [switch]$Uninstall,
    [string]$Name = '局域网剪贴板'   # 桌面图标显示名
)

$ErrorActionPreference = 'Stop'

$ProjDir  = $PSScriptRoot
$StartPs1 = Join-Path $ProjDir 'start.ps1'
$IconFile = Join-Path $ProjDir 'clipboard.ico'
$Desktop  = [Environment]::GetFolderPath('Desktop')
$LnkPath  = Join-Path $Desktop "$Name.lnk"
$WS = New-Object -ComObject WScript.Shell

# ---------- 卸载 ----------
if ($Uninstall) {
    if (Test-Path $LnkPath) {
        [System.IO.File]::Delete($LnkPath)
        Write-Host "  已移除桌面图标：$Name" -ForegroundColor Yellow
    } else {
        Write-Host "  桌面上没有找到「$Name」" -ForegroundColor DarkGray
    }
    Write-Host "`n卸载完成。（正在运行的服务不受影响）" -ForegroundColor Green
    exit 0
}

# ---------- 前置检查 ----------
if (-not (Test-Path $StartPs1)) { throw "找不到 start.ps1，请在项目根目录运行本脚本" }

# PowerShell 宿主：优先 pwsh 7+，回退到系统自带的 Windows PowerShell
$Shell = (Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $Shell) {
    $Shell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    Write-Host "  未找到 pwsh 7，改用 Windows PowerShell" -ForegroundColor DarkGray
}
Write-Host "  PowerShell: $Shell" -ForegroundColor Cyan

# Python 检查（start.ps1 最终要调 python）
if (-not (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    Write-Host "  [警告] PATH 中找不到 python.exe，双击图标会启动失败" -ForegroundColor Yellow
}

# ---------- 创建桌面快捷方式 ----------
# 出错时留住窗口，不然双击闪退看不到原因
$cmd = "try { & '$StartPs1' } catch { Write-Host `$_ -ForegroundColor Red; Read-Host '出错了，按回车关闭' }"

$lnk = $WS.CreateShortcut($LnkPath)
$lnk.TargetPath       = $Shell
$lnk.Arguments        = "-NoProfile -ExecutionPolicy Bypass -Command `"$cmd`""
$lnk.WorkingDirectory = $ProjDir
if (Test-Path $IconFile) { $lnk.IconLocation = "$IconFile,0" }
$lnk.Description      = '局域网剪贴板 —— 手机与电脑之间传文字和文件'
$lnk.Save()

Write-Host "  桌面图标已创建：$Name" -ForegroundColor Green
Write-Host "`n安装完成。" -ForegroundColor Green
Write-Host "  使用：双击桌面「$Name」，浏览器会自动打开"
Write-Host "  手机：连同一个 WiFi，访问终端里打印的那个局域网地址"
Write-Host "  卸载：pwsh install.ps1 -Uninstall"
