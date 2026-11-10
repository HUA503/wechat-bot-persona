@echo off
setlocal
cd /d "%~dp0"
rem All paths use %~dp0 so the current directory never matters
set "ROOT=%~dp0"
set "REQS=%~dp0requirements.txt"
set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"
rem Try py launcher 3.11, then explicit D:\Python311, then PATH python
set "PY311="
where py >nul 2>&1
if %errorlevel%==0 (
  py -3.11 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PY311=py -3.11"
)
if not defined PY311 if exist "D:\Python311\python.exe" set "PY311=D:\Python311\python.exe"
if not defined PY311 set "PY311=python"
if not exist "%REQS%" (
  echo [ERROR] requirements.txt not found: %REQS%
  echo Extract ALL files from the zip into ONE folder, then run this script again.
  pause
  exit /b 1
)
echo Using: %PY311%
echo Project: %ROOT%
%PY311% -m venv "%VENV%"
if not exist "%VPY%" (
  echo Python 3.11 is required. Install Python 3.11 and retry.
  pause
  exit /b 1
)
"%VPY%" -c "import sys;print('venv Python',sys.version.split()[0])"
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r "%REQS%"
if errorlevel 1 (echo Dependency installation failed.& pause& exit /b 1)
echo Ready. Next: run the key setup script, then start the bot.
pause
