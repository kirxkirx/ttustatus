import time

from safety import config, wu_poll
from safety.monitor import SafetyMonitor


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
    # latch persists ~3 h; simulate expiry
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
            "trigger_km": 50.0, "cooloff_hours": 3.0, "source": "GLM"}


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
            "age_s": 60, "trigger_km": 50.0, "dbz": 20.0, "polling_active": True,
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
