"""The radar's two frame sources (NOAA's MRMS bucket on AWS, IEM's lcref archive), the
exact GRIB2 -> lcref conversion, the fallback between them, frames counted once and fresh
by their own time, and the wrong-clock guard (radar and GLM). No network: every request
goes to a fake ``radar._http``.

Real-data checks behind the constants (2026-11-10/11, by hand against the live archives):
the bucket's GRIB2 converted by aws_image equals IEM's lcref PNG cell for cell (all 24.5 M
cells of 01:22Z; the box around a 60.5 dBZ Gulf-coast cell at 02:00Z: 23,544 echo cells,
114 distinct values, none different), and IEM's values below are cells of those frames.
"""
import email.utils
import gzip
import io
import json
import struct
import time
import types
import urllib.error
from datetime import datetime, timezone

import pytest

from safety import config, servertime
from safety import glm_lightning as gl
from safety import radar as rd
from safety.tests.test_radar import _poller, _stub_sources

UTC = timezone.utc
NOW = datetime(2026, 11, 11, 1, 28, 22, tzinfo=UTC).timestamp()


def _t(h, m, s=0):
    return datetime(2026, 11, 11, h, m, s, tzinfo=UTC).timestamp()


class _Events:
    def __init__(self):
        self.records = []

    def record(self, kind, **kw):
        self.records.append((kind, kw))
        return {}

    def kinds(self):
        return [k for k, _ in self.records]


# ---- the exact conversion -------------------------------------------------------------
MRMS_DRS = {"R": -9990.0, "E": 0, "D": 1, "nbits": 16}      # SeamlessHSR's packing


def _raw(dbz):
    return int(round((dbz + 999.0) * 10))


def test_lcref_index_reproduces_iem_values():
    idx = rd.lcref_index(MRMS_DRS)
    # IEM's index of cells whose GRIB2 value was this (2026-11-11 01:22Z / 02:00Z)
    for dbz, iem in ((8.4, 80), (10.8, 85), (11.3, 86), (13.3, 90), (13.9, 91), (7.3, 78),
                     (15.4, 94), (11.8, 87), (60.5, 185)):
        assert idx(_raw(dbz)) == iem, dbz
    # no echo, no coverage and anything below -32 dBZ: 0, as lcref writes them
    for dbz in (-99.0, -999.0, -32.0, -31.6):
        assert idx(_raw(dbz)) == 0, dbz
    assert idx(_raw(-31.5)) == 1
    # 255 is lcref's "no data": never written — beyond 95 dBZ is the strongest echo
    assert idx(_raw(95.5)) == idx(_raw(120.0)) == 254


