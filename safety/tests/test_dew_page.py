"""Status-page rendering of the dew monitor card and the weather-log download block
(make_status_page.build_dew_html / build_weather_log_html), from hand-built state dicts."""
import json
import re
import sys
import time
from unittest.mock import MagicMock

# The page imports the Raspberry Pi hardware / heavy libraries at module load; stub them
# exactly as test_smoke.py does (setdefault: a real installed module is left alone).
for _m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
           "astropy.time", "astropy.coordinates", "astropy.utils", "astropy.utils.iers"]:
    sys.modules.setdefault(_m, MagicMock())

import make_status_page as msp        # noqa: E402  (must follow the sys.modules stubbing)
from safety import config, dew        # noqa: E402

GREEN, RED = "var(--good)", "#b42318"
GREY = "var(--faint, #8b959c)"


def dew_block(is_safe=True, **over):
    block = {
        "enabled": True, "is_safe": is_safe, "reasons": [],
        "checks": {"device0": True, "spread": True, "wind": True, "cloud": True,
                   "precip": True, "lightning": True},
        "spread_c": 4.44, "wind_mph": 4.6, "gust_mph": 7.0, "stations_used": 5,
        "stations_wind": 4, "wind_calm": True,
        "forecast": {"available": True, "cloud_now": 12, "cloud_next": 20,
                     "precip_now": 0, "precip_next": 1},
        "lightning": {"available": True, "why": None, "age_s": 120, "nearest_km": None,
                      "recent_km": None, "recent_age_s": None, "clear_in_s": None,
                      "watched_s": 7200},
        "thresholds": {"spread_max_c": 5.0, "wind_max_mph": 8.0,
                       "wind_unsafe_above_mph": 9.0, "cloud_below_pct": 30.0,
                       "precip_below_pct": 5.0, "min_stations": 3, "glm_km": 60.0,
                       "glm_hold_min": 30.0, "glm_max_age_min": 15.0,
                       "glm_min_granules": 10},
        "connected": True, "last_read_age_s": 12,
        "alpaca": {"device_number": 1, "name": "TTU Dew Monitor",
                   "issafe_path": "/api/v1/safetymonitor/1/issafe"},
    }
    block.update(over)
    return block


def state(dew=None, ts=None, **extra):
    st = {"ts": time.time() if ts is None else ts, "is_safe": True, "reasons": [],
          "components": {}, "dew": dew_block() if dew is None else dew}
    st.update(extra)
    return st


def tile(page, label):
    m = re.search(r'<div class="tile">\s*<div class="k">%s</div>(.*?)</div>\s*</div>'
                  % re.escape(label), page, re.S)
    assert m, "no tile %r" % label
    return m.group(1)


# --- dew monitor card --------------------------------------------------------------------
def test_ignored_is_shown_as_ignored_never_as_safe():
    page = msp.build_dew_html(state())
    assert "RAIN SENSOR IGNORED" in page and "RAIN SENSOR TRUSTED" not in page
    assert ">SAFE</span>" not in page and "UNSAFE" not in page.replace("wind_unsafe", "")
    assert msp._DEW_IGNORED_BG in page
    assert "treated as dew, not rain" in page
    assert "device 1" in page and "/api/v1/safetymonitor/1/issafe" in page
    assert "dome driver last asked 12 s ago" in page
    assert "For the dome driver, not for NINA" in page


def test_tiles_show_the_numbers_with_their_limits_and_units():
    page = msp.build_dew_html(state())
    spread, wind = tile(page, "Dew-point spread"), tile(page, "Wind")
    assert "4.4" in spread and "°C" in spread and "needs &le; 5 °C" in spread
    assert "5 stations" in spread and GREEN in spread
    assert "4.6" in wind and "mph" in wind and "km" not in wind
    assert "calm &le; 8 mph, until &gt; 9" in wind and "4 stations" in wind
    cloud, precip = tile(page, "Forecast cloud"), tile(page, "Forecast precip.")
    assert "now 12%" in cloud and "next 20%" in cloud and "&lt; 30%" in cloud
    assert "now 0%" in precip and "next 1%" in precip and "&lt; 5%" in precip


