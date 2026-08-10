"""Safety aggregation: the rain poller (with the 3-hour latch) and the SafetyMonitor
that combines sun altitude, humidity, and rain into a single IsSafe boolean.

Design rules:
  * FAIL SAFE — any missing/stale input, or any unmet condition, means UNSAFE.
  * Rain trips on the FIRST detection by ANY station (no confirmation), and stays unsafe
    for RAIN_LATCH_HOURS after the last rain seen (the latch is persisted to disk).
  * WU is only polled when the sun is below RAIN_POLL_SUN_BELOW_DEG (saves API calls).
"""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import tempfile
import threading
import time

from . import glm_lightning, nws_forecast, wu_poll

log = logging.getLogger("ttu.safety.monitor")


def _finite(x):
    """Return x if it is a finite real number, else None (so callers fail safe)."""
    if isinstance(x, (int, float)) and math.isfinite(x):
        return x
    return None


_cached_ip = None


def _primary_ip():
    """Best-effort primary outbound IPv4 of this host, for display on the status page."""
    global _cached_ip
    if _cached_ip:
        return _cached_ip
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # no packet sent; just picks the egress interface
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    _cached_ip = ip
    return ip


class RainPoller:
    """Polls Weather Underground and owns the persistent 3-hour rain latch."""

    def __init__(self, cfg, eventlog):
        self.cfg = cfg
        self.log = eventlog
        self._lock = threading.Lock()
        self._stations: list = []       # [(station_id, distance_km), ...]
        self._stations_ts = 0.0
        self._latch_until = None        # epoch when the rain latch expires
        self._last_rain_ts = None
        self._last_poll_ts = None
        self._last_result: dict | None = None
        self._latch_dirty = False       # a latch we failed to persist; retry later
        self._load_latch()

    # -- persistence ---------------------------------------------------------
    def _load_latch(self):
        try:
            with open(self.cfg.LATCH_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return  # genuinely first run -> no latch
        except Exception:
            # Corrupt/unreadable latch file: we cannot prove rain has cleared, so
            # fail safe and arm a full latch rather than starting clean.
            self._latch_until = time.time() + self.cfg.RAIN_LATCH_HOURS * 3600
            self._latch_dirty = True
            log.warning("latch file unreadable; arming a full latch (fail-safe)")
            return
        self._latch_until = _finite(d.get("latch_until"))
        self._last_rain_ts = d.get("last_rain_ts")
        if self._latch_until and self._latch_until > time.time():
            mins = int((self._latch_until - time.time()) / 60)
            log.info("restored active rain latch (%d min remaining)", mins)

    def _save_latch(self) -> bool:
        try:
            tmp = self.cfg.LATCH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"latch_until": self._latch_until,
                           "last_rain_ts": self._last_rain_ts}, f)
            os.replace(tmp, self.cfg.LATCH_FILE)
            return True
        except Exception:
            log.exception("cannot persist rain latch to %s", self.cfg.LATCH_FILE)
            return False

    def _retry_persist(self, now):
        """Re-attempt a previously-failed latch save (e.g. after a transient disk error)."""
        with self._lock:
            if not self._latch_dirty:
                return
            active = self._latch_until is not None and self._latch_until > now
            if not active:
                self._latch_dirty = False   # nothing worth persisting anymore
            elif self._save_latch():
                self._latch_dirty = False

    # -- polling -------------------------------------------------------------
    def _ensure_stations(self, now):
        if self._stations and (now - self._stations_ts) < 86400:
            return
        try:
            found = wu_poll.discover_stations(*self.cfg.GEOCODE)
        except Exception:
            log.exception("station discovery failed")
            return
        if found:
            self._stations = found
            self._stations_ts = now
            names = ", ".join(f"{s} ({d:.1f}km)" for s, d in found)
            self.log.record("WU-STATIONS", detail=f"{len(found)} nearest: {names}")

    def poll_now(self, now=None) -> dict:
        """Poll WU once and update the latch. Returns the poll-result dict."""
        if now is None:
            now = time.time()
        with self._lock:
            self._ensure_stations(now)
            stations = list(self._stations)
        result = wu_poll.poll_stations(stations)
        with self._lock:
            self._last_poll_ts = now
            self._last_result = result
            if result["raining"]:
                self._last_rain_ts = now
                self._latch_until = now + self.cfg.RAIN_LATCH_HOURS * 3600
                self._latch_dirty = not self._save_latch()   # retry later if it failed
                which = ", ".join(f"{r['station']} {r['precip_in_hr']} in/hr"
                                  for r in result["raining"])
                self.log.record(
                    "RAIN", reason=which, source="wu",
                    result=f"latch {self.cfg.RAIN_LATCH_HOURS:g}h",
                    live=f"{result['live']}/{result['total']}")
        return result

    def maybe_poll(self, sun_alt, now=None):
        """Poll only if it's night (sun below the gate) and the interval has elapsed."""
        if now is None:
            now = time.time()
        self._retry_persist(now)
        if not self.cfg.WU_API_KEY:
            return None                      # no key -> rain polling disabled (logged at startup)
        if sun_alt is None or sun_alt >= self.cfg.RAIN_POLL_SUN_BELOW_DEG:
            return None
        with self._lock:
            due = (self._last_poll_ts is None
                   or (now - self._last_poll_ts) >= self.cfg.WU_POLL_INTERVAL)
        if not due:
            return None
        return self.poll_now(now)

    def component(self, sun_alt, now=None) -> dict:
        if now is None:
            now = time.time()
        with self._lock:
            # A non-finite latch (should not happen; defensive) is treated as latched.
            latched = self._latch_until is not None and (
                not math.isfinite(self._latch_until) or now < self._latch_until)
            remaining = (int(self._latch_until - now)
                         if latched and math.isfinite(self._latch_until) else 0)
            res = self._last_result or {}
            enabled = bool(self.cfg.WU_API_KEY)
            polling_active = (enabled and sun_alt is not None
                              and sun_alt < self.cfg.RAIN_POLL_SUN_BELOW_DEG)
            return {
                "safe": not latched,
                "enabled": enabled,
                "latched": latched,
                "latched_until": self._latch_until,
                "seconds_remaining": remaining,
                "last_rain_ts": self._last_rain_ts,
                "polling_active": polling_active,
                "last_poll_ts": self._last_poll_ts,
                "stations_live": res.get("live", 0),
                "stations_total": res.get("total", len(self._stations)),
                "raining": res.get("raining", []),
                "results": res.get("results", []),
            }


