#!/bin/bash
# Double-click this file to run Grabbit on macOS.
cd "$(dirname "$0")" || exit 1
echo
echo "  Setting up Grabbit..."
echo

# --- find a working Python 3; install it only if missing -------------------
# Test by running it: macOS has a /usr/bin/python3 placeholder that only
# prompts to install developer tools, so a plain presence check isn't enough.
if python3 -c "import sys" >/dev/null 2>&1; then
  PY=python3
else
  echo "  Python 3 is not installed. Trying to install it for you..."
  if command -v brew >/dev/null 2>&1; then
    brew install python
  fi
  if python3 -c "import sys" >/dev/null 2>&1; then
    PY=python3
  else
    echo "  Opening the Python download page in your browser..."
    open "https://www.python.org/downloads/" 2>/dev/null
    echo "  Please install Python 3.10+, then run this launcher again."
    echo "  (Tip: installing Homebrew from https://brew.sh lets this set it up"
    echo "   automatically next time, via 'brew install python'.)"
    read -r -p "  Press Return to close."
    exit 1
  fi
fi

# --- create a self-contained environment on first run ----------------------
if [ ! -x ".venv/bin/python" ]; then
  echo "  Creating local environment (first run only)..."
  "$PY" -m venv .venv || {
    echo "  [X] Could not create the environment."
    read -r -p "  Press Return to close."
    exit 1
  }
fi
VENV_PY=".venv/bin/python"

# --- install / verify dependencies -----------------------------------------
echo "  Checking dependencies (first run may take a minute)..."
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1

if ! "$VENV_PY" -c "import flask" >/dev/null 2>&1; then
  echo "  Installing Flask..."
  "$VENV_PY" -m pip install flask
fi

if ! "$VENV_PY" -c "import yt_dlp" >/dev/null 2>&1; then
  echo "  Installing yt-dlp (this can take a minute)..."
  "$VENV_PY" -m pip install "yt-dlp[default]" || "$VENV_PY" -m pip install yt-dlp
else
  echo "  Updating yt-dlp..."
  "$VENV_PY" -m pip install --quiet --upgrade "yt-dlp[default]" >/dev/null 2>&1
fi

# make sure yt-dlp really installed
if ! "$VENV_PY" -c "import yt_dlp" >/dev/null 2>&1; then
  echo "  [X] yt-dlp could not be installed. Check your internet connection,"
  echo "      then run this launcher again."
  read -r -p "  Press Return to close."
  exit 1
fi

echo
echo "  Starting Grabbit. ffmpeg will be set up inside ./bin on first run."
echo "  A browser tab will open at http://127.0.0.1:8080"
echo "  Leave this window open while you use it; press Ctrl+C (or close it) to stop."
echo
"$VENV_PY" app.py