def test_trusted_lists_the_reasons_and_marks_the_failing_tile():
    block = dew_block(is_safe=False, wind_mph=11.5, wind_calm=False,
                      reasons=["wind 11.5 mph is above 9 mph (too windy for dew)"])
    block["checks"]["wind"] = False
    page = msp.build_dew_html(state(block))
    assert "RAIN SENSOR TRUSTED" in page and "RAIN SENSOR IGNORED" not in page
    assert msp._DEW_TRUSTED_BG in page
    assert "wind 11.5 mph is above 9 mph (too windy for dew)" in page
    assert RED in tile(page, "Wind") and GREEN in tile(page, "Dew-point spread")
    assert "the dome acts on its own rain sensor" in page


def test_no_data_is_grey_and_shows_no_number():
    block = dew_block(is_safe=False, spread_c=3.0, stations_used=2,
                      reasons=["no dew-point spread: 2 station(s) ...", "<script>x</script>"])
    block["checks"].update(spread=None, cloud=None)
    block["forecast"].update(cloud_now=None, cloud_next=None)
    page = msp.build_dew_html(state(block))
    spread = tile(page, "Dew-point spread")
    assert "N/A" in spread and "3.0" not in spread and GREY in spread
    assert "2 stations" in spread
    assert "N/A" in tile(page, "Forecast cloud")
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_device_0_unsafe_is_named_on_its_own_tile():
    block = dew_block(is_safe=False, reasons=["the safety monitor (device 0) is UNSAFE"])
    block["checks"]["device0"] = False
    page = msp.build_dew_html(state(block))
    t = tile(page, "Safety monitor")
    assert "UNSAFE" in t and RED in t


def test_device_0_is_unknown_not_unsafe_when_the_dew_monitor_itself_failed():
    block = dew_block(is_safe=False, reasons=["dew monitor error: boom"])
    block["checks"] = {"device0": None, "spread": None, "wind": None, "cloud": None,
                       "precip": None, "lightning": None}
    page = msp.build_dew_html(state(block))
    t = tile(page, "Safety monitor")
    assert "N/A" in t and "UNSAFE" not in t and GREY in t
    assert GREY in tile(page, "Lightning (GLM)") and GREEN not in page


KM = '<span class="u">km</span>'


def test_lightning_tile_shows_the_nearest_flash_and_the_ring():
    t = tile(msp.build_dew_html(state()), "Lightning (GLM)")
    assert ">none<" in t and KM not in t and GREEN in t
    assert "needs none within 60 km for 30 min" in t and "nearest flash" not in t
    block = dew_block()
    block["lightning"]["nearest_km"] = 182.4                 # far away: still passes
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert ">182<" in t and KM in t and GREEN in t and "mph" not in t
    assert "nearest flash &middot; needs none within 60 km for 30 min" in t


def test_lightning_tile_agrees_with_the_verdict_at_the_ring():
    # the rule works to 0.1 km and "within 60 km" is inclusive: 60.3 km passes and must
    # not read "60 km" beside it; 59.6 km fails and must not read "60 km" either
    block = dew_block()
    block["lightning"]["nearest_km"] = 60.3
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert ">60.3<" in t and GREEN in t
    block = dew_block(is_safe=False)
    block["checks"]["lightning"] = False
    block["lightning"].update(nearest_km=59.6, recent_km=59.6, recent_age_s=60,
                              clear_in_s=1740)
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert ">59.6<" in t and RED in t and "seen 1 min ago" in t
    block["lightning"].update(recent_km=0.4, recent_age_s=45)
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert ">0.4<" in t and "seen 45 s ago" in t


