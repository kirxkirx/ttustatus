"""Configuration for the TTU Alpaca SafetyMonitor daemon.

Every value can be overridden with an environment variable (TTU_SAFETY_*). The defaults
suit the observatory Raspberry Pi, where make_status_page.py runs from /home/kirx.
"""
from __future__ import annotations

import math
import os
import re
import urllib.parse

# Problems found while parsing the environment. Config is imported before logging is set
# up, so we collect messages here and server.main() logs them LOUDLY after basicConfig —
# a typo in an env var must never silently fall back to a default.
CONFIG_WARNINGS: list[str] = []


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    if name not in os.environ:
        return default
    try:
        v = float(os.environ[name])
    except ValueError:
        CONFIG_WARNINGS.append(f"{name}={os.environ[name]!r} is not a number — "
                               f"using default {default}")
        return default
    if not math.isfinite(v):
        CONFIG_WARNINGS.append(f"{name}={os.environ[name]!r} is not finite — "
                               f"using default {default}")
        return default
    return v


def _env_int(name: str, default: int) -> int:
    if name not in os.environ:
        return default
    try:
        return int(os.environ[name])
    except ValueError:
        CONFIG_WARNINGS.append(f"{name}={os.environ[name]!r} is not an integer — "
                               f"using default {default}")
        return default


def _clamp(name: str, value, lo, hi=None):
    """Bound a parsed value LOUDLY: an out-of-range setting is corrected with a startup
    warning, never silently obeyed (a 0 s poll interval would hammer api.weather.gov, a
    fill alpha of 900 would break the map renderer)."""
    if value < lo or (hi is not None and value > hi):
        fixed = lo if value < lo else hi
        CONFIG_WARNINGS.append(f"{name}={value!r} is outside [{lo}, "
                               f"{'inf' if hi is None else hi}] — using {fixed}")
        return fixed
    return value


# --- Weather Underground (rain input) --------------------------------------
# REQUIRED for rain polling. NEVER hardcode a key here — it would end up on GitHub.
# Set the TTU_SAFETY_WU_KEY environment variable instead (see README_SAFETY.md). If it's
# empty, the daemon still runs (sun/humidity protection) but rain polling is disabled.
WU_API_KEY = _env_str("TTU_SAFETY_WU_KEY", "")
# ttustatus.env.example's value, copied without editing. Only warned about: the key is
# still used as given (WU then refuses it and the rain layer reports itself unavailable,
# exactly as for any other wrong key) — treating it as "unset" would switch rain off.
WU_KEY_TEMPLATE = "your-weather-underground-pws-api-key-here"
if WU_API_KEY.strip() == WU_KEY_TEMPLATE:
    CONFIG_WARNINGS.append("TTU_SAFETY_WU_KEY is still the ttustatus.env.example "
                           "placeholder — paste your Weather Underground API key")

# --- site coordinates -------------------------------------------------------
# Priority: TTU_SAFETY_LAT/LON env vars > GPS fix adopted from make_status_page.py's
# inputs file (once, at first fresh reading) > this built-in TTU default. Coordinates are
# ROUNDED to 0.001 deg (~100 m) so GPS jitter never changes the value — the derived NWS
# grid, WU station set, and cached radar basemap therefore stay stable across restarts;
# only a genuine site move (>~100 m) re-derives them. The station is assumed static.
GEOCODE_ROUND_DECIMALS = 3


def round_coords(lat: float, lon: float) -> tuple:
    return (round(lat, GEOCODE_ROUND_DECIMALS), round(lon, GEOCODE_ROUND_DECIMALS))


_LAT_SET = "TTU_SAFETY_LAT" in os.environ
_LON_SET = "TTU_SAFETY_LON" in os.environ
GEOCODE_FROM_ENV = _LAT_SET or _LON_SET
GEOCODE = round_coords(_env_float("TTU_SAFETY_LAT", 33.7483333),
                       _env_float("TTU_SAFETY_LON", -101.9584001))   # TTU observatory
if _LAT_SET and _LON_SET:
    GEOCODE_SOURCE = "env"
elif GEOCODE_FROM_ENV:
    # exactly one of the pair set: the other half silently came from the TTU default —
    # say so, loudly (the label is shown on /setup, the warning at startup)
    GEOCODE_SOURCE = "env (INCOMPLETE — other coordinate from built-in default!)"
    CONFIG_WARNINGS.append("only one of TTU_SAFETY_LAT/TTU_SAFETY_LON is set — "
                           "the other comes from the built-in TTU default")
else:
    GEOCODE_SOURCE = "built-in default (TTU)"
GEOCODE_MISMATCH_KM = _env_float("TTU_SAFETY_GEO_MISMATCH_KM", 0.1)  # warn beyond ~100 m
WU_MAX_STATION_KM = _env_float("TTU_SAFETY_WU_MAX_KM", 60.0)  # drop 'nearest' beyond this
# Stations excluded from rain detection (comma-separated IDs, case-insensitive).
# Reserve this list for stations that LIE — merely dead hardware is handled by the
# automatic backoff (WU_BACKOFF_AFTER below), which probes it hourly and restores it
# the moment it answers. This list does not self-heal; entries stay until removed.
#   KTXSHALL25 (2026-08-29): reports precipitation under a radar-clear sky.
WU_EXCLUDE_STATIONS = frozenset(
    x.strip().upper()
    for x in _env_str("TTU_SAFETY_WU_EXCLUDE", "KTXSHALL25").split(",") if x.strip())
