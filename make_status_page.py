import time
import signal
import subprocess
import html
import os
import glob
import json
import math
import re
import shutil
import shlex
import warnings
from datetime import datetime
from datetime import timedelta

import numpy as np

import board
import adafruit_dht
import gps

import astropy.units as u
from astropy.time import Time
from astropy.coordinates import EarthLocation
from astropy.coordinates import AltAz
from astropy.coordinates import get_sun
from astropy.utils import iers


IMAGE_FILE = "/var/www/html/snapshot.jpg"
HTML_FILE = "/var/www/html/status.html"
# Write the page into /dev/shm and leave a one-time symlink at HTML_FILE: the page is
# rewritten every ~90 s (~1000 SD writes/day) on a Pi whose brownouts corrupt SD cards
# mid-write. Debian's Apache/nginx/lighttpd follow symlinks by default. Set
# TTU_STATUS_HTML_SHM=0 to write directly.
HTML_VIA_SHM = (os.environ.get("TTU_STATUS_HTML_SHM", "1").strip().lower()
                not in ("0", "false", "no")) and os.path.isdir("/dev/shm")

# /dev/shm is a RAM tmpfs on Raspberry Pi OS — these transient files are rewritten every
# cycle and must not wear the SD card (and must not survive a reboot looking "fresh").
_SHM = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"
if _SHM == "/tmp":
    print("WARNING: /dev/shm not found — transient files fall back to /tmp, which is "
          "ON THE SD CARD on Raspberry Pi OS")
CACHE_FILE = _SHM + "/status_page_cache.json"

# Camera processing runs on the RAM disk: the night pipeline writes GBs of DNG/TIFF
# intermediates per cycle, which on SD-backed /tmp would wear the card out in months.
# ImageMagick's pixel-cache spill is pointed there too. Because /dev/shm is RAM-sized,
# the DNGs are converted and stacked IN BATCHES (deleting as we go), and if there isn't
# enough free RAM for the RAW pipeline at all, capture degrades to JPEG-only stacking.
STACK_DIR = _SHM + "/status_stack"
os.environ.setdefault("MAGICK_TMPDIR", STACK_DIR)
NIGHT_STACK_RAW_MIN_FREE = 3 * 1024 ** 3   # need ~3 GB free in RAM for the RAW pipeline
NIGHT_STACK_BATCH = 10                     # DNGs converted+averaged per batch

# Shared with the Alpaca SafetyMonitor daemon (safety_monitor.py). We WRITE the sun
# altitude + humidity + GPS position it consumes, and READ back its state to display.
# Paths must match safety/config.py — update both sides together when deploying.
SAFETY_INPUTS_FILE = _SHM + "/safety_inputs.json"
SAFETY_STATE_FILE = _SHM + "/safety_state.json"
SAFETY_STATE_STALE_SEC = 300

# The enclosure camera is OPTIONAL and disabled by default (at TTU the Pi + camera are
# covered to reduce in-dome light pollution, so the camera sees nothing). Enable with
# TTU_STATUS_CAMERA=1 in ~/ttustatus.env (the daemon passes its environment down to this
# script). While disabled: no captures, no stacking, no image writes at all.
CAMERA_ENABLED = os.environ.get("TTU_STATUS_CAMERA", "0").strip().lower() \
    not in ("", "0", "false", "no")

GPIO_PIN = board.D17

DHT_RETRIES = 4
DHT_DELAY = 0.8

GPS_TIMEOUT = 3.0

WIFI_RETRIES = 3
WIFI_DELAY = 0.5

CMD_TIMEOUT_SHORT = 2.0
CMD_TIMEOUT_CAMERA_DAY = 8.0
CMD_TIMEOUT_CAMERA_NIGHT = 240.0
CMD_TIMEOUT_DCRAW = 20.0
CMD_TIMEOUT_STACK = 180.0

CACHE_MAX_AGE_DHT = 1800
CACHE_MAX_AGE_WIFI = 1800
CACHE_MAX_AGE_GPS = 604800

REFRESH_SECONDS_DEFAULT = 90
REFRESH_SECONDS_ENV_VAR = "STATUS_PAGE_INTERVAL"

CHRONY_CMD = ["chronyc", "sources", "-v"]
CHRONY_CMD_STR = "chronyc sources -v"

NIGHT_STACK_ALTITUDE_DEG = -6.0

SUNRISE_ALTITUDE_DEG = -0.833
CIVIL_TWILIGHT_ALTITUDE_DEG = -6.0
NAUTICAL_TWILIGHT_ALTITUDE_DEG = -12.0
ASTRONOMICAL_TWILIGHT_ALTITUDE_DEG = -18.0

TWILIGHT_SEARCH_HOURS = 48.0
TWILIGHT_SCAN_STEP_MIN = 10.0
TWILIGHT_BISECTION_STEPS = 25
TIMELINE_BISECTION_STEPS = 8

NIGHT_STACK_CAPTURE_MS = 120000
NIGHT_STACK_TIMELAPSE_MS = 1500
NIGHT_STACK_SHUTTER_US = 1200000
NIGHT_STACK_GAIN = 16
NIGHT_STACK_AWBGAINS = "1,1"

NIGHT_STACK_DCRAW_CMD_STR = ("dcraw -T -6 -w -q 0 " + STACK_DIR
                             + "/*.dng (batched, DNGs deleted as converted)")
NIGHT_STACK_PROCESS_CMD_STR = (
    "magick " + STACK_DIR + "/mean*.tiff -evaluate-sequence mean "
    "-contrast-stretch 1%x0.05% -gamma 1.5 -quality 95 "
    "/var/www/html/snapshot.jpg"
)


try:
    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
    iers.conf.iers_degraded_accuracy = "warn"
except Exception:
    pass

# The 50-yr-mean polar motion fallback shifts sun event times by well under
# a second, which this page cannot display anyway; without this filter the
# warning is logged several times per run, every run.
warnings.filterwarnings(
    "ignore",
    message="Tried to get polar motions for times after IERS data is valid"
)


def load_cache():
    try:
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache):
    tmp_file = CACHE_FILE + ".tmp"

    try:
        with open(tmp_file, "w") as f:
            json.dump(cache, f)
        os.replace(tmp_file, CACHE_FILE)
    except Exception:
        pass


def get_cached_section(cache, section, max_age):
    entry = cache.get(section)
    now = time.time()

    if not isinstance(entry, dict):
        return None

    ts = entry.get("timestamp")
    if ts is None:
        return None

    if now - ts > max_age:
        return None

    return entry


def update_cache_section(cache, section, values):
    entry = {}
    entry["timestamp"] = time.time()

    for key in values:
        entry[key] = values[key]

    cache[section] = entry


def get_dht(cache):
    dht = None
    t = None
    h = None
    i = None
    cached = None

    for i in range(DHT_RETRIES):
        try:
            dht = adafruit_dht.DHT11(GPIO_PIN, use_pulseio=False)
            t = dht.temperature
            h = dht.humidity

            if t is not None and h is not None:
                update_cache_section(cache, "dht", {"temperature": t, "humidity": h})
                return t, h, 0.0                       # live reading, age 0
        except RuntimeError:
            pass
        except Exception:
            pass
        finally:
            try:
                if dht is not None:
                    dht.exit()
            except Exception:
                pass

        time.sleep(DHT_DELAY)

    cached = get_cached_section(cache, "dht", CACHE_MAX_AGE_DHT)
    if cached is not None:
        # Report the measurement's REAL age so the safety daemon can judge staleness —
        # the inputs-file timestamp alone would launder a cached reading as fresh.
        age = time.time() - cached.get("timestamp", time.time())
        return cached.get("temperature"), cached.get("humidity"), age

    return None, None, None


def get_time_string():
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def run_command(cmd, timeout_sec):
    try:
        return subprocess.check_output(
            cmd,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=timeout_sec
        )
    except Exception:
        return None


def run_subprocess(cmd, timeout_sec):
    try:
        subprocess.run(
            cmd,
            check=True,
            timeout=timeout_sec,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False


def command_to_string(cmd):
    parts = []
    item = None

    for item in cmd:
        parts.append(shlex.quote(str(item)))

    return " ".join(parts)


def get_chrony():
    out = run_command(CHRONY_CMD, CMD_TIMEOUT_SHORT)
    if out is None:
        return "chronyc failed"
    return out


def get_gps(cache):
    session = None
    deadline = time.time() + GPS_TIMEOUT
    report = None
    lat = None
    lon = None
    alt = None
    cached = None

    # HARD wall around the whole gpsd exchange: the deadline below is only checked
    # BETWEEN session.next() calls, but next() itself blocks on the socket with no
    # timeout — a silent gpsd would freeze page generation forever (and with it the
    # safety inputs). SIGALRM breaks out of a stuck read; we then fall back to cache.
    def _gps_alarm(signum, frame):
        raise TimeoutError("gpsd read timed out")

    old_handler = signal.signal(signal.SIGALRM, _gps_alarm)
    signal.alarm(int(GPS_TIMEOUT) + 3)

    try:
        try:
            session = gps.gps(mode=gps.WATCH_ENABLE)
        except TimeoutError:
            session = None
        except Exception:
            session = None

        if session is not None:
            while time.time() < deadline:
                try:
                    report = session.next()
                except StopIteration:
                    break
                except TimeoutError:
                    break                      # alarm fired inside a stuck read
                except Exception:
                    continue

                if report.get("class") != "TPV":
                    continue

                lat = report.get("lat")
                lon = report.get("lon")
                alt = report.get("altMSL")
                if alt is None:
                    alt = report.get("alt")

                if lat is not None and lon is not None:
                    update_cache_section(
                        cache,
                        "gps",
                        {
                            "lat": lat,
                            "lon": lon,
                            "alt": alt
                        }
                    )
                    return (lat, lon, alt), "live"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    cached = get_cached_section(cache, "gps", CACHE_MAX_AGE_GPS)
    if cached is not None:
        lat = cached.get("lat")
        lon = cached.get("lon")
        alt = cached.get("alt")

        if lat is not None and lon is not None:
            return (lat, lon, alt), "cached"

    return None, None


def get_sun_altitude_deg(location, t_astropy):
    frame = None
    sun = None
    alt = None

    frame = AltAz(obstime=t_astropy, location=location)
    sun = get_sun(t_astropy)
    alt = sun.transform_to(frame).alt.deg

    return float(alt)


def get_sun_altitudes_deg(location, times):
    frame = None
    sun = None
    altitudes = None

    frame = AltAz(obstime=times, location=location)
    sun = get_sun(times)
    altitudes = sun.transform_to(frame).alt.deg

    return np.asarray(altitudes, dtype=float)


def bisect_sun_crossing(location, t0, t1, target_alt_deg, direction, steps=TWILIGHT_BISECTION_STEPS):
    lo = None
    hi = None
    mid = None
    alt_mid = None
    i = None

    lo = t0
    hi = t1

    for i in range(steps):
        mid = lo + (hi - lo) * 0.5
        alt_mid = get_sun_altitude_deg(location, mid)

        if direction == "up":
            if alt_mid >= target_alt_deg:
                hi = mid
            else:
                lo = mid
        else:
            if alt_mid <= target_alt_deg:
                hi = mid
            else:
                lo = mid

    return hi


def find_sun_crossing_between(location, t_start, t_end, target_alt_deg, direction, steps=TWILIGHT_BISECTION_STEPS):
    duration_min = None
    offsets_min = None
    times = None
    altitudes = None
    i = None

    duration_min = (float(t_end.unix) - float(t_start.unix)) / 60.0

    offsets_min = np.arange(
        0.0,
        duration_min + TWILIGHT_SCAN_STEP_MIN,
        TWILIGHT_SCAN_STEP_MIN
    )

    times = t_start + offsets_min * u.min
    altitudes = get_sun_altitudes_deg(location, times)

    for i in range(len(times) - 1):
        if direction == "up":
            if altitudes[i] < target_alt_deg and altitudes[i + 1] >= target_alt_deg:
                return bisect_sun_crossing(
                    location,
                    times[i],
                    times[i + 1],
                    target_alt_deg,
                    direction,
                    steps
                )
        else:
            if altitudes[i] > target_alt_deg and altitudes[i + 1] <= target_alt_deg:
                return bisect_sun_crossing(
                    location,
                    times[i],
                    times[i + 1],
                    target_alt_deg,
                    direction,
                    steps
                )

    return None


def find_next_sun_crossing(location, now_astropy, target_alt_deg, direction):
    t_end = None

    t_end = now_astropy + TWILIGHT_SEARCH_HOURS * u.hour

    return find_sun_crossing_between(
        location,
        now_astropy,
        t_end,
        target_alt_deg,
        direction
    )


def get_local_day_astropy_times(now_unix):
    now_dt = None
    start_dt = None
    end_dt = None
    start_unix = None
    end_unix = None
    start_time = None
    end_time = None

    now_dt = datetime.fromtimestamp(now_unix)
    start_dt = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = start_dt + timedelta(days=1)

    start_unix = time.mktime(start_dt.timetuple())
    end_unix = time.mktime(end_dt.timetuple())

    start_time = Time(start_unix, format="unix")
    end_time = Time(end_unix, format="unix")

    return start_time, end_time


def get_noon_window_times(now_unix):
    now_dt = None
    noon_dt = None
    end_dt = None
    start_unix = None
    end_unix = None

    now_dt = datetime.fromtimestamp(now_unix)
    noon_dt = now_dt.replace(hour=12, minute=0, second=0, microsecond=0)

    if now_dt < noon_dt:
        noon_dt = noon_dt - timedelta(days=1)

    end_dt = noon_dt + timedelta(days=1)

    start_unix = time.mktime(noon_dt.timetuple())
    end_unix = time.mktime(end_dt.timetuple())

    return Time(start_unix, format="unix"), Time(end_unix, format="unix")


TIMELINE_TARGETS = [
    (SUNRISE_ALTITUDE_DEG, "down"),
    (CIVIL_TWILIGHT_ALTITUDE_DEG, "down"),
    (NAUTICAL_TWILIGHT_ALTITUDE_DEG, "down"),
    (ASTRONOMICAL_TWILIGHT_ALTITUDE_DEG, "down"),
    (ASTRONOMICAL_TWILIGHT_ALTITUDE_DEG, "up"),
    (NAUTICAL_TWILIGHT_ALTITUDE_DEG, "up"),
    (CIVIL_TWILIGHT_ALTITUDE_DEG, "up"),
    (SUNRISE_ALTITUDE_DEG, "up"),
]


def get_timeline_info(location, now_unix):
    start_time = None
    end_time = None
    timeline = None
    crossings = None
    crossing = None
    target_alt_deg = None
    direction = None

    start_time, end_time = get_noon_window_times(now_unix)

    timeline = {}
    timeline["start_unix"] = float(start_time.unix)
    timeline["end_unix"] = float(end_time.unix)

    crossings = []
    for target_alt_deg, direction in TIMELINE_TARGETS:
        crossing = find_sun_crossing_between(
            location,
            start_time,
            end_time,
            target_alt_deg,
            direction,
            TIMELINE_BISECTION_STEPS
        )
        if crossing is None:
            crossings.append(None)
        else:
            crossings.append(float(crossing.unix))

    timeline["crossings"] = crossings

    return timeline


def format_astropy_time_local(t_astropy):
    if t_astropy is None:
        return "N/A"

    return time.strftime(
        "%Y-%m-%d %H:%M:%S %Z",
        time.localtime(float(t_astropy.unix))
    )


def minutes_until_astropy_time(t_astropy, now_unix):
    if t_astropy is None:
        return None

    return (float(t_astropy.unix) - now_unix) / 60.0


def format_minutes_only(value):
    if value is None:
        return "N/A"

    if value < 0:
        return "passed"

    return "%.0f min" % value


def format_duration_minutes(value):
    minutes = None
    hours = None
    remaining_minutes = None

    if value is None:
        return "N/A"

    if value < 0:
        return "passed"

    minutes = int(round(value))

    if minutes < 120:
        return "%d min" % minutes

    hours = minutes // 60
    remaining_minutes = minutes % 60

    if remaining_minutes == 0:
        return "%d h" % hours

    return "%d h %d min" % (hours, remaining_minutes)


def get_sun_info(gps_data):
    info = None
    lat = None
    lon = None
    alt = None
    location = None
    now_astropy = None
    now_unix = None
    day_start = None
    day_end = None
    sun_altitude_deg = None
    astronomical_dawn = None
    nautical_dawn = None
    civil_dawn = None
    sunrise = None
    sunset = None
    civil_dusk = None
    nautical_dusk = None
    astronomical_dusk = None
    next_sunrise = None
    next_sunset = None
    until_sunrise = None
    until_sunset = None
    next_nautical_dusk = None
    next_astronomical_dusk = None
    until_nautical_dusk = None
    next_civil_dawn = None
    until_civil_dawn = None
    next_nautical_dawn = None
    until_nautical_dawn = None

    info = {}
    info["available"] = False
    info["error"] = None
    info["sun_altitude_deg"] = None
    info["camera_should_stack"] = False

    info["astronomical_dawn"] = None
    info["nautical_dawn"] = None
    info["civil_dawn"] = None
    info["sunrise"] = None

    info["sunset"] = None
    info["civil_dusk"] = None
    info["nautical_dusk"] = None
    info["astronomical_dusk"] = None

    info["until_sunrise"] = None
    info["until_sunset"] = None

    info["next_sunrise"] = None
    info["next_sunset"] = None
    info["next_nautical_dusk"] = None
    info["next_astronomical_dusk"] = None
    info["next_civil_dawn"] = None
    info["next_nautical_dawn"] = None
    info["until_nautical_dusk"] = None
    info["until_civil_dawn"] = None
    info["until_nautical_dawn"] = None
    info["timeline"] = None

    if gps_data is None:
        info["error"] = "GPS coordinates unavailable"
        return info

    try:
        lat, lon, alt = gps_data

        if alt is None:
            alt = 0.0

        location = EarthLocation(
            lat=float(lat) * u.deg,
            lon=float(lon) * u.deg,
            height=float(alt) * u.m
        )

        now_astropy = Time.now()
        now_unix = time.time()

        day_start, day_end = get_local_day_astropy_times(now_unix)

        sun_altitude_deg = get_sun_altitude_deg(location, now_astropy)

        astronomical_dawn = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            ASTRONOMICAL_TWILIGHT_ALTITUDE_DEG,
            "up"
        )
        nautical_dawn = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            NAUTICAL_TWILIGHT_ALTITUDE_DEG,
            "up"
        )
        civil_dawn = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            CIVIL_TWILIGHT_ALTITUDE_DEG,
            "up"
        )
        sunrise = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            SUNRISE_ALTITUDE_DEG,
            "up"
        )

        sunset = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            SUNRISE_ALTITUDE_DEG,
            "down"
        )
        civil_dusk = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            CIVIL_TWILIGHT_ALTITUDE_DEG,
            "down"
        )
        nautical_dusk = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            NAUTICAL_TWILIGHT_ALTITUDE_DEG,
            "down"
        )
        astronomical_dusk = find_sun_crossing_between(
            location,
            day_start,
            day_end,
            ASTRONOMICAL_TWILIGHT_ALTITUDE_DEG,
            "down"
        )

        next_sunrise = find_next_sun_crossing(
            location,
            now_astropy,
            SUNRISE_ALTITUDE_DEG,
            "up"
        )
        next_sunset = find_next_sun_crossing(
            location,
            now_astropy,
            SUNRISE_ALTITUDE_DEG,
            "down"
        )

        next_nautical_dusk = find_next_sun_crossing(
            location,
            now_astropy,
            NAUTICAL_TWILIGHT_ALTITUDE_DEG,
            "down"
        )
        next_astronomical_dusk = find_next_sun_crossing(
            location,
            now_astropy,
            ASTRONOMICAL_TWILIGHT_ALTITUDE_DEG,
            "down"
        )

        next_civil_dawn = find_next_sun_crossing(
            location,
            now_astropy,
            CIVIL_TWILIGHT_ALTITUDE_DEG,
            "up"
        )
        next_nautical_dawn = find_next_sun_crossing(
            location,
            now_astropy,
            NAUTICAL_TWILIGHT_ALTITUDE_DEG,
            "up"
        )

        until_sunrise = minutes_until_astropy_time(next_sunrise, now_unix)
        until_sunset = minutes_until_astropy_time(next_sunset, now_unix)
        until_nautical_dusk = minutes_until_astropy_time(next_nautical_dusk, now_unix)
        until_civil_dawn = minutes_until_astropy_time(next_civil_dawn, now_unix)
        until_nautical_dawn = minutes_until_astropy_time(next_nautical_dawn, now_unix)

        info["available"] = True
        info["sun_altitude_deg"] = sun_altitude_deg
        info["camera_should_stack"] = sun_altitude_deg < NIGHT_STACK_ALTITUDE_DEG

        info["astronomical_dawn"] = astronomical_dawn
        info["nautical_dawn"] = nautical_dawn
        info["civil_dawn"] = civil_dawn
        info["sunrise"] = sunrise

        info["sunset"] = sunset
        info["civil_dusk"] = civil_dusk
        info["nautical_dusk"] = nautical_dusk
        info["astronomical_dusk"] = astronomical_dusk

        info["until_sunrise"] = until_sunrise
        info["until_sunset"] = until_sunset

        info["next_sunrise"] = next_sunrise
        info["next_sunset"] = next_sunset
        info["next_nautical_dusk"] = next_nautical_dusk
        info["next_astronomical_dusk"] = next_astronomical_dusk
        info["next_civil_dawn"] = next_civil_dawn
        info["next_nautical_dawn"] = next_nautical_dawn
        info["until_nautical_dusk"] = until_nautical_dusk
        info["until_civil_dawn"] = until_civil_dawn
        info["until_nautical_dawn"] = until_nautical_dawn

        info["timeline"] = get_timeline_info(location, now_unix)

        return info
    except Exception as e:
        info["error"] = str(e)
        return info