def test_lightning_tile_names_the_flash_that_counts():
    block = dew_block(is_safe=False, reasons=[
        "GLM lightning 55.2 km away 12 min ago is within 60 km (counts for another 17 min)"])
    block["checks"]["lightning"] = False
    block["lightning"].update(nearest_km=310.0, recent_km=55.2, recent_age_s=12 * 60 + 40,
                              clear_in_s=17 * 60 + 20)
    page = msp.build_dew_html(state(block))
    t = tile(page, "Lightning (GLM)")
    # the flash that keeps the answer false, not the (distant) nearest of the latest poll
    assert ">55.2<" in t and "310" not in t and RED in t
    assert KM in t and '<span class="u">mi' not in t         # kilometres, never miles
    # whole minutes, rounded down like the reason line under the tiles
    assert "needs none within 60 km for 30 min &middot; seen 12 min ago" in t
    assert "RAIN SENSOR TRUSTED" in page and "counts for another 17 min" in page
    # a flash whose distance could not be read still shows as the flash that counts
    block["lightning"]["recent_km"] = None
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert ">?<" in t and RED in t and "seen 12 min ago" in t and "310" not in t


def test_lightning_tile_without_data_is_grey_and_shows_no_number():
    block = dew_block(is_safe=False,
                      reasons=["no current lightning data: the GLM lightning layer is off"])
    block["checks"]["lightning"] = None
    block["lightning"].update(available=False, why="the GLM lightning layer is off",
                              age_s=None, nearest_km=44.0, watched_s=None)
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert "N/A" in t and "44" not in t and GREY in t


def test_lightning_tile_says_when_it_has_not_watched_long_enough():
    block = dew_block(is_safe=False, reasons=[
        "GLM lightning watched for only 12 of the 30 min needed (after a daemon start, a "
        "clock correction or a gap in the data)"])
    block["checks"]["lightning"] = None
    block["lightning"].update(nearest_km=412.6, watched_s=12 * 60 + 5)
    page = msp.build_dew_html(state(block))
    t = tile(page, "Lightning (GLM)")
    assert ">413<" in t and GREY in t and GREEN not in t
    assert "needs none within 60 km for 30 min &middot; watched 12 min so far" in t
    assert "RAIN SENSOR TRUSTED" in page and "watched for only 12 of the 30 min" in page
    block["lightning"].update(nearest_km=None, watched_s=20)
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert ">none<" in t and "watched 20 s so far" in t


def _real_block(nearest, seen_ago_s=60, component_over=None, watched=True):
    """The dew block as the daemon builds it (safety/dew.py), through JSON as the state
    file carries it: the page must read the keys that module really writes. The last
    GLM poll (nearest flash `nearest` km) was taken in `seen_ago_s` seconds ago, after an
    hour of quiet polls unless watched=False."""
    now, mono = time.time(), 90_000.0
    mon = dew.DewMonitor(config)
    mon.update_stations([{"station": s, "state": "live", "obs_ts": now - 120,
                          "temp_f": 50.0, "dewpt_f": 45.0, "humidity_pct": 80.0,
                          "wind_mph": 3.0, "gust_mph": 5.0} for s in ("S1", "S2", "S3")], now)

    def poll(near, back, **over):
        comp = {"enabled": True, "available": True, "last_poll_ts": now - back,
                "granules_scanned": 14, "nearest_km": near, "polling_active": True}
        comp.update(over)
        mon.update_lightning(comp, now - back, mono - back)

    if watched:
        for back in range(3600 + int(seen_ago_s), int(seen_ago_s), -300):
            poll(None, back)
    poll(nearest, seen_ago_s, **(component_over or {}))
    hour = {"cloud_cover_pct": 10, "precip_prob_pct": 0}
    block = mon.evaluate(True, {"available": True, "why": None, "age_s": 60,
                                "this_hour": hour, "next_hour": hour}, now, mono)
    block.update(connected=True, last_read_age_s=5, alpaca={
        "device_number": 1, "issafe_path": "/api/v1/safetymonitor/1/issafe"})
    return json.loads(json.dumps(block))


