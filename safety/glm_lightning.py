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

Each poll carries on where the previous one stopped: it returns a cursor (the start of the
newest granule up to which every granule was read) and the next one reads from a minute
before it, so a granule that failed, that the poll's deadline did not get to, or that is
missing from the listing while it may still be published, is read by the next poll.
Time no poll read is never passed off as read: a poll whose granules leave a hole or a
lag in its window is flagged partial (_coverage); what a poll gives up for good — the
oldest granules of a full-length catch-up its time ran out on, the minutes before a
catch-up limit — it reports (skipped_s), and the dew monitor's watch over lightning data
starts again after it. Ideas ported from vlbaweather's vlba/glm.py; replies, granules and
the whole poll are capped in size and time.

Wrong-clock guard: the bucket keeps years of granules and the listing names the hour from
THIS host's clock, so a Pi booted with a wrong clock would read another day's lightning as
current. A listing whose Date header disagrees with the clock by more than
SERVER_CLOCK_TOL_SEC is refused (servertime.py), and the component carries a warning.
Once refused, the clock has to agree to within the tolerance less
servertime.HYSTERESIS_SEC before granules are accepted again (no flicker at the edge),
and both changes are logged with a CLOCK event.
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
# ends 10-95 s before the listing. Measured again 2026-11-14 (12 h of noaa-goes19: 2160
# granules, starts exactly 20 s apart, none missing): published 5-77 s after their end
# (half within 10 s), and 14 granules after a newer one — by 60 s at most.
GRANULE_S = 20
# A poll reaches back WINDOW_OVERLAP_S before the cursor (the start of the newest granule
# up to which the previous polls read everything): a granule published late, after a
# newer one, is read then. A flash read twice only renews the latch.
WINDOW_OVERLAP_S = 60
# ...but never further back than MAX_CATCHUP_S (or the configured window, if longer): after
# a long pause (daytime, an outage) the next poll reads that much, not the whole pause.
# The time before it was never read: the poll reports it (skipped_s, see poll_flashes).
MAX_CATCHUP_S = 900
# Completeness of a poll, from the granules it READ: more than MAX_GAP_S of unread time
# between two of them (or between where the poll had to carry on from and the first) is
# a hole, and a newest granule that ends more than MAX_LAG_S before the poll is a lag.
# Either makes the poll partial: a flash in what was read still counts, but "no flash"
# does not vouch for the whole window.
MAX_GAP_S = 120
MAX_LAG_S = 180
# A granule missing from a listing that already holds newer ones may still be published
# (they reach S3 out of order, see above). While it ended at most MAX_LAG_S before the
# poll — the lateness a poll accepts of the feed as a whole, more than twice the slowest
# publication measured — the cursor stays close enough before it that the next poll
# reads it (_cursor_after). One that ended longer ago is taken to be missing for good:
# waiting for it would only make every later poll read the same granules again.
# Caps on what a reply or a granule can cost. Real granules are 0.17-0.6 MB and hold a
# few hundred flashes; a listing of one hour (180 keys) is about 70 kB.
MAX_LIST_BYTES = 2 * 1024 * 1024
MAX_GRANULE_BYTES = 8 * 1024 * 1024
MAX_FLASHES_PER_GRANULE = 100000
# Granules read per poll at most: one per GRANULE_S of the window plus this margin (a
# listing may hold up to 1000 keys an hour; duplicates of one start are read once).
GRANULE_MARGIN = 10
# One request's socket timeout and whole-body deadline, and the whole poll's deadline
# (listings included). Granules are read newest first. The oldest ones a poll did not get
# to by its deadline are read by the next poll (the cursor stays before them), unless its
# window already reached back MAX_CATCHUP_S, or the cap above left them out: reading
# again cannot catch up then, and they are given up (poll_flashes).
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
    on stderr — although the open itself then works. That was every granule (15 a poll
    then), some 3600 journal lines an hour at night (seen in the Raspberry Pi OS test, CI's
    raspios.yml). A real file has nothing to probe in vain. Its name starts "ttu-glm-": the
    daemon's start-up sweep removes a file that a killed poll left behind.

    Raises ValueError for a granule whose flash variables are not two 1-D arrays of one
    length, or that declares more than MAX_FLASHES_PER_GRANULE flashes (checked before
    anything is read)."""
    path = None
    if os.path.isdir("/dev/shm"):
        try:
            fd, path = tempfile.mkstemp(dir="/dev/shm", prefix="ttu-glm-", suffix=".nc")
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
                return _read_flash_vars(ds)
            finally:
                ds.close()
        finally:
            _unlink(path)
    ds = netCDF4.Dataset("inmem.nc", mode="r", memory=buf)
    try:
        return _read_flash_vars(ds)
    finally:
        ds.close()


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
    """What the granules READ (their start times, epoch s) leave out of [lo, end], in
    plain words: [] when it is complete. ``lo`` is where the poll had to carry on from:
    the end of the granule the cursor marks (the minute of overlap before it was read by
    an earlier poll), else the window's start. A hole is more than MAX_GAP_S of unread
    time between two granules or before the first; a lag is a newest granule that ends
    more than MAX_LAG_S before ``end``."""
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


def _near(x, starts):
    """Whether one of ``starts`` (epoch s) is the granule that starts at ``x``: start times
    are compared to within half a granule (they are rounded to 0.1 s, and a slot of a gap
    is computed from its neighbours)."""
    return any(abs(x - s) < GRANULE_S / 2.0 for s in starts)


def _cursor_after(selected, read, lo, since, end=None, given_up_to=None, pending=()):
    """``(cursor, pending)`` for the next poll.

    The cursor: the start of the newest granule up to which every listed granule
    (``selected``: ascending start times) was read, without a hole. A granule that failed,
    or that the deadline did not get to, stops it, so the next poll reads that granule
    again; so does a hole, so a granule published late is read then. Only what this poll
    had to read counts: a granule up to the previous cursor (``since``; the minute of
    overlap) was read by an earlier poll, and failing to read it again holds nothing up
    — unless it is one the previous poll left ``pending`` (below).

    ``given_up_to``: the oldest granule this poll read, when it gave up the older ones it
    did not get to (only when reading them again could not catch up, see poll_flashes).
    The cursor walks on from there.

    A gap in the listing whose youngest missing granule ended at most MAX_LAG_S before
    ``end`` (the poll's time) may still be filled: the cursor stays at most
    WINDOW_OVERLAP_S past the gap, so the next poll's window reaches it, and the gap's
    slots that the cursor went past are returned as ``pending``. A granule listed there
    by the next poll has never been read: that poll reads it first and has to read it
    — failing that, the cursor stays — although it lies in its minute of overlap, which
    otherwise was read before. (Before, a gap of up to MAX_GAP_S was passed: a granule
    published more than WINDOW_OVERLAP_S after a newer one was read by no poll; and one
    published within the minute was lost when its one download in the overlap failed,
    with nothing said either time.) A slot stays pending while it is within
    WINDOW_OVERLAP_S of the cursor; a granule not listed by the time the cursor moves on
    is taken to be missing for good, as GLM drops one now and then.

    The cursor never moves back (``since``, the previous one). With neither, the next
    poll has to carry on from ``lo``: the cursor is then the start of the 20 s before it
    (the next poll's coverage starts at the end of the cursor's granule, so a granule at
    ``lo`` that failed is read again then, and not taken as read)."""
    # from here on every granule has to have been read (failures, holes stop the cursor)
    must_from = lo if since is None else max(lo, since + GRANULE_S)
    scan_from = lo
    if given_up_to is not None and given_up_to > must_from:
        must_from = scan_from = given_up_to
    pending = [p for p in pending if p >= scan_from - 1e-6]     # (the rest is lost)
    newest = None
    covered_to = scan_from
    wait_from = None
    gaps = []
    for s in selected:
        if s < scan_from:
            continue                        # given up (this poll's deadline)
        if (wait_from is None and end is not None and s - covered_to >= GRANULE_S - 0.05
                and end - s <= MAX_LAG_S):
            wait_from = covered_to          # a granule missing before s may still come
        must = s >= must_from - GRANULE_S / 2.0 or _near(s, pending)
        if must and (s not in read or s - covered_to > MAX_GAP_S):
            break
        if wait_from is not None and s > wait_from + WINDOW_OVERLAP_S:
            break
        if wait_from is not None:
            t = covered_to                  # the slots of a gap the cursor goes past
            while s - t >= GRANULE_S / 2.0:
                gaps.append(t)
                t += GRANULE_S
        newest = s
        covered_to = max(covered_to, s + GRANULE_S)
    marks = [x for x in (newest, since) if x is not None]
    cursor = max(marks) if marks else lo - GRANULE_S
    left = []
    for p in sorted(pending + gaps):
        if (p >= cursor - WINDOW_OVERLAP_S - GRANULE_S / 2.0 and not _near(p, read)
                and not _near(p, left)):
            left.append(round(p, 1))
    return cursor, left


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


def poll_flashes(cfg, since=None, now=None, pending=()):
    """Read the granules not read yet and summarise them (SLOW; call outside locks).

    ``since``: the cursor the previous poll returned (epoch s; None for the first poll
    after a start or a clock step, which reads the last GLM_WINDOW_MIN). The poll reads
    from WINDOW_OVERLAP_S before the cursor — or the configured window, if that reaches
    further — but at most MAX_CATCHUP_S (or the window) back. ``pending``: the slots of a
    gap the previous poll's cursor went past (it returned them as ``pending``): a granule
    listed there now was never read, and is read first (_cursor_after). ``now``: an aware
    datetime for the tests (default: this host's clock).

    The result is ``ok`` once one granule was read. ``partial`` (with ``coverage``, the
    reason in words) when what was read leaves a hole or a lag in the window (_coverage):
    a flash in what was read counts all the same, but "no flash" is then not a clear
    window. ``cursor`` and ``pending`` are for the next poll (_cursor_after).
    ``granules_in_window`` counts the granules read that start within the last
    GLM_WINDOW_MIN (the dew monitor's measure of a poll; ``granules_scanned`` counts all
    read). ``skipped_s``: the time no poll read that this one passes for good, in whole
    granules (0 when there is none) — the minutes before a window the catch-up limit cut
    short, the oldest granules given up, a pending granule that was never read.
    ``first_error``: the first granule that could not be read, and why (for the log;
    None when every granule tried was read)."""
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
    # (a poll whose window reaches back this far cannot catch up by reading again: see
    # given_up_to below)
    limit_lo = end - max(float(MAX_CATCHUP_S), window_s)
    lo = max(lo, limit_lo)
    # (pending slots belong to the cursor they came with)
    pending = ([] if since is None else
               [p for p in (_finite_ts(x) for x in (pending or ())) if p is not None])
    # Where this poll has to carry on from: the end of the granule the cursor marks
    # (earlier polls read everything before it; the minute of overlap is read again only
    # for a granule published late) — or the window's start, without a cursor or when
    # the catch-up limit cut the window short of it. Completeness is judged from here:
    # judged from the window's start, the overlap counted as unread whenever a slow link
    # did not get to it again, although an earlier poll had read it.
    prev_end = None if since is None else since + GRANULE_S
    base = lo if prev_end is None else max(lo, prev_end)
    minutes = int(round((end - lo) / 60.0))
    tol = _clock_tolerance(cfg)
    keys = []
    # The bucket's clock as measured on a listing that was ACCEPTED (None: no Date
    # header). Returned as "server_skew_s" so the poller learns that the clock agrees
    # again after a refusal; a refusal itself returns "clock_skew_s" instead.
    agreed = None
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
        if skew is not None:
            agreed = skew
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
    # What to read, in order: first a granule listed in a slot the previous poll left
    # pending (published late, behind newer ones: no poll has read it, and as the oldest
    # granule of the window a slow link would get to it last), then the newest first —
    # they matter most — at most `cap` of them; the cap leaves the oldest out.
    cap = int((end - lo) / GRANULE_S) + GRANULE_MARGIN
    late = [x for x in starts if _near(x, pending)]
    order = late + [x for x in reversed(starts[-cap:]) if x not in late]

    lat0, lon0 = cfg.GEOCODE
    nearest_km = None
    nearest_ll = None
    within = 0
    dropped = 0
    read = set()
    tried = set()
    failed = 0
    # A request the deadline cut off (its time budget was what was left) is as unreached
    # as one never started: counted only as failed, it left a full-length catch-up that
    # read all but its oldest granule with nothing to give up, and the cursor at the
    # morning's — the same catch-up again on every poll.
    cut_off = []
    first_error = None
    for x in order:
        left = deadline_at - _mono()
        if left < MIN_REQUEST_S:
            first_error = first_error or "poll deadline reached"
            break                           # the rest is not reached
        tried.add(x)
        key = by_start[x]
        try:
            lats, lons = _flashes_from_buffer(
                _get(f"https://{cfg.GLM_BUCKET}.s3.amazonaws.com/{key}",
                     min(REQUEST_TIMEOUT_S, left)))
            n, near, ll, bad = _granule_summary(lat0, lon0, lats, lons, cfg.GLM_TRIGGER_KM)
        except Exception as e:  # noqa: BLE001 - one bad granule never costs the poll
            # unread: the cursor stops before it, so the next poll reads it again (unless
            # this poll gives up what is older than the oldest granule it read, see below)
            failed += 1
            if deadline_at - _mono() < MIN_REQUEST_S:
                cut_off.append(x)
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
    unreached = [x for x in starts if x not in tried]
    if len(order) < len(starts):
        first_error = first_error or "more granules listed than a poll reads (%d)" % cap
    # GLM writes a granule every 20 s, flashes or not: a window without one read is a
    # feed outage (or a clock off by most of the window), never "no lightning". The
    # cause goes with the result: the poller logs it once, when polls start failing (a
    # warning on every poll was a dozen lines an hour all night).
    if not read:
        error = ("no GLM granule in the last %d min" % minutes if not starts else
                 "none of the %d GLM granules of the last %d min could be read"
                 % (len(starts), minutes))
        log.debug("GLM poll: no granule read (%s)", first_error)
        return {"ok": False, "granules_scanned": 0, "granules_in_window": 0,
                "server_skew_s": agreed, "error": error, "first_error": first_error}
    # Cut short, a poll leaves its oldest new granules unread (it reads the newest first).
    # Normally the cursor stays before them, and the next poll reads them: one stalled
    # poll costs nothing then (before, it gave them up for good, and a flash in them never
    # reached the latch). But when what is left to read already reaches back as far as a
    # poll may (MAX_CATCHUP_S: the cursor's minute of overlap, or without a cursor the
    # window, lies at or before that limit), or the cap on granules per poll left some
    # out, reading again cannot catch up — the next window would be as long, and cut
    # short the same way: on a slow link no poll was complete again all night. Then the
    # poll gives up what is older than the oldest new granule it read (the ones it did not
    # get to, and a request the deadline cut off), and the cursor walks on from there, so
    # the next window is back to its normal size (_cursor_after). (Judged by the window
    # alone, a configured GLM_WINDOW_MIN of 15 or more reached the limit on every poll,
    # and one stalled download gave granules up that the next window would have read.)
    new_read = [x for x in read if x >= base - GRANULE_S / 2.0]
    cut_new = [x for x in unreached + cut_off if x >= base - GRANULE_S / 2.0]
    backlog_from = lo if since is None else since - WINDOW_OVERLAP_S
    cannot_catch_up = (backlog_from <= limit_lo + 0.001
                       or any(x not in order for x in cut_new))
    given_up_to = (min(new_read) if cut_new and new_read and cannot_catch_up
                   and min(new_read) > base else None)
    problems = _coverage(read, base, end)
    if cut_new and not problems:
        # A poll cut short is partial even when what it left is less than a hole: the
        # granules were published, and its "no flash" does not cover them (counted as a
        # good poll, it let the dew monitor vouch for them until a later poll read them
        # or gave them up). A hole or a lag is reported instead when there is one.
        problems.append("%d new granule(s) %s: the poll's %.0f s ran out" % (
            len(cut_new), "given up" if given_up_to is not None
            else "left for the next poll", POLL_DEADLINE_S))
    # Time no poll read that the cursor now leaves behind for good: the minutes before a
    # window the catch-up limit cut short of the cursor, what this poll gave up, and a
    # pending granule (listed late) that was never read before it was given up or fell
    # out of the window. ALL of it is reported, in whole granules (a fraction of one
    # rounded to 0 hid a granule the catch-up limit had just cut off; exactly 0 in a poll
    # that gave nothing up), so that a watch over the lightning data — the dew monitor's
    # 30 minutes — starts again instead of running on across it. Unlike a hole in the
    # listing (a granule GLM never published, tolerated up to MAX_GAP_S by _coverage),
    # this is data that exists and was not read: on a link too slow to read every
    # granule, the dew monitor stays false rather than vouch for lightning data nobody
    # looked at. (After the daytime pause this is the whole day: harmless, that watch
    # restarts anyway.)
    # (the slots from prev_end that start before the cut: within a microsecond is float
    # noise, anything more is a granule the window or the give-up left out)
    cut = given_up_to if given_up_to is not None else base
    skipped = 0
    if prev_end is not None and cut - 1e-6 > prev_end:
        skipped = GRANULE_S * int(math.ceil((cut - 1e-6 - prev_end) / GRANULE_S))
    # (a pending slot lies before the cursor: given up with the rest, or out of the
    # window — any amount before its start, as the window's own filter has it)
    lost = [p for p in pending if not _near(p, read)
            and (given_up_to is not None or p < lo - 1e-6)]
    skipped += GRANULE_S * len(lost)
    if failed or unreached or skipped:
        log.debug("GLM poll: %d of %d listed granules not read (%s); %s; %d s left "
                  "behind unread", failed + len(unreached), len(starts), first_error,
                  "the oldest given up" if given_up_to is not None else
                  "read again next poll", skipped)
    cursor, still_pending = _cursor_after(starts, read, lo, since, end, given_up_to, pending)
    brng = _cardinal(_bearing(lat0, lon0, *nearest_ll)) if nearest_ll else None
    return {"ok": True, "error": None, "granules_scanned": len(read),
            "granules_in_window": sum(1 for x in read if x >= end - window_s),
            "granules_listed": len(starts), "granules_unread": failed + len(unreached),
            "in_ring": within > 0, "flashes_in_ring": within, "flashes_dropped": dropped,
            "nearest_km": round(nearest_km, 1) if nearest_km is not None else None,
            "nearest_bearing": brng, "server_skew_s": agreed,
            "partial": bool(problems), "coverage": "; ".join(problems) or None,
            "skipped_s": skipped, "first_error": first_error,
            "window_start": lo, "window_end": end,
            "cursor": cursor, "pending": still_pending}


def _failure_kind(res):
    """None for a poll that worked; else what failed, for the log: "clock" (the
    wrong-clock guard refused the listing), "list" (the listing itself), "none listed"
    (no granule in the window), "unreadable" (granules listed, none could be read) or
    "other"."""
    if res.get("ok"):
        return None
    if res.get("clock_skew_s") is not None:
        return "clock"
    error = str(res.get("error") or "")
    if error.startswith("list:"):
        return "list"
    if error.startswith("no GLM granule"):
        return "none listed"
    if error.startswith("none of the"):
        return "unreadable"
    return "other"


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
        # the slots of a gap the cursor went past that may still be filled (granules
        # published late): the next poll has to read what is listed there (poll_flashes)
        self._pending = ()
        # The wrong-clock guard's verdict from the bucket's Date headers: None (nothing
        # known yet), True (the last listing agreed), False (the last listing was
        # refused). It sets the next poll's limit (servertime.limit: hysteresis) and its
        # changes go to the log and the event log, as the radar's do.
        self._clock_ok = None
        # How the last poll failed (_failure_kind): None (it did not), "clock" (the
        # wrong-clock guard refused it), or the kind of any other failure. A failed poll
        # used to leave no trace in the log at all; now each change between these is
        # logged once — kept apart so a feed outage right after a clock refusal is still
        # reported, and so is the cause that lasts when a run of failures starts with
        # another one (a listing that failed after a boot, then granules that cannot be
        # read all night).
        self._failing = None
        # Whether the last poll that worked was partial: logged when that starts and
        # ends, not on every poll (a slow link made it two lines a poll all night)
        self._partial = False
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
            since, pending = self._cursor, self._pending
            clock_ok = self._clock_ok
        cfg = _WithLimit(self.cfg, servertime.limit(_clock_tolerance(self.cfg), clock_ok))
        # SLOW, outside the lock. (The first poll is called without a cursor at all, and
        # a poll without pending slots without those.)
        if since is None:
            res = poll_flashes(cfg)
        elif pending:
            res = poll_flashes(cfg, since=since, pending=pending)
        else:
            res = poll_flashes(cfg, since=since)
        if not isinstance(res, dict):
            res = {"ok": False, "error": "poll returned %s" % type(res).__name__}
        with self._lock:
            self._last_poll_ts = now
            self._last_result = res
            was_clock, was_failing = self._clock_ok, self._failing
            was_partial = self._partial
            if not res.get("ok") and res.get("clock_skew_s") is not None:
                self._clock_ok = False
            elif res.get("server_skew_s") is not None:
                self._clock_ok = True
            self._failing = _failure_kind(res)
            clock_now = self._clock_ok
            if res.get("ok"):
                self._partial = bool(res.get("partial"))
                cursor = _finite_ts(res.get("cursor"))
                if cursor is not None:
                    self._cursor = cursor
                    left = res.get("pending")
                    self._pending = tuple(
                        p for p in (_finite_ts(x) for x in
                                    (left if isinstance(left, (list, tuple)) else ()))
                        if p is not None)
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
        self._log_changes(res, was_clock, clock_now, was_failing, was_partial)
        return res

    def _log_changes(self, res, was_clock, clock_now, was_failing, was_partial=False):
        """Changes only, never every poll (the event log is on the SD card; the journal
        should not fill with one line per poll either): the clock found wrong by the
        bucket's Date and right again (log + CLOCK event, as the radar does — before, a
        GLM refusal left no trace in either log), polls starting to fail — with the first
        granule's error, if that was it — failing for another reason from then on, or
        working again, and polls starting to leave part of their window unread or reading
        all of it again (log only)."""
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
            if res.get("partial") and not was_partial:
                log.info("GLM poll partial: %s — a flash in what was read counts, but no "
                         "flash there is no proof of none (logged again when a poll reads "
                         "its whole window)", res.get("coverage") or "incomplete")
            elif was_partial and not res.get("partial"):
                log.info("GLM polls read their whole window again")
        elif res.get("clock_skew_s") is None and was_failing != _failure_kind(res):
            # (a clock refusal is logged above, as an error; this is any other failure,
            # including a feed outage that follows a refusal, and a failure whose kind
            # changed: its cause would never reach the log otherwise)
            first = res.get("first_error")
            log.warning("GLM poll failed: %s%s — lightning data unavailable until a poll "
                        "succeeds (an active latch stays)", res.get("error"),
                        " (first error: %s)" % first if first else "")

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
            self._pending = ()

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
            # the last poll read what it could, but left a hole or a lag in its window: a
            # flash in it has counted; "no flash" is not a clear window
            partial = current and bool(res.get("partial"))
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
                # time the last poll left behind unread for good (poll_flashes): the dew
                # monitor's lightning watch does not run on across it
                "skipped_s": (_finite_ts(res.get("skipped_s")) or 0) if current else 0,
                "available": deps_available() and bool(res.get("ok")) and fresh,
                "latched": latched, "latched_until": self._latch_until,
                "seconds_remaining": remaining, "last_flash_ts": self._last_flash_ts,
                "in_ring": bool(res.get("in_ring")) and fresh,
                "nearest_km": self._nearest_km if current else None,
                "nearest_bearing": self._nearest_bearing if current else None,
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
        "granules_in_window": 0, "partial": False, "coverage": None, "skipped_s": 0,
        "trigger_km": cfg.GLM_TRIGGER_KM, "cooloff_hours": cfg.GLM_COOLOFF_HOURS,
        "source": "GOES-19 GLM total lightning (disabled)",
    }
