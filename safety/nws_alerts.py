"""NWS active alerts for the SafetyMonitor: the configured veto events (by default Tornado,
Dust Storm and High Wind Warnings) make the monitor UNSAFE while they are in effect OVER
THE SITE; every other alert on the radar map is INFORMATION ONLY and never touches IsSafe.

Three queries, all api.weather.gov (free, no key; NWS asks for a User-Agent; gzip, capped):

  * POINT  /alerts/active?point=<lat>,<lon>   every HAZARD_POINT_POLL_SEC (60 s). NWS's own
    answer to "what is in effect HERE": polygon warnings by their polygon, zone products by
    the zone that contains the point. A small, fast request: the veto's primary path.
  * AREA   /alerts/active?area=<states>       every HAZARD_AREA_POLL_SEC (120 s). Every alert
    of the states on the radar map (auto: the states whose box touches it), for the page's
    lists and the map overlay, AND an independent local test of the site: a polygon alert
    covers the site when its CAP polygon contains it (polygon ONLY: a polygon warning lists
    every county it merely touches, and Lubbock County is far larger than a storm), a zone
    alert when one of the site's own zones (/points: forecast zone, county, fire zone) is
    listed. The two sources are UNIONED for the veto (fail-safe: either one suffices).
  * CIVIL  /alerts/active?event=<CIVIL_EVENTS>  with every area poll: the civil emergency
    messages that local authorities send straight through IPAWS carry only SAME county
    codes and never appear in ?area=; kept when they touch the map (see CIVIL_EVENTS).

Three facts about the feed drive the design (all checked on the live API, 2026-09-24):

  * One VTEC event (office.phenomena.significance.ETN) is often split over several CAP
    messages at once, one per segment (58 of 328 keys in the national feed), each with its
    own zones and end time; the active feed already drops superseded messages. So an alert
    is tested per MESSAGE, and a key covers the site when any of its live messages does.
  * "Cancel" is not the only way an event ends: NWS also sends EXP (expired) segments as
    messageType Update and UPG ("has been replaced") segments as messageType Alert, the
    latter with ``expires`` in the past but ``ends`` in the future. The VTEC action decides.
  * Zone outlines come at full resolution (up to 12,000 vertices / 1.2 MB per zone). They
    are fetched once per zone and cached forever in HAZARD_CACHE_DIR: simplified (~100 m,
    below one map pixel) for zones near the map, bounding box only for far ones, so neither
    the SD card nor the Pi's RAM pays for a statewide Heat Advisory.

VETO LATCH. A veto is held until the alert's end (``ends``, else ``expires``), persisted to
HAZARD_LATCH_FILE (written only when the veto set changes) and restored at start-up. It is
released EARLY only when a fresh point query AND a fresh area query both succeed after the
alert's last sighting and neither lists it as covering the site (cancelled, expired, or its
updated polygon moved off the site). Feed outage => held until its end, never beyond; no end
time => held 60 min from the last sighting. A warning issued for a LATER period (a High Wind
Warning "from 10 AM Friday") vetoes from its onset (minus a short lead), not from issuance:
"for the duration of the warning", and the observatory must not close the night before.

With no veto held the component is "unavailable" when both queries are stale, which does
NOT veto on its own (like the forecast and radar layers; the connectivity watchdog covers a
total loss of internet).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:                      # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None

log = logging.getLogger("ttu.safety.hazards")

BASE = "https://api.weather.gov"
SOURCE = "NWS api.weather.gov active alerts"
HTTP_TIMEOUT = 20                     # s per request

# Contract defaults (config.py owns the env parsing; these apply to a cfg without them).
DEFAULT_VETO_EVENTS = "Tornado Warning,Dust Storm Warning,High Wind Warning"
DEFAULT_POINT_POLL_SEC = 60
DEFAULT_AREA_POLL_SEC = 120
DEFAULT_STALE_AFTER_SEC = 600

# A veto with no end time in the alert is held this long after its last sighting.
NO_END_HOLD_SEC = 3600
# Upper bound on how long ONE veto can be held from its first sighting when the feeds say
# nothing more (feed outage, wrong clock). Long-fuse warnings (High Wind) run up to ~2 days
# from issuance; a still-active alert is simply re-sighted by the next fresh query.
MAX_VETO_SPAN_SEC = 72 * 3600
# A warning issued ahead of its onset vetoes from (onset - lead): time to park and close.
ONSET_LEAD_SEC = 900
# Zone-outline fetching per area poll: the POINT query is the veto's fast path and shares
# this thread, so a first-run backlog (hundreds of zones in a statewide event, ~0.4-2 s
# each) is spread over several polls instead of stalling it. Cached zones cost nothing.
ZONE_FETCH_BUDGET_SEC = 15.0
ZONE_FETCH_MAX = 40
ZONE_RETRY_SEC = 600                  # a failed zone fetch is retried after this (monotonic)
POINTS_RETRY_SEC = 600                # /points failure retry
POINTS_REFRESH_SEC = 86400            # re-check the site's zones daily (write only on change)
NEAR_MARGIN_DEG = 0.5                 # zones within the map box + this keep their outline
SIMPLIFY_DEG = 0.001                  # ~100 m: below one pixel of the 1-degree map
AREA_DESC_MAX = 300
DESCRIPTION_MAX = 2000
INSTRUCTION_MAX = 1000
CLOCK_TOLERANCE_SEC = 60              # a result/stamp further "in the future" = clock stepped
# Every response body is capped (compressed and inflated): a zone outline is <= ~1.2 MB and
# the TX/NM/OK area answer 0.05-1.5 MB, so 16 MiB never cuts a real answer — and a runaway
# one cannot take the daemon that serves IsSafe out of memory.
MAX_BODY_BYTES = 16 * 1024 * 1024
# A FAILED latch write (read-only or full SD card) is retried at most this often, from the
# poll thread only, and logged when the error changes or every PERSIST_FAIL_LOG_SEC.
PERSIST_RETRY_SEC = 60
PERSIST_FAIL_LOG_SEC = 600
# A veto WITHOUT an end time is held NO_END_HOLD_SEC from its last sighting, so the latch
# file carries that sighting — rewritten at most once per this while it is re-sighted.
NO_END_SEEN_QUANTUM_SEC = 600

# Civil emergency messages. NWS relays some (with UGC zones: they come with the area query),
# but those that local authorities send straight through IPAWS carry NO zones and NO UGC,
# only SAME county codes, and api.weather.gov's ?area= filter never returns them (checked
# 2026-09-24: Ruidoso NM's three Local Area Emergencies and a Los Angeles Civil Emergency
# Message were absent from ?area=NM / ?area=CA). A small national query of these types
# (usually empty: ~200 bytes) on the area cadence brings them in; those touching the map
# (polygon) or naming a county of the map's states (SAME) are kept.
CIVIL_EVENTS = ("Civil Emergency Message", "Evacuation Immediate", "Shelter In Place Warning",
                "Local Area Emergency", "Civil Danger Warning", "Law Enforcement Warning",
                "Hazardous Materials Warning", "Fire Warning", "Nuclear Power Plant Warning",
                "Radiological Hazard Warning", "Earthquake Warning", "911 Telephone Outage")
_CIVIL_LC = frozenset(e.lower() for e in CIVIL_EVENTS)
# SAME location code PSSCCC: SS = state FIPS -> USPS code; the county zone is <ST>C<CCC>
# (048303 = TXC303, Lubbock County).
FIPS_STATES = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO", "09": "CT",
    "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI", "16": "ID", "17": "IL",
    "18": "IN", "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME", "24": "MD",
    "25": "MA", "26": "MI", "27": "MN", "28": "MS", "29": "MO", "30": "MT", "31": "NE",
    "32": "NV", "33": "NH", "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA", "54": "WV",
    "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR", "78": "VI",
}
# Only these reach the API: Actual alerts and their updates (a Cancel means over, and is
# dropped here anyway) — fewer bytes on every poll.
_QUERY_FILTER = "status=actual&message_type=alert,update"


# ---- config access (the config builder owns the names; defaults per the contract) ------
def _cfg(cfg, name, default):
    v = getattr(cfg, name, None)
    return default if v is None else v


def veto_event_names(cfg) -> List[str]:
    """The configured veto events (HAZARD_VETO_EVENTS: a comma list or a sequence), with
    whitespace normalised and duplicates dropped; matching is case-insensitive."""
    raw = _cfg(cfg, "HAZARD_VETO_EVENTS", DEFAULT_VETO_EVENTS)
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (set, frozenset)):
        items = sorted(raw)
    else:
        items = list(raw)
    out, seen = [], set()
    for it in items:
        name = " ".join(str(it).split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def _norm_event(name) -> str:
    return " ".join(str(name or "").split()).lower()


def _veto_set(cfg) -> frozenset:
    return frozenset(n.lower() for n in veto_event_names(cfg))


def _enabled(cfg) -> bool:
    v = getattr(cfg, "HAZARDS_ENABLED", True)
    if isinstance(v, str):
        return v.strip().lower() not in ("0", "false", "no", "")
    return bool(v)


# ---- small helpers ----------------------------------------------------------------------
def _finite(x) -> Optional[float]:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def _parse_ts(s) -> Optional[float]:
    """ISO-8601 (as the API writes it) -> epoch seconds, None when absent or malformed."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _finite(dt.timestamp())


def _text(v) -> str:
    return v if isinstance(v, str) else ""


def _text_or_none(v) -> Optional[str]:
    return v if isinstance(v, str) and v.strip() else None


def _clip(s: Optional[str], n: int) -> Optional[str]:
    if s is None or len(s) <= n:
        return s
    return s[:n - 1].rstrip() + "…"


def _coord(v: float) -> str:
    """The API accepts at most 4 decimals in ?point= (more redirects)."""
    s = "%.4f" % float(v)
    return s.rstrip("0").rstrip(".")


_tz_cache: Dict[str, object] = {}
_tz_warned = False


def _zone_info(tzname):
    global _tz_warned
    if tzname in _tz_cache:
        return _tz_cache[tzname]
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            if not _tz_warned:
                _tz_warned = True
                log.warning("TTU_SAFETY_LOCAL_TZ=%r is not a valid timezone — alert times "
                            "shown in UTC", tzname)
    _tz_cache[tzname] = tz
    return tz


def fmt_local(ts, tzname, now=None) -> Optional[str]:
    """'18:45 CDT' today, 'Fri 08:00 CDT' on another day (local to the site)."""
    ts = _finite(ts)
    if ts is None:
        return None
    tz = _zone_info(tzname) or timezone.utc
    try:
        loc = datetime.fromtimestamp(ts, timezone.utc).astimezone(tz)
        ref = datetime.fromtimestamp(time.time() if now is None else now,
                                     timezone.utc).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return None
    label = loc.strftime("%Z") or "UTC"
    if loc.date() == ref.date():
        return loc.strftime("%H:%M ") + label
    return loc.strftime("%a %H:%M ") + label


# ---- geometry (pure Python; GeoJSON order is lon, lat; boxes lonmin, latmin, lonmax, latmax)
Box = Tuple[float, float, float, float]


