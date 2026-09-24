import json
import logging
import re
import time
import types
from datetime import datetime, timezone

import pytest

from safety import config, wu_poll
from safety import monitor as monitor_mod
from safety.monitor import RainPoller, SafetyMonitor


def test_safe_when_all_clear(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = env["monitor"].evaluate()
    assert st["is_safe"] is True
    assert st["reasons"] == []


def test_unsafe_when_sun_up(env, write_inputs):
    write_inputs(env["cfg"], sun=2.5, humidity=40.0)
    st = env["monitor"].evaluate()
    assert st["is_safe"] is False
    assert any("sun above horizon" in r for r in st["reasons"])


def test_sun_exactly_zero_is_safe(env, write_inputs):
    # unsafe is strictly > 0; exactly 0 stays safe
    write_inputs(env["cfg"], sun=0.0, humidity=40.0)
    assert env["monitor"].evaluate()["is_safe"] is True


def test_unsafe_when_humidity_high(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=96.0)
    st = env["monitor"].evaluate()
    assert st["is_safe"] is False
    assert any("humidity" in r for r in st["reasons"])


def test_humidity_hysteresis(env, write_inputs):
    m, cfg = env["monitor"], env["cfg"]
    write_inputs(cfg, sun=-10.0, humidity=96.0)
    assert m.evaluate()["is_safe"] is False          # 96 > 95 -> unsafe
    write_inputs(cfg, sun=-10.0, humidity=94.0)
    assert m.evaluate()["is_safe"] is False          # 93 < 94 <= 95 -> stays unsafe
    write_inputs(cfg, sun=-10.0, humidity=92.0)
    assert m.evaluate()["is_safe"] is True           # 92 < 93 -> clears


def test_stale_inputs_fail_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0, ts=time.time() - 99999)
    st = env["monitor"].evaluate()
    assert st["is_safe"] is False
    assert any("stale" in r for r in st["reasons"])


def test_missing_inputs_fail_safe(env):
    assert env["monitor"].evaluate()["is_safe"] is False


def test_rain_latch_trips_on_first_detection_and_expires(env, write_inputs, monkeypatch):
    cfg, poller, m = env["cfg"], env["poller"], env["monitor"]
    write_inputs(cfg, sun=-10.0, humidity=40.0)
    assert m.evaluate()["is_safe"] is True
    # a single station reporting rain must trip it immediately (no confirmation)
    monkeypatch.setattr(wu_poll, "poll_stations",
                        lambda stations, max_age_min=None: {
                            "live": 2, "total": 2,
                            "raining": [{"station": "S1", "precip_in_hr": 0.05}],
                            "max_rate": 0.05, "results": []})
    poller.poll_now()
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any("rain latch" in r for r in st["reasons"])
    # the latch persists for RAIN_LATCH_HOURS (1 h); simulate expiry
    poller._latch_until = time.time() - 1
    assert m.evaluate()["is_safe"] is True


def test_rain_latch_persisted_across_restart(env, write_inputs, monkeypatch):
    cfg, poller = env["cfg"], env["poller"]
    monkeypatch.setattr(wu_poll, "poll_stations",
                        lambda stations, max_age_min=None: {
                            "live": 1, "total": 1,
                            "raining": [{"station": "S1", "precip_in_hr": 0.1}],
                            "max_rate": 0.1, "results": []})
    poller.poll_now()
    # a fresh poller (daemon restart) must reload the active latch from disk
    from safety.monitor import RainPoller
    reborn = RainPoller(cfg, env["log"])
    assert reborn.component(None)["latched"] is True


def test_rain_poll_gated_by_sun(env):
    poller = env["poller"]
    assert poller.maybe_poll(10.0) is None      # daytime: no poll
    assert poller.maybe_poll(-5.0) is not None   # night: polls


def test_nan_humidity_fails_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=float("nan"))
    st = env["monitor"].evaluate()
    assert st["is_safe"] is False
    assert any("humidity unknown" in r for r in st["reasons"])


def test_nan_timestamp_is_stale_and_does_not_crash(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0, ts=float("nan"))
    st = env["monitor"].evaluate()          # must not raise
    assert st["is_safe"] is False
    assert any("stale" in r for r in st["reasons"])


def test_future_timestamp_is_stale(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0, ts=time.time() + 3600)
    assert env["monitor"].evaluate()["is_safe"] is False


def test_humidity_hysteresis_fails_safe_on_restart(env, write_inputs):
    cfg = env["cfg"]
    # A fresh monitor (simulated restart) with humidity in the 93-95 hold band must
    # report UNSAFE until a fresh reading below the 93% clear threshold proves safe.
    write_inputs(cfg, sun=-10.0, humidity=94.0)
    fresh = SafetyMonitor(cfg, env["log"], env["poller"])
    assert fresh.evaluate()["is_safe"] is False
    write_inputs(cfg, sun=-10.0, humidity=92.0)
    assert fresh.evaluate()["is_safe"] is True


def test_rain_polling_disabled_without_key(env, monkeypatch):
    monkeypatch.setattr(env["cfg"], "WU_API_KEY", "")
    poller = env["poller"]
    assert poller.maybe_poll(-5.0) is None            # night, but no key -> no poll
    comp = poller.component(-5.0)
    assert comp["enabled"] is False
    assert comp["polling_active"] is False
    assert comp["safe"] is True                        # no latch -> rain axis stays safe


