@echo off
setlocal
cd /d "%~dp0"
title Zhixu Study Workbench

echo ============================================================
echo   Zhixu Study Workbench
echo   URL: http://127.0.0.1:8788
echo   Keep this window open. Closing it will stop the server.
echo ============================================================
echo.

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" -m uvicorn app:app --host 127.0.0.1 --port 8788
set "SERVER_EXIT=%ERRORLEVEL%"

echo.
if not "%SERVER_EXIT%"=="0" (
  echo Startup failed with exit code %SERVER_EXIT%.
  echo Check that the Python environment and dependencies are installed.
) else (
  echo Server stopped.
)
echo Press any key to close this window.
pause >nul
endlocal
