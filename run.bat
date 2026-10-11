@echo off
rem ============================================================================
rem The Memokeeper - one-click runner
rem
rem 1. Catch up any message history that arrived while the bot was offline.
rem 2. Start the live bot (captures messages as you send them).
rem
rem Double-click this file, or run it from a terminal.
rem ============================================================================

setlocal
cd /d "%~dp0"

set PY=C:\Users\Balaji\AppData\Local\Python\bin\python.exe

if not exist "%PY%" (
    echo [run.bat] Python not found at %PY% - edit PY at the top of this file.
    pause
    exit /b 1
)

if not exist .env (
    echo [run.bat] .env not found. Copy .env.example to .env and fill it in first.
    pause
    exit /b 1
)

echo [run.bat] Step 1/2: catching up message history...
"%PY%" backfill.py
if errorlevel 1 (
    echo [run.bat] Backfill failed - see messages above.
    pause
    exit /b 1
)

echo.
echo [run.bat] Step 2/2: starting live bot (Ctrl+C to stop)...
"%PY%" bot.py

pause
