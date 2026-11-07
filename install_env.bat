@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %errorlevel%==0 (py -3.11 -m venv .venv) else (python -m venv .venv)
if not exist .venv\Scripts\python.exe (
  echo Python 3.11 is required. Install Python 3.11 and retry.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (echo Dependency installation failed.& pause& exit /b 1)
echo Ready. Double-click 启动后台.vbs to start the bot.
pause