def iter_polygons(geometry) -> Iterator[list]:
    """Each polygon of a GeoJSON geometry as a list of rings (outer first): Polygon,
    MultiPolygon and GeometryCollection (zone outlines use all three); others yield nothing."""
    if not isinstance(geometry, dict):
        return
    t = geometry.get("type")
    if t == "Polygon":
        rings = geometry.get("coordinates") or []
        if rings and rings[0]:
            yield rings
    elif t == "MultiPolygon":
        for rings in geometry.get("coordinates") or []:
            if rings and rings[0]:
                yield rings
    elif t == "GeometryCollection":
        for g in geometry.get("geometries") or []:
            for rings in iter_polygons(g):
                yield rings


def point_in_ring(lon: float, lat: float, ring) -> bool:
    """Ray casting (even-odd). A point exactly on an edge counts as inside (fail-safe for
    a site on a polygon's border)."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (min(xi, xj) - 1e-12 <= lon <= max(xi, xj) + 1e-12
                and min(yi, yj) - 1e-12 <= lat <= max(yi, yj) + 1e-12):
            if abs((xj - xi) * (lat - yi) - (yj - yi) * (lon - xi)) < 1e-12:
                return True
        if (yi > lat) != (yj > lat):
            if lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def point_in_polygon(lon: float, lat: float, rings) -> bool:
    """Inside the outer ring and outside every hole."""
    if not rings or not point_in_ring(lon, lat, rings[0]):
        return False
    return not any(point_in_ring(lon, lat, hole) for hole in rings[1:])


def point_in_geometry(lon: float, lat: float, geometry) -> bool:
    return any(point_in_polygon(lon, lat, rings) for rings in iter_polygons(geometry))


def ring_bbox(ring) -> Optional[Box]:
    if not ring:
        return None
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def geometry_bbox(geometry) -> Optional[Box]:
    box = None
    for rings in iter_polygons(geometry):
        b = ring_bbox(rings[0])
        if b is not None:
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                         max(box[2], b[2]), max(box[3], b[3]))
    return box


def bbox_intersects(a: Optional[Sequence[float]], b: Optional[Sequence[float]]) -> bool:
    if a is None or b is None:
        return False
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def touches_box(geometry, box: Box) -> bool:
    """Any member polygon's box intersects ``box`` (per polygon, not the merged box: a watch
    made of distant zones must not count as 'on the map' because its overall box does)."""
    return any(bbox_intersects(ring_bbox(rings[0]), box) for rings in iter_polygons(geometry))


def merge_geometries(geoms) -> Optional[dict]:
    """Several geometries -> one MultiPolygon (no dissolve; fine for fills and point tests).
    Rings are shared by reference: cached zone outlines are never mutated."""
    polys = [rings for g in geoms for rings in iter_polygons(g)]
    if not polys:
        return None
    return {"type": "MultiPolygon", "coordinates": polys}


def _polygon_geometry(g) -> Optional[dict]:
    """Keep a geometry only if it has polygons (a Point or empty collection = no shape)."""
    return g if isinstance(g, dict) and geometry_bbox(g) is not None else None


def _simplify_ring(ring, tol: float) -> list:
    """Radial-distance thinning, then Douglas-Peucker (iterative: a 12,000-vertex zone
    must not hit the recursion limit). Coordinates rounded to 5 decimals (~1 m)."""
    pts = [(float(p[0]), float(p[1])) for p in ring]
    if len(pts) <= 4:
        return [[round(x, 5), round(y, 5)] for x, y in pts]
    thin = [pts[0]]
    for p in pts[1:-1]:
        if abs(p[0] - thin[-1][0]) > tol or abs(p[1] - thin[-1][1]) > tol:
            thin.append(p)
    thin.append(pts[-1])
    n = len(thin)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ax, ay = thin[a]
        bx, by = thin[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = thin[i]
            if norm == 0.0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    out = [[round(x, 5), round(y, 5)] for (x, y), k in zip(thin, keep) if k]
    return out


def simplify_geometry(geometry, tol: float = SIMPLIFY_DEG) -> Optional[dict]:
    """Polygon/MultiPolygon/GeometryCollection -> simplified MultiPolygon. Holes and islands
    that collapse below a triangle are dropped; an outer ring that would collapse is kept
    unsimplified (a zone must never vanish from the map)."""
    polys = []
    for rings in iter_polygons(geometry):
        outer = _simplify_ring(rings[0], tol)
        if len(outer) < 4:
            outer = [[round(float(p[0]), 5), round(float(p[1]), 5)] for p in rings[0]]
        holes = [h for h in (_simplify_ring(r, tol) for r in rings[1:]) if len(h) >= 4]
        polys.append([outer] + holes)
    if not polys:
        return None
    return {"type": "MultiPolygon", "coordinates": polys}


def map_box(cfg) -> Box:
    """The radar thumbnail's extent (radar._region): RADAR_THUMB_HALF_DEG around the site."""
    lat0, lon0 = cfg.GEOCODE
    h = float(_cfg(cfg, "RADAR_THUMB_HALF_DEG", 1.0))
    return (lon0 - h, lat0 - h, lon0 + h, lat0 + h)


def _grow(box: Box, m: float) -> Box:
    return (box[0] - m, box[1] - m, box[2] + m, box[3] + m)


# ---- US states (for HAZARD_AREA_STATES=auto) ----------------------------------------------
# Bounding boxes (lonmin, latmin, lonmax, latmax) of the 50 states, DC and the territories,
# ported from the owner's weather/states.py: U.S. Census Bureau 2024 TIGER/Line "States and
# Equivalent Entities" (tl_2024_us_state.zip), every vertex's min/max rounded outward to
# 0.01 deg. Keys are USPS codes, which are also the api.weather.gov area codes. A box
# over-includes near borders; that only adds alerts to the download (each alert is placed
# by its own geometry). Marine areas are left out: marine-only products are not shown.
STATE_BOXES: Dict[str, Tuple[Box, ...]] = {
    "AK": ((-179.24, 51.17, -129.97, 71.44), (172.34, 51.29, 179.86, 53.07)),
    "AL": ((-88.48, 30.14, -84.88, 35.01),), "AR": ((-94.62, 33.00, -89.64, 36.50),),
    "AS": ((-171.15, -14.61, -168.10, -10.99),), "AZ": ((-114.82, 31.33, -109.04, 37.01),),
    "CA": ((-124.49, 32.52, -114.13, 42.01),), "CO": ((-109.07, 36.99, -102.04, 41.01),),
    "CT": ((-73.73, 40.95, -71.78, 42.06),), "DC": ((-77.12, 38.79, -76.90, 39.00),),
    "DE": ((-75.79, 38.45, -74.98, 39.84),), "FL": ((-87.64, 24.39, -79.97, 31.01),),
    "GA": ((-85.61, 30.35, -80.78, 35.01),), "GU": ((144.56, 13.18, 145.01, 13.71),),
    "HI": ((-178.45, 18.86, -154.75, 28.52),), "IA": ((-96.64, 40.37, -90.14, 43.51),),
    "ID": ((-117.25, 41.98, -111.04, 49.01),), "IL": ((-91.52, 36.97, -87.01, 42.51),),
    "IN": ((-88.10, 37.77, -84.78, 41.77),), "KS": ((-102.06, 36.99, -94.58, 40.01),),
    "KY": ((-89.58, 36.49, -81.96, 39.15),), "LA": ((-94.05, 28.85, -88.75, 33.02),),
    "MA": ((-73.51, 41.18, -69.85, 42.89),), "MD": ((-79.49, 37.88, -74.98, 39.73),),
    "ME": ((-71.09, 42.91, -66.88, 47.46),), "MI": ((-90.42, 41.69, -82.12, 48.31),),
    "MN": ((-97.24, 43.49, -89.48, 49.39),), "MO": ((-95.78, 35.99, -89.09, 40.62),),
    "MP": ((144.81, 14.03, 146.16, 20.62),), "MS": ((-91.66, 30.13, -88.09, 35.00),),
    "MT": ((-116.05, 44.35, -104.03, 49.01),), "NC": ((-84.33, 33.75, -75.40, 36.59),),
    "ND": ((-104.05, 45.93, -96.55, 49.01),), "NE": ((-104.06, 39.99, -95.30, 43.01),),
    "NH": ((-72.56, 42.69, -70.57, 45.31),), "NJ": ((-75.57, 38.78, -73.88, 41.36),),
    "NM": ((-109.06, 31.33, -103.00, 37.01),), "NV": ((-120.01, 35.00, -114.03, 42.01),),
    "NY": ((-79.77, 40.47, -71.77, 45.02),), "OH": ((-84.83, 38.40, -80.51, 42.33),),
    "OK": ((-103.01, 33.61, -94.43, 37.01),), "OR": ((-124.71, 41.99, -116.46, 46.30),),
    "PA": ((-80.52, 39.71, -74.68, 42.52),), "PR": ((-68.00, 17.83, -65.16, 18.57),),
    "RI": ((-71.91, 41.09, -71.08, 42.02),), "SC": ((-83.36, 31.99, -78.49, 35.22),),
    "SD": ((-104.06, 42.47, -96.43, 45.95),), "TN": ((-90.32, 34.98, -81.64, 36.68),),
    "TX": ((-106.65, 25.83, -93.50, 36.51),), "UT": ((-114.06, 36.99, -109.04, 42.01),),
    "VA": ((-83.68, 36.54, -75.16, 39.47),), "VI": ((-65.16, 17.62, -64.51, 18.47),),
    "VT": ((-73.44, 42.72, -71.46, 45.02),), "WA": ((-124.85, 45.54, -116.91, 49.01),),
    "WI": ((-92.89, 42.49, -86.24, 47.31),), "WV": ((-82.65, 37.20, -77.71, 40.64),),
    "WY": ((-111.06, 40.99, -104.05, 45.01),),
}
# The map box grows by this before the state test: the 1-degree thumbnail around TTU
# stops 0.04 deg short of New Mexico, whose zone outlines still reach the map's edge area.
AREA_MARGIN_DEG = 0.5
_area_warned: set = set()


def states_touching(box: Sequence[float]) -> List[str]:
    return sorted(code for code, boxes in STATE_BOXES.items()
                  if any(bbox_intersects(box, b) for b in boxes))


def _warn_area_once(key, msg, *args):
    if key not in _area_warned:
        _area_warned.add(key)
        log.warning(msg, *args)


def area_codes(cfg) -> List[str]:
    """HAZARD_AREA_STATES: 'auto' = the states whose box touches the map (+ margin), else
    an explicit comma list of state/territory codes.

    An explicit list is checked against STATE_BOXES: api.weather.gov rejects the WHOLE
    query (HTTP 400) for one unknown code, so a typo ('NW' for 'NM') would silently cost
    the map, the nearby list, the local site test and every early release. Unknown codes
    are dropped (loudly); nothing valid left -> 'auto'. A list naming none of the site's
    candidate states (by bounding box: TTU is inside TX's and OK's) gets them added:
    without the site's state the local test of the site could never succeed."""
    raw = _cfg(cfg, "HAZARD_AREA_STATES", "auto")
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    items = [str(x).strip().upper() for x in items if str(x).strip()]
    auto = states_touching(_grow(map_box(cfg), AREA_MARGIN_DEG))
    if not items or items == ["AUTO"]:
        return auto
    good = [c for c in items if c in STATE_BOXES]
    bad = tuple(c for c in items if c not in STATE_BOXES)
    if bad:
        _warn_area_once(("bad",) + bad, "HAZARD_AREA_STATES: ignoring unknown code(s) %s "
                        "(api.weather.gov would reject the whole area query)", ", ".join(bad))
    if not good:
        _warn_area_once(("auto",) + bad, "HAZARD_AREA_STATES has no valid code — using "
                        "'auto' (%s)", ",".join(auto))
        return auto
    lat, lon = cfg.GEOCODE
    site_states = states_touching((lon, lat, lon, lat))
    missing = [] if any(c in good for c in site_states) else site_states
    if missing:
        _warn_area_once(("site",) + tuple(missing), "HAZARD_AREA_STATES: adding the site's "
                        "own state(s) %s (the local site test needs them)", ", ".join(missing))
    return sorted(set(good) | set(missing))


