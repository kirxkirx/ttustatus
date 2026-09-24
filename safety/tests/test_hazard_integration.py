"""Hazard layers wired into the daemon: server.main() creates the NWS-alerts poller (the
narrow warning veto) and the INFORMATION-ONLY hazard-feeds poller only when enabled, runs
each in its own thread, hands their overlays() to the radar map, and includes them in the
clock-step fan-out. Plus end-to-end checks through the real modules (network blocked):
a Tornado Warning over the site vetoes IsSafe with the contract's reason text, a Red Flag
Warning at the site does not, and failing information feeds never touch IsSafe.

The CAP fixtures are trimmed from real api.weather.gov payloads fetched 2026-09-24: a
Tornado Warning (NWS Charleston WV, /O.NEW.KRLX.TO.W.0040/, 2026-09-21) and a Red Flag
Warning (NWS Reno NV), moved over the site with times shifted to "now"; the zone outline
is the real TXZ035 / TXC303 (Lubbock) polygon, and the /points answer is the real one.
"""
import io
import json
import socket
import sys
import time
import types
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from safety import config, server
from safety import monitor as monitor_mod
from safety.eventlog import EventLog
from safety.monitor import SafetyMonitor

SITE = (33.748, -101.958)


# --- stubs --------------------------------------------------------------------------
class _Halt(BaseException):
    """Ends a daemon loop from inside a stub (the loops catch Exception, not this)."""


class _StubLayer:
    """Stands in for NwsAlertsPoller / HazardFeedsPoller / RadarPoller."""
    created = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.polls, self.steps = [], []
        _StubLayer.created.append(self)

    def maybe_poll(self, *args):
        self.polls.append(args)
        raise _Halt()

    def clock_stepped(self, pre, post):
        self.steps.append((pre, post))

    def overlays(self):
        return [{"kind": "alert", "key": "k", "event": "Tornado Warning",
                 "color": "#FF0000", "geometry": None, "rank": 0, "vetoes": True}]

    def component(self, *args, **kwargs):
        return {"safe": True}


class _Alerts(_StubLayer):
    pass


class _Feeds(_StubLayer):
    pass


class _Radar(_StubLayer):
    pass


