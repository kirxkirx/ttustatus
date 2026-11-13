"""A GLM poll flagged partial (glm_lightning: the poll did not read its whole window) is
shown as "partial window" with a grey dot on the status page and /setup, never as the
green "no strike" of a checked and clear ring. Not a veto either way."""
import json
import sys
import time
from unittest.mock import MagicMock

from safety import config, glm_lightning, wu_poll
from safety.alpaca import create_app
from safety.eventlog import EventLog
from safety.monitor import RainPoller, SafetyMonitor

# The page imports the Raspberry Pi hardware / heavy libraries at module load; stub them
# exactly as test_smoke.py does (setdefault: a real installed module is left alone).
for _m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
           "astropy.time", "astropy.coordinates", "astropy.utils", "astropy.utils.iers"]:
    sys.modules.setdefault(_m, MagicMock())

import make_status_page as msp        # noqa: E402  (must follow the sys.modules stubbing)

GREY = "var(--faint, #8b959c)"         # safety_dot_html(..., unknown=True)
GREEN = "var(--good)"


def _glm(partial, nearest=None):
    comp = dict(glm_lightning.unavailable_component(config))
    comp.update(enabled=True, available=True, polling_active=True, safe=True,
                latched=False, nearest_km=nearest, nearest_bearing="NW" if nearest else None,
                partial=partial,
                coverage="2 min of the window unread (03:10-03:12 UTC)" if partial else None)
    return comp


def _tile(tiles, label="Lightning (GLM)"):
    for chunk in tiles.split('<div class="tile">'):
        if '<div class="k">%s</div>' % label in chunk:
            return chunk
    return ""


def _comp(glm):
    return {"sun": {"value_deg": -12.0, "threshold_deg": 0.0, "safe": True},
            "humidity": {"value_pct": 40.0, "threshold_pct": 95.0, "safe": True},
            "rain": {"safe": True, "enabled": True, "latched": False,
                     "polling_active": True, "stations_live": 7, "stations_total": 10},
            "glm": glm}


def test_page_tile_shows_a_partial_poll_grey_not_green():
    tile = _tile(msp.build_safety_tiles_html(_comp(_glm(True))))
    assert "partial window" in tile and GREY in tile
    assert "no strike" not in tile and GREEN not in tile
    clear = _tile(msp.build_safety_tiles_html(_comp(_glm(False))))
    assert "no strike" in clear and GREEN in clear


class _Glm:
    def __init__(self, comp):
        self.comp = comp

    def component(self, sun_alt, now=None):
        return dict(self.comp)


def test_setup_row_shows_a_partial_poll_grey_and_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INPUTS_FILE", str(tmp_path / "i.json"))
    monkeypatch.setattr(config, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(config, "LATCH_FILE", str(tmp_path / "l.json"))
    monkeypatch.setattr(config, "EVENT_LOG", str(tmp_path / "e.log"))
    monkeypatch.setattr(config, "WU_API_KEY", "testkey")
    monkeypatch.setattr(wu_poll, "discover_stations", lambda a, b: [("S1", 1.0)])
    monkeypatch.setattr(wu_poll, "poll_stations", lambda s, max_age_min=None: {
        "live": 1, "total": 1, "raining": [], "max_rate": 0.0, "results": []})
    with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "sun_altitude_deg": -10.0, "humidity_pct": 40.0}, f)
    log = EventLog(config.EVENT_LOG)
    mon = SafetyMonitor(config, log, RainPoller(config, log))
    mon.glm = _Glm(_glm(True, nearest=120.0))
    page = create_app(mon, config).test_client().get("/setup").get_data(as_text=True)
    row = [r for r in page.split("<tr>") if "Lightning (GLM" in r][0]
    assert "partial window" in row and "nearest 120.0 km NW" in row
    assert "2 min of the window unread" in row
    assert "#8b959c" in row                     # grey: no proof of a clear ring
    assert mon.evaluate()["components"]["glm"]["safe"] is True     # never a veto
