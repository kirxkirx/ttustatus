"""The radar's defences against what a server sends (a ported vlbaweather fix each):

* images (IEM's lcref frame, the bucket's GRIB2 payload, map tiles, the CARTO probe tile,
  the cached basemap) are opened by Pillow's PNG (and, for the map, JPEG) reader only,
  after a magic-byte check, so an EPS reply never reaches Ghostscript; their size and
  mode are checked on the header BEFORE a pixel is decoded;
* the bucket's gzip'ed file is inflated in steps up to FRAME_INFLATED_MAX, never whole;
* every reply body is read against a cap and a deadline for the whole body (frames,
  listings and tiles), so a trickling server cannot hold the radar thread;
* the crop's values are read without Image.getdata, which goes in Pillow 14.

No network: urlopen and the clocks are faked.
"""
import email.utils
import gzip
import io
import struct
import tracemalloc
import types
import urllib.request
import warnings
import zlib
from datetime import datetime, timezone

import pytest

from safety import config, netio
from safety import radar as rd
from safety.tests.test_radar import _cfg
from safety.tests.test_radar_sources import _grib2, _png16

pytest.importorskip("PIL")
from PIL import Image, PngImagePlugin  # noqa: E402

UTC = timezone.utc
TS = datetime(2026, 11, 11, 1, 22, tzinfo=UTC).timestamp()

# An EPS file whose PostScript would print a marker if Ghostscript ever ran it.
EPS = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"
       b"(GS-RAN-ON-RADAR-INPUT) = flush\nshowpage\n%%EOF\n")


@pytest.fixture
def small_grid(monkeypatch):
    """A 400 x 400 lcref-style grid around TTU instead of CONUS's 7000 x 3500."""
    monkeypatch.setattr(rd, "UL_LAT", 35.495)
    monkeypatch.setattr(rd, "UL_LON", -103.495)
    monkeypatch.setattr(rd, "GRID_W", 400)
    monkeypatch.setattr(rd, "GRID_H", 400)
    return types.SimpleNamespace(ni=400, nj=400, la1=35.495, lo1=360 - 103.495)


@pytest.fixture
def opens(monkeypatch):
    """Records every Image.open call (its first bytes and its formats=) and passes it on."""
    seen = []
    real = Image.open

    def spy(fp, *a, **kw):
        head = fp.getvalue()[:8] if isinstance(fp, io.BytesIO) else open(fp, "rb").read(8)
        seen.append((head, kw.get("formats")))
        return real(fp, *a, **kw)
    monkeypatch.setattr(Image, "open", spy)
    return seen


@pytest.fixture
def loads(monkeypatch):
    """Records every PNG decode, and makes it fail: a test that sees one has decoded
    pixels of an image whose header already said it is not what was expected."""
    seen = []

    def load(self):
        seen.append(self.size)
        raise RuntimeError("decoded %dx%d" % self.size)
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", load)
    return seen


def _encoded(fmt, size, mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size).save(buf, format=fmt)
    return buf.getvalue()


def _png_declaring(w, h, mode_byte=0, depth=8):
    """A PNG of a few bytes whose header declares ``w`` x ``h`` pixels (colour type
    ``mode_byte``: 0 grey, 2 RGB, 3 palette): decoding it would take w * h bytes or more."""
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, depth, mode_byte, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if mode_byte == 3:
        out += chunk(b"PLTE", bytes(3 * 256))
    return out + chunk(b"IDAT", zlib.compress(bytes(1000))) + chunk(b"IEND", b"")


# ---- D1: only PNG (and JPEG for the map) reaches Pillow, by magic and by formats= ------
def test_an_eps_frame_never_reaches_pillow(opens):
    # the magic check refuses it: Image.open (and with it Ghostscript) is never called
    with pytest.raises(rd.FrameError, match="not a PNG image"):
        rd.lcref_image(EPS)
    with pytest.raises(rd.ImageRefused, match="not a PNG or JPEG image"):
        rd._decode_tile(EPS)
    with pytest.raises(rd.ImageRefused):
        rd._open_tile(EPS)
    assert opens == []


