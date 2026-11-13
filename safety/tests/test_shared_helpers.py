"""The shared helpers the layers build on: safety/isotime.py (ISO 8601 read alike on every
Python), safety/textclean.py (a feed's text made safe to write and show) and safety/netio.py
(a download read within a size cap and a deadline, a gzip'ed file inflated within a cap).
No network: the replies are in-memory objects, an http.client.HTTPResponse over a fake
socket, and urllib's reply for a local file."""
import email.message
import gzip
import http.client
import io
import logging
import urllib.request
import urllib.response
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from safety import isotime, netio
from safety.isotime import iso_ts, parse_iso
from safety.netio import ReplyCutShort, ReplyTooLarge, ReplyTooSlow, gunzip_capped, read_capped
from safety.textclean import clean_text

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _fresh_warned(monkeypatch):
    """Each test starts with no unreadable text logged yet."""
    monkeypatch.setattr(isotime, "_iso_warned", set())


# ---- isotime.parse_iso (vlbaweather's #160 cases) --------------------------------------------
@pytest.mark.parametrize("text,expect", [
    ("2026-11-12T10:53:00Z", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T10:53:00+00:00", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T03:53:00-07:00", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T10:53:00.123Z", datetime(2026, 11, 12, 10, 53, 0, 123000, tzinfo=UTC)),
    ("2026-11-12T10:53:00", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),      # no offset: UTC
    ("2026-11-12", datetime(2026, 11, 12, tzinfo=UTC)),                         # a date: its midnight UTC
    ("2026-11-12T10:53:00.5Z", datetime(2026, 11, 12, 10, 53, 0, 500000, tzinfo=UTC)),
    ("2026-11-12T10:53:00.123456789Z", datetime(2026, 11, 12, 10, 53, 0, 123456, tzinfo=UTC)),
    ("2026-11-12T10:53:00+0000", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T03:53:00-0700", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T03:53:00-07", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12t10:53:00z", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    (" 2026-11-12T10:53:00Z\n", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12 10:53:00Z", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),     # a blank for the T
    ("2026-11-12T10:53Z", datetime(2026, 11, 12, 10, 53, tzinfo=UTC)),
    ("2026-11-12T10:53:00,25+0530", datetime(2026, 11, 12, 5, 23, 0, 250000, tzinfo=UTC)),
    ("2026-11-12T25:00:00Z", None), ("garbage", None), ("", None), ("   ", None),
    (None, None), (12345, None), (b"2026-11-12T10:53:00Z", None),
    ("2026-11-12T10:53:00+2500", None), ("2026-02-30", None), ("0000-01-01", None)])
def test_parse_iso(text, expect):
    got = parse_iso(text)
    assert got == expect
    assert got is None or got.tzinfo is not None


def test_parse_iso_keeps_the_offset_it_reads():
    """Aware, in the text's own offset: comparisons and timestamps are right whatever it is."""
    got = parse_iso("2026-11-12T03:53:00-07:00")
    assert got.utcoffset() == timedelta(hours=-7)
    assert parse_iso("2026-11-12T10:53:00").utcoffset() == timedelta(0)


def test_parse_iso_reads_no_fraction_of_a_minute():
    """"10:53.5" is half a minute past 10:53 in ISO 8601, not 10:53:00.5: refused."""
    assert parse_iso("2026-11-12 10:53.5") is None
    assert parse_iso("2026-11-12T10:53,5Z") is None


@pytest.mark.parametrize("text", [
    "2026-11-12T10:53.500Z",                # a fraction of a minute (3.9's fromisoformat: 10:53:00.5)
    "2026-11-12T10.5Z",                     # a fraction of an hour (likewise: 10:00:00.5)
    "20261112T105300Z", "2026-W46-4", "2026-316",      # basic format, week and ordinal dates (3.11+)
    "2026-11-12+00:00",                     # an offset on a date (3.9: its midnight, "+" taken as the "T")
    "2026-11-12x10:53:00Z",                 # another separator
    "2026-11-12T10:53:00+05:30:15",         # an offset with seconds
    "2026-11-12T10:53:00+05:75",            # an offset of 75 minutes
    "2026-11-12T24:00:00Z",                 # ISO 8601's end of the day: not read
    "2026-11-12T10:53:00.1234567890123Z",   # a fraction of 13 digits
    "2026-11-12T10:53:00Z trailing",        # anything after the time
    "２０２６-11-12",       # full-width digits
    "٢٠٢٦-11-12"])      # Arabic-Indic digits
def test_parse_iso_reads_the_same_on_every_python(text):
    """What datetime.fromisoformat reads besides the forms parse_iso lists, on one Python
    version or all, is no time here on any: the CI runs 3.11, the Pi 3.9 or later, and an
    alert's end or a latch's start must be the same instant on each."""
    assert parse_iso(text) is None


def test_parse_iso_reads_a_time_to_the_hour():
    """Read by fromisoformat on every Python, and kept: ISO 8601's reduced precision."""
    assert parse_iso("2026-11-12T10") == datetime(2026, 11, 12, 10, tzinfo=UTC)
    assert parse_iso("2026-11-12T03-07") == datetime(2026, 11, 12, 10, tzinfo=UTC)


def test_parse_iso_never_calls_fromisoformat(monkeypatch):
    """The datetime is built from the match, so the Python's own reading cannot leak in."""
    class NoFromIso(datetime):
        @classmethod
        def fromisoformat(cls, s):
            raise AssertionError("fromisoformat called")
    monkeypatch.setattr(isotime, "datetime", NoFromIso)
    assert parse_iso("2026-11-12T10:53:00.5Z") == datetime(2026, 11, 12, 10, 53, 0, 500000, tzinfo=UTC)


def _iso_lines(caplog):
    return sum("not an ISO 8601 time" in r.getMessage() for r in caplog.records)


def test_parse_iso_logs_an_unreadable_text_once(caplog):
    caplog.set_level(logging.WARNING, logger="ttu.safety.isotime")
    for _ in range(3):
        assert parse_iso("not a time at all") is None
    assert _iso_lines(caplog) == 1
    parse_iso("another odd text")
    assert _iso_lines(caplog) == 2
    for absent in (None, "", "  ", 5):          # no text: nothing to warn of
        parse_iso(absent)
    assert _iso_lines(caplog) == 2


def test_parse_iso_logs_nothing_without_warn(caplog):
    caplog.set_level(logging.WARNING, logger="ttu.safety.isotime")
    assert parse_iso("quiet odd text", warn=False) is None
    assert iso_ts("quiet odd text", warn=False) is None
    assert _iso_lines(caplog) == 0
    assert "quiet odd text" not in isotime._iso_warned


def test_parse_iso_keeps_its_warned_set_bounded(caplog):
    """A feed that writes a new odd text every poll cannot grow the set without limit."""
    caplog.set_level(logging.WARNING, logger="ttu.safety.isotime")
    for i in range(isotime._WARNED_MAX + 50):
        parse_iso("odd %d" % i)
    assert len(isotime._iso_warned) == isotime._WARNED_MAX
    assert _iso_lines(caplog) == isotime._WARNED_MAX


def test_iso_ts():
    assert iso_ts("2026-11-12T10:53:00Z") == datetime(2026, 11, 12, 10, 53, tzinfo=UTC).timestamp()
    assert iso_ts("1970-01-01") == 0.0
    assert iso_ts("1970-01-01T00:00:00.25-00:30") == 1800.25
    assert isinstance(iso_ts("2026-11-12T10"), float)
    assert iso_ts("2026-11-12T10:53.5") is None and iso_ts(None) is None and iso_ts("") is None
    # the ends of datetime's range, pushed past them by the offset: still a number, no raise
    assert iso_ts("9999-12-31T23:59:59-23:59") == 253402387139.0
    assert iso_ts("0001-01-01T00:00:00+23:59") == -62135683140.0


# ---- textclean.clean_text ---------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, 5, 1.5, b"bytes", ["x"], {"a": "b"}, True])
def test_clean_text_of_a_non_str_is_empty(value):
    assert clean_text(value) == ""


def test_clean_text_replaces_a_lone_surrogate():
    """JSON's "\\ud800" escape decodes to a lone surrogate, which UTF-8 cannot encode: the page
    write raised and /setup answered 500. Replaced, the text encodes."""
    for raw, want in (("a\ud800b", "a�b"), ("\udc00", "�"), ("x\udfff", "x�"),
                      ("\ude00\ud83d", "��"),            # low then high: no pair
                      ("\ud83d😀", "�\U0001F600")):
        got = clean_text(raw)
        assert got == want
        got.encode("utf-8")
    import json
    got = clean_text(json.loads('"Tornado \\ud83c Warning"'))
    assert got == "Tornado � Warning"
    got.encode("utf-8")


def test_clean_text_joins_a_true_surrogate_pair():
    assert clean_text("x😀y") == "x\U0001F600y"


def test_clean_text_drops_controls_and_keeps_tab_and_newline():
    assert clean_text("a\x00b\x07c\x1b[31md\x7fe\x85f\x9fg") == "abc[31mdefg"
    assert clean_text("col1\tcol2\nline2") == "col1\tcol2\nline2"
    assert clean_text("\x0b\x0c") == ""


def test_clean_text_turns_a_carriage_return_into_a_newline():
    assert clean_text("one\r\ntwo\rthree\n\rfour") == "one\ntwo\nthree\n\nfour"


@pytest.mark.parametrize("ch", ["؜", "‎", "‏", "‪", "‫", "‬",
                                "‭", "‮", "⁦", "⁧", "⁨", "⁩"])
def test_clean_text_drops_the_bidirectional_controls(ch):
    assert clean_text("Ext%sreme" % ch) == "Extreme"


def test_clean_text_keeps_everything_else():
    text = ("Tornado Warning ⚠️ \U0001F32A️ — café, مرحبا, "
            "中文, ‍​ � \U0001F468‍\U0001F469‍\U0001F467")
    assert clean_text(text) == text
    assert clean_text("") == ""
    assert clean_text("plain ASCII alert text, 12:00 MDT.") == "plain ASCII alert text, 12:00 MDT."


def test_clean_text_is_cheap_for_an_alert():
    """Linear: a long alert of 200 kB (with a surrogate) is cleaned at once."""
    text = ("Strong winds ‮ and \x1b blowing dust. " * 6000) + "\ud800"
    got = clean_text(text)
    assert got.endswith("�") and "‮" not in got and "\x1b" not in got


# ---- netio.read_capped ------------------------------------------------------------------------
class Trickle:
    """A reply that sends ``per_read`` bytes per read and lets ``step`` seconds pass on the
    fake clock each time: a server that keeps the connection busy without finishing."""

    def __init__(self, data, per_read, clock, step, headers=None, has_read1=True):
        self.buf = io.BytesIO(data)
        self.per_read, self.clock, self.step = per_read, clock, step
        self.headers = headers if headers is not None else {}
        self.asked = []
        if not has_read1:
            self.read1 = None

    def read1(self, n):
        return self._read(n)

    def read(self, n=-1):
        return self._read(n)

    def _read(self, n):
        self.asked.append(n)
        self.clock[0] += self.step
        return self.buf.read(min(n, self.per_read))


@pytest.fixture
def clock(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(netio, "_clock", lambda: t[0])
    return t


def test_read_capped_gives_up_on_a_trickling_reply(clock):
    """1 byte a second: the socket timeout never fires, the deadline does."""
    r = Trickle(b"x" * 1000, per_read=1, clock=clock, step=1.0)
    with pytest.raises(ReplyTooSlow, match=r"radar frame: reply not complete after 30 s"):
        read_capped(r, 10000, 30, what="radar frame")
    assert len(r.asked) == 31                   # stopped at the chunk past the deadline


def test_read_capped_too_slow_is_an_oserror(clock):
    """A TimeoutError, so the layers' ``except OSError`` already takes it as a failed fetch."""
    r = Trickle(b"x" * 100, per_read=10, clock=clock, step=5.0)
    with pytest.raises(OSError):
        read_capped(r, 1000, 12)


def test_read_capped_reads_a_reply_within_the_deadline(clock):
    data = bytes(range(256)) * 1000
    r = Trickle(data, per_read=50000, clock=clock, step=0.1)
    assert read_capped(r, len(data), 10) == data
    assert all(n <= netio.READ_CHUNK for n in r.asked)


def test_read_capped_refuses_a_too_large_content_length_before_reading(clock):
    r = Trickle(b"x" * 10, per_read=10, clock=clock, step=0.0, headers={"Content-Length": "5000001"})
    with pytest.raises(ReplyTooLarge, match="nws: reply of 5000001 bytes is over the limit of 5000000"):
        read_capped(r, 5000000, 30, what="nws")
    assert r.asked == []


def test_read_capped_refuses_a_too_large_body_without_content_length(clock):
    r = Trickle(b"y" * 300000, per_read=10 ** 9, clock=clock, step=0.0)
    with pytest.raises(ReplyTooLarge, match="over the limit of 100000 bytes"):
        read_capped(r, 100000, 30)
    assert sum(r.asked) <= 100001 + netio.READ_CHUNK
    assert r.asked[-1] <= 100001                # never asks far past the cap


def test_read_capped_refuses_a_body_longer_than_its_content_length_says(clock):
    """A lying Content-Length is no licence: the bytes are counted too."""
    r = Trickle(b"z" * 2000, per_read=10 ** 9, clock=clock, step=0.0, headers={"Content-Length": "10"})
    with pytest.raises(ReplyTooLarge):
        read_capped(r, 1000, 30)


def test_read_capped_accepts_exactly_max_bytes(clock):
    data = b"q" * 200000
    assert read_capped(Trickle(data, 70000, clock, 0.0), 200000, 30) == data
    hdr = {"Content-Length": "200000"}
    assert read_capped(Trickle(data, 70000, clock, 0.0, headers=hdr), 200000, 30) == data
    with pytest.raises(ReplyTooLarge):
        read_capped(Trickle(data + b"!", 70000, clock, 0.0), 200000, 30)
    assert read_capped(Trickle(b"", 10, clock, 0.0), 0, 30) == b""
    with pytest.raises(ReplyTooLarge):
        read_capped(Trickle(b"a", 10, clock, 0.0), 0, 30)


@pytest.mark.parametrize("value", ["abc", "-5", "", " 12 ", None])
def test_read_capped_ignores_an_unreadable_content_length(clock, value):
    r = Trickle(b"ok", 10, clock, 0.0, headers={"Content-Length": value})
    assert read_capped(r, 100, 30) == b"ok"


def test_read_capped_uses_read_without_read1(clock):
    r = Trickle(b"abc" * 100, per_read=7, clock=clock, step=0.0, has_read1=False)
    assert read_capped(r, 1000, 30) == b"abc" * 100


def test_read_capped_reads_a_plain_file_object_and_bytesio():
    assert read_capped(io.BytesIO(b"hello"), 5, 1) == b"hello"
    with pytest.raises(ReplyTooLarge):
        read_capped(io.BytesIO(b"hello!"), 5, 1)


class _Sock:
    """What http.client.HTTPResponse needs of a socket: a makefile over the raw reply."""

    def __init__(self, raw):
        self.raw = raw

    def makefile(self, mode, *a, **k):
        return io.BytesIO(self.raw)


def _http_response(raw):
    r = http.client.HTTPResponse(_Sock(raw))
    r.begin()
    return r


def test_read_capped_reads_an_http_response():
    body = b'{"features": []}' * 5000
    r = _http_response(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
    assert read_capped(r, len(body), 30, what="alerts") == body


def test_read_capped_reads_a_chunked_http_response():
    raw = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
           b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
    assert read_capped(_http_response(raw), 11, 30) == b"hello world"
    with pytest.raises(ReplyTooLarge):
        read_capped(_http_response(raw), 10, 30)


def test_read_capped_refuses_an_http_response_by_its_content_length():
    r = _http_response(b"HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\nabc")
    with pytest.raises(ReplyTooLarge, match="99999999 bytes"):
        read_capped(r, 1000, 30)


def test_read_capped_reports_an_http_response_cut_short():
    """read1 ends quietly when the connection closes before the declared length: a truncated
    body must not pass for a whole one."""
    r = _http_response(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"a" * 60)
    with pytest.raises(ReplyCutShort, match="60 bytes read, 40 more expected") as ei:
        read_capped(r, 1000, 30)
    assert isinstance(ei.value, ValueError) and isinstance(ei.value, http.client.HTTPException)
    assert ei.value.partial == b"a" * 60


def test_read_capped_reads_urllibs_addinfourl(tmp_path):
    hdr = email.message.Message()
    hdr["Content-Length"] = "4"
    r = urllib.response.addinfourl(io.BytesIO(b"data"), hdr, "file:///x", 200)
    assert read_capped(r, 4, 30) == b"data"
    hdr.replace_header("Content-Length", "400")
    with pytest.raises(ReplyTooLarge):
        read_capped(urllib.response.addinfourl(io.BytesIO(b"data"), hdr, "file:///x", 200), 4, 30)
    f = tmp_path / "local.bin"
    f.write_bytes(b"\x00\x01" * 5000)
    with urllib.request.urlopen(f.as_uri()) as r:      # a local file: no network
        assert read_capped(r, 10000, 30) == b"\x00\x01" * 5000
    with urllib.request.urlopen(f.as_uri()) as r:
        with pytest.raises(ReplyTooLarge):
            read_capped(r, 9999, 30)


def test_read_capped_too_large_is_a_valueerror():
    with pytest.raises(ValueError):
        read_capped(io.BytesIO(b"xx"), 1, 30)


# ---- netio.gunzip_capped (vlbaweather's test_gzip_* cases) --------------------------------------
STEP = 64 * 1024
CAP = 4 * STEP


def test_gzip_is_inflated_in_steps():
    """``step`` at a time: a file of several steps comes out whole, one of exactly the cap
    passes and one byte more does not, and a stream that stops at a step's end without its
    trailer is cut short."""
    raw = bytes(range(256)) * (STEP * 5 // 512 + 1)       # two and a half steps
    assert gunzip_capped(gzip.compress(raw), CAP, STEP) == raw
    full = b"\0" * CAP
    assert len(gunzip_capped(gzip.compress(full), CAP, STEP)) == CAP
    with pytest.raises(ValueError, match="inflates to more than %d bytes" % CAP):
        gunzip_capped(gzip.compress(full + b"\0"), CAP, STEP)
    z = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    open_ended = z.compress(b"\1" * (2 * STEP)) + z.flush(zlib.Z_SYNC_FLUSH)
    with pytest.raises(ValueError, match="cut short"):
        gunzip_capped(open_ended, CAP, STEP)


def test_gzip_is_read_whole_or_not_at_all():
    good = gzip.compress(b"GRIB" + bytes(range(256)) * 100 + b"7777")
    assert gunzip_capped(good + b"\0\0\0", CAP, STEP).startswith(b"GRIB")      # zero padding
    with pytest.raises(ValueError, match="cut short"):
        gunzip_capped(good[:-12], CAP, STEP)
    with pytest.raises(ValueError, match="after the gzip stream"):
        gunzip_capped(good + good, CAP, STEP)                   # a second member
    with pytest.raises(ValueError, match="after the gzip stream"):
        gunzip_capped(good + b"\0\0junk", CAP, STEP)


def test_gzip_bomb_is_refused_holding_no_more_than_the_cap_and_a_step(monkeypatch):
    """A few kB that inflate to 64 MB: refused, and no more than the cap plus one step is
    ever inflated (each decompress call is bounded by ``step``)."""
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))
    assert len(bomb) < 100000
    asked, out = [], [0]
    real = zlib.decompressobj

    class Spy:
        def __init__(self, *a):
            self.z = real(*a)

        def decompress(self, data, n=0):
            asked.append(n)
            piece = self.z.decompress(data, n)
            out[0] += len(piece)
            return piece

        def __getattr__(self, name):
            return getattr(self.z, name)

    monkeypatch.setattr(netio.zlib, "decompressobj", Spy)
    with pytest.raises(ValueError, match="inflates to more than"):
        gunzip_capped(bomb, CAP, STEP)
    assert asked and all(n == STEP for n in asked)
    assert out[0] <= CAP + STEP


@pytest.mark.parametrize("data", [b"", b"\x1f\x8b", b"not gzip at all", b"\x1f\x8b\x08\x00garbage!"])
def test_gzip_unreadable_is_a_valueerror(data):
    with pytest.raises(ValueError):
        gunzip_capped(data, CAP, STEP)


def test_gzip_default_step_and_a_step_of_zero():
    raw = b"abc" * 1000000
    assert gunzip_capped(gzip.compress(raw), len(raw)) == raw
    assert gunzip_capped(gzip.compress(b"abc"), 3, 0) == b"abc"       # 0 is not "no limit"
    assert netio.INFLATE_STEP == 1024 * 1024
