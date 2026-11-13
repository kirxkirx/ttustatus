"""Fixes from the review of the radar-sources / wrong-clock-guard change (255911c), and the
gaps its mutation run found in the tests. No network: the archives are faked as in
test_radar_sources.py.

  * one site for a whole radar poll (a GPS position adopted mid-poll);
  * no flicker of the wrong-clock verdict at the tolerance (radar and GLM);
  * quick retries limited, and a refused file not downloaded again;
  * no stale GLM distance after a failed poll; GLM failures and clock refusals logged;
  * Pillow 14 (no Image.getdata) and bounded decompression of a frame.
"""
import email.utils
import gzip
import json
import logging
import math
import os
import subprocess
import sys
import time
import urllib.error

import pytest

from safety import config, servertime
from safety import glm_lightning as gl
from safety import radar as rd
from safety.tests.test_radar import _poller, _stub_sources
from safety.tests.test_radar_sources import NOW, _cfg, _Events, _grib2, _listing, _Net, _png16, _t
from safety.tests.test_radar_sources import small_grid  # noqa: F401 (a fixture, used by name)

G = dict(ni=400, nj=400, la1=35.495, lo1=360 - 103.495)     # small_grid's lcref-style grid
_REAL_FETCH = rd.fetch_frame          # before any test replaces it

# ---- one site for the whole poll ------------------------------------------------------------


def test_a_site_change_during_the_poll_sets_the_frame_aside(monkeypatch):
    # the evaluator thread adopts the GPS position while the poll downloads its frame: the
    # AWS frame was decoded only around the OLD site, so it must not be checked for the new
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch)
    stub = rd.fetch_frame
    new = (round(config.GEOCODE[0] + 1.5, 3), config.GEOCODE[1])
    seen = []
    monkeypatch.setattr(rd, "check_rain",
                        lambda cfg, img: seen.append(tuple(cfg.GEOCODE)) or (False, None, 0))

    def fetch(cfg, net, source, url, ts, box):
        monkeypatch.setattr(config, "GEOCODE", new)
        return stub(cfg, net, source, url, ts, box)
    monkeypatch.setattr(rd, "fetch_frame", fetch)
    r = p.poll_now(now=NOW)
    assert r["ok"] is False and r["error"].startswith("the site changed during the poll")
    assert seen == [] and p._frame_ts is None
    assert p._poll_every == config.RADAR_RETRY_SEC            # fetched again soon
    monkeypatch.setattr(rd, "fetch_frame", stub)
    assert p.poll_now(now=NOW + 60)["ok"] is True
    assert seen == [new]                                      # checked for the new site


def test_the_poll_s_site_view_reads_every_other_setting_through(monkeypatch):
    v = rd._AtSite(config, (40.0, -100.0))
    assert v.GEOCODE == (40.0, -100.0) and v.RADAR_TRIGGER_KM == config.RADAR_TRIGGER_KM
    monkeypatch.setattr(config, "GEOCODE", (40.0, -100.0))
    assert rd.frame_box(v) == rd.frame_box(config)


# ---- the clock verdict does not flicker at the tolerance ------------------------------------
def test_the_refusal_limit_follows_the_last_verdict():
    assert rd._PollNet(_cfg(SERVER_CLOCK_TOL_SEC=60)).limit == 60
    assert rd._PollNet(_cfg(SERVER_CLOCK_TOL_SEC=60), clock_ok=True).limit == 60
    assert rd._PollNet(_cfg(SERVER_CLOCK_TOL_SEC=60), clock_ok=False).limit == 50
    assert rd._PollNet(_cfg(SERVER_CLOCK_TOL_SEC=0), clock_ok=False).limit == 0
    assert servertime.limit(12, False) == 6                  # never below half
    assert servertime.limit(None, False) == servertime.limit("x", True) == 0.0


