param(
    [ValidateSet("admin", "user", "both")]
    [string]$Mode = "admin"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopDir = [Environment]::GetFolderPath("Desktop")

if (-not (Test-Path $DesktopDir)) {
    throw "Desktop directory not found: $DesktopDir"
}

$Shell = New-Object -ComObject WScript.Shell

function New-HudShortcut {
    param(
        [string]$ShortcutName,
        [string]$TargetBat,
        [string]$Description
    )
    $TargetPath = Join-Path $ProjectDir $TargetBat
    if (-not (Test-Path $TargetPath)) {
        throw "Target script missing: $TargetPath"
    }
    $LnkPath = Join-Path $DesktopDir "$ShortcutName.lnk"
    $Shortcut = $Shell.CreateShortcut($LnkPath)
    $Shortcut.TargetPath = $TargetPath
    $Shortcut.WorkingDirectory = $ProjectDir
    $Shortcut.Description = $Description
    $Shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
    $Shortcut.Save()
    Write-Output "Created shortcut: $LnkPath -> $TargetPath"
}

if ($Mode -eq "admin" -or $Mode -eq "both") {
    New-HudShortcut -ShortcutName "KV Cache HUD" -TargetBat "run_hud_admin.bat" -Description "KV Cache & Latency HUD (Admin Kernel EStats)"
}

if ($Mode -eq "user" -or $Mode -eq "both") {
    New-HudShortcut -ShortcutName "KV Cache HUD (User)" -TargetBat "run_hud.bat" -Description "KV Cache & Latency HUD (Standard User)"
}
