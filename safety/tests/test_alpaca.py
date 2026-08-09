import json
import time

from safety import config, wu_poll
from safety.alpaca import create_app
from safety.eventlog import EventLog
from safety.monitor import RainPoller, SafetyMonitor


def _client(tmp_path, monkeypatch, sun=-10.0, humidity=40.0):
    monkeypatch.setattr(config, "INPUTS_FILE", str(tmp_path / "i.json"))
    monkeypatch.setattr(config, "STATE_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(config, "LATCH_FILE", str(tmp_path / "l.json"))
    monkeypatch.setattr(config, "EVENT_LOG", str(tmp_path / "e.log"))
    monkeypatch.setattr(config, "WU_API_KEY", "testkey")
    monkeypatch.setattr(wu_poll, "discover_stations", lambda a, b: [("S1", 1.0)])
    monkeypatch.setattr(wu_poll, "poll_stations", lambda s, max_age_min=None: {
        "live": 1, "total": 1, "raining": [], "max_rate": 0.0, "results": []})
    with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "sun_altitude_deg": sun,
                   "humidity_pct": humidity}, f)
    log = EventLog(config.EVENT_LOG)
    mon = SafetyMonitor(config, log, RainPoller(config, log))
    app = create_app(mon, config)
    app.testing = True
    return app.test_client(), mon


def test_configured_devices_lists_safetymonitor(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    body = c.get("/management/v1/configureddevices").get_json()
    assert body["ErrorNumber"] == 0
    assert body["Value"][0]["DeviceType"] == "SafetyMonitor"
    assert body["Value"][0]["DeviceNumber"] == 0


def test_envelope_fields_and_client_txn(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    body = c.get("/api/v1/safetymonitor/0/name?ClientTransactionID=42").get_json()
    for key in ("ClientTransactionID", "ServerTransactionID",
                "ErrorNumber", "ErrorMessage", "Value"):
        assert key in body
    assert body["ClientTransactionID"] == 42


def test_issafe_requires_connected(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    body = c.get("/api/v1/safetymonitor/0/issafe").get_json()
    assert body["ErrorNumber"] == 0x407     # NotConnected


def test_connect_then_issafe_true(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch, sun=-10.0, humidity=40.0)
    assert c.put("/api/v1/safetymonitor/0/connected",
                 data={"Connected": "true"}).get_json()["ErrorNumber"] == 0
    body = c.get("/api/v1/safetymonitor/0/issafe").get_json()
    assert body["ErrorNumber"] == 0
    assert body["Value"] is True


def test_issafe_false_when_sun_up(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch, sun=5.0, humidity=40.0)
    c.put("/api/v1/safetymonitor/0/connected", data={"Connected": "true"})
    assert c.get("/api/v1/safetymonitor/0/issafe").get_json()["Value"] is False


def test_interfaceversion_and_setup(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    assert c.get("/api/v1/safetymonitor/0/interfaceversion").get_json()["Value"] == 2
    r = c.get("/setup/v1/safetymonitor/0/setup")
    assert r.status_code == 200
    assert b"Safety" in r.data or b"SAFE" in r.data


def test_wrong_device_number(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    body = c.get("/api/v1/safetymonitor/1/issafe").get_json()
    assert body["ErrorNumber"] == 0x400     # NotImplemented / no such device
