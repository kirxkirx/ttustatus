"""Operations hardening: the single-instance lock, the start-up sweep of temp files a
killed run left behind, and /setup with text that cannot be encoded as it came."""
import errno
import fcntl
import logging
import os
import stat
import subprocess
import sys
import textwrap
import time
import types

import pytest

from safety import config, housekeeping

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---- the run lock --------------------------------------------------------------------

def test_lock_is_private_not_inherited_and_records_the_pid(tmp_path):
    path = str(tmp_path / "run.lock")
    fd = housekeeping.run_lock(path)
    try:
        st = os.stat(path)
        assert stat.S_IMODE(st.st_mode) == 0o600
        assert not os.get_inheritable(fd)                 # the page run never gets it
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        with open(path, encoding="ascii") as f:
            assert f.read().split()[0] == str(os.getpid())
    finally:
        housekeeping.release_lock(fd)


def test_second_lock_is_already_running_with_the_holders_pid(tmp_path):
    path = str(tmp_path / "run.lock")
    fd = housekeeping.run_lock(path)
    try:
        with pytest.raises(housekeeping.AlreadyRunning) as ei:
            housekeeping.run_lock(path)       # a separate open file: flock conflicts
        assert ei.value.pid == os.getpid()
    finally:
        housekeeping.release_lock(fd)
    housekeeping.release_lock(housekeeping.run_lock(path))   # free again once released


def test_wider_mode_is_narrowed(tmp_path):
    path = tmp_path / "run.lock"
    path.write_text("")
    os.chmod(str(path), 0o644)
    housekeeping.release_lock(housekeeping.run_lock(str(path)))
    assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o600


