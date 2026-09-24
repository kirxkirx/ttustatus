"""Review fixes for the hazards display (status page + /setup): a layer that failed to start
is not shown as switched off, freshness is the daemon's own verdict, capped lists say how
many there really are, and /setup never says "none" for data it does not have.

Reuses test_hazards_page's fixtures (real CAP-derived alert views, trimmed feed items); that
module also stubs the Pi-only libraries make_status_page imports.
"""
import time

from safety import alpaca
from safety.tests.test_hazards_page import _hazard_info, _hazards, _state, _text, msp


# ---- status page ---------------------------------------------------------------------
def test_a_layer_that_failed_to_start_is_not_shown_as_switched_off():
    # what SafetyMonitor publishes when the NwsAlertsPoller constructor raised
    hz = {"safe": True, "enabled": True, "available": False, "veto": [], "at_site": [],
          "nearby": [], "veto_events": ["Tornado Warning"],
          "error": "NWS alerts layer failed to start (RuntimeError: boom) — no warning "
                   "veto; see the log"}
    label, value, sub = msp._hazards_tile(hz)
    assert "N/A" in value and "failed to start" in sub
    assert "TTU_SAFETY_HAZARDS=0" not in sub
    h = _text(msp.build_hazards_html(_state(time.time(), hz)))
    assert "failed to start" in h and "No NWS alert" not in h


def test_nearby_freshness_is_the_daemons_verdict():
    now = time.time()
    # a longer TTU_SAFETY_HAZARD_STALE_SEC: 15 min old is still current for the daemon
    hz = _hazards(now, veto=False, at_site=[], nearby=[], area_age_s=900,
                  area_fresh=True, stale_after_s=1800)
    assert ("No NWS alerts in effect at the site or elsewhere on the map"
            in msp.build_hazards_html(_state(now, hz)))
    # the daemon says stale although the age looks small (e.g. a shorter stale time)
    hz = _hazards(now, veto=False, at_site=[], nearby=[], area_age_s=100, area_fresh=False)
    h = msp.build_hazards_html(_state(now, hz))
    assert "Map-area query unavailable" in h and "None elsewhere" not in h
    # an older daemon: its published threshold, else the default
    assert msp._hz_area_fresh({"area_age_s": 900, "stale_after_s": 1800}) is True
    assert msp._hz_area_fresh({"area_age_s": 900}) is False


def test_capped_info_lists_show_the_real_count():
    now = time.time()
    info = _hazard_info()
    one = dict(info["lsr"][0])
    info["lsr"] = [dict(one, city="%d W Justiceburg" % i) for i in range(20)]
    info["totals"] = {"lsr": 104}
    h = _text(msp.build_hazards_html(_state(now, _hazards(now, veto=False), info)))
    assert "Storm reports (104)" in h
    assert "+84 more not listed (all are drawn on the map)" in h
    info["totals"] = {}                                  # older daemon: the feed count
    info["feeds"]["lsr"]["count"] = 30
    h = _text(msp.build_hazards_html(_state(now, _hazards(now, veto=False), info)))
    assert "Storm reports (30)" in h and "+10 more not listed" in h


# ---- /setup --------------------------------------------------------------------------
def _setup(hz, info):
    return alpaca._hazards_html(hz, info)


def test_setup_never_says_none_for_failed_or_disabled_layers():
    now = time.time()
    down = _hazards(now, veto=False, at_site=[], nearby=[], available=False,
                    point_fresh=False, area_fresh=False,
                    error="point query: URLError: offline")
    info = _hazard_info()
    for name in info["feeds"]:
        info["feeds"][name] = {"ok": False, "error": "network: offline", "age_s": None,
                               "count": 0}
    info.update(quakes=[], fires=[], lsr=[], smoke=None, space_weather=None)
    h = _setup(down, info)
    assert "none" not in h.replace("none configured", "")
    assert "unknown" in h and "no current data" in h
    off = {"enabled": False, "available": False, "safe": True, "veto": [], "at_site": [],
           "nearby": [], "veto_events": ["Tornado Warning", "Dust Storm Warning"]}
    h = _setup(off, None)
    assert "NWS alert layer off" in h and "no veto" in h
    assert "make the monitor UNSAFE" not in h


def test_setup_lists_fresh_empty_data_as_none_and_credits_sources():
    now = time.time()
    hz = _hazards(now, veto=False, at_site=[], nearby=[], point_fresh=True,
                  area_fresh=True)
    info = _hazard_info()
    info.update(source="USGS earthquakes · NOAA HMS smoke")
    h = _setup(hz, info)
    assert "NWS alerts at the site</h3><p>none</p>" in h
    assert "Sources:" in h and "api.weather.gov" in h and "USGS earthquakes" in h
