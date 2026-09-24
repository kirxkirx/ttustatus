"""MRMS radar component for the SafetyMonitor — a simple 30 km "any rain" ring.

Every poll (DAY AND NIGHT — free data, and daytime rain matters for the roof) it fetches
the latest MRMS composite-reflectivity frame
from the Iowa Environmental Mesonet (free, no key), and:
  * CHECK: declares unsafe if any echo >= RADAR_DBZ is within RADAR_TRIGGER_KM of the dome.
    Deliberately simple — no upwind/downwind logic, just a plain radius.
  * THUMBNAIL: renders a TTU-centered map with the radar overlaid, the trigger ring, and
    10 km / 10 mi scale bars, saved where the status page can load it. The basemap is
    built once and cached to disk (never re-downloaded every cycle), from the first source
    that works: a CARTO composite already cached on this Pi, CARTO tiles fetched with
    TTU_SAFETY_CARTO_KEY, OpenStreetMap tiles (the key-free backup), else a plain
    background — see the basemap block in config.py. The component reports which source
    each map uses, and the attribution line follows it.

A single frame never vetoes: an in-ring echo must repeat on RADAR_TRIGGER_AFTER (2)
CONSECUTIVE polls before it counts, because MRMS composites occasionally carry a
one-frame artefact (aircraft, anomalous propagation, clutter). The first, unconfirmed
frame is reported honestly — neither "rain" nor "no rain" — and starts no freeze.

Live check: unsafe while rain is within the ring, and for RADAR_LATCH_SEC (15 min) after
the LAST in-ring detection — a post-rain freeze that clear frames do NOT cancel, so the
roof does not reopen the instant a cell's edge leaves the ring, and a blind feed cannot
drop the veto either. The radar does NOT rely on the WU latch, which only arms when rain
reaches a nearby station, not for a ranged echo. Fail-safe: a fetch error or stale frame
with no recent detection => unavailable (no veto on its own). Needs Pillow; absent =>
radar disabled.

HAZARD OVERLAYS (display only): the thumbnails also show whatever the NWS-alerts and
hazard-feeds pollers hand over through RadarPoller(overlay_sources=[...overlays]) — drawn
over the radar and under the ring/crosshair/scale bars, information layers first (smoke,
SPC outline and mesoscale discussions, wildfires, storm reports), then the
NWS alert areas, warnings over watches and a vetoing alert last with a thicker outline.
Nothing drawn here feeds the verdict: the veto decision lives in nws_alerts.py. The last
radar frame is kept (reprojected, ~1 MB per map), so a new warning re-renders the maps
within one radar-loop pass instead of waiting up to 5 min for the next frame. With no
overlay on the map the PNG is pixel-identical to the pre-hazards thumbnail.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat
except Exception:                       # pragma: no cover - optional dep
    Image = ImageChops = ImageDraw = ImageFont = ImageStat = None

from . import config as _config

log = logging.getLogger("ttu.safety.radar")

ARCHIVE = ("https://mesonet.agron.iastate.edu/archive/data/"
           "%Y/%m/%d/GIS/mrms/lcref_%Y%m%d%H%M.png")
GRID_W, GRID_H, PX, UL_LON, UL_LAT = 7000, 3500, 0.01, -129.995, 54.995
UA = {"User-Agent": _config.NWS_USER_AGENT}   # one configurable contact UA for all fetches


def site_in_coverage(lat, lon, margin_deg=0.5):
    """MRMS is a CONUS product (lon -130..-60, lat 20..55). Outside it every pixel read
    is out of bounds and the ring scan would report an eternally 'clear' frame — so the
    component must refuse to run there, loudly, instead of being silently safe."""
    return (UL_LAT - GRID_H * PX + margin_deg < lat < UL_LAT - margin_deg
            and UL_LON + margin_deg < lon < UL_LON + GRID_W * PX - margin_deg)


def deps_available():
    return Image is not None


# ---- HTTP -----------------------------------------------------------------
class TileRefused(ValueError):
    """A tile reply that must never be drawn although it came with HTTP 200: OSM's
    "access blocked" tile (marked not cacheable) or CARTO's "API KEY REQUIRED" watermark
    (a keyed tile identical to its unkeyed twin)."""


def _get(url, timeout=25, refuse_uncacheable=False, fresh=False):
    """GET -> bytes. refuse_uncacheable (OSM and custom basemap tiles): a reply the
    server marks Cache-Control no-cache / no-store is refused (TileRefused) —
    OpenStreetMap sends its "Access blocked" tile that way, with HTTP 200 — so it is never
    pasted into a basemap, let alone into the basemap cached for good. fresh (the CARTO
    key check): asks every cache on the way for a fresh copy (Cache-Control/Pragma
    no-cache), so a proxy holding a pre-watermark copy of an unkeyed tile cannot make a
    rejected key look accepted. (CARTO's own CDN ignores it — checked 2026-09-24 — which
    is harmless: its unkeyed copies are all watermarked.)"""
    headers = dict(UA)
    if fresh:
        headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                timeout=timeout) as r:
        if refuse_uncacheable:
            cc = (r.headers.get("Cache-Control") or "").lower()
            if "no-cache" in cc or "no-store" in cc:
                raise TileRefused("tile server marked the tile not cacheable (%s): an "
                                  "'access blocked' tile?" % cc)
        return r.read()


# ---- geometry -------------------------------------------------------------
def latlon_to_px(lat, lon):
    return int(round((lon - UL_LON) / PX)), int(round((UL_LAT - lat) / PX))


def dest_point(lat, lon, dist_km, bearing_deg):
    br, p1, l1, dr = (math.radians(bearing_deg), math.radians(lat),
                      math.radians(lon), dist_km / 6371.0)
    p2 = math.asin(math.sin(p1) * math.cos(dr) + math.cos(p1) * math.sin(dr) * math.cos(br))
    l2 = l1 + math.atan2(math.sin(br) * math.sin(dr) * math.cos(p1),
                         math.cos(dr) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _deg2num(lat, lon, z):
    n = 2 ** z
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def _num2deg(xt, yt, z):
    n = 2 ** z
    return (math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yt / n)))), xt / n * 360 - 180)


def _dbz(idx):
    return None if idx == 255 else -32.0 + idx * 0.5


# ---- radar fetch + in-ring check --------------------------------------------
def latest_frame(max_back=8):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if now.minute % 2:
        now -= timedelta(minutes=1)
    for i in range(max_back):
        ts = now - timedelta(minutes=2 * i)
        try:
            req = urllib.request.Request(ts.strftime(ARCHIVE), method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    return ts, ts.strftime(ARCHIVE)
        except Exception:
            continue
    return None, None


def check_rain(cfg, img):
    """Scan the radar grid within RADAR_TRIGGER_KM of the dome. Returns (in_ring, nearest_km,
    count) — nearest_km is the closest echo >= RADAR_DBZ, or None if the ring is clear."""
    lat0, lon0 = cfg.GEOCODE
    px = img.load()
    coslat = math.cos(math.radians(lat0))
    dlat = cfg.RADAR_TRIGGER_KM / 111.0
    dlon = cfg.RADAR_TRIGGER_KM / (111.0 * coslat)
    c0, r0 = latlon_to_px(lat0, lon0)
    dc = int(dlon / PX) + 1
    dr = int(dlat / PX) + 1
    nearest = None
    count = 0
    for r in range(max(0, r0 - dr), min(GRID_H, r0 + dr + 1)):
        lat = UL_LAT - r * PX
        for c in range(max(0, c0 - dc), min(GRID_W, c0 + dc + 1)):
            v = px[c, r]
            v = v if isinstance(v, int) else v[0]
            d = _dbz(v)
            if d is None or d < cfg.RADAR_DBZ:
                continue
            lon = UL_LON + c * PX
            dist = math.hypot((lat - lat0) * 111.0, (lon - lon0) * 111.0 * coslat)
            if dist <= cfg.RADAR_TRIGGER_KM:
                count += 1
                if nearest is None or dist < nearest:
                    nearest = dist
    return (nearest is not None), (round(nearest, 1) if nearest is not None else None), count


# ---- thumbnail (tiles cached to disk) -------------------------------------
def _dbz_color(d):
    if d is None or d < 5:
        return None
    for thr, c in [(65, (255, 255, 255)), (55, (230, 0, 140)), (50, (190, 0, 0)),
                   (45, (230, 60, 20)), (40, (240, 170, 0)), (35, (230, 230, 0)),
                   (30, (0, 210, 0)), (20, (0, 150, 60)), (10, (3, 120, 170)), (5, (4, 63, 120))]:
        if d >= thr:
            return c
    return None


def _region(cfg):
    lat0, lon0 = cfg.GEOCODE
    h = cfg.RADAR_THUMB_HALF_DEG
    return lat0 - h, lat0 + h, lon0 - h, lon0 + h


def _cache_key(cfg, tile_url, inverted=False):
    """The cached composite's file-name hash. With inverted=False this is EXACTLY the
    formula the code has always used, so a CARTO composite cached before the key
    requirement (keyed by the key-free CARTO URL) is found again; a keyed CARTO build is
    stored under that same key-free name, so the key never reaches a file name."""
    s = (f"{cfg.GEOCODE}|{cfg.RADAR_THUMB_HALF_DEG}|{cfg.RADAR_THUMB_PX}|"
         f"{cfg.RADAR_TILE_ZOOM}|{tile_url}" + ("|inverted" if inverted else ""))
    return hashlib.md5(s.encode()).hexdigest()[:10]


def _geo_sig(cfg):
    """Everything the map's region and cached composite name depend on (bar the URL)."""
    return (cfg.GEOCODE, cfg.RADAR_THUMB_HALF_DEG, cfg.RADAR_THUMB_PX, cfg.RADAR_TILE_ZOOM)


# ---- basemap sources (the chain is described in config.py's basemap block) -----------
# Per map, first that works: carto-cached -> carto (keyed) -> osm -> none; a custom
# TTU_SAFETY_RADAR_TILE_URL(_DAY) takes CARTO's place as custom-cached -> custom.
BASEMAP_SOURCES = ("carto-cached", "carto", "custom-cached", "custom", "osm", "none")
BASEMAP_RETRY_SEC = 1800             # a missing/incomplete basemap is rebuilt this often
# A rejected CARTO key is not re-probed sooner; OSM standing in for a rejected key or a
# failed custom source is re-checked this often (for CARTO: 2 probe requests). A merely
# incomplete build (some tiles arrived) is retried after BASEMAP_RETRY_SEC instead.
CARTO_REJECT_MEMORY_SEC = 6 * 3600
# HTTP statuses of an unkeyed CARTO request that prove CARTO enforces the key: the keyed
# twin that DID come through is then a real tile. (408/429 prove nothing: a timeout, a
# rate limit.) Keyed requests answered 401/403 = the key refused outright.
_CARTO_KEY_REFUSED = (401, 403)
BASEMAP_TILE_TIMEOUT = 15            # s per tile request
BASEMAP_GIVE_UP_AFTER = 3            # failed tiles, none arrived: the source is down
BASEMAP_BG = (16, 20, 30)            # the plain background where no tile arrived
CARTO_CREDIT = "© OpenStreetMap contributors, © CARTO"
OSM_CREDIT = "© OpenStreetMap contributors"
CARTO_KEY_URL = "carto.com/basemaps/apikey"
# CARTO key verdicts of THIS process: sha256(key) -> (monotonic expiry, "rejected" |
# "unreachable", short reason for the page). Module-level so the night and day maps share
# one probe; in memory only (a restart re-probes: 2 requests), and never the key itself.
_CARTO_REJECTED: dict = {}
_CARTO_ERROR_LOGGED: set = set()     # key hashes whose rejection was logged at ERROR
_OSM_UA_WARNED: set = set()          # User-Agents already logged as blocked by OSM
# any key-like query parameter, for URLs whose key did not come from CARTO_API_KEY (a
# custom template with its key written in)
_KEY_PARAM_RE = re.compile(r"([?&](?:api_?key|key|token|access_token)=)[^&#\s'\"]+",
                           re.IGNORECASE)


def redact(text, cfg=None, keys=()) -> str:
    """``text`` with the CARTO key (raw and URL-encoded), any further ``keys`` (a key
    found in a tile URL) and any key= / apikey= / token= query value replaced by ***.
    Every log line that can carry a tile URL or a fetch error goes through this: the key
    is a secret, and logs end up in journald, bug reports and screenshots."""
    s = str(text)
    keys = {str(k or "").strip() for k in keys} | {
        str(getattr(c, "CARTO_API_KEY", "") or "").strip()
        for c in (cfg, _config) if c is not None}
    forms = set()
    for k in keys:
        if k:
            forms.update((k, urllib.parse.quote(k, safe="")))
    for form in sorted(forms, key=len, reverse=True):
        s = s.replace(form, "***")
    return _KEY_PARAM_RE.sub(r"\1***", s)


def _with_key(url, key):
    """The tile URL with CARTO's ?key= parameter (URL-encoded)."""
    return url + ("&" if "?" in url else "?") + "key=" + urllib.parse.quote(key, safe="")


def _key_id(key):
    return hashlib.sha256(key.encode()).hexdigest()


def carto_key(cfg) -> str:
    return str(getattr(cfg, "CARTO_API_KEY", "") or "").strip()


