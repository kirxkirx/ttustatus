"""NWS alerts hardening (safety/nws_alerts.py; SafetyMonitor._held_vetoes):

  * a veto that cannot be confirmed gone is released 24 h after its last sighting, whatever
    its end time (UNSEEN_RELEASE_SEC), in the poller and in the monitor's carried vetoes;
  * a polygon that cannot be read places the alert by its zones; a feature that cannot be
    read at all is kept as a stub that vetoes (point) or blinds the answer (area), so the
    silence of a query that skipped it never releases a held veto;
  * one alert's or one zone's bad shape never fails a whole poll;
  * zone replies must name the zone asked for and be no larger than a zone; a zone without
    an outline is asked for again after a day;
  * feed text is scrubbed (lone surrogates, bidi controls), times read alike on every
    Python, and every reply is inflated stepwise and read within a deadline.

The fixtures and the fake api.weather.gov are those of test_nws_alerts.py.
"""
import copy
import gzip
import io
import json
import time
import types

import pytest

from safety import netio
from safety import nws_alerts as na
from safety.monitor import HAZARD_UNSEEN_RELEASE_SEC, SafetyMonitor
from safety.tests.test_nws_alerts import (
    DAY, END, FFA, HWW, NOW, SITE, SV_1611, FakeNWS, Log, Z, _rect, both, derive, latch,
    make_cfg, tor, write_latch)

BOUND = na.UNSEEN_RELEASE_SEC + na.CAP_RENEW_SEC     # the cap: 24-25 h after a sighting


@pytest.fixture
def aenv(tmp_path, monkeypatch):
    nws = FakeNWS()
    monkeypatch.setattr(na, "_get", nws)
    monkeypatch.setattr(na, "_warned_once", set())
    cfg, log = make_cfg(tmp_path), Log()
    return types.SimpleNamespace(nws=nws, cfg=cfg, log=log, tmp=tmp_path,
                                 new=lambda: na.NwsAlertsPoller(cfg, log))


def _bad_ring(poly):
    """The polygon with one position replaced by a bare number (not a [lon, lat] pair)."""
    ring = copy.deepcopy(poly[0])
    ring[1] = 5
    return {"type": "Polygon", "coordinates": [ring]}


def _string_coords(poly):
    return {"type": "Polygon", "coordinates": [[[str(x), str(y)] for x, y in poly[0]]]}


def _unreadable(feature, event="keep"):
    """A feature normalize cannot read (affectedZones not a list of URLs: iterating an int
    raises), optionally with its event name unreadable too."""
    f = copy.deepcopy(feature)
    f["properties"]["affectedZones"] = 5
    if event != "keep":
        f["properties"]["event"] = event
    return f


# ---- A4: released 24 h after the last sighting -----------------------------------------
def test_a_veto_not_sighted_for_24_h_is_released_whatever_its_end_time(aenv):
    ends = NOW + 3 * DAY
    aenv.nws.area = [derive(HWW, ends=ends, expires=ends)]
    p = aenv.new()
    both(p, NOW)
    aenv.nws.down = {"point", "area", "points", "zones"}
    for t in range(int(NOW) + 3600, int(NOW + na.UNSEEN_RELEASE_SEC), 3600):
        p.maybe_poll(float(t))
    c = p.component(NOW + na.UNSEEN_RELEASE_SEC)        # 24 h on: never sooner
    assert c["safe"] is False and c["veto"][0]["source"] == "latched"
    assert c["veto"][0]["end_ts"] == NOW + BOUND and c["veto"][0]["alert_end_ts"] == ends
    assert p.component(NOW + BOUND)["safe"] is True     # ... and never beyond 25 h
    ev = aenv.log.vetoes()[-1]
    assert ev["result"] == "released: not sighted for 24 h, could not be confirmed gone"
    p.maybe_poll(NOW + BOUND + 1)                       # the poll thread writes the latch
    assert latch(aenv) == []