WU_POLL_INTERVAL = _env_int("TTU_SAFETY_POLL_INTERVAL", 600)  # s between WU polls
# Automatic dead-station backoff: after this many CONSECUTIVE no-response polls a
# station is probed only every WU_BACKOFF_RETRY_SEC instead of every cycle (saves API
# calls on dead hardware), and rejoins the full cadence by itself on the first answer —
# unlike the manual WU_EXCLUDE_STATIONS list, this self-heals.
WU_BACKOFF_AFTER = _env_int("TTU_SAFETY_WU_BACKOFF_AFTER", 6)       # ~1 h at night cadence
WU_BACKOFF_RETRY_SEC = _env_int("TTU_SAFETY_WU_BACKOFF_RETRY", 3600)
WU_DAILY_BUDGET = 1500          # PWS-owner cap (calls/day); informational only
MAX_OBS_AGE_MIN = 30            # ignore a station reading older than this
HTTP_TIMEOUT = 15               # s per WU request
RAIN_THRESHOLD = 0.0            # in/hr; precipRate strictly greater than this = rain

# --- safety thresholds ------------------------------------------------------
SUN_UNSAFE_ABOVE_DEG = 0.0      # unsafe when sun altitude > 0 (no refraction/size)
RAIN_POLL_SUN_BELOW_DEG = 5.0   # only poll WU when sun altitude < this (save calls)
HUMIDITY_UNSAFE_ABOVE = 95.0    # unsafe when humidity > 95 %
HUMIDITY_CLEAR_BELOW = 93.0     # hysteresis: clear humidity-unsafe below this
# Freeze time after the LAST rain reading at a WU station: hold unsafe this long, then
# clear if nothing else is still triggering.
RAIN_LATCH_HOURS = _env_float("TTU_SAFETY_RAIN_LATCH_HOURS", 1.0)
INPUTS_STALE_SEC = _env_int("TTU_SAFETY_INPUTS_STALE_SEC", 600)  # older => fail safe
CLOCK_SKEW_TOLERANCE_SEC = 5    # future-dated inputs beyond this => also stale

# --- NWS forecast component (pre-emptive cloud/precip/thunder) --------------
# Pulls the raw NWS gridpoint forecast every NWS_POLL_INTERVAL and flags UNSAFE if THIS
# hour or NEXT hour breaches any threshold. Free, no key (NWS asks for a User-Agent). A
# fetch error or stale forecast is treated as "unavailable" (does not by itself flip unsafe
# — it's a forecast, not a local sensor); a breach in a fresh forecast DOES flip unsafe.
NWS_ENABLED = _env_str("TTU_SAFETY_NWS", "1").strip().lower() not in ("0", "false", "no")
# ONE User-Agent for every request the daemon makes to NWS, IEM, the hazard feeds and the
# basemap tile servers. Put a REAL contact e-mail in it, e.g.
#   ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; you@example.org)
# with you@example.org replaced by YOUR address. NWS uses the contact to reach you about
# your traffic, and OpenStreetMap (the backup basemap) blocks a User-Agent that carries a
# placeholder copied from an example, such as you@example.org (checked 2026-09-24: an
# "access blocked" tile, refused by radar._get) — so no OSM tile is requested with one,
# and a map with no CARTO basemap (cached or keyed) then has none. The bare default names
# the app but gives no contact.
NWS_USER_AGENT = _env_str("TTU_SAFETY_NWS_UA", "ttu-safety-monitor")
# Placeholder contacts left over from an example (you@example.org, your.name@ttu.edu,
# CONTACT_EMAIL). Matched case-insensitively. "you@" and "your.name@" count only as a
# whole local part: zhouyou@ttu.edu is a real address, not a placeholder. (A contact
# with no "@" at all, such as "<your e-mail>", is not an address: ua_has_real_email.)
UA_PLACEHOLDER_RE = re.compile(
    r"example\.(?:org|com|net)\b"
    r"|(?<![\w.%+-])(?:you|your[._-]?(?:name|e-?mail|address))@"
    r"|CONTACT_EMAIL", re.IGNORECASE)
_UA_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def ua_has_placeholder(ua) -> bool:
    """A placeholder contact (you@example.org, CONTACT_EMAIL, ...) in the User-Agent."""
    return bool(UA_PLACEHOLDER_RE.search(str(ua or "")))


def ua_has_real_email(ua) -> bool:
    """An address-shaped contact that is not a placeholder."""
    return bool(_UA_EMAIL_RE.search(str(ua or ""))) and not ua_has_placeholder(ua)


if ua_has_placeholder(NWS_USER_AGENT):
    CONFIG_WARNINGS.append(
        f"TTU_SAFETY_NWS_UA={NWS_USER_AGENT!r} carries a placeholder contact — "
        "OpenStreetMap blocks such requests, so no OSM basemap tiles are fetched; put your "
        "real e-mail address in it (NWS asks for a contact too)")
NWS_GRID = _env_str("TTU_SAFETY_NWS_GRID", "")   # e.g. "LUB/46,41"; empty => resolve via GEOCODE
NWS_POLL_INTERVAL = _env_int("TTU_SAFETY_NWS_POLL_INTERVAL", 900)     # 15 min
NWS_STALE_AFTER_MIN = _env_int("TTU_SAFETY_NWS_STALE_MIN", 150)
NWS_CLOUD_MAX = _env_float("TTU_SAFETY_NWS_CLOUD_MAX", 45.0)          # % ; unsafe when >
NWS_PRECIP_PROB_MAX = _env_float("TTU_SAFETY_NWS_PRECIP_MAX", 20.0)   # % ; unsafe when >
NWS_THUNDER_PROB_MAX = _env_float("TTU_SAFETY_NWS_THUNDER_MAX", 15.0)  # % ; unsafe when >
NWS_RENDER_HOURS = _env_int("TTU_SAFETY_NWS_RENDER_HOURS", 12)        # 12-h table (display)
LOCAL_TZ = _env_str("TTU_SAFETY_LOCAL_TZ", "America/Chicago")         # for the render table

