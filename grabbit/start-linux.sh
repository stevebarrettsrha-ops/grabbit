#!/bin/bash
# Run Grabbit on Linux.  In a terminal:  ./start-linux.sh
# From the file manager: right-click it -> "Run as a Program".
cd "$(dirname "$0")" || exit 1

# --- started from the file manager? open a terminal to run in --------------
# "Run as a Program" gives no terminal: nothing below could be seen, and a
# failure would just look like nothing happening. On a desktop, re-run this
# script inside a terminal window instead.
# Anything run with GRABBIT_NO_TERMINAL=1 (a service, an IDE task) stays
# where it was started. Not INVOCATION_ID: a file manager that runs as
# a systemd service hands it to everything it starts.
if [ -z "${GRABBIT_IN_TERMINAL:-}" ] && [ ! -t 0 ] && [ ! -t 1 ] \
   && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] \
   && [ -z "${GRABBIT_NO_TERMINAL:-}" ]; then
  export GRABBIT_IN_TERMINAL=1
  SELF="$(pwd)/$(basename "$0")"
  for term in gnome-terminal ptyxis kgx konsole xfce4-terminal mate-terminal \
              tilix x-terminal-emulator xterm; do
    command -v "$term" >/dev/null 2>&1 || continue
    case "$term" in
      gnome-terminal|ptyxis|kgx) "$term" -- bash "$SELF" && exit 0 ;;
      xfce4-terminal|mate-terminal) "$term" -x bash "$SELF" && exit 0 ;;
      *) "$term" -e bash "$SELF" && exit 0 ;;
    esac
  done
  # No terminal found: carry on without one. The browser tab still opens.
fi

# In a window we opened, keep it open if Grabbit stops with an error, so the
# reason can be read. (The setup steps below already pause on their own.)
press_enter() { PAUSED=1; read -r -p "  Press Enter to close."; }
pause_on_error() {
  code=$?
  if [ -n "${GRABBIT_IN_TERMINAL:-}" ] && [ -z "${PAUSED:-}" ] && [ -t 0 ] \
     && [ "$code" -ne 0 ] && [ "$code" -ne 130 ]; then
    echo
    read -r -p "  Grabbit stopped (exit code $code). Press Enter to close." || true
  fi
}
trap pause_on_error EXIT
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
    press_enter
    exit 1
  fi
fi

# --- create a self-contained environment on first run ----------------------
# Check for pip, not just a Python: on Debian/Ubuntu without python3-venv,
# venv dies after writing .venv/bin/python but before pip, and keeping that
# half-built folder made every later run fail with "No module named pip".
if [ -e ".venv" ] && ! ".venv/bin/python" -c "import pip" >/dev/null 2>&1; then
  echo "  The local environment is incomplete. Building it again..."
  rm -rf .venv
fi
if [ ! -x ".venv/bin/python" ]; then
  echo "  Creating local environment (first run only)..."
  if ! "$PY" -m venv .venv; then
    rm -rf .venv
    echo "  [X] Could not create the environment."
    echo "      On Debian/Ubuntu install the venv module first:"
    echo "        sudo apt-get install -y python3-venv"
    echo "      then run this launcher again."
    press_enter
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
  press_enter
  exit 1
fi

echo
echo "  Starting Grabbit. ffmpeg will be set up inside ./bin on first run."
echo "  A browser tab will open at http://127.0.0.1:8080"
echo "  Press Ctrl+C to stop."
echo
# The server takes this script's place (exec), so whatever stops the
# script stops the server. Only in a window this script opened does
# the shell stay, so the trap above can keep a failure on screen.
if [ -n "${GRABBIT_IN_TERMINAL:-}" ]; then
  "$VENV_PY" app.py
else
  exec "$VENV_PY" app.py
fi
