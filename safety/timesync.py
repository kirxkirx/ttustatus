"""System-clock check: device 0 is UNSAFE until chrony is synchronised to a real time
source — the GPS (its reference clock) or an NTP server.

Why. Everything the monitor decides leans on the system clock: the sun's altitude (the
status page computes it from the clock), which radar frame and which GOES granules are
"now", the age of every observation, how long a latch still has to run. A Pi that boots
with a wrong clock (the 2026-08 incident: a "March" RTC in August; fake-hwclock restores
only the last shutdown time) puts the sun below the horizon at noon and asks the archives
for another day's weather until chrony sets the clock. The radar and GLM layers refuse
data whose server Date disagrees with the clock (servertime.py), but a refused layer only
reads "unavailable", which never vetoes — so device 0 could read SAFE in daylight. chrony
knows whether the clock is right; until it says so, device 0 says UNSAFE.

What counts as synchronised (``chronyc -n -c tracking``, one CSV line):
  * a Reference ID that is a real source: not 00000000 (no source at all) and not
    7F7F0101 — the "local" reference chrony uses when it is told to serve time from its
    own clock (``local stratum N``), i.e. the Pi trusting itself, not the GPS or a server;
  * a stratum of 1..15 and a leap status other than "Not synchronised";
  * chrony's own bound on the clock's error — |system time offset| + root delay / 2 +
    root dispersion — of at most CLOCK_MAX_ERROR_SEC (chrony can report a source while it
    is still slewing a large error away, e.g. when it is not allowed to step the clock).

Failing safe: chronyc missing, chronyd not running ("506 Cannot talk to daemon"), a
timeout or output that cannot be read all count as NOT confirmed. Two things keep that
from closing the dome for no reason on a night when the clock is fine:
  * GRACE. Once the clock has been confirmed in this run of the daemon, a failed or
    negative check is tolerated for CLOCK_GRACE_SEC (default 10 min): enough for chronyd
    to restart after a package upgrade and synchronise again, far too short for a clock
    that was right to drift by anything that matters here (milliseconds). A clock STEP
    (seen by the evaluator's step detector) ends the grace at once: the clock just changed,
    so it has to be confirmed anew.
  * The check runs in its own thread every CLOCK_CHECK_SEC (default 30 s); evaluate() and
    IsSafe never wait for chronyc. A result older than three intervals counts as none.

All timing here is MONOTONIC — the wall clock is exactly what is in question. Nothing is
persisted: a restarted daemon starts unconfirmed, and its first check, made at once,
settles it within a second when chrony is synchronised.
"""
from __future__ import annotations

import logging
import math
import subprocess
import threading
import time

from .servertime import describe       # "45 s", "9 min": a grace is not floored to "0 min"

log = logging.getLogger("ttu.safety.clock")

NO_REFID = "00000000"          # chrony has no reference at all
LOCAL_REFID = "7F7F0101"       # chrony's "local" reference: the Pi's own clock
RUN_TIMEOUT_SEC = 10           # one chronyc call (it answers in milliseconds normally)
STATES = ("synchronised", "grace", "unconfirmed", "disabled")


def run_chronyc(cfg) -> str:
    """``chronyc -n -c tracking`` -> its output. Raises OSError when chronyc cannot be run
    (FileNotFoundError: not installed), subprocess.TimeoutExpired, or RuntimeError with
    chronyc's own message when it fails (e.g. "506 Cannot talk to daemon": chronyd is not
    running). -n: numeric addresses, no DNS lookup that could hang; -c: CSV."""
    cmd = [str(getattr(cfg, "CLOCK_CHRONYC", "chronyc") or "chronyc"), "-n", "-c", "tracking"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT_SEC)
    if p.returncode != 0:
        lines = ((p.stdout or "") + "\n" + (p.stderr or "")).strip().splitlines()
        if p.returncode == 127 and not lines:
            # "command not found". Python raises FileNotFoundError itself on the Pi, but
            # where the exec error cannot reach it (the Raspberry Pi OS image under QEMU in
            # CI) the child just exits with 127: the same fact, said the same way
            raise FileNotFoundError(cmd[0])
        raise RuntimeError(lines[0].strip() if lines else "exit status %d" % p.returncode)
    return p.stdout


