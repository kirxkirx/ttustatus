"""What one GLM poll does when it cannot read everything since the last one, and what that
means for the dew monitor's lightning watch (fixes from the reviews of 16cfd4c and of
the first fixes):

  * a poll whose window reaches back the full catch-up and that its deadline cuts short
    gives up the oldest granules, and the cursor moves on: before, it stayed put, and on
    a slow link every poll of the night was cut short the same way and partial, so
    device 1 never read true;
  * a poll of a shorter window that its deadline cuts short (one stalled download on a
    fast link) gives nothing up: the next poll reads what it did not get to (the first
    fix gave it up for good, and a flash in it never reached the latch);
  * the minute of overlap before the cursor was read by the poll before: a slow link
    that does not get to it again leaves no hole;
  * a granule listed late, behind newer ones, is read by the next poll — first, and it
    has to be: one failed download no longer loses it — or reported when given up;
  * time no poll read (given up, before a window the catch-up limit cut short, a late
    granule never read) never counts as watched for device 1, in whole granules;
  * a GLM failure, or a run of partial polls, is logged once when it starts, again when
    the failure's cause changes, and once when it ends, not on every poll;
  * the docs name the temp file the code writes.

The listing and the downloads are stubbed (no network). Most granules hold no flash, so
neither numpy nor netCDF4 is needed (CI installs them on its 3.13 leg only); the one
test with a flash needs the real numpy and skips without it."""
import logging
import os
import types
from datetime import datetime, timedelta, timezone

import pytest

from safety import config, dew, glm_lightning as gl
from safety.tests.test_glm_window import EMPTY, NOW, Bucket, _real_numpy, cfg, key, starts

UTC = timezone.utc
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _events():
    return types.SimpleNamespace(record=lambda *a, **k: {})


def _slow(monkeypatch, b, cost):
    """Every download through Bucket ``b`` costs ``cost`` seconds of the poll's clock (a
    request whose time budget is shorter fails when it is spent, as a socket timeout
    does)."""
    clock = {"t": 0.0}
    monkeypatch.setattr(gl, "_mono", lambda: clock["t"])
    get = b.get

    def slow_get(url, timeout=15):
        if cost > timeout:
            clock["t"] += timeout
            raise OSError("timed out")
        clock["t"] += cost
        return get(url, timeout)
    monkeypatch.setattr(gl, "_get", slow_get)
    return clock


def _stall(monkeypatch, b, hang, cost=0.45):
    """A fast link through Bucket ``b`` (``cost`` s a download) on which the downloads
    numbered in ``hang`` (1-based, counted from the call) hang until their timeout — a
    network stall. Returns the poll clock; set its "t" to 0 for the next poll."""
    clock = {"t": 0.0, "n": 0}
    monkeypatch.setattr(gl, "_mono", lambda: clock["t"])
    get = b.get

    def stalling_get(url, timeout=15):
        clock["n"] += 1
        if clock["n"] in hang:
            clock["t"] += timeout
            raise OSError("timed out")
        clock["t"] += cost
        return get(url, timeout)
    monkeypatch.setattr(gl, "_get", stalling_get)
    return clock


# ---- one stall on a fast link costs nothing ---------------------------------------------------
def test_a_poll_a_stall_cut_short_gives_nothing_up(monkeypatch):
    # A fast link (0.45 s a granule) and the cursor of the poll before. After three
    # downloads the network stalls: five requests hang until their 15-s timeout, and the
    # 75-s deadline passes with the older granules not reached. The first fix gave those
    # up for good: the next window began a minute before the newest granule read, and a
    # flash in them never reached the latch. This window did not reach back the full
    # catch-up, so reading again catches up: the cursor stays and nothing is given up...
    b = Bucket(monkeypatch, starts(-900, 240))
    clock = _stall(monkeypatch, b, hang={4, 5, 6, 7, 8})
    since = (NOW - timedelta(seconds=60)).timestamp()
    r1 = gl.poll_flashes(cfg(), since=since, now=NOW + timedelta(seconds=300))
    assert r1["granules_scanned"] == 3 and b.read_starts()[0] == NOW + timedelta(seconds=200)
    assert r1["cursor"] == since and r1["skipped_s"] == 0
    assert r1["partial"] is True and "4 min of the window not read" in r1["coverage"]
    # ...and the next poll, the link fine again, reads what this one did not get to
    clock["t"], clock["n"] = 0.0, 100
    b.keys = [key(t) for t in starts(-900, 540)]
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=600))
    assert set(starts(-40, 180)) <= set(b.read_starts())
    assert (r2["partial"], r2["skipped_s"]) == (False, 0)
    assert r2["cursor"] == (NOW + timedelta(seconds=540)).timestamp()


