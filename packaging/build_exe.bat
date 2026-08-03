@echo off
REM Builds the Windows .exe distribution. Run from anywhere; always operates
REM against the repo root regardless of where it's invoked from.
setlocal
cd /d "%~dp0.."

python -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 goto :error

python -m PyInstaller packaging\USGS_GC_Reports.spec --noconfirm
if errorlevel 1 goto :error

echo.
echo Build complete: dist\USGS_GC_Reports\USGS_GC_Reports.exe
goto :eof

:error
echo.
echo Build failed - see errors above.
exit /b 1
