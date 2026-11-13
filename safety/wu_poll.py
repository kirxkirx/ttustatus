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

import email.utils
import http.client
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from .netio import read_capped

log = logging.getLogger("ttu.safety.wu")

# A WU reply is a few kB (one observation, or the ~10 nearest stations): anything bigger
# than this is not one, and a reply must arrive whole within the deadline — urlopen's
# timeout bounds each wait on the socket, not the reply (safety/netio.py).
MAX_REPLY_BYTES = 256 * 1024
READ_DEADLINE_S = 30.0
# Retry-After of a 429/503 is honoured, but never beyond this: a rain check paused for
# longer than an hour on one header would be a rain check switched off.
RETRY_AFTER_MAX_S = 3600.0
# HTTP statuses that mean "WU (or its CDN) is overloaded: stop asking for this poll".
THROTTLE_STATUSES = (429, 503)
# HTTP statuses that mean "this station is not there": they count toward the dead-station
# backoff like a 204. Every other HTTP error is an API problem (a bad key, a server
# error) that says nothing about the station.
GONE_STATUSES = (404, 410)


class WuError(Exception):
    """A station could not be read. `state` is the poll-result state it becomes."""
    state = "error"


class WuBadReply(WuError):
    """A reply came (HTTP 200) but cannot be used: it counts toward the dead-station
    backoff, like a station that sends nothing."""
    state = "bad-reply"


