from safety import wu_poll


def test_poll_detects_any_rain(monkeypatch):
    def fake_precip(sid):
        return (0.05, 60) if sid == "S2" else (0.0, 60)
    monkeypatch.setattr(wu_poll, "station_precip", fake_precip)
    res = wu_poll.poll_stations([("S1", 1.0), ("S2", 2.0)])
    assert res["live"] == 2
    assert len(res["raining"]) == 1
    assert res["raining"][0]["station"] == "S2"


def test_poll_skips_offline_and_stale(monkeypatch):
    def fake_precip(sid):
        return {"S1": None, "S2": (0.0, 999999), "S3": (0.0, 60)}[sid]
    monkeypatch.setattr(wu_poll, "station_precip", fake_precip)
    res = wu_poll.poll_stations(["S1", "S2", "S3"])
    assert res["live"] == 1          # S1 offline, S2 stale, S3 live
    assert res["raining"] == []
    assert res["total"] == 3


def test_poll_accepts_bare_ids_and_tuples(monkeypatch):
    monkeypatch.setattr(wu_poll, "station_precip", lambda sid: (0.0, 30))
    assert wu_poll.poll_stations(["A", "B"])["live"] == 2
    assert wu_poll.poll_stations([("A", 1.0)])["live"] == 1
