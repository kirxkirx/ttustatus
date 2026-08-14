"""Smoke tests for the two top-level scripts, with Raspberry Pi hardware/heavy libs
mocked so they run in CI (and on any machine without the Pi).

make_status_page.py imports board / adafruit_dht / gps / astropy / numpy at module load;
we stub those in sys.modules before importing it, then exercise its pure page-rendering
functions. safety_monitor.py needs no hardware — importing it verifies the daemon's whole
import graph wires up.
"""
import json
import os
import sys
import time
from unittest.mock import MagicMock

# Simulate the missing hardware / heavy libraries (only used by make_status_page.py).
for _m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
           "astropy.time", "astropy.coordinates", "astropy.utils", "astropy.utils.iers"]:
    sys.modules.setdefault(_m, MagicMock())

import make_status_page as msp        # noqa: E402  (must follow the sys.modules stubbing)
import safety_monitor                 # noqa: E402


def _safe_state():
    comp = {"sun": {"value_deg": -7.5, "threshold_deg": 0.0, "safe": True},
            "humidity": {"value_pct": 42.0, "threshold_pct": 95.0, "safe": True},
            "rain": {"safe": True, "enabled": True, "latched": False,
                     "polling_active": True, "stations_live": 7, "stations_total": 10}}
    return {"ts": time.time(), "is_safe": True, "reasons": [], "components": comp,
            "alpaca": {"address": "10.0.0.1", "port": 11111, "device_number": 0,
                       "issafe_path": "/api/v1/safetymonitor/0/issafe"},
            "events_tail": ["2026-01-01T00:00:00  INIT  (safe)"]}


def test_daemon_entrypoint_imports():
    assert callable(safety_monitor.main)


def test_write_safety_inputs_roundtrip(tmp_path):
    msp.SAFETY_INPUTS_FILE = str(tmp_path / "inputs.json")
    msp.write_safety_inputs({"sun_altitude_deg": -7.5}, 42.0)
    d = json.load(open(msp.SAFETY_INPUTS_FILE))
    assert d["humidity_pct"] == 42.0 and d["sun_altitude_deg"] == -7.5


def test_read_safety_state_missing(tmp_path):
    msp.SAFETY_STATE_FILE = str(tmp_path / "nope.json")
    assert msp.read_safety_state() is None


def test_safety_card_safe_state():
    html = msp.build_safety_html(_safe_state())
    assert "Alpaca safety monitor" in html
    assert "10.0.0.1:11111" in html and "/api/v1/safetymonitor/0/issafe" in html
    assert "Inputs it is using" in html and "Log (latest events)" in html
    assert "#1a7f37" in html                     # green SAFE badge


def test_safety_card_offline_is_not_green():
    html = msp.build_safety_html(None)
    assert "OFFLINE" in html and "#1a7f37" not in html


def test_nws_forecast_section_and_tile_render():
    nws = {"safe": False, "available": True, "stale": False,
           "reasons": ["NWS cloud cover 85% > 70% (next hour)"],
           "grid": "LUB/46,41", "update_time": "2026-01-01T00:00:00+00:00",
           "now_hour": {"cloud_cover_pct": 85, "precip_prob_pct": 20, "thunder_prob_pct": 0},
           "next_hour": {"cloud_cover_pct": 90, "precip_prob_pct": 30, "thunder_prob_pct": 5},
           "hours": [{"local": "Mon 00:00", "cloud_cover_pct": 85, "precip_prob_pct": 20,
                      "thunder_prob_pct": 0, "temp_f": 70, "wind_speed_kmh": 16.0,
                      "wind_dir_deg": 200}]}
    comp = {"sun": {"value_deg": -7.5, "threshold_deg": 0.0, "safe": True},
            "humidity": {"value_pct": 42.0, "threshold_pct": 95.0, "safe": True},
            "rain": {"safe": True, "enabled": True, "latched": False, "polling_active": True,
                     "stations_live": 7, "stations_total": 10},
            "nws": nws}
    comp["connectivity"] = {"probed": True, "online": False, "safe": False, "offline_min": 75}
    tiles = msp.build_safety_tiles_html(comp)
    assert "NWS (now/next)" in tiles                   # relabelled tile
    assert "C85%/P20%/T0%" in tiles and "C90%/P30%/T5%" in tiles   # both hours packed
    assert "no&nbsp;rain" in tiles                     # rain wording fixed
    assert "Lightning (GLM" in tiles                   # GLM tile present (off by default here)
    assert "Internet" not in tiles                     # connectivity has no always-on tile
    fc = msp.build_forecast_html({"ts": time.time(), "components": {"nws": nws}})
    assert "12-hour forecast" in fc and "Mon 00:00" in fc and "NWS" in fc
    assert "mph" in fc                                 # wind column present
    # no forecast data -> section omitted, no crash
    assert msp.build_forecast_html({"components": {"nws": {"hours": []}}}) == ""