def _num(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def describe_error(seconds) -> str:
    """An error bound for people: "11 µs", "52 ms", "1.5 s" ("?" when not a number)."""
    v = _num(seconds)
    if v is None:
        return "?"
    v = abs(v)
    if v < 1e-3:
        return "%.0f µs" % (v * 1e6)
    if v < 1.0:
        return "%.0f ms" % (v * 1e3)
    return "%.3g s" % v


def parse_tracking(text, max_error) -> dict:
    """One ``chronyc -c tracking`` line -> {"ok", "why", "refid", "reference", "stratum",
    "offset_s", "max_error_s", "leap"}. ok is True only for synchronisation to a real
    source within ``max_error`` seconds (see the module docstring); "why" says what is
    missing otherwise. The CSV fields: Reference ID, its name or address, stratum, ref
    time, system time offset, last offset, RMS offset, frequency, residual frequency,
    skew, root delay, root dispersion, update interval, leap status."""
    line = next((ln for ln in str(text or "").splitlines() if ln.strip()), "")
    f = [x.strip() for x in line.split(",")]
    if len(f) < 14:
        return {"ok": False, "why": "chronyc gave no tracking report (%r)" % line[:80]}
    refid, name, leap = f[0].upper(), f[1], f[13]
    try:
        stratum = int(f[2])
    except ValueError:
        stratum = None
    offset, rdelay, rdisp = _num(f[4]), _num(f[10]), _num(f[11])
    out = {"refid": refid, "reference": name or refid, "stratum": stratum, "offset_s": offset,
           "max_error_s": None, "leap": leap}
    if stratum is None or offset is None or rdelay is None or rdisp is None:
        why = "chronyc's tracking report is unreadable (%r)" % line[:80]
    elif leap.lower().startswith("not sync") or refid == NO_REFID or stratum <= 0:
        why = "chrony is not synchronised to any time source yet"
    elif refid == LOCAL_REFID:
        why = ("chrony is synchronised only to this Pi's own clock (its 'local' reference), "
               "not to the GPS or an NTP server")
    elif stratum >= 16:
        why = "chrony's time source is itself unsynchronised (stratum %d)" % stratum
    else:
        err = abs(offset) + abs(rdelay) / 2.0 + abs(rdisp)
        out["max_error_s"] = err
        why = (None if err <= float(max_error) else
               "chrony bounds the clock's error at %.3g s, more than the %g s allowed (still "
               "correcting a large offset?)" % (err, float(max_error)))
    out["ok"] = why is None
    out["why"] = why
    return out


class ClockSyncWatch:
    """The clock check (see the module docstring). check_now() runs in the daemon's clock
    thread; component() is cheap and called by every evaluation."""

    def __init__(self, cfg, eventlog, runner=None):
        self.cfg = cfg
        self.log = eventlog
        self._runner = runner or run_chronyc     # (cfg) -> chronyc output; tests replace it
        self._lock = threading.Lock()
        self._last = None               # the last check's result, with its monotonic "mono"
        self._confirmed = None          # monotonic time of the last check that confirmed sync
        self._recheck = threading.Event()   # a clock step asks for a check at once
        self._logged = None             # the state last written to the logs
        # Bumped by every clock step. A check whose chronyc call began before a step must
        # not store its answer after it: that answer speaks of the clock before the step
        # (and would log a spurious unconfirmed/confirmed pair besides). It is dropped;
        # the check the step asked for decides.
        self._generation = 0

    # -- the clock thread ----------------------------------------------------------------
    def check_now(self, mono=None) -> dict:
        """Ask chrony once and keep the answer; changes of state go to the logs. An answer
        overtaken by a clock step (see _generation) is returned but not kept."""
        mono = time.monotonic() if mono is None else mono
        with self._lock:
            generation = self._generation
        try:
            res = parse_tracking(self._runner(self.cfg), self.cfg.CLOCK_MAX_ERROR_SEC)
        except FileNotFoundError:
            res = {"ok": False, "why": "chronyc is not installed (apt install chrony)"}
        except subprocess.TimeoutExpired:
            res = {"ok": False, "why": "chronyc did not answer within %d s" % RUN_TIMEOUT_SEC}
        except Exception as e:          # noqa: BLE001 - any failure is "not confirmed"
            text = str(e)[:160] or type(e).__name__
            res = {"ok": False, "why": "cannot ask chrony (%s)" % text}
        res["mono"] = mono
        with self._lock:
            if generation != self._generation:
                return res              # the clock stepped meanwhile: not this answer
            self._last = res
            if res["ok"]:
                self._confirmed = mono
        self._log_change(mono)
        return res

    def wait(self, stop, seconds) -> None:
        """The clock thread's pause between checks: ``seconds``, cut short by a clock step
        (clock_stepped) or by ``stop`` (a threading.Event) being set."""
        end = time.monotonic() + max(0.0, float(seconds))
        while not stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return
            if self._recheck.wait(min(left, 1.0)):
                self._recheck.clear()
                return

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """The wall clock just stepped (NTP setting a wrong boot clock, or someone else).
        Whatever confirmed the clock before no longer covers it: the last result and the
        grace are dropped — device 0 reads UNSAFE until the check asked for here confirms
        the clock again (within a second when chrony made the step itself)."""
        with self._lock:
            self._last = None
            self._confirmed = None
            self._generation += 1
        self._recheck.set()

    def _log_change(self, mono) -> None:
        c = self.component(mono)
        state, prev = c["state"], self._logged
        if state == prev:
            return
        self._logged = state
        if state == "unconfirmed":
            log.error("SYSTEM CLOCK NOT CONFIRMED by chrony: %s — device %d is UNSAFE until "
                      "chrony is synchronised to the GPS or an NTP server", c["why"],
                      self.cfg.DEVICE_NUMBER)
            self.log.record("CLOCK-SYNC", reason=str(c["why"])[:200],
                            result="device %d unsafe until chrony is synchronised"
                            % self.cfg.DEVICE_NUMBER)
        elif state == "grace":
            log.warning("chrony could not confirm the clock (%s) — tolerated for up to %s, "
                        "as it was synchronised before", c["why"],
                        describe(self.cfg.CLOCK_GRACE_SEC))
        elif state == "synchronised":
            text = "system clock synchronised to %s (stratum %s, error at most %s)" % (
                c["reference"], c["stratum"], describe_error(c["max_error_s"]))
            if prev == "unconfirmed":
                log.warning("%s — the clock check no longer vetoes", text)
                self.log.record("CLOCK-SYNC", reason=text, result="clock confirmed")
            else:
                log.info(text)

    # -- the evaluator ---------------------------------------------------------------------
    def component(self, mono=None) -> dict:
        mono = time.monotonic() if mono is None else mono
        cfg = self.cfg
        with self._lock:
            last, confirmed = self._last, self._confirmed
        age = None if last is None else mono - last["mono"]
        # three missed checks (plus one chronyc timeout) and the last answer is no answer
        fresh = age is not None and 0 <= age <= 3.0 * cfg.CLOCK_CHECK_SEC + RUN_TIMEOUT_SEC
        since = None if confirmed is None else mono - confirmed
        if fresh and last["ok"]:
            state, why = "synchronised", None
        else:
            if last is None:
                why = "the clock has not been checked yet"
            elif not fresh:
                why = "no clock check for %d s" % max(0, round(age))
            else:
                why = last.get("why") or "chrony did not confirm the clock"
            in_grace = since is not None and 0 <= since <= cfg.CLOCK_GRACE_SEC
            state = "grace" if in_grace else "unconfirmed"
        shown = last if last is not None else {}
        return {
            "safe": state != "unconfirmed",
            "enabled": True,
            "state": state,
            "synchronised": state == "synchronised",
            "why": why,
            "reference": shown.get("reference"),
            "stratum": shown.get("stratum"),
            "offset_s": shown.get("offset_s"),
            "max_error_s": shown.get("max_error_s"),
            "leap": shown.get("leap"),
            "checked_age_s": max(0, round(age)) if age is not None else None,
            "confirmed_age_s": max(0, round(since)) if since is not None else None,
            "grace_left_s": (max(0, round(cfg.CLOCK_GRACE_SEC - since))
                             if state == "grace" else None),
            "max_error_limit_s": cfg.CLOCK_MAX_ERROR_SEC,
            "check_sec": cfg.CLOCK_CHECK_SEC,
            "grace_sec": cfg.CLOCK_GRACE_SEC,
        }


def unavailable_component(cfg) -> dict:
    """The clock check switched off (TTU_SAFETY_CLOCK_SYNC=0): no veto, said as much."""
    return {
        "safe": True, "enabled": False, "state": "disabled", "synchronised": False,
        "why": "the clock check is switched off (TTU_SAFETY_CLOCK_SYNC=0)",
        "reference": None, "stratum": None, "offset_s": None, "max_error_s": None,
        "leap": None, "checked_age_s": None, "confirmed_age_s": None, "grace_left_s": None,
        "max_error_limit_s": getattr(cfg, "CLOCK_MAX_ERROR_SEC", None),
        "check_sec": getattr(cfg, "CLOCK_CHECK_SEC", None),
        "grace_sec": getattr(cfg, "CLOCK_GRACE_SEC", None),
    }
