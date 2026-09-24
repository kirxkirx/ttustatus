"""Hazard INFORMATION for the status page and the radar map (NOAA, NIFC, IEM).

INFORMATION ONLY — nothing here ever influences IsSafe. component() always reports
safe=True and info_only=True, and SafetyMonitor must never AND it into the verdict (a test
enforces that). The only hazard veto is the handful of configured NWS warnings over the
site (safety/nws_alerts.py). Everything in this module is context for the observer —
smoke aloft this afternoon, a grass fire on the horizon, the SPC risk for tonight, what
storms actually did nearby — and none of it says anything about the sky over the dome
right now that the rain / radar / lightning / forecast layers do not measure more
directly. So none of it may close the roof.

Feeds (all free, no key). Each is INDEPENDENT — its own ok / error / age, its own
cadence — so one failing source never blanks the others:

  smoke            NOAA HMS analyst smoke polygons: today's KML (UTC day), else yesterday's
  fires            NIFC WFIGS current wildland-fire incidents (points, radius query)
  fire_perimeters  NIFC WFIGS current interagency fire perimeters (map-box query)
  spc_outlook      SPC Day-1 categorical convective outlook (the site's category + outlines)
  spc_md           SPC mesoscale discussions in effect now (via IEM)
  lsr              NWS Local Storm Reports, last HAZARD_LSR_HOURS, inside the map (via IEM)

Network discipline (the Pi): every request is small by construction — radius / bbox
filtering is done SERVER-side, gzip is requested where offered, conditional GETs
(If-None-Match / If-Modified-Since: a 304 costs no body) are used wherever the server
sends validators, every body is capped (gzip bombs included), and each feed runs at its
own cadence (never faster than HAZARD_FEEDS_POLL_SEC; slow products slower). Nothing here
touches the disk: all state lives in RAM, so the SD card sees nothing from this module,
and failures are logged only on transitions (journald is persistent on the SD card too).

Fail-safe DISPLAY (not safety): a feed whose last success is older than two poll
intervals is reported stale and its items are withheld — old information is never
presented as current — and the product's own time (file date, valid window, report time)
always travels with it. A clock step discards everything fetched under the wrong clock
(the HMS file name and the LSR window are date-derived) and re-polls at once.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:                      # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None

log = logging.getLogger("ttu.safety.hazard_feeds")

# ---- endpoints (all verified live 2026-09-24) -------------------------------------
# One KML per UTC day, re-published as the analysts add polygons (daytime only).
HMS_SMOKE_KML = ("https://satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Smoke_Polygons/KML/"
                 "{d:%Y}/{d:%m}/hms_smoke{d:%Y%m%d}.kml")
WFIGS = "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services"
WFIGS_INCIDENTS = WFIGS + "/WFIGS_Incident_Locations_Current/FeatureServer/0/query"
WFIGS_PERIMETERS = WFIGS + "/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
SPC_DAY1_CAT = "https://www.spc.noaa.gov/products/outlook/day1otlk_cat.nolyr.geojson"
IEM_SPC_MCD = "https://mesonet.agron.iastate.edu/api/1/nws/spc_mcd.geojson"
IEM_LSR_BY_POINT = "https://mesonet.agron.iastate.edu/api/1/nws/lsrs_by_point.geojson"
SPC_MD_PAGE = "https://www.spc.noaa.gov/products/md/{year}/md{num:04d}.html"

# (the SPC mesoscale discussions come from IEM's copy, like the storm reports)
SOURCE = ("NOAA HMS smoke · NIFC WFIGS fires · NOAA SPC outlook · "
          "SPC mesoscale discussions and NWS storm reports via IEM")
INFO_NOTE = ("Information only: these feeds never affect the safety monitor — only the "
             "configured NWS warnings over the site do.")

# ---- tuning (behaviour knobs; the operator-facing ones live in config.py) ---------
HTTP_TIMEOUT = 20                  # s per request (own thread: never blocks evaluate())
MAX_BODY_BYTES = 4 * 1024 * 1024   # every JSON answer here is KBs; cap garbage/runaway
# A finished HMS day in a heavy smoke season (2023's Canadian fires) runs to MBs.
HMS_MAX_BODY_BYTES = 24 * 1024 * 1024
MAX_ITEMS = 20                     # per displayed list: the state file and page stay small
# A FAILED feed is retried after min(cadence, this) — a boot before the network is up
# must not leave a 30-min-cadence feed empty for half an hour.
FAIL_RETRY_SEC = 300
# Per-feed MINIMUM cadence (s); the effective one is max(HAZARD_FEEDS_POLL_SEC, this).
# HMS smoke is analysed a few times per day, in daylight only: 30 min is plenty (and the
# poll is a conditional GET). IEM is courteous at 5 min; the rest at 10 min.
FEED_MIN_INTERVAL = {"smoke": 1800, "fires": 600, "fire_perimeters": 600,
                     "spc_outlook": 600, "spc_md": 300, "lsr": 300}
# Wildfire incidents: WFIGS "current" does NOT mean burning — records discovered months
# ago stay current while anybody edits them (the NM batch edits of 2026-09-22). Listed:
# type WF, not out / contained / controlled / 100 %, updated in the last 72 h, and either
# discovered in the last 14 days or large (>= 1000 ac, still worth knowing about).
FIRE_RADIUS_KM = 150.0             # text radius (the map box is always covered as well)
FIRE_UPDATED_WITHIN_H = 72.0
FIRE_NEW_WITHIN_D = 14.0
FIRE_LARGE_ACRES = 1000.0
MD_LOOKBACK_H = 8                  # MDs issued this long ago can still be in effect
LSR_MAX_KEEP = 200                 # reports kept per poll (a tornado outbreak is ~100)
SMOKE_MAX_POLYS = 60               # smoke polygons kept per file (touching the map only)
PERIMETER_MAX = 30
# Coverage (lonmin, latmin, lonmax, latmax). SPC, LSR and WFIGS are US products and HMS
# covers North America: at a site outside, a feed says so instead of a false "nothing".
CONUS = (-130.0, 20.0, -60.0, 55.0)
NORTH_AMERICA = (-170.0, 5.0, -50.0, 80.0)

FEEDS = (
    # name, page label, attribution, coverage box
    ("smoke", "Smoke (satellite analysis)", "NOAA/NESDIS Hazard Mapping System", NORTH_AMERICA),
    ("fires", "Wildfires", "NIFC WFIGS incident locations", CONUS),
    ("fire_perimeters", "Fire perimeters", "NIFC WFIGS interagency perimeters", CONUS),
    ("spc_outlook", "SPC Day 1 outlook", "NOAA Storm Prediction Center", CONUS),
    ("spc_md", "SPC mesoscale discussions", "NOAA Storm Prediction Center via IEM", CONUS),
    ("lsr", "Local storm reports", "NWS Local Storm Reports via IEM", CONUS),
)
FEED_NAMES = tuple(f[0] for f in FEEDS)

# ---- map styling (hints for the radar renderer) ------------------------------------
# The owner's rule: hazards are NEVER drawn green — on this page green reads as "good"
# and blends into the 15-35 dBZ radar greens a storm brings. SPC paints TSTM and MRGL
# green, so TSTM (text only) gets a neutral grey and MRGL the radar map's own sand colour
# (radar._THEME "mrgl": the light-map line / the dark-map line), so the page swatch and
# the map agree; every other category keeps SPC's own colours (verified against archived
# outlooks). stroke = the darker line colour, fill = the lighter shade (a better line
# colour on the dark map); outlines only (fill_alpha 0).
SPC_CATEGORIES = {
    # LABEL: (rank, name, stroke, fill)
    "TSTM": (1, "General Thunderstorms Risk", "#9E9E9E", "#D6D6D6"),   # SPC #55BB55/#C1E9C1
    "MRGL": (2, "Marginal Risk", "#7D6E41", "#C8B98C"),                # SPC #005500/#66A366
    "SLGT": (3, "Slight Risk", "#DDAA00", "#FFE066"),
    "ENH": (4, "Enhanced Risk", "#FF6600", "#FFA366"),
    "MDT": (5, "Moderate Risk", "#CC0000", "#E06666"),
    "HIGH": (6, "High Risk", "#CC00CC", "#EE99EE"),
}
SPC_DRAW_MIN_RANK = 2              # outlines for MRGL and above; TSTM is text only
SMOKE_FILL = {"Light": ("#B4B4B4", 45), "Medium": ("#969696", 80), "Heavy": ("#787878", 115)}
MD_COLOR = "#9370DB"                # as radar.MD_RGB draws it
FIRE_COLOR = "#FF4500"
# Local storm reports by kind: (symbol, colour). Tornado red, wind blue as on SPC's
# report maps; hail light blue instead of SPC's green (never green); flood red like the
# NWS flood products on this page.
LSR_KINDS = {
    "tornado": ("triangle", "#FF0000"), "hail": ("circle", "#40C4FF"),
    "wind": ("square", "#3050FF"), "flood": ("diamond", "#C62828"),
    "dust": ("diamond", "#C8A165"), "winter": ("circle", "#E0E0FF"),
    "fire": ("triangle", "#FF4500"), "rain": ("circle", "#7B68EE"),
    "lightning": ("diamond", "#FFD700"), "other": ("circle", "#C0C0C0"),
}
# Draw order hints: lower rank = drawn later (on top), the same convention as the NWS
# alert overlays. Info layers bottom-up: smoke, SPC outlines, MDs, fire perimeters, fire
# points, storm reports.
RANK = {"smoke": 90, "spc_outlook": 80, "spc_md": 70, "fire_perimeter": 60, "fire": 50,
        "lsr": 30}


def _style(stroke=None, fill=None, fill_alpha=0, width=2, dash=False, symbol=None,
           size=None):
    return {"stroke": stroke, "fill": fill, "fill_alpha": fill_alpha, "width": width,
            "dash": dash, "symbol": symbol, "size": size}


# ---- small helpers ----------------------------------------------------------------
def _cfgv(cfg, name, default):
    """A config value with a default: the hazard knobs are new, and this module must keep
    working (with the documented defaults) against a config that predates them."""
    v = getattr(cfg, name, default)
    return default if v is None else v


def _enabled(cfg):
    return bool(_cfgv(cfg, "HAZARD_FEEDS_ENABLED", True))


def _num(x):
    """A finite float, or None (feeds mix numbers, numeric strings, '' and null)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
    elif isinstance(x, str) and x.strip():
        try:
            v = float(x.strip())
        except ValueError:
            return None
    else:
        return None
    return v if math.isfinite(v) else None


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _clip(s, n):
    """Remote text -> one clean, bounded line (the page escapes it; this keeps the
    state file small and a hostile feed from injecting control characters)."""
    if s is None:
        return ""
    s = " ".join(_CTRL.sub(" ", str(s)).split())
    return s if len(s) <= n else s[:max(0, n - 1)].rstrip() + "…"


