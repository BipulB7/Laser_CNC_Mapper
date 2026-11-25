@echo off
rem Bow Mapper - starts the app and opens it in the browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Python environment. Needs internet once, about 1 minute.
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" ( echo Could not create the environment. Install Python 3.12 64-bit from python.org. & pause & exit /b 1 )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" -m bowmapper %*
if errorlevel 1 pause
