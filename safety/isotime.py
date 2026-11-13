"""ISO 8601 times read alike on every Python: ``parse_iso`` and ``iso_ts``.

The layers read the times the feeds write (NWS alerts and forecasts, the hazard feeds,
the GLM and radar listings) and the times this daemon wrote itself into its state files.
``datetime.fromisoformat`` is no fit for that: it reads more from Python 3.11 on than on
3.9, and some of it wrongly. "10:53.5" is half a minute past 10:53 in ISO 8601, where 3.11+
read 10:53:00.5 (and 3.9 reads "10:53.500" so); 3.11+ also read basic-format, week and
ordinal dates that 3.9 refuses, and 3.9 reads "2026-11-12+00:00" as that date's midnight.
The Pi runs one Python, the CI another and a developer's box a third, and an alert's end
or a latch's start must be the same instant on each of them. So the grammar below is
matched here and the datetime built directly; ``fromisoformat`` is never called.

A port of vlbaweather's ``weather.util.parse_iso``, the same grammar and the same answers.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("ttu.safety.isotime")

# The distinct unreadable texts already logged: one warning line each, not one per poll of a
# feed that keeps writing the same odd time. Bounded, so a feed that writes a new odd text
# every poll cannot grow it without limit; past the bound the rest go unlogged.
_iso_warned = set()
_WARNED_MAX = 100

# The ISO 8601 times parse_iso reads: a date, alone or with a time of day to the hour, minute
# or second; a fraction only after the seconds, of up to 12 digits (cut to microseconds); an
# offset Z, +hh, +hhmm or +hh:mm. Also read: a lower-case "t" or "z", a blank for the "T",
# blanks around the text. re.ASCII: \d is 0-9 only, not the full-width or Arabic-Indic
# digits that int() would accept. Nothing else is a time, on any Python.
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})"
                  r"(?:[Tt ](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d{1,12}))?)?)?"
                  r"(?:([Zz])|([+-])(\d{2})(?::?(\d{2}))?)?)?\Z", re.ASCII)


def parse_iso(s, warn: bool = True) -> Optional[datetime]:
    """ISO 8601 time -> timezone-aware datetime, or None.

    Read: the forms ``_ISO`` lists, alike on every Python from 3.9 on. A text without an
    offset is taken as UTC, and a date alone as its midnight UTC: the feeds that write UTC
    that way would otherwise lose their times. None for anything else: no text (None, "",
    a number), another form, or a date or time that does not exist (25:00, February 30, an
    offset of 24 h or more, 75 offset minutes). With ``warn``, an unreadable text is logged
    once per distinct text (see ``_iso_warned``); an absent one (None, "", blanks) never is.
    """
    if not isinstance(s, str) or not s.strip():
        return None
    dt = _iso_time(s.strip())
    if dt is None and warn and s not in _iso_warned and len(_iso_warned) < _WARNED_MAX:
        _iso_warned.add(s)
        log.warning("not an ISO 8601 time: %.60r", s)
    return dt


def iso_ts(s, warn: bool = True) -> Optional[float]:
    """``parse_iso`` as epoch seconds (a float), for the callers that keep timestamps; None
    where ``parse_iso`` is None."""
    dt = parse_iso(s, warn=warn)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):    # the year 1 or 9999 pushed past by its offset
        return None


def _iso_time(s: str) -> Optional[datetime]:
    """``parse_iso`` of a stripped text, without its log line."""
    m = _ISO.match(s)
    if not m:
        return None
    year, month, day, hh, mm, ss, frac, _z, sign, oh, om = m.groups()
    tz = timezone.utc
    try:
        if sign:
            if int(om or 0) > 59:
                return None
            off = timedelta(hours=int(oh), minutes=int(om or 0))
            tz = timezone(-off if sign == "-" else off)
        return datetime(int(year), int(month), int(day), int(hh or 0), int(mm or 0), int(ss or 0),
                        int((frac or "")[:6].ljust(6, "0")), tzinfo=tz)
    except ValueError:                  # 25:00, February 30, the year 0, an offset of 24 h or more
        return None
