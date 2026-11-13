"""What one GLM poll reads: the cursor that leaves no time unread between polls, partial
polls (a hole or a lag), a granule that failed read again, the per-granule guard, the
nearest flash over finite coordinates, the caps, the temp-file prefix, the GLM settings.

The listing and the download are stubbed (no network). numpy and netCDF4 are optional:
the cursor and completeness tests use granules without flashes, which never reach numpy
(CI installs neither); the flash tests need the real numpy and skip without it."""
import io
import json
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from safety import config, dew, glm_lightning as gl, netio

UTC = timezone.utc
NOW = datetime(2026, 11, 13, 3, 10, 0, tzinfo=UTC)
EMPTY = types.SimpleNamespace(size=0)        # a granule's flash arrays when it has none


def _real_numpy():
    np = pytest.importorskip("numpy")
    # the page tests put a MagicMock 'numpy' into sys.modules when numpy is absent
    if not isinstance(getattr(np, "__version__", None), str):
        pytest.skip("numpy not installed")
    return np


def key(t):
    """A granule key starting at datetime t (as the bucket names them)."""
    s = t.strftime("%Y%j%H%M%S") + "%d" % (t.microsecond // 100000)
    return ("%s/%s/OR_GLM-L2-LCFA_G19_s%s_e%s_c%s.nc"
            % (gl.PREFIX, t.strftime("%Y/%j/%H"), s, s, s))


def starts(first_s, last_s, step=20):
    """Granule starts from first_s to last_s seconds relative to NOW."""
    return [NOW + timedelta(seconds=x) for x in range(first_s, last_s + 1, step)]


class Bucket:
    """A stubbed S3 bucket: listed granules (by start), the ones that fail to download,
    the flashes of some, and a record of what was listed and downloaded."""

    def __init__(self, monkeypatch, granules, fail=(), flashes=None, skew=0.0):
        self.keys = [key(t) for t in granules]
        self.fail = {key(t) for t in fail}
        self.flashes = {key(t): v for t, v in (flashes or {}).items()}
        self.listed, self.got = [], []
        self.skew = skew
        monkeypatch.setattr(gl, "deps_available", lambda: True)
        monkeypatch.setattr(gl, "_list_keys", self.list_keys)
        monkeypatch.setattr(gl, "_get", self.get)
        monkeypatch.setattr(gl, "_flashes_from_buffer", self.parse)

    def list_keys(self, bucket, prefix, timeout=15):
        self.listed.append(prefix)
        return [k for k in self.keys if k.startswith(prefix)], self.skew

    def get(self, url, timeout=15):
        k = url.split(".amazonaws.com/", 1)[1]
        self.got.append(k)
        if k in self.fail:
            raise OSError("granule download failed")
        return k.encode()

    def parse(self, buf):
        return self.flashes.get(buf.decode(), (EMPTY, EMPTY))

    def read_starts(self):
        return sorted(gl._key_start(k) for k in self.got if k not in self.fail)


def cfg(**over):
    c = types.SimpleNamespace(GLM_WINDOW_MIN=5, GLM_BUCKET="b", GEOCODE=(33.58, -101.88),
                              GLM_TRIGGER_KM=50.0, SERVER_CLOCK_TOL_SEC=60)
    c.__dict__.update(over)
    return c


# ---- C1: the window, the cursor, completeness --------------------------------------------
def test_first_poll_reads_the_configured_window_and_returns_a_cursor(monkeypatch):
    b = Bucket(monkeypatch, starts(-900, -60))
    r = gl.poll_flashes(cfg(), now=NOW)
    assert r["ok"] is True and r["partial"] is False and r["coverage"] is None
    assert b.read_starts()[0] >= NOW - timedelta(minutes=5)
    assert r["granules_scanned"] == r["granules_in_window"] == 13      # -300 .. -60
    assert r["cursor"] == (NOW - timedelta(seconds=60)).timestamp()
    assert b.listed == ["GLM-L2-LCFA/2026/317/03/"]     # the window is within one hour


def test_the_next_poll_reads_what_the_previous_could_not_yet(monkeypatch):
    # poll 1 at NOW sees granules up to 60 s old (the newer ones are not published yet);
    # poll 2 comes 6 min later. The old fixed 5-minute look-back of poll 2 began at
    # NOW+60 s: the granules from -40 to +40 s were read by no poll.
    b = Bucket(monkeypatch, starts(-900, -60))
    r1 = gl.poll_flashes(cfg(), now=NOW)
    later = NOW + timedelta(seconds=360)
    b.keys = [key(t) for t in starts(-900, 300)]
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=later)
    got = b.read_starts()
    assert got[0] == NOW - timedelta(seconds=120)        # one minute of overlap
    for t in starts(-40, 40):
        assert t in got                                  # the minutes no poll read before
    assert r2["partial"] is False and r2["cursor"] == (NOW + timedelta(seconds=300)).timestamp()
    # the dew monitor judges a poll by its configured window only, as before
    assert r2["granules_in_window"] == 13 and r2["granules_scanned"] == 22


