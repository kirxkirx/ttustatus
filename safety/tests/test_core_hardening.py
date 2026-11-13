"""The core layers hardened: a broken layer never takes IsSafe down, one WU station never
stops the rain poll, a WU or network outage never backs the stations off, the forecast
reads only numbers, the gating settings are range-checked, and a blind rain check is
shown (a warning, never a veto)."""
import http.client
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from safety import config, monitor as monitor_mod, nws_forecast, wu_poll
from safety.monitor import RainPoller, SafetyMonitor
from safety.tests.test_dew import _wu_reply
from safety.tests.test_monitor import _StubRadar, _conn_comp, _glm_comp, _radar_comp


class _Boom:
    """A layer whose component() raises (or, with `value`, returns that instead)."""

    def __init__(self, exc=None, value=None):
        self.exc = exc or TypeError("'>' not supported between 'str' and 'float'")
        self.value = value
        self.raise_it = value is None

    def component(self, *args, **kwargs):
        if self.raise_it:
            raise self.exc
        return self.value


class _Switch:
    """A layer that first reports `comp`, then raises once `broken` is set."""

    def __init__(self, comp):
        self.comp = comp
        self.broken = False

    def component(self, *args, **kwargs):
        if self.broken:
            raise RuntimeError("layer bug")
        return self.comp


# --- B3: a layer that raises never takes evaluate() down ----------------------------------
def _night(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)


def test_a_raising_forecast_is_unavailable_and_does_not_veto(env, write_inputs):
    _night(env, write_inputs)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], nws=_Boom())
    st = m.evaluate()
    assert st["is_safe"] is True
    nws = st["components"]["nws"]
    assert nws["available"] is False and "TypeError" in nws["error"]
    assert any(w.startswith("forecast layer failed (TypeError)") for w in st["warnings"])
    # a forecast component that is not a component at all: the same
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], nws=_Boom(value="oops"))
    assert m.evaluate()["is_safe"] is True


@pytest.mark.parametrize("layer", ["glm", "radar"])
def test_a_raising_glm_or_radar_without_a_veto_is_unavailable(env, write_inputs, layer):
    _night(env, write_inputs)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], **{layer: _Boom()})
    st = m.evaluate()
    assert st["is_safe"] is True
    assert st["components"][layer]["available"] is False
    assert st["components"][layer]["error"]


def test_a_raising_rain_layer_without_a_latch_is_unavailable(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    monkeypatch.setattr(env["poller"], "component", _Boom().component)
    st = env["monitor"].evaluate()
    assert st["is_safe"] is True
    rain = st["components"]["rain"]
    assert rain["available"] is False and rain["stations_live"] == 0
    assert any(w.startswith("rain layer failed") for w in st["warnings"])


def test_a_rain_latch_is_held_to_its_end_when_the_rain_layer_breaks(
        env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    p, m = env["poller"], env["monitor"]
    now = time.time()
    p._latch_until = now + 1800                     # 30 min of rain latch left
    st = m.evaluate()
    assert st["is_safe"] is False
    real = p.component
    monkeypatch.setattr(p, "component", _Boom().component)
    st = m.evaluate()
    assert st["is_safe"] is False                   # a bug never reopens the dome
    rain = st["components"]["rain"]
    assert rain["held"] is True and rain["latched"] is True
    assert 1700 < rain["seconds_remaining"] <= 1800
    assert any("held, the rain layer failed" in r for r in st["reasons"])
    assert any("its last veto is held until" in w for w in st["warnings"])
    # ... but only to the latch's own end
    monkeypatch.setattr(monitor_mod.time, "time", lambda: now + 1801)
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0, ts=now + 1801)
    assert m.evaluate()["is_safe"] is True
    monkeypatch.setattr(p, "component", real)


@pytest.mark.parametrize("layer, comp", [
    ("glm", _glm_comp(False, latched=True, remaining=1200)),
    ("radar", dict(_radar_comp(False, in_ring=True, near=12.0), latched=False,
                   seconds_remaining=900)),
])
def test_a_glm_or_radar_veto_is_held_when_the_layer_breaks(
        env, write_inputs, monkeypatch, layer, comp):
    _night(env, write_inputs)
    stub = _Switch(comp)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], **{layer: stub})
    now = time.time()
    assert m.evaluate()["is_safe"] is False
    stub.broken = True
    st = m.evaluate()
    assert st["is_safe"] is False and st["components"][layer]["held"] is True
    assert isinstance(st["is_safe"], bool)
    later = now + comp["seconds_remaining"] + 5
    monkeypatch.setattr(monitor_mod.time, "time", lambda: later)
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0, ts=later)
    st = m.evaluate()
    assert st["is_safe"] is True and st["components"][layer]["available"] is False