class _Monitor(SafetyMonitor):
    def evaluate(self):
        raise _Halt()                   # the evaluator's first pass ends the test loop


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """server.main() with every external effect stubbed; returns a runner that yields
    what main() built."""
    for k, name in (("EVENT_LOG", "events.log"), ("LATCH_FILE", "latch.json"),
                    ("INPUTS_FILE", "inputs.json"), ("STATE_FILE", "state.json"),
                    ("HAZARD_LATCH_FILE", "hazard_latch.json")):
        monkeypatch.setattr(config, k, str(tmp_path / name))
    monkeypatch.setattr(config, "HAZARD_CACHE_DIR", str(tmp_path / "zones"))
    for flag in ("NWS_ENABLED", "GLM_ENABLED", "CONN_ENABLED", "PAGE_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "RADAR_ENABLED", True)
    monkeypatch.setattr(server, "nws_alerts", types.SimpleNamespace(NwsAlertsPoller=_Alerts))
    monkeypatch.setattr(server, "hazard_feeds",
                        types.SimpleNamespace(HazardFeedsPoller=_Feeds))
    monkeypatch.setattr(server, "RadarPoller", _Radar)
    monkeypatch.setattr(server, "SafetyMonitor", _Monitor)
    monkeypatch.setattr(server.discovery, "start", lambda port: None)
    monkeypatch.setitem(sys.modules, "waitress",
                        types.SimpleNamespace(serve=lambda app, **kw: None))
    built = {}
    monkeypatch.setattr(server, "create_app",
                        lambda monitor, cfg: built.setdefault("monitor", monitor))
    # a wall clock that STEPS by an hour after the first reading (monotonic does not)
    t0 = 1_790_000_000.0
    reads = []

    def fake_time():
        reads.append(1)
        return t0 if len(reads) == 1 else t0 + 3600.0
    monkeypatch.setattr(server, "time", types.SimpleNamespace(
        time=fake_time, monotonic=lambda: 500.0))

    def start(target, name):
        built.setdefault("threads", []).append(name)
        if name in ("evaluator", "radar-poller", "hazard-alerts", "hazard-feeds"):
            try:
                target()                # one pass; the stubs end it with _Halt
            except _Halt:
                pass
    monkeypatch.setattr(server, "_start_thread", start)

    def run(hazards=True, feeds=True):
        _StubLayer.created = []
        built.clear()
        reads.clear()
        monkeypatch.setattr(config, "HAZARDS_ENABLED", hazards)
        monkeypatch.setattr(config, "HAZARD_FEEDS_ENABLED", feeds)
        server.main()
        by = {type(o): o for o in _StubLayer.created}
        built.update(t0=t0, alerts=by.get(_Alerts), feeds=by.get(_Feeds),
                     radar=by.get(_Radar), created=list(_StubLayer.created))
        return built
    return run


# --- server wiring ------------------------------------------------------------------
def test_main_wires_both_hazard_layers(daemon):
    b = daemon()
    alerts, feeds, radar, mon = b["alerts"], b["feeds"], b["radar"], b["monitor"]
    assert alerts.args == (config, alerts.args[1]) and isinstance(alerts.args[1], EventLog)
    assert feeds.args == (config,)
    # the monitor gets both; only `hazards` can veto (see test_monitor)
    assert mon.hazards is alerts and mon.hazard_feeds is feeds
    # the radar map draws both layers' overlays, as zero-arg callables
    assert radar.kwargs["overlay_sources"] == [alerts.overlays, feeds.overlays]
    assert all(callable(f) and f() for f in radar.kwargs["overlay_sources"])
    # each layer has its own thread, which polls it with the current time
    assert "hazard-alerts" in b["threads"] and "hazard-feeds" in b["threads"]
    assert alerts.polls and isinstance(alerts.polls[0][0], float)
    assert feeds.polls and isinstance(feeds.polls[0][0], float)


def test_clock_step_fans_out_to_the_hazard_layers(daemon):
    b = daemon()
    step = [(b["t0"], b["t0"] + 3600.0)]
    assert b["alerts"].steps == step, "NWS alerts poller missed the clock step"
    assert b["feeds"].steps == step, "hazard feeds poller missed the clock step"
    assert b["radar"].steps == step


def test_no_hazard_threads_or_overlays_when_disabled(daemon):
    b = daemon(hazards=False, feeds=False)
    assert b["alerts"] is None and b["feeds"] is None      # never even constructed
    assert "hazard-alerts" not in b["threads"] and "hazard-feeds" not in b["threads"]
    assert b["radar"].kwargs["overlay_sources"] is None
    assert b["monitor"].hazards is None and b["monitor"].hazard_feeds is None


def test_each_hazard_layer_is_enabled_independently(daemon):
    b = daemon(hazards=True, feeds=False)
    assert b["feeds"] is None and "hazard-feeds" not in b["threads"]
    assert b["radar"].kwargs["overlay_sources"] == [b["alerts"].overlays]
    b = daemon(hazards=False, feeds=True)
    assert b["alerts"] is None and "hazard-alerts" not in b["threads"]
    assert b["radar"].kwargs["overlay_sources"] == [b["feeds"].overlays]


def test_hazard_threads_run_without_the_radar(daemon, monkeypatch):
    monkeypatch.setattr(config, "RADAR_ENABLED", False)
    b = daemon()
    assert b["radar"] is None and "radar-poller" not in b["threads"]
    assert "hazard-alerts" in b["threads"] and b["monitor"].hazards is b["alerts"]


def test_a_broken_hazard_module_is_reported_and_skipped(daemon, monkeypatch):
    # import failure (partial deploy) and a constructor that raises: the daemon still
    # starts, says so LOUDLY in the audit log, and the other layer keeps working
    monkeypatch.setattr(server, "nws_alerts", None)
    monkeypatch.setattr(server, "NWS_ALERTS_IMPORT_ERROR", "SyntaxError: invalid syntax")

    class _Boom:
        def __init__(self, cfg):
            raise OSError("cache dir not writable")
    monkeypatch.setattr(server, "hazard_feeds", types.SimpleNamespace(HazardFeedsPoller=_Boom))
    b = daemon()
    assert b["alerts"] is None and b["feeds"] is None
    assert "hazard-alerts" not in b["threads"] and "hazard-feeds" not in b["threads"]
    with open(config.EVENT_LOG, encoding="utf-8") as f:
        log = f.read()
    assert "NWS alerts layer failed: ImportError: SyntaxError: invalid syntax" in log
    assert "no veto for Tornado Warning, Dust Storm Warning, High Wind Warning" in log
    assert "hazard information layer failed: OSError: cache dir not writable" in log
    # ...and the state file says it too: an ENABLED layer that failed to start must not
    # read as one switched off on purpose (TTU_SAFETY_HAZARDS=0)
    mon = b["monitor"]
    assert mon.hazard_feeds_error == "OSError: cache dir not writable"
    hz = SafetyMonitor._hazards_component(mon, time.time())
    info = SafetyMonitor._hazard_info_component(mon, time.time())
    assert hz["enabled"] is True and hz["safe"] is True and hz["available"] is False
    assert "failed to start" in hz["error"] and "no warning veto" in hz["error"]
    assert info["enabled"] is True and "cache dir not writable" in info["error"]


def test_a_layer_whose_constructor_raises_is_not_shown_as_switched_off(daemon, monkeypatch):
    # (the page's rendering of this component: test_hazards_page)
    class _Boom:
        def __init__(self, cfg, eventlog):
            raise RuntimeError("simulated constructor bug")
    monkeypatch.setattr(server, "nws_alerts", types.SimpleNamespace(NwsAlertsPoller=_Boom))
    mon = daemon()["monitor"]
    hz = SafetyMonitor._hazards_component(mon, time.time())
    assert hz["enabled"] is True and hz["safe"] is True
    assert "simulated constructor bug" in hz["error"] and "no warning veto" in hz["error"]


def test_empty_veto_list_is_announced(daemon, monkeypatch):
    monkeypatch.setattr(config, "HAZARD_VETO_EVENTS", ())
    daemon()
    with open(config.EVENT_LOG, encoding="utf-8") as f:
        assert "no hazard veto events configured" in f.read()


def test_overlay_sources_skips_absent_layers():
    a, f = _Alerts(), object()
    assert server.overlay_sources(None, a, f) == [a.overlays]
    assert server.overlay_sources(None, None) == []


# --- end-to-end through the real modules (network blocked) -------------------------
_CDT = timezone(timedelta(hours=-5))
# the real TXZ035 / TXC303 (Lubbock) outline from api.weather.gov/zones
_LUBBOCK = {"type": "Polygon", "coordinates": [[
    [-101.556595, 33.394711], [-101.625397, 33.394611], [-101.711594, 33.393913],
    [-101.750397, 33.393311], [-101.868393, 33.392014], [-101.876297, 33.391712],
    [-101.9814, 33.390614], [-102.0159, 33.389812], [-102.067596, 33.389614],
    [-102.075897, 33.389412], [-102.078194, 33.468811], [-102.078094, 33.476112],
    [-102.078293, 33.480213], [-102.078194, 33.490711], [-102.078499, 33.50061],
    [-102.0784, 33.506111], [-102.078995, 33.519413], [-102.080399, 33.578911],
    [-102.081398, 33.636414], [-102.083397, 33.722813], [-102.083893, 33.738811],
    [-102.084999, 33.794411], [-102.085693, 33.82151], [-102.085594, 33.824711],
    [-102.055695, 33.824913], [-101.874496, 33.827011], [-101.871399, 33.826912],
    [-101.639595, 33.82991], [-101.596298, 33.830112], [-101.563599, 33.830612],
    [-101.561195, 33.667713], [-101.560097, 33.609612], [-101.558693, 33.520714],
    [-101.557999, 33.490711], [-101.556999, 33.403214], [-101.556595, 33.394711]]]}


def _iso(ts):
    return datetime.fromtimestamp(ts, _CDT).isoformat(timespec="seconds")


def _vtec(action, office, phen, etn, t1, t2):
    f = "%y%m%dT%H%MZ"
    return "/O.%s.%s.%s.%04d.%s-%s/" % (
        action, office, phen, etn, datetime.fromtimestamp(t1, timezone.utc).strftime(f),
        datetime.fromtimestamp(t2, timezone.utc).strftime(f))


def _tornado_warning(now):
    lat, lon = SITE
    t1, t2 = now - 300, now + 1800
    uid = "urn:oid:2.49.0.1.840.0.a2fd7e77be1a89e6c3d832373feb236e240ab419.001.1"
    ring = [[lon - 0.15, lat - 0.12], [lon - 0.14, lat + 0.10], [lon + 0.16, lat + 0.08],
            [lon + 0.15, lat - 0.10], [lon - 0.15, lat - 0.12]]
    return {"id": "https://api.weather.gov/alerts/" + uid, "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "@id": "https://api.weather.gov/alerts/" + uid, "@type": "wx:Alert",
                "id": uid, "areaDesc": "Lubbock, TX",
                "geocode": {"SAME": ["048303"], "UGC": ["TXC303"]},
                "affectedZones": ["https://api.weather.gov/zones/county/TXC303"],
                "references": [], "sent": _iso(t1), "effective": _iso(t1),
                "onset": _iso(t1), "expires": _iso(t2), "ends": _iso(t2),
                "status": "Actual", "messageType": "Alert", "category": "Met",
                "severity": "Extreme", "certainty": "Observed", "urgency": "Immediate",
                "event": "Tornado Warning", "sender": "w-nws.webmaster@noaa.gov",
                "senderName": "NWS Lubbock TX",
                "headline": "Tornado Warning issued by NWS Lubbock TX",
                "description": "* At 640 PM CDT, a severe thunderstorm capable of producing "
                               "a tornado was located near Shallowater, moving east at 30 "
                               "mph.\n\nHAZARD...Tornado and golf ball size hail.\n\n"
                               "SOURCE...Radar indicated rotation.",
                "instruction": "TAKE COVER NOW! Move to a basement or an interior room on "
                               "the lowest floor of a sturdy building. Avoid windows.",
                "response": "Shelter", "note": None,
                "parameters": {
                    "AWIPSidentifier": ["TORLUB"], "WMOidentifier": ["WFUS54 KLUB 212340"],
                    "maxHailSize": ["1.75"], "tornadoDetection": ["RADAR INDICATED"],
                    "VTEC": [_vtec("NEW", "KLUB", "TO.W", 12, t1, t2)],
                    "eventEndingTime": [_iso(t2)]},
                "scope": "Public", "code": "IPAWSv1.0", "language": "en-US",
                "web": "http://www.weather.gov",
                "eventCode": {"SAME": ["TOR"], "NationalWeatherService": ["TOW"]}}}