def test_radar_section_renders():
    now = time.time()
    # thumb_path must be co-located with make_status_page.HTML_FILE for the <img> to show
    thumb = os.path.join(os.path.dirname(msp.HTML_FILE), "ttu_radar.png")
    base = {"trigger_km": 50, "enabled": True, "available": True,
            "thumb_available": True, "thumb_path": thumb,
            "attribution": "© OpenStreetMap contributors, © CARTO", "frame_utc": "2026-01-01"}
    # fresh state + rain in ring -> unsafe wording + image + attribution + source
    st = {"ts": now, "components": {"radar": {**base, "in_ring": True, "nearest_km": 18.0}}}
    h = msp.build_radar_html(st)
    assert "Radar (MRMS" in h and "RAIN within 50" in h and "ttu_radar.png" in h
    assert "CARTO" in h and "MRMS" in h and "Iowa Environmental" in h   # source + attribution
    # clear ring (fresh)
    st2 = {"ts": now, "components": {"radar": {**base, "in_ring": False}}}
    assert "no rain within 50" in msp.build_radar_html(st2)
    # day + night maps -> both <img> emitted with switch classes
    day = os.path.join(os.path.dirname(msp.HTML_FILE), "ttu_radar_day.png")
    st_both = {"ts": now, "components": {"radar": {**base, "in_ring": False,
                                                   "thumb_path_day": day}}}
    hb = msp.build_radar_html(st_both)
    assert "radar-night" in hb and "radar-day" in hb and "ttu_radar_day.png" in hb
    # stale state -> never a green "no rain"; shows stale
    st3 = {"ts": now - 99999, "components": {"radar": {**base, "in_ring": False}}}
    assert "stale" in msp.build_radar_html(st3) and "no rain within 50" not in msp.build_radar_html(st3)
    # disabled -> section omitted
    assert msp.build_radar_html({"components": {"radar": {"enabled": False}}}) == ""


def test_paused_tiles_never_claim_no_rain_or_stale_distance():
    # Daytime pause: WU must NOT say "no rain" and GLM must NOT show a stale distance —
    # both must display the no-data state instead (grey dot).
    comp = {"sun": {"value_deg": 41.5, "threshold_deg": 0.0, "safe": False},
            "humidity": {"value_pct": 40.0, "threshold_pct": 95.0, "safe": True},
            "rain": {"safe": True, "enabled": True, "latched": False,
                     "polling_active": False, "stations_live": 0, "stations_total": 0},
            "nws": {"available": False},
            "glm": {"enabled": True, "available": True, "latched": False,
                    "polling_active": False, "nearest_km": 158.0, "trigger_km": 50,
                    "safe": True}}
    tiles = msp.build_safety_tiles_html(comp)
    assert "no&nbsp;rain" not in tiles           # the misleading claim is gone
    assert "paused" in tiles and "not polling (daytime)" in tiles
    assert "158" not in tiles                    # stale GLM distance hidden while paused
    # polling but zero stations reporting -> "no data", not "no rain"
    comp["rain"].update(polling_active=True, stations_live=0, stations_total=10)
    tiles2 = msp.build_safety_tiles_html(comp)
    assert "no&nbsp;data" in tiles2 and "no&nbsp;rain" not in tiles2


def test_glm_tile_latched_render():
    comp = {"sun": {"value_deg": -7.5, "threshold_deg": 0.0, "safe": True},
            "humidity": {"value_pct": 42.0, "threshold_pct": 95.0, "safe": True},
            "rain": {"safe": True, "enabled": True, "latched": False, "polling_active": True,
                     "stations_live": 7, "stations_total": 10},
            "glm": {"enabled": True, "available": True, "latched": True,
                    "seconds_remaining": 3600, "trigger_km": 50, "nearest_km": 12.0,
                    "nearest_bearing": "N", "polling_active": True, "safe": False}}
    tiles = msp.build_safety_tiles_html(comp)
    assert "STRIKE" in tiles and "Lightning (GLM" in tiles


def test_dng_batch_processing_deletes_as_it_goes(tmp_path, monkeypatch):
    # simulate the RAM-disk batch pipeline: 23 DNGs -> 3 batch means (10+10+3),
    # with every DNG and per-batch TIFF deleted along the way
    monkeypatch.setattr(msp, "STACK_DIR", str(tmp_path))
    dngs = []
    for i in range(23):
        p = tmp_path / ("frame%04d.dng" % i)
        p.write_bytes(b"x")
        dngs.append(str(p))

    def fake_run(cmd, timeout):
        if cmd[0] == "dcraw":                      # "convert": create the .tiff
            open(os.path.splitext(cmd[-1])[0] + ".tiff", "wb").write(b"t")
        else:                                       # "magick": create the output file
            open(cmd[-1], "wb").write(b"m")
        return True

    monkeypatch.setattr(msp, "run_subprocess", fake_run)
    monkeypatch.setattr(msp, "get_imagemagick_cmd", lambda: "magick")
    means, ok = msp.process_dngs_in_batches(dngs)
    assert ok is True and len(means) == 3
    leftovers = [f for f in os.listdir(tmp_path)
                 if f.endswith(".dng") or (f.endswith(".tiff")
                                           and not f.startswith("mean"))]
    assert leftovers == []                          # everything transient was deleted


def test_rain_tile_disabled_without_key():
    comp = {"sun": {"value_deg": -7.0, "threshold_deg": 0.0, "safe": True},
            "humidity": {"value_pct": 40.0, "threshold_pct": 95.0, "safe": True},
            "rain": {"safe": True, "enabled": False, "latched": False,
                     "polling_active": False}}
    tiles = msp.build_safety_tiles_html(comp)
    assert "disabled" in tiles and "WU_API_KEY" in tiles
