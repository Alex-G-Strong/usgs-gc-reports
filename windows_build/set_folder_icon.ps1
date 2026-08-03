<#
.SYNOPSIS
  Gives a folder a custom icon in Windows Explorer (via desktop.ini) - a visual
  signal that "this folder is one unit, don't move files out of it individually."

.PARAMETER FolderPath
  The folder to apply the icon to (e.g. the exe\ or portable\ distribution folder).

.PARAMETER IconPath
  Path to a .ico file. Defaults to assets\icon.ico in this repo. A copy of it is
  placed inside FolderPath itself (desktop.ini works most reliably with a
  same-folder relative path, and it keeps the folder self-contained if it's
  later copied somewhere else).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File windows_build\set_folder_icon.ps1 -FolderPath "dist\USGS_GC_Reports"
  powershell -ExecutionPolicy Bypass -File windows_build\set_folder_icon.ps1 -FolderPath "D:\USGS_GC_Reports_App\exe"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$FolderPath,

    [string]$IconPath
)

$ErrorActionPreference = "Stop"

if (-not $IconPath) {
    $ScriptDir = Split-Path -Parent $PSCommandPath
    $IconPath = Join-Path (Split-Path -Parent $ScriptDir) "assets\icon.ico"
}

if (-not (Test-Path $FolderPath)) {
    Write-Error "Folder not found: $FolderPath"
    exit 1
}
if (-not (Test-Path $IconPath)) {
    Write-Error "Icon not found: $IconPath"
    exit 1
}
$FolderPath = (Resolve-Path $FolderPath).Path

# Copy the icon into the folder itself (as a hidden file) so the folder stays
# self-contained - the icon keeps working even if this folder is later copied
# to a different machine or drive letter.
$LocalIcon = Join-Path $FolderPath ".folder_icon.ico"
Copy-Item $IconPath $LocalIcon -Force
(Get-Item $LocalIcon).Attributes = "Hidden"

$desktopIniPath = Join-Path $FolderPath "desktop.ini"
# Clear any existing desktop.ini's read-only/hidden/system flags first - Windows
# marks it that way once applied, and a second run would otherwise fail to
# overwrite it.
if (Test-Path $desktopIniPath) {
    (Get-Item $desktopIniPath).Attributes = "Normal"
}
@"
[.ShellClassInfo]
IconResource=.folder_icon.ico,0
[ViewState]
Mode=
Vid=
FolderType=Generic
"@ | Set-Content -Path $desktopIniPath -Encoding ASCII

(Get-Item $desktopIniPath).Attributes = "Hidden, System"
# The System attribute on the folder itself is what actually makes Explorer
# honor desktop.ini's custom icon - without it, the icon is silently ignored.
(Get-Item $FolderPath).Attributes = (Get-Item $FolderPath).Attributes -bor [System.IO.FileAttributes]::System

Write-Host "Custom icon applied to: $FolderPath"
Write-Host "(You may need to press F5 in Explorer, or reopen the folder's parent, to see it refresh.)"
