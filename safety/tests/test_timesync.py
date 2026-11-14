"""The clock check (safety/timesync.py): device 0 is UNSAFE until chrony is synchronised to
the GPS or an NTP server. No chronyd is needed here: chronyc's CSV output is given as text
(the lines below are chrony 4.x's), and the real subprocess wrapper is run against small
stand-in scripts."""
import json
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from safety import config, timesync
from safety.timesync import ClockSyncWatch, parse_tracking

# the real wrapper: conftest's autouse fixture replaces timesync.run_chronyc in every test
REAL_RUN = timesync.run_chronyc

# `chronyc -n -c tracking`: synchronised to an NTP server, to the GPS's PPS reference
# clock, not synchronised at all, and to chrony's "local" reference (the Pi's own clock,
# `local stratum 10` in chrony.conf; chrony leaves its name empty). The NTP, unsynchronised
# and local lines are chrony 4.x's own output (review of 2026-11-13); the PPS one is laid
# out the same way.
NTP = ("8176014E,129.118.1.78,4,1794599964.606847838,0.000027697,-0.000031624,"
       "0.000121956,3.690,-0.002,0.226,0.070391133,0.022161044,516.8,Normal")
PPS = ("50505300,PPS,1,1794600000.000000000,-0.000000150,-0.000000210,0.000000480,"
       "-7.112,0.000,0.004,0.000000001,0.000011023,16.0,Normal")
UNSYNC = ("00000000,,0,0.000000000,0.000000000,0.000000000,0.000000000,0.000,0.000,"
          "0.000,1.000000000,1.000000000,0.0,Not synchronised")
LOCAL = ("7F7F0101,,10,1794600000.000000000,0.000000000,0.000000000,"
         "0.000000000,0.000,0.000,0.000,0.000000000,0.000000000,64.0,Normal")


def _cfg(**over):
    c = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    for k, v in over.items():
        setattr(c, k, v)
    return c


class _Events:
    def __init__(self):
        self.records = []

    def record(self, kind, **kw):
        self.records.append((kind, kw))
        return {}

    def recent(self, n):
        return []

    def kinds(self):
        return [k for k, _ in self.records]


class _Chrony:
    """Stands in for the chronyc call: answers ``out`` (a line), or raises it (an
    exception)."""

    def __init__(self, out=NTP):
        self.out, self.calls = out, 0

    def __call__(self, cfg):
        self.calls += 1
        if isinstance(self.out, BaseException):
            raise self.out
        return self.out


def _watch(chrony, events=None, **over):
    return ClockSyncWatch(_cfg(**over), events if events is not None else _Events(),
                          runner=chrony)


# ---- what chrony's report means ---------------------------------------------------------
def test_synchronised_to_an_ntp_server_is_confirmed():
    r = parse_tracking(NTP, 5.0)
    assert r["ok"] is True and r["why"] is None
    assert (r["reference"], r["stratum"], r["leap"]) == ("129.118.1.78", 4, "Normal")
    # chrony's own bound on the clock's error: |offset| + root delay / 2 + root dispersion
    # (no pytest.approx: the page tests leave a stand-in numpy in sys.modules, which it trips on)
    assert abs(r["max_error_s"] - (0.000027697 + 0.070391133 / 2 + 0.022161044)) < 1e-12


def test_synchronised_to_the_gps_is_confirmed():
    r = parse_tracking(PPS, 5.0)
    assert r["ok"] is True and (r["reference"], r["stratum"]) == ("PPS", 1)


@pytest.mark.parametrize("line, why", [
    (UNSYNC, "chrony is not synchronised to any time source yet"),
    (NTP.replace(",Normal", ",Not synchronised"), "not synchronised to any time source"),
    (NTP.replace("8176014E", "00000000"), "not synchronised to any time source"),
    (NTP.replace(",4,", ",0,", 1), "not synchronised to any time source"),
    (LOCAL, "synchronised only to this Pi"),
    (NTP.replace(",4,", ",16,", 1), "stratum 16"),
])
def test_what_is_not_synchronisation_to_a_real_source(line, why):
    r = parse_tracking(line, 5.0)
    assert r["ok"] is False and why in r["why"]


