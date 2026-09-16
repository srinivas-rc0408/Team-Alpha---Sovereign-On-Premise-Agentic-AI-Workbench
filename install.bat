@echo off
REM ===========================================================================
REM AEGIS one-time setup for Windows.
REM
REM Double-click this file, or run it from a terminal:  install.bat
REM It just wraps install.ps1 so you never have to type the PowerShell
REM execution-policy flags by hand. This is the ONLY step that needs internet.
REM ===========================================================================
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "AEGIS_EXIT=%ERRORLEVEL%"

echo.
if not "%AEGIS_EXIT%"=="0" (
    echo Setup did not finish cleanly ^(exit code %AEGIS_EXIT%^). Read the message above,
    echo fix the one thing it names, then run install.bat again - finished steps are skipped.
) else (
    echo Setup complete. Start AEGIS by double-clicking run.bat
)
echo.
pause
exit /b %AEGIS_EXIT%