def test_resightings_keep_a_long_warning_vetoing(aenv):
    ends = NOW + 2 * DAY
    aenv.nws.point = [derive(HWW, ends=ends, expires=ends)]
    p = aenv.new()
    for h in range(0, 48, 2):                           # listed at the site all along
        p.poll_point(NOW + h * 3600)
        assert p.component(NOW + h * 3600 + 1)["safe"] is False, h
    assert not [e for e in aenv.log.vetoes() if e["result"].startswith("released")]
    assert p.component(ends + 1)["safe"] is True
    assert aenv.log.vetoes()[-1]["result"].startswith("released: ended")


def test_restart_bounds_the_restored_veto_by_its_last_sighting(aenv):
    t = time.time()
    # written before UNSEEN_RELEASE_SEC: a 72 h cap from the first sighting
    write_latch(aenv, [{"key": "KLUB.HW.W.0003", "event": "High Wind Warning",
                        "end_ts": t + 3 * DAY, "first_seen_ts": t - 30 * 3600,
                        "last_seen_ts": t - 26 * 3600, "cap_ts": t + 42 * 3600},
                       {"key": "KLUB.HW.W.0004", "event": "High Wind Warning",
                        "end_ts": t + 3 * DAY, "first_seen_ts": t - 30 * 3600,
                        "last_seen_ts": t - 20 * 3600, "cap_ts": t + 42 * 3600}])
    c = aenv.new().component(t)
    (v,) = c["veto"]                                    # 0003: not seen for 26 h -> gone
    assert v["key"] == "KLUB.HW.W.0004"
    assert abs(v["end_ts"] - (t - 20 * 3600 + BOUND)) < 1
    assert {it["key"] for it in latch(aenv)} == {"KLUB.HW.W.0004"}


def test_restart_of_an_old_latch_without_a_last_sighting_holds_at_most_25_h(aenv):
    t = time.time()
    write_latch(aenv, [{"key": "KLUB.HW.W.0003", "event": "High Wind Warning",
                        "end_ts": t + 3 * DAY, "first_seen_ts": t - 3600}])
    (v,) = aenv.new().component(t)["veto"]
    assert t + BOUND - 5 < v["end_ts"] <= t + BOUND + 1


def test_a_clock_step_keeps_the_time_left_of_the_unseen_bound(aenv):
    ends = NOW + 3 * DAY
    aenv.nws.point = [derive(HWW, ends=ends, expires=ends)]
    p = aenv.new()
    p.poll_point(NOW)
    pre, post = NOW + 600, NOW + 600 - 158 * DAY
    p.clock_stepped(pre, post)                          # backward: not renewed, kept
    assert p.component(post + 10)["veto"][0]["end_ts"] == post + (NOW + BOUND - pre)
    p.clock_stepped(post + 10, NOW + 610)               # forward again
    assert p.component(NOW + 620)["veto"][0]["end_ts"] == NOW + BOUND
    assert p.component(NOW + BOUND + 1)["safe"] is True


def test_carried_vetoes_of_a_broken_layer_end_24_h_after_its_last_report(env, write_inputs):
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)

    class Hz:
        comp = None

        def component(self, now=None):
            if isinstance(self.comp, BaseException):
                raise self.comp
            return self.comp

    hz = Hz()
    t = time.time()
    hz.comp = {"safe": False, "enabled": True, "available": True, "veto_events": [],
               "veto": [{"key": "KLUB.HW.W.1", "event": "High Wind Warning",
                         "end_ts": t + 3 * DAY, "end_local": "x", "source": "both"}],
               "at_site": [], "nearby": [], "counts": {}, "error": None}
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], hazards=hz)
    assert m.evaluate()["is_safe"] is False
    seen = m._last_veto_ts
    hz.comp = RuntimeError("boom")
    assert m._held_vetoes(seen + HAZARD_UNSEEN_RELEASE_SEC - 60) != []
    assert m._held_vetoes(seen + HAZARD_UNSEEN_RELEASE_SEC + 1) == []
    assert HAZARD_UNSEEN_RELEASE_SEC == na.UNSEEN_RELEASE_SEC


