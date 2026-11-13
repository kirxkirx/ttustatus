"""Safety aggregation: the rain poller (with the 3-hour latch) and the SafetyMonitor
that combines sun altitude, humidity, and rain into a single IsSafe boolean.

Design rules:
  * FAIL SAFE — any missing/stale input, or any unmet condition, means UNSAFE.
  * Rain trips on the FIRST detection by ANY station (no confirmation), and stays unsafe
    for RAIN_LATCH_HOURS after the last rain seen (the latch is persisted to disk).
  * WU is only polled when the sun is below RAIN_POLL_SUN_BELOW_DEG (saves API calls).
  * NWS alerts: ONLY the configured HAZARD_VETO_EVENTS in effect OVER THE SITE veto (for
    the life of the warning); every other alert, and all the non-NWS hazard information
    (smoke, fires, SPC, storm reports), is display-only and is never read by the IsSafe
    decision.
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

from . import (connectivity as conn_mod, dew as dew_mod, glm_lightning, nws_forecast,
               radar as radar_mod, weatherlog as weatherlog_mod, wu_poll)

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
# ... and none is carried forward beyond this after the layer's last report, whatever its
# end time: a veto that cannot be confirmed gone must not hang on for ever (the same 24 h
# as the poller's own bound, nws_alerts.UNSEEN_RELEASE_SEC).
HAZARD_UNSEEN_RELEASE_SEC = 24 * 3600
# A broken layer's traceback is logged when the error changes, else at most this often:
# evaluate() runs on every Alpaca poll, and the journal lives on the SD card.
COMPONENT_ERROR_LOG_SEC = 600
# The rain layer's results are "stale" (no data, not "no rain") once the last completed WU
# poll is older than this many poll intervals while polling should be running.
RAIN_STALE_POLLS = 3
# "Rain check blind" warning (never a veto): this many consecutive WU polls in a row with
# no station reporting, while polling is active and a key is set.
RAIN_BLIND_POLLS = 3
# A gap this long between two WU polls starts a new blind count (the next night).
RAIN_BLIND_RESET_SEC = 7200


def _finite(x):
    """Return x if it is a finite real number, else None (so callers fail safe)."""
    if isinstance(x, (int, float)) and math.isfinite(x):
        return x
    return None


def _persisted_end(persisted, now, hold_sec):
    """A broken layer's own latch end (epoch) for SafetyMonitor._guarded, or None:
    persisted() reads it from the layer's state as its latch file restored it, without
    the layer's component(). An end in the past is none; an endless one counts hold_sec
    from now. Never raises: it is the fallback of a fallback."""
    try:
        end = persisted()
        if end is None or isinstance(end, bool):
            return None
        end = float(end)
    except Exception:  # noqa: BLE001
        return None
    if math.isnan(end):
        return None
    if math.isinf(end):
        return now + float(hold_sec) if end > 0 else None
    return end if end > now else None


def _radar_latch_end(radar, cfg, now):
    """The radar layer's latch end: its last rain on the ring (a stamp from the future, as
    after a clock step, read as now, the way the layer itself heals it) plus RADAR_LATCH_SEC."""
    last = getattr(radar, "_last_rain_ts", None)
    if last is None:
        return None
    return min(float(last), now) + float(cfg.RADAR_LATCH_SEC)


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


def _rain_unavailable(cfg, error=None):
    """The 'rain' component when the rain poller itself fails: no data, and no veto of its
    own (SafetyMonitor carries a latch it last reported forward separately)."""
    return {"safe": True, "enabled": bool(getattr(cfg, "WU_API_KEY", "")),
            "available": False, "latched": False, "latched_until": None,
            "seconds_remaining": 0, "last_rain_ts": None, "polling_active": False,
            "last_poll_ts": None, "last_poll_age_s": None, "stale": True, "note": None,
            "stations_live": 0, "stations_total": 0, "stations_error": 0,
            "throttle": None, "hold_until": None, "blind_polls": 0, "blind_since": None,
            "raining": [], "results": [], "error": error}


def _local_hhmm(ts, cfg):
    """'21:45 CDT' for an epoch (local time), or '?' for one that cannot be shown."""
    try:
        return nws_forecast._local(datetime.fromtimestamp(ts, timezone.utc),
                                   cfg.LOCAL_TZ).strftime("%H:%M %Z")
    except (OverflowError, OSError, ValueError, TypeError):
        return "?"


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
                "smoke": None, "fires": [],
                "spc": {"category": None, "label": "", "mds": []},
                "lsr": [], "error": None,
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
        # WU asked us to wait (HTTP 429/503 with Retry-After): no poll before this epoch
        self._hold_until = None
        # consecutive polls with no station reporting, and when that run began
        self._blind_polls = 0
        self._blind_since = None
        # the last time component() found polling off (daytime, sun unknown, no key): a
        # poll older than that is no sign of a stuck poll loop (see component's STALE)
        self._inactive_ts = None
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
            # each entry validated (a non-empty id, a finite distance) INSIDE the guard:
            # a malformed discovery reply keeps the previous station list
            found = wu_poll.clean_station_list(wu_poll.discover_stations(*self.cfg.GEOCODE))
        except Exception as e:  # noqa: BLE001 - discovery failed: keep the old list
            log.warning("WU station discovery failed (%s: %s) — keeping the previous "
                        "station list (%d stations)", type(e).__name__, str(e)[:200],
                        len(self._stations))
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
        throttle = result.get("throttle")
        with self._lock:
            # HTTP 429/503: honour Retry-After (wu_poll bounds it to an hour) before the
            # next poll; without one the next poll comes at the normal interval
            wait = _finite((throttle or {}).get("retry_after_s"))
            self._hold_until = now + wait if wait and wait > 0 else None
            # blind count: consecutive polls in which no station reported
            prev = self._last_poll_ts
            if result.get("live", 0) > 0:
                self._blind_polls, self._blind_since = 0, None
            else:
                gap = None if prev is None else now - prev
                if (self._blind_since is None or gap is None or gap < 0
                        or gap > max(RAIN_STALE_POLLS * self.cfg.WU_POLL_INTERVAL,
                                     RAIN_BLIND_RESET_SEC)):
                    self._blind_polls, self._blind_since = 1, now
                else:
                    self._blind_polls += 1
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
        results = result.get("results", [])
        # A reply that cannot be used ("bad-reply") counts against the station only when
        # another station of the same poll got a proper answer (a reading, or WU's own "no
        # data" for a dead station): when none did, the garbage is WU's (an HTML error page
        # sent as HTTP 200), like a 5xx, and backing every station off for it would leave
        # the rain layer blind after WU recovers.
        answered = any(r.get("state") in ("live", "stale", "offline") for r in results)
        with self._lock:
            for r in results:
                sid = r.get("station")
                if r.get("state") == "bad-reply" and not answered:
                    continue
                if r.get("state") in ("error", "not-asked", "backed-off"):
                    # an API or network problem (HTTP 429/5xx, a timeout) or a station
                    # this poll did not ask: says nothing about the station — after a
                    # WU or network outage the stations must not all be backed off,
                    # leaving the rain layer blind for an hour after the recovery
                    continue
                if r.get("state") in wu_poll.BACKOFF_STATES:
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
            hold = self._hold_until
            if hold is not None and (now >= hold
                                     or hold - now > wu_poll.RETRY_AFTER_MAX_S + 60):
                self._hold_until = hold = None      # over, or a clock step: forget it
            if hold is not None:
                due = False                         # WU's Retry-After is still running
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
            self._hold_until = None
            self._inactive_ts = None

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
            # STALE: polling should be running but the last completed poll is old (a hung
            # or crashing poll loop, a long Retry-After). Its "N/M live, no rain" is no
            # longer a reading: report no live station, so the page and the Alpaca text
            # say "no data" instead of "no rain". No veto change. Timed from the last poll,
            # or from when polling was last seen off if that is later: at dusk the last
            # poll is the morning's, and a day of sun-up silence is no stuck poll loop
            # (the first poll of the night follows within the rain loop's 30 s tick).
            if not polling_active:
                self._inactive_ts = now
            poll_age = (None if self._last_poll_ts is None
                        else now - self._last_poll_ts)
            since_off = (self._inactive_ts is not None and self._last_poll_ts is not None
                         and self._inactive_ts > self._last_poll_ts)
            quiet = now - self._inactive_ts if since_off else poll_age
            stale = bool(polling_active and quiet is not None
                         and quiet > RAIN_STALE_POLLS * self.cfg.WU_POLL_INTERVAL)
            # (a blind run of polls made before polling was last off is not tonight's)
            blind_polls = self._blind_polls if polling_active and not since_off else 0
            return {
                "safe": not latched,
                "enabled": enabled,
                "latched": latched,
                "latched_until": self._latch_until,
                "seconds_remaining": remaining,
                "last_rain_ts": self._last_rain_ts,
                "polling_active": polling_active,
                "last_poll_ts": self._last_poll_ts,
                "last_poll_age_s": None if poll_age is None else round(poll_age),
                "stale": stale,
                # how long no poll has completed while polling was on (the blind warning)
                "stale_for_s": round(quiet) if stale else None,
                "note": (None if not stale else
                         "stale, no WU poll completed for %d min" % round(quiet / 60)
                         if since_off else "stale, last poll %d min ago" % round(quiet / 60)),
                "stations_live": 0 if stale else res.get("live", 0),
                "stations_total": res.get("total", len(self._stations)),
                "stations_error": res.get("errors", 0),
                "throttle": res.get("throttle"),
                "hold_until": self._hold_until,
                # consecutive polls with no station reporting (see SafetyMonitor's
                # "rain check blind" warning), and when the run began
                "blind_polls": blind_polls,
                "blind_since": self._blind_since if blind_polls else None,
                "raining": res.get("raining", []),
                "results": res.get("results", []),
            }


class SafetyMonitor:
    """Aggregates the inputs into IsSafe and publishes state for the status page."""

    def __init__(self, cfg, eventlog, poller: RainPoller, nws=None, glm=None, radar=None,
                 conn=None, hazards=None, hazard_feeds=None, hazards_error=None,
                 hazard_feeds_error=None, dew=None, weatherlog=None):
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
        # rain / GLM / radar: when each layer's last reported veto runs out (epoch), carried
        # forward while the layer itself is broken (see _guarded)
        self._held_latch: dict = {}
        # connectivity: the verdict last reported, and when (monotonic) the watchdog last
        # reported "safe" — a watchdog broken that long vetoes, like an hour offline
        self._conn_last_safe = None
        self._conn_ok_mono = time.monotonic()
        self._comp_err_logged: dict = {}   # component name -> (message, monotonic ts)
        # The dew monitor (Alpaca device 1, safety/dew.py) and the weather log. Neither
        # is an input of IsSafe: device 1 only READS the verdict made here, and the log
        # only records. None = switched off.
        self.dew = dew                  # DewMonitor or None
        self.weatherlog = weatherlog    # WeatherLog or None
        self._last_dew_safe = None      # for transition logging
        self._dew_last_read = None      # monotonic time a client last read IsSafe(1)
        self._lock = threading.Lock()
        self._eval_lock = threading.Lock()   # serialize whole evaluations
        self._connected: dict = {}      # Alpaca device number -> its Connected flag
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
                # enclosure temperature, same DHT reading as the humidity (weather log
                # only; absent from an older page script)
                "temperature": _finite(d.get("temperature_c")),
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

        # Every gating layer is called through _guarded: a layer that raises (or returns
        # something that is not a component) must not take evaluate() down — that would
        # turn /issafe into an HTTP 500, freeze the state file and take device 1 with it.
        # A broken layer is "unavailable" (no veto of its own, like an unreachable feed)
        # — except that a veto it last reported is held to that veto's own end. The
        # warnings list names the broken layer.
        sun_for_layers = None if stale else sun_alt

        # Rain latch is independent of the page inputs; it applies even when stale.
        rain = self._guarded(
            "rain", lambda: self.poller.component(sun_for_layers, now),
            lambda err: _rain_unavailable(self.cfg, err), now, warnings,
            hold_sec=self.cfg.RAIN_LATCH_HOURS * 3600,
            persisted=lambda: getattr(self.poller, "_latch_until", None))
        rain_safe = rain["safe"]
        if not rain_safe:
            reasons.append("rain latch active (%d min left%s)"
                           % (self._mins_left(rain),
                              "; held, the rain layer failed" if rain.get("held") else ""))

        # NWS forecast (pre-emptive): unsafe only when it has fresh data that breaches a
        # threshold; an unavailable/stale forecast does not flip safe->unsafe on its own,
        # and neither does a forecast layer that fails (no hold: it is a forecast).
        if self.nws is not None:
            nws = self._guarded(
                "forecast", lambda: self.nws.component(now),
                lambda err: dict(nws_forecast.unavailable_component(self.cfg),
                                 enabled=True, error=err),
                now, warnings, hold_sec=None,
                check=lambda c: isinstance(c.get("reasons", []), list))
        else:
            nws = nws_forecast.unavailable_component(self.cfg)
        nws_safe = nws["safe"]
        if not nws_safe:
            reasons.extend(str(r) for r in (nws.get("reasons") or ["NWS forecast unsafe"]))

        # GLM lightning latch (independent of page inputs; applies even when stale).
        if self.glm is not None:
            glm = self._guarded(
                "lightning", lambda: self.glm.component(sun_for_layers, now),
                lambda err: dict(glm_lightning.unavailable_component(self.cfg),
                                 enabled=True, error=err),
                now, warnings, hold_sec=self.cfg.GLM_COOLOFF_HOURS * 3600,
                persisted=lambda: getattr(self.glm, "_latch_until", None))
        else:
            glm = glm_lightning.unavailable_component(self.cfg)
        glm_safe = glm["safe"]
        if not glm_safe:
            reasons.append("lightning within %g km (GLM latch %d min left%s)"
                           % (self._km(glm, self.cfg.GLM_TRIGGER_KM), self._mins_left(glm),
                              "; held, the GLM layer failed" if glm.get("held") else ""))

        # MRMS radar (simple any-rain ring, RADAR_TRIGGER_KM). Live check; unavailable
        # => no veto.
        if self.radar is not None:
            radar = self._guarded(
                "radar", lambda: self.radar.component(sun_for_layers, now),
                lambda err: dict(radar_mod.unavailable_component(self.cfg),
                                 enabled=True, error=err),
                now, warnings, hold_sec=self.cfg.RADAR_LATCH_SEC,
                persisted=lambda: _radar_latch_end(self.radar, self.cfg, now))
        else:
            radar = radar_mod.unavailable_component(self.cfg)
        radar_safe = radar["safe"]
        if not radar_safe:
            near = _finite(radar.get("nearest_km"))
            reasons.append("rain on radar within %g km%s%s"
                           % (self._km(radar, self.cfg.RADAR_TRIGGER_KM),
                              "" if near is None else " (nearest %g km)" % near,
                              " (held %d min, the radar layer failed)" % self._mins_left(radar)
                              if radar.get("held") else ""))
        self._rain_blind_warning(rain, radar, now, warnings)

        # Basemap configuration problems (a CARTO key CARTO did not accept, a User-Agent
        # OpenStreetMap blocks or that lacks a contact): shown as warnings, NEVER a veto —
        # the basemap is decoration, the rain check never reads it.
        # The radar's and GLM's own warnings (the wrong-clock guard: "this host's clock is
        # 158 d behind the radar servers' — radar frames refused ...") are shown the same
        # way: the layer already reports itself unavailable, which never vetoes on its own.
        for wl in (radar.get("basemap_warnings"), radar.get("warnings"), glm.get("warnings")):
            if isinstance(wl, list):
                for w in wl:
                    if isinstance(w, str) and w and w not in warnings:
                        warnings.append(w)

        # Connectivity watchdog (loss of internet). HARD veto after the offline threshold.
        conn = self._conn_component(warnings)
        conn_safe = conn["safe"]
        if not conn_safe:
            if conn.get("error"):
                reasons.append("connectivity watchdog failed — no proof the internet is "
                               "up (failing safe)")
            else:
                reasons.append("no internet — online services unreachable for %d min "
                               "(failing safe)" % (_finite(conn.get("offline_min")) or 0))

        # NWS warnings OVER THE SITE. Only the configured veto events (Tornado / Dust
        # Storm / High Wind Warning by default) can veto, each for the life of the warning
        # (the poller persists it; one no fresh query has listed for 24 h is released);
        # every other alert is display-only. With no veto held
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

        # Settings the daemon corrected or ignored at startup: shown (never a veto) for as
        # long as they apply, not only once in the log.
        for w in getattr(self.cfg, "CONFIG_WARNINGS", None) or ():
            text = "configuration: %s" % w
            if text not in warnings:
                warnings.append(text)

        # Hazard INFORMATION (smoke, fires, SPC, storm reports) is shown on the page and
        # the map and is NEVER part of the decision: it is fetched for the state file
        # only, and deliberately not referenced below (tests pin this).
        hazard_info = self._hazard_info_component(now)

        is_safe = bool(sun_safe and hum_safe and rain_safe and nws_safe
                       and glm_safe and radar_safe and conn_safe and hazards_safe
                       and not stale)

        # Device 1, the dew monitor. It comes AFTER the decision above and is no part of
        # it: it reads is_safe, never the other way round (tests pin this).
        outlook = self._forecast_outlook(now)
        dew = self._dew_component(is_safe, rain, outlook, glm, now)

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
            # Alpaca device 1: "a WET rain-sensor reading right now is dew". Kept apart
            # from "components" on purpose — it is not one of this monitor's inputs.
            "dew": dew,
            "weather_log": self._weatherlog_status(),
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
        self._log_dew_transition(dew)
        with self._lock:
            self._state = state
        self._maybe_write_state(state, now)
        if self.weatherlog is not None:
            # RAM only. The enclosure reading is logged only while it is current (the
            # same staleness rule as the humidity check above). observe() catches its
            # own errors; the guard here is for a log object broken beyond that — the
            # verdict above is already made and must be returned whatever happens.
            try:
                current = inp is not None and not stale and humidity is not None
                self.weatherlog.observe(state, now, outlook=outlook, inputs={
                    "temperature": inp.get("temperature") if current else None,
                    "humidity": humidity if current else None})
            except Exception as e:
                self._log_component_error("weather_log", e)
        return state

    # -- the gating layers, guarded ------------------------------------------------
    def _guarded(self, name, call, fallback, now, warnings, hold_sec, check=None,
                 persisted=None):
        """One gating layer's component, never an exception.

        call() must return a dict whose 'safe' is a bool (and whose seconds_remaining, if
        any, is a finite number; `check` may add a test). Anything else, or an exception,
        is logged (_log_component_error) and replaced by fallback(error_text): an
        UNAVAILABLE layer, which does not veto by itself — unless the layer's last good
        report was a veto that is still running. That veto is held, marked 'held', to its
        own end: now + seconds_remaining at the last report (hold_sec when it gave none;
        hold_sec None = never held). With no good report since the start (a layer that
        breaks from its first call after a restart), persisted() gives the latch end the
        layer restored from its own latch file, held the same way. A bug in the code that
        REPORTS rain must never be what reopens the dome. Either way a warning names the
        broken layer."""
        try:
            comp = call()
            if not isinstance(comp, dict) or not isinstance(comp.get("safe"), bool):
                raise TypeError("malformed %s component (%s)" % (name, type(comp).__name__))
            rem = comp.get("seconds_remaining")
            if rem is not None and _finite(rem) is None:
                raise TypeError("%s component: seconds_remaining is %r" % (name, rem))
            if check is not None and not check(comp):
                raise TypeError("malformed %s component" % name)
        except Exception as e:  # noqa: BLE001 - a broken layer must not break IsSafe
            self._log_component_error(name, e)
            err = "%s layer error: %s: %s" % (name, type(e).__name__, str(e)[:200])
            comp = fallback(err)
            comp.update(safe=True, available=False, error=err)
            with self._lock:
                until = self._held_latch.get(name) if hold_sec is not None else None
            if until is None and hold_sec is not None and persisted is not None:
                until = _persisted_end(persisted, now, hold_sec)
            if until is not None and now < until:
                comp.update(safe=False, latched=True, held=True,
                            seconds_remaining=int(math.ceil(until - now)))
                warnings.append("%s layer failed (%s) — its last veto is held until %s"
                                % (name, type(e).__name__, _local_hhmm(until, self.cfg)))
            else:
                warnings.append("%s layer failed (%s) — treated as unavailable (no veto "
                                "from it); see the daemon log" % (name, type(e).__name__))
            return comp
        if hold_sec is not None:
            with self._lock:
                if comp["safe"]:
                    self._held_latch.pop(name, None)
                else:
                    rem = _finite(comp.get("seconds_remaining"))
                    self._held_latch[name] = now + (rem if rem and rem > 0
                                                    else float(hold_sec))
        return comp

    def _conn_component(self, warnings) -> dict:
        """The connectivity component, never an exception. The watchdog is a HARD veto
        after CONN_OFFLINE_UNSAFE_SEC offline; when it breaks, the verdict it last gave is
        kept (an "offline too long" stays unsafe), and a watchdog that has not reported
        "safe" for CONN_OFFLINE_UNSAFE_SEC vetoes too: a broken watchdog proves the
        internet up no better than a failed probe."""
        if self.conn is None:
            return conn_mod.unavailable_component(self.cfg)
        mono = time.monotonic()
        try:
            comp = self.conn.component()      # its clock is monotonic-internal
            if not isinstance(comp, dict) or not isinstance(comp.get("safe"), bool):
                raise TypeError("malformed connectivity component (%s)"
                                % type(comp).__name__)
        except Exception as e:  # noqa: BLE001 - a broken layer must not break IsSafe
            self._log_component_error("connectivity", e)
            with self._lock:
                last_safe, ok_mono = self._conn_last_safe, self._conn_ok_mono
            broken = max(0.0, mono - ok_mono)
            unsafe = last_safe is False or broken > self.cfg.CONN_OFFLINE_UNSAFE_SEC
            comp = dict(conn_mod.unavailable_component(self.cfg))
            comp.update(safe=not unsafe, enabled=True, available=False,
                        error="connectivity layer error: %s: %s"
                              % (type(e).__name__, str(e)[:200]),
                        offline_sec=round(broken), offline_min=round(broken / 60))
            warnings.append("connectivity layer failed (%s) — %s" % (
                type(e).__name__, "failing safe" if unsafe else
                "its last verdict (online) is kept; UNSAFE once it has been broken for "
                "%d min" % round(self.cfg.CONN_OFFLINE_UNSAFE_SEC / 60)))
            return comp
        with self._lock:
            self._conn_last_safe = comp["safe"]
            if comp["safe"]:
                self._conn_ok_mono = mono
        return comp

    @staticmethod
    def _mins_left(comp) -> int:
        return int((_finite(comp.get("seconds_remaining")) or 0) // 60)

    @staticmethod
    def _km(comp, default):
        km = _finite(comp.get("trigger_km"))
        return default if km is None else km

    @staticmethod
    def _rain_blind_warning(rain, radar, now, warnings) -> None:
        """A NON-VETOING warning while the rain check is blind: polling active, a WU key
        set, and either no station has reported for RAIN_BLIND_POLLS polls in a row or
        the last completed poll is stale. 0 of N stations reporting counts as "no rain"
        for IsSafe by design (the owner's choice: a warning only); this makes it visible
        on the page and /setup."""
        if not (rain.get("enabled") and rain.get("polling_active")):
            return
        radar = ("radar still watches for rain"
                 if radar.get("available") and not radar.get("error")
                 else "the radar is unavailable too — nothing watches for rain")
        if rain.get("stale"):
            age = (_finite(rain.get("stale_for_s")) or _finite(rain.get("last_poll_age_s"))
                   or 0)
            warnings.append("rain check blind: no WU poll completed for %d min (%s)"
                            % (round(age / 60), radar))
            return
        polls = _finite(rain.get("blind_polls")) or 0
        since = _finite(rain.get("blind_since"))
        if polls >= RAIN_BLIND_POLLS and since is not None:
            warnings.append("rain check blind: 0/%d WU stations reporting for %d min (%s)"
                            % (_finite(rain.get("stations_total")) or 0,
                               max(0, round((now - since) / 60)), radar))

    # -- dew monitor (Alpaca device 1) and weather log ---------------------------
    def _forecast_outlook(self, now):
        """NwsForecastPoller.outlook() for the dew monitor and the weather log, or None
        (forecast layer off, or a poller without it)."""
        fn = getattr(self.nws, "outlook", None)
        if not callable(fn):
            return None
        try:
            out = fn(now, self.cfg.DEW_FORECAST_MAX_AGE_MIN * 60.0)
            if not isinstance(out, dict):
                raise TypeError("outlook is %s, not a dict" % type(out).__name__)
            return out
        except Exception as e:
            self._log_component_error("forecast outlook", e)
            return {"available": False, "why": "forecast outlook error: %s" % str(e)[:120],
                    "age_s": None, "this_hour": None, "next_hour": None}

    def _dew_component(self, is_safe, rain, outlook, glm, now) -> dict:
        """The device 1 block of the state. Whatever goes wrong in there, the answer is
        'not safe' = the rain sensor is trusted — and IsSafe (device 0) is unaffected.
        `rain` and `glm` are the components of this same evaluation: the dew monitor
        reads the stations' observations and the nearest lightning out of them."""
        cfg = self.cfg
        if self.dew is None:
            comp = dew_mod.unavailable_component(cfg)
        else:
            try:
                self.dew.update_stations(rain.get("results"), now)
                self.dew.update_lightning(glm, now)
                comp = self.dew.evaluate(is_safe, outlook, now)
                if not isinstance(comp, dict) or not isinstance(comp.get("is_safe"), bool):
                    raise TypeError("malformed dew component (%s)" % type(comp).__name__)
                comp = _json_clean(comp)
            except Exception as e:
                self._log_component_error("dew", e)
                comp = dew_mod.unavailable_component(
                    cfg, "dew monitor error: %s" % str(e)[:200])
                comp["enabled"] = True
        with self._lock:
            read = self._dew_last_read
        comp["connected"] = self.is_connected(cfg.DEW_DEVICE_NUMBER)
        # how long ago a client (the dome driver) last asked: tells the operator, from
        # this side, whether the driver is polling at all
        comp["last_read_age_s"] = None if read is None else round(time.monotonic() - read)
        comp["alpaca"] = {
            "device_number": cfg.DEW_DEVICE_NUMBER, "name": cfg.DEW_SERVER_NAME,
            "issafe_path": f"/api/v1/safetymonitor/{cfg.DEW_DEVICE_NUMBER}/issafe"}
        return comp

    def _log_dew_transition(self, dew) -> None:
        """A line in the daemon log when the device 1 verdict changes. Deliberately NOT
        an event-log record: that file is on the SD card, and this verdict may change
        several times a night. The weather log (RAM) has every change with its inputs."""
        if self.dew is None:
            return
        value = bool(dew.get("is_safe"))
        with self._lock:
            prev, self._last_dew_safe = self._last_dew_safe, value
        if prev is value:
            return
        try:
            if value:
                def n(key):
                    v = _finite(dew.get(key))
                    return "?" if v is None else "%.1f" % v
                log.info("dew monitor (device %d): SAFE — dew likely, the rain sensor may "
                         "be ignored (spread %s °C, wind %s mph)",
                         self.cfg.DEW_DEVICE_NUMBER, n("spread_c"), n("wind_mph"))
            else:
                reasons = dew.get("reasons")
                reasons = reasons if isinstance(reasons, (list, tuple)) else []
                log.info("dew monitor (device %d): UNSAFE — rain sensor trusted (%s)",
                         self.cfg.DEW_DEVICE_NUMBER,
                         "; ".join(str(r) for r in reasons) or "no reason given")
        except Exception as e:      # a log line must never cost an evaluation
            self._log_component_error("dew transition log", e)

    def _weatherlog_status(self) -> dict:
        if self.weatherlog is None:
            return weatherlog_mod.unavailable_status()
        try:
            return _json_clean(self.weatherlog.status())
        except Exception as e:
            self._log_component_error("weather_log", e)
            return weatherlog_mod.unavailable_status("weather log error: %s" % str(e)[:200])

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
        warning's end_ts, or HAZARD_NO_END_HOLD_SEC after the last report without one, and
        never beyond HAZARD_UNSEEN_RELEASE_SEC after the last report (a layer broken for a
        day can no longer confirm anything: the held veto is released, not kept for ever)."""
        with self._lock:
            last, seen = list(self._last_veto), self._last_veto_ts
        held = []
        base = _finite(seen)
        base = now if base is None else base
        for v in last:
            end = _finite(v.get("end_ts"))
            if end is None:
                end = base + HAZARD_NO_END_HOLD_SEC
            end = min(end, base + HAZARD_UNSEEN_RELEASE_SEC)
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
        dew = state.get("dew") or {}
        sig = json.dumps({
            "is_safe": state["is_safe"], "reasons": state["reasons"],
            "warnings": state["warnings"], "connected": state["connected"],
            # the device 1 verdict (and whether the dome driver is connected to it) is
            # shown on the page: written at once when it changes
            "dew": (dew.get("is_safe"), dew.get("connected"), dew.get("reasons")),
            "comp": {k: (c.get("safe"), c.get("latched"), c.get("available"),
                         c.get("enabled")) for k, c in state["components"].items()},
            # a change in WHICH warnings veto (or their end time: an extension) is written
            # at once — even when the reasons read the same (two warnings, same text)
            "hazard_veto": sorted("%s|%s" % (v.get("key"), v.get("end_ts"))
                                  for v in hz.get("veto") or [] if isinstance(v, dict)),
        }, sort_keys=True)
        # (0 <= : after the clock steps BACK the difference is negative for the length of
        # the step — without the lower bound no heartbeat was written all that time, and
        # the file on disk stayed dated in the future, which the page reads as "fresh")
        if (sig == self._last_state_sig
                and 0 <= (now - self._last_state_write) < self.cfg.STATE_WRITE_HEARTBEAT_SEC):
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

    def is_dew_safe(self) -> bool:
        """IsSafe of Alpaca device 1 (see safety/dew.py). False whenever in doubt."""
        return bool((self.evaluate().get("dew") or {}).get("is_safe"))

    def note_dew_read(self) -> None:
        """A client read IsSafe(1) just now (shown on the status page)."""
        with self._lock:
            self._dew_last_read = time.monotonic()

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def set_connected(self, value: bool, device=None):
        """The Alpaca Connected flag of one device (default: the main monitor)."""
        with self._lock:
            self._connected[self.cfg.DEVICE_NUMBER if device is None else device] = bool(value)

    def is_connected(self, device=None) -> bool:
        with self._lock:
            return bool(self._connected.get(
                self.cfg.DEVICE_NUMBER if device is None else device, False))
