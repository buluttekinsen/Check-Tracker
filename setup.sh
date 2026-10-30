#!/bin/sh
# Check Tracker setup (macOS / Linux): creates the .venv, installs the
# requirements and creates an empty database. Needs Python 3.12 or newer.
# Safe to run again, e.g. after a git pull changed requirements.txt
# (stop the app first).
set -e
cd "$(dirname "$0")"

CHECK='import sys; sys.exit(sys.version_info[:2] < (3, 12))'
venv_python() {
    if [ -x .venv/bin/python ]; then echo .venv/bin/python
    elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe  # Git Bash
    fi
}

VPY=$(venv_python)
if [ -n "$VPY" ]; then
    if ! "$VPY" -c "$CHECK" 2>/dev/null; then
        echo "The existing .venv is broken or uses a Python older than 3.12."
        echo "Delete it (rm -rf .venv) and run ./setup.sh again."
        exit 1
    fi
else
    PY=""
    for c in python3.14 python3.13 python3.12 python3 python; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "$CHECK" 2>/dev/null; then
            PY=$c
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "Python 3.12 or newer was not found (the python3 that ships with macOS is older)."
        echo "Install it from https://www.python.org/downloads/ (or: brew install python@3.13)"
        echo "and run ./setup.sh again."
        exit 1
    fi
    echo "Using $("$PY" --version) ($(command -v "$PY"))"
    echo "Creating .venv ..."
    "$PY" -m venv .venv
    VPY=$(venv_python)
fi

echo "Installing requirements ..."
"$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt
"$VPY" db.py init
echo
echo "Setup complete. Start the app with ./start.sh"
