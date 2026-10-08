@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set PY=%%i
)

if not defined PY (
  echo.
  echo   [ERROR] Python not found.
  echo.
  pause
  exit /b 1
)

echo.
echo   Running self-test...
echo.
"%PY%" selftest.py
echo.
pause