def test_lcref_index_is_exact_over_the_whole_16_bit_range():
    # (raw / 10 - 999 + 32) * 2 = raw / 5 - 1934: integer arithmetic is the reference,
    # so no floating-point rounding can move a value across an index boundary
    idx = rd.lcref_index(MRMS_DRS)
    for raw in range(65536):
        assert idx(raw) == max(0, min(254, raw // 5 - 1934)), raw


# ---- GRIB2 messages built here (MRMS's layout) ------------------------------------------
def _sint(v, n):
    return ((1 << (8 * n - 1)) | -v if v < 0 else v).to_bytes(n, "big")


def _sec(num, body):
    return struct.pack(">IB", 5 + len(body), num) + body


def _grib2(ts, payload, ni, nj, la1, lo1, di=0.01, dj=0.01, scan=0, discipline=209,
           param=(8, 8), drs=None, bitmap=False):
    """A one-field GRIB2 message laid out as MRMS writes it (template 3.0, 4.0, 5.41)."""
    drs = drs or MRMS_DRS
    t = datetime.fromtimestamp(ts, UTC)
    s1 = _sec(1, struct.pack(">HHBBB", 161, 0, 2, 1, 1)
              + struct.pack(">HBBBBB", t.year, t.month, t.day, t.hour, t.minute, t.second)
              + bytes([0, 1]))
    micro = lambda deg: int(round(deg * 1e6))                       # noqa: E731
    s3 = _sec(3, bytes([0]) + (ni * nj).to_bytes(4, "big") + bytes([0, 0])
              + (0).to_bytes(2, "big") + bytes([6, 0]) + bytes(4) + bytes([0]) + bytes(4)
              + bytes([0]) + bytes(4) + ni.to_bytes(4, "big") + nj.to_bytes(4, "big")
              + bytes(4) + bytes(4) + _sint(micro(la1), 4) + _sint(micro(lo1), 4)
              + bytes([48]) + bytes(4) + bytes(4) + micro(di).to_bytes(4, "big")
              + micro(dj).to_bytes(4, "big") + bytes([scan]))
    assert len(s3) == 72
    s4 = _sec(4, bytes(2) + bytes(2) + bytes(param) + bytes(23))
    s5 = _sec(5, (ni * nj).to_bytes(4, "big") + (41).to_bytes(2, "big")
              + struct.pack(">f", drs["R"]) + _sint(drs["E"], 2) + _sint(drs["D"], 2)
              + bytes([drs["nbits"], 0]))
    assert len(s5) == 21
    s6 = _sec(6, bytes([0 if bitmap else 255]))
    s7 = _sec(7, payload)
    body = s1 + s3 + s4 + s5 + s6 + s7 + b"7777"
    return b"GRIB" + b"\x00\x00" + bytes([discipline, 2]) + (16 + len(body)).to_bytes(8, "big") + body


@pytest.fixture
def small_grid(monkeypatch):
    """A 400 x 400 lcref-style grid around TTU instead of CONUS's 7000 x 3500."""
    monkeypatch.setattr(rd, "UL_LAT", 35.495)
    monkeypatch.setattr(rd, "UL_LON", -103.495)
    monkeypatch.setattr(rd, "GRID_W", 400)
    monkeypatch.setattr(rd, "GRID_H", 400)
    return types.SimpleNamespace(ni=400, nj=400, la1=35.495, lo1=360 - 103.495)


def _png16(cells, fill=9000, size=(400, 400)):
    from PIL import Image
    im = Image.new("I;16", size, fill)
    for (c, r), v in cells.items():
        im.putpixel((c, r), v)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_bucket_file_becomes_the_lcref_image(small_grid):
    pytest.importorskip("PIL")
    c0, r0 = rd.latlon_to_px(*config.GEOCODE)
    box = rd.frame_box(config)
    assert box[0] < c0 < box[2] and box[1] < r0 < box[3]
    cells = {(c0, r0): _raw(8.4), (c0 + 3, r0): _raw(58.5), (c0, r0 + 3): _raw(-999.0),
             (c0 + 5, r0 + 5): _raw(-31.5), (c0 + 7, r0): _raw(99.0),
             (395, 395): _raw(45.0)}                        # outside the box: not read
    ts = _t(1, 22)
    g = small_grid
    msg = _grib2(ts, _png16(cells), g.ni, g.nj, g.la1, g.lo1)
    for data in (msg, gzip.compress(msg)):                  # the bucket's files are gzipped
        img = rd.aws_image(data, ts, box)
        assert img.mode == "L" and img.size == (400, 400)
        px = img.load()
        assert px[c0, r0] == 80 and px[c0 + 3, r0] == 181 and px[c0, r0 + 3] == 0
        assert px[c0 + 5, r0 + 5] == 1 and px[c0 + 7, r0] == 254
        assert px[395, 395] == 0 and px[c0 + 1, r0] == 0    # no echo: 0
        # rain is >= RADAR_DBZ (20): the 58.5 and 99 dBZ cells, 3 and 7 cells east
        in_ring, nearest, count = rd.check_rain(config, img)
        assert in_ring is True and count == 2 and 2.5 < nearest < 3.5


@pytest.mark.parametrize("change, why", [
    ({"param": (8, 9)}, "not MRMS's SeamlessHSR"),
    ({"discipline": 0}, "not MRMS's SeamlessHSR"),
    ({"la1": 35.6}, "not lcref's"),
    ({"di": 0.02}, "not lcref's"),
    ({"scan": 0x40}, "not lcref's"),
    ({"bitmap": True}, "unreadable GRIB2"),
])
def test_a_file_that_is_not_lcref_s_product_is_refused(small_grid, change, why):
    g = small_grid
    kw = dict(ni=g.ni, nj=g.nj, la1=g.la1, lo1=g.lo1)
    kw.update(change)
    msg = _grib2(_t(1, 22), b"not even a png", **kw)
    with pytest.raises(rd.FrameError, match=why):
        rd.aws_image(msg, _t(1, 22), (0, 0, 10, 10))


def test_a_file_holding_another_time_is_refused(small_grid):
    g = small_grid
    msg = _grib2(_t(1, 20), b"png", g.ni, g.nj, g.la1, g.lo1)
    with pytest.raises(rd.FrameError, match="holds the data of 2026-11-11 01:20:00Z"):
        rd.aws_image(msg, _t(1, 22), (0, 0, 10, 10))


def test_garbage_is_refused_not_guessed(small_grid):
    for data in (b"", b"<html>404</html>", gzip.compress(b"GRIB\x00\x00\xd1\x02" + bytes(8)),
                 b"\x1f\x8b broken gzip"):
        with pytest.raises(rd.FrameError):
            rd.aws_image(data, _t(1, 22), (0, 0, 10, 10))
    g = small_grid
    msg = _grib2(_t(1, 22), b"not a png", g.ni, g.nj, g.la1, g.lo1)
    with pytest.raises(rd.FrameError, match="unreadable PNG payload"):
        rd.aws_image(msg, _t(1, 22), (0, 0, 10, 10))


def test_lcref_png_is_checked():
    pytest.importorskip("PIL")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, format="PNG")
    with pytest.raises(rd.FrameError, match="palette indices"):
        rd.lcref_image(buf.getvalue())
    with pytest.raises(rd.FrameError, match="unreadable PNG"):
        rd.lcref_image(b"<html>not found</html>")


def test_frame_box_covers_the_ring_and_every_map_pixel():
    t = rd.Thumbnailer(config, config.RADAR_TILE_URL, config.RADAR_THUMB_PATH, "dark")
    t._geometry()                       # the map's tile box (no network)
    t._build_remap()
    c0, r0, c1, r1 = rd.frame_box(config, [t])
    for row in t._remap:
        for c, r in row:
            assert c0 <= c < c1 and r0 <= r < r1
    # check_rain's scan box
    lat0, lon0 = config.GEOCODE
    cc, rr = rd.latlon_to_px(lat0, lon0)
    import math
    dc = int(config.RADAR_TRIGGER_KM / (111.0 * math.cos(math.radians(lat0))) / rd.PX) + 1
    dr = int(config.RADAR_TRIGGER_KM / 111.0 / rd.PX) + 1
    assert c0 <= cc - dc and cc + dc < c1 and r0 <= rr - dr and rr + dr < r1
    # small: ~45 000 cells, not the 24.5 M of the grid
    assert (c1 - c0) * (r1 - r0) < 60000


# ---- the network, faked ------------------------------------------------------------------
class _Net:
    """Stands in for radar._http: ``routes`` maps a URL (or a callable(url) -> bool) to
    (status, body) or an exception; replies carry a Date ``skew`` s off this host's clock
    (None: no Date header). Every request is logged."""

    def __init__(self, routes=None, skew=0.0, default=(404, b"")):
        self.routes, self.skew, self.default = list((routes or {}).items()), skew, default
        self.calls = []

    def headers(self):
        if self.skew is None:
            return {}
        return {"Date": email.utils.formatdate(time.time() + self.skew, usegmt=True)}

    def __call__(self, url, method="GET", timeout=10, max_bytes=None):
        self.calls.append((method, url))
        for key, reply in self.routes:
            if (key(url) if callable(key) else key == url):
                break
        else:
            reply = self.default
        if isinstance(reply, BaseException):
            raise reply
        status, body = reply
        return status, self.headers(), body


def _cfg(**over):
    c = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    for k, v in over.items():
        setattr(c, k, v)
    return c


# ---- IEM ---------------------------------------------------------------------------------
def test_iem_probe_starts_four_minutes_back_and_takes_the_newest(monkeypatch):
    net = _Net({rd.iem_url(_t(1, 22)): (200, b"")})
    monkeypatch.setattr(rd, "_http", net)
    got = rd.iem_newest(config, rd._PollNet(config), NOW)
    assert got == (_t(1, 22), rd.iem_url(_t(1, 22)))
    # 01:28:22 - 4 min -> 01:24 (not written yet), then 01:22: never 01:28 or 01:26,
    # which IEM (5.5 min behind) cannot have
    assert net.calls == [("HEAD", rd.iem_url(_t(1, 24))), ("HEAD", rd.iem_url(_t(1, 22)))]


def test_iem_probe_walks_back_to_the_stale_limit_only(monkeypatch):
    net = _Net()                                             # 404 everywhere
    monkeypatch.setattr(rd, "_http", net)
    assert rd.iem_newest(config, rd._PollNet(config), NOW) is None
    oldest = NOW - config.RADAR_STALE_AFTER_SEC
    times = [datetime.strptime(u.rsplit("_", 1)[1][:12], "%Y%m%d%H%M").replace(
        tzinfo=UTC).timestamp() for _, u in net.calls]
    assert times[0] == _t(1, 24) and min(times) >= oldest and len(times) == 8


def test_iem_that_does_not_answer_costs_one_request(monkeypatch):
    net = _Net(default=urllib.error.URLError("timed out"))
    monkeypatch.setattr(rd, "_http", net)
    pn = rd._PollNet(config)
    with pytest.raises(rd.SourceError, match="no answer"):
        rd.iem_newest(config, pn, NOW)
    assert len(net.calls) == 1                               # not one per frame probed
    with pytest.raises(rd.SourceError, match="no answer"):   # the host is down this poll
        pn.request("iem", rd.iem_url(_t(1, 0)), method="HEAD")
    assert len(net.calls) == 1


def test_iem_error_status_ends_the_walk(monkeypatch):
    monkeypatch.setattr(rd, "_http", _Net(default=(503, b"")))
    with pytest.raises(rd.SourceError, match="HTTP 503"):
        rd.iem_newest(config, rd._PollNet(config), NOW)


def test_spent_poll_deadline_sends_nothing(monkeypatch):
    net = _Net()
    monkeypatch.setattr(rd, "_http", net)
    clock = [1000.0]
    monkeypatch.setattr(rd, "_mono", lambda: clock[0])
    pn = rd._PollNet(config)
    clock[0] += rd.POLL_DEADLINE + 1
    with pytest.raises(rd.SourceError, match="no time left"):
        rd.iem_newest(config, pn, NOW)
    assert net.calls == []


# ---- the bucket ---------------------------------------------------------------------------
def _key(ts):
    d = datetime.fromtimestamp(ts, UTC)
    return "CONUS/SeamlessHSR_00.00/%s/MRMS_SeamlessHSR_00.00_%s.grib2.gz" % (
        d.strftime("%Y%m%d"), d.strftime("%Y%m%d-%H%M%S"))


def _listing(*times):
    keys = "".join("<Contents><Key>%s</Key><LastModified>x</LastModified></Contents>"
                   % _key(t) for t in times)
    return ('<?xml version="1.0"?><ListBucketResult>%s</ListBucketResult>' % keys).encode()


def _hour(url, h):
    return "SeamlessHSR_00.00_20261111-%02d" % h in url


def test_bucket_listing_gives_the_newest_file(monkeypatch):
    net = _Net({lambda u: _hour(u, 1): (200, _listing(_t(1, 20), _t(1, 22), _t(1, 26),
                                                      _t(1, 24)))})
    monkeypatch.setattr(rd, "_http", net)
    ts, url = rd.aws_newest(config, rd._PollNet(config), NOW)
    assert ts == _t(1, 26) and url == config.RADAR_AWS_URL + "/" + _key(_t(1, 26))
    assert len(net.calls) == 1 and "list-type=2" in net.calls[0][1]


def test_bucket_lists_the_previous_hour_only_while_the_window_reaches_it(monkeypatch):
    net = _Net({lambda u: _hour(u, 1): (200, _listing()),
                lambda u: _hour(u, 0): (200, _listing(_t(0, 56), _t(0, 58)))})
    monkeypatch.setattr(rd, "_http", net)
    # 01:01: the current hour has nothing yet; the 20-min window reaches into 00Z
    assert rd.aws_newest(config, rd._PollNet(config), _t(1, 1))[0] == _t(0, 58)
    assert len(net.calls) == 2
    # 01:25: the window starts at 01:05, so 00Z is not asked
    net.calls.clear()
    assert rd.aws_newest(config, rd._PollNet(config), _t(1, 25)) is None
    assert len(net.calls) == 1


def test_bucket_files_outside_the_window_are_not_frames(monkeypatch):
    # older than the stale limit, or further in the future than the clock tolerance
    net = _Net({lambda u: _hour(u, 1): (200, _listing(_t(1, 2), _t(1, 50)))})
    monkeypatch.setattr(rd, "_http", net)
    assert rd.aws_newest(config, rd._PollNet(config), NOW) is None


def test_bucket_error_status_is_a_source_error(monkeypatch):
    monkeypatch.setattr(rd, "_http", _Net(default=(403, b"AccessDenied")))
    with pytest.raises(rd.SourceError, match="HTTP 403 for the listing of 01Z"):
        rd.aws_newest(config, rd._PollNet(config), NOW)


# ---- the wrong-clock guard ----------------------------------------------------------------
def test_reply_from_a_server_whose_clock_disagrees_is_refused(monkeypatch):
    day = 86400.0
    monkeypatch.setattr(rd, "_http", _Net(default=(200, b""), skew=158 * day))
    with pytest.raises(rd.ClockMismatch, match="158 d behind the server's") as e:
        rd._PollNet(config).request("aws", "https://example.org/x")
    # (not pytest.approx: it trips over the mock 'numpy' the page tests put into sys.modules)
    assert abs(e.value.skew - 158 * day) <= 5
    monkeypatch.setattr(rd, "_http", _Net(default=(200, b""), skew=-2 * 3600))
    with pytest.raises(rd.ClockMismatch, match="2 h ahead of the server's"):
        rd._PollNet(config).request("aws", "https://example.org/x")


def test_clock_within_tolerance_or_no_date_header_is_accepted(monkeypatch):
    assert config.SERVER_CLOCK_TOL_SEC == 60
    for skew in (0.0, 30.0, -45.0, None):
        monkeypatch.setattr(rd, "_http", _Net(default=(200, b"ok"), skew=skew))
        assert rd._PollNet(config).request("aws", "https://example.org/x") == (200, b"ok")
    # a cache's Age counts: Date + Age is the server's now
    old = email.utils.formatdate(time.time() - 3600, usegmt=True)
    assert abs(servertime.skew_s({"Date": old, "Age": "3600"})) < 5
    assert servertime.skew_s({"Date": "yesterday"}) is None


def test_odd_date_and_age_headers_never_raise():
    now = email.utils.formatdate(time.time(), usegmt=True)
    # an absurd Age (a broken cache or proxy) is ignored, not an OverflowError
    for age in ("9" * 400, "-5", "1e9", "soon", 10 ** 9):
        assert abs(servertime.skew_s({"Date": now, "Age": age})) < 5, age
    for date in ("Mon, 01 Jan 0001 00:00:00 GMT", "Fri, 31 Dec 9999 23:59:59 GMT", "", None, 42):
        skew = servertime.skew_s({"Date": date})
        assert skew is None or isinstance(skew, float), date
    assert servertime.skew_s(object()) is None and servertime.skew_s(None) is None


def test_a_slow_download_is_not_a_wrong_clock(monkeypatch):
    # the Date is the moment the reply STARTED; a body read for 7 min (a slow link) must
    # not make the clock look 7 min ahead (the reply's headers arrived on time)
    def slow(url, method="GET", timeout=10, max_bytes=None):
        started = time.time() - 420
        return 200, {"Date": email.utils.formatdate(started, usegmt=True)}, b"frame", started
    monkeypatch.setattr(rd, "_http", slow)
    assert rd._PollNet(config).request("aws", "https://example.org/x") == (200, b"frame")


def test_http_notes_the_arrival_of_the_headers_not_the_end_of_the_body(monkeypatch):
    # the real _http: the reply's headers arrive at T, its body takes 7 min to read; the
    # Date is compared with T, so the clock is right
    clock = [NOW]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    class _Reply:
        status = 200

        def __init__(self):             # the server stamps the reply as it starts it
            self.headers = {"Date": email.utils.formatdate(clock[0], usegmt=True)}

        def read(self, n=-1):
            clock[0] += 420
            return b"frame"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(rd.urllib.request, "urlopen", lambda req, timeout=None: _Reply())
    status, headers, body, received = rd._http("https://example.org/x")
    assert (status, body, received) == (200, b"frame", NOW) and clock[0] == NOW + 420
    assert rd._PollNet(config).request("aws", "https://example.org/x") == (200, b"frame")


def test_a_slow_source_cannot_starve_the_other(monkeypatch):
    # IEM stalled AND overloaded: every HEAD answers 404, but only after 11 s (under the
    # 10-s socket timeout's reach). Its walk must not eat the poll: the bucket's frame
    # is still downloaded, and IEM's state says what happened.
    clock = [1000.0]
    monkeypatch.setattr(rd, "_mono", lambda: clock[0])

    def net(url, method="GET", timeout=10, max_bytes=None):
        if "mesonet" in url:
            clock[0] += 11.0
            return 404, {}, b""
        clock[0] += 0.5
        if "list-type" in url:
            return 200, {}, _listing(NOW - 70)
        return 200, {}, b"grib"
    monkeypatch.setattr(rd, "_http", net)
    monkeypatch.setattr(rd, "aws_image", lambda data, ts, box: object())
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    p = _poller(monkeypatch)
    r = p.poll_now(now=NOW)
    assert r["ok"] is True and r["source"] == "aws"
    assert p.component(-10.0, NOW)["sources"]["iem"]["error"] == \
        "the lookup took longer than %d s" % rd.LOOKUP_BUDGET


def test_frames_less_than_a_minute_apart_are_one_frame(monkeypatch):
    # both archives name MRMS's even minutes, but should a key ever carry seconds, the
    # same frame from the other source must not count (or download) twice
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {"aws": None, "iem": (_t(1, 2), "http://x/iem/0102")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (True, 9.0, 3))
    assert p.poll_now(now=_t(1, 8))["streak"] == 1
    fr["aws"], fr["iem"] = (_t(1, 2, 40), "http://x/aws/010240"), None
    r = p.poll_now(now=_t(1, 13))
    assert r["ok"] is False and fr["fetched"] == 1 and p._ring_streak == 1


def test_a_nan_packing_is_a_bad_frame_and_iem_stands_in(monkeypatch, small_grid):
    g = small_grid
    for r_value in (float("nan"), float("inf")):
        msg = _grib2(_t(1, 22), b"png", g.ni, g.nj, g.la1, g.lo1,
                     drs=dict(MRMS_DRS, R=r_value))
        with pytest.raises(rd.FrameError, match="packing"):
            rd.aws_image(msg, _t(1, 22), (0, 0, 10, 10))
    # in a poll: the bucket's bad file costs nothing but its own frame
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch, {"aws": (NOW - 60, "http://x/aws/nan"),
                                "iem": (NOW - 330, "http://x/iem/ok")})
    real_fetch = rd.fetch_frame

    def fetch(cfg, net, source, url, ts, box):
        if source == "aws":
            raise ValueError("cannot convert float NaN to integer")   # any surprise
        return object()
    monkeypatch.setattr(rd, "fetch_frame", fetch)
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    r = p.poll_now(now=NOW)
    assert r["ok"] is True and r["source"] == "iem" and real_fetch is not fetch
    assert "unexpected error (ValueError" in p.component(-10.0, NOW)["sources"]["aws"]["error"]


