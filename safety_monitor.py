#!/usr/bin/env python3
"""Entry point for the TTU Alpaca SafetyMonitor daemon.

On the Pi:  python3 /home/kirx/safety_monitor.py   (the `safety/` package sits beside it)
For dev:    python3 safety_monitor.py               (from the ttustatus directory)
"""
from safety.server import main

if __name__ == "__main__":
    main()