def test_a_granule_that_failed_is_read_again_next_poll(monkeypatch):
    bad = NOW - timedelta(seconds=200)
    b = Bucket(monkeypatch, starts(-900, -60), fail=[bad])
    r1 = gl.poll_flashes(cfg(), now=NOW)
    assert r1["ok"] is True and r1["granules_unread"] == 1
    assert r1["partial"] is False                        # 40 s unread is no hole
    assert r1["cursor"] == (bad - timedelta(seconds=20)).timestamp()     # stops before it
    b.fail.clear()
    b.got.clear()
    r2 = gl.poll_flashes(cfg(), since=r1["cursor"], now=NOW + timedelta(seconds=330))
    assert bad in b.read_starts()
    assert r2["cursor"] > bad.timestamp()


def test_a_lagging_feed_makes_the_poll_partial(monkeypatch):
    # the newest granule ends 4 min before the poll: "no flash" vouches for nothing newer
    Bucket(monkeypatch, starts(-600, -260))
    r = gl.poll_flashes(cfg(), now=NOW)
    assert r["ok"] is True and r["partial"] is True
    assert "newest granule read ends 4 min before the poll" in r["coverage"]


def test_a_hole_makes_the_poll_partial(monkeypatch):
    Bucket(monkeypatch, starts(-300, -240) + starts(-60, -40))
    r = gl.poll_flashes(cfg(), now=NOW)
    assert r["partial"] is True and "of the window not read (from 03:06 UTC)" in r["coverage"]
    # and the cursor stays before the hole, so a late-published granule there is read
    assert r["cursor"] == (NOW - timedelta(seconds=240)).timestamp()


def test_catch_up_is_capped(monkeypatch):
    b = Bucket(monkeypatch, starts(-3500, -60))
    r = gl.poll_flashes(cfg(), since=(NOW - timedelta(hours=3)).timestamp(), now=NOW)
    assert b.read_starts()[0] == NOW - timedelta(seconds=gl.MAX_CATCHUP_S)
    assert r["window_start"] == (NOW - timedelta(seconds=gl.MAX_CATCHUP_S)).timestamp()
    assert len(b.listed) == 2                           # 02:55 .. 03:10 spans two hours


def test_a_cursor_from_the_future_is_ignored(monkeypatch):
    b = Bucket(monkeypatch, starts(-900, -60))
    gl.poll_flashes(cfg(), since=(NOW + timedelta(hours=2)).timestamp(), now=NOW)
    assert b.read_starts()[0] == NOW - timedelta(minutes=5)


def test_nothing_read_is_no_data_and_keeps_no_cursor(monkeypatch):
    Bucket(monkeypatch, starts(-300, -60), fail=starts(-300, -60))
    r = gl.poll_flashes(cfg(), now=NOW)
    assert r["ok"] is False and "cursor" not in r
    assert r["error"] == "none of the 13 GLM granules of the last 5 min could be read"


# ---- C4: caps on keys, granules per poll, the deadline, reply sizes ------------------------
def test_keys_without_a_start_time_are_dropped(monkeypatch):
    b = Bucket(monkeypatch, starts(-300, -60))
    b.keys.append(gl.PREFIX + "/2026/317/03/OR_GLM-L2-LCFA_G19_junk.nc")
    gl.poll_flashes(cfg(), now=NOW)
    assert not any("junk" in k for k in b.got)


def test_granules_read_per_poll_are_capped(monkeypatch):
    # a listing of 1000 keys a second apart: one per 20 s of the window plus a margin
    b = Bucket(monkeypatch, [NOW - timedelta(seconds=x) for x in range(1, 290)])
    r = gl.poll_flashes(cfg(), now=NOW)
    assert len(b.got) == 300 // gl.GRANULE_S + gl.GRANULE_MARGIN
    assert r["granules_unread"] == 289 - len(b.got)
    # the newest were read; the cursor cannot pass the unread older ones
    assert b.read_starts()[-1] == NOW - timedelta(seconds=1)
    assert r["cursor"] == (NOW - timedelta(seconds=300)).timestamp()


