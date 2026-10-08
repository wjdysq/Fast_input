@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set PY=%%i
)

if not defined PY (
  echo.
  echo   [ERROR] Python not found.
  echo   Please install Python 3.9+ and make sure it is on PATH.
  echo.
  pause
  exit /b 1
)

echo.
echo   ==========================================================
echo    Clipboard Auto-Paste  [TYPING MODE]
echo   ==========================================================
echo    Python : %PY%
echo.
echo    Copy with Ctrl+C
echo    Click into your target window
echo    Press Ctrl+Alt+V
echo.
echo    Text is typed character by character, fast -
echo    you will see it being typed out, not pasted at once.
echo.
echo    Press Ctrl+C in this window to quit.
echo   ==========================================================
echo.

"%PY%" main.py --method type --speed 200 %*
echo.
echo   [Program exited]
pause
