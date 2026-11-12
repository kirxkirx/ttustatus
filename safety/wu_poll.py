"""Weather Underground PWS polling for rain detection (self-contained).

Reuses the proven logic from wu_rain/rain_alert.py: discover the ~10 nearest stations via
the WU location/near API and read each station's current precipRate. No audio, no CLI —
just the functions the SafetyMonitor's rain poller needs. Dead stations (HTTP 204) and
network errors are skipped so one offline station cannot blind the check.

The same reply also carries the station's temperature, dew point, humidity and wind.
They cost no extra API call, so they are kept too: the dew monitor (safety/dew.py) and
the weather log use them. The request asks for units=e, so they arrive in IMPERIAL
units — degrees Fahrenheit and miles per hour — and keep those units, and names that
say so (temp_f, wind_mph), until safety/dew.py converts them.
"""
from __future__ import annotations

import json
import math
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


def _number(x):
    """x as a finite float, or None (WU sends null for a sensor the station lacks).
    A number sent as a string is still a number: a rain rate must not be lost to that."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):   # (an integer too large for a float)
        return None
    return v if math.isfinite(v) else None


def _rate(x):
    """The rain rate, converted exactly as this module always did: float(x) of whatever
    WU sent that is not null. Deliberately NOT limited to finite numbers — an "Infinity"
    or a JSON true has always counted as rain, and a rain vote must never be lost to
    tidier parsing. Only something float() cannot read is 'no reading'."""
    if x is None:
        return None
    try:
        return float(x)
    except OverflowError:               # an integer too large for a float: still a rate
        return math.inf if x > 0 else -math.inf
    except (TypeError, ValueError):
        return None


def station_observation(station_id: str):
    """The station's current observation as a dict, or None if there is no usable one.

    None covers a dead station (204), an empty/garbled reply, any network/HTTP error and
    a reply without a rain rate or a timestamp — the caller treats that station as 'no
    vote'. Keys: precip_in_hr, age_s, obs_ts (epoch of the observation) and, each None
    when the station does not report it, temp_f, dewpt_f (degrees FAHRENHEIT),
    humidity_pct, wind_mph, gust_mph (MILES PER HOUR)."""
    url = ("https://api.weather.com/v2/pws/observations/current"
           f"?stationId={urllib.parse.quote(station_id)}&format=json&units=e"
           f"&apiKey={urllib.parse.quote(config.WU_API_KEY)}")
    try:
        data = _get_json(url)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    obs = (data.get("observations") or [None])[0]
    if not isinstance(obs, dict):
        return None
    imperial = obs.get("imperial")
    if not isinstance(imperial, dict):
        imperial = {}
    rate = _rate(imperial.get("precipRate"))
    epoch = _number(obs.get("epoch"))
    if rate is None or not epoch:
        return None
    return {"precip_in_hr": rate, "age_s": time.time() - epoch, "obs_ts": epoch,
            "temp_f": _number(imperial.get("temp")),
            "dewpt_f": _number(imperial.get("dewpt")),
            "humidity_pct": _number(obs.get("humidity")),
            "wind_mph": _number(imperial.get("windSpeed")),
            "gust_mph": _number(imperial.get("windGust"))}


def station_precip(station_id: str):
    """Return (precipRate_in_hr, age_seconds, observation) for a station, or None if no
    fresh data. The third item is station_observation()'s dict; poll_stations() also
    accepts a plain (rate, age) pair here."""
    obs = station_observation(station_id)
    if obs is None:
        return None
    return obs["precip_in_hr"], obs["age_s"], obs


# The observation fields poll_stations() copies into a live station's result.
OBS_FIELDS = ("obs_ts", "temp_f", "dewpt_f", "humidity_pct", "wind_mph", "gust_mph")


def poll_stations(stations, max_age_min: int | None = None) -> dict:
    """Poll every station and summarise. Any FRESH station with rain lands in 'raining'.

    stations: a list of (station_id, distance_km) tuples or bare station-id strings.
    A live station's result also carries OBS_FIELDS (imperial units, None = not reported)
    and its distance_km when that was given.
    """
    if max_age_min is None:
        max_age_min = config.MAX_OBS_AGE_MIN
    live = 0
    raining = []
    results = []
    max_rate = 0.0
    for item in stations:
        is_pair = isinstance(item, (tuple, list))
        sid = item[0] if is_pair else item
        where = {}
        if is_pair and len(item) > 1 and _number(item[1]) is not None:
            where["distance_km"] = float(item[1])
        res = station_precip(sid)
        if res is None:
            results.append({"station": sid, "state": "offline", **where})
            continue
        rate, age = res[0], res[1]
        obs = res[2] if len(res) > 2 and isinstance(res[2], dict) else {}
        if age > max_age_min * 60:
            results.append({"station": sid, "state": "stale",
                            "age_min": round(age / 60), **where})
            continue
        live += 1
        max_rate = max(max_rate, rate)
        entry = {"station": sid, "state": "live", "precip_in_hr": rate,
                 "age_min": round(age / 60), **where}
        entry.update({k: obs.get(k) for k in OBS_FIELDS if k in obs})
        results.append(entry)
        if rate > config.RAIN_THRESHOLD:
            raining.append({"station": sid, "precip_in_hr": rate})
    return {"live": live, "total": len(stations), "raining": raining,
            "max_rate": max_rate, "results": results}
