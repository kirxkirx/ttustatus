"""TTU Alpaca SafetyMonitor daemon — wires the pieces together and serves the API.

Threads:
  * evaluator    — every EVAL_INTERVAL: recompute IsSafe, log transitions, write state.
  * rain-poller  — every ~30 s: if it's night (sun below the gate) and the interval has
                   elapsed, poll Weather Underground and update the 3-hour latch.
  * nws-poller   — every ~60 s: if the interval elapsed, pull the NWS gridpoint forecast.
  * hazard-alerts — every ~10 s: if due, the NWS active-alerts point query (the warning
                   veto, every HAZARD_POINT_POLL_SEC) and area query (map + list).
  * hazard-feeds — every ~60 s: if due, the INFORMATION-ONLY hazard feeds (quakes, smoke,
                   fires, SPC, storm reports, space weather).
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
from .monitor import (HAZARD_FEEDS_IMPORT_ERROR, NWS_ALERTS_IMPORT_ERROR, RainPoller,
                      SafetyMonitor, hazard_feeds, nws_alerts)
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
    start = time.monotonic()
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
        return None, time.monotonic() - start
    if proc.returncode != 0:
        tail = (out or b"")[-2000:].decode("utf-8", "replace")
        log.warning("status page run failed (rc=%s):\n%s", proc.returncode, tail)
    return proc.returncode, time.monotonic() - start


# Loop wake-ups. The hazard-alerts loop only asks the poller what is due (point query
# every HAZARD_POINT_POLL_SEC, area query every HAZARD_AREA_POLL_SEC), so waking often is
# free and a new warning over the site is seen within about one point-poll interval.
HAZARD_LOOP_WAKE_SEC = 10
HAZARD_FEEDS_LOOP_WAKE_SEC = 60
# The radar loop also carries the hazard overlays: RadarPoller.maybe_poll re-renders the
# last MRMS frame (no refetch) when the overlay set changes, so with overlays it wakes
# every 20 s — a new warning reaches the map about a minute after the alert poll sees
# it. Without overlays, nothing but the 5-min poll is due and 60 s is plenty.
RADAR_LOOP_WAKE_SEC = 60
RADAR_LOOP_WAKE_OVERLAYS_SEC = 20


def _start_thread(target, name):
    """Start one of the daemon's background loops (a seam the tests replace)."""
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def _make_layer(module, import_error, cls_name, args, label, eventlog, consequence,
                errors=None, name=None):
    """Instantiate one hazard poller, or report LOUDLY why not and return None.

    A layer that cannot be created (its module failed to import, its constructor raised)
    is left out and then reads as 'unavailable' — exactly like an unreachable feed —
    instead of taking the whole daemon down with it: the rain, sun, humidity, lightning
    and radar layers must keep protecting the dome regardless. The reason also goes into
    ``errors[name]``, for SafetyMonitor to publish: an enabled layer that failed to start
    must not look like one switched off on purpose."""
    try:
        if module is None:
            raise ImportError(import_error or "module missing")
        return getattr(module, cls_name)(*args)
    except Exception as e:
        log.error("%s layer could not be created (%s: %s) — %s", label,
                  type(e).__name__, e, consequence, exc_info=True)
        eventlog.record("CONFIG", reason=f"{label} layer failed: {type(e).__name__}: {e}",
                        result=consequence)
        if errors is not None and name:
            errors[name] = f"{type(e).__name__}: {e}"
        return None


def build_hazard_layers(cfg, eventlog, errors=None):
    """(hazards, hazard_info): the NWS-alerts poller — the narrow warning veto — and the
    INFORMATION-ONLY hazard-feeds poller, each created only when enabled (else None).
    ``errors`` (a dict), if given, receives {"hazards" / "hazard_info": why} for an
    enabled layer that could not be created."""
    hazards = hazard_info = None
    if cfg.HAZARDS_ENABLED:
        hazards = _make_layer(
            nws_alerts, NWS_ALERTS_IMPORT_ERROR, "NwsAlertsPoller", (cfg, eventlog),
            "NWS alerts", eventlog,
            "NWS warnings layer DISABLED — no veto for %s"
            % (", ".join(cfg.HAZARD_VETO_EVENTS) or "any event"), errors, "hazards")
    if cfg.HAZARD_FEEDS_ENABLED:
        hazard_info = _make_layer(
            hazard_feeds, HAZARD_FEEDS_IMPORT_ERROR, "HazardFeedsPoller", (cfg,),
            "hazard information", eventlog,
            "hazard information feeds disabled (information only; IsSafe unaffected)",
            errors, "hazard_info")
    return hazards, hazard_info


