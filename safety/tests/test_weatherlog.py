"""The weather log: rows go to RAM, the SD card is written rarely and in daytime, archive
runs never duplicate or lose rows, and the archive cannot grow without bound.

"SD card" here is a temp directory standing in for the web root's weather_log/, "RAM" one
standing in for /dev/shm.
"""
import csv
import json
import logging
import os
import signal
import sys
import time
import types

import pytest

from safety import config, domes as domes_mod, server, weatherlog as wlmod, wu_poll
from safety.config import DomeConfig
from safety.dew import DewMonitor
from safety.domes import DomeStore, parse_report
from safety.eventlog import EventLog
from safety.monitor import RainPoller, SafetyMonitor
from safety.weatherlog import HEADERS, STATION_COLUMNS, SUMMARY_COLUMNS, WeatherLog

NOV = 1_794_427_200.0       # 2026-11-11 20:00:00 UTC
DAY, NIGHT = 30.0, -30.0    # sun altitudes


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    sd, ram = tmp_path / "www" / "weather_log", tmp_path / "shm" / "ttu_weather_log"
    monkeypatch.setattr(config, "WEATHER_LOG_DIR", str(sd))
    monkeypatch.setattr(config, "WEATHER_LOG_RAM_DIR", str(ram))
    monkeypatch.setattr(config, "EVENT_LOG", str(tmp_path / "events.log"))
    # no dome, whatever the environment running the tests has: the table every test here
    # expects, until one of them configures domes (see "the domes' rain sensors" below)
    monkeypatch.setattr(config, "DOMES", ())
    return sd, ram


@pytest.fixture
def wl(dirs):
    log = WeatherLog(config)
    log._created_ts = NOV       # "the daemon started" at the tests' own time, not today's
    return log


def state(safe0=True, safe1=False, reasons0=(), reasons1=("wind 12 mph is above 9 mph",),
          poll_ts=None, results=()):
    return {
        "ts": NOV, "is_safe": safe0, "reasons": list(reasons0),
        "dew": {"enabled": True, "is_safe": safe1, "reasons": [] if safe1 else list(reasons1),
                "checks": {"device0": safe0, "spread": True, "wind": safe1, "cloud": True,
                           "precip": None, "lightning": True},
                "spread_c": 4.4444, "wind_mph": 4.6, "gust_mph": 7.0, "temp_c": 11.25,
                "dewpoint_c": 6.8, "rh_pct": 74.0, "stations_used": 5, "stations_wind": 4,
                "wind_calm": safe1},
        "components": {
            "sun": {"value_deg": -31.257}, "humidity": {"value_pct": 47.0},
            "rain": {"latched": False, "raining": [], "stations_live": 5,
                     "stations_total": 7, "last_poll_ts": poll_ts, "results": list(results)},
            "radar": {"available": True, "in_ring": False, "unconfirmed_echo": False,
                      "nearest_km": 142.0},
            "glm": {"latched": False, "nearest_km": None}},
    }


OUTLOOK = {"available": True, "age_s": 540,
           "this_hour": {"cloud_cover_pct": 12, "precip_prob_pct": 1, "thunder_prob_pct": 0,
                         "temp_c": 11.0, "dewpoint_c": 7.0, "rh_pct": 76,
                         "wind_speed_kmh": 12.874752, "wind_gust_kmh": 32.18688},
           "next_hour": {"cloud_cover_pct": 20, "precip_prob_pct": 2}}


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def lines(path):
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def fill(wl, n=3, start=NOV, step=300, **kw):
    for i in range(n):
        wl.observe(state(**kw), start + i * step)


# --- rows -----------------------------------------------------------------------------------
def test_rows_go_to_ram_and_nothing_touches_the_archive(wl, dirs):
    sd, ram = dirs
    wl.observe(state(), NOV, outlook=OUTLOOK, inputs={"temperature": 14.0, "humidity": 47.0})
    assert not sd.exists()                      # no directory, no file: nothing on "SD"
    rows = read(ram / "weather_current.csv")
    assert len(rows) == 1 and list(rows[0]) == list(SUMMARY_COLUMNS)
    r = rows[0]
    assert r["utc"] == "2026-11-11T20:00:00Z" and r["event"] == "start"
    assert r["local"].startswith("2026-11-11 14:00:00")          # America/Chicago, CST
    assert (r["safe0"], r["safe1"]) == ("1", "0")
    assert (r["spread_c"], r["wind_mph"], r["temp_c"]) == ("4.44", "4.6", "11.2")
    assert (r["check_spread"], r["check_wind"], r["check_precip"]) == ("1", "0", "")
    assert r["check_lightning"] == "1"
    assert SUMMARY_COLUMNS.index("check_lightning") == SUMMARY_COLUMNS.index("check_precip") + 1
    assert (r["fc_cloud_pct"], r["fc_cloud_next_pct"], r["fc_precip_next_pct"]) == ("12", "20", "2")
    # the forecast wind arrives in km/h and is logged in mph
    assert (r["fc_wind_mph"], r["fc_gust_mph"], r["fc_age_min"]) == ("8.0", "20.0", "9")
    assert (r["encl_temp_c"], r["encl_rh_pct"], r["sun_alt_deg"]) == ("14.0", "47", "-31.26")
    assert r["radar_nearest_km"] == "142.0" and r["glm_nearest_km"] == ""
    assert r["reasons1"] == "wind 12 mph is above 9 mph" and r["reasons0"] == ""


def test_the_lightning_check_is_logged_as_pass_fail_or_no_data(wl, dirs):
    _, ram = dirs
    for i, value in enumerate((True, False, None, "absent")):
        st = state()
        if value == "absent":
            del st["dew"]["checks"]["lightning"]             # a state without that check
        else:
            st["dew"]["checks"]["lightning"] = value
        wl.observe(st, NOV + i * 300)
    logged = [r["check_lightning"] for r in read(ram / "weather_current.csv")]
    assert logged == ["1", "0", "", ""]


def test_a_row_every_interval_and_at_once_on_a_verdict_change(wl, dirs):
    _, ram = dirs
    wl.observe(state(), NOV)
    wl.observe(state(), NOV + 30)               # the evaluator runs every 30 s
    wl.observe(state(), NOV + 299)
    assert len(read(ram / "weather_current.csv")) == 1
    wl.observe(state(), NOV + 300)
    wl.observe(state(safe1=True), NOV + 330)                 # device 1 turned true
    wl.observe(state(safe0=False, safe1=False), NOV + 360)   # both changed
    wl.observe(state(safe0=False, safe1=False), NOV + 390)
    wl.observe(state(safe0=True, safe1=False), NOV + 420)
    events = [r["event"] for r in read(ram / "weather_current.csv")]
    assert events == ["start", "interval", "device1", "device0+device1", "device0"]


def test_text_with_commas_quotes_and_line_breaks_stays_one_row(wl, dirs):
    _, ram = dirs
    nasty = ['NWS cloud cover 80% > 60% (this hour), "really"', "two\nlines\r\nhere"]
    wl.observe(state(safe0=False, reasons0=nasty), NOV)
    assert len(lines(ram / "weather_current.csv")) == 2          # header + ONE row
    row = read(ram / "weather_current.csv")[0]
    assert row["reasons0"] == ('NWS cloud cover 80% > 60% (this hour), "really" | '
                               "two lines here")
    assert len(row) == len(SUMMARY_COLUMNS)