# ---- A1: unreadable polygons and features ----------------------------------------------
def test_unreadable_polygon_of_a_veto_warning_is_placed_by_its_zones(aenv):
    bad = tor(SV_1611, geometry=_bad_ring(SV_1611["geometry"]["coordinates"]))
    rec = na.normalize(bad)
    assert rec["geometry"] is None and rec["zones"]
    aenv.nws.point = aenv.nws.area = [bad]
    p = aenv.new()
    both(p, NOW)
    c = p.component(NOW)
    assert c["safe"] is False and c["veto"][0]["source"] == "both"   # county TXC303 listed


def test_a_held_veto_is_not_released_when_its_polygon_turns_unreadable(aenv):
    aenv.nws.point = aenv.nws.area = [tor(SV_1611)]
    p = aenv.new()
    both(p, NOW)
    bad = tor(SV_1611, geometry=_bad_ring(SV_1611["geometry"]["coordinates"]))
    aenv.nws.point = aenv.nws.area = [bad]
    both(p, NOW + 120)
    both(p, NOW + 240)
    assert p.component(NOW + 240)["safe"] is False


def test_unread_feature_of_a_veto_event_at_the_point_vetoes_until_its_end(aenv):
    aenv.nws.point = [_unreadable(tor(SV_1611))]
    p = aenv.new()
    p.poll_point(NOW)
    c = p.component(NOW)
    (v,) = c["veto"]
    assert v["key"] == "KLUB.TO.W.0263" and v["end_ts"] == END   # VTEC key and end read
    assert "point query: 1 alert(s) could not be read" in c["error"]
    assert c["at_site"][0]["event"] == "Tornado Warning"


def test_unread_feature_without_an_end_time_gets_the_no_end_hold(aenv):
    f = _unreadable(tor(SV_1611, ends=None, expires=None))
    f["properties"]["parameters"] = 7                   # no VTEC either: keyed by its id
    aenv.nws.point = [f]
    p = aenv.new()
    p.poll_point(NOW)
    (v,) = p.component(NOW)["veto"]
    assert v["key"] == SV_1611["properties"]["id"] and v["end_ts"] == NOW + na.NO_END_HOLD_SEC


def test_unnamed_unread_feature_at_the_point_blocks_every_release_but_vetoes_nothing(aenv):
    p = aenv.new()
    aenv.nws.point = [_unreadable(FFA, event=None)]
    both(p, NOW)
    c = p.component(NOW)
    assert c["safe"] is True and c["at_site"][0]["event"] == na.UNREAD_EVENT
    aenv.nws.point = aenv.nws.area = [tor(SV_1611)]     # a veto, then gone from both...
    both(p, NOW + 120)
    aenv.nws.point, aenv.nws.area = [_unreadable(FFA, event=None)], []
    both(p, NOW + 240)
    assert p.component(NOW + 240)["safe"] is False      # ...but the point answer is blind
    aenv.nws.point = []
    both(p, NOW + 360)
    assert p.component(NOW + 360)["safe"] is True


@pytest.mark.parametrize("event", ["Tornado Warning", None])
def test_unread_area_feature_blinds_the_area_answer(aenv, event):
    aenv.nws.point = [tor(SV_1611)]
    p = aenv.new()
    both(p, NOW)
    # elsewhere in the states: a tornado warning (or an alert of unknown event) that
    # cannot be read, so the area answer cannot say the site is clear of tornado warnings
    other = tor(derive(SV_1611, cap_id="urn:oid:elsewhere", geometry=None),
                vtec="/O.NEW.KAMA.TO.W.0001.000000T0000Z-260921T2200Z/")
    aenv.nws.point, aenv.nws.area = [], [_unreadable(other, event=event or 5)]
    both(p, NOW + 120)
    c = p.component(NOW + 120)
    assert c["safe"] is False and "area query: 1 alert(s) could not be read" in c["error"]
    aenv.nws.area = []
    both(p, NOW + 240)
    assert p.component(NOW + 240)["safe"] is True