def overlay_sources(*layers):
    """The zero-arg overlay callables the radar map draws (each layer's overlays())."""
    return [layer.overlays for layer in layers
            if layer is not None and callable(getattr(layer, "overlays", None))]


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config

    eventlog = EventLog(cfg.EVENT_LOG)
    poller = RainPoller(cfg, eventlog)
    nws = NwsForecastPoller(cfg) if cfg.NWS_ENABLED else None
    glm = GlmLightningPoller(cfg, eventlog) if cfg.GLM_ENABLED else None
    layer_errors = {}
    hazards, hazard_info = build_hazard_layers(cfg, eventlog, layer_errors)
    # The radar map draws the hazard layers (alert areas, smoke, fires, quakes, ...) over
    # the radar image; the pollers hand it their overlays() as zero-arg callables.
    sources = overlay_sources(hazards, hazard_info)
    radar = (RadarPoller(cfg, eventlog, overlay_sources=sources or None)
             if cfg.RADAR_ENABLED else None)
    conn = ConnectivityWatch(cfg) if cfg.CONN_ENABLED else None   # monotonic-internal
    monitor = SafetyMonitor(cfg, eventlog, poller, nws=nws, glm=glm, radar=radar, conn=conn,
                            hazards=hazards, hazard_feeds=hazard_info,
                            hazards_error=layer_errors.get("hazards"),
                            hazard_feeds_error=layer_errors.get("hazard_info"))

    for w in cfg.CONFIG_WARNINGS:
        log.warning("CONFIG: %s", w)
        # each message says what is used instead (default, clamped value, kept name)
        eventlog.record("CONFIG", reason=w, result="check configuration")
    # (a placeholder contact is already a CONFIG warning above)
    if not cfg.ua_has_real_email(cfg.NWS_USER_AGENT) and not cfg.ua_has_placeholder(
            cfg.NWS_USER_AGENT):
        log.warning("TTU_SAFETY_NWS_UA=%r has no contact e-mail — set it to e.g. "
                    "'ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; "
                    "you@example.org)' with you@example.org replaced by YOUR real address: "
                    "NWS asks for a contact, and OpenStreetMap (the backup basemap) wants a "
                    "User-Agent that identifies the app", cfg.NWS_USER_AGENT)
    if radar is not None:
        # which basemap chain runs and whether a key is configured — never the key itself
        log.info("radar basemap: TTU_SAFETY_RADAR_BASEMAP=%s, CARTO key %s",
                 cfg.RADAR_BASEMAP, "set" if cfg.CARTO_API_KEY else "not set")
    eventlog.record("STARTUP", detail=f"{cfg.SERVER_NAME} v{cfg.DRIVER_VERSION}",
                    result=f"http {cfg.HTTP_HOST}:{cfg.HTTP_PORT}",
                    site=f"{cfg.GEOCODE[0]},{cfg.GEOCODE[1]} ({cfg.GEOCODE_SOURCE})")
    if hazards is not None:
        if cfg.HAZARD_VETO_EVENTS:
            log.info("NWS alerts: UNSAFE while any of [%s] is in effect over the site; "
                     "every other alert is display-only", ", ".join(cfg.HAZARD_VETO_EVENTS))
        else:
            log.warning("TTU_SAFETY_HAZARD_VETO_EVENTS is empty — NWS alerts are "
                        "DISPLAY-ONLY; no warning can veto observing")
            eventlog.record("CONFIG", reason="no hazard veto events configured",
                            result="NWS alerts display-only")
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
        # Clock-step detector: wall time can STEP (NTP sync after a wrong-RTC boot — see
        # the 2026-08 incident where a March-clock boot read August latches as "227329
        # min left"); monotonic cannot. When the two disagree by more than 30 s between
        # iterations, the wall clock jumped: say so loudly, and give every component a
        # chance to re-arm evaporating latches and re-fetch data under the correct date.
        last_wall, last_mono = time.time(), time.monotonic()
        while not stop.is_set():
            try:
                wall, mono = time.time(), time.monotonic()
                step = (wall - last_wall) - (mono - last_mono)
                if abs(step) > 30.0:
                    log.critical("SYSTEM CLOCK STEPPED by %+.0f s (%+.2f days) — NTP "
                                 "sync or wrong RTC. Re-arming latches and re-polling "
                                 "all layers under the corrected clock.",
                                 step, step / 86400.0)
                    eventlog.record("CLOCK-STEP", reason=f"{step:+.0f}s",
                                    result="latches re-checked, all layers re-polled")
                    for comp in (poller, nws, glm, radar, hazards, hazard_info):
                        if comp is not None:
                            try:
                                comp.clock_stepped(last_wall, wall)
                            except Exception:
                                log.exception("clock_stepped failed for %r", comp)
                last_wall, last_mono = wall, mono
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
        wake = RADAR_LOOP_WAKE_OVERLAYS_SEC if sources else RADAR_LOOP_WAKE_SEC
        while not stop.is_set():
            try:
                radar.maybe_poll(_fresh_sun(monitor, cfg), time.time())
            except Exception:
                log.exception("radar poll failed")
            stop.wait(wake)

    def hazards_loop():
        # Own thread: the NWS point/area queries and the one-time zone-shape fetches never
        # block the evaluator or HTTP; maybe_poll decides which query is due.
        while not stop.is_set():
            try:
                hazards.maybe_poll(time.time())
            except Exception:
                log.exception("NWS alerts poll failed")
            stop.wait(HAZARD_LOOP_WAKE_SEC)

    def hazard_feeds_loop():
        # INFORMATION ONLY, in its own thread: a slow or hung feed delays nothing else.
        while not stop.is_set():
            try:
                hazard_info.maybe_poll(time.time())
            except Exception:
                log.exception("hazard feeds poll failed")
            stop.wait(HAZARD_FEEDS_LOOP_WAKE_SEC)

    def conn_loop():
        # Probe internet reachability day and night so the "offline > 1 h" clock is accurate.
        while not stop.is_set():
            try:
                conn.probe_once()
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

    _start_thread(evaluator, "evaluator")
    _start_thread(rain_loop, "rain-poller")
    if nws is not None:
        _start_thread(nws_loop, "nws-poller")
    else:
        log.warning("NWS forecast component disabled (TTU_SAFETY_NWS=0)")
    if glm is not None:
        _start_thread(glm_loop, "glm-poller")
    else:
        log.warning("GLM lightning component disabled (TTU_SAFETY_GLM=0)")
    if radar is not None:
        _start_thread(radar_loop, "radar-poller")
    else:
        log.warning("MRMS radar component disabled (TTU_SAFETY_RADAR=0)")
    if hazards is not None:
        _start_thread(hazards_loop, "hazard-alerts")
    elif not cfg.HAZARDS_ENABLED:      # (a failed layer was already reported loudly)
        log.warning("NWS alerts layer disabled (TTU_SAFETY_HAZARDS=0) — no veto for %s",
                    ", ".join(cfg.HAZARD_VETO_EVENTS) or "any event")
    if hazard_info is not None:
        _start_thread(hazard_feeds_loop, "hazard-feeds")
    elif not cfg.HAZARD_FEEDS_ENABLED:
        log.warning("hazard information feeds disabled (TTU_SAFETY_HAZARD_FEEDS=0)")
    if conn is not None:
        _start_thread(conn_loop, "conn-probe")
    else:
        log.warning("connectivity watchdog disabled (TTU_SAFETY_CONN=0)")
    if cfg.PAGE_ENABLED:
        _start_thread(page_loop, "page-runner")
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