@pytest.mark.parametrize("window_min", [15, 20])
def test_a_stall_with_a_long_configured_window_gives_nothing_up(monkeypatch, window_min):
    # TTU_SAFETY_GLM_WINDOW_MIN of 15 or more: every window reaches back the catch-up limit,
    # so judged by the window the stalled poll gave up what the next window — as long —
    # reads anyway, and device 1's watch started again for nothing. Judged by the cursor's
    # backlog (a minute), reading again catches up.
    b = Bucket(monkeypatch, starts(-1800, 240))
    clock = _stall(monkeypatch, b, hang={4, 5, 6, 7, 8})
    since = (NOW - timedelta(seconds=60)).timestamp()
    c = cfg(GLM_WINDOW_MIN=window_min)
    r1 = gl.poll_flashes(c, since=since, now=NOW + timedelta(seconds=300))
    assert r1["cursor"] == since and r1["skipped_s"] == 0 and r1["partial"] is True
    clock["t"], clock["n"] = 0.0, 100
    b.got.clear()
    b.keys = [key(t) for t in starts(-1800, 540)]
    r2 = gl.poll_flashes(c, since=r1["cursor"], now=NOW + timedelta(seconds=600))
    assert set(starts(-40, 180)) <= set(b.read_starts())
    assert (r2["partial"], r2["skipped_s"]) == (False, 0)


def test_a_flash_a_stalled_poll_did_not_reach_still_arms_the_latch(tmp_path, monkeypatch):
    # the same through the poller, with a flash 2 km from the site in a granule the
    # stalled poll did not reach: device 0 latches at the next poll (the first fix never)
    np = _real_numpy()
    monkeypatch.setattr(gl, "np", np)
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    lat, lon = config.GEOCODE
    flash_at = NOW + timedelta(seconds=60)
    b = Bucket(monkeypatch, starts(-900, 240),
               flashes={flash_at: (np.array([lat + 0.02]), np.array([lon - 0.02]))})
    clock = _stall(monkeypatch, b, hang={4, 5, 6, 7, 8})
    when = {"now": NOW + timedelta(seconds=300)}
    real = gl.poll_flashes
    monkeypatch.setattr(gl, "poll_flashes", lambda c, **kw: real(c, now=when["now"], **kw))
    p = gl.GlmLightningPoller(config, _events())
    p._cursor = (NOW - timedelta(seconds=60)).timestamp()
    assert p.poll_now(now=when["now"].timestamp())["in_ring"] is False
    assert p.component(-20.0, now=when["now"].timestamp() + 30)["latched"] is False
    clock["t"], clock["n"] = 0.0, 100
    when["now"] = NOW + timedelta(seconds=600)
    b.keys = [key(t) for t in starts(-900, 540)]
    assert p.poll_now(now=when["now"].timestamp())["in_ring"] is True
    assert p.component(-20.0, now=when["now"].timestamp() + 30)["latched"] is True


# ---- the cursor moves on when the deadline cuts a catch-up short ----------------------------
def test_a_catch_up_cut_short_by_the_deadline_moves_the_cursor_on(monkeypatch):
    # the first poll of the night (the cursor is from the morning) on a link that needs
    # 2.4 s a granule: it reads the newest 31 of the 43 granules of its 15-min catch-up
    b = Bucket(monkeypatch, starts(-900, -60))
    _slow(monkeypatch, b, 2.4)
    morning = (NOW - timedelta(hours=12)).timestamp()
    r = gl.poll_flashes(cfg(), since=morning, now=NOW)
    assert r["ok"] is True and r["granules_scanned"] == 31
    # what it gave up it says, as a hole in what it read...
    assert r["partial"] is True and "4 min of the window not read (from 02:55 UTC)" \
        in r["coverage"]
    # ...but the cursor moves on to the newest granule read (before, it stayed at the
    # morning's, and every later poll had the same 15-min window, cut short the same way)
    assert r["cursor"] == (NOW - timedelta(seconds=60)).timestamp()
    assert r["skipped_s"] > 12 * 3600 - 900          # the day, and what it gave up
    # the next poll's window is back to its normal size, and complete
    b.keys = [key(t) for t in starts(-900, 240)]
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r["cursor"], now=NOW + timedelta(seconds=300))
    assert r2["window_start"] == (NOW - timedelta(seconds=120)).timestamp()
    assert r2["granules_listed"] == 19 and r2["granules_scanned"] == 19
    assert (r2["partial"], r2["skipped_s"]) == (False, 0)
    assert r2["cursor"] == (NOW + timedelta(seconds=240)).timestamp()


