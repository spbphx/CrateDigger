@echo off
setlocal
cd /d "%~dp0"
title CrateDigger
echo.
echo   CrateDigger - sample digging for boom bap
echo   ------------------------------------------
echo.

if exist ".venv\Scripts\python.exe" goto setup

set "PYEXE="
call :findpy
if defined PYEXE goto mkvenv

echo Python not found. Installing Python 3.12 (one time, about 1 minute)...
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :findpy
if defined PYEXE goto mkvenv

echo.
echo Could not install Python automatically.
echo 1) Download Python 3.12 from https://www.python.org/downloads/
echo 2) In the installer tick "Add python.exe to PATH"
echo 3) Run START_WINDOWS.bat again
start "" https://www.python.org/downloads/
pause
exit /b 1

:mkvenv
echo Creating a private Python environment in this folder...
"%PYEXE%" -m venv .venv
if errorlevel 1 (
  echo Failed to create the environment.
  pause
  exit /b 1
)

:setup
".venv\Scripts\python.exe" setup_env.py
if errorlevel 1 (
  pause
  exit /b 1
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
exit /b 0

:findpy
for %%V in (313 312 311 310 314) do (
  if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python%%V\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python%%V\python.exe"
  if not defined PYEXE if exist "%ProgramFiles%\Python%%V\python.exe" set "PYEXE=%ProgramFiles%\Python%%V\python.exe"
)
if defined PYEXE exit /b 0
where py >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%I in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
)
if defined PYEXE exit /b 0
for /f "delims=" %%I in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%I"
exit /b 0
