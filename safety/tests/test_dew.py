"""The dew monitor — Alpaca device 1 ("a WET rain-sensor reading right now is dew").

The rule is the owner's: device 0 SAFE, dew-point spread <= 5 degC and wind <= 8 mph (both
medians of the WU stations; wind stays calm until it is above 9 mph), forecast cloud cover
< 30 % and precipitation probability < 5 % for this hour and the next, and no GLM lightning
within 60 km (for 30 min after the last poll that saw a flash that close). No lower spread
limit, no memory of earlier dew. Missing data = false.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from safety import config, dew, glm_lightning, nws_forecast, wu_poll
from safety.alpaca import create_app
from safety.dew import DewMonitor
from safety.eventlog import EventLog
from safety.monitor import RainPoller, SafetyMonitor
from safety.tests.test_hazard_integration import daemon  # noqa: F401  (a fixture)

NOW = 1_800_000_000.0


def close(a, b, tol=1e-6):
    """abs(a - b) <= tol. (pytest.approx cannot be used in this suite: the page tests
    put a mock 'numpy' into sys.modules, which approx then trips over.)"""
    return a is not None and abs(a - b) <= tol


def station(sid, temp_f=50.0, dewpt_f=45.0, wind=3.0, gust=5.0, hum=80.0, age_s=120,
            now=NOW):
    """A live WU poll result (imperial units, as WU sends them)."""
    return {"station": sid, "state": "live", "precip_in_hr": 0.0,
            "age_min": round(age_s / 60), "obs_ts": now - age_s, "temp_f": temp_f,
            "dewpt_f": dewpt_f, "humidity_pct": hum, "wind_mph": wind, "gust_mph": gust}


def three(**kw):
    return [station(s, **kw) for s in ("S1", "S2", "S3")]


def outlook(cloud=(10, 10), precip=(0, 0), available=True, why=None):
    return {"available": available, "why": why, "age_s": 60,
            "this_hour": {"cloud_cover_pct": cloud[0], "precip_prob_pct": precip[0]},
            "next_hour": {"cloud_cover_pct": cloud[1], "precip_prob_pct": precip[1]}}


def glm(nearest=None, age_s=60, now=NOW, **over):
    """The GLM lightning component as GlmLightningPoller.component() gives it after a
    good poll `age_s` ago whose nearest flash was `nearest` km away (None: no flash)."""
    comp = {"safe": True, "enabled": True, "available": True, "latched": False,
            "latched_until": None, "seconds_remaining": 0, "last_flash_ts": None,
            "in_ring": False, "nearest_km": nearest, "nearest_bearing": None,
            "polling_active": True, "last_poll_ts": now - age_s, "granules_scanned": 14,
            "trigger_km": config.GLM_TRIGGER_KM, "cooloff_hours": config.GLM_COOLOFF_HOURS,
            "source": "test"}
    comp.update(over)
    return comp


def verdict(stations, device0=True, fc="default", mon=None, now=NOW, lightning="default"):
    """One evaluation at `now` (which also serves as the monotonic clock here).

    By default the lightning is no concern: the monitor gets quiet GLM polls five minutes
    apart, from where its lightning history ends (an hour before `now` for a new monitor)
    up to a minute before `now` — a daemon in the middle of a quiet night. Pass a
    component (or None: the layer is off) to hand over exactly that instead."""
    mon = mon or DewMonitor(config)
    mon.update_stations(stations, now)
    if lightning == "default":
        t = getattr(mon, "watched_to", now - 3660.0)
        while t + 300.0 < now - 60.0:
            t += 300.0
            mon.update_lightning(glm(age_s=0, now=t), t, t)
        mon.update_lightning(glm(now=now), now, now)
        mon.watched_to = now - 60.0
    else:
        mon.update_lightning(lightning, now, now)
    return mon.evaluate(device0, outlook() if fc == "default" else fc, now, now)


class Sky:
    """Drives one DewMonitor through time for the lightning tests. `wall` is the wall
    clock and `mono` the monotonic one: they advance together unless a test steps the
    wall clock. The stations and the forecast are always fine, so the verdict hangs on
    the lightning line alone."""

    def __init__(self, watched=True):
        self.mon = DewMonitor(config)
        self.wall, self.mono = NOW, 50_000.0
        self.comp = None                    # what the GLM layer says at the moment
        if watched:                         # an hour of quiet polls: mid-night
            self.wait(60)

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def look(self):
        """One evaluation, now, with whatever the GLM layer says at the moment."""
        self.mon.update_stations(three(now=self.wall), self.wall)
        self.mon.update_lightning(self.comp, self.wall, self.mono)
        return self.mon.evaluate(True, outlook(), self.wall, self.mono)

    def poll(self, nearest=None, after=300.0, **over):
        """GLM polls `after` seconds from now: nearest flash `nearest` km (None: none)."""
        self.advance(after)
        self.comp = glm(nearest, age_s=0, now=self.wall, **over)
        return self.look()

    def fail(self, after=300.0):
        """...or that poll fails."""
        self.advance(after)
        self.comp = glm(available=False, age_s=0, now=self.wall)
        return self.look()

    def wait(self, minutes, polls=True):
        """Let `minutes` pass with a GLM poll every five minutes — quiet ones, or
        (polls=False) failing ones. Returns the verdict at the end."""
        left = round(minutes * 60.0, 3)
        while left >= 300.0:
            self.poll() if polls else self.fail()
            left = round(left - 300.0, 3)
        self.advance(left)
        return self.look()


def lightning_only(v):
    """True if the lightning line is the only one of this verdict that is not passing."""
    return [k for k, ok in v["checks"].items() if ok is not True] == ["lightning"]


# --- units -------------------------------------------------------------------------------
def test_miles_per_hour_are_not_kilometres_per_hour():
    assert close(dew.kmh_from_mph(8.0), 12.874752)
    assert close(dew.mph_from_kmh(12.874752), 8.0)
    assert close(dew.mph_from_kmh(8.0), 4.971, 1e-3)
    assert dew.mph_from_kmh(None) is None and dew.kmh_from_mph(None) is None


def test_a_temperature_difference_converts_without_the_32_degree_offset():
    assert close(dew.c_from_f(41.0), 5.0)                # a temperature
    assert close(dew.delta_c_from_delta_f(9.0), 5.0)     # a difference
    assert close(dew.delta_c_from_delta_f(5.0), 2.778, 1e-3)


def test_the_thresholds_are_the_owners():
    assert config.DEW_SPREAD_MAX_C == 5.0
    assert config.DEW_WIND_MAX_MPH == 8.0 and config.DEW_WIND_UNSAFE_ABOVE_MPH == 9.0
    assert config.DEW_CLOUD_BELOW_PCT == 30.0 and config.DEW_PRECIP_BELOW_PCT == 5.0
    assert config.DEW_DEVICE_NUMBER == 1 and config.DEVICE_NUMBER == 0
    # data freshness: 3 stations, 30 min per observation, 40 min per forecast download
    assert config.DEW_MIN_STATIONS == 3 and config.DEW_OBS_MAX_AGE_MIN == 30.0
    assert config.DEW_FORECAST_MAX_AGE_MIN == 40.0 and config.DEW_ENABLED is True
    # lightning: 60 km (the main monitor's ring is 50), held 30 min, one failed poll allowed
    assert config.DEW_GLM_KM == 60.0 and config.GLM_TRIGGER_KM == 50.0
    assert config.GLM_COOLOFF_HOURS == 0.5 and config.DEW_GLM_MAX_AGE_MIN == 15.0
    assert dew.thresholds(config)["glm_km"] == 60.0
    assert dew.thresholds(config)["glm_hold_min"] == 30.0
    assert dew.thresholds(config)["glm_max_age_min"] == 15.0
    # ...and a poll must have read 10 of its (up to 15) granules to count as data
    assert config.DEW_GLM_MIN_GRANULES == 10 and config.GLM_WINDOW_MIN == 5
    assert dew.thresholds(config)["glm_min_granules"] == 10


# --- the rule ----------------------------------------------------------------------------
def _config_with(**env):
    """The settings a daemon started with this environment would run with (a child
    process: the module reads the environment once, at import)."""
    code = ("import json; from safety import config as c; print(json.dumps({"
            "'enabled': c.DEW_ENABLED, 'calm': c.DEW_WIND_MAX_MPH, "
            "'above': c.DEW_WIND_UNSAFE_ABOVE_MPH, 'spread': c.DEW_SPREAD_MAX_C, "
            "'fc_age': c.DEW_FORECAST_MAX_AGE_MIN, 'log': c.WEATHER_LOG_ENABLED, "
            "'glm_km': c.DEW_GLM_KM, 'glm_age': c.DEW_GLM_MAX_AGE_MIN, "
            "'granules': c.DEW_GLM_MIN_GRANULES, "
            "'warnings': c.CONFIG_WARNINGS}))")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TTU_SAFETY_")}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=dict(clean, **env),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_settings_from_the_environment():
    plain = _config_with()
    assert plain["enabled"] is True and plain["log"] is True
    assert (plain["calm"], plain["above"], plain["spread"], plain["fc_age"]) == (8, 9, 5, 40)
    # a dead band turned upside down is corrected, loudly, not obeyed
    odd = _config_with(TTU_SAFETY_DEW_WIND_MAX_MPH="8", TTU_SAFETY_DEW_WIND_UNSAFE_ABOVE_MPH="6")
    assert (odd["calm"], odd["above"]) == (8, 8)
    assert any("WIND_UNSAFE_ABOVE_MPH" in w for w in odd["warnings"])
    # "off" switches it off; a word it does not know is reported, not read as "on"
    for word in ("0", "off", "OFF", "false", "no", "disabled"):
        assert _config_with(TTU_SAFETY_DEW=word)["enabled"] is False, word
    typo = _config_with(TTU_SAFETY_DEW="of", TTU_SAFETY_WEATHER_LOG="off")
    assert typo["enabled"] is True and typo["log"] is False
    assert any("TTU_SAFETY_DEW='of'" in w for w in typo["warnings"])
    # an old forecast cannot be asked for beyond the hours a download holds
    assert _config_with(TTU_SAFETY_DEW_FORECAST_MAX_AGE_MIN="600")["fc_age"] == 240
    # the lightning ring, how long a good GLM poll counts, how much it must have read
    assert (plain["glm_km"], plain["glm_age"], plain["granules"]) == (60, 15, 10)
    assert plain["warnings"] == []
    wide = _config_with(TTU_SAFETY_DEW_GLM_KM="80")
    assert wide["glm_km"] == 80 and wide["warnings"] == []
    odd = _config_with(TTU_SAFETY_DEW_GLM_KM="sixty", TTU_SAFETY_DEW_GLM_MAX_AGE_MIN="500",
                       TTU_SAFETY_DEW_GLM_MIN_GRANULES="0")
    assert (odd["glm_km"], odd["glm_age"], odd["granules"]) == (60, 120, 1)
    for name in ("TTU_SAFETY_DEW_GLM_KM='sixty'", "TTU_SAFETY_DEW_GLM_MAX_AGE_MIN=500",
                 "TTU_SAFETY_DEW_GLM_MIN_GRANULES=0"):
        assert any(name in w for w in odd["warnings"]), name
    assert _config_with(TTU_SAFETY_DEW_GLM_KM="30000")["glm_km"] == 20000
    # a ring no wider than the main monitor's adds no distance: obeyed, but said
    for km, kept in (("40", 40), ("50", 50), ("-5", 0)):
        narrow = _config_with(TTU_SAFETY_DEW_GLM_KM=km)
        assert narrow["glm_km"] == kept
        assert any("is not wider than the main monitor's" in w for w in narrow["warnings"])
    assert _config_with(TTU_SAFETY_DEW_GLM_KM="50.1")["warnings"] == []
    # lightning data that would go stale between two good polls: corrected, loudly
    for minutes in ("4", "-3", "6.9"):
        short = _config_with(TTU_SAFETY_DEW_GLM_MAX_AGE_MIN=minutes)
        assert short["glm_age"] == 7, minutes               # one poll interval + 2 min
        assert any("shorter than one GLM poll interval" in w for w in short["warnings"])
    assert _config_with(TTU_SAFETY_DEW_GLM_MAX_AGE_MIN="7")["warnings"] == []
    # the defaults follow the GLM layer's own settings: two poll intervals plus five
    # minutes, and two thirds of the granules of a look-back window
    slow = _config_with(TTU_SAFETY_GLM_POLL_INTERVAL="600")
    assert slow["glm_age"] == 25 and slow["warnings"] == []
    assert _config_with(TTU_SAFETY_GLM_WINDOW_MIN="7")["granules"] == 14
    # ...and stay within their limits whatever those are (a default never warns about
    # itself): a two-hour poll interval gives the two hours allowed, raised to "one
    # interval plus two minutes", and only that is reported
    rare = _config_with(TTU_SAFETY_GLM_POLL_INTERVAL="7200")
    assert rare["glm_age"] == 122 and len(rare["warnings"]) == 1
    assert "shorter than one GLM poll interval" in rare["warnings"][0]
    negative = _config_with(TTU_SAFETY_GLM_POLL_INTERVAL="-600")
    assert negative["glm_age"] == 1 and negative["warnings"] == []
    assert _config_with(TTU_SAFETY_GLM_POLL_INTERVAL="-600",
                        TTU_SAFETY_DEW_GLM_MAX_AGE_MIN="-3")["glm_age"] == 1
    assert _config_with(TTU_SAFETY_DEW_GLM_MIN_GRANULES="12")["granules"] == 12


def test_all_conditions_met_means_the_rain_sensor_may_be_ignored():
    v = verdict(three())
    assert v["is_safe"] is True and v["reasons"] == []
    assert v["checks"] == {"device0": True, "spread": True, "wind": True, "cloud": True,
                           "precip": True, "lightning": True}
    assert close(v["spread_c"], 2.778, 1e-3)                    # 50 F - 45 F
    assert v["wind_mph"] == 3.0 and v["stations_used"] == 3
    assert close(v["temp_c"], 10.0) and close(v["dewpoint_c"], 7.222, 1e-3)


def test_never_safe_while_device_0_is_unsafe():
    v = verdict(three(), device0=False)
    assert v["is_safe"] is False and v["checks"]["device0"] is False
    assert any("device 0" in r for r in v["reasons"])
    # ...and nothing else was wrong
    assert [k for k, ok in v["checks"].items() if ok is not True] == ["device0"]


def test_spread_limit_is_five_celsius_inclusive():
    assert verdict(three(temp_f=50.0, dewpt_f=41.0))["is_safe"] is True     # exactly 5.0 C
    v = verdict(three(temp_f=50.0, dewpt_f=40.8))                          # 5.11 C
    assert v["is_safe"] is False and v["checks"]["spread"] is False
    assert any("spread" in r and "5" in r for r in v["reasons"])


def test_spread_is_a_difference_in_celsius_not_a_converted_temperature():
    # 18 F of spread is 10 C: too dry. (Converted like a temperature it would be -7.8 C
    # and pass.)
    v = verdict(three(temp_f=60.0, dewpt_f=42.0))
    assert close(v["spread_c"], 10.0) and v["is_safe"] is False


def test_no_lower_spread_limit_fog_does_not_bring_the_sensor_back():
    v = verdict(three(temp_f=50.0, dewpt_f=50.0))
    assert v["spread_c"] == 0.0 and v["is_safe"] is True
    # a dew point reported a shade above the temperature is saturation, not an error
    assert verdict(three(temp_f=50.0, dewpt_f=50.4))["spread_c"] == 0.0


def test_wind_limit_is_in_miles_per_hour():
    assert verdict(three(wind=8.0))["is_safe"] is True
    # 12 mph is 19 km/h: far too windy. (It would pass a limit mistaken for 12.87 km/h.)
    v = verdict(three(wind=12.0))
    assert v["is_safe"] is False and v["checks"]["wind"] is False
    assert any("12 mph" in r and "9 mph" in r for r in v["reasons"])


def test_wind_dead_band_safe_at_8_or_below_unsafe_above_9():
    mon = DewMonitor(config)
    t = NOW

    def at(wind):
        nonlocal t
        t += 60
        return verdict(three(wind=wind, now=t), mon=mon, now=t)

    first = at(8.5)                 # never seen calm yet: between the limits is NOT calm
    assert first["is_safe"] is False and first["wind_calm"] is False
    assert any("has not dropped to 8 mph" in r for r in first["reasons"])
    assert at(8.0)["is_safe"] is True
    assert at(8.5)["is_safe"] is True       # inside the band: stays calm
    assert at(9.0)["is_safe"] is True       # "unsafe ABOVE 9"
    assert at(9.1)["is_safe"] is False
    assert at(8.5)["is_safe"] is False      # inside the band: stays not calm
    assert at(8.1)["is_safe"] is False
    assert at(8.0)["is_safe"] is True


def test_wind_dead_band_is_forgotten_when_the_wind_data_goes_missing():
    mon = DewMonitor(config)
    assert verdict(three(wind=5.0), mon=mon)["is_safe"] is True
    later = NOW + 3600                      # every observation is now too old
    assert mon.evaluate(True, outlook(), later)["wind_calm"] is False
    fresh = three(wind=8.5, now=later)
    assert verdict(fresh, mon=mon, now=later)["is_safe"] is False


@pytest.mark.parametrize("cloud,precip,ok,failing", [
    ((29, 29), (4, 4), True, None),
    ((30, 10), (0, 0), False, "cloud"),          # "< 30 %", this hour
    ((10, 30), (0, 0), False, "cloud"),          # ...and the next hour
    ((10, 10), (5, 0), False, "precip"),         # "< 5 %", this hour
    ((10, 10), (0, 5), False, "precip"),
    ((10, 45), (0, 20), False, "cloud"),
])
def test_forecast_limits_apply_to_this_hour_and_the_next(cloud, precip, ok, failing):
    v = verdict(three(), fc=outlook(cloud=cloud, precip=precip))
    assert v["is_safe"] is ok
    if failing:
        assert v["checks"][failing] is False
        assert any("is not below" in r for r in v["reasons"])


def test_a_missing_forecast_value_is_not_a_pass():
    v = verdict(three(), fc=outlook(cloud=(10, None)))
    assert v["is_safe"] is False and v["checks"]["cloud"] is None
    assert any("cloud cover for next hour is missing" in r for r in v["reasons"])
    v = verdict(three(), fc=outlook(cloud=(40, None)))
    assert v["checks"]["cloud"] is False        # a breach is a breach, whatever is missing


@pytest.mark.parametrize("fc", [None, outlook(available=False, why="no forecast downloaded yet")])
def test_no_current_forecast_means_trust_the_sensor(fc):
    v = verdict(three(), fc=fc)
    assert v["is_safe"] is False
    assert v["checks"]["cloud"] is None and v["checks"]["precip"] is None
    assert any("no current forecast" in r for r in v["reasons"])


# --- lightning (GLM) -----------------------------------------------------------------------
FLASH_55 = "GLM lightning 55 km away %d min ago is within 60 km (%s)"
WATCH = ("GLM lightning watched for only %d of the 30 min needed (after a daemon start, a "
         "clock correction or a gap in the data)")
NO_DATA = "no current lightning data: %s"
FAILED = "the last GLM poll failed or is too old"


def test_no_lightning_or_distant_lightning_passes():
    v = Sky().look()                                         # no flash anywhere
    assert v["is_safe"] is True and v["checks"]["lightning"] is True
    assert v["lightning"] == {"available": True, "why": None, "age_s": 0,
                              "nearest_km": None, "recent_km": None,
                              "recent_age_s": None, "clear_in_s": None, "watched_s": 3300}
    v = Sky().poll(182.4)
    assert v["is_safe"] is True and v["lightning"]["nearest_km"] == 182.4


def test_the_ring_is_sixty_kilometres_inclusive():
    assert Sky().poll(60.1)["is_safe"] is True
    for km in (60.0, 59.9, 55.0, 0.0):
        v = Sky().poll(km)
        assert v["is_safe"] is False and v["checks"]["lightning"] is False, km
        assert lightning_only(v)
    sky = Sky()
    assert sky.poll(55.0)["reasons"] == [FLASH_55 % (0, "counts for another 30 min")]
    sky.advance(60)
    v = sky.look()
    assert v["reasons"] == [FLASH_55 % (1, "counts for another 29 min")]
    assert v["lightning"]["recent_km"] == 55.0 and v["lightning"]["recent_age_s"] == 60
    assert v["lightning"]["clear_in_s"] == 29 * 60


def test_the_ring_is_in_kilometres_not_miles():
    # 70 km is 43.5 miles: outside a 60 km ring. (It would be inside one mistaken for
    # 60 miles = 96.6 km.)
    assert Sky().poll(70.0)["is_safe"] is True
    assert Sky().poll(96.0)["is_safe"] is True


def test_a_flash_in_the_ring_counts_for_thirty_minutes():
    sky = Sky()
    assert sky.poll(55.0)["is_safe"] is False
    # five-minute polls that see nothing more do not clear it...
    for minutes in (5, 10, 15, 20, 25):
        v = sky.wait(5)
        assert v["is_safe"] is False and v["checks"]["lightning"] is False, minutes
        assert v["lightning"]["nearest_km"] is None          # the latest poll: no flash
        assert v["lightning"]["recent_km"] == 55.0
        assert v["lightning"]["recent_age_s"] == minutes * 60
    # whole minutes left, rounded down like the main monitor's "GLM latch N min left"
    assert v["reasons"] == [FLASH_55 % (25, "counts for another 5 min")]
    assert sky.wait(3.5)["reasons"] == [FLASH_55 % (28, "counts for another 1 min")]
    v = sky.wait(1.4)                                        # 29.9 min after the flash
    assert v["reasons"] == [FLASH_55 % (29, "counts for less than a minute more")]
    assert v["is_safe"] is False
    # ...thirty minutes do, to the second (as the main monitor's latch: now < then + 30 min)
    v = sky.wait(0.1)
    assert v["is_safe"] is True and v["lightning"]["recent_km"] is None


def test_a_later_flash_restarts_the_thirty_minutes():
    sky = Sky()
    assert sky.poll(58.0)["is_safe"] is False
    sky.wait(15)
    v = sky.poll(52.0)                                       # 20 min on: again, nearer
    assert v["is_safe"] is False and v["lightning"]["recent_km"] == 52.0
    v = sky.wait(25)                                         # 45 min after the first
    assert v["is_safe"] is False and v["lightning"]["recent_km"] == 52.0
    assert sky.wait(4.9)["is_safe"] is False
    assert sky.wait(0.1)["is_safe"] is True


def test_a_far_flash_does_not_restart_or_shorten_the_hold():
    sky = Sky()
    assert sky.poll(40.0)["is_safe"] is False
    sky.wait(5)
    v = sky.poll(300.0)                                      # ten minutes on
    assert v["is_safe"] is False and v["lightning"]["nearest_km"] == 300.0
    assert v["lightning"]["recent_km"] == 40.0 and v["lightning"]["clear_in_s"] == 20 * 60
    sky.wait(15)
    assert sky.poll(300.0)["is_safe"] is True                # thirty minutes on


# --- lightning: what counts as data ----------------------------------------------------------
@pytest.mark.parametrize("component,why", [
    (None, "the GLM lightning layer is off"),
    ("unavailable", "the GLM lightning layer is off"),
    ({"enabled": False, "available": True, "last_poll_ts": NOW, "granules_scanned": 14},
     "the GLM lightning layer is off"),
    (glm(available=False, last_poll_ts=None), "no GLM poll yet"),
    (glm(available=False, last_poll_ts=None, polling_active=False),
     "GLM is not polled now (daytime, or the sun's altitude is unknown)"),
    (glm(available=False, polling_active=False),
     "GLM is not polled now (daytime, or the sun's altitude is unknown)"),
    (glm(available=False), FAILED),
    (glm(available="yes"), FAILED),
    (glm(granules_scanned=0), "the last GLM poll read only 0 of the 10 granules needed"),
    (glm(granules_scanned=None), "the last GLM poll read only 0 of the 10 granules needed"),
    (glm(granules_scanned=9), "the last GLM poll read only 9 of the 10 granules needed"),
    (glm(granules_scanned=float("nan")),
     "the last GLM poll read only 0 of the 10 granules needed"),
    (glm(last_poll_ts="soon"), "the last GLM poll has no usable time"),
    (glm(last_poll_ts=NOW + 3600), "the last GLM poll has no usable time"),
])
def test_what_is_not_lightning_data(component, why):
    if component == "unavailable":
        component = glm_lightning.unavailable_component(config)
    # for a monitor that knows nothing else: no data, so the sensor is trusted
    v = verdict(three(), lightning=component)
    assert v["is_safe"] is False and v["checks"]["lightning"] is None
    assert v["reasons"] == [NO_DATA % why]
    assert v["lightning"]["available"] is False and v["lightning"]["why"] == why
    assert v["lightning"]["nearest_km"] is None and lightning_only(v)
    # for one in the middle of a quiet night it is one failed poll: tolerated...
    sky = Sky()
    sky.advance(300)
    sky.comp = component
    assert sky.look()["is_safe"] is True
    sky.advance(601)                                         # ...for fifteen minutes
    v = sky.look()
    assert v["is_safe"] is False and v["checks"]["lightning"] is None
    assert v["lightning"]["nearest_km"] is None


def test_a_good_poll_has_read_two_thirds_of_its_window():
    # GLM writes three granules a minute and the GLM layer skips one it cannot download:
    # "no flash" out of a fraction of the window is not "no lightning"
    assert config.GLM_WINDOW_MIN == 5 and config.DEW_GLM_MIN_GRANULES == 10
    assert Sky().poll(granules_scanned=10)["lightning"]["age_s"] == 0
    sky = Sky()
    v = sky.poll(granules_scanned=9)                         # tolerated like a failed poll
    assert v["is_safe"] is True and v["lightning"]["age_s"] == 300
    for _ in range(2):
        v = sky.poll(granules_scanned=1)
    assert v["is_safe"] is True and v["lightning"]["age_s"] == 900
    sky.advance(1)
    v = sky.look()
    assert v["is_safe"] is False and v["reasons"] == [NO_DATA % (
        "no good GLM poll for 15 min (the last GLM poll read only 1 of the 10 granules "
        "needed)")]


def test_without_any_lightning_data_ever_the_answer_is_false():
    mon = DewMonitor(config)
    mon.update_stations(three(), NOW)
    v = mon.evaluate(True, outlook(), NOW, NOW)               # update_lightning never called
    assert v["is_safe"] is False and v["checks"]["lightning"] is None
    assert v["reasons"] == [NO_DATA % "no GLM lightning data yet"]


def test_one_failed_glm_poll_does_not_flip_the_answer_but_old_data_does():
    sky = Sky()
    assert sky.poll(182.4)["is_safe"] is True
    for seconds in (312, 624, 900):               # the good poll is this old by now
        v = sky.fail(after=seconds - sky.look()["lightning"]["age_s"])
        assert v["is_safe"] is True and v["lightning"]["age_s"] == seconds, seconds
        assert v["lightning"]["nearest_km"] == 182.4          # still that poll's answer
    v = sky.fail(after=1)
    assert v["is_safe"] is False and v["checks"]["lightning"] is None
    assert v["reasons"] == [NO_DATA % ("no good GLM poll for 15 min (%s)" % FAILED)]
    assert v["lightning"]["nearest_km"] is None   # too old to be shown as the present
    assert v["lightning"]["available"] is False and v["lightning"]["watched_s"] is None


def test_how_long_a_good_glm_poll_counts_is_its_own_setting(monkeypatch):
    monkeypatch.setattr(config, "DEW_GLM_MAX_AGE_MIN", 6.0)
    monkeypatch.setattr(config, "GLM_STALE_AFTER_SEC", 7200)      # (not this one)
    sky = Sky()
    assert sky.fail(after=360)["is_safe"] is True
    assert sky.fail(after=6)["is_safe"] is False
    assert dew.thresholds(config)["glm_max_age_min"] == 6.0


def test_by_day_the_reason_says_that_glm_is_not_polled():
    # after a night of polling the layer simply stops at dawn: not a failure
    sky = Sky()
    sky.advance(8 * 3600)
    sky.comp = glm(available=False, polling_active=False, age_s=8 * 3600, now=sky.wall)
    assert sky.look()["reasons"] == [NO_DATA % (
        "GLM is not polled now (daytime, or the sun's altitude is unknown)")]


# --- lightning: the watch --------------------------------------------------------------------
def test_lightning_must_have_been_watched_for_a_whole_hold():
    # A daemon that has just started does not know what the last half hour was like:
    # the first quiet poll is not enough.
    sky = Sky(watched=False)
    v = sky.poll()
    assert v["is_safe"] is False and v["checks"]["lightning"] is None
    assert v["reasons"] == [WATCH % 0] and lightning_only(v)
    assert v["lightning"]["available"] is True and v["lightning"]["watched_s"] == 0
    assert v["lightning"]["why"] == "GLM lightning watched for only 0 of the 30 min needed"
    v = sky.wait(25)
    assert v["is_safe"] is False and v["reasons"] == [WATCH % 25]
    assert sky.wait(4.9)["is_safe"] is False
    v = sky.wait(0.1)                             # thirty minutes after the first poll
    assert v["is_safe"] is True and v["lightning"]["watched_s"] == 1800


def test_a_gap_in_the_lightning_data_starts_the_watch_again():
    sky = Sky()
    assert sky.wait(15, polls=False)["is_safe"] is True       # three failed polls: 15 min
    sky.advance(1)
    assert sky.look()["checks"]["lightning"] is None          # ...and now it is a gap
    v = sky.poll()                                # GLM is back, and quiet
    assert v["is_safe"] is False and v["reasons"] == [WATCH % 0]
    assert sky.wait(29.9)["is_safe"] is False
    assert sky.wait(0.1)["is_safe"] is True
    # one failed poll (two would be 15.6 min at the real cadence) is not a gap
    sky = Sky()
    sky.fail()
    v = sky.poll()
    assert v["is_safe"] is True and v["lightning"]["watched_s"] == 3300 + 600
    # the limit itself: good polls fifteen minutes apart are still one watch, a second
    # more is a gap
    for seconds, one_watch in ((900, True), (901, False)):
        sky = Sky()
        assert sky.poll(after=seconds)["is_safe"] is one_watch, seconds


def test_the_watch_trusts_no_time_stamp():
    # A good poll is taken in 1000 s after the last one: a gap. Its time stamp says it
    # was made 301 s before that (the wall clock jumped forward in between, so it looks
    # older than it is) — which must not shrink the gap to "699 s, no gap at all".
    sky = Sky()
    sky.advance(1000)
    sky.comp = glm(age_s=301, now=sky.wall)
    v = sky.look()
    assert v["checks"]["lightning"] is None and v["reasons"] == [WATCH % 0]
    assert v["lightning"]["age_s"] == 301         # (for the data age it does count: older)
    # ...and the new watch starts when the poll was SEEN, not when it claims to be from
    assert sky.wait(29.9)["checks"]["lightning"] is None
    assert sky.wait(0.1)["is_safe"] is True


def test_a_poll_stamped_a_little_ahead_of_the_clock_is_dated_now_not_later():
    # within the tolerance for clock jitter it is good data — as old as its intake, and
    # stale fifteen minutes after that (not fifteen minutes after its own time stamp)
    sky = Sky()
    sky.advance(300)
    sky.comp = glm(age_s=-200, now=sky.wall)
    assert sky.look()["lightning"]["age_s"] == 0
    sky.advance(900)
    sky.comp = glm(available=False, now=sky.wall)
    assert sky.look()["is_safe"] is True
    sky.advance(1)
    assert sky.look()["checks"]["lightning"] is None


def test_a_flash_seen_while_the_watch_is_young_names_both():
    sky = Sky(watched=False)
    v = sky.poll(55.0)
    assert v["checks"]["lightning"] is False
    assert v["reasons"] == [FLASH_55 % (0, "counts for another 30 min"), WATCH % 0]


# --- lightning: evidence of a flash is never thrown away ---------------------------------------
def test_missing_lightning_data_never_clears_a_flash_that_still_counts():
    sky = Sky()
    assert sky.poll(48.0)["is_safe"] is False
    v = sky.wait(10, polls=False)
    assert v["checks"]["lightning"] is False                 # FALSE (a flash), not "no data"
    assert v["reasons"] == ["GLM lightning 48 km away 10 min ago is within 60 km "
                            "(counts for another 20 min)"]
    v = sky.wait(10, polls=False)                 # the data is too old by now: both said
    assert v["checks"]["lightning"] is False
    assert v["reasons"] == ["GLM lightning 48 km away 20 min ago is within 60 km "
                            "(counts for another 10 min)",
                            NO_DATA % ("no good GLM poll for 20 min (%s)" % FAILED)]
    sky.advance(9 * 60)
    sky.comp = glm_lightning.unavailable_component(config)
    v = sky.look()
    assert v["checks"]["lightning"] is False and "48 km away 29 min ago" in v["reasons"][0]
    sky.advance(2 * 60)                           # the flash no longer counts, but now...
    v = sky.look()
    assert v["checks"]["lightning"] is None       # ...there is no current data
    assert v["is_safe"] is False
    assert v["reasons"] == [NO_DATA % "the GLM lightning layer is off"]


def test_a_flash_counts_even_from_a_poll_that_is_not_good_data():
    # too few granules: no proof of "no lightning" — but what it did see, it saw
    sky = Sky()
    v = sky.poll(55.0, granules_scanned=1)
    assert v["checks"]["lightning"] is False and v["lightning"]["recent_km"] == 55.0
    assert v["lightning"]["age_s"] == 300         # (the data is still the earlier poll's)
    assert sky.wait(29.9)["checks"]["lightning"] is False
    # a time stamp that cannot be used (the wall clock went back under it)
    sky = Sky()
    sky.advance(300)
    sky.comp = glm(55.0, age_s=-390, now=sky.wall)
    v = sky.look()
    assert v["checks"]["lightning"] is False and v["lightning"]["recent_km"] == 55.0
    sky.advance(1790)
    assert sky.look()["checks"]["lightning"] is False


@pytest.mark.parametrize("distance", ["near", float("nan"), float("inf"), -1.0, True, [55]])
def test_a_flash_at_an_unreadable_distance_is_taken_as_inside_the_ring(distance):
    sky = Sky()
    v = sky.poll(distance)
    assert v["is_safe"] is False and v["checks"]["lightning"] is False
    assert v["lightning"]["recent_km"] is None and v["lightning"]["recent_age_s"] == 0
    assert v["reasons"] == ["GLM lightning at an unreadable distance 0 min ago is taken "
                            "as within 60 km (counts for another 30 min)"]
    json.dumps(v)
    assert sky.wait(30)["is_safe"] is True


def test_the_same_poll_passed_again_changes_nothing():
    sky = Sky()
    assert sky.poll(57.0)["is_safe"] is False
    for seconds in (30, 60, 290):                 # every evaluation passes it again
        sky.advance(seconds - sky.look()["lightning"]["recent_age_s"])
        v = sky.look()
        assert v["lightning"]["recent_age_s"] == seconds and v["lightning"]["age_s"] == seconds
    assert sky.poll(after=10)["lightning"]["age_s"] == 0
    # a poll dated BEFORE the newest one (it cannot happen, but) does not replace it...
    sky.comp = glm(300.0, age_s=600, now=sky.wall)
    v = sky.look()
    assert v["lightning"]["age_s"] == 0 and v["lightning"]["nearest_km"] is None
    assert v["lightning"]["recent_km"] == 57.0 and v["lightning"]["recent_age_s"] == 300
    # ...but a flash it shows is a flash
    sky.comp = glm(10.0, age_s=700, now=sky.wall)
    v = sky.look()
    assert v["lightning"]["recent_km"] == 10.0 and v["lightning"]["recent_age_s"] == 0
    assert v["lightning"]["nearest_km"] is None


def test_two_polls_are_never_mistaken_for_one_because_a_clock_repeated_itself():
    # the same time stamp twice (the wall clock stepped back by exactly one poll
    # interval): the second poll is still a new one, and its flash counts
    sky = Sky()
    stamp = sky.wall + 300
    sky.advance(300)
    sky.comp = glm(None, last_poll_ts=stamp)
    assert sky.look()["is_safe"] is True
    sky.advance(310)
    sky.wall -= 310
    sky.comp = glm(55.0, last_poll_ts=stamp)
    v = sky.look()
    assert v["checks"]["lightning"] is False and v["lightning"]["recent_km"] == 55.0
    sky.advance(30)
    assert sky.look()["lightning"]["recent_age_s"] == 30      # (...and is taken in once)
    # a poll that cannot be dated at all is taken in once, too
    sky = Sky()
    sky.advance(300)
    sky.comp = glm(55.0, last_poll_ts=None)
    assert sky.look()["checks"]["lightning"] is False
    sky.advance(60)
    assert sky.look()["lightning"]["recent_age_s"] == 60


@pytest.mark.parametrize("cooloff_hours", [0.0, -1.0])
def test_with_no_hold_a_flash_counts_while_it_is_the_latest_lightning_data(monkeypatch,
                                                                           cooloff_hours):
    monkeypatch.setattr(config, "GLM_COOLOFF_HOURS", cooloff_hours)
    assert dew.thresholds(config)["glm_hold_min"] == 0.0
    sky = Sky(watched=False)                      # (and no hold: nothing to watch through)
    assert sky.poll()["is_safe"] is True
    v = sky.poll(30.0)
    assert v["is_safe"] is False and v["lightning"]["clear_in_s"] == 0
    assert v["reasons"] == ["GLM lightning 30 km away 0 min ago is within 60 km "
                            "(the latest lightning data)"]
    sky.advance(200)                              # still the latest poll
    assert sky.look()["is_safe"] is False
    assert sky.poll(30.0, granules_scanned=2)["is_safe"] is False     # seen again, barely
    assert sky.poll(granules_scanned=2)["is_safe"] is False   # too little to say "gone"
    assert sky.poll()["is_safe"] is True          # a good poll, clear


def test_a_wider_or_narrower_ring_from_the_settings(monkeypatch):
    monkeypatch.setattr(config, "DEW_GLM_KM", 100.0)
    assert Sky().poll(99.0)["is_safe"] is False
    assert Sky().poll(101.0)["is_safe"] is True
    monkeypatch.setattr(config, "DEW_GLM_KM", 0.0)        # no ring: data still needed
    assert Sky().poll(0.4)["is_safe"] is True
    assert verdict(three(), lightning=None)["is_safe"] is False


# --- lightning: the wall clock may do what it likes ---------------------------------------------
@pytest.mark.parametrize("step_s", [-86400.0, -7200.0, -600.0, 600.0, 7200.0, 86400.0])
def test_a_stepped_wall_clock_neither_shortens_nor_stretches_the_hold(step_s):
    sky = Sky()
    assert sky.poll(50.5)["is_safe"] is False
    sky.wait(10)
    sky.wall += step_s                            # the clock jumps; nobody has noticed yet
    v = sky.look()                                # an Alpaca request in that gap
    assert v["checks"]["lightning"] is False and v["lightning"]["recent_age_s"] == 600
    v = sky.poll(after=10)                        # GLM polls in the gap too: quiet
    assert v["checks"]["lightning"] is False
    sky.advance(10)
    sky.mon.clock_stepped(sky.wall - step_s, sky.wall)        # now the evaluator notices
    v = sky.look()
    assert v["checks"]["lightning"] is False
    assert v["lightning"]["clear_in_s"] == 1800 - 620         # what it had left, no more
    sky.advance(1800 - 620 - 1)
    assert sky.look()["checks"]["lightning"] is False         # 29 min 59 s after the flash
    sky.advance(1)
    assert sky.look()["lightning"]["recent_km"] is None       # 30 min: it no longer counts


def test_a_clock_that_jumps_away_and_back_unnoticed_does_not_lose_the_flash():
    # back two hours and forward again within one evaluator interval: no step is ever
    # reported, but an Alpaca request was answered in between
    sky = Sky()
    assert sky.poll(55.0)["is_safe"] is False
    sky.advance(5)
    sky.wall -= 7200
    assert sky.look()["checks"]["lightning"] is False
    sky.advance(3)
    sky.wall += 7200
    for _ in range(5):
        v = sky.wait(5)
        assert v["checks"]["lightning"] is False, v["lightning"]
    assert sky.wait(4.8)["checks"]["lightning"] is False      # 29 min 56 s
    assert sky.wait(0.1)["is_safe"] is True


def test_a_wall_clock_stepped_back_does_not_make_old_data_current_again():
    sky = Sky()
    assert sky.wait(28, polls=False)["checks"]["lightning"] is None       # 28 min of failures
    sky.wall -= 1500                              # ...and now the clock says "3 min ago"
    v = sky.look()
    assert v["checks"]["lightning"] is None and v["lightning"]["age_s"] == 28 * 60
    sky.comp = glm(age_s=1680, now=sky.wall + 1500)           # the old poll, passed again
    assert sky.look()["checks"]["lightning"] is None


def test_clock_step_drops_the_poll_and_the_watch_starts_again():
    # what GLM fetched under the old clock may be another day's data
    sky = Sky()
    assert sky.poll(300.0)["is_safe"] is True
    sky.mon.clock_stepped(sky.wall, sky.wall + 86400)
    sky.wall += 86400
    v = sky.look()                                # the old poll is passed once more
    assert v["checks"]["lightning"] is None and v["lightning"]["age_s"] is None
    assert v["reasons"] == [NO_DATA % "no GLM lightning data yet"]
    v = sky.poll(after=30)                        # the layer polls again at once
    assert v["checks"]["lightning"] is None and v["reasons"] == [WATCH % 0]
    assert sky.wait(29.9)["is_safe"] is False
    assert sky.wait(0.1)["is_safe"] is True


# --- station data ------------------------------------------------------------------------
def test_needs_three_stations_with_temperature_and_dew_point():
    two = [station("S1"), station("S2")]
    v = verdict(two)
    assert v["is_safe"] is False and v["checks"]["spread"] is None
    assert any("2 station(s)" in r and "3 needed" in r for r in v["reasons"])
    # a station without a hygrometer does not count for the spread...
    v = verdict(two + [station("S3", dewpt_f=None)])
    assert v["checks"]["spread"] is None and v["checks"]["wind"] is True
    # ...and one without an anemometer does not count for the wind
    v = verdict(two + [station("S3", wind=None)])
    assert v["checks"]["spread"] is True and v["checks"]["wind"] is None
    assert v["is_safe"] is False


def test_median_ignores_one_odd_station():
    rows = three() + [station("S4", temp_f=90.0, dewpt_f=30.0, wind=40.0)]
    v = verdict(rows + [station("S5")])
    assert v["is_safe"] is True and v["stations_used"] == 5 and v["wind_mph"] == 3.0


def test_only_live_results_with_an_observation_time_count():
    rows = [{"station": "S1", "state": "offline"},
            {"station": "S2", "state": "stale", "age_min": 55},
            {"station": "S3", "state": "backed-off"},
            {"station": "S4", "state": "live", "precip_in_hr": 0.0, "age_min": 1}]
    v = verdict(rows)
    assert v["stations_used"] == 0 and v["is_safe"] is False
    assert DewMonitor(config).update_stations(None) is None       # tolerated


def test_one_failed_poll_does_not_flip_the_answer_but_old_data_does():
    mon = DewMonitor(config)
    assert verdict(three(), mon=mon)["is_safe"] is True
    failed = [{"station": s, "state": "offline"} for s in ("S1", "S2", "S3")]
    ten_min = NOW + 600
    assert verdict(failed, mon=mon, now=ten_min)["is_safe"] is True
    # observations were 2 min old at NOW: 30 min after that they no longer count
    assert verdict(failed, mon=mon, now=NOW + 27 * 60)["is_safe"] is True
    v = verdict(failed, mon=mon, now=NOW + 29 * 60)
    assert v["is_safe"] is False and v["stations_used"] == 0


def test_newest_observation_per_station_wins_and_an_older_one_is_ignored():
    mon = DewMonitor(config)
    mon.update_stations(three(wind=2.0, age_s=60), NOW)
    mon.update_stations(three(wind=20.0, age_s=600), NOW)   # an OLDER reading arrives late
    assert mon.evaluate(True, outlook(), NOW)["wind_mph"] == 2.0
    mon.update_stations(three(wind=20.0, age_s=10), NOW)
    assert mon.evaluate(True, outlook(), NOW)["wind_mph"] == 20.0


def test_an_observation_dated_in_the_future_is_never_used_and_shadows_nothing():
    # A station clock glitch: one reply stamped two hours ahead (calm, humid). It must
    # not be kept — kept, it hid every later correct reading and then counted as fresh
    # when its time came, two hours after it was sent.
    mon = DewMonitor(config)
    glitch = three(wind=1.0, temp_f=50.0, dewpt_f=49.0, age_s=-7200)
    assert verdict(glitch, mon=mon)["stations_used"] == 0
    for minutes in (10, 60, 119, 121, 140):
        t = NOW + minutes * 60
        v = verdict(three(wind=25.0, temp_f=80.0, dewpt_f=40.0, now=t), mon=mon, now=t)
        assert v["is_safe"] is False and v["wind_mph"] == 25.0, minutes
    # a few minutes of clock difference are tolerated
    ahead = three(age_s=-200)
    assert verdict(ahead)["is_safe"] is True


def test_a_broken_sensor_does_not_vote_for_dew():
    # stations send -99.9 / -9999 for a failed sensor; none of it may read as "humid, calm"
    for bad in (dict(temp_f=-9999.0), dict(dewpt_f=-999.0), dict(temp_f=50.0, dewpt_f=85.0),
                dict(wind=-1.0), dict(wind=-0.5), dict(wind=-9999.0), dict(wind=900.0)):
        v = verdict([station("S1", **bad), station("S2", **bad), station("S3")])
        assert v["is_safe"] is False, bad
        assert v["stations_used"] == 1 or v["stations_wind"] == 1, bad
    # with enough healthy stations the broken one is simply left out
    v = verdict(three() + [station("S4", temp_f=-9999.0, wind=-9999.0)])
    assert v["is_safe"] is True and v["stations_used"] == 3 and v["stations_wind"] == 3


def test_limits_are_inclusive_despite_float_conversion():
    # 68.9 F - 59.9 F is 9.000000000000007 F = 5.0000000000000036 C: still "at most 5"
    v = verdict(three(temp_f=68.9, dewpt_f=59.9))
    assert v["spread_c"] > 5.0 and v["is_safe"] is True
    assert verdict(three(temp_f=68.9, dewpt_f=59.8))["is_safe"] is False
    # ...but the allowance is the last bit of a float, not a real margin
    assert verdict(three(temp_f=50.0, dewpt_f=40.98))["is_safe"] is False    # 5.011 C
    assert verdict(three(wind=8.0))["is_safe"] is True
    assert verdict(three(wind=8.01))["is_safe"] is False
    mon = DewMonitor(config)
    assert verdict(three(wind=5.0), mon=mon)["is_safe"] is True
    assert verdict(three(wind=9.01, age_s=60), mon=mon)["is_safe"] is False


def test_a_dew_point_well_above_the_temperature_is_a_fault_not_saturation():
    assert verdict(three(temp_f=50.0, dewpt_f=52.0))["spread_c"] == 0.0     # rounding
    v = verdict(three(temp_f=50.0, dewpt_f=52.5))                          # a broken sensor
    assert v["stations_used"] == 0 and v["is_safe"] is False


def test_a_stored_observation_that_is_now_in_the_future_gives_way():
    # (the clock stepped back and clock_stepped() was missed: do not let it shadow)
    mon = DewMonitor(config)
    mon.update_stations(three(wind=1.0, age_s=60), NOW)
    earlier = NOW - 7200
    mon.update_stations(three(wind=20.0, age_s=60, now=earlier), earlier)
    assert mon.evaluate(True, outlook(), earlier)["wind_mph"] == 20.0


def test_results_that_are_not_live_are_never_used_even_if_they_carry_values():
    stale = [dict(station(s), state="stale") for s in ("S1", "S2", "S3")]
    assert verdict(stale)["stations_used"] == 0


def test_a_boolean_is_not_a_forecast_value():
    v = verdict(three(), fc=outlook(cloud=(True, 10)))
    assert v["is_safe"] is False and v["checks"]["cloud"] is None


def test_an_hour_without_the_value_at_all_is_missing_too():
    for hole in ({}, None, {"cloud_cover_pct": 10}, {"precip_prob_pct": 0}):
        fc = outlook()
        fc["next_hour"] = hole
        v = verdict(three(), fc=fc)
        assert v["is_safe"] is False, hole
        assert None in (v["checks"]["cloud"], v["checks"]["precip"]), hole


def test_clock_step_drops_the_observations():
    mon = DewMonitor(config)
    assert verdict(three(), mon=mon)["is_safe"] is True
    mon.clock_stepped(NOW, NOW + 86400)
    v = mon.evaluate(True, outlook(), NOW)
    assert v["is_safe"] is False and v["stations_used"] == 0 and v["wind_calm"] is False


def test_disabled_block_is_never_safe():
    block = dew.unavailable_component(config)
    assert block["enabled"] is False and block["is_safe"] is False and block["reasons"]
    assert set(block["checks"]) == set(verdict(three())["checks"])      # same shape...
    assert set(block) == set(verdict(three()))
    assert all(ok is None for ok in block["checks"].values())           # ...nothing known
    assert block["lightning"]["available"] is False


# --- WU: the extra observation fields ------------------------------------------------------
WU_REPLY = {"observations": [{
    "stationID": "KTXLUBBO1", "epoch": None, "humidity": 74, "winddir": 180,
    "imperial": {"temp": 50, "dewpt": 42, "windSpeed": 3, "windGust": 5,
                 "precipRate": 0.0, "precipTotal": 0.0}}]}


def _wu_reply(**imperial):
    reply = json.loads(json.dumps(WU_REPLY))
    reply["observations"][0]["epoch"] = time.time() - 90
    reply["observations"][0]["imperial"].update(imperial)
    return reply


def test_station_observation_keeps_temperature_dew_point_and_wind(monkeypatch):
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: _wu_reply())
    sent = _wu_reply()
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: sent)
    obs = wu_poll.station_observation("KTXLUBBO1")
    assert obs["precip_in_hr"] == 0.0 and 80 < obs["age_s"] < 100
    # the time of the OBSERVATION (the station's own), not of this poll
    assert obs["obs_ts"] == sent["observations"][0]["epoch"]
    assert (obs["temp_f"], obs["dewpt_f"], obs["humidity_pct"]) == (50.0, 42.0, 74.0)
    assert (obs["wind_mph"], obs["gust_mph"]) == (3.0, 5.0)
    rate, age, same = wu_poll.station_precip("KTXLUBBO1")
    assert rate == 0.0 and same["temp_f"] == 50.0


def test_station_without_some_sensors_still_votes_on_rain(monkeypatch):
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: _wu_reply(
        dewpt=None, windSpeed=None, windGust=None, precipRate="0.02"))
    obs = wu_poll.station_observation("X")
    assert obs["precip_in_hr"] == 0.02            # a rate sent as text is still a rate
    assert obs["dewpt_f"] is None and obs["wind_mph"] is None
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: _wu_reply(
        precipRate=None))
    assert wu_poll.station_observation("X") is None
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: None)   # HTTP 204
    assert wu_poll.station_observation("X") is None


def test_an_odd_rain_rate_still_counts_as_rain(monkeypatch):
    # whatever float() can read is a rate, exactly as before this module also kept the
    # temperature: "Infinity" and a JSON true have always meant rain
    for odd in (float("inf"), "inf", True, "0.5", 2):
        monkeypatch.setattr(wu_poll, "_get_json",
                            lambda url, timeout=None, odd=odd: _wu_reply(precipRate=odd))
        res = wu_poll.poll_stations(["X"])
        assert res["live"] == 1 and len(res["raining"]) == 1, odd
    monkeypatch.setattr(wu_poll, "_get_json",
                        lambda url, timeout=None: _wu_reply(precipRate="abc"))
    res = wu_poll.poll_stations(["X", "Y"])          # unreadable: that station only
    assert res["live"] == 0 and [r["state"] for r in res["results"]] == ["offline"] * 2


def test_an_absurd_number_from_one_station_does_not_abort_the_whole_rain_poll(monkeypatch):
    huge = 10 ** 400                                 # an integer no float can hold

    def reply(url, timeout=None):
        if "stationId=WET" in url:
            return _wu_reply(precipRate=0.4)
        return _wu_reply(dewpt=huge, windSpeed=huge)
    monkeypatch.setattr(wu_poll, "_get_json", reply)
    obs = wu_poll.station_observation("ODD")
    assert obs["precip_in_hr"] == 0.0 and obs["dewpt_f"] is None and obs["wind_mph"] is None
    res = wu_poll.poll_stations(["WET", "ODD"])
    assert res["live"] == 2 and [r["station"] for r in res["raining"]] == ["WET"]
    monkeypatch.setattr(wu_poll, "_get_json",
                        lambda url, timeout=None: _wu_reply(precipRate=huge))
    assert len(wu_poll.poll_stations(["X"])["raining"]) == 1     # absurd, but it is rain


def test_poll_results_carry_the_observation_and_the_distance(monkeypatch):
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: _wu_reply())
    res = wu_poll.poll_stations([("A", 3.5), "B"])
    a, b = res["results"]
    assert a["state"] == "live" and a["distance_km"] == 3.5 and "distance_km" not in b
    assert a["temp_f"] == 50.0 and a["wind_mph"] == 3.0 and a["obs_ts"] > 0
    assert res["live"] == 2 and res["raining"] == []


# --- NWS: the outlook by clock hour ----------------------------------------------------------
def _hour(k):
    return {"cloud_cover_pct": 10 * k, "precip_prob_pct": k, "thunder_prob_pct": 0,
            "qpf_mm": 0, "wind_speed_kmh": 10, "wind_dir_deg": 180, "temp_c": 12,
            "dewpoint_c": 8, "rh_pct": 77, "wind_gust_kmh": 20}


def _poller(monkeypatch, fetched=NOW, hour0=NOW - NOW % 3600, ok=True, age_min=20.0):
    result = {"ok": ok, "error": None if ok else "boom", "grid": "LUB/46,41",
              "update_time": "2027-01-15T07:00:00+00:00", "age_min": age_min,
              "fetched_ts": fetched, "now_hour": _hour(0), "next_hour": _hour(1),
              "hours": [], "hour0_ts": hour0,
              "outlook_hours": [_hour(k) for k in range(nws_forecast.OUTLOOK_HOURS)]}
    box = {"result": result}
    monkeypatch.setattr(nws_forecast, "fetch", lambda cfg: box["result"])
    poller = nws_forecast.NwsForecastPoller(config)
    poller.poll_now(fetched)
    return poller, box


def test_outlook_picks_this_hour_and_the_next_by_the_clock(monkeypatch):
    hour0 = NOW - NOW % 3600
    poller, _ = _poller(monkeypatch, fetched=hour0 + 3300, hour0=hour0)   # fetched at :55
    out = poller.outlook(hour0 + 3400, 2400)
    assert out["available"] and out["this_hour"]["cloud_cover_pct"] == 0
    assert out["next_hour"]["cloud_cover_pct"] == 10
    out = poller.outlook(hour0 + 3700, 2400)          # five minutes into the next hour
    assert out["this_hour"]["cloud_cover_pct"] == 10
    assert out["next_hour"]["cloud_cover_pct"] == 20 and out["age_s"] == 400


def test_outlook_survives_one_failed_poll_but_not_a_long_outage(monkeypatch):
    poller, box = _poller(monkeypatch)
    box["result"] = {"ok": False, "error": "HTTP 503"}
    poller.poll_now(NOW + 900)
    assert poller.component(NOW + 900)["available"] is False      # device 0's view
    assert poller.outlook(NOW + 900, 2400)["available"] is True   # device 1's view
    late = poller.outlook(NOW + 2500, 2400)
    assert late["available"] is False and "42 min old" in late["why"]


def test_outlook_unavailable_cases(monkeypatch):
    fresh = nws_forecast.NwsForecastPoller(config)
    assert fresh.outlook(NOW)["why"] == "no forecast downloaded yet"
    poller, _ = _poller(monkeypatch, age_min=config.NWS_STALE_AFTER_MIN + 1)
    assert "stale" in poller.outlook(NOW + 10, 2400)["why"]
    poller, _ = _poller(monkeypatch, age_min=-3.0)        # "issued" after it was fetched
    assert "stale" in poller.outlook(NOW + 10, 2400)["why"]
    poller, _ = _poller(monkeypatch)
    assert poller.outlook(NOW - 7200, 1e9)["available"] is False    # before its first hour
    # a download dated more than two minutes AHEAD of the clock (same hour) is not used
    late, _ = _poller(monkeypatch, fetched=NOW + 600, hour0=NOW)
    assert late.outlook(NOW + 479, 1e9)["available"] is False
    assert late.outlook(NOW + 481, 1e9)["available"] is True
    assert poller.outlook(NOW + 6 * 3600, 1e9)["available"] is False  # beyond its hours
    hour0 = NOW - NOW % 3600
    last = nws_forecast.OUTLOOK_HOURS - 1
    # the last hour it holds has no "next hour" after it
    assert poller.outlook(hour0 + last * 3600 + 10, 1e9)["available"] is False
    assert poller.outlook(hour0 + (last - 1) * 3600 + 10, 1e9)["available"] is True
    poller.clock_stepped(NOW, NOW + 86400)
    assert poller.outlook(NOW + 10, 2400)["available"] is False


def test_a_malformed_log_only_series_does_not_cost_the_main_monitor_its_forecast(monkeypatch):
    # dewpoint / relativeHumidity / windGust are carried for the weather log only. A
    # garbled one used to make the whole download "malformed": the main monitor's
    # cloud veto was then silently gone.
    now = time.time()
    start = time.strftime("%Y-%m-%dT%H:00:00+00:00", time.gmtime(now))
    good = {"values": [{"validTime": start + "/PT6H", "value": 90}]}
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    for name in ("dewpoint", "relativeHumidity", "windGust"):
        for garbage in ("n/a", [1, 2], {"values": [{"value": 5}]}, {"values": None},
                        {"values": [None]}, {"values": [{"validTime": "soon/PT1H", "value": 1}]}):
            props = {"updateTime": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                 time.gmtime(now - 600)),
                     "skyCover": good, name: garbage}
            monkeypatch.setattr(nws_forecast, "_get", lambda url, ua, p=props: {"properties": p})
            poller = nws_forecast.NwsForecastPoller(config)
            assert poller.poll_now()["ok"] is True, (name, garbage)
            comp = poller.component()
            assert comp["available"] is True and comp["safe"] is False, (name, garbage)
            assert "cloud cover 90%" in comp["reasons"][0]
    # ...while a garbled series the main monitor reads still makes the download unusable
    props = {"updateTime": start, "skyCover": "n/a"}
    monkeypatch.setattr(nws_forecast, "_get", lambda url, ua: {"properties": props})
    assert nws_forecast.fetch(config)["ok"] is False


def test_fetched_hours_line_up_with_the_clock(monkeypatch):
    # a different value for every hour: outlook_hours[k] must be the hour of the
    # download plus k, so that "this hour and the next" are really those two
    now = time.time()
    hour0 = now - now % 3600

    def iso(ts):
        return time.strftime("%Y-%m-%dT%H:00:00+00:00", time.gmtime(ts))

    def hourly(base):
        return {"values": [{"validTime": iso(hour0 + k * 3600) + "/PT1H", "value": base + k}
                           for k in range(-2, 10)]}

    props = {"updateTime": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 600)),
             "skyCover": hourly(40), "probabilityOfPrecipitation": hourly(10)}
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    monkeypatch.setattr(nws_forecast, "_get", lambda url, ua: {"properties": props})
    poller = nws_forecast.NwsForecastPoller(config)
    res = poller.poll_now()
    assert [h["cloud_cover_pct"] for h in res["outlook_hours"]] == [40, 41, 42, 43, 44, 45]
    assert res["now_hour"]["cloud_cover_pct"] == 40 and res["next_hour"]["cloud_cover_pct"] == 41
    out = poller.outlook(now, 2400)
    assert (out["this_hour"]["cloud_cover_pct"], out["next_hour"]["cloud_cover_pct"]) == (40, 41)
    assert (out["this_hour"]["precip_prob_pct"], out["next_hour"]["precip_prob_pct"]) == (10, 11)
    out = poller.outlook(hour0 + 3600 + 5, 7200)                 # an hour on, same download
    assert (out["this_hour"]["cloud_cover_pct"], out["next_hour"]["cloud_cover_pct"]) == (41, 42)


def test_fetch_keeps_the_hours_and_fields_the_outlook_needs(monkeypatch):
    now = time.time()
    start = time.strftime("%Y-%m-%dT%H:00:00+00:00", time.gmtime(now))

    def series(value, uom):
        return {"uom": uom, "values": [{"validTime": start + "/PT12H", "value": value}]}

    props = {"updateTime": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 600)),
             "skyCover": series(12, "wmoUnit:percent"),
             "probabilityOfPrecipitation": series(3, "wmoUnit:percent"),
             "temperature": series(11.0, "wmoUnit:degC"),
             "dewpoint": series(7.5, "wmoUnit:degC"),
             "relativeHumidity": series(79, "wmoUnit:percent"),
             "windSpeed": series(12.874752, "wmoUnit:km_h-1"),
             "windGust": series(25.0, "wmoUnit:km_h-1")}
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    monkeypatch.setattr(nws_forecast, "_get", lambda url, ua: {"properties": props})
    res = nws_forecast.fetch(config)
    assert res["ok"] and len(res["outlook_hours"]) == nws_forecast.OUTLOOK_HOURS
    assert res["hour0_ts"] == now - now % 3600
    hour = res["outlook_hours"][1]
    assert hour["cloud_cover_pct"] == 12 and hour["precip_prob_pct"] == 3
    assert hour["dewpoint_c"] == 7.5 and hour["rh_pct"] == 79 and hour["wind_gust_kmh"] == 25.0
    # the grid's wind is km/h: 12.87 km/h is the 8 mph of the dew rule
    assert close(dew.mph_from_kmh(hour["wind_speed_kmh"]), 8.0)
    # device 0's view of the same download is unchanged
    assert set(res["now_hour"]) == set(nws_forecast.WANTED)


# --- wiring: monitor + Alpaca ----------------------------------------------------------------
class FakeNws:
    """Stands in for NwsForecastPoller: a forecast that satisfies both monitors."""

    def __init__(self, cloud=(10, 10), precip=(0, 0), available=True):
        self.cloud, self.precip, self.available = cloud, precip, available

    def component(self, now=None):
        comp = nws_forecast.unavailable_component(config)
        comp.update(available=True, stale=False)
        return comp

    def outlook(self, now=None, max_age_s=None):
        self.asked_max_age_s = max_age_s
        return outlook(self.cloud, self.precip, self.available, "test")


class FakeGlm:
    """Stands in for GlmLightningPoller: a current poll, by default with no flash at all.
    (A new poll at every evaluation: its time stamp moves with the clock.)"""

    def __init__(self):
        self.nearest, self.over = None, {}

    def component(self, sun_alt=None, now=None):
        now = time.time() if now is None else now
        comp = glm(self.nearest, age_s=30, now=now, **self.over)
        self.returned = json.loads(json.dumps(comp))        # a copy nobody else holds
        return comp


def seasoned(mon=None):
    """A dew monitor in the middle of a quiet night by the REAL clocks: an hour of quiet
    GLM polls behind it, so the lightning line is not held back as "not watched yet"."""
    mon = mon or DewMonitor(config)
    wall, mono = time.time(), time.monotonic()
    for back in range(3600, 0, -300):
        mon.update_lightning(glm(age_s=0, now=wall - back), wall - back, mono - back)
    return mon


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for key, name in (("INPUTS_FILE", "i.json"), ("STATE_FILE", "s.json"),
                      ("LATCH_FILE", "l.json"), ("EVENT_LOG", "e.log")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, "WU_API_KEY", "testkey")
    monkeypatch.setattr(wu_poll, "discover_stations",
                        lambda lat, lon: [("S1", 1.0), ("S2", 2.0), ("S3", 3.0)])
    box = {"results": None}

    def poll(stations, max_age_min=None):
        results = box["results"] if box["results"] is not None else three(now=time.time())
        return {"live": len(results), "total": len(stations), "raining": [],
                "max_rate": 0.0, "results": results}
    monkeypatch.setattr(wu_poll, "poll_stations", poll)

    def inputs(sun=-20.0, humidity=40.0):
        with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "sun_altitude_deg": sun,
                       "humidity_pct": humidity, "temperature_c": 14.0}, f)

    inputs()
    log = EventLog(config.EVENT_LOG)
    poller = RainPoller(config, log)
    nws, lightning = FakeNws(), FakeGlm()
    monitor = SafetyMonitor(config, log, poller, nws=nws, glm=lightning, dew=seasoned())
    poller.poll_now()
    app = create_app(monitor, config)
    app.testing = True
    return {"monitor": monitor, "poller": poller, "nws": nws, "glm": lightning,
            "client": app.test_client(), "inputs": inputs, "box": box, "log": log}


def test_monitor_publishes_the_dew_verdict_beside_its_own(rig):
    state = rig["monitor"].evaluate()
    assert state["is_safe"] is True and state["dew"]["is_safe"] is True
    assert "dew" not in state["components"]          # not one of device 0's inputs
    assert state["dew"]["alpaca"] == {
        "device_number": 1, "name": "TTU Dew Monitor",
        "issafe_path": "/api/v1/safetymonitor/1/issafe"}
    assert rig["monitor"].is_dew_safe() is True
    json.dumps(state)                                # the state file must serialise


def test_monitor_asks_for_a_forecast_no_older_than_forty_minutes(rig):
    rig["monitor"].evaluate()
    assert rig["nws"].asked_max_age_s == 40 * 60


def test_a_change_of_the_dew_verdict_is_written_to_the_state_file_at_once(rig):
    def on_disk():
        with open(config.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)["dew"]["is_safe"]
    rig["monitor"].evaluate()
    assert on_disk() is True
    rig["nws"].cloud = (80, 80)
    rig["monitor"].evaluate()                    # well inside the 60 s heartbeat
    assert on_disk() is False


def test_state_file_heartbeat_survives_a_backward_clock_step(rig, monkeypatch):
    from safety import monitor as monitor_mod
    clock = {"now": time.time()}
    monkeypatch.setattr(monitor_mod.time, "time", lambda: clock["now"])
    rig["inputs"]()
    rig["monitor"].evaluate()
    clock["now"] -= 3 * 86400                    # NTP corrects a clock that ran ahead
    with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": clock["now"], "sun_altitude_deg": -20.0, "humidity_pct": 40.0}, f)
    rig["monitor"].evaluate()
    with open(config.STATE_FILE, encoding="utf-8") as f:
        assert json.load(f)["ts"] == clock["now"]    # rewritten, not left in the future


def test_dew_verdict_follows_device_0_and_never_the_other_way_round(rig):
    rig["inputs"](sun=10.0)                          # daylight: device 0 UNSAFE
    state = rig["monitor"].evaluate()
    assert state["is_safe"] is False and state["dew"]["is_safe"] is False
    rig["inputs"]()                                  # night again...
    rig["nws"].cloud = (80, 80)                      # ...but the dew forecast rule fails
    state = rig["monitor"].evaluate()
    assert state["dew"]["is_safe"] is False
    assert state["is_safe"] is True and state["reasons"] == []


def test_a_broken_dew_monitor_reads_false_and_leaves_device_0_alone(rig):
    def boom(*args, **kwargs):
        raise RuntimeError("dew exploded")
    rig["monitor"].dew.evaluate = boom
    state = rig["monitor"].evaluate()
    assert state["is_safe"] is True
    assert state["dew"]["is_safe"] is False and state["dew"]["enabled"] is True
    assert "dew exploded" in state["dew"]["reasons"][0]
    assert rig["monitor"].is_dew_safe() is False


def test_a_malformed_dew_block_cannot_break_the_evaluation(rig):
    # defence in depth: whatever a future dew.py returns, IsSafe(0) is answered
    for block in ({"is_safe": False, "reasons": 5}, {"is_safe": True, "spread_c": "x"},
                  {"is_safe": False, "reasons": None}, {"is_safe": True}):
        rig["monitor"]._last_dew_safe = None
        rig["monitor"].dew.evaluate = lambda *a, block=block, **k: dict(block)
        assert rig["monitor"].evaluate()["is_safe"] is True, block

    def boom(now=None, max_age_s=None):
        raise RuntimeError("outlook exploded")
    rig["nws"].outlook = boom
    del rig["monitor"].dew.evaluate                     # the real one again
    state = rig["monitor"].evaluate()
    assert state["is_safe"] is True and state["dew"]["is_safe"] is False
    assert any("outlook exploded" in r for r in state["dew"]["reasons"])


def test_dew_transitions_are_not_written_to_the_sd_event_log(rig):
    rig["monitor"].evaluate()
    rig["nws"].cloud = (80, 80)
    rig["monitor"].evaluate()
    rig["nws"].cloud = (10, 10)
    rig["monitor"].evaluate()
    actions = [e["action"] for e in rig["log"].recent(50)]
    assert "INIT" in actions and not any("DEW" in a.upper() for a in actions)


def test_alpaca_lists_two_safety_monitors(rig):
    devices = rig["client"].get("/management/v1/configureddevices").get_json()["Value"]
    assert [(d["DeviceNumber"], d["DeviceType"]) for d in devices] == [
        (0, "SafetyMonitor"), (1, "SafetyMonitor")]
    assert devices[0]["UniqueID"] != devices[1]["UniqueID"]
    assert devices[1]["DeviceName"] == "TTU Dew Monitor"


def test_device_1_is_a_standard_safety_monitor_with_its_own_connection(rig):
    c = rig["client"]
    assert c.get("/api/v1/safetymonitor/1/issafe").get_json()["ErrorNumber"] == 0x407
    assert c.put("/api/v1/safetymonitor/1/connected",
                 data={"Connected": "true"}).get_json()["ErrorNumber"] == 0
    body = c.get("/api/v1/safetymonitor/1/issafe?ClientTransactionID=7").get_json()
    assert body["ErrorNumber"] == 0 and body["Value"] is True
    assert body["ClientTransactionID"] == 7
    # connecting device 1 did not connect device 0 (NINA's), and vice versa
    assert c.get("/api/v1/safetymonitor/0/connected").get_json()["Value"] is False
    assert c.get("/api/v1/safetymonitor/0/issafe").get_json()["ErrorNumber"] == 0x407
    assert c.get("/api/v1/safetymonitor/1/connected").get_json()["Value"] is True
    assert c.get("/api/v1/safetymonitor/1/name").get_json()["Value"] == "TTU Dew Monitor"
    assert "NOT an observing-safety monitor" in c.get(
        "/api/v1/safetymonitor/1/description").get_json()["Value"]
    assert c.get("/api/v1/safetymonitor/1/interfaceversion").get_json()["Value"] == 2
    assert c.get("/api/v1/safetymonitor/7/issafe").get_json()["ErrorNumber"] == 0x400
    assert c.put("/api/v1/safetymonitor/7/connected",
                 data={"Connected": "true"}).get_json()["ErrorNumber"] == 0x400


def test_device_1_reads_false_when_the_rule_fails(rig):
    c = rig["client"]
    c.put("/api/v1/safetymonitor/1/connected", data={"Connected": "true"})
    rig["box"]["results"] = three(wind=15.0, now=time.time())
    rig["poller"].poll_now()
    assert c.get("/api/v1/safetymonitor/1/issafe").get_json()["Value"] is False
    state = rig["monitor"].evaluate()
    assert state["dew"]["connected"] is True and state["dew"]["last_read_age_s"] == 0
    assert state["connected"] is False


def test_without_the_dew_monitor_there_is_no_device_1(rig):
    plain = SafetyMonitor(config, rig["log"], rig["poller"])
    app = create_app(plain, config)
    app.testing = True
    c = app.test_client()
    assert c.get("/api/v1/safetymonitor/1/issafe").get_json()["ErrorNumber"] == 0x400
    assert len(c.get("/management/v1/configureddevices").get_json()["Value"]) == 1
    state = plain.evaluate()
    assert state["dew"]["enabled"] is False and state["dew"]["is_safe"] is False


def test_daemon_gives_the_dew_monitor_clock_steps_and_can_leave_it_out(daemon, monkeypatch):  # noqa: F811
    steps = []
    monkeypatch.setattr(DewMonitor, "clock_stepped",
                        lambda self, pre, post: steps.append((pre, post)))
    built = daemon()
    assert isinstance(built["monitor"].dew, DewMonitor)
    assert steps == [(built["t0"], built["t0"] + 3600.0)]
    monkeypatch.setattr(config, "DEW_ENABLED", False)        # TTU_SAFETY_DEW=0
    assert daemon()["monitor"].dew is None


def test_setup_page_shows_the_dew_monitor_as_ignored_or_trusted(rig):
    page = rig["client"].get("/setup").get_data(as_text=True)
    assert "Dew monitor (device 1)" in page
    assert "rain sensor may be IGNORED (dew likely)" in page
    assert "needs ≤ 5 °C" in page and "calm ≤ 8 mph, until above 9 mph" in page
    assert "no flash seen (needs none within 60 km for 30 min)" in page
    rig["nws"].precip = (9, 0)
    page = rig["client"].get("/setup/v1/safetymonitor/1/setup").get_data(as_text=True)
    assert "rain sensor TRUSTED" in page
    assert "forecast precipitation probability 9% (this hour) is not below 5%" in page


def test_setup_page_lightning_row(rig):
    def row():
        page = rig["client"].get("/setup").get_data(as_text=True)
        return page.split("Lightning (GLM)</td>")[1].split("</tr>")[0]

    rig["glm"].nearest = 143.2
    assert "nearest flash 143 km (needs none within 60 km for 30 min)" in row()
    assert "#1a7f37" in row()                               # green: checked and clear
    # at the precision the rule works with: 60.3 km is outside, and must not read "60 km"
    rig["glm"].nearest = 60.3
    assert "nearest flash 60.3 km (needs none" in row() and "#1a7f37" in row()
    rig["glm"].nearest = 57.0
    assert "flash 57.0 km away 0 min ago (needs none within 60 km" in row()
    assert "#b42318" in row()
    page = rig["client"].get("/setup").get_data(as_text=True)
    assert "rain sensor TRUSTED" in page and "GLM lightning 57 km away" in page
    # the flash that counts is named, not the (distant) nearest one of a later poll
    rig["glm"].nearest = 143.2
    assert "flash 57.0 km away 0 min ago" in row() and "143" not in row()
    # a monitor that has only just started watching: grey, and it says so
    rig["monitor"].dew = DewMonitor(config)
    assert "nearest flash 143 km, watched for 0 min so far (needs none" in row()
    assert "#8b959c" in row()
    # no lightning layer at all: grey, with the reason
    rig["monitor"].dew = DewMonitor(config)
    rig["monitor"].glm = None
    assert "no data — the GLM lightning layer is off" in row() and "#8b959c" in row()


def test_setup_page_lightning_row_prints_the_limits_in_force(rig, monkeypatch):
    monkeypatch.setattr(config, "DEW_GLM_KM", 80.0)
    monkeypatch.setattr(config, "GLM_COOLOFF_HOURS", 0.75)
    page = rig["client"].get("/setup").get_data(as_text=True)
    assert "(needs none within 80 km for 45 min)" in page


def test_setup_page_survives_any_lightning_block(rig):
    # /setup is a debug page: whatever the block holds, it renders (never a 500)
    real = rig["monitor"].dew.evaluate

    def block(**lightning):
        def evaluate(*args, **kwargs):
            out = real(*args, **kwargs)
            out["is_safe"] = False
            out["checks"]["lightning"] = lightning.pop("check", False)
            out["lightning"].update(lightning)
            return out
        rig["monitor"].dew.evaluate = evaluate
        resp = rig["client"].get("/setup")
        assert resp.status_code == 200, lightning
        return resp.get_data(as_text=True).split("Lightning (GLM)</td>")[1].split("</tr>")[0]

    for odd in (float("nan"), float("inf"), -5, "soon", None, True, [3]):
        assert "needs none within 60 km" in block(recent_km=55.0, recent_age_s=odd)
        assert "needs none within 60 km" in block(recent_km=odd, recent_age_s=120)
        assert "needs none within 60 km" in block(check=None, available=True, watched_s=odd)
        assert "needs none within 60 km" in block(check=True, available=True, nearest_km=odd)
    assert "flash at an unreadable distance 2 min ago" in block(recent_km=None,
                                                                recent_age_s=120)
    rig["monitor"].dew.evaluate = lambda *a, **k: dict(real(*a, **k), lightning="nonsense")
    assert rig["client"].get("/setup").status_code == 200


# --- wiring: the real GLM lightning layer -----------------------------------------------------
@pytest.fixture
def storm(rig, tmp_path, monkeypatch):
    """The rig with the REAL GlmLightningPoller (only its S3 download is faked), in the
    middle of a quiet night, and clocks the test moves — the wall clock and the monotonic
    one together. at(seconds, nearest=...) goes to that time, refreshes the inputs and
    the stations, lets GLM poll (nearest flash at `nearest` km; None: no flash;
    ok=False: the poll fails; poll=False: no poll) and evaluates. at.quiet(a, b) runs
    quiet five-minute polls from a to b seconds."""
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    monkeypatch.setattr(glm_lightning, "deps_available", lambda: True)
    sky = {"nearest": None, "ok": True, "granules": 14}

    def poll_flashes(cfg):
        if not sky["ok"]:
            return {"ok": False, "error": "list: timed out"}
        near = sky["nearest"]
        in_ring = near is not None and near <= cfg.GLM_TRIGGER_KM
        return {"ok": True, "error": None, "granules_scanned": sky["granules"],
                "in_ring": in_ring, "flashes_in_ring": int(in_ring), "nearest_km": near,
                "nearest_bearing": None if near is None else "NW"}
    monkeypatch.setattr(glm_lightning, "poll_flashes", poll_flashes)
    lightning = glm_lightning.GlmLightningPoller(config, rig["log"])
    rig["monitor"].glm = lightning
    t0, m0 = time.time(), time.monotonic()
    clock = {"now": t0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    monkeypatch.setattr(time, "monotonic", lambda: m0 + (clock["now"] - t0))
    rig["monitor"].dew = DewMonitor(config)
    for back in range(3600, 0, -300):                 # the quiet hour before the test
        rig["monitor"].dew.update_lightning(glm(age_s=0, now=t0 - back), t0 - back,
                                            m0 - back)

    def at(seconds, nearest=None, ok=True, poll=True, granules=14):
        clock["now"] = t0 + seconds
        rig["inputs"]()
        rig["poller"].poll_now()
        if poll:
            sky.update(nearest=nearest, ok=ok, granules=granules)
            lightning.poll_now(clock["now"])
        return rig["monitor"].evaluate()

    def quiet(start, stop):
        for seconds in range(start, stop + 1, 300):
            state = at(seconds)
        return state
    at.quiet = quiet
    at.rig = rig
    return at


def test_lightning_between_the_two_rings_device_0_safe_device_1_false(storm):
    st = storm(0, nearest=55.0)
    assert st["is_safe"] is True and st["components"]["glm"]["latched"] is False
    assert st["dew"]["is_safe"] is False and st["dew"]["checks"]["lightning"] is False
    assert st["dew"]["reasons"] == ["GLM lightning 55 km away 0 min ago is within 60 km "
                                    "(counts for another 30 min)"]
    st = storm.quiet(300, 1500)                              # later polls see nothing
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False
    assert storm(30 * 60 - 10, poll=False)["dew"]["is_safe"] is False
    st = storm(30 * 60, poll=False)
    assert st["dew"]["is_safe"] is True and st["dew"]["checks"]["lightning"] is True
    json.dumps(st)


def test_lightning_inside_the_main_ring_both_false_and_both_clear_together(storm):
    st = storm(0, nearest=45.0)
    assert st["is_safe"] is False and st["components"]["glm"]["latched"] is True
    assert st["dew"]["is_safe"] is False
    assert st["dew"]["checks"]["device0"] is False
    assert st["dew"]["checks"]["lightning"] is False
    st = storm.quiet(300, 1500)                              # later polls, all quiet
    assert st["is_safe"] is False and st["dew"]["is_safe"] is False
    st = storm(30 * 60 - 10, poll=False)
    assert st["is_safe"] is False and st["dew"]["is_safe"] is False
    st = storm(30 * 60, poll=False)
    assert st["is_safe"] is True and st["dew"]["is_safe"] is True


def test_distant_lightning_changes_nothing(storm):
    st = storm(0, nearest=61.0)
    assert st["is_safe"] is True and st["dew"]["is_safe"] is True
    assert st["dew"]["lightning"]["nearest_km"] == 61.0


def test_one_failed_glm_poll_is_tolerated_end_to_end(storm):
    assert storm(0)["dew"]["is_safe"] is True
    st = storm(310, ok=False)
    assert st["components"]["glm"]["available"] is False      # device 0's view: no data
    assert st["dew"]["is_safe"] is True                       # device 1: the last good poll
    st = storm(620)                               # the next poll works: nothing happened
    assert st["dew"]["is_safe"] is True and st["dew"]["lightning"]["watched_s"] >= 1800


def test_a_longer_glm_outage_is_a_gap_and_the_watch_starts_again(storm):
    assert storm(0)["dew"]["is_safe"] is True
    assert storm(310, ok=False)["dew"]["is_safe"] is True
    assert storm(620, ok=False)["dew"]["is_safe"] is True
    st = storm(905, poll=False)                   # no good poll for more than 15 min
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False
    assert st["dew"]["reasons"] == ["no current lightning data: no good GLM poll for "
                                    "15 min (the last GLM poll failed or is too old)"]
    st = storm(930)                               # GLM is back
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False
    assert st["dew"]["reasons"] == [WATCH % 0]
    assert storm.quiet(1230, 2430)["dew"]["is_safe"] is False         # 25 min of it
    assert storm(930 + 1800, poll=False)["dew"]["is_safe"] is True    # 30 min


def test_a_daemon_that_has_just_started_keeps_device_1_false_for_a_hold(storm):
    storm.rig["monitor"].dew = DewMonitor(config)             # what a restart leaves behind
    st = storm(0)
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False
    assert st["dew"]["reasons"] == [WATCH % 0]
    assert storm.quiet(300, 1500)["dew"]["is_safe"] is False
    assert storm(1790, poll=False)["dew"]["is_safe"] is False
    assert storm(1800, poll=False)["dew"]["is_safe"] is True


def test_a_glm_poll_that_read_too_little_is_not_no_lightning(storm):
    # the network dropped 13 of the 14 granules: "no flash" in the one that was read
    assert storm(0)["dew"]["is_safe"] is True
    for seconds in (310, 620):
        st = storm(seconds, granules=1)
        assert st["is_safe"] is True and st["dew"]["is_safe"] is True     # tolerated...
    st = storm(930, granules=1)                   # ...for fifteen minutes
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False
    assert st["dew"]["reasons"] == ["no current lightning data: no good GLM poll for "
                                    "15 min (the last GLM poll read only 1 of the 10 "
                                    "granules needed)"]
    # and a flash in the little that was read counts at once, device 0 unaffected
    st = storm(1240, nearest=55.0, granules=1)
    assert st["is_safe"] is True and st["dew"]["checks"]["lightning"] is False


def test_an_alpaca_read_inside_a_clock_glitch_does_not_lose_the_flash(storm, monkeypatch):
    c = storm.rig["client"]
    c.put("/api/v1/safetymonitor/1/connected", data={"Connected": "true"})
    assert storm(0, nearest=55.0)["dew"]["is_safe"] is False
    wall = time.time
    monkeypatch.setattr(time, "time", lambda: wall() - 7200)  # the wall clock, 2 h back
    storm.rig["inputs"]()
    assert c.get("/api/v1/safetymonitor/1/issafe").get_json()["Value"] is False
    monkeypatch.setattr(time, "time", wall)                   # ...and back again
    st = storm.quiet(300, 1500)
    assert st["is_safe"] is True and st["dew"]["checks"]["lightning"] is False
    assert c.get("/api/v1/safetymonitor/1/issafe").get_json()["Value"] is False
    assert storm(1795, poll=False)["dew"]["checks"]["lightning"] is False
    assert storm(1800, poll=False)["dew"]["is_safe"] is True


def test_without_the_glm_layer_device_1_is_false_and_device_0_unaffected(rig, monkeypatch):
    rig["monitor"].glm = None                                 # TTU_SAFETY_GLM=0
    rig["monitor"].dew = DewMonitor(config)
    st = rig["monitor"].evaluate()
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False
    assert st["dew"]["reasons"] == ["no current lightning data: the GLM lightning layer "
                                    "is off"]
    # ...and the same with the layer present but numpy / netCDF4 missing
    monkeypatch.setattr(config, "GLM_LATCH_FILE", config.LATCH_FILE + ".glm")
    monkeypatch.setattr(glm_lightning, "deps_available", lambda: False)
    rig["monitor"].glm = glm_lightning.GlmLightningPoller(config, rig["log"])
    st = rig["monitor"].evaluate()
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False
    assert "the GLM lightning layer is off" in st["dew"]["reasons"][0]


def test_the_dew_monitor_reads_the_lightning_component_and_leaves_it_alone(rig):
    seen = []
    real = rig["monitor"].dew.update_lightning

    def spy(comp, now=None, mono=None):
        seen.append(comp)
        times.append((now, mono))
        return real(comp, now, mono)
    times = []
    rig["monitor"].dew.update_lightning = spy
    rig["glm"].nearest = 45.0
    rig["glm"].over = {"in_ring": True, "nearest_bearing": "NW"}
    st = rig["monitor"].evaluate()
    # it is handed the component of this very evaluation, dated with this evaluation's
    # own wall-clock time (and left to read the monotonic clock itself)...
    assert len(seen) == 1 and seen[0] is not None
    assert times == [(st["ts"], None)]
    # ...which device 0 still publishes exactly as the GLM layer returned it
    assert st["components"]["glm"] == rig["glm"].returned == seen[0]
    assert st["components"]["glm"]["in_ring"] is True


def test_a_failing_lightning_intake_reads_false_and_leaves_device_0_alone(rig):
    def boom(*args, **kwargs):
        raise RuntimeError("lightning exploded")
    rig["monitor"].dew.update_lightning = boom
    st = rig["monitor"].evaluate()
    assert st["is_safe"] is True and st["reasons"] == []
    assert st["dew"]["is_safe"] is False and st["dew"]["enabled"] is True
    assert "lightning exploded" in st["dew"]["reasons"][0]
    assert st["dew"]["checks"]["lightning"] is None
    json.dumps(st)
    c = rig["client"]
    c.put("/api/v1/safetymonitor/0/connected", data={"Connected": "true"})
    assert c.get("/api/v1/safetymonitor/0/issafe").get_json()["Value"] is True
    assert c.get("/setup").status_code == 200
    del rig["monitor"].dew.update_lightning
    rig["monitor"].dew.update_stations = boom                 # ...and the same for stations
    st = rig["monitor"].evaluate()
    assert st["is_safe"] is True and st["dew"]["is_safe"] is False


def test_an_odd_lightning_component_cannot_break_either_verdict(rig):
    for odd in ({"nearest_km": "x", "last_poll_ts": "y", "granules_scanned": [1]},
                {"available": 1, "enabled": 1}, {"last_poll_ts": float("inf")},
                {"nearest_km": float("nan")}, {"granules_scanned": float("nan")},
                {"nearest_km": [55.0]}, {"last_poll_ts": None}, {"available": None},
                {"granules_scanned": "14"}, {"polling_active": "?", "available": False}):
        rig["monitor"].dew = DewMonitor(config)               # it knows nothing else
        rig["glm"].over = odd
        st = rig["monitor"].evaluate()
        assert st["is_safe"] is True, odd
        assert st["dew"]["is_safe"] is False and st["dew"]["enabled"] is True, odd
        assert st["dew"]["checks"]["lightning"] is not True, odd
        json.dumps(st)


def _daemon_events(daemon):  # noqa: F811
    built = daemon()
    with open(config.EVENT_LOG, encoding="utf-8") as f:
        return built, f.read()


def test_daemon_says_so_when_the_dew_monitor_has_no_lightning_layer(daemon, tmp_path,  # noqa: F811
                                                                    monkeypatch, caplog):
    needs = "dew monitor needs the GLM lightning layer"
    always = "device 1 will ALWAYS read false"
    # the fixture runs with TTU_SAFETY_GLM=0; the libraries are there, so it is the
    # switch alone that must be reported
    monkeypatch.setattr(glm_lightning, "deps_available", lambda: True)
    _, events = _daemon_events(daemon)
    assert needs in events and "device 1 always false" in events
    assert "the GLM lightning layer is off (TTU_SAFETY_GLM=0)" in caplog.text
    assert always in caplog.text
    # the layer is on, but numpy / netCDF4 are not installed: the same, said as such
    os.remove(config.EVENT_LOG)
    caplog.clear()
    monkeypatch.setattr(config, "GLM_ENABLED", True)
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    monkeypatch.setattr(glm_lightning, "deps_available", lambda: False)
    built, events = _daemon_events(daemon)
    assert built["monitor"].glm is not None and needs in events
    assert "the GLM lightning layer is off (numpy/netCDF4 not installed)" in caplog.text
    # everything installed, but the site is where GOES-East cannot see: the layer never polls
    os.remove(config.EVENT_LOG)
    caplog.clear()
    monkeypatch.setattr(glm_lightning, "deps_available", lambda: True)
    monkeypatch.setattr(config, "GEOCODE", (52.0, 21.0))
    _, events = _daemon_events(daemon)
    assert needs in events and always in caplog.text
    assert "the GLM lightning layer is off (the site is outside GOES-East coverage)" in caplog.text
    # with a working lightning layer there is nothing to say
    os.remove(config.EVENT_LOG)
    caplog.clear()
    monkeypatch.setattr(config, "GEOCODE", (33.748, -101.958))
    built, events = _daemon_events(daemon)
    assert built["monitor"].glm is not None and "glm-poller" in built["threads"]
    assert needs not in events and always not in caplog.text
    # ...nor without a dew monitor
    os.remove(config.EVENT_LOG)
    monkeypatch.setattr(config, "GLM_ENABLED", False)
    monkeypatch.setattr(config, "DEW_ENABLED", False)
    assert needs not in _daemon_events(daemon)[1]


def test_daemon_start_up_line_names_the_lightning_ring(daemon, caplog, monkeypatch):  # noqa: F811
    import logging
    caplog.set_level(logging.INFO)
    daemon()
    assert "no GLM lightning within 60 km in the last 30 min" in caplog.text
    caplog.clear()
    monkeypatch.setattr(config, "GLM_COOLOFF_HOURS", -1.0)    # nonsense: read as "no hold"
    daemon()
    assert "no GLM lightning within 60 km in the last 0 min" in caplog.text
