"""ASCOM Alpaca SafetyMonitor REST API over Flask.

Exposes the aggregated verdict as a standard Alpaca SafetyMonitor device (device 0) so
NINA connects natively and reads a single IsSafe boolean. Every reply uses the standard
envelope with an echoed ClientTransactionID and an incrementing ServerTransactionID. A
human-readable /setup page (and /status JSON) is served too.

A SECOND standard SafetyMonitor, device 1, is the dew monitor (safety/dew.py): its IsSafe
means "a WET reading of the dome's rain sensor right now is dew — the rain sensor may be
ignored". It is for the dome driver, NOT for NINA; it has its own Connected flag, and it
is absent (no such device) when the dew monitor is switched off.
"""
from __future__ import annotations

import html
import math
import re
import threading
import time

from flask import Flask, jsonify, request

from . import __version__
from .radar import basemap_summary

# The /setup page links to the source code (make_status_page.py has its own SOURCE_URL).
SOURCE_URL = "https://github.com/kirxkirx/ttustatus"

ERR_OK = 0
ERR_NOT_IMPLEMENTED = 0x400
ERR_INVALID_VALUE = 0x401
ERR_NOT_CONNECTED = 0x407
ERR_UNSPECIFIED = 0x4FF


class _Txn:
    def __init__(self):
        self._n = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n


def _ci_get(mapping, key, default=None):
    """Case-insensitive lookup across a request MultiDict."""
    low = key.lower()
    for k in mapping:
        if k.lower() == low:
            return mapping.get(k)
    return default


def create_app(monitor, cfg) -> Flask:
    app = Flask(__name__)
    txn = _Txn()
    dev = cfg.DEVICE_NUMBER
    driver_info = f"{cfg.SERVER_NAME} v{__version__}"
    # device number -> what a client is told about it and where its IsSafe comes from
    devices = {dev: {"name": cfg.SERVER_NAME, "info": driver_info,
                     "unique_id": cfg.UNIQUE_ID, "is_safe": monitor.is_safe}}
    if getattr(monitor, "dew", None) is not None:
        def dew_is_safe():
            monitor.note_dew_read()
            return monitor.is_dew_safe()
        devices[cfg.DEW_DEVICE_NUMBER] = {
            "name": cfg.DEW_SERVER_NAME,
            "info": (f"{cfg.DEW_SERVER_NAME} v{__version__} - IsSafe means: a WET reading "
                     "of the dome rain sensor is dew, the rain sensor may be ignored. For "
                     "the dome driver; NOT an observing-safety monitor, do not use in NINA"),
            "unique_id": cfg.DEW_UNIQUE_ID, "is_safe": dew_is_safe}

    def client_txn() -> int:
        src = request.form if request.method == "PUT" else request.args
        raw = _ci_get(src, "ClientTransactionID", 0)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def envelope(value=None, err=ERR_OK, msg=""):
        body = {
            "ClientTransactionID": client_txn(),
            "ServerTransactionID": txn.next(),
            "ErrorNumber": err,
            "ErrorMessage": msg,
        }
        if value is not None or (err == ERR_OK and request.method == "GET"):
            body["Value"] = value
        return jsonify(body)

    def ok(value=None):
        return envelope(value=value)

    def fail(err, msg):
        return envelope(err=err, msg=msg)

    # --- management API ------------------------------------------------------
    @app.get("/management/apiversions")
    def api_versions():
        return ok([1])

    @app.get("/management/v1/description")
    def mgmt_description():
        return ok({
            "ServerName": cfg.SERVER_NAME,
            "Manufacturer": "TTU observatory",
            "ManufacturerVersion": __version__,
            "Location": cfg.LOCATION,
        })

    @app.get("/management/v1/configureddevices")
    def configured_devices():
        return ok([
            {"DeviceName": d["name"], "DeviceType": "SafetyMonitor",
             "DeviceNumber": number, "UniqueID": d["unique_id"]}
            for number, d in sorted(devices.items())])

    # --- SafetyMonitor device: GET properties -------------------------------
    @app.get("/api/v1/safetymonitor/<int:d>/<action>")
    def get_prop(d, action):
        device = devices.get(d)
        if device is None:
            return fail(ERR_NOT_IMPLEMENTED, "no such device number")
        a = action.lower()
        if a == "connected":
            return ok(monitor.is_connected(d))
        if a == "name":
            return ok(device["name"])
        if a in ("description", "driverinfo"):
            return ok(device["info"])
        if a == "driverversion":
            return ok(cfg.DRIVER_VERSION)
        if a == "interfaceversion":
            return ok(2)
        if a == "supportedactions":
            return ok([])
        if a == "issafe":
            if not monitor.is_connected(d):
                return fail(ERR_NOT_CONNECTED, "device not connected")
            return ok(bool(device["is_safe"]()))
        return fail(ERR_NOT_IMPLEMENTED, f"unknown or unsupported property: {action}")

    # --- SafetyMonitor device: PUT methods ----------------------------------
    @app.put("/api/v1/safetymonitor/<int:d>/<action>")
    def put_prop(d, action):
        if d not in devices:
            return fail(ERR_NOT_IMPLEMENTED, "no such device number")
        a = action.lower()
        if a == "connected":
            raw = _ci_get(request.form, "Connected")
            if raw is None:
                return fail(ERR_INVALID_VALUE, "missing required parameter 'Connected'")
            monitor.set_connected(str(raw).strip().lower() == "true", d)
            return ok()
        return fail(ERR_NOT_IMPLEMENTED, f"cannot set: {action}")

    # --- human-readable setup / debug ---------------------------------------
    @app.get("/setup")
    def server_setup():
        return _setup_html(monitor, cfg)

    @app.get("/setup/v1/safetymonitor/<int:d>/setup")
    def device_setup(d):
        return _setup_html(monitor, cfg)

    @app.get("/")
    def index():
        return _setup_html(monitor, cfg)

    @app.get("/status")
    def status_json():
        return jsonify(monitor.evaluate())

    return app


