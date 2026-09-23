@echo off
REM Windows double-click launcher.  Creates a private virtual environment next to this
REM file on first run, installs the two dependencies, then starts the alarm clock.
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" ( where python >nul 2>nul && set PY=python )
if "%PY%"=="" (
  echo Python 3 is required. Install it from https://www.python.org/downloads/windows/
  echo and tick "Add python.exe to PATH" and "tcl/tk" during setup, then run this file again.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import pygame.mixer, sounddevice" >nul 2>nul
if errorlevel 1 (
  echo First run: setting up, this takes about a minute...
  if exist .venv rmdir /s /q .venv
  %PY% -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
)
start "" ".venv\Scripts\pythonw.exe" alarm_clock.py
exit /b 0
:fail
echo Setup failed. Check your internet connection and try again.
pause
exit /b 1
