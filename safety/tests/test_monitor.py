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


def test_env_float_rejects_nonfinite(monkeypatch):
    monkeypatch.setenv("TTU_TEST_FLOAT", "nan")
    assert config._env_float("TTU_TEST_FLOAT", 3.0) == 3.0
    monkeypatch.setenv("TTU_TEST_FLOAT", "inf")
    assert config._env_float("TTU_TEST_FLOAT", 3.0) == 3.0
    monkeypatch.setenv("TTU_TEST_FLOAT", "2.5")
    assert config._env_float("TTU_TEST_FLOAT", 3.0) == 2.5
