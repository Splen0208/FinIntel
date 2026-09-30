@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Create .venv and install requirements-platforms.txt first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\open_boss_browser.py
pause