def test_an_eps_grib2_payload_never_reaches_pillow(small_grid, opens):
    g = small_grid
    with pytest.raises(rd.FrameError, match="unreadable PNG payload .*not a PNG image"):
        rd.aws_image(_grib2(TS, EPS, g.ni, g.nj, g.la1, g.lo1), TS, (0, 0, 10, 10))
    assert opens == []


def test_a_png_is_opened_by_the_png_reader_only(small_grid, opens):
    rd.lcref_image(_encoded("PNG", (400, 400), "P"))
    rd._decode_tile(_encoded("PNG", (256, 256)))
    rd._decode_tile(_encoded("JPEG", (256, 256)))
    g = small_grid
    rd.aws_image(_grib2(TS, _png16({}), g.ni, g.nj, g.la1, g.lo1), TS, (0, 0, 10, 10))
    assert [f for _, f in opens] == [["PNG"], ["PNG", "JPEG"], ["PNG", "JPEG"], ["PNG"]]


def test_a_frame_in_another_format_is_refused(small_grid):
    # a GIF of the right size and mode used to pass as IEM's frame
    gif = _encoded("GIF", (400, 400), "P")
    with pytest.raises(rd.FrameError, match="not a PNG image"):
        rd.lcref_image(gif)
    # a JPEG is a tile format, not a frame format
    with pytest.raises(rd.FrameError, match="not a PNG image"):
        rd.lcref_image(_encoded("JPEG", (400, 400), "L"))


def test_tiles_in_another_format_are_refused():
    for fmt in ("GIF", "BMP", "TIFF"):
        with pytest.raises(rd.ImageRefused, match="not a PNG or JPEG image"):
            rd._decode_tile(_encoded(fmt, (256, 256)))


def test_a_pillow_without_formats_is_refused_not_trusted(monkeypatch):
    def old_open(fp, mode="r"):         # the signature of a Pillow before formats=
        raise AssertionError("opened without formats=")
    monkeypatch.setattr(Image, "open", old_open)
    with pytest.raises(rd.FrameError, match="cannot restrict the image format"):
        rd.lcref_image(_encoded("PNG", (8, 8), "P"))
    with pytest.raises(rd.ImageRefused, match="cannot restrict the image format"):
        rd._decode_tile(_encoded("PNG", (256, 256)))


# ---- D1: size and mode are checked on the header, before a pixel is decoded ------------
def test_lcref_size_and_mode_are_checked_before_decoding(loads):
    # 8000 x 3500 grey: under Pillow's bomb limit, 28 MB to decode, not lcref's grid
    with pytest.raises(rd.FrameError, match="PNG of 8000x3500 in mode L"):
        rd.lcref_image(_png_declaring(8000, 3500))
    # lcref's size, but RGB: 74 MB to decode, and no palette indices
    with pytest.raises(rd.FrameError, match="PNG of 7000x3500 in mode RGB"):
        rd.lcref_image(_png_declaring(7000, 3500, mode_byte=2))
    assert loads == []


def test_a_decompression_bomb_header_is_an_error_not_a_warning(loads):
    # 136 kB that declares 12000 x 12000 px used to cost 144 MB (and a journal warning)
    with warnings.catch_warnings():
        warnings.simplefilter("error")          # any warning that escapes fails the test
        with pytest.raises(rd.FrameError, match="unreadable PNG"):
            rd.lcref_image(_png_declaring(12000, 12000))
        with pytest.raises(rd.ImageRefused):
            rd._decode_tile(_png_declaring(12000, 12000, mode_byte=2))
    assert loads == []