# ---- colours and classification (ported from the owner's weather/alerts.py) -------------
# Official NWS "Watch, Warning, Advisory Display" colours (weather.gov/help-map, checked row
# by row against the chart on 2026-09-23). Keys are the exact event strings; lookups are
# case-insensitive. The owner's rule is that HAZARDS ARE NEVER DRAWN GREEN (green reads as
# "good" and blends into the 15-35 dBZ radar greens): the chart's green rows are replaced in
# FLOOD_COLORS / NOT_GREEN below, and a test pins that no colour in the table is green.
NWS_COLORS: Dict[str, str] = {
    "Tsunami Warning": "#FD6347", "Tornado Warning": "#FF0000",
    "Extreme Wind Warning": "#FF8C00", "Severe Thunderstorm Warning": "#FFA500",
    "Flash Flood Warning": "#8B0000", "Flash Flood Statement": "#8B0000",
    "Severe Weather Statement": "#00FFFF", "Shelter In Place Warning": "#FA8072",
    "Evacuation Immediate": "#7FFF00", "Civil Danger Warning": "#FFB6C1",
    "Nuclear Power Plant Warning": "#4B0082", "Radiological Hazard Warning": "#4B0082",
    "Hazardous Materials Warning": "#4B0082", "Fire Warning": "#A0522D",
    "Civil Emergency Message": "#FFB6C1", "Law Enforcement Warning": "#C0C0C0",
    "Storm Surge Warning": "#B524F7", "Hurricane Force Wind Warning": "#CD5C5C",
    "Hurricane Warning": "#DC143C", "Typhoon Warning": "#DC143C",
    "Special Marine Warning": "#FFA500", "Blizzard Warning": "#FF4500",
    "Snow Squall Warning": "#C71585", "Ice Storm Warning": "#8B008B",
    "Heavy Freezing Spray Warning": "#00BFFF", "Winter Storm Warning": "#FF69B4",
    "Lake Effect Snow Warning": "#008B8B", "Dust Storm Warning": "#FFE4C4",
    "Blowing Dust Warning": "#FFE4C4", "High Wind Warning": "#DAA520",
    "Tropical Storm Warning": "#B22222", "Storm Warning": "#9400D3",
    "Gale Warning": "#DDA0DD", "Hazardous Seas Warning": "#D8BFD8",
    "Avalanche Warning": "#1E90FF", "Earthquake Warning": "#8B4513",
    "Volcano Warning": "#2F4F4F", "Ashfall Warning": "#A9A9A9",
    "Flood Warning": "#00FF00", "Flood Statement": "#00FF00",
    "Coastal Flood Warning": "#228B22", "Lakeshore Flood Warning": "#228B22",
    "High Surf Warning": "#228B22", "Extreme Heat Warning": "#C71585",
    "Excessive Heat Warning": "#C71585", "Extreme Cold Warning": "#0000FF",
    "Wind Chill Warning": "#B0C4DE", "Hard Freeze Warning": "#9400D3",
    "Freeze Warning": "#483D8B", "Red Flag Warning": "#FF1493",
    "Tornado Watch": "#FFFF00", "Severe Thunderstorm Watch": "#DB7093",
    "Flood Watch": "#2E8B57", "Flash Flood Watch": "#2E8B57",
    "Coastal Flood Watch": "#66CDAA", "Lakeshore Flood Watch": "#66CDAA",
    "Tsunami Watch": "#FF00FF", "Hurricane Watch": "#FF00FF",
    "Hurricane Force Wind Watch": "#9932CC", "Typhoon Watch": "#FF00FF",
    "Tropical Storm Watch": "#F08080", "Storm Watch": "#FFE4B5",
    "Storm Surge Watch": "#DB7FF7", "Gale Watch": "#FFC0CB",
    "Hazardous Seas Watch": "#483D8B", "Heavy Freezing Spray Watch": "#BC8F8F",
    "Winter Storm Watch": "#4682B4", "Blizzard Watch": "#ADFF2F",
    "Lake Effect Snow Watch": "#87CEFA", "Avalanche Watch": "#F4A460",
    "High Wind Watch": "#B8860B", "Excessive Heat Watch": "#800000",
    "Extreme Heat Watch": "#800000", "Extreme Cold Watch": "#5F9EA0",
    "Wind Chill Watch": "#5F9EA0", "Hard Freeze Watch": "#4169E1",
    "Freeze Watch": "#00FFFF", "Fire Weather Watch": "#FFDEAD",
    "Tsunami Advisory": "#D2691E", "Winter Weather Advisory": "#7B68EE",
    "Freezing Rain Advisory": "#7B68EE", "Blowing Snow Advisory": "#7B68EE",
    "Lake Effect Snow Advisory": "#48D1CC", "Wind Chill Advisory": "#AFEEEE",
    "Cold Weather Advisory": "#AFEEEE", "Heat Advisory": "#FF7F50",
    "Flood Advisory": "#00FF7F", "Urban and Small Stream Flood Advisory": "#00FF7F",
    "Small Stream Flood Advisory": "#00FF7F", "Arroyo and Small Stream Flood Advisory": "#00FF7F",
    "Hydrologic Advisory": "#00FF7F", "Coastal Flood Advisory": "#7CFC00",
    "Lakeshore Flood Advisory": "#7CFC00", "High Surf Advisory": "#BA55D3",
    "Dense Fog Advisory": "#708090", "Freezing Fog Advisory": "#008080",
    "Dense Smoke Advisory": "#F0E68C", "Small Craft Advisory": "#D8BFD8",
    "Brisk Wind Advisory": "#D8BFD8", "Freezing Spray Advisory": "#00BFFF",
    "Low Water Advisory": "#A52A2A", "Dust Advisory": "#BDB76B",
    "Blowing Dust Advisory": "#BDB76B", "Wind Advisory": "#D2B48C",
    "Lake Wind Advisory": "#D2B48C", "Frost Advisory": "#6495ED",
    "Ashfall Advisory": "#696969", "Avalanche Advisory": "#CD853F",
    "Air Stagnation Advisory": "#808080",
    "Special Weather Statement": "#FFE4B5", "Marine Weather Statement": "#FFDAB9",
    "Coastal Flood Statement": "#6B8E23", "Lakeshore Flood Statement": "#6B8E23",
    "Rip Current Statement": "#40E0D0", "Beach Hazards Statement": "#40E0D0",
    "Hurricane Local Statement": "#FFE4B5", "Typhoon Local Statement": "#FFE4B5",
    "Tropical Storm Local Statement": "#FFE4B5",
    "Tropical Depression Local Statement": "#FFE4B5",
    "Tropical Cyclone Local Statement": "#FFE4B5", "Air Quality Alert": "#808080",
    "Hazardous Weather Outlook": "#EEE8AA", "Hydrologic Outlook": "#90EE90",
    "Short Term Forecast": "#98FB98", "Extreme Fire Danger": "#E9967A",
    "Local Area Emergency": "#C0C0C0", "911 Telephone Outage": "#C0C0C0",
    "Administrative Message": "#C0C0C0", "Child Abduction Emergency": "#FFFFFF",
    "Blue Alert": "#FFFFFF", "Test": "#F0FFFF",
}
# Inland flood products in reds (the owner's choice, ported): darker = more serious.
FLOOD_COLORS: Dict[str, str] = {
    "Flood Watch": "#E53935", "Flash Flood Watch": "#E53935",
    "Flood Warning": "#C62828", "Flash Flood Warning": "#8B0000",
    "Flash Flood Statement": "#8B0000", "Flood Advisory": "#FA8072",
    "Flood Statement": "#FA8072", "Hydrologic Outlook": "#F4A6A6",
    "Arroyo and Small Stream Flood Advisory": "#FA8072",
    "Urban and Small Stream Flood Advisory": "#FA8072",
    "Small Stream Flood Advisory": "#FA8072", "Hydrologic Advisory": "#FA8072",
}
# The chart's remaining green rows (the port kept coastal NWS colours; the rule here is
# general): coastal/lakeshore flood follow the inland flood reds, the rest get non-green hues.
NOT_GREEN: Dict[str, str] = {
    "Coastal Flood Warning": "#C62828", "Lakeshore Flood Warning": "#C62828",
    "Coastal Flood Watch": "#E53935", "Lakeshore Flood Watch": "#E53935",
    "Coastal Flood Advisory": "#FA8072", "Lakeshore Flood Advisory": "#FA8072",
    "Coastal Flood Statement": "#FA8072", "Lakeshore Flood Statement": "#FA8072",
    "Evacuation Immediate": "#E040FB", "High Surf Warning": "#6A1B9A",
    "Blizzard Watch": "#7EC8E3", "Short Term Forecast": "#D3D3D3",
}
# The flood reds above gave Flood Advisory the chart's salmon (#FA8072), which is also the
# chart's Shelter In Place Warning: a hazmat shelter order must not look like a minor flood
# on the map. The owner's flood colours stay; Shelter In Place Warning gets a plum that no
# other shown product uses (CIELAB distance >= 30 to every other colour in this table).
DISTINCT: Dict[str, str] = {"Shelter In Place Warning": "#804870"}
EVENT_COLORS: Dict[str, str] = dict(NWS_COLORS)
EVENT_COLORS.update(FLOOD_COLORS)
EVENT_COLORS.update(NOT_GREEN)
EVENT_COLORS.update(DISTINCT)
# Fallback per kind for events not in the table (new products appear now and then).
KIND_COLORS = {"warning": "#d00000", "watch": "#e6b800", "advisory": "#7b68ee",
               "statement": "#ffe4b5", "other": "#808080"}
KIND_RANK = {"warning": 0, "watch": 1, "advisory": 2, "statement": 3, "other": 4}
SEVERITY_RANK = {"extreme": 0, "severe": 1, "moderate": 2, "minor": 3, "unknown": 4}
_COLORS_LC = {k.lower(): v for k, v in EVENT_COLORS.items()}
_STATEMENT_SUFFIXES = ("statement", "outlook", "alert", "message", "forecast", "emergency")
# Civil products whose name does not end in their grade. In the EAS/IPAWS scheme a Civil
# Emergency Message and an Evacuation Immediate are WARNING-level (an order to act now), so
# they sort and draw with the warnings — not after the statements, and not first to fall
# off a capped list. A Local Area Emergency and a 911 Telephone Outage are statement-level.
CIVIL_KINDS = {"evacuation immediate": "warning", "civil emergency message": "warning",
               "local area emergency": "statement", "911 telephone outage": "statement"}


