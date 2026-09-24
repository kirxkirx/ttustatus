"""Status-page rendering of the Hazards section (make_status_page.build_hazards_html) and
the NWS-warnings safety tile, from hand-built safety-state dicts.

Fixtures are trimmed from REAL payloads fetched with curl on 2026-09-24: NWS Lubbock's
TO.W.0033 of 2025-06-05 (a confirmed PDS tornado over Reese Center — the at-site veto
case, IEM text), api.weather.gov Flood Watch / Flash Flood / Dust Storm Warning features,
IEM LSR / SPC MD records, and a live hazard_feeds component. Only the fields the page
reads are kept; times are re-based on "now".
"""
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

# The page imports the Raspberry Pi hardware / heavy libraries at module load; stub them
# exactly as test_smoke.py does (setdefault: a real installed module is left alone).
for _m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
           "astropy.time", "astropy.coordinates", "astropy.utils", "astropy.utils.iers"]:
    sys.modules.setdefault(_m, MagicMock())

import make_status_page as msp        # noqa: E402  (must follow the sys.modules stubbing)

GREEN = "var(--good)"                  # safety_dot_html(True): "checked and clear"
RED = "#b42318"                        # safety_dot_html(False) / the UNSAFE badge


def _iso(dt_s):
    """epoch -> ISO-8601 with offset, as api.weather.gov sends it."""
    return datetime.fromtimestamp(dt_s, timezone(timedelta(hours=-5))).isoformat()


def _tor_warning(now):
    # NWS Lubbock TO.W.0033, 2025-06-05 19:39 CDT (text trimmed).
    return {
        "key": "KLUB.TO.W.0033", "event": "Tornado Warning", "kind": "warning",
        "severity": "Extreme", "urgency": "Immediate",
        "headline": ("Tornado Warning issued June 5 at 7:39PM CDT until June 5 at 8:45PM "
                     "CDT by NWS Lubbock TX"),
        "nws_headline": None, "sender": "NWS Lubbock TX",
        "area_desc": "Hockley, TX; Lubbock, TX",
        "onset": _iso(now - 600), "ends": _iso(now + 3600), "expires": _iso(now + 3600),
        "end_local": "20:45 CDT", "color": "#FF0000", "threat": "PDS", "vetoes": True,
        "veto_event": True, "geometry_source": "polygon",
        "description": (
            "* At 738 PM CDT, a confirmed large and extremely dangerous tornado\n"
            "  was located over Reese Center, or 11 miles west of Lubbock.\n\n"
            "  This is a PARTICULARLY DANGEROUS SITUATION. TAKE COVER NOW!"),
        "instruction": ("Heavy rainfall may hide this tornado. Do not wait to see or hear "
                        "the tornado. TAKE COVER NOW!"),
    }


def _flood_watch(now):
    # NWS Amarillo Flood Watch KAMA.FA.A.0002 (zone-based: geometry from affectedZones).
    return {
        "key": "KAMA.FA.A.0002", "event": "Flood Watch", "kind": "watch",
        "severity": "Severe", "urgency": "Future",
        "headline": ("Flood Watch issued September 24 at 11:44AM CDT until September 25 at "
                     "7:00AM CDT by NWS Amarillo TX"),
        "nws_headline": "FLOOD WATCH REMAINS IN EFFECT THROUGH FRIDAY MORNING",
        "sender": "NWS Amarillo TX", "area_desc": "Cimarron; Texas; Dallam; Sherman; Hartley",
        "onset": _iso(now - 7200), "ends": _iso(now + 50000), "expires": _iso(now + 20000),
        "end_local": "Fri 07:00 CDT", "color": "#E53935", "threat": None, "vetoes": False,
        "veto_event": False, "geometry_source": "zones",
        "description": ("* WHAT...Flooding caused by excessive rainfall continues to be\n"
                        "possible."),
        "instruction": ("You should monitor later forecasts and be alert for possible Flood\n"
                        "Warnings."),
    }


def _ffw(now):
    # NWS Midland/Odessa FF.W.0203 (Eddy NM) with its impact-based damage tag.
    return {
        "key": "KMAF.FF.W.0203", "event": "Flash Flood Warning", "kind": "warning",
        "severity": "Severe", "urgency": "Immediate",
        "headline": ("Flash Flood Warning issued September 24 at 9:52AM MDT until "
                     "September 24 at 12:45PM MDT by NWS Midland/Odessa TX"),
        "nws_headline": None, "sender": "NWS Midland/Odessa TX", "area_desc": "Eddy, NM",
        "onset": _iso(now - 900), "ends": _iso(now + 5400), "expires": _iso(now + 5400),
        "end_local": "13:45 CDT", "color": "#8B0000",
        "threat": "CONSIDERABLE FLASH FLOODING", "vetoes": False, "veto_event": False,
        "geometry_source": "polygon",
        "description": "* At 952 AM MDT, the public reported water flowing over roadways.",
        "instruction": "Turn around, don't drown when encountering flooded roads.",
    }


