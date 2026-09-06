#!/bin/bash
# Run Grabbit on Linux.  In a terminal:  ./start-linux.sh
# (Some file managers can also run it on double-click, choosing "Run".)
cd "$(dirname "$0")" || exit 1
echo
echo "  Setting up Grabbit..."
echo

# --- find a working Python 3; install it only if missing -------------------
if python3 -c "import sys" >/dev/null 2>&1; then
  PY=python3
else
  echo "  Python 3 is not installed. Trying to install it for you"
  echo "  (you may be asked for your password)..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm python python-pip
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper install -y python3 python3-pip
  fi
  if python3 -c "import sys" >/dev/null 2>&1; then
    PY=python3
  else
    echo "  [X] Could not install Python automatically."
    echo "      Install python3 (and python3-venv) with your package manager,"
    echo "      then run this launcher again."
    read -r -p "  Press Enter to close."
    exit 1
  fi
fi

# --- create a self-contained environment on first run ----------------------
if [ ! -x ".venv/bin/python" ]; then
  echo "  Creating local environment (first run only)..."
  if ! "$PY" -m venv .venv; then
    echo "  [X] Could not create the environment."
    echo "      On Debian/Ubuntu you may need:  sudo apt install python3-venv"
    read -r -p "  Press Enter to close."
    exit 1
  fi
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
  read -r -p "  Press Enter to close."
  exit 1
fi

echo
echo "  Starting Grabbit. ffmpeg will be set up inside ./bin on first run."
echo "  A browser tab will open at http://127.0.0.1:8080"
echo "  Press Ctrl+C to stop."
echo
"$VENV_PY" app.py
