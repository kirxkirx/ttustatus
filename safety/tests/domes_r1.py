"""R1 of the dome-status design: with no dome configured, the status page and /setup are
byte for byte what they were before the dome code existed.

The expected outputs in fixtures/domes_r1/ were rendered by THIS module from the code as
it stood before the dome change (ttustatus 7516afb), with subsequent NWS forecast display
changes applied to that baseline, from three fixed daemon states:
  a: a real evaluation, dew monitor and weather log on (SAFE)
  b: UNSAFE, with reasons and warnings, dew monitor off, no weather log
  c: an old daemon's minimal state, two hours stale (the page only: /setup always shows
     the running daemon's own, complete state)
Regenerate them only from such a commit:  python -m safety.tests.domes_r1 --regen
"""
import contextlib
import copy
import gzip
import json
import os
import sys
import time
from unittest.mock import MagicMock

for _m in ["numpy", "board", "adafruit_dht", "gps", "astropy", "astropy.units",
           "astropy.time", "astropy.coordinates", "astropy.utils", "astropy.utils.iers"]:
    sys.modules.setdefault(_m, MagicMock())

import make_status_page as msp        # noqa: E402  (must follow the sys.modules stubbing)
from safety import alpaca, config     # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "domes_r1")
STATES = ("a", "b", "c")
SETUP_STATES = ("a", "b")
RENDER_AT = 1795000030.0               # state a's own time + 30 s: a fresh state


class FixedMonitor:
    """What /setup needs of a SafetyMonitor: evaluate() returns the fixed state."""
    dew = None
    domes = None

    def __init__(self, state):
        self._state = state

    def evaluate(self):
        return copy.deepcopy(self._state)


def load_state(name):
    with open(os.path.join(FIXTURES, "state_%s.json" % name), encoding="utf-8") as f:
        return json.load(f)


@contextlib.contextmanager
def frozen_time():
    """time.time, and time.strftime / localtime / gmtime called without a time, all read
    RENDER_AT, in UTC (the page also reads the hour and the clock string directly)."""
    saved = (time.time, time.strftime, time.localtime, time.gmtime, os.environ.get("TZ"))
    real_strftime, real_localtime, real_gmtime = time.strftime, time.localtime, time.gmtime
    os.environ["TZ"] = "UTC"
    time.tzset()
    time.time = lambda: RENDER_AT
    time.strftime = lambda fmt, t=None: real_strftime(
        fmt, real_localtime(RENDER_AT) if t is None else t)
    time.localtime = lambda secs=None: real_localtime(RENDER_AT if secs is None else secs)
    time.gmtime = lambda secs=None: real_gmtime(RENDER_AT if secs is None else secs)
    try:
        yield
    finally:
        time.time, time.strftime, time.localtime, time.gmtime = saved[:4]
        if saved[4] is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved[4]
        time.tzset()


def render(name, workdir):
    """(status page bytes, /setup bytes or None) for state `name`. Call it inside
    frozen_time()."""
    saved = (msp.SAFETY_STATE_FILE, msp.HTML_VIA_SHM, msp.HTML_FILE)
    msp.SAFETY_STATE_FILE = os.path.join(FIXTURES, "state_%s.json" % name)
    msp.HTML_VIA_SHM = False
    msp.HTML_FILE = os.path.join(workdir, "status.html")
    try:
        msp.write_html(20.0, 40.0, "2026-11-18 21:00:00 CST", "chronyc failed", None, None,
                       None, None, {"available": False}, False,
                       {"mode": "disabled", "error": None}, None)
        with open(msp.HTML_FILE, "rb") as f:
            page = f.read()
    finally:
        msp.SAFETY_STATE_FILE, msp.HTML_VIA_SHM, msp.HTML_FILE = saved
    setup = None
    if name in SETUP_STATES:
        setup = alpaca.setup_page(FixedMonitor(load_state(name)), config).encode("utf-8")
    return page, setup


def expected(name, kind):
    with gzip.open(os.path.join(FIXTURES, "%s_%s.html.gz" % (kind, name)), "rb") as f:
        return f.read()


def _regen():
    import tempfile
    work = tempfile.mkdtemp(prefix="domes-r1-")
    with frozen_time():
        for name in STATES:
            page, setup = render(name, work)
            for kind, body in (("page", page), ("setup", setup)):
                if body is None:
                    continue
                path = os.path.join(FIXTURES, "%s_%s.html.gz" % (kind, name))
                # mtime=0: the same bytes give the same .gz file
                with open(path, "wb") as raw, gzip.GzipFile(
                        filename="", mode="wb", fileobj=raw, mtime=0) as f:
                    f.write(body)
                print("%s: %d bytes" % (path, len(body)))


if __name__ == "__main__" and "--regen" in sys.argv:
    _regen()