def test_unread_civil_message_blinds_a_civil_veto(aenv):
    aenv.cfg.HAZARD_VETO_EVENTS = "Tornado Warning,Civil Emergency Message"
    cem = derive(HWW, cap_id="urn:oid:cem", event="Civil Emergency Message",
                 params={"VTEC": []}, zones=["county/TXC303"])
    aenv.nws.point = [cem]
    p = aenv.new()
    both(p, NOW)
    assert p.component(NOW)["safe"] is False
    aenv.nws.point, aenv.nws.civil = [], [_unreadable(cem, event=None)]
    both(p, NOW + 120)
    assert p.component(NOW + 120)["safe"] is False
    aenv.nws.civil = []
    both(p, NOW + 240)
    assert p.component(NOW + 240)["safe"] is True


def test_geometry_validation():
    good = _rect(-102.0, 33.0, -101.0, 34.0)
    assert na.geometry_ok(good) and na._polygon_geometry(good) is good
    for bad in (_string_coords(good["coordinates"]), _bad_ring(good["coordinates"]),
                {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 0]]]},
                {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, float("nan")],
                                                     [0, 0]]]},
                {"type": "MultiPolygon", "coordinates": [good["coordinates"], "x"]},
                {"type": "GeometryCollection", "geometries": [good, "x"]},
                {"type": "Polygon", "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]}):
        assert na._polygon_geometry(bad) is None, bad
        assert na.touches_box(bad, (-180, -90, 180, 90)) in (True, False)   # never raises
    line = {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
    assert na.geometry_ok({"type": "GeometryCollection", "geometries": [line, good]})


# ---- A2: one bad shape never fails a poll ----------------------------------------------
def test_string_coordinates_elsewhere_do_not_fail_the_area_poll(aenv):
    ffa = derive(FFA, geometry=_string_coords(_rect(-102, 36, -101, 36.5)["coordinates"]))
    aenv.nws.area = [ffa, HWW]
    p = aenv.new()
    assert p.poll_area(NOW)["ok"] is True
    assert p.component(NOW)["veto"][0]["event"] == "High Wind Warning"


def test_string_coordinates_at_the_site_still_veto(aenv):
    bad = tor(SV_1611, geometry=_string_coords(SV_1611["geometry"]["coordinates"]))
    aenv.nws.point = aenv.nws.area = [bad]
    p = aenv.new()
    assert p.poll_point(NOW)["ok"] is True and p.poll_area(NOW)["ok"] is True
    assert p.component(NOW)["veto"][0]["source"] == "both"


def test_a_shape_test_that_raises_is_contained_per_record(aenv, monkeypatch):
    aenv.nws.point = aenv.nws.area = [HWW]
    p = aenv.new()

    def boom(*a, **k):
        raise TypeError("bug in a shape test")
    monkeypatch.setattr(na, "touches_box", boom)
    assert p.poll_point(NOW)["ok"] is True and p.poll_area(NOW)["ok"] is True
    c = p.component(NOW)
    assert c["veto"][0]["source"] == "both"             # point: NWS; area: its zone match
    assert c["at_site"][0]["geometry_source"] is None


def test_a_zone_outline_that_breaks_the_entry_is_memoised_as_failed(aenv, monkeypatch):
    aenv.nws.area = [HWW]
    p = aenv.new()

    def boom(*a, **k):
        raise ValueError("bug in simplify")
    monkeypatch.setattr(na, "simplify_geometry", boom)
    assert p.poll_area(NOW)["ok"] is True
    n = aenv.nws.n("zones")
    assert p.poll_area(NOW + 120)["ok"] is True
    assert aenv.nws.n("zones") == n                     # not fetched again on every poll
    assert p.component(NOW + 120)["veto"][0]["source"] == "local"


def test_a_crashing_point_poll_does_not_cost_the_area_poll(aenv, monkeypatch):
    aenv.nws.area = [HWW]
    p = aenv.new()

    def boom(now=None):
        raise RuntimeError("bug")
    monkeypatch.setattr(p, "poll_point", boom)
    out = p.maybe_poll(NOW)
    assert out["point"]["ok"] is False and out["area"]["ok"] is True
    assert p.component(NOW)["safe"] is False


# ---- A3: zone replies -------------------------------------------------------------------
class _ZoneReply(FakeNWS):
    def __init__(self, reply):
        super().__init__()
        self.reply = reply

    def __call__(self, url, ua):
        doc = super().__call__(url, ua)
        if "/zones/" in url:
            return self.reply(url, doc)
        return doc


def _zone_env(aenv, monkeypatch, reply):
    nws = _ZoneReply(reply)
    monkeypatch.setattr(na, "_get", nws)
    aenv.nws = nws
    nws.area = [HWW]
    return aenv.new()


def test_a_zone_reply_naming_another_zone_is_not_used(aenv, monkeypatch):
    p = _zone_env(aenv, monkeypatch, lambda url, d: dict(
        d, id=Z + "forecast/TXZ999", properties={"id": "TXZ999"}))
    both(p, NOW)
    c = p.component(NOW)
    assert c["veto"][0]["event"] == "High Wind Warning"     # placed by the site's zone
    assert c["at_site"][0]["geometry_source"] is None and p.overlays(NOW) == []


def test_a_zone_reply_naming_the_zone_is_used(aenv, monkeypatch):
    p = _zone_env(aenv, monkeypatch, lambda url, d: dict(
        d, id="x", properties={"id": url.rsplit("/", 1)[1]}))
    both(p, NOW)
    assert p.component(NOW)["at_site"][0]["geometry_source"] == "zones"


def test_an_outline_larger_than_any_zone_is_not_used(aenv, monkeypatch):
    p = _zone_env(aenv, monkeypatch, lambda url, d: dict(d, geometry=_rect(-130, 20, -70, 50)))
    both(p, NOW)
    assert p.component(NOW)["at_site"][0]["geometry_source"] is None
    assert p.overlays(NOW) == []


def test_a_zone_without_an_outline_is_asked_for_again_after_a_day(aenv, monkeypatch):
    p = _zone_env(aenv, monkeypatch, lambda url, d: dict(d, geometry=None))
    p.poll_area(NOW)
    n = aenv.nws.n("zones")
    p.poll_area(NOW + 120)
    assert aenv.nws.n("zones") == n                     # within the day: not again
    path = p._zone_path("forecast", "TXZ035")
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["fetched_ts"] is not None
    p._zones[("forecast", "TXZ035")]["fetched_ts"] -= na.ZONE_NULL_TTL + 1
    p.poll_area(NOW + 240)
    assert aenv.nws.n("zones") == n + 1
    # an older cache file (no fetch time) without an outline: asked for again too
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"id": "TXZ035", "type": "forecast", "bbox": None, "geometry": None}, f)
    q = aenv.new()
    q.poll_area(NOW + 360)
    assert aenv.nws.n("zones") == n + 2


