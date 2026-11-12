"""The weather log: what the daemon knows about the weather, as CSV, for later analysis.

Its purpose is tuning the dew monitor (safety/dew.py): for every moment of a night it
records the numbers that verdict was made from — the WU stations' temperature, dew point
and wind, the NWS forecast, the radar state, the enclosure sensor — next to what both
Alpaca monitors said. Nothing is fetched for it: these are data the daemon already has.

Two kinds of rows:
  * weather  — a summary row every WEATHER_LOG_INTERVAL_SEC, plus one at once whenever
               the verdict of device 0 or device 1 changes (column `event` says which);
  * stations — one row per WU station per WU poll (so one can see which stations are
               representative of the site). WU is polled at night only.

SD-CARD WEAR comes first — the Pi runs from an SD card:
  * Rows are appended to files in RAM (WEATHER_LOG_RAM_DIR, /dev/shm). observe() runs
    inside the safety evaluation and touches nothing else, ever.
  * A separate thread copies the RAM rows to the SD card in ONE append per monthly file,
    once every WEATHER_LOG_FLUSH_HOURS (24) and only in daytime, plus when the daemon is
    stopped in an orderly way (systemctl stop/restart, reboot) and when the RAM files
    grow unexpectedly large. A power cut loses the rows not copied yet, nothing else.
  * /dev/shm survives a daemon restart, so a crash or restart loses nothing either.

The archive (WEATHER_LOG_DIR, inside the web root, so the status page can link to it):
    weather_2026-11.csv   stations_2026-11.csv        one pair per month (UTC)
    weather_current.csv   stations_current.csv        symlinks to the RAM files
Every file starts with a header line. If a later version changes the columns in the
middle of a month, that month continues in weather_2026-11_b.csv (then _c ...), so every
file stays one consistent table.

Copying is all-or-nothing and never duplicates rows, even if the daemon is killed in the
middle. The rows to be archived are first moved aside inside the RAM directory
(pending/), a marker records how long each archive file was, the rows are appended and
synced, and only then is pending/ renamed to done/ — the commit. A run that finds marker
+ pending/ knows the previous one did not commit: it cuts the archive files back to the
recorded lengths and repeats the append. (After a reboot there is nothing to repeat: the
RAM directory is gone. A row torn by a power cut at the end of an archive file is cut off
before the next append.)

Growth is bounded: when the archive exceeds WEATHER_LOG_MAX_MB, the oldest months are
deleted — never the current one.

Units are in the column names: temperatures in degrees C, wind in mph, the rain rate in
inches per hour as WU reports it.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from . import dew as dew_mod, nws_forecast

log = logging.getLogger("ttu.safety.weatherlog")

KINDS = ("weather", "stations")

SUMMARY_COLUMNS = (
    "utc", "local", "event", "sun_alt_deg",
    # the two Alpaca verdicts: device 0 (safe to observe), device 1 (rain sensor may be
    # ignored: dew likely) — 1 / 0, blank = that monitor is off
    "safe0", "safe1",
    # what device 1 decided on: medians of the WU stations with a fresh observation
    "spread_c", "wind_mph", "gust_mph", "temp_c", "dewpoint_c", "rh_pct",
    "stations_used", "stations_wind", "stations_live", "stations_total",
    # device 1's individual checks: 1 = passes, 0 = fails, blank = no data
    "wind_calm", "check_spread", "check_wind", "check_cloud", "check_precip",
    # NWS forecast for the hour containing this row, and the next hour
    "fc_cloud_pct", "fc_cloud_next_pct", "fc_precip_pct", "fc_precip_next_pct",
    "fc_thunder_pct", "fc_temp_c", "fc_dewpoint_c", "fc_rh_pct", "fc_wind_mph",
    "fc_gust_mph", "fc_age_min",
    "rain_latched", "rain_stations",
    "radar_available", "radar_in_ring", "radar_unconfirmed", "radar_nearest_km",
    "glm_latched", "glm_nearest_km",
    # the DHT sensor INSIDE the enclosure (not outdoor air)
    "encl_temp_c", "encl_rh_pct",
    "reasons0", "reasons1",
)

STATION_COLUMNS = (
    "utc", "local", "station", "distance_km", "state", "obs_age_min",
    "temp_c", "dewpoint_c", "spread_c", "rh_pct", "wind_mph", "gust_mph", "precip_in_hr",
)

HEADERS = {"weather": ",".join(SUMMARY_COLUMNS), "stations": ",".join(STATION_COLUMNS)}

_ARCHIVE_RE = re.compile(r"^(weather|stations)_(\d{4}-\d{2})(?:_([b-z]))?\.csv$")
_ROW_MONTH_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}T")
_SUFFIXES = ("",) + tuple("_" + c for c in "bcdefghijklmnopqrstuvwxyz")
# After a failed archive run, wait this long before trying again (a broken SD card or a
# read-only web root must not be hammered once a minute).
RETRY_SEC = 600.0
# A problem with the RAM files is logged when it changes, else at most this often.
ERROR_LOG_SEC = 600.0
_MB = 1024 * 1024


def _num(x, digits=1):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x:
        return ""
    if x in (float("inf"), float("-inf")):
        return ""
    return "%.*f" % (digits, x)


def _flag(x):
    return "" if x is None else ("1" if x else "0")


def _text(x):
    """One line of text for a CSV field (a line break would split the row in two)."""
    return " ".join(str(x).split())


def _iso_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class WeatherLog:
    def __init__(self, cfg, eventlog=None):
        self.cfg = cfg
        self._events = eventlog
        self.dir = cfg.WEATHER_LOG_DIR                # the archive, on the SD card
        self.ram = cfg.WEATHER_LOG_RAM_DIR            # RAM
        # _lock guards the RAM files and the bookkeeping; it is held for RAM work only,
        # never across SD I/O — observe() takes it inside the safety evaluation.
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()           # one archive run at a time (SD I/O)
        self._last_row_ts = None
        self._last_verdicts = None                    # (safe0, safe1) of the last row
        self._last_station_poll_ts = None
        self._last_flush_ts = None                    # wall time of the last archive run
        self._created_ts = time.time()                # when this daemon started logging
        self._last_attempt = None                     # monotonic time of the last attempt
        self._error = None                            # why the last archive run failed
        self._ram_error = None
        self._ram_error_logged = (None, 0.0)
        self._dropped = 0                             # rows lost to the RAM limit
        self._cur_rows = {k: 0 for k in KINDS}
        self._pend_rows = {k: 0 for k in KINDS}
        self._archives: list = []                     # listing for the status page
        self._total_bytes = 0
        self._dir_ready = False
        self._load_ram_state()

    # -- paths ------------------------------------------------------------------------
    def _cur(self, kind):
        return os.path.join(self.ram, "%s_current.csv" % kind)

    def _pend_dir(self):
        return os.path.join(self.ram, "pending")

    def _pend(self, kind):
        return os.path.join(self._pend_dir(), "%s.csv" % kind)

    def _done_dir(self):
        return os.path.join(self.ram, "done")

    def _marker(self):
        return os.path.join(self.ram, "flush_marker.json")

    def _meta(self):
        return os.path.join(self.ram, "meta.json")

    def _kinds(self):
        return KINDS if self.cfg.WEATHER_LOG_STATIONS else ("weather",)

    # -- start-up -----------------------------------------------------------------------
    @staticmethod
    def _count_rows(path) -> int:
        try:
            with open(path, "rb") as f:
                lines = f.read().count(b"\n")
        except OSError:
            return 0
        return max(0, lines - 1)                      # minus the header

    def _load_ram_state(self) -> None:
        """Pick up the RAM files a previous daemon process left behind (they survive a
        restart, not a reboot). RAM only — no SD access here."""
        try:
            os.makedirs(self.ram, exist_ok=True)
        except OSError as e:
            self._note_ram_error("cannot create %s: %s" % (self.ram, e))
            return
        for name in self._pending_files():
            self._pend_rows[name.split(".")[0]] += self._count_rows(
                os.path.join(self._pend_dir(), name))
        for kind in KINDS:
            cur = self._cur(kind)
            self._cur_rows[kind] = self._count_rows(cur)
            try:
                # errors="replace": a write cut short on a full RAM disk can end in half a
                # character, and one such byte must not stop the log from starting
                with open(cur, encoding="utf-8", errors="replace") as f:
                    first = f.readline().rstrip("\r\n")
            except OSError:
                continue
            if first and first != HEADERS[kind]:
                # written by a version with other columns: keep it apart, as a file of
                # its own that the next archive run copies under its own header
                self._set_aside_locked(kind)
        try:
            with open(self._meta(), encoding="utf-8") as f:
                ts = json.load(f).get("last_flush_ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                self._last_flush_ts = float(ts)
        except (OSError, ValueError, AttributeError):
            pass

    def _pending_files(self) -> list:
        """The files waiting in pending/: '<kind>.csv', or '<kind>.<tag>.csv' for a file
        that was set aside."""
        try:
            names = sorted(os.listdir(self._pend_dir()))
        except OSError:
            return []
        return [n for n in names if n.endswith(".csv") and n.split(".")[0] in KINDS]

    def _set_aside_locked(self, kind) -> None:
        """Move a current file with foreign columns to pending/, under a name of its
        own: rows with other columns must never be appended below its header."""
        try:
            os.makedirs(self._pend_dir(), exist_ok=True)
            target = os.path.join(self._pend_dir(), "%s.old-%d.csv" % (kind, time.time_ns()))
            os.replace(self._cur(kind), target)
            self._pend_rows[kind] += self._cur_rows[kind]
            self._cur_rows[kind] = 0
        except OSError as e:
            self._note_ram_error("cannot set aside %s: %s" % (self._cur(kind), e))

    def prepare(self) -> None:
        """Once at start-up, from the weather-log thread (it touches the SD card): the
        archive directory with its two symlinks, an interrupted archive run, the file
        listing for the page, and the time of the last archive run."""
        with self._flush_lock:
            try:
                self._prepare_dir()
                self._recover()
                err = None
            except Exception as e:  # noqa: BLE001 — the log must never stop the daemon
                err = "%s: %s" % (type(e).__name__, e)
                log.error("weather log: archive directory %s is not usable (%s) — rows "
                          "stay in RAM (%s) until it is", self.dir, err, self.ram)
            self._refresh_listing()
            with self._lock:
                self._error = err
                if self._last_flush_ts is None:
                    # after a reboot the RAM note is gone: the newest archive file tells
                    newest = 0.0
                    for a in self._archives:
                        newest = max(newest, a.get("mtime") or 0.0)
                    self._last_flush_ts = newest or None

    def _prepare_dir(self) -> None:
        if self._dir_ready:
            return
        os.makedirs(self.dir, exist_ok=True)
        if os.path.realpath(self.dir) != os.path.realpath(self.ram):
            for kind in KINDS:
                link = os.path.join(self.dir, "%s_current.csv" % kind)
                target = self._cur(kind)
                if os.path.islink(link):
                    if os.readlink(link) == target:
                        continue
                    os.remove(link)
                elif os.path.exists(link):
                    os.remove(link)
                os.symlink(target, link)
        self._dir_ready = True

    # -- rows (RAM only) ----------------------------------------------------------------
    def _note_ram_error(self, msg) -> None:
        self._ram_error = msg
        last, when = self._ram_error_logged
        mono = time.monotonic()
        if msg != last or mono - when >= ERROR_LOG_SEC:
            self._ram_error_logged = (msg, mono)
            log.warning("weather log: %s", msg)

    def _ram_bytes_locked(self) -> int:
        total = 0
        paths = [self._cur(kind) for kind in KINDS]
        paths += [os.path.join(self._pend_dir(), n) for n in self._pending_files()]
        for path in paths:
            try:
                total += os.path.getsize(path)
            except OSError:
                pass
        return total

    def _make_room_locked(self, incoming: int) -> None:
        """Keep the RAM files under WEATHER_LOG_RAM_MAX_MB by dropping the OLDEST rows:
        first the set waiting to be archived, then the older half of each current file
        (again and again, if that is still not enough)."""
        limit = self.cfg.WEATHER_LOG_RAM_MAX_MB * _MB
        if self._ram_bytes_locked() + incoming <= limit:
            return
        dropped = 0
        if os.path.isdir(self._pend_dir()) and not self._flush_lock.locked():
            dropped += sum(self._pend_rows.values())
            shutil.rmtree(self._pend_dir(), ignore_errors=True)
            self._pend_rows = {k: 0 for k in KINDS}
        for _ in range(40):
            if self._ram_bytes_locked() + incoming <= limit:
                break
            before = dropped
            for kind in KINDS:
                dropped += self._drop_older_half_locked(kind)
            if dropped == before:
                break                   # nothing left to drop
        self._dropped += dropped
        self._note_ram_error("RAM files reached %g MB and the archive (%s) is not being "
                             "written — dropped the %d oldest unarchived row(s)"
                             % (self.cfg.WEATHER_LOG_RAM_MAX_MB, self.dir, dropped))

    def _drop_older_half_locked(self, kind) -> int:
        """Remove the older half of a current file's rows (all of them if only one is
        left). Returns how many rows went."""
        path, rows = self._cur(kind), self._cur_rows[kind]
        if rows <= 0:
            return 0
        try:
            with open(path, "rb") as f:
                lines = f.read().split(b"\n")
            body = [ln for ln in lines[1:] if ln]
            keep = body[len(body) - len(body) // 2:] if len(body) > 1 else []
            with open(path + ".tmp", "wb") as f:
                f.write(lines[0] + b"\n" + b"".join(ln + b"\n" for ln in keep))
            os.replace(path + ".tmp", path)
        except OSError:
            keep = []
            for leftover in (path + ".tmp", path):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        self._cur_rows[kind] = len(keep)
        return rows - len(keep)

    def _append_locked(self, kind, rows) -> bool:
        if not rows:
            return True
        path = self._cur(kind)
        try:
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            for row in rows:
                writer.writerow(row)
            data = buf.getvalue()
            # (may remove this very file: decide about the header only afterwards)
            self._make_room_locked(len(data.encode("utf-8")) + len(HEADERS[kind]) + 1)
            os.makedirs(self.ram, exist_ok=True)
            prefix = ""
            try:
                with open(path, "r+b") as f:
                    size = f.seek(0, os.SEEK_END)
                    if size:
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":
                            # The previous write was cut short (RAM disk full). Remove
                            # the torn piece NOW: left in the middle of the file it could
                            # pass for a complete row (cut in its last text field).
                            back = min(size, 65536)
                            f.seek(size - back)
                            cut = f.read(back).rfind(b"\n")
                            if cut >= 0 or back == size:
                                size = size - back + cut + 1 if cut >= 0 else 0
                                f.truncate(size)
                            else:
                                # no line break in the last 64 KiB: not a torn row of
                                # ours. Leave it (the archive run skips what it cannot
                                # read) and start on a new line.
                                prefix = "\n"
                    if size == 0:
                        prefix = HEADERS[kind] + "\n"
            except OSError:
                prefix = HEADERS[kind] + "\n"       # no such file yet
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(prefix + data)
            self._cur_rows[kind] += len(rows)
            if self._ram_error and not self._ram_error.startswith("RAM files reached"):
                self._ram_error = None
            return True
        except Exception as e:  # noqa: BLE001 — a full /dev/shm must not hurt IsSafe
            self._note_ram_error("cannot write %s: %s: %s" % (path, type(e).__name__, e))
            return False

    def _local(self, ts) -> str:
        dt = nws_forecast._local(datetime.fromtimestamp(ts, timezone.utc), self.cfg.LOCAL_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z").strip()

    def _summary_row(self, state, now, event, outlook, inputs) -> list:
        comp = state.get("components") or {}
        dew = state.get("dew") or {}
        checks = dew.get("checks") or {}
        sun, rain = comp.get("sun") or {}, comp.get("rain") or {}
        radar, glm = comp.get("radar") or {}, comp.get("glm") or {}
        outlook = outlook if isinstance(outlook, dict) and outlook.get("available") else {}
        this_hour = outlook.get("this_hour") or {}
        next_hour = outlook.get("next_hour") or {}
        age = outlook.get("age_s")
        inputs = inputs or {}
        dew_on = bool(dew.get("enabled"))
        raining = rain.get("raining")
        values = {
            "utc": _iso_utc(now), "local": self._local(now), "event": event,
            "sun_alt_deg": _num(sun.get("value_deg"), 2),
            "safe0": _flag(bool(state.get("is_safe"))),
            "safe1": _flag(bool(dew.get("is_safe"))) if dew_on else "",
            "spread_c": _num(dew.get("spread_c"), 2),
            "wind_mph": _num(dew.get("wind_mph")),
            "gust_mph": _num(dew.get("gust_mph")),
            "temp_c": _num(dew.get("temp_c")),
            "dewpoint_c": _num(dew.get("dewpoint_c")),
            "rh_pct": _num(dew.get("rh_pct"), 0),
            "stations_used": dew.get("stations_used", "") if dew_on else "",
            "stations_wind": dew.get("stations_wind", "") if dew_on else "",
            "stations_live": rain.get("stations_live", ""),
            "stations_total": rain.get("stations_total", ""),
            "wind_calm": _flag(dew.get("wind_calm")) if dew_on else "",
            "check_spread": _flag(checks.get("spread")),
            "check_wind": _flag(checks.get("wind")),
            "check_cloud": _flag(checks.get("cloud")),
            "check_precip": _flag(checks.get("precip")),
            "fc_cloud_pct": _num(this_hour.get("cloud_cover_pct"), 0),
            "fc_cloud_next_pct": _num(next_hour.get("cloud_cover_pct"), 0),
            "fc_precip_pct": _num(this_hour.get("precip_prob_pct"), 0),
            "fc_precip_next_pct": _num(next_hour.get("precip_prob_pct"), 0),
            "fc_thunder_pct": _num(this_hour.get("thunder_prob_pct"), 0),
            "fc_temp_c": _num(this_hour.get("temp_c")),
            "fc_dewpoint_c": _num(this_hour.get("dewpoint_c")),
            "fc_rh_pct": _num(this_hour.get("rh_pct"), 0),
            # the NWS grid gives km/h: logged in mph, like the station wind
            "fc_wind_mph": _num(dew_mod.mph_from_kmh(this_hour.get("wind_speed_kmh"))),
            "fc_gust_mph": _num(dew_mod.mph_from_kmh(this_hour.get("wind_gust_kmh"))),
            "fc_age_min": _num(age / 60.0, 0) if isinstance(age, (int, float)) else "",
            "rain_latched": _flag(rain.get("latched")),
            "rain_stations": len(raining) if isinstance(raining, list) else "",
            "radar_available": _flag(radar.get("available")),
            "radar_in_ring": _flag(radar.get("in_ring")),
            "radar_unconfirmed": _flag(radar.get("unconfirmed_echo")),
            "radar_nearest_km": _num(radar.get("nearest_km")),
            "glm_latched": _flag(glm.get("latched")),
            "glm_nearest_km": _num(glm.get("nearest_km")),
            "encl_temp_c": _num(inputs.get("temperature")),
            "encl_rh_pct": _num(inputs.get("humidity"), 0),
            "reasons0": _text(" | ".join(str(r) for r in state.get("reasons") or [])),
            "reasons1": _text(" | ".join(str(r) for r in dew.get("reasons") or [])),
        }
        return [values[c] for c in SUMMARY_COLUMNS]

    def _station_rows(self, poll_ts, results) -> list:
        utc, local = _iso_utc(poll_ts), self._local(poll_ts)
        rows = []
        for r in results:
            if not isinstance(r, dict) or not r.get("station"):
                continue
            temp_f, dewpt_f = r.get("temp_f"), r.get("dewpt_f")
            both = isinstance(temp_f, (int, float)) and isinstance(dewpt_f, (int, float))
            values = {
                "utc": utc, "local": local, "station": _text(r.get("station")),
                "distance_km": _num(r.get("distance_km")),
                "state": _text(r.get("state") or ""),
                "obs_age_min": r.get("age_min", ""),
                "temp_c": _num(dew_mod.c_from_f(temp_f)
                               if isinstance(temp_f, (int, float)) else None),
                "dewpoint_c": _num(dew_mod.c_from_f(dewpt_f)
                                   if isinstance(dewpt_f, (int, float)) else None),
                "spread_c": _num(dew_mod.delta_c_from_delta_f(temp_f - dewpt_f)
                                 if both else None, 2),
                "rh_pct": _num(r.get("humidity_pct"), 0),
                "wind_mph": _num(r.get("wind_mph")),
                "gust_mph": _num(r.get("gust_mph")),
                "precip_in_hr": _num(r.get("precip_in_hr"), 2),
            }
            rows.append([values[c] for c in STATION_COLUMNS])
        return rows

    def observe(self, state, now=None, outlook=None, inputs=None) -> None:
        """Called at the end of every safety evaluation. Writes a summary row when one is
        due and the station rows of a new WU poll — to RAM. Never raises."""
        try:
            now = time.time() if now is None else now
            dew = state.get("dew") or {}
            verdicts = (bool(state.get("is_safe")),
                        bool(dew.get("is_safe")) if dew.get("enabled") else None)
            rain = (state.get("components") or {}).get("rain") or {}
            with self._lock:
                event = None
                if self._last_row_ts is None or self._last_verdicts is None:
                    event = "start"
                else:
                    changed = [name for name, new, old in (
                        ("device0", verdicts[0], self._last_verdicts[0]),
                        ("device1", verdicts[1], self._last_verdicts[1])) if new != old]
                    elapsed = now - self._last_row_ts
                    if changed:
                        event = "+".join(changed)
                    elif elapsed < 0 or elapsed >= self.cfg.WEATHER_LOG_INTERVAL_SEC:
                        event = "interval"      # (elapsed < 0: the clock stepped back)
                if event:
                    self._append_locked(
                        "weather", [self._summary_row(state, now, event, outlook, inputs)])
                    self._last_row_ts, self._last_verdicts = now, verdicts
                poll_ts = rain.get("last_poll_ts")
                if (self.cfg.WEATHER_LOG_STATIONS and isinstance(poll_ts, (int, float))
                        and poll_ts != self._last_station_poll_ts):
                    self._last_station_poll_ts = poll_ts
                    results = rain.get("results")
                    if isinstance(results, list):
                        self._append_locked("stations",
                                            self._station_rows(poll_ts, results))
        except Exception as e:  # noqa: BLE001 — information only
            with self._lock:
                self._note_ram_error("row not written: %s: %s" % (type(e).__name__, e))

    def clock_stepped(self, pre_now: float, post_now: float) -> None:
        """The system clock stepped: write the next summary row at once (its time stamp
        is then right) instead of waiting out an interval measured across the step."""
        with self._lock:
            self._last_row_ts = None

    # -- archiving (SD card; weather-log thread only) -------------------------------------
    def flush_due(self, sun_alt, now=None):
        """Why an archive run is due now ('scheduled' / 'RAM limit'), or None."""
        now = time.time() if now is None else now
        with self._lock:
            rows = sum(self._cur_rows.values()) + sum(self._pend_rows.values())
            ram = self._ram_bytes_locked()
            last, attempt, failed = self._last_flush_ts, self._last_attempt, self._error
        if rows == 0:
            return None
        if (failed and attempt is not None
                and time.monotonic() - attempt < RETRY_SEC):
            return None                 # it just failed: not again for a while
        if ram >= self.cfg.WEATHER_LOG_RAM_FLUSH_MB * _MB:
            return "RAM limit"
        interval = self.cfg.WEATHER_LOG_FLUSH_HOURS * 3600.0
        age = None if last is None else now - last
        # age < 0: the clock stepped back past the last run — do not wait out the step
        if age is not None and 0 <= age < interval:
            return None
        if sun_alt is None:
            # Not known to be daytime (no fresh inputs from the page script: the first
            # minute after a reboot, or the script is down). Wait for the sun — but not
            # for ever: a run that is a whole further interval overdue goes ahead. With
            # nothing archived yet, the wait counts from the start of this daemon.
            waited = age if age is not None else now - self._created_ts
            if 0 <= waited < 2.0 * interval:
                return None
        elif sun_alt <= self.cfg.WEATHER_LOG_FLUSH_SUN_ABOVE_DEG:
            return None                 # night: the SD card is written in daytime
        return "scheduled"

    def maybe_flush(self, sun_alt, now=None) -> bool:
        """Archive if due. Returns True when a run was made and succeeded."""
        why = self.flush_due(sun_alt, now)
        return bool(why) and self.flush(why, now)

    def flush(self, reason="requested", now=None) -> bool:
        """Copy every RAM row to the archive now. Returns False if that failed (the rows
        then stay in RAM and the next run takes them)."""
        now = time.time() if now is None else now
        with self._flush_lock:
            with self._lock:
                self._last_attempt = time.monotonic()
            rows, err = 0, None
            try:
                self._prepare_dir()
                self._recover()
                for _ in range(2):      # what was already waiting, then the current rows
                    with self._lock:
                        self._rotate_locked()
                    if not os.path.isdir(self._pend_dir()):
                        break
                    rows += self._archive_pending()
            except Exception as e:  # noqa: BLE001 — never into the daemon
                err = "%s: %s" % (type(e).__name__, e)
                log.error("weather log: archiving to %s FAILED (%s) — the rows stay in "
                          "RAM (%s) and are retried", self.dir, err, self.ram)
            with self._lock:
                self._error = err
                if err is None:
                    self._last_flush_ts = now
                    self._save_meta_locked()
                    if (self._ram_error or "").startswith("RAM files reached"):
                        self._ram_error = None      # the archive works again
            if err is None:
                try:
                    self._enforce_cap(now)
                except Exception:  # noqa: BLE001
                    log.exception("weather log: could not check the archive size")
            self._refresh_listing()
            if err is None and rows:
                log.info("weather log: %d row(s) archived to %s (%s)", rows, self.dir, reason)
            return err is None

    def _save_meta_locked(self) -> None:
        try:
            tmp = self._meta() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"last_flush_ts": self._last_flush_ts}, f)
            os.replace(tmp, self._meta())
        except OSError:
            pass                        # only a convenience across daemon restarts

    def _rotate_locked(self) -> None:
        """Move the current RAM files aside as the set to archive (pending/), and start
        new ones. Only when no set is waiting already."""
        pend_dir = self._pend_dir()
        if os.path.isdir(pend_dir):
            if os.listdir(pend_dir):
                return
            os.rmdir(pend_dir)
        kinds = [k for k in KINDS if self._cur_rows[k] > 0 and os.path.exists(self._cur(k))]
        if not kinds:
            return
        os.makedirs(pend_dir)
        for kind in kinds:
            os.replace(self._cur(kind), self._pend(kind))
            self._pend_rows[kind], self._cur_rows[kind] = self._cur_rows[kind], 0
            # a fresh file at once, so the page's "current" link never dangles
            with open(self._cur(kind), "w", encoding="utf-8", newline="") as f:
                f.write(HEADERS[kind] + "\n")

    def _recover(self) -> None:
        """Finish or undo what an archive run that was killed left behind. MUST run
        before anything new is moved to pending/ (see the module text)."""
        done, marker = self._done_dir(), self._marker()
        for path in (done, self._pend_dir()):
            if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path)):
                os.remove(path)         # a stray file where our directory belongs
        if os.path.isdir(done):
            # the commit happened: those rows are in the archive
            if os.path.exists(marker):
                os.remove(marker)
            shutil.rmtree(done)
        if not os.path.exists(marker):
            return
        uncommitted = os.path.isdir(self._pend_dir()) and bool(os.listdir(self._pend_dir()))
        if uncommitted:
            try:
                with open(marker, encoding="utf-8") as f:
                    sizes = json.load(f).get("sizes") or {}
            except (OSError, ValueError, AttributeError):
                sizes = {}
            here = os.path.realpath(self.dir)
            for path, size in sizes.items():
                # only our own files in our own directory, whatever the marker says
                if (os.path.realpath(os.path.dirname(path)) != here
                        or not _ARCHIVE_RE.match(os.path.basename(path))
                        or not isinstance(size, int) or size < 0):
                    continue
                if os.path.exists(path) and os.path.getsize(path) > size:
                    with open(path, "r+b") as f:
                        f.truncate(size)
                        f.flush()
                        os.fsync(f.fileno())
            log.warning("weather log: the previous archive run was interrupted — it was "
                        "rolled back and is repeated now")
        os.remove(marker)

    @staticmethod
    def _repair_tail(path) -> None:
        """Cut off a last line without its line break (a power cut during an append)."""
        size = os.path.getsize(path)
        if size == 0:
            return
        with open(path, "r+b") as f:
            f.seek(-1, os.SEEK_END)
            if f.read(1) == b"\n":
                return
            back = min(size, 1024 * 1024)
            f.seek(size - back)
            cut = f.read(back).rfind(b"\n")
            keep = size - back + cut + 1 if cut >= 0 else (0 if back == size else size)
            if keep == size:
                return                  # no line break within 1 MB: not one of our files
            f.truncate(keep)
            f.flush()
            os.fsync(f.fileno())
        log.warning("weather log: removed an incomplete last row from %s", path)

    def _archive_file(self, kind, month, header, claimed=()) -> str:
        """The archive file of this month whose header is `header` (or a new one).
        `claimed`: files this run has already given to another column layout."""
        for suffix in _SUFFIXES:
            path = os.path.join(self.dir, "%s_%s%s.csv" % (kind, month, suffix))
            if path in claimed:
                continue
            if not os.path.exists(path):
                return path
            self._repair_tail(path)
            if os.path.getsize(path) == 0:
                return path
            with open(path, "rb") as f:
                first = f.readline().decode("utf-8", "replace").rstrip("\r\n")
            if first == header:
                return path
        raise RuntimeError("too many column layouts for %s %s" % (kind, month))

    def _archive_pending(self) -> int:
        """Append the rows in pending/ to their monthly files. Returns the row count."""
        plan: dict = {}
        skipped = 0
        for name in self._pending_files():
            kind = name.split(".")[0]
            with open(os.path.join(self._pend_dir(), name), "rb") as f:
                raw_lines = f.read().split(b"\n")
            raw_lines.pop()             # b"" after the final line break, or a torn row
            lines = []
            for raw in raw_lines:
                try:
                    lines.append(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    # half a character: a write cut short on a full RAM disk
                    lines.append(None)
            header = HEADERS[kind]
            if lines and lines[0] and lines[0].startswith("utc,"):
                header = lines[0].rstrip("\r")
            columns = header.count(",") + 1
            for line in lines:
                if line is None:
                    skipped += 1
                    continue
                if not line or line.startswith("utc,"):
                    continue            # blank, or the header
                m = _ROW_MONTH_RE.match(line)
                whole = False
                if m and line.count('"') % 2 == 0:
                    # (an odd number of quotes is a row torn inside a quoted field: a CSV
                    # reader would run it into the rows that follow)
                    try:
                        whole = len(next(csv.reader([line], strict=True))) == columns
                    except (csv.Error, StopIteration):
                        whole = False
                if whole:               # only complete rows: every file stays one table
                    plan.setdefault((kind, m.group(1), header), []).append(line + "\n")
                else:
                    skipped += 1
        targets, claimed = [], set()
        for (kind, month, header), lines in sorted(plan.items()):
            # (claimed: two column layouts of one month in the same run need two files)
            path = self._archive_file(kind, month, header, claimed)
            claimed.add(path)
            targets.append((path, header, lines))
        sizes = {path: (os.path.getsize(path) if os.path.exists(path) else 0)
                 for path, _, _ in targets}
        marker = self._marker()
        with open(marker + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"sizes": sizes, "ts": time.time()}, f)
        os.replace(marker + ".tmp", marker)
        rows = 0
        for path, header, lines in targets:
            with open(path, "ab") as f:
                if sizes[path] == 0:
                    f.write((header + "\n").encode("utf-8"))
                f.write("".join(lines).encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            rows += len(lines)
        # THE COMMIT: one atomic rename. Before it, a restart repeats the append (after
        # cutting the files back); after it, the rows count as archived.
        os.rename(self._pend_dir(), self._done_dir())
        os.remove(marker)
        shutil.rmtree(self._done_dir(), ignore_errors=True)
        with self._lock:
            self._pend_rows = {k: 0 for k in KINDS}
        if skipped:
            log.warning("weather log: %d incomplete row(s) were not archived", skipped)
        return rows

    def _list_archives(self) -> list:
        out = []
        try:
            names = os.listdir(self.dir)
        except OSError:
            return out
        for name in names:
            m = _ARCHIVE_RE.match(name)
            path = os.path.join(self.dir, name)
            if not m or os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            out.append({"name": name, "kind": m.group(1), "month": m.group(2),
                        "bytes": st.st_size, "mtime": st.st_mtime})
        out.sort(key=lambda a: (a["month"], a["name"]), reverse=True)
        return out

    def _refresh_listing(self) -> None:
        files = self._list_archives()
        with self._lock:
            self._archives = files
            self._total_bytes = sum(a["bytes"] for a in files)

    def _enforce_cap(self, now) -> None:
        """Delete the oldest months while the archive is above WEATHER_LOG_MAX_MB."""
        cap = self.cfg.WEATHER_LOG_MAX_MB * _MB
        files = self._list_archives()
        total = sum(a["bytes"] for a in files)
        if total <= cap:
            return
        this_month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
        deleted = []
        for month in sorted({a["month"] for a in files}):
            if total <= cap or month >= this_month:
                break                   # never the current month
            for a in [a for a in files if a["month"] == month]:
                os.remove(os.path.join(self.dir, a["name"]))
                total -= a["bytes"]
                deleted.append(a["name"])
        if deleted:
            log.warning("weather log: archive above %g MB — deleted the oldest month(s): %s",
                        self.cfg.WEATHER_LOG_MAX_MB, ", ".join(deleted))
            if self._events is not None:
                try:
                    self._events.record(
                        "WEATHER-LOG", reason="archive above %g MB" % self.cfg.WEATHER_LOG_MAX_MB,
                        result="deleted %s" % ", ".join(deleted))
                except Exception:  # noqa: BLE001
                    log.exception("could not record the weather-log clean-up")
        if total > cap:
            log.warning("weather log: the current month alone exceeds TTU_SAFETY_WEATHER_"
                        "LOG_MAX_MB=%g — nothing more to delete", self.cfg.WEATHER_LOG_MAX_MB)

    # -- for the state file / status page ---------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "dir": self.dir,
                "interval_s": self.cfg.WEATHER_LOG_INTERVAL_SEC,
                "flush_hours": self.cfg.WEATHER_LOG_FLUSH_HOURS,
                "cap_mb": self.cfg.WEATHER_LOG_MAX_MB,
                # rows still only in RAM, per file the page links to
                "current": [{"kind": k, "name": "%s_current.csv" % k,
                             "rows": self._cur_rows[k], "waiting": self._pend_rows[k]}
                            for k in self._kinds()],
                "archives": [{"name": a["name"], "kind": a["kind"], "month": a["month"],
                              "bytes": a["bytes"]} for a in self._archives],
                "total_bytes": self._total_bytes,
                "last_flush_ts": self._last_flush_ts,
                "error": self._error,
                "ram_error": self._ram_error,
                "dropped_rows": self._dropped,
            }


def unavailable_status(why="weather log disabled (TTU_SAFETY_WEATHER_LOG=0)") -> dict:
    return {"enabled": False, "error": why, "current": [], "archives": []}
