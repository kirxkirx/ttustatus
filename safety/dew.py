"""The dew monitor: the verdict behind the SECOND Alpaca SafetyMonitor (device 1).

Why it exists. The dome's own rain sensor is a bare plate under the night sky. On a clear,
calm, humid night dew forms on it, it reads WET, and the dome controller board closes the
roof — while the weather is perfectly fine. The observatory's own dome-control software
(the "dome driver") can be told to ignore its rain sensor, but it needs to know WHEN that
is reasonable, and the weather knowledge is here on the Pi. So this daemon publishes the
answer as a second, entirely standard Alpaca SafetyMonitor that the driver polls like any
other:

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
    6. no GLM lightning within DEW_GLM_KM of the site in the last GLM_COOLOFF_HOURS —
       and the GLM layer must really have been watching for that long

No lower limit on the spread (fog is not treated as dangerous), no waiting period once
every line holds, no memory of earlier dew. Missing or stale data for any line is "false":
every failure hands control back to the rain sensor.

Line 6 is the main monitor's lightning check with a wider ring. Device 0 already goes
UNSAFE for a flash within GLM_TRIGGER_KM (and line 1 then makes this monitor false); with
the rain sensor ignored there is one protection fewer against a storm's rain, hence the
extra distance. A flash counts for the same time as that latch: a storm does not flash
every five minutes, and one quiet poll must not hand the roof back to "it is only dew".
"Missing data is false" here means three things, all read off the GLM layer's component:
    * the latest GOOD poll is at most DEW_GLM_MAX_AGE_MIN old (one failed poll is
      tolerated), a good poll being one that read at least DEW_GLM_MIN_GRANULES of its
      look-back window and is not flagged partial (a hole or a lag in what it read) — a
      granule the GLM layer cannot download is read by a later poll, but "no flash" out
      of a fraction of the data is not "no lightning";
    * good polls have been arriving, never further apart than that, for a whole hold:
      after a daemon start, a clock correction or an outage nobody knows what happened
      in the minutes not watched, so the answer stays false until a hold has been
      watched again (nothing is persisted: no file, no SD write);
    * evidence of a flash is never thrown away, whatever else is wrong with the poll
      that showed it.
Every age in line 6 is measured on the MONOTONIC clock: a wall clock that is stepped
(NTP after a boot with a stale clock) must not shorten a hold or freshen old data.

"Dew-point spread" is the air temperature minus the dew point. The plate radiates to the
sky and runs a few degrees colder than the air, so it collects dew when the spread is
smaller than that cooling — long before the air itself is saturated. Wind keeps the plate
at air temperature, which is why calm air is part of the rule.

UNITS — the one place they are converted. WU reports degrees Fahrenheit and miles per
hour (units=e), NWS degrees Celsius and km/h. The thresholds are a spread in degrees
CELSIUS and a wind speed in MILES PER HOUR. Mind that a temperature DIFFERENCE converts
without the 32-degree offset (5 degF of spread is 2.78 degC, not -15). The lightning ring
is in KILOMETRES, as the GLM layer reports its distances: nothing to convert.
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
            "forecast_max_age_min": cfg.DEW_FORECAST_MAX_AGE_MIN,
            "glm_km": cfg.DEW_GLM_KM,
            "glm_hold_min": _lightning_hold_s(cfg) / 60.0,
            "glm_max_age_min": cfg.DEW_GLM_MAX_AGE_MIN,
            "glm_min_granules": cfg.DEW_GLM_MIN_GRANULES}


def _lightning_hold_s(cfg) -> float:
    """How long a flash inside the ring keeps the answer false, seconds: the cool-off of
    the main monitor's lightning latch."""
    return max(0.0, cfg.GLM_COOLOFF_HOURS * 3600.0)


def _lightning_block(why=None) -> dict:
    """The 'lightning' part of the state block, empty: no current data."""
    return {"available": False, "why": why, "age_s": None, "nearest_km": None,
            "recent_km": None, "recent_age_s": None, "clear_in_s": None,
            "watched_s": None}


def unavailable_component(cfg, why="dew monitor disabled (TTU_SAFETY_DEW=0)") -> dict:
    """The state block when the dew monitor is off or broke: never 'safe'."""
    return {"enabled": False, "is_safe": False, "reasons": [why],
            "checks": {"device0": None, "spread": None, "wind": None, "cloud": None,
                       "precip": None, "lightning": None},
            "spread_c": None, "wind_mph": None, "gust_mph": None, "temp_c": None,
            "dewpoint_c": None, "rh_pct": None, "stations_used": 0, "stations_wind": 0,
            "stations": [], "wind_calm": False,
            "forecast": {"available": False, "why": why, "age_s": None,
                         "cloud_now": None, "cloud_next": None,
                         "precip_now": None, "precip_next": None},
            "lightning": _lightning_block(why),
            "thresholds": thresholds(cfg)}