def test_the_overlap_already_read_is_no_hole_when_a_slow_link_misses_it(monkeypatch):
    # 5 s a granule: the newest 15 of the 19 granules from a minute before the cursor are
    # read — every new one; what is left is the minute of overlap, which the poll before
    # read. Judged from the window's start, it counted as unread and the poll was partial.
    b = Bucket(monkeypatch, starts(-900, 240))
    _slow(monkeypatch, b, 5.0)
    since = (NOW - timedelta(seconds=60)).timestamp()
    r = gl.poll_flashes(cfg(), since=since, now=NOW + timedelta(seconds=300))
    assert r["granules_scanned"] == 15 and b.read_starts()[0] == NOW - timedelta(seconds=40)
    assert r["partial"] is False and r["coverage"] is None and r["skipped_s"] == 0
    assert r["cursor"] == (NOW + timedelta(seconds=240)).timestamp()


def test_new_granules_a_poll_did_not_get_to_make_it_partial_and_wait(monkeypatch):
    # 6 s a granule: 12 read, and one minute of NEW granules left besides the overlap —
    # less than a hole (MAX_GAP_S), but published and not read: the poll is partial (it
    # used to count as a good poll for the dew monitor), nothing is given up (the window
    # does not reach back the full catch-up), and the cursor stays: the next poll reads it.
    b = Bucket(monkeypatch, starts(-900, 240))
    _slow(monkeypatch, b, 6.0)
    since = (NOW - timedelta(seconds=60)).timestamp()
    r = gl.poll_flashes(cfg(), since=since, now=NOW + timedelta(seconds=300))
    assert r["granules_scanned"] == 12 and b.read_starts()[0] == NOW + timedelta(seconds=20)
    assert r["partial"] is True and r["skipped_s"] == 0
    assert r["coverage"] == "3 new granule(s) left for the next poll: the poll's 75 s ran out"
    assert r["cursor"] == since
    monkeypatch.setattr(gl, "_get", b.get)                    # (the link is fast again)
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r["cursor"], now=NOW + timedelta(seconds=310))
    assert set(starts(-40, 0)) <= set(b.read_starts())
    assert (r2["partial"], r2["skipped_s"]) == (False, 0)
    assert r2["cursor"] == (NOW + timedelta(seconds=240)).timestamp()


def test_a_failed_granule_at_the_start_of_the_first_window_is_read_again(monkeypatch):
    # (a guard: the minute before the cursor no longer holds the cursor up, so the first
    # poll's cursor must not claim the granule at its window's start when that failed)
    first = NOW - timedelta(seconds=300)
    b = Bucket(monkeypatch, starts(-300, -60), fail=[first])
    r1 = gl.poll_flashes(cfg(), now=NOW)
    assert r1["cursor"] < first.timestamp()
    b.keys = [key(t) for t in starts(-300, 240)]
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=300))
    assert r2["cursor"] == r1["cursor"]                 # it failed again: held
    b.fail.clear()
    b.got.clear()
    r3 = gl.poll_flashes(cfg(), since=r2["cursor"], now=NOW + timedelta(seconds=600))
    assert first in b.read_starts() and r3["cursor"] == (NOW + timedelta(seconds=240)).timestamp()


class _SlowSky:
    """A GLM bucket behind a slow link, on virtual clocks: a granule every 20 s, listed
    20 s after its end; a listing costs 0.5 s and a download ``cost`` s of both clocks
    (a request whose time budget is shorter fails when it is spent)."""

    def __init__(self, monkeypatch, cost, start):
        self.cost, self.wall, self.mono = cost, start.timestamp(), 1000.0
        self.by_prefix = {}
        t = start - timedelta(hours=1)
        while t <= start + timedelta(hours=13):
            k = key(t)
            self.by_prefix.setdefault(k.rsplit("/", 1)[0] + "/", []).append((t.timestamp(), k))
            t += timedelta(seconds=20)
        sky = self

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(sky.wall, tz=tz)
        monkeypatch.setattr(gl, "datetime", _DT)
        monkeypatch.setattr(gl, "_mono", lambda: sky.mono)
        monkeypatch.setattr(gl, "deps_available", lambda: True)
        monkeypatch.setattr(gl, "_list_keys", self.list_keys)
        monkeypatch.setattr(gl, "_get", self.get)
        monkeypatch.setattr(gl, "_flashes_from_buffer", lambda buf: (EMPTY, EMPTY))

    def advance(self, s):
        self.wall += s
        self.mono += s

    def list_keys(self, bucket, prefix, timeout=15):
        self.advance(0.5)
        return [k for s, k in self.by_prefix.get(prefix, ()) if s + 40 <= self.wall], 0.0

    def get(self, url, timeout=15):
        if self.cost > timeout:
            self.advance(timeout)
            raise OSError("timed out")
        self.advance(self.cost)
        return b"granule"


