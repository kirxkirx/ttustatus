"""Hardening of the hazard information feeds and of the status page: the KML DOCTYPE
refusal in any encoding, the capped and deadline-bound reads, the text scrub, the
version-independent ISO parsing, writes that survive any text, and the page dating
itself in the viewer's browser."""
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from safety import hazard_feeds as hf
from safety import netio

# The page imports the Raspberry Pi hardware / heavy libraries at module load; stub them
# exactly as test_smoke.py does (setdefault: a real installed module is left alone).
for _m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
           "astropy.time", "astropy.coordinates", "astropy.utils", "astropy.utils.iers"]:
    sys.modules.setdefault(_m, MagicMock())

import make_status_page as msp        # noqa: E402  (must follow the sys.modules stubbing)

NODE = shutil.which("node")

SITE = (33.748, -101.958)
BOX = (SITE[1] - 1, SITE[0] - 1, SITE[1] + 1, SITE[0] + 1)


def _ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def _kml_text(density="Heavy"):
    ring = "-102.5,33.2,0 -101.4,33.2,0 -101.4,34.3,0 -102.5,34.3,0 -102.5,33.2,0"
    return ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><Placemark>'
            '<description>Start Time: 2026265 1300 UTC End Time: 2026265 1500 UTC '
            'Density: %s</description><Polygon><outerBoundaryIs><LinearRing>'
            '<coordinates>%s</coordinates></LinearRing></outerBoundaryIs></Polygon>'
            '</Placemark></Folder></Document></kml>' % (density, ring))


# ---- E1a: DOCTYPE/ENTITY refused whatever the encoding ------------------------------
def test_utf16_kml_with_entity_is_refused():
    # the byte search sees "<\0!\0D\0..." in UTF-16 and let this through; ElementTree
    # then expanded the entity (the density came out of it)
    doc = ('<?xml version="1.0" encoding="UTF-16"?>'
           '<!DOCTYPE kml [<!ENTITY d "Heavy">]>' + _kml_text("&d;"))
    body = doc.encode("utf-16")
    assert b"<!DOCTYPE" not in body
    with pytest.raises(ValueError, match="DOCTYPE"):
        hf.parse_hms_kml(body, SITE, BOX)


def test_utf16_kml_without_dtd_still_parses():
    body = ('<?xml version="1.0" encoding="UTF-16"?>' + _kml_text()).encode("utf-16")
    doc = hf.parse_hms_kml(body, SITE, BOX, "2026-09-22")
    assert doc["total"] == 1 and doc["polys"][0]["density"] == "Heavy"


def test_refuse_dtd_maps_garbage_to_not_xml():
    with pytest.raises(ValueError, match="not XML"):
        hf._refuse_dtd(b"<kml><unclosed></kml>")
    hf._refuse_dtd(_kml_text().encode())          # a plain document passes