def event_kind(event) -> str:
    """warning | watch | advisory | statement | other, by the event name's last word (the
    suffix NWS itself grades by; new products need no table entry), except the civil
    products named in CIVIL_KINDS."""
    name = _norm_event(event)
    if name in CIVIL_KINDS:
        return CIVIL_KINDS[name]
    if name.endswith("warning"):
        return "warning"
    if name.endswith("watch"):
        return "watch"
    if name.endswith("advisory"):
        return "advisory"
    if name.endswith(_STATEMENT_SUFFIXES):
        return "statement"
    return "other"


def color_for(event, kind=None) -> str:
    return _COLORS_LC.get(_norm_event(event)) or KIND_COLORS.get(
        kind or event_kind(event), KIND_COLORS["other"])


def alert_rank(kind: str, severity) -> int:
    """0 = Extreme warning ... 44 = unknown-severity oddity (lower = more serious)."""
    return (KIND_RANK.get(kind, KIND_RANK["other"]) * 10
            + SEVERITY_RANK.get(_norm_event(severity), SEVERITY_RANK["unknown"]))


def _param_values(params, key: str) -> List[str]:
    v = params.get(key) if isinstance(params, dict) else None
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


# Impact-based-warning damage tags -> chip text, most serious first (the tag, not the event
# name, decides: an emergency must never be missed because of how the event is spelled).
_THREATS = (
    ("tornadoDamageThreat", "CATASTROPHIC", "TORNADO EMERGENCY"),
    ("flashFloodDamageThreat", "CATASTROPHIC", "FLASH FLOOD EMERGENCY"),
    ("tornadoDamageThreat", "CONSIDERABLE", "PDS"),
    ("thunderstormDamageThreat", "DESTRUCTIVE", "DESTRUCTIVE"),
    ("flashFloodDamageThreat", "CONSIDERABLE", "CONSIDERABLE FLASH FLOODING"),
    ("thunderstormDamageThreat", "CONSIDERABLE", "CONSIDERABLE DAMAGE"),
)


def alert_threat(params) -> Optional[str]:
    for key, level, label in _THREATS:
        if level in (v.upper() for v in _param_values(params, key)):
            return label
    return None


# ---- what is shown at all -----------------------------------------------------------------
# Not natural hazards to an observatory, or not real messages: never listed (the owner's
# rule: every active Actual alert on the map EXCEPT these). A configured veto event is never
# dropped by these product-type rules.
EXCLUDED_EVENTS = frozenset({
    "child abduction emergency", "blue alert", "test", "test message",
    "administrative message", "required weekly test", "required monthly test",
    "practice/demo warning", "national periodic test", "network message notification",
})
EXCLUDED_SAME = frozenset({"CAE", "BLU", "ADR", "RWT", "RMT", "DMO", "NPT", "NMN", "NIC"})
EXCLUDED_NWS_CODES = frozenset({"TST"})
# Marine-only products (open water), and the NWS marine area prefixes of marine zone ids
# (LMZ740 is Lake Michigan; they are served under /zones/forecast/ like land zones).
MARINE_EVENTS = frozenset({
    "small craft advisory", "small craft advisory for hazardous seas",
    "small craft advisory for rough bar", "small craft advisory for winds",
    "gale warning", "gale watch", "storm warning", "storm watch",
    "hazardous seas warning", "hazardous seas watch", "special marine warning",
    "marine weather statement", "heavy freezing spray warning", "heavy freezing spray watch",
    "freezing spray advisory", "hurricane force wind warning", "hurricane force wind watch",
    "brisk wind advisory", "low water advisory",
})
MARINE_AREAS = frozenset({"AM", "AN", "GM", "LC", "LE", "LH", "LM", "LO", "LS", "PH", "PK",
                          "PM", "PS", "PZ", "SL"})
# VTEC actions after which a segment is no longer in effect, whatever the messageType says
# (EXP arrives as Update, UPG as Alert with expires in the past; checked on the live feed).
TERMINAL_VTEC_ACTIONS = frozenset({"CAN", "EXP", "UPG"})

_VTEC_RE = re.compile(r"([OTEX])\.([A-Z]{3})\.([A-Z0-9]{4})\.([A-Z0-9]{2})\.([A-Z])\.(\d{4})\.")
_ZONE_URL_RE = re.compile(
    r"^https://api\.weather\.gov/zones/(forecast|county|fire|public)/([A-Z]{2}[CZ]\d{3})$")
_ZONE_ID_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")


def _vtec(params) -> List[dict]:
    out = []
    for s in _param_values(params, "VTEC"):
        m = _VTEC_RE.search(s)
        if m:
            cls, action, office, phen, sig, etn = m.groups()
            out.append({"class": cls, "action": action,
                        "key": "%s.%s.%s.%s" % (office, phen, sig, etn)})
    return out


def _same_county(code) -> Optional[str]:
    """SAME location code 'PSSCCC' -> the NWS county zone id ('048303' -> 'TXC303'), or
    None (unknown state, the all-state code CCC=000, malformed)."""
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code.strip()):
        return None
    code = code.strip()
    st = FIPS_STATES.get(code[1:3])
    if st is None or code[3:] == "000":
        return None
    return "%sC%s" % (st, code[3:])


def _zone_refs(props) -> List[Tuple[str, str, str]]:
    """[(type, id, url)] from affectedZones; only api.weather.gov zone URLs are accepted (a
    URL taken from feed data is never fetched blindly). UGC codes are the fallback, then
    SAME county codes (civil messages sent straight through IPAWS carry nothing else)."""
    out = []
    for u in props.get("affectedZones") or []:
        m = _ZONE_URL_RE.match(u.strip()) if isinstance(u, str) else None
        if m:
            ztype = "forecast" if m.group(1) == "public" else m.group(1)
            out.append((ztype, m.group(2), "%s/zones/%s/%s" % (BASE, ztype, m.group(2))))
    geocode = props.get("geocode") if isinstance(props.get("geocode"), dict) else {}
    if not out:
        for ugc in geocode.get("UGC") or []:
            if isinstance(ugc, str) and _ZONE_ID_RE.match(ugc.strip()):
                zid = ugc.strip()
                ztype = "county" if zid[2] == "C" else "forecast"
                out.append((ztype, zid, "%s/zones/%s/%s" % (BASE, ztype, zid)))
    if not out:
        seen = set()
        for same in geocode.get("SAME") or []:
            zid = _same_county(same)
            if zid is not None and zid not in seen:
                seen.add(zid)
                out.append(("county", zid, "%s/zones/county/%s" % (BASE, zid)))
    return out


def normalize(feature) -> dict:
    """One GeoJSON alert feature -> an internal alert record. Pure; never raises on missing
    keys (the feed omits ends, instruction, geometry ... now and then)."""
    feature = feature if isinstance(feature, dict) else {}
    props = feature.get("properties")
    props = props if isinstance(props, dict) else {}
    event = " ".join(_text(props.get("event")).split())
    kind = event_kind(event)
    severity = _text(props.get("severity")) or "Unknown"
    params = props.get("parameters")
    vt = _vtec(params)
    live_v = [v for v in vt if v["action"] not in TERMINAL_VTEC_ACTIONS]
    primary = (live_v or vt or [None])[0]
    cap_id = _text(props.get("id")) or _text(feature.get("id"))
    geometry = _polygon_geometry(feature.get("geometry"))
    headlines = _param_values(params, "NWSheadline")
    ends, expires = _text_or_none(props.get("ends")), _text_or_none(props.get("expires"))
    ec = props.get("eventCode") if isinstance(props.get("eventCode"), dict) else {}
    zones = _zone_refs(props)
    return {
        "id": cap_id,
        "key": primary["key"] if primary else cap_id,
        "event": event, "event_lc": event.lower(), "kind": kind,
        "severity": severity, "urgency": _text(props.get("urgency")) or "Unknown",
        "rank": alert_rank(kind, severity), "color": color_for(event, kind),
        "headline": _text(props.get("headline")),
        "nws_headline": headlines[0] if headlines else None,
        "threat": alert_threat(params),
        "description": _text(props.get("description")),
        "instruction": _text_or_none(props.get("instruction")),
        "area_desc": _text(props.get("areaDesc")),
        "sender": _text(props.get("senderName")) or _text(props.get("sender")),
        "status": _text(props.get("status")), "message_type": _text(props.get("messageType")),
        "vtec_class": primary["class"] if primary else None,
        "vtec_action": primary["action"] if primary else None,
        "sent": _text_or_none(props.get("sent")),
        "onset": _text_or_none(props.get("onset")) or _text_or_none(props.get("effective")),
        "ends": ends, "expires": expires,
        "sent_ts": _parse_ts(props.get("sent")),
        "onset_ts": _parse_ts(props.get("onset")) or _parse_ts(props.get("effective")),
        "end_ts": _parse_ts(ends) or _parse_ts(expires),
        "zones": zones,
        "zone_keys": frozenset((t, z) for t, z, _u in zones),
        "same_codes": frozenset(_param_values(ec, "SAME")),
        "nws_codes": frozenset(_param_values(ec, "NationalWeatherService")),
        "geometry": geometry,
        "geometry_source": "polygon" if geometry is not None else None,
    }


def is_live(rec: dict, now: float) -> bool:
    """Actual Alert/Update messages whose segment is still in effect: a Cancel, a terminal
    VTEC action (CAN/EXP/UPG) or a passed end means the event is over; a VTEC test (T)
    product never counts. No end at all is kept ('no end given' is not 'over')."""
    if rec.get("status", "").lower() != "actual":
        return False
    if rec.get("message_type", "").lower() not in ("alert", "update"):
        return False
    if rec.get("vtec_class") == "T" or rec.get("vtec_action") in TERMINAL_VTEC_ACTIONS:
        return False
    end = rec.get("end_ts")
    return end is None or end > now


def excluded_reason(rec: dict, veto_set) -> Optional[str]:
    """Why a live alert is not shown at all (None = shown). Veto events are never excluded."""
    ev = rec.get("event_lc", "")
    if ev in veto_set:
        return None
    if (ev in EXCLUDED_EVENTS or rec.get("same_codes", frozenset()) & EXCLUDED_SAME
            or rec.get("nws_codes", frozenset()) & EXCLUDED_NWS_CODES):
        return "not a natural hazard / test / administrative"
    zones = rec.get("zones") or []
    if ev in MARINE_EVENTS or (zones and all(z[1][:2] in MARINE_AREAS for z in zones)):
        return "marine-only"
    return None


def _sighting(recs: List[dict]) -> dict:
    """The site's version of one key from one feed: the newest message wins (an Update
    replaces earlier versions); among equally new ones the latest end."""
    def order(r):
        return (r.get("sent_ts") or 0.0,
                r["end_ts"] if r.get("end_ts") is not None else float("inf"))
    r = max(recs, key=order)
    return {"event": r["event"], "headline": r.get("nws_headline") or r.get("headline", ""),
            "sender": r.get("sender", ""), "end_ts": r.get("end_ts"),
            "onset_ts": r.get("onset_ts"), "sent_ts": r.get("sent_ts"),
            "geometry": r.get("geometry")}


def _group_sightings(recs: List[dict]) -> Dict[str, dict]:
    by_key: Dict[str, List[dict]] = {}
    for r in recs:
        by_key.setdefault(r["key"], []).append(r)
    return {k: _sighting(v) for k, v in by_key.items()}


