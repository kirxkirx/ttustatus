"""Fixes from the review of the merged hardening (16cfd4c), operations side:

* WU: one HTTP 503 (or any 5xx) is that station's error and the poll goes on; only a 429
  (WU's rate limit) ends a poll, and its Retry-After never pauses WU rain detection for
  longer than one normal poll interval.
* Device 1 (the dew monitor) reads false while the WU rain check is paused or stale, and
  while any gating layer of device 0 is broken; a broken GLM layer is not "daytime".
  Device 0 is unchanged in both cases (the owner's accepted policies).
* The status page does not call itself "no longer being updated" while the Pi's clock is
  not confirmed by chrony, and judges its age again when the tab is shown.
* CI runs the GLM layer's numpy/netCDF4 tests on the Pi's Python.
"""
import json
import os
import subprocess
import time

import pytest

from safety import config, glm_lightning, monitor as monitor_mod, wu_poll
from safety.eventlog import EventLog
from safety.monitor import RainPoller, SafetyMonitor
from safety.tests.test_core_hardening import _http_error, _Resp
from safety.tests.test_dew import FakeGlm, FakeNws, Sky, _wu_reply, seasoned, three
from safety.tests.test_monitor import (_conn_comp, _hazards_comp, _radar_comp, _StubConn,
                                       _StubHazards, _StubRadar)
from safety.tests.test_page_feeds_hardening import (NODE, _page_attrs, _script, _state,
                                                    _write_page, msp)

POLL_STATIONS = wu_poll.poll_stations        # the real one (the env fixture stubs it)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---- A3: a 503 is one station's error; a 429's Retry-After never pauses rain detection ------
def _urlopen(replies):
    """urlopen answering per station id from a list of replies, one per request (the last
    one repeats): a dict is sent as JSON, an exception is raised (as urllib raises an
    HTTPError for a 4xx/5xx reply)."""
    def urlopen(req, timeout):
        sid = req.full_url.split("stationId=")[1].split("&")[0]
        queue = replies[sid]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return _Resp(json.dumps(reply).encode())
    return urlopen


def _poller(env, monkeypatch, replies):
    """A RainPoller asking the real poll_stations, the stations of `replies` in order."""
    monkeypatch.setattr(wu_poll, "poll_stations", POLL_STATIONS)   # undo the conftest stub
    monkeypatch.setattr(wu_poll, "discover_stations",
                        lambda lat, lon: [(s, 1.0 + i) for i, s in enumerate(replies)])
    monkeypatch.setattr(wu_poll.urllib.request, "urlopen", _urlopen(replies))
    return RainPoller(env["cfg"], env["log"])


@pytest.mark.parametrize("code", [503, 500, 502, 504])
def test_a_5xx_is_one_stations_error_and_the_poll_goes_on(monkeypatch, code):
    # 16cfd4c ended the whole poll at a 503 (and waited out its Retry-After): the station
    # reporting rain right after it was never asked
    monkeypatch.setattr(wu_poll.urllib.request, "urlopen", _urlopen({
        "A": [_http_error(code, retry_after=3600)], "B": [_wu_reply(precipRate=0.25)],
        "C": [_wu_reply()]}))
    res = POLL_STATIONS(["A", "B", "C"])
    assert [r["state"] for r in res["results"]] == ["error", "live", "live"]
    assert [r["station"] for r in res["raining"]] == ["B"]
    assert res["throttle"] is None and res["errors"] == 1


def test_rain_behind_a_503_is_latched_by_the_same_poll(env, monkeypatch):
    p = _poller(env, monkeypatch, {"A": [_http_error(503, retry_after=3600), _wu_reply()],
                                   "B": [_wu_reply(precipRate=0.25)]})
    t = time.time()
    assert p.maybe_poll(-10.0, t) is not None
    rain = p.component(-10.0, t)
    assert rain["latched"] is True and rain["safe"] is False   # not an hour later
    assert rain["throttle"] is None
    assert p._backoff_until == {} and p._offline_streak == {}  # a 5xx never backs off


