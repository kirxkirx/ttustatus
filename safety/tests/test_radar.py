import datetime as _dt
import io
import os
import time
import types
import urllib.error

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
    p._ring_streak = p._trigger_after          # confirmed over consecutive frames
    p._nearest_km = 12.0
    c = p.component(sun_alt=-10.0, now=now)
    assert c["available"] is True and c["in_ring"] is True and c["safe"] is False
    assert c["nearest_km"] == 12.0 and c["trigger_km"] == config.RADAR_TRIGGER_KM


def test_single_echo_does_not_trigger(monkeypatch):
    # One frame is an observation, not weather: MRMS artefacts (aircraft, anomalous
    # propagation, clutter) show up for a single frame and must not close the dome.
    p = _poller(monkeypatch)
    now = 1000.0
    p._last_ok_ts = now
    p._in_ring = True
    p._ring_streak = 1
    p._nearest_km = 12.0
    c = p.component(sun_alt=-10.0, now=now)
    assert c["safe"] is True, "a single radar frame triggered a closure"
    assert c["unconfirmed_echo"] is True        # ... and it is reported, not hidden
    assert c["in_ring"] is True                 # the observation stays honest
    assert c["ring_streak"] == 1 and c["trigger_after"] == config.RADAR_TRIGGER_AFTER
    assert c["latched"] is False                # no freeze started by an unconfirmed echo


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


def test_freeze_is_fifteen_minutes():
    # the operator-chosen value; a stray edit must not pass unnoticed
    assert config.RADAR_LATCH_SEC == 900


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
    monkeypatch.setattr(rd, "_get", lambda url, timeout=25, **kw: b"x")

    class _Img:
        def load(self):
            return None
    monkeypatch.setattr(rd, "Image", type("I", (), {"open": staticmethod(lambda b: _Img())}))
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (True, 8.5, 42))
    p._trigger_after = 1                       # confirmation is exercised separately
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


def _stub_fetch(monkeypatch, ring_sequence):
    """Drive poll_now without PIL: each call returns the next (in_ring, nearest) pair."""
    import datetime as _dt
    monkeypatch.setattr(rd, "latest_frame",
                        lambda: (_dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc),
                                 "http://x/lcref.png"))
    monkeypatch.setattr(rd, "_get", lambda url, timeout=25, **kw: b"x")

    class _Img:
        def load(self):
            return None
    monkeypatch.setattr(rd, "Image", type("I", (), {"open": staticmethod(lambda b: _Img())}))
    seq = list(ring_sequence)

    def _check(cfg, img):
        in_ring = seq.pop(0)
        return (in_ring, 12.0 if in_ring else None, 42 if in_ring else 0)
    monkeypatch.setattr(rd, "check_rain", _check)


def test_two_consecutive_echoes_are_needed_to_trigger(monkeypatch):
    p = _poller(monkeypatch)
    _stub_fetch(monkeypatch, [True, True])
    t = 1000.0
    r1 = p.poll_now(now=t)
    assert r1["in_ring"] is True and r1["streak"] == 1 and r1["confirmed"] is False
    c1 = p.component(-10.0, t)
    assert c1["safe"] is True and c1["unconfirmed_echo"] is True
    assert p._last_rain_ts is None, "an unconfirmed echo started the freeze"

    t += config.RADAR_POLL_INTERVAL
    r2 = p.poll_now(now=t)
    assert r2["streak"] == 2 and r2["confirmed"] is True
    c2 = p.component(-10.0, t)
    assert c2["safe"] is False and c2["unconfirmed_echo"] is False
    assert p._last_rain_ts == t                      # freeze starts on confirmation


def test_clear_frame_between_echoes_resets_the_count(monkeypatch):
    # The glitch case: echo, then clear, then echo again — never two in a row, so the
    # dome stays open and no freeze is ever armed.
    p = _poller(monkeypatch)
    _stub_fetch(monkeypatch, [True, False, True])
    t = 1000.0
    for expected_streak in (1, 0, 1):
        r = p.poll_now(now=t)
        assert r["streak"] == expected_streak, r
        assert r["confirmed"] is False
        assert p.component(-10.0, t)["safe"] is True
        t += config.RADAR_POLL_INTERVAL
    assert p._last_rain_ts is None


def test_confirmation_default_is_two_frames():
    # the operator-chosen value; a stray edit must not pass unnoticed
    assert config.RADAR_TRIGGER_AFTER == 2


def test_trigger_radius_default_is_thirty_km():
    # the operator-chosen ring; the map plot and both pages label themselves from it
    assert config.RADAR_TRIGGER_KM == 30.0


