@echo off
REM ===========================================================================
REM Start AEGIS on Windows.
REM
REM Double-click this file, or run it from a terminal:  run.bat
REM Optional port:  run.bat -Port 8502
REM
REM Fully offline - the only thing it contacts is the Ollama daemon on this
REM machine. Run install.bat once first. Press Ctrl+C in the window to stop.
REM ===========================================================================
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set "AEGIS_EXIT=%ERRORLEVEL%"

REM run.ps1 holds the window while the app serves; if it exits with an error
REM (e.g. setup not run yet, port in use), keep the window open so the reason
REM stays visible for anyone who launched this by double-clicking.
if not "%AEGIS_EXIT%"=="0" (
    echo.
    echo AEGIS stopped with exit code %AEGIS_EXIT%. See the message above.
    echo.
    pause
)
exit /b %AEGIS_EXIT%
