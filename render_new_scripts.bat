@echo off
setlocal enabledelayedexpansion
title ATC SkyForge - Upload Scripts & Batch Render
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

echo ====================================================================
echo   ✈️  ATC SKYFORGE — SCRIPT DEPLOYER & BATCH RENDERER
echo ====================================================================
echo.

if "%~1"=="" (
    python render_new_scripts.py
) else (
    python render_new_scripts.py "%~1"
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Process encountered an issue. See details above.
)

echo.
pause
