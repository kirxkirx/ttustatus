"""Safety aggregation: the rain poller (with the 3-hour latch) and the SafetyMonitor
that combines sun altitude, humidity, and rain into a single IsSafe boolean.

Design rules:
  * FAIL SAFE — any missing/stale input, or any unmet condition, means UNSAFE.
  * Rain trips on the FIRST detection by ANY station (no confirmation), and stays unsafe
    for RAIN_LATCH_HOURS after the last rain seen (the latch is persisted to disk).
  * WU is only polled when the sun is below RAIN_POLL_SUN_BELOW_DEG (saves API calls).
  * NWS alerts: ONLY the configured HAZARD_VETO_EVENTS in effect OVER THE SITE veto (for
    the life of the warning); every other alert, and all the non-NWS hazard information
    (quakes, smoke, fires, SPC, storm reports, space weather), is display-only and is
    never read by the IsSafe decision.
"""
from __future__ import annotations

import importlib
import json
import logging
import math
import os
import socket
import tempfile
import threading
import time
from datetime import datetime, timezone

from . import (connectivity as conn_mod, glm_lightning, nws_forecast,
               radar as radar_mod, wu_poll)

log = logging.getLogger("ttu.safety.monitor")


def _import_layer(name):
    """Import one of the two hazard modules defensively: (module, None) or (None, why).

    Both are stdlib-only, so on a sane deploy this never fails. But hazard_feeds is
    INFORMATION ONLY, and nws_alerts already fails to "unavailable" by design — so a broken
    or missing file (a partial git pull, a syntax slip) must degrade exactly like an
    unreachable feed, never stop the daemon that serves IsSafe for rain, sun, humidity and
    lightning. server.main() reports a failed import LOUDLY (log + CONFIG event)."""
    try:
        return importlib.import_module("." + name, __package__), None
    except Exception as e:  # noqa: BLE001 — anything at import time = layer absent
        return None, "%s: %s" % (type(e).__name__, e)


nws_alerts, NWS_ALERTS_IMPORT_ERROR = _import_layer("nws_alerts")
hazard_feeds, HAZARD_FEEDS_IMPORT_ERROR = _import_layer("hazard_feeds")

# When the hazard layer itself breaks, the vetoes it last reported are carried forward
# (see SafetyMonitor._held_vetoes); one that gave no end time is held this long after its
# last report — the same bound the NWS-alerts poller applies to such a warning.
HAZARD_NO_END_HOLD_SEC = 3600
# A broken layer's traceback is logged when the error changes, else at most this often:
# evaluate() runs on every Alpaca poll, and the journal lives on the SD card.
COMPONENT_ERROR_LOG_SEC = 600


def _finite(x):
    """Return x if it is a finite real number, else None (so callers fail safe)."""
    if isinstance(x, (int, float)) and math.isfinite(x):
        return x
    return None


def _json_clean(obj):
    """A deep, JSON-safe, detached copy of a component dict. The state file, /status and
    the status page all serialize the components; one stray datetime or set inside a
    hazard poller's output must not be able to stop the state file from being written
    (the page would then show the whole monitor as OFFLINE)."""
    return json.loads(json.dumps(obj, default=str))


def _import_note(err):
    return None if err is None else "module failed to import (%s)" % err


def _hazards_unavailable(cfg, error=None, enabled=False):
    """The 'hazards' component when the NWS-alerts layer is off, absent or broken: never a
    veto of its own (SafetyMonitor carries an already-held veto forward separately)."""
    comp = None
    if nws_alerts is not None:
        try:
            comp = dict(nws_alerts.unavailable_component(cfg))
        except Exception:
            log.exception("nws_alerts.unavailable_component failed")
    if not isinstance(comp, dict):
        comp = {"safe": True, "enabled": False, "available": False,
                "veto_events": list(getattr(cfg, "HAZARD_VETO_EVENTS", ())),
                "veto": [], "at_site": [], "nearby": [],
                "counts": {"at_site": 0, "nearby": 0},
                "point_age_s": None, "area_age_s": None, "error": None,
                "source": "NWS api.weather.gov active alerts (unavailable)"}
    comp.update(safe=True, available=False, veto=[])
    if enabled:
        comp["enabled"] = True
    if error:
        comp["error"] = error
    return comp


