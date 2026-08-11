"""TTU Alpaca SafetyMonitor daemon — wires the pieces together and serves the API.

Threads:
  * evaluator    — every EVAL_INTERVAL: recompute IsSafe, log transitions, write state.
  * rain-poller  — every ~30 s: if it's night (sun below the gate) and the interval has
                   elapsed, poll Weather Underground and update the 3-hour latch.
  * nws-poller   — every ~60 s: if the interval elapsed, pull the NWS gridpoint forecast.
  * discovery    — UDP responder so NINA can auto-find us.
  * main thread  — waitress serving the Alpaca HTTP API.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from . import config, discovery, glm_lightning, radar as radar_mod
from .alpaca import create_app
from .connectivity import ConnectivityWatch
from .eventlog import EventLog
from .monitor import RainPoller, SafetyMonitor
from .nws_forecast import NwsForecastPoller
from .glm_lightning import GlmLightningPoller
from .radar import RadarPoller

log = logging.getLogger("ttu.safety")


def _fresh_sun(monitor, cfg):
    """Return the current sun altitude if inputs are fresh, else None (skip polling)."""
    inp = monitor.read_inputs()
    if not inp or inp["age"] is None or inp["age"] > cfg.INPUTS_STALE_SEC:
        return None
    return inp["sun_alt"]


def run_page_once(cfg):
    """Run make_status_page.py as an isolated subprocess; kill the whole process tree
    if it hangs. Returns (rc, seconds); rc None = could not start / killed."""
    start = time.time()
    try:
        proc = subprocess.Popen(
            [sys.executable, cfg.PAGE_SCRIPT],
            cwd=os.path.dirname(cfg.PAGE_SCRIPT) or ".",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)          # own process group -> killable as a tree
    except Exception as e:
        log.warning("cannot start status page script %s: %s", cfg.PAGE_SCRIPT, e)
        return None, 0.0
    try:
        out, _ = proc.communicate(timeout=cfg.PAGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.warning("status page run exceeded %ds — killing its process tree",
                    cfg.PAGE_TIMEOUT)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        proc.wait()
        return None, time.time() - start
    if proc.returncode != 0:
        tail = (out or b"")[-2000:].decode("utf-8", "replace")
        log.warning("status page run failed (rc=%s):\n%s", proc.returncode, tail)
    return proc.returncode, time.time() - start


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config

    eventlog = EventLog(cfg.EVENT_LOG)
    poller = RainPoller(cfg, eventlog)
    nws = NwsForecastPoller(cfg) if cfg.NWS_ENABLED else None
    glm = GlmLightningPoller(cfg, eventlog) if cfg.GLM_ENABLED else None
    radar = RadarPoller(cfg, eventlog) if cfg.RADAR_ENABLED else None
    conn = ConnectivityWatch(cfg, start_ts=time.time()) if cfg.CONN_ENABLED else None
    monitor = SafetyMonitor(cfg, eventlog, poller, nws=nws, glm=glm, radar=radar, conn=conn)

    for w in cfg.CONFIG_WARNINGS:
        log.warning("CONFIG: %s", w)
        eventlog.record("CONFIG", reason=w, result="using default")
    eventlog.record("STARTUP", detail=f"{cfg.SERVER_NAME} v{cfg.DRIVER_VERSION}",
                    result=f"http {cfg.HTTP_HOST}:{cfg.HTTP_PORT}",
                    site=f"{cfg.GEOCODE[0]},{cfg.GEOCODE[1]} ({cfg.GEOCODE_SOURCE})")
    if not cfg.GEOCODE_FROM_ENV:
        log.warning("site coordinates not set via TTU_SAFETY_LAT/LON — will adopt the "
                    "GPS fix from the status page (current default: %s)", cfg.GEOCODE)
    if not cfg.WU_API_KEY:
        log.warning("TTU_SAFETY_WU_KEY is not set — RAIN POLLING DISABLED "
                    "(sun/humidity protection still active). Set it via the environment.")
        eventlog.record("CONFIG", reason="WU_API_KEY not set",
                        result="rain polling disabled")
    if glm is not None and not glm_lightning.deps_available():
        log.warning("numpy/netCDF4 not installed — GLM LIGHTNING DISABLED "
                    "(apt install python3-numpy python3-netcdf4). Other layers unaffected.")
        eventlog.record("CONFIG", reason="numpy/netCDF4 missing",
                        result="GLM lightning disabled")
    if radar is not None and not radar_mod.deps_available():
        log.warning("Pillow not installed — MRMS RADAR DISABLED "
                    "(apt install python3-pil). Other layers unaffected.")
        eventlog.record("CONFIG", reason="Pillow missing", result="radar disabled")

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

    def nws_loop():
        while not stop.is_set():
            try:
                nws.maybe_poll(time.time())    # polls immediately, then every NWS_POLL_INTERVAL
            except Exception:
                log.exception("nws poll failed")
            stop.wait(60)

    def glm_loop():
        # Its own thread: the slow S3/netCDF poll never blocks the evaluator or HTTP.
        while not stop.is_set():
            try:
                glm.maybe_poll(_fresh_sun(monitor, cfg), time.time())
            except Exception:
                log.exception("glm poll failed")
            stop.wait(60)

    def radar_loop():
        # Own thread: the slow tile/radar fetch + thumbnail render never blocks refresh.
        while not stop.is_set():
            try:
                radar.maybe_poll(_fresh_sun(monitor, cfg), time.time())
            except Exception:
                log.exception("radar poll failed")
            stop.wait(60)

    def conn_loop():
        # Probe internet reachability day and night so the "offline > 1 h" clock is accurate.
        while not stop.is_set():
            try:
                conn.probe_once(time.time())
            except Exception:
                log.exception("connectivity probe failed")
            stop.wait(cfg.CONN_PROBE_INTERVAL)

    def page_loop():
        # One service for everything: the daemon runs the page generator every
        # PAGE_INTERVAL, back-to-back if a run (night camera stack) takes longer.
        while not stop.is_set():
            try:
                _, elapsed = run_page_once(cfg)
            except Exception:
                log.exception("status page runner failed")
                elapsed = 0.0
            stop.wait(max(1.0, cfg.PAGE_INTERVAL - elapsed))

    threading.Thread(target=evaluator, name="evaluator", daemon=True).start()
    threading.Thread(target=rain_loop, name="rain-poller", daemon=True).start()
    if nws is not None:
        threading.Thread(target=nws_loop, name="nws-poller", daemon=True).start()
    else:
        log.warning("NWS forecast component disabled (TTU_SAFETY_NWS=0)")
    if glm is not None:
        threading.Thread(target=glm_loop, name="glm-poller", daemon=True).start()
    else:
        log.warning("GLM lightning component disabled (TTU_SAFETY_GLM=0)")
    if radar is not None:
        threading.Thread(target=radar_loop, name="radar-poller", daemon=True).start()
    else:
        log.warning("MRMS radar component disabled (TTU_SAFETY_RADAR=0)")
    if conn is not None:
        threading.Thread(target=conn_loop, name="conn-probe", daemon=True).start()
    else:
        log.warning("connectivity watchdog disabled (TTU_SAFETY_CONN=0)")
    if cfg.PAGE_ENABLED:
        threading.Thread(target=page_loop, name="page-runner", daemon=True).start()
        log.info("status page runner: %s every %ds", cfg.PAGE_SCRIPT, cfg.PAGE_INTERVAL)
    else:
        log.warning("status page runner disabled (TTU_SAFETY_PAGE=0)")
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