def _red_flag_warning(now):
    t1, t2 = now - 3600, now + 6 * 3600
    uid = "urn:oid:2.49.0.1.840.0.5a2c1c3f0f0e4c7d9b1e2f3a4b5c6d7e8f901234.001.1"
    return {"id": "https://api.weather.gov/alerts/" + uid, "type": "Feature",
            "geometry": None,
            "properties": {
                "@id": "https://api.weather.gov/alerts/" + uid, "@type": "wx:Alert",
                "id": uid, "areaDesc": "Lubbock",
                "geocode": {"SAME": ["048303"], "UGC": ["TXZ035"]},
                "affectedZones": ["https://api.weather.gov/zones/fire/TXZ035"],
                "references": [], "sent": _iso(t1), "effective": _iso(t1),
                "onset": _iso(t1), "expires": _iso(t2), "ends": _iso(t2),
                "status": "Actual", "messageType": "Alert", "category": "Met",
                "severity": "Severe", "certainty": "Likely", "urgency": "Expected",
                "event": "Red Flag Warning", "sender": "w-nws.webmaster@noaa.gov",
                "senderName": "NWS Lubbock TX",
                "headline": "Red Flag Warning issued by NWS Lubbock TX",
                "description": "* AFFECTED AREA...Fire Weather Zone 035 Lubbock.\n\n* WIND"
                               "...Southwest 20 to 25 mph with gusts up to 40 mph.\n\n* "
                               "HUMIDITY...As low as 8 percent.",
                "instruction": "A Red Flag Warning means that critical fire weather "
                               "conditions are either occurring now, or will shortly.",
                "response": "Prepare", "note": None,
                "parameters": {
                    "AWIPSidentifier": ["RFWLUB"], "NWSheadline": [
                        "RED FLAG WARNING REMAINS IN EFFECT UNTIL 8 PM CDT THIS EVENING"],
                    "VTEC": [_vtec("NEW", "KLUB", "FW.W", 10, t1, t2)]},
                "scope": "Public", "code": "IPAWSv1.0", "language": "en-US",
                "web": "http://www.weather.gov",
                "eventCode": {"SAME": ["FWW"], "NationalWeatherService": ["FWW"]}}}