def test_the_poll_has_a_deadline(monkeypatch):
    b = Bucket(monkeypatch, starts(-300, -60))
    clock = {"t": 0.0}

    def mono():
        clock["t"] += 10.0                                # every look costs 10 s
        return clock["t"]
    monkeypatch.setattr(gl, "_mono", mono)
    r = gl.poll_flashes(cfg(), now=NOW)
    assert 0 < len(b.got) < 13 and r["granules_unread"] == 13 - len(b.got)
    assert r["ok"] is True and r["partial"] is True       # the oldest minutes were not read


def test_replies_are_read_through_the_capped_reader(monkeypatch):
    class Reply(io.BytesIO):
        def __init__(self, body, declared):
            super().__init__(body)
            self.headers = {"Content-Length": str(declared)}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()
    replies = {}
    monkeypatch.setattr(gl.urllib.request, "urlopen",
                        lambda req, timeout=None: replies[req.full_url])
    listing = "https://b.s3.amazonaws.com/?list-type=2&max-keys=1000&prefix=x"
    replies[listing] = Reply(b"<Key>a</Key>", gl.MAX_LIST_BYTES + 1)
    with pytest.raises(netio.ReplyTooLarge):
        gl._fetch(listing)
    replies[listing] = Reply(b"<Key>a</Key>", 12)
    assert gl._fetch(listing)[0] == b"<Key>a</Key>"
    granule = "https://b.s3.amazonaws.com/GLM-L2-LCFA/x.nc"
    replies[granule] = Reply(b"x" * 10, gl.MAX_GRANULE_BYTES + 1)
    with pytest.raises(netio.ReplyTooLarge):
        gl._get(granule)
    # a listing that is too large is a failed poll, not an exception out of it
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "_fetch", lambda url, timeout=15: (_ for _ in ()).throw(
        netio.ReplyTooLarge("GLM listing: reply over the limit")))
    r = gl.poll_flashes(cfg(), now=NOW)
    assert r["ok"] is False and r["error"].startswith("list: ")


# ---- C2 / C3: the per-granule guard and the nearest flash ---------------------------------
def test_a_malformed_granule_costs_itself_only(monkeypatch):
    np = _real_numpy()
    monkeypatch.setattr(gl, "np", np)
    site = (33.58, -101.88)
    near = NOW - timedelta(seconds=100)
    bad = NOW - timedelta(seconds=200)
    flat = NOW - timedelta(seconds=220)
    b = Bucket(monkeypatch, starts(-300, -60), flashes={
        near: (np.array([33.60]), np.array([-101.90])),                 # ~3 km away
        bad: (np.array([33.6, 33.7, 33.8]), np.array([-101.9, -101.9])),  # 3 lats, 2 lons
        flat: (np.zeros((2, 2)), np.zeros((2, 2))),                    # not 1-D
    })
    r = gl.poll_flashes(cfg(GEOCODE=site), now=NOW)
    assert r["ok"] is True and r["in_ring"] is True and r["nearest_km"] < 5
    assert r["granules_unread"] == 2
    assert r["cursor"] == (flat - timedelta(seconds=20)).timestamp()   # read again next poll
    assert r["granules_scanned"] == 11 and len(b.got) == 13      # both were downloaded


def test_the_nearest_flash_is_found_past_a_nan(monkeypatch):
    np = _real_numpy()
    monkeypatch.setattr(gl, "np", np)
    g = NOW - timedelta(seconds=100)
    Bucket(monkeypatch, starts(-300, -60), flashes={
        g: (np.array([np.nan, 40.0, 33.60, 95.0]), np.array([-101.9, -101.9, -101.90, 0.0]))})
    r = gl.poll_flashes(cfg(), now=NOW)
    assert r["nearest_km"] < 5 and r["in_ring"] is True and r["flashes_in_ring"] == 1
    assert r["flashes_dropped"] == 2                     # the NaN and the latitude of 95


def test_a_flash_in_a_partial_poll_still_arms_the_latch(tmp_path, monkeypatch):
    np = _real_numpy()
    monkeypatch.setattr(gl, "np", np)
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    g = NOW - timedelta(seconds=280)
    Bucket(monkeypatch, starts(-300, -260),
           flashes={g: (np.array([33.60]), np.array([-101.90]))})
    real = gl.poll_flashes
    monkeypatch.setattr(gl, "poll_flashes", lambda c, **kw: real(c, now=NOW, **kw))
    p = gl.GlmLightningPoller(config, types.SimpleNamespace(record=lambda *a, **k: {}))
    monkeypatch.setattr(config, "GEOCODE", (33.58, -101.88))
    res = p.poll_now(now=NOW.timestamp())
    assert res["partial"] is True and res["in_ring"] is True
    c = p.component(-10.0, NOW.timestamp() + 1)
    assert c["latched"] is True and c["safe"] is False and c["partial"] is True
    assert any("did not read its whole window" in w for w in c["warnings"])