def test_station_rows_once_per_poll_in_metric_and_mph(wl, dirs):
    _, ram = dirs
    results = [
        {"station": "KTXA", "state": "live", "distance_km": 3.2, "age_min": 2,
         "precip_in_hr": 0.0, "temp_f": 50.0, "dewpt_f": 41.0, "humidity_pct": 71.0,
         "wind_mph": 8.0, "gust_mph": 11.0},
        {"station": "KTXB", "state": "live", "age_min": 4, "precip_in_hr": 0.02,
         "temp_f": 48.2, "dewpt_f": None, "humidity_pct": None, "wind_mph": None},
        {"station": "KTXC", "state": "offline"},
        {"station": "KTXD", "state": "backed-off"}]
    for t in (NOV, NOV + 30, NOV + 60):
        wl.observe(state(poll_ts=NOV - 5, results=results), t)
    rows = read(ram / "stations_current.csv")
    assert len(rows) == 4 and list(rows[0]) == list(STATION_COLUMNS)
    a, b, c, d = rows
    assert a["utc"] == "2026-11-11T19:59:55Z"                    # the poll's time
    assert (a["temp_c"], a["dewpoint_c"], a["spread_c"]) == ("10.0", "5.0", "5.00")
    assert (a["wind_mph"], a["gust_mph"], a["rh_pct"], a["distance_km"]) == ("8.0", "11.0", "71", "3.2")
    assert (b["temp_c"], b["dewpoint_c"], b["spread_c"], b["wind_mph"]) == ("9.0", "", "", "")
    assert b["precip_in_hr"] == "0.02" and (c["state"], d["state"]) == ("offline", "backed-off")
    wl.observe(state(poll_ts=NOV + 595, results=results), NOV + 600)   # the next poll
    assert len(read(ram / "stations_current.csv")) == 8


def test_station_rows_can_be_switched_off(wl, dirs, monkeypatch):
    _, ram = dirs
    monkeypatch.setattr(config, "WEATHER_LOG_STATIONS", False)
    wl.observe(state(poll_ts=NOV, results=[{"station": "A", "state": "live"}]), NOV)
    assert not (ram / "stations_current.csv").exists()
    assert [c["kind"] for c in wl.status()["current"]] == ["weather"]


def test_observe_never_raises(wl, dirs):
    for junk in ({}, {"dew": None, "components": None}, {"components": {"rain": 5}}, None):
        wl.observe(junk, NOV)                    # whatever the state looks like
    assert wl.status()["enabled"] is True


def test_a_full_ram_disk_drops_rows_but_never_raises(wl, dirs, monkeypatch):
    def no_space(*a, **k):
        raise OSError(28, "No space left on device")
    wl.observe(state(), NOV)
    with monkeypatch.context() as patch:
        patch.setattr(wlmod.os, "makedirs", no_space)
        wl.observe(state(), NOV + 300)
        assert "No space left" in wl.status()["ram_error"]
    wl.observe(state(), NOV + 600)
    assert wl.status()["ram_error"] is None


# --- archiving --------------------------------------------------------------------------------
def test_flush_appends_to_a_monthly_file_and_empties_ram(wl, dirs):
    sd, ram = dirs
    fill(wl, 3)
    assert wl.flush("test", NOV + 1000) is True
    archive = sd / "weather_2026-11.csv"
    assert lines(archive)[0] == HEADERS["weather"] and len(read(archive)) == 3
    assert lines(ram / "weather_current.csv") == [HEADERS["weather"]]   # link stays valid
    assert not (ram / "pending").exists() and not (ram / "done").exists()
    assert not (ram / "flush_marker.json").exists()
    before = archive.read_bytes()
    assert wl.flush("again", NOV + 2000) is True                   # nothing new
    assert archive.read_bytes() == before
    fill(wl, 2, start=NOV + 3000)
    wl.flush("more", NOV + 4000)
    rows = read(archive)
    assert len(rows) == 5 and lines(archive).count(HEADERS["weather"]) == 1
    assert [r["utc"] for r in rows] == sorted(r["utc"] for r in rows)


def test_current_files_are_linked_into_the_archive_directory(wl, dirs):
    sd, ram = dirs
    wl.prepare()
    for kind in ("weather", "stations"):
        link = sd / f"{kind}_current.csv"
        assert os.path.islink(link) and os.readlink(link) == str(ram / f"{kind}_current.csv")
    wl.observe(state(), NOV)
    assert len(read(sd / "weather_current.csv")) == 1               # served through the link
    (sd / "weather_current.csv").unlink()
    (sd / "weather_current.csv").write_text("a stale regular file")
    again = WeatherLog(config)
    again.prepare()
    assert os.path.islink(sd / "weather_current.csv")


def test_rows_are_filed_by_their_own_month(wl, dirs):
    sd, _ = dirs
    dec = 1_796_068_800.0                       # 2026-11-30 20:00:00 UTC
    fill(wl, 3, start=dec, step=4 * 3600)       # 20:00, 00:00 (1 Dec), 04:00
    wl.flush("test", dec + 86400)
    assert len(read(sd / "weather_2026-11.csv")) == 1
    assert len(read(sd / "weather_2026-12.csv")) == 2


def test_flush_schedule_is_once_a_day_and_only_in_daytime(wl, dirs):
    assert wl.flush_due(DAY, NOV) is None                     # nothing to write
    fill(wl, 2)
    assert wl.flush_due(NIGHT, NOV + 600) is None             # night: wait
    assert wl.flush_due(0.0, NOV + 600) is None               # sun exactly on the horizon
    assert wl.flush_due(DAY, NOV + 600) == "scheduled"
    assert wl.flush_due(None, NOV + 600) is None              # sun unknown: not "daytime"
    assert wl.maybe_flush(NIGHT, NOV + 600) is False
    assert wl.maybe_flush(DAY, NOV + 600) is True
    fill(wl, 2, start=NOV + 700)
    assert wl.flush_due(DAY, NOV + 600 + 23.9 * 3600) is None     # not yet 24 h
    assert wl.flush_due(NIGHT, NOV + 600 + 30 * 3600) is None     # overdue, but night
    assert wl.flush_due(DAY, NOV + 600 + 24 * 3600) == "scheduled"
    assert wl.flush_due(DAY, NOV - 3600) == "scheduled"           # the clock stepped back
    # Sun unknown (the first minute after a reboot; the page script down): wait for it,
    # but a run that is a whole further day overdue goes ahead.
    assert wl.flush_due(None, NOV + 600 + 30 * 3600) is None
    assert wl.flush_due(None, NOV + 600 + 47.9 * 3600) is None
    assert wl.flush_due(None, NOV + 600 + 48 * 3600) == "scheduled"


def test_sun_never_known_and_nothing_archived_yet_still_gets_archived(wl, dirs):
    # a fresh install whose page script never delivers the sun altitude: the wait counts
    # from the start of the daemon instead of lasting for ever
    wl._created_ts = NOV
    fill(wl, 2)
    assert wl.flush_due(None, NOV + 3600) is None
    assert wl.flush_due(None, NOV + 47 * 3600) is None
    assert wl.flush_due(None, NOV + 48 * 3600) == "scheduled"
    assert wl.flush_due(DAY, NOV + 3600) == "scheduled"       # with a sun: at once, by day


