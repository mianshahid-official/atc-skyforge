@echo off
title ATC SkyForge - Setup & Installation
chcp 65001 >nul
cd /d "%~dp0"

echo =====================================================================
echo    ✈️  ATC SKYFORGE — Aviation Emergency Video Generator Setup
echo =====================================================================
echo.
echo [1/3] Checking Python installation...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Python is not installed or not added to your system PATH!
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

python --version
echo.
echo [2/3] Checking FFmpeg installation...
ffmpeg -version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [NOTE] FFmpeg was not detected in PATH.
    echo Make sure FFmpeg is installed and accessible in PATH for video rendering.
) else (
    echo [OK] FFmpeg is installed and ready.
)

echo.
echo [3/3] Installing Python dependencies from requirements.txt...
echo Please wait, this may take a couple of minutes...
echo.

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Some packages failed to install. Please check your internet connection.
    echo.
    pause
    exit /b 1
)

echo.
echo =====================================================================
echo    🎉 Installation Complete! All dependencies are installed.
echo    You can now run ATC SkyForge by double-clicking 'run.bat'
echo    or web UI mode via 'start_ui.bat'.
echo =====================================================================
echo.
pause