def test_a_429s_retry_after_never_pauses_rain_detection_beyond_one_interval(env, monkeypatch):
    p = _poller(env, monkeypatch, {"A": [_http_error(429, retry_after=3600), _wu_reply()],
                                   "B": [_wu_reply(precipRate=0.25)]})
    iv = env["cfg"].WU_POLL_INTERVAL
    t = time.time()
    res = p.maybe_poll(-10.0, t)
    # the rate limit does end this poll: B is not asked, so no rain yet
    assert [r["state"] for r in res["results"]] == ["error", "not-asked"]
    assert res["throttle"] == {"status": 429, "retry_after_s": 3600.0}
    assert p.component(-10.0, t)["latched"] is False
    # ...but the next poll comes at the normal interval, not after WU's hour
    assert p.maybe_poll(-10.0, t + iv - 1) is None
    assert p.maybe_poll(-10.0, t + iv) is not None
    assert p.component(-10.0, t + iv)["latched"] is True


# ---- device 1 and the WU rain check -----------------------------------------------------------
class _Broken:
    """A gating layer whose component() raises: a bug in its code."""

    def component(self, *args, **kwargs):
        raise RuntimeError("layer bug")


@pytest.fixture
def dewrig(tmp_path, monkeypatch):
    """test_dew's rig with every gating layer present: a quiet night in which device 1
    reads true. box['results'] / box['throttle'] are what the next WU poll returns."""
    for key, name in (("INPUTS_FILE", "i.json"), ("STATE_FILE", "s.json"),
                      ("LATCH_FILE", "l.json"), ("GLM_LATCH_FILE", "g.json"),
                      ("RADAR_LATCH_FILE", "r.json"), ("EVENT_LOG", "e.log")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, "WU_API_KEY", "testkey")
    monkeypatch.setattr(config, "CONFIG_WARNINGS", [])
    monkeypatch.setattr(wu_poll, "discover_stations",
                        lambda lat, lon: [("S1", 1.0), ("S2", 2.0), ("S3", 3.0)])
    box = {"results": None, "throttle": None}

    def poll(stations, max_age_min=None):
        results = box["results"] if box["results"] is not None else three(now=time.time())
        return {"live": sum(1 for r in results if r.get("state") == "live"),
                "total": len(stations), "raining": [], "max_rate": 0.0,
                "results": results, "errors": 0, "throttle": box["throttle"]}
    monkeypatch.setattr(wu_poll, "poll_stations", poll)
    with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "sun_altitude_deg": -20.0, "humidity_pct": 40.0}, f)
    log = EventLog(config.EVENT_LOG)
    poller = RainPoller(config, log)
    nws, lightning = FakeNws(), FakeGlm()
    monitor = SafetyMonitor(config, log, poller, nws=nws, glm=lightning,
                            radar=_StubRadar(_radar_comp(True)),
                            conn=_StubConn(_conn_comp(True)),
                            hazards=_StubHazards(_hazards_comp()), dew=seasoned())
    poller.poll_now()
    return {"monitor": monitor, "poller": poller, "nws": nws, "glm": lightning, "box": box}


def test_device_1_reads_false_while_wus_rate_limit_has_cut_the_poll_short(dewrig):
    m, p, box = dewrig["monitor"], dewrig["poller"], dewrig["box"]
    assert m.evaluate()["dew"]["is_safe"] is True
    box["results"] = three(now=time.time())[:1] + [
        {"station": "S2", "state": "error", "error": "HTTP 429"},
        {"station": "S3", "state": "not-asked"}]
    box["throttle"] = {"status": 429, "retry_after_s": 3600.0}
    p.poll_now()
    st = m.evaluate()
    assert st["is_safe"] is True and st["reasons"] == []        # device 0 as before
    dew = st["dew"]
    assert dew["is_safe"] is False                # the stations' readings are still fresh
    assert dew["blocked"] == ["WU rain check paused: WU's rate limit (HTTP 429) cut the "
                              "last poll short (1 station(s) not asked) — until a poll "
                              "gets through"]
    assert dew["reasons"][0] == dew["blocked"][0]
    assert all(dew["checks"].values())            # the dew rule itself holds
    box["results"] = box["throttle"] = None       # the next poll gets through
    p.poll_now()
    st = m.evaluate()
    assert st["dew"]["is_safe"] is True and st["dew"]["blocked"] == []


