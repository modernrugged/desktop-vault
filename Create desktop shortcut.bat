@echo off
rem Put a Desktop Vault shortcut on the desktop.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Desktop Vault.lnk');" ^
  "$s.TargetPath='%~dp0Desktop Vault.bat';" ^
  "$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%~dp0app.ico';" ^
  "$s.Description='Encrypted local storage for documents, files and folders';" ^
  "$s.Save()"
echo Shortcut created on your desktop.
pause
