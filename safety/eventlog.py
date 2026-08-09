"""Persistent, append-only log of safety events — every UNSAFE trigger, recovery, and
rain detection, with the exact reason. This audit trail is the point of the file; the
tail of it is also shown on the status page.

Each line is human-readable and greppable, e.g.:

    2026-08-09T02:14:07  UNSAFE  reason=rain at KTXSHALL7 0.02 in/hr  result=latch 3h

Writing never raises into the caller: if the file can't be written the event is still
kept in memory (visible via recent()) and mirrored to stdout.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("ttu.safety.events")


class EventLog:
    def __init__(self, path, keep: int = 500):
        self._path = Path(path) if path else None
        self._keep = keep
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self._file_ok = False
        if self._path:
            try:
                if self._path.parent and not self._path.parent.exists():
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a", encoding="utf-8"):
                    pass
                self._file_ok = True
                log.info("logging safety events to %s", self._path)
            except Exception:
                log.exception("cannot open event log %s; memory only", self._path)

    def record(self, action: str, *, reason: str = "", source: str = "",
               result: str = "", **fields) -> dict:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        parts = [ts, action]
        if reason:
            parts.append(f"reason={reason}")
        if source:
            parts.append(f"source={source}")
        if result:
            parts.append(f"result={result}")
        parts += [f"{k}={v}" for k, v in fields.items()]
        line = "  ".join(parts)
        ev = {"time": ts, "action": action, "reason": reason,
              "source": source, "result": result, **fields}
        # Durable record FIRST (memory ring + file) so the audit trail is never lost if
        # the terminal is broken/redirected.
        with self._lock:
            self._events.append(ev)
            del self._events[:-self._keep]
            if self._file_ok:
                try:
                    with open(self._path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except Exception:
                    log.exception("event log write failed")
        # Mirror to the terminal too — best-effort; a broken stdout must never raise.
        try:
            print("EVENT  " + line, flush=True)
        except Exception:
            pass
        return ev

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self._events[-n:])

    @property
    def path(self):
        return str(self._path) if self._path else None
