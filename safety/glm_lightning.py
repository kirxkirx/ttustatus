"""GOES-19 GLM lightning component for the SafetyMonitor.

Polls GLM-L2-LCFA granules from AWS Open Data (anonymous S3, no key), parses them ENTIRELY
IN RAM (no SD-card writes), and LATCHES unsafe for GLM_COOLOFF_HOURS whenever any flash is
within GLM_TRIGGER_KM of the observatory. Polls only when the sun is below the night gate
(same idea as WU rain). Mirrors RainPoller's discipline: the slow S3/netCDF work happens
OUTSIDE the lock, so SafetyMonitor.evaluate() and the HTTP handlers (which only read the
cached latch via component()) never block on a poll.

Fail-safe: a fetch error — or missing numpy/netCDF4 — never clears an active latch and never
forces unsafe on its own; an active latch keeps IsSafe false regardless. A window in which
no granule could be read (GLM writes one every 20 s, flashes or not) is no data, never "no
lightning". GLM flashes are already geodetic lat/lon, so no projection is needed.

Wrong-clock guard: the bucket keeps years of granules and the listing names the hour from
THIS host's clock, so a Pi booted with a wrong clock would read another day's lightning as
current. A listing whose Date header disagrees with the clock by more than
SERVER_CLOCK_TOL_SEC is refused (servertime.py), and the component carries a warning.
Once refused, the clock has to agree to within the tolerance less
servertime.HYSTERESIS_SEC before granules are accepted again (no flicker at the edge),
and both changes are logged with a CLOCK event.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    import numpy as np
    import netCDF4
except Exception:                       # pragma: no cover - optional heavy deps
    np = None
    netCDF4 = None

from . import servertime

log = logging.getLogger("ttu.safety.glm")
PREFIX = "GLM-L2-LCFA"


def deps_available():
    return np is not None and netCDF4 is not None


GOES_EAST_SUBLON = -75.2


def site_in_fov(lat, lon, max_arc_deg=70.0):
    """GOES-East sees roughly a 75-80 deg arc from the sub-satellite point (0, -75.2);
    detection quality collapses near the limb, so use a conservative 70 deg. Outside it
    the lightning layer would be silently 'clear' forever — refuse loudly instead."""
    p1, p2 = math.radians(0.0), math.radians(lat)
    dl = math.radians(lon - GOES_EAST_SUBLON)
    arc = math.degrees(math.acos(
        max(-1.0, min(1.0, math.sin(p1) * math.sin(p2)
                      + math.cos(p1) * math.cos(p2) * math.cos(dl)))))
    return arc < max_arc_deg


# ----- low-level fetch/parse (all in RAM) -----------------------------------
def _fetch(url, timeout=15):
    """GET -> (body, response headers, this host's clock when the headers arrived)."""
    req = urllib.request.Request(url, headers={"User-Agent": "ttu-safety-glm/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        received = time.time()
        return r.read(), r.headers, received


def _get(url, timeout=15):
    return _fetch(url, timeout)[0]


def _list_keys(bucket, prefix):
    """(the keys under ``prefix``, the S3 server's clock minus this host's in s — None
    without a Date header)."""
    url = f"https://{bucket}.s3.amazonaws.com/?list-type=2&max-keys=1000&prefix={prefix}"
    reply = _fetch(url)
    body, headers = reply[0], reply[1]
    return (re.findall(r"<Key>([^<]+)</Key>", body.decode("utf-8", "replace")),
            servertime.skew_s(headers, reply[2] if len(reply) > 2 else None))


def _clock_tolerance(cfg) -> float:
    try:
        v = float(getattr(cfg, "SERVER_CLOCK_TOL_SEC", 60))
    except (TypeError, ValueError):
        v = 60.0
    return v if v > 0 and math.isfinite(v) else 0.0


class _WithLimit:
    """``cfg`` with SERVER_CLOCK_TOL_SEC replaced by the limit in force for one poll (the
    tolerance, or a little less while the clock is believed wrong — see
    servertime.HYSTERESIS_SEC); every other setting is read through from cfg. The poller
    hands this to poll_flashes(cfg), so the function keeps its one-argument signature (and
    every stand-in for it in the tests keeps working)."""

    def __init__(self, cfg, limit):
        self._cfg = cfg
        self.SERVER_CLOCK_TOL_SEC = limit

    def __getattr__(self, name):
        return getattr(self._cfg, name)


def _key_start(key):
    m = re.search(r"_s(\d{14})", key)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1)[:13], "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _flashes_from_buffer(buf):
    """(lats, lons) from a GLM granule, parsed IN RAM: from a file in /dev/shm (a RAM
    tmpfs) when there is one, else from the bytes in memory. NEVER from the SD card.

    Why /dev/shm first: GLM polls in a thread of its own, and Raspberry Pi OS's HDF5
    (trixie: 1.14, built thread-safe) keeps its "print errors" switch per thread. Opened
    from memory, libnetcdf first probes the dataset's NAME as a file; that probe fails,
    and in any thread but the main one HDF5 prints the failure as a ~20-line error stack
    on stderr — although the open itself then works. That was every granule, ~15 per poll,
    some 3600 journal lines an hour at night (seen in the Raspberry Pi OS test, CI's
    raspios.yml). A real file has nothing to probe in vain."""
    path = None
    if os.path.isdir("/dev/shm"):
        try:
            fd, path = tempfile.mkstemp(dir="/dev/shm", suffix=".nc")
            with os.fdopen(fd, "wb") as f:
                f.write(buf)
        except OSError:                 # /dev/shm unusable (full?): from memory below
            if path is not None:
                _unlink(path)
            path = None
    if path is not None:
        try:
            ds = netCDF4.Dataset(path, "r")
            try:
                return (np.asarray(ds.variables["flash_lat"][:]),
                        np.asarray(ds.variables["flash_lon"][:]))
            finally:
                ds.close()
        finally:
            _unlink(path)
    ds = netCDF4.Dataset("inmem.nc", mode="r", memory=buf)
    try:
        return (np.asarray(ds.variables["flash_lat"][:]),
                np.asarray(ds.variables["flash_lon"][:]))
    finally:
        ds.close()


def _hav_km(lat, lon, lats, lons):
    r = 6371.0
    p1 = math.radians(lat)
    dp = np.radians(lats - lat)
    dl = np.radians(lons - lon)
    a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(np.radians(lats)) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def _bearing(lat, lon, lat2, lon2):
    p1, p2 = math.radians(lat), math.radians(lat2)
    dl = math.radians(lon2 - lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _cardinal(d):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((d % 360) / 45 + 0.5) % 8]


def poll_flashes(cfg):
    """Fetch the last GLM_WINDOW_MIN of granules and summarise (SLOW; call outside locks)."""
    if not deps_available():
        return {"ok": False, "error": "numpy/netCDF4 not installed"}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=cfg.GLM_WINDOW_MIN)
    tol = _clock_tolerance(cfg)
    keys = []
    # The bucket's clock as measured on a listing that was ACCEPTED (None: no Date
    # header). Returned as "server_skew_s" so the poller learns that the clock agrees
    # again after a refusal; a refusal itself returns "clock_skew_s" instead.
    agreed = None
    for dt in (now, now - timedelta(hours=1)):     # cover a window crossing the hour
        try:
            got, skew = _list_keys(cfg.GLM_BUCKET, f"{PREFIX}/{dt:%Y}/{dt:%j}/{dt:%H}/")
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": f"list: {e}"}
        if tol and skew is not None and abs(skew) > tol:
            # the hours listed are the wrong clock's: another day's granules
            return {"ok": False, "clock_skew_s": skew,
                    "error": "%s (AWS S3): GLM granules refused until the clock is corrected"
                             % servertime.clock_phrase(skew)}
        if skew is not None:
            agreed = skew
        keys += got
    granules = sorted({k for k in keys if (_key_start(k) or now) >= cutoff})

    lat0, lon0 = cfg.GEOCODE
    nearest_km = None
    nearest_ll = None
    within = 0
    scanned = 0
    for key in granules:
        try:
            lats, lons = _flashes_from_buffer(_get(f"https://{cfg.GLM_BUCKET}.s3.amazonaws.com/{key}"))
        except Exception:
            continue                      # skip a bad/slow granule; next poll retries
        scanned += 1
        if int(getattr(lats, "size", 0)) == 0:
            continue
        d = _hav_km(lat0, lon0, lats, lons)
        within += int(np.count_nonzero(d <= cfg.GLM_TRIGGER_KM))
        i = int(np.argmin(d))
        if nearest_km is None or d[i] < nearest_km:
            nearest_km = float(d[i])
            nearest_ll = (float(lats[i]), float(lons[i]))
    # GLM writes a granule every 20 s, flashes or not: a window without one read is a
    # feed outage (or a clock off by most of the window), never "no lightning"
    if scanned == 0:
        return {"ok": False, "granules_scanned": 0, "server_skew_s": agreed, "error": (
            "no GLM granule in the last %d min" % cfg.GLM_WINDOW_MIN if not granules else
            "none of the %d GLM granules of the last %d min could be read"
            % (len(granules), cfg.GLM_WINDOW_MIN))}
    brng = _cardinal(_bearing(lat0, lon0, *nearest_ll)) if nearest_ll else None
    return {"ok": True, "error": None, "granules_scanned": scanned,
            "in_ring": within > 0, "flashes_in_ring": within,
            "nearest_km": round(nearest_km, 1) if nearest_km is not None else None,
            "nearest_bearing": brng, "server_skew_s": agreed}


# ----- latching poller (mirrors RainPoller) --------------------------------
class GlmLightningPoller:
    def __init__(self, cfg, eventlog):
        self.cfg = cfg
        self.log = eventlog
        self._lock = threading.Lock()
        self._fov_warned = False
        self._latch_until = None
        self._last_flash_ts = None
        self._latch_dirty = False
        self._last_poll_ts = None
        self._last_result = None
        self._nearest_km = None
        self._nearest_bearing = None
        # The wrong-clock guard's verdict from the bucket's Date headers: None (nothing
        # known yet), True (the last listing agreed), False (the last listing was
        # refused). It sets the next poll's limit (servertime.limit: hysteresis) and its
        # changes go to the log and the event log, as the radar's do.
        self._clock_ok = None
        # How the last poll failed: None (it did not), "clock" (the wrong-clock guard
        # refused it) or "poll" (anything else). A failed poll used to leave no trace in
        # the log at all; now each change between these is logged once — kept apart so a
        # feed outage right after a clock refusal is still reported.
        self._failing = None
        self._load_latch()

    def _load_latch(self):
        try:
            with open(self.cfg.GLM_LATCH_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            self._latch_until = time.time() + self.cfg.GLM_COOLOFF_HOURS * 3600
            self._latch_dirty = True
            log.warning("GLM latch file unreadable; arming a full latch (fail-safe)")
            return
        lu = d.get("latch_until")
        self._latch_until = lu if isinstance(lu, (int, float)) and math.isfinite(lu) else None
        self._last_flash_ts = d.get("last_flash_ts")
        # CLAMP a persisted expiry beyond one full cool-off: the clock is (or was) wrong
        # (see the 2026-08 incident: a stale-RTC boot read an August latch as 227202 min).
        max_until = time.time() + self.cfg.GLM_COOLOFF_HOURS * 3600
        if self._latch_until is not None and self._latch_until > max_until + 60:
            log.warning("persisted GLM latch expiry is %.1f days in the future — system "
                        "clock is (or was) wrong; clamping to one full cool-off",
                        (self._latch_until - time.time()) / 86400.0)
            self._latch_until = max_until
            self._latch_dirty = True
        if self._latch_until and self._latch_until > time.time():
            log.info("restored active GLM lightning latch (%d min remaining)",
                     max(0, int((self._latch_until - time.time()) / 60)))

    def _save_latch(self) -> bool:
        try:
            tmp = self.cfg.GLM_LATCH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"latch_until": self._latch_until,
                           "last_flash_ts": self._last_flash_ts}, f)
            os.replace(tmp, self.cfg.GLM_LATCH_FILE)
            return True
        except Exception:
            log.exception("cannot persist GLM latch to %s", self.cfg.GLM_LATCH_FILE)
            return False

    def _retry_persist(self, now):
        with self._lock:
            if not self._latch_dirty:
                return
            active = self._latch_until is not None and self._latch_until > now
            if not active:
                self._latch_dirty = False
            elif self._save_latch():
                self._latch_dirty = False

    def poll_now(self, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            clock_ok = self._clock_ok
        limit = servertime.limit(_clock_tolerance(self.cfg), clock_ok)
        res = poll_flashes(_WithLimit(self.cfg, limit))     # SLOW, outside the lock
        if not isinstance(res, dict):
            res = {"ok": False, "error": "poll returned %s" % type(res).__name__}
        with self._lock:
            self._last_poll_ts = now
            self._last_result = res
            was_clock, was_failing = self._clock_ok, self._failing
            if not res.get("ok") and res.get("clock_skew_s") is not None:
                self._clock_ok = False
            elif res.get("server_skew_s") is not None:
                self._clock_ok = True
            self._failing = (None if res.get("ok") else
                             "clock" if res.get("clock_skew_s") is not None else "poll")
            clock_now = self._clock_ok
            if res.get("ok"):
                self._nearest_km = res.get("nearest_km")
                self._nearest_bearing = res.get("nearest_bearing")
                if res.get("in_ring"):
                    self._last_flash_ts = now
                    self._latch_until = now + self.cfg.GLM_COOLOFF_HOURS * 3600
                    self._latch_dirty = not self._save_latch()
                    self.log.record(
                        "LIGHTNING", reason=f"GLM flash within {self.cfg.GLM_TRIGGER_KM:g} km",
                        source="glm", result=f"latch {self.cfg.GLM_COOLOFF_HOURS:g}h",
                        nearest=f"{res.get('nearest_km')}km {res.get('nearest_bearing')}")
        self._log_changes(res, was_clock, clock_now, was_failing)
        return res

    def _log_changes(self, res, was_clock, clock_now, was_failing):
        """Changes only, never every poll (the event log is on the SD card): the clock
        found wrong by the bucket's Date and right again (log + CLOCK event, as the radar
        does — before, a GLM refusal left no trace in either log), and polls starting to
        fail or working again (log only)."""
        if clock_now is False and was_clock is not False:
            skew = res.get("clock_skew_s")
            log.error("WRONG SYSTEM CLOCK? %s", res.get("error"))
            self.log.record("CLOCK", reason=servertime.clock_phrase(skew, "the GOES bucket's"),
                            result="GLM granules refused")
        elif clock_now is True and was_clock is False:
            log.warning("the system clock agrees with the GOES bucket again")
            self.log.record("CLOCK", reason="system clock agrees with the GOES bucket again",
                            result="GLM granules accepted")
        if res.get("ok"):
            if was_failing is not None:
                log.info("GLM polls work again (%s granules read)", res.get("granules_scanned"))
        elif res.get("clock_skew_s") is None and was_failing != "poll":
            # (a clock refusal is logged above, as an error; this is any other failure,
            # including a feed outage that follows a refusal)
            log.warning("GLM poll failed: %s — lightning data unavailable until a poll "
                        "succeeds (an active latch stays)", res.get("error"))

    def maybe_poll(self, sun_alt, now=None):
        if now is None:
            now = time.time()
        self._retry_persist(now)
        if not deps_available():
            return None
        if not site_in_fov(*self.cfg.GEOCODE):
            if not self._fov_warned:
                self._fov_warned = True
                log.error("site %s is outside the usable GOES-East field of view — "
                          "GLM lightning layer disabled (would be silently 'clear')",
                          self.cfg.GEOCODE)
                self.log.record("CONFIG", reason="site outside GOES-East FOV",
                                result="GLM layer disabled")
            return None
        if sun_alt is None or sun_alt >= self.cfg.GLM_POLL_SUN_BELOW_DEG:
            return None                        # daytime / unknown sun -> don't poll
        with self._lock:
            elapsed = None if self._last_poll_ts is None else now - self._last_poll_ts
            due = elapsed is None or elapsed < 0 or elapsed >= self.cfg.GLM_POLL_INTERVAL
        if due:
            return self.poll_now(now)
        return None

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """Re-arm a latch the clock step would silently evaporate; force a re-poll
        (the S3 prefixes are date-derived, so pre-step data came from the wrong day)."""
        with self._lock:
            lu = self._latch_until
            if (lu is not None and math.isfinite(lu)
                    and lu > pre_now and lu <= post_now):
                self._latch_until = post_now + self.cfg.GLM_COOLOFF_HOURS * 3600
                self._latch_dirty = True
                log.warning("GLM latch re-armed across a clock step")
            self._last_poll_ts = None

    def component(self, sun_alt, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            # self-heal a latch stretched by a backward clock step at check time
            max_until = now + self.cfg.GLM_COOLOFF_HOURS * 3600
            if (self._latch_until is not None and math.isfinite(self._latch_until)
                    and self._latch_until > max_until + 60):
                log.warning("GLM latch expiry beyond one full cool-off (clock step?) — "
                            "clamping")
                self._latch_until = max_until
                self._latch_dirty = True
            latched = self._latch_until is not None and (
                not math.isfinite(self._latch_until) or now < self._latch_until)
            remaining = (int(self._latch_until - now)
                         if latched and math.isfinite(self._latch_until) else 0)
            res = self._last_result or {}
            polling_active = (deps_available() and sun_alt is not None
                              and sun_alt < self.cfg.GLM_POLL_SUN_BELOW_DEG)
            # observation fields are only "current" while the last poll is recent —
            # last night's nearest-flash must not be presented as today's data
            fresh = (self._last_poll_ts is not None
                     and (now - self._last_poll_ts) <= self.cfg.GLM_STALE_AFTER_SEC)
            # the wrong-clock guard refused the last poll: say so (the monitor shows it)
            clock_refused = (fresh and not res.get("ok")
                             and res.get("clock_skew_s") is not None)
            # The nearest flash belongs to the last GOOD poll. After a failed, refused or
            # granule-less poll there is no current distance: exporting the previous one
            # would present an old measurement as current (the weather log, which has no
            # GLM-availability column, recorded it as if it had just been measured).
            current = fresh and bool(res.get("ok"))
            return {
                "safe": not latched, "enabled": deps_available(),
                "error": None if res.get("ok") else res.get("error"),
                "warnings": [str(res.get("error"))] if clock_refused else [],
                "available": deps_available() and bool(res.get("ok")) and fresh,
                "latched": latched, "latched_until": self._latch_until,
                "seconds_remaining": remaining, "last_flash_ts": self._last_flash_ts,
                "in_ring": bool(res.get("in_ring")) and fresh,
                "nearest_km": self._nearest_km if current else None,
                "nearest_bearing": self._nearest_bearing if current else None,
                "polling_active": polling_active, "last_poll_ts": self._last_poll_ts,
                "granules_scanned": res.get("granules_scanned", 0),
                "trigger_km": self.cfg.GLM_TRIGGER_KM,
                "cooloff_hours": self.cfg.GLM_COOLOFF_HOURS,
                "source": "GOES-19 GLM total lightning (AWS S3, in-RAM)",
            }


def unavailable_component(cfg):
    """Component dict when GLM is disabled/absent (does not affect IsSafe)."""
    return {
        "safe": True, "enabled": False, "available": False, "latched": False,
        "error": None, "warnings": [],
        "latched_until": None, "seconds_remaining": 0, "last_flash_ts": None,
        "in_ring": False, "nearest_km": None, "nearest_bearing": None,
        "polling_active": False, "last_poll_ts": None, "granules_scanned": 0,
        "trigger_km": cfg.GLM_TRIGGER_KM, "cooloff_hours": cfg.GLM_COOLOFF_HOURS,
        "source": "GOES-19 GLM total lightning (disabled)",
    }