def _dawn_day_night(tmp_path, monkeypatch, cost):
    # The poller and the dew monitor as the daemon runs them (server.glm_loop wakes every
    # 60 s), over a dawn, a day without polls and 90 min of the next night, on a link that
    # needs `cost` s a granule. Returns (the night's polls, its lightning checks, sky,
    # poller).
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    sky = _SlowSky(monkeypatch, cost, datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC))
    poller = gl.GlmLightningPoller(config, _events())
    mon = dew.DewMonitor(config)

    def night(minutes):
        polls, checks = [], []
        stop = sky.wall + minutes * 60
        while sky.wall < stop:
            res = poller.maybe_poll(-20.0, now=sky.wall)
            if res is not None:
                polls.append(res)
            mon.update_lightning(poller.component(-20.0, now=sky.wall), now=sky.wall,
                                 mono=sky.mono)
            checks.append(mon._lightning(sky.mono)[0])
            sky.advance(60)
        return polls, checks

    night(20)                                   # the last polls before dawn
    sky.advance(10 * 3600)                      # the day: no polls
    polls, checks = night(90)
    return polls, checks, sky, poller


@pytest.mark.parametrize("cost", [1.74, 1.75, 2.0, 2.4, 3.5, 4.4])
def test_a_slow_link_is_back_to_normal_after_the_first_poll_of_the_night(tmp_path, monkeypatch,
                                                                         cost):
    # Before, the first poll of the night (a 15-min catch-up from the morning's cursor)
    # was cut short by its 75-s deadline, the cursor stayed at the morning, and so did
    # every poll after it: all partial, all night, and device 1's lightning check never
    # true. (At 1.74-1.75 s a granule the deadline cuts off the download of the
    # catch-up's last granule, which then was not given up: the catch-up repeated.)
    polls, checks, sky, poller = _dawn_day_night(tmp_path, monkeypatch, cost)
    assert all(p["ok"] for p in polls)
    # only the catch-up gives anything up; every later poll reads all it lists
    assert all(p["skipped_s"] == 0 for p in polls[1:])
    # at most the catch-up itself is partial (it says what it gave up)...
    assert [i for i, p in enumerate(polls) if p["partial"]] in ([], [0])
    # ...and from the next poll on the window is its normal size again (a minute before
    # the cursor to the poll: some 20-23 granules, where the catch-up listed 43)
    assert polls[0]["granules_listed"] == 43
    assert all(p["granules_listed"] <= 25 for p in polls[1:])
    assert sky.wall - poller._cursor < 400
    # device 1: the watch starts at the first complete poll; 30 min later it is true
    assert checks[-1] is True and checks[-15:] == [True] * 15


def test_a_link_too_slow_to_read_every_granule_keeps_device_1_false(tmp_path, monkeypatch):
    # 5 s a granule: even a normal 5-minute window (some 20 granules) cannot be read
    # within the 75-s deadline. A poll cut short leaves its oldest new granules to the
    # next one (the cursor stays); that window is longer and cut short too, until one
    # reaches back the full 15-min catch-up: it gives up what it did not get to, the
    # cursor moves on, and the next window is back to its normal size. Each give-up is
    # reported (skipped_s), and the dew monitor's watch starts again every time: device 1
    # never vouches for lightning data nobody read. (A wrong FALSE only costs observing
    # time; this link is ~1 Mbit/s.)
    polls, checks, sky, poller = _dawn_day_night(tmp_path, monkeypatch, 5.0)
    assert all(p["ok"] for p in polls)
    night = polls[1:]
    assert all(p["granules_listed"] <= 45 for p in night)     # never more than the limit
    gave_up = [i for i, p in enumerate(night) if p["skipped_s"] > 0]
    assert len(gave_up) >= len(night) // 4                    # every few polls
    assert all(night[i + 1]["granules_listed"] <= 22 for i in gave_up if i + 1 < len(night))
    assert sky.wall - poller._cursor < 1200                   # the cursor does not stick
    assert True not in checks


# ---- a granule listed late is read by the next poll -----------------------------------------
@pytest.mark.parametrize("missing", [[-140], [-160, -140], [-200, -180, -160, -140]])
def test_a_granule_listed_late_behind_newer_ones_is_read_next_poll(monkeypatch, missing):
    # poll 1 lists the granules up to -60 s, but not the ones starting at `missing`
    # (published late: they reach S3 out of order). Before, the cursor passed the gap and
    # the next window began a minute before it: those granules were read by no poll, and
    # neither poll was partial.
    listed = [t for t in starts(-900, -60) if (t - NOW).total_seconds() not in missing]
    b = Bucket(monkeypatch, listed)
    r1 = gl.poll_flashes(cfg(), now=NOW)
    assert r1["partial"] is False
    # Where the cursor went past the gap (at most a minute past it), the gap's slots are
    # pending, for the next poll to read first; a longer gap stops the cursor before it.
    passed = [(NOW + timedelta(seconds=m)).timestamp() for m in missing
              if (NOW + timedelta(seconds=m)).timestamp() < r1["cursor"]]
    assert r1["pending"] == passed
    assert passed or r1["cursor"] < (NOW + timedelta(seconds=missing[0])).timestamp()
    b.keys = [key(t) for t in starts(-900, 240)]           # now they are listed
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=300),
                         pending=r1["pending"])
    for m in missing:
        assert NOW + timedelta(seconds=m) in b.read_starts(), m
    assert sorted(gl._key_start(k).timestamp() for k in b.got[:len(passed)]) == passed
    assert r2["partial"] is False and r2["cursor"] == (NOW + timedelta(seconds=240)).timestamp()
    assert r2["pending"] == []


