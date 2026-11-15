"""Review fixes of the NWS alerts layer, the texts that show its vetoes, and the forecast's
number parsing. Each test failed before its fix.

  * the warning's own end is what every display calls "until"; when the veto is held to
    an earlier time (24 h after NWS last listed it), that time is given beside it —
    "until Wed 08:12 CDT; held until Tue 17:12 CDT unless NWS lists it again" — in the
    events, the component, the IsSafe reason, /setup and the status page;
  * a latch file written by the version before the 24 h bound keeps a long warning that
    NWS was still listing; every veto dropped at start-up is logged and recorded;
  * a clock step moves each veto with it the moment the alerts layer sees it, by the
    change of the veto's own clock frame: once, whoever sees the step first (an IsSafe
    call, a poll, the step detector), not at all for a veto first listed after the step,
    and also for a step the detector never reports; an answer that arrives after a step
    is read in the new clock;
  * control characters that separate words become a blank, so the event still matches;
    inside a word, and inside a status, type, VTEC string or time, they are dropped;
  * a key's end is the latest of its messages, an unread one's readable end included;
  * a feature whose properties, event, status or type cannot be read is an unread stub;
  * a readable message of a key wins over an unread stub of the same key;
  * a forecast value of +infinity vetoes; NaN and -infinity are no value.

The fixtures and the fake api.weather.gov are those of test_nws_alerts.py.
"""
import copy
import json
import time
import types

import pytest

from safety import config, hazard_feeds, monitor as monitor_mod, nws_forecast
from safety import nws_alerts as na
from safety.textclean import clean_text
from safety.tests.test_nws_alerts import (
    HWW, NOW, FakeNWS, Log, both, derive, latch, make_cfg, write_latch)

H = 3600.0
BOUND = na.UNSEEN_RELEASE_SEC + na.CAP_RENEW_SEC     # the hold: 24-25 h after a sighting
DOWN = {"point", "area", "points", "zones", "civil"}


@pytest.fixture
def nenv(tmp_path, monkeypatch):
    nws = FakeNWS()
    monkeypatch.setattr(na, "_get", nws)
    monkeypatch.setattr(na, "_warned_once", set())
    cfg, log = make_cfg(tmp_path), Log()
    return types.SimpleNamespace(nws=nws, cfg=cfg, log=log, tmp=tmp_path,
                                 new=lambda: na.NwsAlertsPoller(cfg, log))


def _hww(ends, **kw):
    return derive(HWW, ends=ends, expires=ends, **kw)


def _released(env):
    return [e for e in env.log.vetoes() if e["result"].startswith("released")]


# ---- the warning's own end, and the hold beside it ---------------------------------------
def test_a_long_warning_shows_its_own_end_and_the_hold_beside_it(nenv):
    # NOW is Mon 16:12 CDT: the warning ends Wed 08:12, the 24 h hold is Tue 17:12
    ends = NOW + 40 * H
    nenv.nws.point = nenv.nws.area = [_hww(ends)]
    p = nenv.new()
    both(p, NOW)
    for k in range(1, 6):                             # NWS lists it every minute
        p.poll_point(NOW + 60 * k)
    now = NOW + 330
    c = p.component(now)
    (v,) = c["veto"]
    assert v["alert_end_ts"] == ends and v["alert_end_local"] == "Wed 08:12 CDT"
    assert v["end_ts"] == NOW + BOUND and v["end_local"] == "Tue 17:12 CDT"
    phrase = "until Wed 08:12 CDT; held until Tue 17:12 CDT unless NWS lists it again"
    assert c["reasons"] == ["NWS High Wind Warning in effect for the site %s "
                            "(NWS Lubbock TX)" % phrase]
    (ev,) = nenv.log.vetoes()                         # the creation event (and log line)
    assert ev["result"] == "unsafe " + phrase
    # the IsSafe reason, which /setup and the page list under "Why unsafe"
    assert monitor_mod.hazard_veto_reason(v, nenv.cfg, now) == c["reasons"][0]
    held = monitor_mod.hazard_veto_reason(dict(v, source="latched"), nenv.cfg, now)
    assert held == ("NWS High Wind Warning in effect for the site %s (NWS Lubbock TX; not "
                    "re-confirmed by a fresh NWS query)" % phrase)
    assert "held to its end time" not in held
    # an extension: the warning's new end against its old one, not the hold twice
    upd = _hww(NOW + 50 * H, cap_id="urn:oid:upd", sent=NOW + 600.0, messageType="Update",
               vtec="/O.EXT.KLUB.HW.W.0003.000000T0000Z-260923T2300Z/")
    nenv.nws.point = [upd]
    p.poll_point(NOW + 660)
    assert nenv.log.vetoes()[-1]["result"] == (
        "unsafe until Wed 18:12 CDT (was Wed 08:12 CDT); held until Tue 17:12 CDT unless "
        "NWS lists it again")


