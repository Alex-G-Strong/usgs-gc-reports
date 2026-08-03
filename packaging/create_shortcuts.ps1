<#
.SYNOPSIS
  Creates a Desktop shortcut and a Start Menu shortcut for USGS GC Reports,
  and best-effort pins it to the taskbar.

.PARAMETER Mode
  "exe"      - shortcut points at dist\USGS_GC_Reports\USGS_GC_Reports.exe
               (build it first with packaging\build_exe.bat)
  "portable" - shortcut points at the portable bundle's launcher .bat
               (build it first with: python packaging\build_portable.py)

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\create_shortcuts.ps1 -Mode exe
  powershell -ExecutionPolicy Bypass -File packaging\create_shortcuts.ps1 -Mode portable
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("exe", "portable")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$IconPath = Join-Path $RepoRoot "assets\icon.ico"
$ShortcutName = "USGS GC Reports"

if ($Mode -eq "exe") {
    $TargetPath = Join-Path $RepoRoot "dist\USGS_GC_Reports\USGS_GC_Reports.exe"
    $WorkingDir = Split-Path -Parent $TargetPath
    $Arguments = ""
    if (-not (Test-Path $TargetPath)) {
        Write-Error "Not found: $TargetPath`nBuild it first: packaging\build_exe.bat"
        exit 1
    }
} else {
    $BatPath = Join-Path $RepoRoot "portable\USGS_GC_Reports_Portable\Launch USGS GC Reports.bat"
    if (-not (Test-Path $BatPath)) {
        Write-Error "Not found: $BatPath`nBuild it first: python packaging\build_portable.py"
        exit 1
    }
    # Shortcut launches the .bat via cmd.exe /c so it can carry its own icon
    # (a .bat file's own icon can't be overridden directly in a .lnk the same
    # way an .exe's can - pointing the shortcut at cmd.exe with the .bat as an
    # argument, plus IconLocation set explicitly, is the reliable way).
    $TargetPath = "$env:WINDIR\System32\cmd.exe"
    $Arguments = "/c `"`"$BatPath`"`""
    $WorkingDir = Split-Path -Parent $BatPath
}

function New-AppShortcut {
    param([string]$LnkPath)
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($LnkPath)
    $shortcut.TargetPath = $TargetPath
    if ($Arguments) { $shortcut.Arguments = $Arguments }
    $shortcut.WorkingDirectory = $WorkingDir
    $shortcut.IconLocation = $IconPath
    $shortcut.Description = "USGS GC Reports"
    $shortcut.Save()
    Write-Host "Created: $LnkPath"
}

# -- Desktop shortcut ----------------------------------------------------------
$DesktopLnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "$ShortcutName.lnk"
New-AppShortcut -LnkPath $DesktopLnk

# -- Start Menu shortcut ---------------------------------------------------------
# Appears in the Start Menu / searchable via Windows search either way; the user
# can right-click it there and choose "Pin to taskbar" themselves if the
# programmatic attempt below doesn't take (Microsoft has progressively locked
# down automated taskbar pinning across Windows versions - this is the one path
# that reliably works on every version).
$StartMenuDir = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs"
$StartMenuLnk = Join-Path $StartMenuDir "$ShortcutName.lnk"
New-AppShortcut -LnkPath $StartMenuLnk

# -- best-effort taskbar pin -----------------------------------------------------
try {
    $shellApp = New-Object -ComObject Shell.Application
    $folder = $shellApp.Namespace((Split-Path -Parent $StartMenuLnk))
    $item = $folder.ParseName((Split-Path -Leaf $StartMenuLnk))
    $pinVerb = $item.Verbs() | Where-Object { $_.Name -replace '&', '' -eq 'Pin to taskbar' }
    if ($pinVerb) {
        $pinVerb.DoIt()
        Write-Host "Pinned to taskbar."
    } else {
        Write-Host "Could not auto-pin to taskbar on this Windows version - right-click the Start Menu shortcut and choose 'Pin to taskbar' manually."
    }
} catch {
    Write-Host "Could not auto-pin to taskbar - right-click the Start Menu shortcut and choose 'Pin to taskbar' manually."
}

Write-Host "`nDone. Desktop and Start Menu shortcuts created for: $TargetPath"