def test_the_page_reads_what_the_daemon_really_writes():
    page = msp.build_dew_html(state(_real_block(None)))
    assert "RAIN SENSOR IGNORED" in page and page.count('<div class="tile">') == 6
    t = tile(page, "Lightning (GLM)")
    assert ">none<" in t and GREEN in t and "needs none within 60 km for 30 min" in t
    t = tile(msp.build_dew_html(state(_real_block(412.6))), "Lightning (GLM)")
    assert ">413<" in t and GREEN in t
    page = msp.build_dew_html(state(_real_block(57.3, seen_ago_s=250)))
    t = tile(page, "Lightning (GLM)")
    assert "RAIN SENSOR TRUSTED" in page and ">57.3<" in t and RED in t
    assert "seen 4 min ago" in t
    assert "GLM lightning 57.3 km away 4 min ago is within 60 km" in page
    # a daemon that has just started: data, but not yet half an hour of it
    page = msp.build_dew_html(state(_real_block(None, seen_ago_s=130, watched=False)))
    t = tile(page, "Lightning (GLM)")
    assert "RAIN SENSOR TRUSTED" in page and ">none<" in t and GREY in t
    assert "watched 2 min so far" in t
    assert "GLM lightning watched for only 2 of the 30 min needed" in page
    # no lightning layer
    page = msp.build_dew_html(state(_real_block(None, component_over={"enabled": False},
                                                watched=False)))
    t = tile(page, "Lightning (GLM)")
    assert "RAIN SENSOR TRUSTED" in page and "N/A" in t and GREY in t
    assert "no current lightning data: the GLM lightning layer is off" in page


def test_lightning_tile_survives_odd_values_and_other_limits():
    block = dew_block()
    block["thresholds"].update(glm_km=80.0, glm_hold_min=45.0)
    block["lightning"] = "nonsense"
    t = tile(msp.build_dew_html(state(block)), "Lightning (GLM)")
    assert "needs none within 80 km for 45 min" in t and "N/A" in t
    odd_values = (float("nan"), float("inf"), "55", True, [55], None, -30)
    for odd in odd_values:
        for key in ("recent_km", "recent_age_s", "nearest_km", "watched_s", "available"):
            for check in (False, None, True):
                block = dew_block(is_safe=False)
                block["checks"]["lightning"] = check
                # (a valid flash beside the odd value, so every branch is reached)
                block["lightning"].update(recent_km=55.0, recent_age_s=600, nearest_km=70.0)
                block["lightning"][key] = odd
                page = msp.build_dew_html(state(block))
                t = tile(page, "Lightning (GLM)")
                assert "needs none within 60 km" in t and "<script" not in page, (odd, key)
                assert "nan" not in t and "inf" not in t, (odd, key, check)


def test_state_of_a_daemon_from_before_the_lightning_check_shows_no_such_tile():
    # between `git pull` and the daemon's restart the new page reads the old daemon's
    # state: five checks, all true, IsSafe true. No grey sixth tile beside "IGNORED".
    block = dew_block()
    del block["checks"]["lightning"], block["lightning"]
    for key in ("glm_km", "glm_hold_min", "glm_max_age_min"):
        del block["thresholds"][key]
    page = msp.build_dew_html(state(block))
    assert "RAIN SENSOR IGNORED" in page and "Lightning" not in page
    assert page.count('<div class="tile">') == 5
    assert msp.build_dew_html(state()).count('<div class="tile">') == 6
    # six tiles sit three to a row (no orphan under a row of five), like the safety card
    three_columns = "minmax(max(170px,calc((100% - 25px) / 3)),1fr)"
    assert three_columns in msp.build_dew_html(state()) and three_columns not in page
    assert "repeat(auto-fit,minmax(170px,1fr))" in page