def test_the_radar_clock_verdict_does_not_flicker_at_the_tolerance(monkeypatch):
    # a clock ~60 s off: the Date header's whole seconds put the skew just inside the
    # tolerance on one reply and just outside on the next — before, every flip was a
    # CLOCK event on the SD card (68 in six hours)
    monkeypatch.setattr(rd, "deps_available", lambda: True)
    monkeypatch.setattr(config, "RADAR_LATCH_FILE", "/nonexistent-dir/radar_latch.json")
    net = _Net({lambda u: "list-type" in u: (200, _listing(_t(1, 26))),
                lambda u: "lcref" in u: (200, b"")}, skew=61.0)
    monkeypatch.setattr(rd, "_http", net)
    monkeypatch.setattr(rd, "fetch_frame", lambda *a: object())
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    ev = _Events()
    p = rd.RadarPoller(config, ev)
    p._thumbs = []
    p.poll_now(now=NOW)
    assert p.component(-10.0, NOW)["clock"]["ok"] is False and ev.kinds() == ["CLOCK"]
    for i, skew in enumerate((59.0, 61.0, 58.0, 59.5, 52.0)):    # within 1 s of 60, or > 50
        net.skew = skew
        assert p.poll_now(now=NOW + 60 * (i + 1))["ok"] is False
    assert ev.kinds() == ["CLOCK"]                                # no flicker
    net.skew = 49.0                                               # clearly inside: accepted
    assert p.poll_now(now=NOW + 600)["ok"] is True
    assert ev.kinds() == ["CLOCK", "CLOCK"]
    net.skew = 59.0                    # a clock found right keeps the whole tolerance
    assert p.poll_now(now=NOW + 900)["ok"] is True and ev.kinds() == ["CLOCK", "CLOCK"]


def _glm_listing(skew, listing=b"<ListBucketResult/>"):
    """A fake GLM bucket: every listing is ``listing``, dated skew[0] s off this clock."""
    def _fetch(url, timeout=15):
        return listing, {"Date": email.utils.formatdate(time.time() + skew[0], usegmt=True)}
    return _fetch


def test_the_glm_clock_verdict_is_logged_once_and_does_not_flicker(monkeypatch, caplog):
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(config, "GLM_LATCH_FILE", "/nonexistent-dir/glm_latch.json")
    skew = [61.0]
    monkeypatch.setattr(gl, "_fetch", _glm_listing(skew))
    ev = _Events()
    p = gl.GlmLightningPoller(config, ev)
    with caplog.at_level(logging.INFO, logger="ttu.safety.glm"):
        r = p.poll_now()
        assert r["ok"] is False and r["clock_skew_s"] > 60
        # before, a GLM refusal was in neither log: now it is an error and a CLOCK event
        assert ev.kinds() == ["CLOCK"] and ev.records[0][1]["result"] == "GLM granules refused"
        assert "behind the GOES bucket's" in ev.records[0][1]["reason"]
        for s in (59.0, 55.0, 52.0):                  # beyond 60 - 10: still refused
            skew[0] = s
            assert "clock_skew_s" in p.poll_now()
        assert ev.kinds() == ["CLOCK"] and p.component(-10.0)["warnings"]
        skew[0] = 49.0                                 # agrees again
        r = p.poll_now()
    assert r["server_skew_s"] is not None and "clock_skew_s" not in r
    assert ev.kinds() == ["CLOCK", "CLOCK"]
    assert ev.records[1][1]["reason"] == "system clock agrees with the GOES bucket again"
    errors = [m.getMessage() for m in caplog.records if m.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].startswith("WRONG SYSTEM CLOCK?")


def test_glm_refuses_a_listing_from_a_clock_that_is_ahead(monkeypatch):
    # the guard is two-sided: a Pi clock 158 days AHEAD would read a future date's (empty)
    # hours — before this test only the clock-behind side was checked
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "_fetch", _glm_listing([-158 * 86400.0]))
    res = gl.poll_flashes(config)
    assert res["ok"] is False and res["clock_skew_s"] < -150 * 86400
    assert "158 d ahead of the server's (AWS S3)" in res["error"]


def test_glm_poll_failures_are_logged_when_they_start_and_end(monkeypatch, caplog):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", "/nonexistent-dir/glm_latch.json")
    seq = [{"ok": False, "error": "list: timed out"}] * 3 + [
        {"ok": True, "error": None, "granules_scanned": 14, "in_ring": False,
         "nearest_km": None, "nearest_bearing": None}]
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: seq.pop(0))
    p = gl.GlmLightningPoller(config, _Events())
    with caplog.at_level(logging.INFO, logger="ttu.safety.glm"):
        for _ in range(4):
            p.poll_now()
    msgs = [m.getMessage() for m in caplog.records if m.name == "ttu.safety.glm"]
    assert sum(m.startswith("GLM poll failed: list: timed out") for m in msgs) == 1
    assert "GLM polls work again (14 granules read)" in msgs


def test_an_old_glm_refusal_is_no_current_warning(monkeypatch):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", "/nonexistent-dir/glm_latch.json")
    res = {"ok": False, "clock_skew_s": 9e6, "error": "this host's clock is 104 d 4 h behind"
           " the server's (AWS S3): GLM granules refused until the clock is corrected"}
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: res)
    p = gl.GlmLightningPoller(config, _Events())
    t = time.time()
    p.poll_now(now=t)
    assert p.component(-10.0, now=t + 60)["warnings"] == [res["error"]]
    assert p.component(-10.0, now=t + config.GLM_STALE_AFTER_SEC + 1)["warnings"] == []


