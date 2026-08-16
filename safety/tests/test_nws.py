from safety import config
from safety import nws_forecast as nf


def _res(nc=0, npp=0, nt=0, xc=0, xp=0, xt=0, age=5, ok=True):
    return {"ok": ok, "age_min": age,
            "now_hour": {"cloud_cover_pct": nc, "precip_prob_pct": npp, "thunder_prob_pct": nt},
            "next_hour": {"cloud_cover_pct": xc, "precip_prob_pct": xp, "thunder_prob_pct": xt}}


# Tests are written RELATIVE to the configured thresholds so that tuning them (they are
# operator-facing knobs) does not silently invalidate the suite.
CLOUD, PRECIP, THUNDER = (config.NWS_CLOUD_MAX, config.NWS_PRECIP_PROB_MAX,
                          config.NWS_THUNDER_PROB_MAX)


def test_configured_thresholds_are_the_intended_defaults():
    # the values the operator asked for; a stray edit to config must not pass unnoticed
    assert (CLOUD, PRECIP, THUNDER) == (70.0, 20.0, 15.0)


def test_safe_when_all_under():
    ev = nf.evaluate(_res(nc=CLOUD - 10, xc=CLOUD - 1, npp=PRECIP - 1, xt=THUNDER - 1), config)
    assert ev["available"] and ev["safe"] and ev["reasons"] == []


def test_cloud_over_next_hour():
    ev = nf.evaluate(_res(xc=CLOUD + 1), config)
    assert not ev["safe"]
    assert any("cloud cover" in r and "next hour" in r for r in ev["reasons"])


def test_precip_over_this_hour():
    ev = nf.evaluate(_res(npp=PRECIP + 1), config)
    assert not ev["safe"]
    assert any("precip" in r and "this hour" in r for r in ev["reasons"])


def test_thunder_over():
    ev = nf.evaluate(_res(xt=THUNDER + 1), config)
    assert not ev["safe"] and any("thunder" in r for r in ev["reasons"])


def test_just_under_the_new_thresholds_is_safe():
    # 20 % precip / 15 % thunder must NOT trip (the raised limits are inclusive-safe)
    ev = nf.evaluate(_res(npp=20, nt=15, xp=20, xt=15), config)
    assert ev["safe"] and ev["reasons"] == []


def test_boundary_exact_is_safe():
    # strictly greater-than: exactly at the limit is still safe
    ev = nf.evaluate(_res(nc=CLOUD, npp=PRECIP, nt=THUNDER,
                          xc=CLOUD, xp=PRECIP, xt=THUNDER), config)
    assert ev["safe"]


def test_error_result_is_unavailable_not_unsafe():
    ev = nf.evaluate({"ok": False, "error": "boom"}, config)
    assert ev["available"] is False and ev["safe"] is True


def test_stale_is_unavailable_not_unsafe():
    ev = nf.evaluate(_res(xc=99, age=99999), config)
    assert ev["available"] is False and ev["safe"] is True


def test_poller_component(monkeypatch):
    def fake_fetch(cfg):
        r = _res(xc=90, age=3)
        r.update({"grid": "LUB/46,41", "update_time": "2026-01-01T00:00:00+00:00",
                  "hours": [{"local": "Mon 00:00", "cloud_cover_pct": 90,
                             "precip_prob_pct": 0, "thunder_prob_pct": 0, "temp_f": 70}]})
        return r
    monkeypatch.setattr(nf, "fetch", fake_fetch)
    p = nf.NwsForecastPoller(config)
    p.poll_now()
    c = p.component()
    assert c["available"] is True and c["safe"] is False
    assert c["hours"] and c["now_hour"] is not None