def _setup_html(monitor, cfg) -> str:
    st = monitor.evaluate()
    safe = st["is_safe"]
    badge_col = "#1a7f37" if safe else "#b42318"
    label = "SAFE" if safe else "UNSAFE"
    comp = st["components"]
    rows = []

    def row(name, value, is_safe_flag, unknown=False):
        # grey = no current data; green strictly means "checked and clear"
        dot = "#8b959c" if unknown else ("#1a7f37" if is_safe_flag else "#b42318")
        return (f'<tr><td>{html.escape(name)}</td><td>{html.escape(str(value))}</td>'
                f'<td><span style="color:{dot}">&#9679;</span></td></tr>')

    sun = comp["sun"]
    if sun.get("stale"):
        rows.append(row("Sun altitude", "unknown (inputs stale)", False, unknown=True))
    else:
        rows.append(row("Sun altitude",
                        "unknown" if sun["value_deg"] is None else f'{sun["value_deg"]:.1f}° '
                        f'(unsafe > {sun["threshold_deg"]:g}°)', sun["safe"],
                        unknown=sun["value_deg"] is None))
    hum = comp["humidity"]
    if hum.get("stale"):
        rows.append(row("Humidity", "unknown (inputs stale)", False, unknown=True))
    else:
        rows.append(row("Humidity",
                        "unknown" if hum["value_pct"] is None else f'{hum["value_pct"]:.0f}% '
                        f'(unsafe > {hum["threshold_pct"]:g}%)', hum["safe"],
                        unknown=hum["value_pct"] is None))
    rain = comp["rain"]
    rain_unknown = False
    if not rain.get("enabled", True):
        rv, rain_unknown = "disabled (no WU key)", True
    elif rain["latched"]:
        rv = f'RAIN — latched, {rain["seconds_remaining"] // 60} min remaining'
    elif not rain["polling_active"]:
        rv, rain_unknown = "polling paused (daytime) — no current data", True
    elif rain.get("stations_live", 0) == 0:
        rv = (f'no station data ({rain["stations_live"]}/{rain["stations_total"]} '
              f'reporting)')
        rain_unknown = True
    else:
        rv = f'no rain, polling {rain["stations_live"]}/{rain["stations_total"]} stations'
    rows.append(row("Rain (WU)", rv, rain["safe"], unknown=rain_unknown))

    nws = comp.get("nws")
    if nws:
        if not nws.get("available"):
            err = nws.get("error")
            nv = "unavailable / stale (not gating)" + (
                " — %s" % str(err)[:60] if err else "")
            nws_unknown = True
        else:
            nws_unknown = False
            th = nws.get("thresholds", {})

            def _fc(h):
                return ("c%s%% p%s%% t%s%%" % (
                    h.get("cloud_cover_pct"), h.get("precip_prob_pct"),
                    h.get("thunder_prob_pct"))) if h else "?"
            nv = ("now[%s] next[%s]  (limits c>%g%% p>%g%% t>%g%%)" % (
                _fc(nws.get("now_hour")), _fc(nws.get("next_hour")),
                th.get("cloud_pct", 60), th.get("precip_prob_pct", 20),
                th.get("thunder_prob_pct", 15)))
        rows.append(row("NWS forecast (this/next hr)", nv, nws.get("safe", True),
                        unknown=nws_unknown))

    glm = comp.get("glm")
    if glm:
        tk = glm.get("trigger_km", 50)
        glm_unknown = False
        if not glm.get("enabled"):
            gv, glm_unknown = "disabled (numpy/netCDF4 not installed)", True
        elif glm.get("latched"):
            gv = "STRIKE ≤%g km — latched, %d min remaining" % (
                tk, glm.get("seconds_remaining", 0) // 60)
        elif not glm.get("polling_active"):
            gv, glm_unknown = "polling paused (daytime) — no current data", True
        elif not glm.get("available"):
            gv, glm_unknown = "no fresh data (no recent successful poll)", True
        else:
            nk = glm.get("nearest_km")
            gv = "no strikes seen" if nk is None else "no strike in ring; nearest %s km %s" % (
                nk, glm.get("nearest_bearing") or "")
        rows.append(row("Lightning (GLM ≤%g km)" % tk, gv, glm.get("safe", True),
                        unknown=glm_unknown))

    rad = comp.get("radar")
    if rad:
        rk = rad.get("trigger_km", 30)
        rad_unknown = False
        if not rad.get("enabled"):
            rv, rad_unknown = "disabled (Pillow not installed)", True
        elif rad.get("unconfirmed_echo"):
            # checked before in_ring: an unconfirmed echo is not a veto, and this row must
            # not read "RAIN" while the component reports safe
            near = rad.get("nearest_km")
            rv = "echo ≤%g km%s — unconfirmed (%d of %d frames), not triggering" % (
                rk, "" if near is None else " (nearest %g km)" % near,
                rad.get("ring_streak", 1), rad.get("trigger_after", 2))
            rad_unknown = True
        elif rad.get("in_ring"):
            near = rad.get("nearest_km")
            rv = "RAIN within %g km%s" % (rk, "" if near is None else " (nearest %g km)" % near)
        elif rad.get("latched"):
            # the freeze holds the veto whether or not the current frame is fresh, so it
            # is checked before availability — otherwise this row would read "no data"
            # while the component is in fact vetoing
            rv = "recent rain ≤%g km — freeze, %d min remaining%s" % (
                rk, rad.get("seconds_remaining", 0) // 60,
                "" if rad.get("available") else " (no fresh frame)")
        elif not rad.get("available"):
            rv, rad_unknown = "no fresh frame (radar unreachable?)", True
        else:
            rv = "no rain within %g km" % rk
        rows.append(row("Radar (MRMS ≤%g km)" % rk, rv, rad.get("safe", True),
                        unknown=rad_unknown))
    # The radar map's basemap (CARTO / OpenStreetMap / none) and what to configure — the
    # same line as the status page; absent from an older daemon's component.
    basemap_line = ""
    if isinstance(rad, dict) and rad.get("enabled"):
        line = basemap_summary(rad.get("basemap"), rad.get("basemap_notes"))
        if line:
            credit = str(rad.get("attribution") or "")
            basemap_line = ('<p style="color:#666;font-size:.85em">Radar map &mdash; %s%s</p>'
                            % (html.escape(line),
                               " Credit: " + html.escape(credit) if credit else ""))

    conn = comp.get("connectivity")
    if conn:
        conn_unknown = False
        if not conn.get("enabled", True):
            cv, conn_unknown = "watchdog disabled", True
        elif not conn.get("probed"):
            cv, conn_unknown = "not yet probed — no data", True
        elif conn.get("online"):
            cv = "online"
        elif conn.get("safe"):
            cv = "offline %d min (grace, threshold %d min)" % (
                conn.get("offline_min", 0), conn.get("threshold_sec", 3600) // 60)
        else:
            cv = "OFFLINE %d min — no internet, failing safe" % conn.get("offline_min", 0)
        rows.append(row("Internet", cv, conn.get("safe", True), unknown=conn_unknown))

    hz = comp.get("hazards")
    if hz:
        names = _veto_names(hz) or "none configured"
        veto = [v for v in hz.get("veto") or [] if isinstance(v, dict)]
        hz_unknown = False
        if veto:
            # checked first: a held veto is in force whether or not the feed is fresh
            hv = "; ".join("VETO — %s over the site until %s%s" % (
                _txt(v.get("event"), 60), _txt(v.get("end_local"), 40) or "?",
                " (held: not re-confirmed by a fresh query)"
                if v.get("source") == "latched" else "") for v in veto)
        elif not hz.get("enabled"):
            hv, hz_unknown = "disabled", True
        elif not hz.get("available"):
            hv, hz_unknown = "unavailable / stale (not gating)", True
        else:
            counts = hz.get("counts") if isinstance(hz.get("counts"), dict) else {}
            hv = "no veto event over the site · %s alert(s) at the site, %s nearby" % (
                counts.get("at_site", 0), counts.get("nearby", 0))
        if hz.get("error") and not veto:
            hv += " — %s" % _txt(hz["error"], 90)
        rows.append(row("NWS warnings (veto: %s)" % names, hv,
                        bool(hz.get("safe", True)) and not veto, unknown=hz_unknown))
    hazards_html = _hazards_html(hz, comp.get("hazard_info"))

    reasons = ""
    if st["reasons"]:
        items = "".join(f"<li>{html.escape(r)}</li>" for r in st["reasons"])
        reasons = f"<p><b>Why unsafe:</b></p><ul>{items}</ul>"
    if st.get("warnings"):
        items = "".join(f"<li>&#9888; {html.escape(w)}</li>" for w in st["warnings"])
        reasons += f"<p><b>Warnings:</b></p><ul>{items}</ul>"
    geo = st.get("geocode") or {}
    if geo:
        reasons += ('<p style="color:#666;font-size:.85em">Site: %s, %s (%s)</p>'
                    % (geo.get("lat"), geo.get("lon"), html.escape(str(geo.get("source")))))
    events = "".join(f"<li>{html.escape(e)}</li>" for e in st.get("events_tail", []))
    source = '<a href="%s" rel="noopener">%s</a>' % (
        html.escape(SOURCE_URL), html.escape(SOURCE_URL.split("://", 1)[-1]))
    dew_html = _dew_html(st.get("dew"), row)
    wlog_html = _weather_log_html(st.get("weather_log"))

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(cfg.SERVER_NAME)}</title>
<meta http-equiv="refresh" content="30">
<style>body{{font-family:system-ui,sans-serif;max-width:680px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td{{padding:.35rem .5rem;border-bottom:1px solid #ddd}}
.badge{{display:inline-block;color:#fff;padding:.4rem 1rem;border-radius:6px;font-weight:700;
background:{badge_col}}}code{{background:#f3f3f3;padding:.1rem .3rem}}</style></head><body>
<h1>{html.escape(cfg.SERVER_NAME)}</h1>
<p><span class="badge">{label}</span></p>
{reasons}
<h2>Inputs</h2><table>{''.join(rows)}</table>
{basemap_line}
{dew_html}
{hazards_html}
{wlog_html}
<h2>Recent events</h2><ul>{events}</ul>
<p>ASCOM Alpaca SafetyMonitor · device {cfg.DEVICE_NUMBER} · IsSafe at
<code>/api/v1/safetymonitor/{cfg.DEVICE_NUMBER}/issafe</code><br>
Source code: {source}</p>
</body></html>"""


# --- dew monitor (device 1) and weather log on /setup ---------------------------------
def _dew_html(dew, row) -> str:
    """The device 1 section: what it answers, the answer, and each check with its numbers.
    Worded as 'rain sensor ignored / trusted' — SAFE/UNSAFE would read as a weather
    verdict, which this is not."""
    if not isinstance(dew, dict) or not dew.get("enabled"):
        return ""
    th = dew.get("thresholds") if isinstance(dew.get("thresholds"), dict) else {}
    checks = dew.get("checks") if isinstance(dew.get("checks"), dict) else {}
    fc = dew.get("forecast") if isinstance(dew.get("forecast"), dict) else {}
    al = dew.get("alpaca") if isinstance(dew.get("alpaca"), dict) else {}
    safe = bool(dew.get("is_safe"))

    def num(v, unit, digits=1):
        return "no data" if not isinstance(v, (int, float)) else "%.*f%s" % (digits, v, unit)

    def finite(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    def lim(key, default):
        v = th.get(key)
        return "%g" % (v if finite(v) else default)

    def check(key):
        return checks.get(key) is True, checks.get(key) is None

    rows = []
    ok, unknown = check("device0")
    rows.append(row("Safety monitor (device 0)",
                    "unknown" if unknown else ("SAFE" if ok else "UNSAFE"), ok,
                    unknown=unknown))
    ok, unknown = check("spread")
    rows.append(row("Dew-point spread (WU stations, median)",
                    "%s of %s station(s) (needs ≤ %s °C)"
                    % (num(dew.get("spread_c"), " °C"), dew.get("stations_used", 0),
                       lim("spread_max_c", 5)), ok, unknown=unknown))
    ok, unknown = check("wind")
    rows.append(row("Wind (WU stations, median)",
                    "%s of %s station(s) (calm ≤ %s mph, until above %s mph)"
                    % (num(dew.get("wind_mph"), " mph"), dew.get("stations_wind", 0),
                       lim("wind_max_mph", 8), lim("wind_unsafe_above_mph", 9)),
                    ok, unknown=unknown))
    ok, unknown = check("cloud")
    rows.append(row("Forecast cloud cover (this / next hour)",
                    "%s / %s (needs < %s%%)"
                    % (num(fc.get("cloud_now"), "%", 0), num(fc.get("cloud_next"), "%", 0),
                       lim("cloud_below_pct", 30)), ok, unknown=unknown))
    ok, unknown = check("precip")
    rows.append(row("Forecast precipitation probability (this / next hour)",
                    "%s / %s (needs < %s%%)"
                    % (num(fc.get("precip_now"), "%", 0), num(fc.get("precip_next"), "%", 0),
                       lim("precip_below_pct", 5)), ok, unknown=unknown))
    if "lightning" in checks:
        lt = dew.get("lightning") if isinstance(dew.get("lightning"), dict) else {}
        ok, unknown = check("lightning")
        need = ("needs none within %s km for %s min"
                % (lim("glm_km", 60), lim("glm_hold_min", 30)))

        def km(v):
            # one decimal, as the rule works (whole kilometres from 100 km on)
            return "%.*f km" % (1 if abs(v) < 100 else 0, v)

        seen, watched = lt.get("recent_age_s"), lt.get("watched_s")
        if ok is False and not unknown and finite(seen) and seen >= 0:
            hit = lt.get("recent_km")
            found = "flash %s %d min ago" % (
                km(hit) + " away" if finite(hit) else "at an unreadable distance",
                seen // 60)
        elif lt.get("available") is not True:
            found = "no data" + (" — %s" % _txt(lt.get("why"), 160) if lt.get("why") else "")
        else:
            near = lt.get("nearest_km")
            found = "nearest flash %s" % km(near) if finite(near) else "no flash seen"
            if unknown and finite(watched) and watched >= 0:
                found += ", watched for %d min so far" % (watched // 60)
        rows.append(row("Lightning (GLM)", "%s (%s)" % (found, need), ok, unknown=unknown))
    verdict = ("IsSafe = true: rain sensor may be IGNORED (dew likely)" if safe
               else "IsSafe = false: rain sensor TRUSTED")
    why = ""
    if not safe and dew.get("reasons"):
        why = "<ul>%s</ul>" % "".join("<li>%s</li>" % html.escape(_txt(r, 200))
                                      for r in dew["reasons"])
    age = dew.get("last_read_age_s")
    asked = ("never asked since the daemon started" if not isinstance(age, (int, float))
             else "last asked %d s ago" % age)
    number = html.escape(str(al.get("device_number", 1)))
    return (
        "<h2>Dew monitor (device %s)</h2>"
        '<p style="color:#666;font-size:.85em">A second SafetyMonitor for the dome driver, '
        "not for NINA. Its IsSafe answers: <i>if the dome rain sensor reads WET right now, "
        "is that dew?</i> True only while every line below holds; missing data counts as "
        "false.</p>"
        "<p><b>%s</b></p>%s<table>%s</table>"
        '<p style="color:#666;font-size:.85em">IsSafe at <code>%s</code> · client %s, %s</p>'
        % (number, html.escape(verdict), why, "".join(rows),
           html.escape(str(al.get("issafe_path", "/api/v1/safetymonitor/1/issafe"))),
           "connected" if dew.get("connected") else "not connected", html.escape(asked)))


def _weather_log_html(wl) -> str:
    if not isinstance(wl, dict) or not wl.get("enabled"):
        return ""
    archives = [a for a in wl.get("archives") or [] if isinstance(a, dict)]
    current = [c for c in wl.get("current") or [] if isinstance(c, dict)]
    total = wl.get("total_bytes")
    lines = ["Archive: %s — %d file(s), %s" % (
        _txt(wl.get("dir"), 200), len(archives),
        "%.1f MB" % (total / 1048576.0) if isinstance(total, (int, float)) else "size unknown")]
    lines.append("Not archived yet (in RAM): " + (", ".join(
        "%s %s row(s)" % (_txt(c.get("name"), 60),
                          (c.get("rows") or 0) + (c.get("waiting") or 0))
        for c in current) or "nothing"))
    last = wl.get("last_flush_ts")
    if isinstance(last, (int, float)):
        lines.append("Last archived: %s" % time.strftime("%Y-%m-%d %H:%M:%S",
                                                         time.localtime(last)))
    for key, label in (("error", "ARCHIVE NOT WRITTEN"), ("ram_error", "RAM file problem")):
        if wl.get(key):
            lines.append("%s: %s" % (label, _txt(wl[key], 200)))
    return "<h2>Weather log</h2><ul>%s</ul>" % "".join(
        "<li>%s</li>" % html.escape(ln) for ln in lines)


# --- hazards section of /setup ------------------------------------------------------
_HEX_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")          # used with fullmatch
# keys a pre-formatted info item may carry its display line under, most specific first
_TEXT_KEYS = ("text", "line", "summary", "label", "headline", "title", "name", "place")
_MAX_ALERTS = 25


def _txt(x, limit=300) -> str:
    """One line of plain text, bounded (callers html.escape it)."""
    return "" if x is None else " ".join(str(x).split())[:limit]


def _item_text(x, limit=160) -> str:
    """A display line for one info item, whatever its shape: the feeds pre-format their
    items, but this debug page must never crash (or go blank) on an unexpected one."""
    if isinstance(x, dict):
        for k in _TEXT_KEYS:
            if x.get(k):
                return _txt(x[k], limit)
        return _txt(", ".join("%s %s" % (k, v) for k, v in x.items()
                              if isinstance(v, (str, int, float))
                              and not isinstance(v, bool)), limit)
    return _txt(x, limit)


def _swatch(color) -> str:
    # only a strict #RRGGBB reaches the style attribute; anything else draws no swatch
    if not isinstance(color, str) or not _HEX_COLOR.fullmatch(color):
        return ""
    return ('<span style="display:inline-block;width:.8em;height:.8em;margin-right:.35em;'
            'border:1px solid #888;background:%s"></span>' % color)


def _veto_names(hz) -> str:
    names = hz.get("veto_events")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)):
        return ""
    return ", ".join(_txt(n, 60) for n in names if n)


def _alert_items(alerts, known=True) -> str:
    """The alert list; ``known`` = the query behind it is current. Fail-safe wording (the
    status page's rule): an empty list from a failing or stale query is 'unknown', never
    'none' — and a non-empty one is labelled last-known."""
    alerts = [a for a in (alerts if isinstance(alerts, list) else []) if isinstance(a, dict)]
    if not alerts:
        return ("<p>none</p>" if known else
                "<p>unknown — the NWS queries are failing or stale</p>")
    items = [] if known else ["<li><i>last known — the NWS queries are not current</i></li>"]
    for a in alerts[:_MAX_ALERTS]:
        chips = ""
        if a.get("vetoes"):
            chips += ' <b style="color:#b42318">VETO</b>'
        if a.get("threat"):
            chips += " <b>[%s]</b>" % html.escape(_txt(a["threat"], 40))
        head = _txt(a.get("nws_headline") or a.get("headline"), 220)
        tail = "".join(" · %s" % html.escape(t) for t in (
            _txt(a.get("sender"), 60),
            ("until " + _txt(a.get("end_local"), 40)) if a.get("end_local") else "") if t)
        items.append("<li>%s<b>%s</b>%s%s%s</li>" % (
            _swatch(a.get("color")), html.escape(_txt(a.get("event"), 60) or "alert"), chips,
            (" — " + html.escape(head)) if head else "", tail))
    if len(alerts) > _MAX_ALERTS:
        items.append("<li>… and %d more</li>" % (len(alerts) - _MAX_ALERTS))
    return "<ul>%s</ul>" % "".join(items)


def _info_lines(info) -> list:
    lines = []
    feeds = info.get("feeds")
    if isinstance(feeds, dict) and feeds:
        st = []
        for name in sorted(feeds, key=str):
            f = feeds[name] if isinstance(feeds[name], dict) else {}
            if f.get("ok"):
                age = f.get("age_s")
                st.append("%s ok%s" % (_txt(name, 30), "" if not isinstance(age, (int, float))
                                       else " (%d min old)" % (age // 60)))
            else:
                st.append("%s ERROR%s" % (_txt(name, 30), (": " + _txt(f.get("error"), 60))
                                          if f.get("error") else ""))
        lines.append("Feeds: " + "; ".join(st))
    feeds = feeds if isinstance(feeds, dict) else {}
    totals = info.get("totals") if isinstance(info.get("totals"), dict) else {}

    def unknown(feed):
        """None when the feed is current, else 'no current data (why)': an empty list
        from a failed, stale or missing feed is never reported as 'none'."""
        f = feeds.get(feed) if isinstance(feeds.get(feed), dict) else {}
        if f.get("ok"):
            return None
        return "no current data (%s)" % (_txt(f.get("error"), 60) or "status unknown")

    def listed(label, items, feed, none="none"):
        items = items if isinstance(items, list) else []
        shown = [t for t in (_item_text(i) for i in items[:5]) if t]
        total = totals.get(feed)
        total = total if isinstance(total, int) and total >= len(items) else len(items)
        more = " (+%d more)" % (total - len(shown)) if shown and total > len(shown) else ""
        stale = (" — last known, %s" % unknown(feed)) if unknown(feed) else ""
        if shown:
            lines.append("%s: %s%s%s" % (label, "; ".join(shown), more, stale))
        elif items:                     # present but not displayable: never "none"
            lines.append("%s: %d item(s) without a displayable text%s"
                         % (label, total, stale))
        else:
            lines.append("%s: %s" % (label, unknown(feed) or none))

    smoke = info.get("smoke")
    lines.append("Smoke (NOAA HMS): %s" % (_item_text(smoke) if smoke
                                           else unknown("smoke") or "none reported"))
    listed("Wildfires", info.get("fires"), "fires")
    spc = info.get("spc") if isinstance(info.get("spc"), dict) else {}
    mds = spc.get("mds") if isinstance(spc.get("mds"), list) else []
    outlook = (unknown("spc_outlook")
               or _txt(spc.get("text") or spc.get("label") or spc.get("category"), 120)
               or "no risk area at the site")
    lines.append("SPC Day-1 outlook: %s · mesoscale discussions on the map: %s"
                 % (outlook, unknown("spc_md") or len(mds)))
    listed("Storm reports", info.get("lsr"), "lsr")
    return lines


def _hazards_html(hz, info) -> str:
    """NWS alerts (at the site / nearby) and the information-only hazard feeds. Every
    string is escaped; a colour reaches the markup only as a validated #RRGGBB."""
    if not hz and not info:
        return ""
    out = ["<h2>Hazards</h2>"]
    sources = []
    if hz:
        names = _veto_names(hz)
        veto = [v for v in hz.get("veto") or [] if isinstance(v, dict)]
        if not hz.get("enabled") and not veto:
            # switched off (or failed to load): no lists, and no policy line claiming a
            # veto that cannot happen
            err = hz.get("error")
            out.append("<p>NWS alert layer off — no veto%s.</p>"
                       % (" (%s)" % html.escape(_txt(err, 160)) if err
                          else " (TTU_SAFETY_HAZARDS=0)"))
        else:
            out.append('<p style="color:#666;font-size:.85em">Only these NWS warnings, while '
                       "in effect over the site, make the monitor UNSAFE: %s. Every other "
                       "alert below is information only.</p>"
                       % html.escape(names or "none configured (all alerts display-only)"))
            available = bool(hz.get("available"))
            area = hz.get("area_fresh")
            area = area if isinstance(area, bool) else available
            if hz.get("error") and not available:
                out.append("<p>%s</p>" % html.escape(_txt(hz["error"], 200)))
            out.append("<h3>NWS alerts at the site</h3>"
                       + _alert_items(hz.get("at_site"), known=available))
            out.append("<h3>NWS alerts nearby (on the map)</h3>"
                       + _alert_items(hz.get("nearby"), known=area))
            sources.append(_txt(hz.get("source"), 120) or "NWS api.weather.gov active alerts")
    if info:
        out.append("<h3>Other hazard information "
                   "<small>(information only — never affects IsSafe)</small></h3>")
        if not info.get("enabled"):
            err = info.get("error")
            out.append("<p>disabled%s</p>" % (" — " + html.escape(_txt(err, 120)) if err else ""))
        else:
            if info.get("error"):
                out.append("<p>%s</p>" % html.escape(_txt(info["error"], 160)))
            out.append("<ul>%s</ul>" % "".join(
                "<li>%s</li>" % html.escape(ln) for ln in _info_lines(info)))
            if info.get("source"):
                sources.append(_txt(info["source"], 300))
    if sources:
        out.append('<p style="color:#666;font-size:.85em">Sources: %s</p>'
                   % html.escape(" · ".join(sources)))
    return "\n".join(out)
