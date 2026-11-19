"""The Domes card of the status page (make_status_page.build_domes_html) and R1: with no
dome configured, the page and /setup are byte for byte what they were before."""
import copy
import re
import sys
import time
from unittest.mock import MagicMock

import pytest

for _m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
           "astropy.time", "astropy.coordinates", "astropy.utils", "astropy.utils.iers"]:
    sys.modules.setdefault(_m, MagicMock())

import make_status_page as msp        # noqa: E402  (must follow the sys.modules stubbing)
from safety import alpaca, config, domes     # noqa: E402
from safety.tests import domes_r1     # noqa: E402

GREEN, RED = "var(--good)", "#b42318"


# --- R1: no dome configured -> the same bytes for the same state ------------------------
@pytest.mark.parametrize("name", domes_r1.STATES)
def test_r1_page_and_setup_are_unchanged_without_a_dome(name, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOMES", ())
    with domes_r1.frozen_time():
        page, setup = domes_r1.render(name, str(tmp_path))
    assert page == domes_r1.expected(name, "page")
    if setup is not None:
        assert setup == domes_r1.expected(name, "setup")


def test_r1_no_card_without_a_domes_block():
    for st in (None, {}, {"ts": time.time()}, {"ts": time.time(), "domes": None},
               {"ts": time.time(), "domes": {"enabled": False}},
               {"ts": time.time(), "domes": {"enabled": True, "domes": []}},
               {"ts": time.time(), "domes": "garbage"}):
        assert msp.build_domes_html(st) == ""


# --- the card ------------------------------------------------------------------------------
def sw(label, on, known=True, warn=False):
    return {"label": label, "on": on, "known": known, "warn": warn}


def dome(n=1, name="Dome 1 (west)", shutter="closed", known=True, age_s=12, why=None,
         switches=None):
    return {"n": n, "name": name, "shutter": shutter, "known": known, "age_s": age_s,
            "why": why, "source": "merlino-dome 0.4.3",
            "switches": [sw("Mount", True), sw("CCD", False), sw("Light", False)]
            if switches is None else switches}


def state(*domes_, ts=None):
    return {"ts": time.time() if ts is None else ts, "is_safe": True,
            "domes": {"enabled": True, "stale_after_s": 120, "error": None,
                      "domes": list(domes_)}}


def tile(page, name):
    m = re.search(r'<div class="tile">\s*<div class="k">%s</div>(.*?)\n    </div>\n'
                  % re.escape(name), page, re.S)
    assert m, "no tile %r" % name
    return m.group(1)


def test_one_tile_per_dome_in_a_card_of_its_own():
    page = msp.build_domes_html(state(dome(), dome(2, "Dome 2 (east)", "open")))
    assert page.startswith("  <section ") and page.endswith("  </section>\n")
    assert "<h2" in page and ">Domes</h2>" in page
    assert page.count('<div class="tile">') == 2 and '<div class="tiles"' in page
    assert '<div class="v">CLOSED</div>' in tile(page, "Dome 1 (west)")
    assert '<div class="v">OPEN</div>' in tile(page, "Dome 2 (east)")
    assert "Display only: not an input of the safety monitor." in page


@pytest.mark.parametrize("shutter", domes.SHUTTER_STATES)
def test_each_roof_state_in_capitals(shutter):
    t = tile(msp.build_domes_html(state(dome(shutter=shutter))), "Dome 1 (west)")
    assert ">%s</div>" % shutter.upper() in t
    # UNKNOWN and ERROR in the warning colour, the others plain
    warned = 'style="color:var(--warn)">%s<' % shutter.upper() in t
    assert warned == (shutter in ("unknown", "error"))


def test_the_report_age_is_counted_on_to_the_page_time():
    # the state file was written 40 s ago, the report was then 12 s old: 52 s now
    t = tile(msp.build_domes_html(state(dome(age_s=12), ts=time.time() - 40)),
             "Dome 1 (west)")
    assert re.search(r"reported 5[23] s ago", t)


@pytest.mark.parametrize("limit,report_age,state_age,expired", [
    (120, 110, 30, True),             # fresh at the last evaluation, expired now
    (120, 90, 30, False),             # the store expires only ABOVE its limit
    (120, 89, 30, False),
    (60, 40, 30, True),               # use the configured limit, not a fixed 120 s
    (180, 110, 30, False),
])
def test_report_expiry_is_rechecked_when_rendering(monkeypatch, limit, report_age,
                                                   state_age, expired):
    now = 1_800_000_000.0
    monkeypatch.setattr(msp.time, "time", lambda: now)
    st = state(dome(shutter="open", age_s=report_age,
                    switches=[sw("Mount", True), sw("CCD", False),
                              sw("Light", True, warn=True)]),
               dome(2, "Dome 2 (east)", "closed", age_s=0), ts=now - state_age)
    st["domes"]["stale_after_s"] = limit
    page = msp.build_domes_html(st)
    west = tile(page, "Dome 1 (west)")
    if expired:
        assert ">UNKNOWN</div>" in west and ">OPEN</div>" not in west
        assert "no report for %d min" % ((report_age + state_age) // 60) in west
        for label in ("Mount", "CCD", "Light"):
            assert msp._DOME_DOT_UNKNOWN + label + " ?" in west
        assert "Light ON" not in west
    else:
        assert ">OPEN</div>" in west and "reported " in west
        assert msp._DOME_DOT_ON + "Mount on" in west and "Light ON" in west
    # Expiring one dome does not hide another dome's fresh report.
    assert ">CLOSED</div>" in tile(page, "Dome 2 (east)")


def test_switches_on_off_unknown_and_warn():
    d = dome(switches=[sw("Mount", True), sw("CCD", False), sw("USB", None, known=False),
                       sw("Light", True, warn=True)])
    t = tile(msp.build_domes_html(state(d)), "Dome 1 (west)")
    assert msp._DOME_DOT_ON + "Mount on" in t
    assert msp._DOME_DOT_OFF + "CCD off" in t
    assert msp._DOME_DOT_UNKNOWN + "USB ?" in t
    assert (msp._DOME_DOT_WARN + '<span style="color:var(--warn);font-weight:600">Light ON'
            in t)
    # a warn switch that is OFF is just off
    t = tile(msp.build_domes_html(state(dome(switches=[sw("Light", False, warn=False)]))),
             "Dome 1 (west)")
    assert msp._DOME_DOT_OFF + "Light off" in t and "Light ON" not in t


@pytest.mark.parametrize("why,words", [
    ("no report yet", "no report received yet"),
    ("not current", "the dome driver has no current reading"),
    ("error", "dome reports unavailable: see the daemon log"),
])
def test_unknown_dome_says_why(why, words):
    d = dome(shutter="unknown", known=False, why=why, age_s=None,
             switches=[sw("Mount", None, known=False)])
    t = tile(msp.build_domes_html(state(d)), "Dome 1 (west)")
    assert 'color:var(--warn)">UNKNOWN<' in t and words in t
    assert msp._DOME_DOT_UNKNOWN + "Mount ?" in t


def test_a_stale_report_says_for_how_long():
    d = dome(shutter="unknown", known=False, why="stale", age_s=600,
             switches=[sw("Mount", None, known=False)])
    t = tile(msp.build_domes_html(state(d)), "Dome 1 (west)")
    assert "no report for 10 min" in t and ">UNKNOWN<" in t


def test_a_stale_state_file_shows_every_dome_unknown():
    # the daemon is down: its last word about the roofs is no longer a reading
    st = state(dome(shutter="open", switches=[sw("Light", True, warn=True)]),
               ts=time.time() - msp.SAFETY_STATE_STALE_SEC - 5)
    t = tile(msp.build_domes_html(st), "Dome 1 (west)")
    assert ">UNKNOWN<" in t and ">OPEN<" not in t
    assert "safety daemon state is stale" in t
    assert msp._DOME_DOT_UNKNOWN + "Light ?" in t and "Light ON" not in t
    # ...and so does a state dated in the future
    st["ts"] = time.time() + 600
    assert ">UNKNOWN<" in tile(msp.build_domes_html(st), "Dome 1 (west)")


def test_known_false_never_shows_a_roof_state_even_if_one_is_sent():
    d = dome(shutter="closed", known=False, why="not current",
             switches=[sw("Mount", True, known=True)])
    t = tile(msp.build_domes_html(state(d)), "Dome 1 (west)")
    assert ">CLOSED<" not in t and ">UNKNOWN<" in t
    assert "Mount on" not in t and "Mount ?" in t


def test_hostile_texts_are_escaped_and_odd_values_render():
    d = dome(name='<script>alert(1)</script>', shutter="<b>open</b>",
             switches=[sw('<img src=x onerror=alert(1)>', True), "junk", None])
    page = msp.build_domes_html(state(d, {"n": 2}, "junk"))
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page and "&lt;img src=x" in page
    assert ">UNKNOWN<" in page            # an unknown roof word is not echoed as a state
    assert "Dome 2" in page               # a dome without a name gets its number


def test_no_green_and_no_red_in_the_card():
    d = dome(switches=[sw("Mount", True), sw("CCD", False), sw("Light", True, warn=True)])
    for d2 in (d, dome(shutter="error"), dome(shutter="unknown", known=False,
                                              why="no report yet")):
        page = msp.build_domes_html(state(d2))
        assert GREEN not in page and RED not in page and "var(--good" not in page


def test_no_switch_line_when_no_switch_is_shown():
    t = tile(msp.build_domes_html(state(dome(switches=[]))), "Dome 1 (west)")
    assert t.count('<div class="s">') == 1


def test_the_card_sits_right_after_the_safety_card(tmp_path, monkeypatch):
    st = copy.deepcopy(domes_r1.load_state("a"))
    st["domes"] = state(dome())["domes"]
    path = tmp_path / "state.json"
    path.write_text(__import__("json").dumps(st), encoding="utf-8")
    with domes_r1.frozen_time():
        monkeypatch.setattr(msp, "SAFETY_STATE_FILE", str(path))
        monkeypatch.setattr(msp, "HTML_VIA_SHM", False)
        monkeypatch.setattr(msp, "HTML_FILE", str(tmp_path / "status.html"))
        msp.write_html(20.0, 40.0, "2026-11-18 21:00:00 CST", "chronyc failed", None, None,
                       None, None, {"available": False}, False,
                       {"mode": "disabled", "error": None}, None)
    page = (tmp_path / "status.html").read_text(encoding="utf-8")
    safety_end = page.index("Log (latest events)")
    domes_at = page.index(">Domes</h2>")
    dew_at = page.index("Dew monitor (rain-sensor override)")
    assert safety_end < domes_at < dew_at


# --- /setup ------------------------------------------------------------------------------
class _Store:
    refused_count = 3


def test_setup_lists_the_domes_and_the_refused_count():
    st = copy.deepcopy(domes_r1.load_state("a"))
    st["domes"] = state(dome(switches=[sw("Mount", True), sw("Light", True, warn=True)]),
                        dome(2, "Dome 2 (east)", "unknown", False, None, "no report yet",
                             []))["domes"]
    mon = domes_r1.FixedMonitor(st)
    mon.domes = _Store()
    html = alpaca.setup_page(mon, config)
    part = html[html.index("<h2>Domes</h2>"):]
    assert "Dome 1 (west)" in part and "<b>CLOSED</b> · reported 12 s ago" in part
    assert "Mount on · Light ON (should be off)" in part and "merlino-dome 0.4.3" in part
    assert "Dome 2 (east)" in part and "<b>UNKNOWN</b> · no report received yet" in part
    assert "refused for their key since the daemon started: 3." in part
    assert "/domes/v1/report" in part


def test_setup_without_domes_has_no_section():
    mon = domes_r1.FixedMonitor(domes_r1.load_state("a"))
    assert "Domes" not in alpaca.setup_page(mon, config)