def test_the_grib2_payload_size_and_mode_are_checked_before_decoding(small_grid, loads):
    g = small_grid
    for payload, why in ((_png_declaring(4000, 4000, depth=16), "PNG payload of 4000x4000"),
                         (_png_declaring(400, 400, mode_byte=2), "in mode RGB")):
        with pytest.raises(rd.FrameError, match=why):
            rd.aws_image(_grib2(TS, payload, g.ni, g.nj, g.la1, g.lo1), TS, (0, 0, 10, 10))
    assert loads == []


def test_a_tile_of_another_size_is_refused_before_decoding(loads):
    for size in ((4096, 4096), (256, 512), (300, 300)):
        with pytest.raises(rd.ImageRefused, match="tile of %dx%d px" % size):
            rd._decode_tile(_png_declaring(*size, mode_byte=2))
        with pytest.raises(rd.ImageRefused, match="tile of"):
            rd._open_tile(_png_declaring(*size, mode_byte=2)).verify()
    assert loads == []


def test_a_2x_tile_is_drawn_at_the_size_of_the_tiles_requested():
    tile = rd._decode_tile(_encoded("PNG", (512, 512), "P"))
    assert (tile.mode, tile.size) == ("RGB", (256, 256))


def test_a_truncated_frame_is_a_frame_error(small_grid):
    # opened (its header is whole), refused when decoded: still the frame's own error
    buf = io.BytesIO()
    Image.effect_noise((400, 400), 64).convert("P").save(buf, format="PNG")
    with pytest.raises(rd.FrameError, match="unreadable PNG .*does not decode"):
        rd.lcref_image(buf.getvalue()[:len(buf.getvalue()) // 2])


# ---- D1: the map's tiles, the CARTO probe tile and the cached composite ----------------
def _thumb(tmp_path):
    cfg = _cfg(tmp_path)
    t = rd.Thumbnailer(cfg, config.OSM_TILE_URL, str(tmp_path / "n.png"), "light")
    t._geometry()
    return t


def test_an_eps_or_oversized_tile_is_never_drawn(tmp_path, opens, loads):
    t = _thumb(tmp_path)
    replies = [EPS, _png_declaring(4096, 4096, mode_byte=2)]
    _, n, expected, _ = t._build_tiles(lambda tx, ty: replies[(tx + ty) % 2], "osm", False)
    assert n == 0 and expected == len(t._tiles)
    assert all(head.startswith(b"\x89PNG") and f == ["PNG", "JPEG"] for head, f in opens)
    assert loads == []


def test_good_tiles_still_build_the_map(tmp_path):
    t = _thumb(tmp_path)
    png, jpeg = _encoded("PNG", (256, 256)), _encoded("JPEG", (256, 256))
    _, n, expected, _ = t._build_tiles(lambda tx, ty: (png, jpeg)[tx % 2], "osm", False)
    assert n == expected > 0


def test_an_eps_carto_probe_tile_is_not_an_image(tmp_path, monkeypatch, opens):
    # verify() ran no Ghostscript, but let the EPS through as the probe tile, which
    # _build_tiles then decoded (and Ghostscript ran there)
    monkeypatch.setattr(rd, "_get", lambda url, **kw: EPS)
    t = _thumb(tmp_path)
    verdict, keyed, _, reason = t._carto_tile("https://a.basemaps.cartocdn.com/x.png", "k")
    assert (verdict, keyed, reason) == ("unreachable", None, "not an image")
    assert opens == []


def test_the_cached_composite_is_read_as_png_or_jpeg_of_the_map_s_size(tmp_path, opens):
    t = _thumb(tmp_path)
    path = tmp_path / "basemap.png"
    path.write_bytes(EPS)
    assert t._load_cache(str(path)) is None and opens == []
    path.write_bytes(_png_declaring(4 * t._ox, 4 * t._oy, mode_byte=2))
    assert t._load_cache(str(path)) is None
    Image.new("RGB", (t._ox, t._oy), (1, 2, 3)).save(str(path), format="PNG")
    img = t._load_cache(str(path))
    assert img.size == (t._ox, t._oy) and img.getpixel((0, 0)) == (1, 2, 3)


# ---- D2: the bucket's file is inflated in steps, up to FRAME_INFLATED_MAX --------------
def _gzip_of_zeros(n):
    """A gzip file of ``n`` zero bytes, built without holding them (~1000:1)."""
    z = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    block = bytes(1 << 20)
    parts = [z.compress(block) for _ in range(n >> 20)]
    return b"".join(parts) + z.flush()


def test_a_gzip_bomb_is_refused_without_inflating_it(small_grid):
    bomb = _gzip_of_zeros(rd.FRAME_INFLATED_MAX + (32 << 20))     # 64 MB from ~64 kB
    assert len(bomb) < 200 << 10
    tracemalloc.start()
    try:
        with pytest.raises(rd.FrameError, match="inflates to more than"):
            rd.aws_image(bomb, TS, (0, 0, 10, 10))
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < rd.FRAME_INFLATED_MAX + (8 << 20)         # never the whole 64 MB


def test_a_gzip_file_with_data_behind_it_is_refused(small_grid):
    g = small_grid
    msg = _grib2(TS, _png16({}), g.ni, g.nj, g.la1, g.lo1)
    assert rd.aws_image(gzip.compress(msg), TS, (0, 0, 10, 10)).size == (400, 400)
    for data in (gzip.compress(msg) + gzip.compress(b"more"), gzip.compress(msg)[:-12]):
        with pytest.raises(rd.FrameError, match="unreadable GRIB2"):
            rd.aws_image(data, TS, (0, 0, 10, 10))


def test_the_inflated_limit_leaves_room_for_a_real_file():
    # the bucket's SeamlessHSR file of 2026-11-10 inflated to 1 003 393 bytes
    assert rd.FRAME_INFLATED_MAX >= 16 * 1003393


# ---- D3: a reply's body is read against a cap and a deadline -----------------------------
class _Trickle:
    """A reply whose body arrives ``rate`` seconds of the fake clock per byte (0: all at
    once): ``read1`` gives what has arrived, ``read(n)`` waits for all n bytes, as
    http.client's do."""

    def __init__(self, clock, body, headers=None, rate=1):
        self.clock, self.body, self.status, self.rate = clock, body, 200, rate
        self.headers = dict(headers or {})
        self.headers.setdefault("Date", email.utils.formatdate(usegmt=True))
        self.reads = 0

    def read1(self, n=-1):
        self.reads += 1
        k = 1 if self.rate else (len(self.body) if n is None or n < 0 else n)
        piece, self.body = self.body[:k], self.body[k:]
        self.clock[0] += len(piece) * self.rate
        return piece

    def read(self, n=-1):
        self.reads += 1
        n = len(self.body) if n is None or n < 0 else n
        piece, self.body = self.body[:n], self.body[n:]
        self.clock[0] += len(piece) * self.rate
        return piece

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def clock(monkeypatch):
    c = [1000.0]
    monkeypatch.setattr(netio, "_clock", lambda: c[0])
    return c


def test_a_trickling_frame_is_cut_off_at_its_timeout(clock, monkeypatch):
    reply = _Trickle(clock, bytes(600))     # 600 s to arrive in full
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: reply)
    with pytest.raises(netio.ReplyTooSlow):
        rd._http("https://noaa-mrms-pds.s3.amazonaws.com/x.grib2.gz", timeout=30,
                 max_bytes=rd.FRAME_MAX_BYTES)
    assert clock[0] - 1000.0 <= 31
    # through the poll: the host answers too slowly to be asked again this poll
    reply2 = _Trickle(clock, bytes(600))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: reply2)
    net = rd._PollNet(config)
    with pytest.raises(rd.SourceError, match="no answer .*not complete after"):
        net.request("aws", "https://noaa-mrms-pds.s3.amazonaws.com/x.grib2.gz",
                    timeout=rd.FRAME_TIMEOUT, max_bytes=rd.FRAME_MAX_BYTES)
    assert "noaa-mrms-pds.s3.amazonaws.com" in net.down