def _polls(rig, *times, answered=True):
    """WU polls at these times, each followed by an evaluation (the evaluator runs every
    30 s, so the dew monitor takes in every poll's readings); with answered=False no
    station answers them (an API error for each)."""
    rig["box"]["results"] = None if answered else [
        {"station": s, "state": "error", "error": "HTTP 500"} for s in ("S1", "S2", "S3")]
    for t in times:
        rig["poller"].poll_now(t)
        rig["monitor"].evaluate()


def test_device_1_reads_false_once_no_complete_wu_poll_for_two_intervals(dewrig):
    m = dewrig["monitor"]
    limit = 2 * config.WU_POLL_INTERVAL + 5 * 60  # two 10-min intervals plus 5 min
    t = time.time()
    _polls(dewrig, t - limit - 1)                 # the last poll a station answered
    _polls(dewrig, t - limit + 600, t - 60, answered=False)   # polls go on, none answers
    st = m.evaluate()
    assert st["is_safe"] is True and st["reasons"] == []        # device 0 as before
    dew = st["dew"]
    assert dew["is_safe"] is False
    assert dew["blocked"] == ["WU rain check not current: no complete WU poll with a "
                              "station reporting for 25 min (needed within 25 min)"]
    # the stations' last readings are still fresh (30 min): the dew rule itself holds
    assert all(dew["checks"].values())
    rain = st["components"]["rain"]
    assert rain["last_good_poll_age_s"] >= limit and rain["stale"] is False
    assert (monitor_mod.DEW_WU_MAX_POLLS * config.WU_POLL_INTERVAL
            + monitor_mod.DEW_WU_MARGIN_SEC) == limit == 25 * 60


def test_a_rate_limit_that_goes_on_reads_as_a_stopped_rain_check(dewrig):
    t = time.time()
    _polls(dewrig, t - 1800)                      # the last poll that got through whole
    dewrig["box"]["throttle"] = {"status": 429, "retry_after_s": None}
    _polls(dewrig, t - 1200, t - 600, t - 30)     # every later poll cut short by a 429
    dew = dewrig["monitor"].evaluate()["dew"]
    assert dew["is_safe"] is False
    assert dew["blocked"] == ["WU rain check not current: no complete WU poll with a "
                              "station reporting for 30 min (needed within 25 min)"]


def test_wu_polls_that_raise_count_as_failed_polls_for_device_1(dewrig, monkeypatch):
    # A poll whose code raises (a bug; the rain loop logs it and tries again every 30 s)
    # used to change no counter: device 1 stayed true on the last good poll until the
    # 25-min age rule. Each one counts as a failed poll now; device 0 is unchanged.
    m, p = dewrig["monitor"], dewrig["poller"]
    t0 = time.time()
    _polls(dewrig, t0 - 2 * config.WU_POLL_INTERVAL)

    def broken(stations, max_age_min=None):
        raise RuntimeError("poll bug (test)")
    monkeypatch.setattr(wu_poll, "poll_stations", broken)
    with pytest.raises(RuntimeError):
        p.maybe_poll(-20.0, t0 - 60)                          # due: it raises
    assert m.evaluate()["dew"]["is_safe"] is True             # one is tolerated
    with pytest.raises(RuntimeError):
        p.maybe_poll(-20.0, t0 - 30)                          # still due 30 s later
    st = m.evaluate()
    assert st["is_safe"] is True and st["reasons"] == []        # device 0 as before
    assert st["dew"]["is_safe"] is False
    assert st["dew"]["blocked"][0].startswith("WU rain check not current: the last 2 WU "
                                              "polls failed")


def test_one_failed_wu_poll_is_tolerated_by_device_1(dewrig):
    t = time.time()
    _polls(dewrig, t - 2 * config.WU_POLL_INTERVAL - 60)
    _polls(dewrig, t - config.WU_POLL_INTERVAL, answered=False)    # one failed poll
    st = dewrig["monitor"].evaluate()
    assert st["dew"]["is_safe"] is True and st["dew"]["blocked"] == []
    assert st["components"]["rain"]["failed_polls"] == 1


def _rate_limited(rig):
    """The next WU poll is cut short by HTTP 429 at the first station."""
    rig["box"]["results"] = [{"station": "S1", "state": "error", "error": "HTTP 429"},
                             {"station": "S2", "state": "not-asked"},
                             {"station": "S3", "state": "not-asked"}]
    rig["box"]["throttle"] = {"status": 429, "retry_after_s": 3600.0}