def test_ram_limit_forces_an_archive_run_even_at_night(wl, dirs, monkeypatch):
    fill(wl, 2)
    wl.flush("test", NOV)
    fill(wl, 5, start=NOV + 600)
    assert wl.flush_due(NIGHT, NOV + 3600) is None
    monkeypatch.setattr(config, "WEATHER_LOG_RAM_FLUSH_MB", 0.0001)   # ~100 bytes
    assert wl.flush_due(NIGHT, NOV + 3600) == "RAM limit"


def test_orderly_shutdown_archives_whatever_the_time_of_day(wl, dirs):
    sd, _ = dirs
    fill(wl, 2)
    assert wl.flush("shutdown") is True                      # no sun check here
    assert len(read(sd / "weather_2026-11.csv")) == 2


def test_last_archive_time_survives_a_daemon_restart_and_a_reboot(wl, dirs):
    sd, ram = dirs
    fill(wl, 2)
    wl.flush("test", NOV + 600)
    restarted = WeatherLog(config)                           # /dev/shm survives a restart
    restarted.observe(state(), NOV + 900)
    assert restarted.flush_due(DAY, NOV + 3600) is None
    assert restarted.flush_due(DAY, NOV + 600 + 86400) == "scheduled"
    os.remove(ram / "meta.json")                             # ...a reboot wipes it
    os.utime(sd / "weather_2026-11.csv", (NOV + 600, NOV + 600))
    rebooted = WeatherLog(config)
    rebooted.prepare()                                       # falls back to the file time
    rebooted.observe(state(), NOV + 1200)
    assert rebooted.flush_due(DAY, NOV + 3600) is None
    assert rebooted.status()["last_flush_ts"] == NOV + 600


def test_rows_left_in_ram_by_a_crashed_daemon_are_archived_by_the_next_one(wl, dirs):
    sd, _ = dirs
    fill(wl, 4)
    del wl                                                   # killed: no flush
    successor = WeatherLog(config)
    assert successor.status()["current"][0]["rows"] == 4
    successor.observe(state(), NOV + 5000)
    successor.flush("test", NOV + 6000)
    rows = read(sd / "weather_2026-11.csv")
    assert len(rows) == 5 and rows[-1]["event"] == "start"


