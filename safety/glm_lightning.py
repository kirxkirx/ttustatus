"""GOES-19 GLM lightning component for the SafetyMonitor.

Polls GLM-L2-LCFA granules from AWS Open Data (anonymous S3, no key), parses them ENTIRELY
IN RAM (no SD-card writes), and LATCHES unsafe for GLM_COOLOFF_HOURS whenever any flash is
within GLM_TRIGGER_KM of the observatory. Polls only when the sun is below the night gate
(same idea as WU rain). Mirrors RainPoller's discipline: the slow S3/netCDF work happens
OUTSIDE the lock, so SafetyMonitor.evaluate() and the HTTP handlers (which only read the
cached latch via component()) never block on a poll.

Fail-safe: a fetch error — or missing numpy/netCDF4 — never clears an active latch and never
forces unsafe on its own; an active latch keeps IsSafe false regardless. GLM flashes are
already geodetic lat/lon, so no projection is needed.
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
def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "ttu-safety-glm/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _list_keys(bucket, prefix):
    url = f"https://{bucket}.s3.amazonaws.com/?list-type=2&max-keys=1000&prefix={prefix}"
    xml = _get(url).decode("utf-8", "replace")
    return re.findall(r"<Key>([^<]+)</Key>", xml)


def _key_start(key):
    m = re.search(r"_s(\d{14})", key)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1)[:13], "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _flashes_from_buffer(buf):
    """(lats, lons) from a GLM granule, parsed IN RAM (in-memory; /dev/shm RAM fallback)."""
    try:
        ds = netCDF4.Dataset("inmem.nc", mode="r", memory=buf)
        try:
            return (np.asarray(ds.variables["flash_lat"][:]),
                    np.asarray(ds.variables["flash_lon"][:]))
        finally:
            ds.close()
    except (ValueError, OSError, TypeError):
        # Fallback ONLY to /dev/shm (a RAM tmpfs). NEVER write granules to the SD card:
        # if there's no RAM-backed tmpfs, skip this granule rather than touch disk.
        if not os.path.isdir("/dev/shm"):
            raise RuntimeError("no in-memory netCDF and no /dev/shm; "
                               "skipping granule (refusing to write to disk)")
        fd, path = tempfile.mkstemp(dir="/dev/shm", suffix=".nc")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(buf)
            ds = netCDF4.Dataset(path, "r")
            try:
                return (np.asarray(ds.variables["flash_lat"][:]),
                        np.asarray(ds.variables["flash_lon"][:]))
            finally:
                ds.close()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


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
    keys = []
    for dt in (now, now - timedelta(hours=1)):     # cover a window crossing the hour
        try:
            keys += _list_keys(cfg.GLM_BUCKET, f"{PREFIX}/{dt:%Y}/{dt:%j}/{dt:%H}/")
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": f"list: {e}"}
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
    brng = _cardinal(_bearing(lat0, lon0, *nearest_ll)) if nearest_ll else None
    return {"ok": True, "error": None, "granules_scanned": scanned,
            "in_ring": within > 0, "flashes_in_ring": within,
            "nearest_km": round(nearest_km, 1) if nearest_km is not None else None,
            "nearest_bearing": brng}


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
        if self._latch_until and self._latch_until > time.time():
            log.info("restored active GLM lightning latch (%d min remaining)",
                     int((self._latch_until - time.time()) / 60))

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
        res = poll_flashes(self.cfg)          # SLOW, outside the lock
        with self._lock:
            self._last_poll_ts = now
            self._last_result = res
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
        return res

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
            due = (self._last_poll_ts is None
                   or (now - self._last_poll_ts) >= self.cfg.GLM_POLL_INTERVAL)
        if due:
            return self.poll_now(now)
        return None

    def component(self, sun_alt, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            latched = self._latch_until is not None and (
                not math.isfinite(self._latch_until) or now < self._latch_until)
            remaining = (int(self._latch_until - now)
                         if latched and math.isfinite(self._latch_until) else 0)
            res = self._last_result or {}
            polling_active = (deps_available() and sun_alt is not None
                              and sun_alt < self.cfg.GLM_POLL_SUN_BELOW_DEG)
            return {
                "safe": not latched, "enabled": deps_available(),
                "available": deps_available() and bool(res.get("ok")),
                "latched": latched, "latched_until": self._latch_until,
                "seconds_remaining": remaining, "last_flash_ts": self._last_flash_ts,
                "in_ring": bool(res.get("in_ring")),
                "nearest_km": self._nearest_km, "nearest_bearing": self._nearest_bearing,
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
        "latched_until": None, "seconds_remaining": 0, "last_flash_ts": None,
        "in_ring": False, "nearest_km": None, "nearest_bearing": None,
        "polling_active": False, "last_poll_ts": None, "granules_scanned": 0,
        "trigger_km": cfg.GLM_TRIGGER_KM, "cooloff_hours": cfg.GLM_COOLOFF_HOURS,
        "source": "GOES-19 GLM total lightning (disabled)",
    }
