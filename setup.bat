@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 bootstrap.py --setup-only
) else (
  python bootstrap.py --setup-only
)
pause