def _collection(features, title):
    return {"@context": {"@version": "1.1"}, "type": "FeatureCollection",
            "features": features, "title": title,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}


_POINTS = {"properties": {
    "@id": "https://api.weather.gov/points/33.748,-101.958", "@type": "wx:Point",
    "cwa": "LUB", "type": "land", "forecastOffice": "https://api.weather.gov/offices/LUB",
    "gridId": "LUB", "gridX": 46, "gridY": 41,
    "forecastGridData": "https://api.weather.gov/gridpoints/LUB/46,41",
    "forecastZone": "https://api.weather.gov/zones/forecast/TXZ035",
    "county": "https://api.weather.gov/zones/county/TXC303",
    "fireWeatherZone": "https://api.weather.gov/zones/fire/TXZ035",
    "timeZone": "America/Chicago", "radarStation": "KLBB"}}


class _Resp(io.BytesIO):
    def __init__(self, url, obj):
        super().__init__(json.dumps(obj).encode())
        self.url, self.status, self.code = url, 200, 200
        self.headers = Message()
        self.headers["Content-Type"] = "application/geo+json"

    def getcode(self):
        return 200

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _fake_nws(monkeypatch, point_features, area_features):
    """Route urlopen by URL: the NWS point/area/points/zones endpoints answer from the
    fixtures; everything else (every non-NWS feed) fails like a dead network."""
    seen = []

    def urlopen(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        seen.append(url)
        if "api.weather.gov/alerts/active" in url and "point=" in url:
            return _Resp(url, _collection(point_features, "Current alerts for the site"))
        if "api.weather.gov/alerts/active" in url:
            return _Resp(url, _collection(area_features, "Current alerts for TX, NM, OK"))
        if "api.weather.gov/points/" in url:
            return _Resp(url, _POINTS)
        if "api.weather.gov/zones/" in url:
            zid = url.rstrip("/").rsplit("/", 1)[-1]
            return _Resp(url, {"id": url, "type": "Feature", "geometry": _LUBBOCK,
                               "properties": {"id": zid, "name": "Lubbock", "state": "TX"}})
        raise urllib.error.URLError("network blocked in tests: " + url)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def no_socket(*args, **kwargs):             # anything bypassing urlopen fails too
        raise OSError("network blocked in tests")
    monkeypatch.setattr(socket, "create_connection", no_socket)
    return seen


@pytest.fixture
def site(tmp_path, monkeypatch, env, write_inputs):
    monkeypatch.setattr(config, "GEOCODE", SITE)
    monkeypatch.setattr(config, "HAZARD_LATCH_FILE", str(tmp_path / "hazard_latch.json"))
    monkeypatch.setattr(config, "HAZARD_CACHE_DIR", str(tmp_path / "zones"))
    write_inputs(config, sun=-10.0, humidity=40.0)
    return env


def test_real_poller_tornado_warning_over_the_site_vetoes(site, monkeypatch):
    nws_alerts = pytest.importorskip("safety.nws_alerts")
    now = time.time()
    _fake_nws(monkeypatch, [_tornado_warning(now)],
              [_tornado_warning(now), _red_flag_warning(now)])
    p = nws_alerts.NwsAlertsPoller(config, site["log"])
    p.poll_point(now)
    p.poll_area(now)
    m = SafetyMonitor(config, site["log"], site["poller"], hazards=p)
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any(r.startswith("NWS Tornado Warning in effect for the site until ")
               and r.endswith("(NWS Lubbock TX)") for r in st["reasons"]), st["reasons"]
    hz = st["components"]["hazards"]
    assert [v["event"] for v in hz["veto"]] == ["Tornado Warning"]
    assert {a["event"] for a in hz["at_site"]} >= {"Tornado Warning"}


def test_real_poller_veto_survives_a_restart_with_the_network_down(site, monkeypatch):
    # systemd Restart=always mid-warning must not reopen the dome, even if NWS is
    # unreachable after the restart: the persisted veto holds until the warning's end
    nws_alerts = pytest.importorskip("safety.nws_alerts")
    now = time.time()
    _fake_nws(monkeypatch, [_tornado_warning(now)], [_tornado_warning(now)])
    first = nws_alerts.NwsAlertsPoller(config, site["log"])
    first.poll_point(now)
    first.poll_area(now)
    assert first.component(now)["veto"]

    def dead(*args, **kwargs):
        raise urllib.error.URLError("network down after the restart")
    monkeypatch.setattr(urllib.request, "urlopen", dead)
    reborn = nws_alerts.NwsAlertsPoller(config, site["log"])      # reads the latch file
    reborn.maybe_poll(time.time())                                 # both queries fail
    st = SafetyMonitor(config, site["log"], site["poller"], hazards=reborn).evaluate()
    assert st["is_safe"] is False
    assert any(r.startswith("NWS Tornado Warning in effect for the site until ")
               for r in st["reasons"]), st["reasons"]


def test_real_poller_releases_when_fresh_queries_show_the_warning_gone(site, monkeypatch):
    nws_alerts = pytest.importorskip("safety.nws_alerts")
    now = time.time()
    earlier = now - 120
    _fake_nws(monkeypatch, [_tornado_warning(earlier)], [_tornado_warning(earlier)])
    p = nws_alerts.NwsAlertsPoller(config, site["log"])
    p.poll_point(earlier)
    p.poll_area(earlier)
    m = SafetyMonitor(config, site["log"], site["poller"], hazards=p)
    assert m.evaluate()["is_safe"] is False
    _fake_nws(monkeypatch, [], [])                  # cancelled: gone from both queries
    p.poll_point(now)
    p.poll_area(now)
    st = m.evaluate()
    assert st["is_safe"] is True, st["reasons"]
    assert st["components"]["hazards"]["veto"] == []


def test_real_poller_red_flag_warning_at_the_site_does_not_veto(site, monkeypatch):
    nws_alerts = pytest.importorskip("safety.nws_alerts")
    now = time.time()
    _fake_nws(monkeypatch, [_red_flag_warning(now)], [_red_flag_warning(now)])
    p = nws_alerts.NwsAlertsPoller(config, site["log"])
    p.poll_point(now)
    p.poll_area(now)
    st = SafetyMonitor(config, site["log"], site["poller"], hazards=p).evaluate()
    assert st["is_safe"] is True, st["reasons"]
    hz = st["components"]["hazards"]
    assert hz["veto"] == [] and hz["available"] is True
    assert [a["event"] for a in hz["at_site"]] == ["Red Flag Warning"]


def test_real_poller_honours_a_lowercase_configured_event(site, monkeypatch):
    # the operator may write the env var in any case; config stores NWS's Title Case
    nws_alerts = pytest.importorskip("safety.nws_alerts")
    monkeypatch.setattr(config, "HAZARD_VETO_EVENTS",
                        config._parse_event_names("red flag warning"))
    now = time.time()
    _fake_nws(monkeypatch, [_red_flag_warning(now)], [_red_flag_warning(now)])
    p = nws_alerts.NwsAlertsPoller(config, site["log"])
    p.poll_point(now)
    p.poll_area(now)
    st = SafetyMonitor(config, site["log"], site["poller"], hazards=p).evaluate()
    assert st["is_safe"] is False
    assert any(r.startswith("NWS Red Flag Warning in effect for the site")
               for r in st["reasons"])


def test_real_feeds_poller_failing_everywhere_never_gates(site, monkeypatch):
    hazard_feeds = pytest.importorskip("safety.hazard_feeds")
    _fake_nws(monkeypatch, [], [])               # every non-NWS feed: network error
    f = hazard_feeds.HazardFeedsPoller(config)
    f.poll_now(time.time())
    base = SafetyMonitor(config, site["log"], site["poller"]).evaluate()
    st = SafetyMonitor(config, site["log"], site["poller"], hazard_feeds=f).evaluate()
    assert st["is_safe"] is base["is_safe"] is True
    info = st["components"]["hazard_info"]
    assert info["safe"] is True and info["info_only"] is True


def test_real_unavailable_components_are_harmless(site):
    for mod in (monitor_mod.nws_alerts, monitor_mod.hazard_feeds):
        if mod is None:
            pytest.skip("hazard module not present yet")
        comp = mod.unavailable_component(config)
        assert comp["safe"] is True
    st = SafetyMonitor(config, site["log"], site["poller"]).evaluate()
    assert st["is_safe"] is True


def test_real_components_render_on_setup(site, monkeypatch):
    nws_alerts = pytest.importorskip("safety.nws_alerts")
    hazard_feeds = pytest.importorskip("safety.hazard_feeds")
    from safety.alpaca import create_app
    now = time.time()
    _fake_nws(monkeypatch, [_tornado_warning(now)],
              [_tornado_warning(now), _red_flag_warning(now)])
    p = nws_alerts.NwsAlertsPoller(config, site["log"])
    p.poll_point(now)
    p.poll_area(now)
    f = hazard_feeds.HazardFeedsPoller(config)
    f.poll_now(now)                                 # every non-NWS feed fails
    app = create_app(SafetyMonitor(config, site["log"], site["poller"],
                                   hazards=p, hazard_feeds=f), config)
    app.testing = True
    r = app.test_client().get("/setup")
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert "VETO — Tornado Warning over the site until" in page
    assert "Red Flag Warning" in page and "Other hazard information" in page