def _carto_memory(key, now=None):
    """(verdict, reason) while this process remembers the key failing its probe
    (CARTO_REJECT_MEMORY_SEC), else None."""
    if not key:
        return None
    kid = _key_id(key)
    hit = _CARTO_REJECTED.get(kid)
    if hit is None:
        return None
    if (time.monotonic() if now is None else now) >= hit[0]:
        _CARTO_REJECTED.pop(kid, None)
        return None
    return hit[1], (hit[2] if len(hit) > 2 else "")


def carto_rejection(key, now=None):
    """'rejected' / 'unreachable' while this process remembers the key failing its probe
    (CARTO_REJECT_MEMORY_SEC), else None."""
    hit = _carto_memory(key, now)
    return hit[0] if hit else None


def _osm_url(cfg):
    return getattr(cfg, "OSM_TILE_URL", _config.OSM_TILE_URL)


def basemap_mode(cfg) -> str:
    m = str(getattr(cfg, "RADAR_BASEMAP", "auto") or "auto").strip().lower()
    return m if m in ("auto", "carto", "osm") else "auto"


# A tile whose mean luminance is above this is "light" and, for the night map, gets its
# lightness inverted (decided PER TILE: a missing tile's dark canvas never tips a whole
# composite, and a dark tile set is left alone). Ported from the weather project.
LIGHT_TILE_LUMINANCE = 128.0


def invert_lightness(img):
    """A night basemap from light tiles: every channel becomes c + 255 - (max + min),
    which flips lightness but keeps hue and saturation (CSS invert(1) hue-rotate(180deg)):
    white land turns near-black, black labels white, blue water stays blue."""
    rgb = img.convert("RGB")
    r, g, b = rgb.split()
    hi = ImageChops.lighter(ImageChops.lighter(r, g), b)
    lo = ImageChops.darker(ImageChops.darker(r, g), b)
    pos = ImageChops.add(hi, lo, 1.0, -255)                 # max(0, hi + lo - 255)
    neg = ImageChops.subtract(ImageChops.invert(hi), lo)    # max(0, 255 - hi - lo)
    return Image.merge("RGB", [ImageChops.add(ImageChops.subtract(c, pos), neg)
                               for c in (r, g, b)])


def _mean_luminance(img) -> float:
    return float(ImageStat.Stat(img.convert("L")).mean[0])


# overlay colours per basemap theme (dark labels/lines are invisible on a light basemap).
# smoke = the HMS smoke veil (a light grey that still shows on the light basemap);
# mrgl = the SPC Marginal-risk outline, which SPC draws dark green (see _never_green);
# base = the basemap's typical colour, against which _casing_for judges an outline: the
# median pixel of the TTU maps from OpenStreetMap (its land colour, and the lightness-
# inverted twin at night). CARTO's medians — Dark Matter ~(9, 9, 9), Positron
# ~(250, 250, 248) — are close enough that the 3:1 casing rule holds against them too
# (test_radar_review_fixes checks every NWS colour against both basemaps).
_THEME = {
    "dark":  {"ink": (255, 255, 255), "stroke": (0, 0, 0), "ring": (90, 200, 255),
              "label": (120, 210, 255), "text": (200, 215, 235),
              "smoke": (215, 215, 215), "mrgl": (200, 185, 140), "base": (22, 19, 13)},
    "light": {"ink": (25, 30, 40), "stroke": (255, 255, 255), "ring": (10, 90, 200),
              "label": (10, 90, 200), "text": (40, 55, 75),
              "smoke": (140, 140, 140), "mrgl": (125, 110, 65), "base": (242, 239, 233)},
}
# An outline colour closer to the basemap than this (WCAG contrast ratio) gets the
# opposite ink as its casing (see Thumbnailer._casing_for).
CASING_MIN_CONTRAST = 3.0


def _rel_luminance(rgb):
    """WCAG relative luminance of an sRGB colour (0 = black, 1 = white)."""
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    """WCAG contrast ratio of two colours (1 = identical, 21 = black on white)."""
    la, lb = _rel_luminance(a), _rel_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _font(sz):
    for p in ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            pass
    return ImageFont.load_default()


# ---- hazard overlays: normalisation, draw order, signature (no PIL needed) ---------------
# Overlays come from nws_alerts.NwsAlertsPoller.overlays() ("alert") and
# hazard_feeds.HazardFeedsPoller.overlays() (the information kinds). Each is a dict with a
# GeoJSON "geometry" (or "lat"/"lon" for a point) plus, per kind:
#   alert        : "key", "event", "color" (#RRGGBB), "rank" (lower = drawn later/on top),
#                  "vetoes" (True = this alert holds the monitor UNSAFE: drawn last, thicker)
#   smoke        : density Light/Medium/Heavy        ("density", in the dict or its "style")
#   spc_outlook  : category MRGL/SLGT/ENH/MDT/HIGH   ("category"/"label"/"LABEL"/"DN")
#   lsr          : report type, IEM code or text     ("type"/"typetext"/"label")
#   spc_md, fire, fire_perimeter : geometry only
# The reader is deliberately liberal (top level or "style", several spellings): a missing
# hint degrades one symbol's look, never the map. Unknown kinds are ignored.
ALERT_OUTLINE_PX = 2          # NWS alert outline; the casing adds 1 px on each side
ALERT_VETO_OUTLINE_PX = 4     # an alert that vetoes the monitor: drawn last and thicker
INFO_OUTLINE_PX = 2           # SPC / MD / fire-perimeter outlines
FIRE_RGB = (255, 69, 0)       # orange-red: wildfire perimeters and incident triangles
MD_RGB = (147, 112, 219)      # SPC mesoscale discussion (dashed, finer than the outlook)
# SPC categorical OUTLINE colours as SPC / NOAA mapservices draw them (verified against
# the Day-1 categorical layer's renderer, 2026-09-24). TSTM is never drawn (MRGL and above
# only) and MRGL's dark green is replaced on the map by the theme's "mrgl" sand colour.
SPC_RGB = {"TSTM": (85, 187, 85), "MRGL": (0, 85, 0), "SLGT": (221, 170, 0),
           "ENH": (255, 102, 0), "MDT": (204, 0, 0), "HIGH": (204, 0, 204)}
_SPC_ORDER = {"MRGL": 1, "SLGT": 2, "ENH": 3, "MDT": 4, "HIGH": 5}      # low risk first
_SPC_WORDS = (("MARGINAL", "MRGL"), ("SLIGHT", "SLGT"), ("ENHANCED", "ENH"),
              ("MODERATE", "MDT"), ("HIGH", "HIGH"), ("THUNDER", "TSTM"), ("GENERAL", "TSTM"))
_SPC_DN = {2: "TSTM", 3: "MRGL", 4: "SLGT", 5: "ENH", 6: "MDT", 8: "HIGH"}
SMOKE_ALPHA = {"light": 40, "medium": 70, "heavy": 100}   # HMS density -> veil opacity
# Fallback alert colour by product type (the event name's last word) for a missing or
# invalid colour, and for a green one (_never_green). Same values as the NWS-chart fallback
# of the weather project's alerts.py, so an unknown new product still reads by its type.
KIND_RGB = {"warning": (208, 0, 0), "watch": (230, 184, 0), "advisory": (123, 104, 238),
            "statement": (255, 228, 181), "other": (128, 128, 128)}
_STATEMENT_SUFFIXES = ("statement", "outlook", "alert", "message", "forecast", "emergency")
# bottom -> top: information layers first, NWS alert areas last (contract C4)
_OVERLAY_LAYER = {"smoke": 0, "spc_outlook": 1, "spc_md": 2, "fire_perimeter": 3,
                  "fire": 4, "lsr": 5, "alert": 6}
_DENSITY_ORDER = {"light": 0, "medium": 1, "heavy": 2}
_GREEN_WARNED: set = set()    # events already logged for a green colour (log once each)
_BAD_WARNED: set = set()      # malformed overlays already logged (log once each)


def _num(x):
    """x as a finite float, else None (bools are not numbers here)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _hex_rgb(s):
    """'#RRGGBB' / 'RRGGBB' / '#RGB' -> (r, g, b), else None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    s = s[1:] if s.startswith("#") else s
    if re.fullmatch(r"[0-9A-Fa-f]{3}", s):
        s = "".join(ch * 2 for ch in s)
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        return None
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# Civil products whose name does not end in their EAS level (as nws_alerts.CIVIL_KINDS).
_CIVIL_KINDS = {"evacuation immediate": "warning", "civil emergency message": "warning",
                "local area emergency": "statement", "911 telephone outage": "statement"}


def _event_kind(event):
    """warning | watch | advisory | statement | other, from the event name's last word —
    the suffix NWS itself grades products by, so new product names need no table — except
    the civil products in _CIVIL_KINDS."""
    name = " ".join((event or "").split()).lower()
    if name in _CIVIL_KINDS:
        return _CIVIL_KINDS[name]
    for kind in ("warning", "watch", "advisory"):
        if name.endswith(kind):
            return kind
    return "statement" if name.endswith(_STATEMENT_SUFFIXES) else "other"


def _is_green(rgb):
    """Green-dominant hue (sea green, lime, spring green, chartreuse, SPC MRGL, ...).
    Cyan/turquoise/teal are not: their blue is as strong as their green."""
    r, g, b = rgb
    return g >= 64 and g - max(r, b) >= 40


def _never_green(rgb, fallback, what=""):
    """HAZARDS ARE NEVER DRAWN GREEN (the owner's rule): on this page green reads as "good",
    and it is the colour of the 15-35 dBZ echoes a hazard usually comes with, so a green
    outline would both reassure and vanish into the rain it warns about. The NWS chart does
    paint some products green (the flood family — recoloured red upstream in nws_alerts —
    and e.g. Evacuation Immediate), so the map enforces the rule itself, whatever the
    source sent."""
    if not _is_green(rgb):
        return rgb
    if what and what not in _GREEN_WARNED:
        _GREEN_WARNED.add(what)
        log.warning("hazard overlay %r arrived in a green colour %s — drawn in %s instead "
                    "(hazards are never drawn green)", what, rgb, fallback)
    return fallback


def _positions(coords, depth=0):
    """Every position of a GeoJSON coordinates array of any nesting depth."""
    if depth > 6 or not isinstance(coords, (list, tuple)) or not coords:
        return
    if isinstance(coords[0], (int, float)) and not isinstance(coords[0], bool):
        yield coords
        return
    for c in coords:
        yield from _positions(c, depth + 1)


def _geom_positions(geom, depth=0):
    if not isinstance(geom, dict) or depth > 4:
        return
    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries") or ():
            yield from _geom_positions(g, depth + 1)
    else:
        yield from _positions(geom.get("coordinates"))


def _geom_bbox(geom):
    """(lonmin, latmin, lonmax, latmax) of any GeoJSON geometry, None when it has no
    positions. Raises ValueError on a malformed or non-finite position — one bad vertex
    would otherwise poison the drawing (NaN pixels) or the signature."""
    box = None
    for p in _geom_positions(geom):
        if len(p) < 2:
            raise ValueError("position with fewer than 2 numbers")
        lon, lat = _num(p[0]), _num(p[1])
        if lon is None or lat is None:
            raise ValueError("non-finite coordinate %r" % (list(p[:2]),))
        if box is None:
            box = [lon, lat, lon, lat]
        else:
            box = [min(box[0], lon), min(box[1], lat), max(box[2], lon), max(box[3], lat)]
    return tuple(box) if box else None


def _iter_polygons(geom, depth=0):
    """Each polygon of a geometry as a list of rings (outer first, then holes)."""
    if not isinstance(geom, dict) or depth > 4:
        return
    t = geom.get("type")
    if t == "Polygon":
        rings = geom.get("coordinates") or []
        if rings and rings[0]:
            yield rings
    elif t == "MultiPolygon":
        for rings in geom.get("coordinates") or []:
            if rings and rings[0]:
                yield rings
    elif t == "GeometryCollection":
        for g in geom.get("geometries") or ():
            yield from _iter_polygons(g, depth + 1)


def _iter_lines(geom, depth=0):
    if not isinstance(geom, dict) or depth > 4:
        return
    t = geom.get("type")
    if t == "LineString":
        if geom.get("coordinates"):
            yield geom["coordinates"]
    elif t == "MultiLineString":
        for line in geom.get("coordinates") or []:
            if line:
                yield line
    elif t == "GeometryCollection":
        for g in geom.get("geometries") or ():
            yield from _iter_lines(g, depth + 1)


def _iter_points(geom, depth=0):
    if not isinstance(geom, dict) or depth > 4:
        return
    t = geom.get("type")
    if t == "Point":
        if geom.get("coordinates"):
            yield geom["coordinates"]
    elif t == "MultiPoint":
        for p in geom.get("coordinates") or []:
            if p:
                yield p
    elif t == "GeometryCollection":
        for g in geom.get("geometries") or ():
            yield from _iter_points(g, depth + 1)


def _hint(ov, style, *names):
    """First non-empty value among names, looked up in the overlay, then its style."""
    for src in (ov, style):
        for n in names:
            v = src.get(n)
            if v not in (None, ""):
                return v
    return None