def test_after_a_failed_glm_poll_there_is_no_current_distance(monkeypatch):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", "/nonexistent-dir/glm_latch.json")
    monkeypatch.setattr(gl, "deps_available", lambda: True)       # (CI has no numpy)
    good = {"ok": True, "error": None, "granules_scanned": 14, "in_ring": False,
            "nearest_km": 80.0, "nearest_bearing": "SW"}
    bad = {"ok": False, "granules_scanned": 0, "error": "no GLM granule in the last 5 min"}
    seq = [good, bad]
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: seq.pop(0))
    p = gl.GlmLightningPoller(config, _Events())
    t = time.time()
    p.poll_now(now=t)
    c = p.component(-10.0, now=t)
    assert (c["available"], c["nearest_km"], c["nearest_bearing"], c["error"]) \
        == (True, 80.0, "SW", None)
    p.poll_now(now=t + 300)
    c = p.component(-10.0, now=t + 300)
    # the 80 km of the poll before is no measurement of this one (the weather log, which
    # has no GLM-availability column, recorded it as if it were)
    assert (c["available"], c["nearest_km"], c["nearest_bearing"]) == (False, None, None)
    assert c["error"] == "no GLM granule in the last 5 min"


# ---- fewer requests during outages -----------------------------------------------------------
def test_quick_retries_stop_after_three_misses_in_a_row(monkeypatch):
    p = _poller(monkeypatch)
    frames = _stub_sources(monkeypatch, {"aws": None})          # no source has a frame
    q, full = config.RADAR_RETRY_SEC, config.RADAR_POLL_INTERVAL
    every = []
    for i in range(5):
        p.poll_now(now=NOW + 60 * i)
        every.append(p._poll_every)
    assert every == [q, q, q, full, full]
    frames["aws"] = "new"
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    assert p.poll_now(now=NOW + 600)["ok"] is True and p._poll_every == full
    frames["aws"] = None
    p.poll_now(now=NOW + 900)
    assert p._poll_every == q                                   # a new miss is quick again


def test_a_stalled_iem_with_the_bucket_down_costs_the_old_request_rate(monkeypatch):
    # IEM stalled (404 everywhere) and the bucket not answering, for an hour: every
    # 60-s retry used to walk IEM's whole 20-min window — ~480 HEADs an hour; the
    # single-source radar before the AWS bucket made 96
    monkeypatch.setattr(rd, "_http", _Net({lambda u: "list-type" in u:
                                           urllib.error.URLError("timed out")}))
    net = rd._http
    p = _poller(monkeypatch)
    for s in range(0, 3600, 20):                     # the radar loop wakes every 20 s
        p.maybe_poll(-10.0, now=NOW + s)
    heads = sum(1 for method, _ in net.calls if method == "HEAD")
    assert 100 <= heads <= 140


def test_a_refused_file_is_not_downloaded_again_but_a_newer_one_is(monkeypatch):
    p = _poller(monkeypatch)
    ts = NOW - 60
    frames = _stub_sources(monkeypatch, {"aws": (ts, "http://x/aws/a")})
    tried = []

    def fetch(cfg, net, source, url, t, box):
        tried.append(t)
        raise rd.FrameError("grid of 7000x3501 cells (not lcref's)")
    monkeypatch.setattr(rd, "fetch_frame", fetch)
    p.poll_now(now=NOW)
    p.poll_now(now=NOW + 60)
    assert tried == [ts]                                 # the same bytes: not again
    err = p.component(-10.0, NOW + 60)["sources"]["aws"]["error"]
    assert "not lcref's" in err and err.endswith("(not downloaded again)")
    frames["aws"] = (ts + 120, "http://x/aws/b")
    p.poll_now(now=NOW + 120)
    assert tried == [ts, ts + 120]


def test_a_file_that_could_not_be_downloaded_is_tried_again(monkeypatch):
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch, {"aws": (NOW - 60, "http://x/aws/a")})
    tried = []

    def fetch(cfg, net, source, url, t, box):
        tried.append(t)
        raise rd.SourceError("noaa-mrms-pds.s3.amazonaws.com: no answer (timed out)")
    monkeypatch.setattr(rd, "fetch_frame", fetch)
    p.poll_now(now=NOW)
    p.poll_now(now=NOW + 60)
    assert tried == [NOW - 60, NOW - 60]