def test_a_source_lookup_that_raises_unexpectedly_leaves_the_other(monkeypatch):
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch, {"aws": OverflowError("int too large"),
                                "iem": (NOW - 330, "http://x/iem/a")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    r = p.poll_now(now=NOW)
    assert r["ok"] is True and r["source"] == "iem"
    assert "unexpected error (OverflowError" in p.component(-10.0, NOW)["sources"]["aws"]["error"]


def test_tolerance_zero_switches_the_guard_off(monkeypatch):
    monkeypatch.setattr(rd, "_http", _Net(default=(200, b"ok"), skew=158 * 86400.0))
    assert rd._PollNet(_cfg(SERVER_CLOCK_TOL_SEC=0)).request("aws", "https://e.org/x") \
        == (200, b"ok")


def test_wrong_clock_refuses_both_sources_and_warns(monkeypatch):
    # the 2026-08 incident: the Pi's clock months behind; both archives would have had
    # "now" (a March date's frames) — every reply's Date gives it away
    monkeypatch.setattr(rd, "deps_available", lambda: True)
    monkeypatch.setattr(config, "RADAR_LATCH_FILE", "/nonexistent-dir/radar_latch.json")
    net = _Net({lambda u: "list-type" in u: (200, _listing(_t(1, 26))),
                lambda u: "lcref" in u: (200, b"")}, skew=158 * 86400.0)
    monkeypatch.setattr(rd, "_http", net)
    ev = _Events()
    p = rd.RadarPoller(config, ev)
    p._thumbs = []
    r = p.poll_now(now=NOW)
    assert r["ok"] is False and p._frame_ts is None
    c = p.component(-10.0, NOW)
    assert c["available"] is False and c["clock"]["ok"] is False
    assert c["warnings"] and "158 d behind the radar servers'" in c["warnings"][0]
    assert all("158 d behind" in s["error"] for s in c["sources"].values())
    assert ev.kinds() == ["CLOCK"]
    # the clock is corrected: frames again, and the event log says so
    net.skew = 0.0
    monkeypatch.setattr(rd, "fetch_frame", lambda *a: object())
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    assert p.poll_now(now=NOW + 60)["ok"] is True
    c = p.component(-10.0, NOW + 60)
    assert c["warnings"] == [] and c["clock"]["ok"] is True and c["available"] is True
    assert ev.kinds() == ["CLOCK", "CLOCK"]


def test_monitor_shows_the_radar_and_glm_clock_warnings(env, write_inputs):
    from safety.monitor import SafetyMonitor

    class _Stub:
        def __init__(self, comp):
            self.c = comp

        def component(self, sun_alt=None, now=None):
            return self.c
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    radar = rd.unavailable_component(config)
    radar.update(enabled=True, warnings=["this host's clock is 158 d behind the radar "
                                         "servers' — radar frames refused"])
    glm = gl.unavailable_component(config)
    glm.update(enabled=True, warnings=["this host's clock is 158 d behind the server's "
                                       "(AWS S3): GLM granules refused"])
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], glm=_Stub(glm), radar=_Stub(radar))
    st = m.evaluate()
    assert radar["warnings"][0] in st["warnings"] and glm["warnings"][0] in st["warnings"]
    assert st["is_safe"] is True            # an unavailable layer never vetoes on its own


