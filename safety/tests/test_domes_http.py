"""POST /domes/v1/report: the route's checks, in their order (size, key, JSON, fields),
and its replies."""
import json

import pytest

from safety import config
from safety.alpaca import create_app
from safety.config import DomeConfig
from safety.domes import DomeStore
from safety.monitor import SafetyMonitor

KEY1 = "k1-" + "a" * 29
KEY2 = "k2-" + "b" * 29
WEST = DomeConfig(1, "Dome 1 (west)", KEY1)
EAST = DomeConfig(2, "Dome 2 (east)", KEY2)
PATH = "/domes/v1/report"


@pytest.fixture
def rig(env, monkeypatch):
    monkeypatch.setattr(config, "DOMES", (WEST, EAST))
    monkeypatch.setattr(config, "DOME_STALE_SEC", 120)
    store = DomeStore(config)
    mon = SafetyMonitor(config, env["log"], env["poller"], domes=store)
    app = create_app(mon, config)
    app.testing = True
    return {"client": app.test_client(), "store": store, "monitor": mon}


def report(**kw):
    rep = {"shutter": "closed", "current": True,
           "switches": [{"name": "Mount power", "on": True}], "source": "test 1.0"}
    rep.update(kw)
    return json.dumps(rep).encode()


def post(rig, data=b"", key=KEY1, scheme="Bearer", headers=None, **kw):
    h = {"Content-Type": "application/json"}
    if key is not None:
        h["Authorization"] = "%s %s" % (scheme, key)
    h.update(headers or {})
    return rig["client"].post(PATH, data=data, headers=h, **kw)


def test_a_valid_report_is_stored_and_answered_ok(rig):
    r = post(rig, report())
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    d = rig["monitor"].evaluate()["domes"]["domes"][0]
    assert d["known"] is True and d["shutter"] == "closed"


def test_no_content_length_or_a_larger_body_is_413_before_anything_else(rig):
    # chunked: no Content-Length at all
    r = rig["client"].post(PATH, input_stream=__import__("io").BytesIO(report()),
                           headers={"Transfer-Encoding": "chunked",
                                    "Authorization": "Bearer " + KEY1})
    assert r.status_code == 413 and r.get_json()["ok"] is False
    big = report(source="x" * 5000)
    r = post(rig, big, key="wrong-key-but-irrelevant")
    assert r.status_code == 413                    # the size is checked before the key
    assert rig["store"].refused_count == 0
    # an announced length above the limit is refused without reading the body
    r = rig["client"].post(PATH, data=report(), environ_overrides={"CONTENT_LENGTH": "4097"},
                           headers={"Authorization": "Bearer " + KEY1})
    assert r.status_code == 413
    exactly = report(source="y" * (4096 - len(report(source=""))))
    assert len(exactly) == 4096 and post(rig, exactly).status_code == 200


@pytest.mark.parametrize("key,scheme", [
    (None, "Bearer"),                          # no header at all
    (KEY1, "Basic"),                           # another scheme
    (KEY1, "Token"),
    (KEY1[:-1] + "Z", "Bearer"),               # a wrong key
    (KEY1 + "x", "Bearer"),
    ("", "Bearer"),
])
def test_a_missing_or_wrong_key_is_403_and_nothing_is_stored(rig, key, scheme):
    r = post(rig, report(), key=key, scheme=scheme)
    assert r.status_code == 403 and r.get_json() == {"ok": False, "error": "key not accepted"}
    assert rig["monitor"].evaluate()["domes"]["domes"][0]["why"] == "no report yet"


def test_a_key_with_non_ascii_characters_is_just_a_wrong_key(rig):
    environ = {"HTTP_AUTHORIZATION": "Bearer " + KEY1[:-1] + "é"}
    r = rig["client"].post(PATH, data=report(), environ_overrides=environ,
                           content_type="application/json")
    assert r.status_code == 403


def test_the_scheme_is_matched_without_regard_to_case(rig):
    assert post(rig, report(), scheme="bearer").status_code == 200


@pytest.mark.parametrize("data,field", [
    (b"[]", "body"), (b"{nope", "body"), (b'{"shutter": NaN, "current": true}', "body"),
    (report(shutter="ajar"), "shutter"), (report(current="true"), "current"),
    (report(switches="Mount"), "switches"),
    (report(switches=[{"name": "Mount"}]), "switches[0].on"),
    (report(source=["x"]), "source"),
])
def test_a_malformed_report_is_400_naming_the_field(rig, data, field):
    r = post(rig, data)
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False and body["error"].startswith(field + ":")


def test_unknown_keys_are_ignored(rig):
    assert post(rig, report(battery_v=12.8, future={"a": [1, 2]})).status_code == 200


def test_the_key_of_dome_2_updates_dome_2_only(rig):
    assert post(rig, report(shutter="open"), key=KEY2).status_code == 200
    west, east = rig["monitor"].evaluate()["domes"]["domes"]
    assert east["shutter"] == "open" and east["known"] is True
    assert west["known"] is False


def test_the_route_never_evaluates(rig, monkeypatch):
    monkeypatch.setattr(rig["monitor"], "evaluate",
                        lambda: pytest.fail("the report route evaluated"))
    assert post(rig, report()).status_code == 200
    assert post(rig, report(), key="nope-nope-nope-nope").status_code == 403


def test_without_a_dome_the_route_does_not_exist(env, monkeypatch):
    monkeypatch.setattr(config, "DOMES", ())
    app = create_app(env["monitor"], config)
    r = app.test_client().post(PATH, data=report(),
                               headers={"Authorization": "Bearer " + KEY1})
    assert r.status_code == 404


def test_a_configured_dome_without_its_store_is_503(env, monkeypatch):
    monkeypatch.setattr(config, "DOMES", (WEST,))
    mon = SafetyMonitor(config, env["log"], env["poller"], domes=None)
    r = create_app(mon, config).test_client().post(
        PATH, data=report(), headers={"Authorization": "Bearer " + KEY1})
    assert r.status_code == 503 and r.get_json()["ok"] is False
