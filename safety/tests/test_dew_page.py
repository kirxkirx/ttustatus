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

GREEN, RED = "var(--good)", "#b42318"
GREY = "var(--faint, #8b959c)"


def dew_block(is_safe=True, **over):
    block = {
        "enabled": True, "is_safe": is_safe, "reasons": [],
        "checks": {"device0": True, "spread": True, "wind": True, "cloud": True,
                   "precip": True},
        "spread_c": 4.44, "wind_mph": 4.6, "gust_mph": 7.0, "stations_used": 5,
        "stations_wind": 4, "wind_calm": True,
        "forecast": {"available": True, "cloud_now": 12, "cloud_next": 20,
                     "precip_now": 0, "precip_next": 1},
        "thresholds": {"spread_max_c": 5.0, "wind_max_mph": 8.0,
                       "wind_unsafe_above_mph": 9.0, "cloud_below_pct": 30.0,
                       "precip_below_pct": 5.0, "min_stations": 3},
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
                       "precip": None}
    t = tile(msp.build_dew_html(state(block)), "Safety monitor")
    assert "N/A" in t and "UNSAFE" not in t and GREY in t


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
