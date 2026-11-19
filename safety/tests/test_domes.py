"""Dome status reports (safety/domes.py, config.parse_domes, SafetyMonitor's `domes` block):
which domes are expected, what counts as known, and the requirements of the design —
R1 (nothing changes without a dome), R3 (display only), R6 (the key never leaks),
R8 (no SD-card writes)."""
import json
import logging
import os

import pytest

from safety import config, domes
from safety.alpaca import create_app
from safety.config import DomeConfig, parse_domes
from safety.domes import DomeStore, parse_report
from safety.monitor import SafetyMonitor

KEY1 = "k1-" + "a" * 29                  # 32 printable characters
KEY2 = "k2-" + "b" * 29


def env(**kw):
    return {"TTU_SAFETY_" + k: v for k, v in kw.items()}


# --- which domes are expected ------------------------------------------------------------
def test_a_dome_exists_exactly_when_its_key_is_set():
    assert parse_domes({}) == ((), [])
    assert parse_domes({"PATH": "/bin", "TTU_SAFETY_DEW": "1"}) == ((), [])
    found, warnings = parse_domes(env(DOME1_KEY=KEY1, DOME3_KEY=KEY2))
    assert [d.n for d in found] == [1, 3] and warnings == []        # gaps are fine
    assert [d.name for d in found] == ["Dome 1", "Dome 3"]
    assert found[0].switches == "all" and found[0].warn_on == frozenset()


def test_names_with_spaces_and_parentheses_and_too_long():
    found, warnings = parse_domes(env(DOME1_KEY=KEY1, DOME1_NAME="Dome 1 (west)",
                                      DOME2_KEY=KEY2, DOME2_NAME="x" * 45))
    assert found[0].name == "Dome 1 (west)"
    assert found[1].name == "x" * 40 and any("DOME2_NAME" in w for w in warnings)


@pytest.mark.parametrize("bad,why", [
    ("short-key", "shorter than 16"),
    ("has a blank in the middle", "printable ASCII"),
    (" " + KEY1, "blanks around it"),
    ('quote"' + "q" * 20, "printable ASCII"),
    ("ü" * 20, "printable ASCII"),
    ("z" * 129, "longer than 128"),
])
def test_a_key_of_the_wrong_shape_leaves_the_dome_out_and_is_never_quoted(bad, why):
    found, warnings = parse_domes(env(DOME1_KEY=bad, DOME1_NAME="West"))
    assert found == ()
    assert len(warnings) == 1 and "TTU_SAFETY_DOME1_KEY" in warnings[0] and why in warnings[0]
    assert bad.strip() not in warnings[0] and bad not in repr(warnings)


def test_one_key_for_two_domes_leaves_the_second_out():
    found, warnings = parse_domes(env(DOME1_KEY=KEY1, DOME2_KEY=KEY1))
    assert [d.n for d in found] == [1]
    assert any("DOME2_KEY is the same key as TTU_SAFETY_DOME1_KEY" in w for w in warnings)
    assert KEY1 not in json.dumps(warnings)


def test_settings_of_a_dome_without_a_key_and_unknown_dome_variables_are_reported():
    found, warnings = parse_domes(env(DOME2_NAME="East", DOME9_KEY=KEY1, DOME_1_KEY=KEY2,
                                      DOMES="x", DOME1_KEY=""))
    assert found == ()
    text = " | ".join(warnings)
    assert "TTU_SAFETY_DOME2_NAME is set but TTU_SAFETY_DOME2_KEY is not" in text
    assert "TTU_SAFETY_DOME9_KEY: domes are numbered 1 to 8" in text
    assert "TTU_SAFETY_DOME_1_KEY is not a dome setting" in text
    assert "TTU_SAFETY_DOMES is not a dome setting" in text
    assert KEY1 not in text and KEY2 not in text


def test_the_switch_list_forms():
    warn = []
    assert config.parse_dome_switches("", "V", warn) is None
    assert config.parse_dome_switches("  ALL ", "V", warn) == "all"
    assert config.parse_dome_switches("None", "V", warn) == "none"
    assert config.parse_dome_switches("Mount power=Mount, CCD power = CCD ,Light", "V",
                                      warn) == (("Mount power", "Mount"),
                                                ("CCD power", "CCD"), ("Light", "Light"))
    assert warn == []
    assert config.parse_dome_switches("Light,,light=Lamp, =x", "V", warn) == (
        ("Light", "Light"),)
    assert any("listed twice" in w for w in warn)
    assert any("without a switch name" in w for w in warn)
    warn.clear()
    assert config.parse_dome_switches(",,", "V", warn) == "all" and warn