def test_a_frame_download_error_status_is_a_source_error_not_a_refusal(monkeypatch):
    monkeypatch.setattr(rd, "_http", _Net(default=(404, b"")))
    with pytest.raises(rd.SourceError, match="HTTP 404 for the frame of 01:22Z") as e:
        rd.fetch_frame(config, rd._PollNet(config), "aws", "https://e.org/f", _t(1, 22),
                       (0, 0, 1, 1))
    assert not isinstance(e.value, rd.FrameError)    # so it is tried again next poll


def test_a_reply_larger_than_its_limit_is_refused(monkeypatch):
    monkeypatch.setattr(rd, "_http", _Net(default=(200, b"x" * 101)))
    pn = rd._PollNet(config)
    with pytest.raises(rd.FrameError, match="reply larger than"):
        pn.request("aws", "https://e.org/x", max_bytes=100)
    assert pn.request("aws", "https://e.org/y", max_bytes=101) == (200, b"x" * 101)


# ---- decoding: Pillow 14, bounded inflating, header checks first -----------------------------
def test_pixel_values_without_getdata():
    # Pillow 14 removes Image.getdata(); Pillow 11 (the Pi's) has no get_flattened_data()
    class _Old:
        def getdata(self):
            return [1, 2, 3]

    class _New:
        def get_flattened_data(self):
            return (4, 5)

        def getdata(self):
            raise AssertionError("getdata used where get_flattened_data exists")
    assert list(rd._values(_Old())) == [1, 2, 3] and list(rd._values(_New())) == [4, 5]


@pytest.mark.usefixtures("small_grid")
def test_a_file_that_inflates_beyond_the_limit_is_refused(monkeypatch):
    msg = _grib2(_t(1, 22), _png16({}), **G)
    monkeypatch.setattr(rd, "GRIB_MAX_BYTES", len(msg) - 1)
    with pytest.raises(rd.FrameError, match="inflates to more than"):
        rd.aws_image(gzip.compress(msg), _t(1, 22), (0, 0, 10, 10))
    monkeypatch.setattr(rd, "GRIB_MAX_BYTES", len(msg))
    assert rd.aws_image(gzip.compress(msg), _t(1, 22), (0, 0, 10, 10)).size == (400, 400)
    # a stream cut short is still refused, as gzip.decompress refused it
    with pytest.raises(rd.FrameError, match="ends early"):
        rd.aws_image(gzip.compress(msg)[:-20], _t(1, 22), (0, 0, 10, 10))


@pytest.mark.usefixtures("small_grid")
def test_png_size_is_checked_before_the_pixels_are_decoded(monkeypatch):
    pytest.importorskip("PIL")
    import io

    from PIL import Image, ImageFile
    # a payload that is not the grid: refused from its header, never decoded
    payload = _grib2(_t(1, 22), _png16({}, size=(401, 400)), **G)
    buf = io.BytesIO()
    Image.new("P", (401, 400)).save(buf, format="PNG")
    decoded = []
    real_load = ImageFile.ImageFile.load

    def load(self):                    # what Image.open()'s images decode their pixels with
        decoded.append(self.size)
        return real_load(self)
    monkeypatch.setattr(ImageFile.ImageFile, "load", load)
    with pytest.raises(rd.FrameError, match="PNG payload of 401x400"):
        rd.aws_image(payload, _t(1, 22), (0, 0, 9, 9))
    with pytest.raises(rd.FrameError, match="PNG of 401x400"):
        rd.lcref_image(buf.getvalue())
    assert decoded == []
    # the right size is decoded as before
    rd.aws_image(_grib2(_t(1, 22), _png16({}), **G), _t(1, 22), (0, 0, 9, 9))
    assert decoded and set(decoded) == {(400, 400)}


@pytest.mark.usefixtures("small_grid")
def test_grib2_signed_numbers_are_sign_and_magnitude():
    assert rd._sint(b"\x80\x01") == -1 and rd._sint(b"\x00\x01") == 1
    assert rd._sint(b"\x80\x00\x00\x05") == -5 and rd._sint(b"\x7f\xff") == 32767
    drs = {"R": -9990.0, "E": -1, "D": -2, "nbits": 16}
    meta = rd.parse_grib2(_grib2(_t(1, 22), b"png", **G, drs=drs))
    assert (meta["drs"]["E"], meta["drs"]["D"]) == (-1, -2)


@pytest.mark.usefixtures("small_grid")
def test_other_packings_are_refused():
    msg = bytearray(_grib2(_t(1, 22), b"png", **G))
    s5 = msg.index(b"\x00\x00\x00\x15\x05")          # section 5: 21 bytes long
    msg[s5 + 9:s5 + 11] = (40).to_bytes(2, "big")    # JPEG 2000 instead of PNG
    with pytest.raises(rd.FrameError, match=r"data template 5\.40"):
        rd.aws_image(bytes(msg), _t(1, 22), (0, 0, 10, 10))


