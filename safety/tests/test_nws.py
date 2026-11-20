from datetime import datetime, timezone

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
    assert (CLOUD, PRECIP, THUNDER) == (60.0, 20.0, 15.0)


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


def test_old_issue_uses_valid_times_across_hour_boundary_and_expires_between_polls(monkeypatch):
    # Issued just after midnight UTC, downloaded at 11:56. The thunderstorm is for 13Z.
    issue = datetime(2026, 11, 20, 0, 5, tzinfo=timezone.utc)
    boundary = issue.timestamp() + 12 * 3600
    monkeypatch.setattr(nf.time, "time", lambda: boundary - 9 * 60)
    monkeypatch.setattr(config, "NWS_GRID", "LUB/46,41")
    monkeypatch.setattr(config, "LOCAL_TZ", "America/Chicago")
    assert config.NWS_STALE_AFTER_MIN == 720
    series = {"values": [
        {"validTime": "2026-11-19T18:00:00-06:00/PT12H", "value": 0},
        {"validTime": "2026-11-20T12:00:00Z/PT1H", "value": 4},
        {"validTime": "2026-11-20T13:00:00Z/PT6H", "value": 55},
    ]}
    grid = {"properties": {"updateTime": issue.isoformat(), "probabilityOfThunder": series}}
    monkeypatch.setattr(nf, "_get", lambda url, ua: grid)
    p = nf.NwsForecastPoller(config)
    assert p.poll_now()["ok"]
    before = p.component()
    assert before["available"] and before["safe"]
    assert before["now_hour"]["thunder_prob_pct"] == 0
    assert before["next_hour"]["thunder_prob_pct"] == 4
    at_limit = p.component(boundary)
    assert at_limit["available"] and at_limit["age_min"] == 720
    assert not at_limit["safe"] and "next hour" in at_limit["reasons"][0]
    assert at_limit["now_hour"]["thunder_prob_pct"] == 4
    assert at_limit["next_hour"]["thunder_prob_pct"] == 55
    assert len(at_limit["hours"]) == config.NWS_RENDER_HOURS
    assert at_limit["hours"][0]["local"] == "Fri 06:00"
    assert at_limit["hours"][0]["thunder_prob_pct"] == 4
    assert p.outlook(boundary)["next_hour"]["thunder_prob_pct"] == 55
    for view in (p.component, p.outlook):
        expired = view(boundary + 1)
        assert not expired["available"] and expired["why"] == "Forecast stale"
    assert p.component(boundary + 1)["safe"]

    # A grid with only 12 hours of actual data ends at 12Z. The missing next hour
    # must not look clear or suppress a known veto from the covered hour.
    series["values"] = series["values"][:1]
    for value in (0, 90):
        series["values"][0]["value"] = value
        assert p.poll_now()["ok"]
        c = p.component()
        assert not c["available"] and c["why"] == "No forecast data for next hour"
        assert c["safe"] is (value == 0)
        assert not p.outlook()["available"]