def test_a_declared_oversized_frame_is_not_read(clock, monkeypatch):
    reply = _Trickle(clock, b"x", {"Content-Length": str(rd.FRAME_MAX_BYTES + 1)})
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: reply)
    net = rd._PollNet(config)
    with pytest.raises(rd.FrameError, match="larger than 32 MB"):
        net.request("iem", "https://mesonet.agron.iastate.edu/x.png",
                    timeout=rd.FRAME_TIMEOUT, max_bytes=rd.FRAME_MAX_BYTES)
    assert reply.reads == 0 and net.down == {}      # an answer: the host is asked on


def test_a_listing_beyond_its_cap_is_a_frame_error(clock, monkeypatch):
    # no Content-Length: the cap is found while reading, one chunk past it at most
    reply = _Trickle(clock, bytes(4 * rd.LISTING_MAX_BYTES), rate=0)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: reply)
    with pytest.raises(rd.FrameError, match="larger than 1 MB"):
        rd._PollNet(config).request("aws", "https://example.org/?list-type=2",
                                    max_bytes=rd.LISTING_MAX_BYTES)
    assert len(reply.body) >= 3 * rd.LISTING_MAX_BYTES - netio.READ_CHUNK


def test_a_trickling_tile_is_cut_off_at_its_timeout(clock, monkeypatch):
    reply = _Trickle(clock, bytes(300))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: reply)
    with pytest.raises(netio.ReplyTooSlow):
        rd._get("https://tile.openstreetmap.org/8/1/1.png", timeout=rd.BASEMAP_TILE_TIMEOUT)
    assert clock[0] - 1000.0 <= rd.BASEMAP_TILE_TIMEOUT + 1


