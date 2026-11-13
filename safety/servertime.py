"""The wrong-clock guard: a server's ``Date`` header against this host's clock.

The radar archives (NOAA's MRMS bucket, IEM) and the GOES GLM bucket keep years of data,
and every request names the time it wants from THIS host's clock. A Pi that boots with a
wrong clock (the 2026-08 incident: a "March" RTC in August) would therefore fetch that
date's archived frames and granules and read them as current until chrony steps the
clock. Every HTTP reply carries the server's own time, so the layers compare it with
``time.time()`` and refuse the data while the two disagree by more than a tolerance.
"""
from __future__ import annotations

import email.utils
import time
from datetime import timezone


def skew_s(headers, now=None):
    """Seconds by which the server's clock is AHEAD of this host's (negative: behind),
    from the reply's ``Date`` header plus its ``Age`` when a cache answered; None when the
    reply carries no usable ``Date``."""
    if headers is None:
        return None
    try:
        date = headers.get("Date")
    except Exception:                   # noqa: BLE001 - an odd headers object: no verdict
        return None
    if not isinstance(date, str) or not date.strip():
        return None
    try:
        t = email.utils.parsedate_to_datetime(date.strip())
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    try:
        age = int(str(headers.get("Age") or "0").strip())
    except (TypeError, ValueError, AttributeError):
        age = 0
    if not 0 <= age <= 366 * 86400:     # nonsense (a cache keeps nothing that long): ignored
        age = 0
    try:
        return t.timestamp() + age - (time.time() if now is None else now)
    except (OverflowError, ValueError, TypeError):
        return None


def describe(seconds) -> str:
    """A duration in words, rounded to its last unit: "45 s", "12 min", "5 h 10 min",
    "158 d 3 h" (a Date header has whole seconds, so 158 days is rarely 158 d exactly)."""
    s = abs(float(seconds))
    if s < 119.5:
        return "%d s" % round(s)
    m = int(round(s / 60.0))
    if m < 120:
        return "%d min" % m
    if m < 48 * 60:
        h, m = divmod(m, 60)
        return "%d h %d min" % (h, m) if m else "%d h" % h
    d, h = divmod(int(round(s / 3600.0)), 24)
    return "%d d %d h" % (d, h) if h else "%d d" % d


def clock_phrase(skew, whose="the server's") -> str:
    """How this host's clock stands against a server's: "this host's clock is 158 d 3 h
    behind the server's" (skew = server minus host, as ``skew_s`` returns it)."""
    return "this host's clock is %s %s %s" % (
        describe(skew), "behind" if skew > 0 else "ahead of", whose)
