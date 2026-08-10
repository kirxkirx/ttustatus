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

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:                      # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None

log = logging.getLogger("ttu.safety.nws")

BASE = "https://api.weather.gov"
WANTED = {
    "cloud_cover_pct": "skyCover",
    "precip_prob_pct": "probabilityOfPrecipitation",
    "qpf_mm": "quantitativePrecipitation",
    "thunder_prob_pct": "probabilityOfThunder",
    "wind_speed_kmh": "windSpeed",
    "wind_dir_deg": "windDirection",
    "temp_c": "temperature",
}
_SAFETY_VIEW = ("cloud_cover_pct", "precip_prob_pct", "thunder_prob_pct", "qpf_mm")


def _iso(t):
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


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
            out[start + timedelta(hours=k)] = it["value"]
    return out


def _get(url, ua):
    req = urllib.request.Request(
        url, headers={"User-Agent": ua, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def _local(dt, tzname):
    if ZoneInfo:
        try:
            return dt.astimezone(ZoneInfo(tzname))
        except Exception:
            pass
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
            label = f"{pts['gridId']} {pts['gridX']},{pts['gridY']}"
        p = _get(url, ua)["properties"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        return {"ok": False, "error": str(e)}

    now = datetime.now(timezone.utc)
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    maps = {k: _expand(p.get(prop)) for k, prop in WANTED.items()}

    def at(h):
        return {k: maps[k].get(h) for k in WANTED}

    update = p.get("updateTime")
    age_min = (now - _iso(update)).total_seconds() / 60.0 if update else None

    hours = []
    for k in range(cfg.NWS_RENDER_HOURS):
        h = hour0 + timedelta(hours=k)
        row = at(h)
        row["local"] = _local(h, cfg.LOCAL_TZ).strftime("%a %H:%M")
        row["temp_f"] = None if row["temp_c"] is None else round(row["temp_c"] * 9 / 5 + 32)
        hours.append(row)

    return {
        "ok": True, "error": None, "grid": label, "update_time": update,
        "age_min": round(age_min, 1) if age_min is not None else None,
        "now_hour": at(hour0),
        "next_hour": at(hour0 + timedelta(hours=1)),
        "hours": hours,
    }


def evaluate(result, cfg):
    """Pure: decide safe + reasons from a fetch() result. 'available' False if none/stale."""
    if not result or not result.get("ok"):
        return {"available": False, "safe": True, "reasons": [], "stale": True}
    age = result.get("age_min")
    if age is None or age > cfg.NWS_STALE_AFTER_MIN:
        return {"available": False, "safe": True, "reasons": [], "stale": True}
    checks = (
        ("cloud_cover_pct", cfg.NWS_CLOUD_MAX, "cloud cover"),
        ("precip_prob_pct", cfg.NWS_PRECIP_PROB_MAX, "precip probability"),
        ("thunder_prob_pct", cfg.NWS_THUNDER_PROB_MAX, "thunder probability"),
    )
    reasons = []
    for label, hour in (("this hour", result["now_hour"]), ("next hour", result["next_hour"])):
        for key, limit, name in checks:
            v = hour.get(key)
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
        self._last_poll_ts = None

    def poll_now(self, now_ts=None):
        res = fetch(self.cfg)
        with self._lock:
            self._latest = res
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
            due = (self._last_poll_ts is None
                   or (now_ts - self._last_poll_ts) >= self.cfg.NWS_POLL_INTERVAL)
        if due:
            self.poll_now(now_ts)

    def component(self, now=None):
        with self._lock:
            res = self._latest
        ev = evaluate(res, self.cfg)
        comp = {
            "safe": ev["safe"], "available": ev["available"], "stale": ev["stale"],
            "reasons": ev["reasons"],
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
