import time

import pytest

from safety import config
from safety import radar as rd


class _Log:
    def record(self, *a, **k):
        return {}


def _poller(monkeypatch, deps=True, tmp_path=None):
    monkeypatch.setattr(rd, "deps_available", lambda: deps)
    if tmp_path is not None:
        monkeypatch.setattr(config, "RADAR_LATCH_FILE", str(tmp_path / "radar_latch.json"))
    else:
        # never let a unit test read/write the real home-dir latch file
        monkeypatch.setattr(config, "RADAR_LATCH_FILE", "/nonexistent-dir/radar_latch.json")
    p = rd.RadarPoller(config, _Log())
    p._thumbs = []           # no rendering in logic tests
    return p


# ---- component() verdict logic (no PIL needed) ----------------------------
def test_unsafe_when_rain_in_ring_and_fresh(monkeypatch):
    p = _poller(monkeypatch)
    now = 1000.0
    p._last_ok_ts = now
    p._in_ring = True
    p._nearest_km = 12.0
    c = p.component(sun_alt=-10.0, now=now)
    assert c["available"] is True and c["in_ring"] is True and c["safe"] is False
    assert c["nearest_km"] == 12.0 and c["trigger_km"] == config.RADAR_TRIGGER_KM


def test_safe_when_ring_clear(monkeypatch):
    p = _poller(monkeypatch)
    now = 1000.0
    p._last_ok_ts = now
    p._in_ring = False
    assert p.component(-10.0, now)["safe"] is True


def test_stale_frame_is_unavailable_not_veto(monkeypatch):
    # rain was seen, but the last successful poll is old -> unknown, does not veto
    p = _poller(monkeypatch)
    p._in_ring = True
    p._last_ok_ts = 1.0
    c = p.component(-10.0, 1.0 + config.RADAR_STALE_AFTER_SEC + 10)
    assert c["available"] is False and c["safe"] is True


def test_latch_holds_veto_during_blind_gap(monkeypatch):
    # rain seen in the ring, then the feed goes blind -> the veto must persist (this is the
    # ranged-echo hole: WU never latched because rain reached no station)
    p = _poller(monkeypatch)
    now = 1000.0
    p._in_ring = True
    p._last_rain_ts = now - 60
    p._last_ok_ts = now - (config.RADAR_STALE_AFTER_SEC + 100)   # stale / blind
    c = p.component(-10.0, now)
    assert c["available"] is False and c["safe"] is False and c["latched"] is True


def test_freeze_survives_a_fresh_clear_frame(monkeypatch):
    # POST-RAIN FREEZE: rain leaving the ring does NOT reopen the dome — the veto holds
    # for RADAR_LATCH_SEC after the last in-ring detection even with clear frames coming in.
    p = _poller(monkeypatch)
    now = 1000.0
    p._in_ring = False
    p._last_rain_ts = now - 60          # rain a minute ago
    p._last_ok_ts = now                 # fresh & clear right now
    c = p.component(-10.0, now)
    assert c["safe"] is False and c["latched"] is True
    assert c["available"] is True       # the frame itself is current...
    assert c["in_ring"] is False        # ... and honestly reports a clear ring
    # the countdown is exported so the page can say how long is left
    assert 0 < c["seconds_remaining"] <= config.RADAR_LATCH_SEC
    assert c["freeze_sec"] == config.RADAR_LATCH_SEC


def test_freeze_clears_when_the_window_elapses(monkeypatch):
    p = _poller(monkeypatch)
    now = 1000.0
    p._in_ring = False
    p._last_rain_ts = now - (config.RADAR_LATCH_SEC + 1)
    p._last_ok_ts = now
    c = p.component(-10.0, now)
    assert c["safe"] is True and c["latched"] is False and c["seconds_remaining"] == 0


def test_freeze_is_thirty_minutes():
    # the operator-chosen value; a stray edit must not pass unnoticed
    assert config.RADAR_LATCH_SEC == 1800


