"""Configuration for the TTU Alpaca SafetyMonitor daemon.

Every value can be overridden with an environment variable (TTU_SAFETY_*). The defaults
suit the observatory Raspberry Pi, where make_status_page.py runs from /home/kirx.
"""
from __future__ import annotations

import math
import os

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


# --- Weather Underground (rain input) --------------------------------------
# REQUIRED for rain polling. NEVER hardcode a key here — it would end up on GitHub.
# Set the TTU_SAFETY_WU_KEY environment variable instead (see README_SAFETY.md). If it's
# empty, the daemon still runs (sun/humidity protection) but rain polling is disabled.
WU_API_KEY = _env_str("TTU_SAFETY_WU_KEY", "")

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
WU_POLL_INTERVAL = _env_int("TTU_SAFETY_POLL_INTERVAL", 600)  # s between WU polls
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
NWS_USER_AGENT = _env_str("TTU_SAFETY_NWS_UA", "ttu-safety-monitor")  # add a contact via env
NWS_GRID = _env_str("TTU_SAFETY_NWS_GRID", "")   # e.g. "LUB/46,41"; empty => resolve via GEOCODE
NWS_POLL_INTERVAL = _env_int("TTU_SAFETY_NWS_POLL_INTERVAL", 900)     # 15 min
NWS_STALE_AFTER_MIN = _env_int("TTU_SAFETY_NWS_STALE_MIN", 150)
NWS_CLOUD_MAX = _env_float("TTU_SAFETY_NWS_CLOUD_MAX", 70.0)          # % ; unsafe when >
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
# Freeze time after the last in-range flash (30 min), matching the radar freeze.
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
# the observatory. Deliberately simple — a plain 50 km "any rain" ring, no upwind logic.
# Also renders a TTU-centered radar thumbnail (dark OSM tiles, cached to disk) with the
# 50 km ring + scale bars for the status page. Needs Pillow (apt: python3-pil); absent =>
# radar disabled (other layers unaffected). A fetch error/stale frame => unavailable (does
# not by itself force unsafe); the radar keeps its own post-rain freeze (RADAR_LATCH_SEC).
RADAR_ENABLED = _env_str("TTU_SAFETY_RADAR", "1").strip().lower() not in ("0", "false", "no")
RADAR_TRIGGER_KM = _env_float("TTU_SAFETY_RADAR_KM", 50.0)
RADAR_DBZ = _env_float("TTU_SAFETY_RADAR_DBZ", 20.0)        # echo >= this = rain
RADAR_POLL_INTERVAL = _env_int("TTU_SAFETY_RADAR_POLL_INTERVAL", 300)   # 5 min
# NOTE: radar polls day AND night (free data, daytime rain matters, live map) — unlike
# the WU/GLM night gates.
RADAR_STALE_AFTER_SEC = _env_int("TTU_SAFETY_RADAR_STALE_SEC", 1200)  # older => unavailable
# FREEZE TIME after the last in-ring detection: hold the veto this long even if later
# frames are clear, so the dome does not reopen the moment a cell's leading edge leaves
# the ring (and so a ranged echo can't reopen it when IEM goes blind, since a cell inside
# 50 km may sit over no WU station at all).
RADAR_LATCH_SEC = _env_int("TTU_SAFETY_RADAR_LATCH_SEC", 1800)  # 30 min post-rain freeze
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
RADAR_TILE_URL = _env_str(
    "TTU_SAFETY_RADAR_TILE_URL",
    "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png")   # OSM data, dark theme
# Daytime (light) version of the map, shown when the status page is in day style.
RADAR_DAY_ENABLED = _env_str("TTU_SAFETY_RADAR_DAY", "1").strip().lower() not in ("0", "false", "no")
RADAR_TILE_URL_DAY = _env_str(
    "TTU_SAFETY_RADAR_TILE_URL_DAY",
    "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png")  # OSM data, light theme
RADAR_CACHE_DIR = _env_str("TTU_SAFETY_RADAR_CACHE", os.path.expanduser("~/.cache/ttu-radar"))
RADAR_ATTRIBUTION = "© OpenStreetMap contributors, © CARTO · Radar: NOAA/NSSL MRMS via IEM"
# (Persistence note: the radar keeps its OWN post-rain freeze above — it does NOT rely on the
# WU rain latch, which only arms when rain reaches a nearby station, not for ranged echoes.)

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