def test_late_granules_cost_no_extra_reads_within_the_measured_spread(monkeypatch):
    # one granule listed after the three newer ones (the most measured): the minute of
    # overlap reaches it, the cursor goes to the newest granule as before
    b = Bucket(monkeypatch, [t for t in starts(-900, -60) if t != NOW - timedelta(seconds=120)])
    r = gl.poll_flashes(cfg(), now=NOW)
    assert r["cursor"] == (NOW - timedelta(seconds=60)).timestamp()
    # a granule missing long enough to be missing for good holds nothing up either
    b.keys = [key(t) for t in starts(-900, 240) if t != NOW - timedelta(seconds=120)]
    r = gl.poll_flashes(cfg(), since=r["cursor"], now=NOW + timedelta(seconds=300),
                        pending=r["pending"])
    assert r["cursor"] == (NOW + timedelta(seconds=240)).timestamp()
    assert r["pending"] == [] and r["skipped_s"] == 0          # (nothing was published)


def test_a_late_granule_whose_download_fails_once_is_read_by_the_poll_after(monkeypatch):
    # Listed late, the granule lies in the next poll's minute of overlap, which an earlier
    # poll read already — except this one. The first fix read it there once: one failed
    # download lost it for good, and no poll said so (device 1 counted the time as
    # watched). Now that poll has to read it: failing that, the cursor stays.
    late = NOW - timedelta(seconds=100)
    b = Bucket(monkeypatch, [t for t in starts(-900, -60) if t != late])
    r1 = gl.poll_flashes(cfg(), now=NOW)
    assert r1["pending"] == [late.timestamp()]
    b.keys = [key(t) for t in starts(-900, 240)]
    b.fail = {key(late)}
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=300),
                         pending=r1["pending"])
    assert b.got[0] == key(late)                              # tried first
    assert r2["cursor"] == r1["cursor"] and r2["pending"] == [late.timestamp()]
    assert (r2["partial"], r2["skipped_s"]) == (False, 0)     # (one granule: no hole)
    b.fail.clear()
    b.got.clear()
    r3 = gl.poll_flashes(cfg(), since=r2["cursor"], now=NOW + timedelta(seconds=600),
                         pending=r2["pending"])
    assert late in b.read_starts() and r3["pending"] == []
    assert r3["cursor"] == (NOW + timedelta(seconds=240)).timestamp()


def test_a_late_granule_is_read_first_so_a_slow_link_gets_to_it(monkeypatch):
    # 4.2 s a granule: the late granule is the oldest of the next window, which the first
    # fix read last, so the deadline cut it off — and, in the minute of overlap, it was
    # passed without a word
    late = NOW - timedelta(seconds=100)
    b = Bucket(monkeypatch, [t for t in starts(-900, -60) if t != late])
    r1 = gl.poll_flashes(cfg(), now=NOW)
    b.keys = [key(t) for t in starts(-900, 240)]
    b.got.clear()
    _slow(monkeypatch, b, 4.2)
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=300),
                         pending=r1["pending"])
    assert b.got[0] == key(late) and late in b.read_starts()
    assert r2["pending"] == [] and r2["skipped_s"] == 0
    assert r2["cursor"] == (NOW + timedelta(seconds=240)).timestamp()


def test_a_late_granule_no_poll_read_is_reported_as_time_no_poll_read(monkeypatch):
    # The poll after the late granule's listing comes 20 min later (one failed between):
    # its window reaches back only 15 minutes, and the granule, never read, falls out of
    # it. It is counted with the minutes before that window (skipped_s), so the dew
    # monitor's watch starts again.
    late = NOW - timedelta(seconds=100)
    b = Bucket(monkeypatch, [t for t in starts(-900, -60) if t != late])
    r1 = gl.poll_flashes(cfg(), now=NOW)
    b.keys = [key(t) for t in starts(-900, 1140)]
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=1200),
                         pending=r1["pending"])
    assert late not in b.read_starts()
    assert r2["skipped_s"] == 340 + 20             # -40 s .. +300 s, and the late granule
    assert r2["pending"] == []


def test_a_late_granule_just_before_a_window_the_limit_cut_is_reported(monkeypatch):
    # The next good poll comes 865 s after the late granule's poll: its window starts 5 s
    # after that granule, which is published by then but outside the window. It used to
    # be neither read nor counted (it lay less than half a granule before the window).
    late = NOW - timedelta(seconds=100)
    b = Bucket(monkeypatch, [t for t in starts(-900, -60) if t != late])
    r1 = gl.poll_flashes(cfg(), now=NOW)
    b.keys = [key(t) for t in starts(-900, 780)]
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=805),
                         pending=r1["pending"])
    assert r2["window_start"] == (NOW - timedelta(seconds=95)).timestamp()
    assert late not in b.read_starts()
    assert r2["skipped_s"] == 20 and r2["pending"] == []


def _night_with_one_late_granule(monkeypatch, tmp_path, late_off):
    """The poller and the dew monitor as the daemon runs them (glm_loop every 60 s, the
    evaluator every 30 s) for 2 h on virtual clocks; granules reach S3 20 s after their
    end, except the one at ``late_off`` s, which reaches it 150 s after its end — after
    newer ones — and whose first download fails. Returns (its start, whether it was ever
    read, the wall times of the evaluations at which the lightning check was true)."""
    t0 = datetime(2026, 11, 13, 2, 0, 0, tzinfo=UTC).timestamp()
    clk = {"wall": t0, "mono": 1000.0}
    late = t0 + late_off
    tries, read = {}, set()

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clk["wall"], tz=tz)

    def list_keys(bucket, prefix, timeout=15):
        clk["wall"] += 0.5
        clk["mono"] += 0.5
        out, x = [], t0 - 3600
        while x <= clk["wall"]:
            k = key(datetime.fromtimestamp(x, tz=UTC))
            if x + 20 + (150.0 if x == late else 20.0) <= clk["wall"] and k.startswith(prefix):
                out.append(k)
            x += 20
        return out, 0.0

    def get(url, timeout=15):
        k = url.split(".amazonaws.com/", 1)[1]
        x = gl._key_start(k).timestamp()
        clk["wall"] += 0.45
        clk["mono"] += 0.45
        tries[x] = tries.get(x, 0) + 1
        if x == late and tries[x] == 1:
            raise OSError("granule download failed")
        read.add(x)
        return k.encode()
    monkeypatch.setattr(gl, "datetime", _DT)
    monkeypatch.setattr(gl, "_mono", lambda: clk["mono"])
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "_list_keys", list_keys)
    monkeypatch.setattr(gl, "_get", get)
    monkeypatch.setattr(gl, "_flashes_from_buffer", lambda buf: (EMPTY, EMPTY))
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    poller = gl.GlmLightningPoller(config, _events())
    mon = dew.DewMonitor(config)
    true_at = []
    while clk["wall"] < t0 + 2 * 3600:
        poller.maybe_poll(-20.0, now=clk["wall"])
        for _ in range(2):
            mon.update_lightning(poller.component(-20.0, now=clk["wall"]), now=clk["wall"],
                                 mono=clk["mono"])
            if mon._lightning(clk["mono"])[0] is True:
                true_at.append(clk["wall"])
            clk["wall"] += 30
            clk["mono"] += 30
    return late, late in read, true_at


def test_device_1_is_never_true_with_a_late_granule_no_poll_read_in_its_watch(monkeypatch,
                                                                              tmp_path):
    # the late granule's first download fails, wherever it falls between two polls: a
    # later poll reads it (the first fix left it unread for 3 of these 16 positions, and
    # device 1 true for half an hour with it in its watch), and device 1 works as usual
    for k in range(16):
        late, was_read, true_at = _night_with_one_late_granule(monkeypatch, tmp_path,
                                                               2400 + 20 * k)
        assert was_read, k
        assert len(true_at) > 150, k                # (true for the last ~1.5 h of 2 h)


# ---- time no poll read never counts as watched for device 1 ---------------------------------
def _dew_cfg(**over):
    c = types.SimpleNamespace(**{k: getattr(config, k) for k in dir(config) if k.isupper()})
    c.__dict__.update(over)
    return c


def _comp(ts, **over):
    c = {"safe": True, "enabled": True, "available": True, "latched": False,
         "nearest_km": None, "polling_active": True, "last_poll_ts": ts,
         "granules_scanned": 15, "granules_in_window": 14, "partial": False,
         "coverage": None, "skipped_s": 0}
    c.update(over)
    return c


def _watched(mon, polls):
    """Take in (time, component) pairs, each one minute after its poll; the lightning
    block at the last."""
    for t, comp in polls:
        mon.update_lightning(comp, now=t + 60, mono=t + 60)
    return mon._lightning(polls[-1][0] + 60)