def test_after_a_429_a_poll_nobody_answers_does_not_end_the_pause(dewrig):
    # A good poll, one cut short by a 429, then one that is not cut short but that no
    # station answers (all 5xx): no reading since the good poll, so device 1 stays false
    # — it used to read true again until the 25-min age rule caught up.
    m, p = dewrig["monitor"], dewrig["poller"]
    iv = config.WU_POLL_INTERVAL
    t = time.time()
    _polls(dewrig, t - 2 * iv)                    # the last poll that got through
    _rate_limited(dewrig)
    p.poll_now(t - iv)
    dew = m.evaluate()["dew"]
    assert dew["is_safe"] is False and dew["blocked"][0].startswith("WU rain check paused")
    dewrig["box"]["throttle"] = None
    _polls(dewrig, t, answered=False)
    st = m.evaluate()
    rain = st["components"]["rain"]
    assert rain["stations_live"] == 0 and rain["throttle"] is None
    assert rain["last_good_poll_age_s"] < 2 * iv + monitor_mod.DEW_WU_MARGIN_SEC
    assert st["is_safe"] is True and st["reasons"] == []        # device 0 as before
    assert st["dew"]["is_safe"] is False
    assert st["dew"]["blocked"] == ["WU rain check not current: the last 2 WU polls "
                                    "failed (cut short by WU's rate limit, or no station "
                                    "reporting) — until a poll gets through"]
    _polls(dewrig, t + 1)                         # a poll gets through whole
    st = m.evaluate()
    assert st["dew"]["is_safe"] is True and st["dew"]["blocked"] == []
    assert st["components"]["rain"]["failed_polls"] == 0
    assert st["components"]["rain"]["last_throttle_ts"] is None


def test_a_second_failed_wu_poll_in_a_row_is_not_tolerated(dewrig):
    m = dewrig["monitor"]
    iv = config.WU_POLL_INTERVAL
    t = time.time()
    _polls(dewrig, t - 2 * iv)                    # the last good poll
    _polls(dewrig, t - iv, answered=False)        # one failed poll: tolerated
    assert m.evaluate()["dew"]["is_safe"] is True
    _polls(dewrig, t, answered=False)             # a second in a row: not
    st = m.evaluate()
    assert st["components"]["rain"]["last_good_poll_age_s"] < (
        2 * iv + monitor_mod.DEW_WU_MARGIN_SEC)   # (the age rule alone would not block)
    assert st["is_safe"] is True and st["reasons"] == []        # device 0 as before
    assert st["dew"]["is_safe"] is False
    assert st["dew"]["blocked"][0].startswith("WU rain check not current: the last 2 WU "
                                              "polls failed")


def test_the_429_pause_waits_for_a_poll_that_gets_through(dewrig):
    # Even with more failed polls allowed, a 429 pauses device 1 until a SUCCESSFUL poll,
    # not until the next poll that is merely not cut short.
    m, p = dewrig["monitor"], dewrig["poller"]
    t = time.time()                               # (the rig's good poll was just before)
    _rate_limited(dewrig)
    p.poll_now(t + 10)
    dewrig["box"]["throttle"] = None              # not cut short, nobody answers
    p.poll_now(t + 20)
    rain = p.component(-20.0, t + 30)
    assert rain["throttle"] is None and rain["last_throttle_ts"] == t + 10
    assert rain["failed_polls"] == 2
    rain["failed_polls"] = 1                      # as if DEW_WU_MAX_POLLS were higher
    assert m._dew_blockers(rain, [], t + 30) == [
        "WU rain check paused: WU's rate limit (HTTP 429) cut a poll short, and none has "
        "got through since"]


def test_a_clock_step_forgets_the_failed_polls_and_the_429(dewrig):
    m, p = dewrig["monitor"], dewrig["poller"]
    t = time.time()
    _rate_limited(dewrig)
    p.poll_now(t - 60)
    p.poll_now(t - 30)
    p.clock_stepped(t - 7200.0, t)
    rain = p.component(-20.0, t)
    assert rain["failed_polls"] == 0 and rain["last_throttle_ts"] is None
    dewrig["box"]["results"] = dewrig["box"]["throttle"] = None
    p.poll_now(t)                                 # the re-poll the step forces
    assert m.evaluate()["dew"]["is_safe"] is True