# --- GLM lightning component (GOES-19 total lightning via AWS S3) -----------
# Every GLM_POLL_INTERVAL (night only, sun below the gate) fetches the last GLM_WINDOW_MIN
# of GLM granules IN RAM (no SD writes) and LATCHES unsafe for GLM_COOLOFF_HOURS if any
# flash is within GLM_TRIGGER_KM. The slow S3/netCDF work runs in its own thread; evaluate()
# only reads the cached latch, so it never blocks page/monitor refresh. Needs numpy+netCDF4
# (apt: python3-numpy python3-netcdf4); if absent, GLM is disabled (other layers unaffected).
GLM_ENABLED = _env_str("TTU_SAFETY_GLM", "1").strip().lower() not in ("0", "false", "no")
GLM_BUCKET = _env_str("TTU_SAFETY_GLM_BUCKET", "noaa-goes19")
GLM_TRIGGER_KM = _env_float("TTU_SAFETY_GLM_TRIGGER_KM", 50.0)
# Freeze time after the last in-range flash (30 min — lightning warrants a longer
# hold than the 15 min radar rain freeze).
GLM_COOLOFF_HOURS = _env_float("TTU_SAFETY_GLM_COOLOFF_HOURS", 0.5)
GLM_POLL_INTERVAL = _env_int("TTU_SAFETY_GLM_POLL_INTERVAL", 300)     # 5 min
GLM_WINDOW_MIN = _env_int("TTU_SAFETY_GLM_WINDOW_MIN", 5)             # look-back per poll
GLM_POLL_SUN_BELOW_DEG = _env_float("TTU_SAFETY_GLM_SUN_BELOW", 5.0)  # night gate (like WU)
GLM_LATCH_FILE = _env_str("TTU_SAFETY_GLM_LATCH_FILE",
                          os.path.expanduser("~/safety_glm_latch.json"))
GLM_STALE_AFTER_SEC = _env_int("TTU_SAFETY_GLM_STALE_SEC", 900)  # older poll => no data

# --- MRMS radar (rain within a radius) --------------------------------------
# Every RADAR_POLL_INTERVAL (DAY AND NIGHT — free data) fetch the latest MRMS composite
# reflectivity and declare UNSAFE if ANY echo >= RADAR_DBZ is within RADAR_TRIGGER_KM of
# the observatory. Deliberately simple — a plain 30 km "any rain" ring, no upwind logic.
# Also renders a TTU-centered radar thumbnail (a CARTO or OpenStreetMap basemap, see the
# basemap block below, cached to disk) with the 30 km ring + scale bars for the status
# page. Needs Pillow (apt: python3-pil); absent => radar disabled (other layers
# unaffected). A fetch error/stale frame => unavailable (does not by itself force
# unsafe); the radar keeps its own post-rain freeze (RADAR_LATCH_SEC).
RADAR_ENABLED = _env_str("TTU_SAFETY_RADAR", "1").strip().lower() not in ("0", "false", "no")
RADAR_TRIGGER_KM = _env_float("TTU_SAFETY_RADAR_KM", 30.0)
RADAR_DBZ = _env_float("TTU_SAFETY_RADAR_DBZ", 20.0)        # echo >= this = rain
RADAR_POLL_INTERVAL = _env_int("TTU_SAFETY_RADAR_POLL_INTERVAL", 300)   # 5 min
# CONFIRMATION: how many CONSECUTIVE polls must see an in-ring echo before the radar
# vetoes. MRMS composites occasionally carry a single-frame artefact (aircraft, anomalous
# propagation, a ground-clutter or de-aliasing glitch), and one such frame should not
# close the dome. Costs one poll interval (~5 min) of extra latency on real rain, which is
# affordable for a 30 km early-warning ring — the close-in layers (WU stations, GLM) are
# unaffected. Set to 1 to trigger on a single frame again.
RADAR_TRIGGER_AFTER = _env_int("TTU_SAFETY_RADAR_TRIGGER_AFTER", 2)
# NOTE: radar polls day AND night (free data, daytime rain matters, live map) — unlike
# the WU/GLM night gates.
RADAR_STALE_AFTER_SEC = _env_int("TTU_SAFETY_RADAR_STALE_SEC", 1200)  # older => unavailable
# FREEZE TIME after the last in-ring detection: hold the veto this long even if later
# frames are clear, so the dome does not reopen the moment a cell's leading edge leaves
# the ring (and so a ranged echo can't reopen it when IEM goes blind, since a cell inside
# the ring may sit over no WU station at all).
RADAR_LATCH_SEC = _env_int("TTU_SAFETY_RADAR_LATCH_SEC", 900)   # 15 min post-rain freeze
RADAR_LATCH_FILE = _env_str("TTU_SAFETY_RADAR_LATCH_FILE",
                            os.path.expanduser("~/safety_radar_latch.json"))
# thumbnail: written where the status page can load it (beside status.html on the Pi)
RADAR_THUMB_PATH = _env_str("TTU_SAFETY_RADAR_THUMB", "/var/www/html/ttu_radar.png")


def _day_variant(path):
    base, ext = os.path.splitext(path)
    return base + "_day" + ext