# ---- the poller: cursor kept, passed on, dropped on a clock step --------------------------
def test_the_poller_passes_the_cursor_on(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    calls = []

    def poll(c, **kw):
        calls.append(kw)
        return {"ok": True, "granules_scanned": 15, "in_ring": False, "nearest_km": None,
                "cursor": 1000.0 + len(calls)}
    monkeypatch.setattr(gl, "poll_flashes", poll)
    p = gl.GlmLightningPoller(config, types.SimpleNamespace(record=lambda *a, **k: {}))
    p.poll_now(now=2000.0)
    p.poll_now(now=2300.0)
    assert calls == [{}, {"since": 1001.0}]            # the first poll: no cursor at all
    monkeypatch.setattr(gl, "poll_flashes", lambda c, **kw: calls.append(kw) or {
        "ok": False, "error": "list: timed out"})
    p.poll_now(now=2600.0)                               # a failed poll keeps the cursor
    p.clock_stepped(2600.0, 9000.0)
    p.poll_now(now=9000.0)
    assert calls[2:] == [{"since": 1002.0}, {}]


def test_component_and_unavailable_block_carry_the_new_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(tmp_path / "glm_latch.json"))
    monkeypatch.setattr(gl, "deps_available", lambda: True)
    monkeypatch.setattr(gl, "poll_flashes", lambda c, **kw: {
        "ok": True, "granules_scanned": 22, "granules_in_window": 15, "in_ring": False,
        "nearest_km": None, "partial": False, "coverage": None, "cursor": 5.0})
    p = gl.GlmLightningPoller(config, types.SimpleNamespace(record=lambda *a, **k: {}))
    p.poll_now(now=1000.0)
    c = p.component(-10.0, 1001.0)
    assert c["available"] is True and c["partial"] is False and c["warnings"] == []
    assert (c["granules_scanned"], c["granules_in_window"]) == (22, 15)
    u = gl.unavailable_component(config)
    assert u["partial"] is False and u["granules_in_window"] == 0


# ---- C1(d): the dew monitor -------------------------------------------------------------
def _quiet_hour(mon, now, comp_at):
    for back in range(3600, 0, -300):
        mon.update_lightning(comp_at(now - back), now - back, now - back)


def _comp(now, **over):
    c = {"safe": True, "enabled": True, "available": True, "latched": False,
         "nearest_km": None, "polling_active": True, "last_poll_ts": now - 30,
         "granules_scanned": 22, "granules_in_window": 14, "partial": False}
    c.update(over)
    return c


def test_dew_judges_the_granules_of_the_window_not_the_catch_up(monkeypatch):
    now = 100000.0
    mon = dew.DewMonitor(config)
    _quiet_hour(mon, now, lambda t: _comp(t))
    # 22 granules read, but only 6 of them in the last 5 minutes: not lightning data
    mon.update_lightning(_comp(now, granules_scanned=22, granules_in_window=6), now, now)
    assert "read only 6 of the 10 granules" in mon._glm_why
    # an older component without granules_in_window is judged as before
    mon.update_lightning(_comp(now + 1, granules_in_window=None, granules_scanned=14),
                         now + 1, now + 1)
    assert mon._glm_why is None


def test_dew_does_not_take_a_partial_poll_as_clear_but_takes_its_flash():
    now = 100000.0
    mon = dew.DewMonitor(config)
    _quiet_hour(mon, now, lambda t: _comp(t))
    mon.update_lightning(_comp(now, partial=True, coverage="3 min of the window not read"),
                         now, now)
    assert "left part of its window unread (3 min of the window not read)" in mon._glm_why
    check, _, _ = mon._lightning(now)
    assert check is True                                # the earlier good polls still count
    mon.update_lightning(_comp(now + 60, partial=True, nearest_km=55.0), now + 60, now + 60)
    check, _, _ = mon._lightning(now + 61)
    assert check is False                               # a flash counts, partial or not