def get_imagemagick_cmd():
    cmd = None

    cmd = shutil.which("magick")
    if cmd is not None:
        return cmd

    cmd = shutil.which("convert")
    if cmd is not None:
        return cmd

    return None


def prepare_stack_dir():
    try:
        shutil.rmtree(STACK_DIR)
    except Exception:
        pass

    try:
        os.makedirs(STACK_DIR, exist_ok=True)
        return True
    except Exception:
        return False


def run_dcraw_on_dngs(dng_files):
    dng_file = None
    cmd = None
    ok = None

    ok = True

    for dng_file in dng_files:
        cmd = [
            "dcraw",
            "-T",
            "-6",
            "-w",
            "-q",
            "0",
            dng_file
        ]

        if not run_subprocess(cmd, CMD_TIMEOUT_DCRAW):
            ok = False

        # The stack dir lives on the RAM disk: free each DNG as soon as it is
        # converted so the pipeline fits in memory.
        try:
            os.remove(dng_file)
        except Exception:
            pass

    return ok


def stack_tiff_batch(tiff_files, out_path):
    """Average one batch of TIFFs into a single TIFF (no post-processing — the
    contrast stretch/gamma is applied once, on the final mean of means)."""
    imagemagick_cmd = get_imagemagick_cmd()
    if imagemagick_cmd is None:
        return False
    cmd = [imagemagick_cmd]
    cmd.extend(tiff_files)
    cmd.extend(["-evaluate-sequence", "mean", out_path])
    return run_subprocess(cmd, CMD_TIMEOUT_STACK)