def test_thumbnail_writes_go_to_shm_via_symlink(tmp_path, monkeypatch):
    # The thumbnail pair is rewritten every poll (~150-250 MB/day) — it must land in
    # RAM, with the web-root path as a one-time symlink the server can follow.
    monkeypatch.setattr(config, "RADAR_THUMB_VIA_SHM", True, raising=False)
    t = rd.Thumbnailer.__new__(rd.Thumbnailer)
    t.cfg = config
    t.thumb_path = str(tmp_path / "ttu_radar_test_shm.png")
    out = t._output_target()
    assert out == "/dev/shm/ttu_radar_test_shm.png"
    assert os.path.islink(t.thumb_path) and os.readlink(t.thumb_path) == out
    # idempotent: second call keeps the same link, and a stale regular file is replaced
    assert t._output_target() == out
    os.remove(t.thumb_path)
    (tmp_path / "ttu_radar_test_shm.png").write_bytes(b"old regular file")
    assert t._output_target() == out and os.path.islink(t.thumb_path)


def test_thumbnail_shm_disabled_writes_directly(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RADAR_THUMB_VIA_SHM", False, raising=False)
    t = rd.Thumbnailer.__new__(rd.Thumbnailer)
    t.cfg = config
    t.thumb_path = str(tmp_path / "direct.png")
    assert t._output_target() == t.thumb_path and not os.path.islink(t.thumb_path)


# ==== Hazard overlays on the radar map (display only) ====================================
# Fixtures: REAL payloads fetched once, trimmed (rounded; far/straight runs decimated).
# NWS Tornado Warning KLUB TO.W.0032, 2025-06-05 23:54Z, the polygon over TTU (IEM sbw)
TOW_32 = {"type": "MultiPolygon", "coordinates": [[[
    [-102.27, 33.61], [-102.24, 33.83], [-101.67, 33.83], [-101.76, 33.42], [-102.27, 33.61]]]]}
# api.weather.gov /zones/forecast/TXZ035 (Lubbock), the outline of a zone-based watch
TXZ035 = {"type": "Polygon", "coordinates": [[
    [-101.557, 33.395], [-101.868, 33.392], [-102.076, 33.389], [-102.078, 33.501],
    [-102.081, 33.636], [-102.086, 33.825], [-101.874, 33.827], [-101.564, 33.831],
    [-101.56, 33.61], [-101.557, 33.395]]]}
# SPC Day-1 2026-09-24 1630Z: MRGL, and TSTM with MRGL cut out as a HOLE (map's NE corner)
SPC_MRGL_RING = [
    [-100.92, 38.82], [-99.44, 38.99], [-98.47, 36.0], [-98.86, 34.81], [-99.38, 34.52],
    [-100.59, 34.3], [-101.49, 34.32], [-101.83, 34.5], [-101.9, 34.85], [-101.2, 35.97],
    [-100.92, 38.82]]
SPC_MRGL = {"type": "Polygon", "coordinates": [SPC_MRGL_RING]}
SPC_TSTM = {"type": "Polygon", "coordinates": [[
    [-117.35, 33.57], [-105.46, 41.27], [-98.5, 42.42], [-95.18, 39.5], [-98.0, 33.1],
    [-100.0, 33.15], [-100.6, 33.07], [-101.64, 32.44], [-102.38, 31.34], [-102.96, 28.86],
    [-108.0, 31.62], [-117.12, 32.4], [-117.35, 33.57]], list(reversed(SPC_MRGL_RING))]}
# SPC mesoscale discussion 2323 (2026-09-21 19:38Z, South Plains), IEM spc_mcd.geojson
MD_2323 = {"type": "Polygon", "coordinates": [[
    [-101.83, 32.64], [-102.84, 33.46], [-102.88, 33.9], [-102.3, 34.23], [-101.63, 34.06],
    [-100.79, 33.34], [-100.05, 32.8], [-99.96, 32.6], [-100.35, 32.35], [-101.02, 32.29],
    [-101.83, 32.64]]]}
# NOAA HMS smoke 2026-09-22 13-15Z, Light: covers TTU; its NW edge crosses the map
HMS_LIGHT = {"type": "Polygon", "coordinates": [[
    [-100.07, 34.76], [-84.99, 36.38], [-75.64, 29.57], [-89.23, 16.3], [-108.56, 10.58],
    [-119.88, 16.7], [-103.73, 26.92], [-102.95, 31.17], [-103.09, 32.12], [-102.94, 33.09],
    [-102.32, 33.74], [-101.73, 34.03], [-100.41, 34.6], [-100.07, 34.76]]]}
# NIFC WFIGS perimeter "Mimms" (Quay Co. NM, 7093 ac), 12320 -> 11 vertices
MIMMS = {"type": "Polygon", "coordinates": [[
    [-103.9316, 34.8485], [-103.9159, 34.8611], [-103.9008, 34.8729], [-103.8909, 34.8616],
    [-103.8551, 34.8762], [-103.813, 34.8752], [-103.863, 34.8535], [-103.887, 34.8348],
    [-103.9051, 34.8392], [-103.9376, 34.8197], [-103.9316, 34.8485]]]}
# api.weather.gov zone TXZ213 (Inland Harris), a GeometryCollection: a Polygon with an
# enclave hole + a MultiPolygon whose first member IS the enclave
HARRIS_GC = {"type": "GeometryCollection", "geometries": [
    {"type": "Polygon", "coordinates": [
        [[-95.147, 29.737], [-95.204, 29.736], [-95.257, 29.727], [-95.276, 29.722],
         [-95.29, 29.752], [-95.234, 29.724], [-95.173, 29.747], [-95.147, 29.737]],
        [[-95.274, 29.725], [-95.276, 29.726], [-95.276, 29.721], [-95.271, 29.724],
         [-95.274, 29.725]]]},
    {"type": "MultiPolygon", "coordinates": [
        [[[-95.274, 29.725], [-95.271, 29.724], [-95.276, 29.721], [-95.276, 29.726],
          [-95.274, 29.725]]],
        [[[-95.546, 30.169], [-95.073, 30.104], [-94.994, 29.977], [-95.16, 29.743],
          [-95.141, 29.506], [-95.494, 29.613], [-95.922, 30.138], [-95.546, 30.169]]]]}]}
# Real NWS Local Storm Reports near TTU (IEM, 2021-2026)
LSRS = [("T", "TORNADO", 33.65, -102.1), ("C", "FUNNEL CLOUD", 33.59, -102.02),
        ("2", "DUST STORM", 33.76, -102.09), ("R", "RAIN", 33.73, -102.19),
        ("s", "SLEET", 33.52, -101.87), ("L", "LIGHTNING", 33.59, -101.93)]
YELLOW_LAKE = (33.7954, -101.8267)  # WFIGS incident, 2026-03-01, 159 ac


def _alert(key, event, color, rank, geometry, vetoes=False):
    return {"kind": "alert", "key": key, "event": event, "color": color, "rank": rank,
            "vetoes": vetoes, "geometry": geometry}


EDGE = (33.79, -101.6788)  # on TO.W.0032's steep east edge, clear of the ring label
TS = _dt.datetime(2026, 9, 24, 17, 2, tzinfo=_dt.timezone.utc)
TOR = _alert("KLUB.TO.W.0032", "Tornado Warning", "#FF0000", 0, TOW_32, vetoes=True)
TOA = _alert("KWNS.TO.A.0367", "Tornado Watch", "#FFFF00", 11, TXZ035)
FAR = _alert("TXZ213.FA.A", "Flood Watch", "#E53935", 11, HARRIS_GC)  # Houston: off the map


# ---- normalisation / order / signature (pure Python: runs in CI without Pillow) ----
def test_overlay_signature_tracks_what_would_be_drawn():
    sig = rd.overlay_signature
    assert sig(config, None) == sig(config, []) == ""
    assert sig(config, [FAR]) == ""  # nothing reaches the map
    base = sig(config, [TOR, TOA])
    assert base and sig(config, [TOA, TOR]) == base  # list order is irrelevant
    assert sig(config, [TOA, FAR, TOR]) == base  # far-away changes are too
    assert sig(config, [TOA, dict(TOR, vetoes=False)]) != base  # the veto flag is drawn
    assert sig(config, [TOA, dict(TOR, color="#FF00FF")]) != base
    assert sig(config, [TOA, dict(TOR, geometry=TXZ035)]) != base  # an updated polygon
    assert sig(config, [TOA]) != base  # cancelled / expired


def test_draw_order_info_first_then_watches_then_warnings_then_veto():
    svr = _alert("KLUB.SV.W.0218", "Severe Thunderstorm Warning", "#FFA500", 1, TOW_32)
    veto_hi = dict(TOR, rank=30)  # a vetoing alert goes last whatever its rank
    ovs = [veto_hi, svr, TOA,
           {"kind": "lsr", "key": "l", "typetext": "HAIL", "lat": 33.8, "lon": -102.07},
           {"kind": "smoke", "key": "s", "style": {"density": "Heavy"}, "geometry": HMS_LIGHT},
           {"kind": "smoke", "key": "s2", "style": {"density": "Light"}, "geometry": HMS_LIGHT},
           {"kind": "spc_md", "key": "md", "geometry": MD_2323}]
    got = [s["key"] for s in rd._prepare_overlays(config, ovs)]
    assert got == ["s2", "s", "md", "l", TOA["key"], svr["key"], TOR["key"]]
    # without the veto, rank decides: the lower rank (warning) is painted on top
    got = [s["key"] for s in rd._prepare_overlays(config, [dict(TOR, vetoes=False), TOA])]
    assert got == [TOA["key"], TOR["key"]]


def test_malformed_overlays_are_skipped_not_fatal():
    good = dict(TOA)
    nan = [[float("nan"), 33.7], [-101.9, 33.8], [-101.8, 33.7]]
    bad = [None, "alert", {"kind": "alert"}, {"kind": "nope", "geometry": TXZ035},
           {"kind": "alert", "geometry": {"type": "Polygon", "coordinates": [nan]}},
           {"kind": "lsr", "lat": "north", "lon": -101.9},
           {"kind": "alert", "geometry": {"type": "Polygon", "coordinates": [[[-101.9]]]}}]
    specs = rd._prepare_overlays(config, bad + [good])
    assert [s["key"] for s in specs] == [good["key"]]


def test_spc_outline_categories_and_general_thunder_is_not_drawn():
    def cat(**ov):
        s = rd._prepare_overlays(config, [dict(kind="spc_outlook", geometry=MD_2323, **ov)])
        return s[0]["category"] if s else None
    assert cat(label="MRGL") == "MRGL"
    assert cat(style={"LABEL": "SLGT", "LABEL2": "Slight Risk"}) == "SLGT"
    assert cat(style={"DN": 5}) == "ENH"
    assert cat(label="SPC Day 1: Moderate Risk") == "MDT"
    assert cat(category="high") == "HIGH"
    assert cat(label="TSTM") is None  # outlines are MRGL and above
    assert cat(style={"LABEL2": "General Thunderstorms Risk"}) is None
    assert cat(label="???") == "?"  # unreadable: drawn neutral, not lost


def test_hazards_are_never_drawn_green():
    # the NWS chart paints these chartreuse / lime: the type's fallback is drawn instead
    ev = _alert("x", "Evacuation Immediate", "#7FFF00", 5, TXZ035)
    fl = _alert("y", "Flood Warning", "#00FF00", 5, TXZ035)
    colors = {s["event"]: s["color"] for s in rd._prepare_overlays(config, [ev, fl])}
    # (an Evacuation Immediate is warning-level in the EAS scheme: the warning fallback)
    assert colors == {"Evacuation Immediate": list(rd.KIND_RGB["warning"]),
                      "Flood Warning": list(rd.KIND_RGB["warning"])}
    assert rd._never_green(rd.SPC_RGB["MRGL"], (1, 2, 3)) == (1, 2, 3)  # SPC's MRGL too
    assert rd._never_green(rd.SPC_RGB["SLGT"], (1, 2, 3)) == rd.SPC_RGB["SLGT"]
    for ok in ("#00FFFF", "#40E0D0", "#5F9EA0", "#D2B48C", "#FFFF00"):  # cyan/teal/tan/yellow
        assert not rd._is_green(rd._hex_rgb(ok)), ok


def test_lsr_symbols():
    sym = {code: rd._lsr_symbol({"type": code}, {}) for code, *_ in LSRS}
    assert sym == {"T": "tornado", "C": "tornado", "2": "dust", "R": "flood",
                   "s": "winter", "L": "other"}
    assert rd._lsr_symbol({}, {"typetext": "TSTM WND DMG"}) == "wind"
    assert rd._lsr_symbol({"label": "HAIL 1.75 in, 4 N Shallowater"}, {}) == "hail"
    assert rd._lsr_symbol({"typetext": "FREEZING RAIN"}, {}) == "winter"  # not "flood"
    assert rd._lsr_symbol({"typetext": "EXTR WIND CHILL"}, {}) == "winter"  # not "wind"
    # hazard_feeds' kind wins; a style "symbol" is a shape name, not a report type
    assert rd._lsr_symbol({"lsr_kind": "rain", "typetext": "HAIL"}, {}) == "flood"
    assert rd._lsr_symbol({"typetext": "HAIL"}, {"symbol": "triangle"}) == "hail"


# ---- the poller: redraw on overlay change, never refetch (fake maps, no PIL needed) ----
class _FakeThumb:
    frame, ok = None, True

    def __init__(self):
        self.renders, self.rerenders = [], []

    def render(self, img, frame_txt, overlays=None):
        self.frame = (img, frame_txt)
        self.renders.append(list(overlays or []))
        return True

    def rerender(self, overlays):
        if self.frame is None:
            return False
        self.rerenders.append(list(overlays or []))
        return self.ok

    def forget_frame(self):
        self.frame = None


def _hazard_poller(monkeypatch, sources, frames):
    monkeypatch.setattr(rd, "deps_available", lambda: True)
    monkeypatch.setattr(config, "RADAR_LATCH_FILE", "/nonexistent-dir/radar_latch.json")
    _stub_fetch(monkeypatch, [False] * 20)
    monkeypatch.setattr(rd, "latest_frame", lambda: frames.get("next", (TS, "http://x/l.png")))

    def _get(url, timeout=25, **kw):
        frames["fetched"] = frames.get("fetched", 0) + 1
        return b"x"
    monkeypatch.setattr(rd, "_get", _get)
    p = rd.RadarPoller(config, _Log(), overlay_sources=sources)
    p._thumbs = [_FakeThumb(), _FakeThumb()]  # the dark and the light map
    return p


def test_new_warning_redraws_the_kept_frame_without_refetching(monkeypatch):
    current, frames = [TOA], {}
    p = _hazard_poller(monkeypatch, [lambda: list(current)], frames)
    t0 = 1000.0
    p.maybe_poll(None, now=t0)  # first poll: fetch + draw
    assert frames["fetched"] == 1
    assert all(t.renders == [[TOA]] and not t.rerenders for t in p._thumbs)
    p.maybe_poll(None, now=t0 + 20)  # nothing changed
    assert all(not t.rerenders for t in p._thumbs)
    current.append(TOR)  # a tornado warning arrives
    p.maybe_poll(None, now=t0 + 40)
    assert frames["fetched"] == 1, "a hazard change must not refetch MRMS"
    assert all(t.rerenders == [[TOA, TOR]] for t in p._thumbs)  # both maps, at once
    current.reverse()
    current.append(FAR)  # same picture: no redraw
    p.maybe_poll(None, now=t0 + 60)
    assert all(len(t.rerenders) == 1 for t in p._thumbs)
    current[:] = [TOA, dict(TOR, vetoes=False)]  # the veto released
    p.maybe_poll(None, now=t0 + 80)
    assert all(len(t.rerenders) == 2 for t in p._thumbs)
    p.maybe_poll(None, now=t0 + config.RADAR_POLL_INTERVAL)  # the next frame is due
    assert frames["fetched"] == 2
    assert all(len(t.renders) == 2 and len(t.rerenders) == 2 for t in p._thumbs)


def test_failed_poll_still_redraws_changed_hazards(monkeypatch):
    current, frames = [], {}
    p = _hazard_poller(monkeypatch, [lambda: list(current)], frames)
    p.maybe_poll(None, now=1000.0)
    frames["next"] = (None, None)  # IEM has no new frame
    current.append(TOR)
    p.maybe_poll(None, now=1000.0 + config.RADAR_POLL_INTERVAL)  # due, fails, still redraws
    assert frames["fetched"] == 1
    assert all(t.rerenders == [[TOR]] for t in p._thumbs)


def test_redraw_waits_for_a_frame_and_retries_a_failed_write(monkeypatch):
    current, frames = [TOR], {}
    p = _hazard_poller(monkeypatch, [lambda: list(current)], frames)
    assert p.refresh_overlays(now=1.0) is False  # no frame yet: the first poll draws it
    p.maybe_poll(None, now=1000.0)
    current.append(TOA)
    p._thumbs[1].ok = False  # the light map's write fails ...
    assert p.refresh_overlays(now=1010.0) is False
    p._thumbs[1].ok = True  # ... so the next pass tries again
    assert p.refresh_overlays(now=1020.0) is True
    assert p.refresh_overlays(now=1030.0) is False  # and then it is up to date


def test_clock_step_drops_the_kept_frame(monkeypatch):
    current, frames = [], {}
    p = _hazard_poller(monkeypatch, [lambda: list(current)], frames)
    p.maybe_poll(None, now=1000.0)
    p.clock_stepped(1000.0, 1000.0 + 86400 * 150)  # the frame came from the wrong day
    current.append(TOR)
    assert p.refresh_overlays(now=1010.0) is False  # never drawn onto the wrong-day frame
    p.maybe_poll(None, now=1020.0)  # re-poll forced by the step: drawn
    assert frames["fetched"] == 2 and all(t.renders[-1] == [TOR] for t in p._thumbs)


def test_failing_overlay_source_keeps_its_last_list_then_drops_it(monkeypatch):
    state = {"fail": False}

    def flaky():
        if state["fail"]:
            raise RuntimeError("feed parser crashed")
        return [TOR]
    p = _hazard_poller(monkeypatch, [flaky, lambda: [TOA]], {})
    assert p._collect_overlays(1000.0) == [TOR, TOA]
    state["fail"] = True
    stale = getattr(config, "HAZARD_STALE_AFTER_SEC", 600)
    assert p._collect_overlays(1000.0 + stale - 1) == [TOR, TOA]  # no blinking warning
    assert p._collect_overlays(1000.0 + stale + 1) == [TOA]  # ... nor a stale one
    p._overlay_sources.append(lambda: "not a list")  # garbage: left off
    assert p._collect_overlays(1000.0 + stale + 2) == [TOA]


def test_overlays_never_change_the_radar_verdict(monkeypatch):
    # display only: a vetoing warning ON THE MAP is no radar veto
    p = _hazard_poller(monkeypatch, [lambda: [TOR, TOA]], {})
    q = _hazard_poller(monkeypatch, None, {})
    p.maybe_poll(None, now=1000.0)
    q.maybe_poll(None, now=1000.0)
    assert p.component(-10.0, 1000.0) == q.component(-10.0, 1000.0)
    assert p.component(-10.0, 1000.0)["safe"] is True


# ---- real rendering (Pillow; skipped where it is absent, e.g. CI) -----------------------
BG = {"dark": (16, 20, 30), "light": (242, 242, 240)}
_FRAME = {}


def _cfg(tmp_path, **over):
    c = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    c.RADAR_CACHE_DIR = str(tmp_path / "cache")
    c.RADAR_THUMB_VIA_SHM = False
    c.RADAR_THUMB_PX = 220  # ~0.84 km/px
    c.RADAR_THUMB_PATH = str(tmp_path / "ttu_radar.png")
    c.RADAR_THUMB_PATH_DAY = str(tmp_path / "ttu_radar_day.png")
    c.RADAR_LATCH_FILE = str(tmp_path / "radar_latch.json")
    c.HAZARD_ALERT_FILL_ALPHA = 60
    for k, v in over.items():
        setattr(c, k, v)
    return c


@pytest.fixture
def offline(monkeypatch):
    pytest.importorskip("PIL")

    def _blocked(url, timeout=25, **kw):
        raise urllib.error.URLError("network blocked in tests")
    monkeypatch.setattr(rd, "_get", _blocked)


def _frame():
    """Full-size MRMS frame: one 30 dBZ cell NE, outside the ring and the probed pixels."""
    if "img" not in _FRAME:
        from PIL import Image
        img = Image.new("P", (rd.GRID_W, rd.GRID_H), 0)
        c0, r0 = rd.latlon_to_px(*config.GEOCODE)
        img.paste(124, (c0 + 20, r0 - 45, c0 + 50, r0 - 30))
        _FRAME["img"] = img
    return _FRAME["img"]


def _thumb(cfg, theme, name="m.png"):
    from PIL import Image
    t = rd.Thumbnailer(cfg, "http://tiles.invalid/{z}/{x}/{y}.png",
                       os.path.join(os.path.dirname(cfg.RADAR_THUMB_PATH), name), theme)
    t._build_basemap()  # tiles fail offline -> solid canvas
    t._basemap = Image.new("RGB", t._basemap.size, BG[theme])
    return t


def _render(t, overlays=None):
    from PIL import Image
    assert t.render(_frame(), "MRMS 2025-06-06 00:30Z", overlays)
    with Image.open(t.thumb_path) as im:
        return im.convert("RGB")


def _xy(t, lat, lon):
    x, y = t._mv(lat, lon)
    return int(x), int(y)


def _count(img, rgb):
    return sum(1 for p in img.getdata() if p == tuple(rgb))


def _near(img, xy, rgb, r=1):
    x0, y0 = xy
    return any(img.getpixel((x, y)) == tuple(rgb)
               for x in range(x0 - r, x0 + r + 1) for y in range(y0 - r, y0 + r + 1))


def _legacy_render(t, img, frame_txt):
    """The pre-hazards Thumbnailer.render (minus the file write)."""
    from PIL import Image, ImageDraw
    cfg = t.cfg
    ox, oy = t._ox, t._oy
    base = t._basemap.copy().convert("RGBA")
    ov = Image.new("RGBA", (ox, oy), (0, 0, 0, 0))
    op = ov.load()
    rp = img.load()
    for j in range(oy):
        for i in range(ox):
            c, r = t._remap[j][i]
            if 0 <= c < rd.GRID_W and 0 <= r < rd.GRID_H:
                v = rp[c, r]
                col = rd._dbz_color(rd._dbz(v if isinstance(v, int) else v[0]))
                if col:
                    op[i, j] = col + (205,)
    im = Image.alpha_composite(base, ov).convert("RGB")
    d = ImageDraw.Draw(im)
    lat0, lon0 = cfg.GEOCODE
    ink, stroke = t.colors["ink"], t.colors["stroke"]
    ring = [t._mv(*rd.dest_point(lat0, lon0, cfg.RADAR_TRIGGER_KM, b)) for b in range(0, 361, 6)]
    d.line(ring, fill=t.colors["ring"], width=2)
    cx, cy = t._mv(lat0, lon0)
    d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=ink, width=2)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.line([cx + dx * 6, cy + dy * 6, cx + dx * 11, cy + dy * 11], fill=ink, width=2)
    d.text((cx + 8, cy + 6), "%g km" % cfg.RADAR_TRIGGER_KM, fill=t.colors["label"],
           font=rd._font(13), stroke_width=2, stroke_fill=stroke)
    f = rd._font(12)

    def px_for(km):
        x2, _ = t._mv(*rd.dest_point(lat0, lon0, km, 90))
        x1, _ = t._mv(lat0, lon0)
        return abs(x2 - x1)
    bx, by = 14, oy - 34
    for label, km in (("10 km", 10.0), ("10 mi", 16.0934)):
        L = px_for(km)
        d.line([bx, by, bx + L, by], fill=ink, width=3)
        d.line([bx, by - 3, bx, by + 3], fill=ink, width=2)
        d.line([bx + L, by - 3, bx + L, by + 3], fill=ink, width=2)
        d.text((bx + L + 5, by - 7), label, fill=ink, font=f, stroke_width=2, stroke_fill=stroke)
        by += 15
    d.text((8, 6), frame_txt, fill=t.colors["text"], font=rd._font(12), stroke_width=2,
           stroke_fill=stroke)
    return im


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_no_overlays_is_pixel_identical_to_the_pre_hazards_map(tmp_path, offline, theme):
    t = _thumb(_cfg(tmp_path), theme)
    _render(t)
    legacy = _legacy_render(t, _frame(), "MRMS 2025-06-06 00:30Z").tobytes()
    off_map = [FAR,
               {"kind": "lsr", "key": "ca", "typetext": "HAIL", "lat": 36.6, "lon": -119.33},
               {"kind": "fire_perimeter", "key": "Mimms", "geometry": MIMMS},  # in NM
               {"kind": "spc_outlook", "key": "tstm", "label": "TSTM", "geometry": SPC_TSTM}]
    for ovs in (None, [], off_map):
        assert _render(t, ovs).tobytes() == legacy, ovs


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_alert_area_is_filled_and_cased_on_both_themes(tmp_path, offline, theme):
    t = _thumb(_cfg(tmp_path), theme)
    base = _render(t)
    img = _render(t, [dict(TOR, vetoes=False)])
    red, stroke = (255, 0, 0), rd._THEME[theme]["stroke"]
    # on the east edge: pure alert colour, the theme's casing on both sides
    x, y = _xy(t, *EDGE)
    assert _near(img, (x, y), red)
    row = [img.getpixel((i, y)) for i in range(x - 5, x + 6)]
    first, last = row.index(red), len(row) - 1 - row[::-1].index(red)
    assert stroke in row[:first] and stroke in row[last + 1:]
    # inside: the colour at HAZARD_ALERT_FILL_ALPHA over the map
    inside = _xy(t, 33.62, -102.1)
    a = 60 / 255.0
    want = tuple(round(b * (1 - a) + c * a) for b, c in zip(base.getpixel(inside), red))
    assert all(abs(g - w) <= 1 for g, w in zip(img.getpixel(inside), want))
    # outside the polygon the map is untouched
    for far in ((34.6, -102.8), (33.0, -101.2)):
        assert img.getpixel(_xy(t, *far)) == base.getpixel(_xy(t, *far))