def test_a_tile_beyond_its_cap_is_refused(clock, monkeypatch):
    reply = _Trickle(clock, bytes(4 * rd.TILE_MAX_BYTES), rate=0)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: reply)
    with pytest.raises(netio.ReplyTooLarge):
        rd._get("https://tile.openstreetmap.org/8/1/1.png")
    declared = _Trickle(clock, b"x", {"Content-Length": str(50 << 20)})
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: declared)
    with pytest.raises(netio.ReplyTooLarge):
        rd._get("https://tile.openstreetmap.org/8/1/1.png")
    assert declared.reads == 0


def test_a_whole_tile_still_comes_through(clock, monkeypatch):
    png = _encoded("PNG", (256, 256))
    reply = _Trickle(clock, png, rate=0)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: reply)
    assert rd._get("https://tile.openstreetmap.org/8/1/1.png") == png


# ---- D4: the crop's values without Image.getdata (gone in Pillow 14) ---------------------
def test_pixels_uses_get_flattened_data_where_pillow_has_it():
    class New:                          # Pillow 12.1 on
        def get_flattened_data(self):
            return (1, 2, 3)

        def getdata(self):
            raise AssertionError("getdata is deprecated here")

    class Old:                          # the apt Pillow of Bookworm (9.4) and Trixie (11.x)
        def getdata(self):
            return [1, 2, 3]
    assert list(rd._pixels(New())) == list(rd._pixels(Old())) == [1, 2, 3]


def test_pixels_of_a_real_image_are_its_values_row_by_row():
    im = Image.new("I;16", (3, 2), 0)
    for i, (c, r) in enumerate((c, r) for r in range(2) for c in range(3)):
        im.putpixel((c, r), 9000 + i)
    assert list(rd._pixels(im)) == [9000, 9001, 9002, 9003, 9004, 9005]


def test_the_frame_conversion_uses_no_deprecated_pillow_call(small_grid):
    g = small_grid
    msg = _grib2(TS, _png16({(5, 5): 10310}), g.ni, g.nj, g.la1, g.lo1)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        img = rd.aws_image(gzip.compress(msg), TS, (0, 0, 10, 10))
    assert img.getpixel((5, 5)) == rd.lcref_index({"R": -9990.0, "E": 0, "D": 1})(10310)