def test_state_includes_alpaca_endpoint(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    a = env["monitor"].evaluate()["alpaca"]
    assert a["port"] == env["cfg"].HTTP_PORT
    assert a["device_number"] == env["cfg"].DEVICE_NUMBER
    assert a["issafe_path"].endswith("/issafe")
    assert a["address"]      # some IP string


class _StubNws:
    def __init__(self, comp):
        self._c = comp

    def component(self, now=None):
        return self._c


def _nws_comp(safe, reasons=None):
    return {"safe": safe, "available": True, "stale": False, "reasons": reasons or [],
            "source": "NWS", "thresholds": {}, "grid": "LUB/46,41", "update_time": None,
            "age_min": 3, "now_hour": {}, "next_hour": {}, "hours": []}


def test_nws_breach_makes_unsafe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"],
                      nws=_StubNws(_nws_comp(False, ["NWS cloud cover 85% > 70% (next hour)"])))
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any("cloud cover" in r for r in st["reasons"])
    assert st["components"]["nws"]["safe"] is False


def test_nws_unavailable_does_not_block(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    comp = _nws_comp(True)
    comp["available"] = False
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], nws=_StubNws(comp))
    assert m.evaluate()["is_safe"] is True


def test_no_nws_poller_is_safe_and_reports_unavailable(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])   # nws=None
    st = m.evaluate()
    assert st["is_safe"] is True
    assert st["components"]["nws"]["available"] is False


class _StubGlm:
    def __init__(self, comp):
        self._c = comp

    def component(self, sun_alt=None, now=None):
        return self._c


def _glm_comp(safe, latched=False, remaining=0):
    return {"safe": safe, "enabled": True, "available": True, "latched": latched,
            "latched_until": None, "seconds_remaining": remaining, "in_ring": latched,
            "nearest_km": 12.0 if latched else 800.0, "nearest_bearing": "N",
            "polling_active": True, "last_poll_ts": None, "granules_scanned": 15,
            "trigger_km": 50.0, "cooloff_hours": 0.5, "source": "GLM"}


def test_glm_latch_makes_unsafe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"],
                      glm=_StubGlm(_glm_comp(False, latched=True, remaining=3600)))
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any("lightning within" in r for r in st["reasons"])
    assert st["components"]["glm"]["latched"] is True


def test_glm_clear_is_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], glm=_StubGlm(_glm_comp(True)))
    assert m.evaluate()["is_safe"] is True


def test_no_glm_poller_is_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])   # glm=None
    st = m.evaluate()
    assert st["is_safe"] is True
    assert st["components"]["glm"]["enabled"] is False


class _StubRadar:
    def __init__(self, comp):
        self._c = comp

    def component(self, sun_alt=None, now=None):
        return self._c


def _radar_comp(safe, in_ring=False, near=None):
    return {"safe": safe, "enabled": True, "available": True, "in_ring": in_ring,
            "nearest_km": near, "pixels": 5 if in_ring else 0, "frame_utc": None,
            "age_s": 60, "trigger_km": 30.0, "dbz": 20.0, "polling_active": True,
            "thumb_available": True, "thumb_path": "/x/ttu_radar.png",
            "attribution": "attr", "source": "MRMS"}


def test_radar_rain_makes_unsafe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"],
                      radar=_StubRadar(_radar_comp(False, in_ring=True, near=18.0)))
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any("rain on radar within" in r for r in st["reasons"])
    assert st["components"]["radar"]["in_ring"] is True


def test_radar_clear_is_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"],
                      radar=_StubRadar(_radar_comp(True)))
    assert m.evaluate()["is_safe"] is True


def test_no_radar_poller_is_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])   # radar=None
    st = m.evaluate()
    assert st["is_safe"] is True
    assert st["components"]["radar"]["enabled"] is False


class _StubConn:
    def __init__(self, comp):
        self._c = comp

    def component(self, now=None):
        return self._c


def _conn_comp(safe, offline_min=0):
    return {"safe": safe, "online": safe, "offline_sec": offline_min * 60,
            "offline_min": offline_min, "threshold_sec": 3600, "probed": True,
            "source": "probe"}


def test_connectivity_offline_makes_unsafe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"],
                      conn=_StubConn(_conn_comp(False, offline_min=75)))
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any("no internet" in r for r in st["reasons"])
    assert st["components"]["connectivity"]["online"] is False


def test_connectivity_online_is_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], conn=_StubConn(_conn_comp(True)))
    assert m.evaluate()["is_safe"] is True


def test_no_conn_watch_is_safe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])   # conn=None
    st = m.evaluate()
    assert st["is_safe"] is True
    assert st["components"]["connectivity"]["online"] is None   # honest: never probed


def test_env_float_rejects_nonfinite(monkeypatch):
    monkeypatch.setenv("TTU_TEST_FLOAT", "nan")
    assert config._env_float("TTU_TEST_FLOAT", 3.0) == 3.0
    monkeypatch.setenv("TTU_TEST_FLOAT", "inf")
    assert config._env_float("TTU_TEST_FLOAT", 3.0) == 3.0
    monkeypatch.setenv("TTU_TEST_FLOAT", "2.5")
    assert config._env_float("TTU_TEST_FLOAT", 3.0) == 2.5


def test_rain_latch_default_is_one_hour():
    # the operator-chosen freeze time after the last WU station rain reading
    from safety import config as _cfg
    assert _cfg.RAIN_LATCH_HOURS == 1.0


