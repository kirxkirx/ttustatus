"""Weather Underground PWS polling for rain detection (self-contained).

Reuses the proven logic from wu_rain/rain_alert.py: discover the ~10 nearest stations via
the WU location/near API and read each station's current precipRate. No audio, no CLI —
just the functions the SafetyMonitor's rain poller needs. Dead stations (HTTP 204) and
network errors are skipped so one offline station cannot blind the check.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


def _get_json(url: str, timeout: int | None = None):
    """Return parsed JSON, or None for HTTP 204 / empty body."""
    if timeout is None:
        timeout = config.HTTP_TIMEOUT
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status == 204:
            return None
        raw = resp.read()
    return json.loads(raw) if raw else None


def discover_stations(lat: float, lon: float) -> list[tuple[str, float]]:
    """Return (station_id, distance_km) for the ~10 nearest PWS the WU API knows."""
    url = ("https://api.weather.com/v3/location/near"
           f"?geocode={lat},{lon}&product=pws&format=json"
           f"&apiKey={urllib.parse.quote(config.WU_API_KEY)}")
    data = _get_json(url) or {}
    loc = data.get("location", {})
    ids = loc.get("stationId", []) or []
    dist = loc.get("distanceKm", []) or []
    out = [(sid, dist[i] if i < len(dist) else 9999.0) for i, sid in enumerate(ids)]
    out.sort(key=lambda x: x[1])
    return out


def station_precip(station_id: str):
    """Return (precipRate_in_hr, age_seconds) for a station, or None if no fresh data.

    None covers a dead station (204), an empty/garbled reply, or any network/HTTP error —
    the caller just treats that station as 'no vote'.
    """
    url = ("https://api.weather.com/v2/pws/observations/current"
           f"?stationId={urllib.parse.quote(station_id)}&format=json&units=e"
           f"&apiKey={urllib.parse.quote(config.WU_API_KEY)}")
    try:
        data = _get_json(url)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not data:
        return None
    obs = (data.get("observations") or [None])[0]
    if not obs:
        return None
    rate = obs.get("imperial", {}).get("precipRate")
    epoch = obs.get("epoch")
    age = (time.time() - epoch) if epoch else None
    if rate is None or age is None:
        return None
    return float(rate), age


def poll_stations(stations, max_age_min: int | None = None) -> dict:
    """Poll every station and summarise. Any FRESH station with rain lands in 'raining'.

    stations: a list of (station_id, distance_km) tuples or bare station-id strings.
    """
    if max_age_min is None:
        max_age_min = config.MAX_OBS_AGE_MIN
    live = 0
    raining = []
    results = []
    max_rate = 0.0
    for item in stations:
        sid = item[0] if isinstance(item, (tuple, list)) else item
        res = station_precip(sid)
        if res is None:
            results.append({"station": sid, "state": "offline"})
            continue
        rate, age = res
        if age > max_age_min * 60:
            results.append({"station": sid, "state": "stale",
                            "age_min": round(age / 60)})
            continue
        live += 1
        max_rate = max(max_rate, rate)
        results.append({"station": sid, "state": "live", "precip_in_hr": rate,
                        "age_min": round(age / 60)})
        if rate > config.RAIN_THRESHOLD:
            raining.append({"station": sid, "precip_in_hr": rate})
    return {"live": live, "total": len(stations), "raining": raining,
            "max_rate": max_rate, "results": results}
