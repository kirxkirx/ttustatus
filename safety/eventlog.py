"""Persistent, append-only log of safety events — every UNSAFE trigger, recovery, and
rain detection, with the exact reason. This audit trail is the point of the file; the
tail of it is also shown on the status page.

Each line is human-readable and greppable, e.g.:

    2026-08-09T02:14:07  UNSAFE  reason=rain at KTXSHALL7 0.02 in/hr  result=latch 3h

Writing never raises into the caller: if the file can't be written the event is still
kept in memory (visible via recent()) and mirrored to stdout.

Size cap. The file used to grow without limit (slowly: a few kB a day). It is now capped
at TTU_SAFETY_EVENT_LOG_MAX_MB (default 1024 = 1 GB) TOGETHER with its one backup: when
the file reaches half the cap it is renamed NAME.1, replacing the older backup, and a new
file starts. So the newest 512 MB - 1 GB of events are kept, in two plain-text files, and
the pair never takes more than the cap on the SD card. A rename is one metadata write,
once in many years at this rate; nothing else is added to the card's write load.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger("ttu.safety.events")

DEFAULT_MAX_MB = 1024.0


class EventLog:
    def __init__(self, path, keep: int = 500, max_mb: float = DEFAULT_MAX_MB):
        self._path = Path(path) if path else None
        self._keep = keep
        # the size cap (see the module docstring): NAME + NAME.1 within max_mb
        self._backup = self._path.with_name(self._path.name + ".1") if self._path else None
        self._max_bytes = max(2, int(max_mb * 1024 * 1024))
        self._cap_problem = None   # the last rotation problem, said once (not per event)
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
                log.info("logging safety events to %s (at most %.0f MB together with %s)",
                         self._path, self._max_bytes / (1024 * 1024), self._backup.name)
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
            if self._file_ok and self._room_locked(len((line + "\n").encode("utf-8"))):
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

    def _room_locked(self, nbytes: int) -> bool:
        """Rotate the file if it is due; True when `nbytes` may be appended now.

        Due at half the cap: the file becomes NAME.1 (replacing the older backup). Should
        the rename fail (a directory made read-only, say), the file keeps growing until
        it and its backup together reach the cap, and then new events go to the journal
        and the page only - said once per cause, not with every event."""
        try:
            size = self._path.stat().st_size
        except OSError:
            return True                              # (the write itself will tell)
        if size == 0 or size + nbytes <= self._max_bytes // 2:
            return True
        try:
            os.replace(self._path, self._backup)
        except OSError as e:
            problem = f"{type(e).__name__}: {e}"
            if problem != self._cap_problem:
                self._cap_problem = problem
                log.warning("event log %s cannot be renamed to %s (%s): it grows up to "
                            "the %.0f MB cap, then new events are only in the journal "
                            "and on the page", self._path, self._backup.name, problem,
                            self._max_bytes / (1024 * 1024))
            try:
                backup = self._backup.stat().st_size
            except OSError:
                backup = 0
            return size + nbytes + backup <= self._max_bytes
        self._cap_problem = None
        log.info("event log %s reached %.0f MB: renamed to %s (replacing the older one); "
                 "a new %s is started", self._path, size / (1024 * 1024),
                 self._backup.name, self._path.name)
        return True

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            return list(self._events[-n:])

    @property
    def path(self):
        return str(self._path) if self._path else None
