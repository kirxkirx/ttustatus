"""The radar basemap chain: CARTO first (a composite cached on the Pi, then tiles fetched
with TTU_SAFETY_CARTO_KEY), OpenStreetMap as the key-free backup, else a plain
background — and everything that reports it: the component, the attribution, the
warnings, the status page and /setup, the docs' env example. The key is a secret and
must never show up in a log line, the component or a file name.

The network is faked per URL (CARTO answers an unaccepted key with the same watermark
bytes as no key at all, as it does since 2026-09).
"""
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import types

import pytest

from safety import config
from safety import radar as rd
from safety.tests.test_radar import _cfg

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOOD_KEY = "s3cr3t/Key+42"          # needs URL-encoding: '/' and '+'
BAD_KEY = "abc123"
REAL_UA = "ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; kirx@ttu.edu)"
PLACEHOLDER_UA = "ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; you@example.org)"
DARK = (30, 34, 40)                # CARTO Dark Matter-ish
POSITRON = (250, 250, 248)
WATERMARK = (245, 245, 245)
LAND = (242, 239, 233)             # OSM's land colour


def _png(rgb):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), rgb).save(buf, format="PNG")
    return buf.getvalue()


def _legacy_cache_key(cfg, tile_url):
    """safety/radar.py _cache_key as of commit 53f5f0b — the name the Pi's cached CARTO
    composites were saved under. Copied, not imported: the point is that it never changes."""
    s = (f"{cfg.GEOCODE}|{cfg.RADAR_THUMB_HALF_DEG}|{cfg.RADAR_THUMB_PX}|"
         f"{cfg.RADAR_TILE_ZOOM}|{tile_url}")
    return hashlib.md5(s.encode()).hexdigest()[:10]


LEGACY_NIGHT = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
LEGACY_DAY = "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"


@pytest.fixture(autouse=True)
def _fresh_memory():
    """The CARTO verdict memory is per process: start every test without one."""
    rd._CARTO_REJECTED.clear()
    rd._CARTO_ERROR_LOGGED.clear()
    rd._OSM_UA_WARNED.clear()
    yield
    rd._CARTO_REJECTED.clear()
    rd._CARTO_ERROR_LOGGED.clear()
    rd._OSM_UA_WARNED.clear()


class _Net:
    """A fake tile network. carto: 'good' = GOOD_KEY accepted, 'watermark' = every key
    rejected, 'down' = unreachable, 'refuse-unkeyed' = GOOD_KEY accepted and keyless
    requests refused with HTTP 403, 'keyed-403' = every keyed request refused with HTTP
    403; osm: 'ok', 'blocked' (no-cache tile), 'down'. ``fresh`` records, per URL, whether
    the request asked caches for a fresh copy."""

    def __init__(self, monkeypatch, carto="good", osm="ok"):
        self.carto, self.osm, self.calls, self.fresh = carto, osm, [], {}
        monkeypatch.setattr(rd, "_get", self.get)

    def get(self, url, timeout=25, refuse_uncacheable=False, fresh=False):
        self.calls.append(url)
        self.fresh[url] = fresh
        if "cartocdn" in url:
            if self.carto == "down":
                raise OSError("connection refused")
            keyed = "key=" in url
            if self.carto == "keyed-403" and keyed:
                raise rd.urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            if self.carto == "refuse-unkeyed" and not keyed:
                raise rd.urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
            light = "light_all" in url
            if (self.carto in ("good", "refuse-unkeyed")
                    and "key=" + rd.urllib.parse.quote(GOOD_KEY, safe="") in url):
                return _png(POSITRON if light else DARK)
            return _png(WATERMARK)
        if "openstreetmap" in url:
            if self.osm == "down":
                raise OSError("connection refused")
            if self.osm == "blocked" and refuse_uncacheable:
                raise rd.TileRefused("tile server marked the tile not cacheable (no-cache): "
                                     "an 'access blocked' tile?")
            return _png(LAND)
        raise OSError("unexpected host in test: %s" % url)

    def hits(self, host):
        return [u for u in self.calls if host in u]


def _maps(cfg):
    return (rd.Thumbnailer(cfg, cfg.RADAR_TILE_URL, cfg.RADAR_THUMB_PATH, "dark"),
            rd.Thumbnailer(cfg, cfg.RADAR_TILE_URL_DAY, cfg.RADAR_THUMB_PATH_DAY, "light"))


def _seed_legacy(cfg, rgb_night=DARK, rgb_day=POSITRON):
    """Stand-ins for the CARTO composites the Pi cached before the watermark, under the
    names the 53f5f0b code gave them."""
    os.makedirs(cfg.RADAR_CACHE_DIR, exist_ok=True)
    night, day = _maps(cfg)
    for t, url, rgb in ((night, LEGACY_NIGHT, rgb_night), (day, LEGACY_DAY, rgb_day)):
        t._geometry()
        Image.new("RGB", (t._ox, t._oy), rgb).save(
            os.path.join(cfg.RADAR_CACHE_DIR, "basemap_%s.png" % _legacy_cache_key(cfg, url)))


def _poller(cfg, maps):
    p = rd.RadarPoller.__new__(rd.RadarPoller)
    p.cfg, p._thumbs = cfg, list(maps)
    return p


# ---- the cache name: the Pi's old CARTO composites are found again --------------------
def test_legacy_cache_name_is_the_new_carto_lookup(tmp_path):
    for cfg in (_cfg(tmp_path), config):            # the test cfg and the real defaults
        night, day = _maps(cfg)
        assert night.tile_url == LEGACY_NIGHT and day.tile_url == LEGACY_DAY
        for t, url in ((night, LEGACY_NIGHT), (day, LEGACY_DAY)):
            t._geometry()
            want = os.path.join(cfg.RADAR_CACHE_DIR,
                                "basemap_%s.png" % _legacy_cache_key(cfg, url))
            assert t._cache_path("carto-cached") == t._cache_path("carto") == want


# ---- step 1: cached CARTO, no network --------------------------------------------------
def test_cached_carto_composite_is_used_with_zero_network(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY)      # even with a key: the cache wins
    _seed_legacy(cfg)
    net = _Net(monkeypatch)
    night, day = _maps(cfg)
    night._build_basemap()
    day._build_basemap()
    assert net.calls == []
    assert (night.basemap_source, day.basemap_source) == ("carto-cached", "carto-cached")
    assert night._basemap.getpixel((3, 3)) == DARK and day._basemap.getpixel((3, 3)) == POSITRON
    assert not night.basemap_due() and not day.basemap_due()       # final: never rebuilt
    basemap, notes, warns, attr = _poller(cfg, (night, day))._basemap_status()
    assert basemap == {"night": "carto-cached", "day": "carto-cached", "night_inverted": False,
                       "chosen_by": {}}
    assert notes == [] and warns == []
    assert attr == "© OpenStreetMap contributors, © CARTO · Radar: NOAA/NSSL MRMS via IEM"