# ---- C5: the /dev/shm temp file ----------------------------------------------------------
def test_the_dev_shm_fallback_names_its_file_for_the_startup_sweep(monkeypatch):
    np = _real_numpy()
    monkeypatch.setattr(gl, "np", np)
    made = []

    class Var:
        def __init__(self, v):
            self.v, self.shape = np.asarray(v), np.asarray(v).shape

        def __getitem__(self, i):
            return self.v[i]

    class DS:
        def __init__(self, path, mode="r", memory=None):
            if memory is not None:
                raise ValueError("no in-memory support")
            made.append(path)
            self.variables = {"flash_lat": Var([1.0]), "flash_lon": Var([2.0])}

        def close(self):
            pass
    monkeypatch.setattr(gl, "netCDF4", types.SimpleNamespace(Dataset=DS))
    if not os.path.isdir("/dev/shm"):
        pytest.skip("no /dev/shm here")
    lats, lons = gl._flashes_from_buffer(b"granule")
    assert list(lats) == [1.0] and list(lons) == [2.0]
    assert os.path.basename(made[0]).startswith("ttu-glm-") and made[0].endswith(".nc")
    assert not os.path.exists(made[0])                   # and removed again


# ---- C2 / C4 with the real netCDF4 -------------------------------------------------------
def _granule(tmp_path, n_lat, n_lon, write=True):
    netCDF4 = pytest.importorskip("netCDF4")
    np = _real_numpy()
    path = str(tmp_path / "g.nc")
    ds = netCDF4.Dataset(path, "w")
    ds.createDimension("a", n_lat)
    ds.createDimension("b", n_lon)
    # unwritten chunks take no space: a small file can declare many flashes
    la = ds.createVariable("flash_lat", "f4", ("a",), chunksizes=(min(n_lat, 1024),))
    lo = ds.createVariable("flash_lon", "f4", ("b",), chunksizes=(min(n_lon, 1024),))
    if write:
        la[:] = np.full(n_lat, 33.6, dtype="f4")
        lo[:] = np.full(n_lon, -101.9, dtype="f4")
    ds.close()
    with open(path, "rb") as f:
        return f.read()


def test_real_granules_are_checked_before_they_are_read(tmp_path, monkeypatch):
    buf = _granule(tmp_path, 3, 3)
    monkeypatch.setattr(gl, "np", _real_numpy())
    lats, lons = gl._flashes_from_buffer(buf)
    assert lats.shape == lons.shape == (3,)
    with pytest.raises(ValueError, match="differ in shape"):
        gl._flashes_from_buffer(_granule(tmp_path, 3, 2))
    # a small file that declares more flashes than any real granule holds: refused
    # before a single value is read
    huge = _granule(tmp_path, gl.MAX_FLASHES_PER_GRANULE + 1,
                    gl.MAX_FLASHES_PER_GRANULE + 1, write=False)
    assert len(huge) < 100000
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="declares"):
        gl._flashes_from_buffer(huge)
    assert time.monotonic() - t0 < 5


# ---- C6: the GLM settings ----------------------------------------------------------------
def _config_with(**env):
    code = ("import json; from safety import config as c; print(json.dumps({"
            "'on': c.GLM_ENABLED, 'km': c.GLM_TRIGGER_KM, 'hours': c.GLM_COOLOFF_HOURS, "
            "'interval': c.GLM_POLL_INTERVAL, 'window': c.GLM_WINDOW_MIN, "
            "'stale': c.GLM_STALE_AFTER_SEC, 'warnings': c.CONFIG_WARNINGS}))")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TTU_SAFETY_")}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run([sys.executable, "-c", code], cwd=root, env=dict(clean, **env),
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_glm_settings_are_bounded_loudly():
    plain = _config_with()
    assert (plain["on"], plain["km"], plain["hours"], plain["interval"], plain["window"],
            plain["stale"]) == (True, 50.0, 0.5, 300, 5, 900)
    assert plain["warnings"] == []
    odd = _config_with(TTU_SAFETY_GLM_TRIGGER_KM="0", TTU_SAFETY_GLM_COOLOFF_HOURS="-1",
                       TTU_SAFETY_GLM_WINDOW_MIN="90", TTU_SAFETY_GLM_POLL_INTERVAL="600",
                       TTU_SAFETY_GLM_STALE_SEC="300")
    assert (odd["km"], odd["hours"], odd["window"], odd["stale"]) == (1.0, 0.1, 60, 720)
    for name in ("TTU_SAFETY_GLM_TRIGGER_KM=0.0", "TTU_SAFETY_GLM_COOLOFF_HOURS=-1.0",
                 "TTU_SAFETY_GLM_WINDOW_MIN=90", "TTU_SAFETY_GLM_STALE_SEC=300"):
        assert any(name in w for w in odd["warnings"]), name


def test_the_glm_switch_says_when_it_does_not_understand():
    for word in ("0", "off", "false", "no", "disabled"):
        assert _config_with(TTU_SAFETY_GLM=word)["on"] is False, word
    typo = _config_with(TTU_SAFETY_GLM="of")
    assert typo["on"] is True and any("TTU_SAFETY_GLM='of'" in w for w in typo["warnings"])