def test_a_clock_step_makes_device_1_wait_for_the_wu_re_poll(dewrig):
    m, p = dewrig["monitor"], dewrig["poller"]
    t = time.time()
    p.clock_stepped(t - 7200.0, t)                # the poll's age under the old clock
    st = m.evaluate()
    assert st["dew"]["is_safe"] is False
    assert st["dew"]["blocked"] == ["WU rain check not current: no complete WU poll with a "
                                    "station reporting yet"]
    p.poll_now()                                  # the re-poll the step forces
    assert m.evaluate()["dew"]["is_safe"] is True


# ---- C6: device 1 reads false while any gating layer of device 0 is broken -----------------
def _break(rig, layer):
    m = rig["monitor"]
    if layer == "rain (WU)":
        rig["poller"].component = _Broken().component
    elif layer == "forecast":
        rig["nws"].component = _Broken().component    # its outlook (device 1's) still works
    elif layer == "lightning (GLM)":
        rig["glm"].component = _Broken().component
    elif layer == "radar":
        m.radar = _Broken()
    elif layer == "connectivity":
        m.conn = _Broken()
    elif layer == "NWS alerts":
        m.hazards = _StubHazards(RuntimeError("layer bug"))
    else:                                            # enabled, but failed to start
        m.hazards, m.hazards_error = None, "ImportError: no module named nws_alerts"
        layer = "NWS alerts"
    return layer


@pytest.mark.parametrize("layer", ["rain (WU)", "forecast", "lightning (GLM)", "radar",
                                   "connectivity", "NWS alerts", "NWS alerts (start)"])
def test_device_1_reads_false_while_a_gating_layer_is_broken(dewrig, layer):
    m = dewrig["monitor"]
    assert m.evaluate()["dew"]["is_safe"] is True
    name = _break(dewrig, layer)
    st = m.evaluate()
    # device 0: the owner's accepted policy is unchanged — a broken layer is unavailable,
    # no veto from it, with a warning (the NWS alerts layer: its error on the page)
    assert st["is_safe"] is True and st["reasons"] == []
    if name == "NWS alerts":
        assert st["components"]["hazards"]["error"]
    else:
        assert any("layer failed" in w for w in st["warnings"])
    # device 1: the rain sensor stays trusted, and the reason names the layer
    dew = st["dew"]
    assert dew["is_safe"] is False and m.is_dew_safe() is False
    assert ("the %s check of device 0 is broken (a software error; see the daemon log) — "
            "not relied on until it works again" % name) in dew["blocked"]
    assert dew["reasons"][:len(dew["blocked"])] == dew["blocked"]


def test_a_layer_switched_off_on_purpose_does_not_hold_device_1_back(dewrig):
    m = dewrig["monitor"]
    m.radar = m.conn = m.hazards = None              # TTU_SAFETY_RADAR/_CONN/_HAZARDS=0
    st = m.evaluate()
    assert st["dew"]["is_safe"] is True and st["dew"]["blocked"] == []


def test_a_broken_glm_layer_is_not_called_daytime_on_device_1(dewrig):
    dewrig["glm"].component = _Broken().component
    lt = dewrig["monitor"].evaluate()["dew"]["lightning"]
    assert lt["available"] is False
    assert "daytime" not in lt["why"]
    assert lt["why"].startswith("the GLM lightning layer has failed (lightning layer "
                                "error: RuntimeError: layer bug)")


def _failed_glm():
    """The component SafetyMonitor._guarded stands in for a GLM layer that raised."""
    comp = dict(glm_lightning.unavailable_component(config), enabled=True,
                error="lightning layer error: KeyError: 'x'")
    comp.update(safe=True, available=False, failed=True)
    return comp


