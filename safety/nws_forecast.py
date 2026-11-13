"""NWS raw-gridpoint forecast component for the SafetyMonitor.

Pulls ONE endpoint (the raw /gridpoints time series), expands the run-length-encoded values
to hourly, and evaluates THIS hour + NEXT hour against thresholds (cloud cover, precip
probability, thunder probability). It's a pre-emptive forecast layer.

Fail-safe policy: a fetch error or a stale forecast makes the component "unavailable" and
does NOT by itself force unsafe (it's a forecast, not a local sensor) — but a threshold
breach in a FRESH forecast DOES make it unsafe. Numeric only; wind/temp are carried for
display / a future upwind-radar ring.
"""
from __future__ import annotations

import http.client
import json
import logging
import math
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .isotime import parse_iso
from .netio import read_capped

try:
    from zoneinfo import ZoneInfo
except Exception:                      # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None

log = logging.getLogger("ttu.safety.nws")

BASE = "https://api.weather.gov"
# The /points reply names the gridpoint URL to fetch; only one under this prefix is
# followed (urllib would otherwise open a file:, data: or ftp: URL just as readily).
GRIDPOINTS_PREFIX = BASE + "/gridpoints/"
# A raw gridpoint forecast is a few hundred kB; anything over this is not one. The body
# must arrive whole within the deadline (urlopen's timeout bounds each wait on the socket,
# not the reply: safety/netio.py).
MAX_REPLY_BYTES = 4 * 1024 * 1024
READ_DEADLINE_S = 60.0
WANTED = {
    "cloud_cover_pct": "skyCover",
    "precip_prob_pct": "probabilityOfPrecipitation",
    "qpf_mm": "quantitativePrecipitation",
    "thunder_prob_pct": "probabilityOfThunder",
    "wind_speed_kmh": "windSpeed",
    "wind_dir_deg": "windDirection",
    "temp_c": "temperature",
    # carried for the weather log only (the dew monitor takes spread and wind from the
    # WU stations): degC, percent, km/h — the grid's own units, as for wind_speed_kmh
    "dewpoint_c": "dewpoint",
    "rh_pct": "relativeHumidity",
    "wind_gust_kmh": "windGust",
}
_SAFETY_VIEW = ("cloud_cover_pct", "precip_prob_pct", "thunder_prob_pct", "qpf_mm")
# The series added for the weather log. The main monitor never reads them, so a malformed
# one must not be able to make the whole download "malformed" (which would take the
# forecast the main monitor DOES read away with it): on any error it is simply absent.
LOG_ONLY = ("dewpoint_c", "rh_pct", "wind_gust_kmh")
# Hours kept (from the hour of the download on) for outlook(): enough to find "this hour
# and the next" by the clock for as long as a download may stay in use.
OUTLOOK_HOURS = 6


def _iso(t):
    """An NWS ISO 8601 time as an aware datetime. Read by safety.isotime.parse_iso, which
    reads the same text alike on every Python (datetime.fromisoformat accepts far more on
    3.11+ than on 3.9). Raises ValueError for anything it cannot read — the caller's
    series (or the whole download, for updateTime) is then unusable, never misread."""
    dt = parse_iso(t)
    if dt is None:
        raise ValueError("unreadable time %r" % (t,))
    return dt