def test_frame_box_covers_a_ring_wider_than_the_map_with_its_margin():
    cfg = _cfg(RADAR_TRIGGER_KM=150.0, RADAR_THUMB_HALF_DEG=0.5)
    lat0, lon0 = cfg.GEOCODE
    dlat = 150.0 / 111.0
    dlon = 150.0 / (111.0 * math.cos(math.radians(lat0)))
    m = rd.FRAME_BOX_MARGIN_DEG
    c0, r0 = rd.latlon_to_px(lat0 + dlat + m, lon0 - dlon - m)
    c1, r1 = rd.latlon_to_px(lat0 - dlat - m, lon0 + dlon + m)
    assert rd.frame_box(cfg) == (c0, r0, c1 + 1, r1 + 1)


# ---- the poll's state -------------------------------------------------------------------------
def test_a_repeated_frame_after_a_clock_step_keeps_the_freeze(monkeypatch, tmp_path):
    p = _poller(monkeypatch, tmp_path=tmp_path)
    frames = _stub_sources(monkeypatch)
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (True, 9.0, 3))
    p.poll_now(now=NOW)
    assert p.poll_now(now=NOW + 300)["confirmed"] is True
    assert p._last_rain_ts == NOW + 300
    frames["aws"] = (NOW + 240, "http://x/aws/%d" % (NOW + 240))     # that frame again
    p.clock_stepped(NOW + 300, NOW + 340)
    r = p.poll_now(now=NOW + 345)
    assert r["new"] is False and r["confirmed"] is False
    assert p._last_rain_ts == NOW + 300           # the freeze runs from the real detection


def test_a_frame_from_the_future_is_not_current_and_the_stale_limit_is_inclusive(monkeypatch):
    p = _poller(monkeypatch)
    p._frame_ts = 1000.0 + 61.0               # beyond the 60 s a clock may be off unnoticed
    assert p.component(-10.0, now=1000.0)["available"] is False
    p._frame_ts = 1000.0 + 59.0
    assert p.component(-10.0, now=1000.0)["available"] is True
    p._frame_ts = 1000.0
    stale = config.RADAR_STALE_AFTER_SEC
    assert p.component(-10.0, now=1000.0 + stale)["available"] is True
    assert p.component(-10.0, now=1000.0 + stale + 1)["available"] is False


def test_a_count_made_under_a_clock_far_ahead_is_dropped_by_the_step(monkeypatch):
    p = _poller(monkeypatch)
    p._counted_ts, p._ring_streak = 10_000.0, 1
    p.clock_stepped(10_100.0, 5_000.0)            # the clock was ~1.4 h ahead
    assert p._counted_ts is None and p._ring_streak == 0
    p._counted_ts, p._ring_streak = 5_030.0, 1
    p.clock_stepped(5_100.0, 5_000.0)             # a small step keeps the count
    assert p._counted_ts == 5_030.0 and p._ring_streak == 1


def test_one_radar_server_agreeing_means_the_clock_is_right(monkeypatch):
    p = _poller(monkeypatch)
    net = rd._PollNet(config)
    net.skews = [("aws", 0.4), ("iem", 3600.0)]       # IEM's own clock off: not ours
    p._note_clock(net)
    assert p._clock_ok is True and p._clock_skew == 0.4
    net.skews = [("aws", 3600.0), ("iem", 3601.0)]
    p._note_clock(net)
    assert p._clock_ok is False


# ---- the wrong-clock guard's own helpers --------------------------------------------------
def test_a_date_without_a_zone_is_read_as_utc():
    # RFC 5322's "-0000" means "no zone information" and parses to a naive time; read as
    # the Pi's local time (CST) it would make a right clock look 6 h off
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        now = 1794600000.0
        date = email.utils.formatdate(now)              # "... -0000"
        assert date.endswith("-0000")
        assert abs(servertime.skew_s({"Date": date}, now=now)) < 1
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_durations_are_said_in_their_last_unit():
    d = servertime.describe
    assert [d(45), d(-45), d(119.4), d(120), d(7199), d(5 * 3600 + 600), d(158 * 86400 + 3 * 3600)] \
        == ["45 s", "45 s", "119 s", "2 min", "2 h", "5 h 10 min", "158 d 3 h"]


