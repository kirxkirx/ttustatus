"""Integration review fixes, each pinned by a test that fails without its fix.

1. The rain layer's "stale" judgement (and the "rain check blind" warning it feeds) must not
   fire every evening at dusk: the last WU poll then dates from the morning, but polling was
   off all day (sun up), so its age says nothing about the poll loop; nor may a blind run of
   polls from the morning be shown as tonight's.
"""
import time

from safety.tests.test_core_hardening import _blind_poller, _night


# --- 1. no "stale" / "rain check blind" at dusk ----------------------------------------------
def test_dusk_after_a_day_without_polling_is_not_stale(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    p, m = _blind_poller(env, monkeypatch, 2), env["monitor"]
    iv = env["cfg"].WU_POLL_INTERVAL
    t = time.time()
    p.poll_now(t - 12 * 3600)               # the last poll of the morning
    p.component(30.0, t - 60)               # all day long: sun up, polling off
    # dusk: polling is active again, the first poll of the night has not run yet
    rain = p.component(-10.0, t)
    assert rain["stale"] is False and rain["note"] is None
    st = m.evaluate()
    assert not any(w.startswith("rain check blind") for w in st["warnings"])
    # ... but a night in which no poll completes is stale, timed from when polling resumed
    rain = p.component(-10.0, t - 60 + 3 * iv + 120)
    assert rain["stale"] is True and rain["stations_live"] == 0
    assert rain["note"] == "stale, no WU poll completed for 32 min"
    assert rain["last_poll_age_s"] == round(3 * iv + 120 - 60 + 12 * 3600)


def test_a_poll_that_hangs_at_night_is_still_stale(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    p = _blind_poller(env, monkeypatch, 2)
    iv = env["cfg"].WU_POLL_INTERVAL
    t = time.time()
    p.component(30.0, t - 6 * iv)           # daytime, long before
    p.poll_now(t - 4 * iv)                  # a poll at night, then nothing
    rain = p.component(-10.0, t)
    assert rain["stale"] is True and rain["note"] == "stale, last poll 40 min ago"


def test_the_blind_warning_names_how_long_no_poll_completed(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    p, m = _blind_poller(env, monkeypatch, 2), env["monitor"]
    t = time.time()
    p.poll_now(t - 12 * 3600)               # the morning's poll
    p.component(30.0, t - 45 * 60)          # polling last seen off 45 min ago
    st = m.evaluate()
    blind = [w for w in st["warnings"] if w.startswith("rain check blind")]
    assert len(blind) == 1 and blind[0].startswith(
        "rain check blind: no WU poll completed for 45 min"), blind


def test_a_blind_morning_is_not_carried_into_the_evening(env, write_inputs, monkeypatch):
    _night(env, write_inputs)
    p, m = _blind_poller(env, monkeypatch, 0), env["monitor"]
    iv = env["cfg"].WU_POLL_INTERVAL
    t = time.time()
    for k in range(4):                      # the morning ends blind: 4 polls, no station
        p.poll_now(t - 12 * 3600 + k * iv)
    p.component(30.0, t - 60)               # the day: polling off
    rain = p.component(-10.0, t)            # dusk, before the first poll of the night
    assert rain["blind_polls"] == 0 and rain["blind_since"] is None
    assert not any(w.startswith("rain check blind") for w in m.evaluate()["warnings"])