def test_a_large_error_bound_is_not_confirmed():
    # chrony can report its source while it is still slewing a big error away
    r = parse_tracking(NTP.replace("0.000027697", "-42.000000000", 1), 5.0)
    assert r["ok"] is False and "bounds the clock's error at 42.1 s" in r["why"]
    # the limit itself is still fine, a hair under it is not
    err = abs(0.000027697) + abs(0.070391133) / 2.0 + abs(0.022161044)
    assert parse_tracking(NTP, err)["ok"] is True
    assert parse_tracking(NTP, err - 1e-9)["ok"] is False


def test_error_bounds_are_shown_in_peoples_units():
    assert [timesync.describe_error(v) for v in (0.0000111735, 0.0517, 1.5, 42.0, -2.0)] \
        == ["11 µs", "52 ms", "1.5 s", "42 s", "2 s"]
    assert {timesync.describe_error(v) for v in (None, "x", float("nan"))} == {"?"}


@pytest.mark.parametrize("text", [
    "", None, "506 Cannot talk to daemon", "a,b,c",
    NTP.replace(",4,", ",four,", 1),                   # stratum not a number
    NTP.replace("0.000027697", "nan", 1),              # offset not finite
    NTP.replace("0.070391133", "x", 1),                # root delay not a number
])
def test_output_that_cannot_be_read_is_not_confirmed(text):
    r = parse_tracking(text, 5.0)
    assert r["ok"] is False and r["why"]


# ---- asking chrony ----------------------------------------------------------------------
def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(0o755)
    return str(p)


@pytest.mark.skipif(sys.platform == "win32", reason="shell stand-ins")
def test_the_wrapper_runs_chronyc_numeric_csv_and_reports_its_failure(tmp_path, monkeypatch):
    ok = _script(tmp_path, "ok", 'echo "%s"' % NTP)
    assert REAL_RUN(_cfg(CLOCK_CHRONYC=ok)).strip() == NTP
    args = _script(tmp_path, "args", "printf '%s\\n' \"$*\"")
    # -n: no DNS lookup that could hang; -c: the CSV parsed above
    assert REAL_RUN(_cfg(CLOCK_CHRONYC=args)).strip() == "-n -c tracking"
    down = _script(tmp_path, "down", 'echo "506 Cannot talk to daemon" >&2; exit 1')
    with pytest.raises(RuntimeError, match="506 Cannot talk to daemon"):
        REAL_RUN(_cfg(CLOCK_CHRONYC=down))
    with pytest.raises(FileNotFoundError):
        REAL_RUN(_cfg(CLOCK_CHRONYC=str(tmp_path / "missing")))
    # "command not found" reported as exit status 127 (how a missing program looks under
    # QEMU, where Python cannot see the exec error) is the same "not installed"
    gone = _script(tmp_path, "gone", "exit 127")
    with pytest.raises(FileNotFoundError):
        REAL_RUN(_cfg(CLOCK_CHRONYC=gone))
    other = _script(tmp_path, "other", "exit 3")
    with pytest.raises(RuntimeError, match="exit status 3"):
        REAL_RUN(_cfg(CLOCK_CHRONYC=other))
    monkeypatch.setattr(timesync, "RUN_TIMEOUT_SEC", 0.3)
    # (exec: the timeout kills the sleep itself — a plain `sleep` would be orphaned and live
    # on in the test's working directory, which in CI kept the image from unmounting)
    hung = _script(tmp_path, "hung", "exec sleep 5")
    with pytest.raises(subprocess.TimeoutExpired):
        REAL_RUN(_cfg(CLOCK_CHRONYC=hung))


@pytest.mark.parametrize("exc, why", [
    (FileNotFoundError("chronyc"), "chronyc is not installed"),
    (subprocess.TimeoutExpired("chronyc", 10), "chronyc did not answer within 10 s"),
    (RuntimeError("506 Cannot talk to daemon"), "cannot ask chrony (506 Cannot talk to daemon)"),
    (PermissionError("denied"), "cannot ask chrony (denied)"),
])
def test_when_chrony_cannot_be_asked_the_clock_is_not_confirmed(exc, why):
    w = _watch(_Chrony(exc))
    w.check_now(mono=100.0)
    c = w.component(mono=100.0)
    assert c["safe"] is False and c["state"] == "unconfirmed" and why in c["why"]