def test_a_short_warning_reads_as_before(nenv):
    ends = NOW + 2 * H
    nenv.nws.point = [_hww(ends)]
    p = nenv.new()
    p.poll_point(NOW)
    (v,) = p.component(NOW)["veto"]
    assert v["end_ts"] == v["alert_end_ts"] == ends
    assert v["end_local"] == v["alert_end_local"] == "18:12 CDT"
    assert monitor_mod.hazard_veto_reason(v, nenv.cfg, NOW) == (
        "NWS High Wind Warning in effect for the site until 18:12 CDT (NWS Lubbock TX)")
    assert monitor_mod.hazard_veto_reason(dict(v, source="latched"), nenv.cfg, NOW).endswith(
        "(NWS Lubbock TX; held to its end time — not re-confirmed by a fresh NWS query)")


def test_a_warning_without_an_end_time_says_so_instead_of_an_end(nenv):
    nenv.nws.point = [derive(HWW, ends=None, expires=None)]
    p = nenv.new()
    p.poll_point(NOW)
    (v,) = p.component(NOW)["veto"]
    assert v["alert_end_ts"] is None and v["alert_end_local"] is None
    assert monitor_mod.hazard_veto_reason(v, nenv.cfg, NOW) == (
        "NWS High Wind Warning in effect for the site with no end time given; held until "
        "17:12 CDT unless NWS lists it again (NWS Lubbock TX)")
    assert nenv.log.vetoes()[-1]["result"] == ("unsafe with no end time given; held until "
                                               "17:12 CDT unless NWS lists it again")


def _held_veto(t, source="latched"):
    """A veto entry as the poller reports a High Wind Warning held to its 24 h bound."""
    return {"key": "KLUB.HW.W.0003", "event": "High Wind Warning", "headline": "",
            "sender": "NWS Lubbock TX", "end_ts": t + 25 * H, "end_local": "Tue 17:12 CDT",
            "alert_end_ts": t + 40 * H, "alert_end_local": "Wed 08:12 CDT",
            "first_seen_ts": t - 60, "source": source}