def test_warning_is_painted_over_the_watch_whatever_the_input_order(tmp_path, offline):
    t = _thumb(_cfg(tmp_path), "dark")
    warn = dict(TOR, vetoes=False)
    a = _render(t, [warn, TOA])
    assert _render(t, [TOA, warn]).tobytes() == a.tobytes()
    # its east edge runs INSIDE the watch zone and is pure red: nothing painted over it
    assert _near(a, _xy(t, *EDGE), (255, 0, 0))
    # where the outlines coincide (the county's north line) the warning is on top
    assert _near(a, _xy(t, 33.83, -101.9), (255, 0, 0))
    assert not _near(a, _xy(t, 33.83, -101.9), (255, 255, 0), r=0)


def test_vetoing_alert_gets_the_thicker_outline(tmp_path, offline):
    t = _thumb(_cfg(tmp_path), "dark")

    plain = _count(_render(t, [dict(TOR, vetoes=False)]), (255, 0, 0))
    veto = _count(_render(t, [TOR]), (255, 0, 0))
    assert veto >= 1.6 * plain > 0
    # measured across the (steep) east edge: 2 px of colour vs 4 px
    for alert, width in ((dict(TOR, vetoes=False), rd.ALERT_OUTLINE_PX),
                         (TOR, rd.ALERT_VETO_OUTLINE_PX)):
        img = _render(t, [alert])
        x, y = _xy(t, *EDGE)
        run = sum(1 for i in range(x - 6, x + 7) if img.getpixel((i, y)) == (255, 0, 0))
        assert width <= run <= width + 1, (alert["vetoes"], run)