def test_a_layer_that_reported_clear_and_then_breaks_holds_nothing(env, write_inputs):
    _night(env, write_inputs)
    stub = _Switch(_glm_comp(True))
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], glm=stub)
    assert m.evaluate()["is_safe"] is True
    stub.broken = True
    assert m.evaluate()["is_safe"] is True


@pytest.mark.parametrize("layer", ["rain", "glm", "radar"])
def test_a_restored_latch_is_held_when_the_layer_breaks_from_its_first_call(
        env, write_inputs, monkeypatch, layer):
    """After a restart a latch comes back from the layer's own latch file, and there is no
    good report to hold yet: a layer whose component() raises from its first call must
    still keep that latch (the code before the guard answered /issafe with an HTTP 500,
    which failed safe too). A latch that has run out holds nothing."""
    _night(env, write_inputs)
    now = time.time()
    holder = env["poller"] if layer == "rain" else _Boom()
    if layer == "radar":
        holder._last_rain_ts = now - 60
        left = env["cfg"].RADAR_LATCH_SEC - 60
    else:
        holder._latch_until = now + 1800
        left = 1800
    if layer == "rain":
        monkeypatch.setattr(holder, "component", _Boom().component)
        m = env["monitor"]
    else:
        m = SafetyMonitor(env["cfg"], env["log"], env["poller"], **{layer: holder})
    st = m.evaluate()
    comp = st["components"][layer]
    assert st["is_safe"] is False and comp["held"] is True and comp["latched"] is True
    assert left - 120 < comp["seconds_remaining"] <= left + 1
    if layer == "radar":
        holder._last_rain_ts = now - env["cfg"].RADAR_LATCH_SEC - 1
    else:
        holder._latch_until = now - 1
    st = m.evaluate()
    assert st["is_safe"] is True and st["components"][layer]["available"] is False


def test_bad_replies_from_every_station_back_none_off(env):
    """An HTML error page that WU sends as HTTP 200 makes every station's reply unusable:
    like a 5xx it says nothing about the stations, and none is backed off. A bad reply
    while another station of the same poll got a proper answer (a reading, or a 204 for a
    dead station: test_a_dead_station_is_still_backed_off) still counts against it."""
    p = env["poller"]
    after = max(1, int(env["cfg"].WU_BACKOFF_AFTER))
    for i in range(after + 1):
        p._update_backoff({"results": [{"station": "A", "state": "bad-reply"},
                                       {"station": "B", "state": "bad-reply"}]}, 1000.0 + i)
    assert p._backoff_until == {} and p._offline_streak == {}
    for i in range(after + 1):
        p._update_backoff({"results": [{"station": "A", "state": "bad-reply"},
                                       {"station": "B", "state": "live"}]}, 2000.0 + i)
    assert set(p._backoff_until) == {"A"}