def _dust_nearby(now):
    # NWS Goodland DS.W.0012 — a veto-TYPE warning that does not cover the site.
    return {
        "key": "KGLD.DS.W.0012", "event": "Dust Storm Warning", "kind": "warning",
        "severity": "Severe", "urgency": "Expected",
        "headline": ("Dust Storm Warning issued September 18 at 4:20PM MDT until "
                     "September 18 at 5:00PM MDT by NWS Goodland KS"),
        "nws_headline": None, "sender": "NWS Goodland KS",
        "area_desc": "Cheyenne, CO; Kit Carson, CO; Sherman, KS",
        "onset": _iso(now - 300), "ends": _iso(now + 2400), "expires": _iso(now + 2400),
        "end_local": "18:00 CDT", "color": "#FFE4C4", "threat": None, "vetoes": False,
        "veto_event": True, "geometry_source": "polygon",
        "description": "* At 420 PM MDT, a dust channel was near Burlington, moving east.",
        "instruction": "If caught in one, pull off the road, turn off your lights.",
    }


def _hazards(now, veto=True, at_site=None, nearby=None, **kw):
    # the component as safety/nws_alerts.py builds it: an AlertView's "vetoes" means "is
    # vetoing now" (its key is in "veto"), "veto_event" means "is a configured veto type"
    tor = _tor_warning(now)
    tor["vetoes"] = bool(veto)
    comp = {
        "safe": not veto, "enabled": True, "available": True,
        "veto_events": ["Tornado Warning", "Dust Storm Warning", "High Wind Warning"],
        "veto": ([{"key": tor["key"], "event": "Tornado Warning",
                   "headline": tor["headline"], "sender": "NWS Lubbock TX",
                   "end_ts": now + 3600, "end_local": "20:45 CDT",
                   "first_seen_ts": now - 540, "source": "both"}] if veto else []),
        "at_site": [tor] if at_site is None else at_site,
        "nearby": ([_flood_watch(now), _ffw(now), _dust_nearby(now)]
                   if nearby is None else nearby),
        "veto_pending": [],
        "point_age_s": 35, "area_age_s": 70, "error": None,
        "source": "NWS api.weather.gov active alerts",
    }
    comp["counts"] = {"at_site": len(comp["at_site"]), "nearby": len(comp["nearby"])}
    comp.update(kw)
    return comp


def _feeds(**override):
    feeds = {n: {"ok": True, "error": None, "age_s": 180, "count": 0}
             for n in ("smoke", "fires", "fire_perimeters", "spc_outlook", "spc_md", "lsr")}
    feeds.update(override)
    return feeds


def _hazard_info():
    return {
        "safe": True, "info_only": True, "enabled": True, "feeds": _feeds(),
        # HMS 2026-09-22: Light smoke over the site in the afternoon analysis
        "smoke": {"at_site": True, "density": "Light", "window": "17:00-23:00 UTC",
                  "count": 2, "on_map": True},
        "fires": [],
        "spc": {"category": "MRGL", "label": "Marginal risk", "on_map": True,
                "mds": [{"num": 2326, "concerning": "SEVERE POTENTIAL...WATCH UNLIKELY",
                         "expire_local": "17:45 CDT", "watch_confidence": 5,
                         "on_map": True}]},
        # IEM LSR (LUB): 1.50 in hail 5 W Justiceburg — inside the map box
        "lsr": [{"time_local": "Wed 18:12 CDT", "type": "HAIL", "magnitude": 1.5,
                 "unit": "INCH", "city": "5 W Justiceburg", "county": "Garza",
                 "distance_km": 88.0, "bearing": "SE", "remark": "Quarter to ping pong "
                 "ball sized hail.", "on_map": True}],
    }


def _state(now=None, hazards=None, info=None, ts=None):
    now = time.time() if now is None else now
    comp = {"sun": {"value_deg": -12.0, "threshold_deg": 0.0, "safe": True},
            "humidity": {"value_pct": 40.0, "threshold_pct": 95.0, "safe": True},
            "rain": {"safe": True, "enabled": True, "latched": False,
                     "polling_active": True, "stations_live": 7, "stations_total": 10}}
    if hazards is not None:
        comp["hazards"] = hazards
    if info is not None:
        comp["hazard_info"] = info
    return {"ts": now if ts is None else ts, "is_safe": not (hazards or {}).get("veto"),
            "reasons": [], "components": comp}


def _text(h):
    """Tag-free, whitespace-collapsed text of an HTML fragment (entities kept)."""
    return " ".join(re.sub(r"<[^>]+>", " ", h).split())


def _tile(tiles, label="NWS warnings"):
    """The one tile with this label out of build_safety_tiles_html's output ('' if none)."""
    for chunk in tiles.split('<div class="tile">'):
        if '<div class="k">%s</div>' % label in chunk:
            return chunk
    return ""


BANNER = 'class="hz-veto"'             # the red "UNSAFE: <event> over the site" banner


# ---- older daemon / switched off ----------------------------------------------------
def test_missing_components_older_daemon_omits_section_and_tile():
    st = _state()                                      # no hazards / hazard_info at all
    assert msp.build_hazards_html(st) == ""
    assert msp.build_hazards_html(None) == ""
    assert msp.build_hazards_html({"components": None}) == ""
    tiles = msp.build_safety_tiles_html(st["components"])
    assert "NWS warnings" not in tiles
    # five tiles keep the original auto-fit grid (no 3-column cap)
    assert "minmax(170px,1fr)" in tiles and "calc(" not in tiles