def test_unreliable_station_is_excluded_from_discovery(monkeypatch):
    # KTXSHALL25 reports bogus precipitation (2026-08-29): a config blocklist must keep
    # it out of the polled set so it can neither close nor hold open the dome.
    from safety import config as _cfg
    from safety import wu_poll as _wu
    # the exclusion list is reserved for LYING stations; dead ones (KTXLUBBO680,
    # KTXSHALL23, last seen 2026-08-11 / 2026-07-27) are handled by the backoff instead
    assert _cfg.WU_EXCLUDE_STATIONS == {"KTXSHALL25"}
    found = [("KTXLUBBO851", 0.1), ("KTXSHALL25", 11.0), ("KTXSHALL7", 0.6)]
    monkeypatch.setattr(_wu, "discover_stations", lambda lat, lon: list(found))
    p = RainPoller(_cfg, _NullEventLog())
    p._ensure_stations(now=time.time())
    ids = [s for s, d in p._stations]
    assert "KTXSHALL25" not in ids, "excluded station still in the polled set"
    assert "KTXLUBBO851" in ids and "KTXSHALL7" in ids   # the others survive
    # the audit trail names the exclusion
    ev = _NullEventLog.last_detail
    assert ev and "excluded by config: KTXSHALL25" in ev


def test_exclusion_is_case_insensitive_and_removable(monkeypatch):
    from safety import config as _cfg
    from safety import wu_poll as _wu
    monkeypatch.setattr(_cfg, "WU_EXCLUDE_STATIONS", frozenset({"KTXSHALL25"}))
    monkeypatch.setattr(_wu, "discover_stations",
                        lambda lat, lon: [("ktxshall25", 11.0), ("KTXSHALL7", 0.6)])
    p = RainPoller(_cfg, _NullEventLog())
    p._ensure_stations(now=time.time())
    assert [s for s, d in p._stations] == ["KTXSHALL7"]  # matched despite lowercase
    # emptying the list (TTU_SAFETY_WU_EXCLUDE="") restores the station
    monkeypatch.setattr(_cfg, "WU_EXCLUDE_STATIONS", frozenset())
    p._stations_ts = 0.0                                 # force re-discovery
    p._ensure_stations(now=time.time())
    assert "ktxshall25" in [s for s, d in p._stations]


class _NullEventLog:
    last_detail = None

    def record(self, action, **kw):
        _NullEventLog.last_detail = kw.get("detail")


def _poller_with_stations(monkeypatch, stations):
    from safety import config as _cfg
    p = RainPoller(_cfg, _NullEventLog())
    p._stations = list(stations)
    p._stations_ts = time.time()          # discovery fresh: no network in _ensure
    monkeypatch.setattr(_cfg, "WU_API_KEY", "test-key", raising=False)
    return p


def test_dead_station_backs_off_and_revives(monkeypatch):
    # After WU_BACKOFF_AFTER consecutive silent polls the station is probed only every
    # WU_BACKOFF_RETRY_SEC; the first ANSWER restores full cadence automatically.
    from safety import config as _cfg
    from safety import wu_poll as _wu
    p = _poller_with_stations(monkeypatch, [("KGOOD1", 1.0), ("KDEAD1", 5.0)])
    monkeypatch.setattr(_cfg, "WU_BACKOFF_AFTER", 3)
    monkeypatch.setattr(_cfg, "WU_BACKOFF_RETRY_SEC", 3600)
    dead = {"KDEAD1"}
    polled_log = []

    def fake_poll(stations, max_age_min=None):
        sids = [s for s, d in stations]
        polled_log.append(sids)
        results = [{"station": s, "state": "offline"} if s in dead
                   else {"station": s, "state": "live", "precip_in_hr": 0.0, "age_min": 1}
                   for s in sids]
        return {"live": len([s for s in sids if s not in dead]), "total": len(sids),
                "raining": [], "max_rate": 0.0, "results": results}
    monkeypatch.setattr(_wu, "poll_stations", fake_poll)

    for _ in range(3):                     # three silent polls -> backoff arms
        r = p.poll_now()
    assert r["total"] == 2                 # totals stay honest
    r = p.poll_now()                       # 4th poll: dead station must be skipped
    assert polled_log[-1] == ["KGOOD1"], "backed-off station was still polled"
    assert {"station": "KDEAD1", "state": "backed-off"} in r["results"]
    assert r["total"] == 2 and r["live"] == 1

    # hourly revival probe: pretend the hour passed, and the station answers
    p._backoff_until["KDEAD1"] = time.monotonic() - 1
    dead.clear()
    p.poll_now()
    assert "KDEAD1" in polled_log[-1]      # probe happened
    p.poll_now()
    assert "KDEAD1" in polled_log[-1], "revived station did not resume full cadence"
    assert _NullEventLog.last_detail and "answering again" in _NullEventLog.last_detail


def test_stale_station_never_backs_off(monkeypatch):
    # stale = responding with old data: alive, may freshen — keep polling it
    from safety import config as _cfg
    from safety import wu_poll as _wu
    p = _poller_with_stations(monkeypatch, [("KSTALE1", 2.0)])
    monkeypatch.setattr(_cfg, "WU_BACKOFF_AFTER", 2)
    monkeypatch.setattr(_wu, "poll_stations", lambda st, max_age_min=None: {
        "live": 0, "total": len(st), "raining": [], "max_rate": 0.0,
        "results": [{"station": s, "state": "stale", "age_min": 90} for s, d in st]})
    for _ in range(5):
        p.poll_now()
    assert p._backoff_until == {}, "a stale (responding) station was backed off"