def test_a_corrupt_zone_cache_file_is_fetched_again_not_raised(aenv):
    p = aenv.new()
    path = p._zone_path("forecast", "TXZ035")
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"id": "TXZ035", "bbox": ["a", "b", "c", "d"],
                   "geometry": _string_coords(_rect(0, 0, 1, 1)["coordinates"])}, f)
    aenv.nws.area = [HWW]
    assert p.poll_area(NOW)["ok"] is True
    assert aenv.nws.n("zones") >= 1
    assert p.component(NOW)["at_site"][0]["geometry_source"] == "zones"


# ---- A5: feed text is scrubbed ----------------------------------------------------------
def test_feed_text_is_scrubbed_before_it_is_kept(aenv):
    f = tor(SV_1611)
    pr = f["properties"]
    pr["event"] = "Tornado Warning‎"               # a bidi mark: still the veto event
    pr["headline"] = "Tornado \ud83d Warning"            # a lone surrogate (JSON \\ud83d)
    pr["description"] = "line\r\nbreak\x07"
    pr["areaDesc"] = "Lubbock‮, TX"
    pr["senderName"] = "NWS \udc00Lubbock"
    pr["parameters"]["NWSheadline"] = ["HEADLINE \ud800"]
    aenv.nws.point = [f]
    p = aenv.new()
    p.poll_point(NOW)
    c = p.component(NOW)
    assert c["safe"] is False and c["veto"][0]["event"] == "Tornado Warning"
    json.dumps(c, ensure_ascii=False).encode("utf-8")   # would raise on a lone surrogate
    (at,) = c["at_site"]
    assert at["headline"] == "Tornado � Warning" and at["description"] == "line\nbreak"
    assert at["area_desc"] == "Lubbock, TX" and at["nws_headline"] == "HEADLINE �"


