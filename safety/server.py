"""TTU Alpaca SafetyMonitor daemon — wires the pieces together and serves the API.

Threads:
  * evaluator    — every EVAL_INTERVAL: recompute IsSafe, log transitions, write state.
  * rain-poller  — every ~30 s: if it's night (sun below the gate) and the interval has
                   elapsed, poll Weather Underground and update the 3-hour latch.
  * discovery    — UDP responder so NINA can auto-find us.
  * main thread  — waitress serving the Alpaca HTTP API.
"""
from __future__ import annotations

import logging
import threading
import time

from . import config, discovery
from .alpaca import create_app
from .eventlog import EventLog
from .monitor import RainPoller, SafetyMonitor

log = logging.getLogger("ttu.safety")


def _fresh_sun(monitor, cfg):
    """Return the current sun altitude if inputs are fresh, else None (skip polling)."""
    inp = monitor.read_inputs()
    if not inp or inp["age"] is None or inp["age"] > cfg.INPUTS_STALE_SEC:
        return None
    return inp["sun_alt"]


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config

    eventlog = EventLog(cfg.EVENT_LOG)
    poller = RainPoller(cfg, eventlog)
    monitor = SafetyMonitor(cfg, eventlog, poller)

    eventlog.record("STARTUP", detail=f"{cfg.SERVER_NAME} v{cfg.DRIVER_VERSION}",
                    result=f"http {cfg.HTTP_HOST}:{cfg.HTTP_PORT}")
    if not cfg.WU_API_KEY:
        log.warning("TTU_SAFETY_WU_KEY is not set — RAIN POLLING DISABLED "
                    "(sun/humidity protection still active). Set it via the environment.")
        eventlog.record("CONFIG", reason="WU_API_KEY not set",
                        result="rain polling disabled")

    stop = threading.Event()

    def evaluator():
        while not stop.is_set():
            try:
                monitor.evaluate()
            except Exception:
                log.exception("evaluate failed")
            stop.wait(cfg.EVAL_INTERVAL)

    def rain_loop():
        while not stop.is_set():
            try:
                poller.maybe_poll(_fresh_sun(monitor, cfg), time.time())
            except Exception:
                log.exception("rain poll failed")
            stop.wait(30)

    threading.Thread(target=evaluator, name="evaluator", daemon=True).start()
    threading.Thread(target=rain_loop, name="rain-poller", daemon=True).start()
    discovery.start(cfg.HTTP_PORT)

    app = create_app(monitor, cfg)
    log.info("Serving Alpaca SafetyMonitor on http://%s:%d (device %d)",
             cfg.HTTP_HOST, cfg.HTTP_PORT, cfg.DEVICE_NUMBER)
    try:
        from waitress import serve
        serve(app, host=cfg.HTTP_HOST, port=cfg.HTTP_PORT, threads=8)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        eventlog.record("SHUTDOWN")


if __name__ == "__main__":
    main()