def test_symlinked_lock_file_is_an_error_not_already_running(tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("keep me\n")
    link = tmp_path / "run.lock"
    os.symlink(str(target), str(link))
    with pytest.raises(housekeeping.LockError) as ei:
        housekeeping.run_lock(str(link))
    assert not isinstance(ei.value, housekeeping.AlreadyRunning)
    assert "symbolic link" in str(ei.value)
    assert target.read_text() == "keep me\n"              # never followed


def test_lock_file_owned_by_someone_else_is_an_error(tmp_path, monkeypatch):
    path = str(tmp_path / "run.lock")
    monkeypatch.setattr(housekeeping.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(housekeeping.LockError):
        housekeeping.run_lock(path)


def test_only_ewouldblock_means_already_running(tmp_path, monkeypatch):
    def nolck(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(housekeeping.fcntl, "flock", nolck)
    with pytest.raises(housekeeping.LockError) as ei:
        housekeeping.run_lock(str(tmp_path / "run.lock"))
    assert not isinstance(ei.value, housekeeping.AlreadyRunning)


# ---- the daemon with the lock ----------------------------------------------------------

@pytest.fixture
def daemon(tmp_path, monkeypatch):
    """server.main() with every layer off and the HTTP server stubbed; records what it
    started and whether it served."""
    from safety import server
    for key, name in (("LATCH_FILE", "l.json"), ("INPUTS_FILE", "i.json"),
                      ("STATE_FILE", "s.json"), ("EVENT_LOG", "events.log")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    for flag in ("NWS_ENABLED", "GLM_ENABLED", "RADAR_ENABLED", "HAZARDS_ENABLED",
                 "HAZARD_FEEDS_ENABLED", "CONN_ENABLED", "PAGE_ENABLED", "DEW_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "WEATHER_LOG_ENABLED", True)
    monkeypatch.setattr(config, "WEATHER_LOG_RAM_DIR", str(tmp_path / "ram"))
    monkeypatch.setattr(config, "WEATHER_LOG_DIR", str(tmp_path / "sd"))
    rec = {"threads": [], "served": 0, "weatherlogs": 0, "lock_free_while_serving": None}
    monkeypatch.setattr(server.discovery, "start",
                        lambda port: rec["threads"].append("discovery"))
    monkeypatch.setattr(server, "install_sigterm_handler", lambda: True)
    monkeypatch.setattr(server, "ignore_stop_signals", lambda: None)
    monkeypatch.setattr(server, "_start_thread",
                        lambda target, name: rec["threads"].append(name))
    real_wl = server.WeatherLog

    def weatherlog(*a, **kw):
        rec["weatherlogs"] += 1
        return real_wl(*a, **kw)
    monkeypatch.setattr(server, "WeatherLog", weatherlog)
    monkeypatch.setattr(server, "create_app", lambda monitor, cfg: object())

    def serve(app, **kw):
        rec["served"] += 1
        try:        # while serving, the lock is held
            housekeeping.release_lock(housekeeping.run_lock(config.LOCK_FILE))
            rec["lock_free_while_serving"] = True
        except housekeeping.AlreadyRunning:
            rec["lock_free_while_serving"] = False
        except housekeeping.LockError:
            pass                                # an unusable lock file: nothing to tell
    monkeypatch.setitem(sys.modules, "waitress", types.SimpleNamespace(serve=serve))
    rec["main"] = server.main
    return rec


def test_second_daemon_exits_1_having_started_nothing(daemon, caplog):
    holder = housekeeping.run_lock(config.LOCK_FILE)     # the instance already running
    try:
        caplog.set_level(logging.INFO, logger="ttu.safety")
        with pytest.raises(SystemExit) as ei:
            daemon["main"]()
        assert ei.value.code == 1
    finally:
        housekeeping.release_lock(holder)
    assert daemon["threads"] == [] and daemon["served"] == 0
    assert daemon["weatherlogs"] == 0                     # no shutdown flush either
    assert not os.path.exists(config.EVENT_LOG)           # not even a STARTUP line
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "another instance is running (pid %d)" % os.getpid() in msg


def test_daemon_holds_the_lock_while_serving_and_releases_it(daemon):
    daemon["main"]()
    assert daemon["served"] == 1 and "evaluator" in daemon["threads"]
    assert daemon["lock_free_while_serving"] is False
    housekeeping.release_lock(housekeeping.run_lock(config.LOCK_FILE))   # released


def test_unusable_lock_file_warns_and_runs_unguarded(daemon, tmp_path, monkeypatch, caplog):
    target = tmp_path / "victim"
    target.write_text("x")
    link = tmp_path / "linked.lock"
    os.symlink(str(target), str(link))
    monkeypatch.setattr(config, "LOCK_FILE", str(link))
    caplog.set_level(logging.WARNING, logger="ttu.safety")
    daemon["main"]()
    assert daemon["served"] == 1                          # it runs all the same
    assert any("WITHOUT the guard" in r.getMessage() for r in caplog.records)
    assert target.read_text() == "x"


def test_lock_held_by_another_process_stops_the_daemon(daemon, tmp_path):
    """A real second process (the old run_safety_monitor.sh loop) holding the lock."""
    ready = tmp_path / "ready"
    code = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        from safety import housekeeping
        fd = housekeeping.run_lock(%r)
        open(%r, "w").close()
        time.sleep(30)
    """ % (HERE, config.LOCK_FILE, str(ready)))
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        with pytest.raises(SystemExit) as ei:
            daemon["main"]()
        assert ei.value.code == 1 and daemon["threads"] == []
    finally:
        proc.kill()
        proc.wait()
    daemon["main"]()                                      # the holder died: lock is free
    assert daemon["served"] == 1


# ---- the sweep of leftovers --------------------------------------------------------------

def _make(path, age_s, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


@pytest.fixture
def leftovers(tmp_path, monkeypatch):
    shm = str(tmp_path / "shm")
    state_dir = str(tmp_path / "state")
    monkeypatch.setattr(housekeeping, "SHM_DIR", shm)
    monkeypatch.setattr(config, "STATE_FILE", os.path.join(state_dir, "safety_state.json"))
    monkeypatch.setattr(config, "INPUTS_FILE", os.path.join(shm, "safety_inputs.json"))
    old = 2 * 3600
    gone = [_make(os.path.join(shm, "ttu-glm-a1b2c3.nc"), old),
            _make(os.path.join(state_dir, ".safety_state.x7yq.tmp"), old),
            _make(os.path.join(shm, "safety_inputs.json.tmp"), old),
            _make(os.path.join(shm, "status_page_cache.json.tmp"), old),
            _make(os.path.join(shm, "status.html.tmp"), old)]
    kept = [_make(os.path.join(shm, "ttu-glm-young.nc"), 60),          # maybe in use
            _make(os.path.join(shm, "tmpabc123.nc"), old),             # not our name
            _make(os.path.join(shm, "safety_inputs.json"), old),       # the real file
            _make(os.path.join(shm, "other.html.tmp"), old),
            _make(os.path.join(state_dir, "safety_state.json"), old),
            _make(os.path.join(state_dir, "safety_state.x.tmp"), old)]  # no leading dot
    victim = _make(str(tmp_path / "victim.nc"), old)
    os.symlink(victim, os.path.join(shm, "ttu-glm-link.nc"))
    os.utime(os.path.join(shm, "ttu-glm-link.nc"), (time.time() - old,) * 2,
             follow_symlinks=False)
    os.makedirs(os.path.join(shm, "ttu-glm-dir.nc"))
    kept += [victim, os.path.join(shm, "ttu-glm-link.nc"), os.path.join(shm, "ttu-glm-dir.nc")]
    return gone, kept


def test_sweep_removes_only_old_regular_files_of_our_own_names(leftovers, caplog):
    gone, kept = leftovers
    caplog.set_level(logging.INFO, logger="ttu.safety")
    removed = housekeeping.sweep_leftovers(config)
    assert sorted(removed) == sorted(gone)
    for p in gone:
        assert not os.path.lexists(p)
    for p in kept:
        assert os.path.lexists(p), p
    logged = " ".join(r.getMessage() for r in caplog.records)
    for p in gone:
        assert p in logged


def test_sweep_leaves_other_users_files(leftovers, monkeypatch):
    gone, _ = leftovers
    monkeypatch.setattr(housekeeping.os, "geteuid", lambda: os.getuid() + 1)
    assert housekeeping.sweep_leftovers(config) == []
    assert all(os.path.exists(p) for p in gone)


def test_sweep_never_raises_on_missing_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(housekeeping, "SHM_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(config, "STATE_FILE", str(tmp_path / "nope2" / "s.json"))
    monkeypatch.setattr(config, "INPUTS_FILE", str(tmp_path / "nope3" / "i.json"))
    assert housekeeping.sweep_leftovers(config) == []


def test_daemon_start_sweeps_but_a_second_copy_does_not(daemon, leftovers):
    gone, kept = leftovers
    holder = housekeeping.run_lock(config.LOCK_FILE)
    try:
        with pytest.raises(SystemExit):
            daemon["main"]()
    finally:
        housekeeping.release_lock(holder)
    assert all(os.path.exists(p) for p in gone)          # not the second copy's business
    daemon["main"]()
    assert not any(os.path.exists(p) for p in gone)
    assert all(os.path.lexists(p) for p in kept)


# ---- /setup with text that cannot be encoded as it came --------------------------------

LONE = "Gusts \ud83d to 50 mph"          # a JSON "\ud83d" escape decodes to a lone surrogate
BIDI = "Extreme‮ emertxE‬"


def _setup_client(env, write_inputs, monkeypatch):
    pytest.importorskip("flask")
    from safety.alpaca import create_app
    mon = env["monitor"]
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    real = mon.evaluate

    def evaluate():
        st = real()
        st["reasons"] = list(st.get("reasons") or []) + ["NWS: " + LONE]
        st["warnings"] = list(st.get("warnings") or []) + [LONE]
        st["events_tail"] = list(st.get("events_tail") or []) + ["HAZARD " + LONE]
        st["components"]["hazards"] = {
            "enabled": True, "available": True, "safe": False,
            "veto_events": ["Tornado Warning"],
            "veto": [{"event": "Tornado \ud800Warning" + "‮", "end_local": "21:00",
                      "source": "point"}],
            "error": LONE,
            "alerts": [{"event": "High Wind Warning", "headline": LONE + BIDI,
                        "sender": "NWS \udfff Lubbock", "end_local": "22:00",
                        "color": "#ff0000"}]}
        st["components"]["nws"] = {"available": False, "error": LONE, "safe": True}
        return st
    monkeypatch.setattr(mon, "evaluate", evaluate)
    app = create_app(mon, env["cfg"])
    app.testing = True
    return app.test_client()


def test_setup_with_a_lone_surrogate_is_served_not_500(env, write_inputs, monkeypatch):
    c = _setup_client(env, write_inputs, monkeypatch)
    for url in ("/setup", "/", "/setup/v1/safetymonitor/0/setup"):
        r = c.get(url)
        assert r.status_code == 200, url
        text = r.get_data().decode("utf-8")            # strict: it IS valid UTF-8
        assert "Gusts � to 50 mph" in text
        assert "‮" not in text and "‬" not in text
    # the JSON route stays as it was: escaped, so always encodable
    assert c.get("/status").status_code == 200


def test_txt_scrubs_surrogates_controls_and_bidi():
    pytest.importorskip("flask")
    from safety.alpaca import _txt
    assert _txt("a\ud83db\x07c‮d\r\ne\x85f") == "a�bcd ef"
    assert _txt("😀 ok") == "\U0001F600 ok"     # a real pair is one character
    _txt(LONE).encode("utf-8")                            # never raises