# --- an archive run that is interrupted ---------------------------------------------------------
def test_killed_between_append_and_commit_rows_are_archived_exactly_once(wl, dirs, monkeypatch):
    sd, ram = dirs
    fill(wl, 2)
    wl.flush("first", NOV + 600)
    fill(wl, 3, start=NOV + 900)
    real_rename = os.rename

    def die_at_commit(src, dst):
        if str(dst).endswith("done"):
            raise KeyboardInterrupt("SIGKILL stand-in")      # after the append, before the commit
        return real_rename(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(wlmod.os, "rename", die_at_commit)
        with pytest.raises(KeyboardInterrupt):
            wl.flush("second", NOV + 2000)
    # the rows ARE in the archive, but not committed: marker and pending/ still there
    assert len(read(sd / "weather_2026-11.csv")) == 5
    assert (ram / "flush_marker.json").exists() and (ram / "pending" / "weather.csv").exists()

    successor = WeatherLog(config)                           # the restarted daemon
    successor.observe(state(), NOV + 3000)
    assert successor.flush("after restart", NOV + 4000) is True
    rows = read(sd / "weather_2026-11.csv")
    assert len(rows) == 6                                    # 2 + 3 + 1, nothing twice
    assert len({r["utc"] for r in rows}) == 6
    assert not (ram / "flush_marker.json").exists() and not (ram / "pending").exists()


def test_killed_in_the_middle_of_the_append_is_rolled_back_and_repeated(wl, dirs):
    sd, ram = dirs
    fill(wl, 2)
    wl.flush("first", NOV + 600)
    archive = sd / "weather_2026-11.csv"
    size = archive.stat().st_size
    fill(wl, 3, start=NOV + 900)
    # the state a kill in mid-append leaves: rows set aside, marker written, half a write
    os.makedirs(ram / "pending")
    os.replace(ram / "weather_current.csv", ram / "pending" / "weather.csv")
    (ram / "flush_marker.json").write_text(json.dumps({"sizes": {str(archive): size}}))
    with open(archive, "ab") as f:
        f.write(b"2026-11-11T20:15:00Z,2026-11-11 14:15:00 CST,interval,-31.26,1,0,4.4")
    successor = WeatherLog(config)
    assert successor.status()["current"][0]["waiting"] == 3
    assert successor.flush("after restart", NOV + 4000) is True
    rows = read(archive)
    assert len(rows) == 5 and all(len(r) == len(SUMMARY_COLUMNS) for r in rows)
    assert all(None not in r.values() for r in rows)         # no short (torn) row


def test_killed_after_the_commit_nothing_is_cut_off(wl, dirs):
    sd, ram = dirs
    fill(wl, 3)
    wl.flush("first", NOV + 600)
    archive = sd / "weather_2026-11.csv"
    # the state a kill right after the commit leaves: done/ and the (now obsolete) marker,
    # whose sizes are from BEFORE those three rows were appended
    os.makedirs(ram / "done")
    (ram / "done" / "weather.csv").write_text("old rows")
    (ram / "flush_marker.json").write_text(json.dumps({"sizes": {str(archive): 0}}))
    successor = WeatherLog(config)
    successor.observe(state(), NOV + 900)
    assert successor.flush("after restart", NOV + 4000) is True
    assert len(read(archive)) == 4                           # the three survive, one added
    assert not (ram / "done").exists() and not (ram / "flush_marker.json").exists()


def test_marker_cannot_make_it_truncate_a_foreign_file(wl, dirs, tmp_path):
    sd, ram = dirs
    victim = tmp_path / "precious.txt"
    victim.write_text("do not touch " * 10)
    other = sd.parent / "status.html"
    fill(wl, 1)
    wl.flush("first", NOV + 600)
    other.write_text("<html>" * 50)
    fill(wl, 1, start=NOV + 900)
    os.makedirs(ram / "pending")
    os.replace(ram / "weather_current.csv", ram / "pending" / "weather.csv")
    (ram / "flush_marker.json").write_text(json.dumps({"sizes": {
        str(victim): 0, str(other): 0, str(sd / "notes.csv"): 0, "weather_2026-11.csv": "x"}}))
    (sd / "notes.csv").write_text("mine")
    assert WeatherLog(config).flush("after restart", NOV + 4000) is True
    assert victim.read_text().startswith("do not touch") and other.read_text().startswith("<html>")
    assert (sd / "notes.csv").read_text() == "mine"


def test_a_row_torn_by_a_power_cut_is_cut_off_before_the_next_append(wl, dirs):
    sd, _ = dirs
    fill(wl, 2)
    wl.flush("first", NOV + 600)
    archive = sd / "weather_2026-11.csv"
    with open(archive, "ab") as f:
        f.write(b"2026-11-11T21:00:00Z,2026-11-11 15:00")     # power cut mid-row
    rebooted = WeatherLog(config)                            # RAM is empty after a reboot
    rebooted.observe(state(), NOV + 7200)
    assert rebooted.flush("test", NOV + 8000) is True
    rows = read(archive)
    assert len(rows) == 3 and all(None not in r.values() for r in rows)
    assert not any("15:00" == r["local"][-5:] for r in rows)


def test_new_columns_in_mid_month_continue_in_a_second_file(wl, dirs):
    sd, _ = dirs
    old = sd / "weather_2026-11.csv"
    os.makedirs(sd)
    old.write_text("utc,local,safe0\n2026-11-01T00:00:00Z,2026-10-31 19:00:00 CDT,1\n")
    fill(wl, 2)
    wl.flush("test", NOV + 600)
    assert old.read_text().count("\n") == 2                  # the old table is left alone
    assert lines(sd / "weather_2026-11_b.csv")[0] == HEADERS["weather"]
    assert len(read(sd / "weather_2026-11_b.csv")) == 2
    fill(wl, 1, start=NOV + 900)
    wl.flush("test", NOV + 1200)
    assert len(read(sd / "weather_2026-11_b.csv")) == 3      # and it keeps using that one
    assert not (sd / "weather_2026-11_c.csv").exists()


def test_ram_file_from_an_older_version_is_archived_under_its_own_header(dirs):
    sd, ram = dirs
    os.makedirs(ram)
    (ram / "weather_current.csv").write_text(
        "utc,local,safe0\n2026-11-10T03:00:00Z,2026-11-09 21:00:00 CST,1\n")
    wl = WeatherLog(config)                                  # the upgraded daemon starts
    wl.observe(state(), NOV)
    assert lines(ram / "weather_current.csv")[0] == HEADERS["weather"]
    assert wl.flush("test", NOV + 600) is True
    assert lines(sd / "weather_2026-11.csv") == [
        "utc,local,safe0", "2026-11-10T03:00:00Z,2026-11-09 21:00:00 CST,1"]
    assert len(read(sd / "weather_2026-11_b.csv")) == 1


# --- failures and limits ---------------------------------------------------------------------
def test_unwritable_archive_keeps_the_rows_in_ram_and_says_so(tmp_path, dirs, monkeypatch):
    sd, ram = dirs
    blocker = tmp_path / "www"
    blocker.write_text("a file where the web root should be")
    wl = WeatherLog(config)
    wl.prepare()
    assert "www" in wl.status()["error"]
    fill(wl, 3)
    monkeypatch.setattr(wlmod, "RETRY_SEC", 600.0)
    assert wl.flush("test", NOV + 600) is False
    st = wl.status()
    assert st["error"] and st["last_flush_ts"] is None
    assert st["current"][0]["rows"] + st["current"][0]["waiting"] == 3
    assert wl.flush_due(DAY, NOV + 700) is None              # not hammered every minute
    monkeypatch.setattr(wlmod, "RETRY_SEC", 0.0)
    assert wl.flush_due(DAY, NOV + 700) == "scheduled"
    blocker.unlink()                                         # the operator fixes it
    fill(wl, 1, start=NOV + 1500)
    assert wl.maybe_flush(DAY, NOV + 2000) is True
    assert len(read(sd / "weather_2026-11.csv")) == 4 and wl.status()["error"] is None


def test_archive_size_cap_deletes_the_oldest_months_never_the_current_one(dirs, monkeypatch):
    sd, _ = dirs
    os.makedirs(sd)
    for month in ("2026-07", "2026-08", "2026-09", "2026-10"):
        (sd / f"weather_{month}.csv").write_text(HEADERS["weather"] + "\n" + "x" * 40_000)
        (sd / f"stations_{month}.csv").write_text("y" * 10_000)
    (sd / "weather_2026-08_b.csv").write_text("z" * 5_000)
    (sd / "status.html").write_text("not ours")
    log = EventLog(config.EVENT_LOG)
    wl = WeatherLog(config, log)
    monkeypatch.setattr(config, "WEATHER_LOG_MAX_MB", 0.11)      # ~115 kB: room for two months
    fill(wl, 2)
    wl.flush("test", NOV + 600)
    left = sorted(p.name for p in sd.iterdir() if p.is_file() and not p.is_symlink())
    assert left == ["stations_2026-09.csv", "stations_2026-10.csv", "status.html",
                    "weather_2026-09.csv", "weather_2026-10.csv", "weather_2026-11.csv"]
    events = [e for e in log.recent(10) if e["action"] == "WEATHER-LOG"]
    assert len(events) == 1 and "weather_2026-07.csv" in events[0]["result"]
    assert "weather_2026-08_b.csv" in events[0]["result"]
    monkeypatch.setattr(config, "WEATHER_LOG_MAX_MB", 0.0001)    # absurd: smaller than a month
    fill(wl, 1, start=NOV + 900)
    wl.flush("test", NOV + 1200)
    assert (sd / "weather_2026-11.csv").exists()                 # the current month stays
    assert not (sd / "weather_2026-10.csv").exists()
    assert wl.status()["total_bytes"] == (sd / "weather_2026-11.csv").stat().st_size


def test_default_cap_and_schedule_are_the_owners():
    assert config.WEATHER_LOG_MAX_MB == 6144.0                   # 6 GB
    assert config.WEATHER_LOG_FLUSH_HOURS == 24.0
    assert config.WEATHER_LOG_INTERVAL_SEC == 300 and config.WEATHER_LOG_STATIONS is True
    assert config.WEATHER_LOG_FLUSH_SUN_ABOVE_DEG == 0.0


def test_ram_use_is_bounded_when_the_archive_never_works(tmp_path, dirs, monkeypatch):
    (tmp_path / "www").write_text("blocked")
    monkeypatch.setattr(config, "WEATHER_LOG_RAM_MAX_MB", 0.004)     # ~4 kB
    wl = WeatherLog(config)
    for i in range(60):
        wl.observe(state(), NOV + i * 300)
        if i % 10 == 9:
            wl.flush("test", NOV + i * 300)                      # fails; rows wait in RAM
    st = wl.status()
    assert st["dropped_rows"] > 0 and "dropped" in st["ram_error"]
    total = sum(os.path.getsize(os.path.join(root, f))
                for root, _, files in os.walk(config.WEATHER_LOG_RAM_DIR) for f in files)
    assert total < 3 * 4096


def test_ram_limit_message_clears_once_the_archive_works_and_files_keep_their_header(
        tmp_path, dirs, monkeypatch):
    sd, ram = dirs
    blocker = tmp_path / "www"
    blocker.write_text("blocked")
    monkeypatch.setattr(config, "WEATHER_LOG_RAM_MAX_MB", 0.002)     # ~2 kB
    wl = WeatherLog(config)
    for i in range(30):
        wl.observe(state(), NOV + i * 300)
    assert wl.status()["dropped_rows"] > 0
    # the current file was emptied to make room: it must still start with a header
    assert lines(ram / "weather_current.csv")[0] == HEADERS["weather"]
    blocker.unlink()
    monkeypatch.setattr(wlmod, "RETRY_SEC", 0.0)
    assert wl.flush("test", NOV + 9000) is True
    assert wl.status()["ram_error"] is None
    rows = read(sd / "weather_2026-11.csv")
    assert rows and all(None not in r.values() and len(r) == len(SUMMARY_COLUMNS) for r in rows)


def test_a_torn_row_in_ram_is_not_archived_and_does_not_spoil_its_neighbours(wl, dirs):
    sd, ram = dirs
    fill(wl, 2)
    with open(ram / "weather_current.csv", "a", encoding="utf-8") as f:
        f.write("2026-11-11T20:20:00Z,2026-11-11 14:20:00 CST,interval,-31")   # RAM disk full
    fill(wl, 2, start=NOV + 3000)                # the next rows start on a new line
    assert wl.flush("test", NOV + 6000) is True
    rows = read(sd / "weather_2026-11.csv")
    assert len(rows) == 4 and all(None not in r.values() for r in rows)
    assert [r["utc"][11:16] for r in rows] == ["20:00", "20:05", "20:50", "20:55"]


def test_ram_limit_drops_the_oldest_rows_and_keeps_the_newest(tmp_path, dirs, monkeypatch):
    _, ram = dirs
    (tmp_path / "www").write_text("blocked")                     # the archive never works
    monkeypatch.setattr(config, "WEATHER_LOG_RAM_MAX_MB", 0.01)      # ~10 kB
    wl = WeatherLog(config)
    for i in range(120):
        wl.observe(state(), NOV + i * 300)
    rows = read(ram / "weather_current.csv")
    assert 5 < len(rows) < 120 and wl.status()["dropped_rows"] == 120 - len(rows)
    assert not (ram / "weather_current.csv.tmp").exists()
    assert rows[-1]["utc"] == "2026-11-12T05:55:00Z"             # the newest row is there
    assert [r["utc"] for r in rows] == sorted(r["utc"] for r in rows)
    assert "oldest" in wl.status()["ram_error"]


def test_half_a_character_in_a_ram_file_does_not_stop_the_log(wl, dirs):
    sd, ram = dirs
    fill(wl, 2)
    with open(ram / "weather_current.csv", "ab") as f:
        f.write(b"2026-11-11T20:20:00Z,torn \xc2")              # RAM disk full in mid-row
    fill(wl, 1, start=NOV + 3000)
    successor = WeatherLog(config)                               # must not raise
    successor.observe(state(), NOV + 3300)
    assert successor.flush("test", NOV + 6000) is True
    rows = read(sd / "weather_2026-11.csv")
    assert len(rows) == 4 and all(None not in r.values() for r in rows)
    # ...also when the damaged file is the very first thing the new daemon reads
    with open(ram / "weather_current.csv", "wb") as f:
        f.write(b"\xc2\xff garbage\n")
    again = WeatherLog(config)
    again.observe(state(), NOV + 9000)
    assert again.flush("test", NOV + 9600) is True
    assert len(read(sd / "weather_2026-11.csv")) == 5


def test_a_torn_piece_is_removed_before_the_next_row_is_written(wl, dirs):
    # Cut in its last text field, a torn row has the right number of fields: nothing at
    # archive time could tell. So the piece is removed as soon as the next row arrives.
    sd, ram = dirs
    wl.observe(state(), NOV)
    path = ram / "weather_current.csv"
    whole = path.read_bytes()
    wl.observe(state(), NOV + 300)
    path.write_bytes(path.read_bytes()[:-9])                     # "...above 9 mph\n" cut short
    wl.observe(state(), NOV + 600)
    content = path.read_bytes()
    assert content.startswith(whole) and content.count(b"\n") == 3    # header + 2 rows
    assert [r["utc"][11:16] for r in read(path)] == ["20:00", "20:10"]
    path.write_bytes(b"utc,local,ev")                            # even the header torn
    wl.observe(state(), NOV + 900)
    assert lines(path)[0] == HEADERS["weather"] and len(read(path)) == 1


def test_garbage_without_line_breaks_does_not_wipe_the_rows_before_it(wl, dirs):
    sd, ram = dirs
    fill(wl, 5)
    with open(ram / "weather_current.csv", "ab") as f:
        f.write(b"x" * 70000)                                    # not a torn row of ours
    fill(wl, 2, start=NOV + 3000)
    assert len(lines(ram / "weather_current.csv")) == 1 + 5 + 1 + 2
    assert wl.flush("test", NOV + 6000) is True
    assert len(read(sd / "weather_2026-11.csv")) == 7            # the garbage line is skipped


def test_the_replacement_character_in_real_text_is_not_mistaken_for_damage(wl, dirs):
    sd, _ = dirs
    wl.observe(state(safe0=False, reasons0=["NWS headline with mojibake \ufffd in it"]), NOV)
    assert wl.flush("test", NOV + 600) is True
    assert "\ufffd" in read(sd / "weather_2026-11.csv")[0]["reasons0"]


def test_a_row_torn_inside_a_quoted_field_does_not_swallow_the_rows_after_it(wl, dirs):
    # second line of defence, for a damaged file however it came about
    sd, ram = dirs
    quoted = ["NWS cloud cover 80% > 60% (this hour), and more"]     # a comma: quoted field
    wl.observe(state(safe0=False, reasons0=quoted), NOV)
    fill(wl, 2, start=NOV + 3000)
    path = ram / "weather_current.csv"
    header, first, rest = path.read_text().split("\n", 2)
    assert first.count('"') == 2
    path.write_text(header + "\n" + first[:first.rindex('"') - 5] + "\n" + rest)
    assert wl.flush("test", NOV + 6000) is True
    rows = read(sd / "weather_2026-11.csv")
    assert len(rows) == 2 and all(len(r) == len(SUMMARY_COLUMNS) for r in rows)
    assert [r["utc"][11:16] for r in rows] == ["20:50", "20:55"]
    assert len(lines(sd / "weather_2026-11.csv")) == 3


def test_two_old_column_layouts_of_one_month_go_to_two_files(dirs):
    sd, ram = dirs
    os.makedirs(ram / "pending")
    (ram / "pending" / "weather.old-1.csv").write_text(
        "utc,local,a\n2026-11-01T00:00:00Z,x,1\n")
    (ram / "pending" / "weather.old-2.csv").write_text(
        "utc,local,a,b\n2026-11-02T00:00:00Z,x,1,2\n")
    wl = WeatherLog(config)
    wl.observe(state(), NOV)
    assert wl.flush("test", NOV + 600) is True
    files = {p.name: lines(p) for p in sd.glob("weather_2026-11*.csv") if not p.is_symlink()}
    assert sorted(files) == ["weather_2026-11.csv", "weather_2026-11_b.csv",
                             "weather_2026-11_c.csv"]
    for content in files.values():                               # one header, one layout each
        assert len(content) == 2 and sum(ln.startswith("utc,") for ln in content) == 1
    assert {content[0] for content in files.values()} == {
        "utc,local,a", "utc,local,a,b", HEADERS["weather"]}


def test_a_stray_file_named_like_its_work_directories_does_not_block_it(wl, dirs):
    sd, ram = dirs
    fill(wl, 2)
    (ram / "done").write_text("not a directory")
    (ram / "pending").write_text("neither")
    assert wl.flush("test", NOV + 600) is True
    assert len(read(sd / "weather_2026-11.csv")) == 2
    assert not (ram / "done").exists() and not (ram / "pending").exists()


def test_status_lists_what_the_page_links_to(wl, dirs):
    fill(wl, 2, poll_ts=NOV, results=[{"station": "A", "state": "live"}])
    wl.flush("test", NOV + 600)
    fill(wl, 1, start=NOV + 900)
    st = json.loads(json.dumps(wl.status()))                     # must be JSON
    assert [(c["name"], c["rows"]) for c in st["current"]] == [
        ("weather_current.csv", 1), ("stations_current.csv", 0)]
    assert [a["name"] for a in st["archives"]] == ["weather_2026-11.csv",
                                                   "stations_2026-11.csv"]
    assert st["total_bytes"] == sum(a["bytes"] for a in st["archives"]) > 0
    assert st["last_flush_ts"] == NOV + 600 and st["error"] is None
    assert st["dir"] == config.WEATHER_LOG_DIR and st["cap_mb"] == config.WEATHER_LOG_MAX_MB


def test_clock_step_writes_the_next_row_at_once(wl, dirs):
    _, ram = dirs
    wl.observe(state(), NOV)
    wl.clock_stepped(NOV, NOV + 86400)
    wl.observe(state(), NOV + 86400 + 5)
    assert len(read(ram / "weather_current.csv")) == 2


# --- the domes' roofs and rain sensors -----------------------------------------------------
# What each dome's rain sensor read (WET / DRY) and what its roof did, as the dome's own
# driver reports them: the ground truth the dew monitor's limits are checked against. Two
# columns per CONFIGURED dome; a record only, never an input (test_domes.py pins that for
# the `domes` block).
WEST = DomeConfig(1, "Dome 1 (west)", "k1-" + "a" * 29)
EAST = DomeConfig(3, "Dome 3 (east)", "k3-" + "c" * 29)      # numbers need not be 1, 2, ...
DOME_COLUMNS = ["dome1_roof", "dome1_rain_wet", "dome3_roof", "dome3_rain_wet"]


@pytest.fixture
def domed(dirs, monkeypatch):
    """The weather log of a site with two domes, and the store their reports go to."""
    monkeypatch.setattr(config, "DOMES", (WEST, EAST))
    monkeypatch.setattr(config, "DOME_STALE_SEC", 120)
    log = WeatherLog(config)
    log._created_ts = NOV
    return log, DomeStore(config)


def report(store, dome, mono, **fields):
    """A dome's report arrives at `mono` (the store's clock): roof open, plus `fields`."""
    rep = {"shutter": "open", "current": True, "source": "merlino-dome 0.4.4"}
    rep.update(fields)
    store.accept(dome, parse_report(json.dumps(rep).encode()), mono=mono)


def domed_state(store, mono, **kw):
    """The state of an evaluation at `mono`, with the `domes` block the store gives then."""
    st = state(**kw)
    st["domes"] = store.component(mono=mono)
    return st


def dome_cells(row):
    return tuple(row[c] for c in DOME_COLUMNS)


class Site:
    """The two domes over time: every 30 s each dome that is up reports its roof
    (self.roof, open unless set) and what its rain sensor reads (self.wet), then the
    daemon evaluates and the log looks."""

    def __init__(self, log, store):
        self.log, self.store, self.t = log, store, 0.0
        self.wet = {}               # dome -> its sensor right now (not in it: silent)
        self.roof = {}              # dome -> its roof right now (not in it: open)

    def look(self, **kw):
        self.t += 30.0
        for dome, wet in self.wet.items():
            report(self.store, dome, self.t - 1.0, rain_wet=wet,
                   shutter=self.roof.get(dome, "open"))
        self.log.observe(domed_state(self.store, self.t, **kw), NOV + self.t)


def test_without_a_dome_the_weather_table_is_the_one_it_always_was(wl, dirs):
    _, ram = dirs
    assert wlmod.summary_columns(config) == SUMMARY_COLUMNS and wlmod.dome_columns(config) == ()
    st = state()
    # (a `domes` block cannot be there without a dome; even so it would change nothing)
    st["domes"] = {"enabled": True, "domes": [
        {"n": 1, "known": True, "shutter": "open", "rain_wet": True}]}
    wl.observe(st, NOV)
    st["domes"]["domes"][0].update(shutter="closed", rain_wet=False)
    wl.observe(st, NOV + 30)
    assert lines(ram / "weather_current.csv")[0] == HEADERS["weather"]
    assert [r["event"] for r in read(ram / "weather_current.csv")] == ["start"]


def test_each_configured_dome_has_a_roof_and_a_rain_column_before_the_reasons(domed, dirs):
    _, ram = dirs
    log, store = domed
    report(store, WEST, 0.0, rain_wet=True)
    report(store, EAST, 0.0, rain_wet=False, shutter="closed")
    log.observe(domed_state(store, 1.0), NOV, outlook=OUTLOOK)
    assert list(wlmod.dome_columns(config)) == DOME_COLUMNS
    assert lines(ram / "weather_current.csv")[0].split(",") == (
        list(SUMMARY_COLUMNS[:-2]) + DOME_COLUMNS + ["reasons0", "reasons1"])
    row = read(ram / "weather_current.csv")[0]
    assert dome_cells(row) == ("open", "1", "closed", "0")       # open and WET, closed and DRY
    # every other column is where it was and says what it said
    assert (row["event"], row["safe0"], row["safe1"], row["spread_c"]) == ("start", "1", "0", "4.44")
    assert row["fc_cloud_pct"] == "12" and row["reasons1"] == "wind 12 mph is above 9 mph"
    assert None not in row.values() and len(row) == len(SUMMARY_COLUMNS) + 4


@pytest.mark.parametrize("shutter", ["open", "closed", "opening", "closing", "moving", "error"])
def test_the_roof_is_logged_in_the_words_of_the_report(domed, dirs, shutter):
    _, ram = dirs
    log, store = domed
    report(store, WEST, 0.0, shutter=shutter)
    log.observe(domed_state(store, 1.0), NOV)
    assert read(ram / "weather_current.csv")[0]["dome1_roof"] == shutter
    assert shutter in wlmod.ROOF_STATES and "unknown" not in wlmod.ROOF_STATES


def test_not_known_is_blank_never_closed_or_dry(domed, dirs):
    _, ram = dirs
    log, store = domed
    nothing = (None, None)

    def readings(mono):
        return wlmod.dome_readings({"domes": store.component(mono=mono)}, (1, 3))

    assert readings(0.0) == (nothing, nothing)                   # no report yet
    report(store, WEST, 10.0, rain_wet=True)
    report(store, EAST, 10.0, shutter="closed")                  # a sender without a rain reading
    assert readings(11.0) == (("open", True), ("closed", None))
    report(store, WEST, 20.0, rain_wet=None)                     # sent, but no board reading
    assert readings(21.0) == (("open", None), ("closed", None))
    report(store, WEST, 30.0, rain_wet=True, current=False)      # the whole report not current
    assert readings(31.0) == (nothing, ("closed", None))
    report(store, WEST, 40.0, rain_wet=True, shutter="unknown")  # a roof the driver cannot tell
    assert readings(41.0) == ((None, True), ("closed", None))
    report(store, WEST, 50.0, rain_wet=True)
    report(store, EAST, 50.0, shutter="closed")
    assert readings(51.0) == (("open", True), ("closed", None))
    assert readings(50.0 + 120.5) == (nothing, nothing)          # too old: not "still WET"
    # the store missing or broken, a state without the block, nonsense in it
    broken = domes_mod.unavailable_component(config, "boom")
    odd_list = [None, 3, "x", {"n": [1], "known": True, "shutter": "open", "rain_wet": True},
                {"n": 1, "known": True, "shutter": "ajar", "rain_wet": "yes"},
                {"n": 1, "known": True, "shutter": 5}, {"n": 1, "known": True},
                {"n": 3, "known": False, "shutter": "open", "rain_wet": True},
                # (True is not dome 1)
                {"n": True, "known": True, "shutter": "open", "rain_wet": True}]
    for st in (None, "x", {}, {"domes": None}, {"domes": "x"}, {"domes": {"domes": 5}},
               {"domes": broken}, {"domes": {"enabled": True, "domes": odd_list}}):
        assert wlmod.dome_readings(st, (1, 3)) == (nothing, nothing), st
    log.observe(domed_state(store, 500.0), NOV)                  # both silent for minutes
    assert dome_cells(read(ram / "weather_current.csv")[0]) == ("", "", "", "")


def test_a_row_at_once_when_a_dome_s_roof_or_rain_sensor_changes(domed, dirs):
    _, ram = dirs
    site = Site(*domed)
    site.look()                                 # 20:00:30 the daemon starts: no report yet
    site.wet = {WEST: False, EAST: False}
    site.look()                                 # 20:01:00 both domes report: open and DRY
    site.look()
    site.look()                                 # ...and again: nothing new, no row
    site.wet[WEST] = True
    site.look()                                 # 20:02:30 dew on the west dome's sensor
    site.roof[WEST] = "closing"
    site.look()                                 # 20:03:00 ...and its roof starts to close
    site.roof[WEST] = "closed"
    site.look()                                 # 20:03:30 closed
    site.wet[EAST] = True
    site.look(safe1=True)                       # 20:04:00 the east sensor, as device 1 turns true
    site.look(safe1=True)                       # (its roof stays open: the sensor is ignored)
    site.wet[WEST] = False
    site.look(safe1=True)                       # 20:05:00 the west sensor has dried
    del site.wet[EAST]                          # the east dome's PC falls silent...
    for _ in range(4):
        site.look(safe1=True)                   # ...20:07:00: 121 s without a report
    rows = read(ram / "weather_current.csv")
    assert [(r["utc"][11:19], r["event"]) + dome_cells(r) for r in rows] == [
        ("20:00:30", "start", "", "", "", ""),
        ("20:01:00", "dome1+dome3", "open", "0", "open", "0"),
        ("20:02:30", "dome1", "open", "1", "open", "0"),
        ("20:03:00", "dome1", "closing", "1", "open", "0"),
        ("20:03:30", "dome1", "closed", "1", "open", "0"),
        ("20:04:00", "device1+dome3", "closed", "1", "open", "1"),
        ("20:05:00", "dome1", "closed", "0", "open", "1"),
        ("20:07:00", "dome3", "closed", "0", "", "")]
    # each row has the numbers of its moment beside the readings: what the limits are
    # checked against. Here: the roof closed on a WET sensor while device 0 said SAFE and
    # device 1 said "trust the rain sensor" - a closure the rain sensor caused
    closing = rows[3]
    assert (closing["safe0"], closing["safe1"], closing["spread_c"], closing["wind_mph"]) == (
        "1", "0", "4.44", "4.6")
    # the 5-minute rows go on as before, with the readings of the moment
    for _ in range(10):
        site.look(safe1=True)
    rows = read(ram / "weather_current.csv")
    assert [(r["utc"][11:19], r["event"]) + dome_cells(r) for r in rows[8:]] == [
        ("20:12:00", "interval", "closed", "0", "", "")]


def test_dome_reports_reach_ram_only_and_only_when_a_reading_changes(domed, dirs):
    sd, ram = dirs
    log, store = domed
    report(store, WEST, 0.0, rain_wet=True)
    log.observe(domed_state(store, 0.5), NOV)
    for i in range(1, 250):                     # the same roof and reading, report after report
        report(store, WEST, float(i), rain_wet=True, source="sender %d" % i,
               switches=[{"name": "Light", "on": bool(i % 2)}])
        log.observe(domed_state(store, i + 0.5), NOV + i)
    assert len(read(ram / "weather_current.csv")) == 1       # (the switches are not logged)
    report(store, WEST, 250.0, rain_wet=True, shutter="closing")
    log.observe(domed_state(store, 250.5), NOV + 250)
    assert [r["event"] for r in read(ram / "weather_current.csv")] == ["start", "dome1"]
    assert not sd.exists()                                   # and nothing on the "SD card"
    assert log.flush_due(NIGHT, NOV + 250) is None           # no archive run because of it


def test_a_dome_added_or_removed_starts_a_table_of_its_own(dirs, monkeypatch):
    sd, ram = dirs
    WeatherLog(config).observe(state(), NOV)                 # before: no dome
    monkeypatch.setattr(config, "DOMES", (WEST,))            # ttustatus.env gains a dome
    monkeypatch.setattr(config, "DOME_STALE_SEC", 120)
    log, store = WeatherLog(config), DomeStore(config)       # ...and the daemon restarts
    report(store, WEST, 0.0, rain_wet=True)
    log.observe(domed_state(store, 1.0), NOV + 300)
    assert lines(ram / "weather_current.csv")[0].endswith(
        ",dome1_roof,dome1_rain_wet,reasons0,reasons1")
    assert log.status()["current"][0] == {"kind": "weather", "name": "weather_current.csv",
                                          "rows": 1, "waiting": 1}
    assert log.flush("test", NOV + 600) is True
    assert lines(sd / "weather_2026-11.csv")[0] == HEADERS["weather"]     # the old table
    assert len(read(sd / "weather_2026-11.csv")) == 1
    with_dome = read(sd / "weather_2026-11_b.csv")
    assert len(with_dome) == 1
    assert (with_dome[0]["dome1_roof"], with_dome[0]["dome1_rain_wet"]) == ("open", "1")
    monkeypatch.setattr(config, "DOMES", ())                 # the dome is taken out again
    again = WeatherLog(config)
    again.observe(state(), NOV + 900)
    assert again.flush("test", NOV + 1200) is True
    assert len(read(sd / "weather_2026-11.csv")) == 2        # back to the first file
    assert len(read(sd / "weather_2026-11_b.csv")) == 1
    assert not (sd / "weather_2026-11_c.csv").exists()


# --- wiring ------------------------------------------------------------------------------------
def test_monitor_feeds_the_log_and_publishes_its_status(tmp_path, dirs, monkeypatch):
    _, ram = dirs
    for key, name in (("INPUTS_FILE", "i.json"), ("STATE_FILE", "s.json"),
                      ("LATCH_FILE", "l.json")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, "WU_API_KEY", "testkey")
    monkeypatch.setattr(wu_poll, "discover_stations", lambda lat, lon: [("S1", 1.0)])
    monkeypatch.setattr(wu_poll, "poll_stations", lambda st, max_age_min=None: {
        "live": 1, "total": 1, "raining": [], "max_rate": 0.0, "results": [
            {"station": "S1", "state": "live", "distance_km": 1.0, "age_min": 1,
             "precip_in_hr": 0.0, "obs_ts": time.time() - 60, "temp_f": 50.0,
             "dewpt_f": 44.0, "humidity_pct": 80.0, "wind_mph": 2.0, "gust_mph": 4.0}]})
    with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "sun_altitude_deg": -20.0, "humidity_pct": 40.0,
                   "temperature_c": 13.0}, f)
    log = EventLog(config.EVENT_LOG)
    poller = RainPoller(config, log)
    monitor = SafetyMonitor(config, log, poller, dew=DewMonitor(config),
                            weatherlog=WeatherLog(config))
    poller.poll_now()
    st = monitor.evaluate()
    monitor.evaluate()                                           # no second row
    assert st["weather_log"]["enabled"] is True
    row = read(ram / "weather_current.csv")
    assert len(row) == 1
    assert (row[0]["safe0"], row[0]["safe1"], row[0]["encl_temp_c"]) == ("1", "0", "13.0")
    assert row[0]["stations_used"] == "1" and "station(s)" in row[0]["reasons1"]
    # (this monitor has no GLM layer: no lightning data, so that check is blank)
    assert row[0]["check_lightning"] == "" and "lightning data" in row[0]["reasons1"]
    stations = read(ram / "stations_current.csv")
    assert len(stations) == 1 and stations[0]["temp_c"] == "10.0"
    with open(config.STATE_FILE, encoding="utf-8") as f:
        assert json.load(f)["weather_log"]["dir"] == config.WEATHER_LOG_DIR


