import time

from safety import config
from safety import glm_lightning as gl


class _Log:
    def record(self, *a, **k):
        return {}


def _prep(tmp_path, monkeypatch, deps=True):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    monkeypatch.setattr(gl, "deps_available", lambda: deps)


def _res(in_ring, near=None, bearing="N", ok=True):
    return {"ok": ok, "in_ring": in_ring, "flashes_in_ring": 2 if in_ring else 0,
            "nearest_km": near, "nearest_bearing": bearing, "granules_scanned": 15}


def test_poll_sets_latch_on_in_ring(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch)
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: _res(True, 12.3, "NW"))
    p = gl.GlmLightningPoller(config, _Log())
    p.poll_now(now=1000.0)
    c = p.component(sun_alt=-10.0, now=1001.0)
    assert c["latched"] is True and c["safe"] is False
    assert c["nearest_km"] == 12.3 and c["in_ring"] is True
    # clears after the cool-off / freeze (30 min)
    later = 1000.0 + config.GLM_COOLOFF_HOURS * 3600 + 1
    assert p.component(-10.0, later)["latched"] is False
    # still latched just BEFORE the window closes (the freeze really lasts its full time)
    assert p.component(-10.0, 1000.0 + config.GLM_COOLOFF_HOURS * 3600 - 5)["latched"] is True


def test_cooloff_default_is_thirty_minutes():
    # the operator-chosen freeze time; a stray edit must not pass unnoticed
    assert config.GLM_COOLOFF_HOURS == 0.5


def test_no_ring_no_latch(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch)
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: _res(False, 833.0, "NE"))
    p = gl.GlmLightningPoller(config, _Log())
    p.poll_now(now=1000.0)
    c = p.component(-10.0, 1001.0)
    assert c["safe"] is True and c["latched"] is False and c["nearest_km"] == 833.0


def test_maybe_poll_gated_by_sun(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(gl, "poll_flashes",
                        lambda cfg: calls.append(1) or _res(False, None))
    p = gl.GlmLightningPoller(config, _Log())
    assert p.maybe_poll(sun_alt=10.0, now=1.0) is None       # daytime -> skip
    assert not calls
    assert p.maybe_poll(sun_alt=-5.0, now=2.0) is not None    # night -> polls
    assert calls


def test_deps_absent_disables(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch, deps=False)
    p = gl.GlmLightningPoller(config, _Log())
    assert p.maybe_poll(-10.0, 1.0) is None                   # no deps -> no poll
    c = p.component(-10.0, 1.0)
    assert c["enabled"] is False and c["available"] is False and c["safe"] is True


def test_fetch_error_keeps_latch(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch)
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: _res(True, 10.0))
    p = gl.GlmLightningPoller(config, _Log())
    p.poll_now(now=1000.0)
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: {"ok": False, "error": "network"})
    p.poll_now(now=1100.0)                                     # failed poll
    assert p.component(-10.0, 1200.0)["latched"] is True       # latch survives


def test_latch_persisted_across_restart(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch)
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: _res(True, 5.0))
    gl.GlmLightningPoller(config, _Log()).poll_now(now=time.time())
    reborn = gl.GlmLightningPoller(config, _Log())
    assert reborn.component(-10.0)["latched"] is True