def process_dngs_in_batches(dng_files):
    """Convert + average DNGs in batches so the multi-GB TIFF set never exists at once
    (the RAM disk cannot hold it). Each batch: dcraw -> batch-mean TIFF -> delete the
    batch TIFFs (DNGs are deleted by run_dcraw_on_dngs). Returns (mean_files, raw_ok);
    mean of equal batches = true mean (a smaller final batch weighs its frames slightly
    higher — visually irrelevant)."""
    raw_ok = True
    mean_files = []

    for i in range(0, len(dng_files), NIGHT_STACK_BATCH):
        batch = dng_files[i:i + NIGHT_STACK_BATCH]
        if not run_dcraw_on_dngs(batch):
            raw_ok = False

        tiffs = []
        for dng in batch:
            tiff = os.path.splitext(dng)[0] + ".tiff"
            if os.path.exists(tiff):
                tiffs.append(tiff)

        if tiffs:
            mean_path = os.path.join(
                STACK_DIR, "mean%04d.tiff" % (i // NIGHT_STACK_BATCH))
            if stack_tiff_batch(tiffs, mean_path) and os.path.exists(mean_path):
                mean_files.append(mean_path)
            else:
                raw_ok = False
            for tiff in tiffs:
                try:
                    os.remove(tiff)
                except Exception:
                    pass

    return mean_files, raw_ok


def stack_tiffs_to_image(tiff_files):
    imagemagick_cmd = None
    cmd = None

    imagemagick_cmd = get_imagemagick_cmd()
    if imagemagick_cmd is None:
        return False

    cmd = [imagemagick_cmd]
    cmd.extend(tiff_files)
    cmd.extend([
        "-evaluate-sequence",
        "mean",
        "-contrast-stretch",
        "1%x0.05%",
        "-gamma",
        "1.5",
        "-quality",
        "95",
        IMAGE_FILE
    ])

    return run_subprocess(cmd, CMD_TIMEOUT_STACK)


def stack_jpegs_to_image(jpeg_files):
    imagemagick_cmd = None
    cmd = None

    imagemagick_cmd = get_imagemagick_cmd()
    if imagemagick_cmd is None:
        return False

    cmd = [imagemagick_cmd]
    cmd.extend(jpeg_files)
    cmd.extend([
        "-evaluate-sequence",
        "mean",
        "-contrast-stretch",
        "1%x0.05%",
        "-gamma",
        "1.5",
        "-quality",
        "95",
        IMAGE_FILE
    ])

    return run_subprocess(cmd, CMD_TIMEOUT_STACK)


def take_day_snapshot(camera_info):
    cmd = None

    cmd = [
        "rpicam-still",
        "-o",
        IMAGE_FILE,
        "--timeout",
        "1500",
        "--nopreview",
        "--exposure",
        "normal",
        "--metering",
        "average"
    ]

    camera_info["mode"] = "day, single frame"
    camera_info["capture_command"] = command_to_string(cmd)
    camera_info["processing_command"] = "N/A"

    return run_subprocess(cmd, CMD_TIMEOUT_CAMERA_DAY)


def take_night_stack_snapshot(camera_info):
    cmd = None
    output_pattern = None
    dng_files = None
    tiff_files = None
    jpeg_files = None
    raw_ok = None
    stacked_ok = None

    if not prepare_stack_dir():
        camera_info["error"] = "could not create stack directory"
        return False

    # The stack dir is on the RAM disk. The RAW pipeline needs ~2 GB for the captured
    # DNGs plus batch headroom; if this Pi's /dev/shm can't hold that, capture JPEGs
    # only and stack those — a clean degradation instead of ENOSPC mid-pipeline.
    use_raw = True
    try:
        if shutil.disk_usage(STACK_DIR).free < NIGHT_STACK_RAW_MIN_FREE:
            use_raw = False
    except Exception:
        pass

    output_pattern = os.path.join(STACK_DIR, "frame%04d.jpg")

    cmd = [
        "rpicam-still",
        "--timeout",
        str(NIGHT_STACK_CAPTURE_MS),
        "--timelapse",
        str(NIGHT_STACK_TIMELAPSE_MS),
        "-o",
        output_pattern,
        "--shutter",
        str(NIGHT_STACK_SHUTTER_US),
        "--gain",
        str(NIGHT_STACK_GAIN),
        "--awbgains",
        NIGHT_STACK_AWBGAINS,
        "--immediate",
        "--nopreview"
    ]
    if use_raw:
        cmd.insert(cmd.index("--shutter"), "--raw")

    camera_info["mode"] = ("night, raw stack" if use_raw
                           else "night, JPEG stack (RAM disk too small for RAW)")
    camera_info["capture_command"] = command_to_string(cmd)
    camera_info["processing_command"] = (
        NIGHT_STACK_DCRAW_CMD_STR + " ; " + NIGHT_STACK_PROCESS_CMD_STR
    )

    if not run_subprocess(cmd, CMD_TIMEOUT_CAMERA_NIGHT):
        camera_info["error"] = "rpicam-still night stack capture failed"
        return False

    dng_files = sorted(glob.glob(os.path.join(STACK_DIR, "*.dng")))
    jpeg_files = sorted(glob.glob(os.path.join(STACK_DIR, "*.jpg")))

    if not use_raw:
        # low-RAM path: plain JPEG stack, no RAW intermediates at all
        if jpeg_files and stack_jpegs_to_image(jpeg_files):
            camera_info["processing_command"] = (
                "magick " + STACK_DIR + "/*.jpg -evaluate-sequence mean "
                "-contrast-stretch 1%x0.05% -gamma 1.5 -quality 95 " + IMAGE_FILE)
            return True
        camera_info["error"] = "JPEG stack failed"
        return False

    if len(dng_files) == 0:
        camera_info["error"] = "no DNG files produced"
        if len(jpeg_files) > 0:
            stacked_ok = stack_jpegs_to_image(jpeg_files)
            if stacked_ok:
                camera_info["error"] = None    # fallback succeeded - not an error state
                camera_info["mode"] = "night, JPEG stack fallback"
                camera_info["processing_command"] = (
                    "magick " + STACK_DIR + "/*.jpg -evaluate-sequence mean "
                    "-contrast-stretch 1%x0.05% -gamma 1.5 -quality 95 "
                    + IMAGE_FILE
                )
                return True
        return False

    # batched: DNG -> TIFF -> batch mean, deleting intermediates as we go, so the
    # multi-GB TIFF set never exists at once on the RAM disk
    tiff_files, raw_ok = process_dngs_in_batches(dng_files)

    if len(tiff_files) == 0:
        camera_info["error"] = "dcraw produced no TIFF files"
        if len(jpeg_files) > 0:
            stacked_ok = stack_jpegs_to_image(jpeg_files)
            if stacked_ok:
                camera_info["error"] = None    # fallback succeeded - not an error state
                camera_info["mode"] = "night, JPEG stack fallback"
                camera_info["processing_command"] = (
                    "magick " + STACK_DIR + "/*.jpg -evaluate-sequence mean "
                    "-contrast-stretch 1%x0.05% -gamma 1.5 -quality 95 "
                    + IMAGE_FILE
                )
                return True
        return False

    stacked_ok = stack_tiffs_to_image(tiff_files)

    if not stacked_ok:
        camera_info["error"] = "TIFF stacking failed"
        if len(jpeg_files) > 0:
            stacked_ok = stack_jpegs_to_image(jpeg_files)
            if stacked_ok:
                camera_info["error"] = None    # fallback succeeded - not an error state
                camera_info["mode"] = "night, JPEG stack fallback"
                camera_info["processing_command"] = (
                    "magick " + STACK_DIR + "/*.jpg -evaluate-sequence mean "
                    "-contrast-stretch 1%x0.05% -gamma 1.5 -quality 95 "
                    + IMAGE_FILE
                )
                return True
        return False

    if not raw_ok:
        camera_info["error"] = "some DNG files failed dcraw conversion"

    return True


def take_snapshot(sun_info):
    camera_info = None
    hour = None
    use_stack = None
    have_image = None

    camera_info = {}
    camera_info["mode"] = "unknown"
    camera_info["capture_command"] = "N/A"
    camera_info["processing_command"] = "N/A"
    camera_info["error"] = None

    if not CAMERA_ENABLED:
        camera_info["mode"] = "disabled"
        return False, camera_info

    use_stack = False

    if sun_info is not None and sun_info.get("available"):
        use_stack = bool(sun_info.get("camera_should_stack"))
    else:
        hour = time.localtime().tm_hour
        if hour < 6 or hour >= 21:
            use_stack = True

    if use_stack:
        have_image = take_night_stack_snapshot(camera_info)
    else:
        have_image = take_day_snapshot(camera_info)

    return have_image, camera_info


def get_wifi_interfaces():
    interfaces = []
    paths = glob.glob("/sys/class/net/*/wireless")
    path = None
    iface = None

    for path in paths:
        iface = os.path.basename(os.path.dirname(path))
        interfaces.append(iface)

    return interfaces


def get_ethernet_interfaces():
    interfaces = []
    paths = glob.glob("/sys/class/net/*")
    path = None
    iface = None
    wireless_path = None
    priority = None
    others = None

    priority = []
    others = []

    for path in paths:
        iface = os.path.basename(path)

        if iface == "lo":
            continue

        wireless_path = os.path.join(path, "wireless")
        if os.path.exists(wireless_path):
            continue

        if iface.startswith("docker"):
            continue
        if iface.startswith("br-"):
            continue
        if iface.startswith("veth"):
            continue
        if iface.startswith("tun"):
            continue
        if iface.startswith("tap"):
            continue
        if iface.startswith("wg"):
            continue
        if iface.startswith("virbr"):
            continue

        if iface == "eth0":
            priority.insert(0, iface)
        elif iface.startswith("en"):
            priority.append(iface)
        else:
            others.append(iface)

    interfaces = priority + others

    return interfaces


def get_ip_for_interface(iface):
    out = None
    line = None
    fields = None
    idx = None

    out = run_command(
        ["ip", "-4", "-o", "addr", "show", "dev", iface],
        CMD_TIMEOUT_SHORT
    )
    if out is None:
        return None

    for line in out.splitlines():
        fields = line.split()
        if "inet" in fields:
            idx = fields.index("inet")
            if idx + 1 < len(fields):
                return fields[idx + 1].split("/")[0]

    return None


def get_ssid_for_interface(iface):
    ssid = None
    out = None
    line = None

    out = run_command(["iwgetid", iface, "--raw"], CMD_TIMEOUT_SHORT)
    if out is not None:
        ssid = out.strip()
        if ssid != "":
            return ssid

    out = run_command(["iw", "dev", iface, "link"], CMD_TIMEOUT_SHORT)
    if out is not None:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SSID:"):
                ssid = line[5:].strip()
                if ssid != "":
                    return ssid

    return None


def get_wifi_status(cache):
    interfaces = []
    iface = None
    ip_addr = None
    ssid = None
    attempt = None
    cached = None

    interfaces = get_wifi_interfaces()

    for attempt in range(WIFI_RETRIES):
        for iface in interfaces:
            ip_addr = get_ip_for_interface(iface)
            if ip_addr is None:
                continue

            ssid = get_ssid_for_interface(iface)
            if ssid is not None:
                update_cache_section(
                    cache,
                    "wifi",
                    {
                        "interface": iface,
                        "ssid": ssid,
                        "ip": ip_addr
                    }
                )
                return iface, ssid, ip_addr

            cached = get_cached_section(cache, "wifi", CACHE_MAX_AGE_WIFI)
            if cached is not None:
                if cached.get("interface") == iface and cached.get("ip") == ip_addr:
                    return iface, cached.get("ssid"), ip_addr

            return iface, None, ip_addr

        time.sleep(WIFI_DELAY)

    # No interface holds an IP right now. Unlike GPS (a static position), connectivity is
    # inherently a LIVE state — showing a cached SSID/IP as "connected" would be false.
    return None


def get_ethernet_status():
    interfaces = []
    iface = None
    ip_addr = None

    interfaces = get_ethernet_interfaces()

    for iface in interfaces:
        ip_addr = get_ip_for_interface(iface)
        if ip_addr is not None:
            return iface, ip_addr

    return None


def celsius_to_fahrenheit(value):
    return value * 9.0 / 5.0 + 32.0


def get_sun_phase_word(value):
    if value is None:
        return "N/A"

    if value >= SUNRISE_ALTITUDE_DEG:
        return "daylight"
    if value >= CIVIL_TWILIGHT_ALTITUDE_DEG:
        return "civil twilight"
    if value >= NAUTICAL_TWILIGHT_ALTITUDE_DEG:
        return "nautical twilight"
    if value >= ASTRONOMICAL_TWILIGHT_ALTITUDE_DEG:
        return "astronomical twilight"

    return "night"


def format_astropy_hms(t_astropy):
    if t_astropy is None:
        return "N/A"

    return time.strftime("%H:%M:%S", time.localtime(float(t_astropy.unix)))


def format_astropy_hm(t_astropy):
    if t_astropy is None:
        return "N/A"

    return time.strftime("%H:%M", time.localtime(float(t_astropy.unix)))


def pretty_minus(text):
    return text.replace("-", "−")


CHRONY_TRACKING_CMD = ["chronyc", "tracking"]
CHRONY_TRACKING_CMD_STR = "chronyc tracking"
CHRONY_CLIENTS_CMD = ["chronyc", "clients"]
CHRONY_CLIENTS_CMD_STR = "chronyc clients"
CHRONY_CLIENTS_SUDO_CMD = ["sudo", "-n", "chronyc", "clients"]

CHRONY_STATE_LABELS = {
    "*": ("current best", "best"),
    "+": ("combined", "best"),
    "-": ("not combined", "nc"),
    "?": ("unusable", "unus"),
    "x": ("may be in error", "unus"),
    "~": ("too variable", "unus"),
}


def parse_chrony_number(token):
    units = [("ns", 1e-9), ("us", 1e-6), ("ms", 1e-3), ("s", 1.0)]
    unit = None
    scale = None

    if token is None:
        return None

    token = token.strip()

    for unit, scale in units:
        if token.endswith(unit):
            try:
                return float(token[:-len(unit)]) * scale
            except ValueError:
                return None

    return None


def format_seconds_offset(seconds, include_sign=True):
    sign = None
    magnitude = None

    if seconds is None:
        return "N/A"

    if seconds < 0:
        sign = "−"
    elif include_sign:
        sign = "+"
    else:
        sign = ""

    magnitude = abs(seconds)

    if magnitude < 9.9995e-6:
        return "%s%.0f ns" % (sign, magnitude * 1e9)
    if magnitude < 0.0009995:
        return "%s%.3g µs" % (sign, magnitude * 1e6)
    if magnitude < 0.9995:
        return "%s%.3g ms" % (sign, magnitude * 1e3)
    if magnitude < 100.0:
        return "%s%.3g s" % (sign, magnitude)

    return "%s%.0f s" % (sign, magnitude)


def parse_chrony_sources(raw):
    rows = []
    in_data = None
    line = None
    fields = None
    sample = None
    row = None

    if raw is None:
        return rows

    in_data = False

    for line in raw.splitlines():
        if line.startswith("==="):
            in_data = True
            continue
        if not in_data:
            continue

        line = line.rstrip()
        if len(line) < 3:
            continue

        fields = line[2:].split()
        if len(fields) < 6:
            continue

        sample = " ".join(fields[5:])

        row = {}
        row["mode"] = line[0]
        row["state"] = line[1]
        row["name"] = fields[0]
        row["stratum"] = fields[1]
        row["poll"] = fields[2]
        row["reach"] = fields[3]
        row["lastrx"] = fields[4]
        row["offset_raw"] = sample.split("[")[0].strip()
        row["offset_s"] = parse_chrony_number(row["offset_raw"])

        if "+/-" in sample:
            row["error_raw"] = sample.split("+/-")[-1].strip()
        else:
            row["error_raw"] = None
        row["error_s"] = parse_chrony_number(row["error_raw"])

        rows.append(row)

    return rows


def find_best_chrony_source(rows):
    row = None

    for row in rows:
        if row.get("state") == "*":
            return row

    return None


def parse_chrony_tracking(raw):
    data = None
    line = None
    key = None
    value = None

    if raw is None:
        return None

    data = {}

    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()

    if len(data) == 0:
        return None

    return data


def get_tracking_reference_name(tracking):
    ref = None

    if tracking is None:
        return None

    ref = tracking.get("Reference ID")
    if ref is None:
        return None

    if "(" in ref and ")" in ref:
        return ref.split("(", 1)[1].split(")", 1)[0]

    return ref


def parse_tracking_seconds(text):
    fields = None
    value = None

    if text is None:
        return None

    fields = text.split()
    if len(fields) == 0:
        return None

    try:
        value = float(fields[0])
    except ValueError:
        return None

    if "slow" in text:
        value = -value

    return value


def parse_tracking_ppm(text):
    fields = None
    value = None

    if text is None:
        return None

    fields = text.split()
    if len(fields) == 0:
        return None

    try:
        value = float(fields[0])
    except ValueError:
        return None

    if "slow" in text:
        value = -value

    return value


def get_ntp_service_status():
    name = None
    out = None

    for name in ["chrony", "chronyd"]:
        out = run_command(["systemctl", "is-active", name], CMD_TIMEOUT_SHORT)
        if out is not None and out.strip() == "active":
            return "active"

    return None


def parse_chrony_clients(out):
    count = None
    in_data = None
    line = None
    fields = None

    if out is None:
        return None

    count = 0
    in_data = False

    for line in out.splitlines():
        if line.startswith("==="):
            in_data = True
            continue
        if not in_data:
            continue

        line = line.strip()
        if line == "":
            continue

        # error replies like "501 Not authorised" appear as data lines
        fields = line.split()
        if len(fields[0]) == 3 and fields[0].isdigit():
            return None

        # count only hosts that sent NTP queries; localhost typically
        # shows command packets only (chronyc monitoring, this script)
        if len(fields) >= 2:
            try:
                if int(fields[1]) == 0:
                    continue
            except ValueError:
                pass

        count += 1

    if not in_data:
        return None

    return count


# When sudoers does not allow it, the `sudo -n chronyc clients` fallback writes a
# pam/authpriv line into the persistent journal on EVERY page run (~1000 lines/day of
# pure noise on the SD). A denial leaves a marker in shm and the fallback is retried at
# most once an hour — the client count is a display nicety, not safety data.
SUDO_CHRONYC_DENIED_MARKER = _SHM + "/status_chronyc_sudo_denied"


def get_chrony_clients_count():
    out = run_command(CHRONY_CLIENTS_CMD, CMD_TIMEOUT_SHORT)
    count = parse_chrony_clients(out)
    if count is not None:
        return count

    try:
        if (os.path.exists(SUDO_CHRONYC_DENIED_MARKER)
                and time.time() - os.path.getmtime(SUDO_CHRONYC_DENIED_MARKER) < 3600):
            return None                        # denied recently; don't spam authpriv
    except OSError:
        pass
    out = run_command(CHRONY_CLIENTS_SUDO_CMD, CMD_TIMEOUT_SHORT)
    count = parse_chrony_clients(out)
    if count is None:
        try:
            with open(SUDO_CHRONYC_DENIED_MARKER, "w", encoding="utf-8"):
                pass
        except OSError:
            pass
    return count


def get_ntp_info():
    info = {}

    info["tracking_raw"] = run_command(CHRONY_TRACKING_CMD, CMD_TIMEOUT_SHORT)
    info["tracking"] = parse_chrony_tracking(info["tracking_raw"])
    info["service"] = get_ntp_service_status()
    info["clients"] = get_chrony_clients_count()

    return info


def get_refresh_seconds():
    value = None
    seconds = None

    value = os.environ.get(REFRESH_SECONDS_ENV_VAR)
    if value is None:
        return REFRESH_SECONDS_DEFAULT

    try:
        seconds = int(value)
    except ValueError:
        return REFRESH_SECONDS_DEFAULT

    if seconds < 1:
        return REFRESH_SECONDS_DEFAULT

    return seconds


def is_night_default(sun_info):
    hour = None

    if sun_info is not None and sun_info.get("available"):
        return bool(sun_info.get("camera_should_stack"))

    hour = time.localtime().tm_hour
    return hour < 6 or hour >= 21


PAGE_CSS = """  body { margin: 0; }
  .page {
    --bg: #fbfaf7; --card: #ffffff; --ink: #23282c; --muted: #5f6a72;
    --faint: #8b959c; --line: #e3e1da; --accent: #0e6a63;
    --good: #1e7d4f; --goodbg: #e3efe9; --warn: #a2620d; --warnbg: #f6ecdc;
    --neut: #5f6a72; --neutbg: #efede8;
    --tl-night: #24314f; --tl-astro: #3a4d78; --tl-naut: #5a6f97;
    --tl-civil: #8fa3bf; --tl-day: #ecd9a8; --tl-now: #c23b22;
    --codebg: #f1efe9; --mastline: #23282c;
    background: var(--bg); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 28px 24px 52px; min-height: 100vh;
  }
  .page.night {
    --bg: #0f141d; --card: #161d29; --ink: #e9edf4; --muted: #94a0b4;
    --faint: #67748a; --line: #263042; --accent: #e3a548;
    --good: #57c98f; --goodbg: rgba(87,201,143,.13); --warn: #e0b25e; --warnbg: rgba(224,178,94,.13);
    --neut: #94a0b4; --neutbg: #1c2534;
    --tl-night: #131a2e; --tl-astro: #1d2a52; --tl-naut: #2a4173;
    --tl-civil: #4a6a9a; --tl-day: #b8c6d8; --tl-now: #e3a548;
    --codebg: #161d29; --mastline: #3a4558;
  }
  .wrap { max-width: 1020px; margin: 0 auto; }
  .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }

  .mast { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; flex-wrap: wrap;
          border-bottom: 2px solid var(--mastline); padding-bottom: 14px; }
  .mast h1 { font-family: Georgia, "Times New Roman", serif; font-size: 30px; font-weight: 600; margin: 0; letter-spacing: .01em; }
  .mastright { display: flex; align-items: flex-end; gap: 18px; }
  .when { text-align: right; }
  .when .t { font-size: 27px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .when .d { font-size: 13px; color: var(--muted); }
  .modebtn {
    font: 13px system-ui, sans-serif; color: var(--muted); background: var(--card);
    border: 1px solid var(--line); border-radius: 999px; padding: 7px 14px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 7px; margin-bottom: 4px;
  }
  .modebtn:hover { color: var(--ink); border-color: var(--muted); }
  .modebtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

  .pills { display: flex; gap: 8px; flex-wrap: wrap; margin: 14px 0 0; }
  .pill { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px;
          padding: 5px 12px; border-radius: 999px; border: 1px solid var(--line);
          background: var(--card); color: var(--ink); white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--good); flex: none; }
  .dot.off { background: var(--faint); }
  .dot.warnc { background: var(--warn); }

  .lede { font-size: 15.5px; line-height: 1.55; margin: 18px 0 22px; }
  .lede .ok { color: var(--good); font-weight: 600; }
  .lede .attn { color: var(--warn); font-weight: 600; }

  .tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 26px; }
  @media (max-width: 860px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 13px 16px; }
  .tile .k { font-size: 11.5px; color: var(--muted); letter-spacing: .06em; text-transform: uppercase; margin-bottom: 7px; }
  .tile .v { font-size: 29px; font-weight: 600; line-height: 1.05; }
  .tile .v.dual { font-size: 23px; }
  /* radar map: exactly one image visible, matching the page style */
  .radar-img { width: 100%; max-width: 440px; height: auto; display: block;
               border: 1px solid var(--line); border-radius: 10px; }
  #page:not(.night) .radar-img.radar-night { display: none; }
  #page.night .radar-img.radar-day { display: none; }
  .tile .v .u { font-size: 16px; font-weight: 400; color: var(--muted); margin-left: 2px; }
  .tile .s { font-size: 12.5px; color: var(--faint); margin-top: 6px; }
  .tile.hot .v { color: var(--accent); }

  .grid { display: grid; grid-template-columns: 1fr 1.08fr; gap: 30px; margin-bottom: 24px; }
  @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } }
  h2 { font-size: 12px; letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
       font-weight: 700; margin: 0 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }

  .chips { display: flex; gap: 8px 20px; flex-wrap: wrap; font-size: 13.5px; color: var(--muted); margin-bottom: 12px; }
  .chips b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
  .chips .em b { color: var(--accent); }
  .tl { position: relative; height: 22px; border-radius: 5px; overflow: hidden; display: flex; margin: 2px 0 6px; }
  .tl div { height: 100%; }
  .b-night { background: var(--tl-night); } .b-astro { background: var(--tl-astro); }
  .b-naut { background: var(--tl-naut); } .b-civil { background: var(--tl-civil); }
  .b-day { background: var(--tl-day); }
  .now { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--tl-now); }
  .tlx { display: flex; justify-content: space-between; font-size: 11px; color: var(--faint);
         margin-bottom: 14px; font-variant-numeric: tabular-nums; }
  table.sun { width: 100%; border-collapse: collapse; font-size: 14px; margin-bottom: 28px; }
  table.sun th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
                 color: var(--faint); font-weight: 700; padding: 0 8px 7px 0; border-bottom: 1px solid var(--line); }
  table.sun td { padding: 6px 8px 6px 0; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
  table.sun td:first-child { color: var(--muted); }

  .rows { display: grid; grid-template-columns: max-content 1fr; gap: 7px 18px; font-size: 14.5px; margin: 0 0 28px; }
  .rows dt { color: var(--muted); }
  .rows dd { margin: 0; font-variant-numeric: tabular-nums; }
  .rows dd.down { color: var(--faint); }
  .livechip { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 999px;
              background: var(--goodbg); color: var(--good); vertical-align: 1px; margin-left: 6px; }

  figure { margin: 0 0 28px; }
  figure img { width: 100%; height: auto; display: block; border: 1px solid var(--line); }
  figcaption { font-size: 12.5px; color: var(--faint); margin-top: 8px; }

  .srcwrap { overflow-x: auto; margin-bottom: 8px; }
  table.src { width: 100%; min-width: 560px; border-collapse: collapse; font-size: 13.5px; }
  table.src th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
                 color: var(--faint); font-weight: 700; padding: 0 10px 7px 0; border-bottom: 1px solid var(--line); }
  table.src td { padding: 7px 10px 7px 0; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
  table.src td.num, table.src th.num { text-align: right; padding-right: 0; }
  .st { font-size: 11px; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
  .st.best { background: var(--goodbg); color: var(--good); }
  .st.nc { background: var(--neutbg); color: var(--neut); }
  .st.unus { background: var(--warnbg); color: var(--warn); }

  .unavail { color: var(--faint); font-size: 14px; }

  details { border-top: 1px solid var(--line); }
  details:last-of-type { border-bottom: 1px solid var(--line); }
  details summary { cursor: pointer; padding: 12px 0; font-size: 13.5px; color: var(--muted); }
  details summary:hover { color: var(--ink); }
  details pre { overflow-x: auto; background: var(--codebg); border: 1px solid var(--line); border-radius: 6px;
                padding: 12px; font-size: 12px; color: var(--muted); }
  details .cmdrow { font-size: 13px; color: var(--muted); margin: 6px 0; }
  details .cmdrow code { background: var(--codebg); border: 1px solid var(--line); padding: 2px 6px; border-radius: 4px; }
  .foot { font-size: 12px; color: var(--faint); margin-top: 22px; }

  /* hazards section: NWS alerts (the only hazard input that can veto) + info-only feeds.
     Red is kept for borders/badges with white text: red TEXT on the night card is unreadable. */
  .hz { margin: 0 0 24px; }
  .hz h3 { font-size: 14px; margin: 14px 0 6px; }
  .hz-veto { display: flex; gap: 8px; align-items: flex-start; border: 2px solid #b42318;
             border-radius: 10px; padding: 10px 14px; margin: 0 0 10px; background: var(--card); }
  .hz-veto .dot { margin-top: 5px; }
  .hz-veto.hz-pend { border-color: var(--warn); }
  .hz-veto-t { font-size: 15px; }
  .hz-veto-s { font-size: 12.5px; color: var(--muted); margin-top: 3px; }
  .hz-note { font-size: 13px; color: var(--muted); margin: 0 0 10px; }
  .hz-line { font-size: 14px; margin: 4px 0 10px; }
  .hz-list { list-style: none; padding: 0; margin: 0 0 12px; }
  .hz-a { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line); }
  .hz-sw { flex: none; width: 14px; height: 14px; border-radius: 3px; margin-top: 3px;
           border: 1px solid var(--line); }
  .hz-body { min-width: 0; flex: 1 1 auto; }
  .hz-ev { font-size: 14.5px; }
  .hz-hl { font-size: 13px; margin-top: 2px; }
  .hz-m { font-size: 12.5px; color: var(--muted); margin-top: 2px; }
  .hz-chip { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 999px;
             background: var(--neutbg); color: var(--neut); margin-left: 6px; white-space: nowrap;
             vertical-align: 1px; }
  .hz-chip.threat { background: var(--ink); color: var(--bg); font-weight: 700; letter-spacing: .04em; }
  .hz-chip.veto { background: #b42318; color: #fff; font-weight: 700; letter-spacing: .04em; }
  .hz-chip.vtype { background: var(--warnbg); color: var(--warn); }
  .hz-txt { white-space: pre-wrap; font-size: 12.5px; color: var(--muted); margin: 6px 0; }
  .hz-info { list-style: none; padding: 0; margin: 0 0 10px; font-size: 13.5px; }
  .hz-info > li { padding: 5px 0; border-bottom: 1px solid var(--line); }
  .hz-info ul { margin: 3px 0 0; padding-left: 18px; }
  .hz-fs { font-size: 11.5px; color: var(--faint); margin-left: 6px; }
  .hz-src { font-size: 11px; color: var(--faint); margin: 6px 0 0; }
  .hz details, .hz details:last-of-type { border: 0; }
  .hz details summary { padding: 4px 0 0; font-size: 12.5px; }
"""

PAGE_JS = """  var btn = document.getElementById('modebtn');
  var page = document.getElementById('page');
  function setMode(night) {
    if (night) { page.classList.add('night'); } else { page.classList.remove('night'); }
    btn.textContent = night ? '\\u2600 Day mode' : '\\u263E Night mode';
    btn.setAttribute('aria-pressed', String(night));
    try { sessionStorage.setItem('obs-night', night ? '1' : '0'); } catch (e) {}
  }
  btn.addEventListener('click', function () {
    setMode(!page.classList.contains('night'));
  });
  try {
    var stored = sessionStorage.getItem('obs-night');
    if (stored === '1') { setMode(true); }
    else if (stored === '0') { setMode(false); }
  } catch (e) {}
"""

TIMELINE_CLASSES = [
    "b-day", "b-civil", "b-naut", "b-astro", "b-night",
    "b-astro", "b-naut", "b-civil", "b-day"
]


def build_masthead_html(night_default):
    clock_str = None
    date_str = None
    utc_str = None
    btn_label = None
    pressed = None

    clock_str = time.strftime("%H:%M:%S %Z")
    date_str = "%s, %s %d, %s" % (
        time.strftime("%A"),
        time.strftime("%B"),
        int(time.strftime("%d")),
        time.strftime("%Y")
    )
    utc_str = time.strftime("%H:%M", time.gmtime())

    if night_default:
        btn_label = "☀ Day mode"
        pressed = "true"
    else:
        btn_label = "☾ Night mode"
        pressed = "false"

    return """  <div class="mast">
    <h1>Observatory Clock and Safety Monitor</h1>
    <div class="mastright">
      <button class="modebtn" id="modebtn" type="button" aria-pressed="%s">%s</button>
      <div class="when">
        <div class="t mono">%s</div>
        <div class="d">%s · %s UTC</div>
      </div>
    </div>
  </div>
""" % (
        pressed,
        html.escape(btn_label),
        html.escape(clock_str),
        html.escape(date_str),
        html.escape(utc_str)
    )


def build_pill_html(label, dot_class):
    return '    <span class="pill"><span class="dot%s"></span>%s</span>\n' % (
        dot_class,
        html.escape(label)
    )


def build_pills_html(best_source, ntp_info, gps_source, camera_ok, wifi_data):
    parts = []
    tracking = None
    stratum = None
    ssid = None
    ip_addr = None

    if best_source is not None and best_source.get("name") == "PPS":
        parts.append(build_pill_html("GPS PPS lock", ""))
    elif best_source is not None:
        parts.append(build_pill_html("Clock synced · %s" % best_source.get("name"), ""))
    else:
        parts.append(build_pill_html("Clock not synced", " warnc"))

    if ntp_info is not None:
        tracking = ntp_info.get("tracking")
    if tracking is not None:
        stratum = tracking.get("Stratum")

    if ntp_info is not None and ntp_info.get("service") == "active":
        if stratum == "0":
            parts.append(build_pill_html("NTP serving · not synced", " warnc"))
        elif stratum is not None:
            parts.append(build_pill_html("NTP serving · stratum %s" % stratum, ""))
        else:
            parts.append(build_pill_html("NTP serving", ""))
    else:
        parts.append(build_pill_html("NTP service unknown", " off"))

    if gps_source == "live":
        parts.append(build_pill_html("GPS fix · live", ""))
    elif gps_source == "cached":
        parts.append(build_pill_html("GPS fix · cached", " warnc"))
    else:
        parts.append(build_pill_html("No GPS fix", " off"))

    if camera_ok is None:
        parts.append(build_pill_html("Camera disabled", " off"))
    elif camera_ok:
        parts.append(build_pill_html("Enclosure camera OK", ""))
    else:
        parts.append(build_pill_html("Enclosure camera error", " warnc"))

    if wifi_data is not None and wifi_data[2] is not None:
        ssid = wifi_data[1]
        ip_addr = wifi_data[2]
        if ssid is not None and ssid != "":
            parts.append(build_pill_html("WiFi · %s" % ssid, ""))
        else:
            parts.append(build_pill_html("WiFi · %s" % ip_addr, ""))
    else:
        parts.append(build_pill_html("WiFi down", " off"))

    return '  <div class="pills">\n' + "".join(parts) + '  </div>\n'


def build_lede_html(t, h, sun_info, best_source, camera_ok,
                    dht_age=None, gps_source=None, ntp_info=None):
    sentences = []
    lead = None
    alt = None
    error_str = None

    if best_source is not None:
        if best_source.get("error_s") is not None:
            error_str = "±" + format_seconds_offset(best_source.get("error_s"), False)
        if best_source.get("name") == "PPS":
            if error_str is not None:
                sentences.append("The station clock is locked to GPS PPS within %s." % error_str)
            else:
                sentences.append("The station clock is locked to GPS PPS.")
        else:
            if error_str is not None:
                sentences.append(
                    "The station clock is synchronized to %s within %s."
                    % (best_source.get("name"), error_str)
                )
            else:
                sentences.append(
                    "The station clock is synchronized to %s." % best_source.get("name")
                )
    else:
        sentences.append("The station clock is not currently synchronized to any time source.")

    if t is not None and h is not None:
        age_note = (" (sensor reading %d min old)" % (dht_age // 60)
                    if dht_age is not None and dht_age > 150 else "")
        sentences.append(
            "Enclosure reads %.1f °C (%.0f °F) at %d %% humidity.%s"
            % (t, celsius_to_fahrenheit(t), h, age_note)
        )
    elif t is not None:
        sentences.append(
            "Enclosure reads %.1f °C (%.0f °F)."
            % (t, celsius_to_fahrenheit(t))
        )
    elif h is not None:
        sentences.append("Enclosure humidity is %d %%." % h)

    if sun_info is not None and sun_info.get("available"):
        alt = sun_info.get("sun_altitude_deg")
        if alt is not None and alt >= 0.0:
            sentences.append(
                "The sun is %.1f° up — sunset at %s (in %s), astronomical darkness from %s."
                % (
                    alt,
                    format_astropy_hm(sun_info.get("next_sunset")),
                    format_duration_minutes(sun_info.get("until_sunset")),
                    format_astropy_hm(sun_info.get("next_astronomical_dusk"))
                )
            )
        elif alt is not None:
            sentences.append(
                "The sun is %.1f° below the horizon — sunrise at %s (in %s)."
                % (
                    -alt,
                    format_astropy_hm(sun_info.get("next_sunrise")),
                    format_minutes_only(sun_info.get("until_sunrise"))
                )
            )

    # "All systems" must actually mean all of them: clock sync, camera, GPS fix, NTP.
    ntp_ok = ntp_info is not None and ntp_info.get("service") == "active"
    if (best_source is not None and camera_ok is not False
            and gps_source is not None and ntp_ok):
        lead = '<span class="ok">All systems nominal.</span>'
    else:
        lead = '<span class="attn">Attention needed.</span>'

    return '  <p class="lede">%s %s</p>\n' % (lead, html.escape(" ".join(sentences)))


def build_tiles_html(t, h, sun_info, tz_str, dht_age=None):
    temp_html = None
    hum_html = None
    sun_html = None
    sun_sub = None
    dawn_html = None
    dawn_sub = None
    alt = None
    until_civil_dawn = None
    next_civil_dawn = None

    if t is None:
        temp_html = '<div class="v mono">N/A</div>'
    else:
        temp_html = (
            '<div class="v mono dual">%.1f<span class="u">°C</span>'
            ' / %.0f<span class="u">°F</span></div>'
            % (t, celsius_to_fahrenheit(t))
        )

    if h is None:
        hum_html = '<div class="v mono">N/A</div>'
    else:
        hum_html = '<div class="v mono">%d<span class="u">%%</span></div>' % h

    if sun_info is not None and sun_info.get("available"):
        alt = sun_info.get("sun_altitude_deg")
        until_civil_dawn = sun_info.get("until_civil_dawn")
        next_civil_dawn = sun_info.get("next_civil_dawn")

    if alt is None:
        sun_html = '<div class="v mono">N/A</div>'
        sun_sub = ""
    else:
        sun_html = '<div class="v mono">%+.1f<span class="u">°</span></div>' % alt
        sun_sub = get_sun_phase_word(alt)

    if until_civil_dawn is None:
        dawn_html = '<div class="v mono">N/A</div>'
        dawn_sub = ""
    else:
        dawn_html = '<div class="v mono">%.0f<span class="u">min</span></div>' % until_civil_dawn
        dawn_sub = "civil dawn %s %s" % (format_astropy_hms(next_civil_dawn), tz_str)

    # an old cached sensor reading must be labelled as such, not shown as current
    if dht_age is not None and dht_age > 150:
        dht_sub = "enclosure &middot; cached %d min ago" % (dht_age // 60)
    else:
        dht_sub = "enclosure"

    return """  <div class="tiles">
    <div class="tile">
      <div class="k">Temperature</div>
      %s
      <div class="s">%s</div>
    </div>
    <div class="tile">
      <div class="k">Humidity</div>
      %s
      <div class="s">%s</div>
    </div>
    <div class="tile">
      <div class="k">Sun altitude</div>
      %s
      <div class="s">%s</div>
    </div>
    <div class="tile hot">
      <div class="k">Until civil dawn</div>
      %s
      <div class="s">%s</div>
    </div>
  </div>
""" % (
        temp_html,
        dht_sub,
        hum_html,
        dht_sub,
        sun_html,
        html.escape(sun_sub),
        dawn_html,
        html.escape(dawn_sub)
    )


def build_timeline_html(timeline, now_unix):
    crossings = None
    bounds = None
    total = None
    segments = None
    width = None
    now_pct = None
    i = None

    if timeline is None:
        return ""

    crossings = timeline.get("crossings")
    if crossings is None or len(crossings) != 8:
        return ""

    for i in range(len(crossings)):
        if crossings[i] is None:
            return ""

    bounds = [timeline["start_unix"]] + crossings + [timeline["end_unix"]]
    total = timeline["end_unix"] - timeline["start_unix"]

    if total <= 0:
        return ""

    for i in range(len(bounds) - 1):
        if bounds[i + 1] < bounds[i]:
            return ""

    segments = []
    for i in range(9):
        width = (bounds[i + 1] - bounds[i]) / total * 100.0
        segments.append(
            '        <div class="%s" style="width:%.2f%%"></div>\n'
            % (TIMELINE_CLASSES[i], width)
        )

    now_pct = (now_unix - timeline["start_unix"]) / total * 100.0
    now_pct = min(max(now_pct, 0.0), 100.0)

    return (
        '      <div class="tl">\n'
        + "".join(segments)
        + '        <span class="now" style="left:%.2f%%"></span>\n' % now_pct
        + '      </div>\n'
        + '      <div class="tlx"><span>12:00</span><span>18:00</span>'
        + '<span>00:00</span><span>06:00</span><span>12:00</span></div>\n'
    )


def build_twilight_html(sun_info, tz_str, now_unix):
    reason = None
    chips = None
    timeline_html = None
    table = None
    tz_esc = None

    if sun_info is None or not sun_info.get("available"):
        reason = "N/A"
        if sun_info is not None and sun_info.get("error") is not None:
            reason = sun_info.get("error")
        return (
            '      <h2>Night &amp; twilight</h2>\n'
            '      <p class="unavail">Sun information unavailable — %s.</p>\n'
            % html.escape(reason)
        )

    tz_esc = html.escape(tz_str)

    chips = (
        '      <div class="chips">\n'
        '        <span>Sunset <b>%s</b> · in <b>%s</b></span>\n'
        '        <span>Nautical dark <b>%s</b> · in <b>%s</b></span>\n'
        '        <span class="em">Sunrise <b>%s</b> · in <b>%s</b></span>\n'
        '        <span>Nautical dark ends <b>%s</b> · in <b>%s</b></span>\n'
        '      </div>\n'
        % (
            html.escape(format_astropy_hm(sun_info.get("next_sunset"))),
            html.escape(format_duration_minutes(sun_info.get("until_sunset"))),
            html.escape(format_astropy_hm(sun_info.get("next_nautical_dusk"))),
            html.escape(format_duration_minutes(sun_info.get("until_nautical_dusk"))),
            html.escape(format_astropy_hm(sun_info.get("next_sunrise"))),
            html.escape(format_minutes_only(sun_info.get("until_sunrise"))),
            html.escape(format_astropy_hm(sun_info.get("next_nautical_dawn"))),
            html.escape(format_minutes_only(sun_info.get("until_nautical_dawn")))
        )
    )

    timeline_html = build_timeline_html(sun_info.get("timeline"), now_unix)

    table = (
        '      <table class="sun mono">\n'
        '        <tr><th>Phase</th><th>Dusk (%s)</th><th>Dawn (%s)</th></tr>\n'
        '        <tr><td>Sunset / sunrise</td><td>%s</td><td>%s</td></tr>\n'
        '        <tr><td>Civil</td><td>%s</td><td>%s</td></tr>\n'
        '        <tr><td>Nautical</td><td>%s</td><td>%s</td></tr>\n'
        '        <tr><td>Astronomical</td><td>%s</td><td>%s</td></tr>\n'
        '      </table>\n'
        % (
            tz_esc,
            tz_esc,
            html.escape(format_astropy_hms(sun_info.get("sunset"))),
            html.escape(format_astropy_hms(sun_info.get("sunrise"))),
            html.escape(format_astropy_hms(sun_info.get("civil_dusk"))),
            html.escape(format_astropy_hms(sun_info.get("civil_dawn"))),
            html.escape(format_astropy_hms(sun_info.get("nautical_dusk"))),
            html.escape(format_astropy_hms(sun_info.get("nautical_dawn"))),
            html.escape(format_astropy_hms(sun_info.get("astronomical_dusk"))),
            html.escape(format_astropy_hms(sun_info.get("astronomical_dawn")))
        )
    )

    return (
        '      <h2>Night &amp; twilight — %s</h2>\n' % tz_esc
        + chips
        + timeline_html
        + table
    )


def build_position_html(gps_data, gps_source, wifi_data, ethernet_data):
    rows = []
    lat = None
    lon = None
    alt = None
    coord_html = None
    iface = None
    ssid = None
    ip_addr = None
    eth_iface = None
    eth_ip = None

    if gps_data is not None:
        lat, lon, alt = gps_data
        coord_html = html.escape(pretty_minus("%.7f°, %.7f°" % (lat, lon)))
        if gps_source == "live":
            coord_html += '<span class="livechip">live fix</span>'
        elif gps_source == "cached":
            coord_html += ' (cached)'
        rows.append(('GPS', coord_html, 'mono'))
        if alt is not None:
            rows.append(('Altitude', html.escape("%.1f m" % alt), 'mono'))
    else:
        rows.append(('GPS', 'no fix', 'down'))

    if ethernet_data is not None:
        eth_iface, eth_ip = ethernet_data
        rows.append((
            'Ethernet',
            html.escape("%s · %s" % (eth_iface, eth_ip)),
            'mono'
        ))
    else:
        rows.append(('Ethernet', 'not connected', 'down'))

    if wifi_data is not None and wifi_data[2] is not None:
        iface, ssid, ip_addr = wifi_data
        if ssid is None or ssid == "":
            ssid = "unknown network"
        rows.append((
            'WiFi',
            html.escape("%s · %s · %s" % (iface, ssid, ip_addr)),
            'mono'
        ))
    else:
        rows.append(('WiFi', 'not connected', 'down'))

    return (
        '      <dl class="rows">\n'
        + "".join(
            '        <dt>%s</dt><dd class="%s">%s</dd>\n' % (name, css, value)
            for name, value, css in rows
        )
        + '      </dl>\n'
    )


def build_ntp_html(ntp_info, ethernet_data):
    rows = []
    tracking = None
    service = None
    clients = None
    eth_ip = None
    value = None
    stratum = None
    ref_name = None
    sys_seconds = None
    freq_ppm = None
    skew = None
    leap = None

    if ntp_info is not None:
        tracking = ntp_info.get("tracking")
        service = ntp_info.get("service")
        clients = ntp_info.get("clients")

    if ethernet_data is not None:
        eth_ip = ethernet_data[1]

    if service == "active":
        value = '<b style="color:var(--good)">active</b>'
        if eth_ip is not None:
            value += ', serving on %s:123' % html.escape(eth_ip)
    else:
        value = 'status unknown'
    rows.append(('chronyd', value))

    if tracking is not None:
        stratum = tracking.get("Stratum")
        ref_name = get_tracking_reference_name(tracking)
        if stratum is not None and ref_name is not None:
            rows.append((
                'Stratum',
                html.escape("%s · reference: %s" % (stratum, ref_name))
            ))
        elif stratum is not None:
            rows.append(('Stratum', html.escape(stratum)))

        sys_seconds = parse_tracking_seconds(tracking.get("System time"))
        if sys_seconds is not None:
            rows.append((
                'System time',
                html.escape(
                    "within %s of true time"
                    % format_seconds_offset(abs(sys_seconds), False)
                )
            ))

        freq_ppm = parse_tracking_ppm(tracking.get("Frequency"))
        skew = tracking.get("Skew")
        if freq_ppm is not None:
            value = pretty_minus("%+.2f ppm" % freq_ppm)
            if skew is not None:
                value += " (skew %s)" % skew
            rows.append(('Frequency', html.escape(value)))

        leap = tracking.get("Leap status")
        if leap is not None:
            rows.append(('Leap status', html.escape(leap.lower())))
    else:
        rows.append(('Tracking', 'chronyc tracking unavailable'))

    if clients is not None:
        rows.append(('Clients', html.escape("%d known" % clients)))
    else:
        rows.append(('Clients', 'N/A'))

    return (
        '      <dl class="rows">\n'
        + "".join(
            '        <dt>%s</dt><dd>%s</dd>\n' % (name, value)
            for name, value in rows
        )
        + '      </dl>\n'
    )


def build_camera_html(have_image, camera_info):
    if camera_info is not None and camera_info.get("mode") == "disabled":
        return ('      <p class="unavail">Camera disabled '
                '(enable with TTU_STATUS_CAMERA=1 in ttustatus.env).</p>\n')
    image_name = None
    image_url = None
    image_html = None
    mode = None
    mode_caption = None
    error = None
    error_caption = None
    caption = None

    if have_image and os.path.exists(IMAGE_FILE):
        image_name = os.path.basename(IMAGE_FILE)
        image_url = "%s?t=%d" % (image_name, int(time.time()))
        image_html = (
            '        <img src="%s" alt="Latest enclosure camera snapshot">\n'
            % html.escape(image_url)
        )
    else:
        image_html = '        <p class="unavail">Snapshot unavailable</p>\n'

    mode = "unknown"
    error = None
    if camera_info is not None:
        if camera_info.get("mode") is not None:
            mode = camera_info.get("mode")
        error = camera_info.get("error")

    mode_caption = mode.replace("day,", "day mode,").replace("night,", "night mode,")

    if error is None:
        error_caption = "no errors"
    else:
        error_caption = "error: %s" % error

    # honest timestamp: when the snapshot was actually WRITTEN (capture end), not when
    # this page happened to be rendered — at night these can differ by many minutes
    try:
        cap_time = time.strftime("%H:%M:%S %Z",
                                 time.localtime(os.path.getmtime(IMAGE_FILE)))
    except Exception:
        cap_time = "time unknown"
    caption = "captured %s — %s, %s." % (
        cap_time,
        mode_caption,
        error_caption
    )

    return (
        '      <figure>\n'
        + image_html
        + '        <figcaption>%s</figcaption>\n' % html.escape(caption)
        + '      </figure>\n'
    )


def build_sources_html(source_rows):
    parts = []
    row = None
    label = None
    css_class = None
    offset_str = None
    error_str = None

    if len(source_rows) == 0:
        return '      <p class="unavail">No time sources available.</p>\n'

    parts.append('      <div class="srcwrap">\n')
    parts.append('      <table class="src mono">\n')
    parts.append(
        '        <tr><th>Source</th><th>State</th><th class="num">Str</th>'
        '<th class="num">Reach</th><th class="num">Offset</th>'
        '<th class="num">Error</th></tr>\n'
    )

    for row in source_rows:
        label, css_class = CHRONY_STATE_LABELS.get(
            row.get("state"),
            (row.get("state"), "nc")
        )

        if row.get("offset_s") is not None:
            offset_str = format_seconds_offset(row.get("offset_s"))
        else:
            offset_str = pretty_minus(str(row.get("offset_raw")))

        if row.get("error_s") is not None:
            error_str = "±" + format_seconds_offset(row.get("error_s"), False)
        elif row.get("error_raw") is not None:
            error_str = "±" + str(row.get("error_raw"))
        else:
            error_str = "N/A"

        parts.append(
            '        <tr><td>%s</td><td><span class="st %s">%s</span></td>'
            '<td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td></tr>\n'
            % (
                html.escape(str(row.get("name"))),
                css_class,
                html.escape(str(label)),
                html.escape(str(row.get("stratum"))),
                html.escape(str(row.get("reach"))),
                html.escape(offset_str),
                html.escape(error_str)
            )
        )

    parts.append('      </table>\n')
    parts.append('      </div>\n')

    return "".join(parts)


def build_details_html(chrony_raw, ntp_info, camera_info):
    parts = []
    tracking_raw = None
    capture_cmd = None
    processing_cmd = None
    mode = None
    error = None

    if ntp_info is not None:
        tracking_raw = ntp_info.get("tracking_raw")

    parts.append('  <details>\n')
    parts.append('    <summary>Raw chrony output &amp; commands</summary>\n')
    parts.append(
        '    <div class="cmdrow">Sources: <code class="mono">%s</code>'
        ' · Tracking: <code class="mono">%s</code>'
        ' · Clients: <code class="mono">%s</code></div>\n'
        % (
            html.escape(CHRONY_CMD_STR),
            html.escape(CHRONY_TRACKING_CMD_STR),
            html.escape(CHRONY_CLIENTS_CMD_STR)
        )
    )
    if chrony_raw is not None:
        parts.append('    <pre class="mono">%s</pre>\n' % html.escape(chrony_raw))
    if tracking_raw is not None:
        parts.append('    <pre class="mono">%s</pre>\n' % html.escape(tracking_raw))
    parts.append('  </details>\n')

    mode = "N/A"
    error = "N/A"
    capture_cmd = "N/A"
    processing_cmd = "N/A"

    if camera_info is not None:
        if camera_info.get("mode") is not None:
            mode = camera_info.get("mode")
        if camera_info.get("capture_command") is not None:
            capture_cmd = camera_info.get("capture_command")
        if camera_info.get("processing_command") is not None:
            processing_cmd = camera_info.get("processing_command")
        if camera_info.get("error") is None:
            error = "none"
        else:
            error = camera_info.get("error")

    parts.append('  <details>\n')
    parts.append('    <summary>Camera mode, commands &amp; diagnostics</summary>\n')
    parts.append(
        '    <div class="cmdrow">Mode: %s · Camera error: %s</div>\n'
        % (html.escape(mode), html.escape(error))
    )
    parts.append(
        '    <div class="cmdrow">Capture: <code class="mono">%s</code></div>\n'
        % html.escape(capture_cmd)
    )
    parts.append(
        '    <div class="cmdrow">Processing: <code class="mono">%s</code></div>\n'
        % html.escape(processing_cmd)
    )
    parts.append('  </details>\n')

    return "".join(parts)


def write_safety_inputs(sun_info, humidity, humidity_age_s=None, gps_data=None,
                        gps_source=None):
    alt = None
    if sun_info is not None:
        alt = sun_info.get("sun_altitude_deg")
    data = {
        "ts": time.time(),
        "sun_altitude_deg": alt,
        "humidity_pct": humidity,
        # real age of the humidity measurement (cache fallback), so the daemon's
        # staleness fail-safe judges the reading, not just this file's timestamp
        "humidity_age_s": humidity_age_s,
    }
    # the daemon adopts these once at startup when TTU_SAFETY_LAT/LON are unset
    # (rounded to ~1 km there, so GPS jitter never re-derives grids/tiles/stations)
    if gps_data is not None:
        data["lat"], data["lon"] = gps_data[0], gps_data[1]
        data["gps_source"] = gps_source
    tmp_file = SAFETY_INPUTS_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_file, SAFETY_INPUTS_FILE)


def read_safety_state():
    try:
        with open(SAFETY_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safety_dot_html(safe, unknown=False):
    # Three honest states: green = confirmed OK by current data, red = unsafe,
    # grey = NO current data (paused/off/unreachable) — never green, which would
    # falsely suggest the condition was checked and found clear.
    if unknown:
        color = "var(--faint, #8b959c)"
    else:
        color = "var(--good)" if safe else "#b42318"
    return ('<span class="dot" style="background:%s;display:inline-block;'
            'margin-right:6px;vertical-align:middle"></span>' % color)


def build_safety_tiles_html(comp, state_stale=False):
    sun = comp.get("sun", {})
    hum = comp.get("humidity", {})
    rain = comp.get("rain", {})

    sun_val = sun.get("value_deg")
    if sun_val is None:
        sun_v = '<div class="v mono">N/A</div>'
    else:
        sun_v = '<div class="v mono">%+.1f<span class="u">°</span></div>' % sun_val
    if sun.get("stale"):
        sun_s = safety_dot_html(False, unknown=True) + "stale inputs"
    elif sun.get("safe"):
        sun_s = safety_dot_html(True) + "below horizon"
    elif sun_val is None:
        sun_s = safety_dot_html(False, unknown=True) + "no data &mdash; failing safe"
    else:
        sun_s = safety_dot_html(False) + ("unsafe &gt; %g°" % sun.get("threshold_deg", 0))

    hum_val = hum.get("value_pct")
    if hum_val is None:
        hum_v = '<div class="v mono">N/A</div>'
    else:
        hum_v = '<div class="v mono">%.0f<span class="u">%%</span></div>' % hum_val
    if hum.get("stale"):
        hum_s = safety_dot_html(False, unknown=True) + "stale inputs"
    elif hum.get("safe"):
        hum_s = safety_dot_html(True) + "ok"
    elif hum_val is None:
        # no measurement -> we cannot claim a threshold breach, only that we fail safe
        hum_s = safety_dot_html(False, unknown=True) + "no data &mdash; failing safe"
    elif hum_val > hum.get("threshold_pct", 95):
        hum_s = safety_dot_html(False) + ("unsafe &gt; %g%%" % hum.get("threshold_pct", 95))
    else:
        # in the hysteresis hold band: the value itself is below the trip threshold
        hum_s = safety_dot_html(False) + "holding (hysteresis, clears &lt; 93%)"

    # Truthfulness rule: the value may only claim "no rain" when CURRENT data backs it.
    # Paused / no reporting stations => the value IS the no-data state (grey dot).
    if not rain.get("enabled", True):
        rain_v = '<div class="v mono">off</div>'
        rain_s = safety_dot_html(False, unknown=True) + "disabled &mdash; set WU_API_KEY"
    elif rain.get("latched"):
        rain_v = ('<div class="v mono">%d<span class="u">min</span></div>'
                  % (rain.get("seconds_remaining", 0) // 60))
        rain_s = safety_dot_html(False) + "rain latch active"
    elif not rain.get("polling_active"):
        rain_v = '<div class="v mono">paused</div>'
        rain_s = safety_dot_html(True, unknown=True) + "not polling (daytime)"
    elif rain.get("stations_live", 0) == 0:
        rain_v = '<div class="v mono">no&nbsp;data</div>'
        rain_s = safety_dot_html(True, unknown=True) + ("%d/%d stations reporting" % (
            rain.get("stations_live", 0), rain.get("stations_total", 0)))
    else:
        rain_v = '<div class="v mono">no&nbsp;rain</div>'
        rain_s = safety_dot_html(True) + ("polling %d/%d stations" % (
            rain.get("stations_live", 0), rain.get("stations_total", 0)))

    nws = comp.get("nws") or {}

    def _cpt(hour):
        def g(k):
            v = (hour or {}).get(k)
            return "?" if v is None else "%.0f" % v
        return "C%s%%/P%s%%/T%s%%" % (g("cloud_cover_pct"), g("precip_prob_pct"),
                                      g("thunder_prob_pct"))

    if not nws.get("available"):
        nws_v = '<div class="v mono" style="font-size:15px">N/A</div>'
        err = nws.get("error")
        why = (" (%s)" % html.escape(str(err)[:60])) if err else ""
        nws_s = safety_dot_html(True, unknown=True) + "forecast unavailable" + why
    else:
        nws_v = ('<div class="v mono" style="font-size:13px;line-height:1.4">'
                 'now %s<br>nxt %s</div>' % (_cpt(nws.get("now_hour")),
                                             _cpt(nws.get("next_hour"))))
        nws_s = (safety_dot_html(bool(nws.get("safe")))
                 + "Cloud &middot; Precip &middot; Thunder")

    glm = comp.get("glm") or {}
    glm_tk = glm.get("trigger_km", 50)
    if not glm.get("enabled", False):
        glm_v = '<div class="v mono">off</div>'
        glm_s = safety_dot_html(True, unknown=True) + "GLM off (deps)"
    elif glm.get("latched"):
        glm_v = ('<div class="v mono">%d<span class="u">min</span></div>'
                 % (glm.get("seconds_remaining", 0) // 60))
        glm_s = safety_dot_html(False) + ("STRIKE &le;%g km" % glm_tk)
    elif not glm.get("polling_active"):
        # a stale nearest-flash distance would look like current data — don't show it
        glm_v = '<div class="v mono">paused</div>'
        glm_s = safety_dot_html(True, unknown=True) + "not polling (daytime)"
    elif not glm.get("available"):
        glm_v = '<div class="v mono">no&nbsp;data</div>'
        glm_s = safety_dot_html(True, unknown=True) + "no successful poll yet"
    else:
        nk = glm.get("nearest_km")
        glm_v = ('<div class="v mono">%s</div>'
                 % ("&mdash;" if nk is None else '%.0f<span class="u">km</span>' % nk))
        glm_s = safety_dot_html(True) + ("no strike &le;%g km" % glm_tk)

    # Connectivity has no tile — it's a quiet watchdog; when the internet is down long
    # enough it forces UNSAFE and shows up as a reason, so no always-on "online" blob here.
    tile = ('    <div class="tile">\n      <div class="k">%s</div>\n      %s\n'
            '      <div class="s">%s</div>\n    </div>\n')
    items = [("Sun altitude", sun_v, sun_s), ("Humidity", hum_v, hum_s),
             ("Rain (WU)", rain_v, rain_s), ("NWS (now/next)", nws_v, nws_s),
             ("Lightning (GLM)", glm_v, glm_s)]
    hz_tile = _hazards_tile(comp.get("hazards"))
    if hz_tile is not None:           # absent = a daemon older than this page: no tile
        items.append(hz_tile)
    hz_vetoing = hz_tile is not None and hz_tile[1] == _HZ_TILE_VETO
    if state_stale:
        # The whole safety state is frozen (daemon down): none of the per-tile verdicts
        # are current — grey-out every claim (mirrors the radar section's guard; the
        # STALE banner alone must not leave green "no rain" behind). Numeric last values
        # stay, labelled; but a verdict WORD like "no rain" is itself a claim and gets
        # blanked — there is no current data to back it. (A last-known VETO stays: it
        # errs toward caution, and the stale sub-line says it may be over.)
        stale_sub = safety_dot_html(False, unknown=True) + "state stale &mdash; last known"
        blank = '<div class="v mono">&mdash;</div>'
        blanked = {"Rain (WU)"} | (set() if hz_vetoing else {"NWS warnings"})
        items = [(k, (blank if k in blanked else v), stale_sub) for k, v, _ in items]
    tiles = "".join(tile % it for it in items)
    # auto-fit: 5-across on the ~1020px desktop wrap, and wraps to 3/2/1 columns on
    # narrower/phone screens instead of shrinking into an unreadable single row. Six
    # tiles (with the NWS-warnings tile) would leave one orphan under a row of five, so
    # they are capped at three columns (3 + 3); the 1px of slack in the calc keeps
    # sub-pixel rounding from dropping the grid to two columns.
    if len(items) == 6:
        cols = "repeat(auto-fit,minmax(max(170px,calc((100% - 25px) / 3)),1fr))"
    else:
        cols = "repeat(auto-fit,minmax(170px,1fr))"
    return ('  <div class="tiles" style="grid-template-columns:%s">\n' % cols
            + tiles + '  </div>\n')


# The NWS-warnings tile value while a veto is held (also used to spot the veto state).
_HZ_TILE_VETO = '<div class="v mono">VETO</div>'


def _hazards_tile(hz):
    """(label, value html, sub html) for the NWS-warnings safety tile, or None when the
    state has no hazards component (older daemon). Three honest states, like the other
    tiles: red = a configured warning is vetoing, green = fresh NWS data shows no veto
    warning at the site, grey = no current data (which does not veto on its own)."""
    if not isinstance(hz, dict):
        return None
    label = "NWS warnings"
    veto = _hz_dicts(hz.get("veto"))
    if not hz.get("enabled", True) and not veto:
        # off by config, or the layer failed to load (then the daemon says why)
        err = hz.get("error")
        return (label, '<div class="v mono">off</div>',
                safety_dot_html(True, unknown=True)
                + (("unavailable (%s)" % _hz_e(err, 60)) if err
                   else "disabled (TTU_SAFETY_HAZARDS=0)"))
    if veto:
        first = veto[0]
        until = _hz_until(first)
        sub = _hz_e(first.get("event") or "NWS warning", 60)
        if until:
            sub += " until %s" % html.escape(until)
        if len(veto) > 1:
            sub += " (+%d more)" % (len(veto) - 1)
        return (label, _HZ_TILE_VETO, safety_dot_html(False) + sub)
    if not hz.get("safe", True):
        # unsafe without a listed veto should not happen; never render it as clear
        return (label, '<div class="v mono">unsafe</div>',
                safety_dot_html(False) + "NWS warning veto")
    # a veto warning issued for a later period: not a veto yet, but worth a line here
    pending = _hz_dicts(hz.get("veto_pending"))
    pend = ""
    if pending:
        p = pending[0]
        onset = p.get("onset_local") if isinstance(p.get("onset_local"), str) else None
        pend = " &middot; scheduled: %s%s" % (_hz_e(p.get("event") or "NWS warning", 60),
                                              (" from %s" % _hz_e(onset, 30)) if onset else "")
    if not hz.get("available"):
        err = hz.get("error")
        why = (" (%s)" % _hz_e(err, 60)) if err else ""
        return (label, '<div class="v mono" style="font-size:15px">N/A</div>',
                safety_dot_html(True, unknown=True) + "alerts unavailable" + why + pend)
    if pending:
        return (label, '<div class="v mono">none</div>',
                safety_dot_html(True) + "no veto now" + pend)
    counts = hz.get("counts") if isinstance(hz.get("counts"), dict) else {}
    n_site = counts.get("at_site")
    if not _hz_num(n_site):
        n_site = len(_hz_dicts(hz.get("at_site")))
    sub = safety_dot_html(True) + "no veto warning at the site"
    if n_site:
        sub += " &middot; %d info alert%s" % (n_site, "" if n_site == 1 else "s")
    return (label, '<div class="v mono">none</div>', sub)


def _safety_endpoint_html(state):
    a = state.get("alpaca") or {}
    addr = a.get("address")
    port = a.get("port")
    line = 'style="margin:0 0 12px;font-size:12.5px;color:var(--muted)"'
    mono = 'style="font-family:ui-monospace,Consolas,monospace"'
    if not addr or not port:
        return '    <div %s>ASCOM Alpaca SafetyMonitor (endpoint unknown)</div>\n' % line
    dev = a.get("device_number", 0)
    path = a.get("issafe_path", "/api/v1/safetymonitor/%s/issafe" % dev)
    return ('    <div %s>ASCOM Alpaca SafetyMonitor &middot; '
            '<span %s>%s:%s</span> &middot; device %s &middot; '
            'IsSafe: <span %s>%s</span></div>\n'
            % (line, mono, html.escape(str(addr)), html.escape(str(port)),
               html.escape(str(dev)), mono, html.escape(str(path))))


def build_safety_html(state):
    # A self-contained card so the safety monitor is visually distinct from the
    # clock/sensor/NTP content that follows it.
    card_open = ('  <section style="border:1px solid var(--line);border-radius:12px;'
                 'padding:16px 18px;margin:0 0 26px;background:var(--card)">\n')
    card_close = '  </section>\n'

    def head(label_text, bg):
        badge = ('<span style="padding:.35rem .9rem;border-radius:8px;font-weight:700;'
                 'letter-spacing:.03em;color:#fff;background:%s">%s</span>'
                 % (bg, html.escape(label_text)))
        return ('    <div style="display:flex;justify-content:space-between;'
                'align-items:center;gap:12px;flex-wrap:wrap">\n'
                '      <h2 style="margin:0">Alpaca safety monitor</h2>\n'
                '      %s\n    </div>\n' % badge)

    if state is None:
        body = ('    <p class="lede" style="margin:10px 0 0">No state from the safety '
                'daemon — is safety_monitor.py running?</p>\n')
        return card_open + head("OFFLINE", "#a2620d") + body + card_close

    is_safe = bool(state.get("is_safe"))
    age = None
    if state.get("ts") is not None:
        age = time.time() - state.get("ts")
    comp = state.get("components", {})

    # Fail-safe display: never show the green SAFE badge unless the state is BOTH
    # safe AND fresh. A stale/undated state (daemon crashed/hung) shows STALE, not SAFE.
    stale = (age is None) or (age > SAFETY_STATE_STALE_SEC)
    if is_safe and not stale:
        bg, label, detail = "#1a7f37", "SAFE", "safe to observe"
        detail_color = "var(--good)"     # green: this line was wrongly amber before
    elif stale:
        bg, label = "#a2620d", "STALE"
        detail = ("safety state has no timestamp — daemon may be down" if age is None
                  else "last update %d min ago — daemon may be down" % int(age / 60))
        detail_color = "var(--warn)"
    else:
        bg, label = "#b42318", "UNSAFE"
        detail = "%d reason(s)" % len(state.get("reasons") or [])
        detail_color = "var(--warn)"

    detail_html = ('    <div style="margin:4px 0 10px;font-size:12.5px;color:%s">'
                   '%s</div>\n' % (detail_color, html.escape(detail))) if detail else ""

    reasons_html = ""
    if (not is_safe or stale) and state.get("reasons"):
        items = "".join("        <li>%s</li>\n" % html.escape(r)
                        for r in state.get("reasons"))
        reasons_html = ('    <ul style="margin:0 0 14px;padding-left:20px;'
                        'color:var(--warn)">\n' + items + "    </ul>\n")
    # Non-vetoing warnings (e.g. GPS vs configured-coordinates mismatch) — always shown.
    if state.get("warnings"):
        items = "".join("        <li>&#9888; %s</li>\n" % html.escape(w)
                        for w in state.get("warnings"))
        reasons_html += ('    <ul style="margin:0 0 14px;padding-left:20px;'
                         'color:var(--warn);font-weight:600">\n' + items + "    </ul>\n")

    tiles_head = ('    <h3 style="margin:14px 0 8px;font-size:14px">'
                  'Inputs it is using</h3>\n')
    tiles = build_safety_tiles_html(comp, state_stale=stale)

    events = state.get("events_tail") or []
    ev_text = "".join("%s\n" % html.escape(e) for e in events) or "(none yet)"
    events_html = (
        '    <h3 style="margin:16px 0 8px;font-size:14px">Log (latest events)</h3>\n'
        '    <pre style="background:var(--codebg);border:1px solid var(--line);'
        'border-radius:8px;padding:10px 12px;overflow-x:auto;font-size:12px;'
        'white-space:pre-wrap;margin:0">%s</pre>\n' % ev_text
    )

    return (card_open + head(label, bg) + detail_html + _safety_endpoint_html(state)
            + reasons_html + tiles_head + tiles + events_html + card_close)


# ---- radar basemap line -----------------------------------------------------------------
# The daemon reports which basemap each radar map shows (state components.radar.basemap:
# "carto-cached" = CARTO tiles cached on the Pi, "carto" = fetched with
# TTU_SAFETY_CARTO_KEY, "custom-cached"/"custom" = TTU_SAFETY_RADAR_TILE_URL(_DAY),
# "osm" = OpenStreetMap — the key-free fallback, or the configured choice when "chosen_by"
# names the setting — "none" = plain background) plus short notes on what to configure.
# This is a COPY of safety/radar.py's basemap_summary (the page does not import the daemon
# package); a test keeps the two identical, so the page and /setup always say the same.
_BASEMAP_BOTH = {
    "carto-cached": "CARTO Dark Matter (night) / Positron (day), from tiles cached on this Pi",
    "carto": ("CARTO Dark Matter (night) / Positron (day), fetched with the configured "
              "CARTO API key"),
    "custom-cached": "custom tiles (TTU_SAFETY_RADAR_TILE_URL / _DAY), cached on this Pi",
    "custom": "custom tiles (TTU_SAFETY_RADAR_TILE_URL / _DAY)",
    "none": "none (plain background)",
}


def _basemap_osm_role(by):
    return "as set by %s" % by if by else "the key-free fallback"


def _basemap_one(name, src, inverted, by=None):
    style = "Dark Matter" if name == "night" else "Positron"
    var = "TTU_SAFETY_RADAR_TILE_URL" + ("" if name == "night" else "_DAY")
    return {
        "carto-cached": "CARTO %s, from tiles cached on this Pi" % style,
        "carto": "CARTO %s, fetched with the configured CARTO API key" % style,
        "custom-cached": "custom tiles (%s), cached on this Pi" % var,
        "custom": "custom tiles (%s)" % var,
        "osm": "OpenStreetMap standard tiles%s, %s"
               % (" (colour-inverted)" if inverted and name == "night" else "",
                  _basemap_osm_role(by)),
        "none": "none (plain background)",
    }.get(src, str(src))


def radar_basemap_summary(basemap, notes=()):
    """'Basemap: ...' in plain text (callers escape it); '' for an older daemon (no
    basemap dict) or before the first map is built."""
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
                       _basemap_osm_role(maps[0][2])))
        else:
            desc = _BASEMAP_BOTH.get(night, str(night))
    elif len(maps) == 1:
        desc = _basemap_one(maps[0][0], maps[0][1], inverted, maps[0][2])
    else:
        desc = "; ".join("%s map: %s" % (m, _basemap_one(m, s, inverted, b))
                         for m, s, b in maps)
    notes = [str(n) for n in (notes if isinstance(notes, (list, tuple)) else ()) if n]
    return "Basemap: " + desc + "".join("; " + n for n in notes) + "."


def build_radar_html(state):
    rad = ((state or {}).get("components", {}) or {}).get("radar")
    if not rad or not rad.get("enabled"):
        return ""   # radar off / Pillow missing -> omit the section
    rk = rad.get("trigger_km", 30)

    # Fail-safe display: if the whole safety state is stale (daemon hung/crashed), do NOT
    # show a reassuring green "no rain" over a frozen thumbnail — the radar verdict is only
    # as fresh as the daemon writing it.
    age = None
    if state.get("ts") is not None:
        age = time.time() - state.get("ts")
    state_stale = (age is None) or (age > SAFETY_STATE_STALE_SEC)

    if state_stale:
        verdict = safety_dot_html(False) + "safety state stale &mdash; radar reading unknown"
    elif rad.get("unconfirmed_echo"):
        # An echo IS present but has not repeated yet, so the layer is not vetoing. Checked
        # BEFORE the in_ring branch: saying "RAIN" here would contradict safe=True.
        near = rad.get("nearest_km")
        verdict = safety_dot_html(True, unknown=True) + (
            "echo within %g km%s &mdash; unconfirmed (%d of %d frames), not triggering" % (
                rk, "" if near is None else ", nearest %g km" % near,
                rad.get("ring_streak", 1), rad.get("trigger_after", 2)))
    elif rad.get("in_ring") and rad.get("available"):
        # in_ring is only exported while fresh, but require available too (belt-and-braces
        # against a state file written by an older daemon)
        near = rad.get("nearest_km")
        verdict = safety_dot_html(False) + ("<b>RAIN within %g km</b>%s" % (
            rk, "" if near is None else " &mdash; nearest %g km" % near))
    elif rad.get("latched"):
        left = int(rad.get("seconds_remaining", 0) // 60)
        verdict = safety_dot_html(False) + (
            "recent rain within %g km &mdash; %d min of the %g min freeze left" % (
                rk, left, rad.get("freeze_sec", 1800) / 60.0))
    elif rad.get("available"):
        verdict = safety_dot_html(True) + ("no rain within %g km" % rk)
    else:
        verdict = safety_dot_html(True, unknown=True) + "no fresh frame (radar unreachable?)"

    # The <img> uses a relative basename, which only resolves if the thumbnail lives in the
    # same web directory as status.html. There are up to two maps — a dark (night) and a
    # light (day) version — and CSS shows whichever matches the page's day/night style.
    web_dir = os.path.dirname(os.path.abspath(HTML_FILE))
    night_path = rad.get("thumb_path") or ""
    day_path = rad.get("thumb_path_day")
    # NOTE: no inline style here — an inline display:block would override the stylesheet's
    # day/night display:none switch, making BOTH maps show. All styling lives in .radar-img.

    def _usable(p):
        return bool(p) and os.path.dirname(os.path.abspath(p)) == web_dir

    def _img(p, cls):
        try:
            bust = int(os.path.getmtime(p))
        except Exception:
            bust = int(time.time())
        return ('<img class="radar-img %s" src="%s?t=%d" '
                'alt="MRMS radar with a %g km ring around the observatory">'
                % (cls, html.escape(os.path.basename(p)), bust, rk))

    if rad.get("thumb_available") and not state_stale and _usable(night_path):
        if day_path and _usable(day_path):
            img = _img(night_path, "radar-night") + _img(day_path, "radar-day")
        else:
            img = _img(night_path, "")   # single map, always shown
    elif rad.get("thumb_available") and night_path and not _usable(night_path):
        img = ('<div class="lede" style="color:var(--warn)">Radar image is at %s (not in the '
               'web directory) &mdash; set TTU_SAFETY_RADAR_THUMB beside status.html.</div>'
               % html.escape(night_path))
    else:
        img = ('<div class="lede" style="color:var(--muted)">Radar image not available '
               'yet (generated on the next radar poll, within 5&nbsp;min).</div>')

    frame = html.escape(str(rad.get("frame_utc") or "?"))
    attribution = html.escape(str(rad.get("attribution") or ""))
    source = (
        "Map: NOAA/NSSL <b>MRMS</b> composite radar reflectivity (every US weather radar "
        "fused, ~1&nbsp;km, updated every 2&nbsp;min), fetched from the Iowa Environmental "
        "Mesonet. The cyan ring is the <b>%g&nbsp;km</b> rain trigger: any echo inside it "
        "marks the monitor UNSAFE. Colours run green (light) &rarr; red/magenta "
        "(downpour). Polled every 5&nbsp;min, day and night." % rk)
    _comp = state.get("components") or {}
    if isinstance(_comp.get("hazards"), dict) or isinstance(_comp.get("hazard_info"), dict):
        source += (" Active NWS alert areas and other hazards are drawn over the radar when "
                   "present &mdash; the <b>Hazards</b> section below is their key.")
    # Which basemap the maps actually show (CARTO / OpenStreetMap / none) and what to set
    # for a better one, as the daemon reports it; an older daemon reports nothing here and
    # the paragraph stays as it was. The attribution line below follows the same sources.
    basemap_line = radar_basemap_summary(rad.get("basemap"), rad.get("basemap_notes"))
    if basemap_line:
        source += " " + html.escape(basemap_line)

    return (
        '  <h2>Radar (MRMS, %g km ring)</h2>\n'
        '  <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start;'
        'margin:0 0 24px">\n'
        '    <div style="flex:0 0 auto">%s</div>\n'
        '    <div style="flex:1 1 260px;min-width:240px">\n'
        '      <p class="lede" style="margin:0 0 8px">%s</p>\n'
        '      <p style="font-size:13px;color:var(--muted);margin:0 0 8px">%s</p>\n'
        '      <p style="font-size:11px;color:var(--faint,#8b959c);margin:0">%s · frame %s</p>\n'
        '    </div>\n'
        '  </div>\n' % (rk, img, verdict, source, attribution, frame)
    )


# ---- Hazards section: NWS alerts (the veto) + information-only hazard feeds -----------
# Inputs: state["components"]["hazards"] (safety/nws_alerts.py — the ONLY hazard input
# that can make the monitor unsafe, and only for the configured veto events over the
# site) and state["components"]["hazard_info"] (safety/hazard_feeds.py — information
# only, never part of IsSafe). Either may be missing (a daemon older than this page):
# then its part is omitted rather than claiming "no hazards".
# Fallback only: the daemon publishes its own verdict (area_fresh) and threshold
# (stale_after_s, TTU_SAFETY_HAZARD_STALE_SEC), which win; this is for an older daemon.
HAZARD_ALERTS_STALE_SEC = 600    # = the daemon's TTU_SAFETY_HAZARD_STALE_SEC default
# Sanity bound only: hazard_feeds.py already marks a feed not-ok after two poll intervals
# (ok=False, "stale: ..."); this catches an inconsistent "ok" with an ancient age, which
# must never back a "nothing current" statement.
HAZARD_INFO_STALE_SEC = 6 * 3600
HAZARD_LIST_MAX = 25             # listed per group; every alert is still drawn on the map
_HZ_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")   # used with fullmatch ($ would pass "...\n")
_HZ_GREY = "#808080"
_HZ_VETO_SOURCE = {
    "point": "confirmed by the NWS point query for the site",
    "local": "its polygon / the site's zone covers the site",
    "both": "confirmed by the NWS point query and its polygon / the site's zone",
    "latched": ("held until the warning ends (not re-confirmed by the latest NWS data "
                "— released early only when fresh data shows it is over)"),
}


def _hz_dicts(v):
    """The dict entries of a component list; anything else (a missing key, a
    malformed state file) is an empty list, never an exception."""
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _hz_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _hz_str(v, limit=None):
    """Display text for a feed value (None -> ''), cut to ``limit`` characters."""
    if v is None:
        return ""
    s = str(v).strip()
    if limit and len(s) > limit:
        s = s[:limit - 1].rstrip() + "…"
    return s


def _hz_e(v, limit=None):
    """Escaped display text. EVERY feed-derived string goes through here (or through
    _hz_color): alert text is written by thousands of offices and relayed civil
    authorities, and must never be able to inject markup into the page."""
    return html.escape(_hz_str(v, limit))


def _hz_color(c):
    """An alert colour is used inline in a style= attribute, so only a strict #RRGGBB
    passes; anything else (None, 'red', an injection attempt) becomes neutral grey."""
    return c if isinstance(c, str) and _HZ_HEX_RE.fullmatch(c) else _HZ_GREY


# The map's fallback colour per product type (safety/radar.py KIND_RGB), used there for a
# missing/invalid colour and for a green one — hazards are never drawn green.
_HZ_KIND_COLOR = {"warning": "#D00000", "watch": "#E6B800", "advisory": "#7B68EE",
                  "statement": "#FFE4B5", "other": "#808080"}
_HZ_STATEMENT_SUFFIXES = ("statement", "outlook", "alert", "message", "forecast",
                          "emergency")


def _hz_alert_color(a):
    """The swatch colour for an alert = the colour the radar map actually draws it in, so
    this list stays the map's legend: the feed's #RRGGBB, unless it is missing, invalid
    or green-dominant (radar._is_green: g >= 64 and g - max(r, b) >= 40), in which case
    the map uses its product-type fallback, and so does the swatch."""
    c = a.get("color")
    # same accepted spellings as radar._hex_rgb ('#RRGGBB', 'RRGGBB', '#RGB'), re-emitted
    # as a canonical, strictly validated #RRGGBB
    h = c.strip().lstrip("#") if isinstance(c, str) else ""
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if _HZ_HEX_RE.fullmatch("#" + h):
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        if not (g >= 64 and g - max(r, b) >= 40):
            return "#" + h.upper()
    kind = a.get("kind") if a.get("kind") in _HZ_KIND_COLOR else None
    if kind is None:
        name = _hz_str(a.get("event")).lower()
        kind = next((k for k in ("warning", "watch", "advisory") if name.endswith(k)),
                    "statement" if name.endswith(_HZ_STATEMENT_SUFFIXES) else "other")
    return _HZ_KIND_COLOR[kind]


def _hz_iso_ts(s):
    """ISO-8601 with an offset (as api.weather.gov sends it) -> epoch s, else None."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip()
    if t[-1:] in ("Z", "z"):
        t = t[:-1] + "+00:00"            # fromisoformat() on Python 3.9 rejects 'Z'
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None                      # a naive time is ambiguous: unknown
    return dt.timestamp()


def _hz_local(ts):
    """'Fri 10:00 CDT' in the daemon's display zone (TTU_SAFETY_LOCAL_TZ, which the daemon
    passes down to this script), so times formatted here match the ones the daemon
    pre-formats (end_local, onset_local) even if the Pi's system zone differs. Invalid or
    missing zone data -> the system's local time."""
    try:
        ts = float(ts)
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(os.environ.get("TTU_SAFETY_LOCAL_TZ", "America/Chicago"))
            return datetime.fromtimestamp(ts, tz).strftime("%a %H:%M %Z")
        except Exception:
            return time.strftime("%a %H:%M %Z", time.localtime(ts))
    except Exception:
        return None


def _hz_until(d):
    """Local end time of an alert / veto entry: the daemon's end_local, else ends, else
    expires (ISO), else end_ts (epoch). None when none is usable."""
    s = d.get("end_local")
    if isinstance(s, str) and s.strip():
        return s.strip()[:40]
    for k in ("ends", "expires"):
        ts = _hz_iso_ts(d.get(k))
        if ts is not None:
            return _hz_local(ts)
    if _hz_num(d.get("end_ts")):
        return _hz_local(d["end_ts"])
    return None


def _hz_age(s):
    if not _hz_num(s):
        return None
    s = max(0.0, float(s))
    if s < 90:
        return "%d s ago" % s
    if s < 5400:
        return "%d min ago" % round(s / 60.0)
    return "%.1f h ago" % (s / 3600.0)


def _hz_join(parts, sep=" &middot; "):
    return sep.join(p for p in parts if p)


def _hz_veto_html(veto, stale):
    """The red banner(s): one per configured warning currently vetoing the monitor."""
    out = []
    for v in veto:
        until = _hz_until(v)
        head = "UNSAFE: %s over the site%s" % (
            _hz_e(v.get("event") or "NWS warning", 80),
            (" until %s" % html.escape(until)) if until else "")
        if stale:
            head = "Last known (safety state stale): " + head
        sub = [_hz_e(v.get("sender"), 80)]
        if _hz_num(v.get("first_seen_ts")):
            sub.append("first seen %s" % html.escape(_hz_local(v["first_seen_ts"]) or "?"))
        sub.append(_HZ_VETO_SOURCE.get(v.get("source"), ""))
        hl = _hz_e(v.get("headline"), 300)
        out.append(
            '    <div class="hz-veto">%s<div>\n'
            '      <div class="hz-veto-t"><b>%s</b></div>\n'
            '%s'
            '      <div class="hz-veto-s">%s</div>\n'
            '    </div></div>\n'
            % (safety_dot_html(False), head,
               ('      <div class="hz-veto-s">%s</div>\n' % hl) if hl else "",
               _hz_join(sub)))
    return "".join(out)


def _hz_pending_html(pending, stale):
    """Amber notice(s): a configured warning issued for a LATER period over the site (a
    High Wind Warning "from 10 AM Friday"). nws_alerts vetoes it from shortly before its
    onset, not from issuance — so it is announced here, but it is not (yet) a veto."""
    out = []
    for p in pending:
        onset = p.get("onset_local") if isinstance(p.get("onset_local"), str) else None
        if not onset and _hz_num(p.get("onset_ts")):
            onset = _hz_local(p["onset_ts"])
        until = _hz_until(p)
        head = "Scheduled: %s for the site%s%s" % (
            _hz_e(p.get("event") or "NWS warning", 80),
            (" from %s" % _hz_e(onset, 40)) if onset else "",
            (" until %s" % html.escape(until)) if until else "")
        if stale:
            head = "Last known (safety state stale): " + head
        sub = [_hz_e(p.get("sender"), 80),
               "not a veto yet &mdash; the safety monitor goes UNSAFE shortly before it "
               "takes effect"]
        out.append(
            '    <div class="hz-veto hz-pend">'
            '<span class="dot warnc" style="display:inline-block;margin-right:6px;'
            'vertical-align:middle"></span><div>\n'
            '      <div class="hz-veto-t"><b>%s</b></div>\n'
            '      <div class="hz-veto-s">%s</div>\n'
            '    </div></div>\n' % (head, _hz_join(sub)))
    return "".join(out)


def _hz_alert_li(a, at_site, veto_keys, pending_keys, veto_names):
    """One NWS alert: colour swatch (the same colour the map uses), event, threat and
    VETO chips, headline(s), sender / time / area, and the full text folded away."""
    chips = []
    if a.get("threat"):
        chips.append('<span class="hz-chip threat">%s</span>' % _hz_e(a["threat"], 40))
    ev = _hz_str(a.get("event"))
    # VETO only for what is in the component's veto list — the very list IsSafe uses
    # (nws_alerts also flags those rows vetoes=True); a veto-TYPE event that is not
    # vetoing (not over the site, or not yet in effect) is labelled as such
    veto_type = (a.get("veto_event") is True or a.get("vetoes") is True
                 or (a.get("veto_event") is None and ev.lower() in veto_names))
    key = a.get("key") if isinstance(a.get("key"), str) else None
    if key is not None and key in veto_keys:
        chips.append('<span class="hz-chip veto">VETO</span>')
    elif veto_type:
        if key is not None and key in pending_keys:
            why = "not in effect yet"
        elif at_site:
            why = "not vetoing"
        else:
            # so a red Tornado Warning 20 km away is not mistaken for the reason the
            # monitor is (or is not) closed
            why = "not over the site"
        chips.append('<span class="hz-chip vtype">veto type &middot; %s</span>' % why)
    for k in ("severity", "urgency"):
        val = _hz_str(a.get(k), 20)
        if val and val.lower() != "unknown":
            chips.append('<span class="hz-chip">%s</span>' % html.escape(val))

    headline = _hz_str(a.get("headline"), 300)
    nws_hl = _hz_str(a.get("nws_headline"), 300)
    lines = ""
    if headline:
        lines += '      <div class="hz-hl">%s</div>\n' % html.escape(headline)
    if nws_hl and nws_hl.lower() != headline.lower():
        lines += '      <div class="hz-hl">%s</div>\n' % html.escape(nws_hl)

    meta = [_hz_e(a.get("sender"), 80)]
    onset = _hz_iso_ts(a.get("onset"))
    if onset is not None and onset > time.time():
        meta.append("from %s" % html.escape(_hz_local(onset) or "?"))
    until = _hz_until(a)
    if until:
        meta.append("until %s" % html.escape(until))
    meta.append(_hz_e(a.get("area_desc"), 300))

    body = ""
    if a.get("description"):
        body += '<div class="hz-txt">%s</div>' % _hz_e(a["description"], 2000)
    if a.get("instruction"):
        body += ('<div class="hz-txt"><b>Instruction:</b> %s</div>'
                 % _hz_e(a["instruction"], 1000))
    details = ('      <details><summary>Full text</summary>%s</details>\n' % body
               if body else "")
    return ('    <li class="hz-a"><span class="hz-sw" style="background:%s"></span>'
            '<div class="hz-body">\n'
            '      <div class="hz-ev"><b>%s</b>%s</div>\n'
            '%s'
            '      <div class="hz-m">%s</div>\n'
            '%s'
            '    </div></li>\n'
            % (_hz_alert_color(a), _hz_e(ev or "NWS alert", 80),
               "".join(" " + c for c in chips), lines, _hz_join(meta), details))


def _hz_alert_list(alerts, total, at_site, veto_keys, pending_keys, veto_names):
    shown = alerts[:HAZARD_LIST_MAX]
    items = "".join(_hz_alert_li(a, at_site, veto_keys, pending_keys, veto_names)
                    for a in shown)
    more = ""
    if total > len(shown):
        more = ('    <p class="hz-note">+%d more not listed (all are drawn on the map).</p>\n'
                % (total - len(shown)))
    return '    <ul class="hz-list">\n' + items + '    </ul>\n' + more


def _hz_policy_html(hz):
    """Say plainly which alerts can close the observatory — and that nothing else can."""
    events = [e for e in (hz.get("veto_events") or []) if isinstance(e, str) and e.strip()]
    if not events:
        return ('    <p class="hz-note">No veto events are configured: every NWS alert '
                'here is information only and never changes the safety monitor.</p>\n')
    names = ["<b>%s</b>" % _hz_e(e, 60) for e in events]
    listed = (names[0] if len(names) == 1 else
              ", ".join(names[:-1]) + " or " + names[-1])
    return ('    <p class="hz-note">Only a %s in effect <b>over the site</b> makes the safety '
            'monitor UNSAFE, for as long as the warning lasts. Every other alert, and '
            'everything under &ldquo;Other hazard information&rdquo;, is information only '
            'and never changes IsSafe.</p>\n' % listed)


def _hz_stale_line(age):
    when = ("the safety state has no timestamp" if age is None else
            "last safety-daemon update %d min ago" % int(age / 60))
    return ('    <p class="hz-line">%sHazard information stale &mdash; %s; current NWS '
            'alerts and hazards are unknown.</p>\n'
            % (safety_dot_html(False, unknown=True), when))


def _hz_area_fresh(hz):
    """Is the map-area query current? The daemon's own verdict when it publishes one (it
    applies TTU_SAFETY_HAZARD_STALE_SEC, which this page cannot know), else the area
    query's age against the daemon's published threshold, else against the default."""
    if isinstance(hz.get("area_fresh"), bool):
        return hz["area_fresh"]
    limit = hz.get("stale_after_s")
    limit = limit if _hz_num(limit) and limit > 0 else HAZARD_ALERTS_STALE_SEC
    age = hz.get("area_age_s")
    return bool(_hz_num(age) and -5 <= age <= limit)


def _hz_nws_html(hz, stale, age=None):
    veto = _hz_dicts(hz.get("veto"))
    if not hz.get("enabled", True) and not veto:
        err = _hz_str(hz.get("error"), 160)
        return ('    <p class="hz-line">%sNWS alert layer off%s: no alerts are shown and NWS '
                'warnings cannot veto.</p>\n'
                % (safety_dot_html(True, unknown=True),
                   (" (%s)" % html.escape(err)) if err else " (TTU_SAFETY_HAZARDS=0)"))
    at = _hz_dicts(hz.get("at_site"))
    near = _hz_dicts(hz.get("nearby"))
    counts = hz.get("counts") if isinstance(hz.get("counts"), dict) else {}
    n_at = counts.get("at_site") if _hz_num(counts.get("at_site")) else len(at)
    n_near = counts.get("nearby") if _hz_num(counts.get("nearby")) else len(near)
    n_at, n_near = max(int(n_at), len(at)), max(int(n_near), len(near))
    pending = _hz_dicts(hz.get("veto_pending"))
    veto_keys = {v["key"] for v in veto if isinstance(v.get("key"), str)}
    pending_keys = {p["key"] for p in pending if isinstance(p.get("key"), str)} - veto_keys
    veto_names = {_hz_str(e).lower() for e in (hz.get("veto_events") or [])
                  if isinstance(e, str)}
    parts = [_hz_veto_html(veto, stale), _hz_pending_html(pending, stale)]

    if stale:
        # Fail-safe display (the radar/forecast sections' rule): the lists are only as
        # fresh as the daemon writing them. A last-known VETO stays visible above (it
        # errs toward caution); reassuring "no alerts" text must not.
        parts.append(_hz_stale_line(age))
        return "".join(parts) + _hz_policy_html(hz)

    available = bool(hz.get("available"))
    area_fresh = _hz_area_fresh(hz)
    err = _hz_str(hz.get("error"), 120)

    if not at and not near and available and area_fresh:
        parts.append('    <p class="hz-line">%sNo NWS alerts in effect at the site or '
                     'elsewhere on the map.</p>\n' % safety_dot_html(True))
    else:
        # a count is shown only when data backs it: "(0)" from a failed query is a claim
        parts.append('    <h3>NWS alerts at the site%s</h3>\n'
                     % ((" (%d)" % n_at) if (at or available) else ""))
        if at:
            if not available:
                parts.append('    <p class="hz-note">Last known &mdash; the NWS alert '
                             'queries are currently failing.</p>\n')
            parts.append(_hz_alert_list(at, n_at, True, veto_keys, pending_keys, veto_names))
        elif available:
            parts.append('    <p class="hz-line">%sNo NWS alert in effect at the site.</p>\n'
                         % safety_dot_html(True))
        else:
            parts.append('    <p class="hz-line">%sNWS alerts unavailable%s &mdash; whether '
                         'an alert is in effect at the site is unknown. This does not veto '
                         'on its own (the connectivity watchdog covers a total outage).</p>\n'
                         % (safety_dot_html(True, unknown=True),
                            (" (%s)" % html.escape(err)) if err else ""))
        parts.append('    <h3>NWS alerts nearby (on the map)%s</h3>\n'
                     % ((" (%d)" % n_near) if (near or area_fresh) else ""))
        if near:
            if not area_fresh:
                parts.append('    <p class="hz-note">Last known &mdash; the map-area query '
                             'is not current.</p>\n')
            parts.append(_hz_alert_list(near, n_near, False, veto_keys, pending_keys,
                                        veto_names))
        elif area_fresh:
            parts.append('    <p class="hz-line">None elsewhere on the map.</p>\n')
        else:
            parts.append('    <p class="hz-line">%sMap-area query unavailable &mdash; '
                         'alerts near the site are unknown.</p>\n'
                         % safety_dot_html(True, unknown=True))

    parts.append(_hz_policy_html(hz))
    status = ["Source: NWS api.weather.gov active alerts"]
    for k, name in (("point_age_s", "site query"), ("area_age_s", "map-area query")):
        age = _hz_age(hz.get(k))
        status.append("%s %s" % (name, age) if age else "%s: no successful poll" % name)
    if err:
        status.append("last error: %s" % html.escape(err))
    parts.append('    <p class="hz-src">%s</p>\n' % _hz_join(status))
    return "".join(parts)


# ---- information-only feeds (hazard_info) -------------------------------------------
def _hz_first(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _hz_dist(d):
    dist = _hz_first(d, "distance_km", "dist_km")
    if not _hz_num(dist):
        return ""
    brg = _hz_str(_hz_first(d, "bearing", "direction"), 4)
    return "%.0f km%s from the site" % (dist, (" " + brg) if brg else "")


def _hz_quake_text(q):
    mag = _hz_first(q, "mag", "magnitude")
    head = ("M%.1f" % mag) if _hz_num(mag) else "M?"
    place = _hz_str(q.get("place"), 120)
    return _hz_join([html.escape(head + (" " + place if place else "")),
                     html.escape(_hz_dist(q)),
                     _hz_e(_hz_first(q, "time_local", "local", "time"), 40)])


def _hz_fire_text(f):
    acres = _hz_first(f, "acres", "size_acres", "size_ac")
    cont = _hz_first(f, "contained_pct", "percent_contained", "containment_pct")
    return _hz_join([
        _hz_e(_hz_first(f, "name", "incident") or "Wildfire", 80),
        ("%s ac" % ("{:,.0f}".format(acres))) if _hz_num(acres) else "",
        ("%.0f%% contained" % cont) if _hz_num(cont) else "",
        html.escape(_hz_dist(f)),
        _hz_e(_hz_first(f, "county"), 40),
        ("updated %s" % _hz_e(f["updated_local"], 40)) if f.get("updated_local") else ""])


def _hz_lsr_text(r):
    mag = _hz_first(r, "magnitude", "mag")
    unit = _hz_str(r.get("unit"), 12)
    what = _hz_str(_hz_first(r, "type", "typetext", "event") or "Report", 40)
    if _hz_num(mag) and mag:
        what += " %g%s" % (mag, (" " + unit) if unit else "")
    elif isinstance(mag, str) and mag.strip():
        what += " " + _hz_str(mag, 20)
    return _hz_join([
        _hz_e(_hz_first(r, "time_local", "local", "valid"), 40),
        html.escape(what),
        _hz_e(_hz_first(r, "place", "city", "location"), 80),
        html.escape(_hz_dist(r)),
        _hz_e(r.get("remark"), 200)])


def _hz_md_text(m):
    num = _hz_first(m, "num", "number")
    prob = _hz_first(m, "watch_prob", "watch_confidence", "watch_probability")
    return _hz_join([
        _hz_e(("MD #%s" % num) if num is not None else "Mesoscale discussion", 30),
        _hz_e(_hz_first(m, "concerning", "title"), 160),
        ("until %s" % _hz_e(_hz_first(m, "expire_local", "until", "expires_local"), 40))
        if _hz_first(m, "expire_local", "until", "expires_local") else "",
        ("watch probability %s%%" % _hz_e(prob, 6)) if _hz_num(prob) else
        (_hz_e(prob, 40) if prob else "")])


def _hz_smoke_text(s):
    at = _hz_first(s, "at_site", "over_site", "site_in_smoke")
    dens = _hz_str(s.get("density"), 20)
    window = _hz_str(_hz_first(s, "window", "time_window"), 60)
    if at:
        return html.escape("%s smoke over the site%s" % (
            dens or "Analysed", (" (%s)" % window) if window else ""))
    n = _hz_first(s, "count", "on_map_count", "polygons")
    return html.escape("no smoke analysed over the site%s" % (
        ("; %d smoke area%s on the map" % (n, "" if n == 1 else "s"))
        if _hz_num(n) and n else ""))


def _hz_spc_text(spc):
    cat = _hz_str(spc.get("category"), 12)
    label = _hz_str(spc.get("label"), 120)
    if not cat:
        return html.escape(label or "the site is outside every Day-1 risk area")
    if label and cat.lower() not in label.lower():
        return html.escape("%s (%s) at the site" % (label, cat))
    return html.escape("%s at the site" % (label or cat))


def _hz_sw_text(sw):
    kp = _hz_first(sw, "kp", "kp_now")
    kpmax = _hz_first(sw, "kp_max_24h", "kp_max")
    scales = sw.get("scales") if isinstance(sw.get("scales"), dict) else {}
    parts = []
    if _hz_num(kp):
        peak = (" (max %.1f in 24 h)" % kpmax) if _hz_num(kpmax) else ""
        parts.append("Kp %.1f now%s" % (kp, peak))
    sc = []
    for k in ("G", "R", "S"):
        v = _hz_first(scales, k, k.lower())
        if v is None:
            v = _hz_first(sw, k, k.lower())
        if v is not None:
            v = _hz_str(v, 4)
            sc.append(v if v[:1].upper() == k else k + v)
    if sc:
        parts.append("NOAA scales " + " ".join(sc))
    return html.escape(", ".join(parts))


def _hz_unprefix(s, prefixes):
    """'SPC Day 1: Marginal Risk ...' -> 'Marginal Risk ...' under a row already labelled
    'SPC Day 1 outlook' (case-insensitive; the first matching prefix is removed)."""
    for p in prefixes:
        if s[:len(p)].lower() == p.lower():
            return s[len(p):].lstrip()
    return s


def _hz_item(item, compose, prefixes=()):
    """(escaped text, escaped "on map" chip text or '') for one information item: a
    pre-formatted string, or a dict whose 'text' the daemon formatted (else composed from
    its fields). on_map may be a flag, a count (smoke areas) or a list of what is drawn
    (SPC categories — named on the chip, since they need not be the site's category)."""
    if isinstance(item, str):
        return _hz_e(_hz_unprefix(item, prefixes), 400), ""
    if not isinstance(item, dict):
        return "", ""
    txt = _hz_first(item, "text", "summary", "line")
    txt = (_hz_e(_hz_unprefix(txt, prefixes), 400) if isinstance(txt, str)
           else compose(item))
    if not txt and isinstance(item.get("label"), str):
        txt = _hz_e(item["label"], 400)     # a bare short label, as the last resort
    on_map = _hz_first(item, "on_map", "in_box", "in_map")
    if isinstance(on_map, list):
        names = [_hz_str(x, 12) for x in on_map[:6] if isinstance(x, str) and x.strip()]
        chip = ("on map: " + ", ".join(names)) if names else ("on map" if on_map else "")
    else:
        chip = "on map" if on_map else ""
    return txt, html.escape(chip)


def _hz_feed_status(feeds, names):
    """("ok"|"stale"|"error"|"unknown", escaped status text) for the first feed of
    ``names`` present in the component's per-feed status block."""
    st = None
    for n in names:
        if isinstance(feeds.get(n), dict):
            st = feeds[n]
            break
    if st is None:
        return "unknown", "status unknown"
    age = st.get("age_s")
    if not st.get("ok"):
        e = _hz_str(st.get("error"), 80)
        return "error", "unavailable" + ((": " + html.escape(e)) if e else "")
    if not _hz_num(age) or age > HAZARD_INFO_STALE_SEC:
        a = _hz_age(age)
        return "stale", "no recent update" + ((" (%s)" % a) if a else "")
    return "ok", "updated " + (_hz_age(age) or "?")


# (row label, where the items are, feed-status names, source credit, item formatter,
# what an empty row means, text prefixes the row label already says). "where" is a key
# of hazard_info, or "spc.mds". The first feed name is hazard_feeds.py's (FEEDS); the
# others are tolerated spellings, and a feed the page cannot find degrades to "no current
# data" (never to a false "nothing current").
_HZ_INFO_ROWS = (
    ("Smoke", "smoke", ("smoke", "hms", "hms_smoke"), "NOAA HMS", _hz_smoke_text,
     "smoke analysed on the map", ()),
    ("SPC Day 1 outlook", "spc", ("spc_outlook", "spc", "spc_day1", "outlook"),
     "NOAA SPC", _hz_spc_text, "SPC Day 1 risk area at the site",
     ("SPC Day 1 outlook:", "SPC Day 1:", "SPC Day-1 outlook:")),
    ("SPC mesoscale discussions", "spc.mds", ("spc_md", "spc_mds", "mds", "md"),
     "NOAA SPC via IEM", _hz_md_text, "SPC mesoscale discussions on the map", ()),
    ("Storm reports", "lsr", ("lsr", "lsrs", "storm_reports"), "NWS LSR via IEM",
     _hz_lsr_text, "storm reports on the map", ()),
    ("Wildfires", "fires", ("fires", "wfigs", "fire_incidents", "wfigs_incidents"),
     "NIFC WFIGS", _hz_fire_text, "wildfire incidents", ()),
    ("Earthquakes", "quakes", ("quakes", "earthquakes", "usgs"), "USGS", _hz_quake_text,
     "earthquakes", ()),
    ("Space weather", "space_weather", ("space_weather", "swpc", "spaceweather"),
     "NOAA SWPC", _hz_sw_text, "space-weather status", ()),
)


def _hz_total(info, feeds, where, names, n_listed):
    """How many items a list row really has: the daemon's uncapped total (hazard_info
    totals, else the feed's count), never less than what is listed. The daemon caps each
    list at 20 for the state file, so len(list) alone would pass the cap off as the count
    (104 storm reports drawn on the map, "Storm reports (20)" in the text)."""
    totals = info.get("totals") if isinstance(info.get("totals"), dict) else {}
    t = totals.get(where)
    if not _hz_num(t):
        st = feeds.get(names[0]) if isinstance(feeds.get(names[0]), dict) else {}
        t = st.get("count")
    return int(t) if _hz_num(t) and t >= n_listed else n_listed


def _hz_info_html(info):
    if not info.get("enabled", True):
        err = _hz_str(info.get("error"), 160)
        return ('    <p class="hz-line">Other hazard feeds off%s.</p>\n'
                % ((" (%s)" % html.escape(err)) if err
                   else " (TTU_SAFETY_HAZARD_FEEDS=0)")), []
    feeds = info.get("feeds") if isinstance(info.get("feeds"), dict) else {}
    spc = info.get("spc") if isinstance(info.get("spc"), dict) else {}
    rows, quiet, unavailable, on_map_layers = [], [], [], []
    for label, where, names, credit, fmt, empty, prefixes in _HZ_INFO_ROWS:
        if where == "spc.mds":
            raw = spc.get("mds")
        else:
            raw = info.get(where)
        state, status = _hz_feed_status(feeds, names)
        note = ""
        total = None
        if isinstance(raw, dict):
            # one-object feeds: shown when they say something — smoke over the site (now
            # or earlier today) or on the map; the SPC line (the daemon writes it only
            # from a fresh outlook: a risk at the site, or none, or "no current outlook");
            # the space-weather line
            on_map = _hz_first(raw, "on_map", "in_box", "in_map")
            notable = bool(
                where == "space_weather"
                or (where == "spc" and (_hz_str(raw.get("category")) or on_map
                                        or isinstance(raw.get("text"), str)))
                or (where == "smoke" and (_hz_first(raw, "at_site", "over_site",
                                                    "site_in_smoke", "site_in_smoke_today")
                                          or on_map or _hz_first(raw, "count"))))
            items = [_hz_item(raw, fmt, prefixes)] if notable else []
            if notable and isinstance(raw.get("note"), str):
                note = ('<div class="hz-fs" style="margin:2px 0 0">%s</div>'
                        % _hz_e(raw["note"], 300))
        elif isinstance(raw, list):
            items = [_hz_item(x, fmt, prefixes) for x in raw[:20]]
            total = _hz_total(info, feeds, where, names, len(raw))
        else:
            items = []
        items = [(t, m) for t, m in items if t]
        fs = '<span class="hz-fs">%s &middot; %s</span>' % (credit, status) + note
        if items:
            if state != "ok":
                fs = ('<span class="hz-fs">%s &middot; last known, %s</span>'
                      % (credit, status)) + note
            if any(m for _t, m in items):
                on_map_layers.append(label)
            chips = [(' <span class="hz-chip">%s</span>' % m) if m else "" for _t, m in items]
            if len(items) == 1:
                rows.append('      <li><b>%s:</b> %s%s %s</li>\n'
                            % (label, items[0][0], chips[0], fs))
            else:
                sub = "".join('<li>%s%s</li>' % (t, c) for (t, _m), c in zip(items, chips))
                n = max(total or 0, len(items))
                more = ""
                if n > len(items):
                    more = ('<li class="hz-fs">+%d more not listed%s</li>'
                            % (n - len(items), " (all are drawn on the map)"
                               if where == "lsr" else ""))
                rows.append('      <li><b>%s</b> (%d) %s<ul>%s%s</ul></li>\n'
                            % (label, n, fs, sub, more))
        elif state == "ok":
            quiet.append("%s (%s)" % (empty, credit))
        else:
            # failed / stale / unknown feed: say so — never "nothing current"
            unavailable.append("%s (%s, %s)" % (empty, credit, status))
    if quiet:
        rows.append('      <li class="hz-fs" style="margin:0">None current: %s.</li>\n'
                    % ", ".join(quiet))
    if unavailable:
        rows.append('      <li class="hz-fs" style="margin:0">No current data: %s.</li>\n'
                    % "; ".join(unavailable))
    src = ('    <p class="hz-src">Sources: %s</p>\n' % _hz_e(info["source"], 300)
           if isinstance(info.get("source"), str) and info["source"].strip() else "")
    return ('    <ul class="hz-info">\n' + "".join(rows) + '    </ul>\n' + src,
            on_map_layers)


def _hz_feed_details(info):
    """Every information feed's raw status, folded away (for diagnosing a quiet feed)."""
    feeds = info.get("feeds") if isinstance(info.get("feeds"), dict) else {}
    fl = []
    for name, st in feeds.items():
        if not isinstance(st, dict):
            continue
        desc = "ok" if st.get("ok") else "error"
        if not st.get("ok") and st.get("error"):
            desc += ": " + _hz_str(st.get("error"), 120)
        a = _hz_age(st.get("age_s"))
        if a:
            desc += ", " + a
        if _hz_num(st.get("count")):
            desc += ", %d item%s" % (st["count"], "" if st["count"] == 1 else "s")
        fl.append("%s &mdash; %s" % (_hz_e(name, 40), html.escape(desc)))
    if not fl:
        return ""
    return ('    <details><summary>Feed status (%d feeds)</summary>'
            '<div class="hz-txt">%s</div></details>\n' % (len(fl), "\n".join(fl)))


def _hz_map_key(alerts_on_map, info_layers):
    """What the overlays on the radar map mean — the page is the map's legend."""
    # (wording follows what safety/radar.py draws for each overlay kind)
    key = []
    if alerts_on_map:
        key.append("NWS alert areas are shaded and outlined in the colours listed above "
                   "(warnings on top of watches and advisories; a vetoing warning is drawn "
                   "last, with a thicker outline)")
    names = {"Smoke": "smoke: a grey veil, more opaque where denser",
             "SPC Day 1 outlook": ("SPC outlook: dashed outlines in the SPC risk colours "
                                   "(Marginal in sand, never green; general thunder not "
                                   "drawn)"),
             "SPC mesoscale discussions": "mesoscale discussions: purple dashed outline",
             "Wildfires": "wildfires: orange-red triangles, perimeters in orange-red",
             "Earthquakes": "earthquakes: rings sized by magnitude",
             "Storm reports": ("storm reports: &#9660; tornado, &#9679; hail, &#9632; wind, "
                               "&#9670; flood/rain, &#215; dust, + winter, "
                               "&#9675; other")}
    key.extend(names[x] for x in info_layers if x in names)
    if not key:
        return ""
    return ('    <p class="hz-note" style="margin-top:10px">On the radar map: %s.</p>\n'
            % "; ".join(key))


def build_hazards_html(state):
    """The "Hazards" section: the NWS-warning veto banner, NWS alerts at the site and on
    the map (text + colour key for the radar overlays), and the information-only feeds.
    Omitted entirely when the daemon publishes neither hazard component (older daemon)."""
    comp = state.get("components") if isinstance(state, dict) else None
    if not isinstance(comp, dict):
        return ""
    hz = comp.get("hazards") if isinstance(comp.get("hazards"), dict) else None
    info = comp.get("hazard_info") if isinstance(comp.get("hazard_info"), dict) else None
    if hz is None and info is None:
        return ""

    def _off(c):
        # switched off on purpose (no error, nothing held) — a layer that failed to load
        # reports enabled=False WITH an error, and that must stay visible
        return c is None or (not c.get("enabled", True) and not c.get("error")
                             and not c.get("veto"))
    if _off(hz) and _off(info):
        return ""                            # both layers switched off -> like radar off

    ts = state.get("ts")
    age = (time.time() - ts) if _hz_num(ts) else None
    stale = (age is None) or (age > SAFETY_STATE_STALE_SEC)

    parts = ['  <section class="hz">\n',
             '  <h2>Hazards (NWS alerts &amp; other hazard information)</h2>\n']
    alerts_on_map = False
    if hz is not None:
        parts.append(_hz_nws_html(hz, stale, age))
        alerts_on_map = bool(hz.get("enabled", True)
                             and (_hz_dicts(hz.get("at_site")) or _hz_dicts(hz.get("nearby"))))
    if stale and (hz is None or not hz.get("enabled", True)):
        parts.append(_hz_stale_line(age))    # (an enabled NWS part says it itself)
    layers, feed_details = [], ""
    if info is not None and not stale:
        parts.append('  <h3>Other hazard information <span class="hz-chip">information only'
                     '</span></h3>\n')
        body, layers = _hz_info_html(info)
        parts.append(body)
        if info.get("enabled", True):
            feed_details = _hz_feed_details(info)
    if not stale:
        parts.append(_hz_map_key(alerts_on_map, layers))
    parts.append(feed_details)
    parts.append('  </section>\n')
    return "".join(parts)


def build_forecast_html(state):
    nws = ((state or {}).get("components", {}) or {}).get("nws") or {}
    hours = nws.get("hours") or []
    if not hours or not nws.get("available"):
        return ""   # no current forecast -> omit rather than show rejected/old data
    ts = (state or {}).get("ts")
    if ts is None or (time.time() - ts) > SAFETY_STATE_STALE_SEC:
        return ""   # frozen state (daemon down) -> the table would be stale too

    def pct(v):
        return "?" if v is None else "%.0f%%" % v

    def cardinal(deg):
        if deg is None:
            return ""
        return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45 + 0.5) % 8]

    def wind(hr):
        kmh = hr.get("wind_speed_kmh")
        if kmh is None:
            return "?"
        return "%d mph %s" % (round(kmh * 0.621371), cardinal(hr.get("wind_dir_deg")))

    fmt = "%-15s %6s %6s %6s %6s %10s"
    lines = [fmt % ("local", "cloud", "precip", "thndr", "temp", "wind")]
    for hr in hours:
        temp = hr.get("temp_f")
        lines.append(fmt % (
            str(hr.get("local", ""))[:15],
            pct(hr.get("cloud_cover_pct")), pct(hr.get("precip_prob_pct")),
            pct(hr.get("thunder_prob_pct")),
            "?" if temp is None else "%d°F" % temp,
            wind(hr)))
    pre = html.escape("\n".join(lines))
    grid = html.escape(str(nws.get("grid") or "?"))
    upd = html.escape(str(nws.get("update_time") or "?"))
    blurb = (
        "Source: <b>NWS</b> (US National Weather Service) gridpoint forecast for grid %s "
        "via api.weather.gov &mdash; free, no key, model-blended (HRRR near-term / GFS), "
        "updated about hourly (last %s UTC). Cloud cover, precipitation probability and "
        "thunder probability for <b>this hour and next</b> drive the safety monitor's "
        "pre-emptive check; the full table here is for reference." % (grid, upd)
    )
    return (
        '  <h2>12-hour forecast (NWS)</h2>\n'
        '  <div class="grid">\n'
        '    <div>\n'
        '      <pre style="background:var(--codebg);border:1px solid var(--line);'
        'border-radius:8px;padding:10px 12px;overflow-x:auto;font-size:12.5px;margin:0">'
        '%s</pre>\n'
        '    </div>\n'
        '    <div>\n'
        '      <p class="lede" style="font-size:13.5px;margin-top:0">%s</p>\n'
        '    </div>\n'
        '  </div>\n' % (pre, blurb)
    )


def write_html(
    t,
    h,
    now_str,
    chrony,
    gps_data,
    gps_source,
    wifi_data,
    ethernet_data,
    sun_info,
    have_image,
    camera_info,
    ntp_info,
    dht_age=None
):
    source_rows = None
    best_source = None
    camera_ok = None
    night_default = None
    tz_str = None
    now_unix = None
    refresh_seconds = None
    page_class = None
    stratum_str = None
    tracking = None
    footer = None
    parts = None
    page = None

    source_rows = parse_chrony_sources(chrony)
    best_source = find_best_chrony_source(source_rows)

    if camera_info is not None and camera_info.get("mode") == "disabled":
        camera_ok = None            # disabled: not an error, excluded from "all systems"
    else:
        camera_ok = bool(have_image)
        if camera_info is not None and camera_info.get("error") is not None:
            camera_ok = False

    night_default = is_night_default(sun_info)
    if night_default:
        page_class = "page night"
    else:
        page_class = "page"

    tz_str = time.strftime("%Z")
    now_unix = time.time()
    refresh_seconds = get_refresh_seconds()

    stratum_str = ""
    if ntp_info is not None:
        tracking = ntp_info.get("tracking")
        if tracking is not None and tracking.get("Stratum") is not None:
            stratum_str = ", stratum %s" % tracking.get("Stratum")

    footer = "Generated %s · auto-refreshes every %d s · GPS-disciplined NTP server%s" % (
        now_str,
        refresh_seconds,
        stratum_str
    )

    parts = []
    parts.append('<!DOCTYPE html>\n<html lang="en">\n<head>\n')
    parts.append('<meta charset="utf-8">\n')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    parts.append('<meta http-equiv="refresh" content="%d">\n' % refresh_seconds)
    parts.append('<title>Observatory Clock and Safety Monitor</title>\n')
    parts.append('<style>\n')
    parts.append(PAGE_CSS)
    parts.append('</style>\n</head>\n<body>\n')
    parts.append('<section class="%s" id="page">\n<div class="wrap">\n' % page_class)

    parts.append(build_masthead_html(night_default))

    # Section 1: the Alpaca safety monitor (its own card) + the NWS 12-h forecast.
    try:
        _safety_state = read_safety_state()
    except Exception:
        _safety_state = None
    try:
        parts.append(build_safety_html(_safety_state))
    except Exception:
        parts.append('  <h2>Alpaca safety monitor</h2>\n'
                     '  <p class="lede">Safety section unavailable.</p>\n')
    try:
        parts.append(build_radar_html(_safety_state))
    except Exception:
        pass
    try:
        parts.append(build_hazards_html(_safety_state))
    except Exception:
        # Not silently omitted like the purely informational sections: a missing Hazards
        # section would read as "no warnings". The veto itself is still in the reasons
        # list and the NWS-warnings tile of the safety card above.
        _comp = (_safety_state.get("components")
                 if isinstance(_safety_state, dict) else None)
        if isinstance(_comp, dict) and _comp.get("hazards") is not None:
            parts.append('  <h2>Hazards</h2>\n  <p class="lede">Hazards section unavailable '
                         '(render error) &mdash; see the safety monitor card above.</p>\n')
    try:
        parts.append(build_forecast_html(_safety_state))
    except Exception:
        pass

    # Section 2: the observatory clock, sensors, and NTP service.
    parts.append('  <h2 style="margin:0 0 6px">Observatory clock, sensors &amp; NTP</h2>\n')
    parts.append(build_pills_html(best_source, ntp_info, gps_source, camera_ok, wifi_data))
    parts.append(build_lede_html(t, h, sun_info, best_source, camera_ok,
                                 dht_age=dht_age, gps_source=gps_source,
                                 ntp_info=ntp_info))
    parts.append(build_tiles_html(t, h, sun_info, tz_str, dht_age=dht_age))

    parts.append('  <div class="grid">\n')
    parts.append('    <div>\n')
    parts.append(build_twilight_html(sun_info, tz_str, now_unix))
    parts.append('      <h2>Position &amp; network</h2>\n')
    parts.append(build_position_html(gps_data, gps_source, wifi_data, ethernet_data))
    parts.append('      <h2>NTP server</h2>\n')
    parts.append(build_ntp_html(ntp_info, ethernet_data))
    parts.append('    </div>\n')
    parts.append('    <div>\n')
    parts.append('      <h2>Enclosure camera</h2>\n')
    parts.append(build_camera_html(have_image, camera_info))
    parts.append('      <h2>Time sources (chrony)</h2>\n')
    parts.append(build_sources_html(source_rows))
    parts.append('    </div>\n')
    parts.append('  </div>\n')

    parts.append(build_details_html(chrony, ntp_info, camera_info))

    parts.append('  <p class="foot">%s</p>\n' % html.escape(footer))
    parts.append('</div>\n</section>\n')
    parts.append('<script>\n')
    parts.append(PAGE_JS)
    parts.append('</script>\n</body>\n</html>\n')

    page = "".join(parts)

    # Atomic replace: a crash/power cut mid-write must never leave a half page behind
    # (matches every other writer in this file).
    out = _html_output_target()
    tmp_file = out + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(tmp_file, out)


def _html_output_target():
    """Physical write target for the page: /dev/shm + a one-time symlink at HTML_FILE
    (unless disabled), so the every-90-s rewrite never touches the SD card."""
    if not HTML_VIA_SHM:
        return HTML_FILE
    real = os.path.join("/dev/shm", os.path.basename(HTML_FILE))
    try:
        if os.path.islink(HTML_FILE):
            if os.readlink(HTML_FILE) != real:
                os.remove(HTML_FILE)
                os.symlink(real, HTML_FILE)
        else:
            if os.path.exists(HTML_FILE):
                os.remove(HTML_FILE)               # replace the old regular file once
            os.symlink(real, HTML_FILE)
        return real
    except OSError as e:
        print(f"WARNING: cannot set up shm symlink for {HTML_FILE} ({e}); "
              f"writing directly")
        return HTML_FILE


def main():
    cache = {}
    have_image = None
    t = None
    h = None
    now_str = None
    chrony = None
    gps_data = None
    gps_source = None
    wifi_data = None
    ethernet_data = None
    sun_info = None
    camera_info = None
    ntp_info = None

    cache = load_cache()

    gps_data, gps_source = get_gps(cache)
    sun_info = get_sun_info(gps_data)
    t, h, h_age = get_dht(cache)

    # Write the safety inputs BEFORE the camera work: the night stack can take many
    # minutes, and the daemon fails safe (UNSAFE) when these go older than 10 min —
    # exactly during observing hours. Fresh inputs must never wait for the camera.
    try:
        write_safety_inputs(sun_info, h, humidity_age_s=h_age,
                            gps_data=gps_data, gps_source=gps_source)
    except Exception:
        pass

    have_image, camera_info = take_snapshot(sun_info)

    now_str = get_time_string()
    chrony = get_chrony()
    ntp_info = get_ntp_info()
    wifi_data = get_wifi_status(cache)
    ethernet_data = get_ethernet_status()

    save_cache(cache)

    write_html(
        t,
        h,
        now_str,
        chrony,
        gps_data,
        gps_source,
        wifi_data,
        ethernet_data,
        sun_info,
        have_image,
        camera_info,
        ntp_info,
        dht_age=h_age
    )


if __name__ == "__main__":
    main()