def test_discovery_refresh_prunes_backoff_state(monkeypatch):
    from safety import wu_poll as _wu
    p = _poller_with_stations(monkeypatch, [("KOLD1", 2.0)])
    p._offline_streak["KOLD1"] = 99
    p._backoff_until["KOLD1"] = time.monotonic() + 999
    monkeypatch.setattr(_wu, "discover_stations", lambda lat, lon: [("KNEW1", 1.0)])
    p._stations_ts = 0.0
    p._ensure_stations(now=time.time())
    assert p._offline_streak == {} and p._backoff_until == {}


# --- advertised Alpaca address (must never be loopback on a wildcard bind) ---------
def _reset_ip_cache():
    monitor_mod._ip_cache["ip"], monitor_mod._ip_cache["ts"] = None, 0.0


def test_failed_ip_detection_is_not_cached(monkeypatch):
    # THE BUG: the daemon starts before the network is up (Pi boot after a power cut),
    # detection fails, and the old code cached 127.0.0.1 for the life of the process —
    # the page advertised loopback for days. A failure must never be remembered.
    _reset_ip_cache()
    monkeypatch.setattr(monitor_mod.socket, "socket",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    assert monitor_mod._primary_ip() is None
    assert monitor_mod._ip_cache["ip"] is None, "a failure was cached"

    class _Sock:                               # the network comes up later
        def connect(self, addr):
            pass

        def getsockname(self):
            return ("129.118.86.107", 51234)

        def close(self):
            pass
    monkeypatch.setattr(monitor_mod.socket, "socket", lambda *a, **k: _Sock())
    assert monitor_mod._primary_ip() == "129.118.86.107", \
        "the address did not self-correct once the network was up"


def test_loopback_detection_is_never_cached_as_the_address(monkeypatch):
    _reset_ip_cache()

    class _Loop:
        def connect(self, addr):
            pass

        def getsockname(self):
            return ("127.0.0.1", 1)

        def close(self):
            pass
    monkeypatch.setattr(monitor_mod.socket, "socket", lambda *a, **k: _Loop())
    assert monitor_mod._primary_ip() is None and monitor_mod._ip_cache["ip"] is None


def test_detected_address_is_refreshed_not_frozen(monkeypatch):
    _reset_ip_cache()
    current = ["10.0.0.5"]

    class _S:
        def connect(self, addr):
            pass

        def getsockname(self):
            return (current[0], 1)

        def close(self):
            pass
    monkeypatch.setattr(monitor_mod.socket, "socket", lambda *a, **k: _S())
    t = 1000.0
    assert monitor_mod._primary_ip(t) == "10.0.0.5"
    current[0] = "10.0.0.9"                                  # DHCP moved us
    assert monitor_mod._primary_ip(t + 10) == "10.0.0.5"     # still cached
    assert monitor_mod._primary_ip(t + monitor_mod.IP_REFRESH_SEC + 1) == "10.0.0.9"


def test_advertised_address_for_wildcard_and_explicit_binds(monkeypatch):
    from safety import config as _cfg
    _reset_ip_cache()

    class _S:
        def connect(self, addr):
            pass

        def getsockname(self):
            return ("129.118.86.107", 1)

        def close(self):
            pass
    monkeypatch.setattr(monitor_mod.socket, "socket", lambda *a, **k: _S())
    monkeypatch.setattr(_cfg, "HTTP_HOST", "0.0.0.0")
    assert monitor_mod.alpaca_address(_cfg) == "129.118.86.107"   # the LAN address
    # an explicit bind is authoritative — including loopback, which is then the truth
    monkeypatch.setattr(_cfg, "HTTP_HOST", "127.0.0.1")
    assert monitor_mod.alpaca_address(_cfg) == "127.0.0.1"
    monkeypatch.setattr(_cfg, "HTTP_HOST", "192.168.1.50")
    assert monitor_mod.alpaca_address(_cfg) == "192.168.1.50"


def test_advertised_address_falls_back_to_hostname_not_loopback(monkeypatch):
    from safety import config as _cfg
    _reset_ip_cache()
    monkeypatch.setattr(monitor_mod.socket, "socket",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no route")))
    monkeypatch.setattr(monitor_mod.socket, "gethostname", lambda: "ttu-pi")
    monkeypatch.setattr(_cfg, "HTTP_HOST", "0.0.0.0")
    assert monitor_mod.alpaca_address(_cfg) == "ttu-pi"   # never a bogus 127.0.0.1


# --- NWS hazards: the narrow warning veto, and hazard INFORMATION (never gates) -----
# The owner's rule: a Tornado / Dust Storm / High Wind Warning OVER THE SITE makes the
# monitor unsafe for the life of the warning; everything else is information only.

class _StubHazards:
    """Stands in for NwsAlertsPoller / HazardFeedsPoller: returns (or raises) whatever the
    test puts in .comp, and records the `now` it was asked about."""

    def __init__(self, comp):
        self.comp = comp
        self.calls = []

    def component(self, now=None):
        self.calls.append(now)
        if isinstance(self.comp, BaseException):
            raise self.comp
        return self.comp


_VETO_EVENTS = ["Tornado Warning", "Dust Storm Warning", "High Wind Warning"]


def _veto(event="Tornado Warning", key="KLUB.TO.W.0012", end_ts=None, source="both",
          sender="NWS Lubbock TX", end_local="18:45 CDT"):
    return {"key": key, "event": event, "headline": f"{event} issued by {sender}",
            "sender": sender, "end_ts": time.time() + 1800 if end_ts is None else end_ts,
            "end_local": end_local, "first_seen_ts": time.time() - 60, "source": source}


def _alert(event, vetoes=False, color="#FF0000"):
    return {"key": f"KLUB.{event[:2].upper()}.1", "event": event, "kind": "warning",
            "severity": "Severe", "urgency": "Immediate", "headline": f"{event} for Lubbock",
            "nws_headline": None, "sender": "NWS Lubbock TX", "area_desc": "Lubbock",
            "onset": None, "ends": None, "expires": None, "end_local": "20:00 CDT",
            "color": color, "threat": None, "vetoes": vetoes, "geometry_source": "zones",
            "description": "", "instruction": ""}


def _hazards_comp(veto=(), available=True, safe=None, at_site=(), nearby=()):
    veto, at_site, nearby = list(veto), list(at_site), list(nearby)
    return {"safe": (not veto) if safe is None else safe, "enabled": True,
            "available": available, "veto_events": list(_VETO_EVENTS), "veto": veto,
            "at_site": at_site, "nearby": nearby,
            "counts": {"at_site": len(at_site), "nearby": len(nearby)},
            "point_age_s": 20, "area_age_s": 70, "error": None,
            "source": "NWS api.weather.gov active alerts"}


def _mon(env, hazards=None, hazard_feeds=None):
    return SafetyMonitor(env["cfg"], env["log"], env["poller"],
                         hazards=hazards, hazard_feeds=hazard_feeds)


_REASON_RE = re.compile(r"^NWS Tornado Warning in effect for the site until "
                        r"(\w{3} )?\d\d:\d\d C[SD]T \(NWS Lubbock TX\)$")


def test_hazard_veto_makes_unsafe_with_reason(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    hz = _StubHazards(_hazards_comp(veto=[_veto()]))
    st = _mon(env, hazards=hz).evaluate()
    assert st["is_safe"] is False
    assert any(_REASON_RE.match(r) for r in st["reasons"]), st["reasons"]
    assert st["components"]["hazards"]["veto"][0]["event"] == "Tornado Warning"
    assert hz.calls and abs(hz.calls[-1] - time.time()) < 5      # asked about "now"


@pytest.mark.parametrize("event", _VETO_EVENTS)
def test_each_default_veto_event_vetoes(env, write_inputs, event):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env, hazards=_StubHazards(_hazards_comp(veto=[_veto(event=event)]))).evaluate()
    assert st["is_safe"] is False
    assert any(r.startswith(f"NWS {event} in effect for the site until ")
               for r in st["reasons"])


def test_veto_holds_even_when_page_inputs_are_fine_and_feed_unavailable(env, write_inputs):
    # a held (latched) veto is in force whether or not the feed is currently fresh
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    comp = _hazards_comp(veto=[_veto(source="latched")], available=False)
    st = _mon(env, hazards=_StubHazards(comp)).evaluate()
    assert st["is_safe"] is False
    assert any("held to its end time" in r for r in st["reasons"])


def test_veto_reason_text_matches_the_contract_example(env):
    # 2026-06-05 23:00Z = 18:00 CDT; the warning ends 23:45Z = 18:45 CDT
    now = datetime(2026, 6, 5, 23, 0, tzinfo=timezone.utc).timestamp()
    end = datetime(2026, 6, 5, 23, 45, tzinfo=timezone.utc).timestamp()
    v = _veto(end_ts=end, end_local="ignored when end_ts is usable")
    assert monitor_mod.hazard_veto_reason(v, env["cfg"], now) == \
        "NWS Tornado Warning in effect for the site until 18:45 CDT (NWS Lubbock TX)"


def test_veto_reason_names_the_weekday_when_the_end_is_not_today(env):
    now = datetime(2026, 1, 21, 3, 0, tzinfo=timezone.utc).timestamp()     # Tue 21:00 CST
    end = datetime(2026, 1, 21, 15, 0, tzinfo=timezone.utc).timestamp()    # Wed 09:00 CST
    v = _veto(event="High Wind Warning", end_ts=end)
    assert monitor_mod.hazard_veto_reason(v, env["cfg"], now) == \
        "NWS High Wind Warning in effect for the site until Wed 09:00 CST (NWS Lubbock TX)"


def test_veto_reason_without_a_usable_end_time(env):
    cfg, now = env["cfg"], time.time()
    v = _veto(end_ts=float("nan"), end_local="7:15 PM CDT")
    assert monitor_mod.hazard_veto_reason(v, cfg, now).endswith(
        "until 7:15 PM CDT (NWS Lubbock TX)")
    v = dict(_veto(), end_ts=None, end_local=None, sender=None)
    assert monitor_mod.hazard_veto_reason(v, cfg, now) == \
        "NWS Tornado Warning in effect for the site (no end time given)"
    v = dict(_veto(), end_ts=1e300)                               # absurd -> fall back
    assert "until 18:45 CDT" in monitor_mod.hazard_veto_reason(v, cfg, now)


def test_non_veto_alerts_at_the_site_never_gate(env, write_inputs):
    # Red Flag / Severe Thunderstorm / Flood Warning over the site: shown, not a veto
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    at_site = [_alert("Red Flag Warning"), _alert("Severe Thunderstorm Warning"),
               _alert("Flash Flood Warning")]
    nearby = [_alert("Tornado Warning", color="#FF0000")]      # a tornado 60 km away
    st = _mon(env, hazards=_StubHazards(_hazards_comp(at_site=at_site,
                                                      nearby=nearby))).evaluate()
    assert st["is_safe"] is True and st["reasons"] == []
    assert st["components"]["hazards"]["counts"] == {"at_site": 3, "nearby": 1}


def test_hazards_unavailable_does_not_veto(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    comp = _hazards_comp(available=False)
    comp["error"] = "HTTP Error 503"
    st = _mon(env, hazards=_StubHazards(comp)).evaluate()
    assert st["is_safe"] is True
    assert st["components"]["hazards"]["available"] is False


def test_no_hazards_poller_is_safe_and_reports_unavailable(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env).evaluate()
    hz, info = st["components"]["hazards"], st["components"]["hazard_info"]
    assert st["is_safe"] is True
    assert hz["safe"] is True and hz["available"] is False and hz["veto"] == []
    assert info["safe"] is True and info["info_only"] is True


def test_veto_entry_vetoes_even_if_the_safe_flag_disagrees(env, write_inputs):
    # fail-safe: either signal vetoes
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env, hazards=_StubHazards(_hazards_comp(veto=[_veto()], safe=True))).evaluate()
    assert st["is_safe"] is False and any(_REASON_RE.match(r) for r in st["reasons"])
    comp = _hazards_comp(safe=False)
    comp["error"] = "point query says so"
    st = _mon(env, hazards=_StubHazards(comp)).evaluate()
    assert st["is_safe"] is False
    assert any(r.startswith("NWS hazard layer reports unsafe") for r in st["reasons"])


def test_unreadable_veto_entry_still_vetoes(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env, hazards=_StubHazards(_hazards_comp(veto=["TO.W"], safe=False))).evaluate()
    assert st["is_safe"] is False
    assert any("unreadable veto entry" in r for r in st["reasons"])


def test_broken_hazard_layer_never_releases_a_held_veto(env, write_inputs):
    # A bug in the code that REPORTS a tornado warning must not reopen the dome: the
    # vetoes last reported are carried forward to their own end time.
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    hz = _StubHazards(_hazards_comp(veto=[_veto(end_ts=time.time() + 900)]))
    m = _mon(env, hazards=hz)
    assert m.evaluate()["is_safe"] is False
    for broken in (RuntimeError("boom"), None, ["not", "a", "dict"],
                   {"safe": "yes", "veto": []}, {"safe": True, "veto": "TO.W"}):
        hz.comp = broken
        st = m.evaluate()
        assert st["is_safe"] is False, broken
        assert st["components"]["hazards"]["veto"][0]["source"] == "latched"
        assert "hazard layer error" in st["components"]["hazards"]["error"]
        assert any("held to its end time" in r for r in st["reasons"])


def test_carried_veto_runs_out_at_its_end_time(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    hz = _StubHazards(_hazards_comp(veto=[_veto(end_ts=time.time() - 1)]))
    m = _mon(env, hazards=hz)
    m.evaluate()
    hz.comp = RuntimeError("boom")
    st = m.evaluate()
    assert st["is_safe"] is True                   # ended: nothing to carry
    assert st["components"]["hazards"]["available"] is False


def test_carried_veto_without_end_time_is_bounded(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    v = dict(_veto(), end_ts=None)
    m = _mon(env, hazards=_StubHazards(_hazards_comp(veto=[v])))
    m.evaluate()
    now = time.time()
    m.hazards.comp = RuntimeError("boom")
    assert m._held_vetoes(now + 60) != []
    assert m._held_vetoes(now + monitor_mod.HAZARD_NO_END_HOLD_SEC + 60) == []


def test_broken_hazard_layer_without_prior_veto_is_unavailable_not_unsafe(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env, hazards=_StubHazards(RuntimeError("boom"))).evaluate()
    assert st["is_safe"] is True
    hz = st["components"]["hazards"]
    assert hz["enabled"] is True and hz["available"] is False and "boom" in hz["error"]


def test_veto_released_when_the_layer_reports_it_gone(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    hz = _StubHazards(_hazards_comp(veto=[_veto()]))
    m = _mon(env, hazards=hz)
    assert m.evaluate()["is_safe"] is False
    hz.comp = _hazards_comp()                      # cancelled / expired, fresh data
    assert m.evaluate()["is_safe"] is True
    hz.comp = RuntimeError("boom")                 # nothing held any more
    assert m.evaluate()["is_safe"] is True


def test_veto_is_written_to_the_state_file(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    _mon(env, hazards=_StubHazards(_hazards_comp(veto=[_veto()]))).evaluate()
    with open(env["cfg"].STATE_FILE, encoding="utf-8") as f:
        st = json.load(f)
    assert st["is_safe"] is False
    assert st["components"]["hazards"]["veto"][0]["key"] == "KLUB.TO.W.0012"
    assert st["components"]["hazard_info"]["info_only"] is True


def test_state_is_rewritten_at_once_when_the_veto_set_changes(env, write_inputs, monkeypatch):
    # SD-wear throttle: unchanged state is written only on the heartbeat — but a change in
    # WHICH warnings veto (or an extension of one) must be written immediately, even when
    # the reason text reads the same.
    monkeypatch.setattr(env["cfg"], "STATE_WRITE_HEARTBEAT_SEC", 10 ** 6)
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    end = time.time() + 1800
    hz = _StubHazards(_hazards_comp(veto=[_veto(key="KLUB.TO.W.0012", end_ts=end)]))
    m = _mon(env, hazards=hz)
    writes = []
    monkeypatch.setattr(m, "_write_state", lambda state: writes.append(state))
    m.evaluate()
    m.evaluate()
    assert len(writes) == 1                        # unchanged -> throttled
    reasons = writes[0]["reasons"]
    hz.comp = _hazards_comp(veto=[_veto(key="KLUB.TO.W.0013", end_ts=end)])
    m.evaluate()
    assert len(writes) == 2, "a new vetoing warning was not written at once"
    assert writes[1]["reasons"] == reasons         # ...although the reasons read the same
    hz.comp = _hazards_comp(veto=[_veto(key="KLUB.TO.W.0013", end_ts=end + 0.5)])
    m.evaluate()
    assert len(writes) == 3, "an extended warning was not written at once"
    m.evaluate()
    assert len(writes) == 3


# --- hazard INFORMATION never influences IsSafe --------------------------------------
class _Weird:
    pass


_INFO_PAYLOADS = [
    {"safe": False, "info_only": False, "enabled": True, "veto": [_veto()],
     "reasons": ["M7.9 earthquake 5 km from the site"], "available": False,
     "latched": True},
    {"safe": False},
    {"quakes": [{"mag": 7.9, "place": "5 km E of Lubbock"}],
     "smoke": {"density": "Heavy", "over_site": True},
     "spc": {"category": "HIGH", "label": "High Risk", "mds": [{"num": 1}]},
     "fires": [{"name": "Yellow Lake", "acres": 1e6}],
     "lsr": [{"type": "TORNADO"}], "space_weather": {"kp": 9, "g": "G5"}},
    {"when": datetime(2026, 9, 24, tzinfo=timezone.utc), "set": {1, 2}, "obj": _Weird()},
    None, [], "unsafe", 42, float("nan"),
    RuntimeError("feeds exploded"), ValueError("bad KML"),
]


@pytest.mark.parametrize("payload", _INFO_PAYLOADS,
                         ids=[f"payload{i}" for i in range(len(_INFO_PAYLOADS))])
@pytest.mark.parametrize("sun", [-10.0, 12.0])            # a safe and an unsafe baseline
def test_hazard_info_never_changes_is_safe(env, write_inputs, payload, sun):
    write_inputs(env["cfg"], sun=sun, humidity=40.0)
    base = _mon(env).evaluate()
    info = _StubHazards(payload)
    st = _mon(env, hazard_feeds=info).evaluate()           # must not raise
    assert info.calls, "the info component was not consulted at all"
    assert st["is_safe"] is base["is_safe"]
    assert st["reasons"] == base["reasons"]
    comp = st["components"]["hazard_info"]
    assert comp["safe"] is True and comp["info_only"] is True
    with open(env["cfg"].STATE_FILE, encoding="utf-8") as f:
        assert json.load(f)["is_safe"] is base["is_safe"]   # state file still written


def test_hazard_info_cannot_mask_a_real_veto_either(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env, hazards=_StubHazards(_hazards_comp(veto=[_veto()])),
              hazard_feeds=_StubHazards({"safe": True, "veto": []})).evaluate()
    assert st["is_safe"] is False


def test_hazard_info_error_is_reported_not_raised(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env, hazard_feeds=_StubHazards(RuntimeError("feeds exploded"))).evaluate()
    comp = st["components"]["hazard_info"]
    assert comp["enabled"] is True and "feeds exploded" in comp["error"]


def test_component_is_detached_from_the_poller(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    payload = {"quakes": [{"text": "M3.1 12 km E of Snyder"}]}
    st = _mon(env, hazard_feeds=_StubHazards(payload)).evaluate()
    payload["quakes"].append({"text": "mutated later by the poller thread"})
    assert len(st["components"]["hazard_info"]["quakes"]) == 1


def test_broken_layer_is_logged_once_not_on_every_evaluation(env, write_inputs, caplog):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    m = _mon(env, hazards=_StubHazards(RuntimeError("boom")),
             hazard_feeds=_StubHazards(RuntimeError("bang")))
    with caplog.at_level(logging.ERROR, logger="ttu.safety.monitor"):
        for _ in range(5):
            m.evaluate()
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("hazards component failed" in x for x in msgs) == 1
    assert sum("hazard_info component failed" in x for x in msgs) == 1


def test_missing_hazard_modules_degrade_to_unavailable(env, write_inputs, monkeypatch):
    # a partial deploy (or a syntax slip) in a hazard module must not stop the daemon
    monkeypatch.setattr(monitor_mod, "nws_alerts", None)
    monkeypatch.setattr(monitor_mod, "NWS_ALERTS_IMPORT_ERROR", "SyntaxError: bad")
    monkeypatch.setattr(monitor_mod, "hazard_feeds", None)
    monkeypatch.setattr(monitor_mod, "HAZARD_FEEDS_IMPORT_ERROR", "ImportError: gone")
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env).evaluate()
    assert st["is_safe"] is True
    hz, info = st["components"]["hazards"], st["components"]["hazard_info"]
    assert "failed to import" in hz["error"] and hz["veto"] == []
    assert hz["veto_events"] == list(env["cfg"].HAZARD_VETO_EVENTS)
    assert "failed to import" in info["error"] and info["info_only"] is True


def test_import_layer_reports_why_instead_of_raising():
    mod, err = monitor_mod._import_layer("no_such_hazard_module")
    assert mod is None and err.startswith("ModuleNotFoundError")
    mod, err = monitor_mod._import_layer("nws_forecast")
    assert mod is not None and err is None


def test_unavailable_component_of_a_buggy_module_is_replaced(env, write_inputs, monkeypatch):
    class _Buggy:
        @staticmethod
        def unavailable_component(cfg):
            raise KeyError("HAZARD_SOMETHING")
    monkeypatch.setattr(monitor_mod, "nws_alerts", _Buggy)
    monkeypatch.setattr(monitor_mod, "hazard_feeds", _Buggy)
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = _mon(env).evaluate()
    assert st["is_safe"] is True
    assert st["components"]["hazards"]["available"] is False
    assert st["components"]["hazard_info"]["info_only"] is True


# --- hazard configuration (C1) -----------------------------------------------------
def test_hazard_config_defaults():
    from safety import config as c
    assert c.HAZARDS_ENABLED is True and c.HAZARD_FEEDS_ENABLED is True
    assert c.HAZARD_VETO_EVENTS == ("Tornado Warning", "Dust Storm Warning",
                                    "High Wind Warning")
    assert (c.HAZARD_POINT_POLL_SEC, c.HAZARD_AREA_POLL_SEC, c.HAZARD_STALE_AFTER_SEC) == \
        (60, 120, 600)
    assert c.HAZARD_LATCH_FILE.endswith("safety_hazard_latch.json")
    assert c.HAZARD_CACHE_DIR.endswith(".cache/ttu-hazards")
    assert c.HAZARD_FEEDS_POLL_SEC == 600
    assert (c.HAZARD_QUAKE_RADIUS_KM, c.HAZARD_QUAKE_MIN_MAG) == (300.0, 2.5)
    assert c.HAZARD_LSR_HOURS == 24 and c.HAZARD_ALERT_FILL_ALPHA == 60
    assert c.HAZARD_AREA_STATES == "auto"
    # a warning issued ahead vetoes from 15 min before its onset (read by nws_alerts)
    from safety import nws_alerts
    assert c.HAZARD_VETO_ONSET_LEAD_SEC == nws_alerts.ONSET_LEAD_SEC == 900
    assert nws_alerts.NwsAlertsPoller._onset_lead(types.SimpleNamespace(cfg=c)) == 900.0
    # the defaults are spelled exactly as NWS spells them (a typo would never veto)
    assert all(e.lower() in c.NWS_EVENT_NAMES for e in c.HAZARD_VETO_EVENTS)


def test_veto_event_list_parsing_is_case_insensitive_and_warns_on_typos(monkeypatch):
    from safety import config as c
    monkeypatch.setattr(c, "CONFIG_WARNINGS", [])
    assert c._parse_event_names(" tornado  WARNING,Tornado Warning,, dust storm warning ") \
        == ("Tornado Warning", "Dust Storm Warning")
    assert c.CONFIG_WARNINGS == []
    assert c._parse_event_names("Tornado Warnign") == ("Tornado Warnign",)   # kept...
    assert len(c.CONFIG_WARNINGS) == 1 and "Tornado Warnign" in c.CONFIG_WARNINGS[0]
    assert c._parse_event_names("") == ()                                   # display-only
    # legacy names are known too (old configs keep working without a warning)
    monkeypatch.setattr(c, "CONFIG_WARNINGS", [])
    assert c._parse_event_names("excessive heat warning") == ("Excessive Heat Warning",)
    assert c.CONFIG_WARNINGS == []


def test_area_states_and_range_parsing(monkeypatch):
    from safety import config as c
    monkeypatch.setattr(c, "CONFIG_WARNINGS", [])
    assert c._parse_states("auto") == "auto" and c._parse_states(" ") == "auto"
    assert c._parse_states("tx, nm,TX") == "TX,NM"
    assert c.CONFIG_WARNINGS == []
    assert c._parse_states("Texas") == "auto" and len(c.CONFIG_WARNINGS) == 1
    assert c._clamp("X", 60, 0, 255) == 60
    assert c._clamp("X", 900, 0, 255) == 255 and c._clamp("X", 5, 30) == 30
    assert len(c.CONFIG_WARNINGS) == 3


def test_stale_time_is_kept_above_the_slower_alert_query():
    # a stale time below the area cadence would blank the map and the nearby list (and
    # forbid every early release) for part of each area-poll cycle; and one bad state code
    # is dropped instead of making api.weather.gov reject every area query
    import os
    import subprocess
    import sys
    env = dict(os.environ, TTU_SAFETY_HAZARD_AREA_POLL_SEC="900",
               TTU_SAFETY_HAZARD_AREA_STATES="TX,NW")
    out = subprocess.run(
        [sys.executable, "-c", "from safety import config as c; "
         "print(c.HAZARD_STALE_AFTER_SEC, c.HAZARD_AREA_STATES); "
         "print('|'.join(c.CONFIG_WARNINGS))"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        env=env, capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == "1800 TX"
    assert "TTU_SAFETY_HAZARD_STALE_SEC=600" in out[1] and "NW" in out[1]