RADAR_THUMB_PATH_DAY = _env_str("TTU_SAFETY_RADAR_THUMB_DAY", _day_variant(RADAR_THUMB_PATH))
RADAR_THUMB_HALF_DEG = _env_float("TTU_SAFETY_RADAR_THUMB_HALF", 1.0)  # region half-size
RADAR_THUMB_PX = _env_int("TTU_SAFETY_RADAR_THUMB_PX", 440)
RADAR_TILE_ZOOM = _env_int("TTU_SAFETY_RADAR_TILE_ZOOM", 8)
# --- radar basemap: CARTO first, OpenStreetMap as the backup ---
# The basemap is fetched once per site and theme (~9 tiles per map at zoom 8) and the
# composite is cached in RADAR_CACHE_DIR for good, so tile traffic is a handful of
# requests per site, not per poll. Each map (night, day) uses the FIRST source that works:
#  1. A CARTO composite already cached on this Pi (basemap_<hash>.png in RADAR_CACHE_DIR,
#     the name the code has always given it; on the observatory Pi these predate CARTO's
#     watermark — a watermarked one is deleted by hand, see README.md): no network at
#     all. CARTO Dark Matter at night, Positron by day. The <hash> covers the site
#     (GEOCODE), RADAR_THUMB_HALF_DEG, RADAR_THUMB_PX, RADAR_TILE_ZOOM and the key-free
#     tile URL: changing any of them leaves the cached CARTO maps unused.
#  2. CARTO tiles fetched WITH CARTO_API_KEY, only when a key is set (no CARTO tile is
#     ever requested without one). Since 2026-09 CARTO answers every tile requested
#     without a valid key (none, or a bogus one) with an "API KEY REQUIRED" watermark
#     under HTTP 200 and a 6-month Cache-Control, so the headers cannot tell a good tile
#     from a watermarked one; the bytes can. One probe tile is therefore fetched with
#     AND without the key first: identical bytes, or a keyed request CARTO refuses (HTTP
#     401/403), mean the key is not accepted; a failed fetch means it could not be
#     checked. Either way it is logged (key redacted), shown as a warning, not re-probed
#     for 6 h, and the chain moves on. Then every other keyed tile is compared with its
#     unkeyed twin too (the watermark differs per tile), so a watermarked tile — a key
#     revoked or a quota spent mid-build — is never drawn, let alone cached. A complete
#     keyed build is cached under the SAME key-free name as step 1 (the key never
#     reaches a file name, the state file, the page or a log line); an incomplete one is
#     retried in 30 min.
#  3. OpenStreetMap standard tiles: key-free, the backup. The OSM tile policy wants a
#     User-Agent that identifies the app (TTU_SAFETY_NWS_UA, above) and OSM blocks one
#     with a placeholder contact; with such a UA no OSM tile is requested at all (a
#     composite cached earlier needs no request and is still used). The night map
#     inverts the tiles' lightness (RADAR_TILE_DARK_INVERT). Cached like CARTO.
#  4. Nothing: a plain background; the page says so, and the build is retried every 30 min.
# RADAR_BASEMAP picks the chain: "auto" = 1-2-3-4, "carto" = 1-2-4 (never OSM),
# "osm" = 3-4 (never CARTO, not even the cached composites).
# CARTO keys are free (no account; requested by e-mail at https://carto.com/basemaps/apikey,
# free up to 5M tile requests a month for non-commercial use); the key is sent as
# ?key=... on every CARTO tile URL. Required credit: "© OpenStreetMap contributors, © CARTO".
CARTO_API_KEY = _env_str("TTU_SAFETY_CARTO_KEY", "").strip()   # a SECRET: never logged
RADAR_BASEMAP = _env_str("TTU_SAFETY_RADAR_BASEMAP", "auto").strip().lower() or "auto"
if RADAR_BASEMAP not in ("auto", "carto", "osm"):
    CONFIG_WARNINGS.append(f"TTU_SAFETY_RADAR_BASEMAP={RADAR_BASEMAP!r} is not one of "
                           "auto / carto / osm — using auto")
    RADAR_BASEMAP = "auto"
# These two exact URLs matter beyond fetching: the cached composites of step 1 are named
# after them (see radar._cache_key), so changing them would orphan the Pi's CARTO maps.
CARTO_TILE_URL = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"       # night
CARTO_TILE_URL_DAY = "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"  # day
OSM_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
# Query parameters that carry an API key in a tile URL (also redacted in every log line).
KEY_QUERY_PARAMS = ("key", "apikey", "api_key")


def tile_host_kind(url) -> str:
    """What a tile URL's host is, which decides how its tiles are fetched (radar.py):
    'carto' = basemaps.cartocdn.com and its subdomains (every style, any path: only ever
    fetched with the key, through the key check of step 2, and never inverted);
    'osm' = tile.openstreetmap.org / tile.osm.org and their subdomains (the OSM tile
    policy: the User-Agent check of step 3); 'custom' = anything else."""
    try:
        host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        host = ""
    if host == "basemaps.cartocdn.com" or host.endswith(".basemaps.cartocdn.com"):
        return "carto"
    if host in ("openstreetmap.org", "osm.org") or host.endswith((".openstreetmap.org",
                                                                  ".osm.org")):
        return "osm"
    return "custom"


def split_key_param(url):
    """(the URL without its key= / apikey= / api_key= query parameters, the first such
    value URL-decoded, or ''). Plain string surgery: a tile template's {z}/{x}/{y} and
    any other parameter stay exactly as written."""
    url = str(url or "")
    base, sep, query = url.partition("?")
    if not sep:
        return url, ""
    kept, key = [], ""
    for part in query.split("&"):
        name, _, value = part.partition("=")
        if name.lower() in KEY_QUERY_PARAMS:
            key = key or urllib.parse.unquote(value)
            continue
        if part:
            kept.append(part)
    return base + ("?" + "&".join(kept) if kept else ""), key


def _carto_url(var, url):
    """A CARTO URL with a key written into it (as the docs once advised for keyed
    services): (the URL without it, the key). CARTO URLs always go through the key check
    of step 2, whose unkeyed twin and cache name are the key-free URL, so the key is
    moved out; it serves as TTU_SAFETY_CARTO_KEY when that is unset. Never logged."""
    if tile_host_kind(url) != "carto":
        return url, ""
    bare, key = split_key_param(url)
    if bare != url:
        CONFIG_WARNINGS.append(f"{var} has an API key in it — a CARTO key belongs in "
                               "TTU_SAFETY_CARTO_KEY; the URL is used without it"
                               + (" and the key as TTU_SAFETY_CARTO_KEY"
                                  if key and not CARTO_API_KEY else ""))
    return bare, key