def test_glm_refuses_granules_listed_by_a_wrong_clock(monkeypatch):
    listing = b"<ListBucketResult><Key>GLM-L2-LCFA/x_s20260681200000_e1.nc</Key></ListBucketResult>"
    date = email.utils.formatdate(time.time() + 158 * 86400.0, usegmt=True)
    fetched = []

    def _fetch(url, timeout=15):
        fetched.append(url)
        return listing, {"Date": date}
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "_fetch", _fetch)
    poll_flashes = gl.poll_flashes
    res = poll_flashes(config)
    assert res["ok"] is False and res["clock_skew_s"] > 150 * 86400
    assert "158 d behind" in res["error"] and "GLM granules refused" in res["error"]
    assert len(fetched) == 1                # no granule of the wrong day is downloaded
    p = gl.GlmLightningPoller(config, _Events())
    monkeypatch.setattr(gl, "poll_flashes", lambda cfg: res)
    p.poll_now(now=time.time())
    c = p.component(-10.0)
    assert c["available"] is False and c["safe"] is True and c["warnings"] == [res["error"]]
    # a right clock: the listing is used — and a window without a single granule is a
    # GOES feed outage, no data, never "no lightning"
    date = email.utils.formatdate(time.time(), usegmt=True)
    monkeypatch.setattr(gl, "_fetch", lambda url, timeout=15: (b"<ListBucketResult/>",
                                                               {"Date": date}))
    res = poll_flashes(config)
    assert res["ok"] is False and "clock_skew_s" not in res
    assert res["error"] == "no GLM granule in the last %d min" % config.GLM_WINDOW_MIN