def test_the_warn_list_and_a_dome_s_own_lists():
    found, warnings = parse_domes(env(
        DOME1_KEY=KEY1, DOME2_KEY=KEY2,
        DOME_SWITCHES="Mount power=Mount,CCD power=CCD,Light", DOME_WARN_ON="LIGHT",
        DOME2_SWITCHES="Mount power=Mount,Flat panel", DOME2_WARN_ON="none"))
    assert warnings == []
    west, east = found
    assert west.switches == (("Mount power", "Mount"), ("CCD power", "CCD"),
                             ("Light", "Light"))
    assert west.warn_on == frozenset({"light"})
    assert east.switches == (("Mount power", "Mount"), ("Flat panel", "Flat panel"))
    assert east.warn_on == frozenset()


def test_the_key_is_not_in_the_repr_of_a_dome():
    found, _ = parse_domes(env(DOME1_KEY=KEY1))
    assert KEY1 not in repr(found) and KEY1 not in str(found[0])


def test_the_daemon_reads_it_all_from_its_environment(tmp_path):
    import subprocess
    import sys
    code = ("import json; from safety import config as c; print(json.dumps({"
            "'domes': [[d.n, d.name, d.switches, sorted(d.warn_on)] for d in c.DOMES], "
            "'stale': c.DOME_STALE_SEC, 'warnings': c.CONFIG_WARNINGS}))")
    clean = {k: v for k, v in os.environ.items() if not k.startswith("TTU_SAFETY_")}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def run(**e):
        out = subprocess.run([sys.executable, "-c", code], cwd=root,
                             env=dict(clean, **env(**e)), capture_output=True, text=True,
                             check=True).stdout
        return json.loads(out)
    plain = run()
    assert plain == {"domes": [], "stale": 120, "warnings": []}
    ours = run(DOME1_NAME="Dome 1 (west)", DOME1_KEY=KEY1, DOME2_NAME="Dome 2 (east)",
               DOME2_KEY=KEY2, DOME_SWITCHES="Mount power=Mount,CCD power=CCD,Light",
               DOME_WARN_ON="Light", DOME_STALE_SEC="10")
    assert ours["domes"] == [
        [1, "Dome 1 (west)", [["Mount power", "Mount"], ["CCD power", "CCD"],
                              ["Light", "Light"]], ["light"]],
        [2, "Dome 2 (east)", [["Mount power", "Mount"], ["CCD power", "CCD"],
                              ["Light", "Light"]], ["light"]]]
    assert ours["stale"] == 30 and len(ours["warnings"]) == 1     # clamped, loudly
    assert KEY1 not in json.dumps(ours) and KEY2 not in json.dumps(ours)


# --- the report format -------------------------------------------------------------------
def body(**kw):
    rep = {"shutter": "closed", "current": True,
           "switches": [{"name": "Mount power", "on": True}, {"name": "Light", "on": False}],
           "source": "merlino-dome 0.4.3"}
    rep.update(kw)
    return json.dumps(rep).encode()


def test_a_valid_report_and_unknown_keys():
    r = parse_report(body(extra={"x": 1}, battery_v=12.9))
    assert r == {"shutter": "closed", "current": True,
                 "switches": [("Mount power", True), ("Light", False)],
                 "source": "merlino-dome 0.4.3"}
    r = parse_report(json.dumps({"shutter": "open", "current": False}).encode())
    assert r["switches"] == [] and r["source"] is None


