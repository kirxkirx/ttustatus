#!/bin/bash
# Persistent launcher for the TTU Alpaca SafetyMonitor daemon.
# Use this OR the systemd service (README_SAFETY.md), not both. Keeps the daemon running
# and restarts it if it exits. Location-independent (runs from this script's own dir), so
# it works wherever the repo is cloned (e.g. ~/ttustatus).
# Requires: sudo apt install python3-flask python3-waitress
cd "$(dirname "$0")" || exit 1

# Load secrets (the WU API key, etc.) from a file OUTSIDE the repo — never commit the key.
if [ -f "$HOME/ttustatus.env" ]; then
    set -a; . "$HOME/ttustatus.env"; set +a
fi

while true
do
    /usr/bin/python3 ./safety_monitor.py
    echo "$(date '+%Y-%m-%d %H:%M:%S') safety_monitor exited ($?); restarting in 5s"
    sleep 5
done