# ---- step 2: CARTO with the key --------------------------------------------------------
def test_accepted_key_fetches_keyed_tiles_and_caches_under_the_key_free_name(
        tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)
    night, day = _maps(cfg)
    night._build_basemap()
    assert night.basemap_source == "carto" and night.carto_state == "accepted"
    assert night._basemap.getpixel((3, 3)) == DARK
    carto = net.hits("cartocdn")
    enc = rd.urllib.parse.quote(GOOD_KEY, safe="")
    # the probe pair first (the map's middle tile, keyed then unkeyed), then every other
    # tile keyed (URL-encoded) with its unkeyed twin: 2 requests per tile in all, each
    # asking caches on the way for a fresh copy
    ptx, pty = night._tiles[len(night._tiles) // 2]
    probe = LEGACY_NIGHT.format(z=8, x=ptx, y=pty)
    assert carto[:2] == [probe + "?key=" + enc, probe]
    keyed = [u for u in carto if "key=" in u]
    plain = [u for u in carto if "key=" not in u]
    assert sorted(plain) == sorted(LEGACY_NIGHT.format(z=8, x=tx, y=ty)
                                   for tx, ty in night._tiles)
    assert sorted(keyed) == sorted(u + "?key=" + enc for u in plain)
    assert all(net.fresh[u] for u in carto)
    assert net.hits("openstreetmap") == []
    # cached under the SAME key-free legacy name; the key is in no file name
    cached = os.listdir(cfg.RADAR_CACHE_DIR)
    assert cached == ["basemap_%s.png" % _legacy_cache_key(cfg, LEGACY_NIGHT)]
    assert not any(GOOD_KEY in n or enc in n for n in cached)
    # a restart finds it: step 1, no network at all
    net.calls.clear()
    again, _ = _maps(cfg)
    again._build_basemap()
    assert again.basemap_source == "carto-cached" and net.calls == []
    day._build_basemap()
    basemap, notes, warns, attr = _poller(cfg, (again, day))._basemap_status()
    assert basemap["night"] == "carto-cached" and basemap["day"] == "carto"
    assert warns == [] and attr.startswith("© OpenStreetMap contributors, © CARTO · ")


def test_rejected_key_falls_back_to_osm_with_a_warning_and_is_remembered(
        tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path, CARTO_API_KEY=BAD_KEY)
    net = _Net(monkeypatch, carto="watermark")
    night, day = _maps(cfg)
    with caplog.at_level(logging.INFO, logger="ttu.safety.radar"):
        night._build_basemap()
        day._build_basemap()
    # exactly one probe pair (the night map's); the day map trusted the memory
    assert len(net.hits("cartocdn")) == 2
    assert night.carto_state == day.carto_state == "rejected"
    assert (night.basemap_source, day.basemap_source) == ("osm", "osm")
    assert night.basemap_inverted and not day.basemap_inverted
    assert rd.carto_rejection(BAD_KEY) == "rejected"
    # nothing CARTO was cached, the OSM composites were
    assert "basemap_%s.png" % _legacy_cache_key(cfg, LEGACY_NIGHT) \
        not in os.listdir(cfg.RADAR_CACHE_DIR)
    assert len(os.listdir(cfg.RADAR_CACHE_DIR)) == 2
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "did not accept TTU_SAFETY_CARTO_KEY" in errors[0].getMessage()
    assert "key=***" in errors[0].getMessage()
    assert BAD_KEY not in caplog.text
    # OSM stands in for CARTO: re-checked after the rejection memory, not every poll
    assert night._basemap_retry_at is not None and not night.basemap_due()
    basemap, notes, warns, attr = _poller(cfg, (night, day))._basemap_status()
    assert ("TTU_SAFETY_CARTO_KEY was not accepted by CARTO (tiles still watermarked); "
            "the map uses OSM") in warns
    assert attr == "© OpenStreetMap contributors · Radar: NOAA/NSSL MRMS via IEM"
    blob = json.dumps([basemap, notes, warns, attr])
    assert BAD_KEY not in blob


def test_rejection_memory_expires_and_the_key_is_probed_again(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY)
    net = _Net(monkeypatch, carto="watermark")
    clock = {"t": 1000.0}
    monkeypatch.setattr(rd.time, "monotonic", lambda: clock["t"])
    night, _ = _maps(cfg)
    night._build_basemap()
    assert night.basemap_source == "osm" and len(net.hits("cartocdn")) == 2
    clock["t"] += rd.CARTO_REJECT_MEMORY_SEC - 1
    assert not night.basemap_due() and rd.carto_rejection(GOOD_KEY) == "rejected"
    clock["t"] += 2
    assert night.basemap_due() and rd.carto_rejection(GOOD_KEY) is None
    net.carto = "good"                    # e.g. the key was activated in the meantime
    p = _poller(cfg, (night,))
    p._retry_basemaps()
    assert night.basemap_source == "carto" and not night.basemap_due()


def test_unreachable_probe_counts_as_not_accepted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY)
    _Net(monkeypatch, carto="down")
    night, day = _maps(cfg)
    night._build_basemap()
    day._build_basemap()
    assert night.carto_state == "unreachable" and night.basemap_source == "osm"
    _, notes, warns, _ = _poller(cfg, (night, day))._basemap_status()
    assert any(w.startswith("TTU_SAFETY_CARTO_KEY could not be checked") for w in warns)


def test_no_key_never_requests_carto_not_even_the_probe(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)                        # CARTO_API_KEY = ""
    assert cfg.CARTO_API_KEY == ""
    net = _Net(monkeypatch)
    night, day = _maps(cfg)
    night._build_basemap()
    day._build_basemap()
    assert net.hits("cartocdn") == []
    assert night.carto_state == "no-key" and night.basemap_source == "osm"
    basemap, notes, warns, attr = _poller(cfg, (night, day))._basemap_status()
    assert basemap == {"night": "osm", "day": "osm", "night_inverted": True, "chosen_by": {}}
    assert "for CARTO maps set TTU_SAFETY_CARTO_KEY (free key: carto.com/basemaps/apikey)" in notes
    assert attr == "© OpenStreetMap contributors · Radar: NOAA/NSSL MRMS via IEM"
    # the default UA names the app but carries no contact: warned (not a veto)
    assert any(w.startswith("Set TTU_SAFETY_NWS_UA with a real contact e-mail:")
               and "(now: 'ttu-safety-monitor')" in w for w in warns)