@pytest.mark.parametrize("raw,field", [
    (b"[1, 2]", "body"), (b"not json", "body"), (b'{"shutter": NaN}', "body"),
    (b'{"shutter": "closed", "current": Infinity}', "body"), (b"\xff\xfe", "body"),
    (b"[" * 3000 + b"]" * 3000, "body"),
    (body(shutter="ajar"), "shutter"), (body(shutter=None), "shutter"),
    (json.dumps({"current": True}).encode(), "shutter"),
    (body(current="yes"), "current"), (body(current=1), "current"),
    (body(switches={"a": 1}), "switches"), (body(switches=None), "switches"),
    (body(switches=[{"name": "s%d" % i, "on": True} for i in range(17)]), "switches"),
    (body(switches=["Mount"]), "switches[0]"),
    (body(switches=[{"name": "", "on": True}]), "switches[0].name"),
    (body(switches=[{"name": " \t ", "on": True}]), "switches[0].name"),
    (body(switches=[{"name": 5, "on": True}]), "switches[0].name"),
    (body(switches=[{"name": "Mount"}]), "switches[0].on"),
    (body(switches=[{"name": "Mount", "on": 1}]), "switches[0].on"),
    (body(source=5), "source"),
])
def test_each_violation_names_its_field(raw, field):
    with pytest.raises(domes.ReportError) as e:
        parse_report(raw)
    assert str(e.value).startswith(field + ":")


def test_texts_are_cleaned_and_cut():
    r = parse_report(body(switches=[{"name": "M\x00ount‮ " + "x" * 80, "on": None}],
                          source="s\x07rc " + "y" * 100))
    name = r["switches"][0][0]
    assert len(name) <= 40 and "\x00" not in name and "‮" not in name
    assert len(r["source"]) <= 60 and "\x07" not in r["source"]


# --- the store: what counts as known -------------------------------------------------------
class Cfg:
    def __init__(self, *domes_, stale=120):
        self.DOMES = domes_
        self.DOME_STALE_SEC = stale
        self.DOME_SHOW_ALL_MAX = 8


WEST = DomeConfig(1, "Dome 1 (west)", KEY1,
                  (("Mount power", "Mount"), ("CCD power", "CCD"), ("Light", "Light")),
                  {"light"})
EAST = DomeConfig(2, "Dome 2 (east)", KEY2)


def by_n(comp):
    return {d["n"]: d for d in comp["domes"]}


def test_every_configured_dome_is_listed_before_its_first_report():
    store = DomeStore(Cfg(WEST, EAST))
    comp = store.component(mono=100.0)
    assert comp["enabled"] is True and comp["stale_after_s"] == 120 and comp["error"] is None
    west, east = comp["domes"]
    assert west == {"n": 1, "name": "Dome 1 (west)", "shutter": "unknown", "known": False,
                    "age_s": None, "why": "no report yet", "source": None,
                    "switches": [{"label": "Mount", "on": None, "known": False, "warn": False},
                                 {"label": "CCD", "on": None, "known": False, "warn": False},
                                 {"label": "Light", "on": None, "known": False,
                                  "warn": False}]}
    assert east["switches"] == []          # "all", and no report has named any switch yet


def test_a_current_report_is_known_and_the_light_on_is_flagged():
    store = DomeStore(Cfg(WEST, EAST))
    store.accept(WEST, parse_report(body(switches=[
        {"name": "MOUNT POWER", "on": True}, {"name": "CCD power", "on": False},
        {"name": "Light", "on": True}, {"name": "USB hub", "on": True}])), mono=100.0)
    d = by_n(store.component(mono=112.5))[1]
    assert d["known"] is True and d["shutter"] == "closed" and d["age_s"] == 12
    assert d["why"] is None and d["source"] == "merlino-dome 0.4.3"
    assert d["switches"] == [
        {"label": "Mount", "on": True, "known": True, "warn": False},
        {"label": "CCD", "on": False, "known": True, "warn": False},
        {"label": "Light", "on": True, "known": True, "warn": True}]


def test_the_key_of_dome_2_updates_dome_2_and_nothing_else():
    store = DomeStore(Cfg(WEST, EAST))
    assert store.match(KEY2.encode()) is EAST and store.match(KEY1.encode()) is WEST
    assert store.match(b"wrong" * 8) is None and store.match(KEY1.encode() + b"x") is None
    store.accept(EAST, parse_report(body(shutter="open")), mono=50.0)
    comp = by_n(store.component(mono=51.0))
    assert comp[2]["shutter"] == "open" and comp[2]["known"] is True
    assert comp[1]["known"] is False and comp[1]["why"] == "no report yet"
    # "all": every switch the report named, by its own name
    assert [s["label"] for s in comp[2]["switches"]] == ["Mount power", "Light"]


