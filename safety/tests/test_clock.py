"""Clock-robustness tests, from the 2026-08-25 field incident: the Pi booted with its
RTC ~160 days in the past (stale fake-hwclock), read latch files persisted under the
correct August clock, and reported "rain latch active (227329 min left)". These pin the
clamps, the step handling, and the NWS re-aging that bound any clock error to at most
one latch period instead of months.
"""
import json
import time

from safety import config
from safety import nws_forecast as nf
from safety.monitor import RainPoller


DAY = 86400.0


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


# --- clamp persisted latches on load ----------------------------------------------
def test_rain_latch_from_the_future_is_clamped_on_load(tmp_path, monkeypatch):
    # the incident: latch_until ~158 days ahead of the (wrong) boot clock
    latch = tmp_path / "latch.json"
    _write(latch, {"latch_until": time.time() + 158 * DAY, "last_rain_ts": None})
    monkeypatch.setattr(config, "LATCH_FILE", str(latch))
    p = RainPoller(config, _NullLog())
    remaining = p._latch_until - time.time()
    assert remaining <= config.RAIN_LATCH_HOURS * 3600 + 120, \
        "a wrong-clock latch survived unclamped (the 227329-min incident)"
    assert p._latch_dirty                      # will re-persist the corrected value


def test_glm_latch_from_the_future_is_clamped_on_load(tmp_path, monkeypatch):
    from safety.glm_lightning import GlmLightningPoller
    latch = tmp_path / "glm.json"
    _write(latch, {"latch_until": time.time() + 158 * DAY, "last_flash_ts": None})
    monkeypatch.setattr(config, "GLM_LATCH_FILE", str(latch))
    p = GlmLightningPoller(config, _NullLog())
    assert p._latch_until - time.time() <= config.GLM_COOLOFF_HOURS * 3600 + 120


def test_radar_rain_ts_from_the_future_is_clamped_on_load(tmp_path, monkeypatch):
    from safety.radar import RadarPoller
    latch = tmp_path / "radar.json"
    _write(latch, {"last_rain_ts": time.time() + 158 * DAY})
    monkeypatch.setattr(config, "RADAR_LATCH_FILE", str(latch))
    p = RadarPoller(config, _NullLog())
    assert p._last_rain_ts is not None and p._last_rain_ts <= time.time() + 61, \
        "a future radar detection timestamp survived (froze the radar for 158 days)"


# --- clamp at check time (backward step while running) ----------------------------
def test_rain_latch_clamped_at_check_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATCH_FILE", str(tmp_path / "none.json"))
    p = RainPoller(config, _NullLog())
    p._latch_until = time.time() + 365 * DAY   # clock stepped back a year mid-run
    c = p.component(sun_alt=-10.0)
    assert c["latched"] is True                # still fail-safe: latched...
    assert c["seconds_remaining"] <= config.RAIN_LATCH_HOURS * 3600 + 120, \
        "...but bounded to one full latch, not a year"


def test_radar_freeze_clamped_at_check_time(tmp_path, monkeypatch):
    from safety.radar import RadarPoller
    monkeypatch.setattr(config, "RADAR_LATCH_FILE", str(tmp_path / "none.json"))
    p = RadarPoller(config, _NullLog())
    p._last_rain_ts = time.time() + 365 * DAY
    c = p.component(sun_alt=-10.0)
    assert c["seconds_remaining"] <= config.RADAR_LATCH_SEC + 61
    assert p._last_rain_ts <= time.time() + 61


# --- the evaporation direction: forward step must not silently clear a latch -----
def test_forward_step_rearms_an_evaporating_rain_latch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATCH_FILE", str(tmp_path / "none.json"))
    p = RainPoller(config, _NullLog())
    pre = time.time() - 158 * DAY              # the wrong (past) clock frame
    p._latch_until = pre + 3600                # rain latched 1 h ago, under that clock
    post = time.time()                         # NTP steps to the real date
    p.clock_stepped(pre, post)                 # what the step detector calls
    assert p._latch_until > post + 3000, \
        "an active latch silently evaporated across the NTP step (rain was RECENT " \
        "in real time, whatever the clock said)"
    assert p._last_poll_ts is None             # forces an immediate re-poll