def test_polygon_holes_stay_unfilled_and_members_still_fill_them(tmp_path, offline):
    t = _thumb(_cfg(tmp_path), "dark")
    base = _render(t)
    in_hole, in_poly = _xy(t, 34.6, -101.3), _xy(t, 33.3, -102.3)
    img = _render(t, [_alert("tstm", "Hole Advisory", "#7B68EE", 20, SPC_TSTM)])
    assert img.getpixel(in_hole) == base.getpixel(in_hole)  # the hole: untouched
    assert img.getpixel(in_poly) != base.getpixel(in_poly)  # the polygon: filled
    # a hole never punches through another member of the same MultiPolygon
    merged = {"type": "MultiPolygon",
              "coordinates": [SPC_TSTM["coordinates"], SPC_MRGL["coordinates"]]}
    img = _render(t, [_alert("m", "Hole Advisory", "#7B68EE", 20, merged)])
    assert img.getpixel(in_hole) != base.getpixel(in_hole)


def test_zone_collection_and_fire_perimeter_at_their_real_places(tmp_path, offline):
    t = _thumb(_cfg(tmp_path, GEOCODE=(29.8, -95.45)), "dark")  # map over Houston
    base = _render(t)
    img = _render(t, [dict(FAR, color="#E53935")])
    assert img.getpixel(_xy(t, 29.85, -95.5)) != base.getpixel(_xy(t, 29.85, -95.5))
    assert _count(img, (0xE5, 0x39, 0x35)) > 50  # its outline
    t = _thumb(_cfg(tmp_path, GEOCODE=(34.85, -103.88)), "dark")  # over Quay Co. NM
    img = _render(t, [{"kind": "fire_perimeter", "key": "Mimms", "geometry": MIMMS}])
    assert _count(img, rd.FIRE_RGB) > 20


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_information_layers_are_drawn_on_both_themes(tmp_path, offline, theme):
    cfg = _cfg(tmp_path)
    t = _thumb(cfg, theme)
    base = _render(t)
    ink = rd._THEME[theme]["ink"]
    # HMS smoke: a grey veil inside (south-east), nothing outside (north-west)
    img = _render(t, [{"kind": "smoke", "key": "hms", "label": "Smoke (Light)",
                       "style": {"density": "Light"}, "geometry": HMS_LIGHT}])
    se, nw = _xy(t, 33.0, -101.2), _xy(t, 34.6, -102.8)
    a = rd.SMOKE_ALPHA["light"] / 255.0
    want = tuple(round(b * (1 - a) + s * a)
                 for b, s in zip(base.getpixel(se), rd._THEME[theme]["smoke"]))
    assert all(abs(g - w) <= 1 for g, w in zip(img.getpixel(se), want))
    assert img.getpixel(nw) == base.getpixel(nw)
    # SPC MRGL: dashed (colour and gaps along the edge), and never SPC's own dark green
    img = _render(t, [{"kind": "spc_outlook", "key": "mrgl", "label": "MRGL",
                       "geometry": SPC_MRGL}])
    mrgl = rd._THEME[theme]["mrgl"]
    edge = [_xy(t, 34.32 + (34.5 - 34.32) * f, -101.49 + (-101.83 + 101.49) * f)
            for f in (i / 40.0 for i in range(41))]
    hits = [_near(img, p, mrgl) for p in edge]
    assert any(hits) and not all(hits)
    assert _count(img, rd.SPC_RGB["MRGL"]) == 0
    # SPC mesoscale discussion: dashed purple
    img = _render(t, [{"kind": "spc_md", "key": "md2323", "geometry": MD_2323}])
    assert _count(img, rd.MD_RGB) > 20
    # wildfire incident: an orange-red triangle at the point of origin
    img = _render(t, [{"kind": "fire", "key": "Yellow Lake", "lat": YELLOW_LAKE[0],
                       "lon": YELLOW_LAKE[1]}])
    assert img.getpixel(_xy(t, *YELLOW_LAKE)) == rd.FIRE_RGB
    # storm reports: monochrome ink symbols
    lsr = [{"kind": "lsr", "key": code, "type": code, "typetext": text, "lat": la, "lon": lo}
           for code, text, la, lo in LSRS]
    img = _render(t, lsr)
    for code, text, la, lo in LSRS:
        p = _xy(t, la, lo)
        if code == "L":  # "other": a small hollow ring
            assert _near(img, (p[0] + 3, p[1]), ink), text
        else:
            assert img.getpixel(p) == ink and base.getpixel(p) != ink, text