def test_after_a_broken_glm_layer_a_whole_hold_is_watched_again():
    sky = Sky()                                      # an hour of quiet polls behind it
    assert sky.look()["is_safe"] is True
    sky.advance(60)
    sky.comp = _failed_glm()
    v = sky.look()
    # no lightning data at once (not after the last good poll has aged out), and not
    # "daytime": the layer is broken
    assert v["is_safe"] is False and v["checks"]["lightning"] is None
    assert v["lightning"]["why"] == ("the GLM lightning layer has failed (lightning layer "
                                     "error: KeyError: 'x')")
    assert "daytime" not in " ".join(v["reasons"])
    # working again: what happened while it was broken was not watched, so the 30 min
    # are watched again before device 1 can be true (as after a clock correction)
    v = sky.poll()
    assert v["checks"]["lightning"] is None
    assert v["lightning"]["why"] == "GLM lightning watched for only 0 of the 30 min needed"
    assert sky.wait(25)["checks"]["lightning"] is None
    assert sky.wait(5)["is_safe"] is True


def test_a_flash_counts_on_through_a_broken_glm_layer():
    sky = Sky()
    v = sky.poll(nearest=55.0)                       # inside device 1's 60 km
    assert v["checks"]["lightning"] is False
    sky.advance(60)
    sky.comp = _failed_glm()
    v = sky.look()
    assert v["checks"]["lightning"] is False and v["lightning"]["recent_km"] == 55.0


# ---- F2 / C4: the page's age with an unconfirmed Pi clock; a tab shown again ---------------
def _clock(state):
    return {"clock": {"state": state, "safe": state != "unconfirmed", "enabled": True,
                      "why": None if state != "unconfirmed" else "chronyc is not installed"}}


def _page(tmp_path, monkeypatch, clock_state):
    monkeypatch.delenv("TTU_STATUS_PAGE_STALE_MIN", raising=False)
    monkeypatch.setattr(msp, "CAMERA_ENABLED", False)
    monkeypatch.delenv("STATUS_PAGE_INTERVAL", raising=False)
    comps = _clock(clock_state) if clock_state is not None else {}
    return _write_page(tmp_path, monkeypatch, _state(time.time(), is_safe=False,
                                                     components=comps))


_HARNESS = """
var attrs = %(attrs)s;
var nowMs = %(now)d;
Date.now = function () { return nowMs; };
function ClassList() { this.s = {}; }
ClassList.prototype.add = function (c) { this.s[c] = 1; };
ClassList.prototype.remove = function (c) { delete this.s[c]; };
ClassList.prototype.contains = function (c) { return !!this.s[c]; };
var page = {getAttribute: function (k) { return k in attrs ? attrs[k] : null; },
            classList: new ClassList()};
var box = {hidden: true, textContent: '', className: 'pageage'};
var btn = {textContent: '', setAttribute: function () {}, addEventListener: function () {}};
var handlers = {};
var document = {hidden: false, getElementById: function (id) {
    return id === 'page' ? page : id === 'pageage' ? box : id === 'modebtn' ? btn : null; },
  addEventListener: function (e, f) { handlers['document:' + e] = f; }};
var window = {addEventListener: function (e, f) { handlers['window:' + e] = f; }};
var sessionStorage = {getItem: function () { return null; }, setItem: function () {}};
function setInterval(f, ms) {}
function snap() {
  return {hidden: box.hidden, text: box.textContent, cls: box.className,
          outdated: page.classList.contains('outdated')};
}
%(script)s
var out = [snap()];
var steps = %(steps)s;
for (var i = 0; i < steps.length; i++) {
  nowMs = steps[i][0];
  document.hidden = steps[i][2];
  if (handlers[steps[i][1]]) { handlers[steps[i][1]](); }
  out.push(snap());
}
console.log(JSON.stringify(out));
"""