# ---- the verdict over time (monotonic) ----------------------------------------------------
def test_unsafe_until_a_check_confirms_the_clock():
    ch = _Chrony(UNSYNC)
    w = _watch(ch)
    c = w.component(mono=0.0)
    assert c["safe"] is False and c["why"] == "the clock has not been checked yet"
    w.check_now(mono=1.0)
    c = w.component(mono=1.0)
    assert c["safe"] is False and "not synchronised" in c["why"]
    ch.out = PPS
    w.check_now(mono=31.0)
    c = w.component(mono=31.0)
    assert c["safe"] is True and c["state"] == "synchronised" and c["synchronised"] is True
    assert (c["reference"], c["stratum"], c["why"]) == ("PPS", 1, None)


def test_a_failed_check_is_tolerated_for_the_grace_once_the_clock_was_confirmed():
    ch = _Chrony(NTP)
    w = _watch(ch, CLOCK_GRACE_SEC=600)
    w.check_now(mono=0.0)
    ch.out = RuntimeError("506 Cannot talk to daemon")        # chronyd restarting
    w.check_now(mono=30.0)
    c = w.component(mono=30.0)
    assert c["safe"] is True and c["state"] == "grace" and c["grace_left_s"] == 570
    assert "506" in c["why"] and c["synchronised"] is False
    assert w.component(mono=600.0)["state"] == "grace"
    c = w.component(mono=601.0)
    assert c["safe"] is False and c["state"] == "unconfirmed"
    ch.out = NTP                                               # chrony back
    w.check_now(mono=630.0)
    assert w.component(mono=630.0)["state"] == "synchronised"


def test_no_grace_without_a_clock_confirmed_in_this_run():
    w = _watch(_Chrony(UNSYNC))
    w.check_now(mono=0.0)
    assert w.component(mono=0.0)["safe"] is False
    ch = _Chrony(NTP)
    w = _watch(ch, CLOCK_GRACE_SEC=0)                          # grace switched off
    w.check_now(mono=0.0)
    ch.out = UNSYNC
    w.check_now(mono=30.0)
    assert w.component(mono=30.0)["safe"] is False


def test_an_old_answer_is_no_answer():
    # the clock thread hung: after three missed checks (and one chronyc timeout) the last
    # "synchronised" no longer counts — and once the grace is over, device 0 is unsafe
    w = _watch(_Chrony(NTP), CLOCK_CHECK_SEC=30, CLOCK_GRACE_SEC=0)
    w.check_now(mono=0.0)
    limit = 3 * 30 + timesync.RUN_TIMEOUT_SEC
    assert w.component(mono=limit)["safe"] is True
    c = w.component(mono=limit + 1)
    assert c["safe"] is False and c["why"] == "no clock check for %d s" % (limit + 1)
    w = _watch(_Chrony(NTP), CLOCK_CHECK_SEC=30, CLOCK_GRACE_SEC=600)
    w.check_now(mono=0.0)
    assert w.component(mono=500.0)["state"] == "grace"
    assert w.component(mono=601.0)["safe"] is False


def test_a_clock_step_ends_the_grace_and_asks_for_a_check_at_once():
    w = _watch(_Chrony(NTP), CLOCK_GRACE_SEC=600)
    w.check_now(mono=0.0)
    w.clock_stepped(1000.0, 5000.0)
    c = w.component(mono=1.0)
    assert c["safe"] is False and c["why"] == "the clock has not been checked yet"
    t0 = time.monotonic()
    w.wait(threading.Event(), 30)                              # returns at once
    assert time.monotonic() - t0 < 2.0
    w.check_now(mono=2.0)
    assert w.component(mono=2.0)["safe"] is True


def test_wait_ends_on_stop_or_after_the_interval():
    w = _watch(_Chrony())
    stop = threading.Event()
    stop.set()
    t0 = time.monotonic()
    w.wait(stop, 30)
    assert time.monotonic() - t0 < 0.5
    stop.clear()
    t0 = time.monotonic()
    w.wait(stop, 0.2)
    assert 0.15 <= time.monotonic() - t0 < 1.5