def test_forward_step_does_not_rearm_an_already_expired_latch(tmp_path, monkeypatch):
    # the incident's actual case: the persisted (August) expiry had already passed in
    # real time by the step — it must expire, not re-arm
    monkeypatch.setattr(config, "LATCH_FILE", str(tmp_path / "none.json"))
    p = RainPoller(config, _NullLog())
    pre = time.time() - 158 * DAY
    p._latch_until = pre - 100                 # expired even under the wrong clock
    p.clock_stepped(pre, time.time())
    assert p._latch_until < time.time()


# --- backward step must not stall polling -----------------------------------------
def test_backward_step_does_not_stall_wu_polling(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LATCH_FILE", str(tmp_path / "none.json"))
    p = RainPoller(config, _NullLog())
    monkeypatch.setattr(config, "WU_API_KEY", "test-key")
    p._last_poll_ts = time.time() + 365 * DAY  # "last poll" now in the future
    polled = []
    monkeypatch.setattr(p, "poll_now", lambda now: polled.append(now))
    p.maybe_poll(sun_alt=-10.0)
    assert polled, "polling stalled for the size of a backward clock step"


# --- NWS: re-aging, negative age, parse containment, error export ----------------
def _ok_result(age_min=5.0, fetched_ago=0.0):
    return {"ok": True, "error": None, "age_min": age_min,
            "fetched_ts": time.time() - fetched_ago,
            "now_hour": {"cloud_cover_pct": 0, "precip_prob_pct": 0, "thunder_prob_pct": 0},
            "next_hour": {"cloud_cover_pct": 0, "precip_prob_pct": 0, "thunder_prob_pct": 0}}


def test_nws_dead_poller_goes_stale_instead_of_available_forever():
    ev = nf.evaluate(_ok_result(fetched_ago=2 * config.NWS_POLL_INTERVAL + 60), config)
    assert ev["available"] is False and ev["safe"] is True


def test_nws_negative_age_is_unavailable_not_available():
    # a forecast from "the future" = the clock is wrong; never present it as current
    ev = nf.evaluate(_ok_result(age_min=-230000.0), config)
    assert ev["available"] is False and ev["safe"] is True


def test_nws_malformed_payload_is_contained(monkeypatch):
    # valid JSON, wrong shape: must become ok=False, not an escaping exception
    monkeypatch.setattr(nf, "_get", lambda url, ua: {"properties": {
        "updateTime": "not-a-timestamp", "sky": "garbage"}})
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    res = nf.fetch(config)
    assert res["ok"] is False and "malformed" in res["error"]


def test_nws_component_exports_the_error():
    p = nf.NwsForecastPoller(config)
    p._latest = {"ok": False, "error": "certificate is not yet valid"}
    comp = p.component()
    assert comp["available"] is False and comp["safe"] is True
    assert "certificate" in comp["error"]


def test_nws_backward_step_polls_immediately(monkeypatch):
    p = nf.NwsForecastPoller(config)
    p._last_poll_ts = time.time() + 365 * DAY
    called = []
    monkeypatch.setattr(p, "poll_now", lambda now: called.append(now))
    p.maybe_poll()
    assert called, "NWS polling stalled after a backward clock step"


# --- connectivity is monotonic-internal -------------------------------------------
def test_connectivity_is_immune_to_wall_clock_steps(monkeypatch):
    from safety.connectivity import ConnectivityWatch
    w = ConnectivityWatch(config)
    monkeypatch.setattr("safety.connectivity._reachable", lambda url, t: True)
    w.probe_once()
    c = w.component()
    assert c["safe"] is True and c["offline_sec"] < 5
    # a wall-clock step cannot touch it: its frame is time.monotonic
    monkeypatch.setattr(time, "time", lambda: 4102444800.0)     # year 2100
    c = w.component()
    assert c["safe"] is True and c["offline_sec"] < 5, \
        "a wall-clock step produced a spurious offline veto"


class _NullLog:
    def record(self, *a, **k):
        pass