def test_both_layers_switched_off_omit_section_but_a_load_failure_shows():
    now = time.time()
    off = {"safe": True, "enabled": False, "available": False, "veto": [], "error": None}
    info_off = {"safe": True, "info_only": True, "enabled": False, "feeds": {}}
    assert msp.build_hazards_html(_state(now, off, info_off)) == ""
    tile = _tile(msp.build_safety_tiles_html(_state(now, off)["components"]))
    assert "TTU_SAFETY_HAZARDS=0" in tile and ">off<" in tile and GREEN not in tile
    # the daemon could not load the module: enabled=False WITH an error -> say so
    broken = dict(off, error="module failed to import (SyntaxError: bad)")
    h = msp.build_hazards_html(_state(now, broken, info_off))
    assert "module failed to import" in h and "TTU_SAFETY_HAZARDS=0" not in h
    assert GREEN not in h
    t = _tile(msp.build_safety_tiles_html(_state(now, broken)["components"]))
    assert "module failed to import" in t and GREEN not in t


# ---- the veto ----------------------------------------------------------------------
def test_veto_banner_lists_and_tile():
    now = time.time()
    st = _state(now, _hazards(now), _hazard_info())
    h = msp.build_hazards_html(st)
    txt = _text(h)
    assert "UNSAFE: Tornado Warning over the site until 20:45 CDT" in txt
    assert 'class="hz-veto"' in h and RED in h
    assert "NWS Lubbock TX" in txt and "point query" in txt      # sender + source
    # the at-site entry carries the VETO chip and the PDS threat chip
    assert '<span class="hz-chip veto">VETO</span>' in h
    assert '<span class="hz-chip threat">PDS</span>' in h
    assert "NWS alerts at the site (1)" in txt
    assert "NWS alerts nearby (on the map) (3)" in txt
    # the policy line names exactly the configured veto events, and nothing else vetoes
    assert ("Only a Tornado Warning , Dust Storm Warning or High Wind Warning in effect "
            "over the site makes the safety monitor UNSAFE") in txt
    assert "information only" in txt and "never changes IsSafe" in txt
    tiles = msp.build_safety_tiles_html(st["components"])
    assert "NWS warnings" in tiles and msp._HZ_TILE_VETO in tiles
    assert "Tornado Warning until 20:45 CDT" in tiles and RED in tiles
    # six tiles: capped at three columns (3 + 3, no orphan under a row of five)
    assert "calc((100% - 25px) / 3)" in tiles


def test_latched_veto_says_it_is_held_and_multiple_vetoes_counted():
    now = time.time()
    hz = _hazards(now)
    hz["veto"][0]["source"] = "latched"
    hz["veto"].append({"key": "KLUB.HW.W.0004", "event": "High Wind Warning",
                       "headline": "", "sender": "NWS Lubbock TX", "end_ts": now + 7200,
                       "end_local": "22:00 CDT", "first_seen_ts": now - 60,
                       "source": "point"})
    h = msp.build_hazards_html(_state(now, hz))
    assert "held until the warning ends" in h
    assert "UNSAFE: High Wind Warning over the site until 22:00 CDT" in _text(h)
    tiles = msp.build_safety_tiles_html(_state(now, hz)["components"])
    assert "(+1 more)" in tiles


def test_scheduled_veto_is_announced_but_not_a_veto():
    # a High Wind Warning issued for a LATER period over the site: nws_alerts lists it in
    # veto_pending until shortly before its onset — amber notice, no red banner, no VETO
    now = time.time()
    hw = dict(_tor_warning(now), key="KLUB.HW.W.0007", event="High Wind Warning",
              threat=None, vetoes=False, veto_event=True, color="#DAA520",
              onset=_iso(now + 12 * 3600), end_local="Fri 18:00 CDT")
    hz = _hazards(now, veto=False, at_site=[hw], nearby=[])
    hz["veto_pending"] = [{"key": hw["key"], "event": "High Wind Warning",
                           "headline": "", "sender": "NWS Lubbock TX",
                           "end_ts": now + 20 * 3600, "end_local": "Fri 18:00 CDT",
                           "onset_ts": now + 12 * 3600, "onset_local": "Fri 10:00 CDT",
                           "first_seen_ts": now - 60, "source": "point"}]
    h = msp.build_hazards_html(_state(now, hz))
    txt = _text(h)
    assert 'class="hz-veto hz-pend"' in h and BANNER not in h and "UNSAFE:" not in txt
    assert ("Scheduled: High Wind Warning for the site from Fri 10:00 CDT until Fri 18:00 "
            "CDT") in txt and "not a veto yet" in txt
    assert "veto type &middot; not in effect yet" in h and ">VETO<" not in h
    tile = _tile(msp.build_safety_tiles_html(_state(now, hz)["components"]))
    assert "no veto now &middot; scheduled: High Wind Warning from Fri 10:00 CDT" in tile
    assert msp._HZ_TILE_VETO not in tile


def test_veto_chip_follows_the_veto_list_only():
    now = time.time()
    # flagged vetoes=True but NOT in the component's (empty) veto list: never "VETO"
    tor = _tor_warning(now)
    assert tor["vetoes"] is True
    hz = _hazards(now, veto=False, at_site=[tor], nearby=[])
    h = msp.build_hazards_html(_state(now, hz))
    assert ">VETO<" not in h and "veto type &middot; not vetoing" in h
    # a held veto whose fresh area data no longer puts it at the site sits in "nearby":
    # it is still vetoing (it is in the veto list), and says so
    hz2 = _hazards(now, at_site=[], nearby=[_tor_warning(now)])
    assert '<span class="hz-chip veto">VETO</span>' in msp.build_hazards_html(_state(now, hz2))


def test_real_unavailable_components_render():
    # the exact shapes safety/nws_alerts.py and safety/hazard_feeds.py publish when off
    na = pytest.importorskip("safety.nws_alerts")
    hf = pytest.importorskip("safety.hazard_feeds")
    from safety import config
    hz, info = na.unavailable_component(config), hf.unavailable_component(config)
    now = time.time()
    assert msp.build_hazards_html(_state(now, hz, info)) == ""      # both off on purpose
    tile = _tile(msp.build_safety_tiles_html(_state(now, hz, info)["components"]))
    assert ">off<" in tile and GREEN not in tile
    # NWS layer on but never polled yet (fresh daemon): honest "unknown", no count
    hz_on = dict(hz, enabled=True, source="NWS api.weather.gov active alerts")
    h = msp.build_hazards_html(_state(now, hz_on, info))
    assert "whether an alert is in effect at the site is unknown" in h and GREEN not in h
    assert "Other hazard feeds off (TTU_SAFETY_HAZARD_FEEDS=0)" in h


def test_veto_end_time_falls_back_to_end_ts():
    now = time.time()
    hz = _hazards(now)
    del hz["veto"][0]["end_local"]
    h = msp.build_hazards_html(_state(now, hz))
    assert "UNSAFE: Tornado Warning over the site until %s" % msp._hz_local(now + 3600) \
        in _text(h)


# ---- alert lists ------------------------------------------------------------------
def test_alert_list_entries():
    now = time.time()
    h = msp.build_hazards_html(_state(now, _hazards(now)))
    # swatches in the map's colours (flood products red, never green)
    for c in ("#FF0000", "#E53935", "#8B0000", "#FFE4C4"):
        assert 'class="hz-sw" style="background:%s"' % c in h
    assert '<span class="hz-chip threat">CONSIDERABLE FLASH FLOODING</span>' in h
    # nws_headline shown in addition to the headline; area, sender, local end time
    assert "FLOOD WATCH REMAINS IN EFFECT THROUGH FRIDAY MORNING" in h
    assert "Cimarron; Texas; Dallam; Sherman; Hartley" in h
    assert "until Fri 07:00 CDT" in h and "NWS Amarillo TX" in h
    # full text folded into <details>, description and instruction
    assert h.count("<details><summary>Full text</summary>") == 4
    assert "PARTICULARLY DANGEROUS SITUATION" in h and "Instruction:" in h
    # a veto-type warning NOT over the site is labelled so, never "VETO"
    assert "veto type &middot; not over the site" in h
    assert h.count('<span class="hz-chip veto">VETO</span>') == 1
    # severity / urgency chips
    assert '<span class="hz-chip">Extreme</span>' in h
    # the section credits the source with both query ages
    assert "site query 35 s ago" in h and "map-area query 70 s ago" in h


def test_future_onset_is_shown():
    now = time.time()
    fw = _flood_watch(now)
    fw["onset"] = _iso(now + 6 * 3600)
    h = msp.build_hazards_html(_state(now, _hazards(now, veto=False, at_site=[fw],
                                                    nearby=[])))
    assert "from %s" % msp._hz_local(now + 6 * 3600) in h


def test_long_lists_are_capped_but_counted():
    now = time.time()
    many = []
    for i in range(30):
        a = _flood_watch(now)
        a["key"] = "KAMA.FA.A.%04d" % i
        many.append(a)
    h = msp.build_hazards_html(_state(now, _hazards(now, veto=False, at_site=[],
                                                    nearby=many)))
    assert h.count('class="hz-a"') == msp.HAZARD_LIST_MAX
    assert "+5 more not listed (all are drawn on the map)" in h
    assert "NWS alerts nearby (on the map) (30)" in h


# ---- honest "no alerts" / unavailable / stale ---------------------------------------
def test_no_alerts_with_fresh_data_is_one_green_line():
    now = time.time()
    hz = _hazards(now, veto=False, at_site=[], nearby=[])
    h = msp.build_hazards_html(_state(now, hz))
    assert "No NWS alerts in effect at the site or elsewhere on the map." in h
    assert GREEN in h and "<h3>" not in h and BANNER not in h and "UNSAFE:" not in h
    tile = _tile(msp.build_safety_tiles_html(_state(now, hz)["components"]))
    assert "no veto warning at the site" in tile and ">none<" in tile and GREEN in tile


def test_non_veto_alert_at_site_is_counted_as_info_only():
    now = time.time()
    hz = _hazards(now, veto=False, at_site=[_flood_watch(now)], nearby=[])
    tile = _tile(msp.build_safety_tiles_html(_state(now, hz)["components"]))
    assert "no veto warning at the site &middot; 1 info alert" in tile
    h = msp.build_hazards_html(_state(now, hz))
    assert "VETO" not in h and BANNER not in h and "UNSAFE:" not in h


def test_unavailable_never_claims_no_alerts():
    now = time.time()
    hz = _hazards(now, veto=False, at_site=[], nearby=[], available=False,
                  point_age_s=None, area_age_s=None,
                  error="HTTP Error 503: Service Unavailable")
    h = msp.build_hazards_html(_state(now, hz))
    assert "No NWS alert" not in h and "None elsewhere" not in h and GREEN not in h
    assert "NWS alerts unavailable (HTTP Error 503: Service Unavailable)" in h
    assert "is unknown" in h and "does not veto on its own" in h
    assert "site query: no successful poll" in h
    assert "(0)" not in h                        # no count without data to back it
    tile = _tile(msp.build_safety_tiles_html(_state(now, hz)["components"]))
    assert "alerts unavailable (HTTP Error 503" in tile and ">N/A<" in tile
    assert "no veto warning" not in tile and GREEN not in tile


def test_stale_area_query_makes_nearby_unknown():
    now = time.time()
    hz = _hazards(now, veto=False, at_site=[], nearby=[], area_age_s=5000)
    h = msp.build_hazards_html(_state(now, hz))
    assert "No NWS alert in effect at the site." in h        # point query is fresh
    assert "Map-area query unavailable" in h and "None elsewhere" not in h


def test_last_known_lists_are_labelled_when_queries_fail():
    now = time.time()
    hz = _hazards(now, veto=False, available=False, area_age_s=None)
    h = msp.build_hazards_html(_state(now, hz))
    assert h.count("Last known") == 2


def test_stale_state_shows_stale_not_reassurance():
    now = time.time()
    old = now - 99999
    # no veto: nothing reassuring may remain; the info part is dropped entirely
    hz = _hazards(now, veto=False, at_site=[], nearby=[])
    st = _state(now, hz, _hazard_info(), ts=old)
    h = msp.build_hazards_html(st)
    assert "Hazard information stale" in h and "min ago" in h
    assert "No NWS alert" not in h and GREEN not in h
    assert "<h3>Other hazard information" not in h and "On the radar map" not in h
    tiles = msp.build_safety_tiles_html(st["components"], state_stale=True)
    assert ">none<" not in _tile(tiles) and GREEN not in tiles
    # with a veto: the last-known veto stays visible (errs toward caution), labelled
    st2 = _state(now, _hazards(now), _hazard_info(), ts=old)
    h2 = msp.build_hazards_html(st2)
    assert "Last known (safety state stale): UNSAFE: Tornado Warning" in _text(h2)
    assert "NWS alerts at the site" not in h2           # stale lists are not shown
    tiles2 = msp.build_safety_tiles_html(st2["components"], state_stale=True)
    assert msp._HZ_TILE_VETO in tiles2
    # undated state is stale too
    st3 = _state(now, hz)
    st3["ts"] = None
    assert "no timestamp" in msp.build_hazards_html(st3)


def test_stale_with_only_info_component():
    now = time.time()
    h = msp.build_hazards_html(_state(now, info=_hazard_info(), ts=now - 99999))
    assert "Hazard information stale" in h and "Light smoke" not in h


# ---- information-only feeds -----------------------------------------------------------
def test_info_section_rows_and_map_key():
    now = time.time()
    h = msp.build_hazards_html(_state(now, _hazards(now, veto=False), _hazard_info()))
    txt = _text(h)
    assert "Other hazard information information only" in txt
    assert "Smoke: Light smoke over the site (17:00-23:00 UTC)" in txt
    assert "SPC Day 1 outlook: Marginal risk (MRGL) at the site" in txt
    assert ("MD #2326 &middot; SEVERE POTENTIAL...WATCH UNLIKELY &middot; until 17:45 CDT "
            "&middot; watch probability 5%") in txt
    assert ("Storm reports: Wed 18:12 CDT &middot; HAIL 1.5 INCH &middot; 5 W Justiceburg "
            "&middot; 88 km SE from the site") in txt
    # each row carries its feed status; empty-but-fresh feeds are folded into one line
    assert "NOAA HMS &middot; updated 3 min ago" in h
    assert "None current: wildfire incidents (NIFC WFIGS)." in txt
    # items inside the map box are marked, and the map key names what is drawn
    assert h.count('<span class="hz-chip">on map</span>') == 4    # smoke, SPC, MD, LSR
    assert "On the radar map:" in h and "smoke: a grey veil" in h
    assert "storm reports: &#9660; tornado" in h and "wildfires:" not in h
    assert "Feed status (6 feeds)" in h
    assert h.index("On the radar map:") < h.index("Feed status")


def _live_feeds_component():
    # safety/hazard_feeds.py HazardFeedsPoller.component() output from a live poll on
    # 2026-09-24 ~12:00 CDT (trimmed): site in SPC TSTM with a MRGL outline on the map,
    # one HMS smoke area on the map.
    feeds = {n: {"ok": True, "error": None, "age_s": 3, "count": c, "label": n,
                 "source": "(feed attribution)", "interval_s": 600}
             for n, c in (("smoke", 1), ("fires", 0), ("fire_perimeters", 0),
                          ("spc_outlook", 1), ("spc_md", 0), ("lsr", 0))}
    return {
        "safe": True, "info_only": True, "enabled": True, "available": True,
        "feeds": feeds,
        "smoke": {"file_date": "2026-09-24", "site_in_smoke": False,
                  "site_in_smoke_today": False, "density": None, "on_map": 1,
                  "latest_window": "07:00–10:00 CDT (12:00–15:00 UTC)",
                  "text": "No smoke over the site · 1 smoke area on the map in the latest "
                          "HMS analysis: 07:00–10:00 CDT (12:00–15:00 UTC)",
                  "note": "HMS smoke is analysed from daytime satellite imagery only; it is "
                          "smoke anywhere in the column (aloft), not surface air quality."},
        "fires": [],
        "spc": {"ok": True, "category": "TSTM", "label": "General Thunderstorms Risk",
                "text": "SPC Day 1: General Thunderstorms Risk (TSTM) at the site · valid "
                        "Thu 11:30 CDT – Fri 07:00 CDT",
                "color": "#9E9E9E", "on_map": ["MRGL"], "mds": []},
        "lsr": [],
        "on_map": 2,
        "source": ("NOAA HMS smoke · NIFC WFIGS fires · NOAA SPC outlook · "
                   "SPC mesoscale discussions and NWS storm reports via IEM"),
    }


def test_info_section_with_the_hazard_feeds_component():
    now = time.time()
    h = msp.build_hazards_html(_state(now, _hazards(now, veto=False, at_site=[],
                                                    nearby=[]), _live_feeds_component()))
    txt = _text(h)
    # the daemon's own lines, the row label not repeated ("SPC Day 1: " stripped once)
    assert ("SPC Day 1 outlook: General Thunderstorms Risk (TSTM) at the site · valid "
            "Thu 11:30 CDT – Fri 07:00 CDT") in txt
    assert "SPC Day 1: " not in txt
    # the chip names what is drawn (the MRGL outline), not the site's TSTM category
    assert '<span class="hz-chip">on map: MRGL</span>' in h
    assert "Smoke: No smoke over the site · 1 smoke area on the map" in txt
    assert "aloft), not surface air quality" in txt                   # the HMS caveat
    assert ("None current: SPC mesoscale discussions on the map (NOAA SPC via IEM), storm reports "
            "on the map (NWS LSR via IEM), wildfire incidents (NIFC WFIGS).") in txt
    assert "Sources: NOAA HMS smoke · NIFC WFIGS fires" in txt
    assert h.count("hz-chip\">on map") == 2                           # smoke, SPC
    assert "SPC outlook: dashed outlines" in h and "smoke: a grey veil" in h


def test_failed_or_unknown_feed_never_says_none():
    now = time.time()
    info = _hazard_info()
    info["feeds"] = _feeds(fires={"ok": False, "error": "HTTP Error 503", "age_s": None,
                                  "count": 0},
                           lsr={"ok": True, "error": None, "age_s": 99999, "count": 0})
    info["lsr"] = []
    del info["feeds"]["spc_md"]                         # a feed the page cannot find
    info["spc"]["mds"] = []
    txt = _text(msp.build_hazards_html(_state(now, info=info)))
    assert "None current" not in txt or "wildfire" not in txt.split("None current")[1]
    assert "wildfire incidents (NIFC WFIGS, unavailable: HTTP Error 503)" in txt
    assert "storm reports on the map (NWS LSR via IEM, no recent update (27.8 h ago))" in txt
    assert ("SPC mesoscale discussions on the map (NOAA SPC via IEM, status unknown)"
            in txt)


def test_last_known_items_of_a_failed_feed_are_labelled():
    now = time.time()
    info = _hazard_info()
    info["feeds"]["lsr"] = {"ok": False, "error": "timed out", "age_s": 900, "count": 1}
    h = msp.build_hazards_html(_state(now, info=info))
    assert "NWS LSR via IEM &middot; last known, unavailable: timed out" in h


def test_information_never_renders_unsafe_or_veto():
    # Nothing from hazard_info — however alarming, and even with a bogus safe=False —
    # may read as a veto: only the NWS component's veto list does.
    now = time.time()
    info = _hazard_info()
    info["safe"] = False
    info["lsr"].append({"text": "TORNADO · 2 N Range Hill TX", "on_map": True})
    hz = _hazards(now, veto=False, at_site=[], nearby=[])
    st = _state(now, hz, info)
    h = msp.build_hazards_html(st)
    assert BANNER not in h and "UNSAFE:" not in h and "VETO" not in h and RED not in h
    tile = _tile(msp.build_safety_tiles_html(st["components"]))
    assert msp._HZ_TILE_VETO not in tile and "no veto warning at the site" in tile
    assert GREEN in tile
    # hazard_info alone (no NWS component) never produces a tile either
    assert "NWS warnings" not in msp.build_safety_tiles_html(
        _state(now, info=info)["components"])


def test_older_daemon_state_with_retired_feeds_renders():
    # a state file written by a daemon that still polled the USGS earthquake and NOAA
    # SWPC space-weather feeds (the page is updated before the daemon restarts): their
    # lists are ignored, no row or map-key entry, and everything else renders as usual
    now = time.time()
    info = _hazard_info()
    info["feeds"].update(quakes={"ok": True, "error": None, "age_s": 180, "count": 1},
                         space_weather={"ok": True, "error": None, "age_s": 180,
                                        "count": 1})
    info["quakes"] = [{"text": "M3.6 · 32 km SW of Garden City, Texas", "on_map": True}]
    info["space_weather"] = {"text": "Kp 3.7 (24 h max 4.3) · NOAA scales now R0 S0 G0"}
    info["totals"] = {"quakes": 1}
    h = msp.build_hazards_html(_state(now, _hazards(now, veto=False), info))
    txt = _text(h)
    assert "Garden City" not in txt and "Kp 3.7" not in txt
    assert "Earthquakes" not in txt and "Space weather" not in txt
    assert "Smoke: Light smoke over the site (17:00-23:00 UTC)" in txt
    assert h.count('<span class="hz-chip">on map</span>') == 4    # smoke, SPC, MD, LSR
    # (the folded-away feed-status list shows whatever feeds the daemon reports)
    assert "Feed status (8 feeds)" in h


def test_info_feeds_off_and_nws_off_texts():
    now = time.time()
    info = {"safe": True, "info_only": True, "enabled": False, "feeds": {}}
    h = msp.build_hazards_html(_state(now, _hazards(now, veto=False), info))
    assert "Other hazard feeds off (TTU_SAFETY_HAZARD_FEEDS=0)" in h
    off = {"safe": True, "enabled": False, "available": False, "veto": [], "error": None}
    h2 = msp.build_hazards_html(_state(now, off, _hazard_info()))
    assert "NWS alert layer off (TTU_SAFETY_HAZARDS=0)" in h2 and "Light smoke" in h2


# ---- hostile input ------------------------------------------------------------------
EVIL = '"><script>alert(1)</script><img src=x onerror=alert(2)>'


def test_hostile_text_is_escaped_everywhere():
    now = time.time()
    hz = _hazards(now)
    for a in hz["at_site"] + hz["nearby"]:
        for k in ("event", "headline", "nws_headline", "sender", "area_desc", "threat",
                  "end_local", "severity", "urgency", "description", "instruction"):
            a[k] = EVIL + k
    for k in ("event", "headline", "sender", "end_local", "source"):
        hz["veto"][0][k] = EVIL + k
    hz["veto_events"] = [EVIL]
    hz["error"] = EVIL
    info = _hazard_info()
    info["smoke"] = {"at_site": True, "density": EVIL, "window": EVIL}
    info["spc"] = {"category": EVIL, "label": EVIL,
                   "mds": [{"num": EVIL, "concerning": EVIL, "expire_local": EVIL,
                            "watch_confidence": EVIL}]}
    info["lsr"] = [{"text": EVIL, "on_map": True},
                   {"type": EVIL, "magnitude": EVIL, "unit": EVIL, "city": EVIL,
                    "remark": EVIL, "time_local": EVIL}]
    info["fires"] = [{"name": EVIL, "acres": 12, "county": EVIL, "updated_local": EVIL,
                      "bearing": EVIL, "distance_km": 12}]
    info["feeds"] = {EVIL: {"ok": False, "error": EVIL, "age_s": 5, "count": 1}}
    st = _state(now, hz, info)
    out = msp.build_hazards_html(st) + msp.build_safety_tiles_html(st["components"])
    # no raw tag survives (an escaped "onerror=" is inert text)
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    # every attribute value the page writes is still well-formed (no quote breakout)
    for m in re.finditer(r'<[a-z0-9]+(\s[^>]*)?>', out):
        assert m.group(0).count('"') % 2 == 0, m.group(0)


def test_colour_validation():
    assert msp._hz_color("#FF0000") == "#FF0000"
    assert msp._hz_color("#8b0000") == "#8b0000"
    for bad in (None, "red", "#FF000", "#FF00000", "#GG0000", 123, ["#FF0000"],
                "#FF0000;background:url(javascript:alert(1))", "#FF0000\n",
                " #FF0000", "#FF0000\"><script>"):
        assert msp._hz_color(bad) == msp._HZ_GREY, bad
    now = time.time()
    a = _flood_watch(now)
    a["color"] = "red;background-image:url(//evil.example/x.png)"
    h = msp.build_hazards_html(_state(now, _hazards(now, veto=False, at_site=[a],
                                                    nearby=[])))
    # an invalid colour gets the product-type colour the map falls back to (a watch)
    assert 'class="hz-sw" style="background:#E6B800"' in h and "evil.example" not in h


def test_swatch_is_the_map_colour_never_green():
    c = msp._hz_alert_color
    assert c({"color": "#FF0000", "kind": "warning"}) == "#FF0000"
    assert c({"color": "#e53935"}) == "#E53935"                   # canonical upper case
    assert c({"color": "f00", "event": "Tornado Warning"}) == "#FF0000"
    assert c({"color": "#00FFFF", "event": "Freeze Watch"}) == "#00FFFF"   # cyan != green
    # the NWS chart's greens are drawn in the product-type colour on the map, so here too
    assert c({"color": "#00FF00", "kind": "warning"}) == "#D00000"          # Flood Warning
    assert c({"color": "#2E8B57", "event": "Flood Watch"}) == "#E6B800"
    assert c({"color": "#00FF7F", "kind": "advisory"}) == "#7B68EE"
    assert c({"color": "#7FFF00", "event": "Evacuation Immediate"}) == "#808080"
    assert c({"color": None, "event": "Special Weather Statement"}) == "#FFE4B5"
    assert c({"color": "#FF0000\"><script>", "kind": "bogus"}) == "#808080"


def test_swatch_rule_matches_radar_py():
    # the page is the map's legend: its swatch must be exactly what radar.py draws
    rd = pytest.importorskip("safety.radar")
    if not all(hasattr(rd, n) for n in ("_never_green", "_hex_rgb", "KIND_RGB")):
        pytest.skip("radar.py overlay colour helpers not present")
    for col in ("#FF0000", "#00FF00", "#2E8B57", "#8B0000", "#FFE4C4", "#00FFFF",
                "#7FFF00", "#ADFF2F", "#008B8B", "#90EE90", "0f0", "nonsense", None):
        for kind, rgb in rd.KIND_RGB.items():
            want = rd._never_green(rd._hex_rgb(col) or rgb, rgb)
            assert msp._hz_alert_color({"color": col, "kind": kind}) \
                == "#%02X%02X%02X" % tuple(want), (col, kind)


def test_iso_parsing_and_time_helpers():
    assert msp._hz_iso_ts("2026-09-25T07:00:00-05:00") == 1790337600.0
    assert msp._hz_iso_ts("2026-09-25T12:00:00Z") == 1790337600.0
    for bad in (None, "", "tomorrow", "2026-09-25T07:00:00", 17903376):
        assert msp._hz_iso_ts(bad) is None
    assert msp._hz_until({"ends": None, "expires": "2026-09-25T12:00:00Z"}) \
        == msp._hz_local(1790337600.0)
    assert msp._hz_until({}) is None
    assert msp._hz_age(35) == "35 s ago" and msp._hz_age(600) == "10 min ago"
    assert msp._hz_age(None) is None and msp._hz_age(float("nan")) is None


# ---- the page as a whole ------------------------------------------------------------
def _write_page(tmp_path, monkeypatch, state):
    import json
    sf = tmp_path / "state.json"
    sf.write_text(json.dumps(state))
    monkeypatch.setattr(msp, "SAFETY_STATE_FILE", str(sf))
    monkeypatch.setattr(msp, "HTML_VIA_SHM", False)
    monkeypatch.setattr(msp, "HTML_FILE", str(tmp_path / "status.html"))
    msp.write_html(20.0, 40.0, "2026-09-24 12:00:00 CDT", "chronyc failed", None, None,
                   None, None, {"available": False}, False, {"mode": "disabled",
                                                             "error": None}, None)
    return (tmp_path / "status.html").read_text(encoding="utf-8")


def test_write_html_places_hazards_after_radar(tmp_path, monkeypatch):
    now = time.time()
    st = _state(now, _hazards(now), _hazard_info())
    st["components"]["radar"] = {"trigger_km": 30, "enabled": True, "available": True,
                                 "in_ring": False, "thumb_available": False}
    st["components"]["nws"] = {"available": True, "hours": [
        {"local": "Thu 20:00", "cloud_cover_pct": 10, "precip_prob_pct": 0,
         "thunder_prob_pct": 0, "temp_f": 70, "wind_speed_kmh": 10, "wind_dir_deg": 180}]}
    page = _write_page(tmp_path, monkeypatch, st)
    i_radar = page.index("Radar (MRMS")
    i_hz = page.index("Hazards (NWS alerts")
    i_fc = page.index("12-hour forecast")
    assert i_radar < i_hz < i_fc
    assert "the <b>Hazards</b> section below is their key" in page
    assert "UNSAFE: Tornado Warning over the site" in _text(page)


def test_write_html_render_error_is_visible(tmp_path, monkeypatch):
    def boom(state):
        raise RuntimeError("bug")
    monkeypatch.setattr(msp, "build_hazards_html", boom)
    now = time.time()
    page = _write_page(tmp_path, monkeypatch, _state(now, _hazards(now)))
    assert "Hazards section unavailable" in page
    # ... but an older daemon (no hazards component) gets no such note
    page2 = _write_page(tmp_path, monkeypatch, _state(now))
    assert "Hazards" not in page2


def test_malformed_state_never_crashes(tmp_path, monkeypatch):
    now = time.time()
    for st in ({"components": []}, {"components": {"hazards": []}}, [1, 2], "x",
               {"ts": "yesterday", "components": {"hazards": {"veto": "x", "at_site": 7}}},
               {"ts": now, "components": {"hazards": {
                   "enabled": True, "available": True, "veto": [{"key": ["x"]}, 3],
                   "veto_pending": [{"key": {"a": 1}}], "veto_events": [None, 5, "Tornado Warning"],
                   "at_site": [{"key": {"a": 1}, "event": 5, "color": 7, "onset": 3,
                                "ends": [], "threat": {}}, None],
                   "nearby": "x", "counts": {"at_site": float("nan"), "nearby": "3"},
                   "point_age_s": "x", "area_age_s": float("inf")},
                   "hazard_info": {"feeds": [], "fires": {"x": 1}, "smoke": [],
                                   "spc": "x", "lsr": [None, 3, {"on_map": [1, None]}],
                                   "totals": {"lsr": "many"}}}}):
        msp.build_hazards_html(st)                       # must not raise
        comp = st.get("components") if isinstance(st, dict) else None
        if isinstance(comp, dict):
            msp.build_safety_tiles_html(comp)
    # a state file that is valid JSON but not an object: the page is still written
    page = _write_page(tmp_path, monkeypatch, [1, 2])
    assert "</html>" in page and "Hazards section unavailable" not in page


def test_fixture_file_is_small():
    assert os.path.getsize(__file__) <= 40 * 1024