def test_stale_state_never_claims_ignored():
    page = msp.build_dew_html(state(ts=time.time() - 3600))
    assert "STALE" in page and "RAIN SENSOR IGNORED" not in page
    assert GREEN not in page and "state stale" in page
    assert "last asked" not in page and "dome driver: unknown" in page
    # a state dated in the FUTURE (clock stepped back, daemon gone) is not fresh either
    for ts in (time.time() + 3600, float("inf"), float("nan"), "now", True):
        page = msp.build_dew_html(state(ts=ts))
        assert "STALE" in page and "RAIN SENSOR IGNORED" not in page, ts
    undated = state()
    del undated["ts"]
    assert "STALE" in msp.build_dew_html(undated)
    # the limit itself: five minutes, as for the main card
    assert "RAIN SENSOR IGNORED" in msp.build_dew_html(state(ts=time.time() - 290))
    assert "STALE" in msp.build_dew_html(state(ts=time.time() - 310))


def test_only_a_real_true_reads_as_ignored_and_odd_blocks_still_render():
    for odd in ("false", 1, [1], "true"):
        page = msp.build_dew_html(state(dew_block(is_safe=odd)))
        assert "RAIN SENSOR IGNORED" not in page and "RAIN SENSOR TRUSTED" in page, odd
    page = msp.build_dew_html(state(dew_block(is_safe=False, reasons=5)))
    assert "RAIN SENSOR TRUSTED" in page                     # the card does not vanish


def test_driver_connection_wording():
    assert "dome driver not connected" in msp.build_dew_html(
        state(dew_block(connected=False, last_read_age_s=None)))
    assert "dome driver last asked 3 min ago" in msp.build_dew_html(
        state(dew_block(last_read_age_s=200)))
    assert msp.format_age_short(7500) == "2 h"


def test_no_card_without_a_dew_monitor():
    assert msp.build_dew_html(None) == ""
    assert msp.build_dew_html({"ts": time.time()}) == ""               # an older daemon
    assert msp.build_dew_html(state({"enabled": False, "is_safe": False})) == ""
    assert msp.build_dew_html(state({"enabled": True})) != ""          # sparse but valid


# --- weather log block ---------------------------------------------------------------------
def wlog(**over):
    block = {
        "enabled": True, "dir": "/var/www/html/weather_log", "interval_s": 300,
        "flush_hours": 24.0, "cap_mb": 6144.0,
        "current": [{"kind": "weather", "name": "weather_current.csv", "rows": 31, "waiting": 0},
                    {"kind": "stations", "name": "stations_current.csv", "rows": 1, "waiting": 0}],
        "archives": [
            {"name": "weather_2026-11.csv", "kind": "weather", "month": "2026-11", "bytes": 2_400_000},
            {"name": "stations_2026-11.csv", "kind": "stations", "month": "2026-11", "bytes": 512},
            {"name": "weather_2026-10.csv", "kind": "weather", "month": "2026-10", "bytes": 91_000}],
        "total_bytes": 2_491_512, "last_flush_ts": time.time() - 3600, "error": None,
        "ram_error": None, "dropped_rows": 0,
    }
    block.update(over)
    return block


def test_weather_log_links_are_relative_downloads(monkeypatch):
    monkeypatch.setattr(msp, "HTML_FILE", "/var/www/html/status.html")
    page = msp.build_weather_log_html(state(weather_log=wlog()))
    assert "<h2>Weather log</h2>" in page
    for name in ("weather_current.csv", "stations_current.csv", "weather_2026-11.csv",
                 "stations_2026-11.csv", "weather_2026-10.csv"):
        assert '<a href="weather_log/%s" download>%s</a>' % (name, name) in page
    assert "(31 rows)" in page and "(1 row)" in page
    assert "(2.4 MB)" in page and "(512 B)" in page and "(91 kB)" in page
    assert page.index("2026-11") < page.index("2026-10")               # newest first
    assert "one <b>weather</b> row every 5 min" in page
    assert "every 24 h, in" in page and "above 6 GB the oldest months" in page
    small = msp.build_weather_log_html(state(weather_log=wlog(cap_mb=500.0)))
    assert "above 500 MB the oldest months" in small
    assert "NOT being written" not in page