def test_restored_latch_text_is_scrubbed(aenv):
    t = time.time()
    write_latch(aenv, [{"key": "K.TO.W.1", "event": "Tornado Warning", "headline": "x\ud83d",
                        "end_ts": t + 600, "first_seen_ts": t - 60, "last_seen_ts": t - 60}])
    (v,) = aenv.new().component(t)["veto"]
    assert v["headline"] == "x�"


# ---- A6: times read alike on every Python -----------------------------------------------
def test_alert_times_read_alike_on_every_python():
    t = NOW - 60                                       # 2026-09-21 16:11:00 CDT
    assert na._parse_ts("2026-09-21T16:11:00-05:00") == t
    assert na._parse_ts("2026-09-21T21:11:00Z") == t
    assert na._parse_ts("2026-09-21T21:11:00") == t    # no offset: UTC
    assert na._parse_ts("2026-09-21T21:11:00.5Z") == t + 0.5     # 3.9 refused this
    for bad in ("20260921T211100Z", "2026-W38-1", "2026-09-21T21:11:00Z trailing",
                "2026-09-21T24:00:00Z", "", "  ", None, 5):
        assert na._parse_ts(bad) is None, bad          # 3.11+ read the first ones
    f = derive(HWW, ends="20260922T030000Z", expires="2026-09-22T03:00:00.5Z")
    assert na.normalize(f)["end_ts"] == na._parse_ts("2026-09-22T03:00:00Z") + 0.5


# ---- A7/A8: stepwise gunzip and the reply deadline ---------------------------------------
class _Resp:
    def __init__(self, body, enc=None, trickle=False):
        self._b = io.BytesIO(body)
        self.headers = {"Content-Encoding": enc} if enc else {}
        if trickle:
            self.read1 = lambda n: self._b.read(1)

    def read(self, n=-1):
        return self._b.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, resp):
    monkeypatch.setattr(na.urllib.request, "urlopen", lambda req, timeout: resp)


def test_get_refuses_gzip_with_trailing_data_or_cut_short(monkeypatch):
    body = gzip.compress(json.dumps({"features": []}).encode())
    _serve(monkeypatch, _Resp(body, "gzip"))
    assert na._get("https://api.weather.gov/x", "ua") == {"features": []}
    for bad in (body + b"junk", body + body, body[:-6]):
        _serve(monkeypatch, _Resp(bad, "gzip"))
        with pytest.raises(ValueError):
            na._get("https://api.weather.gov/x", "ua")


def test_get_gives_up_on_a_trickling_reply(monkeypatch):
    clock = iter(range(0, 10000, 7))                   # 7 s per byte
    monkeypatch.setattr(netio, "_clock", lambda: float(next(clock)))
    _serve(monkeypatch, _Resp(json.dumps({"features": [], "pad": "x" * 100}).encode(),
                              trickle=True))
    with pytest.raises(netio.ReplyTooSlow):
        na._get("https://api.weather.gov/x", "ua")
    assert 0 < na.HTTP_DEADLINE_SEC <= 3 * na.HTTP_TIMEOUT
    assert SITE                                         # (fixtures shared with test_nws_alerts)


def test_an_unread_copy_never_replaces_what_a_readable_message_said(aenv):
    aenv.nws.point = [tor(SV_1611)]
    p = aenv.new()
    p.poll_point(NOW)
    stub = _unreadable(tor(SV_1611, ends=None, expires=None, sent=NOW + 60.0))
    aenv.nws.point = [stub]                             # same VTEC key, no end readable
    p.poll_point(NOW + 60)
    (v,) = p.component(NOW + 60)["veto"]
    assert v["key"] == "KLUB.TO.W.0263" and v["end_ts"] == END   # still the real end
    assert v["source"] == "point"                       # ... and it counts as a sighting
    assert p.component(NOW + 60)["at_site"][0]["unread"] is True
