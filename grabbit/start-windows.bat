@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Grabbit
echo.
echo   Setting up Grabbit...
echo.

rem --- find a REAL Python --------------------------------------------------
rem We TEST by running it, because Windows ships fake "python"/"python3" stubs
rem (Microsoft Store App-execution aliases) that pass a 'where' check but only
rem print "Python was not found" when run. Those must not count as installed.
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo   No real Python found ^(the Microsoft Store placeholder does not count^).
  echo   Trying to install Python for you...
  echo.
  where winget >nul 2>&1
  if not errorlevel 1 (
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
      echo.
      echo   Automatic install did not finish. Opening the download page...
      start "" "https://www.python.org/downloads/"
      echo   Install Python 3.10+ ^(tick "Add python.exe to PATH"^), then run this again.
      echo.
      pause
      exit /b 1
    )
    echo.
    echo   Python was installed. Please CLOSE this window and run start-windows
    echo   again so it can use the new installation.
    echo.
    pause
    exit /b 0
  ) else (
    echo   Cannot auto-install ^(winget is not available on this PC^).
    echo   Opening the Python download page in your browser...
    start "" "https://www.python.org/downloads/"
    echo   Install Python 3.10+ ^(tick "Add python.exe to PATH"^), then run this again.
    echo.
    pause
    exit /b 1
  )
)

rem --- create a self-contained environment on first run -----------------
if not exist ".venv\Scripts\python.exe" (
  echo   Creating local environment ^(first run only^)...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo   [X] Could not create the environment. Is Python installed correctly?
    pause
    exit /b 1
  )
)
set "VENV_PY=.venv\Scripts\python.exe"

rem --- upgrade pip (quiet, non-fatal) -----------------------------------
"%VENV_PY%" -m pip install --quiet --upgrade pip 1>nul 2>nul

rem --- Flask ------------------------------------------------------------
"%VENV_PY%" -c "import flask" 1>nul 2>nul
if errorlevel 1 (
  echo   Installing Flask...
  "%VENV_PY%" -m pip install flask
)

rem --- yt-dlp (the download engine) -------------------------------------
"%VENV_PY%" -c "import yt_dlp" 1>nul 2>nul
if errorlevel 1 (
  echo   Installing yt-dlp ^(this can take a minute^)...
  "%VENV_PY%" -m pip install "yt-dlp[default]"
  if errorlevel 1 "%VENV_PY%" -m pip install yt-dlp
) else (
  echo   Updating yt-dlp...
  "%VENV_PY%" -m pip install --quiet --upgrade "yt-dlp[default]" 1>nul 2>nul
)

rem --- make sure yt-dlp is really installed -----------------------------
"%VENV_PY%" -c "import yt_dlp" 1>nul 2>nul
if errorlevel 1 (
  echo.
  echo   [X] yt-dlp could not be installed. Check your internet connection,
  echo       then run start-windows again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting Grabbit. ffmpeg will be set up inside .\bin on first run.
echo   A browser tab will open at http://127.0.0.1:8080
echo   Leave this window open while you use it; close it to stop.
echo.
"%VENV_PY%" app.py
pause
