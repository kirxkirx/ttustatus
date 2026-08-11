"""Tests for the full-codebase-review fixes: coordinate adoption, humidity age,
state-write throttling, WU distance filter, and coverage guards."""
import json
import time

from safety import config, wu_poll
from safety import glm_lightning as gl
from safety import radar as rd
from safety.monitor import SafetyMonitor


def _write_inputs(cfg, **kw):
    d = {"ts": time.time(), "sun_altitude_deg": -10.0, "humidity_pct": 40.0}
    d.update(kw)
    with open(cfg.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)


# ---- coordinate adoption ---------------------------------------------------
def test_geocode_adopted_from_gps_when_env_unset(env, monkeypatch):
    monkeypatch.setattr(config, "GEOCODE_FROM_ENV", False)
    monkeypatch.setattr(config, "GEOCODE", (33.75, -101.96))
    _write_inputs(env["cfg"], lat=35.19845, lon=-111.65432)   # Flagstaff-ish
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])
    m.evaluate()
    assert config.GEOCODE == (35.198, -111.654)       # adopted, rounded to ~100 m
    assert "GPS" in config.GEOCODE_SOURCE
    # jitter later must NOT change it (locked after adoption)
    _write_inputs(env["cfg"], lat=35.19851, lon=-111.65440)   # tens-of-m wobble
    m.evaluate()
    assert config.GEOCODE == (35.198, -111.654)


def test_geocode_env_set_never_adopts_but_warns_on_mismatch(env, monkeypatch):
    monkeypatch.setattr(config, "GEOCODE_FROM_ENV", True)
    monkeypatch.setattr(config, "GEOCODE", (33.75, -101.96))
    _write_inputs(env["cfg"], lat=33.756, lon=-101.96)         # ~670 m away > 100 m
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])
    st = m.evaluate()
    assert config.GEOCODE == (33.75, -101.96)                  # unchanged
    assert any("km from the configured site" in w for w in st["warnings"])
    assert st["is_safe"] is True                               # warning, not a veto


def test_geocode_small_jitter_no_warning(env, monkeypatch):
    monkeypatch.setattr(config, "GEOCODE_FROM_ENV", True)
    monkeypatch.setattr(config, "GEOCODE", (33.75, -101.96))
    _write_inputs(env["cfg"], lat=33.7503, lon=-101.9602)      # ~40 m, within 100 m
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])
    assert m.evaluate()["warnings"] == []


# ---- humidity measurement age ----------------------------------------------
def test_stale_cached_humidity_fails_safe(env, monkeypatch):
    monkeypatch.setattr(config, "GEOCODE_FROM_ENV", True)
    _write_inputs(env["cfg"], humidity_pct=40.0,
                  humidity_age_s=config.INPUTS_STALE_SEC + 60)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])
    st = m.evaluate()
    assert st["is_safe"] is False
    assert any("humidity unknown" in r for r in st["reasons"])


def test_fresh_humidity_age_ok(env, monkeypatch):
    monkeypatch.setattr(config, "GEOCODE_FROM_ENV", True)
    _write_inputs(env["cfg"], humidity_pct=40.0, humidity_age_s=30.0)
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])
    assert m.evaluate()["is_safe"] is True


# ---- state-write throttle ---------------------------------------------------
def test_state_write_throttled_when_unchanged(env, monkeypatch):
    import os
    monkeypatch.setattr(config, "GEOCODE_FROM_ENV", True)
    _write_inputs(env["cfg"])
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"])
    m.evaluate()
    mtime1 = os.path.getmtime(config.STATE_FILE)
    m.evaluate()                                   # unchanged within heartbeat
    assert os.path.getmtime(config.STATE_FILE) == mtime1
    # a decision change forces an immediate write
    _write_inputs(env["cfg"], sun_altitude_deg=5.0)   # sun up -> unsafe
    m.evaluate()
    assert os.path.getmtime(config.STATE_FILE) >= mtime1
    assert json.load(open(config.STATE_FILE))["is_safe"] is False


# ---- WU station distance filter ---------------------------------------------
def test_wu_discovery_drops_far_stations(env, monkeypatch):
    from safety.monitor import RainPoller
    monkeypatch.setattr(wu_poll, "discover_stations",
                        lambda lat, lon: [("NEAR", 5.0), ("FAR", 250.0)])
    p = RainPoller(config, env["log"])
    p._ensure_stations(time.time())
    assert [s for s, d in p._stations] == ["NEAR"]


def test_wu_discovery_all_far_leaves_empty(env, monkeypatch):
    from safety.monitor import RainPoller
    monkeypatch.setattr(wu_poll, "discover_stations",
                        lambda lat, lon: [("FAR1", 300.0), ("FAR2", 400.0)])
    p = RainPoller(config, env["log"])
    p._ensure_stations(time.time())
    assert p._stations == []


# ---- coverage guards ---------------------------------------------------------
def test_radar_coverage_guard():
    assert rd.site_in_coverage(33.75, -101.96) is True         # TTU
    assert rd.site_in_coverage(48.2, 16.4) is False            # Vienna
    assert rd.site_in_coverage(-33.9, 18.4) is False           # Cape Town


def test_glm_fov_guard():
    assert gl.site_in_fov(33.75, -101.96) is True              # TTU
    assert gl.site_in_fov(-33.45, -70.66) is True              # Santiago (GOES-East sees SA)
    assert gl.site_in_fov(48.2, 16.4) is False                 # Vienna