# ---- settings ---------------------------------------------------------------------------------
def _settings(**env):
    code = ("import json; from safety import config as c; print(json.dumps({"
            "'tol': c.SERVER_CLOCK_TOL_SEC, 'retry': c.RADAR_RETRY_SEC, "
            "'aws': c.RADAR_AWS_URL, 'sources': list(c.RADAR_SOURCES), "
            "'warnings': c.CONFIG_WARNINGS}))")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TTU_SAFETY_")}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=dict(clean, **env),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_radar_and_clock_guard_settings_are_checked():
    plain = _settings()
    assert (plain["tol"], plain["retry"], plain["sources"]) == (60, 60, ["aws", "iem"])
    odd = _settings(TTU_SAFETY_SERVER_CLOCK_TOL_SEC="30", TTU_SAFETY_RADAR_RETRY_SEC="5",
                    TTU_SAFETY_RADAR_AWS_URL="ftp://example.org",
                    TTU_SAFETY_RADAR_SOURCES="aws,radarscope")
    assert (odd["tol"], odd["retry"], odd["aws"], odd["sources"]) \
        == (60, 30, "https://noaa-mrms-pds.s3.amazonaws.com", ["aws"])
    w = " | ".join(odd["warnings"])
    assert "below 60 s, too tight for an HTTP Date" in w and "TTU_SAFETY_RADAR_RETRY_SEC" in w
    assert "is not an http(s) URL" in w and "ignoring unknown radar source(s) radarscope" in w
    assert _settings(TTU_SAFETY_SERVER_CLOCK_TOL_SEC="-1")["tol"] == 60
    off = _settings(TTU_SAFETY_SERVER_CLOCK_TOL_SEC="0")
    assert off["tol"] == 0 and any("guard is OFF" in x for x in off["warnings"])


# ---- pages ------------------------------------------------------------------------------------
def test_source_problems_and_the_why_are_escaped_on_the_page():
    from safety.tests.test_smoke import msp
    rad = {"sources": {"aws": {"name": "NOAA Open Data (AWS)", "error": "<b>bad</b>"}},
           "frame_source": None}
    out = msp._radar_sources_html(rad)
    assert "<b>bad</b>" not in out and "&lt;b&gt;bad&lt;/b&gt;" in out
    c = dict(rd.unavailable_component(config), enabled=True, why="<script>x</script>")
    page = msp.build_radar_html({"ts": time.time(), "components": {"radar": c}})
    assert "<script>x" not in page and "&lt;script&gt;x" in page


def test_the_glm_tile_says_why_the_last_poll_failed():
    from safety.tests.test_hazards_page import _state, _tile
    from safety.tests.test_smoke import msp
    st = _state()
    st["components"]["glm"] = dict(gl.unavailable_component(config), enabled=True,
                                   polling_active=True,
                                   error="this host's clock is 5 min ahead <x>")
    tile = _tile(msp.build_safety_tiles_html(st["components"]), "Lightning (GLM)")
    assert "last poll failed &mdash; this host&#x27;s clock is 5 min ahead &lt;x&gt;" in tile
    st["components"]["glm"]["error"] = None
    tile = _tile(msp.build_safety_tiles_html(st["components"]), "Lightning (GLM)")
    assert "no successful poll yet" in tile


def test_setup_says_why_glm_has_no_data(tmp_path, monkeypatch):
    from safety.tests.test_alpaca import _client

    class _Stub:
        def component(self, sun_alt=None, now=None):
            return dict(gl.unavailable_component(config), enabled=True, polling_active=True,
                        error="no GLM granule in the last 5 min")
    c, mon = _client(tmp_path, monkeypatch)
    mon.glm = _Stub()
    page = c.get("/setup").get_data(as_text=True)
    assert "no fresh data — no GLM granule in the last 5 min" in page


# ---- GLM granules read without HDF5 complaining (found by the Raspberry Pi OS test) ---------
def _granule(tmp_path):
    """A small netCDF-4 (HDF5) file with flash_lat / flash_lon, as a GLM granule has."""
    path = tmp_path / "granule.nc"
    ds = gl.netCDF4.Dataset(str(path), "w", format="NETCDF4")
    ds.createDimension("n", 3)
    ds.createVariable("flash_lat", "f4", ("n",))[:] = [33.7, 34.0, 35.1]
    ds.createVariable("flash_lon", "f4", ("n",))[:] = [-101.9, -102.0, -100.5]
    ds.close()
    return path.read_bytes()