def _num(x):
    """A grid value as a finite float, or None (no value). The grid's values are numbers;
    one sent as a string, a bool, NaN or anything else must not reach a comparison with a
    threshold — a string there raised TypeError on every evaluation (IsSafe HTTP 500)."""
    if x is None or isinstance(x, bool) or not isinstance(x, (int, float, str)):
        return None
    try:
        v = float(x)
    except (ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _dur_hours(dur):
    m = re.match(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$", dur)
    if not m:
        return 1
    d, h, mm = (int(x) if x else 0 for x in m.groups())
    return max(d * 24 + h + (1 if mm else 0), 1)


def _expand(series):
    out = {}
    if not series:
        return out
    for it in series.get("values", []):
        s, dur = it["validTime"].split("/")
        start = _iso(s).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        for k in range(_dur_hours(dur)):
            out[start + timedelta(hours=k)] = _num(it["value"])
    return out


def _expand_log_only(series):
    try:
        return _expand(series)
    except Exception:  # noqa: BLE001 — information only
        return {}


def _get(url, ua):
    req = urllib.request.Request(
        url, headers={"User-Agent": ua, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = read_capped(r, MAX_REPLY_BYTES, READ_DEADLINE_S, what="NWS forecast")
    return json.loads(raw)


_tz_warned = False


def _local(dt, tzname):
    global _tz_warned
    if ZoneInfo:
        try:
            return dt.astimezone(ZoneInfo(tzname))
        except Exception:
            if not _tz_warned:
                _tz_warned = True
                log.warning("TTU_SAFETY_LOCAL_TZ=%r is not a valid timezone — "
                            "forecast table will show UTC", tzname)
    return dt


def fetch(cfg):
    """One-shot fetch -> normalized result dict; {'ok': False, ...} on any error."""
    ua = cfg.NWS_USER_AGENT
    try:
        if cfg.NWS_GRID:
            url, label = f"{BASE}/gridpoints/{cfg.NWS_GRID}", cfg.NWS_GRID
        else:
            pts = _get(f"{BASE}/points/{cfg.GEOCODE[0]},{cfg.GEOCODE[1]}", ua)["properties"]
            url = pts["forecastGridData"]
            if not isinstance(url, str) or not url.startswith(GRIDPOINTS_PREFIX):
                return {"ok": False, "error": "the /points reply names a forecast URL "
                                              "outside %s: %r" % (GRIDPOINTS_PREFIX,
                                                                  str(url)[:120])}
            label = f"{pts['gridId']} {pts['gridX']},{pts['gridY']}"
        p = _get(url, ua)["properties"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError,
            http.client.HTTPException) as e:
        return {"ok": False, "error": str(e)}

    # Post-processing sits in its OWN try: a payload that is valid JSON but the wrong
    # shape (or carries a malformed updateTime) must become a normal ok=False result,
    # not an exception that escapes poll_now — that froze _latest at the last good
    # forecast forever while retrying every 60 s.
    try:
        now = datetime.now(timezone.utc)
        hour0 = now.replace(minute=0, second=0, microsecond=0)
        maps = {k: (_expand_log_only if k in LOG_ONLY else _expand)(p.get(prop))
                for k, prop in WANTED.items()}

        def at(h):
            return {k: maps[k].get(h) for k in WANTED}

        update = p.get("updateTime")
        age_min = (now - _iso(update)).total_seconds() / 60.0 if update else None

        hours = []
        for k in range(cfg.NWS_RENDER_HOURS):
            h = hour0 + timedelta(hours=k)
            row = at(h)
            row["local"] = _local(h, cfg.LOCAL_TZ).strftime("%a %H:%M")
            row["temp_f"] = (None if row["temp_c"] is None
                             else round(row["temp_c"] * 9 / 5 + 32))
            hours.append(row)

        return {
            "ok": True, "error": None, "grid": label, "update_time": update,
            "age_min": round(age_min, 1) if age_min is not None else None,
            "fetched_ts": time.time(),
            "now_hour": at(hour0),
            "next_hour": at(hour0 + timedelta(hours=1)),
            "hours": hours,
            # for outlook(): the hour of the download and the hours from it on
            "hour0_ts": hour0.timestamp(),
            "outlook_hours": [at(hour0 + timedelta(hours=k)) for k in range(OUTLOOK_HOURS)],
        }
    except Exception as e:  # noqa: BLE001 — any malformed payload = unavailable
        return {"ok": False, "error": f"malformed forecast payload: {e!r}"}


def evaluate(result, cfg, now_ts=None):
    """Pure: decide safe + reasons from a fetch() result. 'available' False if none/stale."""
    if now_ts is None:
        now_ts = time.time()
    if not result or not result.get("ok"):
        return {"available": False, "safe": True, "reasons": [], "stale": True}
    # The forecast must RE-AGE: age_min was computed at fetch time and never advanced,
    # so a silenced poller kept presenting an old forecast as available forever.
    fetched = result.get("fetched_ts")
    if fetched is not None and (now_ts - fetched) > 2 * cfg.NWS_POLL_INTERVAL:
        return {"available": False, "safe": True, "reasons": [], "stale": True}
    age = result.get("age_min")
    # negative beyond skew = the forecast is from "the future" = the clock is wrong;
    # data selected under a wrong clock must not be presented as a current forecast
    if age is None or age > cfg.NWS_STALE_AFTER_MIN or age < -2.0:
        return {"available": False, "safe": True, "reasons": [], "stale": True}
    checks = (
        ("cloud_cover_pct", cfg.NWS_CLOUD_MAX, "cloud cover"),
        ("precip_prob_pct", cfg.NWS_PRECIP_PROB_MAX, "precip probability"),
        ("thunder_prob_pct", cfg.NWS_THUNDER_PROB_MAX, "thunder probability"),
    )
    reasons = []
    for label, hour in (("this hour", result["now_hour"]), ("next hour", result["next_hour"])):
        for key, limit, name in checks:
            v = _num(hour.get(key))
            if v is not None and v > limit:
                reasons.append(f"NWS {name} {v:g}% > {limit:g}% ({label})")
    return {"available": True, "safe": not reasons, "reasons": reasons, "stale": False}


def _safety_view(hour):
    return {k: hour.get(k) for k in _SAFETY_VIEW}


class NwsForecastPoller:
    """Background-pollable holder of the latest NWS forecast + its safety verdict."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._latest = None
        self._last_good = None          # the last ok download (see outlook())
        self._last_poll_ts = None

    def poll_now(self, now_ts=None):
        res = fetch(self.cfg)
        with self._lock:
            self._latest = res
            if res.get("ok"):
                self._last_good = res
            self._last_poll_ts = time.time() if now_ts is None else now_ts
        if res.get("ok"):
            log.info("NWS forecast updated (grid %s, age %s min)",
                     res.get("grid"), res.get("age_min"))
        else:
            log.warning("NWS forecast fetch failed: %s", res.get("error"))
        return res

    def maybe_poll(self, now_ts=None):
        if now_ts is None:
            now_ts = time.time()
        with self._lock:
            elapsed = None if self._last_poll_ts is None else now_ts - self._last_poll_ts
            # elapsed < 0: the clock stepped backward past the last poll — poll now
            # rather than stalling for the entire step
            due = (elapsed is None or elapsed < 0
                   or elapsed >= self.cfg.NWS_POLL_INTERVAL)
        if due:
            self.poll_now(now_ts)

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        with self._lock:
            self._last_poll_ts = None       # re-fetch under the corrected clock
            self._last_good = None          # its hours were picked under the wrong clock

    def outlook(self, now=None, max_age_s=None) -> dict:
        """The forecast for the hour that contains `now` and for the hour after it, from
        the last GOOD download — for the dew monitor and the weather log.

        Two differences from component(), whose now_hour / next_hour feed the main
        monitor: the hours are found by the clock (component() keeps the two hours of the
        moment of the download), and one failed poll does not blank it — the last good
        download stays in use until it is max_age_s old (default: two poll intervals).
        Anything else — no download yet, too old, a forecast NWS stopped updating, hours
        it does not cover — is "not available", with the reason in 'why'. Each hour is a
        dict of every WANTED key (None where the grid has no value)."""
        if now is None:
            now = time.time()
        if max_age_s is None:
            max_age_s = 2 * self.cfg.NWS_POLL_INTERVAL
        with self._lock:
            res = self._last_good
        out = {"available": False, "why": None, "age_s": None, "update_time": None,
               "grid": None, "this_hour": None, "next_hour": None}
        if not res:
            out["why"] = "no forecast downloaded yet"
            return out
        fetched, hour0 = res.get("fetched_ts"), res.get("hour0_ts")
        hours = res.get("outlook_hours") or []
        out["update_time"], out["grid"] = res.get("update_time"), res.get("grid")
        if fetched is None or hour0 is None:
            out["why"] = "forecast download has no timestamp"
            return out
        age = now - fetched
        out["age_s"] = round(age)
        if age > max_age_s or age < -120.0:
            out["why"] = "last good forecast download is %d min old" % round(age / 60.0)
            return out
        issued = res.get("age_min")         # the forecast's own age when downloaded
        if issued is None or issued > self.cfg.NWS_STALE_AFTER_MIN or issued < -2.0:
            out["why"] = "forecast is stale (NWS has not updated it)"
            return out
        k = int((now - hour0) // 3600)
        if k < 0 or k + 1 >= len(hours):
            out["why"] = "forecast download does not cover this hour and the next"
            return out
        out.update(available=True, this_hour=dict(hours[k]), next_hour=dict(hours[k + 1]))
        return out

    def component(self, now=None):
        with self._lock:
            res = self._latest
        ev = evaluate(res, self.cfg, now)
        comp = {
            "safe": ev["safe"], "available": ev["available"], "stale": ev["stale"],
            "reasons": ev["reasons"],
            # WHY it is unavailable, so the page can say more than "N/A"
            "error": (res or {}).get("error"),
            "source": "NWS api.weather.gov gridpoint forecast",
            "thresholds": {"cloud_pct": self.cfg.NWS_CLOUD_MAX,
                           "precip_prob_pct": self.cfg.NWS_PRECIP_PROB_MAX,
                           "thunder_prob_pct": self.cfg.NWS_THUNDER_PROB_MAX},
            "grid": None, "update_time": None, "age_min": None,
            "now_hour": None, "next_hour": None, "hours": [],
        }
        if res and res.get("ok"):
            comp.update({
                "grid": res.get("grid"), "update_time": res.get("update_time"),
                "age_min": res.get("age_min"),
                "now_hour": _safety_view(res["now_hour"]),
                "next_hour": _safety_view(res["next_hour"]),
                "hours": res.get("hours", []),
            })
        return comp


def unavailable_component(cfg):
    """The component dict when NWS is disabled/absent (does not affect IsSafe)."""
    return {
        "safe": True, "available": False, "stale": True, "reasons": [],
        "source": "NWS api.weather.gov gridpoint forecast (disabled)",
        "thresholds": {"cloud_pct": cfg.NWS_CLOUD_MAX,
                       "precip_prob_pct": cfg.NWS_PRECIP_PROB_MAX,
                       "thunder_prob_pct": cfg.NWS_THUNDER_PROB_MAX},
        "grid": None, "update_time": None, "age_min": None,
        "now_hour": None, "next_hour": None, "hours": [],
    }