def test_setup_row_gives_the_warnings_end_and_the_hold(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from safety.tests.test_alpaca import _Stub, _client, _hz
    c, mon = _client(tmp_path, monkeypatch)
    mon.hazards = _Stub(_hz(veto=[_held_veto(time.time())]))
    page = c.get("/setup").get_data(as_text=True)
    assert ("VETO — High Wind Warning over the site until Wed 08:12 CDT (held until Tue "
            "17:12 CDT unless NWS lists it again; not re-confirmed by a fresh query)") in page
    assert "unless NWS lists it again (NWS Lubbock TX; not re-confirmed by a fresh NWS " \
           "query)" in page                                          # the IsSafe reason
    assert "held to its end time" not in page
    assert "over the site until Tue 17:12 CDT" not in page          # the hold as the end


def test_setup_row_names_a_scheduled_veto_warning(tmp_path, monkeypatch):
    # README: a warning issued for a later period is shown as scheduled on the page AND
    # on /setup (the page's tile always did; /setup did not)
    pytest.importorskip("flask")
    from safety.tests.test_alpaca import _Stub, _client, _hz
    c, mon = _client(tmp_path, monkeypatch)
    hz = _hz()
    hz["veto_pending"] = [dict(_held_veto(time.time()), onset_local="Fri 10:00 CDT")]
    mon.hazards = _Stub(hz)
    page = c.get("/setup").get_data(as_text=True)
    assert "scheduled: High Wind Warning from Fri 10:00 CDT (not a veto yet)" in page


def test_page_banner_and_tile_give_the_warnings_end_and_the_hold():
    from safety.tests import test_hazards_page as thp
    now = time.time()
    hz = thp._hazards(now)
    hz["veto"] = [_held_veto(now)]
    h = thp.msp.build_hazards_html(thp._state(now, hz))
    txt = thp._text(h)
    assert "UNSAFE: High Wind Warning over the site until Wed 08:12 CDT" in txt
    assert "held until Tue 17:12 CDT unless NWS lists it again" in txt
    assert "held until the warning ends" not in txt
    assert "until the warning ends or until 24 h after NWS last listed it" in txt
    tile = thp._tile(thp.msp.build_safety_tiles_html(thp._state(now, hz)["components"]))
    assert "High Wind Warning until Wed 08:12 CDT" in tile
    # no end time given: its 60-min hold is never shown as the warning's end
    hz["veto"] = [dict(_held_veto(now, source="point"), alert_end_ts=None,
                       alert_end_local=None, end_ts=now + H, end_local="17:12 CDT")]
    txt = thp._text(thp.msp.build_hazards_html(thp._state(now, hz)))
    assert ("UNSAFE: High Wind Warning over the site, no end time given" in txt
            and "held until 17:12 CDT unless NWS lists it again" in txt)
    tile = thp._tile(thp.msp.build_safety_tiles_html(thp._state(now, hz)["components"]))
    assert "High Wind Warning" in tile and "until" not in thp._text(tile)
    # a warning issued for a later period: its own end, and the hold beneath
    hz = thp._hazards(now, veto=False, at_site=[], nearby=[])
    hz["veto_pending"] = [dict(_held_veto(now, source="point"), onset_local="Wed 06:00 CDT")]
    txt = thp._text(thp.msp.build_hazards_html(thp._state(now, hz)))
    assert ("Scheduled: High Wind Warning for the site from Wed 06:00 CDT until Wed 08:12 "
            "CDT") in txt and "held until Tue 17:12 CDT unless NWS lists it again" in txt


# ---- the latch file at start-up -------------------------------------------------------------
def test_upgrade_keeps_a_long_warning_from_a_latch_written_before_the_bound(nenv):
    # what the version before the 24 h bound wrote for a High Wind Warning first listed
    # 30 h ago and listed every minute since: no cap_ts, and last_seen_ts = the first
    # sighting (that version rewrote the file only when the veto set changed)
    t = time.time()
    write_latch(nenv, [{"key": "KLUB.HW.W.0003", "event": "High Wind Warning",
                        "headline": "", "sender": "NWS Lubbock TX", "end_ts": t + 30 * H,
                        "first_seen_ts": t - 30 * H, "onset_ts": t - 30 * H,
                        "last_seen_ts": t - 30 * H, "active": True}])
    nenv.nws.down = set(DOWN)                          # and NWS is unreachable at restart
    p = nenv.new()
    c = p.component(t)
    (v,) = c["veto"]
    assert v["key"] == "KLUB.HW.W.0003" and v["source"] == "latched"
    assert t + BOUND - 1 <= v["end_ts"] <= t + BOUND + 5     # bounded from the restart
    (ev,) = nenv.log.vetoes()
    assert ev["result"].startswith("restored: unsafe until ") and "held until" in ev["result"]
    (saved,) = latch(nenv)                             # from now on the file has the bound
    assert abs(saved["cap_ts"] - v["end_ts"]) < 1
    # the old 72 h stale-information cap is read the same way
    write_latch(nenv, [dict(saved, cap_ts=t + 42 * H, last_seen_ts=t - 26 * H)])
    (v,) = nenv.new().component(t)["veto"]
    assert t + BOUND - 1 <= v["end_ts"] <= t + BOUND + 5
    # a later restart keeps the bound the first one set: never moved later
    write_latch(nenv, [dict(saved, cap_ts=t - 2 * H + BOUND, last_seen_ts=t - 30 * H)])
    (v,) = nenv.new().component(t)["veto"]
    assert abs(v["end_ts"] - (t - 2 * H + BOUND)) < 1


def test_a_veto_that_ended_while_down_is_released_with_an_event(nenv, caplog):
    t = time.time()
    write_latch(nenv, [{"key": "K.TO.W.1", "event": "Tornado Warning", "end_ts": t - 10,
                        "first_seen_ts": t - 1800}])
    with caplog.at_level("WARNING", logger="ttu.safety.hazards"):
        assert nenv.new().component(t)["safe"] is True
    (ev,) = nenv.log.vetoes()
    assert ev["key"] == "K.TO.W.1" and ev["result"].startswith("released at start-up: ended")
    assert "HAZARD VETO released at start-up: NWS Tornado Warning (K.TO.W.1)" in caplog.text
    assert latch(nenv) == []


def test_an_unreadable_latch_entry_arms_the_placeholder_veto(nenv):
    t = time.time()
    write_latch(nenv, [5, {"key": ""},
                       {"key": "K.TO.W.1", "event": "Tornado Warning", "end_ts": t + 1800,
                        "first_seen_ts": t - 60, "last_seen_ts": t - 60,
                        "cap_ts": t - 60 + BOUND}])
    p = nenv.new()
    c = p.component(t)
    assert c["safe"] is False
    assert sorted(v["key"] for v in c["veto"]) == ["K.TO.W.1", "latch-file-unreadable"]
    assert {"2 hazard latch entries unreadable"} == {
        e["reason"] for e in nenv.log.vetoes() if e.get("source") == "latch"
        and "unreadable" in e["reason"]}
    # written back at once without the unreadable entries, the placeholder in their place
    assert {it["key"] for it in latch(nenv)} == {"K.TO.W.1", "latch-file-unreadable"}
    both(p, t + 10)                 # fresh queries list nothing at the site: released
    assert p.component(t + 10)["safe"] is True


# ---- clock steps ----------------------------------------------------------------------------
@pytest.fixture
def clocks(monkeypatch):
    """The system's clock frame as the poller reads it: wall minus monotonic. A real step
    changes it, and the poller moves each veto by its own frame's change the moment it
    looks (NwsAlertsPoller._follow_clock), whatever the step detector reports."""
    system = {"offset": 1000.0}
    monkeypatch.setattr(na, "_clock_offset", lambda: system["offset"])
    return system


class StepDuringFetch:
    """The fake api.weather.gov, with the wall clock stepping by ``step`` while the first
    point query is in flight (its answer arrives after the step); ``then`` runs right after
    the step (the step detector's report, for one)."""

    def __init__(self, nws, clocks, step, then=None):
        self.nws, self.clocks, self.step, self.then, self.done = nws, clocks, step, then, False

    def __call__(self, url, ua):
        out = self.nws(url, ua)
        if "?point=" in url and not self.done:
            self.done = True
            self.clocks["offset"] += self.step
            if self.then is not None:
                self.then()
        return out


def test_a_forward_step_seen_first_by_an_issafe_call_keeps_the_24_h(nenv, clocks):
    # The Pi booted two days behind (fake-hwclock), NWS listed a High Wind Warning that
    # ends 30 h from now in real time, then chrony stepped the clock: an IsSafe call (and
    # a poll) came before the step detector called clock_stepped. The bound of a warning
    # NWS listed minutes before used to look two days old there: released.
    real, behind = NOW, 48 * H
    wrong = real - behind
    nenv.nws.point = nenv.nws.area = [
        _hww(real + 30 * H, sent=real - 600.0, onset=real - 600.0, effective=real - 600.0)]
    p = nenv.new()
    both(p, wrong)
    assert p.component(wrong + 5)["safe"] is False
    nenv.nws.down = set(DOWN)
    clocks["offset"] += behind                        # the step
    c = p.component(real + 20)                        # seen here first: moved with it
    assert c["safe"] is False and c["veto"][0]["key"] == "KLUB.HW.W.0003"
    assert c["veto"][0]["end_ts"] >= real + 24 * H
    p.poll_point(real + 25)                           # (NWS down: nothing to apply)
    p.clock_stepped(wrong + 10, real + 30)            # the detector: nothing left to move
    c = p.component(real + 40)
    assert c["safe"] is False and c["veto"][0]["end_ts"] >= real + 24 * H
    assert _released(nenv) == []
    c = p.component(real + 30 + BOUND)                # the bound works as always
    assert c["safe"] is True
    assert _released(nenv)[-1]["result"] == ("released: not sighted for 24 h, could not be "
                                             "confirmed gone")


def _backstep_poller(env, clocks, step_h, issafe_first):
    """A High Wind Warning (40 h left) listed every minute for 2 h, NWS then down, and the
    clock stepped back by step_h hours; an IsSafe call before or after clock_stepped."""
    r = NOW
    ends = r + 40 * H
    env.nws.point = env.nws.area = [_hww(ends, sent=r - 7300.0, onset=r - 7300.0,
                                         effective=r - 7300.0)]
    p = env.new()
    t = r - 2 * H
    both(p, t)
    while t < r - 60:
        t += 60
        p.maybe_poll(t)
    env.nws.down = set(DOWN)
    post = r - step_h * H
    clocks["offset"] -= step_h * H
    if issafe_first:
        p.component(post + 5)
        p.clock_stepped(r, post)
    else:
        p.clock_stepped(r, post)
        p.component(post + 5)
    return p, post


@pytest.mark.parametrize("step_h", [3, 26, 48])
def test_a_backward_step_moves_each_veto_once(tmp_path, monkeypatch, clocks, step_h):
    out = {}
    for first in (True, False):
        nws = FakeNWS()
        monkeypatch.setattr(na, "_get", nws)
        cfg, log = make_cfg(tmp_path / str(first)), Log()
        env = types.SimpleNamespace(nws=nws, cfg=cfg, log=log,
                                    new=lambda cfg=cfg, log=log: na.NwsAlertsPoller(cfg, log))
        clocks["offset"] = 1000.0
        p, post = _backstep_poller(env, clocks, step_h, first)
        c = p.component(post + 40)
        assert c["safe"] is False, (first, step_h)
        assert _released(env) == []
        out[first] = c["veto"][0]["end_ts"] - post
    # whoever sees the step first, the time left is kept (moved twice, a warning with 40 h
    # to run was released once the step reached ~25 h)
    assert abs(out[True] - out[False]) < 2, out
    assert out[True] >= 24 * H


def test_a_step_the_detector_never_reports_is_followed_at_once(nenv, clocks):
    # A step during start-up (after the alerts layer took its baseline, before the step
    # detector took its own) is never reported. It used to suspend the 24 h bound, and
    # with it the release at the warning's own end, until the next restart; then the
    # bound was held back for 2 min. Now the first look moves the veto: the warning's own
    # end, fresh queries and the bound all work as always.
    nenv.nws.point = nenv.nws.area = [_hww(NOW + 40 * H)]
    p = nenv.new()
    both(p, NOW)
    nenv.nws.down = set(DOWN)
    clocks["offset"] += 2 * H                         # the step, never reported
    later = NOW + 2 * H
    c = p.component(later + 5)
    assert c["safe"] is False and c["veto"][0]["end_ts"] >= later + 23 * H
    assert p.component(later + 23 * H)["safe"] is False
    assert p.component(later + 26 * H)["safe"] is True    # 24-25 h after its last sighting
    assert _released(nenv)[-1]["result"] == ("released: not sighted for 24 h, could not be "
                                             "confirmed gone")


def test_fresh_queries_after_a_step_still_release_a_veto(nenv, clocks):
    p = nenv.new()
    nenv.nws.point = nenv.nws.area = [_hww(NOW + 40 * H)]
    both(p, NOW)
    clocks["offset"] += 3 * H                         # a forward step, not reported yet
    nenv.nws.point = nenv.nws.area = []               # cancelled: NWS lists nothing now
    both(p, NOW + 3 * H + 60)
    assert p.component(NOW + 3 * H + 65)["veto"] == []
    assert "fresh NWS point and area queries" in _released(nenv)[-1]["result"]


def test_a_late_step_detector_does_not_move_a_veto_twice(nenv, clocks):
    # A backward step of 26 h: an IsSafe call sees it (and moves the veto), and the step
    # detector reports it minutes later. Moved twice, the veto lost 26 h — released at once.
    r = NOW
    nenv.nws.point = nenv.nws.area = [_hww(r + 40 * H, sent=r - 7300.0, onset=r - 7300.0,
                                           effective=r - 7300.0)]
    p = nenv.new()
    t = r - 2 * H
    both(p, t)
    while t < r - 60:
        t += 60
        p.maybe_poll(t)
    nenv.nws.down = set(DOWN)
    post = r - 26 * H
    clocks["offset"] -= 26 * H
    p.component(post + 5)
    p.component(post + 130)
    p.clock_stepped(r, post)                          # the detector, late
    c = p.component(post + 260)
    assert c["safe"] is False and _released(nenv) == []
    assert c["veto"][0]["end_ts"] - post >= 23 * H


def test_a_veto_first_listed_between_a_step_and_its_report_keeps_the_24_h(nenv, clocks):
    # The clock steps back 26 h; NWS lists a High Wind Warning (40 h left) before the step
    # detector reports the step, and is unreachable afterwards. The veto's stamps are of
    # the new clock already: moved by the whole step, its 24 h bound lay in the past.
    p = nenv.new()
    clocks["offset"] -= 26 * H
    post = NOW - 26 * H
    nenv.nws.point = nenv.nws.area = [_hww(NOW + 40 * H)]
    both(p, post)                                     # first listed under the new clock
    nenv.nws.down = set(DOWN)
    p.clock_stepped(NOW, post + 10)                   # the detector's report
    c = p.component(post + 40)
    assert c["safe"] is False and _released(nenv) == []
    assert c["veto"][0]["end_ts"] - post >= 24 * H


def test_a_step_back_and_forth_the_detector_never_saw_keeps_the_24_h(nenv, clocks):
    # The clock steps back 30 h, an IsSafe call comes, and the clock steps forward again —
    # all between two runs of the step detector, which sees no net step and never calls
    # clock_stepped. The veto was moved back and nothing moved it forward again: released
    # "not sighted for 24 h" a minute after NWS last listed it.
    r = NOW
    nenv.nws.point = nenv.nws.area = [_hww(r + 40 * H, sent=r - 7300.0, onset=r - 7300.0,
                                           effective=r - 7300.0)]
    p = nenv.new()
    t = r - 2 * H
    both(p, t)
    while t < r - 60:                                  # listed every minute for 2 h
        t += 60
        p.maybe_poll(t)
    nenv.nws.down = set(DOWN)
    step = 30 * H
    clocks["offset"] -= step
    assert p.component(r - step + 5)["safe"] is False
    clocks["offset"] += step
    c = p.component(r + 10)
    assert c["safe"] is False and _released(nenv) == []
    assert c["veto"][0]["end_ts"] >= r + 23 * H


@pytest.mark.parametrize("report", ["after", "during", "never"])
def test_a_veto_armed_by_an_answer_that_came_after_a_step_keeps_the_24_h(nenv, clocks,
                                                                         monkeypatch, report):
    # The Pi booted two days behind; the first point query (its time read under the
    # wrong clock) lists a High Wind Warning (30 h left in real time), and chrony steps
    # the clock while the answer is on its way — the two come together, both need the
    # network. NWS is unreachable afterwards. Stamped with the time read before the step
    # but in the new frame, the veto was moved by nothing: released at the next IsSafe
    # call, "not sighted for 24 h", seconds after NWS listed it.
    real, behind = NOW, 48 * H
    wrong = real - behind
    nenv.nws.point = nenv.nws.area = [
        _hww(real + 30 * H, sent=real - 600.0, onset=real - 600.0, effective=real - 600.0)]
    p = nenv.new()
    during = (lambda: p.clock_stepped(wrong + 5, real + 5)) if report == "during" else None
    monkeypatch.setattr(na, "_get", StepDuringFetch(nenv.nws, clocks, behind, during))
    p.poll_point(wrong)
    nenv.nws.down = set(DOWN)
    assert p.component(real + 20)["safe"] is False
    if report == "after":
        p.clock_stepped(wrong + 5, real + 25)
    c = p.component(real + 60)
    assert c["safe"] is False and _released(nenv) == []
    assert c["veto"][0]["end_ts"] >= real + 23 * H


def test_an_answer_after_a_backward_step_is_read_in_the_new_clock(nenv, clocks, monkeypatch):
    # A High Wind Warning with 10 h left, listed by both queries. The clock steps back 26 h
    # while a point query is in flight, and the step detector reports it before the answer
    # (which lacks the key this time) is applied: applied with the time read before the
    # step, 26 h "ahead" of the stamps just moved, it released the veto as "ended", 10 h
    # early.
    nenv.nws.point = nenv.nws.area = [_hww(NOW + 10 * H)]
    p = nenv.new()
    both(p, NOW)
    nenv.nws.point = []
    monkeypatch.setattr(na, "_get", StepDuringFetch(
        nenv.nws, clocks, -26 * H, lambda: p.clock_stepped(NOW + 50, NOW + 55 - 26 * H)))
    p.poll_point(NOW + 60)
    c = p.component(NOW + 65 - 26 * H)
    assert c["safe"] is False and _released(nenv) == []


def test_two_steps_and_a_report_of_the_second_move_each_veto_by_its_own(nenv, clocks):
    # Start-up: the clock steps +48 h (never reported), then -26 h (reported); a High Wind
    # Warning (40 h left) is first listed between the second step and its report. The
    # report used to move every veto by the whole -26 h — this one too, though it was in
    # the new frame already: its 24 h bound lay in the past, released a minute after NWS
    # listed it.
    p = nenv.new()
    t0 = NOW - 48 * H                                 # the wrong boot clock
    p.component(t0)
    clocks["offset"] += 48 * H
    p.component(NOW + 5)
    clocks["offset"] -= 26 * H
    post = NOW - 26 * H + 105
    p.component(post + 20)
    nenv.nws.point = nenv.nws.area = [_hww(NOW + 40 * H)]
    both(p, post + 25)
    nenv.nws.down = set(DOWN)
    p.clock_stepped(post - 30 + 26 * H, post + 30)    # the detector's report of the 2nd
    c = p.component(post + 60)
    assert c["safe"] is False and _released(nenv) == []
    assert c["veto"][0]["end_ts"] - post >= 23 * H


# ---- texts ----------------------------------------------------------------------------------
@pytest.mark.parametrize("ch", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f", "\x85"])
def test_a_control_character_that_separates_words_becomes_a_blank(nenv, ch):
    assert clean_text("High%sWind Warning" % ch) == "High Wind Warning"
    assert hazard_feeds._clip("High%s%sWind" % (ch, ch), 40) == "High Wind"
    nenv.nws.point = nenv.nws.area = [derive(HWW, event="High%sWind Warning" % ch)]
    p = nenv.new()
    both(p, NOW)
    c = p.component(NOW + 1)
    assert c["safe"] is False and c["veto"][0]["event"] == "High Wind Warning"


def _inside(text, at, ch):
    return text[:at] + ch + text[at:]


@pytest.mark.parametrize("ch", ["\x0b", "\x1c", "\x85"])
@pytest.mark.parametrize("field,at", [("event", 2), ("event", 14), ("status", 3),
                                      ("messageType", 2), ("vtec", 12)])
def test_a_separator_inside_a_word_still_vetoes(nenv, ch, field, at):
    # Inside a word the character separates nothing: "Hi<VT>gh Wind Warning" read as
    # "Hi gh", which matched no veto event; a status "Act ual" or type "Al ert" made the
    # warning "not live", and it was dropped as if NWS had not listed it.
    assert clean_text("Act%sual" % ch, "") == "Actual"
    f = _hww(NOW + 6 * H)
    props = f["properties"]
    if field == "vtec":
        props["parameters"]["VTEC"] = [_inside(props["parameters"]["VTEC"][0], at, ch)]
    else:
        props[field] = _inside(props[field], at, ch)
    nenv.nws.point = nenv.nws.area = [f]
    p = nenv.new()
    both(p, NOW)
    (v,) = p.component(NOW + 1)["veto"]
    assert v["event"] == "High Wind Warning" and v["key"] == "KLUB.HW.W.0003"
    assert v["alert_end_ts"] == NOW + 6 * H


@pytest.mark.parametrize("ch", ["\x0b", "\x0c", "\x1f", "\x85"])
@pytest.mark.parametrize("at", [7, 13, 19])
def test_a_separator_inside_a_time_keeps_it(nenv, ch, at):
    # A time is one token: a blank inside broke it ("...T22:12:00 -05:00" is no ISO
    # time), and the veto fell back to 'expires' (3 h early here) or, without one, to the
    # 60-min hold. The onset (a warning for a later period) likewise.
    f = derive(HWW, ends=NOW + 6 * H, expires=NOW + 3 * H, onset=NOW + 2 * H,
               effective=NOW - 600.0)
    props = f["properties"]
    for k in ("ends", "expires", "onset"):
        props[k] = _inside(props[k], at, ch)
    nenv.nws.point = nenv.nws.area = [f]
    p = nenv.new()
    both(p, NOW)
    c = p.component(NOW)
    (v,) = c["veto_pending"]                          # from its onset, not at once
    assert v["alert_end_ts"] == NOW + 6 * H
    assert na.normalize(f)["onset_ts"] == NOW + 2 * H


# ---- features that cannot be read, and stubs -------------------------------------------------
def _spoil(feature, how):
    g = copy.deepcopy(feature)
    props = g["properties"]
    if how == "event-null":
        props["event"] = None
    elif how == "event-list":
        props["event"] = ["High Wind Warning"]
    elif how == "event-blank":
        props["event"] = " \x00 "
    elif how == "properties-string":
        g["properties"] = "garbage"
    elif how == "status-missing":
        del props["status"]
    elif how == "status-blank":
        props["status"] = ""
    elif how == "type-missing":
        del props["messageType"]
    elif how == "feature-string":
        g = "garbage"
    return g


@pytest.mark.parametrize("how", ["event-null", "event-list", "event-blank",
                                 "properties-string", "status-missing", "status-blank",
                                 "type-missing", "feature-string"])
def test_a_feature_that_cannot_be_read_never_releases_a_held_veto(nenv, how):
    base = _hww(NOW + 6 * H)
    nenv.nws.point = nenv.nws.area = [base]
    p = nenv.new()
    both(p, NOW)
    nenv.nws.point = nenv.nws.area = [_spoil(base, how)]   # the same alert, unreadable
    both(p, NOW + 180)
    both(p, NOW + 300)
    c = p.component(NOW + 310)
    assert c["safe"] is False and [v["key"] for v in c["veto"]] == ["KLUB.HW.W.0003"]
    assert "could not be read" in (c["error"] or "") and _released(nenv) == []


@pytest.mark.parametrize("stub_first", [False, True])
def test_a_readable_extension_wins_over_an_unread_stub_of_the_same_key(nenv, stub_first):
    e1, e2 = NOW + 2 * H, NOW + 7 * H
    nenv.nws.point = nenv.nws.area = [_hww(e1)]
    p = nenv.new()
    both(p, NOW)
    upd = _hww(e2, cap_id="urn:oid:upd.site", sent=NOW + 600.0, messageType="Update",
               vtec="/O.EXT.KLUB.HW.W.0003.000000T0000Z-260922T0300Z/")
    bad = copy.deepcopy(upd)                 # a second message of the key: unreadable
    bad["id"] = bad["properties"]["id"] = "urn:oid:upd.other"
    bad["properties"]["affectedZones"] = 5
    if not stub_first:                       # (no end: it ranked as the latest one)
        del bad["properties"]["ends"], bad["properties"]["expires"]
    nenv.nws.point = [bad, upd] if stub_first else [upd, bad]
    nenv.nws.down = {"area", "civil"}        # the area query cannot apply the extension
    p.poll_point(NOW + 900)
    (v,) = p.component(NOW + 900)["veto"]
    assert v["alert_end_ts"] == e2 and v["end_ts"] == e2
    assert p.component(e1 + 30)["safe"] is False and _released(nenv) == []


@pytest.mark.parametrize("nws_down_at_e1", [False, True])
def test_a_newer_unread_message_at_the_first_sighting_keeps_its_later_end(nenv, nws_down_at_e1):
    # The point answer that FIRST lists a key carries an older readable message (end E1)
    # and a newer one that cannot be read, its 'ends' E2 readable. The readable one spoke
    # for the key, end included: armed with E1, released at E1 although NWS still listed
    # the key, SAFE until the next point poll re-armed it (or until NWS answered again).
    e1, e2 = NOW + H, NOW + 6 * H
    a = _hww(e1, sent=NOW - 600.0)
    b = _hww(e2, cap_id="urn:oid:ext", sent=NOW - 60.0, messageType="Update",
             vtec="/O.EXT.KLUB.HW.W.0003.000000T0000Z-260922T0300Z/")
    b["properties"]["affectedZones"] = 5              # normalize raises: an unread stub
    nenv.nws.point = nenv.nws.area = [a, b]
    p = nenv.new()
    both(p, NOW)                                      # (the area query cannot see b)
    (v,) = p.component(NOW + 1)["veto"]
    assert v["alert_end_ts"] == e2 and v["event"] == "High Wind Warning"
    nenv.nws.point = nenv.nws.area = [b]              # the expired message is gone
    if nws_down_at_e1:
        nenv.nws.down = set(DOWN)
        p.maybe_poll(e1 + 5)
    else:
        both(p, e1 + 5)
    c = p.component(e1 + 5)
    assert c["safe"] is False and _released(nenv) == [], _released(nenv)


def test_an_unread_message_with_a_later_end_extends_a_held_veto(nenv):
    # Armed by a readable message; later the point answer lists only a message of the key
    # that cannot be read, whose 'ends' (later) can: the veto runs to that end. Its other
    # words never replace what the readable message said.
    e1, e2 = NOW + H, NOW + 6 * H
    nenv.nws.point = [_hww(e1)]
    p = nenv.new()
    p.poll_point(NOW)
    b = _hww(e2, cap_id="urn:oid:ext", sent=NOW + 60.0, messageType="Update",
             vtec="/O.EXT.KLUB.HW.W.0003.000000T0000Z-260922T0300Z/",
             headline="something else")
    b["properties"]["affectedZones"] = 5
    nenv.nws.point = [b]
    p.poll_point(NOW + 60)
    (v,) = p.component(NOW + 60)["veto"]
    assert v["alert_end_ts"] == e2
    assert nenv.log.vetoes()[-1]["result"].startswith("unsafe until 22:12 CDT (was 17:12 CDT)")
    assert p.component(e1 + 30)["safe"] is False and _released(nenv) == []


# ---- the forecast's numbers -------------------------------------------------------------------
def test_a_forecast_value_of_infinity_vetoes_and_nan_is_no_value(monkeypatch):
    from safety.tests.test_core_hardening import _grid
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    # (+infinity reads as 100 %: above every threshold, and still standard JSON)
    for value, expect in ((float("inf"), 100.0), (10 ** 400, 100.0),
                          ("Infinity", 100.0), ("1e999", 100.0),
                          (json.loads("1e999"), 100.0), (float("nan"), None),
                          (float("-inf"), None), (-10 ** 400, None), ("-Infinity", None)):
        monkeypatch.setattr(nws_forecast, "_get", lambda url, ua, v=value: _grid(v))
        res = nws_forecast.fetch(config)
        assert res["ok"] is True, value
        assert res["now_hour"]["cloud_cover_pct"] == expect, value
        json.dumps(res, allow_nan=False)                # no Infinity/NaN token in it
        ev = nws_forecast.evaluate(res, config)
        assert ev["safe"] is (expect is None), value
        if expect is not None:
            assert ev["reasons"][0].startswith("NWS cloud cover 100% > "), value

    # elsewhere infinity is no value: a temperature of inf does not spoil the download
    def grid_with_inf_temperature(url, ua):
        g = _grid(10)
        g["properties"]["temperature"]["values"][0]["value"] = float("inf")
        return g
    monkeypatch.setattr(nws_forecast, "_get", grid_with_inf_temperature)
    res = nws_forecast.fetch(config)
    assert res["ok"] is True and res["now_hour"]["temp_c"] is None
