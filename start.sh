#!/bin/sh
# Check Tracker (macOS / Linux): start the local server and open it in the
# default browser. Press Ctrl+C (or close the Terminal window) to stop it.
cd "$(dirname "$0")" || exit 1

if [ -x .venv/bin/python ]; then VPY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then VPY=.venv/Scripts/python.exe  # Git Bash
else
    echo "Project venv not found. Run ./setup.sh first."
    exit 1
fi

# The port comes from config.py / config_local.py.
PORT=$("$VPY" -c "import config; print(config.PORT)") || exit 1
URL="http://127.0.0.1:$PORT/"

open_browser() {
    if command -v open >/dev/null 2>&1; then open "$1"
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1
    else echo "Open $1 in your browser."
    fi
}

# Already running (e.g. started twice)? Just open the browser.
if curl -s -o /dev/null --max-time 2 "${URL}accounts"; then
    open_browser "$URL"
    exit 0
fi

# Open the browser in the background as soon as the server answers (~30 s max).
(
    i=0
    while [ $i -lt 30 ]; do
        if curl -s -o /dev/null --max-time 1 "${URL}accounts"; then
            open_browser "$URL"
            exit 0
        fi
        sleep 1
        i=$((i + 1))
    done
) &

echo "Check Tracker is running at $URL"
echo "Press Ctrl+C to stop it."
echo
exec "$VPY" app.py