@pytest.mark.skipif(not gl.deps_available(), reason="numpy/netCDF4 not installed (normal CI)")
def test_a_granule_read_in_the_glm_thread_makes_hdf5_print_nothing(tmp_path, capfd):
    # Raspberry Pi OS's HDF5 prints a ~20-line error stack for every granule opened FROM
    # MEMORY in a thread other than the main one — the GLM poll's (the open itself works);
    # opened from a file in /dev/shm there is nothing to complain about. (Only an HDF5
    # built thread-safe, as Debian's is, shows it: this test bites in the Raspberry Pi OS
    # job, raspios.yml; pip's netCDF4 wheels print nothing either way.)
    import threading
    buf = _granule(tmp_path)
    got = {}
    capfd.readouterr()
    t = threading.Thread(target=lambda: got.update(v=gl._flashes_from_buffer(buf)))
    t.start()
    t.join()
    lats, lons = got["v"]
    assert [round(float(x), 1) for x in lats] == [33.7, 34.0, 35.1]
    assert [round(float(x), 1) for x in lons] == [-101.9, -102.0, -100.5]
    # the in-memory open's failed probe names its dataset "inmem.nc" (older HDF5 — e.g.
    # bookworm's 1.12 — also reports attribute probes from threads: not this problem)
    assert "inmem.nc" not in capfd.readouterr().err


@pytest.mark.skipif(not gl.deps_available(), reason="numpy/netCDF4 not installed (normal CI)")
def test_without_dev_shm_a_granule_is_read_from_memory_never_from_disk(tmp_path, monkeypatch):
    buf = _granule(tmp_path)
    isdir = os.path.isdir
    monkeypatch.setattr(gl.os.path, "isdir", lambda p: False if p == "/dev/shm" else isdir(p))

    def no_file(*a, **k):
        raise AssertionError("a granule written to a file outside /dev/shm")
    monkeypatch.setattr(gl.tempfile, "mkstemp", no_file)
    lats, _ = gl._flashes_from_buffer(buf)
    assert len(lats) == 3


# ---- a download cut short is a failed download, not a bad file (second review) -------------
class _Reply:
    def __init__(self, length):
        self.headers = {} if length is None else {"Content-Length": str(length)}


def test_a_reply_cut_short_is_an_incomplete_download():
    import http.client
    with pytest.raises(http.client.IncompleteRead):
        rd._check_complete(_Reply(100), b"x" * 50, None)
    rd._check_complete(_Reply(100), b"x" * 100, None)          # complete
    rd._check_complete(_Reply(None), b"x" * 50, None)          # no length announced
    rd._check_complete(_Reply(100), b"x" * 41, 40)             # stopped by our own limit


def test_a_frame_cut_short_is_downloaded_again_not_refused(monkeypatch):
    import http.client
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch, {"aws": (NOW - 60, "https://e.org/aws/a")})
    # the server closes the connection half-way through the file
    monkeypatch.setattr(rd, "_http", _Net(default=http.client.IncompleteRead(b"", 923259)))
    tried = []

    def fetch(cfg, net, source, url, ts, box):     # the real download, counted
        tried.append(ts)
        return _REAL_FETCH(cfg, net, source, url, ts, box)
    monkeypatch.setattr(rd, "fetch_frame", fetch)
    p.poll_now(now=NOW)
    p.poll_now(now=NOW + 60)
    assert tried == [NOW - 60, NOW - 60] and p._refused == {}
    assert "no answer (IncompleteRead" in p.component(-10.0, NOW + 60)["sources"]["aws"]["error"]


def test_a_glm_feed_outage_after_a_clock_refusal_is_still_logged(monkeypatch, caplog):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", "/nonexistent-dir/glm_latch.json")
    seq = [{"ok": False, "clock_skew_s": 300.0, "error": "this host's clock is 5 min behind"},
           {"ok": False, "granules_scanned": 0, "server_skew_s": 0.4,
            "error": "no GLM granule in the last 5 min"},
           {"ok": False, "granules_scanned": 0, "server_skew_s": 0.3,
            "error": "no GLM granule in the last 5 min"}]
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: seq.pop(0))
    p = gl.GlmLightningPoller(config, _Events())
    with caplog.at_level(logging.INFO, logger="ttu.safety.glm"):
        for _ in range(3):
            p.poll_now()
    msgs = [m.getMessage() for m in caplog.records if m.name == "ttu.safety.glm"]
    assert sum(m.startswith("GLM poll failed: no GLM granule") for m in msgs) == 1
    assert "the system clock agrees with the GOES bucket again" in msgs


# ---- gaps the second review's mutation pass found ------------------------------------------
def test_the_inflate_limit_leaves_room_for_a_stormy_frame():
    # the busiest AWS frame measured (2024-04-27 22:00Z, storms over half of CONUS) inflates
    # to 1.89 MB: a limit near that would refuse frames exactly on stormy days
    assert rd.GRIB_MAX_BYTES >= 4 * 1_892_400