# ---- E1b: stepwise gunzip and a deadline for the whole body -------------------------
class _Trickle:
    """A reply that hands out one byte per read and advances the deadline's clock by a
    second each time: a server trickling a byte every second."""

    def __init__(self, body, clock, headers=None):
        self._body = io.BytesIO(body)
        self._clock = clock
        self.headers = headers or {}

    def _step(self, n):
        self._clock[0] += 1.0
        return self._body.read(n)

    def read(self, n=-1):
        # the old code asked for max_bytes + 1 at once: a trickle still ends, slowly
        self._clock[0] += 1.0 * len(self._body.getvalue())
        return self._body.read(n)

    def read1(self, n=-1):
        return self._step(1)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_stops_a_trickling_reply_at_the_deadline(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(netio, "_clock", lambda: clock[0])
    monkeypatch.setattr(hf.urllib.request, "urlopen",
                        lambda req, timeout: _Trickle(b"{}" * 200, clock))
    with pytest.raises(TimeoutError):
        hf._get("https://x/", "ua", deadline=60)
    assert clock[0] - 1000.0 <= 62               # stopped near the deadline, not at EOF


def test_get_reads_a_prompt_reply_whole(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(netio, "_clock", lambda: clock[0])
    monkeypatch.setattr(hf.urllib.request, "urlopen",
                        lambda req, timeout: _Trickle(b'{"a": 1}', clock))
    assert hf._get("https://x/", "ua", deadline=60)[1] == b'{"a": 1}'


def test_hms_fetch_has_the_longer_deadline(monkeypatch):
    seen = {}

    def fake_get(url, ua, **kw):
        seen.update(kw)
        return 200, _kml_text().encode(), None, None
    monkeypatch.setattr(hf, "_get", fake_get)
    p = hf.HazardFeedsPoller.__new__(hf.HazardFeedsPoller)
    p.cfg = {}

    class F:
        cond = {}
    p._fetch_smoke(F(), _ts(2026, 9, 22, 20), SITE, BOX)
    assert seen["deadline"] == hf.HMS_HTTP_DEADLINE > hf.HTTP_DEADLINE
    assert seen["max_bytes"] == hf.HMS_MAX_BODY_BYTES


@pytest.mark.parametrize("raw", [
    gzip.compress(b"x" * 1000) + b"garbage",               # trailing bytes
    gzip.compress(b"x" * 1000) + gzip.compress(b"y"),      # a second member
    gzip.compress(b"x" * 100000)[:-30],                    # cut short
], ids=["trailing", "two-members", "cut-short"])
def test_gunzip_refuses_what_the_old_one_let_through(raw):
    with pytest.raises(ValueError):
        hf._gunzip(raw, 1 << 20)


def test_gunzip_caps_and_passes():
    assert hf._gunzip(gzip.compress(b"z" * 5000), 5000) == b"z" * 5000
    with pytest.raises(ValueError, match="inflates"):
        hf._gunzip(gzip.compress(b"z" * 5001), 5000)


# ---- E1c: the feed text scrub --------------------------------------------------------
def test_clip_scrubs_surrogates_c1_and_bidi():
    s = hf._clip("Fire\ud800 ‮emertxE‬ near\x85 Lubbock\x9b‏\r\nnow", 200)
    assert s == "Fire� emertxE near Lubbock now"
    s.encode("utf-8")                           # always writable as UTF-8
    assert hf._clip("a\x00b\tc", 20) == "ab c"
    assert hf._clip(None, 5) == "" and hf._clip("abcdefgh", 5) == "abcd…"


def test_parsed_feed_text_is_scrubbed():
    doc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": {"type": "Point", "coordinates": [-101.9, 33.7]},
        "properties": {"valid": "2026-09-22T20:00:00Z", "typetext": "HAIL",
                       "magnitude": 1.0, "city": "Lub‮bock\ud83d",
                       "county": "Lubbock", "state": "TX",
                       "remark": "big⁦ hail\x1b[31m", "source": "public",
                       "wfo": "LUB"}}]}
    out = hf.parse_lsr(json.dumps(doc).encode(), SITE, BOX)
    r = out[0] if isinstance(out, list) else out["reports"][0]
    assert r["place"] == "Lubbock�"
    assert "\x1b" not in r["remark"] and "⁦" not in r["remark"]
    json.dumps(r, ensure_ascii=False).encode("utf-8")


# ---- E1d: ISO times read alike on every Python ---------------------------------------
@pytest.mark.parametrize("text", [
    "2026-11-13T10:53.5Z",          # a fraction of a minute: 3.11+ read 10:53:00.5, the
                                    # 3.9 retry dropped the fraction: both took it
    "20261113T105300Z",             # basic format: 3.11+ only
    "2026-W46-5",                   # ISO week: 3.11+ only
    "2026-11-13T10:53:00+0000x",
    "",
    None,
])
def test_parse_iso_refuses_the_same_on_every_python(text):
    assert hf._parse_iso(text) is None


def test_parse_iso_keeps_its_contract():
    t = _ts(2026, 11, 13, 10, 53)
    assert hf._parse_iso("2026-11-13T10:53:00Z") == t
    assert hf._parse_iso("2026-11-13 10:53:00") == t              # naive -> UTC (IEM)
    assert hf._parse_iso("2026-11-13T04:53:00-06:00") == t
    assert hf._parse_iso(" 2026-11-13T10:53:00.0000000Z ") == t   # 7-digit fraction
    assert hf._parse_iso("2026-11-13T10:53Z") == t
    assert hf._spc_time(None, "202611131053") == t                 # SPC's own fallback


# ---- the status page ------------------------------------------------------------------
def _state(now, **kw):
    st = {"is_safe": True, "ts": now, "reasons": [], "warnings": [], "components": {}}
    st.update(kw)
    return st


def _write_page(tmp_path, monkeypatch, state=None, raw_state=None):
    sf = tmp_path / "state.json"
    sf.write_text(raw_state if raw_state is not None else json.dumps(state),
                  encoding="utf-8")
    monkeypatch.setattr(msp, "SAFETY_STATE_FILE", str(sf))
    monkeypatch.setattr(msp, "HTML_VIA_SHM", False)
    monkeypatch.setattr(msp, "HTML_FILE", str(tmp_path / "status.html"))
    msp.write_html(20.0, 40.0, "2026-09-24 12:00:00 CDT", "chronyc failed", None, None,
                   None, None, {"available": False}, False,
                   {"mode": "disabled", "error": None}, None)
    return (tmp_path / "status.html").read_text(encoding="utf-8")