def test_changes_are_logged_once_and_a_recovery_is_an_event():
    ev = _Events()
    ch = _Chrony(UNSYNC)
    w = _watch(ch, ev)
    for m in (0.0, 30.0, 60.0):
        w.check_now(mono=m)
    assert ev.kinds() == ["CLOCK-SYNC"]                        # one, not one per check
    assert "not synchronised" in ev.records[0][1]["reason"]
    assert "unsafe until chrony is synchronised" in ev.records[0][1]["result"]
    ch.out = NTP
    w.check_now(mono=90.0)
    w.check_now(mono=120.0)
    assert ev.kinds() == ["CLOCK-SYNC", "CLOCK-SYNC"]
    assert ev.records[1][1]["result"] == "clock confirmed"
    assert "synchronised to 129.118.1.78 (stratum 4" in ev.records[1][1]["reason"]


def test_a_daemon_starting_with_a_synchronised_clock_writes_no_event():
    ev = _Events()
    w = _watch(_Chrony(PPS), ev)
    w.check_now(mono=0.0)
    w.check_now(mono=30.0)
    assert ev.kinds() == []


# ---- device 0 -----------------------------------------------------------------------------
def test_device0_is_unsafe_until_the_clock_is_confirmed(env, write_inputs):
    from safety.monitor import SafetyMonitor
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    ch = _Chrony(UNSYNC)
    w = ClockSyncWatch(env["cfg"], env["log"], runner=ch)
    w.check_now()
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], clock=w)
    st = m.evaluate()
    assert st["is_safe"] is False
    # the clock comes first: every other check is computed from it
    assert st["reasons"][0] == ("system clock not confirmed by chrony — chrony is not "
                                "synchronised to any time source yet (unsafe until chrony "
                                "is synchronised to the GPS or an NTP server)")
    assert st["components"]["clock"]["state"] == "unconfirmed"
    ch.out = NTP
    w.check_now()
    st = m.evaluate()
    assert st["is_safe"] is True and st["components"]["clock"]["synchronised"] is True


def test_the_grace_is_a_warning_not_a_veto(env, write_inputs):
    from safety.monitor import SafetyMonitor
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    ch = _Chrony(NTP)
    w = ClockSyncWatch(env["cfg"], env["log"], runner=ch)
    w.check_now()
    ch.out = RuntimeError("506 Cannot talk to daemon")
    w.check_now()
    st = SafetyMonitor(env["cfg"], env["log"], env["poller"], clock=w).evaluate()
    assert st["is_safe"] is True
    assert any(x.startswith("chrony could not confirm the clock (cannot ask chrony (506")
               for x in st["warnings"])


def test_a_broken_clock_check_fails_safe(env, write_inputs):
    from safety.monitor import SafetyMonitor
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)

    class _Broken:
        def component(self):
            raise RuntimeError("boom")
    st = SafetyMonitor(env["cfg"], env["log"], env["poller"], clock=_Broken()).evaluate()
    assert st["is_safe"] is False and "clock check error: boom" in st["reasons"][0]


def test_without_the_clock_check_there_is_no_veto(env, write_inputs):
    from safety.monitor import SafetyMonitor
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    st = SafetyMonitor(env["cfg"], env["log"], env["poller"]).evaluate()
    assert st["is_safe"] is True
    assert st["components"]["clock"]["state"] == "disabled"


def test_setup_page_shows_the_clock(tmp_path, monkeypatch):
    from safety.tests.test_alpaca import _client
    c, mon = _client(tmp_path, monkeypatch)
    ch = _Chrony(PPS)
    mon.clock = ClockSyncWatch(config, mon.log, runner=ch)
    mon.clock.check_now()
    page = c.get("/setup").get_data(as_text=True)
    assert "System clock (chrony)" in page
    # 0.15 µs offset + 0.5 ns + 11.023 µs dispersion: chrony's bound, in people's units
    assert "synchronised to PPS (stratum 1, error ≤ 11 µs)" in page
    ch.out = UNSYNC                                        # chronyd restarted: the grace
    mon.clock.check_now()
    page = c.get("/setup").get_data(as_text=True)
    assert "not confirmed (chrony is not synchronised to any time source yet) — tolerated " \
           "for another 10 min" in page
    mon.clock = ClockSyncWatch(config, mon.log, runner=_Chrony(UNSYNC))
    mon.clock.check_now()
    page = c.get("/setup").get_data(as_text=True)
    assert "NOT CONFIRMED — chrony is not synchronised to any time source yet; unsafe " \
           "until chrony is synchronised" in page and "UNSAFE" in page
    mon.clock = None
    page = c.get("/setup").get_data(as_text=True)
    assert "check switched off (TTU_SAFETY_CLOCK_SYNC=0)" in page


