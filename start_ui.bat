@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==================================================================
echo   [ATC SKYFORGE] - Web UI & Video Generator Cockpit
echo ==================================================================
echo.
echo Starting local server on http://localhost:5000 ...
echo Opening your web browser in 3 seconds...
echo.

if not exist "plane_videos" mkdir "plane_videos"
if not exist "tower_videos" mkdir "tower_videos"
if not exist "rendered_videos" mkdir "rendered_videos"

start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5000"

python server.py

pause
