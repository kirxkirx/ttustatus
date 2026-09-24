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


# --- hazards on /setup and through the Alpaca API -------------------------------------
class _Stub:
    def __init__(self, comp):
        self.comp = comp

    def component(self, now=None):
        if isinstance(self.comp, BaseException):
            raise self.comp
        return self.comp


def _hz(veto=(), at_site=(), nearby=(), available=True, enabled=True, error=None):
    veto, at_site, nearby = list(veto), list(at_site), list(nearby)
    return {"safe": not veto, "enabled": enabled, "available": available,
            "veto_events": ["Tornado Warning", "Dust Storm Warning", "High Wind Warning"],
            "veto": veto, "at_site": at_site, "nearby": nearby,
            "counts": {"at_site": len(at_site), "nearby": len(nearby)},
            "point_age_s": 12, "area_age_s": 70, "error": error,
            "source": "NWS api.weather.gov active alerts"}


_TOR_VETO = {"key": "KLUB.TO.W.0012", "event": "Tornado Warning",
             "headline": "Tornado Warning <script>alert(1)</script>",
             "sender": "NWS Lubbock TX", "end_ts": time.time() + 1800,
             "end_local": "18:45 CDT", "first_seen_ts": time.time() - 60, "source": "both"}


def _view(event, color, vetoes=False, threat=None, headline=None):
    return {"key": "k-" + event, "event": event, "kind": "warning", "severity": "Extreme",
            "urgency": "Immediate", "headline": headline or event + " for Lubbock",
            "nws_headline": None, "sender": "NWS Lubbock TX", "area_desc": "Lubbock, TX",
            "onset": None, "ends": None, "expires": None, "end_local": "18:45 CDT",
            "color": color, "threat": threat, "vetoes": vetoes, "geometry_source": "polygon",
            "description": "", "instruction": ""}


def _info(**kw):
    d = {"safe": True, "info_only": True, "enabled": True,
         "feeds": {"usgs": {"ok": True, "error": None, "age_s": 120, "count": 1},
                   "hms_smoke": {"ok": False, "error": "HTTP Error 404", "age_s": None,
                                 "count": 0}},
         "quakes": [{"text": "M3.1 · 12 km E of Snyder · 140 km"}],
         "smoke": {"text": "Light smoke over the site (17-23 UTC)"},
         "fires": [], "spc": {"category": "MRGL", "label": "Marginal risk", "mds": []},
         "lsr": [], "space_weather": {"text": "Kp 3 (max 24 h: 4) · G0 R0 S0"}}
    d.update(kw)
    return d


def test_setup_shows_the_veto_and_escapes_everything(tmp_path, monkeypatch):
    c, mon = _client(tmp_path, monkeypatch)
    at_site = [_view("Tornado Warning", "#FF0000", vetoes=True, threat="TORNADO EMERGENCY",
                     headline=_TOR_VETO["headline"])]
    nearby = [_view("Flood Watch", 'red" onmouseover="x')]      # invalid colour
    mon.hazards = _Stub(_hz(veto=[_TOR_VETO], at_site=at_site, nearby=nearby))
    mon.hazard_feeds = _Stub(_info(quakes=[{"text": "<b>M3.1</b>"}]))
    page = c.get("/setup").get_data(as_text=True)
    assert "UNSAFE" in page
    assert "VETO — Tornado Warning over the site until 18:45 CDT" in page
    assert "NWS Tornado Warning in effect for the site until" in page   # the reason
    assert "TORNADO EMERGENCY" in page and "background:#FF0000" in page
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "onmouseover" not in page                   # an invalid colour never reaches CSS
    assert "<b>M3.1</b>" not in page and "&lt;b&gt;M3.1&lt;/b&gt;" in page
    assert "NWS alerts at the site" in page and "NWS alerts nearby (on the map)" in page
    assert "information only" in page
    assert "Tornado Warning, Dust Storm Warning, High Wind Warning" in page


