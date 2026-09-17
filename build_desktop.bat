@echo off
rem ────────────────────────────────────────────────────────────────────
rem  Dual-Router Dashboard — one-click build + install (Windows)
rem  Produces dist\DualRouterDashboard.exe and installs a Desktop
rem  shortcut.  Requires: Python 3.9+ on PATH.  PyInstaller is
rem  auto-installed if missing.
rem ────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"

echo [1/4] Generating icon (app_icon.ico / app_icon.png)...
python make_icon.py || goto :fail

echo [2/4] Checking PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo        installing pyinstaller...
    python -m pip install --quiet pyinstaller || goto :fail
)

echo [3/4] Building single-file executable...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name DualRouterDashboard ^
    --icon app_icon.ico ^
    --distpath dist --workpath build --specpath . ^
    dual_router_gui.py || goto :fail

echo [4/4] Installing Desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File install_shortcut.ps1 || goto :fail

echo.
echo ============================================================
echo  DONE.  Installed to:  %USERPROFILE%\Desktop\Dual-Router Dashboard.lnk
echo  Executable:            %~dp0dist\DualRouterDashboard.exe
echo  First run performs an automatic network scan (about 1 min).
echo ============================================================
pause
exit /b 0

:fail
echo.
echo BUILD FAILED — see messages above.
pause
exit /b 1