class DewMonitor:
    """Holds each station's latest observation, the wind dead-band state and what the
    GLM lightning layer last showed, and turns them — with device 0's verdict and the
    forecast — into the device 1 verdict."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._obs: dict = {}            # station id -> its newest observation
        # Wind dead band. Starts "not calm": after a restart the answer is false until
        # the wind has actually been seen at or below the lower limit.
        self._wind_calm = False
        # Lightning: what the GLM layer has shown. Every time below is one of the
        # MONOTONIC clock (as in the connectivity watchdog): a hold or a data age read
        # off the wall clock is shortened, stretched or lost whenever that clock steps.
        # Starts with nothing: false until GLM has been watched for a whole hold.
        self._glm_id = None             # the last poll taken in (time stamp and content)
        self._glm_poll_mono = None      # when the newest GOOD poll was made
        self._glm_nearest = None        # ...its nearest flash, km (None: no flash at all)
        self._glm_watch_mono = None     # since when good polls have come without a gap
        self._glm_seen_mono = None      # when a flash inside the ring was last taken in
        self._glm_seen_km = None        # ...how far it was (None: distance unreadable)
        self._glm_flash_latest = False  # ...and whether it is still the latest GLM shows
        self._glm_why = None            # why the component last passed in was no good poll
        self._glm_idle = False          # ...and whether that is "nothing to poll" (day, off)

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

    # -- lightning (GLM) ---------------------------------------------------------
    def update_lightning(self, glm, now=None, mono=None) -> None:
        """Take in the GLM lightning component of this evaluation
        (GlmLightningPoller.component(), or the 'unavailable' block when that layer is
        off). Passing the same poll again changes nothing, so the monitor simply passes
        the component on every evaluation.

        A GOOD poll is one that can vouch for "no lightning": the layer is on, the poll
        succeeded and is current, it read enough of its look-back window
        (DEW_GLM_MIN_GRANULES, counted in granules_in_window), it is not partial (no hole
        or lag in what it read), its nearest flash has a readable distance and its time
        stamp can be used. Anything less is not lightning data: the last good poll then
        simply ages out (DEW_GLM_MAX_AGE_MIN), which is what makes one failed poll
        harmless.

        A FLASH at or inside the ring counts whatever else is wrong with the poll that
        showed it (too few granules, a time stamp that cannot be used): evidence of
        lightning is never thrown away.

        `now` is the wall clock: the poll's own time stamp is compared with it, once, to
        date the poll. `mono` is the monotonic clock all ages are measured on from then
        on (default: time.monotonic())."""
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        cfg = self.cfg
        ts = nearest = flash_km = None
        good = flash = idle = False
        if not isinstance(glm, dict) or not glm.get("enabled"):
            why, idle = "the GLM lightning layer is off", True
        elif glm.get("available") is not True:
            if not glm.get("polling_active"):
                why, idle = ("GLM is not polled now (daytime, or the sun's altitude is "
                             "unknown)"), True
            elif _finite(glm.get("last_poll_ts")) is None:
                why = "no GLM poll yet"
            else:
                why = "the last GLM poll failed or is too old"
        else:
            ts = _finite(glm.get("last_poll_ts"))
            raw = glm.get("nearest_km")
            nearest = _finite(raw)
            # the granules read of the look-back window (a poll also reads back to where
            # the previous one stopped; older components have only granules_scanned)
            in_window = glm.get("granules_in_window")
            granules = _finite(glm.get("granules_scanned") if in_window is None
                               else in_window) or 0.0
            if raw is not None and (nearest is None or nearest < 0):
                # there WAS a flash, at a distance that cannot be read: nobody can say
                # it was outside the ring
                why, flash, nearest = "the last GLM poll gave an unreadable distance", True, None
            else:
                flash = nearest is not None and nearest <= cfg.DEW_GLM_KM + _EPS
                flash_km = nearest if flash else None
                if ts is None or ts > now + FUTURE_TOLERANCE_SEC:
                    why = "the last GLM poll has no usable time"
                elif granules < cfg.DEW_GLM_MIN_GRANULES:
                    why = ("the last GLM poll read only %d of the %d granules needed"
                           % (granules, cfg.DEW_GLM_MIN_GRANULES))
                elif glm.get("partial"):
                    why = ("the last GLM poll left part of its window unread (%s)"
                           % (glm.get("coverage") or "incomplete"))
                else:
                    why, good = None, True
        with self._lock:
            self._glm_why, self._glm_idle = why, idle
            if not (good or flash):
                return                          # nothing this component can tell
            # The same poll is passed at every evaluation until the next one: it is
            # taken in once. (Recognised by its time stamp AND what it says — two polls
            # must never be mistaken for one because a clock repeated itself.)
            poll_id = (ts, repr(glm.get("nearest_km")), repr(glm.get("granules_scanned")))
            if poll_id == self._glm_id:
                return
            self._glm_id = poll_id
            if flash:
                # Counted from NOW, the moment the flash is first seen here — not from
                # the poll's own time stamp, which a clock step in between falsifies.
                self._glm_seen_mono, self._glm_seen_km = mono, flash_km
                self._glm_flash_latest = True
            if not good:
                return
            # When the poll was made, as far as its time stamp can be trusted: used for
            # the data age only, where dating it too early errs to "older".
            made = mono - max(0.0, now - ts)
            if self._glm_poll_mono is not None and made < self._glm_poll_mono:
                return                          # (dated before the poll already known)
            # The watch — how long good polls have been coming without a gap — must err
            # the other way, so it trusts no time stamp: the gap is measured up to NOW
            # (it cannot have been longer), and a new record starts NOW.
            if (self._glm_poll_mono is None
                    or mono - self._glm_poll_mono > cfg.DEW_GLM_MAX_AGE_MIN * 60.0):
                self._glm_watch_mono = mono     # the first good poll after a gap
            self._glm_poll_mono, self._glm_nearest = made, nearest
            if not flash:
                self._glm_flash_latest = False

    def _lightning(self, mono):
        """(check, state block, reasons) for the lightning line of the rule. The check
        is False while a flash inside the ring counts; None (unknown, which makes the
        verdict false) without current data or before GLM has been watched for a whole
        hold; True otherwise."""
        cfg = self.cfg
        ring, hold = cfg.DEW_GLM_KM, _lightning_hold_s(cfg)
        max_age = cfg.DEW_GLM_MAX_AGE_MIN * 60.0
        with self._lock:
            made, nearest, watch = self._glm_poll_mono, self._glm_nearest, self._glm_watch_mono
            seen, seen_km, latest = self._glm_seen_mono, self._glm_seen_km, self._glm_flash_latest
            why, idle = self._glm_why, self._glm_idle
        age = None if made is None else max(0.0, mono - made)
        current = age is not None and age <= max_age
        watched = (max(0.0, mono - watch) if watch is not None else 0.0) if current else None
        seen_age = None if seen is None else max(0.0, mono - seen)
        # A flash inside the ring counts for the hold time — and, whatever the hold, for
        # as long as it is the latest thing the lightning data shows.
        recent = seen_age is not None and (seen_age < hold or (current and latest))
        short = current and watched < hold
        block = _lightning_block()
        block.update(available=current, age_s=None if age is None else round(age),
                     nearest_km=nearest if current else None,
                     watched_s=None if watched is None else round(watched))
        reasons = []
        if recent:
            left = max(0.0, hold - seen_age)
            block.update(recent_km=seen_km, recent_age_s=round(seen_age),
                         clear_in_s=round(left))
            # (whole minutes left, rounded down like the main monitor's "GLM latch N min
            # left": the two lines stand side by side on the page)
            if left >= 60.0:
                until = "counts for another %d min" % (left // 60)
            elif left > 0:
                until = "counts for less than a minute more"
            else:
                until = "the latest lightning data"
            where = ("%s km away %d min ago is" % (_g(seen_km), seen_age // 60)
                     if seen_km is not None else
                     "at an unreadable distance %d min ago is taken as" % (seen_age // 60))
            reasons.append("GLM lightning %s within %s km (%s)" % (where, _g(ring), until))
        if not current:
            if made is None or idle:
                block["why"] = why or "no GLM lightning data yet"
            else:
                block["why"] = ("no good GLM poll for %d min" % (age // 60)
                                + (" (%s)" % why if why else ""))
            reasons.append("no current lightning data: %s" % block["why"])
        elif short:
            block["why"] = ("GLM lightning watched for only %d of the %s min needed"
                            % (watched // 60, _g(hold / 60.0)))
            reasons.append(block["why"] + " (after a daemon start, a clock correction "
                           "or a gap in the data)")
        check = False if recent else (None if (not current or short) else True)
        return check, block, reasons

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """The system clock stepped: forget what was stored under the old clock (and
        the 'calm' state with it). The next evaluation takes the latest poll's results
        in again; their ages are then measured against the corrected clock, since each
        observation carries the station's own time.

        Lightning: the ages are on the monotonic clock, so a flash that counts keeps
        counting for exactly the time it had left. But what the GLM layer fetched under
        the old clock may be another day's data (it looks granules up by date), so the
        last poll is forgotten, and with it the record of how long lightning has been
        watched: the layer polls again at once, and that poll starts a new record."""
        with self._lock:
            self._obs.clear()
            self._wind_calm = False
            self._glm_poll_mono = self._glm_nearest = self._glm_watch_mono = None

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
    def evaluate(self, device0_safe, outlook, now=None, mono=None) -> dict:
        """The device 1 state block. `device0_safe` is the main monitor's verdict of this
        same evaluation; `outlook` is NwsForecastPoller.outlook() (or None when the
        forecast layer is off). The station and lightning data are what
        update_stations() and update_lightning() last took in. `now` is the wall clock
        (station and forecast times); `mono` the monotonic clock the lightning ages are
        measured on (default: time.monotonic())."""
        cfg = self.cfg
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        summ = self.station_summary(now)
        need = cfg.DEW_MIN_STATIONS
        reasons = []
        checks = {"device0": bool(device0_safe), "spread": None, "wind": None,
                  "cloud": None, "precip": None, "lightning": None}

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

        checks["lightning"], lightning, why = self._lightning(mono)
        reasons.extend(why)

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
                "forecast": fc, "lightning": lightning, "thresholds": thresholds(cfg)}