# An explicit CUSTOM tile template for a map (TTU_SAFETY_RADAR_TILE_URL / _DAY; unset or
# empty = the CARTO URL above). How it is used depends on its host (tile_host_kind):
#  * another CARTO URL (other subdomain or style: dark_nolabels, rastertiles/voyager,
#    @2x, ...): CARTO rules — its own cached composite, then its tiles fetched ONLY with
#    the key and through the key check of step 2 (a key written into the URL is moved to
#    CARTO_API_KEY, see _carto_url); never fetched without a key.
#  * an OpenStreetMap URL: OpenStreetMap ONLY for that map, even in "auto" (step 3 with
#    that URL: the cached CARTO maps are not used); in "carto" mode it is ignored and the
#    map uses the CARTO chain.
#  * anything else: takes CARTO's place in steps 1-2 (its own cached composite, then its
#    tiles fetched exactly as given — no key is added, so a keyed non-CARTO service
#    needs its key written into the URL); OSM stays the fallback in "auto" mode.
RADAR_TILE_URL, _key_night = _carto_url(
    "TTU_SAFETY_RADAR_TILE_URL", _env_str("TTU_SAFETY_RADAR_TILE_URL", "").strip()
    or CARTO_TILE_URL)
# Daytime (light) version of the map, shown when the status page is in day style.
RADAR_DAY_ENABLED = _env_str("TTU_SAFETY_RADAR_DAY", "1").strip().lower() not in ("0", "false", "no")
RADAR_TILE_URL_DAY, _key_day = _carto_url(
    "TTU_SAFETY_RADAR_TILE_URL_DAY", _env_str("TTU_SAFETY_RADAR_TILE_URL_DAY", "").strip()
    or CARTO_TILE_URL_DAY)
CARTO_API_KEY = CARTO_API_KEY or _key_night or _key_day
del _key_night, _key_day
if RADAR_BASEMAP == "osm" and any(
        tile_host_kind(u) != "osm" and u not in (CARTO_TILE_URL, CARTO_TILE_URL_DAY)
        for u in (RADAR_TILE_URL, RADAR_TILE_URL_DAY)):
    CONFIG_WARNINGS.append("TTU_SAFETY_RADAR_BASEMAP=osm: the custom "
                           "TTU_SAFETY_RADAR_TILE_URL(_DAY) is not used")
if RADAR_BASEMAP == "osm" and CARTO_API_KEY:
    CONFIG_WARNINGS.append("TTU_SAFETY_CARTO_KEY is set but TTU_SAFETY_RADAR_BASEMAP=osm: "
                           "CARTO is never used")
if RADAR_BASEMAP == "carto" and any(tile_host_kind(u) == "osm"
                                    for u in (RADAR_TILE_URL, RADAR_TILE_URL_DAY)):
    CONFIG_WARNINGS.append("TTU_SAFETY_RADAR_BASEMAP=carto: the OpenStreetMap "
                           "TTU_SAFETY_RADAR_TILE_URL(_DAY) is not used — that map uses "
                           "the CARTO chain")
# Night map from light tiles (OpenStreetMap, or a light non-CARTO custom set): each tile
# whose mean luminance is light gets its lightness inverted (hue kept: white land turns
# near-black, black labels white). Dark tiles are left alone, tile by tile. CARTO tiles
# are never inverted: the night map is Dark Matter already, and the cached CARTO
# composites were built uninverted.
RADAR_TILE_DARK_INVERT = (_env_str("TTU_SAFETY_RADAR_TILE_DARK_INVERT", "1").strip().lower()
                          not in ("0", "false", "no"))
RADAR_CACHE_DIR = _env_str("TTU_SAFETY_RADAR_CACHE", os.path.expanduser("~/.cache/ttu-radar"))
# Write the thumbnails into /dev/shm and leave a one-time symlink at RADAR_THUMB_PATH:
# the two PNGs are rewritten every poll (~150-250 MB/day) — the single largest SD write
# of the whole stack, on a Pi whose brownouts corrupt SD cards mid-write. Apache, nginx
# and lighttpd all follow symlinks out of the box on Debian. Set 0 to write directly.
RADAR_THUMB_VIA_SHM = (_env_str("TTU_SAFETY_RADAR_THUMB_SHM", "1").strip().lower()
                       not in ("0", "false", "no")) and os.path.isdir("/dev/shm")
# The radar-data credit. The map's full attribution line is COMPUTED from the basemap
# actually drawn (radar.basemap_attribution): "© OpenStreetMap contributors, © CARTO"
# when a CARTO map is shown, "© OpenStreetMap contributors" for OSM alone, then this.
RADAR_ATTRIBUTION = "Radar: NOAA/NSSL MRMS via IEM"
# (Persistence note: the radar keeps its OWN post-rain freeze above — it does NOT rely on the
# WU rain latch, which only arms when rain reaches a nearby station, not for ranged echoes.)

# --- NWS hazards: active alerts (one narrow veto) + hazard information -------
# Two layers, deliberately unequal (safety/nws_alerts.py and safety/hazard_feeds.py):
#  * NWS ACTIVE ALERTS. Every active warning / watch / advisory / statement touching the
#    radar map is listed on the status page and drawn on the map. ONLY the events named in
#    HAZARD_VETO_EVENTS make the monitor UNSAFE, and only while one is in effect OVER THE
#    SITE (NWS's own point query, or the warning's polygon / the site's zones) — for the
#    whole life of the warning, persisted across restarts like the other latches. It is
#    released early only when FRESH point AND area queries both say the warning is gone;
#    a feed outage holds it to the warning's own end time, never longer.
#  * HAZARD INFORMATION (USGS quakes, NOAA HMS smoke, NIFC fires, SPC outlook and
#    mesoscale discussions, local storm reports, space weather) is INFORMATION ONLY: shown
#    on the page and the map, never part of IsSafe.
# With no veto held, an unreachable alert feed is "unavailable" and does not veto on its
# own — like the forecast and radar layers; the connectivity watchdog covers total loss.
HAZARDS_ENABLED = _env_str("TTU_SAFETY_HAZARDS", "1").strip().lower() not in ("0", "false", "no")