def _hazard_info_unavailable(cfg, error=None, enabled=False):
    """The 'hazard_info' component when the information feeds are off, absent or broken."""
    comp = None
    if hazard_feeds is not None:
        try:
            comp = dict(hazard_feeds.unavailable_component(cfg))
        except Exception:
            log.exception("hazard_feeds.unavailable_component failed")
    if not isinstance(comp, dict):
        comp = {"safe": True, "info_only": True, "enabled": False, "feeds": {},
                "quakes": [], "smoke": None, "fires": [],
                "spc": {"category": None, "label": "", "mds": []},
                "lsr": [], "space_weather": None, "error": None,
                "source": "hazard information feeds (unavailable)"}
    if enabled:
        comp["enabled"] = True
    if error:
        comp["error"] = error
    return comp


def _until_text(v, cfg, now):
    """' until 18:45 CDT' (weekday added when the end is not today, local time)."""
    end_ts = _finite(v.get("end_ts"))
    if end_ts is not None:
        try:
            end = nws_forecast._local(datetime.fromtimestamp(end_ts, timezone.utc),
                                      cfg.LOCAL_TZ)
            today = nws_forecast._local(datetime.fromtimestamp(now, timezone.utc),
                                        cfg.LOCAL_TZ).date()
            return " until " + end.strftime("%H:%M %Z" if end.date() == today
                                            else "%a %H:%M %Z")
        except (OverflowError, OSError, ValueError):
            pass                    # absurd timestamp: fall back to the poller's text
    if v.get("end_local"):
        return " until %s" % " ".join(str(v["end_local"]).split())
    return " (no end time given)"


def hazard_veto_reason(v, cfg, now) -> str:
    """One IsSafe reason per vetoing warning, e.g.
    'NWS Tornado Warning in effect for the site until 18:45 CDT (NWS Lubbock TX)'."""
    if not isinstance(v, dict):
        return "NWS warning in effect for the site (unreadable veto entry — failing safe)"
    event = " ".join(str(v.get("event") or "").split()) or "warning"
    text = "NWS %s in effect for the site%s" % (event, _until_text(v, cfg, now))
    notes = []
    if v.get("sender"):
        notes.append(" ".join(str(v["sender"]).split()))
    if v.get("source") == "latched":
        notes.append("held to its end time — not re-confirmed by a fresh NWS query")
    if notes:
        text += " (%s)" % "; ".join(notes)
    return text


# Detected LAN address, re-checked periodically. Never holds a failure (see _primary_ip).
_ip_cache = {"ip": None, "ts": 0.0}
IP_REFRESH_SEC = 300


def _primary_ip(now=None):
    """Best-effort primary outbound IPv4 of this host, for display on the status page.

    NEVER caches a failure. The daemon routinely starts before the network is up (a Pi
    boot after a power cut), and caching the loopback fallback made the page advertise
    127.0.0.1 — an address no other computer can use — for the whole life of the
    process, long after the network came up. A successful detection is re-checked every
    IP_REFRESH_SEC, so a DHCP change is picked up too.
    """
    now = time.time() if now is None else now
    cached = _ip_cache["ip"]
    if cached and (now - _ip_cache["ts"]) < IP_REFRESH_SEC:
        return cached
    ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))   # no packet sent; just picks the egress interface
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        ip = None
    if ip and not ip.startswith("127."):
        _ip_cache["ip"], _ip_cache["ts"] = ip, now
        return ip
    return cached          # keep the last good address; None until we ever had one


def alpaca_address(cfg, now=None):
    """The address to ADVERTISE for the Alpaca device on the status page.

    An explicit, non-wildcard HTTP_HOST is authoritative — including 127.0.0.1, which
    honestly means "reachable from this computer only". With the default wildcard bind
    the server answers on every interface, so we show the detected LAN address: that is
    the one NINA on another machine actually needs. Falls back to the hostname (usually
    resolvable as <name>.local via mDNS) rather than lying about loopback.
    """
    host = (getattr(cfg, "HTTP_HOST", "") or "").strip()
    if host and host not in ("0.0.0.0", "::", "*"):
        return host
    return _primary_ip(now) or socket.gethostname()