def test_issafe_false_while_a_warning_vetoes(tmp_path, monkeypatch):
    c, mon = _client(tmp_path, monkeypatch)
    c.put("/api/v1/safetymonitor/0/connected", data={"Connected": "true"})
    assert c.get("/api/v1/safetymonitor/0/issafe").get_json()["Value"] is True
    mon.hazards = _Stub(_hz(veto=[_TOR_VETO]))
    assert c.get("/api/v1/safetymonitor/0/issafe").get_json()["Value"] is False
    mon.hazards = _Stub(_hz())                              # warning gone (fresh data)
    assert c.get("/api/v1/safetymonitor/0/issafe").get_json()["Value"] is True


def test_issafe_ignores_hazard_information_even_when_it_raises(tmp_path, monkeypatch):
    c, mon = _client(tmp_path, monkeypatch)
    c.put("/api/v1/safetymonitor/0/connected", data={"Connected": "true"})
    for comp in (_info(safe=False, veto=[_TOR_VETO]), RuntimeError("feeds exploded"), None):
        mon.hazard_feeds = _Stub(comp)
        body = c.get("/api/v1/safetymonitor/0/issafe").get_json()
        assert body["ErrorNumber"] == 0 and body["Value"] is True, comp


def test_setup_hazard_rows_when_unavailable_or_disabled(tmp_path, monkeypatch):
    c, mon = _client(tmp_path, monkeypatch)
    page = c.get("/setup").get_data(as_text=True)          # no pollers at all
    assert "High Wind Warning)</td><td>disabled" in page
    mon.hazards = _Stub(_hz(available=False, error="HTTP Error 503: Service Unavailable"))
    mon.hazard_feeds = _Stub(RuntimeError("feeds exploded"))
    r = c.get("/setup")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "unavailable / stale (not gating)" in page and "HTTP Error 503" in page
    assert "feeds exploded" in page
    assert '<span class="badge">SAFE</span>' in page       # unavailable != unsafe


def test_setup_survives_oddly_shaped_hazard_info(tmp_path, monkeypatch):
    c, mon = _client(tmp_path, monkeypatch)
    mon.hazards = _Stub(_hz(at_site=["not a dict", _view("Wind Advisory", "#D2B48C")]))
    mon.hazard_feeds = _Stub(_info(feeds={"usgs": "?", 3: None}, quakes="many",
                                   fires=[{"acres": 12, "flag": True}, "Yellow Lake"],
                                   spc=["MRGL"], lsr=[{}] * 9, smoke=["x"],
                                   space_weather=7))
    r = c.get("/setup")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "Wind Advisory" in page and "background:#D2B48C" in page
    # nine reports without a displayable text are counted, never called "none"; the
    # feed status is unknown, so what is shown is labelled last-known
    assert "Yellow Lake" in page and "Storm reports: 9 item(s) without a displayable" in page
    assert "last known, no current data (status unknown)" in page
    odd = dict(_hz(nearby=[_view("Dust Advisory", "#BDB76B\n")]), counts=[1],
               veto_events="Tornado Warning")
    mon.hazards = _Stub(odd)
    r = c.get("/setup")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "(veto: Tornado Warning)" in page and "Dust Advisory" in page
    assert "#BDB76B" not in page                    # not a strict #RRGGBB: no swatch


def test_status_json_carries_both_hazard_components(tmp_path, monkeypatch):
    c, mon = _client(tmp_path, monkeypatch)
    mon.hazards = _Stub(_hz(veto=[_TOR_VETO]))
    mon.hazard_feeds = _Stub(_info(odd={1, 2}))              # a set: not JSON by itself
    body = c.get("/status").get_json()
    assert body["is_safe"] is False
    assert body["components"]["hazards"]["veto"][0]["event"] == "Tornado Warning"
    assert body["components"]["hazard_info"]["info_only"] is True