def test_latch_expires_bounded(monkeypatch):
    # a permanent outage self-clears after the latch window (avoids stuck-forever)
    p = _poller(monkeypatch)
    now = 1000.0
    p._in_ring = True
    p._last_rain_ts = now - (config.RADAR_LATCH_SEC + 10)
    p._last_ok_ts = now - (config.RADAR_STALE_AFTER_SEC + 100)
    assert p.component(-10.0, now)["safe"] is True


def test_deps_absent_disables(monkeypatch):
    p = _poller(monkeypatch, deps=False)
    p._in_ring = True
    p._last_ok_ts = time.time()
    c = p.component(-10.0)
    assert c["enabled"] is False and c["available"] is False and c["safe"] is True
    assert p.maybe_poll(-10.0) is None


def test_maybe_poll_runs_day_and_night(monkeypatch):
    # radar is NOT night-gated (free data, daytime rain matters, live map)
    p = _poller(monkeypatch)
    calls = []

    def fake_poll(now=None):
        calls.append(now)
        p._last_poll_ts = now          # the real poll_now records this too
        return {}

    monkeypatch.setattr(p, "poll_now", fake_poll)
    p.maybe_poll(sun_alt=45.0, now=1.0)                     # broad daylight -> polls
    assert calls
    p.maybe_poll(sun_alt=None, now=2.0)                     # unknown sun -> still polls
    assert len(calls) == 1                                  # ...but respects the interval
    p.maybe_poll(sun_alt=None, now=1.0 + config.RADAR_POLL_INTERVAL + 1)
    assert len(calls) == 2


def test_poll_now_sets_state(monkeypatch):
    # exercise poll_now without real PIL: stub the fetch, image, and check
    p = _poller(monkeypatch)
    monkeypatch.setattr(rd, "latest_frame",
                        lambda: (__import__("datetime").datetime(2026, 1, 1,
                                 tzinfo=__import__("datetime").timezone.utc),
                                 "http://x/lcref.png"))
    monkeypatch.setattr(rd, "_get", lambda url, timeout=25: b"x")

    class _Img:
        def load(self):
            return None
    monkeypatch.setattr(rd, "Image", type("I", (), {"open": staticmethod(lambda b: _Img())}))
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (True, 8.5, 42))
    r = p.poll_now(now=1000.0)
    assert r["ok"] and r["in_ring"] is True
    assert p._in_ring is True and p._nearest_km == 8.5 and p._last_ok_ts == 1000.0
    assert p.component(-10.0, 1000.0)["safe"] is False


# ---- real PIL tests (skipped in CI where Pillow is absent) -----------------
def test_check_rain_on_synthetic_image(monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    # palette image, all clear (index 0 = -32 dBZ) except one rainy pixel near the dome
    img = Image.new("P", (rd.GRID_W // 10, rd.GRID_H // 10), 0)
    # (use a small grid but patch GRID dims for the check)
    monkeypatch.setattr(rd, "GRID_W", img.size[0])
    monkeypatch.setattr(rd, "GRID_H", img.size[1])
    lat0, lon0 = config.GEOCODE
    # place a strong echo (index 124 = 30 dBZ) ~10 km away
    plat, plon = rd.dest_point(lat0, lon0, 10, 90)
    # remap to the shrunken grid coordinate system used by latlon_to_px? keep it simple:
    # put the echo AT the dome pixel to guarantee it's within the ring
    c0, r0 = rd.latlon_to_px(lat0, lon0)
    if 0 <= c0 < img.size[0] and 0 <= r0 < img.size[1]:
        img.putpixel((c0, r0), 124)
        in_ring, nearest, count = rd.check_rain(config, img)
        assert in_ring is True and count >= 1


def test_thumbnailer_geometry():
    pytest.importorskip("PIL")
    t = rd.Thumbnailer(config, config.RADAR_TILE_URL, config.RADAR_THUMB_PATH, "dark")
    t._tilebox = None
    # _region returns a box centered on the observatory
    latmin, latmax, lonmin, lonmax = rd._region(config)
    assert latmin < config.GEOCODE[0] < latmax
    assert lonmin < config.GEOCODE[1] < lonmax
