"""Dome status reports: DISPLAY ONLY.

Each dome's own driver POSTs its status (roof state, rain sensor, power switches) to /domes/v1/report
with its own key (README_SAFETY.md, "Dome status"); the dome PCs sit behind NAT, so they
report and nothing here ever connects to them. This module checks a report against the
format, keeps the latest accepted report of each CONFIGURED dome in memory, and turns
them into the state file's `domes` block, which the status page shows as one tile per
dome. Nothing here is an input of either IsSafe: SafetyMonitor publishes the block beside
`dew`, not inside `components`, and its decision never reads it (tests pin this). Nothing
is written to a file (no SD-card wear) and nothing goes to the event log.

The report (unknown fields are ignored so v1 can gain optional readings):
    POST /domes/v1/report
    Authorization: Bearer KEY-OF-THIS-DOME
    Content-Type: application/json
    {"shutter": "closed", "current": true, "rain_wet": false,
     "switches": [{"name": "Mount power", "on": true}, ...],
     "source": "merlino-dome 0.4.3"}
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time

from .textclean import clean_text

log = logging.getLogger("ttu.safety.domes")

REPORT_PATH = "/domes/v1/report"
MAX_BODY = 4096                 # bytes; a larger body (or none announced) is refused (413)
MAX_SWITCHES = 16               # switches one report may carry
SWITCH_NAME_MAX = 40            # characters of a switch name kept
SOURCE_MAX = 60                 # characters of `source` kept
SHUTTER_STATES = ("open", "closed", "opening", "closing", "moving", "error", "unknown")
REFUSED_LOG_EVERY_SEC = 600.0   # a refused key: the first is logged, then one line per 10 min

# Why a dome's values are unknown (state block `why`; the page words them)
NO_REPORT, STALE, NOT_CURRENT, ERROR = "no report yet", "stale", "not current", "error"


class ReportError(ValueError):
    """A report that does not meet the format. str() names the field."""


def _no_constants(name):
    raise ValueError("%s is not allowed" % name)


def parse_report(body: bytes) -> dict:
    """The report in `body` as {"shutter", "current", "rain_wet", "switches": [(name, on)], "source"},
    texts cleaned and cut; ReportError naming the field otherwise. Keys not in the format
    are ignored, so the format can grow."""
    try:
        obj = json.loads(body.decode("utf-8"), parse_constant=_no_constants)
    except UnicodeDecodeError:
        raise ReportError("body: not UTF-8") from None
    except (ValueError, RecursionError):
        raise ReportError("body: not valid JSON (NaN and Infinity are not allowed)") from None
    if not isinstance(obj, dict):
        raise ReportError("body: must be a JSON object")
    shutter = obj.get("shutter")
    if not isinstance(shutter, str) or shutter not in SHUTTER_STATES:
        raise ReportError("shutter: must be one of " + ", ".join(SHUTTER_STATES))
    current = obj.get("current")
    if not isinstance(current, bool):
        raise ReportError("current: must be true or false")
    rain_wet = obj.get("rain_wet")  # older senders omit this reading
    if rain_wet is not None and not isinstance(rain_wet, bool):
        raise ReportError("rain_wet: must be true, false or null")
    switches = obj.get("switches", [])
    if not isinstance(switches, list):
        raise ReportError("switches: must be a list")
    if len(switches) > MAX_SWITCHES:
        raise ReportError("switches: at most %d" % MAX_SWITCHES)
    parsed = []
    for i, sw in enumerate(switches):
        if not isinstance(sw, dict):
            raise ReportError("switches[%d]: must be an object" % i)
        name = sw.get("name")
        if isinstance(name, str):
            name = clean_text(name).strip()[:SWITCH_NAME_MAX].strip()
        if not isinstance(name, str) or not name:
            raise ReportError("switches[%d].name: must be a non-empty string" % i)
        if "on" not in sw or not (sw["on"] is None or isinstance(sw["on"], bool)):
            raise ReportError("switches[%d].on: must be true, false or null" % i)
        parsed.append((name, sw["on"]))
    source = obj.get("source")
    if source is not None and not isinstance(source, str):
        raise ReportError("source: must be a string")
    source = clean_text(source or "").strip()[:SOURCE_MAX].strip() or None
    return {"shutter": shutter, "current": current, "rain_wet": rain_wet,
            "switches": parsed, "source": source}


def unavailable_component(cfg, error) -> dict:
    """The `domes` block when the store is missing or broken: every configured dome,
    unknown, with why = "error" (the configuration decides which domes are listed)."""
    return {"enabled": True, "stale_after_s": int(cfg.DOME_STALE_SEC),
            "error": str(error)[:200],
            "domes": [{"n": d.n, "name": d.name, "shutter": "unknown", "known": False,
                       "rain_wet": None, "age_s": None, "why": ERROR, "source": None, "switches": []}
                      for d in cfg.DOMES]}


def rain_sensor_text(rain_wet) -> str:
    if rain_wet is True:
        return "Rain sensor WET"
    if rain_wet is False:
        return "Rain sensor DRY"
    return "Rain sensor UNKNOWN"


def _shown_switches(dome, report, known, show_all_max) -> dict:
    """The switch rows the page shows for `dome`, keyed by lower-case switch name:
    its list (or, for "all", every switch the latest report named, at most `show_all_max`),
    each with its value only while the report is known."""
    reported = {}
    for name, on in (report["switches"] if report else ()):
        reported.setdefault(name.lower(), (name, on))
    if dome.switches == "none":
        wanted = []
    elif dome.switches == "all":
        wanted = [(name, name) for name, _ in reported.values()][:show_all_max]
    else:
        wanted = list(dome.switches)
    rows = {}
    for name, label in wanted:
        got = reported.get(name.lower())
        value_known = known and got is not None and isinstance(got[1], bool)
        on = got[1] if value_known else None
        rows[name.lower()] = {
            "label": label, "on": on, "known": value_known,
            "warn": bool(value_known and on and name.lower() in dome.warn_on)}
    return rows


class DomeStore:
    """The latest accepted report of each configured dome, in memory, with its arrival
    time on the MONOTONIC clock: a step of the wall clock never makes a report fresh or
    stale. Thread-safe: the route stores, the evaluator reads."""

    def __init__(self, cfg):
        self._domes = tuple(cfg.DOMES)
        self._stale_sec = float(cfg.DOME_STALE_SEC)
        self._show_all = int(getattr(cfg, "DOME_SHOW_ALL_MAX", 8))
        self._keys = tuple((d, d.key.encode("ascii")) for d in self._domes)
        self._lock = threading.Lock()
        self._latest: dict = {}         # dome number -> (report, monotonic arrival)
        self._stale_logged: dict = {}   # dome number -> its "no report for ..." line is out
        self._refused = 0
        self._refused_unlogged = 0
        self._refused_logged_at = None  # monotonic

    @property
    def domes(self) -> tuple:
        return self._domes

    @property
    def stale_after_s(self) -> float:
        return self._stale_sec

    def match(self, presented: bytes):
        """The configured dome whose key `presented` is, or None. Each key is compared in
        constant time (hmac.compare_digest), and all of them are compared every time."""
        found = None
        for dome, key in self._keys:
            if hmac.compare_digest(presented, key) and found is None:
                found = dome
        return found

    def refused(self, mono=None) -> None:
        """Count a report refused for its key. The first is logged, then at most one line
        per REFUSED_LOG_EVERY_SEC with the count since. The key itself is never logged."""
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            self._refused += 1
            self._refused_unlogged += 1
            last = self._refused_logged_at
            if last is not None and 0 <= mono - last < REFUSED_LOG_EVERY_SEC:
                return
            count, self._refused_unlogged = self._refused_unlogged, 0
            self._refused_logged_at = mono
        if last is None:
            log.warning("dome report refused: its key matches no TTU_SAFETY_DOME<n>_KEY "
                        "(compare the dome's status-push-key with the Pi's ~/ttustatus.env)")
        else:
            log.warning("dome reports refused for their key: %d since the last such line",
                        count)

    @property
    def refused_count(self) -> int:
        with self._lock:
            return self._refused

    def accept(self, dome, report, mono=None) -> None:
        """Keep `report` as dome's latest; log its first report, a return after silence
        and each change of the roof or of a shown switch (daemon log only)."""
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            prev = self._latest.get(dome.n)
            self._latest[dome.n] = (report, mono)
            self._stale_logged[dome.n] = False
        label = "dome %d (%s)" % (dome.n, dome.name)
        now_view = self._view(dome, report)
        if prev is None:
            log.info("%s: first report — %s (from %s)", label, _describe(now_view),
                     report.get("source") or "an unnamed sender")
            return
        gap = mono - prev[1]
        if gap > self._stale_sec:
            log.info("%s: reporting again after %d min — %s", label, int(gap // 60),
                     _describe(now_view))
            return
        old_view = self._view(dome, prev[0])
        if old_view["roof"] != now_view["roof"]:
            log.info("%s: roof %s -> %s", label, old_view["roof"], now_view["roof"])
        # Reports may reorder or omit switches, and configured labels may be shared:
        # match each switch by its case-insensitive reported name.
        old_switches, new_switches = old_view["switches"], now_view["switches"]
        for name in dict.fromkeys((*old_switches, *new_switches)):
            lab, old = old_switches.get(name, (name, "unknown"))
            lab, new = new_switches.get(name, (lab, "unknown"))
            if old != new:
                log.info("%s: %s %s -> %s", label, lab, old, new)

    def _view(self, dome, report) -> dict:
        """What the page would show of `report` (for the change log lines)."""
        current = bool(report.get("current"))
        roof = report["shutter"].upper() if current else "UNKNOWN (no current reading)"
        rows = _shown_switches(dome, report, current, self._show_all)
        return {"roof": roof,
                "switches": {name: (r["label"], "unknown" if not r["known"]
                                    else ("on" if r["on"] else "off"))
                             for name, r in rows.items()}}

    def component(self, mono=None) -> dict:
        """The state file's `domes` block: every CONFIGURED dome, in order of its number,
        whether it has reported or not."""
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            latest = dict(self._latest)
        blocks = []
        for dome in self._domes:
            entry = latest.get(dome.n)
            report, age = (None, None) if entry is None else (
                entry[0], max(0.0, mono - entry[1]))
            if report is None:
                known, why = False, NO_REPORT
            elif age > self._stale_sec:
                known, why = False, STALE
                self._log_stale(dome, age)
            elif not report["current"]:
                known, why = False, NOT_CURRENT
            else:
                known, why = True, None
            blocks.append({
                "n": dome.n, "name": dome.name,
                "shutter": report["shutter"] if known else "unknown",
                "known": known,
                "rain_wet": report["rain_wet"] if known else None,
                "age_s": None if age is None else int(age),
                "why": why,
                "source": report["source"] if report else None,
                "switches": list(_shown_switches(dome, report, known, self._show_all).values()),
            })
        return {"enabled": True, "stale_after_s": int(self._stale_sec), "error": None,
                "domes": blocks}

    def _log_stale(self, dome, age) -> None:
        with self._lock:
            if self._stale_logged.get(dome.n):
                return
            self._stale_logged[dome.n] = True
        log.warning("dome %d (%s): no report for %d min — shown as UNKNOWN", dome.n,
                    dome.name, int(age // 60))


def _describe(view) -> str:
    parts = ["roof %s" % view["roof"]]
    parts += ["%s %s" % (lab, value) for lab, value in view["switches"].values()]
    return ", ".join(parts)