# Every event name api.weather.gov/alerts/types listed on 2026-09-24, plus the legacy names
# retired by the 2024-25 heat/cold renaming. Used ONLY to catch a typo in
# HAZARD_VETO_EVENTS — "Tornado Warnign" would otherwise silently never veto — so an
# unknown name is KEPT (NWS does rename products) and announced loudly at startup.
NWS_EVENT_NAMES = frozenset(n.strip().lower() for n in (
    "911 Telephone Outage,Administrative Message,Air Quality Alert,Air Stagnation Advisory,"
    "Ashfall Advisory,Ashfall Warning,Avalanche Advisory,Avalanche Warning,Avalanche Watch,"
    "Beach Hazards Statement,Blizzard Warning,Blowing Dust Advisory,Blowing Dust Warning,"
    "Blue Alert,Brisk Wind Advisory,Child Abduction Emergency,Civil Danger Warning,"
    "Civil Emergency Message,Coastal Flood Advisory,Coastal Flood Statement,"
    "Coastal Flood Warning,Coastal Flood Watch,Cold Weather Advisory,Dense Fog Advisory,"
    "Dense Smoke Advisory,Dust Advisory,Dust Storm Warning,Earthquake Warning,"
    "Evacuation Immediate,Extreme Heat Warning,Extreme Heat Watch,Extreme Cold Warning,"
    "Extreme Cold Watch,Extreme Fire Danger,Extreme Wind Warning,Fire Warning,"
    "Fire Weather Watch,Flash Flood Statement,Flash Flood Warning,Flash Flood Watch,"
    "Flood Advisory,Flood Statement,Flood Warning,Flood Watch,Freeze Warning,Freeze Watch,"
    "Freezing Fog Advisory,Freezing Spray Advisory,Frost Advisory,Gale Warning,Gale Watch,"
    "Hazardous Materials Warning,Hazardous Seas Warning,Hazardous Seas Watch,"
    "Hazardous Weather Outlook,Heat Advisory,Heavy Freezing Spray Warning,"
    "Heavy Freezing Spray Watch,High Surf Advisory,High Surf Warning,High Wind Warning,"
    "High Wind Watch,Hurricane Force Wind Warning,Hurricane Force Wind Watch,"
    "Hurricane Warning,Hurricane Watch,Hydrologic Outlook,Ice Storm Warning,"
    "Lake Effect Snow Warning,Lake Wind Advisory,Lakeshore Flood Advisory,"
    "Lakeshore Flood Statement,Lakeshore Flood Warning,Lakeshore Flood Watch,"
    "Law Enforcement Warning,Local Area Emergency,Low Water Advisory,"
    "Marine Weather Statement,Nuclear Power Plant Warning,Radiological Hazard Warning,"
    "Red Flag Warning,Rip Current Statement,Severe Thunderstorm Warning,"
    "Severe Thunderstorm Watch,Severe Weather Statement,Shelter In Place Warning,"
    "Short Term Forecast,Small Craft Advisory,Snow Squall Warning,Special Marine Warning,"
    "Special Weather Statement,Storm Surge Warning,Storm Surge Watch,Storm Warning,"
    "Storm Watch,Test,Tornado Warning,Tornado Watch,Tropical Cyclone Local Statement,"
    "Tropical Storm Warning,Tropical Storm Watch,Tsunami Advisory,Tsunami Warning,"
    "Tsunami Watch,Typhoon Warning,Typhoon Watch,Volcano Warning,Wind Advisory,"
    "Winter Storm Warning,Winter Storm Watch,Winter Weather Advisory,"
    # legacy names (pre-2025), still possible in old configs
    "Excessive Heat Warning,Excessive Heat Watch,Wind Chill Warning,Wind Chill Watch,"
    "Wind Chill Advisory"
).split(",") if n.strip())


def _parse_event_names(raw: str, var: str = "TTU_SAFETY_HAZARD_VETO_EVENTS") -> tuple:
    """'tornado warning, Dust  Storm Warning' -> ('Tornado Warning', 'Dust Storm Warning').

    Matching is case-insensitive everywhere; the names are nevertheless stored in NWS's
    own Title Case (every CAP event name is Title Case), so even an exact comparison
    matches and the page shows them the way NWS spells them. Duplicates are dropped; a
    name NWS does not know is kept but warned about (see NWS_EVENT_NAMES)."""
    out, seen = [], set()
    for part in (raw or "").split(","):
        words = part.split()
        if not words:
            continue
        canon = " ".join(w[:1].upper() + w[1:].lower() for w in words)
        if canon.lower() in seen:
            continue
        seen.add(canon.lower())
        if canon.lower() not in NWS_EVENT_NAMES:
            CONFIG_WARNINGS.append(f"{var}: {canon!r} is not a known NWS event name — "
                                   f"check the spelling (kept, but it can only veto if "
                                   f"NWS issues an event with exactly this name)")
        out.append(canon)
    return tuple(out)


# NWS event names (comma-separated, case-insensitive) that make the monitor UNSAFE while
# one is in effect OVER THE SITE. The default three are the owner's choice — each means
# conditions that endanger an open dome at this very spot: a tornado; a haboob (near-zero
# visibility, abrasive dust driven into the optics); damaging wind (sustained 40+ mph or
# gusts 58+ mph). Everything else NWS issues — Severe Thunderstorm Warnings included,
# whose rain, hail and lightning the radar, GLM and WU layers already catch — is shown,
# never gated on. Empty => every alert is display-only (warned loudly at startup).
HAZARD_VETO_EVENTS = _parse_event_names(_env_str(
    "TTU_SAFETY_HAZARD_VETO_EVENTS", "Tornado Warning,Dust Storm Warning,High Wind Warning"))
# A veto warning issued AHEAD of its onset (a High Wind Warning "from 10 AM Friday" is often
# issued the afternoon before) vetoes from this long before its onset — time to park and
# close — not from issuance: the owner asked for "the duration of the warning", and the
# dome must not stay shut the whole night before a daytime wind event. Until then it is
# shown as "scheduled". Tornado and Dust Storm Warnings take effect when issued, so this
# never delays them, and a warning without an onset time vetoes at once.
HAZARD_VETO_ONSET_LEAD_SEC = _clamp("TTU_SAFETY_HAZARD_VETO_ONSET_LEAD_SEC",
                                    _env_int("TTU_SAFETY_HAZARD_VETO_ONSET_LEAD_SEC", 900), 0)
