@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo.
echo === Sticker auto-send chance ===
"%PY%" "%~dp0set_chance.py"
echo.
set /p "VAL=Enter percent (0-100, blank = cancel): "
if "%VAL%"=="" goto :done
"%PY%" "%~dp0set_chance.py" "%VAL%"
:done
echo.
pause