def test_not_current_stale_null_and_missing_switches_are_unknown():
    store = DomeStore(Cfg(WEST, stale=120))
    store.accept(WEST, parse_report(body(current=False)), mono=0.0)
    d = by_n(store.component(mono=1.0))[1]
    assert (d["known"], d["why"], d["shutter"]) == (False, "not current", "unknown")
    assert all(s["known"] is False and s["on"] is None for s in d["switches"])
    store.accept(WEST, parse_report(body(switches=[{"name": "Mount power", "on": None}])),
                 mono=10.0)
    d = by_n(store.component(mono=11.0))[1]
    assert d["known"] is True
    assert d["switches"][0] == {"label": "Mount", "on": None, "known": False, "warn": False}
    assert d["switches"][2]["known"] is False         # Light: not in this report at all
    # staleness by the monotonic clock alone
    d = by_n(store.component(mono=10.0 + 120.5))[1]
    assert (d["known"], d["why"], d["shutter"], d["age_s"]) == (False, "stale", "unknown", 120)
    # a later report replaces the earlier one
    store.accept(WEST, parse_report(body(shutter="opening")), mono=200.0)
    d = by_n(store.component(mono=201.0))[1]
    assert (d["known"], d["shutter"]) == (True, "opening")


def test_none_shows_no_switch_and_all_shows_at_most_the_limit():
    cfg = Cfg(DomeConfig(1, "A", KEY1, "none"), DomeConfig(2, "B", KEY2, "all"))
    store = DomeStore(cfg)
    many = [{"name": "s%02d" % i, "on": True} for i in range(16)]
    store.accept(cfg.DOMES[0], parse_report(body(switches=many)), mono=1.0)
    store.accept(cfg.DOMES[1], parse_report(body(switches=many)), mono=1.0)
    comp = by_n(store.component(mono=2.0))
    assert comp[1]["switches"] == [] and len(comp[2]["switches"]) == 8


def test_changes_are_logged_to_the_daemon_log(caplog):
    caplog.set_level(logging.INFO, logger="ttu.safety.domes")
    store = DomeStore(Cfg(WEST))
    store.accept(WEST, parse_report(body()), mono=0.0)
    store.accept(WEST, parse_report(body(shutter="opening")), mono=5.0)
    store.accept(WEST, parse_report(body(shutter="opening", switches=[
        {"name": "Light", "on": True}])), mono=10.0)
    store.component(mono=200.0)
    store.component(mono=260.0)                       # the stale line comes once
    store.accept(WEST, parse_report(body()), mono=400.0)
    text = caplog.text
    assert "dome 1 (Dome 1 (west)): first report — roof CLOSED" in text
    assert "roof CLOSED -> OPENING" in text and "Light off -> on" in text
    assert text.count("no report for 3 min — shown as UNKNOWN") == 1
    assert "reporting again after 6 min" in text


@pytest.mark.parametrize("light_on", [False, True])
def test_reordered_switches_log_only_actual_changes(caplog, light_on):
    caplog.set_level(logging.INFO, logger="ttu.safety.domes")
    store = DomeStore(Cfg(EAST))       # default "all": order comes from the report
    store.accept(EAST, parse_report(body()), mono=0.0)
    caplog.clear()
    store.accept(EAST, parse_report(body(switches=[
        {"name": "light", "on": light_on},
        {"name": "MOUNT POWER", "on": True}])), mono=1.0)
    lines = [r.getMessage() for r in caplog.records if r.name == "ttu.safety.domes"]
    assert lines == (["dome 2 (Dome 2 (east)): light off -> on"] if light_on else [])


def test_switches_added_or_removed_do_not_borrow_another_switch_s_state(caplog):
    caplog.set_level(logging.INFO, logger="ttu.safety.domes")
    store = DomeStore(Cfg(EAST))
    store.accept(EAST, parse_report(body()), mono=0.0)
    caplog.clear()
    store.accept(EAST, parse_report(body(switches=[
        {"name": "Light", "on": False}, {"name": "CCD", "on": True}])), mono=1.0)
    lines = [r.getMessage() for r in caplog.records if r.name == "ttu.safety.domes"]
    assert lines == ["dome 2 (Dome 2 (east)): Mount power on -> unknown",
                     "dome 2 (Dome 2 (east)): CCD unknown -> on"]


