import json
import time

import pytest

from safety import config, wu_poll
from safety.eventlog import EventLog
from safety.monitor import RainPoller, SafetyMonitor


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A SafetyMonitor + RainPoller wired to temp files, with WU network stubbed out."""
    monkeypatch.setattr(config, "INPUTS_FILE", str(tmp_path / "inputs.json"))
    monkeypatch.setattr(config, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "LATCH_FILE", str(tmp_path / "latch.json"))
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    monkeypatch.setattr(config, "RADAR_LATCH_FILE", str(tmp_path / "radar_latch.json"))
    monkeypatch.setattr(config, "EVENT_LOG", str(tmp_path / "events.log"))
    monkeypatch.setattr(config, "WU_API_KEY", "testkey")   # enable rain polling in tests
    monkeypatch.setattr(wu_poll, "discover_stations",
                        lambda lat, lon: [("S1", 1.0), ("S2", 2.0)])
    monkeypatch.setattr(wu_poll, "poll_stations",
                        lambda stations, max_age_min=None: {
                            "live": len(stations), "total": len(stations),
                            "raining": [], "max_rate": 0.0, "results": []})
    log = EventLog(config.EVENT_LOG)
    poller = RainPoller(config, log)
    monitor = SafetyMonitor(config, log, poller)
    return {"cfg": config, "log": log, "poller": poller, "monitor": monitor}


@pytest.fixture
def write_inputs():
    def _w(cfg, sun=None, humidity=None, ts=None):
        if ts is None:
            ts = time.time()
        with open(cfg.INPUTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "sun_altitude_deg": sun, "humidity_pct": humidity}, f)
    return _w
