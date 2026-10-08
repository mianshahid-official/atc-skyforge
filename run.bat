@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==================================================================
echo   [ATC SKYFORGE] - AVIATION EMERGENCY VIDEO GENERATOR
echo ==================================================================
echo.
echo Launching Standalone Desktop Application...
echo.

if not exist "plane_videos" mkdir "plane_videos"
if not exist "tower_videos" mkdir "tower_videos"
if not exist "rendered_videos" mkdir "rendered_videos"

python desktop_app.py

if %ERRORLEVEL% neq 0 (
    echo.
    echo Desktop application closed.
    pause
)