# ---- the daemon ----------------------------------------------------------------------------
def _run_daemon(monkeypatch, tmp_path, during=None, order=None):
    """server.main() with every network layer off and every external effect stubbed; the
    threads it starts are recorded (name -> target, and their names in ``order``), not
    started. ``during(threads, monitor)`` runs while the daemon "serves"."""
    from safety import server
    for key, name in (("LATCH_FILE", "l.json"), ("INPUTS_FILE", "i.json"),
                      ("STATE_FILE", "s.json"), ("EVENT_LOG", "e.log")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    for flag in ("NWS_ENABLED", "GLM_ENABLED", "RADAR_ENABLED", "HAZARDS_ENABLED",
                 "HAZARD_FEEDS_ENABLED", "CONN_ENABLED", "PAGE_ENABLED", "DEW_ENABLED",
                 "WEATHER_LOG_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(server.discovery, "start", lambda port: None)
    monkeypatch.setattr(server, "install_sigterm_handler", lambda: True)
    monkeypatch.setattr(server, "ignore_stop_signals", lambda: None)
    run = {"threads": {}, "order": [] if order is None else order}

    def start(target, name):
        run["threads"][name] = target
        run["order"].append(name)
    monkeypatch.setattr(server, "_start_thread", start)

    def create_app(monitor, cfg):
        run["monitor"] = monitor
        return object()
    monkeypatch.setattr(server, "create_app", create_app)

    def serve(app, **kw):
        if during is not None:
            during(run["threads"], run["monitor"])
        raise KeyboardInterrupt
    monkeypatch.setitem(sys.modules, "waitress", types.SimpleNamespace(serve=serve))
    server.main()
    return run


def test_the_daemon_checks_the_clock_before_its_first_verdict(monkeypatch, tmp_path):
    from safety import server
    order = []

    class _W(ClockSyncWatch):
        def check_now(self, mono=None):
            order.append("check")
            return super().check_now(mono)
    monkeypatch.setattr(server, "ClockSyncWatch",
                        lambda cfg, ev: _W(cfg, ev, runner=_Chrony(NTP)))
    run = _run_daemon(monkeypatch, tmp_path, order=order)
    # the first check runs before any thread starts — so the first verdict already knows
    # the clock — and the clock thread starts before the evaluator
    assert order[0] == "check" and order.count("check") == 1
    assert order.index("clock-check") < order.index("evaluator")
    assert isinstance(run["monitor"].clock, _W)
    assert run["monitor"].clock.component()["state"] == "synchronised"


def test_switched_off_there_is_no_clock_thread_and_no_check(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CLOCK_SYNC_ENABLED", False)
    run = _run_daemon(monkeypatch, tmp_path)
    assert "clock-check" not in run["order"] and run["monitor"].clock is None


def test_a_clock_step_reaches_the_clock_check(monkeypatch, tmp_path):
    # the evaluator's step detector runs for real, with the wall clock jumping an hour
    # ahead of the monotonic one: the clock check must drop its verdict
    from safety import server
    steps = []

    class _W(ClockSyncWatch):
        def clock_stepped(self, pre, post):
            steps.append(post - pre)
            super().clock_stepped(pre, post)
    monkeypatch.setattr(server, "ClockSyncWatch",
                        lambda cfg, ev: _W(cfg, ev, runner=_Chrony(NTP)))
    monkeypatch.setattr(config, "EVAL_INTERVAL", 0.05)
    real = time.time
    offset = [0.0]

    def during(threads, monitor):
        monkeypatch.setattr(time, "time", lambda: real() + offset[0])
        threading.Thread(target=threads["evaluator"], daemon=True).start()
        time.sleep(0.3)
        offset[0] = 3600.0
        deadline = time.monotonic() + 5
        while not steps and time.monotonic() < deadline:
            time.sleep(0.05)
        assert monitor.clock.component()["safe"] is False      # until the next check
    _run_daemon(monkeypatch, tmp_path, during)
    assert steps and 3500 < steps[0] < 3700


# ---- settings ------------------------------------------------------------------------------
def _config_with(**env):
    """The settings a daemon started with this environment would run with (a child
    process: the module reads the environment once, at import)."""
    code = ("import json; from safety import config as c; print(json.dumps({"
            "'on': c.CLOCK_SYNC_ENABLED, 'every': c.CLOCK_CHECK_SEC, "
            "'err': c.CLOCK_MAX_ERROR_SEC, 'grace': c.CLOCK_GRACE_SEC, "
            "'chronyc': c.CLOCK_CHRONYC, 'warnings': c.CONFIG_WARNINGS}))")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TTU_SAFETY_")}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=dict(clean, **env),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_clock_settings_from_the_environment():
    plain = _config_with()
    assert (plain["on"], plain["every"], plain["err"], plain["grace"], plain["chronyc"]) \
        == (True, 30, 1.0, 600, "chronyc")
    assert not any("CLOCK" in w for w in plain["warnings"])
    off = _config_with(TTU_SAFETY_CLOCK_SYNC="0")
    assert off["on"] is False
    assert any("TTU_SAFETY_CLOCK_SYNC=0: the clock check is OFF" in w for w in off["warnings"])
    odd = _config_with(TTU_SAFETY_CLOCK_CHECK_SEC="1", TTU_SAFETY_CLOCK_MAX_ERROR_SEC="600",
                       TTU_SAFETY_CLOCK_GRACE_SEC="-5", TTU_SAFETY_CHRONYC="  ")
    assert (odd["every"], odd["err"], odd["grace"], odd["chronyc"]) == (5, 60.0, 0, "chronyc")
    assert sum("TTU_SAFETY_CLOCK_" in w for w in odd["warnings"]) == 3
    # an unknown word does not switch the check off, and says so
    typo = _config_with(TTU_SAFETY_CLOCK_SYNC="of")
    assert typo["on"] is True and any("TTU_SAFETY_CLOCK_SYNC" in w for w in typo["warnings"])


def test_an_answer_overtaken_by_a_clock_step_is_not_kept():
    # chronyc answers "synchronised" for the clock BEFORE a step that lands while it runs:
    # that answer must not confirm the clock after the step (nor log a spurious pair of
    # CLOCK-SYNC events); the check the step asked for decides
    ev = _Events()
    w = _watch(None, ev)
    started, release = threading.Event(), threading.Event()

    def slow(cfg):
        started.set()
        release.wait(5)
        return NTP
    w._runner = slow
    t = threading.Thread(target=w.check_now)
    t.start()
    assert started.wait(5)
    w.clock_stepped(1000.0, 4600.0)                 # the step, while chronyc is asked
    release.set()
    t.join(5)
    c = w.component()
    assert c["safe"] is False and c["why"] == "the clock has not been checked yet"
    assert ev.kinds() == []                         # nothing logged for the stale answer
    w._runner = _Chrony(NTP)
    w.check_now()                                   # the check the step asked for
    assert w.component()["state"] == "synchronised"


def test_a_grace_under_a_minute_is_said_in_seconds(env, write_inputs):
    from safety.monitor import SafetyMonitor
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    ch = _Chrony(NTP)
    w = _watch(ch, CLOCK_GRACE_SEC=600)
    w.check_now(mono=0.0)
    ch.out = RuntimeError("506 Cannot talk to daemon")
    w.check_now(mono=555.0)
    w.component = (lambda real: lambda mono=None: real(555.0))(w.component)
    st = SafetyMonitor(env["cfg"], env["log"], env["poller"], clock=w).evaluate()
    assert any("tolerated for another 45 s" in x for x in st["warnings"]), st["warnings"]


def test_the_default_error_bound_is_one_second():
    assert config.CLOCK_MAX_ERROR_SEC == 1.0             # the owner's choice, 2026-11-13
    # the GPS's NMEA alone (no PPS yet): root delay 0.2 s, as chrony.conf usually sets it
    # for an NMEA reference clock — about 0.12 s: synchronised
    nmea = NTP.replace("8176014E,129.118.1.78,4", "4E4D4541,NMEA,1", 1).replace(
        "0.070391133", "0.200000000", 1)
    r = parse_tracking(nmea, config.CLOCK_MAX_ERROR_SEC)
    assert r["ok"] is True and r["reference"] == "NMEA"
    # 1.2 s of offset that chrony is still slewing away: not yet
    r = parse_tracking(NTP.replace("0.000027697", "1.200000000", 1), config.CLOCK_MAX_ERROR_SEC)
    assert r["ok"] is False and "more than the 1 s allowed" in r["why"]