# E1c: a lone surrogate (JSON's "\ud800" escape) in the state no longer stops the page
def test_page_is_written_with_a_lone_surrogate_in_the_state(tmp_path, monkeypatch):
    now = time.time()
    st = _state(now, is_safe=False, reasons=["Tornado Warning \ud800 over the site"],
                warnings=["feed says ‮boom‬\x1b[31m"])
    raw = json.dumps(st)                       # ensure_ascii: the escape "\\ud800"
    assert "\\ud800" in raw
    page = _write_page(tmp_path, monkeypatch, raw_state=raw)
    assert "Tornado Warning � over the site" in page
    assert "‮" not in page and "\x1b" not in page and "feed says boom" in page
    assert not (tmp_path / "status.html.tmp").exists()


def test_read_safety_state_scrubs_every_text(tmp_path, monkeypatch):
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps({"a‎": ["x\ud83d", {"k": "y\x85\r\nz"}], "n": 1.5,
                              "b": True, "c": None}), encoding="utf-8")
    monkeypatch.setattr(msp, "SAFETY_STATE_FILE", str(sf))
    assert msp.read_safety_state() == {"a": ["x�", {"k": "y\nz"}], "n": 1.5,
                                       "b": True, "c": None}


def test_hz_text_helpers_scrub():
    assert msp._hz_str("Torn‮ado\ud800\x00") == "Tornado�"
    assert msp._hz_e("<b>⁦x⁩</b>") == "&lt;b&gt;x&lt;/b&gt;"


def test_page_write_replaces_what_the_scrub_missed(tmp_path, monkeypatch):
    # a builder that bypasses the scrub: the write costs one "?" instead of the page
    monkeypatch.setattr(msp, "build_safety_html", lambda st: "<p>odd \udc80 text</p>\n")
    page = _write_page(tmp_path, monkeypatch, _state(time.time()))
    assert "<p>odd ? text</p>" in page


def test_failed_page_write_leaves_no_tmp(tmp_path, monkeypatch):
    def boom(a, b):
        raise OSError("disk full")
    monkeypatch.setattr(msp.os, "replace", boom)
    with pytest.raises(OSError):
        _write_page(tmp_path, monkeypatch, _state(time.time()))
    assert not (tmp_path / "status.html.tmp").exists()
    monkeypatch.setattr(msp, "SAFETY_INPUTS_FILE", str(tmp_path / "inputs.json"))
    with pytest.raises(OSError):
        msp.write_safety_inputs(None, 40.0)
    assert not (tmp_path / "inputs.json.tmp").exists()


# E1d: the page's ISO reader: same answer on every Python, an offset still required
def test_hz_iso_ts_reads_alike_and_keeps_its_contract():
    t = _ts(2026, 11, 13, 10, 53)
    assert msp._hz_iso_ts("2026-11-13T10:53:00Z") == t
    assert msp._hz_iso_ts("2026-11-13T04:53:00-06:00") == t
    assert msp._hz_iso_ts("2026-11-13T04:53:00-0600") == t        # 3.9 refused this
    assert msp._hz_iso_ts("2026-11-13T10:53:00") is None            # naive: unknown
    assert msp._hz_iso_ts("2026-11-13") is None
    assert msp._hz_iso_ts("20261113T105300Z") is None               # 3.11+ read this
    assert msp._hz_iso_ts("2026-11-13T10:53.5Z") is None            # 3.11+ read 10:53:00.5
    assert msp._hz_iso_ts("2026-02-30T10:53:00Z") is None
    assert msp._hz_iso_ts(None) is None and msp._hz_iso_ts(" ") is None


# E1f: the state's warnings are on the page whatever the verdict
def test_state_warnings_are_rendered_on_a_safe_page():
    now = time.time()
    html_ = msp.build_safety_html(_state(now, warnings=[
        "Rain check blind: no Weather Underground station reporting", None,
        {"odd": "entry"}]))
    assert "SAFE" in html_
    assert "&#9888; Rain check blind: no Weather Underground station reporting" in html_
    assert "&#9888; {&#x27;odd&#x27;: &#x27;entry&#x27;}" in html_
    stale = msp.build_safety_html(_state(now - 3600, warnings=["config: X ignored"]))
    assert "STALE" in stale and "&#9888; config: X ignored" in stale


# E1e: the page dates itself
def _page_attrs(page):
    m = re.search(r'<section class="[^"]*" id="page"([^>]*)>', page)
    return dict(re.findall(r'(data-[a-z-]+)="([^"]*)"', m.group(1)))


def _script(page):
    return page.split("<script>\n", 1)[1].split("</script>", 1)[0]


