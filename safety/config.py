"""Configuration for the TTU Alpaca SafetyMonitor daemon.

Every value can be overridden with an environment variable (TTU_SAFETY_*). The defaults
suit the observatory Raspberry Pi, where make_status_page.py runs from /home/kirx.
"""
from __future__ import annotations

import math
import os


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ[name])
    except (KeyError, ValueError):
        return default
    return v if math.isfinite(v) else default   # reject nan/inf -> safe default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# --- Weather Underground (rain input) --------------------------------------
# REQUIRED for rain polling. NEVER hardcode a key here — it would end up on GitHub.
# Set the TTU_SAFETY_WU_KEY environment variable instead (see README_SAFETY.md). If it's
# empty, the daemon still runs (sun/humidity protection) but rain polling is disabled.
WU_API_KEY = _env_str("TTU_SAFETY_WU_KEY", "")
GEOCODE = (_env_float("TTU_SAFETY_LAT", 33.7483333),
           _env_float("TTU_SAFETY_LON", -101.9584001))   # TTU observatory
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
RAIN_LATCH_HOURS = _env_float("TTU_SAFETY_RAIN_LATCH_HOURS", 3.0)
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
NWS_PRECIP_PROB_MAX = _env_float("TTU_SAFETY_NWS_PRECIP_MAX", 15.0)   # % ; unsafe when >
NWS_THUNDER_PROB_MAX = _env_float("TTU_SAFETY_NWS_THUNDER_MAX", 10.0)  # % ; unsafe when >
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
GLM_COOLOFF_HOURS = _env_float("TTU_SAFETY_GLM_COOLOFF_HOURS", 3.0)   # same as WU rain latch
GLM_POLL_INTERVAL = _env_int("TTU_SAFETY_GLM_POLL_INTERVAL", 300)     # 5 min
GLM_WINDOW_MIN = _env_int("TTU_SAFETY_GLM_WINDOW_MIN", 5)             # look-back per poll
GLM_POLL_SUN_BELOW_DEG = _env_float("TTU_SAFETY_GLM_SUN_BELOW", 5.0)  # night gate (like WU)
GLM_LATCH_FILE = _env_str("TTU_SAFETY_GLM_LATCH_FILE",
                          os.path.expanduser("~/safety_glm_latch.json"))

# --- files ------------------------------------------------------------------
# Transient (fine in /tmp): the page<->daemon exchange.
INPUTS_FILE = _env_str("TTU_SAFETY_INPUTS_FILE", "/tmp/safety_inputs.json")
STATE_FILE = _env_str("TTU_SAFETY_STATE_FILE", "/tmp/safety_state.json")
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
