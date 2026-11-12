"""The dew monitor: the verdict behind the SECOND Alpaca SafetyMonitor (device 1).

Why it exists. The dome's own rain sensor is a bare plate under the night sky. On a clear,
calm, humid night dew forms on it, it reads WET, and the dome controller board closes the
roof — while the weather is perfectly fine. The dome driver (merlino-dome) can be told to
ignore its rain sensor, but it needs to know WHEN that is reasonable, and the weather
knowledge is here on the Pi. So this daemon publishes the answer as a second, entirely
standard Alpaca SafetyMonitor that the driver polls like any other:

    IsSafe(device 1) = True    "a WET reading of the rain sensor right now is dew —
                                the rain sensor may be ignored"
    IsSafe(device 1) = False   "trust the rain sensor"  (the normal state)

It is NOT an observing-safety verdict and must not be selected in NINA: on a clear, windy
night it reads false although observing is perfectly safe. Read by mistake it errs to the
cautious side, because it can never be true while the main monitor (device 0) is not.

The rule — the owner's, deliberately simple. True only while ALL hold:

    1. device 0 is SAFE (every rain, lightning, radar, forecast and daylight check there);
    2. dew-point spread <= DEW_SPREAD_MAX_C        median of the WU stations, degrees C
    3. wind is calm:    <= DEW_WIND_MAX_MPH        median of the WU stations, mph;
                        stays calm until it is > DEW_WIND_UNSAFE_ABOVE_MPH (dead band)
    4. forecast cloud cover          < DEW_CLOUD_BELOW_PCT   this hour AND the next
    5. forecast precip. probability  < DEW_PRECIP_BELOW_PCT  this hour AND the next

No lower limit on the spread (fog is not treated as dangerous), no delay, no memory of
earlier dew. Missing or stale data for any line is "false": every failure hands control
back to the rain sensor.

"Dew-point spread" is the air temperature minus the dew point. The plate radiates to the
sky and runs a few degrees colder than the air, so it collects dew when the spread is
smaller than that cooling — long before the air itself is saturated. Wind keeps the plate
at air temperature, which is why calm air is part of the rule.

UNITS — the one place they are converted. WU reports degrees Fahrenheit and miles per
hour (units=e), NWS degrees Celsius and km/h. The thresholds are a spread in degrees
CELSIUS and a wind speed in MILES PER HOUR. Mind that a temperature DIFFERENCE converts
without the 32-degree offset (5 degF of spread is 2.78 degC, not -15).
"""
from __future__ import annotations

import math
import statistics
import threading
import time

KMH_PER_MPH = 1.609344          # exact: 1 mile = 1609.344 m

# What a personal weather station may plausibly report (degF, mph, %). A value outside
# its range is dropped, as if the station lacked that sensor: stations do send -99.9,
# -9999 and the like when a sensor fails, and such a reading must not vote for "dew".
TEMP_RANGE_F = (-80.0, 140.0)
DEWPT_RANGE_F = (-100.0, 100.0)
WIND_RANGE_MPH = (0.0, 200.0)
HUMIDITY_RANGE_PCT = (0.0, 100.0)
# A dew point reported this far above the temperature is rounding at saturation (spread
# 0); further above it is a broken sensor, and the station gives no spread at all.
DEWPT_ABOVE_TEMP_TOLERANCE_F = 2.0
# An observation time-stamped further ahead of our clock than this is a clock glitch at
# the station: it is not kept (kept, it would shadow every later, correct observation and
# then count as "fresh" once its time arrived).
FUTURE_TOLERANCE_SEC = 300.0
# The limits are inclusive ("at most 5 degC"): 9.0 degF of spread must not miss them by
# the last bit of a float conversion.
_EPS = 1e-9


def mph_from_kmh(kmh):
    """km/h -> miles per hour (None stays None)."""
    return None if kmh is None else kmh / KMH_PER_MPH