def test_a_catch_up_limit_that_leaves_minutes_unread_restarts_the_watch(monkeypatch):
    # TTU_SAFETY_GLM_POLL_INTERVAL=600: device 1 takes good polls 25 min apart as one
    # watch (DEW_GLM_MAX_AGE_MIN). One poll fails; the next good one comes 20 min after
    # the last, but its window reaches back only MAX_CATCHUP_S (15 min): the minutes in
    # between were read by no poll. That poll is complete, but says so (skipped_s)...
    b = Bucket(monkeypatch, starts(-900, -60))
    r1 = gl.poll_flashes(cfg(), now=NOW)
    b.keys = [key(t) for t in starts(-900, 1140)]
    r3 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=1200))
    assert r3["window_start"] == (NOW + timedelta(seconds=300)).timestamp()
    assert r3["partial"] is False and r3["skipped_s"] == 340       # -40 s .. +300 s
    # ...and device 1's watch starts again there instead of running on across them
    mon = dew.DewMonitor(_dew_cfg(DEW_GLM_MAX_AGE_MIN=25.0))
    t0 = NOW.timestamp()
    hour = [(t0 + s, _comp(t0 + s)) for s in range(-3600, 1, 600)]
    check, block, _ = _watched(mon, hour)
    assert check is True and block["watched_s"] == 3600
    check, block, reasons = _watched(mon, [(t0 + 1200, _comp(t0 + 1200,
                                                             skipped_s=r3["skipped_s"]))])
    assert check is None and block["watched_s"] == 0
    assert "watched for only 0 of the 30 min" in reasons[0]


def test_a_catch_up_limit_a_fraction_of_a_second_past_a_granule_still_counts_it(monkeypatch):
    # the cursor's granule was read; the next good poll comes 920.3 s later, so its
    # 15-min window starts 0.3 s after the next granule does and leaves it out. It is
    # counted as a whole granule: rounded to whole seconds, 0.3 s was 0, and the dew
    # monitor's watch ran on across a granule nobody read.
    b = Bucket(monkeypatch, starts(-3600, 3600))
    r = gl.poll_flashes(cfg(), since=NOW.timestamp(), now=NOW + timedelta(seconds=920.3))
    # (no pytest.approx: it breaks on the stand-in numpy the page tests leave behind)
    assert abs(r["window_start"] - (NOW + timedelta(seconds=20.3)).timestamp()) < 1e-6
    assert NOW + timedelta(seconds=20) not in b.read_starts()
    assert r["partial"] is False and r["skipped_s"] == 20
    # ... however little before the window it starts (a tolerance of 0.05 s still hid it)
    b.got.clear()
    r = gl.poll_flashes(cfg(), since=NOW.timestamp(), now=NOW + timedelta(seconds=920.03))
    assert NOW + timedelta(seconds=20) not in b.read_starts() and r["skipped_s"] == 20


def test_a_partial_poll_that_gave_time_up_ends_the_watch():
    # a slow link gave up the oldest granules of a catch-up for good (partial, skipped):
    # good polls around it do not make that time watched
    t0 = 100000.0
    mon = dew.DewMonitor(config)
    _watched(mon, [(t0 + s, _comp(t0 + s)) for s in range(-3600, 1, 300)])
    check, block, _ = _watched(mon, [(t0 + 300, _comp(t0 + 300, partial=True, skipped_s=240,
                                                      coverage="4 min of the window not read"))])
    assert check is None and block["watched_s"] == 0
    check, block, _ = _watched(mon, [(t0 + 600, _comp(t0 + 600))])
    assert check is None and block["watched_s"] == 0           # a new watch starts
    check, block, _ = _watched(mon, [(t0 + s, _comp(t0 + s)) for s in range(900, 2701, 300)])
    assert check is True and block["watched_s"] == 2100
    # a partial poll that gave nothing up (a hole the next poll reads again) changes
    # nothing, as before: one poll that is not good is tolerated
    check, block, _ = _watched(mon, [(t0 + 3000, _comp(t0 + 3000, partial=True))])
    assert check is True and block["watched_s"] == 2400


def test_the_component_carries_what_the_last_poll_left_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "poll_flashes", lambda c, **kw: {
        "ok": True, "granules_scanned": 31, "granules_in_window": 14, "in_ring": False,
        "nearest_km": None, "partial": True, "coverage": "4 min of the window not read",
        "skipped_s": 43440, "cursor": 5.0})
    p = gl.GlmLightningPoller(config, _events())
    p.poll_now(now=1000.0)
    assert p.component(-10.0, 1001.0)["skipped_s"] == 43440
    # an old poll is no current news
    assert p.component(-10.0, 1000.0 + config.GLM_STALE_AFTER_SEC + 1)["skipped_s"] == 0
    assert gl.unavailable_component(config)["skipped_s"] == 0


# ---- failures and partial polls: logged when they start and when they end ------------------
def _poller_at_now(monkeypatch):
    real = gl.poll_flashes
    monkeypatch.setattr(gl, "poll_flashes", lambda c, **kw: real(c, now=NOW, **kw))
    monkeypatch.setattr(config, "GLM_LATCH_FILE", "/nonexistent-dir/glm_latch.json")
    return gl.GlmLightningPoller(config, _events())