def _spc_category(ov, style):
    """MRGL/SLGT/ENH/MDT/HIGH/TSTM from a category, label or DN hint; None if unreadable."""
    dn = _num(_hint(ov, style, "DN", "dn"))
    if dn is not None and int(dn) in _SPC_DN:
        return _SPC_DN[int(dn)]
    for n in ("category", "cat", "risk", "LABEL", "label", "LABEL2"):
        v = _hint(ov, style, n)
        if not isinstance(v, str):
            continue
        up = v.upper()
        for w in "".join(c if c.isalnum() else " " for c in up).split():
            if w in SPC_RGB:                    # the abbreviation as a whole word
                return w
        for word, cat in _SPC_WORDS:            # "Marginal Risk", "General Thunderstorms"
            if word in up:
                return cat
    return None


def _smoke_density(ov, style):
    for v in (_hint(ov, style, "density"), _hint(ov, style, "label")):
        if isinstance(v, str):
            low = v.lower()
            for d in ("heavy", "medium", "light"):
                if d in low:
                    return d
    return "light"


# IEM LSR type codes -> symbol (text types are matched by keyword in _lsr_symbol)
_LSR_CODES = {"T": "tornado", "C": "tornado", "W": "tornado", "H": "hail",
              "G": "wind", "D": "wind", "N": "wind", "O": "wind", "M": "wind", "4": "wind",
              "F": "flood", "E": "flood", "R": "flood", "2": "dust",
              "S": "winter", "s": "winter", "5": "winter", "Z": "winter", "B": "winter",
              "7": "winter"}
_LSR_WORDS = (("tornado", ("TORNADO", "FUNNEL", "WATERSPOUT", "LANDSPOUT")),
              ("hail", ("HAIL",)),
              ("dust", ("DUST",)),
              ("winter", ("SNOW", "SLEET", "FREEZ", "ICE", "BLIZZ", "CHILL")),
              ("flood", ("FLOOD", "RAIN")),
              ("wind", ("WND", "WIND", "GUST", "GST", "DMG", "DAMAGE")))


_LSR_SYMBOLS = ("tornado", "hail", "wind", "flood", "dust", "winter", "other")
_LSR_ALIASES = {"rain": "flood", "lightning": "other", "fire": "other"}   # hazard_feeds kinds


def _lsr_symbol(ov, style):
    for n in ("lsr_kind", "symbol", "type", "lsr_type", "typetext", "event", "label"):
        v = _hint(ov, style, n)
        if not isinstance(v, str) or not v.strip():
            continue
        v = v.strip()
        if v.lower() in _LSR_SYMBOLS:
            return v.lower()
        if v.lower() in _LSR_ALIASES:
            return _LSR_ALIASES[v.lower()]
        if n == "symbol":
            continue            # a shape name from the source's own styling, not a type
        if len(v) == 1:
            if v in _LSR_CODES:
                return _LSR_CODES[v]
            continue
        up = v.upper()
        for sym, words in _LSR_WORDS:
            if any(w in up for w in words):
                return sym
    return "other"


def _overlay_spec(ov):
    """One overlay dict -> the normalised drawing spec, or None when it is not drawable
    (unknown kind, no geometry, SPC TSTM)."""
    if not isinstance(ov, dict):
        return None
    kind = str(ov.get("kind") or "").strip().lower()
    if kind not in _OVERLAY_LAYER:
        return None
    style = ov.get("style") if isinstance(ov.get("style"), dict) else {}
    geom = ov.get("geometry")
    if not isinstance(geom, dict):
        lat, lon = _num(ov.get("lat")), _num(ov.get("lon"))
        if lat is None or lon is None:
            return None
        geom = {"type": "Point", "coordinates": [lon, lat]}
    spec = {"kind": kind, "key": str(ov.get("key") or ov.get("id") or ""), "geometry": geom}
    if kind == "alert":
        event = str(ov.get("event") or "")
        fallback = KIND_RGB[_event_kind(event)]
        rgb = _hex_rgb(ov.get("color")) or _hex_rgb(style.get("color")) or fallback
        spec["event"] = event
        spec["color"] = list(_never_green(rgb, fallback, event or spec["key"]))
        rank = _num(ov.get("rank"))
        spec["rank"] = rank if rank is not None else 99.0    # unranked: drawn first
        v = ov.get("vetoes")
        spec["vetoes"] = bool(v) if isinstance(v, (bool, int)) else False
    elif kind == "smoke":
        spec["density"] = _smoke_density(ov, style)
    elif kind == "spc_outlook":
        cat = _spc_category(ov, style)
        if cat == "TSTM":
            return None               # general thunder: outlines are MRGL and above only
        spec["category"] = cat or "?"  # unreadable: still drawn (neutral colour), not lost
    elif kind == "lsr":
        spec["symbol"] = _lsr_symbol(ov, style)
    return spec


def _spec_order(s):
    k = s["kind"]
    if k == "alert":
        # vetoing alerts last of all; then higher rank first, so a LOWER rank (warnings)
        # is painted later, on top of the watch/advisory that usually surrounds it
        sub = (1 if s["vetoes"] else 0, -s["rank"], s["event"], s["key"])
    elif k == "smoke":
        sub = (_DENSITY_ORDER.get(s["density"], 0),)
    elif k == "spc_outlook":
        sub = (_SPC_ORDER.get(s["category"], 0),)
    else:
        sub = ()
    # the digest breaks every remaining tie, so the same SET of overlays always draws in
    # the same order (identical PNG, stable signature) whatever order the sources used
    return (_OVERLAY_LAYER[k], sub, s["digest"])


def _prepare_overlays(cfg, overlays):
    """Normalise, drop what cannot touch the map, sort into draw order. Never raises: a
    malformed overlay is skipped with a warning and the rest are still drawn."""
    if not overlays:
        return []
    lat0, lon0 = cfg.GEOCODE
    h = cfg.RADAR_THUMB_HALF_DEG
    m = 0.1 * h           # a symbol centred just off the map still shows its edge
    box = (lon0 - h - m, lat0 - h - m, lon0 + h + m, lat0 + h + m)
    specs = []
    for ov in overlays:
        try:
            spec = _overlay_spec(ov)
            if spec is None:
                continue
            bb = _geom_bbox(spec["geometry"])
            if bb is None or bb[2] < box[0] or bb[0] > box[2] or bb[3] < box[1] or bb[1] > box[3]:
                continue       # nowhere near the map: not drawn, not in the signature
            spec["digest"] = hashlib.sha1(json.dumps(
                spec, sort_keys=True, separators=(",", ":"), allow_nan=False,
                default=str).encode()).hexdigest()
            specs.append(spec)
        except Exception as e:                  # one bad overlay, not the whole map
            what = (ov.get("kind"), ov.get("key")) if isinstance(ov, dict) else type(ov).__name__
            # logged once per overlay: the radar loop re-checks overlays every pass
            if str(what) not in _BAD_WARNED:
                if len(_BAD_WARNED) > 500:
                    _BAD_WARNED.clear()
                _BAD_WARNED.add(str(what))
                log.warning("hazard overlay skipped (%s): %r", e, what)
    specs.sort(key=_spec_order)
    return specs


def overlay_signature(cfg, overlays):
    """What the overlays would draw, as a short hash ("" = nothing on the map). Built from
    the normalised specs (keys, colours, veto flags, geometry...), so it changes exactly
    when the picture would, is independent of list order, and ignores geometry that does
    not reach the map."""
    return _signature(_prepare_overlays(cfg, overlays))


def _signature(specs):
    if not specs:
        return ""
    return hashlib.sha1("|".join(s["digest"] for s in specs).encode()).hexdigest()


def _vkey(p):
    return (round(float(p[0]), 3), round(float(p[1]), 3))


def _outer_vertices(geoms):
    """The rounded (~100 m) vertices of every outer ring of these geometries."""
    out = set()
    for g in geoms:
        for rings in _iter_polygons(g):
            out.update(_vkey(p) for p in rings[0])
    return out


def _on_shared(ring, shared, frac=0.8):
    """Does this ring lie on the shared outline (>= frac of its vertices on it)?"""
    if not ring:
        return False
    try:
        return sum(_vkey(p) in shared for p in ring) >= frac * len(ring)
    except (TypeError, ValueError, IndexError):
        return False