def _int(x):
    return None if x is None else int(x)


def _ms(x):
    """ArcGIS epoch milliseconds -> epoch seconds (None if absent/garbage)."""
    v = _num(x)
    return v / 1000.0 if v is not None else None


def _parse_iso(s):
    """ISO-8601 -> epoch seconds; a naive stamp is UTC (IEM writes UTC without Z)."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip().replace(" ", "T")
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        # fromisoformat on 3.9 rejects some fractional forms ("...00.000+00:00" is fine,
        # "...00.0000000" is not): retry without the fraction
        try:
            dt = datetime.fromisoformat(re.sub(r"\.\d+", "", t))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _json(body):
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8-sig")
    return json.loads(body)


def _errstr(e):
    if isinstance(e, urllib.error.HTTPError):
        return _clip(f"HTTP {e.code} {e.reason}", 200)
    if isinstance(e, urllib.error.URLError):
        return _clip(f"network: {e.reason}", 200)
    return _clip(f"{type(e).__name__}: {e}", 200)


_tz_warned = False


def _tz(tzname):
    global _tz_warned
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tzname)
        except Exception:
            if not _tz_warned:
                _tz_warned = True
                log.warning("TTU_SAFETY_LOCAL_TZ=%r is not a valid timezone — hazard "
                            "times will show UTC", tzname)
    return timezone.utc


def _fmt_time(ts, tzname, now=None):
    """'Thu 14:05 CDT' (with the date when it is not within the last/next ~6 days)."""
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts, _tz(tzname))
    except (OverflowError, OSError, ValueError):
        return None
    if now is not None and abs(now - ts) > 6 * 86400:
        return dt.strftime("%a %b %d %H:%M %Z")
    return dt.strftime("%a %H:%M %Z")


def _fmt_window(start, end, tzname):
    """'07:00–10:00 CDT (12:00–15:00 UTC)': local first (the observer's clock), UTC as
    the product itself states it."""
    if start is None or end is None:
        return None
    su = datetime.fromtimestamp(start, timezone.utc)
    eu = datetime.fromtimestamp(end, timezone.utc)
    tz = _tz(tzname)
    sl, el = datetime.fromtimestamp(start, tz), datetime.fromtimestamp(end, tz)
    utc = f"{su:%H:%M}–{eu:%H:%M} UTC"
    if tz is timezone.utc:
        return utc
    return f"{sl:%H:%M}–{el:%H:%M} {el.strftime('%Z')} ({utc})"


# ---- geometry (pure Python; ported from the author's weather/geo.py) ---------------
EARTH_RADIUS_KM = 6371.0088


def _haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _bearing(lat, lon, lat2, lon2):
    p1, p2 = math.radians(lat), math.radians(lat2)
    dl = math.radians(lon2 - lon)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _cardinal(d):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((d % 360) / 45 + 0.5) % 8]


def _site_box(cfg, site=None):
    """(lonmin, latmin, lonmax, latmax) of the radar thumbnail: the site +- half-size,
    exactly the region radar.Thumbnailer renders."""
    lat0, lon0 = site if site is not None else cfg.GEOCODE
    h = float(_cfgv(cfg, "RADAR_THUMB_HALF_DEG", 1.0))
    return (lon0 - h, lat0 - h, lon0 + h, lat0 + h)


def _box_radius_km(lat0, lon0, box):
    """Distance from the site to the farthest map corner (radius queries cover the map)."""
    return max(_haversine_km(lat0, lon0, la, lo)
               for lo in (box[0], box[2]) for la in (box[1], box[3]))


def _in_box(lon, lat, box):
    return box[0] <= lon <= box[2] and box[1] <= lat <= box[3]


def _bbox_intersects(a, b):
    if a is None or b is None:
        return False
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _iter_polygons(g):
    """Each polygon of a GeoJSON geometry as a list of rings (outer first)."""
    if not isinstance(g, dict):
        return
    t = g.get("type")
    if t == "Polygon":
        rings = g.get("coordinates") or []
        if rings and rings[0]:
            yield rings
    elif t == "MultiPolygon":
        for rings in g.get("coordinates") or []:
            if rings and rings[0]:
                yield rings
    elif t == "GeometryCollection":
        for sub in g.get("geometries") or []:
            yield from _iter_polygons(sub)


def _point_in_ring(lon, lat, ring):
    """Ray casting (even-odd); a point exactly on an edge counts as inside."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
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


def _point_in_polygon(lon, lat, rings):
    """Inside the outer ring and outside every hole (SPC's non-layered categories and
    HMS smoke polygons both carry holes)."""
    if not rings or not _point_in_ring(lon, lat, rings[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in rings[1:])


def _point_in_geometry(lon, lat, g):
    return any(_point_in_polygon(lon, lat, rings) for rings in _iter_polygons(g))


def _geometry_bbox(g):
    box = None
    for rings in _iter_polygons(g):
        xs = [p[0] for p in rings[0]]
        ys = [p[1] for p in rings[0]]
        b = (min(xs), min(ys), max(xs), max(ys))
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                     max(box[2], b[2]), max(box[3], b[3]))
    return box


def _clean_ring(ring):
    out = []
    for pt in ring or []:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            return None
        lon, lat = _num(pt[0]), _num(pt[1])
        if lon is None or lat is None or not (-180.0 <= lon <= 180.0) \
                or not (-90.0 <= lat <= 90.0):
            return None
        out.append([round(lon, 4), round(lat, 4)])   # ~10 m: plenty for a 440 px map
    return out if len(out) >= 3 else None


def _clean_polygon(rings):
    if not isinstance(rings, list) or not rings:
        return None
    outer = _clean_ring(rings[0])
    if outer is None:
        return None
    return [outer] + [h for h in (_clean_ring(r) for r in rings[1:]) if h is not None]


def _clean_geometry(g):
    """Any polygonal GeoJSON geometry -> a validated Polygon / MultiPolygon (2-D, finite,
    rounded), or None. What the radar renderer receives is always well-formed."""
    polys = [p for p in (_clean_polygon(r) for r in _iter_polygons(g)) if p]
    if not polys:
        return None
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def _clean_point(g, props=None):
    """(lon, lat) of a GeoJSON Point (else of lon/lat properties), or None."""
    c = g.get("coordinates") if isinstance(g, dict) and g.get("type") == "Point" else None
    lon = lat = None
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        lon, lat = _num(c[0]), _num(c[1])
    if (lon is None or lat is None) and isinstance(props, dict):
        lon, lat = _num(props.get("lon")), _num(props.get("lat"))
    if lon is None or lat is None or not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
        return None
    return lon, lat


def _point_geometry(lon, lat):
    return {"type": "Point", "coordinates": [round(lon, 4), round(lat, 4)]}


def _geo_hash(g):
    return hashlib.sha1(json.dumps(g, sort_keys=True).encode()).hexdigest()[:10]


# ---- HTTP ---------------------------------------------------------------------------
def _gunzip(raw, max_bytes):
    """gzip -> bytes, refusing anything that inflates beyond max_bytes (a gzip bomb must
    not exhaust the Pi's RAM)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(raw, max_bytes + 1)
    if len(out) > max_bytes or d.unconsumed_tail:
        raise ValueError(f"response inflates beyond {max_bytes} bytes")
    return out


def _get(url, ua, etag=None, last_modified=None, timeout=HTTP_TIMEOUT,
         max_bytes=MAX_BODY_BYTES, accept="application/geo+json, application/json;q=0.9"):
    """One GET -> (status, body, etag, last_modified).

    Conditional when validators are given: a 304 returns (304, None, ...) — the caller's
    copy is still current and no body crossed the network. gzip is requested and
    inflated here (urllib does not). Every body is capped at max_bytes. Any other
    failure raises (urllib.error.HTTPError for a status, URLError/OSError for transport,
    ValueError for an oversized or oddly encoded body)."""
    headers = {"User-Agent": ua, "Accept": accept, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError(f"response larger than {max_bytes} bytes")
            enc = (r.headers.get("Content-Encoding") or "").strip().lower()
            if enc in ("gzip", "x-gzip"):
                raw = _gunzip(raw, max_bytes)
            elif enc not in ("", "identity"):
                raise ValueError(f"unexpected Content-Encoding {enc!r}")
            return 200, raw, r.headers.get("ETag"), r.headers.get("Last-Modified")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            h = e.headers
            return (304, None, (h.get("ETag") if h else None) or etag,
                    (h.get("Last-Modified") if h else None) or last_modified)
        raise


# ---- parsers (pure: bytes + site/box -> compact records; site-dependent parts done
# here, once per fetch; time-dependent filtering is done at view time) ---------------
_KML_REFUSE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.I)
_HMS_TIME = re.compile(r"(Start|End)\s*Time:\s*(\d{7})\s*(\d{4})\s*UTC", re.I)
_HMS_DENSITY = re.compile(r"Density:\s*(Light|Medium|Heavy)", re.I)
_HMS_STYLE_DENSITY = re.compile(r"Smoke_(Light|Medium|Heavy)", re.I)
_HMS_SAT = re.compile(r"Satellite:\s*([A-Za-z0-9_\- ]+)", re.I)
_DENSITY_RANK = {"Light": 1, "Medium": 2, "Heavy": 3}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kml_ring(text):
    pts = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            return None
        pts.append([parts[0], parts[1]])
    return _clean_ring(pts)


def _kml_polygons(pm):
    """Every <Polygon> under a Placemark (MultiGeometry included) as rings lists."""
    out = []
    for poly in pm.iter():
        if _local(poly.tag) != "Polygon":
            continue
        outer, holes = None, []
        for b in poly:
            kind = _local(b.tag)
            if kind not in ("outerBoundaryIs", "innerBoundaryIs"):
                continue
            coords = next((c for c in b.iter() if _local(c.tag) == "coordinates"), None)
            ring = _kml_ring(coords.text if coords is not None else None)
            if ring is None:
                continue
            if kind == "outerBoundaryIs":
                outer = ring
            else:
                holes.append(ring)
        if outer is not None:
            out.append([outer] + holes)
    return out


def _hms_ts(yday, hhmm):
    try:
        return datetime.strptime(f"{yday} {hhmm}", "%Y%j %H%M").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def parse_hms_kml(body, site, box, file_date=None):
    """HMS smoke KML -> {"file_date", "total", "latest_window", "polys": [...]} keeping
    only polygons that touch the map (the site test needs no others). Parsed with
    xml.etree after refusing any DOCTYPE/ENTITY (defusedxml is not available: no entity
    games), streamed with iterparse and cleared as it goes, so a multi-MB file does not
    balloon in RAM.

    latest_window = (start, end) of the file's LATEST analysis over ALL its placemarks,
    taken before the map filter: "the latest analysis" is the analysts' latest, not the
    latest one that happened to have smoke near the site — else, after an afternoon pass
    with no smoke here, the morning's smoke would be shown as current."""
    if _KML_REFUSE.search(body or b""):
        raise ValueError("HMS KML declares a DOCTYPE/ENTITY (refused)")
    lat0, lon0 = site
    polys, total = [], 0
    lend = lstart = None
    try:
        for _ev, el in ET.iterparse(io.BytesIO(body), events=("end",)):
            if _local(el.tag) != "Placemark":
                continue
            total += 1
            desc, style = "", ""
            for c in el:
                if _local(c.tag) == "description":
                    desc = c.text or ""
                elif _local(c.tag) == "styleUrl":
                    style = c.text or ""
            m = _HMS_DENSITY.search(desc) or _HMS_STYLE_DENSITY.search(style)
            density = m.group(1).capitalize() if m else None
            times = {k.lower(): _hms_ts(d, hm) for k, d, hm in _HMS_TIME.findall(desc)}
            end, start = times.get("end"), times.get("start")
            if end is not None:
                if lend is None or end > lend:
                    lend, lstart = end, start
                elif end == lend and start is not None and (lstart is None or start < lstart):
                    lstart = start
            sat = _HMS_SAT.search(desc)
            for rings in _kml_polygons(el):
                g = {"type": "Polygon", "coordinates": rings}
                bb = _geometry_bbox(g)
                if not _bbox_intersects(bb, box):
                    continue
                polys.append({
                    "density": density, "start_ts": times.get("start"),
                    "end_ts": times.get("end"),
                    "satellite": _clip(sat.group(1), 20) if sat else None,
                    "at_site": _point_in_polygon(lon0, lat0, rings), "geometry": g,
                    # hashed once here, not per view: component() runs on every
                    # Alpaca poll and must stay cheap on the Pi
                    "ghash": _geo_hash(g),
                })
            el.clear()
    except ET.ParseError as e:
        raise ValueError(f"HMS KML is not XML: {e}")
    # newest analyses first, so a (pathological) cap keeps the relevant ones
    polys.sort(key=lambda p: -(p["end_ts"] or 0))
    latest = None if lend is None else (lstart if lstart is not None else lend, lend)
    return {"file_date": file_date, "total": total, "latest_window": latest,
            "polys": polys[:SMOKE_MAX_POLYS]}


def _arcgis_features(body):
    doc = _json(body)
    if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
        # ArcGIS reports query errors as HTTP 200 + {"error": {...}}
        err = doc["error"]
        raise ValueError(_clip(f"ArcGIS error {err.get('code')}: {err.get('message')}", 200))
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise ValueError("ArcGIS answer has no 'features' list")
    return feats


FIRE_FIELDS = ("IncidentName", "IncidentSize", "PercentContained", "FireDiscoveryDateTime",
               "ModifiedOnDateTime_dt", "FireOutDateTime", "ContainmentDateTime",
               "ControlDateTime", "POOCounty", "POOState", "IncidentTypeCategory",
               "UniqueFireIdentifier")
PERIMETER_FIELDS = ("poly_IncidentName", "poly_GISAcres", "attr_IncidentSize",
                    "attr_PercentContained", "attr_IncidentTypeCategory",
                    "attr_UniqueFireIdentifier", "attr_FireDiscoveryDateTime",
                    "attr_ModifiedOnDateTime_dt", "poly_DateCurrent", "attr_FireOutDateTime",
                    "attr_ContainmentDateTime", "attr_ControlDateTime")


def _state_code(s):
    s = _clip(s, 8).upper()
    return s[3:] if s.startswith("US-") else s


def parse_fires(body, site, box, radius_km):
    lat0, lon0 = site
    out = []
    for ft in _arcgis_features(body):
        if not isinstance(ft, dict):
            continue
        p = ft.get("properties") if isinstance(ft.get("properties"), dict) else {}
        if str(p.get("IncidentTypeCategory") or "").strip().upper() != "WF":
            continue                        # prescribed burns (RX) etc. are not wildfires
        pt = _clean_point(ft.get("geometry"))
        if pt is None:
            continue
        lon, lat = pt
        dist = _haversine_km(lat0, lon0, lat, lon)
        if dist > radius_km:
            continue
        disc = _ms(p.get("FireDiscoveryDateTime"))
        out.append({
            "id": _clip(p.get("UniqueFireIdentifier"), 40)
            or f"{_clip(p.get('IncidentName'), 40)}|{disc}",
            "name": _clip(p.get("IncidentName"), 60) or "unnamed",
            "acres": _num(p.get("IncidentSize")),
            "contained_pct": _num(p.get("PercentContained")),
            "discovered_ts": disc, "updated_ts": _ms(p.get("ModifiedOnDateTime_dt")),
            "out_ts": _ms(p.get("FireOutDateTime")),
            "contained_ts": _ms(p.get("ContainmentDateTime")),
            "controlled_ts": _ms(p.get("ControlDateTime")),
            "county": _clip(p.get("POOCounty"), 40), "state": _state_code(p.get("POOState")),
            "lat": round(lat, 4), "lon": round(lon, 4), "dist_km": round(dist, 1),
            "bearing": _cardinal(_bearing(lat0, lon0, lat, lon)),
            "on_map": _in_box(lon, lat, box),
        })
    out.sort(key=lambda f: f["dist_km"])
    return out[:100]


def parse_perimeters(body, site, box):
    lat0, lon0 = site
    out = []
    for ft in _arcgis_features(body):
        if not isinstance(ft, dict):
            continue
        p = ft.get("properties") if isinstance(ft.get("properties"), dict) else {}
        cat = str(p.get("attr_IncidentTypeCategory") or "WF").strip().upper()
        if cat != "WF":
            continue
        g = _clean_geometry(ft.get("geometry"))
        bb = _geometry_bbox(g)
        if g is None or not _bbox_intersects(bb, box):
            continue
        clat, clon = (bb[1] + bb[3]) / 2.0, (bb[0] + bb[2]) / 2.0
        name = _clip(p.get("poly_IncidentName"), 60) or "unnamed"
        out.append({
            "id": _clip(p.get("attr_UniqueFireIdentifier"), 40) or f"{name}|{_geo_hash(g)}",
            "name": name,
            "acres": _num(p.get("poly_GISAcres")) or _num(p.get("attr_IncidentSize")),
            "contained_pct": _num(p.get("attr_PercentContained")),
            "discovered_ts": _ms(p.get("attr_FireDiscoveryDateTime")),
            "updated_ts": (_ms(p.get("attr_ModifiedOnDateTime_dt"))
                           or _ms(p.get("poly_DateCurrent"))),
            "out_ts": _ms(p.get("attr_FireOutDateTime")),
            "contained_ts": _ms(p.get("attr_ContainmentDateTime")),
            "controlled_ts": _ms(p.get("attr_ControlDateTime")),
            "at_site": _point_in_geometry(lon0, lat0, g),
            "dist_km": round(_haversine_km(lat0, lon0, clat, clon), 1),
            "bearing": _cardinal(_bearing(lat0, lon0, clat, clon)),
            "geometry": g,
        })
    out.sort(key=lambda f: f["dist_km"])
    return out[:PERIMETER_MAX]


def _spc_time(iso, raw):
    t = _parse_iso(iso)
    if t is None and isinstance(raw, str) and re.fullmatch(r"\d{12}", raw.strip()):
        t = datetime.strptime(raw.strip(), "%Y%m%d%H%M").replace(
            tzinfo=timezone.utc).timestamp()
    return t


def parse_spc_outlook(body, site, box):
    """SPC Day-1 categorical -> {"valid_ts","expire_ts","issue_ts","areas": [...]} with
    only the category areas touching the map (the site's own area always touches it).
    The .nolyr files are NON-layered: each category's polygon has holes where a higher
    one sits, so the containing polygon (holes honoured) IS the site's category."""
    doc = _json(body)
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise ValueError("SPC outlook: no 'features' list")
    lat0, lon0 = site
    meta = {"valid_ts": None, "expire_ts": None, "issue_ts": None}
    areas = []
    for ft in feats:
        if not isinstance(ft, dict):
            continue
        p = ft.get("properties") if isinstance(ft.get("properties"), dict) else {}
        for key, iso_k, raw_k in (("valid_ts", "VALID_ISO", "VALID"),
                                  ("expire_ts", "EXPIRE_ISO", "EXPIRE"),
                                  ("issue_ts", "ISSUE_ISO", "ISSUE")):
            if meta[key] is None:
                meta[key] = _spc_time(p.get(iso_k), p.get(raw_k))
        label = str(p.get("LABEL") or "").strip().upper()
        if label not in SPC_CATEGORIES:
            continue          # the "no areas" placeholder features carry other labels
        g = _clean_geometry(ft.get("geometry"))
        if g is None or not _bbox_intersects(_geometry_bbox(g), box):
            continue
        areas.append({"label": label, "at_site": _point_in_geometry(lon0, lat0, g),
                      "geometry": g})
    return dict(meta, areas=areas)


def parse_mds(body, site, box):
    doc = _json(body)
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise ValueError("SPC MD feed: no 'features' list")
    lat0, lon0 = site
    out = []
    for ft in feats:
        if not isinstance(ft, dict):
            continue
        p = ft.get("properties") if isinstance(ft.get("properties"), dict) else {}
        g = _clean_geometry(ft.get("geometry"))
        if g is None or not _bbox_intersects(_geometry_bbox(g), box):
            continue
        num = _num(p.get("num"))
        issue, expire = _parse_iso(p.get("issue")), _parse_iso(p.get("expire"))
        if num is None or expire is None:
            continue
        year = _num(p.get("year"))
        if year is None and issue is not None:
            year = datetime.fromtimestamp(issue, timezone.utc).year
        out.append({
            "num": int(num), "year": int(year) if year else None,
            "issue_ts": issue, "expire_ts": expire,
            "concerning": _clip(p.get("concerning"), 120),
            "watch_confidence": _num(p.get("watch_confidence")),
            "at_site": _point_in_geometry(lon0, lat0, g), "geometry": g,
        })
    out.sort(key=lambda m: -m["num"])
    return out[:20]


def _norm(s):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", str(s or "").lower()).split())


def _lsr_kind(typetext):
    t = typetext.upper()
    for kind, words in (("tornado", ("TORNADO", "FUNNEL", "LANDSPOUT", "WATERSPOUT",
                                     "WALL CLOUD")),
                        ("hail", ("HAIL",)),
                        ("flood", ("FLOOD",)),
                        ("dust", ("DUST",)),
                        ("fire", ("FIRE",)),
                        ("wind", ("WND", "WIND", "GST", "GUST", "DOWNBURST")),
                        ("winter", ("SNOW", "SLEET", "ICE", "FREEZING", "BLIZZARD")),
                        ("lightning", ("LIGHTNING",)),
                        ("rain", ("RAIN",))):
        if any(w in t for w in words):
            return kind
    return "other"


_LSR_UNITS = {"inch": "in", "inches": "in", "mph": "mph", "knots": "kt", "kt": "kt",
              "f": "°F", "feet": "ft", "ft": "ft", "mile": "mi", "miles": "mi"}
# IEM prefixes of re-issued reports: "Corrects previous hail report from 2 ESE Tatum.",
# "Corrects location of previous tornado report from 5 N Levelland." (seen 2025-06-05),
# and "Report duplicated with WFO ABQ." (a neighbouring office's copy).
_LSR_CORRECTION = re.compile(r"^\s*correct(?:s|ed|ion)\b[^.]*\.\s*", re.I)
_LSR_CORRECTS_WHAT = re.compile(
    r"\bprevious\s+(?P<type>.+?)\s+report\s+from\s+(?P<place>.+?)\s*\.?\s*$", re.I)
_LSR_DUPLICATE = re.compile(r"^\s*report duplicated with wfo\s+\w+\s*\.\s*", re.I)
_LSR_NEAR_DEG = 0.05
_LSR_CORRECTION_WINDOW = 48 * 3600


def _lsr_near(a, b, tol=_LSR_NEAR_DEG):
    return abs(a["lat"] - b["lat"]) <= tol and abs(a["lon"] - b["lon"]) <= tol


def _fold_lsr_reissues(recs):
    """Apply re-issues in product order: an exact repeat (a summary re-send) or a
    correction REPLACES the report it re-issues, and another office's duplicate is
    dropped — so one hailstone is listed once, at its corrected size and place."""
    recs = sorted(recs, key=lambda r: (r["_product"], r["time_ts"]))
    kept = []
    for r in recs:
        remark = r["remark"] or ""
        dup = _LSR_DUPLICATE.match(remark)
        corr = None if dup else _LSR_CORRECTION.match(remark)
        same = [o for o in kept if o["type"] == r["type"] and o["time_ts"] == r["time_ts"]
                and _lsr_near(o, r, 0.01) and o["magnitude"] == r["magnitude"]]
        if dup:
            if same:
                continue                    # the originating office's copy stays
        elif same:
            kept.remove(same[-1])           # a verbatim re-send: the later copy stays
        elif corr:
            w = _LSR_CORRECTS_WHAT.search(corr.group(0))
            ptype = _norm(w.group("type")) if w else ""
            pplace = _norm(w.group("place")) if w else ""
            want = ptype if ptype == _norm(r["type"]) else _norm(r["type"])
            cands = [o for o in kept if _norm(o["type"]) == want
                     and abs(o["time_ts"] - r["time_ts"]) <= _LSR_CORRECTION_WINDOW
                     and ((pplace and _norm(o["place"]) == pplace) or _lsr_near(o, r))]
            if cands:
                cands.sort(key=lambda o: abs(o["time_ts"] - r["time_ts"]))
                kept.remove(cands[0])
        kept.append(r)
    return kept


def parse_lsr(body, site, box):
    doc = _json(body)
    feats = doc.get("features") if isinstance(doc, dict) else None
    if not isinstance(feats, list):
        raise ValueError("LSR feed: no 'features' list")
    lat0, lon0 = site
    recs = []
    for ft in feats:
        if not isinstance(ft, dict):
            continue
        p = ft.get("properties") if isinstance(ft.get("properties"), dict) else {}
        pt = _clean_point(ft.get("geometry"), p)
        t = _parse_iso(p.get("valid"))
        typetext = _clip(p.get("typetext"), 40).upper()
        if pt is None or t is None or not typetext:
            continue
        lon, lat = pt
        mag = _num(p.get("magnitude"))
        if mag is None:
            mag = _num(p.get("magf"))
        recs.append({
            "type": typetext, "kind": _lsr_kind(typetext), "magnitude": mag,
            "unit": _clip(p.get("unit"), 12), "qualifier": _clip(p.get("qualifier"), 2),
            "place": _clip(p.get("city"), 60), "county": _clip(p.get("county"), 40),
            "state": _clip(p.get("state") or p.get("st"), 4).upper(),
            "time_ts": t, "remark": _clip(p.get("remark"), 300),
            "source": _clip(p.get("source"), 40), "wfo": _clip(p.get("wfo"), 4).upper(),
            "lat": round(lat, 4), "lon": round(lon, 4),
            "_product": str(p.get("product_id") or ""),
        })
    kept = []
    for r in _fold_lsr_reissues(recs):
        if not _in_box(r["lon"], r["lat"], box):
            continue
        r = dict(r)
        r.pop("_product", None)
        r["dist_km"] = round(_haversine_km(lat0, lon0, r["lat"], r["lon"]), 1)
        r["bearing"] = _cardinal(_bearing(lat0, lon0, r["lat"], r["lon"]))
        ident = f"{r['type']}|{r['time_ts']}|{r['lat']}|{r['lon']}|{r['magnitude']}"
        r["id"] = hashlib.sha1(ident.encode()).hexdigest()[:12]
        kept.append(r)
    kept.sort(key=lambda r: -r["time_ts"])
    return kept[:LSR_MAX_KEEP]


# ---- the poller -------------------------------------------------------------------
class _Feed:
    __slots__ = ("name", "label", "source", "coverage", "last_attempt", "last_ok",
                 "error", "data", "site", "cond")

    def __init__(self, name, label, source, coverage):
        self.name, self.label, self.source, self.coverage = name, label, source, coverage
        self.last_attempt = None     # epoch of the last attempt (success or failure)
        self.last_ok = None          # epoch of the last success (a 200 or a 304)
        self.error = None            # the last attempt's error; None after a success
        self.data = None             # parsed doc (never mutated after assignment)
        self.site = None             # the site the doc was computed for
        self.cond = {}               # url -> {"etag","lm","doc","key"} (poll thread only)


class HazardFeedsPoller:
    """Background-pollable holder of the non-NWS hazard information. INFORMATION ONLY."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._feeds = {name: _Feed(name, label, src, cov) for name, label, src, cov in FEEDS}

    # -- cadence ---------------------------------------------------------------
    def _interval(self, name):
        base = int(_cfgv(self.cfg, "HAZARD_FEEDS_POLL_SEC", 600))
        return max(base, FEED_MIN_INTERVAL.get(name, 0))

    def _stale_after(self, name):
        # like the NWS forecast: silent for two poll intervals => no longer "current"
        return max(2 * self._interval(name) + 120,
                   int(_cfgv(self.cfg, "HAZARD_STALE_AFTER_SEC", 600)))

    def _site(self):
        lat, lon = self.cfg.GEOCODE
        return (float(lat), float(lon))

    def _due(self, f, now, site):
        if f.last_attempt is None:
            return True
        if f.site is not None and f.site != site:
            return True                     # the site moved (GPS adoption): re-derive
        elapsed = now - f.last_attempt
        if elapsed < 0:
            return True                     # clock stepped back past the last attempt
        wait = self._interval(f.name)
        if f.error is not None:
            wait = min(wait, FAIL_RETRY_SEC)
        return elapsed >= wait

    def maybe_poll(self, now=None):
        """Poll every feed whose cadence has elapsed (each independently). Slow network
        I/O happens here, in the caller's thread, never under the lock. Returns
        {feed: ok} for the feeds polled, or None when nothing was due / disabled."""
        if now is None:
            now = time.time()
        if not _enabled(self.cfg):
            return None
        site = self._site()
        with self._lock:
            due = [n for n in FEED_NAMES if self._due(self._feeds[n], now, site)]
        if not due:
            return None
        return {n: self._poll_feed(n, now, site) for n in due}

    def poll_now(self, now=None):
        """Poll every feed now, regardless of cadence. Returns {feed: ok}."""
        if now is None:
            now = time.time()
        site = self._site()
        return {n: self._poll_feed(n, now, site) for n in FEED_NAMES}

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """Everything fetched under the wrong clock is suspect (the HMS file name and the
        LSR window are date-derived; the SPC/MD validity checks ran against the wrong
        time): withdraw it as not-current and re-poll every feed at once. The validators
        stay — they are the server's, so an unchanged file still costs only a 304."""
        with self._lock:
            for f in self._feeds.values():
                f.last_attempt = None
                f.last_ok = None

    def _poll_feed(self, name, now, site):
        f = self._feeds[name]
        box = _site_box(self.cfg, site)
        try:
            if not _in_box(site[1], site[0], f.coverage):
                raise ValueError(f"site {site[0]:.3f},{site[1]:.3f} is outside this "
                                 f"feed's coverage")
            doc = getattr(self, "_fetch_" + name)(f, now, site, box)
            err = None
        except Exception as e:  # noqa: BLE001 — a feed failure is data, never a crash
            doc, err = None, _errstr(e)
        with self._lock:
            prev_err, first = f.error, f.last_ok is None
            f.last_attempt = now
            f.error = err
            if err is None:
                f.data, f.last_ok, f.site = doc, now, site
        # log transitions only: journald is persistent (on the SD card) and a feed that
        # stays down must not write a line every few minutes
        if err is None and (prev_err is not None or first):
            log.info("hazard feed %s: ok", name)
        elif err is not None and err != prev_err:
            log.warning("hazard feed %s failed: %s", name, err)
        return err is None

    def _ua(self):
        return _cfgv(self.cfg, "NWS_USER_AGENT", "ttu-safety-monitor")

    def _cond_fetch(self, f, url, parse, key, max_bytes=MAX_BODY_BYTES,
                    accept="application/geo+json, application/json;q=0.9"):
        """Conditional GET + parse. A 304 re-uses the doc parsed from the body those
        validators came with; the key (site/map box) guards against re-using a doc
        derived for another site."""
        prev = f.cond.get(url)
        usable = prev is not None and prev["key"] == key
        status, body, etag, lm = _get(url, self._ua(),
                                      etag=prev["etag"] if usable else None,
                                      last_modified=prev["lm"] if usable else None,
                                      max_bytes=max_bytes, accept=accept)
        if status == 304:
            if not usable:
                raise ValueError("unexpected 304 Not Modified (no cached copy)")
            return prev["doc"]
        doc = parse(body)
        f.cond.pop(url, None)
        if etag or lm:
            f.cond[url] = {"etag": etag, "lm": lm, "doc": doc, "key": key}
            while len(f.cond) > 2:              # HMS keeps today + yesterday at most
                f.cond.pop(next(iter(f.cond)))
        return doc

    # -- per-feed fetchers (network; poll thread only) ---------------------------
    def _fetch_smoke(self, f, now, site, box):
        day = datetime.fromtimestamp(now, timezone.utc).date()
        accept = "application/vnd.google-earth.kml+xml, application/xml;q=0.9, */*;q=0.5"
        # Today's file (UTC) appears with the day's first daytime analysis (~12-16 UTC);
        # until then — i.e. through the local evening and night — yesterday's is the
        # latest analysis ("smoke observed this afternoon").
        for d in (day, day - timedelta(days=1)):
            url = HMS_SMOKE_KML.format(d=d)
            try:
                return self._cond_fetch(
                    f, url, lambda b, d=d: parse_hms_kml(b, site, box, d.isoformat()),
                    (site, box), max_bytes=HMS_MAX_BODY_BYTES, accept=accept)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        raise ValueError(f"no HMS smoke file for {day} or the day before (404)")

    def _fetch_fires(self, f, now, site, box):
        radius = max(FIRE_RADIUS_KM, _box_radius_km(site[0], site[1], box))
        q = {"where": "IncidentTypeCategory='WF' AND FireOutDateTime IS NULL",
             "geometry": f"{site[1]:.4f},{site[0]:.4f}",
             "geometryType": "esriGeometryPoint", "inSR": "4326",
             "spatialRel": "esriSpatialRelIntersects", "distance": f"{radius:.0f}",
             "units": "esriSRUnit_Kilometer", "outFields": ",".join(FIRE_FIELDS),
             "outSR": "4326", "geometryPrecision": "4", "f": "geojson"}
        url = WFIGS_INCIDENTS + "?" + urllib.parse.urlencode(q)
        return self._cond_fetch(f, url, lambda b: parse_fires(b, site, box, radius),
                                (site, box))

    def _fetch_fire_perimeters(self, f, now, site, box):
        # server-side: map-box intersection, generalised to ~200 m (a 440 px map shows
        # ~0.5 km per pixel) — a perimeter's full-resolution outline is 100s of KB. Fully
        # contained / out fires are dropped here already (42 of 155 WF perimeters on
        # 2026-09-24 were 100 % contained); staleness is judged at view time.
        q = {"where": ("attr_IncidentTypeCategory='WF' AND attr_FireOutDateTime IS NULL AND "
                       "(attr_PercentContained IS NULL OR attr_PercentContained < 100)"),
             "geometry": ",".join(f"{v:.4f}" for v in box),
             "geometryType": "esriGeometryEnvelope", "inSR": "4326",
             "spatialRel": "esriSpatialRelIntersects",
             "outFields": ",".join(PERIMETER_FIELDS), "outSR": "4326",
             "geometryPrecision": "4", "maxAllowableOffset": "0.002",
             "resultRecordCount": str(PERIMETER_MAX), "f": "geojson"}
        url = WFIGS_PERIMETERS + "?" + urllib.parse.urlencode(q)
        return self._cond_fetch(f, url, lambda b: parse_perimeters(b, site, box),
                                (site, box))

    def _fetch_spc_outlook(self, f, now, site, box):
        return self._cond_fetch(f, SPC_DAY1_CAT, lambda b: parse_spc_outlook(b, site, box),
                                (site, box))

    def _fetch_spc_md(self, f, now, site, box):
        url = IEM_SPC_MCD + "?" + urllib.parse.urlencode({"hours": MD_LOOKBACK_H})
        _status, body, _e, _l = _get(url, self._ua())
        return parse_mds(body, site, box)

    def _fetch_lsr(self, f, now, site, box):
        hours = int(_cfgv(self.cfg, "HAZARD_LSR_HOURS", 24))
        begin = datetime.fromtimestamp(now - hours * 3600, timezone.utc)
        end = datetime.fromtimestamp(now + 600, timezone.utc)
        miles = _box_radius_km(site[0], site[1], box) / 1.609344 + 1.0
        q = {"lat": f"{site[0]:.4f}", "lon": f"{site[1]:.4f}",
             "radius_miles": f"{miles:.0f}",
             "begints": begin.strftime("%Y-%m-%dT%H:%MZ"),
             "endts": end.strftime("%Y-%m-%dT%H:%MZ")}
        url = IEM_LSR_BY_POINT + "?" + urllib.parse.urlencode(q)
        _status, body, _e, _l = _get(url, self._ua())
        return parse_lsr(body, site, box)

    # -- views (cheap, time-dependent; called by the evaluator and radar threads) -------
    def _snapshot(self):
        with self._lock:
            return {n: (f.data, f.last_ok, f.last_attempt, f.error, f.site)
                    for n, f in self._feeds.items()}

    def _views(self, now):
        cfg = self.cfg
        tzname = _cfgv(cfg, "LOCAL_TZ", "UTC")
        site = self._site()
        snap = self._snapshot()
        enabled = _enabled(cfg)
        feeds, docs = {}, {}
        for name, label, src, _cov in FEEDS:
            data, last_ok, last_attempt, error, fsite = snap[name]
            age = None if last_ok is None else now - last_ok
            # a success "in the future" (backward clock step) has no meaningful age
            fresh = (enabled and age is not None and -60 <= age <= self._stale_after(name)
                     and fsite == site)
            if age is not None:
                age = max(0, round(age)) if age >= -60 else None
            if not enabled:
                err = "disabled"
            elif fresh or error:
                err = error
            elif last_ok is None:
                err = "no data yet" if last_attempt is None else "no successful update yet"
            else:
                err = "stale: last update " + (f"{age // 60} min ago" if age is not None
                                               else "at an unknown time (clock step)")
            feeds[name] = {"ok": bool(fresh), "error": err, "age_s": age, "count": 0,
                           "label": label, "source": src,
                           "interval_s": self._interval(name)}
            if fresh:
                docs[name] = data
        # totals = how many items each list would have WITHOUT the MAX_ITEMS display cap,
        # so the page can say "+N more" instead of passing the cap off as the count
        out = {"feeds": feeds, "overlays": [], "totals": {}}
        self._view_smoke(out, docs.get("smoke"), tzname)
        self._view_fires(out, docs.get("fires"), docs.get("fire_perimeters"), tzname, now,
                         "fires" in docs, "fire_perimeters" in docs)
        self._view_spc(out, docs.get("spc_outlook"), docs.get("spc_md"), tzname, now,
                       "spc_outlook" in docs)
        self._view_lsr(out, docs.get("lsr"), tzname, now)
        return out

    def _view_smoke(self, out, doc, tzname):
        if doc is None:
            out["smoke"] = None
            return
        polys = doc["polys"]
        # the file's latest analysis window (parse_hms_kml, over ALL placemarks — not just
        # the ones near the site); derived from the map's polygons only as a fallback
        latest = doc.get("latest_window")
        if latest is None:
            timed = [p for p in polys if p["end_ts"] is not None]
            if timed:
                lend = max(p["end_ts"] for p in timed)
                latest = (min(p["start_ts"] or lend for p in timed if p["end_ts"] == lend),
                          lend)
        if latest is not None:
            lstart, lend = latest
            # the map shows the LATEST analysis here: polygons overlapping its window.
            # Earlier windows of the same day overlap each other and would only
            # stack into a grey wash that says nothing about "now".
            current = [p for p in polys if p["end_ts"] is not None and p["end_ts"] > lstart
                       and (p["start_ts"] is None or p["start_ts"] < lend)]
        else:
            current = list(polys)
        at_site_any = [p for p in polys if p["at_site"]]
        at_site_now = [p for p in current if p["at_site"]]

        def densest(ps):
            ds = [p["density"] for p in ps if p["density"]]
            return max(ds, key=lambda d: _DENSITY_RANK.get(d, 0)) if ds else None

        windows = sorted({(p["start_ts"], p["end_ts"]) for p in at_site_any
                          if p["start_ts"] is not None and p["end_ts"] is not None})
        win_txt = [_fmt_window(s, e, tzname) for s, e in windows]
        latest_txt = _fmt_window(latest[0], latest[1], tzname) if latest else None
        day = doc.get("file_date") or "?"
        latest_s = latest_txt or "time n/a"
        if at_site_now:
            text = (f"{densest(at_site_now) or 'Some'} smoke over the site in the latest "
                    f"HMS analysis: {latest_s}")
        elif at_site_any:
            text = (f"{densest(at_site_any) or 'Some'} smoke was over the site earlier: "
                    f"{'; '.join(w for w in win_txt if w) or 'time n/a'} · not in the "
                    f"latest analysis: {latest_s}")
        elif current:
            n = len(current)
            text = (f"No smoke over the site · {n} smoke area{'' if n == 1 else 's'} on "
                    f"the map in the latest HMS analysis: {latest_s}")
        elif polys:
            text = (f"No smoke over the site or on the map in the latest HMS analysis: "
                    f"{latest_s} (smoke analysed near here earlier in the day)")
        else:
            text = f"No HMS smoke analysed near the site (HMS analysis of {day})"
        out["smoke"] = {
            "file_date": doc.get("file_date"), "site_in_smoke": bool(at_site_now),
            "site_in_smoke_today": bool(at_site_any), "density": densest(at_site_now),
            "density_today": densest(at_site_any), "windows_at_site": win_txt,
            "latest_window": latest_txt, "on_map": len(current), "text": text,
            "note": ("HMS smoke is analysed from daytime satellite imagery only; it is "
                     "smoke anywhere in the column (aloft), not surface air quality."),
        }
        out["feeds"]["smoke"]["count"] = len(current)
        for p in current:
            fill, alpha = SMOKE_FILL.get(p["density"], SMOKE_FILL["Light"])
            out["overlays"].append({
                "kind": "smoke",
                "key": f"smoke:{day}:{_int(p['start_ts'])}:{_int(p['end_ts'])}:{p['ghash']}",
                "geometry": p["geometry"], "label": f"{p['density'] or 'Some'} smoke",
                "density": p["density"], "rank": RANK["smoke"],
                "style": _style(fill=fill, fill_alpha=alpha, width=0)})

    def _view_fires(self, out, incidents, perimeters, tzname, now, inc_ok, per_ok):
        active = [i for i in (incidents or []) if _fire_active(i, now)]
        active_ids = {i["id"] for i in active}
        # a perimeter is shown when its incident is listed, or on its own when it passes
        # the same test (not out / contained, updated within FIRE_UPDATED_WITHIN_H):
        # WFIGS keeps weeks-old and fully contained perimeters "current"
        perims = [p for p in (perimeters or [])
                  if p["id"] in active_ids or _perimeter_active(p, now)]
        per_by_id = {p["id"]: p for p in perims}
        items = []
        for i in active:
            per = per_by_id.get(i["id"])
            acres = i["acres"]
            title = i["name"] if "fire" in i["name"].lower() else i["name"] + " fire"
            size = f"{acres:,.0f} ac" if acres is not None else "size n/a"
            cont = (f"{i['contained_pct']:.0f}% contained"
                    if i["contained_pct"] is not None else "containment n/a")
            where = ", ".join(x for x in (f"{i['county']} Co." if i["county"] else "",
                                          i["state"]) if x)
            disc = _fmt_time(i["discovered_ts"], tzname, now)
            text = (f"{title} · {size}, {cont}"
                    + (f" · {where}" if where else "")
                    + (f" · discovered {disc}" if disc else "")
                    + f" · {i['dist_km']:.0f} km {i['bearing']} of the site")
            if per is not None and per["at_site"]:
                text += " · the site is INSIDE its perimeter"
            items.append({
                "id": i["id"], "kind": "incident", "name": i["name"], "acres": acres,
                "contained_pct": i["contained_pct"], "county": i["county"],
                "state": i["state"], "discovered_local": disc,
                "dist_km": i["dist_km"], "bearing": i["bearing"], "lat": i["lat"],
                "lon": i["lon"], "on_map": i["on_map"] or per is not None,
                "has_perimeter": per is not None, "text": text})
        listed = {i["id"] for i in active}
        for p in perims:
            if p["id"] in listed:
                continue
            # a current perimeter whose incident point is not listed (filtered as
            # old, or outside the radius): still on the map, so still in the text
            size = f"{p['acres']:,.0f} ac" if p["acres"] is not None else "size n/a"
            text = (f"{p['name']} fire perimeter · {size} · {p['dist_km']:.0f} km "
                    f"{p['bearing']} of the site")
            if p["at_site"]:
                text += " · the site is INSIDE this perimeter"
            items.append({
                "id": p["id"], "kind": "perimeter", "name": p["name"], "acres": p["acres"],
                "contained_pct": p["contained_pct"], "county": "", "state": "",
                "discovered_local": _fmt_time(p["discovered_ts"], tzname, now),
                "dist_km": p["dist_km"], "bearing": p["bearing"], "lat": None, "lon": None,
                "on_map": True, "has_perimeter": True, "text": text})
        items.sort(key=lambda x: x["dist_km"])
        out["fires"] = items[:MAX_ITEMS]
        out["totals"]["fires"] = len(items)
        if inc_ok:
            out["feeds"]["fires"]["count"] = len(active)
        if per_ok:
            out["feeds"]["fire_perimeters"]["count"] = len(perims)
        for p in perims:
            out["overlays"].append({
                "kind": "fire_perimeter", "key": f"perim:{p['id']}",
                "geometry": p["geometry"], "label": p["name"],
                "rank": RANK["fire_perimeter"],
                "style": _style(stroke=FIRE_COLOR, fill=FIRE_COLOR, fill_alpha=50, width=2)})
        for i in active:
            if not i["on_map"]:
                continue
            out["overlays"].append({
                "kind": "fire", "key": f"fire:{i['id']}",
                "geometry": _point_geometry(i["lon"], i["lat"]), "label": i["name"],
                "rank": RANK["fire"],
                "style": _style(fill=FIRE_COLOR, width=1, symbol="triangle", size=7)})

    def _view_spc(self, out, outlook, mds, tzname, now, outlook_ok):
        spc = {"ok": bool(outlook_ok), "category": None, "label": "unavailable",
               "text": None, "color": None, "valid_local": None, "expire_local": None,
               "issue_local": None, "on_map": [], "mds": []}
        if outlook is not None:
            valid, expire = outlook.get("valid_ts"), outlook.get("expire_ts")
            spc["valid_local"] = _fmt_time(valid, tzname, now)
            spc["expire_local"] = _fmt_time(expire, tzname, now)
            spc["issue_local"] = _fmt_time(outlook.get("issue_ts"), tzname, now)
            if expire is not None and now >= expire:
                # SPC always has a current Day 1; an expired one means the feed is stuck
                # (a 304 loop on a dead mirror, or a wrong clock) — never present it
                spc["label"] = f"no current Day 1 outlook (last one expired {spc['expire_local']})"
                spc["text"] = "SPC Day 1 outlook: " + spc["label"]
                areas = []
            else:
                areas = outlook.get("areas") or []
                at = [a for a in areas if a["at_site"]]
                best = max(at, key=lambda a: SPC_CATEGORIES[a["label"]][0]) if at else None
                window = (f"valid {spc['valid_local']} – {spc['expire_local']}"
                          if spc["valid_local"] and spc["expire_local"] else "")
                if best is not None:
                    _rank, name, stroke, _fill = SPC_CATEGORIES[best["label"]]
                    spc.update(category=best["label"], label=name, color=stroke)
                    spc["text"] = (f"SPC Day 1: {name} ({best['label']}) at the site"
                                   + (f" · {window}" if window else ""))
                else:
                    spc["label"] = "no thunderstorm or severe risk area at the site"
                    spc["text"] = ("SPC Day 1: no thunderstorm or severe risk area at the "
                                   "site" + (f" · {window}" if window else ""))
            drawn = [a for a in areas if SPC_CATEGORIES[a["label"]][0] >= SPC_DRAW_MIN_RANK]
            spc["on_map"] = sorted({a["label"] for a in drawn},
                                   key=lambda k: SPC_CATEGORIES[k][0])
            out["feeds"]["spc_outlook"]["count"] = len(drawn)
            for a in drawn:
                rank, name, stroke, fill = SPC_CATEGORIES[a["label"]]
                out["overlays"].append({
                    "kind": "spc_outlook",
                    "key": f"spc:{a['label']}:{_int(outlook.get('issue_ts'))}",
                    "geometry": a["geometry"], "label": a["label"], "category": a["label"],
                    "rank": RANK["spc_outlook"] - rank,     # higher risk drawn later
                    "style": _style(stroke=stroke, fill=fill, fill_alpha=0, width=2,
                                    dash=True)})
        active = [m for m in (mds or []) if m["expire_ts"] > now
                  and (m["issue_ts"] is None or m["issue_ts"] <= now + 300)]
        for m in active:
            until = _fmt_time(m["expire_ts"], tzname, now)
            text = f"MD {m['num']}: {m['concerning'] or 'mesoscale discussion'}"
            if m["watch_confidence"] is not None:
                text += f" · watch probability {m['watch_confidence']:.0f}%"
            text += f" · until {until}" + (" · covers the site" if m["at_site"] else "")
            spc["mds"].append({
                "num": m["num"], "concerning": m["concerning"],
                "watch_confidence": m["watch_confidence"], "at_site": m["at_site"],
                "issue_local": _fmt_time(m["issue_ts"], tzname, now), "expire_local": until,
                "expire_ts": m["expire_ts"], "on_map": True, "text": text,
                "url": (SPC_MD_PAGE.format(year=m["year"], num=m["num"])
                        if m["year"] else None)})
            out["overlays"].append({
                "kind": "spc_md", "key": f"md:{m['year']}:{m['num']}",
                "geometry": m["geometry"], "label": f"MD {m['num']}",
                "rank": RANK["spc_md"],
                "style": _style(stroke=MD_COLOR, width=2, dash=True)})
        out["feeds"]["spc_md"]["count"] = len(active)
        out["spc"] = spc

    def _view_lsr(self, out, doc, tzname, now):
        hours = int(_cfgv(self.cfg, "HAZARD_LSR_HOURS", 24))
        recent = [r for r in (doc or []) if now - hours * 3600 <= r["time_ts"] <= now + 3600]
        items = []
        for r in recent[:MAX_ITEMS]:
            when = _fmt_time(r["time_ts"], tzname, now)
            mag = _lsr_magnitude(r)
            where = r["place"] or "place n/a"
            area = ", ".join(x for x in (f"{r['county']} Co." if r["county"] else "",
                                         r["state"]) if x)
            text = (f"{r['type']}{' ' + mag if mag else ''} · {where}"
                    + (f" ({area})" if area else "")
                    + f" · {when} · {r['dist_km']:.0f} km {r['bearing']} of the site")
            remark = _lsr_remark(r.get("remark"))
            if remark:
                text += f" · “{remark}”"
            items.append(dict(r, mag_text=mag, time_local=when, on_map=True, text=text))
        out["lsr"] = items
        out["feeds"]["lsr"]["count"] = out["totals"]["lsr"] = len(recent)
        for r in recent:
            symbol, color = LSR_KINDS.get(r["kind"], LSR_KINDS["other"])
            mag = _lsr_magnitude(r)
            out["overlays"].append({
                "kind": "lsr", "key": f"lsr:{r['id']}",
                "geometry": _point_geometry(r["lon"], r["lat"]),
                "label": r["type"] + (f" {mag}" if mag else ""), "lsr_kind": r["kind"],
                "typetext": r["type"], "rank": RANK["lsr"],
                "style": _style(fill=color, width=1, symbol=symbol, size=5)})

    # -- public views ----------------------------------------------------------
    def component(self, now=None):
        """The hazard-information component for the state file. ALWAYS safe: this layer
        is information only and must never be part of IsSafe."""
        if now is None:
            now = time.time()
        v = self._views(now)
        return {
            "safe": True, "info_only": True, "enabled": _enabled(self.cfg),
            "available": any(f["ok"] for f in v["feeds"].values()),
            "feeds": v["feeds"], "smoke": v["smoke"], "fires": v["fires"],
            "spc": v["spc"], "lsr": v["lsr"], "totals": v["totals"],
            "on_map": len(v["overlays"]), "source": SOURCE, "note": INFO_NOTE,
        }

    def overlays(self, now=None):
        """Map overlays for the radar thumbnail (fresh feeds only, touching the map):
        [{"kind", "key", "geometry" (GeoJSON), "label", "rank", "style", ...}]. Keys are
        stable while the data is unchanged, so the renderer's signature only changes
        when something on the map really did."""
        if now is None:
            now = time.time()
        return self._views(now)["overlays"]


def _fire_active(i, now):
    """WFIGS 'current' incident that is plausibly still burning (see FIRE_* above)."""
    if i["out_ts"] or i["contained_ts"] or i["controlled_ts"]:
        return False
    if i["contained_pct"] is not None and i["contained_pct"] >= 100:
        return False
    upd = i["updated_ts"] if i["updated_ts"] is not None else i["discovered_ts"]
    if upd is None or now - upd > FIRE_UPDATED_WITHIN_H * 3600:
        return False
    new = i["discovered_ts"] is not None and now - i["discovered_ts"] <= FIRE_NEW_WITHIN_D * 86400
    return new or (i["acres"] or 0) >= FIRE_LARGE_ACRES


LSR_REMARK_MAX = 120


def _lsr_remark(remark):
    """The spotter's remark for display: without IEM's bookkeeping prefix of a re-issued
    report ("Corrects previous hail report from ...", "Report duplicated with WFO ABQ.":
    the correction is already applied), clipped to one short line; '' when nothing is left."""
    s = _clip(remark, 300)
    s = _LSR_DUPLICATE.sub("", _LSR_CORRECTION.sub("", s, count=1), count=1).strip()
    return _clip(s, LSR_REMARK_MAX)


def _perimeter_active(p, now):
    """A WFIGS 'current' perimeter that is plausibly still burning: not out / contained /
    controlled / 100 %, and updated within FIRE_UPDATED_WITHIN_H (no date at all = old
    information, never presented as current)."""
    if p.get("out_ts") or p.get("contained_ts") or p.get("controlled_ts"):
        return False
    if p.get("contained_pct") is not None and p["contained_pct"] >= 100:
        return False
    upd = p.get("updated_ts") if p.get("updated_ts") is not None else p.get("discovered_ts")
    return upd is not None and now - upd <= FIRE_UPDATED_WITHIN_H * 3600


def _lsr_magnitude(r):
    """'1.50 in', '62 mph (est.)' — or '' when the report carries no magnitude."""
    m = r.get("magnitude")
    if m is None:
        return ""
    unit = _LSR_UNITS.get((r.get("unit") or "").strip().lower(), (r.get("unit") or "").lower())
    num = f"{m:.2f}" if unit == "in" and r.get("kind") == "hail" else f"{m:g}"
    txt = f"{num} {unit}".strip()
    if (r.get("qualifier") or "").upper() == "E":
        txt += " (est.)"
    return txt


def unavailable_component(cfg):
    """The component when the hazard feeds are disabled/absent (never affects IsSafe)."""
    feeds = {name: {"ok": False, "error": "disabled", "age_s": None, "count": 0,
                    "label": label, "source": src, "interval_s": None}
             for name, label, src, _cov in FEEDS}
    return {
        "safe": True, "info_only": True, "enabled": False, "available": False,
        "feeds": feeds, "smoke": None, "fires": [],
        "spc": {"ok": False, "category": None, "label": "unavailable", "text": None,
                "color": None, "valid_local": None, "expire_local": None,
                "issue_local": None, "on_map": [], "mds": []},
        "lsr": [], "totals": {}, "on_map": 0,
        "source": SOURCE + " (disabled)", "note": INFO_NOTE,
    }