# Cadences. The POINT query (/alerts/active?point=<site>: NWS's own answer to "what is in
# effect HERE", a few hundred bytes) is the veto's fast path, so it runs every minute — a
# tornado warning here typically lasts only ~35 min. The AREA query (the map and the
# nearby list: every alert in TX/NM/OK, ~8 KB gzipped on a quiet day, 50-150 KB on a busy
# one) runs every 2 min, together with a national query of the civil emergency message
# types (usually empty, ~200 bytes). Zone outlines for zone-based alerts are fetched once
# per zone, ever (HAZARD_CACHE_DIR).
HAZARD_POINT_POLL_SEC = _clamp("TTU_SAFETY_HAZARD_POINT_POLL_SEC",
                               _env_int("TTU_SAFETY_HAZARD_POINT_POLL_SEC", 60), 30)
HAZARD_AREA_POLL_SEC = _clamp("TTU_SAFETY_HAZARD_AREA_POLL_SEC",
                              _env_int("TTU_SAFETY_HAZARD_AREA_POLL_SEC", 120), 60)
# A query result older than this is no longer "current": with no veto held the layer
# reports unavailable (no veto on its own), and a held veto can only run out at the
# warning's end time. At least two intervals of the SLOWER query, so one slow poll never
# flaps it — a stale time shorter than the area cadence would blank the map and the
# nearby list (and forbid every early release) for part of each area-poll cycle.
HAZARD_STALE_AFTER_SEC = _clamp("TTU_SAFETY_HAZARD_STALE_SEC",
                                _env_int("TTU_SAFETY_HAZARD_STALE_SEC", 600),
                                2 * max(HAZARD_POINT_POLL_SEC, HAZARD_AREA_POLL_SEC))
# The veto is PERSISTED — a daemon restart mid-warning must not reopen the dome. A small
# JSON file on the SD card, rewritten only when the set of vetoing warnings changes (a
# few writes per warning, never per poll).
HAZARD_LATCH_FILE = _env_str("TTU_SAFETY_HAZARD_LATCH_FILE",
                             os.path.expanduser("~/safety_hazard_latch.json"))
# Outlines of forecast/county/fire zones, for the zone-based alerts that carry no polygon
# (watches; wind, winter, heat, fire-weather products). A zone's shape practically never
# changes, so each is fetched ONCE and kept on disk — like the radar basemap tiles —
# instead of re-downloaded per alert or per boot.
HAZARD_CACHE_DIR = _env_str("TTU_SAFETY_HAZARD_CACHE",
                            os.path.expanduser("~/.cache/ttu-hazards"))


# The api.weather.gov ?area= codes this daemon accepts: the 50 states, DC and the
# territories (USPS codes; the same keys as nws_alerts.STATE_BOXES — a test pins that).
# Marine areas are left out: marine-only products are never shown.
US_AREA_CODES = frozenset((
    "AK,AL,AR,AS,AZ,CA,CO,CT,DC,DE,FL,GA,GU,HI,IA,ID,IL,IN,KS,KY,LA,MA,MD,ME,MI,MN,MO,MP,"
    "MS,MT,NC,ND,NE,NH,NJ,NM,NV,NY,OH,OK,OR,PA,PR,RI,SC,SD,TN,TX,UT,VA,VI,VT,WA,WI,WV,WY"
).split(","))


def _parse_states(raw: str, var: str = "TTU_SAFETY_HAZARD_AREA_STATES") -> str:
    """'auto', or a normalized comma list of state codes ('tx, nm' -> 'TX,NM').

    api.weather.gov rejects the WHOLE area query (HTTP 400) when one code is unknown, so a
    typo ('NW' for 'NM') would silently cost the map, the nearby list, the local site test
    and every early veto release: unknown codes are dropped with a startup warning, and
    'auto' is used when nothing valid is left."""
    v = (raw or "").strip()
    if not v or v.lower() == "auto":
        return "auto"
    codes = list(dict.fromkeys(c.strip().upper() for c in v.split(",") if c.strip()))
    bad = [c for c in codes if c not in US_AREA_CODES]
    good = [c for c in codes if c in US_AREA_CODES]
    if bad:
        CONFIG_WARNINGS.append(f"{var}: ignoring unknown state code(s) {', '.join(bad)} "
                               f"(api.weather.gov would reject the whole query)"
                               + ("" if good else " — using 'auto'"))
    return ",".join(good) if good else "auto"


# States the area query covers: "auto" = the states whose bounding boxes intersect the
# radar map (for TTU: TX, NM, OK — the map's west edge is a few km from New Mexico),
# re-derived if the site coordinates change; or an explicit list such as "TX,NM".
HAZARD_AREA_STATES = _parse_states(_env_str("TTU_SAFETY_HAZARD_AREA_STATES", "auto"))
# Map styling: alert areas are filled at this alpha (0-255) under a cased outline, so the
# radar echoes beneath stay readable.
HAZARD_ALERT_FILL_ALPHA = _clamp("TTU_SAFETY_HAZARD_FILL_ALPHA",
                                 _env_int("TTU_SAFETY_HAZARD_FILL_ALPHA", 60), 0, 255)

# Hazard information feeds (INFORMATION ONLY — shown, never part of IsSafe). Each feed is
# independent: one failing never blanks the others.
HAZARD_FEEDS_ENABLED = (_env_str("TTU_SAFETY_HAZARD_FEEDS", "1").strip().lower()
                        not in ("0", "false", "no"))
# Every 10 min: slow-moving products (SPC outlooks and HMS smoke update a few times a
# day); a feed whose own cadence is slower is polled at that cadence instead.
HAZARD_FEEDS_POLL_SEC = _clamp("TTU_SAFETY_HAZARD_FEEDS_POLL_SEC",
                               _env_int("TTU_SAFETY_HAZARD_FEEDS_POLL_SEC", 600), 60)