def test_alerts_over_info_layers_and_the_crosshair_over_everything(tmp_path, offline):
    t = _thumb(_cfg(tmp_path), "dark")
    fire = {"kind": "fire", "key": "Yellow Lake", "lat": YELLOW_LAKE[0], "lon": YELLOW_LAKE[1]}
    img = _render(t, [TOR, fire])
    p = _xy(t, *YELLOW_LAKE)  # inside the warning: its fill tints the triangle
    assert img.getpixel(p) != rd.FIRE_RGB and img.getpixel(p)[0] > 200
    # the observatory marker is drawn after every overlay
    base = _render(t)
    cx, cy = _xy(t, *config.GEOCODE)
    ring_px = [(cx + dx, cy + dy) for dx in range(-7, 8) for dy in range(-7, 8)
               if base.getpixel((cx + dx, cy + dy)) == rd._THEME["dark"]["ink"]]
    assert ring_px and all(img.getpixel(q) == rd._THEME["dark"]["ink"] for q in ring_px)


def test_poller_end_to_end_redraws_both_pngs_without_refetch(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    buf = io.BytesIO()
    _frame().save(buf, format="PNG")
    png, fetched = buf.getvalue(), []

    def _get(url, timeout=25, **kw):
        if url.endswith("lcref.png"):
            fetched.append(url)
            return png
        raise urllib.error.URLError("network blocked in tests")  # basemap tiles

    def reds(path):
        with Image.open(path) as im:
            return _count(im.convert("RGB"), (255, 0, 0))
    monkeypatch.setattr(rd, "_get", _get)
    monkeypatch.setattr(rd, "latest_frame", lambda: (TS, "http://x/lcref.png"))
    cfg, current = _cfg(tmp_path), []
    p = rd.RadarPoller(cfg, _Log(), overlay_sources=[lambda: list(current)])
    assert [t.colors for t in p._thumbs] == [rd._THEME["dark"], rd._THEME["light"]]
    p.maybe_poll(None, now=1000.0)
    assert reds(cfg.RADAR_THUMB_PATH) == 0 and reds(cfg.RADAR_THUMB_PATH_DAY) == 0
    current.append(TOR)
    p.maybe_poll(None, now=1060.0)
    assert len(fetched) == 1
    assert reds(cfg.RADAR_THUMB_PATH) > 50 and reds(cfg.RADAR_THUMB_PATH_DAY) > 50
    assert p.component(-10.0, 1060.0)["thumb_available"] is True
