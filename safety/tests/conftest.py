import json
import time

import pytest

from safety import config, housekeeping, timesync, wu_poll
from safety.eventlog import EventLog
from safety.monitor import RainPoller, SafetyMonitor


@pytest.fixture(autouse=True)
def _private_run_lock(tmp_path, monkeypatch):
    """server.main() takes the single-instance lock and sweeps leftover temp files: in the
    tests both stay in a private directory, never the real home or /dev/shm (and two test
    runs side by side never see each other's lock)."""
    monkeypatch.setattr(config, "LOCK_FILE", str(tmp_path / "run.lock"))
    monkeypatch.setattr(housekeeping, "SHM_DIR", str(tmp_path / "shm"))


@pytest.fixture(autouse=True)
def _no_real_chronyc(monkeypatch):
    """No test asks the test machine's chrony. A test that starts the daemon
    (server.main) would otherwise depend on that machine's clock state — synchronised on
    a workstation, "chronyc not installed" on CI. Every ClockSyncWatch made without a
    runner gets this one; the clock-check tests pass their own."""
    def _absent(cfg):
        raise FileNotFoundError("chronyc (not asked in tests)")
    monkeypatch.setattr(timesync, "run_chronyc", _absent)


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
    # the state now shows CONFIG_WARNINGS: start each monitor test without the ones that
    # earlier tests of the config parsers left behind
    monkeypatch.setattr(config, "CONFIG_WARNINGS", [])
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
