@echo off
setlocal
cd /d "%~dp0"

rem 使用 ExecutionPolicy Bypass 只执行本项目这个脚本，不修改电脑的全局策略。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0manage_services.ps1" -Action Restart
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo Service restart failed. Check data\runtime_logs\*.err.log for details.
) else (
    echo All four Web services are ready.
)
pause
exit /b %EXIT_CODE%