def test_page_carries_its_generation_time_and_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("TTU_STATUS_PAGE_STALE_MIN", raising=False)
    monkeypatch.setattr(msp, "CAMERA_ENABLED", False)
    monkeypatch.delenv("STATUS_PAGE_INTERVAL", raising=False)
    t0 = time.time()
    page = _write_page(tmp_path, monkeypatch, _state(t0))
    a = _page_attrs(page)
    assert abs(int(a["data-generated"]) - t0) <= 2
    assert a["data-stale-after"] == "900"
    assert '<div class="pageage" id="pageage" role="alert" hidden></div>' in page
    assert ('This page cannot check its own age without JavaScript: when that time is more '
            'than 15 min ago, the page is no longer being updated and the safety verdict '
            'shown is not current.') in page
    assert '<span class="gl">generated</span>' in page
    assert 'class="sbadge"' in page
    assert _script(page) == msp.PAGE_JS                 # no page data in the script
    js = msp.PAGE_JS
    assert js.index("checkAge();") < js.index("getElementById('modebtn')")


def test_stale_limit_covers_the_longest_normal_run(monkeypatch):
    monkeypatch.delenv("TTU_STATUS_PAGE_STALE_MIN", raising=False)
    monkeypatch.setattr(msp, "CAMERA_ENABLED", False)
    assert msp.page_stale_after_s(90) == 900
    assert msp.page_stale_after_s(600) == 1800           # 3 intervals
    monkeypatch.setattr(msp, "CAMERA_ENABLED", True)     # night stack: minutes per run
    assert msp.page_stale_after_s(90) == 1800
    assert msp.page_stale_after_s(900) == 2700
    monkeypatch.setenv("TTU_STATUS_PAGE_STALE_MIN", "45")
    assert msp.page_stale_after_s(90) == 2700
    for bad in ("0", "-5", "abc", "nan", "inf"):
        monkeypatch.setenv("TTU_STATUS_PAGE_STALE_MIN", bad)
        assert msp.page_stale_after_s(90) == 1800


_HARNESS = """
var attrs = %s;
function ClassList() { this.s = {}; }
ClassList.prototype.add = function (c) { this.s[c] = 1; };
ClassList.prototype.remove = function (c) { delete this.s[c]; };
ClassList.prototype.contains = function (c) { return !!this.s[c]; };
var page = {getAttribute: function (k) { return k in attrs ? attrs[k] : null; },
            classList: new ClassList()};
var box = {hidden: true, textContent: '', className: 'pageage'};
var btn = {textContent: '', setAttribute: function () {}, addEventListener: function () {}};
var document = {getElementById: function (id) {
  return id === 'page' ? page : id === 'pageage' ? box : id === 'modebtn' ? btn : null; }};
var events = [];
var window = {addEventListener: function (e) { events.push(e); }};
var sessionStorage = {getItem: function () { return null; }, setItem: function () {}};
var intervals = [];
function setInterval(f, ms) { intervals.push(ms); }
Date.now = function () { return %d; };
%s
console.log(JSON.stringify({hidden: box.hidden, text: box.textContent, cls: box.className,
  outdated: page.classList.contains('outdated'), intervals: intervals, events: events}));
"""


def _js_age(page, now_s):
    harness = _HARNESS % (json.dumps(_page_attrs(page)), int(now_s * 1000), _script(page))
    out = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_page_script_judges_its_age_by_the_viewers_clock(tmp_path, monkeypatch):
    monkeypatch.delenv("TTU_STATUS_PAGE_STALE_MIN", raising=False)
    monkeypatch.setattr(msp, "CAMERA_ENABLED", False)
    monkeypatch.delenv("STATUS_PAGE_INTERVAL", raising=False)
    page = _write_page(tmp_path, monkeypatch, _state(time.time()))
    t0 = int(_page_attrs(page)["data-generated"])
    fresh = _js_age(page, t0 + 60)
    assert fresh["hidden"] and not fresh["outdated"]
    assert 30000 in fresh["intervals"] and "pageshow" in fresh["events"]
    assert _js_age(page, t0 + 900 - 5)["hidden"]
    old = _js_age(page, t0 + 16 * 60 + 10)
    assert not old["hidden"] and old["outdated"] and old["cls"] == "pageage"
    assert old["text"] == ("This page was generated 16 min ago and is no longer being "
                           "updated: the safety verdict shown is not current.")
    assert _js_age(page, t0 + 5 * 3600 + 120)["text"].startswith(
        "This page was generated 5 h ago")
    behind = _js_age(page, t0 - 10 * 60)            # fake-hwclock: far behind
    assert not behind["hidden"] and not behind["outdated"]
    assert behind["cls"] == "pageage note" and "cannot be judged" in behind["text"]
    assert _js_age(page, t0 - 2 * 60)["hidden"]     # a little behind: nothing


def test_page_flake8_clean():
    flake8 = shutil.which("flake8")
    if flake8 is None:
        pytest.skip("flake8 is not installed")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([flake8, "--max-line-length=100",
                          "--extend-ignore=E203,E501,W503,E402",
                          os.path.join(root, "make_status_page.py")],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout
