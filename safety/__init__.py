"""TTU observatory ASCOM Alpaca SafetyMonitor.

A small daemon that aggregates several safety inputs into a single IsSafe boolean and
serves it as an ASCOM Alpaca SafetyMonitor device (for NINA and other clients). It runs
alongside make_status_page.py on the observatory Raspberry Pi: the page generator writes
the sun altitude and humidity it already computes to a shared JSON file, and this daemon
reads them, adds its own Weather Underground rain polling, and writes back a state file
that make_status_page.py renders on the status page.
"""

__version__ = "0.1.0"
