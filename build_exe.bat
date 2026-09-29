@echo off
rem Package Desktop Vault into a single standalone .exe (no Python needed
rem on the target machine). Output lands in dist\.
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller || goto :fail

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "Desktop Vault" ^
    --icon "app.ico" ^
    --collect-all argon2 ^
    --collect-submodules cryptography ^
    "DesktopVault.pyw" || goto :fail

echo.
echo Built: %~dp0dist\Desktop Vault.exe
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