class SafetyMonitor:
    """Aggregates the inputs into IsSafe and publishes state for the status page."""

    def __init__(self, cfg, eventlog, poller: RainPoller, nws=None, glm=None):
        self.cfg = cfg
        self.log = eventlog
        self.poller = poller
        self.nws = nws                  # NwsForecastPoller or None (component fails to unknown)
        self.glm = glm                  # GlmLightningPoller or None
        self._lock = threading.Lock()
        self._eval_lock = threading.Lock()   # serialize whole evaluations
        self._connected = False
        # Fail-safe start: assume humidity-unsafe until a fresh reading below the clear
        # threshold proves otherwise (the in-memory hysteresis latch isn't persisted, so
        # a restart in the 93-95% hold band must not silently report safe).
        self._humidity_unsafe = True
        self._last_is_safe = None       # for transition logging
        self._state: dict = {}

    # -- inputs from make_status_page.py -------------------------------------
    def read_inputs(self):
        try:
            with open(self.cfg.INPUTS_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return None
        ts = _finite(d.get("ts"))
        age = (time.time() - ts) if ts is not None else None
        return {"sun_alt": _finite(d.get("sun_altitude_deg")),
                "humidity": _finite(d.get("humidity_pct")), "ts": ts, "age": age}

    # -- evaluation ----------------------------------------------------------
    def evaluate(self) -> dict:
        # Serialize evaluations so hysteresis, transition logging, and the state-file
        # write can't interleave across the evaluator thread and HTTP handlers.
        with self._eval_lock:
            return self._evaluate()

    def _evaluate(self) -> dict:
        now = time.time()
        inp = self.read_inputs()
        age = inp["age"] if inp else None
        sun_alt = inp["sun_alt"] if inp else None
        humidity = inp["humidity"] if inp else None
        stale = ((inp is None) or (age is None)
                 or (age > self.cfg.INPUTS_STALE_SEC)
                 or (age < -self.cfg.CLOCK_SKEW_TOLERANCE_SEC))

        reasons: list[str] = []
        if stale:
            reasons.append("sensor inputs stale or missing — failing safe")
            sun_safe = False
            hum_safe = False
        else:
            sun_safe = self._eval_sun(sun_alt, reasons)
            hum_safe = self._eval_humidity(humidity, reasons)

        # Rain latch is independent of the page inputs; it applies even when stale.
        rain = self.poller.component(None if stale else sun_alt, now)
        rain_safe = rain["safe"]
        if not rain_safe:
            reasons.append(f"rain latch active ({rain['seconds_remaining'] // 60} min left)")

        # NWS forecast (pre-emptive): unsafe only when it has fresh data that breaches a
        # threshold; an unavailable/stale forecast does not flip safe->unsafe on its own.
        if self.nws is not None:
            nws = self.nws.component(now)
        else:
            nws = nws_forecast.unavailable_component(self.cfg)
        nws_safe = nws["safe"]
        if not nws_safe:
            reasons.extend(nws["reasons"])

        # GLM lightning latch (independent of page inputs; applies even when stale).
        if self.glm is not None:
            glm = self.glm.component(None if stale else sun_alt, now)
        else:
            glm = glm_lightning.unavailable_component(self.cfg)
        glm_safe = glm["safe"]
        if not glm_safe:
            reasons.append("lightning within %g km (GLM latch %d min left)"
                           % (glm["trigger_km"], glm["seconds_remaining"] // 60))

        is_safe = bool(sun_safe and hum_safe and rain_safe and nws_safe
                       and glm_safe and not stale)

        state = {
            "ts": now,
            "is_safe": is_safe,
            "connected": self.is_connected(),
            "alpaca": {
                "address": _primary_ip(),
                "port": self.cfg.HTTP_PORT,
                "device_number": self.cfg.DEVICE_NUMBER,
                "name": self.cfg.SERVER_NAME,
                "issafe_path": f"/api/v1/safetymonitor/{self.cfg.DEVICE_NUMBER}/issafe",
            },
            "reasons": [] if is_safe else reasons,
            "components": {
                "sun": {"value_deg": sun_alt,
                        "threshold_deg": self.cfg.SUN_UNSAFE_ABOVE_DEG,
                        "safe": sun_safe, "stale": stale,
                        "age_s": round(age) if age is not None else None},
                "humidity": {"value_pct": humidity,
                             "threshold_pct": self.cfg.HUMIDITY_UNSAFE_ABOVE,
                             "safe": hum_safe, "stale": stale,
                             "age_s": round(age) if age is not None else None},
                "rain": rain,
                "nws": nws,
                "glm": glm,
            },
            "events_tail": [self._fmt_event(e) for e in self.log.recent(12)],
        }

        self._log_transition(is_safe, reasons)
        with self._lock:
            self._state = state
        self._write_state(state)
        return state

    def _eval_sun(self, sun_alt, reasons) -> bool:
        if sun_alt is None or not math.isfinite(sun_alt):
            reasons.append("sun altitude unknown — failing safe")
            return False
        safe = sun_alt <= self.cfg.SUN_UNSAFE_ABOVE_DEG
        if not safe:
            reasons.append(f"sun above horizon ({sun_alt:.1f}° > "
                           f"{self.cfg.SUN_UNSAFE_ABOVE_DEG:g}°)")
        return safe

    def _eval_humidity(self, humidity, reasons) -> bool:
        if humidity is None or not math.isfinite(humidity):
            reasons.append("humidity unknown — failing safe")
            return False
        with self._lock:
            if humidity > self.cfg.HUMIDITY_UNSAFE_ABOVE:
                self._humidity_unsafe = True
            elif humidity < self.cfg.HUMIDITY_CLEAR_BELOW:
                self._humidity_unsafe = False
            unsafe = self._humidity_unsafe
        if unsafe:
            reasons.append(f"humidity {humidity:.0f}% > {self.cfg.HUMIDITY_UNSAFE_ABOVE:g}%")
        return not unsafe

    def _log_transition(self, is_safe, reasons):
        with self._lock:
            prev = self._last_is_safe
            self._last_is_safe = is_safe
        if prev is None:
            self.log.record("INIT", result="safe" if is_safe else "unsafe",
                            reason="; ".join(reasons))
        elif prev and not is_safe:
            self.log.record("UNSAFE", reason="; ".join(reasons))
        elif is_safe and not prev:
            self.log.record("SAFE", result="recovered")

    def _write_state(self, state):
        # Unique temp file per write so concurrent writers never share/clobber one inode
        # (the atomic tmp+replace only holds for a private temp).
        try:
            d = os.path.dirname(self.cfg.STATE_FILE) or "."
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".safety_state.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(state, f)
                os.replace(tmp, self.cfg.STATE_FILE)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            log.exception("cannot write state file %s", self.cfg.STATE_FILE)

    @staticmethod
    def _fmt_event(e) -> str:
        parts = [e.get("time", ""), e.get("action", "")]
        if e.get("reason"):
            parts.append(e["reason"])
        if e.get("result"):
            parts.append(f"({e['result']})")
        return "  ".join(p for p in parts if p)

    # -- accessors -----------------------------------------------------------
    def is_safe(self) -> bool:
        return self.evaluate()["is_safe"]

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def set_connected(self, value: bool):
        with self._lock:
            self._connected = bool(value)

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected
