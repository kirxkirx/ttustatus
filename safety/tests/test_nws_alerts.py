"""Tests for safety/nws_alerts.py: the configured NWS veto events (Tornado / Dust Storm / High
Wind Warning) OVER THE SITE make the monitor unsafe for the warning's duration; everything
else is information only.

Fixtures: real api.weather.gov payloads (curl, 2026-09-24), trimmed. Core: NWS Lubbock's
KLUB.SV.W.0263 (2026-09-21): its 16:11 CDT polygon covered the site, its 16:21 Cancel segment
still carried a polygon over it, its 16:34 Update moved the polygon off the site while
Lubbock County (TXC303, the site's county) stayed listed. Plus the real TXZ035 outline, the
site's /points zones, live Flood Watch / Air Quality Alert messages, and KBOX High Wind /
KGLD Dust Storm Warnings moved to Lubbock. No Tornado Warning hit Lubbock in the API's 7-day
history: tornado fixtures are KLUB.SV.W.0263 renamed (event + VTEC phenomenon).
"""
import colorsys
import copy
import gzip
import io
import json
import math
import os
import time
import types
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from safety import config
from safety import nws_alerts as na

NOW = 1790025120.0                         # 2026-09-21 16:12 CDT (21:12Z)
T1611, T1621, T1634 = NOW - 60, NOW + 540, NOW + 1320
END = NOW + 2880                           # 17:00 CDT, the warning's real end
CDT = timezone(timedelta(hours=-5))
SITE = (33.748, -101.958)                  # config.GEOCODE (TTU, rounded)
Z = "https://api.weather.gov/zones/"
DAY = 86400.0


def iso(ts):
    return datetime.fromtimestamp(ts, CDT).isoformat()


# ---- real payloads, trimmed
POLY_1611 = [[[-102.18, 33.94], [-101.99, 33.98], [-101.84, 33.72], [-102.17, 33.67],
              [-102.18, 33.94]]]                                   # covers the site
POLY_1621 = [[[-102.08, 33.93], [-101.97, 33.95], [-101.84, 33.72], [-102.08, 33.68],
              [-102.08, 33.93]]]                                   # covers the site
POLY_1634 = [[[-102.04, 33.93], [-101.96, 33.94], [-101.9, 33.82], [-101.87, 33.77],
              [-102.0, 33.78], [-102.04, 33.93]]]                  # moved off the site
TXZ035 = {"type": "Polygon", "coordinates": [[  # real outline, 4 decimals
    [-101.5566, 33.3947], [-101.6254, 33.3946], [-101.7116, 33.3939], [-101.7504, 33.3933],
    [-101.8684, 33.392], [-101.8763, 33.3917], [-101.9814, 33.3906], [-102.0159, 33.3898],
    [-102.0676, 33.3896], [-102.0759, 33.3894], [-102.0782, 33.4688], [-102.0781, 33.4761],
    [-102.0783, 33.4802], [-102.0782, 33.4907], [-102.0785, 33.5006], [-102.0784, 33.5061],
    [-102.079, 33.5194], [-102.0804, 33.5789], [-102.0814, 33.6364], [-102.0834, 33.7228],
    [-102.0839, 33.7388], [-102.085, 33.7944], [-102.0857, 33.8215], [-102.0856, 33.8247],
    [-102.0557, 33.8249], [-101.8745, 33.827], [-101.8714, 33.8269], [-101.6396, 33.8299],
    [-101.5963, 33.8301], [-101.5636, 33.8306], [-101.5612, 33.6677], [-101.5601, 33.6096],
    [-101.5587, 33.5207], [-101.558, 33.4907], [-101.557, 33.4032], [-101.5566, 33.3947]]]}