def _feed_skew(data, recs: List[dict], fetch_ts: float) -> float:
    """Lower bound of (server clock - our clock) from timestamps the server wrote (sent,
    the feed's 'updated'): all are <= the server's now. Used ONLY to start an onset-gated
    veto no later than the server says (a Pi booted with a clock behind — the 2026-08
    incident — would otherwise see every onset in the future); never to end anything."""
    cands = [r["sent_ts"] - fetch_ts for r in recs if r.get("sent_ts") is not None]
    upd = _parse_ts(data.get("updated")) if isinstance(data, dict) else None
    if upd is not None:
        cands.append(upd - fetch_ts)
    return max([0.0] + cands)


# ---- network ------------------------------------------------------------------------------
def _gunzip(raw: bytes, max_bytes: int) -> bytes:
    """gzip -> bytes, refusing anything that inflates beyond max_bytes (a gzip bomb)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, max_bytes + 1)
    if len(out) > max_bytes or d.unconsumed_tail:
        raise ValueError("response inflates beyond %d bytes" % max_bytes)
    return out


def _get(url, ua, max_bytes=MAX_BODY_BYTES):
    """GET one api.weather.gov JSON document.

    gzip is requested and inflated here (urllib does not): the area answer shrinks ~7x
    (49 KB -> 8 KB on a quiet day for TX/NM/OK, and 0.5-1.5 MB -> ~10 % on a busy one),
    and it is fetched ~30 times an hour. Every body is CAPPED, compressed and inflated:
    this runs inside the daemon that answers IsSafe, and a runaway answer (a captive
    portal, a proxy streaming garbage) must fail the query — a normal, logged failure —
    never push the whole safety daemon out of memory on the Pi."""
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "application/geo+json",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read(max_bytes + 1)
        enc = (r.headers.get("Content-Encoding") or "").strip().lower()
    if len(raw) > max_bytes:
        raise ValueError("response larger than %d bytes" % max_bytes)
    if enc in ("gzip", "x-gzip"):
        raw = _gunzip(raw, max_bytes)
    elif enc not in ("", "identity"):
        raise ValueError("unexpected Content-Encoding %r" % enc)
    return json.loads(raw.decode("utf-8-sig"))


def _atomic_write_json(path: str, obj) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


_DEFERRED = object()     # a zone outline not cached and not fetched in this poll


# ---- the poller ---------------------------------------------------------------------------
class NwsAlertsPoller:
    """Polls the two NWS queries (maybe_poll, from its own thread) and owns the persistent
    veto latch; component()/overlays() only read cached state (no network), so IsSafe and
    the radar thread never wait on NWS."""

    def __init__(self, cfg, eventlog):
        self.cfg = cfg
        self.log = eventlog
        self._lock = threading.Lock()
        self._point = None              # last SUCCESSFUL point result
        self._area = None               # last SUCCESSFUL area result
        self._point_error = None        # latest attempt's error (None = it succeeded)
        self._area_error = None
        self._point_attempt_ts = None   # scheduling (wall clock, like the other pollers)
        self._area_attempt_ts = None
        self._fail_logged = {}          # which -> monotonic of the last failure log line
        self._civil_error = None        # the national civil-message query (see CIVIL_EVENTS)
        self._veto: Dict[str, dict] = {}
        self._saved_sig = None
        self._latch_dirty = False
        self._persist_retry_mono = 0.0  # no latch write attempt before this (after a failure)
        self._persist_error = None      # the last latch-write error (None = the file is current)
        self._persist_logged_mono = None
        # zone outlines + the site's zones: owned by the polling thread (no lock needed)
        self._zones: Dict[Tuple[str, str], dict] = {}
        self._cache_warned = False
        self._site_geo = None
        self._site_zones = None         # frozenset {(type, id)} or None (unknown)
        self._site_meta = {}
        self._points_next_mono = 0.0
        self._coverage_warned = False
        self._noveto_warned = False
        self._load_latch()
        if not self._latch_dirty:
            self._saved_sig = self._sig()   # the file already says this: no write at start
        else:
            with self._lock:                # clamped / dropped / placeholder: say so on disk
                self._persist()

    # -- config ---------------------------------------------------------------------------
    def _ua(self):
        return getattr(self.cfg, "NWS_USER_AGENT", "ttu-safety-monitor")

    def _tz(self):
        return getattr(self.cfg, "LOCAL_TZ", "America/Chicago")

    def _stale_after(self):
        return float(_cfg(self.cfg, "HAZARD_STALE_AFTER_SEC", DEFAULT_STALE_AFTER_SEC))

    def _onset_lead(self):
        return float(_cfg(self.cfg, "HAZARD_VETO_ONSET_LEAD_SEC", ONSET_LEAD_SEC))

    def _latch_path(self):
        return _cfg(self.cfg, "HAZARD_LATCH_FILE",
                    os.path.expanduser("~/safety_hazard_latch.json"))

    def _cache_dir(self):
        return _cfg(self.cfg, "HAZARD_CACHE_DIR", os.path.expanduser("~/.cache/ttu-hazards"))

    def _covered(self) -> bool:
        """NWS covers the US and its territories only. Outside them the layer would report
        an eternal 'no alerts' — refuse loudly instead (like MRMS/GLM coverage guards)."""
        lat, lon = self.cfg.GEOCODE
        ok = bool(states_touching((lon, lat, lon, lat)))
        if not ok and not self._coverage_warned:
            self._coverage_warned = True
            log.error("site %s is outside NWS coverage — NWS alerts layer disabled (it "
                      "would otherwise report a false 'no alerts')", self.cfg.GEOCODE)
            if self.log is not None:
                self.log.record("CONFIG", reason="site outside NWS alert coverage",
                                result="NWS alerts layer disabled")
        return ok

    def _is_enabled(self) -> bool:
        return _enabled(self.cfg) and self._covered()

    # -- latch persistence ------------------------------------------------------------------
    def _new_rec(self, key, event, headline, sender, end_ts, onset_ts, first_seen, now):
        # end_ts/onset_ts are ABSOLUTE NWS times; first/last_seen and cap_ts are stamps of
        # OUR clock. shift (<= 0) maps the former into our clock's frame after a clock
        # step or a wrong-clock restore: our clock's equivalent of an NWS time = ts + shift.
        return {"key": key, "event": event, "headline": headline or "", "sender": sender or "",
                "end_ts": end_ts, "onset_ts": onset_ts, "sent_ts": None, "cap_ts": None,
                "shift": 0.0, "active": False,
                "first_seen_ts": first_seen, "last_seen_ts": now, "geometry": None}

    def _load_latch(self):
        path = self._latch_path()
        now = time.time()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            data = None
        items = data if isinstance(data, list) else (
            data.get("vetoes") if isinstance(data, dict) else None)
        if not isinstance(items, list):
            # Unreadable latch: we cannot prove no warning was in effect, so fail safe with
            # a placeholder veto. It holds at most NO_END_HOLD_SEC and is released within
            # ~2 min by the first fresh point+area queries that list nothing at the site.
            log.warning("hazard latch file %s unreadable; arming a placeholder veto "
                        "(fail-safe) until fresh NWS queries confirm the site is clear", path)
            rec = self._new_rec("latch-file-unreadable", "Unknown hazard (latch file "
                                "unreadable)", "", "", None, None, now, now)
            self._veto[rec["key"]] = rec
            self._latch_dirty = True
            self._emit([{"reason": "hazard latch file unreadable", "source": "latch",
                         "result": "unsafe until fresh NWS queries confirm"}])
            return
        events = []
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("key"), str) or not it["key"]:
                continue
            first = _finite(it.get("first_seen_ts"))
            first = now if first is None else first
            # The last sighting is restored too: a veto WITHOUT an end time is held
            # NO_END_HOLD_SEC from it, and restarting that hour from "now" on every boot
            # would let a crash or reboot loop during an NWS outage hold it for ever. A
            # file that lacks it (older format) is read as "seen now" (fail-safe).
            last = _finite(it.get("last_seen_ts"))
            last = None if last is None else max(last, first)
            end = _finite(it.get("end_ts"))
            onset = _finite(it.get("onset_ts"))
            rec = self._new_rec(it["key"], _text(it.get("event")) or "NWS alert",
                                _text(it.get("headline")), _text(it.get("sender")),
                                end, onset, first, now)
            rec["cap_ts"] = _finite(it.get("cap_ts"))
            rec["shift"] = min(0.0, _finite(it.get("shift")) or 0.0)
            rec["active"] = it.get("active") is True
            # CLAMP (same rule as the other latches): a first sighting in the future means
            # the clock is (or was) wrong — the 2026-08 boot read August latches under a
            # March clock. Move the veto into our clock's frame, so it runs its own
            # duration from now; the alert's absolute NWS times are kept and become
            # authoritative again when the clock steps back to the truth (clock_stepped).
            if first > now + CLOCK_TOLERANCE_SEC:
                log.warning("persisted hazard veto %s was first seen %.1f days in the "
                            "future — system clock is (or was) wrong; clamping", it["key"],
                            (first - now) / 86400.0)
                self._reframe(rec, now - first, now)
                if last is not None:
                    last += now - first
            # never "seen" in the future: a last sighting beyond now (a wrong clock at the
            # time) counts as now — the hold then runs its full length from here
            rec["last_seen_ts"] = now if last is None else min(last, now)
            self._bound_span(rec, now)
            if self._hold_until(rec) <= now:
                self._latch_dirty = True          # expired while we were down
                continue
            self._veto[rec["key"]] = rec
            events.append({"reason": "NWS %s over the site" % rec["event"], "source": "latch",
                           "result": "restored: unsafe until %s" % self._fmt(
                               self._hold_until(rec), now), "key": rec["key"]})
            log.info("restored hazard veto %s (%s) until %s", rec["key"], rec["event"],
                     self._fmt(self._hold_until(rec), now))
        self._emit(events)

    def _reframe(self, rec, delta, now):
        """Our clock moved by ``delta`` relative to the stamps in ``rec`` (callers set
        last_seen_ts themselves: its meaning differs between restore, step and self-heal)."""
        rec["shift"] = min(0.0, rec["shift"] + delta)
        if rec["cap_ts"] is not None:
            rec["cap_ts"] += delta
        rec["first_seen_ts"] = min(rec["first_seen_ts"] + delta, now)
        self._latch_dirty = True

    def _bound_span(self, rec, now):
        """Never hold one veto longer than MAX_VETO_SPAN_SEC from now on stale information."""
        if self._hold_until(rec) - now > MAX_VETO_SPAN_SEC:
            rec["cap_ts"] = now + MAX_VETO_SPAN_SEC
            self._latch_dirty = True

    def _latch_payload(self):
        out = []
        for k in sorted(self._veto):
            r = self._veto[k]
            item = {"key": k, "event": r["event"], "headline": r["headline"],
                    "sender": r["sender"], "end_ts": r["end_ts"],
                    "first_seen_ts": r["first_seen_ts"], "onset_ts": r["onset_ts"],
                    "last_seen_ts": r["last_seen_ts"]}
            if r.get("cap_ts") is not None:
                item["cap_ts"] = r["cap_ts"]
            if r.get("shift"):
                item["shift"] = r["shift"]
            if r.get("active"):
                item["active"] = True
            out.append(item)
        return out

    def _sig(self):
        # What the file must say. The last sighting matters only for a veto without an end
        # time (its hold runs from it), and only to NO_END_SEEN_QUANTUM_SEC: a warning
        # re-sighted every minute must not rewrite the SD-card file every minute.
        return json.dumps([(k, r["event"], r["end_ts"], r["onset_ts"], r["first_seen_ts"],
                            r.get("cap_ts"), r.get("shift"), r.get("active"),
                            None if r["end_ts"] is not None
                            else int(r["last_seen_ts"] // NO_END_SEEN_QUANTUM_SEC))
                           for k, r in sorted(self._veto.items())])

    def _persist(self):
        """Write the latch ONLY when the veto set (keys, ends, onsets) changed — it lives on
        the SD card and the check runs every minute. Caller holds the lock.

        Called from the poll thread only (poll_point / poll_area / maybe_poll's retry /
        clock_stepped), never from component(): IsSafe must not wait on — or keep hitting
        — a failing SD card. After a failed write the next attempt waits
        PERSIST_RETRY_SEC, and the failure is logged when its error changes or every
        PERSIST_FAIL_LOG_SEC (the journal is on the same card). The veto itself is held in
        memory throughout; only a restart during the failure could lose it."""
        sig = self._sig()
        if sig == self._saved_sig and not self._latch_dirty:
            return
        mono = time.monotonic()
        if mono < self._persist_retry_mono:
            self._latch_dirty = True
            return
        try:
            _atomic_write_json(self._latch_path(), self._latch_payload())
        except Exception as e:  # noqa: BLE001 — any write failure: keep in memory, retry
            self._latch_dirty = True
            self._persist_retry_mono = mono + PERSIST_RETRY_SEC
            msg = "%s: %s" % (type(e).__name__, e)
            last = self._persist_logged_mono
            if (msg != self._persist_error or last is None
                    or mono - last >= PERSIST_FAIL_LOG_SEC):
                self._persist_logged_mono = mono
                log.error("cannot persist hazard veto latch to %s (%s) — the veto is held "
                          "in memory, but a restart now would lose it; retrying every %d s",
                          self._latch_path(), msg, PERSIST_RETRY_SEC)
            self._persist_error = msg
            return
        self._saved_sig = sig
        self._latch_dirty = False
        self._persist_retry_mono = 0.0
        if self._persist_error is not None:
            log.info("hazard veto latch %s written again", self._latch_path())
            self._persist_error = None
            self._persist_logged_mono = None

    # -- veto state (caller holds the lock) -----------------------------------------------
    @staticmethod
    def _hold_until(rec) -> float:
        if rec["end_ts"] is not None:
            end = rec["end_ts"] + rec.get("shift", 0.0)
        else:
            end = rec["last_seen_ts"] + NO_END_HOLD_SEC
        cap = rec.get("cap_ts")
        return end if cap is None else min(end, cap)

    def _fmt(self, ts, now=None):
        return fmt_local(ts, self._tz(), now) or "?"

    def _in_effect(self, rec, now, skew) -> bool:
        """In effect from (onset - lead). STICKY: once a veto has been in effect it stays so
        until released — a clock step or the server-clock estimate going stale must never
        turn an active veto back into 'pending'."""
        if rec.get("active"):
            return True
        onset = rec.get("onset_ts")
        if onset is None or onset + rec.get("shift", 0.0) - self._onset_lead() <= now + skew:
            rec["active"] = True
            return True
        return False

    def _apply_sightings(self, source, sightings, now, skew=0.0) -> List[dict]:
        events = []
        for key, s in sightings.items():
            rec = self._veto.get(key)
            if rec is None:
                rec = self._new_rec(key, s["event"], s["headline"], s["sender"], s["end_ts"],
                                    s["onset_ts"], now, now)
                rec["sent_ts"] = s["sent_ts"]
                rec["geometry"] = s["geometry"]
                self._bound_span(rec, now)
                self._veto[key] = rec
                hold = self._hold_until(rec)
                if self._in_effect(rec, now, skew):
                    result = "unsafe until %s" % self._fmt(hold, now)
                else:
                    result = "pending: unsafe from %s until %s" % (
                        self._fmt(rec["onset_ts"] - self._onset_lead(), now),
                        self._fmt(hold, now))
                events.append({"reason": "NWS %s over the site" % s["event"], "source": source,
                               "result": result, "key": key, "sender": s["sender"]})
                log.warning("HAZARD VETO: NWS %s over the site (%s, %s) — %s", s["event"],
                            key, source, result)
                continue
            newer = (s["sent_ts"] is None or rec.get("sent_ts") is None
                     or s["sent_ts"] >= rec["sent_ts"])
            if newer:
                old_hold = self._hold_until(rec)
                rec.update(event=s["event"], headline=s["headline"] or rec["headline"],
                           sender=s["sender"] or rec["sender"], onset_ts=s["onset_ts"],
                           sent_ts=s["sent_ts"])
                if s["geometry"] is not None:
                    rec["geometry"] = s["geometry"]
                changed = s["end_ts"] != rec["end_ts"]
                if changed or rec["shift"]:
                    # fresh NWS times, read against the current clock, replace any clamp
                    rec["end_ts"] = s["end_ts"]
                    rec["shift"] = 0.0
                    rec["cap_ts"] = None
                    self._bound_span(rec, now)
                if changed:
                    events.append({"reason": "NWS %s updated" % s["event"], "source": source,
                                   "result": "unsafe until %s (was %s)" % (
                                       self._fmt(self._hold_until(rec), now),
                                       self._fmt(old_hold, now)), "key": key})
            # A sighting is fresh news, so it renews the stale-information cap: the cap
            # bounds how long a veto is held WITHOUT news, not how long a warning may last
            # (a High Wind Warning can run past 72 h from its first sighting, and letting
            # the cap run out mid-warning would release it until the next poll re-armed
            # it). Renewed only once half of it is used: the SD-card latch file is then
            # rewritten at most every 36 h for such a warning, not on every poll.
            cap = rec.get("cap_ts")
            if cap is not None and cap - now < MAX_VETO_SPAN_SEC / 2:
                rec["cap_ts"] = None
                self._bound_span(rec, now)
            rec["last_seen_ts"] = max(rec["last_seen_ts"], now)
        return events

    def _fresh(self, res, now) -> bool:
        if not res or not res.get("ok") or res.get("ts") is None:
            return False
        age = now - res["ts"]
        return -CLOCK_TOLERANCE_SEC <= age <= self._stale_after()

    def _confirmed_gone(self, rec, now) -> bool:
        """EARLY release: a fresh point query AND a fresh area query, both after the last
        sighting, and neither lists the key at the site. The area result must know the
        site's zones, or its silence about a zone product proves nothing."""
        p, a = self._point, self._area
        if not (self._fresh(p, now) and self._fresh(a, now) and a.get("site_capable")):
            return False
        if _norm_event(rec["event"]) in a.get("blind_events", frozenset()):
            return False            # that area answer could not have seen it (civil query down)
        last = rec["last_seen_ts"]
        return (p["ts"] > last and a["ts"] > last
                and rec["key"] not in p["site_keys"] and rec["key"] not in a["site_keys"])

    def _release_due(self, now) -> List[dict]:
        events = []
        for key in sorted(self._veto):
            rec = self._veto[key]
            hold = self._hold_until(rec)
            if now >= hold:
                why = ("ended %s" % self._fmt(hold, now) if rec["end_ts"] is not None
                       else "no end time given; %d min after the last sighting"
                       % (NO_END_HOLD_SEC // 60))
            elif self._confirmed_gone(rec, now):
                why = ("no longer in effect at the site per fresh NWS point and area queries "
                       "(cancelled, expired or moved)")
            else:
                continue
            del self._veto[key]
            events.append({"reason": "NWS %s" % rec["event"], "source": "nws",
                           "result": "released: " + why, "key": key})
            log.warning("HAZARD VETO released: NWS %s (%s): %s", rec["event"], key, why)
        return events

    def _self_heal(self, now):
        """A backward clock step while running (not yet signalled): a first sighting in the
        future is impossible under a sane clock — bound the veto by its own duration."""
        for rec in self._veto.values():
            if rec["first_seen_ts"] > now + CLOCK_TOLERANCE_SEC:
                log.warning("hazard veto %s first seen in the future (clock step?) — "
                            "clamping", rec["key"])
                delta = now - rec["first_seen_ts"]
                self._reframe(rec, delta, now)
                rec["last_seen_ts"] = min(rec["last_seen_ts"] + delta, now)
            self._bound_span(rec, now)

    def _emit(self, events):
        if self.log is None:
            return
        for ev in events:
            try:
                self.log.record("HAZARD-VETO", **ev)
            except Exception:
                log.exception("event log record failed")

    def _retry_persist(self):
        """From the poll thread: write what component() changed (a veto released at its
        end time, a clock self-heal) and retry a failed write — _persist decides whether
        anything is due (a signature compare when nothing is)."""
        with self._lock:
            self._persist()

    # -- zones ----------------------------------------------------------------------------
    def _zone_path(self, ztype, zid):
        return os.path.join(self._cache_dir(), "zones", "%s_%s.json" % (ztype, zid))

    def _make_zone_entry(self, geometry, near_box):
        geom = _polygon_geometry(geometry)
        bbox = geometry_bbox(geom)
        near = bbox is not None and bbox_intersects(bbox, near_box)
        return {"bbox": list(bbox) if bbox else None,
                "geometry": simplify_geometry(geom) if near else None}

    def _needs_refetch(self, ent, near_box) -> bool:
        # a far zone (bbox only) that the map now reaches (the site moved): fetch once more
        return (ent.get("geometry") is None and ent.get("bbox") is not None
                and bbox_intersects(ent["bbox"], near_box))

    def _zone(self, ztype, zid, url, budget, near_box):
        """The cached outline entry of one zone, fetching it (once, ever) within ``budget``.
        Returns the entry, None (no outline / fetch failed), or _DEFERRED."""
        k = (ztype, zid)
        ent = self._zones.get(k)
        if ent is not None and "failed_mono" in ent:
            if time.monotonic() - ent["failed_mono"] < ZONE_RETRY_SEC:
                return None
            ent = None
        if ent is not None and not self._needs_refetch(ent, near_box):
            return ent
        if ent is None:
            try:
                with open(self._zone_path(ztype, zid), encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and "bbox" in d:
                    ent = {"bbox": d.get("bbox"), "geometry": d.get("geometry")}
            except FileNotFoundError:
                pass
            except Exception:
                log.warning("zone cache %s unreadable; re-fetching", self._zone_path(ztype, zid))
            if ent is not None and not self._needs_refetch(ent, near_box):
                self._zones[k] = ent
                return ent
        if budget is None or budget["left"] <= 0 or time.monotonic() > budget["deadline"]:
            return _DEFERRED
        budget["left"] -= 1
        try:
            data = _get(url, self._ua())
            if not isinstance(data, dict) or "geometry" not in data:
                raise ValueError("no geometry member in zone payload")
        except Exception as e:  # noqa: BLE001 — a zone is decoration; retry later
            log.warning("zone %s/%s outline unavailable: %s", ztype, zid, e)
            self._zones[k] = {"failed_mono": time.monotonic()}
            return None
        ent = self._make_zone_entry(data.get("geometry"), near_box)
        self._zones[k] = ent
        try:   # written ONCE per zone (SD card): never re-fetched while the site stays put
            _atomic_write_json(self._zone_path(ztype, zid),
                               {"id": zid, "type": ztype, "bbox": ent["bbox"],
                                "geometry": ent["geometry"]})
        except Exception as e:  # noqa: BLE001
            if not self._cache_warned:
                self._cache_warned = True
                log.warning("cannot write zone cache in %s (%s) — keeping outlines in "
                            "memory only", self._cache_dir(), e)
        return ent

    def _attach_zone_geometry(self, rec, budget, near_box):
        """Give a zone-based alert the outline of its zones that are near the map. While some
        of its outlines are still deferred it gets none (listed if at the site, not drawn in
        part); a later poll completes it."""
        if rec.get("geometry_source") == "polygon" or not rec.get("zones"):
            return
        geoms, deferred = [], False
        for ztype, zid, url in rec["zones"]:
            ent = self._zone(ztype, zid, url, budget, near_box)
            if ent is _DEFERRED:
                deferred = True
            elif ent is not None and ent.get("geometry") is not None:
                geoms.append(ent["geometry"])
        rec["zones_deferred"] = deferred
        if deferred:
            return
        merged = merge_geometries(geoms)
        if merged is not None:
            rec["geometry"] = merged
            rec["geometry_source"] = "zones"

    def _points_path(self, lat, lon):
        return os.path.join(self._cache_dir(), "points_%s_%s.json" % (_coord(lat), _coord(lon)))

    def _ensure_site_zones(self):
        """The site's forecast zone, county and fire zone (/points), cached on disk and in
        memory; re-checked daily, the file rewritten only if they changed."""
        lat, lon = self.cfg.GEOCODE
        geo = (lat, lon)
        if geo != self._site_geo:
            self._site_geo = geo
            self._site_zones = None
            self._site_meta = {}
            self._points_next_mono = 0.0
            try:
                with open(self._points_path(lat, lon), encoding="utf-8") as f:
                    d = json.load(f)
                zs = frozenset((t, z) for t, z in d.get("zones", [])
                               if isinstance(t, str) and isinstance(z, str)
                               and _ZONE_ID_RE.match(z))
                if zs:
                    self._site_zones = zs
                    self._site_meta = {"cwa": d.get("cwa")}
            except FileNotFoundError:
                pass
            except Exception:
                log.warning("site zone cache unreadable; re-resolving")
        if time.monotonic() < self._points_next_mono:
            return self._site_zones
        try:
            p = _get("%s/points/%s,%s" % (BASE, _coord(lat), _coord(lon)), self._ua())["properties"]
            zones = []
            for field in ("forecastZone", "county", "fireWeatherZone"):
                m = _ZONE_URL_RE.match(p.get(field) or "")
                if m:
                    zones.append(("forecast" if m.group(1) == "public" else m.group(1), m.group(2)))
            if not zones:
                raise ValueError("no zones in /points answer")
        except Exception as e:  # noqa: BLE001
            log.warning("NWS /points lookup for the site failed: %s", e)
            self._points_next_mono = time.monotonic() + POINTS_RETRY_SEC
            return self._site_zones
        self._points_next_mono = time.monotonic() + POINTS_REFRESH_SEC
        zs = frozenset(zones)
        if zs != self._site_zones:
            self._site_zones = zs
            self._site_meta = {"cwa": p.get("cwa")}
            log.info("site NWS zones: %s", ", ".join("%s (%s)" % (z, t) for t, z in sorted(zs)))
            try:
                _atomic_write_json(self._points_path(lat, lon),
                                   {"zones": sorted([list(x) for x in zs]),
                                    "cwa": p.get("cwa")})
            except Exception as e:  # noqa: BLE001
                log.warning("cannot cache site zones: %s", e)
        return self._site_zones

    def _covers_site(self, rec, site_zones) -> bool:
        lat, lon = self.cfg.GEOCODE
        try:
            if rec.get("geometry_source") == "polygon":
                # polygon ONLY: its affectedZones are every county the polygon touches
                return point_in_geometry(lon, lat, rec["geometry"])
            if site_zones:
                return bool(site_zones & rec.get("zone_keys", frozenset()))
            # site zones unknown (/points failed, nothing cached): the outline decides
            return rec.get("geometry") is not None and point_in_geometry(lon, lat, rec["geometry"])
        except Exception as e:  # noqa: BLE001 — bad coordinates in one alert
            log.warning("site test failed for %s: %s", rec.get("id"), e)
            return False

    # -- polling --------------------------------------------------------------------------
    def _parse_feed(self, data, now):
        if not isinstance(data, dict) or not isinstance(data.get("features"), list):
            raise ValueError("not an alert FeatureCollection")
        recs = []
        for f in data["features"]:
            try:
                recs.append(normalize(f))
            except Exception as e:  # noqa: BLE001 — one odd feature must not drop the rest
                log.warning("skipping malformed alert feature: %s", e)
        return recs, _feed_skew(data, recs, now)

    def _keep(self, recs, now, veto_set):
        out = []
        for r in recs:
            if is_live(r, now) and excluded_reason(r, veto_set) is None:
                out.append(r)
        return out

    def _fail(self, which, now, err):
        msg = "%s: %s" % (type(err).__name__, err)
        with self._lock:
            setattr(self, "_%s_error" % which, msg)
        mono = time.monotonic()
        last = self._fail_logged.get(which)
        if last is None or mono - last >= 600:     # not every minute during an outage
            self._fail_logged[which] = mono
            log.warning("NWS alerts %s query failed: %s", which, msg)
        return {"ok": False, "error": msg}

    def _succeeded(self, which):
        if self._fail_logged.pop(which, None) is not None:
            log.info("NWS alerts %s query recovered", which)

    def poll_point(self, now=None):
        """NWS's own determination of what is in effect AT the site (the veto's fast path)."""
        now = time.time() if now is None else now
        with self._lock:
            self._point_attempt_ts = now
        lat, lon = self.cfg.GEOCODE
        url = "%s/alerts/active?point=%s,%s&%s" % (BASE, _coord(lat), _coord(lon),
                                                   _QUERY_FILTER)
        try:
            recs, skew = self._parse_feed(_get(url, self._ua()), now)
        except Exception as e:  # noqa: BLE001 — any failure = this query unavailable
            return self._fail("point", now, e)
        veto_set = _veto_set(self.cfg)
        recs = self._keep(recs, now, veto_set)
        box = map_box(self.cfg)
        near_box = _grow(box, NEAR_MARGIN_DEG)
        for r in recs:
            self._attach_zone_geometry(r, None, near_box)   # cache only: stay fast
            r["at_site"] = True                             # NWS says so
            r["on_map"] = touches_box(r.get("geometry"), box)
        sightings = _group_sightings([r for r in recs if r["event_lc"] in veto_set])
        result = {"ok": True, "ts": now, "recs": recs, "site_keys": frozenset(sightings),
                  "skew": skew, "site_capable": True}
        with self._lock:
            self._point = result
            self._point_error = None
            events = self._apply_sightings("point", sightings, now, skew)
            events += self._release_due(now)
            self._persist()
        self._succeeded("point")
        self._emit(events)
        return {"ok": True, "count": len(recs), "veto_keys": sorted(sightings)}

    def _poll_civil(self, now, codes, have_ids):
        """The national query of CIVIL_EVENTS -> (records that can concern the map, ok).

        Kept: a message whose polygon touches the map, or (no polygon) whose zones — the
        SAME county codes, for IPAWS messages — lie in the map's states; the area pipeline
        then places them like any other alert. A failure leaves the area result standing
        (logged like the other queries, at most every 10 min) and is reported as blind."""
        url = "%s/alerts/active?%s&event=%s" % (
            BASE, _QUERY_FILTER, urllib.parse.quote(",".join(CIVIL_EVENTS), safe=","))
        try:
            recs, _skew = self._parse_feed(_get(url, self._ua()), now)
        except Exception as e:  # noqa: BLE001 — information only; the area result stands
            msg = "%s: %s" % (type(e).__name__, e)
            with self._lock:
                self._civil_error = msg
            mono = time.monotonic()
            last = self._fail_logged.get("civil")
            if last is None or mono - last >= 600:
                self._fail_logged["civil"] = mono
                log.warning("NWS alerts civil-message query failed: %s", msg)
            return [], False
        with self._lock:
            self._civil_error = None
        self._succeeded("civil")
        box, states, out = map_box(self.cfg), set(codes), []
        for r in recs:
            if r["id"] in have_ids:
                continue                        # relayed by NWS: already in the area answer
            if r.get("geometry") is not None:
                if touches_box(r["geometry"], box):
                    out.append(r)
            elif any(zid[:2] in states for _t, zid, _u in r["zones"]):
                out.append(r)
        return out, True

    def poll_area(self, now=None):
        """Every alert of the states on the map: text list, overlay, and the local site test."""
        now = time.time() if now is None else now
        with self._lock:
            self._area_attempt_ts = now
        codes = area_codes(self.cfg)
        if not codes:
            return self._fail("area", now, ValueError("no NWS states on the map"))
        site_zones = self._ensure_site_zones()
        url = "%s/alerts/active?area=%s&%s" % (BASE, ",".join(codes), _QUERY_FILTER)
        try:
            recs, skew = self._parse_feed(_get(url, self._ua()), now)
        except Exception as e:  # noqa: BLE001
            return self._fail("area", now, e)
        veto_set = _veto_set(self.cfg)
        civil, civil_ok = self._poll_civil(now, codes, {r["id"] for r in recs})
        recs += civil
        count_raw = len(recs)
        recs = self._keep(recs, now, veto_set)
        # veto events first, then the most serious: they get the zone-fetch budget first
        recs.sort(key=lambda r: (r["event_lc"] not in veto_set, r["rank"]))
        box = map_box(self.cfg)
        near_box = _grow(box, NEAR_MARGIN_DEG)
        budget = {"deadline": time.monotonic() + ZONE_FETCH_BUDGET_SEC, "left": ZONE_FETCH_MAX}
        kept, deferred = [], 0
        for r in recs:
            self._attach_zone_geometry(r, budget, near_box)
            r["at_site"] = self._covers_site(r, site_zones)
            r["on_map"] = touches_box(r.get("geometry"), box)
            if r["at_site"] or r["on_map"]:
                kept.append(r)
            elif r.get("zones_deferred"):
                deferred += 1
        if deferred:
            log.info("NWS alerts: %d zone-based alert(s) not yet placed (zone outlines "
                     "still being fetched; later polls continue)", deferred)
        sightings = _group_sightings([r for r in kept
                                      if r["at_site"] and r["event_lc"] in veto_set])
        result = {"ok": True, "ts": now, "recs": kept, "site_keys": frozenset(sightings),
                  "skew": skew, "site_capable": bool(site_zones), "areas": codes,
                  "count_raw": count_raw, "count_live": len(recs), "deferred": deferred,
                  # without the civil query this result is blind to IPAWS civil messages:
                  # its silence must not release a (configured) civil-event veto
                  "blind_events": frozenset() if civil_ok else _CIVIL_LC}
        with self._lock:
            self._area = result
            self._area_error = None
            events = self._apply_sightings("local", sightings, now, skew)
            events += self._release_due(now)
            self._persist()
        self._succeeded("area")
        self._emit(events)
        return {"ok": True, "count": len(kept), "live": len(recs), "raw": count_raw,
                "veto_keys": sorted(sightings), "areas": codes}

    @staticmethod
    def _due(last, interval, now) -> bool:
        # elapsed < 0: the clock stepped backward past the last attempt — poll now rather
        # than stalling for the size of the step
        if last is None:
            return True
        elapsed = now - last
        return elapsed < 0 or elapsed >= interval

    def maybe_poll(self, now=None):
        """Point query every HAZARD_POINT_POLL_SEC, area query every HAZARD_AREA_POLL_SEC."""
        explicit = now is not None
        now = time.time() if now is None else now
        if not self._is_enabled():
            return None
        if not _veto_set(self.cfg) and not self._noveto_warned:
            self._noveto_warned = True
            log.warning("HAZARD_VETO_EVENTS is empty — NWS alerts are information only")
        self._retry_persist()
        p_every = float(_cfg(self.cfg, "HAZARD_POINT_POLL_SEC", DEFAULT_POINT_POLL_SEC))
        a_every = float(_cfg(self.cfg, "HAZARD_AREA_POLL_SEC", DEFAULT_AREA_POLL_SEC))
        with self._lock:
            due_p = self._due(self._point_attempt_ts, p_every, now)
            due_a = self._due(self._area_attempt_ts, a_every, now)
        out = {}
        if due_p:
            out["point"] = self.poll_point(now)
        if due_a:
            out["area"] = self.poll_area(now if explicit else time.time())
        return out or None

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """The wall clock stepped by delta. Our own stamps (first/last seen, caps) move with
        it; the alerts' ABSOLUTE NWS times do not. So each veto's shift absorbs a backward
        step (the veto keeps exactly the time it had left, however far away its NWS end now
        looks) and a forward step returns the shift towards 0, never above it: a forward
        step never holds a veto beyond its NWS end (an end the step carries into the past
        is over in real time, and the contract bounds every veto by its end). last_seen =
        the step, so an early release needs queries made AFTER it; results fetched before
        it are no longer fresh; both queries re-poll now."""
        delta = post_now - pre_now
        with self._lock:
            for rec in self._veto.values():
                self._reframe(rec, delta, post_now)
                rec["last_seen_ts"] = post_now
                self._bound_span(rec, post_now)
            self._point_attempt_ts = None
            self._area_attempt_ts = None
            self._point = None
            self._area = None
            self._persist()
        log.warning("NWS alerts: clock step handled (%+.0f s) — re-polling both queries",
                    delta)

    # -- output ---------------------------------------------------------------------------
    def _groups(self, p, a, now):
        """Fresh alert messages grouped by key: {key: {"all": [...], "site": [...]}}."""
        by_id: Dict[str, dict] = {}
        order = []
        for res, from_point in ((a, False), (p, True)):
            for r in (res or {}).get("recs", []):
                if r.get("end_ts") is not None and r["end_ts"] <= now:
                    continue
                cur = by_id.get(r["id"])
                if cur is None:
                    by_id[r["id"]] = dict(r)
                    order.append(r["id"])
                else:   # same message in both feeds: union the flags, keep a shape
                    cur["at_site"] = cur.get("at_site") or r.get("at_site")
                    cur["on_map"] = cur.get("on_map") or r.get("on_map")
                    if cur.get("geometry") is None and r.get("geometry") is not None:
                        cur["geometry"] = r["geometry"]
                        cur["geometry_source"] = r.get("geometry_source")
        groups: Dict[str, dict] = {}
        for i in order:
            r = by_id[i]
            g = groups.setdefault(r["key"], {"all": [], "site": []})
            g["all"].append(r)
            if r.get("at_site"):
                g["site"].append(r)
        return groups

    def _view(self, recs, all_recs, now, active_keys, veto_set):
        prim = max(recs, key=lambda r: (r.get("sent_ts") or 0.0,
                                        r["end_ts"] if r.get("end_ts") is not None
                                        else float("inf")))
        areas = []
        for r in recs + all_recs:
            if r.get("area_desc") and r["area_desc"] not in areas:
                areas.append(r["area_desc"])
        gsrc = prim.get("geometry_source") or next(
            (r.get("geometry_source") for r in all_recs if r.get("geometry_source")), None)
        return {
            "key": prim["key"], "event": prim["event"], "kind": prim["kind"],
            "severity": prim["severity"], "urgency": prim["urgency"],
            "headline": prim["headline"], "nws_headline": prim["nws_headline"],
            "sender": prim["sender"], "area_desc": _clip("; ".join(areas), AREA_DESC_MAX),
            "onset": prim["onset"], "ends": prim["ends"], "expires": prim["expires"],
            "end_local": fmt_local(prim["end_ts"], self._tz(), now),
            "color": prim["color"], "threat": prim["threat"],
            "vetoes": prim["key"] in active_keys,
            "veto_event": prim["event_lc"] in veto_set,
            "geometry_source": gsrc,
            "description": _clip(prim["description"], DESCRIPTION_MAX),
            "instruction": _clip(prim["instruction"], INSTRUCTION_MAX),
            "_sort": (prim["key"] not in active_keys, prim["rank"],
                      prim["end_ts"] if prim["end_ts"] is not None else float("inf"),
                      prim["event"]),
        }

    def _active_keys(self, p, a, now):
        skew = max(0.0, (p or {}).get("skew", 0.0), (a or {}).get("skew", 0.0))
        return {k for k, rec in self._veto.items() if self._in_effect(rec, now, skew)}, skew

    def component(self, now=None) -> dict:
        now = time.time() if now is None else now
        veto_set = _veto_set(self.cfg)
        with self._lock:
            # (no disk I/O here: this runs on every IsSafe evaluation; what changes here —
            # a veto that ran out, a clock self-heal — is written by the poll thread)
            self._self_heal(now)
            events = self._release_due(now)
            p = self._point if self._fresh(self._point, now) else None
            a = self._area if self._fresh(self._area, now) else None
            active_keys, skew = self._active_keys(p, a, now)
            veto, pending, reasons = [], [], []
            for key in sorted(self._veto, key=lambda k: self._hold_until(self._veto[k])):
                rec = self._veto[key]
                in_p = p is not None and key in p["site_keys"]
                in_a = a is not None and key in a["site_keys"]
                hold = self._hold_until(rec)
                entry = {
                    "key": key, "event": rec["event"], "headline": rec["headline"],
                    "sender": rec["sender"], "end_ts": hold,
                    "end_local": self._fmt(hold, now),
                    "alert_end_ts": rec["end_ts"],
                    "first_seen_ts": rec["first_seen_ts"],
                    "onset_ts": rec["onset_ts"],
                    "onset_local": fmt_local(rec["onset_ts"], self._tz(), now),
                    "source": ("both" if in_p and in_a else "point" if in_p
                               else "local" if in_a else "latched"),
                }
                if key in active_keys:
                    veto.append(entry)
                    who = " (%s)" % rec["sender"] if rec["sender"] else ""
                    if rec["end_ts"] is None:
                        reasons.append("NWS %s in effect for the site (no end time given; "
                                       "held until %s)%s" % (rec["event"], entry["end_local"],
                                                             who))
                    else:
                        reasons.append("NWS %s in effect for the site until %s%s"
                                       % (rec["event"], entry["end_local"], who))
                else:
                    pending.append(entry)
            groups = self._groups(p, a, now)
            at_site, nearby = [], []
            for key, g in groups.items():
                if g["site"]:
                    at_site.append(self._view(g["site"], g["all"], now, active_keys, veto_set))
                elif any(r.get("on_map") for r in g["all"]):
                    nearby.append(self._view(g["all"], g["all"], now, active_keys, veto_set))
            for lst in (at_site, nearby):
                lst.sort(key=lambda v: v["_sort"])
                for v in lst:
                    del v["_sort"]
            errors = [("point query: " + self._point_error) if self._point_error else None,
                      ("area query: " + self._area_error) if self._area_error else None,
                      ("civil-message query: " + self._civil_error)
                      if self._civil_error else None,
                      ("latch file not written: " + self._persist_error)
                      if self._persist_error else None]
            comp = {
                "safe": not veto,
                "enabled": self._is_enabled(),
                "available": p is not None or a is not None,
                # freshness as THIS daemon judges it (TTU_SAFETY_HAZARD_STALE_SEC), so the
                # page never has to guess the threshold
                "point_fresh": p is not None,
                "area_fresh": a is not None,
                "stale_after_s": self._stale_after(),
                "veto_events": veto_event_names(self.cfg),
                "veto": veto,
                "veto_pending": pending,
                "reasons": reasons,
                "at_site": at_site,
                "nearby": nearby,
                "counts": {"at_site": len(at_site), "nearby": len(nearby)},
                "point_age_s": (round(now - self._point["ts"])
                                if self._point and self._point.get("ts") is not None else None),
                "area_age_s": (round(now - self._area["ts"])
                               if self._area and self._area.get("ts") is not None else None),
                "error": "; ".join(e for e in errors if e) or None,
                "site_zones": ["%s (%s)" % (z, t) for t, z in sorted(self._site_zones or ())],
                "areas": list((self._area or {}).get("areas") or []),
                "source": SOURCE,
            }
        self._emit(events)
        return comp

    def overlays(self, now=None) -> list:
        """Alert areas for the radar map, in DRAW ORDER: watches/advisories first, warnings
        later, the vetoing alert(s) last. Cheap (cached shapes, no network). A held veto
        whose feeds went stale is still drawn with its last known shape."""
        now = time.time() if now is None else now
        box = map_box(self.cfg)
        with self._lock:
            p = self._point if self._fresh(self._point, now) else None
            a = self._area if self._fresh(self._area, now) else None
            active_keys, _skew = self._active_keys(p, a, now)
            out, drawn = [], set()
            for key, g in self._groups(p, a, now).items():
                geoms = [r["geometry"] for r in g["all"]
                         if r.get("geometry") is not None and r.get("on_map")]
                if not geoms:
                    continue
                recs = g["site"] or g["all"]
                prim = max(recs, key=lambda r: r.get("sent_ts") or 0.0)
                out.append({"kind": "alert", "key": key, "event": prim["event"],
                            "color": prim["color"],
                            "geometry": geoms[0] if len(geoms) == 1 else merge_geometries(geoms),
                            "rank": prim["rank"], "vetoes": key in active_keys,
                            "at_site": bool(g["site"])})
                drawn.add(key)
            for key in sorted(active_keys - drawn):
                rec = self._veto[key]
                if rec.get("geometry") is not None and touches_box(rec["geometry"], box):
                    out.append({"kind": "alert", "key": key, "event": rec["event"],
                                "color": color_for(rec["event"]), "geometry": rec["geometry"],
                                "rank": alert_rank(event_kind(rec["event"]), "Extreme"),
                                "vetoes": True, "at_site": True})
        out.sort(key=lambda o: (o["vetoes"], -o["rank"], o["event"], o["key"]))
        return out


def unavailable_component(cfg) -> dict:
    """The component when the NWS alerts layer is disabled/absent (never affects IsSafe)."""
    return {
        "safe": True, "enabled": False, "available": False,
        "veto_events": veto_event_names(cfg), "veto": [], "veto_pending": [], "reasons": [],
        "at_site": [], "nearby": [], "counts": {"at_site": 0, "nearby": 0},
        "point_age_s": None, "area_age_s": None, "error": None,
        "point_fresh": False, "area_fresh": False,
        "stale_after_s": float(_cfg(cfg, "HAZARD_STALE_AFTER_SEC", DEFAULT_STALE_AFTER_SEC)),
        "site_zones": [], "areas": [],
        "source": SOURCE + " (disabled)",
    }