# Earthquakes listed within this radius and at or above this magnitude. 300 km reaches
# the induced-seismicity cluster near Snyder/Ackerly (125-175 km), where nearly all the
# nearby events occur — well outside the map, so these are mostly text.
HAZARD_QUAKE_RADIUS_KM = _clamp("TTU_SAFETY_HAZARD_QUAKE_KM",
                                _env_float("TTU_SAFETY_HAZARD_QUAKE_KM", 300.0), 1.0)
HAZARD_QUAKE_MIN_MAG = _env_float("TTU_SAFETY_HAZARD_QUAKE_MIN_MAG", 2.5)
# NWS Local Storm Reports (via IEM) from the last this-many hours, within the map.
HAZARD_LSR_HOURS = _clamp("TTU_SAFETY_HAZARD_LSR_HOURS",
                          _env_int("TTU_SAFETY_HAZARD_LSR_HOURS", 24), 1, 168)

# --- status-page runner ------------------------------------------------------
# The daemon periodically runs make_status_page.py as a SUBPROCESS (one systemd service
# for everything; the page's heavy deps and any crash stay isolated from the safety
# logic). A run that hangs is killed with its whole process tree after PAGE_TIMEOUT.
PAGE_ENABLED = _env_str("TTU_SAFETY_PAGE", "1").strip().lower() not in ("0", "false", "no")
PAGE_SCRIPT = _env_str("TTU_SAFETY_PAGE_SCRIPT",
                       os.path.join(os.path.dirname(os.path.dirname(
                           os.path.abspath(__file__))), "make_status_page.py"))
# same env var the page itself uses for its meta-refresh, so the two always agree
PAGE_INTERVAL = _env_int("STATUS_PAGE_INTERVAL", 90)
PAGE_TIMEOUT = _env_int("TTU_SAFETY_PAGE_TIMEOUT", 1800)   # kill a stuck run after 30 min

# --- connectivity watchdog (loss of internet) -------------------------------
# A lightweight probe (day and night) checks whether ANY online service host is reachable.
# If NONE are reachable for CONN_OFFLINE_UNSAFE_SEC (1 h), declare UNSAFE — we've been blind
# to rain/lightning/forecast that long and can't trust "safe". Auto-resolves the moment a
# probe succeeds again. This is a HARD veto (unlike the per-service components, which fail to
# "unknown"): it exists precisely to catch the case where every online layer is silently
# unavailable. A response of ANY kind (even an HTTP error) counts as "reachable" — only a
# connection/DNS/timeout failure is "offline".
CONN_ENABLED = _env_str("TTU_SAFETY_CONN", "1").strip().lower() not in ("0", "false", "no")
CONN_OFFLINE_UNSAFE_SEC = _env_int("TTU_SAFETY_OFFLINE_UNSAFE_SEC", 3600)   # 1 hour
CONN_PROBE_INTERVAL = _env_int("TTU_SAFETY_CONN_PROBE_INTERVAL", 300)       # 5 min
CONN_PROBE_TIMEOUT = _env_int("TTU_SAFETY_CONN_PROBE_TIMEOUT", 10)
# One host per service family the daemon actually depends on (NWS, WU, radar/IEM, GLM/S3);
# ANY response marks the internet up, so the list just needs breadth, not depth.
CONN_PROBE_URLS = [
    "https://api.weather.gov/",
    "https://api.weather.com/",
    "https://mesonet.agron.iastate.edu/",
    "https://noaa-goes19.s3.amazonaws.com/",
]

# --- files ------------------------------------------------------------------
# Transient (fine in /tmp): the page<->daemon exchange.
# /dev/shm is a RAM tmpfs on Raspberry Pi OS: these high-frequency transient files never
# touch the SD card, and (unlike SD-backed /tmp) are guaranteed gone after a reboot, so a
# pre-reboot state can never be mistaken for fresh. make_status_page.py uses the same
# paths — update both sides together when deploying this change.
_SHM = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"
if _SHM == "/tmp":
    # On Raspberry Pi OS bookworm /tmp is ON THE SD CARD, so this fallback silently
    # turns every per-minute state write into SD wear. It should never happen on a
    # real Pi (/dev/shm always exists) — if it does, say so loudly.
    CONFIG_WARNINGS.append("/dev/shm not found — transient state files fall back to "
                           "/tmp, which is ON THE SD CARD on Raspberry Pi OS")
INPUTS_FILE = _env_str("TTU_SAFETY_INPUTS_FILE", _SHM + "/safety_inputs.json")
STATE_FILE = _env_str("TTU_SAFETY_STATE_FILE", _SHM + "/safety_state.json")
# Heartbeat for the throttled state-file write (see monitor._write_state): unchanged state
# is rewritten at most this often so the page's staleness gate still sees a live daemon.
STATE_WRITE_HEARTBEAT_SEC = _env_int("TTU_SAFETY_STATE_HEARTBEAT_SEC", 60)
# Durable (survive reboot): the rain latch and the audit log.
LATCH_FILE = _env_str("TTU_SAFETY_LATCH_FILE", os.path.expanduser("~/safety_latch.json"))
EVENT_LOG = _env_str("TTU_SAFETY_EVENT_LOG", os.path.expanduser("~/safety_events.log"))

# --- Alpaca server ----------------------------------------------------------
HTTP_HOST = _env_str("TTU_SAFETY_HTTP_HOST", "0.0.0.0")   # LAN-reachable for NINA
HTTP_PORT = _env_int("TTU_SAFETY_HTTP_PORT", 11111)
DEVICE_NUMBER = 0
SERVER_NAME = _env_str("TTU_SAFETY_NAME", "TTU Safety Monitor")
LOCATION = _env_str("TTU_SAFETY_LOCATION", "TTU Observatory, Lubbock TX")
UNIQUE_ID = "ttu-safetymonitor-0-9f2a7c31"   # stable per-device id for Alpaca
DRIVER_VERSION = "0.1.0"
EVAL_INTERVAL = _env_int("TTU_SAFETY_EVAL_INTERVAL", 30)  # evaluator cadence (s)