def _glm_lines(caplog, level):
    return [r.getMessage() for r in caplog.records
            if r.name == "ttu.safety.glm" and r.levelno == level]


def test_unreadable_granules_are_logged_once_with_the_cause(monkeypatch, caplog):
    # listed, but none can be read (a broken netCDF4 after an upgrade, every download
    # failing): before, a warning on every poll on top of the one when it started
    b = Bucket(monkeypatch, starts(-300, -60), fail=starts(-300, -60))
    p = _poller_at_now(monkeypatch)
    with caplog.at_level(logging.INFO, logger="ttu.safety.glm"):
        for _ in range(4):
            assert p.poll_now()["ok"] is False
        b.fail.clear()
        assert p.poll_now()["ok"] is True
    warnings = _glm_lines(caplog, logging.WARNING)
    assert len(warnings) == 1, warnings
    assert warnings[0].startswith(
        "GLM poll failed: none of the 13 GLM granules of the last 5 min could be read "
        "(first error: OR_GLM-L2-LCFA_G19_s")
    assert "granule download failed" in warnings[0]
    assert _glm_lines(caplog, logging.INFO) == ["GLM polls work again (13 granules read)"]


def test_a_failure_whose_cause_changes_is_logged_again_with_the_new_cause(monkeypatch, caplog):
    # After a boot the first listing fails (no name resolution yet); from then on the
    # granules are listed but none can be read (say, a broken HDF5 after an upgrade).
    # Before, the journal showed the listing's error all night and never the cause that
    # lasted.
    b = Bucket(monkeypatch, starts(-300, -60), fail=starts(-300, -60))
    p = _poller_at_now(monkeypatch)

    def no_dns(bucket, prefix, timeout=15):
        raise OSError("<urlopen error [Errno -3] Temporary failure in name resolution>")
    with caplog.at_level(logging.INFO, logger="ttu.safety.glm"):
        monkeypatch.setattr(gl, "_list_keys", no_dns)
        assert p.poll_now()["ok"] is False
        monkeypatch.setattr(gl, "_list_keys", b.list_keys)
        for _ in range(3):
            assert p.poll_now()["ok"] is False
    warnings = _glm_lines(caplog, logging.WARNING)
    assert len(warnings) == 2, warnings
    assert warnings[0].startswith("GLM poll failed: list: <urlopen error [Errno -3]")
    assert warnings[1].startswith(
        "GLM poll failed: none of the 13 GLM granules of the last 5 min could be read "
        "(first error: OR_GLM-L2-LCFA_G19_s")
    assert "granule download failed" in warnings[1]


def test_partial_polls_are_logged_when_they_start_and_when_they_end(monkeypatch, caplog):
    # a hole in the feed (the next polls read the same window: it stays a hole), and one
    # granule failing on every poll: before, two INFO lines on every poll
    b = Bucket(monkeypatch, starts(-300, -240) + starts(-60, -40),
               fail=[NOW - timedelta(seconds=40)])
    p = _poller_at_now(monkeypatch)
    with caplog.at_level(logging.INFO, logger="ttu.safety.glm"):
        for _ in range(3):
            assert p.poll_now()["partial"] is True
        b.keys = [key(t) for t in starts(-300, -40)]
        b.fail.clear()
        assert p.poll_now()["partial"] is False
    infos = _glm_lines(caplog, logging.INFO)
    assert len(infos) == 2, infos
    assert infos[0].startswith("GLM poll partial: 3 min of the window not read (from 03:06 UTC)")
    assert infos[1] == "GLM polls read their whole window again"
    assert _glm_lines(caplog, logging.WARNING) == []


# ---- the docs name the temp file the code writes ---------------------------------------------
def test_the_docs_name_the_glm_temp_file_the_code_writes():
    def read(*path):
        with open(os.path.join(REPO, *path), encoding="utf-8") as f:
            return f.read()
    md, hk, src = read("README_SAFETY.md"), read("safety", "housekeeping.py"), \
        read("safety", "glm_lightning.py")
    # every granule goes through a file in /dev/shm named ttu-glm-*.nc, which the
    # start-up sweep knows
    assert 'dir="/dev/shm", prefix="ttu-glm-", suffix=".nc"' in src
    assert '"ttu-glm-*.nc"' in hk
    rows = [line for line in md.splitlines()
            if line.startswith("| `/dev/shm/ttu-glm-*.nc`")]
    assert len(rows) == 1 and "per GLM poll" in rows[0] and rows[0].endswith("| RAM |")
    # what the merge left stale: /dev/shm is where every granule is read, not a fallback
    for stale in ("/dev/shm/tmp*.nc", "~15 per GLM poll", "netCDF fallback",
                  "`/dev/shm` fallback", "No time is left unread between polls",
                  "counts as unread and is read by the next poll"):
        assert stale not in md, stale
    assert "netCDF fallback" not in hk