def test_monitor_hands_the_domes_rain_readings_to_the_log(tmp_path, dirs, monkeypatch):
    _, ram = dirs
    for key, name in (("INPUTS_FILE", "i.json"), ("STATE_FILE", "s.json"),
                      ("LATCH_FILE", "l.json")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, "WU_API_KEY", "")
    monkeypatch.setattr(config, "DOMES", (WEST, EAST))
    monkeypatch.setattr(config, "DOME_STALE_SEC", 120)
    with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "sun_altitude_deg": -20.0, "humidity_pct": 40.0}, f)
    log = EventLog(config.EVENT_LOG)
    store = DomeStore(config)
    monitor = SafetyMonitor(config, log, RainPoller(config, log), dew=DewMonitor(config),
                            weatherlog=WeatherLog(config), domes=store)
    before = monitor.evaluate()                                  # no dome has reported
    report(store, WEST, time.monotonic(), rain_wet=True)         # the west sensor reads WET
    after = monitor.evaluate()
    monitor.evaluate()                                           # the same report: no row
    rows = read(ram / "weather_current.csv")
    assert [(r["event"],) + dome_cells(r) for r in rows] == [
        ("start", "", "", "", ""), ("dome1", "open", "1", "", "")]
    # a record, not an input: a WET sensor moved neither verdict
    assert (after["is_safe"], after["dew"]["is_safe"]) == (before["is_safe"],
                                                           before["dew"]["is_safe"])
    assert [(r["safe0"], r["safe1"]) for r in rows] == [("1", "0"), ("1", "0")]
    assert after["domes"]["domes"][0]["rain_wet"] is True        # what the page shows too
    # the store broken: the domes read unknown on the page, and blank in the log
    monitor.domes = None
    monitor.evaluate()
    rows = read(ram / "weather_current.csv")
    assert (rows[-1]["event"],) + dome_cells(rows[-1]) == ("dome1", "", "", "", "")