def _rect(x0, y0, x1, y1):
    return {"type": "Polygon",
            "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


# TXZ034 (Hockley, west of the site) and the Amarillo Flood Watch zones (~210 km north,
# off the map) as their real bounding boxes
TXZ034 = _rect(-102.615295, 33.388111, -102.075897, 33.825111)
ZONES = {"TXZ035": TXZ035, "TXC303": TXZ035, "TXZ034": TXZ034,
         "TXZ001": _rect(-103.04, 36.06, -102.16, 36.5),
         "TXZ002": _rect(-102.16, 36.06, -101.62, 36.5)}
POINTS = {"properties": {"cwa": "LUB", "forecastZone": Z + "forecast/TXZ035",
                         "county": Z + "county/TXC303", "fireWeatherZone": Z + "fire/TXZ035"}}

SV_1611 = {
    "id": "urn:oid:eb6a907d.001.1", "type": "Feature",
    "geometry": {"type": "Polygon", "coordinates": POLY_1611}, "properties": {
        "id": "urn:oid:eb6a907d.001.1",
        "areaDesc": "Hale, TX; Hockley, TX; Lamb, TX; Lubbock, TX",
        "affectedZones": [Z + "county/TXC189", Z + "county/TXC219", Z + "county/TXC279",
                          Z + "county/TXC303"],
        "geocode": {"UGC": ["TXC189", "TXC219", "TXC279", "TXC303"]},
        "sent": "2026-09-21T16:11:00-05:00", "effective": "2026-09-21T16:11:00-05:00",
        "onset": "2026-09-21T16:11:00-05:00", "expires": "2026-09-21T17:00:00-05:00",
        "ends": "2026-09-21T17:00:00-05:00", "status": "Actual", "messageType": "Update",
        "severity": "Severe", "certainty": "Observed", "urgency": "Immediate",
        "event": "Severe Thunderstorm Warning", "senderName": "NWS Lubbock TX",
        "headline": "Severe Thunderstorm Warning issued September 21 at 4:11PM CDT until "
                    "September 21 at 5:00PM CDT by NWS Lubbock TX",
        "description": "At 411 PM CDT, a severe thunderstorm was located near Anton.",
        "instruction": "Prepare immediately for large hail and damaging winds.",
        "parameters": {
            "AWIPSidentifier": ["SVSLUB"],
            "NWSheadline": ["A SEVERE THUNDERSTORM WARNING REMAINS IN EFFECT UNTIL 500 PM"],
            "maxWindGust": ["70 MPH"], "thunderstormDamageThreat": ["CONSIDERABLE"],
            "VTEC": ["/O.CON.KLUB.SV.W.0263.000000T0000Z-260921T2200Z/"]}}}


def derive(base, *, cap_id=None, geometry="keep", zones=None, **props):
    """A copy of a real message with some properties changed (times given as epoch)."""
    f = copy.deepcopy(base)
    p = f["properties"]
    if cap_id:
        p["id"] = f["id"] = cap_id
    if geometry != "keep":
        f["geometry"] = geometry
    if zones is not None:
        p["affectedZones"] = [Z + z for z in zones]
        p["geocode"]["UGC"] = [z.rsplit("/", 1)[1] for z in zones]
    for k, v in props.items():
        if k == "vtec":
            p["parameters"]["VTEC"] = [v]
        elif k == "params":
            p["parameters"].update(v)
        elif k in ("sent", "onset", "effective", "ends", "expires") and isinstance(v, float):
            p[k] = iso(v)
        else:
            p[k] = v
    return f


def rename(base, event, code, **kw):
    """The same real message as another event: name, headline and VTEC phenomenon."""
    f = derive(base, **kw)
    p = f["properties"]
    p["headline"] = p["headline"].replace(p["event"], event)
    p["event"] = event
    p["parameters"]["VTEC"] = [v.replace(".SV.W.", ".%s." % code)
                               for v in p["parameters"]["VTEC"]]
    return f


CAN = "/O.CAN.KLUB.SV.W.0263.000000T0000Z-260921T2200Z/"
SV_1621 = derive(SV_1611, cap_id="urn:oid:7def1fb5.002.1", geometry={
    "type": "Polygon", "coordinates": POLY_1621}, zones=["county/TXC189", "county/TXC303"],
    sent=T1621, onset=T1621, areaDesc="Hale, TX; Lubbock, TX")
CAN_1621 = derive(SV_1621, cap_id="urn:oid:7def1fb5.001.1", messageType="Cancel", vtec=CAN,
                  zones=["county/TXC219", "county/TXC279"], areaDesc="Hockley, TX; Lamb, TX",
                  expires=NOW + 1480.0)
SV_1634 = derive(SV_1621, cap_id="urn:oid:6abbd2d5.001.1", geometry={
    "type": "Polygon", "coordinates": POLY_1634}, sent=T1634, onset=T1634)


def tor(base, **kw):
    return rename(base, "Tornado Warning", "TO.W", **kw)


# KBOX High Wind Warning (zone-based, geometry null) moved to Lubbock's zone TXZ035
HWW = {"id": "urn:oid:2.49.0.1.840.0.0533f3a2.002.1", "type": "Feature", "geometry": None,
       "properties": {
           "id": "urn:oid:2.49.0.1.840.0.0533f3a2.002.1", "areaDesc": "Lubbock",
           "affectedZones": [Z + "forecast/TXZ035"], "geocode": {"UGC": ["TXZ035"]},
           "sent": iso(NOW - 600), "effective": iso(NOW - 600), "onset": iso(NOW - 600),
           "expires": iso(NOW + 3 * 3600), "ends": iso(NOW + 6 * 3600), "status": "Actual",
           "messageType": "Alert", "severity": "Severe", "urgency": "Expected",
           "event": "High Wind Warning", "senderName": "NWS Lubbock TX",
           "headline": "High Wind Warning issued September 21 by NWS Lubbock TX",
           "description": "* WHAT...West winds 35 to 45 mph.",
           "instruction": "Secure loose objects.",
           "parameters": {"NWSheadline": ["HIGH WIND WARNING IN EFFECT UNTIL 10 PM CDT"],
                          "VTEC": ["/O.NEW.KLUB.HW.W.0003.000000T0000Z-260922T0300Z/"]}}}
# KGLD Dust Storm Warning, polygon shifted 5.5 deg south (over the site)
DS_POLY = [[[-102.3, 34.01], [-101.8, 34.03], [-101.78, 33.63], [-102.05, 33.63],
            [-102.05, 33.46], [-102.31, 33.46], [-102.3, 34.01]]]
DSW = derive(HWW, cap_id="urn:oid:ad43209f.001.1", zones=["county/TXC303"],
             geometry={"type": "Polygon", "coordinates": DS_POLY}, event="Dust Storm Warning",
             ends=NOW + 2280, vtec="/O.NEW.KLUB.DS.W.0002.260921T2110Z-260921T2150Z/")
# live 2026-09-24: Amarillo Flood Watch (zones trimmed to 2), Houston AQA (no VTEC)
FFA = derive(HWW, cap_id="urn:oid:2.49.0.1.840.0.38e3609b.001.1", event="Flood Watch",
             zones=["forecast/TXZ001", "forecast/TXZ002"], senderName="NWS Amarillo TX",
             areaDesc="Dallam; Sherman", messageType="Update", ends=NOW + 14 * 3600,
             vtec="/O.CON.KAMA.FA.A.0002.000000T0000Z-260925T1200Z/")
AQA = derive(HWW, cap_id="urn:oid:2.49.0.1.840.0.c56e0225.001.1", event="Air Quality Alert",
             zones=["forecast/TXZ213"], senderName="NWS Houston/Galveston TX", ends=None,
             params={"VTEC": [], "NWSheadline": ["Ozone Action Day"]})


def zone_alert(event, zones, code):
    """A zone-based message (the real HWW structure) for another event / zone set."""
    return derive(HWW, cap_id="urn:oid:z.%s.%s" % (event, "-".join(zones)), zones=zones,
                  event=event, headline="%s issued by NWS Lubbock TX" % event,
                  vtec="/O.NEW.KLUB.%s.0007.000000T0000Z-260922T0300Z/" % code)


# ---- harness
class FakeNWS:
    """Routes the module's _get(url, ua) like api.weather.gov; records every call."""

    def __init__(self):
        self.point, self.area, self.civil, self.down, self.calls = [], [], [], set(), []
        self.zones = dict(ZONES)

    def __call__(self, url, ua):
        assert ua, "NWS requires a User-Agent"
        self.calls.append(url)
        kind = ("point" if "?point=" in url else "area" if "?area=" in url else
                "civil" if "&event=" in url else
                "points" if "/points/" in url else "zones")
        if kind in self.down:
            raise urllib.error.URLError("offline (test)")
        if kind in ("point", "area", "civil"):
            return {"type": "FeatureCollection", "features": copy.deepcopy(getattr(self, kind))}
        if kind == "points":
            return copy.deepcopy(POINTS)
        zid = url.rsplit("/", 1)[1]
        if zid not in self.zones:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return {"id": url, "type": "Feature", "geometry": copy.deepcopy(self.zones[zid])}

    def n(self, kind):
        key = {"point": "?point=", "area": "?area=", "zones": "/zones/"}[kind]
        return sum(key in u for u in self.calls)


class Log:
    def __init__(self):
        self.events = []

    def record(self, action, **kw):
        self.events.append(dict(kw, action=action))

    def vetoes(self):
        return [e for e in self.events if e["action"] == "HAZARD-VETO"]


def make_cfg(tmp_path, **kw):
    cfg = types.SimpleNamespace(
        GEOCODE=SITE, NWS_USER_AGENT="ttu-test", LOCAL_TZ="America/Chicago",
        RADAR_THUMB_HALF_DEG=1.0, HAZARDS_ENABLED=True,
        HAZARD_VETO_EVENTS="Tornado Warning,Dust Storm Warning,High Wind Warning",
        HAZARD_POINT_POLL_SEC=60, HAZARD_AREA_POLL_SEC=120, HAZARD_STALE_AFTER_SEC=600,
        HAZARD_LATCH_FILE=str(tmp_path / "safety_hazard_latch.json"),
        HAZARD_CACHE_DIR=str(tmp_path / "ttu-hazards"))
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def env(tmp_path, monkeypatch):
    nws = FakeNWS()
    monkeypatch.setattr(na, "_get", nws)
    cfg, log = make_cfg(tmp_path), Log()
    return types.SimpleNamespace(nws=nws, cfg=cfg, log=log, tmp=tmp_path,
                                 new=lambda: na.NwsAlertsPoller(cfg, log))


def both(p, t):
    p.poll_point(t)
    p.poll_area(t)


def latch(env):
    with open(env.cfg.HAZARD_LATCH_FILE, encoding="utf-8") as f:
        return json.load(f)


def write_latch(env, items):
    with open(env.cfg.HAZARD_LATCH_FILE, "w", encoding="utf-8") as f:
        f.write(items if isinstance(items, str) else json.dumps(items))


# ---- the veto
def test_point_query_hit_vetoes_until_the_warning_ends(env):
    env.nws.point = [tor(SV_1611)]
    p = env.new()
    p.poll_point(NOW)
    c = p.component(NOW)
    assert c["safe"] is False and c["available"] is True and c["enabled"] is True
    (v,) = c["veto"]
    assert v["key"] == "KLUB.TO.W.0263" and v["event"] == "Tornado Warning"
    assert v["source"] == "point" and v["end_ts"] == END and v["end_local"] == "17:00 CDT"
    assert v["sender"] == "NWS Lubbock TX" and v["first_seen_ts"] == NOW
    assert c["reasons"] == ["NWS Tornado Warning in effect for the site until 17:00 CDT "
                            "(NWS Lubbock TX)"]
    (at,) = c["at_site"]
    assert at["vetoes"] is True and at["geometry_source"] == "polygon"
    (ev,) = env.log.vetoes()
    assert ev["result"] == "unsafe until 17:00 CDT" and ev["key"] == "KLUB.TO.W.0263"
    (saved,) = latch(env)
    assert {"key", "event", "headline", "sender", "end_ts", "first_seen_ts"} <= set(saved)
    assert saved["key"] == "KLUB.TO.W.0263" and saved["end_ts"] == END
    assert p.component(END - 1)["safe"] is False          # the whole duration...
    assert p.component(END + 1)["safe"] is True           # ...and not beyond it


def test_polygon_warning_is_at_the_site_by_its_polygon_only(env):
    # the real 16:34 update: polygon off the site, the site's own county still listed
    env.nws.area = [tor(SV_1634)]
    p = env.new()
    both(p, T1634 + 10)
    c = p.component(T1634 + 10)
    assert "TXC303 (county)" in c["site_zones"]
    assert c["safe"] is True and c["veto"] == [] and c["at_site"] == []
    (near,) = c["nearby"]
    assert near["event"] == "Tornado Warning" and near["veto_event"] is True
    assert near["vetoes"] is False and env.log.vetoes() == []


def test_local_polygon_test_vetoes_when_the_point_query_does_not_list_it(env):
    env.nws.area = [tor(SV_1611)]                      # point query lagging / empty
    p = env.new()
    both(p, NOW)
    assert p.component(NOW)["veto"][0]["source"] == "local"
    env.nws.point = [tor(SV_1611)]
    p.poll_point(NOW + 60)
    assert p.component(NOW + 60)["veto"][0]["source"] == "both"


def test_zone_based_high_wind_warning_vetoes_via_the_site_zone(env):
    env.nws.area = [HWW]                               # TXZ035, geometry null
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    (v,) = c["veto"]
    assert v["event"] == "High Wind Warning" and v["source"] == "local"
    assert c["at_site"][0]["geometry_source"] == "zones" and c["at_site"][0]["vetoes"]
    (ov,) = p.overlays(NOW)
    assert ov["vetoes"] is True and ov["kind"] == "alert" and ov["color"] == "#DAA520"
    assert na.point_in_geometry(SITE[1], SITE[0], ov["geometry"])


def test_zone_warning_for_a_neighbouring_zone_is_nearby_not_a_veto(env):
    env.nws.area = [zone_alert("High Wind Warning", ["forecast/TXZ034"], "HW.W")]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert c["safe"] is True and c["at_site"] == []
    assert [v["event"] for v in c["nearby"]] == ["High Wind Warning"]


def test_zone_types_are_matched_exactly(env):
    env.nws.area = [zone_alert("Red Flag Warning", ["fire/TXZ035"], "FW.W"),
                    zone_alert("Tornado Watch", ["county/TXC303"], "TO.A"),
                    zone_alert("Fire Weather Watch", ["fire/TXZ034"], "FW.A")]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert sorted(v["event"] for v in c["at_site"]) == ["Red Flag Warning", "Tornado Watch"]
    assert [v["event"] for v in c["nearby"]] == ["Fire Weather Watch"] and c["safe"]


def test_dust_storm_warning_polygon_vetoes(env):
    env.nws.point = [DSW]
    p = env.new()
    p.poll_point(NOW)
    assert p.component(NOW)["veto"][0]["event"] == "Dust Storm Warning"


def test_update_extends_the_veto_and_is_persisted(env):
    env.nws.point = [tor(SV_1611)]
    p = env.new()
    p.poll_point(NOW)
    env.nws.point = [tor(SV_1621, ends=END + 1800, expires=END + 1800)]
    p.poll_point(T1621 + 30)
    (v,) = p.component(T1621 + 30)["veto"]
    assert v["end_ts"] == END + 1800 and v["end_local"] == "17:30 CDT"
    last = env.log.vetoes()[-1]
    assert "updated" in last["reason"]
    assert last["result"] == "unsafe until 17:30 CDT (was 17:00 CDT)"
    assert latch(env)[0]["end_ts"] == END + 1800
    env.nws.area = [tor(SV_1611)]                      # an OLDER version never shortens it
    p.poll_area(T1621 + 40)
    assert p.component(T1621 + 40)["veto"][0]["end_ts"] == END + 1800
    assert p.component(END + 60)["safe"] is False


@pytest.mark.parametrize("area_first", [False, True])
def test_polygon_moved_off_the_site_releases_only_after_fresh_point_and_area(env, area_first):
    env.nws.point = env.nws.area = [tor(SV_1611)]
    p = env.new()
    both(p, T1621)
    assert p.component(T1621)["safe"] is False
    env.nws.point = []                                 # NWS: no longer in effect here
    env.nws.area = [tor(SV_1634)]                      # the Update moved the polygon
    first, second = (p.poll_area, p.poll_point) if area_first else (p.poll_point, p.poll_area)
    first(T1634 + 10)
    c = p.component(T1634 + 10)
    assert c["safe"] is False, "one fresh query released the veto"
    assert c["veto"][0]["source"] == "latched"
    second(T1634 + 40)
    c = p.component(T1634 + 40)
    assert c["safe"] is True and c["veto"] == []
    assert [v["event"] for v in c["nearby"]] == ["Tornado Warning"]   # still on the map
    rel = env.log.vetoes()[-1]
    assert rel["result"].startswith("released:") and "point and area" in rel["result"]
    assert latch(env) == []


def test_cancel_releases_and_a_cancelled_polygon_is_not_a_sighting(env):
    env.nws.point = env.nws.area = [tor(SV_1611)]
    p = env.new()
    both(p, NOW)
    # the real 16:21 Cancel segment still carries a polygon OVER the site
    assert na.point_in_geometry(SITE[1], SITE[0], CAN_1621["geometry"])
    env.nws.point, env.nws.area = [], [tor(CAN_1621)]
    p.poll_area(T1621 + 10)
    assert p.component(T1621 + 10)["safe"] is False
    p.poll_point(T1621 + 20)
    c = p.component(T1621 + 20)
    assert c["safe"] is True and c["at_site"] == [] and c["nearby"] == []


def test_terminal_vtec_actions_end_an_event_whatever_the_message_type(env):
    # live feed facts: EXP segments come as "Update", UPG ("has been replaced") as "Alert"
    for f in (tor(SV_1611, vtec="/O.EXP.KLUB.TO.W.0263.000000T0000Z-260921T2200Z/"),
              tor(SV_1611, messageType="Alert", expires=NOW - 3 * DAY,
                  vtec="/O.UPG.KLUB.TO.W.0263.000000T0000Z-260921T2200Z/"),
              tor(SV_1611, vtec="/T.NEW.KLUB.TO.W.0263.000000T0000Z-260921T2200Z/")):
        env.nws.point = [f]
        p = env.new()
        p.poll_point(NOW)
        c = p.component(NOW)
        assert c["safe"] is True and c["at_site"] == []


def test_feed_outage_holds_until_end_then_releases(env):
    env.nws.point = env.nws.area = [tor(SV_1611)]
    p = env.new()
    both(p, NOW)
    env.nws.down = {"point", "area", "points", "zones"}
    for t in range(int(NOW) + 60, int(END), 120):
        p.maybe_poll(float(t))
    c = p.component(END - 1)
    assert c["safe"] is False and c["available"] is False
    assert c["veto"][0]["source"] == "latched" and "offline" in c["error"]
    assert c["at_site"] == []                          # stale data is never shown as current
    c = p.component(END + 1)
    assert c["safe"] is True and c["veto"] == []
    assert env.log.vetoes()[-1]["result"] == "released: ended 17:00 CDT"


def test_no_end_time_holds_sixty_minutes_from_the_last_sighting(env):
    env.nws.point = [tor(SV_1611, ends=None, expires=None)]
    p = env.new()
    p.poll_point(NOW)
    env.nws.down = {"point", "area", "points"}
    c = p.component(NOW + 3599)
    assert c["safe"] is False and "no end time given" in c["reasons"][0]
    assert p.component(NOW + 3601)["safe"] is True


def test_a_warning_longer_than_the_stale_cap_is_never_released_mid_warning(env, monkeypatch):
    # the 72 h cap bounds a hold on STALE information; fresh sightings renew it, so a
    # long High Wind Warning never gets a SAFE gap when 72 h from its first sighting pass
    ends = NOW + 100 * 3600
    env.nws.area = [derive(HWW, ends=ends, expires=ends)]
    p = env.new()
    both(p, NOW)
    assert p.component(NOW)["veto"][0]["end_ts"] == NOW + na.MAX_VETO_SPAN_SEC   # capped
    writes = []
    real = na._atomic_write_json
    monkeypatch.setattr(na, "_atomic_write_json",
                        lambda path, obj: (writes.append(path), real(path, obj)))
    for h in (1, 2, 3):                                 # early sightings: no rewrite
        both(p, NOW + h * 3600)
    assert not [w for w in writes if w == env.cfg.HAZARD_LATCH_FILE]
    both(p, NOW + 40 * 3600)                           # past half the cap: renewed once
    assert [w for w in writes if w == env.cfg.HAZARD_LATCH_FILE] == [env.cfg.HAZARD_LATCH_FILE]
    env.nws.down = {"point", "area", "points", "zones"}
    c = p.component(NOW + 73 * 3600)
    assert c["safe"] is False and c["veto"][0]["end_ts"] == ends
    assert p.component(ends + 1)["safe"] is True


# ---- persistence, restart and the clock
def test_restart_restores_the_latch(env):
    t0 = time.time()
    env.nws.point = [tor(SV_1611, sent=t0 - 60, onset=t0 - 60, ends=t0 + 1800,
                         expires=t0 + 1800)]
    env.new().poll_point(t0)
    c = env.new().component(t0 + 5)                    # daemon restart, feeds not polled yet
    assert c["safe"] is False and c["veto"][0]["source"] == "latched"
    assert abs(c["veto"][0]["end_ts"] - (t0 + 1800)) < 1
    assert env.log.vetoes()[-1]["result"].startswith("restored")


def test_restart_clamps_a_future_dated_latch(env):
    # the 2026-08 incident: a latch read under a clock 158 days behind
    first = time.time() + 158 * DAY
    write_latch(env, [{"key": "KLUB.TO.W.0263", "event": "Tornado Warning",
                       "sender": "NWS Lubbock TX", "end_ts": first + 1800,
                       "first_seen_ts": first, "onset_ts": first - 60},
                      {"key": "KLUB.TO.W.0264", "event": "Tornado Warning", "end_ts": None,
                       "first_seen_ts": first}])
    c = env.new().component(time.time())
    assert c["safe"] is False and len(c["veto"]) == 2, "the restored veto went pending/away"
    v, no_end = c["veto"]                              # sorted by release time
    assert v["end_ts"] <= time.time() + 1800 + 5, "a wrong-clock veto survived unclamped"
    assert v["alert_end_ts"] == first + 1800           # the absolute NWS end is kept
    assert no_end["end_ts"] > time.time() + 3500       # no end: a full 60 min from now
    assert latch(env)[0]["shift"] < 0                  # re-persisted in our clock's frame


def test_restart_drops_a_veto_that_ended_while_down(env):
    t0 = time.time()
    write_latch(env, [{"key": "K.TO.W.1", "event": "Tornado Warning", "end_ts": t0 - 10,
                       "first_seen_ts": t0 - 1800}])
    assert env.new().component()["safe"] is True
    assert latch(env) == []


def test_unreadable_latch_fails_safe_until_fresh_queries_clear_it(env):
    write_latch(env, "{truncated")
    p = env.new()
    t = time.time()
    assert p.component(t)["safe"] is False
    both(p, t + 10)                                    # both feeds: nothing at the site
    assert p.component(t + 10)["safe"] is True


def test_backward_clock_step_keeps_the_veto_for_the_time_it_had_left(env):
    env.nws.point = [tor(SV_1611)]
    p = env.new()
    p.poll_point(NOW)
    pre, post = NOW + 60, NOW + 60 - 158 * DAY
    p.clock_stepped(pre, post)
    # its NWS onset/end look 158 days away: still IN EFFECT, but not for 158 days
    c = p.component(post + 10)
    assert c["safe"] is False and c["veto_pending"] == []
    assert c["veto"][0]["end_ts"] == post + (END - pre)
    assert latch(env)[0]["shift"] == post - pre
    calls = env.nws.n("point")
    p.maybe_poll(post + 20)                            # re-poll at once under the new clock
    assert env.nws.n("point") == calls + 1 and env.nws.n("area") == 1
    p.clock_stepped(post + 30, NOW + 90)               # back to the truth: NWS end rules
    assert p.component(NOW + 90)["veto"][0]["end_ts"] == END


def test_forward_clock_step_never_holds_beyond_the_alert_end(env):
    env.nws.point = [tor(SV_1611)]
    p = env.new()
    p.poll_point(NOW)
    p.clock_stepped(NOW + 60, END + 600)
    assert p.component(END + 601)["safe"] is True
    assert env.log.vetoes()[-1]["result"] == "released: ended 17:00 CDT"


def test_clock_step_requires_post_step_queries_for_an_early_release(env):
    env.nws.point = env.nws.area = [tor(SV_1611)]
    p = env.new()
    both(p, NOW)
    env.nws.point = env.nws.area = []
    p.clock_stepped(NOW + 30, NOW + 40)
    p.poll_point(NOW + 50)                             # only one post-step query so far
    assert p.component(NOW + 50)["safe"] is False
    p.poll_area(NOW + 60)
    assert p.component(NOW + 60)["safe"] is True


def test_latch_is_written_only_when_the_veto_set_changes(env, monkeypatch):
    writes = []
    real = na._atomic_write_json
    monkeypatch.setattr(na, "_atomic_write_json",
                        lambda path, obj: (writes.append(path), real(path, obj)))
    lpath = env.cfg.HAZARD_LATCH_FILE
    p = env.new()
    for t in range(3):
        both(p, NOW + 60 * t)
        p.component(NOW + 60 * t)
    assert writes.count(lpath) == 0, "no veto, no SD write"
    env.nws.point = [tor(SV_1611)]
    for t in range(3, 8):
        p.poll_point(NOW + 60 * t)
        p.component(NOW + 60 * t)
    assert writes.count(lpath) == 1
    env.nws.point = [tor(SV_1611, ends=END + 900)]     # extended: must be persisted
    p.poll_point(NOW + 600)
    assert writes.count(lpath) == 2
    p.component(END + 901)                             # released...
    assert writes.count(lpath) == 2, "no disk I/O on the IsSafe path"
    p._retry_persist()                                 # ...and written by the poll thread
    assert writes.count(lpath) == 3
    p._retry_persist()
    assert writes.count(lpath) == 3


# ---- information only / onset / configuration
INFO_EVENTS = [
    ("Severe Thunderstorm Warning", "SV.W"), ("Tornado Watch", "TO.A"),
    ("Severe Thunderstorm Watch", "SV.A"), ("Extreme Wind Warning", "EW.W"),
    ("Special Weather Statement", "XX.S"), ("Flash Flood Warning", "FF.W"),
    ("Flood Advisory", "FA.Y"), ("Snow Squall Warning", "SQ.W"),
    ("Blowing Dust Warning", "DU.W"), ("Dust Advisory", "DS.Y"), ("High Wind Watch", "HW.A"),
    ("Wind Advisory", "WI.Y"), ("Red Flag Warning", "FW.W"), ("Winter Storm Warning", "WS.W"),
    ("Extreme Heat Warning", "XH.W"), ("Air Quality Alert", "XX.S"), ("Fire Warning", "XX.W"),
    ("Evacuation Immediate", "XX.W"), ("Civil Emergency Message", "XX.S"),
]


@pytest.mark.parametrize("event,code", INFO_EVENTS)
def test_non_veto_events_over_the_site_are_information_only(env, event, code):
    env.nws.point = env.nws.area = [rename(SV_1611, event, code)]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert c["safe"] is True and c["veto"] == [] and c["veto_pending"] == []
    (at,) = c["at_site"]
    assert at["event"] == event and at["vetoes"] is False and at["veto_event"] is False
    assert env.log.vetoes() == [] and c["reasons"] == []
    assert all(not o["vetoes"] for o in p.overlays(NOW))
    assert not os.path.exists(env.cfg.HAZARD_LATCH_FILE)


@pytest.mark.parametrize("setting", ["  tornado   WARNING ,DUST storm warning",
                                     ("TORNADO WARNING",), ["Tornado warning"]])
def test_veto_event_config_is_case_insensitive(env, setting):
    env.cfg.HAZARD_VETO_EVENTS = setting
    env.nws.point = [tor(SV_1611)]
    p = env.new()
    p.poll_point(NOW)
    assert p.component(NOW)["safe"] is False


def test_veto_event_config_decides_which_events_veto(env):
    env.cfg.HAZARD_VETO_EVENTS = "Severe Thunderstorm Warning"
    env.nws.point = [SV_1611, DSW]
    p = env.new()
    p.poll_point(NOW)
    c = p.component(NOW)
    assert [v["event"] for v in c["veto"]] == ["Severe Thunderstorm Warning"]
    assert c["veto_events"] == ["Severe Thunderstorm Warning"]
    q = na.NwsAlertsPoller(make_cfg(env.tmp / "x", HAZARD_VETO_EVENTS=""), Log())
    env.nws.point = [tor(SV_1611)]
    q.poll_point(NOW)
    assert q.component(NOW)["safe"] is True            # empty list = information only


def test_default_veto_events_are_the_owners():
    names = ["Tornado Warning", "Dust Storm Warning", "High Wind Warning"]
    assert na.veto_event_names(types.SimpleNamespace(GEOCODE=SITE)) == names
    if getattr(config, "HAZARD_VETO_EVENTS", None) is not None:   # once config.py has it
        assert [n.lower() for n in na.veto_event_names(config)] == [n.lower() for n in names]


def test_warning_issued_ahead_vetoes_from_its_onset(env):
    onset = NOW + 12 * 3600
    env.nws.area = [derive(HWW, onset=onset, ends=onset + 8 * 3600)]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert c["safe"] is True and c["veto"] == []
    (pend,) = c["veto_pending"]
    assert pend["event"] == "High Wind Warning" and pend["onset_ts"] == onset
    assert c["at_site"][0]["veto_event"] is True and c["at_site"][0]["vetoes"] is False
    assert env.log.vetoes()[0]["result"].startswith("pending: unsafe from")
    lead = na.ONSET_LEAD_SEC
    assert p.component(onset - lead - 1)["safe"] is True
    c = p.component(onset - lead + 1)                  # feeds stale by now: the latch holds
    assert c["safe"] is False and c["veto"][0]["source"] == "latched"


def test_onset_uses_the_servers_clock_when_ours_is_behind(env):
    # our clock a day behind: NWS's own 'sent' stamps say the warning is in effect now
    env.nws.area = [HWW]
    p = env.new()
    both(p, NOW - DAY)
    c = p.component(NOW - DAY)
    assert c["safe"] is False and c["veto"][0]["event"] == "High Wind Warning"


# ---- what is listed
def test_excluded_products_are_never_listed(env):
    env.nws.point = env.nws.area = [
        rename(SV_1611, "Child Abduction Emergency", "XX.S", cap_id="u:1"),
        rename(SV_1611, "Blue Alert", "XX.S", cap_id="u:2"),
        rename(SV_1611, "Administrative Message", "XX.S", cap_id="u:3"),
        rename(SV_1611, "Test Message", "XX.S", cap_id="u:4"),
        tor(SV_1611, cap_id="u:5", status="Test"), tor(SV_1611, cap_id="u:6", status="Exercise"),
        zone_alert("Small Craft Advisory", ["forecast/ANZ480"], "SC.Y"),
        zone_alert("Gale Warning", ["forecast/TXZ035"], "GL.W")]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert c["at_site"] == [] and c["nearby"] == [] and c["safe"] is True


def test_far_alerts_are_not_listed_and_their_zones_are_cached_as_boxes(env):
    env.nws.area = [FFA, AQA]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert c["at_site"] == [] and c["nearby"] == [] and p.overlays(NOW) == []
    with open(env.tmp / "ttu-hazards" / "zones" / "forecast_TXZ001.json") as f:
        z = json.load(f)
    assert z["geometry"] is None and z["bbox"] == [-103.04, 36.06, -102.16, 36.5]


def test_vtec_key_includes_the_office_and_updates_share_it():
    lub = na.normalize(SV_1611)
    maf = na.normalize(derive(SV_1611, vtec="/O.NEW.KMAF.SV.W.0263.260921T2243Z-260921T2330Z/"))
    assert lub["key"] == "KLUB.SV.W.0263" and maf["key"] == "KMAF.SV.W.0263"
    assert na.normalize(SV_1634)["key"] == lub["key"]
    assert na.normalize(AQA)["key"] == AQA["properties"]["id"]     # no VTEC: the CAP id


def test_concurrent_segments_of_one_key_are_one_entry(env):
    # the live feed splits one VTEC event over several messages (58 of 328 keys)
    env.nws.area = [tor(SV_1611), tor(SV_1634, cap_id="urn:oid:segment2", sent=T1611)]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert [v["key"] for v in c["at_site"]] == ["KLUB.TO.W.0263"] and c["nearby"] == []
    (ov,) = p.overlays(NOW)
    assert len(list(na.iter_polygons(ov["geometry"]))) == 2


def test_alert_view_and_component_follow_the_contract(env):
    long = derive(SV_1611, cap_id="urn:oid:long", description="x" * 5000,
                  instruction="y" * 5000, params={"thunderstormDamageThreat": ["DESTRUCTIVE"]},
                  areaDesc="; ".join("County %d, TX" % i for i in range(60)))
    env.nws.area = [tor(SV_1611, params={"tornadoDamageThreat": ["CATASTROPHIC"]}), long,
                    zone_alert("Wind Advisory", ["forecast/TXZ035"], "WI.Y"),
                    zone_alert("Tornado Watch", ["county/TXC303"], "TO.A")]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert set(c) >= {"safe", "enabled", "available", "veto_events", "veto", "at_site",
                      "nearby", "counts", "point_age_s", "area_age_s", "error", "source"}
    assert set(c) == set(na.unavailable_component(env.cfg))
    assert c["counts"] == {"at_site": 4, "nearby": 0}
    assert set(c["veto"][0]) >= {"key", "event", "headline", "sender", "end_ts", "end_local",
                                 "first_seen_ts", "source"}
    view_keys = {"key", "event", "kind", "severity", "urgency", "headline", "nws_headline",
                 "sender", "area_desc", "onset", "ends", "expires", "end_local", "color",
                 "threat", "vetoes", "geometry_source", "description", "instruction"}
    for v in c["at_site"]:
        assert set(v) >= view_keys
        assert len(v["area_desc"]) <= 300 and len(v["description"]) <= 2000
        assert v["instruction"] is None or len(v["instruction"]) <= 1000
        assert len(v["color"]) == 7 and int(v["color"][1:], 16) >= 0
    # vetoing first, then warnings before watches before advisories
    assert [v["event"] for v in c["at_site"]] == [
        "Tornado Warning", "Severe Thunderstorm Warning", "Tornado Watch", "Wind Advisory"]
    assert [v["kind"] for v in c["at_site"]] == ["warning", "warning", "watch", "advisory"]
    assert [v["threat"] for v in c["at_site"][:2]] == ["TORNADO EMERGENCY", "DESTRUCTIVE"]
    for o in p.overlays(NOW):
        assert set(o) >= {"kind", "key", "event", "color", "geometry", "rank", "vetoes"}


def test_overlays_draw_watches_first_and_the_veto_last(env):
    env.nws.area = [zone_alert("Tornado Watch", ["county/TXC303"], "TO.A"),
                    rename(SV_1634, "Severe Thunderstorm Warning", "SV.W"),
                    tor(SV_1611, vtec="/O.NEW.KLUB.TO.W.0009.260921T2111Z-260921T2200Z/")]
    p = env.new()
    both(p, NOW)
    ov = p.overlays(NOW)
    assert [(o["event"], o["vetoes"]) for o in ov] == [
        ("Tornado Watch", False), ("Severe Thunderstorm Warning", False),
        ("Tornado Warning", True)]
    assert ov[0]["rank"] > ov[1]["rank"]               # lower rank = drawn later / on top


def test_a_held_veto_stays_on_the_map_while_the_feeds_are_down(env):
    env.nws.point = env.nws.area = [tor(SV_1611)]
    p = env.new()
    both(p, NOW)
    env.nws.down = {"point", "area"}
    p.maybe_poll(NOW + 700)
    (ov,) = p.overlays(NOW + 700)
    assert ov["vetoes"] is True and ov["key"] == "KLUB.TO.W.0263"


# ---- zones, areas, availability, cadence
def test_zone_outline_is_fetched_once_and_cached_on_disk(env):
    env.nws.area = [HWW]
    p = env.new()
    both(p, NOW)
    p.poll_area(NOW + 120)
    assert env.nws.n("zones") == 1
    env.new().poll_area(NOW + 240)                     # restart: from disk, not the network
    assert env.nws.n("zones") == 1
    with open(env.tmp / "ttu-hazards" / "zones" / "forecast_TXZ035.json") as f:
        assert json.load(f)["geometry"]["type"] == "MultiPolygon"


def test_geometry_collection_zone_outline_is_handled(env):
    env.nws.zones["TXZ035"] = {"type": "GeometryCollection", "geometries": [
        TXZ035, {"type": "MultiPolygon", "coordinates": [TXZ034["coordinates"]]}]}
    env.nws.area = [HWW]
    p = env.new()
    both(p, NOW)
    (ov,) = p.overlays(NOW)
    assert len(list(na.iter_polygons(ov["geometry"]))) == 2
    assert p.component(NOW)["safe"] is False


def test_zone_fetching_is_budgeted_and_continues_next_poll(env, monkeypatch):
    monkeypatch.setattr(na, "ZONE_FETCH_MAX", 1)
    env.nws.area = [zone_alert("Wind Advisory", ["forecast/TXZ034", "county/TXC303"], "WI.Y")]
    p = env.new()
    both(p, NOW)
    assert env.nws.n("zones") == 1
    assert p.component(NOW)["at_site"][0]["geometry_source"] is None   # listed, not drawn
    p.poll_area(NOW + 120)
    assert env.nws.n("zones") == 2
    assert p.component(NOW + 120)["at_site"][0]["geometry_source"] == "zones"


def test_simplify_keeps_the_shape_of_a_dense_outline():
    ring = [[-101.958 + 0.3 * math.cos(2 * math.pi * i / 12000),
             33.748 + 0.3 * math.sin(2 * math.pi * i / 12000)] for i in range(12000)]
    t0 = time.time()
    g = na.simplify_geometry({"type": "Polygon", "coordinates": [ring + ring[:1]]})
    assert time.time() - t0 < 2.0
    assert 20 < len(g["coordinates"][0][0]) < 1000
    assert na.point_in_geometry(-101.958, 33.748, g)


def test_area_states_auto_cover_the_map(env):
    assert na.area_codes(env.cfg) == ["NM", "OK", "TX"]
    env.new().poll_area(NOW)
    # only Actual alerts and their updates cross the network (a Cancel is dropped anyway)
    assert ("https://api.weather.gov/alerts/active?area=NM,OK,TX&status=actual"
            "&message_type=alert,update") in env.nws.calls
    env.cfg.HAZARD_AREA_STATES = "tx, nm"
    assert na.area_codes(env.cfg) == ["NM", "TX"]
    env.nws.calls = []
    env.new().poll_point(NOW)
    assert env.nws.calls == ["https://api.weather.gov/alerts/active?point=33.748,-101.958"
                             "&status=actual&message_type=alert,update"]


def test_site_outside_nws_coverage_or_disabled(env):
    env.cfg.GEOCODE = (48.2, 16.4)                     # Vienna
    p = env.new()
    assert p.maybe_poll(NOW) is None and env.nws.calls == []
    c = p.component(NOW)
    assert c["enabled"] is False and c["safe"] is True and c["available"] is False
    assert [e["action"] for e in env.log.events] == ["CONFIG"]
    env.cfg.GEOCODE, env.cfg.HAZARDS_ENABLED = SITE, False    # disabled by config
    assert p.maybe_poll(NOW) is None and env.nws.calls == []
    assert p.component(NOW)["enabled"] is False


def test_availability_and_staleness(env):
    p = env.new()
    c = p.component(NOW)
    assert c["available"] is False and c["safe"] is True and c["point_age_s"] is None
    env.nws.area = [zone_alert("Wind Advisory", ["forecast/TXZ035"], "WI.Y")]
    env.nws.down = {"point"}
    p.maybe_poll(NOW)
    c = p.component(NOW + 30)
    assert c["available"] is True and c["area_age_s"] == 30 and "point query" in c["error"]
    assert len(c["at_site"]) == 1
    c = p.component(NOW + 601)                         # both stale: unknown, not unsafe
    assert c["available"] is False and c["safe"] is True and c["at_site"] == []


def test_unavailable_component_never_vetoes():
    c = na.unavailable_component(types.SimpleNamespace(GEOCODE=SITE))
    assert c["safe"] is True and c["enabled"] is False and c["available"] is False
    assert c["veto"] == [] and c["veto_events"][0] == "Tornado Warning"


def test_maybe_poll_cadence_and_backward_step(env):
    p = env.new()
    for t, expect in ((NOW, (1, 1)), (NOW + 30, (1, 1)), (NOW + 60, (2, 1)),
                      (NOW + 120, (3, 2)), (NOW - 1000, (4, 3))):   # last: clock stepped back
        p.maybe_poll(t)
        assert (env.nws.n("point"), env.nws.n("area")) == expect, t - NOW


def test_works_with_the_real_config_module(env, monkeypatch):
    # getattr defaults: works whether or not config.py has the hazard names yet
    monkeypatch.setattr(config, "HAZARD_LATCH_FILE", str(env.tmp / "l.json"), raising=False)
    monkeypatch.setattr(config, "HAZARD_CACHE_DIR", str(env.tmp / "c"), raising=False)
    monkeypatch.setattr(config, "GEOCODE", SITE)
    env.nws.point = [tor(SV_1611)]
    p = na.NwsAlertsPoller(config, env.log)
    p.poll_point(NOW)
    assert p.component(NOW)["safe"] is False


# ---- colours
def _green(hexcolor):
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    return 70 <= h * 360 <= 170 and s >= 0.2 and v >= 0.2


def test_hazards_are_never_drawn_green():
    # the owner's rule: green reads as "good" (and is the radar's 15-35 dBZ colour)
    assert {k: v for k, v in na.EVENT_COLORS.items() if _green(v)} == {}
    assert not any(_green(v) for v in na.KIND_COLORS.values())
    for ev in ("Flood Watch", "Flood Warning", "Flood Advisory", "Flash Flood Warning"):
        r, g, b = (int(na.color_for(ev)[i:i + 2], 16) for i in (1, 3, 5))
        assert r > g and r > b, ev                     # flood products are red
    assert na.color_for("tornado warning") == "#FF0000"
    assert na.color_for("Brand New Hazard Warning") == na.KIND_COLORS["warning"]


def test_civil_products_rank_as_their_eas_level_and_do_not_share_flood_colours():
    # an Evacuation Immediate is an order to act NOW: sorted and drawn with the warnings,
    # not after the Special Weather Statements (and not the first to fall off a list)
    assert na.event_kind("Evacuation Immediate") == "warning"
    assert na.event_kind("civil emergency message") == "warning"
    assert na.event_kind("Local Area Emergency") == "statement"
    assert na.event_kind("911 Telephone Outage") == "statement"
    assert (na.alert_rank("warning", "Unknown")
            < na.alert_rank(na.event_kind("Flood Advisory"), "Minor"))
    # a hazmat shelter order must not look like a minor flood on the map
    assert na.color_for("Shelter In Place Warning") != na.color_for("Flood Advisory")
    assert not _green(na.color_for("Shelter In Place Warning"))


# ---- review fixes: latch persistence --------------------------------------------------
def test_unwritable_latch_never_writes_or_logs_on_the_issafe_path(env, monkeypatch, caplog):
    blocker = env.tmp / "blocker"                      # a FILE where the directory should be
    blocker.write_text("")
    env.cfg.HAZARD_LATCH_FILE = str(blocker / "safety_hazard_latch.json")
    writes = []
    real = na._atomic_write_json
    monkeypatch.setattr(na, "_atomic_write_json",
                        lambda path, obj: (writes.append(path), real(path, obj)))
    mono = [1000.0]
    monkeypatch.setattr(na.time, "monotonic", lambda: mono[0])
    env.nws.point = [tor(SV_1611)]
    p = env.new()
    with caplog.at_level("INFO", logger="ttu.safety.hazards"):
        p.poll_point(NOW)                              # the veto arms; its write fails
        assert len(writes) == 1
        for i in range(20):                            # 20 IsSafe evaluations
            c = p.component(NOW + 1 + i)
        assert len(writes) == 1, "component() must not touch the SD card"
        assert c["safe"] is False and "latch file not written" in c["error"]
        p._retry_persist()                             # poll thread, within the back-off
        assert len(writes) == 1
        mono[0] += na.PERSIST_RETRY_SEC
        p._retry_persist()                             # after it: one retry, not logged again
        assert len(writes) == 2
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1 and errors[0].exc_info is None
        mono[0] += na.PERSIST_FAIL_LOG_SEC
        p._retry_persist()                             # the same error, 10 min on: once more
        assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 2
        blocker.unlink()                               # the card is writable again
        mono[0] += na.PERSIST_RETRY_SEC
        p._retry_persist()
    assert json.loads((blocker / "safety_hazard_latch.json").read_text())[0]["key"] \
        == "KLUB.TO.W.0263"
    assert "written again" in caplog.text
    assert p.component(NOW + 30)["error"] is None


def test_restarts_never_renew_the_hold_of_a_veto_without_an_end_time(env, monkeypatch):
    # a crash / reboot loop during an api.weather.gov outage (internet up, so the
    # connectivity watchdog stays out of it): the hold runs from the LAST SIGHTING
    t0 = time.time()
    env.nws.point = [tor(SV_1611, sent=t0 - 60, onset=t0 - 60, ends=None, expires=None)]
    env.new().poll_point(t0)
    assert latch(env)[0]["last_seen_ts"] == t0
    env.nws.down = {"point", "area", "points", "zones", "civil"}
    holds = []
    for k in (1, 2):
        now = t0 + k * 3000                            # a restart every 50 min
        monkeypatch.setattr(na.time, "time", lambda now=now: now)
        c = env.new().component(now)
        holds.append((c["safe"], c["veto"][0]["end_ts"] if c["veto"] else None))
    assert holds == [(False, t0 + 3600), (True, None)]
    assert latch(env) == []


def test_restarts_never_renew_the_unreadable_latch_placeholder(env, monkeypatch):
    write_latch(env, "")                               # 0-byte file: a brown-out mid-write
    env.nws.down = {"point", "area", "points", "zones", "civil"}
    t0 = time.time()
    seen = []
    for k in range(3):
        now = t0 + k * 3000
        monkeypatch.setattr(na.time, "time", lambda now=now: now)
        p = env.new()
        p.maybe_poll(now)                              # feeds down: nothing confirmed
        seen.append(p.component(now)["safe"])
    assert seen == [False, False, True], "held 60 min from arming, not per restart"


def test_a_re_sighted_no_end_veto_is_rewritten_at_most_every_quantum(env, monkeypatch):
    writes = []
    real = na._atomic_write_json
    monkeypatch.setattr(na, "_atomic_write_json",
                        lambda path, obj: (writes.append(obj), real(path, obj)))
    env.nws.point = [tor(SV_1611, ends=None, expires=None)]
    p = env.new()
    for m in range(31):                                # re-sighted every minute for 30 min
        p.poll_point(NOW + 60 * m)
    lw = [w for w in writes if isinstance(w, list)]
    assert 1 < len(lw) <= 1 + 30 * 60 // na.NO_END_SEEN_QUANTUM_SEC + 1
    assert lw[-1][0]["last_seen_ts"] >= NOW + 1800 - na.NO_END_SEEN_QUANTUM_SEC


def test_restored_last_sighting_in_the_future_counts_as_now(env):
    now = time.time()
    write_latch(env, [{"key": "K.TO.W.9", "event": "Tornado Warning", "end_ts": None,
                       "first_seen_ts": now - 600, "last_seen_ts": now + 5 * DAY}])
    (v,) = env.new().component(now)["veto"]
    assert now + 3500 < v["end_ts"] <= now + 3600 + 5


# ---- review fixes: area states, response cap, civil messages ----------------------------
def test_explicit_area_states_are_validated_so_a_typo_cannot_break_the_query(env, caplog):
    na._area_warned.clear()
    env.cfg.HAZARD_AREA_STATES = "TX,NW,OK"            # 'NW' for 'NM': the API says 400
    with caplog.at_level("WARNING", logger="ttu.safety.hazards"):
        assert na.area_codes(env.cfg) == ["OK", "TX"]
    assert "ignoring unknown code(s) NW" in caplog.text
    env.cfg.HAZARD_AREA_STATES = "NW,ZZ"               # nothing valid: auto
    assert na.area_codes(env.cfg) == ["NM", "OK", "TX"]
    env.cfg.HAZARD_AREA_STATES = "NM"                  # the site's state is always queried
    assert na.area_codes(env.cfg) == ["NM", "OK", "TX"]
    env.cfg.HAZARD_AREA_STATES = "tx"
    assert na.area_codes(env.cfg) == ["TX"]


def test_config_drops_unknown_area_codes_loudly(monkeypatch):
    monkeypatch.setattr(config, "CONFIG_WARNINGS", [])
    assert config._parse_states("tx, NW ,ok") == "TX,OK"
    assert "NW" in config.CONFIG_WARNINGS[0]
    assert config._parse_states("NW") == "auto"
    assert config.US_AREA_CODES == frozenset(na.STATE_BOXES)


class _Resp:
    def __init__(self, body, enc=None):
        self._b = io.BytesIO(body)
        self.headers = {"Content-Encoding": enc} if enc else {}

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_get_requests_gzip_and_caps_every_body(monkeypatch):
    seen = {}

    def fake(body, enc=None):
        def urlopen(req, timeout):
            seen["headers"] = dict(req.header_items())
            return _Resp(body, enc)
        monkeypatch.setattr(na.urllib.request, "urlopen", urlopen)

    doc = {"type": "FeatureCollection", "features": []}
    fake(gzip.compress(json.dumps(doc).encode()), "gzip")
    assert na._get("https://api.weather.gov/x", "ua") == doc
    assert seen["headers"]["Accept-encoding"] == "gzip"
    fake(json.dumps(doc).encode())                     # a server ignoring the request
    assert na._get("https://api.weather.gov/x", "ua") == doc
    fake(b" " * 2048)
    with pytest.raises(ValueError, match="larger than"):
        na._get("https://api.weather.gov/x", "ua", max_bytes=1024)
    fake(gzip.compress(b" " * 4096), "gzip")           # a (tiny) gzip bomb
    with pytest.raises(ValueError, match="inflates beyond"):
        na._get("https://api.weather.gov/x", "ua", max_bytes=1024)
    assert na.MAX_BODY_BYTES >= 8 * 1024 * 1024        # never cuts a real zone / area answer


def _ipaws(event, same, geometry=None, cap_id=None, sender="Lubbock County OEM"):
    """A civil message as IPAWS relays it: no affectedZones, no UGC, only SAME (the real
    structure of the Ruidoso Local Area Emergencies and the LA County CEM, 2026-09)."""
    return {"id": cap_id or "urn:oid:ipaws.%s.%s" % (event, same), "type": "Feature",
            "geometry": geometry, "properties": {
                "id": cap_id or "urn:oid:ipaws.%s.%s" % (event, same),
                "areaDesc": "Lubbock County", "affectedZones": [],
                "geocode": {"SAME": [same], "UGC": []},
                "sent": iso(NOW - 300), "effective": iso(NOW - 300), "onset": iso(NOW - 300),
                "expires": iso(NOW + 3 * 3600), "status": "Actual", "messageType": "Alert",
                "severity": "Extreme", "urgency": "Immediate", "event": event,
                "senderName": sender, "headline": "%s for Lubbock County" % event,
                "description": "A chemical release near the site.", "parameters": {}}}


def test_ipaws_civil_messages_are_listed_and_drawn(env):
    env.nws.civil = [
        _ipaws("Shelter In Place Warning", "048303"),              # Lubbock County: SAME only
        _ipaws("Civil Emergency Message", "006037"),               # Los Angeles: far away
        _ipaws("Local Area Emergency", "035027", geometry={        # Ruidoso: polygon, off map
            "type": "Polygon", "coordinates": [[[-105.7, 33.3], [-105.6, 33.3],
                                                [-105.6, 33.4], [-105.7, 33.3]]]}),
    ]
    p = env.new()
    both(p, NOW)
    c = p.component(NOW)
    assert c["safe"] is True and c["error"] is None      # civil events are information only
    (at,) = c["at_site"]
    assert at["event"] == "Shelter In Place Warning" and at["kind"] == "warning"
    assert at["geometry_source"] == "zones" and at["vetoes"] is False
    (ov,) = p.overlays(NOW)
    assert ov["event"] == "Shelter In Place Warning"
    assert na.point_in_geometry(SITE[1], SITE[0], ov["geometry"])
    zone_calls = [u for u in env.nws.calls if "/zones/" in u]
    assert zone_calls == ["https://api.weather.gov/zones/county/TXC303"]   # no CA/NM fetch
    assert any("&event=Civil%20Emergency%20Message,Evacuation%20Immediate," in u
               for u in env.nws.calls)


def test_a_civil_message_relayed_by_nws_is_not_listed_twice(env):
    msg = _ipaws("Evacuation Immediate", "048303")
    env.nws.area = [msg]
    env.nws.civil = [msg]
    p = env.new()
    both(p, NOW)
    assert [a["event"] for a in p.component(NOW)["at_site"]] == ["Evacuation Immediate"]


def test_a_civil_veto_is_not_released_by_an_area_answer_blind_to_it(env):
    # an operator may add a civil event to the veto list; the area answer only sees IPAWS
    # messages through the civil query, so its silence proves nothing while that is down
    env.cfg.HAZARD_VETO_EVENTS = "Tornado Warning,Shelter In Place Warning"
    msg = _ipaws("Shelter In Place Warning", "048303")
    env.nws.point, env.nws.civil = [msg], [msg]
    p = env.new()
    both(p, NOW)
    assert p.component(NOW)["veto"][0]["source"] == "both"
    env.nws.point, env.nws.civil = [], []
    env.nws.down = {"civil"}
    both(p, NOW + 120)
    c = p.component(NOW + 120)
    assert c["safe"] is False and "civil-message query" in c["error"]
    env.nws.down = set()
    both(p, NOW + 240)                                   # now the area answer can see it
    assert p.component(NOW + 240)["safe"] is True


def test_same_codes_map_to_county_zones():
    assert na._same_county("048303") == "TXC303"
    assert na._same_county("135027") == "NMC027"         # a part-of-county code still counts
    assert na._same_county("048000") is None and na._same_county("99999") is None
    assert na._same_county("998303") is None


def test_component_publishes_its_own_freshness(env):
    env.cfg.HAZARD_STALE_AFTER_SEC = 300
    p = env.new()
    both(p, NOW)
    c = p.component(NOW + 200)
    assert (c["point_fresh"], c["area_fresh"], c["stale_after_s"]) == (True, True, 300)
    c = p.component(NOW + 301)
    assert (c["point_fresh"], c["area_fresh"], c["available"]) == (False, False, False)
    assert na.unavailable_component(env.cfg)["area_fresh"] is False
