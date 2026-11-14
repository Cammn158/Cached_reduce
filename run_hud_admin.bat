@echo off
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/k cd /d ""%~dp0"" && python -m kv_cache_hud %*' -Verb RunAs"
    exit /b 0
)
python -m kv_cache_hud %*
