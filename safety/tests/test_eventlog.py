"""The audit log's size cap (2026-11-13).

~/safety_events.log used to grow without limit. It is now capped at
TTU_SAFETY_EVENT_LOG_MAX_MB (default 1024 = 1 GB) together with its one backup: at half
the cap the file becomes safety_events.log.1 and a new one starts.
"""
import os
import subprocess
import sys
from pathlib import Path

from safety import config
from safety.eventlog import DEFAULT_MAX_MB, EventLog

REPO = Path(__file__).resolve().parents[2]
KB = 1 / 1024                       # max_mb in kilobytes, for small test caps


def size(path):
    return path.stat().st_size if path.exists() else 0


def test_at_half_the_cap_the_log_becomes_the_backup_and_the_pair_stays_within_it(tmp_path):
    path = tmp_path / "safety_events.log"
    events = EventLog(str(path), max_mb=8 * KB)              # an 8 kB cap
    for number in range(400):
        events.record("UNSAFE", reason="rain at KTXSHALL7 0.02 in/hr", result="latch 3h",
                      n=number)
        assert size(path) + size(tmp_path / "safety_events.log.1") <= 8 * 1024
        assert size(path) <= 4 * 1024
    lines = path.read_text(encoding="utf-8").splitlines()
    backup = (tmp_path / "safety_events.log.1").read_text(encoding="utf-8").splitlines()
    assert lines[-1].endswith("n=399")
    # one backup, holding the events just before the current file's first one
    assert backup[-1].endswith(f"n={int(lines[0].rsplit('=', 1)[1]) - 1}")
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["safety_events.log", "safety_events.log.1"]
    assert events.recent(1)[0]["n"] == 399                   # the page's memory: unchanged


def test_a_log_that_cannot_be_renamed_grows_to_the_cap_then_stops(tmp_path, monkeypatch,
                                                                  caplog, capsys):
    calls = []

    def refuse(src, dst):
        calls.append((src, dst))
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)
    path = tmp_path / "safety_events.log"
    events = EventLog(str(path), max_mb=4 * KB)
    for number in range(200):
        events.record("SAFE", reason="all clear", n=number)
    assert 3 * 1024 < size(path) <= 4 * 1024                 # grew to the cap, not past it
    assert calls                                             # (a rename was tried)
    said = [r.getMessage() for r in caplog.records if "cannot be renamed" in r.getMessage()]
    assert len(said) == 1                                    # once, not per event
    assert "n=199" not in path.read_text(encoding="utf-8")
    assert "EVENT" in capsys.readouterr().out                # every event still printed
    assert events.recent(1)[0]["n"] == 199


def test_the_default_cap_is_1_gb():
    assert DEFAULT_MAX_MB == 1024.0 and config.EVENT_LOG_MAX_MB == 1024.0
    assert EventLog(None)._max_bytes == 1024 ** 3


def test_the_cap_is_read_from_the_environment_and_bounded():
    def setting(value):
        env = dict(os.environ, TTU_SAFETY_EVENT_LOG_MAX_MB=value)
        out = subprocess.run([sys.executable, "-c", "from safety import config as c; "
                              "print(c.EVENT_LOG_MAX_MB, *c.CONFIG_WARNINGS, sep='|')"],
                             cwd=REPO, env=env, capture_output=True, text=True, check=True)
        return out.stdout.strip().split("|")

    assert setting("2048")[0] == "2048.0"
    small = setting("0.1")
    assert small[0] == "1.0" and any("TTU_SAFETY_EVENT_LOG_MAX_MB" in w for w in small[1:])
    junk = setting("lots")
    assert junk[0] == "1024.0" and any("not a number" in w for w in junk[1:])