class RainPoller:
    """Polls Weather Underground and owns the persistent 3-hour rain latch."""

    def __init__(self, cfg, eventlog):
        self.cfg = cfg
        self.log = eventlog
        self._lock = threading.Lock()
        self._stations: list = []       # [(station_id, distance_km), ...]
        self._stations_ts = 0.0
        # dead-station backoff (in-memory; a restart just re-probes everything once)
        self._offline_streak: dict = {}   # sid -> consecutive 'offline' results
        self._backoff_until: dict = {}    # sid -> monotonic ts of the next probe
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
        # CLAMP: latch_until is always written as now+RAIN_LATCH_HOURS, so a persisted
        # value further in the future than one full latch means the clock is (or was)
        # wrong — a Pi booting with a stale RTC read an August latch under a March clock
        # as "227329 min left". Bound the damage to one full latch, loudly.
        max_until = time.time() + self.cfg.RAIN_LATCH_HOURS * 3600
        if self._latch_until is not None and self._latch_until > max_until + 60:
            log.warning("persisted rain latch expiry is %.1f days in the future — the "
                        "system clock is (or was) wrong; clamping to one full latch",
                        (self._latch_until - time.time()) / 86400.0)
            self._latch_until = max_until
            self._latch_dirty = True
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
        # NOTE: does network I/O — must be called OUTSIDE self._lock, or every IsSafe
        # response stalls behind a slow discovery request.
        with self._lock:
            fresh = self._stations and (now - self._stations_ts) < 86400
        if fresh:
            return
        try:
            found = wu_poll.discover_stations(*self.cfg.GEOCODE)
        except Exception:
            log.exception("station discovery failed")
            return
        # The WU API returns the ~10 NEAREST stations with no distance cap: in a sparse
        # region that can mean gauges 100s of km away that say nothing about rain here.
        near = [(s, d) for s, d in found if d <= self.cfg.WU_MAX_STATION_KM]
        dropped = len(found) - len(near)
        if dropped:
            log.warning("WU discovery: dropped %d station(s) beyond %g km", dropped,
                        self.cfg.WU_MAX_STATION_KM)
        excluded = [s for s, d in near if s.upper() in self.cfg.WU_EXCLUDE_STATIONS]
        if excluded:
            # operator-maintained blocklist (TTU_SAFETY_WU_EXCLUDE): a station known to
            # report bogus precipitation must not be able to close - or hold open - the
            # dome. Loud on purpose, so the exclusion is never forgotten silently.
            near = [(s, d) for s, d in near if s.upper() not in self.cfg.WU_EXCLUDE_STATIONS]
            log.warning("WU discovery: EXCLUDED unreliable station(s) by config: %s",
                        ", ".join(excluded))
        if not near:
            log.warning("WU discovery found NO stations within %g km of %s — "
                        "the rain layer has nothing to poll here",
                        self.cfg.WU_MAX_STATION_KM, self.cfg.GEOCODE)
            return
        with self._lock:
            self._stations = near
            self._stations_ts = now
            keep = {s for s, d in near}
            self._offline_streak = {k: v for k, v in self._offline_streak.items() if k in keep}
            self._backoff_until = {k: v for k, v in self._backoff_until.items() if k in keep}
        names = ", ".join(f"{s} ({d:.1f}km)" for s, d in near)
        detail = f"{len(near)} nearest: {names}"
        if excluded:
            detail += f" (excluded by config: {', '.join(excluded)})"
        self.log.record("WU-STATIONS", detail=detail)

    def poll_now(self, now=None) -> dict:
        """Poll WU once and update the latch. Returns the poll-result dict."""
        if now is None:
            now = time.time()
        self._ensure_stations(now)          # network I/O, outside the lock
        mono = time.monotonic()
        with self._lock:
            stations = list(self._stations)
            # dead-station backoff: skip stations that have been silent for
            # WU_BACKOFF_AFTER consecutive polls, except when their hourly revival
            # probe is due. Saves API calls on dead hardware; self-heals on answer.
            active = [(s, d) for s, d in stations
                      if self._backoff_until.get(s, 0) <= mono]
            skipped = [(s, d) for s, d in stations
                       if self._backoff_until.get(s, 0) > mono]
        result = wu_poll.poll_stations(active)
        self._update_backoff(result, mono)
        for sid, _d in skipped:             # keep live/total honest on the page
            result["results"].append({"station": sid, "state": "backed-off"})
        result["total"] += len(skipped)
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

    def _update_backoff(self, result, mono):
        after = max(1, int(self.cfg.WU_BACKOFF_AFTER))
        retry = float(self.cfg.WU_BACKOFF_RETRY_SEC)
        with self._lock:
            for r in result.get("results", []):
                sid = r.get("station")
                if r.get("state") == "offline":
                    streak = self._offline_streak.get(sid, 0) + 1
                    self._offline_streak[sid] = streak
                    if streak == after:
                        log.warning("WU station %s silent for %d consecutive polls — "
                                    "backing off to one probe per %.0f min",
                                    sid, streak, retry / 60.0)
                        self.log.record("WU-BACKOFF", detail=f"{sid} silent for "
                                        f"{streak} polls; probing every "
                                        f"{retry / 60:.0f} min")
                    if streak >= after:
                        self._backoff_until[sid] = mono + retry
                else:
                    # any RESPONSE (live or stale) ends the backoff immediately
                    if self._offline_streak.get(sid, 0) >= after:
                        log.info("WU station %s is answering again — resuming normal "
                                 "polling", sid)
                        self.log.record("WU-REVIVED", detail=f"{sid} answering again")
                    self._offline_streak.pop(sid, None)
                    self._backoff_until.pop(sid, None)

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
            # elapsed < 0 = the wall clock stepped backward past the last poll; treat as
            # due, or polling would stall for the entire size of the step
            elapsed = None if self._last_poll_ts is None else now - self._last_poll_ts
            due = elapsed is None or elapsed < 0 or elapsed >= self.cfg.WU_POLL_INTERVAL
        if not due:
            return None
        return self.poll_now(now)

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """The system clock stepped (NTP sync after a wrong-RTC boot). Re-arm a latch
        that the step would silently evaporate: it was armed under the OLD clock, so in
        real time the rain was recent regardless of what the clock said. Also force an
        immediate re-poll so all data is re-fetched under the corrected clock."""
        with self._lock:
            lu = self._latch_until
            if (lu is not None and math.isfinite(lu)
                    and lu > pre_now and lu <= post_now):
                self._latch_until = post_now + self.cfg.RAIN_LATCH_HOURS * 3600
                self._latch_dirty = True
                log.warning("rain latch re-armed across a clock step (it would have "
                            "silently evaporated)")
            self._last_poll_ts = None

    def component(self, sun_alt, now=None) -> dict:
        if now is None:
            now = time.time()
        with self._lock:
            # Self-heal at check time too: a clock stepping backward while we run would
            # otherwise stretch an armed latch to months (and nothing would ever rewrite
            # the file, because all polling stalls with it).
            max_until = now + self.cfg.RAIN_LATCH_HOURS * 3600
            if (self._latch_until is not None and math.isfinite(self._latch_until)
                    and self._latch_until > max_until + 60):
                log.warning("rain latch expiry beyond one full latch (clock step?) — "
                            "clamping")
                self._latch_until = max_until
                self._latch_dirty = True
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

    def __init__(self, cfg, eventlog, poller: RainPoller, nws=None, glm=None, radar=None,
                 conn=None, hazards=None, hazard_feeds=None, hazards_error=None,
                 hazard_feeds_error=None):
        self.cfg = cfg
        self.log = eventlog
        self.poller = poller
        self.nws = nws                  # NwsForecastPoller or None (component fails to unknown)
        self.glm = glm                  # GlmLightningPoller or None
        self.radar = radar              # RadarPoller or None
        self.conn = conn                # ConnectivityWatch or None
        self.hazards = hazards          # NwsAlertsPoller or None (the narrow warning veto)
        self.hazard_feeds = hazard_feeds  # HazardFeedsPoller or None (INFORMATION ONLY)
        # Why an ENABLED hazard layer is absent (its constructor raised; server.main passes
        # it): the state file then says so — "failed to start, no veto" — instead of
        # looking like a layer switched off on purpose (TTU_SAFETY_HAZARDS=0).
        self.hazards_error = hazards_error
        self.hazard_feeds_error = hazard_feeds_error
        # the vetoes the hazard layer last reported, and when (see _held_vetoes)
        self._last_veto: list = []
        self._last_veto_ts = None
        self._comp_err_logged: dict = {}   # component name -> (message, monotonic ts)
        self._lock = threading.Lock()
        self._eval_lock = threading.Lock()   # serialize whole evaluations
        self._connected = False
        # Fail-safe start: assume humidity-unsafe until a fresh reading below the clear
        # threshold proves otherwise (the in-memory hysteresis latch isn't persisted, so
        # a restart in the 93-95% hold band must not silently report safe).
        self._humidity_unsafe = True
        self._last_is_safe = None       # for transition logging
        self._state: dict = {}
        # Site coordinates: adopt the page's GPS fix once (rounded to ~1 km) when no env
        # override was given. The station is static, so this locks after first adoption.
        self._geocode_locked = cfg.GEOCODE_FROM_ENV
        # state-write throttle bookkeeping
        self._last_state_write = 0.0
        self._last_state_sig = None

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
                "humidity": _finite(d.get("humidity_pct")),
                "humidity_age_s": _finite(d.get("humidity_age_s")),
                "lat": _finite(d.get("lat")), "lon": _finite(d.get("lon")),
                "ts": ts, "age": age}

    # -- site coordinates -----------------------------------------------------
    def _update_geocode(self, inp, warnings):
        """Adopt the GPS fix once when env vars are unset; warn loudly on mismatch."""
        lat = inp.get("lat") if inp else None
        lon = inp.get("lon") if inp else None
        if lat is None or lon is None:
            return
        if not self._geocode_locked:
            adopted = self.cfg.round_coords(lat, lon)
            if adopted != self.cfg.GEOCODE:
                self.cfg.GEOCODE = adopted
                self.cfg.GEOCODE_SOURCE = "GPS (adopted from status page)"
                log.warning("adopted site coordinates from GPS: %s (rounded to ~1 km; "
                            "set TTU_SAFETY_LAT/LON to override)", adopted)
                self.log.record("CONFIG", reason="coordinates adopted from GPS",
                                result=f"{adopted[0]},{adopted[1]}")
            else:
                self.cfg.GEOCODE_SOURCE = "GPS (matches default)"
            self._geocode_locked = True
            return
        # Locked (env-set or already adopted): a big separation means a misconfigured or
        # moved site — make it loudly visible, but do not veto (a GPS glitch must not
        # close the dome).
        dlat = (lat - self.cfg.GEOCODE[0]) * 111.0
        dlon = (lon - self.cfg.GEOCODE[1]) * 111.0 * math.cos(math.radians(lat))
        if math.hypot(dlat, dlon) > self.cfg.GEOCODE_MISMATCH_KM:
            warnings.append(
                "GPS position (%.4f, %.4f) is %.2f km from the configured site "
                "coordinates %s — weather layers may be watching the wrong place!"
                % (lat, lon, math.hypot(dlat, dlon), self.cfg.GEOCODE))

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
        warnings: list[str] = []
        self._update_geocode(inp, warnings)

        # The page may serve humidity from its sensor cache: honor the measurement's real
        # age (file ts alone would launder a stale reading as fresh).
        hum_age = inp.get("humidity_age_s") if inp else None
        if (humidity is not None and hum_age is not None
                and (age or 0) + hum_age > self.cfg.INPUTS_STALE_SEC):
            humidity = None                 # too old -> unknown -> fails safe below

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

        # MRMS radar (simple any-rain ring, RADAR_TRIGGER_KM). Live check; unavailable
        # => no veto.
        if self.radar is not None:
            radar = self.radar.component(None if stale else sun_alt, now)
        else:
            radar = radar_mod.unavailable_component(self.cfg)
        radar_safe = radar["safe"]
        if not radar_safe:
            near = radar.get("nearest_km")
            reasons.append("rain on radar within %g km%s"
                           % (radar["trigger_km"],
                              "" if near is None else " (nearest %g km)" % near))

        # Connectivity watchdog (loss of internet). HARD veto after the offline threshold.
        if self.conn is not None:
            conn = self.conn.component()      # its clock is monotonic-internal
        else:
            conn = conn_mod.unavailable_component(self.cfg)
        conn_safe = conn["safe"]
        if not conn_safe:
            reasons.append("no internet — online services unreachable for %d min "
                           "(failing safe)" % conn.get("offline_min", 0))

        # NWS warnings OVER THE SITE. Only the configured veto events (Tornado / Dust
        # Storm / High Wind Warning by default) can veto, each for the life of the warning
        # (the poller persists it); every other alert is display-only. With no veto held
        # an unavailable feed does not veto on its own, like the forecast and radar layers
        # (the connectivity watchdog covers a total outage). Independent of page inputs.
        hazards = self._hazards_component(now)
        veto = hazards["veto"]
        # Fail-safe: EITHER signal vetoes — a listed veto entry makes the layer unsafe even
        # if its flag disagrees, and an explicit safe=False is honoured with no entries.
        hazards_safe = bool(hazards["safe"]) and not veto
        if veto:
            reasons.extend(hazard_veto_reason(v, self.cfg, now) for v in veto)
        elif not hazards_safe:
            err = hazards.get("error")
            reasons.append("NWS hazard layer reports unsafe%s"
                           % (" (%s)" % str(err)[:120] if err else ""))

        # Hazard INFORMATION (quakes, smoke, fires, SPC, storm reports, space weather) is
        # shown on the page and the map and is NEVER part of the decision: it is fetched
        # for the state file only, and deliberately not referenced below (tests pin this).
        hazard_info = self._hazard_info_component(now)

        is_safe = bool(sun_safe and hum_safe and rain_safe and nws_safe
                       and glm_safe and radar_safe and conn_safe and hazards_safe
                       and not stale)

        state = {
            "ts": now,
            "is_safe": is_safe,
            "connected": self.is_connected(),
            "alpaca": {
                "address": alpaca_address(self.cfg, now),
                "port": self.cfg.HTTP_PORT,
                "device_number": self.cfg.DEVICE_NUMBER,
                "name": self.cfg.SERVER_NAME,
                "issafe_path": f"/api/v1/safetymonitor/{self.cfg.DEVICE_NUMBER}/issafe",
            },
            "reasons": [] if is_safe else reasons,
            "warnings": warnings,       # visible but non-vetoing (e.g. GPS/config mismatch)
            "geocode": {"lat": self.cfg.GEOCODE[0], "lon": self.cfg.GEOCODE[1],
                        "source": self.cfg.GEOCODE_SOURCE},
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
                "radar": radar,
                "connectivity": conn,
                "hazards": hazards,
                "hazard_info": hazard_info,     # information only (never gates)
            },
            "events_tail": [self._fmt_event(e) for e in self.log.recent(12)],
        }

        self._log_transition(is_safe, reasons)
        with self._lock:
            self._state = state
        self._maybe_write_state(state, now)
        return state

    # -- hazard layers ---------------------------------------------------------
    def _hazards_component(self, now) -> dict:
        """The NWS-alerts component, validated. A malformed result or an exception is
        'unavailable' — but never a RELEASE: the vetoes last reported stay in force until
        their own end time (see _held_vetoes)."""
        if self.hazards is None:
            if self.hazards_error:
                # enabled, but it could not be created: say so (and that nothing can veto)
                return _hazards_unavailable(
                    self.cfg, enabled=True,
                    error="NWS alerts layer failed to start (%s) — no warning veto; see "
                          "the log" % str(self.hazards_error)[:160])
            return _hazards_unavailable(self.cfg, error=_import_note(NWS_ALERTS_IMPORT_ERROR))
        try:
            comp = self.hazards.component(now)
            if (not isinstance(comp, dict) or not isinstance(comp.get("safe"), bool)
                    or not isinstance(comp.get("veto", []), list)):
                raise TypeError("malformed hazards component (%s)" % type(comp).__name__)
            comp = _json_clean(comp)
            comp["veto"] = comp.get("veto") or []
        except Exception as e:
            self._log_component_error("hazards", e)
            comp = _hazards_unavailable(self.cfg, enabled=True,
                                        error="hazard layer error: %s" % str(e)[:200])
            held = self._held_vetoes(now)
            if held:
                comp["veto"], comp["safe"] = held, False
            return comp
        with self._lock:
            self._last_veto = [v for v in comp["veto"] if isinstance(v, dict)]
            self._last_veto_ts = now
        return comp

    def _held_vetoes(self, now) -> list:
        """The vetoes last reported by the hazard layer that are still running by their
        own end time, marked 'latched'. A bug in the code that REPORTS a tornado warning
        must never be what reopens the dome. Bounded like the poller's own hold: to the
        warning's end_ts, or HAZARD_NO_END_HOLD_SEC after the last report without one."""
        with self._lock:
            last, seen = list(self._last_veto), self._last_veto_ts
        held = []
        for v in last:
            end = _finite(v.get("end_ts"))
            if end is None:
                end = (seen if seen is not None else now) + HAZARD_NO_END_HOLD_SEC
            if now < end:
                held.append(dict(v, source="latched"))
        return held

    def _hazard_info_component(self, now) -> dict:
        """INFORMATION ONLY. Whatever the feeds poller returns — or raises — can at most be
        displayed: the result is JSON-cleaned and stamped safe/info_only, an exception
        becomes an 'unavailable' component (never an HTTP error on IsSafe), and the
        decision in _evaluate never reads it."""
        if self.hazard_feeds is None and self.hazard_feeds_error:
            comp = _hazard_info_unavailable(
                self.cfg, enabled=True,
                error="hazard information feeds failed to start (%s); see the log"
                      % str(self.hazard_feeds_error)[:160])
        elif self.hazard_feeds is None:
            comp = _hazard_info_unavailable(self.cfg,
                                            error=_import_note(HAZARD_FEEDS_IMPORT_ERROR))
        else:
            try:
                comp = self.hazard_feeds.component(now)
                if not isinstance(comp, dict):
                    raise TypeError("hazard info component is %s, not a dict"
                                    % type(comp).__name__)
                comp = _json_clean(comp)
            except Exception as e:
                self._log_component_error("hazard_info", e)
                comp = _hazard_info_unavailable(self.cfg, enabled=True,
                                                error="hazard info error: %s" % str(e)[:200])
        comp["safe"] = True
        comp["info_only"] = True
        return comp

    def _log_component_error(self, name, exc):
        """Log a broken layer's traceback when its error changes, else every
        COMPONENT_ERROR_LOG_SEC — not on each of the many evaluations per minute."""
        msg = "%s: %s" % (type(exc).__name__, exc)
        mono = time.monotonic()
        with self._lock:
            last = self._comp_err_logged.get(name)
            if last and last[0] == msg and (mono - last[1]) < COMPONENT_ERROR_LOG_SEC:
                return
            self._comp_err_logged[name] = (msg, mono)
        log.error("%s component failed: %s", name, msg, exc_info=exc)

    def _maybe_write_state(self, state, now):
        """SD-wear throttle: evaluate() runs on every Alpaca poll, but the state file only
        needs writing when the DECISION changed, or as a slow heartbeat so the status
        page's staleness gate still sees a live daemon."""
        hz = state["components"].get("hazards") or {}
        sig = json.dumps({
            "is_safe": state["is_safe"], "reasons": state["reasons"],
            "warnings": state["warnings"], "connected": state["connected"],
            "comp": {k: (c.get("safe"), c.get("latched"), c.get("available"),
                         c.get("enabled")) for k, c in state["components"].items()},
            # a change in WHICH warnings veto (or their end time: an extension) is written
            # at once — even when the reasons read the same (two warnings, same text)
            "hazard_veto": sorted("%s|%s" % (v.get("key"), v.get("end_ts"))
                                  for v in hz.get("veto") or [] if isinstance(v, dict)),
        }, sort_keys=True)
        if (sig == self._last_state_sig
                and (now - self._last_state_write) < self.cfg.STATE_WRITE_HEARTBEAT_SEC):
            return
        self._last_state_sig = sig
        self._last_state_write = now
        self._write_state(state)

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
