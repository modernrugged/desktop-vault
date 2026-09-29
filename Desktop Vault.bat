@echo off
rem Launch Desktop Vault with no console window.
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "DesktopVault.pyw" %*
    exit /b 0
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe" "DesktopVault.pyw" %*
    exit /b 0
)

echo Could not find pythonw.exe on this system.
echo Install Python 3.9 or newer from https://python.org and tick
echo "Add Python to PATH" during setup.
pause