class WuApiError(WuError):
    """An API or network problem (HTTP 429/5xx or another HTTP error, a timeout, a
    refused or broken connection): it says nothing about the station and NEVER counts
    toward the backoff. `throttled` (HTTP 429 or 503) ends the poll; `retry_after` is
    the server's Retry-After in seconds (bounded), or None."""
    state = "error"

    def __init__(self, msg, status=None, retry_after=None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after

    @property
    def throttled(self) -> bool:
        return self.status in THROTTLE_STATUSES


def _retry_after(headers, now=None):
    """The Retry-After header in seconds (delta-seconds or an HTTP date), bounded to
    [0, RETRY_AFTER_MAX_S]; None when absent or unreadable."""
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:   # noqa: BLE001 - a header object without get()
        return None
    if raw is None:
        return None
    raw = str(raw).strip()
    try:
        v = float(raw) if raw.isdigit() else None
        if v is None:
            dt = email.utils.parsedate_to_datetime(raw)
            if dt is None or dt.tzinfo is None:
                return None
            v = dt.timestamp() - (time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if not math.isfinite(v):
        return None
    return min(max(v, 0.0), RETRY_AFTER_MAX_S)


def _get_json(url: str, timeout: int | None = None):
    """Return parsed JSON, or None for HTTP 204 / empty body.

    Raises WuApiError for an API or network problem and WuBadReply for a reply that is
    not JSON or is too large; the body is read through netio.read_capped (at most
    MAX_REPLY_BYTES, whole within READ_DEADLINE_S)."""
    if timeout is None:
        timeout = config.HTTP_TIMEOUT
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 204:
                return None
            raw = read_capped(resp, MAX_REPLY_BYTES, READ_DEADLINE_S, what="WU")
    except urllib.error.HTTPError as e:
        if e.code in GONE_STATUSES:
            return None                 # no such station: offline, like a 204
        raise WuApiError("HTTP %s" % e.code, status=e.code,
                         retry_after=_retry_after(e.headers)) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # a timeout (ReplyTooSlow included), DNS, a refused, broken or cut-short
        # connection (IncompleteRead, ReplyCutShort, BadStatusLine, LineTooLong)
        raise WuApiError("%s: %s" % (type(e).__name__, str(e)[:160])) from None
    except ValueError as e:             # a 200 reply over MAX_REPLY_BYTES (ReplyTooLarge)
        raise WuBadReply(str(e)[:200]) from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as e:             # not JSON (UnicodeDecodeError is a ValueError)
        raise WuBadReply("reply is not JSON (%s)" % str(e)[:120]) from None


def clean_station_list(entries):
    """The (station_id, distance_km) pairs of a discovery result that can be used, nearest
    first: a station id must be a non-empty string and the distance a finite number. Each
    unusable entry is skipped with a log line."""
    out = []
    for item in entries if isinstance(entries, (list, tuple)) else ():
        sid = item[0] if isinstance(item, (list, tuple)) and len(item) == 2 else None
        dist = _number(item[1]) if sid is not None else None
        if not isinstance(sid, str) or not sid.strip() or dist is None:
            log.warning("WU discovery: skipping an unusable station entry %r", item)
            continue
        out.append((sid.strip(), dist))
    out.sort(key=lambda x: x[1])
    return out


def discover_stations(lat: float, lon: float) -> list[tuple[str, float]]:
    """Return (station_id, distance_km) for the ~10 nearest PWS the WU API knows.
    Raises (WuError, ValueError) when the reply cannot be read at all; unusable single
    entries are skipped (clean_station_list)."""
    url = ("https://api.weather.com/v3/location/near"
           f"?geocode={lat},{lon}&product=pws&format=json"
           f"&apiKey={urllib.parse.quote(config.WU_API_KEY)}")
    data = _get_json(url) or {}
    loc = data.get("location") if isinstance(data, dict) else None
    if not isinstance(loc, dict):
        raise WuBadReply("discovery reply has no 'location' object")
    ids = loc.get("stationId") or []
    dist = loc.get("distanceKm") or []
    if not isinstance(ids, list) or not isinstance(dist, list):
        raise WuBadReply("discovery reply's station list is not a list")
    return clean_station_list(
        [(sid, dist[i] if i < len(dist) else 9999.0) for i, sid in enumerate(ids)])


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
    tidier parsing. Only something float() cannot read, and NaN, are 'no reading': a NaN
    compares False with everything, so it would have made the station "live and dry"."""
    if x is None:
        return None
    try:
        v = float(x)
    except OverflowError:               # an integer too large for a float: still a rate
        return math.inf if x > 0 else -math.inf
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _observe(station_id: str):
    """The station's current observation (see station_observation), or None for a
    station that has none: HTTP 204 (or 404/410), an empty body, an empty observation
    list. Raises WuBadReply for a 200 reply that cannot be used (wrong shape, no rain rate
    or timestamp) and WuApiError for an API or network problem (see _get_json)."""
    url = ("https://api.weather.com/v2/pws/observations/current"
           f"?stationId={urllib.parse.quote(station_id)}&format=json&units=e"
           f"&apiKey={urllib.parse.quote(config.WU_API_KEY)}")
    data = _get_json(url)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise WuBadReply("reply is %s, not an object" % type(data).__name__)
    observations = data.get("observations")
    if observations is None or observations == []:
        return None                     # the station has nothing to report: offline
    if not isinstance(observations, list):
        raise WuBadReply("'observations' is %s, not a list" % type(observations).__name__)
    obs = observations[0]
    if not isinstance(obs, dict):
        raise WuBadReply("observation is %s, not an object" % type(obs).__name__)
    imperial = obs.get("imperial")
    if not isinstance(imperial, dict):
        imperial = {}
    rate = _rate(imperial.get("precipRate"))
    epoch = _number(obs.get("epoch"))
    if rate is None or not epoch:
        raise WuBadReply("observation without a rain rate or a timestamp")
    return {"precip_in_hr": rate, "age_s": time.time() - epoch, "obs_ts": epoch,
            "temp_f": _number(imperial.get("temp")),
            "dewpt_f": _number(imperial.get("dewpt")),
            "humidity_pct": _number(obs.get("humidity")),
            "wind_mph": _number(imperial.get("windSpeed")),
            "gust_mph": _number(imperial.get("windGust"))}


def station_observation(station_id: str):
    """The station's current observation as a dict, or None if there is no usable one.
    Never raises.

    None covers a dead station (204), an empty/garbled reply, any network/HTTP error and
    a reply without a rain rate or a timestamp — the caller treats that station as 'no
    vote' (poll_stations tells these apart: see station_precip). Keys: precip_in_hr,
    age_s, obs_ts (epoch of the observation) and, each None when the station does not
    report it, temp_f, dewpt_f (degrees FAHRENHEIT), humidity_pct, wind_mph, gust_mph
    (MILES PER HOUR)."""
    try:
        return _observe(station_id)
    except Exception:   # noqa: BLE001 - whatever the reply or the network did: no vote
        return None


def station_precip(station_id: str):
    """Return (precipRate_in_hr, age_seconds, observation) for a station, or None when
    the station has no observation (offline). The third item is station_observation()'s
    dict; poll_stations() also accepts a plain (rate, age) pair here.

    Raises WuBadReply for an unusable reply and WuApiError for an API or network problem,
    so that poll_stations can tell a dead station from a WU or network outage."""
    obs = _observe(station_id)
    if obs is None:
        return None
    return obs["precip_in_hr"], obs["age_s"], obs


# The observation fields poll_stations() copies into a live station's result.
OBS_FIELDS = ("obs_ts", "temp_f", "dewpt_f", "humidity_pct", "wind_mph", "gust_mph")

# Result states that count toward the dead-station backoff (monitor.RainPoller): the
# station itself sent nothing usable. "error" (an API or network problem) and "not-asked"
# (the poll stopped at a 429/503 before this station) never do.
BACKOFF_STATES = ("offline", "bad-reply")


def poll_stations(stations, max_age_min: int | None = None) -> dict:
    """Poll every station and summarise. Any FRESH station with rain lands in 'raining'.

    stations: a list of (station_id, distance_km) tuples or bare station-id strings.
    A live station's result also carries OBS_FIELDS (imperial units, None = not reported)
    and its distance_km when that was given.

    One station can never stop the poll: whatever reading it raises is caught and the
    station gets a state — "offline" (no observation), "bad-reply" (a reply that cannot
    be used), "error" (an API or network problem: HTTP 429/5xx, a timeout, a broken
    connection, or anything unexpected). Only HTTP 429 or 503 ends the poll early: WU
    asked to be left alone, so the stations after it are "not-asked", and 'throttle'
    says so ({"status", "retry_after_s"}; retry_after_s None when no usable Retry-After
    came). 'errors' counts the "error" stations.
    """
    if max_age_min is None:
        max_age_min = config.MAX_OBS_AGE_MIN
    live = 0
    errors = 0
    throttle = None
    raining = []
    results = []
    max_rate = 0.0
    for item in stations:
        is_pair = isinstance(item, (tuple, list))
        sid = item[0] if is_pair and len(item) else item
        where = {}
        if is_pair and len(item) > 1 and _number(item[1]) is not None:
            where["distance_km"] = float(item[1])
        if throttle is not None:
            results.append({"station": sid, "state": "not-asked", **where})
            continue
        try:
            res = station_precip(sid)
            if res is None:
                results.append({"station": sid, "state": "offline", **where})
                continue
            rate, age = float(res[0]), float(res[1])
            obs = res[2] if len(res) > 2 and isinstance(res[2], dict) else {}
        except WuError as e:
            entry = {"station": sid, "state": e.state, "error": str(e)[:200], **where}
            results.append(entry)
            if e.state == "error":
                errors += 1
            if isinstance(e, WuApiError) and e.throttled:
                throttle = {"status": e.status, "retry_after_s": e.retry_after}
                log.warning("WU answered HTTP %s at station %s — not asking the remaining "
                            "stations this poll%s", e.status, sid,
                            "" if e.retry_after is None
                            else " (Retry-After %.0f s)" % e.retry_after)
            continue
        except Exception as e:  # noqa: BLE001 - one station must never stop the poll
            errors += 1
            results.append({"station": sid, "state": "error",
                            "error": ("%s: %s" % (type(e).__name__, e))[:200], **where})
            log.warning("WU station %s: unexpected %s: %s", sid, type(e).__name__, e)
            continue
        if not age <= max_age_min * 60:      # (a NaN age is not fresh either)
            results.append({"station": sid, "state": "stale",
                            "age_min": round(age / 60) if math.isfinite(age) else None,
                            **where})
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
            "max_rate": max_rate, "results": results, "errors": errors,
            "throttle": throttle}
