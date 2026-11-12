"""The dew monitor — Alpaca device 1 ("a WET rain-sensor reading right now is dew").

The rule is the owner's: device 0 SAFE, dew-point spread <= 5 degC and wind <= 8 mph (both
medians of the WU stations; wind stays calm until it is above 9 mph), forecast cloud cover
< 30 % and precipitation probability < 5 % for this hour and the next. No lower spread
limit, no memory. Missing data = false.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from safety import config, dew, nws_forecast, wu_poll
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


def verdict(stations, device0=True, fc="default", mon=None, now=NOW):
    mon = mon or DewMonitor(config)
    mon.update_stations(stations, now)
    return mon.evaluate(device0, outlook() if fc == "default" else fc, now)


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


# --- the rule ----------------------------------------------------------------------------
def _config_with(**env):
    """The settings a daemon started with this environment would run with (a child
    process: the module reads the environment once, at import)."""
    code = ("import json; from safety import config as c; print(json.dumps({"
            "'enabled': c.DEW_ENABLED, 'calm': c.DEW_WIND_MAX_MPH, "
            "'above': c.DEW_WIND_UNSAFE_ABOVE_MPH, 'spread': c.DEW_SPREAD_MAX_C, "
            "'fc_age': c.DEW_FORECAST_MAX_AGE_MIN, 'log': c.WEATHER_LOG_ENABLED, "
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


def test_all_conditions_met_means_the_rain_sensor_may_be_ignored():
    v = verdict(three())
    assert v["is_safe"] is True and v["reasons"] == []
    assert v["checks"] == {"device0": True, "spread": True, "wind": True, "cloud": True,
                           "precip": True}
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
    nws = FakeNws()
    monitor = SafetyMonitor(config, log, poller, nws=nws, dew=DewMonitor(config))
    poller.poll_now()
    app = create_app(monitor, config)
    app.testing = True
    return {"monitor": monitor, "poller": poller, "nws": nws, "client": app.test_client(),
            "inputs": inputs, "box": box, "log": log}


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
    rig["nws"].precip = (9, 0)
    page = rig["client"].get("/setup/v1/safetymonitor/1/setup").get_data(as_text=True)
    assert "rain sensor TRUSTED" in page
    assert "forecast precipitation probability 9% (this hour) is not below 5%" in page
