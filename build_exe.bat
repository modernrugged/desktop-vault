@echo off
rem Package Desktop Vault into a single standalone .exe (no Python needed on
rem the target machine). Output lands in dist\Desktop Vault.exe
rem
rem --collect-all tkinterdnd2 matters: that package ships the tkdnd Tcl
rem library as data files, and without them drag-and-drop silently stops
rem working in the packaged build even though the import succeeds.
setlocal
cd /d "%~dp0"

python -m pip install --upgrade pyinstaller || goto :fail

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "Desktop Vault" ^
    --icon "app.ico" ^
    --version-file "version_info.txt" ^
    --collect-all tkinterdnd2 ^
    --collect-all argon2 ^
    --collect-all cryptography ^
    --exclude-module numpy ^
    --exclude-module PIL ^
    --exclude-module pytest ^
    --exclude-module setuptools ^
    "DesktopVault.pyw" || goto :fail

echo.
echo Built: %~dp0dist\Desktop Vault.exe
certutil -hashfile "dist\Desktop Vault.exe" SHA256
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