def _js(page, now_s, steps=()):
    """The page's script run in node at the viewer's time `now_s`; then each step
    (time, event, document.hidden) fires that event at that time. One snapshot of the
    banner after the load and after each step."""
    harness = _HARNESS % {"attrs": json.dumps(_page_attrs(page)), "now": int(now_s * 1000),
                          "script": _script(page),
                          "steps": json.dumps([[int(t * 1000), e, h] for t, e, h in steps])}
    out = subprocess.run([NODE, "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_page_carries_the_daemons_clock_state(tmp_path, monkeypatch):
    for state in ("unconfirmed", "synchronised", "grace", "disabled"):
        assert _page_attrs(_page(tmp_path, monkeypatch, state))["data-clock"] == state
    # no state file, an older daemon, or something odd: "" (the age is judged as before)
    assert _page_attrs(_page(tmp_path, monkeypatch, None))["data-clock"] == ""
    assert msp.page_clock_state(None) == ""
    assert msp.page_clock_state({"components": {"clock": {"state": "<b>"}}}) == ""
    assert msp.page_clock_state({"components": {"clock": ["unconfirmed"]}}) == ""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_an_unconfirmed_pi_clock_never_makes_the_page_look_stopped(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, "unconfirmed")
    t0 = int(_page_attrs(page)["data-generated"])
    # the Pi booted 2 h behind (fake-hwclock), chrony not synchronised yet: the page is
    # regenerated every 90 s, yet by a correct clock it looks 2 h old
    behind = _js(page, t0 + 7200)[0]
    assert not behind["hidden"] and not behind["outdated"]     # the verdict stays shown
    assert behind["cls"] == "pageage note"
    assert behind["text"] == (
        "The clock of the Pi was not confirmed by chrony when this page was generated, so "
        "how old the page is cannot be judged (by the clock of this device it was "
        "generated 120 min ago). The safety monitor reads UNSAFE until chrony confirms the "
        "clock.")
    assert "no longer being updated" not in behind["text"]
    assert "5760 h ago" in _js(page, t0 + 240 * 86400)[0]["text"]
    # a Pi clock AHEAD is not blamed on this device's clock either
    ahead = _js(page, t0 - 600)[0]
    assert not ahead["hidden"] and not ahead["outdated"]
    assert "generated 10 min in the future" in ahead["text"]
    assert "The clock of this device is behind" not in ahead["text"]
    # an age within the limits: nothing to say
    assert _js(page, t0 + 60)[0]["hidden"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("state", ["synchronised", "grace", "disabled", None])
def test_a_confirmed_or_unchecked_clock_keeps_the_stale_banner(tmp_path, monkeypatch, state):
    page = _page(tmp_path, monkeypatch, state)
    t0 = int(_page_attrs(page)["data-generated"])
    old = _js(page, t0 + 16 * 60 + 10)[0]
    assert not old["hidden"] and old["outdated"] and old["cls"] == "pageage"
    assert old["text"] == ("This page was generated 16 min ago and is no longer being "
                           "updated: the safety verdict shown is not current.")
    behind = _js(page, t0 - 600)[0]
    assert behind["cls"] == "pageage note" and "cannot be judged" in behind["text"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_age_is_judged_again_when_the_tab_is_shown(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, "synchronised")
    t0 = int(_page_attrs(page)["data-generated"])
    states = _js(page, t0 + 60, steps=[
        (t0 + 20 * 60, "document:visibilitychange", True),    # hidden: nothing to do
        (t0 + 20 * 60, "document:visibilitychange", False)])  # shown again: judged at once
    assert states[0]["hidden"] and states[1]["hidden"]
    assert not states[2]["hidden"] and states[2]["outdated"]
    assert states[2]["text"].startswith("This page was generated 20 min ago")


def test_without_javascript_an_unconfirmed_clock_is_said_too(tmp_path, monkeypatch):
    page = _page(tmp_path, monkeypatch, "unconfirmed")
    assert ("<noscript><p class=\"pageage note\">Generated 2026-09-24 12:00:00 CDT, by a "
            "clock chrony had not confirmed: that time may be wrong, so how old the page "
            "is cannot be judged from it.</p></noscript>") in page
    plain = _page(tmp_path, monkeypatch, "synchronised")
    assert "This page cannot check its own age without JavaScript" in plain


# ---- C7: CI runs the GLM layer's numeric tests on the Pi's Python ---------------------------
def test_ci_installs_numpy_and_netcdf4_on_the_pis_python():
    with open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8") as f:
        text = f.read()
    steps = [s for s in text.split("\n      - ") if "pip install numpy netCDF4" in s]
    assert len(steps) == 1
    assert "if: matrix.python-version == '3.13'" in steps[0]
    assert "deps_available()" in steps[0]           # fails the job instead of skipping
    # the comments: the Pi runs trixie (3.13), not bookworm (3.11)
    flat = " ".join(line.strip().lstrip("#").strip() for line in text.splitlines())
    assert "3.13 Raspberry Pi OS trixie (the observatory Pi's system python3" in flat
    assert "Bookworm (the Pi's system python3)" not in flat