def test_a_raising_connectivity_watchdog_keeps_its_fail_direction(env, write_inputs):
    _night(env, write_inputs)
    stub = _Switch(_conn_comp(True))
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], conn=stub)
    assert m.evaluate()["is_safe"] is True
    stub.broken = True
    st = m.evaluate()                               # broken, last verdict online: kept
    assert st["is_safe"] is True and st["components"]["connectivity"]["error"]
    assert any(w.startswith("connectivity layer failed") for w in st["warnings"])
    # broken for longer than the offline threshold: no proof the internet is up
    m._conn_ok_mono -= env["cfg"].CONN_OFFLINE_UNSAFE_SEC + 1
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any("connectivity watchdog failed" in r for r in st["reasons"])
    # an "offline too long" verdict stays when the watchdog then breaks
    stub = _Switch(_conn_comp(False, offline_min=75))
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], conn=stub)
    assert m.evaluate()["is_safe"] is False
    stub.broken = True
    assert m.evaluate()["is_safe"] is False


def test_issafe_over_http_answers_while_every_layer_raises(tmp_path, monkeypatch):
    from safety.tests.test_alpaca import _client
    c, mon = _client(tmp_path, monkeypatch)
    mon.set_connected(True)
    mon.set_connected(True, device=config.DEW_DEVICE_NUMBER)
    monkeypatch.setattr(mon.poller, "component", _Boom().component)
    for name in ("nws", "glm", "radar", "conn"):
        setattr(mon, name, _Boom())
    r = c.get("/api/v1/safetymonitor/0/issafe")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ErrorNumber"] == 0 and isinstance(body["Value"], bool)
    assert body["Value"] is True        # no latch was running and the watchdog is new
    st = mon.state()
    assert st["components"]["rain"]["error"] and st["components"]["glm"]["error"]
    assert len([w for w in st["warnings"] if " layer failed (" in w]) == 5


def test_a_malformed_radar_number_does_not_break_the_reason_text(env, write_inputs):
    _night(env, write_inputs)
    comp = dict(_radar_comp(False, in_ring=True), nearest_km="near", trigger_km=None)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], radar=_StubRadar(comp))
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any(r.startswith("rain on radar within 30 km") for r in st["reasons"])