# ---- RADAR_BASEMAP modes ------------------------------------------------------------------
def test_mode_carto_never_touches_osm(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, RADAR_BASEMAP="carto")
    net = _Net(monkeypatch)
    night, day = _maps(cfg)
    night._build_basemap()                          # no key, no cache: nothing
    assert night.basemap_source == "none" and night.basemap_due() is False
    cfg.CARTO_API_KEY = BAD_KEY
    net.carto = "watermark"
    day._build_basemap()                            # a rejected key: still no OSM
    assert day.basemap_source == "none" and day.carto_state == "rejected"
    assert net.hits("openstreetmap") == [] and night.osm_state is None
    _, notes, warns, attr = _poller(cfg, (night, day))._basemap_status()
    assert attr == "Radar: NOAA/NSSL MRMS via IEM"
    assert "the map uses nothing (plain background)" in " ".join(warns)
    assert not any("TTU_SAFETY_NWS_UA" in w for w in warns)   # OSM never in play


def test_mode_osm_never_uses_carto_not_even_cached(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, RADAR_BASEMAP="osm", CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    _seed_legacy(cfg)
    net = _Net(monkeypatch)
    night, day = _maps(cfg)
    assert night.basemap_chain() == ["osm"] == day.basemap_chain()
    night._build_basemap()
    day._build_basemap()
    assert (night.basemap_source, day.basemap_source) == ("osm", "osm")
    assert net.hits("cartocdn") == [] and len(net.hits("openstreetmap")) == 2 * len(night._tiles)
    basemap, notes, warns, attr = _poller(cfg, (night, day))._basemap_status()
    assert notes == [] and warns == []          # a real e-mail: nothing to warn about
    assert attr == "© OpenStreetMap contributors · Radar: NOAA/NSSL MRMS via IEM"
    # OpenStreetMap is the configured choice here, not "the key-free fallback"
    assert basemap["chosen_by"] == {"night": "TTU_SAFETY_RADAR_BASEMAP=osm",
                                    "day": "TTU_SAFETY_RADAR_BASEMAP=osm"}
    assert rd.basemap_summary(basemap, notes) == (
        "Basemap: OpenStreetMap standard tiles (night map colour-inverted), as set by "
        "TTU_SAFETY_RADAR_BASEMAP=osm.")


def test_auto_chain_order_and_custom_source(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    night, day = _maps(cfg)
    assert night.basemap_chain() == ["carto-cached", "carto", "osm"]
    custom = rd.Thumbnailer(cfg, "https://tiles.example.net/{z}/{x}/{y}.png?token=T0K",
                            cfg.RADAR_THUMB_PATH, "dark")
    assert custom.basemap_chain() == ["custom-cached", "custom", "osm"]
    cfg.RADAR_BASEMAP = "carto"
    assert custom.basemap_chain() == ["custom-cached", "custom"]
    assert rd.basemap_attribution(cfg, [("custom", custom.tile_url)]) \
        == "Map tiles: tiles.example.net · Radar: NOAA/NSSL MRMS via IEM"
    assert "T0K" not in rd.redact("GET " + custom.tile_url)


# ---- OpenStreetMap and the User-Agent ------------------------------------------------------
def test_placeholder_user_agent_skips_osm_and_warns(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path, NWS_USER_AGENT=PLACEHOLDER_UA)
    net = _Net(monkeypatch)
    night, day = _maps(cfg)
    with caplog.at_level(logging.WARNING, logger="ttu.safety.radar"):
        night._build_basemap()
        day._build_basemap()
    assert net.hits("openstreetmap") == [] and net.calls == []
    assert night.osm_state == "skipped-ua" and night.basemap_source == "none"
    assert caplog.text.count("OpenStreetMap basemap NOT requested") == 1     # once
    basemap, notes, warns, _ = _poller(cfg, (night, day))._basemap_status()
    assert basemap["night"] == basemap["day"] == "none"
    assert any("OpenStreetMap not requested" in n for n in notes)
    w = [x for x in warns if x.startswith("Set TTU_SAFETY_NWS_UA with a real contact e-mail:")]
    assert len(w) == 1 and "placeholder" in w[0] and "you@example.org" in w[0]
    assert w[0].endswith("(now: '%s')" % PLACEHOLDER_UA)


def test_blocked_osm_tiles_leave_a_plain_map_and_retry(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch, osm="blocked")
    night, _ = _maps(cfg)
    night._build_basemap()
    assert night.basemap_source == "none" and night.osm_state == "refused"
    assert len(net.hits("openstreetmap")) == rd.BASEMAP_GIVE_UP_AFTER   # gave up early
    assert not os.path.exists(cfg.RADAR_CACHE_DIR)                      # nothing cached
    assert night._basemap_retry_at is not None
    # the page says why (the UA has an address, so no warning; the note names the cause)
    notes, warns = rd.basemap_messages(cfg, [night])
    assert "OpenStreetMap refused the tiles ('access blocked' replies): check " \
        "TTU_SAFETY_NWS_UA" in notes and warns == []
    assert rd.basemap_summary({"night": "none"}, notes).startswith(
        "Basemap: none (plain background); ")


def test_user_agent_helpers():
    assert config.ua_has_placeholder(PLACEHOLDER_UA)
    for ua in ("x (CONTACT_EMAIL)", "x you@ttu.edu", "x a@EXAMPLE.com", "x b@example.net",
               "x (+https://github.com/kirxkirx/ttustatus; your.name@ttu.edu)",
               "x (you@ttu.edu)", "x <your.email@ttu.edu>", "x yourname@gmail.com"):
        assert config.ua_has_placeholder(ua), ua
    # a real address whose local part merely ends in "you" is not a placeholder
    for addr in ("zhouyou@ttu.edu", "youyou@gmail.com", "bayou@ttu.edu", "kirx@ttu.edu"):
        ua = "ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; %s)" % addr
        assert not config.ua_has_placeholder(ua) and config.ua_has_real_email(ua), addr
    assert not config.ua_has_placeholder(REAL_UA)
    assert config.ua_has_real_email(REAL_UA)
    assert not config.ua_has_real_email("ttu-safety-monitor")
    assert not config.ua_has_real_email("ttu (@kirx)")
    assert not config.ua_has_real_email(PLACEHOLDER_UA)


def test_placeholder_ua_and_bad_basemap_mode_warn_at_startup():
    env = dict(os.environ, TTU_SAFETY_NWS_UA=PLACEHOLDER_UA, TTU_SAFETY_RADAR_BASEMAP="cartoo",
               TTU_SAFETY_CARTO_KEY=" " + GOOD_KEY + " ")
    out = subprocess.run(
        [sys.executable, "-c", "from safety import config as c; "
         "print(c.RADAR_BASEMAP, c.CARTO_API_KEY == %r); print('|'.join(c.CONFIG_WARNINGS))"
         % GOOD_KEY],
        cwd=REPO, env=env, capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == "auto True"
    assert "TTU_SAFETY_NWS_UA=" in out[1] and "placeholder" in out[1]
    assert "TTU_SAFETY_RADAR_BASEMAP='cartoo'" in out[1] and "using auto" in out[1]
    assert GOOD_KEY not in out[1]


# ---- the key never leaks ------------------------------------------------------------------
def test_redact_hides_the_key_raw_and_url_encoded():
    cfg = types.SimpleNamespace(CARTO_API_KEY=GOOD_KEY)
    enc = rd.urllib.parse.quote(GOOD_KEY, safe="")
    url = rd._with_key(LEGACY_NIGHT.format(z=8, x=54, y=102), GOOD_KEY)
    assert url.endswith("?key=" + enc)
    for text in (url, "error for %s: %s" % (GOOD_KEY, url), "x?apikey=zzz&y=1"):
        out = rd.redact(text, cfg)
        assert GOOD_KEY not in out and enc not in out and "zzz" not in out
    assert rd.redact(url, cfg).endswith("?key=***")


def test_the_key_never_reaches_logs_state_or_component(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY)
    net = _Net(monkeypatch, carto="watermark")
    night, day = _maps(cfg)
    with caplog.at_level(logging.DEBUG):
        night._build_basemap()
        net.carto = "down"
        rd._CARTO_REJECTED.clear()
        day._build_basemap()                     # the fetch-error path logs the URL too
        p = rd.RadarPoller.__new__(rd.RadarPoller)
        rd.RadarPoller.__init__(p, cfg, types.SimpleNamespace(record=lambda *a, **k: {}))
        p._thumbs = [night, day]
        comp = p.component(-10.0, 1000.0)
    enc = rd.urllib.parse.quote(GOOD_KEY, safe="")
    assert "key=***" in caplog.text
    assert GOOD_KEY not in caplog.text and enc not in caplog.text
    blob = json.dumps(comp, ensure_ascii=False)
    assert GOOD_KEY not in blob and enc not in blob
    assert comp["basemap"]["night"] == "osm" and comp["basemap_warnings"]
    assert not any(GOOD_KEY in n or enc in n for n in os.listdir(cfg.RADAR_CACHE_DIR))


# ---- attribution per source ------------------------------------------------------------
@pytest.mark.parametrize("used, credit", [
    ([("carto-cached", LEGACY_NIGHT), ("carto-cached", LEGACY_DAY)],
     "© OpenStreetMap contributors, © CARTO"),
    ([("carto", LEGACY_NIGHT), ("osm", config.OSM_TILE_URL)],
     "© OpenStreetMap contributors, © CARTO"),
    ([("osm", config.OSM_TILE_URL), ("osm", config.OSM_TILE_URL)],
     "© OpenStreetMap contributors"),
    ([("custom", "https://b.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png")],
     "© OpenStreetMap contributors, © CARTO"),
    ([("none", LEGACY_NIGHT)], None),
    ([], None),
])
def test_attribution_follows_the_sources_drawn(used, credit):
    radar = " · Radar: NOAA/NSSL MRMS via IEM"
    got = rd.basemap_attribution(config, used)
    assert got == (credit + radar if credit else radar[3:])


def test_unavailable_component_carries_the_basemap_keys():
    c = rd.unavailable_component(config)
    assert c["basemap"] == {"night": None, "day": None, "night_inverted": False,
                            "chosen_by": {}}
    assert c["basemap_notes"] == [] and c["basemap_warnings"] == []
    assert c["attribution"] == "Radar: NOAA/NSSL MRMS via IEM"


# ---- retries ------------------------------------------------------------------------------
def test_a_missing_basemap_is_rebuilt_after_the_retry_interval(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch, osm="down")
    clock = {"t": 50.0}
    monkeypatch.setattr(rd.time, "monotonic", lambda: clock["t"])
    night, day = _maps(cfg)
    night._build_basemap()
    assert night.basemap_source == "none"
    p = _poller(cfg, (night, day))
    net.osm = "ok"
    p._retry_basemaps()
    assert night.basemap_source == "none"               # not yet due
    clock["t"] += rd.BASEMAP_RETRY_SEC
    p._retry_basemaps()
    assert night.basemap_source == "osm" and not night.basemap_due()
    assert day.basemap_source is None                    # never built: render() builds it


# ---- the monitor: warnings, never a veto ---------------------------------------------------
def test_basemap_warnings_reach_the_state_but_never_veto(env, write_inputs):
    from safety.monitor import SafetyMonitor
    from safety.tests.test_monitor import _StubRadar, _radar_comp
    write_inputs(env["cfg"], sun=-10.0, humidity=40.0)
    comp = dict(_radar_comp(True), basemap={"night": "osm", "day": "osm"},
                basemap_notes=[], basemap_warnings=["Set TTU_SAFETY_NWS_UA with a real "
                                                    "contact e-mail: ... (now: 'x')", 7])
    m = SafetyMonitor(env["cfg"], env["log"], env["poller"], radar=_StubRadar(comp))
    st = m.evaluate()
    assert st["is_safe"] is True and st["reasons"] == []
    assert st["warnings"] == ["Set TTU_SAFETY_NWS_UA with a real contact e-mail: ... "
                              "(now: 'x')"]


# ---- what the page and /setup say ----------------------------------------------------------
def _msp():
    from safety.tests.test_smoke import msp
    return msp


def _page(rad_extra):
    msp = _msp()
    import time
    thumb = os.path.join(os.path.dirname(msp.HTML_FILE), "ttu_radar.png")
    rad = {"trigger_km": 30, "enabled": True, "available": True, "thumb_available": True,
           "thumb_path": thumb, "frame_utc": "2026-09-24", "in_ring": False}
    rad.update(rad_extra)
    return msp.build_radar_html({"ts": time.time(), "components": {"radar": rad}})


def test_page_names_the_basemap_in_use():
    h = _page({"basemap": {"night": "carto-cached", "day": "carto-cached"},
               "basemap_notes": [],
               "attribution": "© OpenStreetMap contributors, © CARTO · Radar: NOAA/NSSL "
                              "MRMS via IEM"})
    assert ("Basemap: CARTO Dark Matter (night) / Positron (day), from tiles cached on "
            "this Pi.") in h
    assert "© OpenStreetMap contributors, © CARTO · Radar" in h
    h = _page({"basemap": {"night": "carto", "day": "carto"}})
    assert "fetched with the configured CARTO API key" in h
    h = _page({"basemap": {"night": "osm", "day": "osm", "night_inverted": True},
               "basemap_notes": ["for CARTO maps set TTU_SAFETY_CARTO_KEY (free key: "
                                 "carto.com/basemaps/apikey)"],
               "attribution": "© OpenStreetMap contributors · Radar: NOAA/NSSL MRMS via IEM"})
    assert ("Basemap: OpenStreetMap standard tiles (night map colour-inverted), the key-free "
            "fallback; for CARTO maps set TTU_SAFETY_CARTO_KEY (free key: "
            "carto.com/basemaps/apikey).") in h
    assert "CARTO ·" not in h and "© CARTO" not in h
    h = _page({"basemap": {"night": "carto-cached", "day": "osm"}})
    assert ("Basemap: night map: CARTO Dark Matter, from tiles cached on this Pi; day map: "
            "OpenStreetMap standard tiles, the key-free fallback.") in h
    h = _page({"basemap": {"night": "none", "day": "none"}})
    assert "Basemap: none (plain background)." in h
    h = _page({"basemap": {"night": "osm", "day": "osm", "night_inverted": True,
                           "chosen_by": {"night": "TTU_SAFETY_RADAR_BASEMAP=osm",
                                         "day": "TTU_SAFETY_RADAR_BASEMAP=osm"}}})
    assert ("Basemap: OpenStreetMap standard tiles (night map colour-inverted), as set by "
            "TTU_SAFETY_RADAR_BASEMAP=osm.") in h and "fallback" not in h


def test_page_from_an_older_daemon_keeps_todays_text():
    h = _page({"attribution": "© OpenStreetMap contributors, © CARTO"})
    assert "Basemap:" not in h and "MRMS" in h and "© CARTO" in h


def test_page_and_setup_summaries_are_identical():
    msp = _msp()
    srcs = [None] + list(rd.BASEMAP_SOURCES) + ["something-new"]
    chosen = [None, {}, {"night": "TTU_SAFETY_RADAR_BASEMAP=osm",
                         "day": "TTU_SAFETY_RADAR_BASEMAP=osm"},
              {"night": "TTU_SAFETY_RADAR_TILE_URL"}, {"day": 7}, "junk"]
    for night in srcs:
        for day in srcs:
            for inv in (False, True):
                for notes in ([], ["a note"], None):
                    for by in chosen:
                        bm = {"night": night, "day": day, "night_inverted": inv,
                              "chosen_by": by}
                        assert rd.basemap_summary(bm, notes) == \
                            msp.radar_basemap_summary(bm, notes), (night, day, inv, notes, by)
    assert rd.basemap_summary(None) == msp.radar_basemap_summary(None) == ""


def test_setup_page_shows_the_same_basemap_line():
    from safety.alpaca import _setup_html
    from safety.tests.test_smoke import _safe_state
    st = _safe_state()
    st["components"]["radar"] = {
        "safe": True, "enabled": True, "available": True, "trigger_km": 30,
        "basemap": {"night": "osm", "day": "osm", "night_inverted": True},
        "basemap_notes": ["for CARTO maps set TTU_SAFETY_CARTO_KEY (free key: "
                          "carto.com/basemaps/apikey)"],
        "attribution": "© OpenStreetMap contributors · Radar: NOAA/NSSL MRMS via IEM"}
    st["warnings"] = ["Set TTU_SAFETY_NWS_UA with a real contact e-mail: x (now: 'y')"]
    mon = types.SimpleNamespace(evaluate=lambda: st)
    h = _setup_html(mon, config)
    line = rd.basemap_summary(st["components"]["radar"]["basemap"],
                              st["components"]["radar"]["basemap_notes"])
    assert line.startswith("Basemap: OpenStreetMap standard tiles (night map colour-inverted)")
    assert line in h and "© OpenStreetMap contributors · Radar" in h
    assert "Set TTU_SAFETY_NWS_UA with a real contact e-mail" in h


# ---- the docs match the code ----------------------------------------------------------
def test_env_example_lists_the_basemap_variables():
    text = open(os.path.join(REPO, "ttustatus.env.example"), encoding="utf-8").read()
    for var in ("TTU_SAFETY_NWS_UA", "TTU_SAFETY_CARTO_KEY", "TTU_SAFETY_RADAR_BASEMAP"):
        assert re.search(r"^#\s*%s=" % var, text, re.M), var
    assert "carto.com/basemaps/apikey" in text
    # the example UA is a placeholder on purpose (it must be replaced) and is quoted, so
    # the bash launcher can source the file
    ua_line = re.search(r"^#\s*TTU_SAFETY_NWS_UA=(.*)$", text, re.M).group(1)
    assert config.ua_has_placeholder(ua_line) and ua_line.startswith('"')
    assert "CRUCIAL" in text


def test_readmes_describe_the_chain_and_drop_stale_claims():
    safety_md = open(os.path.join(REPO, "README_SAFETY.md"), encoding="utf-8").read()
    readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
    for var in ("TTU_SAFETY_CARTO_KEY", "TTU_SAFETY_RADAR_BASEMAP", "TTU_SAFETY_NWS_UA",
                "TTU_SAFETY_RADAR_TILE_URL"):
        assert "`%s`" % var in safety_md, var
    assert "rm ~/.cache/ttu-radar/basemap_*.png" in readme
    assert "TTU_SAFETY_NWS_UA" in readme and "TTU_SAFETY_CARTO_KEY" in readme
    for stale in ("no longer used", "former default", "no longer depends on CARTO"):
        assert stale not in safety_md and stale not in readme, stale


# ---- every CARTO URL follows the CARTO rules (review fixes) ------------------------------
CUSTOM_CARTO = "https://b.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}.png"


def test_a_custom_carto_url_is_never_fetched_without_the_key(tmp_path, monkeypatch):
    # another subdomain and style: still CARTO — no key, no CARTO request at all (the
    # watermark would otherwise be cached for good), OpenStreetMap stands in
    cfg = _cfg(tmp_path, RADAR_TILE_URL=CUSTOM_CARTO, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)
    night, _ = _maps(cfg)
    assert night.basemap_chain() == ["custom-cached", "custom", "osm"]
    night._build_basemap()
    assert net.hits("cartocdn") == []
    assert night.carto_state == "no-key" and night.basemap_source == "osm"
    assert not os.path.exists(night._cache_path("custom"))
    notes, _ = rd.basemap_messages(cfg, [night])
    assert "for CARTO maps set TTU_SAFETY_CARTO_KEY (free key: carto.com/basemaps/apikey)" \
        in notes


def test_a_custom_carto_url_is_probed_keyed_and_cached_under_the_legacy_name(
        tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, RADAR_TILE_URL=CUSTOM_CARTO, CARTO_API_KEY=GOOD_KEY,
               NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)
    night, _ = _maps(cfg)
    night._build_basemap()
    assert night.basemap_source == "custom" and night.carto_state == "accepted"
    assert not night.basemap_inverted and night._basemap.getpixel((3, 3)) == DARK
    carto = net.hits("cartocdn")
    assert len([u for u in carto if "key=" in u]) == len(night._tiles)   # each tile keyed
    assert len(carto) == 2 * len(night._tiles)                           # ... and its twin
    # the 53f5f0b name of that URL: no "|inverted" (CARTO tiles are never inverted)
    assert os.listdir(cfg.RADAR_CACHE_DIR) == [
        "basemap_%s.png" % _legacy_cache_key(cfg, CUSTOM_CARTO)]
    assert rd.basemap_attribution(cfg, [("custom", night.tile_url)]).startswith(
        "© OpenStreetMap contributors, © CARTO · ")
    # a watermark from a rejected key is never cached under it either
    rd._CARTO_REJECTED.clear()
    cfg2 = _cfg(tmp_path / "w", RADAR_TILE_URL=CUSTOM_CARTO, CARTO_API_KEY=BAD_KEY,
                NWS_USER_AGENT=REAL_UA)
    net.carto = "watermark"
    w, _ = _maps(cfg2)
    w._build_basemap()
    assert w.carto_state == "rejected" and w.basemap_source == "osm"
    assert not os.path.exists(w._cache_path("custom"))


def test_a_53f5f0b_composite_of_a_custom_carto_night_url_is_found(tmp_path, monkeypatch):
    # 53f5f0b named every composite _cache_key(cfg, url) — never "|inverted"
    cfg = _cfg(tmp_path, RADAR_TILE_URL=CUSTOM_CARTO, NWS_USER_AGENT=REAL_UA)
    assert cfg.RADAR_TILE_DARK_INVERT is True
    night, _ = _maps(cfg)
    night._geometry()
    os.makedirs(cfg.RADAR_CACHE_DIR)
    Image.new("RGB", (night._ox, night._oy), DARK).save(os.path.join(
        cfg.RADAR_CACHE_DIR, "basemap_%s.png" % _legacy_cache_key(cfg, CUSTOM_CARTO)))
    net = _Net(monkeypatch)
    night._build_basemap()
    assert night.basemap_source == "custom-cached" and net.calls == []
    assert night._basemap.getpixel((3, 3)) == DARK


def test_a_carto_key_written_into_the_url_is_used_once_and_kept_out_of_names(
        tmp_path, monkeypatch, caplog):
    enc = rd.urllib.parse.quote(GOOD_KEY, safe="")
    cfg = _cfg(tmp_path, RADAR_TILE_URL=LEGACY_NIGHT + "?key=" + enc, NWS_USER_AGENT=REAL_UA)
    assert cfg.CARTO_API_KEY == ""
    net = _Net(monkeypatch)
    night, _ = _maps(cfg)
    assert night.basemap_chain() == ["carto-cached", "carto", "osm"]   # it IS the default
    with caplog.at_level(logging.DEBUG):
        night._build_basemap()
    assert night.basemap_source == "carto"
    carto = net.hits("cartocdn")
    assert all(u.count("key=") <= 1 for u in carto)
    assert sorted(u for u in carto if "key=" not in u) == sorted(
        LEGACY_NIGHT.format(z=8, x=tx, y=ty) for tx, ty in night._tiles)
    assert os.listdir(cfg.RADAR_CACHE_DIR) == [
        "basemap_%s.png" % _legacy_cache_key(cfg, LEGACY_NIGHT)]
    assert GOOD_KEY not in caplog.text and enc not in caplog.text


def test_config_moves_a_carto_key_out_of_the_tile_url():
    env = dict(os.environ, TTU_SAFETY_RADAR_TILE_URL=LEGACY_NIGHT + "?key=Zq9secret",
               TTU_SAFETY_RADAR_TILE_URL_DAY="https://c.basemaps.cartocdn.com/light_all/"
                                             "{z}/{x}/{y}.png?apikey=other&lang=en")
    env.pop("TTU_SAFETY_CARTO_KEY", None)
    out = subprocess.run(
        [sys.executable, "-c", "from safety import config as c; print(c.RADAR_TILE_URL); "
         "print(c.RADAR_TILE_URL_DAY); print(c.CARTO_API_KEY == 'Zq9secret'); "
         "print('|'.join(c.CONFIG_WARNINGS))"],
        cwd=REPO, env=env, capture_output=True, text=True, check=True).stdout.splitlines()
    assert out[0] == LEGACY_NIGHT
    assert out[1] == "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png?lang=en"
    assert out[2] == "True"
    assert "TTU_SAFETY_RADAR_TILE_URL has an API key in it" in out[3]
    assert "Zq9secret" not in out[3] and "other" not in out[3]


# ---- the key check follows the KEYED reply ------------------------------------------------
def test_carto_refusing_keyless_requests_proves_a_good_key(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    _Net(monkeypatch, carto="refuse-unkeyed")
    night, day = _maps(cfg)
    night._build_basemap()
    day._build_basemap()
    assert (night.basemap_source, day.basemap_source) == ("carto", "carto")
    assert night.carto_state == day.carto_state == "accepted"
    _, notes, warns, _ = _poller(cfg, (night, day))._basemap_status()
    assert warns == [] and notes == []


@pytest.mark.parametrize("code", [429, 503])
def test_an_unkeyed_hiccup_is_no_verdict_on_the_key(tmp_path, monkeypatch, code):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)
    real = net.get

    def get(url, timeout=25, refuse_uncacheable=False, fresh=False):
        if "cartocdn" in url and "key=" not in url:
            raise rd.urllib.error.HTTPError(url, code, "busy", {}, None)
        return real(url, timeout, refuse_uncacheable, fresh)
    monkeypatch.setattr(rd, "_get", get)
    night, _ = _maps(cfg)
    night._build_basemap()
    # a rate limit / outage proves nothing: "could not be checked", never "rejected"
    assert night.carto_state == "unreachable" and night.basemap_source == "osm"
    _, warns = rd.basemap_messages(cfg, [night])
    assert ("TTU_SAFETY_CARTO_KEY could not be checked (the CARTO probe tile could not be "
            "fetched: HTTP %d); the map uses OSM" % code) in warns


def test_a_keyed_request_carto_refuses_is_a_rejection_named_by_its_status(
        tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, CARTO_API_KEY=BAD_KEY, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch, carto="keyed-403")
    night, day = _maps(cfg)
    night._build_basemap()
    day._build_basemap()
    assert night.carto_state == day.carto_state == "rejected"
    assert night.carto_reason == day.carto_reason == "HTTP 403"
    assert len(net.hits("cartocdn")) == 1          # refused keyed: no twin needed
    _, notes, warns, _ = _poller(cfg, (night, day))._basemap_status()
    assert ("TTU_SAFETY_CARTO_KEY was not accepted by CARTO (HTTP 403); the map uses OSM"
            in warns)


def test_carto_requests_ask_for_fresh_copies_and_keep_uncacheable_keyed_tiles(
        tmp_path, monkeypatch):
    """Through the real _get: every CARTO request carries Cache-Control/Pragma no-cache,
    and a keyed tile marked private/no-store is still used (the twin comparison, not
    the headers, judges CARTO tiles)."""
    import urllib.request

    class _Resp:
        def __init__(self, body, cc):
            self._b, self.headers = io.BytesIO(body), {"Cache-Control": cc}

        def read(self, n=-1):
            return self._b.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    seen = []

    def urlopen(req, timeout=25):
        seen.append((req.full_url, req.get_header("Cache-control"), req.get_header("Pragma")))
        if "key=" in req.full_url:
            return _Resp(_png(DARK), "private, no-store")
        return _Resp(_png(WATERMARK), "public,max-age=15552000")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    night, _ = _maps(cfg)
    night._build_basemap()
    assert night.basemap_source == "carto" and night._basemap.getpixel((3, 3)) == DARK
    assert seen and all(cc == "no-cache" and pragma == "no-cache" for _, cc, pragma in seen)


def test_a_watermarked_tile_after_the_probe_is_never_drawn_or_cached(tmp_path, monkeypatch):
    # the probe tile is real, every other keyed tile the watermark (key revoked or quota
    # spent mid-build): the build is incomplete, nothing CARTO is cached, retried in 30 min
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)
    clock = {"t": 100.0}
    monkeypatch.setattr(rd.time, "monotonic", lambda: clock["t"])
    night, _ = _maps(cfg)
    night._geometry()
    ptx, pty = night._tiles[len(night._tiles) // 2]
    probe = "/8/%d/%d.png" % (ptx, pty)
    real = net.get

    def get(url, timeout=25, refuse_uncacheable=False, fresh=False):
        if "cartocdn" in url and "key=" in url and probe not in url:
            net.calls.append(url)
            return _png(WATERMARK)
        return real(url, timeout, refuse_uncacheable, fresh)
    monkeypatch.setattr(rd, "_get", get)
    night._build_basemap()
    assert night.carto_state == "accepted" and night.basemap_source == "osm"
    assert not os.path.exists(night._cache_path("carto"))
    assert night._basemap_retry_at == clock["t"] + rd.BASEMAP_RETRY_SEC
    notes, _ = rd.basemap_messages(cfg, [night])
    assert "night map: CARTO tiles incomplete (1/%d), retried every 30 min" \
        % len(night._tiles) in notes


def test_one_failed_keyed_tile_is_retried_in_30_min_not_6_h(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)
    clock = {"t": 100.0}
    monkeypatch.setattr(rd.time, "monotonic", lambda: clock["t"])
    night, _ = _maps(cfg)
    night._geometry()
    first = "/8/%d/%d.png?key=" % night._tiles[0]
    real, failed = net.get, []

    def get(url, timeout=25, refuse_uncacheable=False, fresh=False):
        if first in url and not failed:
            failed.append(url)
            raise OSError("timed out")
        return real(url, timeout, refuse_uncacheable, fresh)
    monkeypatch.setattr(rd, "_get", get)
    night._build_basemap()
    assert failed and night.basemap_source == "osm" and night.carto_state == "accepted"
    assert night._basemap_retry_at == clock["t"] + rd.BASEMAP_RETRY_SEC
    notes, _ = rd.basemap_messages(cfg, [night])
    assert any(n.startswith("night map: CARTO tiles incomplete") for n in notes)
    clock["t"] += rd.BASEMAP_RETRY_SEC
    _poller(cfg, [night])._retry_basemaps()
    assert night.basemap_source == "carto" and not night.basemap_due()


# ---- reading the basemap state never breaks IsSafe ----------------------------------------
def test_basemap_messages_survive_a_concurrent_rebuild(tmp_path):
    import threading
    import time
    cfg = _cfg(tmp_path, NWS_USER_AGENT=REAL_UA)
    night, day = _maps(cfg)
    for t in (night, day):
        t.basemap_source, t.carto_state = "carto", "accepted"
    stop = threading.Event()

    def writer():                       # what _use does on the radar thread
        while not stop.is_set():
            night.basemap_tiles = (8, 9)
            night.basemap_tiles = None
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    try:
        deadline = time.time() + 1.5
        while time.time() < deadline:
            rd.basemap_messages(cfg, [night, day])      # must never raise
    finally:
        stop.set()
        th.join()
        sys.setswitchinterval(old)


def test_a_basemap_status_error_never_reaches_the_verdict(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path)
    p = rd.RadarPoller(cfg, types.SimpleNamespace(record=lambda *a, **k: None))

    def boom(*a, **k):
        raise TypeError("'NoneType' object is not iterable")
    monkeypatch.setattr(rd, "basemap_messages", boom)
    with caplog.at_level(logging.WARNING, logger="ttu.safety.radar"):
        c1 = p.component(None, 1000.0)
        c2 = p.component(None, 1001.0)
    assert c1["safe"] is True and c2["basemap"]["night"] is None
    assert c1["basemap_notes"] == [] and c1["basemap_warnings"] == []
    assert c1["attribution"] == "Radar: NOAA/NSSL MRMS via IEM"
    assert caplog.text.count("radar basemap status unavailable") == 1      # logged once


# ---- OpenStreetMap URLs follow the OSM rules; carto mode never uses OSM -------------------
def test_carto_mode_ignores_an_openstreetmap_tile_url(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, RADAR_BASEMAP="carto", RADAR_TILE_URL=config.OSM_TILE_URL,
               CARTO_API_KEY=GOOD_KEY, NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)
    night, _ = _maps(cfg)
    assert night.basemap_chain() == ["carto-cached", "carto"]
    night._build_basemap()
    assert night.basemap_source == "carto" and net.hits("openstreetmap") == []
    env = dict(os.environ, TTU_SAFETY_RADAR_BASEMAP="carto",
               TTU_SAFETY_RADAR_TILE_URL="https://a.tile.openstreetmap.org/{z}/{x}/{y}.png")
    out = subprocess.run([sys.executable, "-c", "from safety import config as c; "
                          "print('|'.join(c.CONFIG_WARNINGS))"],
                         cwd=REPO, env=env, capture_output=True, text=True, check=True).stdout
    assert "TTU_SAFETY_RADAR_BASEMAP=carto: the OpenStreetMap" in out


@pytest.mark.parametrize("mode", ["auto", "carto", "osm"])
def test_an_openstreetmap_subdomain_url_gets_the_user_agent_check(tmp_path, monkeypatch, mode):
    url = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
    cfg = _cfg(tmp_path, RADAR_BASEMAP=mode, RADAR_TILE_URL=url, NWS_USER_AGENT=PLACEHOLDER_UA)
    net = _Net(monkeypatch)
    night, _ = _maps(cfg)
    night._build_basemap()
    assert net.hits("openstreetmap") == []            # never with a placeholder contact
    if mode == "carto":
        assert night.basemap_chain() == ["carto-cached", "carto"]
    else:
        assert night.basemap_chain() == ["osm"] and night.osm_state == "skipped-ua"
    # with a real contact that URL is used, and the page names the setting behind it
    rd._OSM_UA_WARNED.clear()
    if mode == "auto":
        cfg.NWS_USER_AGENT = REAL_UA
        night._build_basemap()
        assert night.basemap_source == "osm"
        assert net.hits("openstreetmap")[0].startswith("https://a.tile.openstreetmap.org/")
        basemap, *_ = _poller(cfg, [night])._basemap_status()
        assert basemap["chosen_by"] == {"night": "TTU_SAFETY_RADAR_TILE_URL"}
        assert rd.basemap_summary(basemap) == ("Basemap: OpenStreetMap standard tiles "
                                               "(colour-inverted), as set by "
                                               "TTU_SAFETY_RADAR_TILE_URL.")


# ---- a GPS position adopted after the first build ------------------------------------------
def test_gps_adoption_rebuilds_the_map_for_the_adopted_site(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, NWS_USER_AGENT=REAL_UA)
    adopted = (33.749, -101.957)
    # the Pi's pre-watermark CARTO composite, named for the adopted coordinates
    at_site = types.SimpleNamespace(**vars(cfg))
    at_site.GEOCODE = adopted
    _seed_legacy(at_site)
    net = _Net(monkeypatch)
    night, _ = _maps(cfg)
    night._build_basemap()              # the first poll after a reboot: default site
    assert night.basemap_source == "osm" and not night.basemap_due()
    cfg.GEOCODE = adopted               # monitor._update_geocode, moments later
    assert night.basemap_due()
    net.calls.clear()
    _poller(cfg, [night])._retry_basemaps()
    assert night.basemap_source == "carto-cached" and net.calls == []
    assert night._basemap.getpixel((3, 3)) == DARK and not night.basemap_due()


# ---- notes say why a source is missing -------------------------------------------------
def test_notes_name_a_failed_custom_source_and_an_unreachable_osm(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, RADAR_TILE_URL="https://tiles.example.net/{z}/{x}/{y}.png",
               NWS_USER_AGENT=REAL_UA)
    net = _Net(monkeypatch)             # tiles.example.net: "unexpected host" = down
    night, _ = _maps(cfg)
    night._build_basemap()
    assert night.basemap_source == "osm"
    notes, _ = rd.basemap_messages(cfg, [night])
    assert "night map: custom TTU_SAFETY_RADAR_TILE_URL failed, re-checked every 6 h" in notes
    net.osm = "down"
    rd._OSM_UA_WARNED.clear()
    other = rd.Thumbnailer(_cfg(tmp_path / "o", NWS_USER_AGENT=REAL_UA,
                                RADAR_TILE_URL="https://tiles.example.net/{z}/{x}/{y}.png"),
                           "https://tiles.example.net/{z}/{x}/{y}.png",
                           str(tmp_path / "o.png"), "dark")
    other._build_basemap()
    notes, _ = rd.basemap_messages(cfg, [other])
    assert other.basemap_source == "none" and "OpenStreetMap tiles could not be fetched" in notes
    assert rd.basemap_summary({"night": "none"}, notes).startswith(
        "Basemap: none (plain background); night map: custom TTU_SAFETY_RADAR_TILE_URL failed")


def test_the_wu_key_template_value_is_warned_about_but_kept():
    env = dict(os.environ, TTU_SAFETY_WU_KEY="your-weather-underground-pws-api-key-here")
    out = subprocess.run([sys.executable, "-c", "from safety import config as c; "
                          "print(bool(c.WU_API_KEY)); print('|'.join(c.CONFIG_WARNINGS))"],
                         cwd=REPO, env=env, capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert out[0] == "True"             # still used: "unset" would switch rain polling off
    assert "TTU_SAFETY_WU_KEY is still the ttustatus.env.example placeholder" in out[1]


def test_docs_examples_use_a_detectable_placeholder_and_targeted_deletion():
    readme = open(os.path.join(REPO, "README.md"), encoding="utf-8").read()
    safety_md = open(os.path.join(REPO, "README_SAFETY.md"), encoding="utf-8").read()
    srcs = [readme, safety_md,
            open(os.path.join(REPO, "safety", "config.py"), encoding="utf-8").read(),
            open(os.path.join(REPO, "safety", "server.py"), encoding="utf-8").read()]
    # every example contact a reader could copy is one the daemon flags as a placeholder
    for text in srcs:
        for m in re.finditer(r"ttustatus; ([^)\s]+)\)", text):
            assert config.ua_has_placeholder("x; %s)" % m.group(1)), m.group(1)
    # deleting just the watermarked map, found through the log line that names it
    assert "basemap loaded from cache" in readme and "journalctl" in readme
    for text in (readme, safety_md):
        assert "TTU_SAFETY_RADAR_THUMB_PX" in text and "TTU_SAFETY_RADAR_TILE_ZOOM" in text
    assert "not put a CARTO key into" in safety_md