def test_glm_window_with_no_readable_granule_is_no_data(monkeypatch):
    start = datetime.now(UTC).strftime("%Y%j%H%M%S") + "0"
    listing = ("<ListBucketResult><Key>GLM-L2-LCFA/OR_GLM-L2-LCFA_G19_s%s_e1_c2.nc</Key>"
               "</ListBucketResult>" % start).encode()
    date = email.utils.formatdate(time.time(), usegmt=True)

    def _fetch(url, timeout=15):
        if "list-type" in url:
            return listing, {"Date": date}
        raise urllib.error.URLError("granule download failed")
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "_fetch", _fetch)
    res = gl.poll_flashes(config)
    assert res["ok"] is False and res["granules_scanned"] == 0
    assert res["error"] == "none of the 1 GLM granules of the last %d min could be read" \
        % config.GLM_WINDOW_MIN
    # one granule read, no flash anywhere: that IS "no lightning". (A granule without
    # flashes never reaches numpy, so a stand-in for its empty arrays will do: CI has no
    # numpy, only the page tests' mock of it, which importorskip("numpy") would return.)
    empty = types.SimpleNamespace(size=0)
    monkeypatch.setattr(gl, "_fetch", lambda url, timeout=15: (
        (listing, {"Date": date}) if "list-type" in url else (b"nc", {})))
    monkeypatch.setattr(gl, "_flashes_from_buffer", lambda buf: (empty, empty))
    res = gl.poll_flashes(config)
    assert res["ok"] is True and res["granules_scanned"] == 1 and res["in_ring"] is False