def test_a_successful_glm_poll_after_a_refusal_restores_the_verdict(monkeypatch, caplog):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", "/nonexistent-dir/glm_latch.json")
    good = {"ok": True, "error": None, "granules_scanned": 14, "in_ring": False,
            "nearest_km": None, "nearest_bearing": None, "server_skew_s": 0.2}
    seq = [dict(good), {"ok": False, "clock_skew_s": 300.0, "error": "clock 5 min behind"},
           dict(good)]
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: seq.pop(0))
    ev = _Events()
    p = gl.GlmLightningPoller(config, ev)
    with caplog.at_level(logging.INFO, logger="ttu.safety.glm"):
        p.poll_now()
        # the first good poll of a run is no "agrees AGAIN"
        assert p._clock_ok is True and ev.kinds() == []
        p.poll_now()
        assert p._clock_ok is False and ev.kinds() == ["CLOCK"]
        p.poll_now()
    assert p._clock_ok is True and ev.kinds() == ["CLOCK", "CLOCK"]
    assert ev.records[1][1]["result"] == "GLM granules accepted"


def test_a_clock_step_makes_the_next_miss_quick_again(monkeypatch):
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch, {"aws": None})
    for i in range(5):
        p.poll_now(now=NOW + 60 * i)
    assert p._poll_every == config.RADAR_POLL_INTERVAL          # quick retries used up
    p.clock_stepped(NOW + 300, NOW + 400)
    assert p._misses == 0
    p.poll_now(now=NOW + 400)
    assert p._poll_every == config.RADAR_RETRY_SEC


@pytest.mark.usefixtures("small_grid")
def test_a_payload_with_a_good_header_and_corrupt_pixels_is_a_frame_error():
    png = bytearray(_png16({}))
    idat = png.index(b"IDAT")
    for i in range(idat + 8, min(len(png) - 16, idat + 40)):   # corrupt the pixel data
        png[i] ^= 0x5A
    with pytest.raises(rd.FrameError, match="unreadable PNG payload"):
        rd.aws_image(_grib2(_t(1, 22), bytes(png), **G), _t(1, 22), (0, 0, 9, 9))


def test_the_ring_is_checked_around_the_site_the_box_was_cut_for(monkeypatch):
    # even if the GPS position is adopted in the very instant between the box and the
    # check, the check reads the poll's site, not the new one (radar._AtSite)
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch)
    site0 = tuple(config.GEOCODE)
    seen = {}

    def box(cfg, thumbs=()):
        seen["box"] = tuple(cfg.GEOCODE)
        return (0, 0, 1, 1)

    def check(cfg, img):
        monkeypatch.setattr(config, "GEOCODE", (site0[0] + 1.5, site0[1]))   # adopted now
        seen["check"] = tuple(cfg.GEOCODE)
        return (False, None, 0)
    monkeypatch.setattr(rd, "frame_box", box)
    monkeypatch.setattr(rd, "check_rain", check)
    assert p.poll_now(now=NOW)["ok"] is True
    assert seen == {"box": site0, "check": site0}


def test_a_refused_newer_frame_is_not_called_no_newer_frame(monkeypatch):
    p = _poller(monkeypatch)
    frames = _stub_sources(monkeypatch)
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    assert p.poll_now(now=NOW)["ok"] is True                    # a frame is held
    frames["aws"] = (NOW + 60, "http://x/aws/b")
    frames["fetch_error"] = {"aws": rd.FrameError("not lcref's grid")}
    p.poll_now(now=NOW + 120)                                   # refused
    why = p.poll_now(now=NOW + 180)["error"]                    # skipped as refused
    assert not why.startswith("no frame newer than") and "(not downloaded again)" in why


def test_a_successful_glm_poll_reports_the_clock_it_agreed_with(monkeypatch):
    # through the real poll_flashes: a good poll carries the bucket's skew, which is what
    # tells the poller that the clock agrees again after a refusal
    import types
    from datetime import datetime, timezone
    start = datetime.now(timezone.utc).strftime("%Y%j%H%M%S") + "0"
    listing = ("<ListBucketResult><Key>GLM-L2-LCFA/OR_GLM-L2-LCFA_G19_s%s_e1_c2.nc</Key>"
               "</ListBucketResult>" % start).encode()
    date = email.utils.formatdate(time.time(), usegmt=True)
    empty = types.SimpleNamespace(size=0)
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "_fetch", lambda url, timeout=15: (
        (listing, {"Date": date}) if "list-type" in url else (b"nc", {})))
    monkeypatch.setattr(gl, "_flashes_from_buffer", lambda buf: (empty, empty))
    res = gl.poll_flashes(config)
    assert res["ok"] is True and abs(res["server_skew_s"]) < 5
