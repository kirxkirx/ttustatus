"""Connectivity watchdog — declare UNSAFE if the internet is down for too long.

Every CONN_PROBE_INTERVAL (day and night) a lightweight probe checks whether ANY of the
online service hosts is reachable. If NONE are reachable for CONN_OFFLINE_UNSAFE_SEC
(default 1 h), we've been blind to rain/lightning/forecast that long and can't trust
"safe" — so this component forces UNSAFE. It auto-resolves the moment a probe succeeds.

This is a HARD veto by design (unlike the per-service components, which fail to "unknown"):
it exists precisely to catch the case where every online layer is silently unavailable.
A response of ANY kind — even an HTTP error code — counts as "reachable" (it proves we got
to the server); only a connection/DNS/timeout failure counts as offline.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("ttu.safety.conn")
UA = {"User-Agent": "ttu-safety-connectivity/1.0"}


def _reachable(url, timeout):
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA),
                               timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True          # got an HTTP response => the host is reachable => internet up
    except Exception:
        return False


class ConnectivityWatch:
    def __init__(self, cfg, start_ts=None):
        self.cfg = cfg
        self._lock = threading.Lock()
        # grace: start the clock at daemon start, so a fresh daemon has a full window before
        # it could declare unsafe (matches "unreachable for an hour").
        self._last_ok = start_ts if start_ts is not None else time.time()
        self._last_probe_ts = None
        self._online = True          # optimistic until the first probe result

    def probe_once(self, now=None):
        """Try each host; the first that answers marks us online. Returns True if reachable."""
        if now is None:
            now = time.time()
        ok = False
        for url in self.cfg.CONN_PROBE_URLS:
            if _reachable(url, self.cfg.CONN_PROBE_TIMEOUT):
                ok = True
                break
        with self._lock:
            self._last_probe_ts = now
            self._online = ok
            if ok:
                self._last_ok = now
        return ok

    def component(self, now=None):
        if now is None:
            now = time.time()
        with self._lock:
            offline_sec = now - self._last_ok
            online = self._online
            probed = self._last_probe_ts is not None
        offline_too_long = offline_sec > self.cfg.CONN_OFFLINE_UNSAFE_SEC
        return {
            "safe": not offline_too_long,
            "online": online,
            "offline_sec": round(offline_sec),
            "offline_min": round(offline_sec / 60),
            "threshold_sec": self.cfg.CONN_OFFLINE_UNSAFE_SEC,
            "probed": probed,
            "source": "internet reachability probe",
        }


def unavailable_component(cfg):
    """When the watchdog is disabled — never vetoes."""
    return {
        "safe": True, "online": True, "offline_sec": 0, "offline_min": 0,
        "threshold_sec": cfg.CONN_OFFLINE_UNSAFE_SEC, "probed": False,
        "source": "internet reachability probe (disabled)",
    }
