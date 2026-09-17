# Creates (or refreshes) a Desktop + Start-Menu shortcut for
# dist\DualRouterDashboard.exe with the app icon.
$ErrorActionPreference = "Stop"
$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe     = Join-Path $root "dist\DualRouterDashboard.exe"
$icon    = Join-Path $root "app_icon.ico"

if (-not (Test-Path $exe)) {
    Write-Error "dist\DualRouterDashboard.exe not found - build first."
    exit 1
}

$wsh = New-Object -ComObject WScript.Shell

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = $wsh.CreateShortcut((Join-Path $desktop "Dual-Router Dashboard.lnk"))
$lnk.TargetPath = $exe
$lnk.WorkingDirectory = $root
$lnk.IconLocation = if (Test-Path $icon) { $icon } else { $exe }
$lnk.Description = "Monitor the outdoor WISP router + indoor LAN (192.168.1.1 / 192.168.0.1)"
$lnk.Save()

$startMenu = [Environment]::GetFolderPath("Programs")
$lnk2 = $wsh.CreateShortcut((Join-Path $startMenu "Dual-Router Dashboard.lnk"))
$lnk2.TargetPath = $exe
$lnk2.WorkingDirectory = $root
$lnk2.IconLocation = if (Test-Path $icon) { $icon } else { $exe }
$lnk2.Description = "Dual-Router Dashboard"
$lnk2.Save()

Write-Host "  Desktop shortcut : $desktop\Dual-Router Dashboard.lnk"
Write-Host "  Start-menu entry : $startMenu\Dual-Router Dashboard.lnk"
exit 0