def test_a_broken_weather_log_never_reaches_the_safety_verdict(tmp_path, dirs, monkeypatch):
    for key, name in (("INPUTS_FILE", "i.json"), ("STATE_FILE", "s.json"),
                      ("LATCH_FILE", "l.json")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, "WU_API_KEY", "")
    with open(config.INPUTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "sun_altitude_deg": -20.0, "humidity_pct": 40.0}, f)
    log = EventLog(config.EVENT_LOG)
    wl = WeatherLog(config)

    def boom():
        raise RuntimeError("status exploded")
    wl.status = boom
    wl._append_locked = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk exploded"))
    monitor = SafetyMonitor(config, log, RainPoller(config, log), weatherlog=wl)
    st = monitor.evaluate()
    assert st["is_safe"] is True
    assert st["weather_log"]["enabled"] is False and "exploded" in st["weather_log"]["error"]


def test_without_the_log_the_state_says_disabled(tmp_path, monkeypatch):
    for key, name in (("INPUTS_FILE", "i.json"), ("STATE_FILE", "s.json"),
                      ("LATCH_FILE", "l.json"), ("EVENT_LOG", "e.log")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    log = EventLog(config.EVENT_LOG)
    st = SafetyMonitor(config, log, RainPoller(config, log)).evaluate()
    assert st["weather_log"] == {"enabled": False, "current": [], "archives": [],
                                 "error": "weather log disabled (TTU_SAFETY_WEATHER_LOG=0)"}


def test_sigterm_becomes_an_orderly_exit():
    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert server.install_sigterm_handler() is True
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)                        # the handler interrupts this
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_a_second_stop_signal_cannot_interrupt_the_shutdown():
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert server.install_sigterm_handler() is True
        server.ignore_stop_signals()                 # what main()'s finally does first
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)                             # nothing is raised here any more
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@pytest.mark.parametrize("expected", [(), (WEST, EAST)])     # no dome; the TTU kind of site
def test_daemon_archives_on_shutdown_and_runs_the_log_in_its_own_thread(
        dirs, tmp_path, monkeypatch, caplog, expected):
    sd, _ = dirs
    caplog.set_level(logging.INFO, logger="ttu.safety")
    monkeypatch.setattr(config, "DOMES", expected)
    for key, name in (("LATCH_FILE", "l.json"), ("INPUTS_FILE", "i.json"),
                      ("STATE_FILE", "s.json")):
        monkeypatch.setattr(config, key, str(tmp_path / name))
    for flag in ("NWS_ENABLED", "GLM_ENABLED", "RADAR_ENABLED", "HAZARDS_ENABLED",
                 "HAZARD_FEEDS_ENABLED", "CONN_ENABLED", "PAGE_ENABLED"):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "DEW_ENABLED", True)        # TTU_SAFETY_DEW=1 (opt-in)
    monkeypatch.setattr(server.discovery, "start", lambda port: None)
    monkeypatch.setattr(server, "install_sigterm_handler", lambda: True)
    calls = []
    monkeypatch.setattr(server, "ignore_stop_signals", lambda: calls.append("ignore"))
    started, built = [], {}
    monkeypatch.setattr(server, "_start_thread", lambda target, name: started.append(name))

    def create_app(monitor, cfg):
        built["monitor"] = monitor
        return object()
    monkeypatch.setattr(server, "create_app", create_app)

    def serve(app, **kw):
        # the daemon is "running": a few evaluations, then systemd says stop
        for i in range(3):
            built["monitor"].weatherlog.observe(state(), NOV + i * 300)
        raise KeyboardInterrupt
    monkeypatch.setitem(sys.modules, "waitress", types.SimpleNamespace(serve=serve))
    server.main()
    assert "weather-log" in started and "evaluator" in started
    assert built["monitor"].dew is not None and built["monitor"].weatherlog is not None
    rows = read(sd / "weather_2026-11.csv")
    assert len(rows) == 3                                        # archived by the shutdown
    assert calls == ["ignore"]                   # ...with further stop signals shut out
    # the daemon's log has the roof and rain-sensor columns of each dome it expects, and
    # says so
    assert [c for c in rows[0] if c.startswith("dome")] == (DOME_COLUMNS if expected else [])
    said = ("weather log: with each dome's roof state and rain sensor reading (dome1_roof, "
            "dome1_rain_wet, dome3_roof, dome3_rain_wet), and a row at once when one changes")
    assert (said in caplog.text) is bool(expected)
    assert ("weather log: with each dome" in caplog.text) is bool(expected)
    with open(config.EVENT_LOG, encoding="utf-8") as f:
        assert "SHUTDOWN" in f.read()
