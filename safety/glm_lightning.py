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

No time is left unread between polls: each poll returns a cursor (the start of the newest
granule up to which every granule was read) and the next one reads from a minute before
it, so a granule that failed, or was published late, is read by the next poll. A poll
whose granules leave a hole or a lag in its window is flagged partial (_coverage). Ideas
ported from vlbaweather's vlba/glm.py; replies, granules and the whole poll are capped in
size and time.

Wrong-clock guard: the bucket keeps years of granules and the listing names the hour from
THIS host's clock, so a Pi booted with a wrong clock would read another day's lightning as
current. A listing whose Date header disagrees with the clock by more than
SERVER_CLOCK_TOL_SEC is refused (servertime.py), and the component carries a warning.
"""
from __future__ import annotations

import http.client
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

from . import netio, servertime

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


# ----- what one poll reads ---------------------------------------------------
# GLM writes a granule every GRANULE_S seconds, flashes or not. Measured 2026-11-12 (in
# vlbaweather, on the same buckets): a single granule is missing two or three times in a
# thousand, and a granule is published 5-75 s after its end, so the newest one listed
# ends 10-95 s before the listing.
GRANULE_S = 20
# A poll reaches back WINDOW_OVERLAP_S before the cursor (the start of the newest granule
# up to which the previous polls read everything): a granule published late, after a
# newer one, is read then. A flash read twice only renews the latch.
WINDOW_OVERLAP_S = 60
# ...but never further back than MAX_CATCHUP_S (or the configured window, if longer): after
# a long pause (daytime, an outage) the next poll reads that much, not the whole pause.
MAX_CATCHUP_S = 900
# Completeness of a poll, from the granules it READ: more than MAX_GAP_S of unread time
# between two of them (or between the window's start and the first) is a hole, and a
# newest granule that ends more than MAX_LAG_S before the poll is a lag. Either makes the
# poll partial: a flash in what was read still counts, but "no flash" does not vouch for
# the whole window.
MAX_GAP_S = 120
MAX_LAG_S = 180
# Caps on what a reply or a granule can cost. Real granules are 0.17-0.6 MB and hold a
# few hundred flashes; a listing of one hour (180 keys) is about 70 kB.
MAX_LIST_BYTES = 2 * 1024 * 1024
MAX_GRANULE_BYTES = 8 * 1024 * 1024
MAX_FLASHES_PER_GRANULE = 100000
# Granules read per poll at most: one per GRANULE_S of the window plus this margin (a
# listing may hold up to 1000 keys an hour; duplicates of one start are read once).
GRANULE_MARGIN = 10
# One request's socket timeout and whole-body deadline, and the whole poll's deadline
# (listings included): what is not read by then counts as unread, like a failed granule.
REQUEST_TIMEOUT_S = 15.0
POLL_DEADLINE_S = 75.0
MIN_REQUEST_S = 1.0                 # s left below which no request is started
# The poll deadline's clock (monotonic: a wall-clock step neither cuts a poll short nor
# lets it run on). A name of its own for the tests to replace.
_mono = time.monotonic


# ----- low-level fetch/parse (all in RAM) -----------------------------------
def _fetch(url, timeout=15):
    """GET -> (body, response headers, this host's clock when the headers arrived).

    The body is read through netio.read_capped: at most MAX_LIST_BYTES for a bucket
    listing and MAX_GRANULE_BYTES for anything else (a granule), and within ``timeout``
    seconds for the whole body (``timeout`` also bounds each wait on the socket)."""
    cap = MAX_LIST_BYTES if "list-type=" in url else MAX_GRANULE_BYTES
    req = urllib.request.Request(url, headers={"User-Agent": "ttu-safety-glm/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        received = time.time()
        body = netio.read_capped(r, cap, timeout, what="GLM " + url.rsplit("/", 1)[-1][:80])
        return body, r.headers, received


def _get(url, timeout=15):
    return _fetch(url, timeout)[0]


def _list_keys(bucket, prefix, timeout=15):
    """(the keys under ``prefix``, the S3 server's clock minus this host's in s — None
    without a Date header)."""
    url = f"https://{bucket}.s3.amazonaws.com/?list-type=2&max-keys=1000&prefix={prefix}"
    reply = _fetch(url, timeout)
    body, headers = reply[0], reply[1]
    return (re.findall(r"<Key>([^<]+)</Key>", body.decode("utf-8", "replace")),
            servertime.skew_s(headers, reply[2] if len(reply) > 2 else None))


def _clock_tolerance(cfg) -> float:
    try:
        v = float(getattr(cfg, "SERVER_CLOCK_TOL_SEC", 60))
    except (TypeError, ValueError):
        v = 60.0
    return v if v > 0 and math.isfinite(v) else 0.0


def _key_start(key):
    """A granule key's start time (its ``_s`` field: YYYYDDDHHMMSS plus tenths), or None."""
    m = re.search(r"_s(\d{14})", key) if isinstance(key, str) else None
    if not m:
        return None
    try:
        t = datetime.strptime(m.group(1)[:13], "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return t + timedelta(seconds=int(m.group(1)[13]) / 10.0)


def _window_s(cfg) -> float:
    """The configured look-back (GLM_WINDOW_MIN) in seconds, held to 1-60 min (only the
    current and the previous hour are listed)."""
    try:
        minutes = int(cfg.GLM_WINDOW_MIN)
    except (TypeError, ValueError, AttributeError):
        minutes = 5
    return 60.0 * max(1, min(60, minutes))


def _as_float_1d(name, var):
    """A flash coordinate variable read as float, masked values as NaN — after its shape
    was checked WITHOUT reading it: 1-D, at most MAX_FLASHES_PER_GRANULE values (a
    granule of a few kB may declare millions)."""
    shape = tuple(getattr(var, "shape", ()) or ())
    if len(shape) != 1:
        raise ValueError("%s has shape %s, not 1-D" % (name, shape))
    if shape[0] > MAX_FLASHES_PER_GRANULE:
        raise ValueError("%s declares %d flashes (more than %d)"
                         % (name, shape[0], MAX_FLASHES_PER_GRANULE))
    return np.ma.asarray(var[:]).astype(np.float64).filled(np.nan)


def _read_flash_vars(ds):
    vlat, vlon = ds.variables["flash_lat"], ds.variables["flash_lon"]
    if tuple(getattr(vlat, "shape", ())) != tuple(getattr(vlon, "shape", ())):
        raise ValueError("flash_lat and flash_lon differ in shape: %s / %s"
                         % (getattr(vlat, "shape", None), getattr(vlon, "shape", None)))
    return _as_float_1d("flash_lat", vlat), _as_float_1d("flash_lon", vlon)


def _flashes_from_buffer(buf):
    """(lats, lons) from a GLM granule, parsed IN RAM (in-memory; /dev/shm RAM fallback).

    Raises ValueError for a granule whose flash variables are not two 1-D arrays of one
    length, or that declares more than MAX_FLASHES_PER_GRANULE flashes (checked before
    anything is read). The fallback is taken only when the in-memory open itself fails."""
    try:
        ds = netCDF4.Dataset("inmem.nc", mode="r", memory=buf)
    except (ValueError, OSError, TypeError):
        ds = None
    if ds is not None:
        try:
            return _read_flash_vars(ds)
        finally:
            ds.close()
    # Fallback ONLY to /dev/shm (a RAM tmpfs). NEVER write granules to the SD card: if
    # there's no RAM-backed tmpfs, skip this granule rather than touch disk. The prefix
    # lets the daemon's start-up sweep remove a file a killed poll left behind.
    if not os.path.isdir("/dev/shm"):
        raise RuntimeError("no in-memory netCDF and no /dev/shm; "
                           "skipping granule (refusing to write to disk)")
    fd, path = tempfile.mkstemp(dir="/dev/shm", prefix="ttu-glm-", suffix=".nc")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(buf)
        ds = netCDF4.Dataset(path, "r")
        try:
            return _read_flash_vars(ds)
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
    # rounding can put a just past 1 (antipodal points), where arcsin gives NaN
    return 2 * r * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _bearing(lat, lon, lat2, lon2):
    p1, p2 = math.radians(lat), math.radians(lat2)
    dl = math.radians(lon2 - lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _cardinal(d):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((d % 360) / 45 + 0.5) % 8]


def _granule_summary(lat0, lon0, lats, lons, trigger_km):
    """One granule's flashes against the site: (flashes within trigger_km, nearest
    distance in km or None, its (lat, lon) or None, flashes dropped).

    Raises ValueError for coordinates that are not two 1-D arrays of one length (or are
    more than MAX_FLASHES_PER_GRANULE): the granule then counts as unread. A flash whose
    latitude or longitude is not a finite number within range is dropped and counted —
    np.argmin would otherwise return the first NaN and hide the real nearest flash."""
    n_lat, n_lon = getattr(lats, "size", None), getattr(lons, "size", None)
    if n_lat == 0 and n_lon == 0:
        return 0, None, None, 0             # no flash: no numpy needed
    if np is None:
        raise RuntimeError("numpy not installed")
    la = np.asarray(lats, dtype=np.float64)
    lo = np.asarray(lons, dtype=np.float64)
    if la.ndim != 1 or lo.ndim != 1 or la.shape != lo.shape:
        raise ValueError("flash_lat / flash_lon shapes %s / %s" % (la.shape, lo.shape))
    if la.size > MAX_FLASHES_PER_GRANULE:
        raise ValueError("%d flashes (more than %d)" % (la.size, MAX_FLASHES_PER_GRANULE))
    good = (np.isfinite(la) & np.isfinite(lo) & (np.abs(la) <= 90.0)
            & (np.abs(lo) <= 180.0))
    dropped = int(la.size - np.count_nonzero(good))
    la, lo = la[good], lo[good]
    if la.size == 0:
        return 0, None, None, dropped
    d = _hav_km(lat0, lon0, la, lo)
    i = int(np.argmin(d))
    return (int(np.count_nonzero(d <= trigger_km)), float(d[i]),
            (float(la[i]), float(lo[i])), dropped)


def _span_words(seconds):
    """``95`` -> ``"95 s"``, ``1500`` -> ``"25 min"``."""
    return "%.0f s" % seconds if seconds < 90 else "%.0f min" % (seconds / 60.0)


def _coverage(read_starts, lo, end):
    """What the granules READ (their start times, epoch s) leave out of the window
    [lo, end], in plain words: [] when the window is complete. A hole is more than
    MAX_GAP_S of unread time between two granules or before the first; a lag is a newest
    granule that ends more than MAX_LAG_S before ``end``."""
    ts = sorted(set(read_starts))
    if not ts:
        return ["no granule read"]
    holes = []
    covered_to = lo
    for s in ts:
        if s - covered_to > MAX_GAP_S:
            holes.append((covered_to, s))
        covered_to = max(covered_to, s + GRANULE_S)
    problems = []
    if holes:
        problems.append("%s of the window not read (from %s UTC)" % (
            _span_words(sum(b - a for a, b in holes)),
            datetime.fromtimestamp(holes[0][0], tz=timezone.utc).strftime("%H:%M")))
    lag = end - (ts[-1] + GRANULE_S)
    if lag > MAX_LAG_S:
        problems.append("newest granule read ends %s before the poll" % _span_words(lag))
    return problems


def _cursor_after(selected, read, lo, since):
    """The new cursor: the start of the newest granule up to which every selected granule
    (ascending start times) was read, from ``lo`` on, without a hole. A granule that
    failed (or was not reached) stops it, so the next poll reads that granule again; so
    does a hole, so a granule published late is read then. The cursor never moves back
    (``since``, the previous one); with neither, ``lo``: the next poll starts there."""
    newest = None
    covered_to = lo
    for s in selected:
        if s not in read or s - covered_to > MAX_GAP_S:
            break
        newest = s
        covered_to = max(covered_to, s + GRANULE_S)
    marks = [x for x in (newest, since) if x is not None]
    return max(marks) if marks else lo


def _hour_prefixes(lo, end):
    """The S3 hour prefixes from the hour of ``lo`` to that of ``end`` (epoch s), newest
    first (two at most: lo is at most an hour before end)."""
    def hour(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(minute=0, second=0,
                                                                   microsecond=0)
    h, first = hour(end), hour(lo)
    out = []
    while h >= first and len(out) < 2:
        out.append(f"{PREFIX}/{h:%Y}/{h:%j}/{h:%H}/")
        h -= timedelta(hours=1)
    return out


def _finite_ts(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def poll_flashes(cfg, since=None, now=None):
    """Read the granules not read yet and summarise them (SLOW; call outside locks).

    ``since``: the cursor the previous poll returned (epoch s; None for the first poll
    after a start or a clock step, which reads the last GLM_WINDOW_MIN). The poll reads
    from WINDOW_OVERLAP_S before the cursor — or the configured window, if that reaches
    further — but at most MAX_CATCHUP_S (or the window) back. ``now``: an aware datetime
    for the tests (default: this host's clock).

    The result is ``ok`` once one granule was read. ``partial`` (with ``coverage``, the
    reason in words) when what was read leaves a hole or a lag in the window (_coverage):
    a flash in what was read counts all the same, but "no flash" is then not a clear
    window. ``cursor`` is for the next poll (_cursor_after). ``granules_in_window`` counts
    the granules read that start within the last GLM_WINDOW_MIN (the dew monitor's
    measure of a poll; ``granules_scanned`` counts all read)."""
    if not deps_available():
        return {"ok": False, "error": "numpy/netCDF4 not installed"}
    deadline_at = _mono() + POLL_DEADLINE_S
    now = now if now is not None else datetime.now(timezone.utc)
    end = now.timestamp()
    window_s = _window_s(cfg)
    lo = end - window_s
    since = _finite_ts(since)
    if since is not None and since > end:
        since = None                        # a cursor from the future: the clock went back
    if since is not None:
        lo = min(lo, since - WINDOW_OVERLAP_S)
    lo = max(lo, end - max(float(MAX_CATCHUP_S), window_s))
    minutes = int(round((end - lo) / 60.0))
    tol = _clock_tolerance(cfg)
    keys = []
    for prefix in _hour_prefixes(lo, end):
        left = deadline_at - _mono()
        if left < MIN_REQUEST_S:
            return {"ok": False, "error": "list: poll deadline reached"}
        try:
            got, skew = _list_keys(cfg.GLM_BUCKET, prefix, min(REQUEST_TIMEOUT_S, left))
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            return {"ok": False, "error": f"list: {e}"}
        if tol and skew is not None and abs(skew) > tol:
            # the hours listed are the wrong clock's: another day's granules
            return {"ok": False, "clock_skew_s": skew,
                    "error": "%s (AWS S3): GLM granules refused until the clock is corrected"
                             % servertime.clock_phrase(skew)}
        keys += got
    # one key per start time (a reprocessed copy sorts after the first by its _c field);
    # a key without a readable start time cannot be placed in the window: dropped
    by_start = {}
    for k in keys:
        t = _key_start(k)
        if t is None:
            continue
        x = round(t.timestamp(), 1)
        if lo <= x <= end and (x not in by_start or k > by_start[x]):
            by_start[x] = k
    starts = sorted(by_start)
    cap = int((end - lo) / GRANULE_S) + GRANULE_MARGIN
    selected = starts[-cap:]                # the newest; the older ones count as unread

    lat0, lon0 = cfg.GEOCODE
    nearest_km = None
    nearest_ll = None
    within = 0
    dropped = 0
    read = set()
    failed = 0
    unreached = len(starts) - len(selected)
    first_error = None
    for x in reversed(selected):            # newest first: they matter most
        left = deadline_at - _mono()
        if left < MIN_REQUEST_S:
            unreached += 1
            continue
        key = by_start[x]
        try:
            lats, lons = _flashes_from_buffer(
                _get(f"https://{cfg.GLM_BUCKET}.s3.amazonaws.com/{key}",
                     min(REQUEST_TIMEOUT_S, left)))
            n, near, ll, bad = _granule_summary(lat0, lon0, lats, lons, cfg.GLM_TRIGGER_KM)
        except Exception as e:  # noqa: BLE001 - one bad granule never costs the poll
            # unread: the cursor stops before it, so the next poll reads it again
            failed += 1
            first_error = first_error or "%s: %s" % (key.rsplit("/", 1)[-1], e)
            log.debug("GLM granule %s unreadable: %s", key, e)
            continue
        read.add(x)
        within += n
        if bad:
            dropped += bad
            log.warning("GLM granule %s: %d flash(es) without a valid lat/lon dropped",
                        key.rsplit("/", 1)[-1], bad)
        if near is not None and (nearest_km is None or near < nearest_km):
            nearest_km, nearest_ll = near, ll
    if unreached:
        first_error = first_error or "poll deadline reached"
    # GLM writes a granule every 20 s, flashes or not: a window without one read is a
    # feed outage (or a clock off by most of the window), never "no lightning"
    if not read:
        out = {"ok": False, "granules_scanned": 0, "granules_in_window": 0, "error": (
            "no GLM granule in the last %d min" % minutes if not starts else
            "none of the %d GLM granules of the last %d min could be read"
            % (len(starts), minutes))}
        if starts and first_error:
            log.warning("GLM poll: no granule read (%s)", first_error)
        return out
    problems = _coverage(read, lo, end)
    if failed or unreached:
        log.info("GLM poll: %d of %d granules unread (%s); read again next poll",
                 failed + unreached, len(starts), first_error)
    if problems:
        log.info("GLM poll partial: %s", "; ".join(problems))
    brng = _cardinal(_bearing(lat0, lon0, *nearest_ll)) if nearest_ll else None
    return {"ok": True, "error": None, "granules_scanned": len(read),
            "granules_in_window": sum(1 for x in read if x >= end - window_s),
            "granules_listed": len(starts), "granules_unread": failed + unreached,
            "in_ring": within > 0, "flashes_in_ring": within, "flashes_dropped": dropped,
            "nearest_km": round(nearest_km, 1) if nearest_km is not None else None,
            "nearest_bearing": brng,
            "partial": bool(problems), "coverage": "; ".join(problems) or None,
            "window_start": lo, "window_end": end,
            "cursor": _cursor_after(starts, read, lo, since)}


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
        # the start (epoch s) of the newest granule up to which every granule was read;
        # None after a start or a clock step: the next poll reads the configured window
        self._cursor = None
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
            since = self._cursor
        # SLOW, outside the lock. (The first poll is called without a cursor at all.)
        res = (poll_flashes(self.cfg) if since is None
               else poll_flashes(self.cfg, since=since))
        with self._lock:
            self._last_poll_ts = now
            self._last_result = res
            if res.get("ok"):
                cursor = _finite_ts(res.get("cursor"))
                if cursor is not None:
                    self._cursor = cursor
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
            self._cursor = None             # the granules it marks were the wrong day's

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
            # the last poll read what it could, but left a hole or a lag in its window: a
            # flash in it has counted; "no flash" is not a clear window
            partial = fresh and bool(res.get("ok")) and bool(res.get("partial"))
            warnings = [str(res.get("error"))] if clock_refused else []
            if partial:
                warnings.append("GLM lightning: the last poll did not read its whole window "
                                "(%s); no flash there is not proof of none"
                                % (res.get("coverage") or "incomplete"))
            return {
                "safe": not latched, "enabled": deps_available(),
                "error": None if res.get("ok") else res.get("error"),
                "warnings": warnings, "partial": partial,
                "coverage": res.get("coverage") if partial else None,
                "available": deps_available() and bool(res.get("ok")) and fresh,
                "latched": latched, "latched_until": self._latch_until,
                "seconds_remaining": remaining, "last_flash_ts": self._last_flash_ts,
                "in_ring": bool(res.get("in_ring")) and fresh,
                "nearest_km": self._nearest_km if fresh else None,
                "nearest_bearing": self._nearest_bearing if fresh else None,
                "polling_active": polling_active, "last_poll_ts": self._last_poll_ts,
                "granules_scanned": res.get("granules_scanned", 0),
                # read in the last GLM_WINDOW_MIN: what the dew monitor judges a poll by
                "granules_in_window": res.get("granules_in_window",
                                              res.get("granules_scanned", 0)),
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
        "granules_in_window": 0, "partial": False, "coverage": None,
        "trigger_km": cfg.GLM_TRIGGER_KM, "cooloff_hours": cfg.GLM_COOLOFF_HOURS,
        "source": "GOES-19 GLM total lightning (disabled)",
    }