def kmh_from_mph(mph):
    """miles per hour -> km/h (None stays None)."""
    return None if mph is None else mph * KMH_PER_MPH


def c_from_f(deg_f):
    """A TEMPERATURE in degrees Fahrenheit -> degrees Celsius (None stays None)."""
    return None if deg_f is None else (deg_f - 32.0) * 5.0 / 9.0


def delta_c_from_delta_f(delta_f):
    """A temperature DIFFERENCE in Fahrenheit degrees -> Celsius degrees: no offset."""
    return None if delta_f is None else delta_f * 5.0 / 9.0


def _finite(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _plausible(x, limits):
    """x as a float if it is a finite number within limits, else None."""
    x = _finite(x)
    return x if x is not None and limits[0] <= x <= limits[1] else None


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _g(x):
    """A number for a sentence: 5 -> '5', 4.44 -> '4.4'."""
    return "%g" % round(x, 1)


def thresholds(cfg) -> dict:
    return {"spread_max_c": cfg.DEW_SPREAD_MAX_C,
            "wind_max_mph": cfg.DEW_WIND_MAX_MPH,
            "wind_unsafe_above_mph": cfg.DEW_WIND_UNSAFE_ABOVE_MPH,
            "cloud_below_pct": cfg.DEW_CLOUD_BELOW_PCT,
            "precip_below_pct": cfg.DEW_PRECIP_BELOW_PCT,
            "min_stations": cfg.DEW_MIN_STATIONS,
            "obs_max_age_min": cfg.DEW_OBS_MAX_AGE_MIN,
            "forecast_max_age_min": cfg.DEW_FORECAST_MAX_AGE_MIN}


def unavailable_component(cfg, why="dew monitor disabled (TTU_SAFETY_DEW=0)") -> dict:
    """The state block when the dew monitor is off or broke: never 'safe'."""
    return {"enabled": False, "is_safe": False, "reasons": [why],
            "checks": {"device0": None, "spread": None, "wind": None, "cloud": None,
                       "precip": None},
            "spread_c": None, "wind_mph": None, "gust_mph": None, "temp_c": None,
            "dewpoint_c": None, "rh_pct": None, "stations_used": 0, "stations_wind": 0,
            "stations": [], "wind_calm": False,
            "forecast": {"available": False, "why": why, "age_s": None,
                         "cloud_now": None, "cloud_next": None,
                         "precip_now": None, "precip_next": None},
            "thresholds": thresholds(cfg)}


class DewMonitor:
    """Holds each station's latest observation and the wind dead-band state, and turns
    them — with device 0's verdict and the forecast — into the device 1 verdict."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._obs: dict = {}            # station id -> its newest observation
        # Wind dead band. Starts "not calm": after a restart the answer is false until
        # the wind has actually been seen at or below the lower limit.
        self._wind_calm = False

    # -- station observations ---------------------------------------------------
    def update_stations(self, results, now=None) -> None:
        """Take in a WU poll's per-station results (wu_poll.poll_stations()['results']).

        Only live entries that carry an observation time count; per station the NEWEST
        observation is kept. Calling it again with the same results changes nothing, so
        the monitor simply passes the current rain component on every evaluation. A
        station that stops answering is not removed: its last observation just ages out
        (DEW_OBS_MAX_AGE_MIN), which is what makes one failed poll harmless. Implausible
        values are dropped one by one; an observation dated in the future is not kept."""
        if not isinstance(results, (list, tuple)):
            return
        now = time.time() if now is None else now
        with self._lock:
            for r in results:
                if not isinstance(r, dict) or r.get("state") != "live":
                    continue
                sid, ts = r.get("station"), _finite(r.get("obs_ts"))
                if not sid or ts is None or ts > now + FUTURE_TOLERANCE_SEC:
                    continue
                old = self._obs.get(sid)
                if (old is not None and old["obs_ts"] >= ts
                        and old["obs_ts"] <= now + FUTURE_TOLERANCE_SEC):
                    continue
                self._obs[sid] = {
                    "obs_ts": ts,
                    "temp_f": _plausible(r.get("temp_f"), TEMP_RANGE_F),
                    "dewpt_f": _plausible(r.get("dewpt_f"), DEWPT_RANGE_F),
                    "humidity_pct": _plausible(r.get("humidity_pct"), HUMIDITY_RANGE_PCT),
                    "wind_mph": _plausible(r.get("wind_mph"), WIND_RANGE_MPH),
                    "gust_mph": _plausible(r.get("gust_mph"), WIND_RANGE_MPH)}

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """The system clock stepped: forget what was stored under the old clock (and
        the 'calm' state with it). The next evaluation takes the latest poll's results
        in again; their ages are then measured against the corrected clock, since each
        observation carries the station's own time."""
        with self._lock:
            self._obs.clear()
            self._wind_calm = False

    def station_summary(self, now=None) -> dict:
        """Medians over the stations whose latest observation is fresh, in the units of
        the rule: spread and temperatures in degrees C, wind in mph."""
        now = time.time() if now is None else now
        max_age = self.cfg.DEW_OBS_MAX_AGE_MIN * 60.0
        with self._lock:
            # an observation from "the future" (beyond clock jitter) is not trusted
            fresh = {sid: o for sid, o in self._obs.items()
                     if -FUTURE_TOLERANCE_SEC <= (now - o["obs_ts"]) <= max_age}
            for sid in [s for s, o in self._obs.items() if (now - o["obs_ts"]) > 86400.0]:
                del self._obs[sid]          # forget stations silent for a day
        spreads, temps, dews, winds, gusts, hums, used = [], [], [], [], [], [], []
        for sid in sorted(fresh):
            o = fresh[sid]
            if (o["temp_f"] is not None and o["dewpt_f"] is not None
                    and o["temp_f"] - o["dewpt_f"] >= -DEWPT_ABOVE_TEMP_TOLERANCE_F):
                # a DIFFERENCE: 5/9 only. (A dew point reported a shade above the
                # temperature is sensor rounding at saturation: spread 0. Well above it
                # is a broken sensor: no spread from this station.)
                spreads.append(max(0.0, delta_c_from_delta_f(o["temp_f"] - o["dewpt_f"])))
                temps.append(c_from_f(o["temp_f"]))
                dews.append(c_from_f(o["dewpt_f"]))
                used.append(sid)
            winds.append(o["wind_mph"])
            gusts.append(o["gust_mph"])
            hums.append(o["humidity_pct"])
        return {"spread_c": _median(spreads), "temp_c": _median(temps),
                "dewpoint_c": _median(dews), "rh_pct": _median(hums),
                "wind_mph": _median(winds), "gust_mph": _median(gusts),
                "stations_used": len(spreads),
                "stations_wind": len([w for w in winds if w is not None]),
                "stations": used}

    # -- the verdict --------------------------------------------------------------
    def evaluate(self, device0_safe, outlook, now=None) -> dict:
        """The device 1 state block. `device0_safe` is the main monitor's verdict of this
        same evaluation; `outlook` is NwsForecastPoller.outlook() (or None when the
        forecast layer is off)."""
        cfg = self.cfg
        now = time.time() if now is None else now
        summ = self.station_summary(now)
        need = cfg.DEW_MIN_STATIONS
        reasons = []
        checks = {"device0": bool(device0_safe), "spread": None, "wind": None,
                  "cloud": None, "precip": None}

        if not device0_safe:
            reasons.append("the safety monitor (device 0) is UNSAFE")

        spread = summ["spread_c"] if summ["stations_used"] >= need else None
        if spread is None:
            reasons.append("no dew-point spread: %d station(s) with a fresh temperature "
                           "and dew point, %d needed" % (summ["stations_used"], need))
        else:
            checks["spread"] = spread <= cfg.DEW_SPREAD_MAX_C + _EPS
            if not checks["spread"]:
                reasons.append("dew-point spread %s °C is above %s °C (air too dry for dew)"
                               % (_g(spread), _g(cfg.DEW_SPREAD_MAX_C)))

        wind = summ["wind_mph"] if summ["stations_wind"] >= need else None
        with self._lock:
            if wind is None:
                self._wind_calm = False         # must be SEEN calm again
            elif wind <= cfg.DEW_WIND_MAX_MPH + _EPS:
                self._wind_calm = True
            elif wind > cfg.DEW_WIND_UNSAFE_ABOVE_MPH + _EPS:
                self._wind_calm = False
            calm = self._wind_calm              # between the two limits: unchanged
        if wind is None:
            reasons.append("no wind speed: %d station(s) with a fresh wind reading, "
                           "%d needed" % (summ["stations_wind"], need))
        else:
            checks["wind"] = calm
            if not calm and wind > cfg.DEW_WIND_UNSAFE_ABOVE_MPH + _EPS:
                reasons.append("wind %s mph is above %s mph (too windy for dew)"
                               % (_g(wind), _g(cfg.DEW_WIND_UNSAFE_ABOVE_MPH)))
            elif not calm:
                reasons.append("wind %s mph has not dropped to %s mph or below yet"
                               % (_g(wind), _g(cfg.DEW_WIND_MAX_MPH)))

        fc = {"available": False, "why": None, "age_s": None, "cloud_now": None,
              "cloud_next": None, "precip_now": None, "precip_next": None}
        if not isinstance(outlook, dict):
            fc["why"] = "NWS forecast layer is off"
        else:
            fc["available"] = bool(outlook.get("available"))
            fc["why"], fc["age_s"] = outlook.get("why"), outlook.get("age_s")
        if not fc["available"]:
            reasons.append("no current forecast (%s)" % (fc["why"] or "unknown reason"))
        else:
            this_hour = outlook.get("this_hour") or {}
            next_hour = outlook.get("next_hour") or {}
            fc["cloud_now"] = _finite(this_hour.get("cloud_cover_pct"))
            fc["cloud_next"] = _finite(next_hour.get("cloud_cover_pct"))
            fc["precip_now"] = _finite(this_hour.get("precip_prob_pct"))
            fc["precip_next"] = _finite(next_hour.get("precip_prob_pct"))
            for key, name, limit, pair in (
                    ("cloud", "cloud cover", cfg.DEW_CLOUD_BELOW_PCT,
                     (("this hour", fc["cloud_now"]), ("next hour", fc["cloud_next"]))),
                    ("precip", "precipitation probability", cfg.DEW_PRECIP_BELOW_PCT,
                     (("this hour", fc["precip_now"]), ("next hour", fc["precip_next"])))):
                ok = True
                for label, value in pair:
                    if value is None:
                        ok = None if ok is not False else False
                        reasons.append("forecast %s for %s is missing" % (name, label))
                    elif not value < limit:
                        ok = False
                        reasons.append("forecast %s %s%% (%s) is not below %s%%"
                                       % (name, _g(value), label, _g(limit)))
                checks[key] = ok

        is_safe = all(v is True for v in checks.values())
        return {"enabled": True, "is_safe": is_safe,
                "reasons": [] if is_safe else reasons,
                "checks": checks,
                "spread_c": summ["spread_c"], "wind_mph": summ["wind_mph"],
                "gust_mph": summ["gust_mph"], "temp_c": summ["temp_c"],
                "dewpoint_c": summ["dewpoint_c"], "rh_pct": summ["rh_pct"],
                "stations_used": summ["stations_used"],
                "stations_wind": summ["stations_wind"],
                "stations": summ["stations"], "wind_calm": calm,
                "forecast": fc, "thresholds": thresholds(cfg)}