def test_switch_identity_is_its_name_even_when_display_labels_match(caplog):
    caplog.set_level(logging.INFO, logger="ttu.safety.domes")
    dome = DomeConfig(1, "West", KEY1, (("Mount power", "Power"), ("CCD", "Power")))
    store = DomeStore(Cfg(dome))
    store.accept(dome, parse_report(body(switches=[
        {"name": "Mount power", "on": True}, {"name": "CCD", "on": False}])), mono=0.0)
    caplog.clear()
    store.accept(dome, parse_report(body(switches=[
        {"name": "CCD", "on": True}, {"name": "Mount power", "on": False}])), mono=1.0)
    lines = [r.getMessage() for r in caplog.records if r.name == "ttu.safety.domes"]
    assert lines == ["dome 1 (West): Power on -> off", "dome 1 (West): Power off -> on"]


def test_a_refused_key_is_counted_and_logged_sparingly_without_the_key(caplog):
    store = DomeStore(Cfg(WEST))
    for i in range(5):
        store.refused(mono=float(i))
    store.refused(mono=700.0)
    assert store.refused_count == 6
    lines = [r.getMessage() for r in caplog.records if r.name == "ttu.safety.domes"]
    assert len(lines) == 2 and "matches no TTU_SAFETY_DOME<n>_KEY" in lines[0]
    assert "refused for their key: 5 since the last such line" in lines[1]


def test_the_store_never_touches_a_file(tmp_path, monkeypatch):
    def no_open(*a, **k):
        raise AssertionError("the dome store opened a file")
    store = DomeStore(Cfg(WEST))
    monkeypatch.setattr("builtins.open", no_open)
    store.accept(WEST, parse_report(body()), mono=1.0)
    store.refused(mono=2.0)
    store.component(mono=3.0)


# --- in the daemon: R1, R3, R6, R8 ---------------------------------------------------------
TODAY_KEYS = {"alpaca", "components", "connected", "dew", "events_tail", "geocode",
              "is_safe", "reasons", "ts", "warnings", "weather_log"}
TODAY_COMPONENTS = {"clock", "connectivity", "glm", "hazard_info", "hazards", "humidity",
                    "nws", "radar", "rain", "sun"}


def test_r1_without_a_dome_the_state_has_today_s_keys_and_no_route(env, monkeypatch):
    monkeypatch.setattr(config, "DOMES", ())
    mon = env["monitor"]
    monkeypatch.setattr(mon, "_domes_component",
                        lambda: pytest.fail("_domes_component called without a dome"))
    st = mon.evaluate()
    assert set(st) == TODAY_KEYS and set(st["components"]) == TODAY_COMPONENTS
    client = create_app(mon, config).test_client()
    assert client.post("/domes/v1/report", data=body()).status_code == 404
    assert "domes" not in client.get("/status").get_json()


def with_domes(env, monkeypatch, store=True):
    cfg = config
    monkeypatch.setattr(cfg, "DOMES", (WEST, EAST))
    monkeypatch.setattr(cfg, "DOME_STALE_SEC", 120)
    mon = SafetyMonitor(cfg, env["log"], env["poller"],
                        domes=DomeStore(cfg) if store else None)
    return mon


def decision(st):
    return (st["is_safe"], st["reasons"], st["warnings"], st["components"],
            st["dew"]["is_safe"], st["dew"].get("reasons"))


def test_r3_reports_never_change_the_verdicts(env, monkeypatch, write_inputs):
    write_inputs(config, sun=-20.0, humidity=40.0)
    plain = env["monitor"].evaluate()
    for safe_case in (False, True):
        if safe_case:
            write_inputs(config, sun=10.0, humidity=99.0)      # unsafe: sun up, humid
            plain = env["monitor"].evaluate()
        mon = with_domes(env, monkeypatch)
        hostile = [{"name": "‮" + "x" * 60 + str(i), "on": bool(i % 2)} for i in range(16)]
        for shutter in domes.SHUTTER_STATES:
            mon.domes.accept(WEST, parse_report(body(shutter=shutter, switches=hostile)))
            mon.domes.accept(EAST, parse_report(body(shutter=shutter, current=False)))
            st = mon.evaluate()
            assert set(st) == TODAY_KEYS | {"domes"}
            got, want = decision(st), decision(plain)
            # the components' ages may tick between the two evaluations: compare verdicts
            assert got[0] == want[0] and got[1] == want[1] and got[4] == want[4]
            assert {k: c.get("safe") for k, c in got[3].items()} == \
                   {k: c.get("safe") for k, c in want[3].items()}
            assert "domes" not in st["components"]