def test_weather_log_older_months_are_folded_away(monkeypatch):
    monkeypatch.setattr(msp, "HTML_FILE", "/var/www/html/status.html")
    months = ["2026-%02d" % m for m in range(1, 12)]
    archives = [{"name": "weather_%s.csv" % m, "kind": "weather", "month": m, "bytes": 10}
                for m in months]
    page = msp.build_weather_log_html(state(weather_log=wlog(archives=archives)))
    head, folded = page.split("<details>")
    assert "Older months (5)" in folded
    assert "weather_2026-11.csv" in head and "weather_2026-06.csv" in head
    assert "weather_2026-05.csv" in folded and "weather_2026-05.csv" not in head


def test_weather_log_problem_is_shown(monkeypatch):
    monkeypatch.setattr(msp, "HTML_FILE", "/var/www/html/status.html")
    page = msp.build_weather_log_html(state(weather_log=wlog(
        error="PermissionError: [Errno 13] Permission denied: '/var/www/html/weather_log'",
        archives=[], last_flush_ts=None, total_bytes=0)))
    assert "The archive is NOT being written" in page and "Permission denied" in page
    assert "No monthly file yet" in page and "Last copied: never yet" in page
    assert 'href="weather_log/weather_current.csv"' in page            # RAM rows still offered


def test_weather_log_outside_the_web_directory_is_named_not_linked(monkeypatch):
    monkeypatch.setattr(msp, "HTML_FILE", "/var/www/html/status.html")
    page = msp.build_weather_log_html(state(weather_log=wlog(dir="/home/kirx/weather_log")))
    assert "<a href" not in page and "weather_2026-11.csv" in page
    assert "/home/kirx/weather_log on the Pi" in page


def test_weather_log_ignores_names_that_are_not_plain_csv_files(monkeypatch):
    monkeypatch.setattr(msp, "HTML_FILE", "/var/www/html/status.html")
    bad = [{"name": '"><script>alert(1)</script>.csv', "kind": "weather",
            "month": "2026-11", "bytes": 1},
           {"name": "../../etc/passwd", "kind": "weather", "month": "2026-11", "bytes": 1},
           {"name": "weather_2026-11.csv", "kind": "weather", "month": "<b>x</b>", "bytes": 1}]
    page = msp.build_weather_log_html(state(weather_log=wlog(archives=bad)))
    assert "<script>" not in page and "passwd" not in page and "<b>x</b>" not in page


def test_weather_log_block_survives_an_odd_state(monkeypatch):
    monkeypatch.setattr(msp, "HTML_FILE", "/var/www/html/status.html")
    for odd in (True, 5, "x", {"a": 1}, None, [None, 3, "y"]):
        for key in ("current", "archives"):
            page = msp.build_weather_log_html(state(weather_log=wlog(**{key: odd})))
            assert "<h2>Weather log</h2>" in page, (key, odd)
    page = msp.build_weather_log_html(state(weather_log=wlog(archives=[
        {"name": "weather_2026-11.csv", "kind": "weather", "month": "../x", "bytes": 1}])))
    assert "../x" not in page and "No monthly file yet" in page


def test_no_weather_log_block_when_off_or_unknown():
    assert msp.build_weather_log_html(None) == ""
    assert msp.build_weather_log_html(state()) == ""                   # an older daemon
    assert msp.build_weather_log_html(
        state(weather_log={"enabled": False, "error": "disabled"})) == ""


def test_inputs_file_carries_the_enclosure_temperature(tmp_path, monkeypatch):
    monkeypatch.setattr(msp, "SAFETY_INPUTS_FILE", str(tmp_path / "inputs.json"))
    msp.write_safety_inputs({"sun_altitude_deg": -7.5}, 42.0, humidity_age_s=0.0,
                            temperature=13.0)
    with open(msp.SAFETY_INPUTS_FILE, encoding="utf-8") as f:
        d = json.load(f)
    assert d["temperature_c"] == 13.0 and d["humidity_pct"] == 42.0
    msp.write_safety_inputs({"sun_altitude_deg": -7.5}, 42.0)          # the old call still works
    with open(msp.SAFETY_INPUTS_FILE, encoding="utf-8") as f:
        assert json.load(f)["temperature_c"] is None