# ---- the poller: newest of the two, fallback, frames counted once -------------------------
def test_newest_frame_wins_and_the_bucket_is_usually_newer(monkeypatch):
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {"iem": (NOW - 360, "http://x/iem/a")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    r = p.poll_now(now=NOW)
    assert r["source"] == "aws" and r["frame_ts"] == NOW - 60
    assert fr["urls"] == ["http://x/aws/%d" % (NOW - 60)]   # only the newest downloaded
    c = p.component(-10.0, NOW)
    assert c["frame_source"] == "aws" and c["available"] is True and c["age_s"] == 60
    assert c["sources"]["iem"]["newest_age_s"] == 360 and c["sources"]["iem"]["error"] is None
    assert c["attribution"].endswith("Radar: NOAA/NSSL MRMS via NOAA Open Data (AWS)")


def test_bucket_behind_iem_takes_over_and_the_switch_is_logged(monkeypatch):
    p = _poller(monkeypatch)
    p.log = ev = _Events()
    # 01:28:22: the bucket's newest is 01:26, IEM's 01:22
    fr = _stub_sources(monkeypatch, {"aws": (_t(1, 26), "http://x/aws/0126"),
                                     "iem": (_t(1, 22), "http://x/iem/0122")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    assert p.poll_now(now=NOW)["source"] == "aws"
    # then the bucket stalls (2026-11-10: files 25-30 min late) and IEM is on time:
    # at 01:38:22 the bucket still has 01:26, IEM has 01:32
    fr["iem"] = (_t(1, 32), "http://x/iem/0132")
    r = p.poll_now(now=NOW + 600)
    assert r["source"] == "iem" and r["new"] is True and r["frame_ts"] == _t(1, 32)
    assert p.component(-10.0, NOW + 600)["frame_source"] == "iem"
    kinds = ev.kinds()
    assert kinds == ["RADAR-SOURCE"]
    assert "NOAA Open Data (AWS)" in ev.records[0][1]["reason"]
    assert ev.records[0][1]["result"] == "frames from IEM"


def test_a_source_that_fails_leaves_the_other(monkeypatch):
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {
        "aws": rd.SourceError("noaa-mrms-pds.s3.amazonaws.com: no answer (timed out)"),
        "iem": (NOW - 330, "http://x/iem/a")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    assert p.poll_now(now=NOW)["source"] == "iem"
    c = p.component(-10.0, NOW)
    assert c["available"] is True and "no answer" in c["sources"]["aws"]["error"]
    # IEM down, the bucket back
    fr["aws"], fr["iem"] = "new", rd.SourceError("mesonet.agron.iastate.edu: no answer")
    assert p.poll_now(now=NOW + 300)["source"] == "aws"


def test_a_failed_download_falls_back_within_the_same_poll(monkeypatch):
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {"iem": (NOW - 330, "http://x/iem/a"),
                                     "fetch_error": {"aws": rd.FrameError("not MRMS's x")}})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    r = p.poll_now(now=NOW)
    assert r["ok"] and r["source"] == "iem" and fr["urls"] == ["http://x/iem/a"]
    err = p.component(-10.0, NOW)["sources"]["aws"]["error"]
    assert err.startswith("frame %s: not MRMS's" % rd._hhmm(NOW - 60))


def test_the_same_frame_from_both_is_read_once_from_iem(monkeypatch):
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {"aws": (NOW - 600, "http://x/aws/a"),
                                     "iem": (NOW - 600, "http://x/iem/a")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    assert p.poll_now(now=NOW)["source"] == "iem" and fr["urls"] == ["http://x/iem/a"]


def test_no_source_no_frame_retries_after_a_minute(monkeypatch):
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {"aws": rd.SourceError("no answer"), "iem": None})
    r = p.poll_now(now=NOW)
    assert r["ok"] is False and "no answer" in r["error"] and "IEM: no frame" in r["error"]
    c = p.component(-10.0, NOW)
    assert c["available"] is False and c["why"] == r["error"]
    calls = []
    monkeypatch.setattr(p, "poll_now", lambda now: calls.append(now) or {})
    p.maybe_poll(None, now=NOW + config.RADAR_RETRY_SEC - 1)
    assert calls == []
    p.maybe_poll(None, now=NOW + config.RADAR_RETRY_SEC)     # not a full poll interval
    assert calls == [NOW + config.RADAR_RETRY_SEC]
    # a poll with a new frame puts the interval back
    monkeypatch.undo()
    p2 = _poller(monkeypatch)
    _stub_sources(monkeypatch)
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    p2._poll_every = config.RADAR_RETRY_SEC
    p2.poll_now(now=NOW)
    assert p2._poll_every == config.RADAR_POLL_INTERVAL
    assert fr is not None


def test_a_stalled_feed_cannot_confirm_a_one_frame_echo(monkeypatch):
    # The old bug: a feed that stalled offered its last frame to the next poll, which
    # counted it again — a single artefact frame "confirmed" itself (2 of 2) and froze
    # the dome for 15 min. A frame counts once.
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {"aws": (NOW - 60, "http://x/aws/a")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (True, 9.0, 3))
    assert p.poll_now(now=NOW)["streak"] == 1
    r = p.poll_now(now=NOW + 300)                            # the same frame again
    assert r["ok"] is False and fr["fetched"] == 1           # not even downloaded
    c = p.component(-10.0, NOW + 300)
    assert c["safe"] is True and c["unconfirmed_echo"] is True and c["ring_streak"] == 1
    assert p._last_rain_ts is None
    # the next real frame with the echo confirms it
    fr["aws"] = (NOW + 300 - 60, "http://x/aws/b")
    assert p.poll_now(now=NOW + 600)["confirmed"] is True


def test_freshness_is_the_frame_s_own_age_not_the_fetch_time(monkeypatch):
    # a frame fetched NOW but 15 min old has 5 min left, whatever the poll time says
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch, {"aws": (NOW - 900, "http://x/aws/old")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    assert p.poll_now(now=NOW)["ok"] is True
    assert p.component(-10.0, NOW)["age_s"] == 900
    assert p.component(-10.0, NOW + 299)["available"] is True
    assert p.component(-10.0, NOW + 301)["available"] is False


def test_frames_either_side_of_an_outage_are_not_consecutive(monkeypatch):
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch)
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (True, 9.0, 3))
    assert p.poll_now(now=NOW)["streak"] == 1
    fr["aws"] = None                                         # 25 min without frames
    later = NOW + 1500
    fr["aws"] = (later - 60, "http://x/aws/after")
    r = p.poll_now(now=later)
    assert r["streak"] == 1 and r["confirmed"] is False
    assert p.component(-10.0, later)["safe"] is True


def test_clock_step_redraws_without_counting_the_frame_again(monkeypatch):
    p = _poller(monkeypatch)
    fr = _stub_sources(monkeypatch, {"aws": (NOW - 60, "http://x/aws/a")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (True, 9.0, 3))
    p.poll_now(now=NOW)
    p.clock_stepped(NOW, NOW + 40)
    r = p.poll_now(now=NOW + 45)                             # the forced re-poll
    assert r["ok"] is True and r["new"] is False and fr["fetched"] == 2   # redrawn ...
    assert r["streak"] == 1 and r["confirmed"] is False      # ... but not counted again


def test_component_and_page_say_where_the_frame_came_from(monkeypatch):
    from safety.tests.test_smoke import msp      # the page module, its Pi libraries stubbed
    p = _poller(monkeypatch)
    _stub_sources(monkeypatch, {"iem": rd.SourceError("mesonet.agron.iastate.edu: no answer "
                                                      "(timed out)")})
    monkeypatch.setattr(rd, "check_rain", lambda cfg, img: (False, None, 0))
    now = time.time()
    p.poll_now(now=now)
    c = json.loads(json.dumps(p.component(-10.0, now)))      # as the state file has it
    h = msp.build_radar_html({"ts": now, "components": {"radar": c}})
    frame = datetime.fromtimestamp(now - 60, UTC).strftime("%Y-%m-%d %H:%MZ")
    assert "frame %s from NOAA Open Data (AWS)" % frame in h
    assert "Frame sources: NOAA Open Data (AWS) &mdash; newest" in h and "(in use)" in h
    assert 'IEM &mdash; <span style="color:var(--warn)">mesonet.agron.iastate.edu: ' \
           'no answer (timed out)</span>' in h
    # no fresh frame: the page says why instead of guessing
    c.update(available=False, why="NOAA Open Data (AWS): no answer; IEM: no frame in the "
                                  "last 20 min")
    h = msp.build_radar_html({"ts": now, "components": {"radar": c}})
    assert "no fresh frame &mdash; NOAA Open Data (AWS): no answer; IEM: no frame" in h
    # the /setup page row too
    from safety import alpaca
    assert "frame from NOAA Open Data (AWS)" in _setup_row(alpaca, dict(c, available=True))
    assert "no fresh frame — NOAA Open Data (AWS): no answer" in _setup_row(alpaca, c)


def _setup_row(alpaca, rad):
    """The /setup page with this radar component (the way test_radar_basemap does it)."""
    from safety.tests.test_smoke import _safe_state
    st = _safe_state()
    st["components"]["radar"] = dict(rad, safe=True, enabled=True, trigger_km=30)
    return alpaca._setup_html(types.SimpleNamespace(evaluate=lambda: st), config)


# ---- configuration ------------------------------------------------------------------------
def test_radar_sources_setting(monkeypatch):
    warnings = []
    monkeypatch.setattr(config, "CONFIG_WARNINGS", warnings)
    assert config._parse_radar_sources("aws,iem") == ("aws", "iem")
    assert config._parse_radar_sources(" IEM ; aws ") == ("iem", "aws")
    assert config._parse_radar_sources("iem") == ("iem",)
    assert warnings == []
    assert config._parse_radar_sources("aws,mrms") == ("aws",) and "mrms" in warnings[-1]
    assert config._parse_radar_sources("") == ("aws", "iem") and "names no radar" in warnings[-1]


def test_defaults():
    assert config.RADAR_SOURCES == ("aws", "iem")
    assert config.RADAR_AWS_URL == "https://noaa-mrms-pds.s3.amazonaws.com"
    assert config.RADAR_RETRY_SEC == 60 and config.SERVER_CLOCK_TOL_SEC == 60
    assert config.RADAR_STALE_AFTER_SEC == 1200


def test_poller_with_one_source_only_asks_that_one(monkeypatch):
    p = _poller(monkeypatch)
    p._sources = ("iem",)
    asked = []
    monkeypatch.setattr(rd, "newest_frame", lambda src, cfg, net, now: asked.append(src))
    p.poll_now(now=NOW)
    assert asked == ["iem"] and list(p.component(-10.0, NOW)["sources"]) == ["iem"]