# ---- pixel-space polyline helpers (ported from the weather project's radar.py) --------
def _clip_segment(x0, y0, x1, y1, xmin, ymin, xmax, ymax):
    """Liang-Barsky: the parameter range (t0, t1) of the segment inside the rectangle,
    or None when no part of it is inside."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - xmin), (dx, xmax - x0), (-dy, y0 - ymin), (dy, ymax - y0)):
        if p == 0:
            if q < 0:
                return None             # parallel to this edge and outside it
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return t0, t1


def _clip_polyline(pts, xmin, ymin, xmax, ymax):
    """The parts of a pixel polyline inside the rectangle, each >= 2 points. Keeps a zone
    outline that runs hundreds of km off the map from costing (or confusing) the drawing."""
    pieces, cur = [], []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        tt = _clip_segment(ax, ay, bx, by, xmin, ymin, xmax, ymax)
        if tt is None:
            if cur:
                pieces.append(cur)
                cur = []
            continue
        t0, t1 = tt
        start = (ax + t0 * (bx - ax), ay + t0 * (by - ay))
        end = (ax + t1 * (bx - ax), ay + t1 * (by - ay))
        if t0 > 0 or not cur:           # entering the frame (or the very first segment)
            if cur:
                pieces.append(cur)
            cur = [start]
        cur.append(end)
        if t1 < 1:                      # leaving the frame
            pieces.append(cur)
            cur = []
    if cur:
        pieces.append(cur)
    return pieces


def _thin(pts, min_d=0.75):
    """Drop vertices closer than min_d px to the previous kept one (keeping both ends):
    zone outlines carry many vertices per output pixel, and wide lines through such
    clusters get ragged joints."""
    if len(pts) <= 2:
        return list(pts)
    out = [pts[0]]
    for p in pts[1:-1]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= min_d:
            out.append(p)
    last = pts[-1]
    if len(out) > 1 and math.hypot(last[0] - out[-1][0], last[1] - out[-1][1]) < min_d:
        out[-1] = last
    else:
        out.append(last)
    return out


def _dashes(pts, on, off):
    """Split a polyline into dashes of length `on` separated by gaps of `off` (the pattern
    runs on across vertices)."""
    out = []
    if len(pts) < 2 or on <= 0:
        return out
    cur = [pts[0]]
    drawing, left = True, float(on)
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        pos = 0.0
        while seg - pos > left:                 # a dash/gap boundary inside this segment
            pos += left
            f = pos / seg
            p = (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
            if drawing:
                cur.append(p)
                out.append(cur)
                cur = []
            else:
                cur = [p]
            drawing = not drawing
            left = float(on if drawing else off)
        left -= seg - pos
        if drawing:
            cur.append(b)
    if drawing and len(cur) >= 2:
        out.append(cur)
    return out


def _triangle(x, y, side, down=False):
    """Equilateral triangle with its centroid at (x, y), pointing up (or down)."""
    s = -1.0 if down else 1.0
    return [(x, y - s * side / math.sqrt(3.0)),
            (x + side / 2.0, y + s * side / (2.0 * math.sqrt(3.0))),
            (x - side / 2.0, y + s * side / (2.0 * math.sqrt(3.0)))]


class Thumbnailer:
    """Builds and caches a tile basemap once, then composites radar each cycle.

    Parametrized by tile URL / output path / theme so the same machinery makes both the
    night (dark) and day (light) maps. ``tile_url`` is the map's configured source: its
    theme's CARTO URL (the default), an OpenStreetMap URL, or a custom template (on a
    CARTO host still under the CARTO rules); the chain follows from it, by host, and from
    RADAR_BASEMAP (basemap_plan).
    """

    def __init__(self, cfg, tile_url, thumb_path, theme):
        self.cfg = cfg
        self.tile_url = tile_url
        self.thumb_path = thumb_path
        self.theme = theme if theme in _THEME else "dark"
        self.colors = _THEME[self.theme]
        self._basemap = None        # PIL RGB image at (_ox, _oy)
        self._tilebox = None        # (gx0, gy0, gx1, gy1, z) for the mercator mapping
        self._remap = None          # per-thumb-pixel -> radar palette index source (col,row)
        self._ox = cfg.RADAR_THUMB_PX
        self._oy = cfg.RADAR_THUMB_PX   # replaced with the true aspect in _build_basemap
        # The last frame, already reprojected onto this map (an RGBA layer, ~1 MB), and its
        # caption: a hazard-overlay change re-renders from it (rerender) without refetching
        # MRMS — and without holding the 24 MB CONUS frame in RAM between polls.
        self._frame = None
        # What the current basemap is, for the component / page / attribution: written
        # only by the radar thread (_use), read from the evaluator thread — each attribute
        # read ONCE there (_snap), as a rebuild may rewrite them between two reads.
        self.basemap_source = None      # one of BASEMAP_SOURCES; None = not built yet
        self.basemap_tiles = None       # (arrived, expected) of an incomplete build
        self.basemap_inverted = False   # light tiles lightness-inverted (the night OSM map)
        self.carto_state = None         # step 2 reached: no-key/accepted/rejected/unreachable
        self.carto_reason = ""          # a failed key check in short ("HTTP 403", ...)
        self.osm_state = None           # step 3 reached: used/refused/failed/skipped-ua
        self.basemap_why = ()           # (kind, arrived, expected) of better sources that
        #                                 failed: kind "carto" or "custom"
        self._basemap_retry_at = None   # monotonic time of the next rebuild; None = final
        self._built_for = None          # _geo_sig of the region the basemap was built for
        # the build in progress writes these; _use publishes them with the new basemap,
        # so the evaluator thread never sees a half-updated state during a rebuild
        self._pending_carto = None
        self._pending_carto_reason = ""
        self._pending_osm = None
        self._geo = None                # the region settings this build uses (_geometry)
        self._invert = False
        self._tiles = []
        self._canvas_size = (1, 1)

    @property
    def map_name(self):
        return "night" if self.theme == "dark" else "day"

    def _default_carto_url(self):
        cfg = self.cfg
        if self.theme == "dark":
            return getattr(cfg, "CARTO_TILE_URL", _config.CARTO_TILE_URL)
        return getattr(cfg, "CARTO_TILE_URL_DAY", _config.CARTO_TILE_URL_DAY)

    def basemap_plan(self):
        """How this map is built, as a dict: "chain" = the sources tried in order ("none"
        is implied at the end); "carto_url" = the key-free CARTO URL of its CARTO steps
        (None: they are not CARTO); "carto_key" = a key written into the configured URL
        (''); "osm_url" = the URL of its OpenStreetMap step; "osm_by" = the setting that
        made OpenStreetMap this map's CHOICE rather than the fallback (None).

        Classified by host (config.tile_host_kind), not by exact URL: EVERY CARTO URL
        (another subdomain or style, @2x, ...) is only ever fetched with the key and
        through the key check, and EVERY OpenStreetMap URL gets the User-Agent check and
        obeys RADAR_BASEMAP — a variant spelling must not slip past either."""
        cfg = self.cfg
        mode = basemap_mode(cfg)
        kind = _config.tile_host_kind(self.tile_url)
        var = "TTU_SAFETY_RADAR_TILE_URL" + ("" if self.theme == "dark" else "_DAY")
        plan = {"carto_url": None, "carto_key": "", "osm_url": _osm_url(cfg), "osm_by": None}
        if mode == "osm":
            # never CARTO, not even the cached maps; the map's own OSM URL if it has one
            plan.update(chain=["osm"], osm_by="TTU_SAFETY_RADAR_BASEMAP=osm")
            if kind == "osm":
                plan["osm_url"] = self.tile_url
            return plan
        if kind == "osm" and mode == "auto":
            # an OpenStreetMap URL configured for this map: OpenStreetMap only
            plan.update(chain=["osm"], osm_url=self.tile_url, osm_by=var)
            return plan
        if kind == "osm":
            # "carto" mode = never OpenStreetMap: the map's own CARTO chain instead
            # (config.py warns about the ignored URL at startup)
            url, key, label = self._default_carto_url(), "", "carto"
        elif kind == "carto":
            url, key = _config.split_key_param(self.tile_url)
            label = "carto" if url == self._default_carto_url() else "custom"
        else:
            url, key, label = None, "", "custom"
        chain = [label + "-cached", label] + (["osm"] if mode == "auto" else [])
        plan.update(chain=chain, carto_url=url, carto_key=key)
        return plan

    def basemap_chain(self):
        """The sources this map tries, in order ("none" is implied at the end)."""
        return list(self.basemap_plan()["chain"])

    def basemap_due(self):
        """A rebuild is due: its scheduled time has come, or the region changed since the
        map was built — a GPS position adopted after the first build (after every reboot:
        the inputs file lives in /dev/shm), whose own cached composite may well exist
        (see RadarPoller._retry_basemaps)."""
        at = self._basemap_retry_at
        if at is not None and time.monotonic() >= at:
            return True
        built = self._built_for
        return built is not None and built != _geo_sig(self.cfg)

    def _cache_path(self, step, plan=None):
        """The cached composite of a step. CARTO steps (a CARTO host) use the key-free
        CARTO URL and never "|inverted": exactly the name the Pi's pre-watermark
        composites carry (CARTO tiles are never inverted). Other sources add "|inverted"
        for a night map made from light tiles."""
        plan = plan or self.basemap_plan()
        geo = self._geo or self.cfg
        if step == "osm":
            name = _cache_key(geo, plan["osm_url"], self._invert)
        elif plan["carto_url"] is not None:
            name = _cache_key(geo, plan["carto_url"])
        else:
            name = _cache_key(geo, self.tile_url, self._invert)
        return os.path.join(self.cfg.RADAR_CACHE_DIR, f"basemap_{name}.png")

    # basemap (static): fetch tiles once, cache the composited image to disk
    def _build_basemap(self):
        cfg = self.cfg
        self._geometry()
        try:
            self._run_chain()
        except Exception as e:          # noqa: BLE001 - the map is never worth a crash
            log.error("radar %s basemap build failed (%s) — plain background, retried in "
                      "%d min", self.map_name, redact(e, cfg), BASEMAP_RETRY_SEC // 60)
            self._use(Image.new("RGB", (self._ox, self._oy), BASEMAP_BG), "none",
                      retry=BASEMAP_RETRY_SEC)

    def _geometry(self):
        """The map's tile box, output size and tile list (no network)."""
        cfg = self.cfg
        # ONE snapshot of the region settings for the whole build: the evaluator thread
        # may adopt a GPS position meanwhile, and a composite must never be cached under
        # a name that describes another region than the one it shows
        geo = types.SimpleNamespace(GEOCODE=cfg.GEOCODE,
                                    RADAR_THUMB_HALF_DEG=cfg.RADAR_THUMB_HALF_DEG,
                                    RADAR_THUMB_PX=cfg.RADAR_THUMB_PX,
                                    RADAR_TILE_ZOOM=cfg.RADAR_TILE_ZOOM)
        self._geo = geo
        z = geo.RADAR_TILE_ZOOM
        latmin, latmax, lonmin, lonmax = _region(geo)
        x0f, y0f = _deg2num(latmax, lonmin, z)
        x1f, y1f = _deg2num(latmin, lonmax, z)
        gx0, gy0, gx1, gy1 = int(x0f * 256), int(y0f * 256), int(x1f * 256), int(y1f * 256)
        box = (gx0, gy0, gx1, gy1, z)
        if self._tilebox is not None and self._tilebox != box:
            # a rebuild after the site moved: the reprojection and the kept frame belong
            # to the old map and must not be drawn on the new one
            self._remap = None
            self._frame = None
        self._tilebox = box
        # Non-square output matching the Mercator canvas aspect, so px/km is equal on both
        # axes: the trigger ring draws as a true circle and one scale bar is valid in every
        # direction (a square would stretch E-W by 1/cos(lat) ~ 1.2x here).
        cw, ch = gx1 - gx0, gy1 - gy0
        self._ox = geo.RADAR_THUMB_PX
        self._oy = max(1, round(self._ox * ch / cw))
        self._canvas_size = (cw, ch)
        self._tiles = [(tx, ty) for tx in range(int(x0f), int(x1f) + 1)
                       for ty in range(int(y0f), int(y1f) + 1)]
        self._invert = self.theme == "dark" and bool(getattr(cfg, "RADAR_TILE_DARK_INVERT",
                                                             False))

    def _run_chain(self):
        cfg = self.cfg
        plan = self.basemap_plan()
        z = self._tilebox[4]
        self._pending_carto = None
        self._pending_carto_reason = ""
        self._pending_osm = None
        why = []                # (kind, arrived, expected) of better sources that failed
        best = None             # the most complete partial build: (arrived, expected, src, img)
        retry = None            # a better source failed: re-check it after this many s

        def later(sec):
            nonlocal retry
            retry = sec if retry is None else min(retry, sec)

        for step in plan["chain"]:
            if step.endswith("-cached"):
                img = self._load_cache(self._cache_path(step, plan))
                if img is not None:
                    return self._use(img, step)             # final: never rebuilt
                continue
            if step == "osm":
                self._pending_osm = "used"
                img = self._load_cache(self._cache_path("osm", plan))
                if img is not None:
                    # a composite built earlier needs no request, whatever the UA says now
                    return self._use(img, "osm", retry=retry, why=why)
                ua = str(getattr(cfg, "NWS_USER_AGENT", "") or "")
                if _config.ua_has_placeholder(ua):
                    # OSM answers such a UA with its "access blocked" tile: asking would
                    # only earn the Pi a block. Warned on the page and /setup.
                    self._pending_osm = "skipped-ua"
                    if ua not in _OSM_UA_WARNED:
                        _OSM_UA_WARNED.add(ua)
                        log.warning("OpenStreetMap basemap NOT requested: TTU_SAFETY_NWS_UA=%r "
                                    "carries a placeholder contact, which OSM blocks — put a "
                                    "real e-mail address in it", redact(ua, cfg))
                    continue
                kind = "osm"
                built = self._build_tiles(self._plain_fetch(plan["osm_url"], z), "osm",
                                          self._invert)
            elif plan["carto_url"] is not None:
                # "carto", or "custom" on a CARTO host: only ever with the key, checked
                kind = "carto"
                built = self._build_carto(plan["carto_url"], plan["carto_key"], z)
                if built is None:
                    if self._pending_carto in ("rejected", "unreachable"):
                        later(CARTO_REJECT_MEMORY_SEC)
                    continue
            else:                                   # custom: fetched exactly as given
                kind = "custom"
                built = self._build_tiles(self._plain_fetch(self.tile_url, z), "custom",
                                          self._invert)
            img, n, expected, refused = built
            if n == expected and n > 0:
                # Only a COMPLETE basemap is cached — never a partial/blank one for good.
                self._save_cache(img, step, n, plan)
                return self._use(img, step, retry=retry, why=why)
            if kind == "osm":
                self._pending_osm = "refused" if refused else "failed"
            else:
                why.append((kind, n, expected))
                # Some tiles came (a timeout, a watermarked tile mid-build): transient, so
                # the better source is retried soon. None came: a dead or wrong source,
                # re-checked like a rejected key.
                later(BASEMAP_RETRY_SEC if n > 0 else CARTO_REJECT_MEMORY_SEC)
            if n > 0 and (best is None or n > best[0]):
                best = (n, expected, step, img)
        if best is not None:
            n, expected, step, img = best
            log.warning("radar %s basemap incomplete (%d/%d tiles, %s) — not cached; retried "
                        "in %d min", self.map_name, n, expected, step, BASEMAP_RETRY_SEC // 60)
            # the partial map shown says so itself (basemap_tiles): no "why" entry for it
            shown = "carto" if (step != "osm" and plan["carto_url"] is not None) else step
            return self._use(img, step, retry=BASEMAP_RETRY_SEC, tiles=(n, expected),
                             why=[w for w in why if w[0] != shown])
        # WARNING on the way in, INFO while it lasts: the retry runs every 30 min, and
        # journald on the SD card needs no identical warning each time
        log.log(logging.WARNING if self.basemap_source != "none" else logging.INFO,
                "radar %s basemap: no tile source worked (%s) — plain background, "
                "retried in %d min", self.map_name, " -> ".join(plan["chain"]),
                BASEMAP_RETRY_SEC // 60)
        return self._use(Image.new("RGB", (self._ox, self._oy), BASEMAP_BG), "none",
                         retry=BASEMAP_RETRY_SEC, why=why)

    @staticmethod
    def _plain_fetch(url, z):
        """The tile fetcher of an OSM or custom source: each tile as the template gives
        it; a reply marked not cacheable (OSM's "access blocked" tile) is refused."""
        return lambda tx, ty: _get(url.format(z=z, x=tx, y=ty), timeout=BASEMAP_TILE_TIMEOUT,
                                   refuse_uncacheable=True)

    def _build_carto(self, url, embedded_key, z):
        """Step 2: CARTO tiles with the key, once a probe tile shows CARTO accepts it —
        and every other tile checked against its unkeyed twin as it comes (_carto_tile).
        None when the step is skipped (no key, a remembered rejection, a failed probe)."""
        cfg = self.cfg
        key = carto_key(cfg) or embedded_key
        if not key:
            # never a CARTO request without a key: every tile would be the watermark
            self._pending_carto = "no-key"
            return None
        remembered = _carto_memory(key)
        if remembered:
            self._pending_carto, self._pending_carto_reason = remembered
            return None
        ptx, pty = self._tiles[len(self._tiles) // 2]        # the map's middle tile
        verdict, keyed, detail, reason = self._carto_tile(url.format(z=z, x=ptx, y=pty), key)
        if verdict != "ok":
            return self._carto_failed(key, verdict, detail, reason)
        _CARTO_REJECTED.pop(_key_id(key), None)
        self._pending_carto = "accepted"
        log.info("CARTO accepted TTU_SAFETY_CARTO_KEY (the keyed probe tile is not the unkeyed "
                 "watermark): building the %s map from keyed tiles, each checked against "
                 "its unkeyed twin", self.map_name)

        def fetch(tx, ty):
            v, data, why, _reason = self._carto_tile(url.format(z=z, x=tx, y=ty), key)
            if v == "ok":
                return data
            # a watermarked tile after an accepted probe (key revoked, quota spent): never
            # drawn — the build stays incomplete, is not cached and is retried
            raise (TileRefused if v == "rejected" else OSError)(redact(why, cfg, (key,)))
        return self._build_tiles(fetch, "carto", False, prefetched={(ptx, pty): keyed},
                                 secret=key)

    def _carto_tile(self, url, key):
        """One CARTO tile, fetched with the key and without it -> (verdict, the keyed
        tile's bytes, log detail, short reason). "ok" = the keyed tile is a real map tile;
        "rejected" = CARTO refused the keyed request (HTTP 401/403) or answered it with
        the same bytes as the unkeyed one — the "API KEY REQUIRED" watermark, which CARTO
        sends for a missing and a bogus key alike under HTTP 200, so the headers cannot
        tell but the bytes can; "unreachable" = no verdict possible (a failed fetch).
        The verdict follows the KEYED reply: an unkeyed twin CARTO refuses outright (a
        4xx) only proves that CARTO enforces the key, so the keyed tile that came through
        is real; a failed keyed request never blames an unkeyed hiccup on the key."""
        keyed_url = _with_key(url, key)
        try:
            keyed = _get(keyed_url, timeout=BASEMAP_TILE_TIMEOUT, fresh=True)
        except urllib.error.HTTPError as e:
            if e.code in _CARTO_KEY_REFUSED:
                return ("rejected", None, "CARTO refused the keyed tile %s (HTTP %d)"
                        % (keyed_url, e.code), "HTTP %d" % e.code)
            return ("unreachable", None, "the keyed tile %s failed (%s)" % (keyed_url, e),
                    "HTTP %d" % e.code)
        except Exception as e:                  # noqa: BLE001 - network, timeout, ...
            return ("unreachable", None, "the keyed tile %s could not be fetched (%s)"
                    % (keyed_url, e), "no answer")
        try:
            Image.open(io.BytesIO(keyed)).verify()
        except Exception as e:                  # noqa: BLE001 - an error page, a portal
            return ("unreachable", None, "the keyed tile %s is not an image (%s)"
                    % (keyed_url, e), "not an image")
        try:
            plain = _get(url, timeout=BASEMAP_TILE_TIMEOUT, fresh=True)
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500 and e.code not in (408, 429):
                return "ok", keyed, "", ""      # CARTO refuses keyless tiles: key enforced
            return ("unreachable", None, "the unkeyed twin %s failed (%s), so the keyed tile "
                    "cannot be told from the watermark" % (url, e), "HTTP %d" % e.code)
        except Exception as e:                  # noqa: BLE001
            return ("unreachable", None, "the unkeyed twin %s could not be fetched (%s), so "
                    "the keyed tile cannot be told from the watermark" % (url, e),
                    "no answer")
        if plain == keyed:
            return ("rejected", None, "the keyed tile %s is byte-identical to the unkeyed one, "
                    "i.e. still CARTO's 'API KEY REQUIRED' watermark" % keyed_url,
                    "tiles still watermarked")
        return "ok", keyed, "", ""

    def _carto_failed(self, key, kind, detail, reason=""):
        _CARTO_REJECTED[_key_id(key)] = (time.monotonic() + CARTO_REJECT_MEMORY_SEC, kind,
                                         reason)
        self._pending_carto = kind
        self._pending_carto_reason = reason
        msg = redact("%s TTU_SAFETY_CARTO_KEY: %s — the %s map falls back to the next "
                     "basemap source; not re-probed for %d h"
                     % ("CARTO did not accept" if kind == "rejected" else "could not check",
                        detail, self.map_name, CARTO_REJECT_MEMORY_SEC // 3600),
                     self.cfg, (key,))
        kid = _key_id(key)
        if kid not in _CARTO_ERROR_LOGGED:      # ERROR once per key and process
            _CARTO_ERROR_LOGGED.add(kid)
            log.error("%s", msg)
        else:
            log.warning("%s", msg)
        return None

    def _build_tiles(self, fetch, source, invert, prefetched=None, secret=""):
        """Fetch this map's tiles with ``fetch(tx, ty) -> bytes`` -> (composite resized
        to the thumbnail, arrived, expected, refused). A tile refused as never drawable
        (TileRefused: OSM's "access blocked" tile, a CARTO watermark) is counted apart, so
        the page can say why. Gives up on a source after BASEMAP_GIVE_UP_AFTER failures
        with nothing arrived (a dead server must not hold the radar thread for minutes)."""
        cfg = self.cfg
        gx0, gy0, z = self._tilebox[0], self._tilebox[1], self._tilebox[4]
        canvas = Image.new("RGB", self._canvas_size, BASEMAP_BG)
        n, refused, errors = 0, 0, []
        prefetched = prefetched or {}
        # a tile already in hand (the CARTO probe) first: it counts as arrived before the
        # give-up rule below can write the whole source off
        order = ([t for t in self._tiles if t in prefetched]
                 + [t for t in self._tiles if t not in prefetched])
        for tx, ty in order:
            if n == 0 and len(errors) >= BASEMAP_GIVE_UP_AFTER:
                break
            try:
                data = prefetched.get((tx, ty))
                if data is None:
                    data = fetch(tx, ty)
                t = Image.open(io.BytesIO(data)).convert("RGB")
                if invert and _mean_luminance(t) > LIGHT_TILE_LUMINANCE:
                    t = invert_lightness(t)
                canvas.paste(t, (tx * 256 - gx0, ty * 256 - gy0))
                n += 1
            except Exception as e:              # noqa: BLE001 - one tile, not the map
                refused += isinstance(e, TileRefused)
                errors.append("z%s x%s y%s: %s" % (z, tx, ty, e))
        if errors:
            log.warning("radar %s basemap (%s): %d of %d tiles failed, first: %s",
                        self.map_name, source, len(errors), len(self._tiles),
                        redact(errors[0], cfg, (secret,)))
        return canvas.resize((self._ox, self._oy), Image.LANCZOS), n, len(self._tiles), refused

    def _load_cache(self, path):
        if not os.path.exists(path):
            return None
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:                  # noqa: BLE001 - a torn file: rebuild
            log.warning("radar basemap cache %s unreadable (%s) — ignored", path, e)
            return None
        if img.size != (self._ox, self._oy):   # never expected (the size is in the name)
            img = img.resize((self._ox, self._oy), Image.LANCZOS)
        # this line names the file each map uses: README.md shows how to find a
        # watermarked CARTO composite with it and delete just that one
        log.info("radar %s basemap loaded from cache %s (tiles not re-fetched)",
                 self.map_name, path)
        return img

    def _save_cache(self, img, step, n, plan=None):
        path = self._cache_path(step, plan)
        tmp = path + ".tmp"
        try:
            os.makedirs(self.cfg.RADAR_CACHE_DIR, exist_ok=True)
            img.save(tmp, format="PNG")         # .tmp ext -> must state the format
            os.replace(tmp, path)               # a brownout never leaves a torn cache
            log.info("radar %s basemap (%s) built from %d tiles and cached to %s",
                     self.map_name, step, n, path)
        except Exception as e:                  # noqa: BLE001
            log.error("could not cache radar basemap %s: %s", path, e)

    def _use(self, img, source, retry=None, tiles=None, why=()):
        self._basemap = img
        self.carto_state = self._pending_carto
        self.carto_reason = self._pending_carto_reason
        self.osm_state = self._pending_osm
        self.basemap_why = tuple(why)
        if source != self.basemap_source:
            log.info("radar %s basemap: %s", self.map_name, source)
        self.basemap_source = source
        self.basemap_tiles = tiles
        # light tiles are inverted for the night map from OSM and non-CARTO custom sets
        self.basemap_inverted = bool(self._invert and (
            source == "osm" or (source in ("custom", "custom-cached")
                                and _config.tile_host_kind(self.tile_url) != "carto")))
        self._basemap_retry_at = None if retry is None else time.monotonic() + retry
        self._built_for = _geo_sig(self._geo) if self._geo is not None else None

    # reproject lookup (static for a fixed region): thumb pixel -> radar (col,row)
    def _build_remap(self):
        ox, oy = self._ox, self._oy
        gx0, gy0, gx1, gy1, z = self._tilebox
        remap = []
        for j in range(oy):
            gy = gy0 + (j + 0.5) / oy * (gy1 - gy0)
            row = []
            for i in range(ox):
                gx = gx0 + (i + 0.5) / ox * (gx1 - gx0)
                lat, lon = _num2deg(gx / 256, gy / 256, z)
                row.append(latlon_to_px(lat, lon))
            remap.append(row)
        self._remap = remap

    def _mv(self, lat, lon):
        gx0, gy0, gx1, gy1, z = self._tilebox
        xf, yf = _deg2num(lat, lon, z)
        return ((xf * 256 - gx0) / (gx1 - gx0) * self._ox,
                (yf * 256 - gy0) / (gy1 - gy0) * self._oy)

    def _radar_layer(self, img):
        """The MRMS frame reprojected onto this map as an RGBA layer (transparent where
        there is no echo)."""
        ox, oy = self._ox, self._oy
        ov = Image.new("RGBA", (ox, oy), (0, 0, 0, 0))
        op = ov.load()
        rp = img.load()
        for j in range(oy):
            rowmap = self._remap[j]
            for i in range(ox):
                c, r = rowmap[i]
                if 0 <= c < GRID_W and 0 <= r < GRID_H:
                    v = rp[c, r]
                    v = v if isinstance(v, int) else v[0]
                    col = _dbz_color(_dbz(v))
                    if col:
                        op[i, j] = (col[0], col[1], col[2], 205)
        return ov

    def render(self, img, frame_txt, overlays=None):
        """Composite radar over the cached basemap, draw the hazard overlays (if any),
        ring + scale bars, save the PNG. Keeps the reprojected frame for rerender()."""
        if self._basemap is None:
            self._build_basemap()
        if self._remap is None:
            self._build_remap()
        layer = self._radar_layer(img)
        self._frame = (layer, frame_txt)
        return self._compose(layer, frame_txt, overlays)

    def rerender(self, overlays):
        """Redraw the last frame with a new overlay set (no MRMS fetch, no reprojection).
        False when there is no frame to draw on yet, or the write failed."""
        frame = self._frame             # one read: clock_stepped may drop it concurrently
        if frame is None:
            return False
        return self._compose(frame[0], frame[1], overlays)

    def forget_frame(self):
        """Drop the kept frame (after a clock step it came from the wrong day)."""
        self._frame = None

    def _compose(self, layer, frame_txt, overlays=None):
        cfg = self.cfg
        oy = self._oy
        base = self._basemap.copy().convert("RGBA")
        im = Image.alpha_composite(base, layer)
        # Hazard overlays go over the radar and UNDER the ring/crosshair/scale bars. Only
        # what touches the map is drawn; with nothing to draw this path is exactly the
        # pre-hazards one, so the PNG stays pixel-identical.
        specs = _prepare_overlays(cfg, overlays) if overlays else []
        if specs:
            im = self._draw_overlays(im, specs)
        im = im.convert("RGB")
        d = ImageDraw.Draw(im)
        lat0, lon0 = cfg.GEOCODE
        ink, stroke = self.colors["ink"], self.colors["stroke"]

        # trigger ring (RADAR_TRIGGER_KM)
        ring = [self._mv(*dest_point(lat0, lon0, cfg.RADAR_TRIGGER_KM, b))
                for b in range(0, 361, 6)]
        d.line(ring, fill=self.colors["ring"], width=2)
        # observatory crosshair
        cx, cy = self._mv(lat0, lon0)
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=ink, width=2)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            d.line([cx + dx * 6, cy + dy * 6, cx + dx * 11, cy + dy * 11], fill=ink, width=2)
        d.text((cx + 8, cy + 6), "%g km" % cfg.RADAR_TRIGGER_KM, fill=self.colors["label"],
               font=_font(13), stroke_width=2, stroke_fill=stroke)

        # scale bars (10 km and 10 mi), bottom-left
        f = _font(12)

        def px_for(km):
            x2, _ = self._mv(*dest_point(lat0, lon0, km, 90))
            x1, _ = self._mv(lat0, lon0)
            return abs(x2 - x1)
        bx, by = 14, oy - 34
        for label, km in (("10 km", 10.0), ("10 mi", 16.0934)):
            L = px_for(km)
            d.line([bx, by, bx + L, by], fill=ink, width=3)
            d.line([bx, by - 3, bx, by + 3], fill=ink, width=2)
            d.line([bx + L, by - 3, bx + L, by + 3], fill=ink, width=2)
            d.text((bx + L + 5, by - 7), label, fill=ink, font=f,
                   stroke_width=2, stroke_fill=stroke)
            by += 15
        # frame time, top-left
        d.text((8, 6), frame_txt, fill=self.colors["text"], font=_font(12),
               stroke_width=2, stroke_fill=stroke)

        out = self._output_target()
        tmp = out + ".tmp"
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            im.save(tmp, format="PNG")     # .tmp ext -> must state the format
            os.replace(tmp, out)
            return True
        except Exception:
            log.exception("could not write radar thumbnail %s", out)
            return False

    def _output_target(self) -> str:
        """Where the PNG is physically written. With RADAR_THUMB_VIA_SHM the bytes go
        to /dev/shm (RAM) and thumb_path becomes a one-time SYMLINK to them — the web
        server follows it, the page keeps its unchanged path, and the every-5-minutes
        rewrite stops touching the SD card entirely. After a reboot the symlink dangles
        for at most one poll; the page already shows 'not available yet' for that."""
        if not getattr(self.cfg, "RADAR_THUMB_VIA_SHM", False):
            return self.thumb_path
        real = os.path.join("/dev/shm", os.path.basename(self.thumb_path))
        try:
            if os.path.islink(self.thumb_path):
                if os.readlink(self.thumb_path) != real:
                    os.remove(self.thumb_path)
                    os.symlink(real, self.thumb_path)
            else:
                if os.path.exists(self.thumb_path):
                    os.remove(self.thumb_path)      # replace the old regular file once
                os.makedirs(os.path.dirname(self.thumb_path) or ".", exist_ok=True)
                os.symlink(real, self.thumb_path)
            return real
        except Exception:
            log.exception("cannot set up the shm symlink for %s — writing directly",
                          self.thumb_path)
            return self.thumb_path

    # ---- hazard overlays ------------------------------------------------------------
    def _px(self, pos):
        """GeoJSON position (lon, lat, ...) -> map pixel (x, y)."""
        return self._mv(float(pos[1]), float(pos[0]))

    def _poly_mask(self, geom):
        """("L" mask of the geometry's polygons, pixel rings) or (None, []) when nothing of
        it lands on the map. Holes are cut per polygon and the polygon is then unioned in,
        so one member's hole never punches through an overlapping member of the same
        MultiPolygon (zone unions overlap freely)."""
        size = (self._ox, self._oy)
        mask = Image.new("L", size, 0)
        md = ImageDraw.Draw(mask)
        rings_px = []
        for rings in _iter_polygons(geom):
            if len(rings[0]) < 3:
                continue                # degenerate outer ring: never promote a hole
            px = [[self._px(p) for p in r] for r in rings if len(r) >= 3]
            if len(px) == 1:
                md.polygon(px[0], fill=255)
            else:
                part = Image.new("L", size, 0)
                pd = ImageDraw.Draw(part)
                pd.polygon(px[0], fill=255)
                for hole in px[1:]:
                    pd.polygon(hole, fill=0)
                mask.paste(255, (0, 0), part)
            rings_px.extend(px)
        if mask.getbbox() is None:
            return None, []
        return mask, rings_px

    def _outline_parts(self, geom, shared=None):
        """Pixel polylines of a geometry's outline: every polygon ring closed, plus lines.
        ``shared``: vertices (rounded lon, lat) of OTHER outlines drawn anyway; a hole
        ring lying on them (>= 80 % of its vertices) is skipped — see _draw_info."""
        parts = []
        for rings in _iter_polygons(geom):
            for i, r in enumerate(rings):
                if i and shared and _on_shared(r, shared):
                    continue
                if len(r) >= 2:
                    px = [self._px(p) for p in r]
                    parts.append(px + px[:1])
        for line in _iter_lines(geom):
            if len(line) >= 2:
                parts.append([self._px(p) for p in line])
        return parts

    def _casing_for(self, rgb):
        """The casing colour that keeps an outline of colour ``rgb`` legible on THIS map.
        Normally the theme's stroke (black at night, white by day): it separates the line
        from radar echoes of its own hue. But a colour that is itself close to the
        basemap — a pale Dust Storm Warning #FFE4C4 or Tornado Watch yellow on the day
        map (contrast ~1.1), a Flash Flood Warning #8B0000 or the hazmat indigo #4B0082
        on the night map — would then vanish, casing and all: it gets the opposite ink
        instead, so every outline stands out from the basemap by >= CASING_MIN_CONTRAST
        either by its own colour or by its casing (a test checks the whole NWS table)."""
        rgb = tuple(rgb)[:3]
        if _contrast(rgb, self.colors["base"]) >= CASING_MIN_CONTRAST:
            return self.colors["stroke"]
        return self.colors["ink"]

    def _stroke_lines(self, d, parts, rgb, width, dash=None):
        """Cased line: the casing (see _casing_for) 1 px wider on each side first (ALL
        parts), then the colour on top, so the line stays visible over echoes of its own
        hue and one part's casing never cuts the colour of its neighbour."""
        pad = width + 4
        xmax, ymax = self._ox + pad, self._oy + pad
        pieces = []
        for pts in parts:
            for pc in _clip_polyline(pts, -pad, -pad, xmax, ymax):
                pc = _thin(pc)
                if len(pc) < 2:
                    continue
                pieces.extend(_dashes(pc, *dash) if dash else [pc])
        casing = self._casing_for(rgb) + (255,)
        for pc in pieces:
            d.line(pc, fill=casing, width=width + 2, joint="curve")
        for pc in pieces:
            d.line(pc, fill=tuple(rgb) + (255,), width=width, joint="curve")
        return bool(pieces)

    def _on_map(self, x, y, margin):
        return -margin <= x <= self._ox + margin and -margin <= y <= self._oy + margin

    def _symbol(self, d, kind, x, y, spec):
        """Point symbols. Fire incidents are orange-red triangles; storm reports are
        drawn in the theme's ink over a halo, never in a hue — alert areas already use
        most hues and the radar the rest, so any colour would collide."""
        ink = self.colors["ink"] + (255,)
        halo = self.colors["stroke"] + (255,)
        if kind == "fire":
            side = 10.0
            d.polygon(_triangle(x, y, side + 2.0 * math.sqrt(3.0) * 1.5), fill=halo)
            d.polygon(_triangle(x, y, side), fill=FIRE_RGB + (255,))
        elif kind == "lsr":
            sym = spec.get("symbol") or "other"
            if sym == "tornado":            # downward triangle (funnel)
                d.polygon(_triangle(x, y, 9.0 + 2.0 * math.sqrt(3.0), down=True), fill=halo)
                d.polygon(_triangle(x, y, 9.0, down=True), fill=ink)
            elif sym == "hail":             # dot
                d.ellipse([x - 4.5, y - 4.5, x + 4.5, y + 4.5], fill=halo)
                d.ellipse([x - 3.5, y - 3.5, x + 3.5, y + 3.5], fill=ink)
            elif sym == "wind":             # square
                d.rectangle([x - 4, y - 4, x + 4, y + 4], fill=halo)
                d.rectangle([x - 3, y - 3, x + 3, y + 3], fill=ink)
            elif sym == "flood":            # diamond
                d.polygon([(x, y - 6), (x + 6, y), (x, y + 6), (x - 6, y)], fill=halo)
                d.polygon([(x, y - 4.5), (x + 4.5, y), (x, y + 4.5), (x - 4.5, y)], fill=ink)
            elif sym in ("dust", "winter"):  # x (dust) / + (winter)
                arms = ([(-4, -4, 4, 4), (-4, 4, 4, -4)] if sym == "dust"
                        else [(-5, 0, 5, 0), (0, -5, 0, 5)])
                for w, fill in ((4, halo), (2, ink)):
                    for ax, ay, bx, by in arms:
                        d.line([(x + ax, y + ay), (x + bx, y + by)], fill=fill, width=w)
            else:                           # anything else: small ring
                d.ellipse([x - 4.5, y - 4.5, x + 4.5, y + 4.5], outline=halo, width=3)
                d.ellipse([x - 3.5, y - 3.5, x + 3.5, y + 3.5], outline=ink, width=1)

    def _draw_info(self, d, spec):
        """One information overlay onto the shared (transparent) info layer."""
        kind, geom = spec["kind"], spec["geometry"]
        if kind == "spc_outlook":
            cat = spec.get("category")
            rgb = SPC_RGB.get(cat, self.colors["mrgl"])
            rgb = _never_green(rgb, self.colors["mrgl"])
            # SPC's non-layered file cuts each higher category out of the lower one as a
            # HOLE, so that hole is the higher category's own outer boundary: stroked by
            # the higher category, it is skipped here (twice would interleave two dash
            # patterns in two colours). A genuine hole (a no-risk island) is still drawn.
            self._stroke_lines(d, self._outline_parts(geom, spec.get("_shared")), rgb,
                               INFO_OUTLINE_PX, dash=(8, 5))
        elif kind == "spc_md":
            self._stroke_lines(d, self._outline_parts(geom), MD_RGB, INFO_OUTLINE_PX,
                               dash=(4, 4))
        elif kind in ("fire_perimeter", "fire"):
            # a perimeter is an outline; a "fire" sent as a polygon is drawn the same way
            self._stroke_lines(d, self._outline_parts(geom), FIRE_RGB, INFO_OUTLINE_PX)
        if kind in ("fire", "lsr"):
            for pos in _iter_points(geom):
                x, y = self._px(pos)
                if self._on_map(x, y, 18):
                    self._symbol(d, kind, x, y, spec)

    def _draw_smoke(self, im, specs):
        """HMS smoke as a translucent grey veil, one union per density (overlapping
        polygons of the same density do not stack), heavier densities on top."""
        by_density = {}
        for s in specs:
            try:
                mask, _ = self._poly_mask(s["geometry"])
            except Exception as e:              # noqa: BLE001 - one bad polygon
                log.warning("smoke overlay %r not drawn: %s", s.get("key"), e)
                continue
            if mask is None:
                continue
            acc = by_density.get(s["density"])
            if acc is None:
                by_density[s["density"]] = mask
            else:
                acc.paste(255, (0, 0), mask)
        for density in sorted(by_density, key=lambda k: _DENSITY_ORDER.get(k, 0)):
            alpha = SMOKE_ALPHA.get(density, SMOKE_ALPHA["light"])
            layer = Image.new("RGBA", im.size, self.colors["smoke"] + (0,))
            layer.putalpha(by_density[density].point([0] + [alpha] * 255))
            im = Image.alpha_composite(im, layer)
        return im

    def _draw_alert(self, im, spec):
        """An NWS alert area: translucent fill (holes respected) + cased outline, thicker
        when the alert vetoes the monitor."""
        mask, rings = self._poly_mask(spec["geometry"])
        if mask is None:
            return im
        rgb = tuple(spec["color"])
        a = _num(getattr(self.cfg, "HAZARD_ALERT_FILL_ALPHA", 60))
        alpha = 60 if a is None else int(max(0, min(255, a)))
        layer = Image.new("RGBA", im.size, rgb + (0,))
        layer.putalpha(mask.point([0] + [alpha] * 255))
        width = ALERT_VETO_OUTLINE_PX if spec.get("vetoes") else ALERT_OUTLINE_PX
        self._stroke_lines(ImageDraw.Draw(layer), [r + r[:1] for r in rings], rgb, width)
        return Image.alpha_composite(im, layer)

    def _draw_overlays(self, im, specs):
        """specs are already in draw order (see _spec_order). Returns the new RGBA image.
        Every overlay is drawn in its own try: one bad geometry never loses the map."""
        smoke = [s for s in specs if s["kind"] == "smoke"]
        if smoke:
            im = self._draw_smoke(im, smoke)
        info = [s for s in specs if s["kind"] not in ("smoke", "alert")]
        if info:
            layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            spc = [s for s in info if s["kind"] == "spc_outlook"]
            for s in info:
                if s["kind"] == "spc_outlook":
                    # the outer-ring vertices of the OTHER categories (see _draw_info);
                    # a copy of the spec: the caller's list (and its digests) stay as is
                    s = dict(s, _shared=_outer_vertices(o["geometry"] for o in spc
                                                        if o is not s))
                try:
                    self._draw_info(d, s)
                except Exception as e:          # noqa: BLE001
                    log.warning("hazard overlay %s %r not drawn: %s", s["kind"], s.get("key"), e)
            im = Image.alpha_composite(im, layer)
        for s in specs:
            if s["kind"] != "alert":
                continue
            try:
                im = self._draw_alert(im, s)
            except Exception as e:              # noqa: BLE001
                log.warning("NWS alert %r (%s) not drawn: %s", s.get("key"), s.get("event"), e)
        return im


# ---- what the basemap is: attribution, notes, warnings, one-line summary ---------------
def basemap_attribution(cfg, used):
    """The map's credit line, from the sources actually drawn. ``used`` = [(source,
    tile_url)] of the maps. Any CARTO map -> "© OpenStreetMap contributors, © CARTO"
    (CARTO's required line, which covers OSM too); OSM alone -> "© OpenStreetMap
    contributors"; a custom source is credited by its host (cartocdn / openstreetmap
    hosts as above); nothing drawn -> the radar credit alone."""
    carto = osm = False
    hosts = []
    for src, url in used:
        if src in ("carto", "carto-cached"):
            carto = True
        elif src == "osm":
            osm = True
        elif src in ("custom", "custom-cached"):
            try:
                host = urllib.parse.urlsplit(str(url)).hostname or ""
            except ValueError:
                host = ""
            if "cartocdn" in host:
                carto = True
            elif "openstreetmap" in host:
                osm = True
            elif host and host not in hosts:
                hosts.append(host)
    parts = [CARTO_CREDIT] if carto else ([OSM_CREDIT] if osm else [])
    parts += ["Map tiles: %s" % h for h in hosts]
    parts.append(str(getattr(cfg, "RADAR_ATTRIBUTION", _config.RADAR_ATTRIBUTION)))
    return " · ".join(parts)


class _MapSnap:
    """One map's basemap attributes, each read ONCE (see _snap)."""
    __slots__ = ("map_name", "source", "tiles", "inverted", "carto", "carto_reason", "osm",
                 "why", "tile_url", "osm_by")


def _snap(t):
    """A consistent-enough copy of a map's basemap attributes. The radar thread rewrites
    them during a rebuild (_use), and two reads of one attribute can straddle that — a
    ``if t.basemap_tiles: tuple(t.basemap_tiles)`` once raised TypeError straight into
    the IsSafe path. So every attribute is read exactly once, here."""
    if isinstance(t, _MapSnap):
        return t
    s = _MapSnap()
    s.map_name = getattr(t, "map_name", None)
    s.source = getattr(t, "basemap_source", None)
    tiles = getattr(t, "basemap_tiles", None)
    s.tiles = tuple(tiles) if isinstance(tiles, (tuple, list)) and len(tiles) == 2 else None
    s.inverted = bool(getattr(t, "basemap_inverted", False))
    s.carto = getattr(t, "carto_state", None)
    s.carto_reason = str(getattr(t, "carto_reason", "") or "")
    s.osm = getattr(t, "osm_state", None)
    why = getattr(t, "basemap_why", ())
    s.why = tuple(w for w in (why if isinstance(why, (tuple, list)) else ())
                  if isinstance(w, (tuple, list)) and len(w) == 3)
    s.tile_url = str(getattr(t, "tile_url", "") or "")
    plan = getattr(t, "basemap_plan", None)
    s.osm_by = plan().get("osm_by") if (callable(plan) and s.source == "osm") else None
    return s


_SOURCE_SHORT = {"carto-cached": "cached CARTO", "carto": "CARTO",
                 "custom-cached": "cached custom tiles", "custom": "custom tiles",
                 "osm": "OSM", "none": "nothing (plain background)"}


def _maps_phrase(built):
    """'the map uses OSM' / 'the night map uses cached CARTO and the day map OSM'."""
    pairs = [(s.map_name, _SOURCE_SHORT.get(s.source, s.source)) for s in built]
    if len({p for _, p in pairs}) == 1:
        return "the map uses %s" % pairs[0][1]
    return ("the %s map uses %s" % pairs[0]
            + "".join(" and the %s map %s" % p for p in pairs[1:]))


def _for_maps(names):
    return "%s map%s" % (" and ".join(names), "s" if len(names) > 1 else "")


def basemap_messages(cfg, thumbs):
    """(notes, warnings) about the basemaps: short page notes (what each map lacks and
    what to set), and warnings for the monitor's state "warnings" (page + /setup; never a
    veto). Never contains the key."""
    notes, warns = [], []
    built = [s for s in (_snap(t) for t in thumbs) if isinstance(s.source, str)]
    if not built:
        return notes, warns
    carto = {s.carto for s in built}
    osm = {s.osm for s in built}
    hours = CARTO_REJECT_MEMORY_SEC // 3600
    if "rejected" in carto:
        reason = next((s.carto_reason for s in built if s.carto == "rejected"
                       and s.carto_reason), "") or "tiles still watermarked"
        warns.append("TTU_SAFETY_CARTO_KEY was not accepted by CARTO (%s); %s"
                     % (reason, _maps_phrase(built)))
        notes.append("TTU_SAFETY_CARTO_KEY not accepted by CARTO (%s), re-checked every %d h"
                     % (reason, hours))
    elif "unreachable" in carto:
        reason = next((s.carto_reason for s in built if s.carto == "unreachable"
                       and s.carto_reason), "")
        warns.append("TTU_SAFETY_CARTO_KEY could not be checked (the CARTO probe tile could "
                     "not be fetched%s); %s" % (": " + reason if reason else "",
                                                _maps_phrase(built)))
        notes.append("CARTO unreachable, re-checked every %d h" % hours)
    if "no-key" in carto and any(s.source in ("osm", "none") for s in built):
        notes.append("for CARTO maps set TTU_SAFETY_CARTO_KEY (free key: %s)" % CARTO_KEY_URL)
    # a better source that failed while the map shows another: why, and when it is retried
    failed = {}
    for s in built:
        for kind, n, m in s.why:
            if kind == "carto":
                text = ("CARTO tiles incomplete (%d/%d), retried every %d min"
                        % (n, m, BASEMAP_RETRY_SEC // 60) if n else
                        "CARTO tiles could not be fetched, re-checked every %d h" % hours)
            elif n:
                text = ("custom tiles incomplete (%d/%d), retried every %d min"
                        % (n, m, BASEMAP_RETRY_SEC // 60))
            else:
                text = ("custom TTU_SAFETY_RADAR_TILE_URL%s failed, re-checked every %d h"
                        % ("" if s.map_name == "night" else "_DAY", hours))
            failed.setdefault(text, []).append(s.map_name)
    notes += ["%s: %s" % (_for_maps(names), text) for text, names in failed.items()]
    ua = str(getattr(cfg, "NWS_USER_AGENT", "") or "")
    if "skipped-ua" in osm:
        notes.append("OpenStreetMap not requested: TTU_SAFETY_NWS_UA has a placeholder "
                     "contact")
    if "refused" in osm:
        notes.append("OpenStreetMap refused the tiles ('access blocked' replies): check "
                     "TTU_SAFETY_NWS_UA")
    elif "failed" in osm:
        notes.append("OpenStreetMap tiles could not be fetched")
    if osm & {"used", "refused", "failed", "skipped-ua"} and not _config.ua_has_real_email(ua):
        # One UA identifies the daemon to OSM and NWS alike. OSM serves a UA that just
        # names the app (checked 2026-09-24) but blocks a placeholder contact; NWS asks
        # for a contact. A real address settles both.
        if _config.ua_has_placeholder(ua):
            why = ("OpenStreetMap blocks tile requests from User-Agents with a placeholder "
                   "contact like you@example.org, so no OSM tiles are fetched, and NWS asks "
                   "for a contact")
        else:
            why = ("OpenStreetMap asks for a User-Agent that identifies the app and blocks "
                   "placeholder contacts like you@example.org, and NWS asks for a contact")
        warns.append("Set TTU_SAFETY_NWS_UA with a real contact e-mail: %s (now: '%s')"
                     % (why, ua[:120]))
    for s in built:
        if s.tiles:
            notes.append("%s map incomplete (%d/%d tiles), retrying" % ((s.map_name,) + s.tiles))
    blank = [s.map_name for s in built if s.source == "none"]
    if blank:
        notes.append("%s retried every %d min" % (_for_maps(blank), BASEMAP_RETRY_SEC // 60))
    return [redact(n, cfg) for n in notes], [redact(w, cfg) for w in warns]


_BOTH_MAPS = {
    "carto-cached": "CARTO Dark Matter (night) / Positron (day), from tiles cached on this Pi",
    "carto": ("CARTO Dark Matter (night) / Positron (day), fetched with the configured "
              "CARTO API key"),
    "custom-cached": "custom tiles (TTU_SAFETY_RADAR_TILE_URL / _DAY), cached on this Pi",
    "custom": "custom tiles (TTU_SAFETY_RADAR_TILE_URL / _DAY)",
    "none": "none (plain background)",
}


def _osm_role(by):
    """OpenStreetMap as the chain's key-free fallback, or as the configured choice."""
    return "as set by %s" % by if by else "the key-free fallback"


def _one_map(name, src, inverted, by=None):
    style = "Dark Matter" if name == "night" else "Positron"
    var = "TTU_SAFETY_RADAR_TILE_URL" + ("" if name == "night" else "_DAY")
    return {
        "carto-cached": "CARTO %s, from tiles cached on this Pi" % style,
        "carto": "CARTO %s, fetched with the configured CARTO API key" % style,
        "custom-cached": "custom tiles (%s), cached on this Pi" % var,
        "custom": "custom tiles (%s)" % var,
        "osm": "OpenStreetMap standard tiles%s, %s"
               % (" (colour-inverted)" if inverted and name == "night" else "", _osm_role(by)),
        "none": "none (plain background)",
    }.get(src, str(src))


def basemap_summary(basemap, notes=()):
    """One plain-text line saying which basemap each map shows, then the notes, e.g.
    "Basemap: CARTO Dark Matter (night) / Positron (day), from tiles cached on this Pi."
    OpenStreetMap is "the key-free fallback" unless basemap["chosen_by"] names the
    setting that chose it for that map. '' when no map is built yet or ``basemap`` is
    not a dict (an older daemon). make_status_page.py carries an identical copy (a test
    keeps the two in step)."""
    if not isinstance(basemap, dict):
        return ""
    night, day = basemap.get("night"), basemap.get("day")
    inverted = bool(basemap.get("night_inverted"))
    chosen = basemap.get("chosen_by") if isinstance(basemap.get("chosen_by"), dict) else {}
    maps = [(m, s, chosen.get(m) if isinstance(chosen.get(m), str) else None)
            for m, s in (("night", night), ("day", day)) if isinstance(s, str) and s]
    if not maps:
        return ""
    if len(maps) == 2 and maps[0][1:] == maps[1][1:]:
        if night == "osm":
            desc = ("OpenStreetMap standard tiles%s, %s"
                    % (" (night map colour-inverted)" if inverted else "",
                       _osm_role(maps[0][2])))
        else:
            desc = _BOTH_MAPS.get(night, str(night))
    elif len(maps) == 1:
        desc = _one_map(maps[0][0], maps[0][1], inverted, maps[0][2])
    else:
        desc = "; ".join("%s map: %s" % (m, _one_map(m, s, inverted, b)) for m, s, b in maps)
    notes = [str(n) for n in (notes if isinstance(notes, (list, tuple)) else ()) if n]
    return "Basemap: " + desc + "".join("; " + n for n in notes) + "."


# ---- poller ----------------------------------------------------------------
class RadarPoller:
    def __init__(self, cfg, eventlog, overlay_sources=None):
        self.cfg = cfg
        self.log = eventlog
        self._lock = threading.Lock()
        # Hazard overlays for the maps: zero-arg callables returning overlay lists
        # (NwsAlertsPoller.overlays, HazardFeedsPoller.overlays). DISPLAY ONLY — nothing
        # from them reaches component()/IsSafe; the alert veto is decided in nws_alerts.
        self._overlay_sources = [s for s in (overlay_sources or ()) if callable(s)]
        self._overlay_last = {}        # source index -> (ts, last good list)
        self._overlay_fail = {}        # source index -> "kept"/"dropped" while failing
        self._drawn_sig = None         # overlay_signature of what the PNGs show now
        # night (dark) map, plus an optional day (light) map for the daytime page style
        self._thumbs = []
        if deps_available():
            self._thumbs.append(Thumbnailer(cfg, cfg.RADAR_TILE_URL,
                                            cfg.RADAR_THUMB_PATH, "dark"))
            if cfg.RADAR_DAY_ENABLED:
                self._thumbs.append(Thumbnailer(cfg, cfg.RADAR_TILE_URL_DAY,
                                                cfg.RADAR_THUMB_PATH_DAY, "light"))
        self._last_ok_ts = None
        self._last_poll_ts = None
        self._in_ring = False
        self._ring_streak = 0          # consecutive polls with an in-ring echo
        self._trigger_after = max(1, int(getattr(cfg, "RADAR_TRIGGER_AFTER", 2)))
        self._nearest_km = None
        self._count = 0
        self._frame_utc = None
        self._thumb_ok = False
        self._coverage_warned = False
        # The post-rain freeze must survive a daemon restart (systemd Restart=always
        # would otherwise silently drop the veto mid-event) — persist last_rain_ts like
        # the WU/GLM latches.
        self._last_rain_ts = None
        try:
            with open(cfg.RADAR_LATCH_FILE, encoding="utf-8") as f:
                lr = json.load(f).get("last_rain_ts")
            if isinstance(lr, (int, float)) and math.isfinite(lr):
                if lr > time.time() + 60:
                    # a detection timestamp in the future is impossible under a sane
                    # clock (2026-08 incident: an August stamp read under a March-RTC
                    # boot froze the radar for "158 days"); clamp to now — the freeze
                    # then runs its normal window from here
                    log.warning("persisted radar rain timestamp is %.1f days in the "
                                "future — system clock is (or was) wrong; clamping",
                                (lr - time.time()) / 86400.0)
                    lr = time.time()
                self._last_rain_ts = lr
                if 0 <= (time.time() - lr) < cfg.RADAR_LATCH_SEC:
                    log.info("restored radar post-rain freeze (%d min old)",
                             max(0, int((time.time() - lr) / 60)))
        except Exception:
            pass

    def _save_rain_ts(self):
        try:
            tmp = self.cfg.RADAR_LATCH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_rain_ts": self._last_rain_ts}, f)
            os.replace(tmp, self.cfg.RADAR_LATCH_FILE)
        except Exception:
            log.exception("cannot persist radar latch")

    def poll_now(self, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            self._last_poll_ts = now
        if not deps_available():
            return {"ok": False, "error": "pillow not installed"}
        if not site_in_coverage(*self.cfg.GEOCODE):
            if not self._coverage_warned:
                self._coverage_warned = True
                log.error("site %s is OUTSIDE the MRMS CONUS grid — radar layer disabled "
                          "(it would otherwise report a false 'clear')", self.cfg.GEOCODE)
                self.log.record("CONFIG", reason="site outside MRMS coverage",
                                result="radar layer disabled")
            return {"ok": False, "error": "site outside MRMS coverage"}
        ts, url = latest_frame()
        if not ts:
            log.warning("no recent MRMS frame")
            return {"ok": False, "error": "no frame"}
        try:
            img = Image.open(io.BytesIO(_get(url)))
            img.load()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("radar fetch failed: %s", e)
            return {"ok": False, "error": str(e)}
        in_ring, nearest, count = check_rain(self.cfg, img)
        frame_txt = "MRMS " + ts.strftime("%Y-%m-%d %H:%MZ")
        # render every configured map (dark + optional light); primary = the first (dark)
        overlays = self._collect_overlays(now)
        sig = overlay_signature(self.cfg, overlays)
        results = [t.render(img, frame_txt, overlays) for t in self._thumbs]
        thumb_ok = bool(results and results[0])
        with self._lock:
            # a failed write leaves the old overlays on disk: None forces a redraw next pass
            self._drawn_sig = sig if (results and all(results)) else None
            self._last_ok_ts = now
            self._frame_utc = ts.isoformat()
            self._in_ring = in_ring
            self._nearest_km = nearest
            self._count = count
            self._thumb_ok = thumb_ok
            # Consecutive-frame confirmation: one frame is an observation, N in a row are
            # weather. A clear frame resets the count; a FAILED fetch does not (we return
            # earlier, so a gap in the data never counts as "clear").
            self._ring_streak = (self._ring_streak + 1) if in_ring else 0
            confirmed = in_ring and self._ring_streak >= self._trigger_after
            if confirmed:
                # the freeze starts only on a CONFIRMED detection, so a one-frame artefact
                # cannot hold the veto for the whole post-rain freeze window
                self._last_rain_ts = now
                self._save_rain_ts()
        if confirmed:
            self.log.record("RADAR-RAIN", reason=f"echo within {self.cfg.RADAR_TRIGGER_KM:g} km",
                            source="mrms", result="unsafe",
                            nearest=f"{nearest}km", pixels=count,
                            frames=f"{self._ring_streak}/{self._trigger_after}")
        elif in_ring:
            log.info("radar echo within %g km (nearest %s km, %d px) — frame %d of %d, "
                     "not vetoing until confirmed",
                     self.cfg.RADAR_TRIGGER_KM, nearest, count,
                     self._ring_streak, self._trigger_after)
            self.log.record("RADAR-ECHO", reason=f"unconfirmed echo within "
                                                 f"{self.cfg.RADAR_TRIGGER_KM:g} km",
                            source="mrms", result=f"frame {self._ring_streak} of "
                                                  f"{self._trigger_after}",
                            nearest=f"{nearest}km", pixels=count)
        self._retry_basemaps()
        return {"ok": True, "in_ring": in_ring, "nearest_km": nearest, "count": count,
                "streak": self._ring_streak, "confirmed": confirmed}

    def _retry_basemaps(self):
        """Rebuild a basemap whose retry time has come — a missing or incomplete one every
        BASEMAP_RETRY_SEC, OSM standing in for a rejected CARTO key or a dead custom
        source every CARTO_REJECT_MEMORY_SEC — or whose region changed (a GPS position
        adopted after the first build: the adopted site's cached CARTO map is then found).
        Runs AFTER the frame's verdict is published, so a slow tile server never delays
        the rain check; the next render shows it."""
        for t in self._thumbs:
            due = getattr(t, "basemap_due", None)
            try:
                if callable(due) and due():
                    t._build_basemap()
            except Exception as e:              # noqa: BLE001 - decoration, never fatal
                log.warning("radar basemap rebuild failed: %s", redact(e, self.cfg))

    def _basemap_status(self):
        """(basemap, notes, warnings, attribution) for component(). Only the source names
        and fixed texts: the CARTO key never reaches the state file, page or /setup. Each
        map's attributes are read once (_snap): the radar thread may be rebuilding."""
        by_name = {}
        for t in self._thumbs:
            name = getattr(t, "map_name", None)
            if name in ("night", "day") and name not in by_name:
                by_name[name] = _snap(t)
        night, day = by_name.get("night"), by_name.get("day")
        maps = [s for s in (night, day) if s is not None]
        basemap = {"night": night.source if night else None,
                   "day": day.source if day else None,
                   "night_inverted": bool(night and night.inverted),
                   # the setting that made OpenStreetMap a map's choice (not the fallback)
                   "chosen_by": {s.map_name: s.osm_by for s in maps if s.osm_by}}
        notes, warns = basemap_messages(self.cfg, maps)
        used = [(s.source, s.tile_url) for s in maps if isinstance(s.source, str)]
        return basemap, notes, warns, basemap_attribution(self.cfg, used)

    def maybe_poll(self, sun_alt, now=None):
        # Radar polls DAY AND NIGHT (unlike WU/GLM): the data is free, daytime rain
        # matters too, and the page's radar map stays live around the clock. The
        # sun_alt argument is kept for interface symmetry but not used.
        if now is None:
            now = time.time()
        if not deps_available():
            return None
        with self._lock:
            elapsed = None if self._last_poll_ts is None else now - self._last_poll_ts
            due = elapsed is None or elapsed < 0 or elapsed >= self.cfg.RADAR_POLL_INTERVAL
        if due:
            r = self.poll_now(now)
            # a failed poll drew nothing: still put changed hazards on the last frame
            self.refresh_overlays(now)
            return r
        # Between frames (the loop wakes every 20-60 s, frames come every 5 min): if the
        # hazard overlays changed — a new tornado warning, a veto released — redraw the
        # last frame now instead of waiting up to RADAR_POLL_INTERVAL for the next one.
        self.refresh_overlays(now)
        return None

    def _collect_overlays(self, now):
        """Every source's overlays. A source that raises (or returns garbage) keeps its last
        good list for HAZARD_STALE_AFTER_SEC — a transient hiccup must not make warnings
        blink off and on the map — and is then left off rather than drawn stale forever."""
        out = []
        stale = _num(getattr(self.cfg, "HAZARD_STALE_AFTER_SEC", 600)) or 600
        for i, src in enumerate(self._overlay_sources):
            try:
                got = src()
                if got is None:
                    got = []
                if not isinstance(got, (list, tuple)):
                    raise TypeError("returned %s, not a list" % type(got).__name__)
                got = list(got)
                self._overlay_last[i] = (now, got)
                if self._overlay_fail.pop(i, None) is not None:
                    log.info("hazard overlay source %r recovered", src)
            except Exception as e:              # noqa: BLE001 - the map must survive it
                last = self._overlay_last.get(i)
                keep = last is not None and 0 <= now - last[0] <= stale
                got = last[1] if keep else []
                state = "kept" if keep else "dropped"
                if self._overlay_fail.get(i) != state:   # log transitions, not every pass
                    self._overlay_fail[i] = state
                    log.warning("hazard overlay source %r failed (%s) — %s", src, e,
                                "keeping its last overlays for now" if keep
                                else "its overlays are left off the map")
            out.extend(got)
        return out

    def refresh_overlays(self, now=None):
        """Re-render the maps from the kept frame when the overlay signature changed.
        Returns True when the thumbnails were rewritten. Cheap when nothing changed: one
        overlays() call per source and a hash, no drawing."""
        if now is None:
            now = time.time()
        if not self._overlay_sources or not self._thumbs or not deps_available():
            return False
        overlays = self._collect_overlays(now)
        specs = _prepare_overlays(self.cfg, overlays)
        sig = _signature(specs)
        with self._lock:
            if sig == self._drawn_sig:
                return False
        # no kept frame yet (first poll pending/failed, or dropped by a clock step): the
        # next successful poll draws the overlays; a write failure is retried next pass
        results = [t.rerender(overlays) for t in self._thumbs]
        if not (results and all(results)):
            return False
        with self._lock:
            self._drawn_sig = sig
            self._thumb_ok = True
        log.info("radar maps redrawn: hazard overlays changed (%d on the map)", len(specs))
        return True

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """Restart the freeze if the step would evaporate it, and force a re-poll —
        the frame URL is date-derived, so pre-step frames came from the wrong day."""
        # the kept frame came from the wrong day too: never redraw hazards onto it
        for t in self._thumbs:
            t.forget_frame()
        with self._lock:
            self._drawn_sig = None
            self._overlay_last.clear()
            lr = self._last_rain_ts
            if (lr is not None and 0 <= (pre_now - lr) < self.cfg.RADAR_LATCH_SEC
                    and not (0 <= (post_now - lr) < self.cfg.RADAR_LATCH_SEC)):
                self._last_rain_ts = post_now
                self._save_rain_ts()
                log.warning("radar post-rain freeze restarted across a clock step")
            self._last_poll_ts = None
            self._last_ok_ts = None         # wrong-clock frame must not read as fresh

    def component(self, sun_alt, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            fresh = (self._last_ok_ts is not None
                     and (now - self._last_ok_ts) <= self.cfg.RADAR_STALE_AFTER_SEC)
            confirmed = self._ring_streak >= self._trigger_after
            live_unsafe = fresh and self._in_ring and confirmed
            # FREEZE after the last in-ring detection: hold the veto for RADAR_LATCH_SEC
            # even once frames come back clear. Rain leaving the ring is not by
            # itself a reason to reopen — the cell can turn back, and the roof should not
            # chase the radar edge. The window is bounded, so a genuinely departed cell
            # (or a blind feed) self-clears instead of sticking. It also covers an echo
            # inside the ring but over no WU station, where WU never latches at all.
            # self-heal future-dated stamps at check time (backward clock step while
            # running); a negative age must read as "just now", not as eternal freeze
            if self._last_rain_ts is not None and self._last_rain_ts > now + 60:
                log.warning("radar rain timestamp in the future (clock step?) — clamping")
                self._last_rain_ts = now
                self._save_rain_ts()
            if self._last_ok_ts is not None and self._last_ok_ts > now + 60:
                self._last_ok_ts = now      # never present old data as freshly fetched
            latched = (self._last_rain_ts is not None
                       and (now - self._last_rain_ts) < self.cfg.RADAR_LATCH_SEC)
            # Pillow absent => radar never really ran; never veto from an artificial state.
            unsafe = deps_available() and (live_unsafe or latched)
            available = deps_available() and fresh
            polling_active = deps_available()      # radar polls day and night
            try:
                basemap, notes, warns, attribution = self._basemap_status()
            except Exception as e:      # noqa: BLE001 - decoration must never break IsSafe
                # component() runs inside monitor.evaluate(), i.e. on every /issafe: a bug
                # in the basemap description must cost the page a line, not NINA a 500
                if not getattr(self, "_basemap_status_failed", False):
                    self._basemap_status_failed = True
                    log.warning("radar basemap status unavailable (%s) — reported empty; the "
                                "rain check is unaffected", redact(e, self.cfg))
                basemap = {"night": None, "day": None, "night_inverted": False,
                           "chosen_by": {}}
                notes, warns = [], []
                attribution = str(getattr(self.cfg, "RADAR_ATTRIBUTION",
                                          _config.RADAR_ATTRIBUTION))
            return {
                # Unsafe on a fresh in-ring frame, OR while the post-rain freeze holds. A stale
                # frame with no recent detection is "unknown" (available False), no veto.
                # Observation fields are exported ONLY while fresh — a stale in_ring/nearest
                # must never be rendered as a current observation.
                "safe": not unsafe,
                "latched": bool(latched and not live_unsafe),
                # countdown of the post-rain freeze, so the page can say how long is left
                # instead of just asserting "recent rain"
                "seconds_remaining": (
                    max(0, round(self._last_rain_ts + self.cfg.RADAR_LATCH_SEC - now))
                    if latched else 0),
                "freeze_sec": self.cfg.RADAR_LATCH_SEC,
                "enabled": deps_available(),
                "available": available,
                "in_ring": bool(fresh and self._in_ring),
                # an echo seen but not yet confirmed: a real observation that is
                # deliberately NOT vetoing, so the page can say so instead of claiming
                # either "rain" or "no rain"
                "unconfirmed_echo": bool(fresh and self._in_ring and not confirmed),
                "ring_streak": self._ring_streak if fresh else 0,
                "trigger_after": self._trigger_after,
                "nearest_km": self._nearest_km if fresh else None,
                "pixels": self._count if fresh else 0,
                "frame_utc": self._frame_utc,
                "age_s": round(now - self._last_ok_ts) if self._last_ok_ts else None,
                "trigger_km": self.cfg.RADAR_TRIGGER_KM,
                "dbz": self.cfg.RADAR_DBZ,
                "polling_active": polling_active,
                "thumb_available": self._thumb_ok,
                "thumb_path": self.cfg.RADAR_THUMB_PATH,
                "thumb_path_day": (self.cfg.RADAR_THUMB_PATH_DAY
                                   if self.cfg.RADAR_DAY_ENABLED else None),
                # which basemap each map shows (BASEMAP_SOURCES; None = not built yet /
                # no day map), what to configure for a better one, and the credit line
                # for exactly those sources
                "basemap": basemap,
                "basemap_notes": notes,
                "basemap_warnings": warns,      # -> the monitor's "warnings" (no veto)
                "attribution": attribution,
                "source": "NOAA/NSSL MRMS composite reflectivity via IEM",
            }


def unavailable_component(cfg):
    return {
        "safe": True, "enabled": False, "available": False, "in_ring": False,
        "unconfirmed_echo": False, "ring_streak": 0,
        "trigger_after": getattr(cfg, "RADAR_TRIGGER_AFTER", 2),
        "latched": False, "seconds_remaining": 0, "freeze_sec": cfg.RADAR_LATCH_SEC,
        "nearest_km": None, "pixels": 0, "frame_utc": None, "age_s": None,
        "trigger_km": cfg.RADAR_TRIGGER_KM, "dbz": cfg.RADAR_DBZ, "polling_active": False,
        "thumb_available": False, "thumb_path": cfg.RADAR_THUMB_PATH, "thumb_path_day": None,
        "basemap": {"night": None, "day": None, "night_inverted": False, "chosen_by": {}},
        "basemap_notes": [], "basemap_warnings": [],
        "attribution": basemap_attribution(cfg, []),
        "source": "NOAA/NSSL MRMS composite reflectivity via IEM (disabled)",
    }