def test_r3_a_broken_or_missing_store_lists_the_domes_as_unknown(env, monkeypatch, caplog):
    mon = with_domes(env, monkeypatch, store=False)
    st = mon.evaluate()
    assert [d["why"] for d in st["domes"]["domes"]] == ["error", "error"]
    assert "not running" in st["domes"]["error"]

    class Broken:
        def component(self):
            raise RuntimeError("boom")
    mon.domes = Broken()
    st = mon.evaluate()
    assert [d["why"] for d in st["domes"]["domes"]] == ["error", "error"]
    assert "boom" in st["domes"]["error"] and isinstance(st["is_safe"], bool)
    mon.domes = type("Odd", (), {"component": lambda self: ["not", "a", "dict"]})()
    assert mon.evaluate()["domes"]["domes"][0]["known"] is False


def test_r6_the_key_appears_nowhere(env, monkeypatch, caplog, tmp_path):
    import sys
    from unittest.mock import MagicMock
    for m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
              "astropy.time", "astropy.coordinates", "astropy.utils",
              "astropy.utils.iers"]:
        sys.modules.setdefault(m, MagicMock())
    import make_status_page as msp
    caplog.set_level(logging.DEBUG)
    mon = with_domes(env, monkeypatch)
    client = create_app(mon, config).test_client()
    headers = {"Authorization": "Bearer " + KEY1, "Content-Type": "application/json"}
    assert client.post("/domes/v1/report", data=body(), headers=headers).status_code == 200
    bad = {"Authorization": "Bearer " + KEY1[:-1] + "Z"}
    assert client.post("/domes/v1/report", data=body(), headers=bad).status_code == 403
    st = mon.evaluate()
    with open(config.STATE_FILE, encoding="utf-8") as f:
        on_disk = f.read()
    monkeypatch.setattr(msp, "SAFETY_STATE_FILE", config.STATE_FILE)
    monkeypatch.setattr(msp, "HTML_VIA_SHM", False)
    monkeypatch.setattr(msp, "HTML_FILE", str(tmp_path / "status.html"))
    msp.write_html(20.0, 40.0, "x", "chronyc failed", None, None, None, None,
                   {"available": False}, False, {"mode": "disabled", "error": None}, None)
    page = (tmp_path / "status.html").read_text(encoding="utf-8")
    assert ">Domes</h2>" in page
    texts = [json.dumps(st), on_disk, client.get("/status").get_data(as_text=True),
             client.get("/setup").get_data(as_text=True), page, caplog.text]
    with open(config.EVENT_LOG, encoding="utf-8") as f:
        texts.append(f.read())
    for t in texts:
        assert KEY1 not in t and KEY1[:-1] not in t and KEY2 not in t
        assert "127.0.0.1" not in json.dumps(st["domes"])     # no sender address


def test_r8_identical_reports_write_no_state_beyond_the_heartbeat(env, monkeypatch):
    mon = with_domes(env, monkeypatch)
    writes = []
    monkeypatch.setattr(mon, "_write_state", lambda state: writes.append(state["ts"]))
    mon.evaluate()
    mon.domes.accept(WEST, parse_report(body()))
    mon.evaluate()                                     # the dome became known: one write
    n = len(writes)
    size = os.path.getsize(config.EVENT_LOG) if os.path.exists(config.EVENT_LOG) else 0
    for _ in range(1000):
        mon.domes.accept(WEST, parse_report(body()))
        mon.evaluate()
    assert len(writes) == n            # within the heartbeat: nothing new to write
    after = os.path.getsize(config.EVENT_LOG) if os.path.exists(config.EVENT_LOG) else 0
    assert after == size               # and nothing in the event log
    mon.domes.accept(WEST, parse_report(body(shutter="opening")))
    mon.evaluate()
    assert len(writes) == n + 1        # a change is written at once


def test_the_daemon_builds_the_store_only_with_a_dome(daemon, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    assert daemon()["monitor"].domes is None
    assert "dome status display" not in caplog.text
    monkeypatch.setattr(config, "DOMES", (WEST, EAST))
    built = daemon()
    assert isinstance(built["monitor"].domes, DomeStore)
    assert ("dome status display: expecting dome 1 (Dome 1 (west)), dome 2 (Dome 2 (east)); "
            "reports to POST /domes/v1/report") in caplog.text
    assert KEY1 not in caplog.text


from safety.tests.test_hazard_integration import daemon  # noqa: E402,F401  (a fixture)