# --- B3 (a): the forecast reads only finite numbers ----------------------------------------
def _grid(value, update=None):
    hour0 = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    vt = (hour0 - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00") + "/PT6H"
    series = {"values": [{"validTime": vt, "value": value}]}
    props = {"updateTime": update or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")}
    for prop in nws_forecast.WANTED.values():
        props[prop] = {"values": [{"validTime": vt, "value": 0}]}
    props["skyCover"] = series
    return {"properties": props}


@pytest.fixture
def grid_cfg(monkeypatch):
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    return config


def test_a_string_forecast_value_never_raises(monkeypatch, grid_cfg):
    for value, expect in (("80", 80.0), ("abc", None), (True, None), (float("nan"), None),
                          ([1], None), (95, 95.0)):
        monkeypatch.setattr(nws_forecast, "_get", lambda url, ua, v=value: _grid(v))
        res = nws_forecast.fetch(grid_cfg)
        assert res["ok"] is True, value
        assert res["now_hour"]["cloud_cover_pct"] == expect, value
        ev = nws_forecast.evaluate(res, grid_cfg)       # (raised TypeError for "80")
        assert ev["available"] is True
        assert ev["safe"] is (expect is None or expect <= grid_cfg.NWS_CLOUD_MAX), value
    # a result carrying a string (an older or a broken poller) is compared as a number
    monkeypatch.setattr(nws_forecast, "_get", lambda url, ua: _grid(10))
    res = nws_forecast.fetch(grid_cfg)
    res["now_hour"]["cloud_cover_pct"] = "bogus"
    assert nws_forecast.evaluate(res, grid_cfg)["safe"] is True


def test_an_unreadable_update_time_makes_the_forecast_unavailable(monkeypatch, grid_cfg):
    for bad in ("2026-13-01T00:00:00Z", "yesterday", "2026-11-13T05:00:00.5+00:00:30"):
        monkeypatch.setattr(nws_forecast, "_get", lambda url, ua, b=bad: _grid(10, b))
        res = nws_forecast.fetch(grid_cfg)
        assert res["ok"] is False and "malformed" in res["error"], bad
        assert nws_forecast.evaluate(res, grid_cfg)["available"] is False


def test_iso_reads_the_same_on_every_python():
    dt = nws_forecast._iso("2026-11-13T05:00:00Z")
    assert dt == datetime(2026, 11, 13, 5, tzinfo=timezone.utc)
    assert nws_forecast._iso("2026-11-13T05:00:00-06:00").utcoffset() == timedelta(hours=-6)
    for bad in ("20261113T050000Z", "2026-W46-5", "2026-11-13T05:00:00.5Z junk", None):
        with pytest.raises(ValueError):
            nws_forecast._iso(bad)


# --- B7: the gridpoint URL from /points is checked -----------------------------------------
def test_points_reply_naming_another_url_is_refused(monkeypatch):
    monkeypatch.setattr(config, "NWS_GRID", "")
    asked = []

    def get(url, ua):
        asked.append(url)
        if "/points/" in url:
            return {"properties": {"forecastGridData": bad, "gridId": "LUB",
                                   "gridX": 46, "gridY": 41}}
        return _grid(10)
    monkeypatch.setattr(nws_forecast, "_get", get)
    for bad in ("file:///etc/passwd", "data:,{}", "ftp://api.weather.gov/gridpoints/x",
                "https://api.weather.gov.evil.example/gridpoints/LUB/1,1",
                "http://api.weather.gov/gridpoints/LUB/1,1", None, 7):
        asked.clear()
        res = nws_forecast.fetch(config)
        assert res["ok"] is False and "outside" in res["error"], bad
        assert len(asked) == 1, bad                     # never followed
    bad = "https://api.weather.gov/gridpoints/LUB/46,41"
    asked.clear()
    assert nws_forecast.fetch(config)["ok"] is True and asked[-1] == bad


# --- B11: capped, deadline-bound reads ---------------------------------------------------------
class _Resp(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_wu_and_forecast_replies_are_size_capped(monkeypatch):
    big = b"[" + b" " * (300 * 1024) + b"]"
    monkeypatch.setattr(wu_poll.urllib.request, "urlopen", lambda req, timeout: _Resp(big))
    with pytest.raises(wu_poll.WuBadReply):
        wu_poll._get_json("https://api.weather.com/x")
    huge = b"{" + b" " * (4 * 1024 * 1024 + 10) + b"}"
    monkeypatch.setattr(nws_forecast.urllib.request, "urlopen",
                        lambda req, timeout: _Resp(huge))
    with pytest.raises(ValueError):
        nws_forecast._get("https://api.weather.gov/gridpoints/LUB/46,41", "ua")
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    res = nws_forecast.fetch(config)
    assert res["ok"] is False and "limit" in res["error"]


# --- B1 / B4: one station never stops the poll; outages never back stations off ---------------
class _CutShort(_Resp):
    def read1(self, n=-1):
        raise http.client.IncompleteRead(b"{", 100)


def _urlopen_by_station(replies):
    """urlopen that answers per station id: a dict (JSON), an exception, or a _Resp."""
    def urlopen(req, timeout):
        sid = req.full_url.split("stationId=")[1].split("&")[0]
        r = replies[sid]
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, _Resp):
            return r
        return _Resp(json.dumps(r).encode())
    return urlopen


def _http_error(code, retry_after=None):
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://api.weather.com/x", code, "err", hdrs, None)


def test_one_station_cut_short_leaves_the_others_polled(monkeypatch):
    monkeypatch.setattr(wu_poll.urllib.request, "urlopen", _urlopen_by_station({
        "A": _wu_reply(), "B": _CutShort(b""), "C": _wu_reply(precipRate=0.3),
        "D": {"observations": {}}, "E": {"observations": [{"epoch": None}]},
        "F": http.client.BadStatusLine("x"), "G": _Resp(b"", status=204)}))
    res = wu_poll.poll_stations(list("ABCDEFG"))
    states = {r["station"]: r["state"] for r in res["results"]}
    assert states == {"A": "live", "B": "error", "C": "live", "D": "bad-reply",
                      "E": "bad-reply", "F": "error", "G": "offline"}
    assert [r["station"] for r in res["raining"]] == ["C"]   # was never polled before
    assert res["errors"] == 2 and res["throttle"] is None


def test_an_unexpected_exception_from_one_station_is_an_error_not_an_abort(monkeypatch):
    def precip(sid):
        if sid == "B":
            raise KeyError(0)
        return (0.2 if sid == "C" else 0.0), 60
    monkeypatch.setattr(wu_poll, "station_precip", precip)
    res = wu_poll.poll_stations(["A", "B", "C"])
    assert res["live"] == 2 and [r["station"] for r in res["raining"]] == ["C"]
    assert res["results"][1]["state"] == "error"
    # station_observation itself never raises
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: 1 / 0)
    assert wu_poll.station_observation("X") is None


def _real_poller(env, monkeypatch, stations, urlopen):
    monkeypatch.setattr(wu_poll, "poll_stations", POLL_STATIONS)   # undo the conftest stub
    monkeypatch.setattr(wu_poll, "discover_stations",
                        lambda lat, lon: [(s, 1.0 + i) for i, s in enumerate(stations)])
    monkeypatch.setattr(wu_poll.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(env["cfg"], "WU_BACKOFF_AFTER", 2)
    return RainPoller(env["cfg"], env["log"])


POLL_STATIONS = wu_poll.poll_stations


@pytest.mark.parametrize("failure", [
    _http_error(500), _http_error(502), _http_error(401),
    urllib.error.URLError("no route to host"), TimeoutError("timed out"),
    http.client.IncompleteRead(b"", 10)],
    ids=["500", "502", "401", "urlerror", "timeout", "incompleteread"])
def test_api_and_network_errors_never_back_stations_off(env, monkeypatch, failure):
    p = _real_poller(env, monkeypatch, ["A", "B"],
                     _urlopen_by_station({"A": failure, "B": failure}))
    t = time.time()
    for k in range(5):
        res = p.poll_now(t + k * 600)
        assert [r["state"] for r in res["results"]] == ["error", "error"]
    assert p._backoff_until == {} and p._offline_streak == {}


def test_a_dead_station_is_still_backed_off(env, monkeypatch):
    p = _real_poller(env, monkeypatch, ["A", "B"], _urlopen_by_station({
        "A": _Resp(b"", status=204), "B": {"observations": [{"epoch": 1}]}}))
    t = time.time()
    p.poll_now(t)
    p.poll_now(t + 600)
    assert set(p._backoff_until) == {"A", "B"}          # offline and bad reply: as before


def test_429_stops_the_poll_and_retry_after_is_honoured(env, monkeypatch):
    p = _real_poller(env, monkeypatch, ["A", "B", "C"], _urlopen_by_station({
        "A": _wu_reply(), "B": _http_error(429, retry_after=1200), "C": _wu_reply()}))
    t = time.time()
    res = p.poll_now(t)
    assert [r["state"] for r in res["results"]] == ["live", "error", "not-asked"]
    assert res["throttle"] == {"status": 429, "retry_after_s": 1200.0}
    assert p._backoff_until == {} and p._offline_streak == {}
    # the normal interval (600 s) has passed, but WU said 20 min
    assert p.maybe_poll(-10.0, t + 700) is None
    assert p.maybe_poll(-10.0, t + 1201) is not None


def test_retry_after_is_bounded_and_read_as_a_date():
    h = Message()
    h["Retry-After"] = "86400"
    assert wu_poll._retry_after(h) == wu_poll.RETRY_AFTER_MAX_S
    h = Message()
    h["Retry-After"] = "Wed, 21 Oct 2015 07:28:00 GMT"      # in the past
    assert wu_poll._retry_after(h) == 0.0
    h = Message()
    h["Retry-After"] = "soon"
    assert wu_poll._retry_after(h) is None
    assert wu_poll._retry_after(Message()) is None
    assert wu_poll._get_json.__module__ == "safety.wu_poll"
    assert isinstance(wu_poll.WuApiError("x", status=503), wu_poll.WuError)
    assert wu_poll.WuApiError("x", status=503).throttled
    assert not wu_poll.WuApiError("x", status=500).throttled


# --- B1: discovery ---------------------------------------------------------------------------
def test_discovery_skips_unusable_entries_and_keeps_the_old_list(env, monkeypatch):
    assert wu_poll.clean_station_list([("A", 3.0), ("", 1.0), (None, 1.0), ("B", "x"),
                                       ("C", float("nan")), ("D", True), "E", ("F", 2)]) \
        == [("F", 2.0), ("A", 3.0)]
    p = env["poller"]
    p.poll_now(time.time())
    assert [s for s, d in p._stations] == ["S1", "S2"]
    p._stations_ts = 0                                   # discovery due again
    monkeypatch.setattr(wu_poll, "discover_stations", lambda lat, lon: None)
    p.poll_now(time.time())
    assert [s for s, d in p._stations] == ["S1", "S2"]
    monkeypatch.setattr(wu_poll, "discover_stations", lambda lat, lon: 1 / 0)
    p.poll_now(time.time())
    assert [s for s, d in p._stations] == ["S1", "S2"]
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: {"location": []})
    with pytest.raises(wu_poll.WuBadReply):
        POLL_DISCOVER(1.0, 2.0)


POLL_DISCOVER = wu_poll.discover_stations


def test_station_id_quoting_is_inside_the_guard(monkeypatch):
    monkeypatch.setattr(wu_poll, "_get_json", lambda url, timeout=None: _wu_reply())
    assert wu_poll.station_observation(None) is None
    assert wu_poll.station_observation(7) is None


# --- B9: a NaN rain rate is no reading ------------------------------------------------------
def test_a_nan_rain_rate_is_no_reading(monkeypatch):
    assert wu_poll._rate(float("nan")) is None and wu_poll._rate("NaN") is None
    assert wu_poll._rate("inf") == float("inf") and wu_poll._rate("-inf") == float("-inf")
    monkeypatch.setattr(wu_poll, "_get_json",
                        lambda url, timeout=None: _wu_reply(precipRate="nan"))
    res = wu_poll.poll_stations(["X"])
    assert res["live"] == 0 and res["results"][0]["state"] == "bad-reply"


# --- B2 / B6: stale results and the blind rain check -------------------------------------------
def _blind_poller(env, monkeypatch, live):
    monkeypatch.setattr(wu_poll, "poll_stations", lambda stations, max_age_min=None: {
        "live": live, "total": len(stations), "raining": [], "max_rate": 0.0,
        "results": [], "errors": 0, "throttle": None})
    return env["poller"]


def test_a_stale_poll_reads_as_no_data(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    p = _blind_poller(env, monkeypatch, 2)
    t = time.time()
    p.poll_now(t - 4 * env["cfg"].WU_POLL_INTERVAL)
    rain = p.component(-10.0, t)
    assert rain["stale"] is True and rain["stations_live"] == 0
    assert rain["note"].startswith("stale, last poll 40 min ago")
    p.poll_now(t)
    rain = p.component(-10.0, t)
    assert rain["stale"] is False and rain["stations_live"] == 2 and rain["note"] is None
    # in daytime (polling not active) an old poll is not "stale": nobody polls
    p.poll_now(t - 4 * env["cfg"].WU_POLL_INTERVAL)
    assert p.component(30.0, t)["stale"] is False
    st = env["monitor"].evaluate()
    assert st["is_safe"] is True                        # no veto change


def test_blind_rain_check_is_a_warning_only(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    p, m = _blind_poller(env, monkeypatch, 0), env["monitor"]
    iv = env["cfg"].WU_POLL_INTERVAL
    t = time.time()
    for k in range(2):
        p.poll_now(t - (2 - k) * iv)
    st = m.evaluate()
    assert not any("rain check blind" in w for w in st["warnings"])   # two polls: not yet
    p.poll_now(t)
    st = m.evaluate()
    assert st["is_safe"] is True                        # the owner's choice: no veto
    blind = [w for w in st["warnings"] if w.startswith("rain check blind")]
    assert blind == ["rain check blind: 0/2 WU stations reporting for 20 min (the radar "
                     "is unavailable too — nothing watches for rain)"]
    m.radar = _StubRadar(_radar_comp(True))
    st = m.evaluate()
    assert ("rain check blind: 0/2 WU stations reporting for 20 min (radar still watches "
            "for rain)") in st["warnings"]
    # one station reporting ends it
    _blind_poller(env, monkeypatch, 1).poll_now(t + 1)
    assert not any("rain check blind" in w for w in m.evaluate()["warnings"])


def test_no_blind_warning_without_a_key_or_in_daytime(env, write_inputs, monkeypatch):
    p = _blind_poller(env, monkeypatch, 0)
    t = time.time()
    for k in range(4):
        p.poll_now(t - (3 - k) * 600)
    write_inputs(env["cfg"], sun=20.0, humidity=40.0)
    assert not any("rain check blind" in w for w in env["monitor"].evaluate()["warnings"])
    _night(env, write_inputs)
    monkeypatch.setattr(env["cfg"], "WU_API_KEY", "")
    assert not any("rain check blind" in w for w in env["monitor"].evaluate()["warnings"])


def test_a_new_night_starts_a_new_blind_count(env, monkeypatch):
    p = _blind_poller(env, monkeypatch, 0)
    t = time.time()
    p.poll_now(t - 12 * 3600)
    p.poll_now(t - 12 * 3600 + 600)
    p.poll_now(t)
    assert p._blind_polls == 1 and p._blind_since == t


# --- B5 / B8: settings ------------------------------------------------------------------------
def _settings(**env):
    code = ("import json; from safety import config as c; print(json.dumps(dict("
            "{k: getattr(c, k) for k in %r}, warnings=c.CONFIG_WARNINGS)))" % (sorted(_NAMES),))
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith("TTU_SAFETY_") and k != "STATUS_PAGE_INTERVAL"}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=dict(clean, **env),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


_NAMES = {"RAIN_LATCH_HOURS", "INPUTS_STALE_SEC", "NWS_POLL_INTERVAL", "NWS_STALE_AFTER_MIN",
          "NWS_CLOUD_MAX", "NWS_PRECIP_PROB_MAX", "NWS_THUNDER_PROB_MAX", "RADAR_TRIGGER_KM",
          "RADAR_DBZ", "RADAR_STALE_AFTER_SEC", "RADAR_LATCH_SEC", "CONN_OFFLINE_UNSAFE_SEC",
          "EVAL_INTERVAL", "WU_POLL_INTERVAL", "NWS_ENABLED", "RADAR_ENABLED",
          "RADAR_DAY_ENABLED", "HAZARDS_ENABLED", "PAGE_ENABLED", "CONN_ENABLED",
          "RADAR_TILE_DARK_INVERT", "HAZARD_FEEDS_ENABLED", "WU_MAX_STATION_KM"}


def test_gating_settings_are_range_checked_loudly():
    plain = _settings()
    assert plain["warnings"] == []
    assert (plain["RAIN_LATCH_HOURS"], plain["INPUTS_STALE_SEC"], plain["EVAL_INTERVAL"],
            plain["NWS_POLL_INTERVAL"], plain["NWS_CLOUD_MAX"]) == (1.0, 600, 30, 900, 60.0)
    odd = _settings(TTU_SAFETY_RAIN_LATCH_HOURS="-1", TTU_SAFETY_INPUTS_STALE_SEC="86400",
                    TTU_SAFETY_NWS_POLL_INTERVAL="0", TTU_SAFETY_NWS_CLOUD_MAX="600",
                    TTU_SAFETY_NWS_PRECIP_MAX="-5", TTU_SAFETY_NWS_THUNDER_MAX="101",
                    TTU_SAFETY_NWS_STALE_MIN="0", TTU_SAFETY_RADAR_KM="-5",
                    TTU_SAFETY_RADAR_DBZ="-20", TTU_SAFETY_RADAR_STALE_SEC="10",
                    TTU_SAFETY_RADAR_LATCH_SEC="0", TTU_SAFETY_OFFLINE_UNSAFE_SEC="5",
                    TTU_SAFETY_EVAL_INTERVAL="0", TTU_SAFETY_POLL_INTERVAL="1",
                    TTU_SAFETY_WU_MAX_KM="0")
    assert odd["RAIN_LATCH_HOURS"] == 0.1             # a negative latch disabled the latch
    assert odd["INPUTS_STALE_SEC"] == 3600
    assert odd["NWS_POLL_INTERVAL"] == 300 and odd["NWS_STALE_AFTER_MIN"] == 30
    assert (odd["NWS_CLOUD_MAX"], odd["NWS_PRECIP_PROB_MAX"],
            odd["NWS_THUNDER_PROB_MAX"]) == (100.0, 0.0, 100.0)
    assert odd["RADAR_TRIGGER_KM"] == 1.0 and odd["RADAR_DBZ"] == 5.0
    assert odd["RADAR_STALE_AFTER_SEC"] == 600 and odd["RADAR_LATCH_SEC"] == 60
    assert odd["CONN_OFFLINE_UNSAFE_SEC"] == 600 and odd["EVAL_INTERVAL"] == 1
    assert odd["WU_POLL_INTERVAL"] == 60 and odd["WU_MAX_STATION_KM"] == 1.0
    for name in ("RAIN_LATCH_HOURS", "INPUTS_STALE_SEC", "NWS_POLL_INTERVAL", "RADAR_KM",
                 "OFFLINE_UNSAFE_SEC", "EVAL_INTERVAL", "NWS_CLOUD_MAX"):
        assert any(w.startswith("TTU_SAFETY_%s=" % name) for w in odd["warnings"]), name


def test_old_switches_understand_off_and_warn_about_the_unknown():
    switches = {"TTU_SAFETY_NWS": "NWS_ENABLED", "TTU_SAFETY_RADAR": "RADAR_ENABLED",
                "TTU_SAFETY_RADAR_DAY": "RADAR_DAY_ENABLED",
                "TTU_SAFETY_HAZARDS": "HAZARDS_ENABLED", "TTU_SAFETY_PAGE": "PAGE_ENABLED",
                "TTU_SAFETY_CONN": "CONN_ENABLED",
                "TTU_SAFETY_RADAR_TILE_DARK_INVERT": "RADAR_TILE_DARK_INVERT",
                "TTU_SAFETY_HAZARD_FEEDS": "HAZARD_FEEDS_ENABLED"}
    off = _settings(**{k: "off" for k in switches})
    assert all(off[v] is False for v in switches.values()), off
    assert off["warnings"] == []
    disabled = _settings(**{k: "Disabled" for k in switches})
    assert all(disabled[v] is False for v in switches.values())
    typo = _settings(**{k: "of" for k in switches})
    assert all(typo[v] is True for v in switches.values())          # the default, ...
    for k in switches:                                               # ... loudly
        assert any(w.startswith("%s='of'" % k) for w in typo["warnings"]), k
    on = _settings(**{k: "1" for k in switches})
    assert all(on[v] is True for v in switches.values()) and on["warnings"] == []


def test_config_warnings_are_shown_in_the_state(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    monkeypatch.setattr(config, "CONFIG_WARNINGS",
                        ["TTU_SAFETY_EVAL_INTERVAL=0 is outside [1, 300] — using 1"])
    st = env["monitor"].evaluate()
    assert st["is_safe"] is True
    assert ("configuration: TTU_SAFETY_EVAL_INTERVAL=0 is outside [1, 300] — using 1"
            in st["warnings"])
    with open(env["cfg"].STATE_FILE, encoding="utf-8") as f:
        assert "configuration: TTU_SAFETY_EVAL_INTERVAL" in json.dumps(json.load(f))
