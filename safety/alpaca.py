"""ASCOM Alpaca SafetyMonitor REST API over Flask.

Exposes the aggregated verdict as a standard Alpaca SafetyMonitor device (device 0) so
NINA connects natively and reads a single IsSafe boolean. Every reply uses the standard
envelope with an echoed ClientTransactionID and an incrementing ServerTransactionID. A
human-readable /setup page (and /status JSON) is served too.
"""
from __future__ import annotations

import html
import threading

from flask import Flask, jsonify, request

from . import __version__

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
            {"DeviceName": cfg.SERVER_NAME, "DeviceType": "SafetyMonitor",
             "DeviceNumber": dev, "UniqueID": cfg.UNIQUE_ID},
        ])

    # --- SafetyMonitor device: GET properties -------------------------------
    @app.get("/api/v1/safetymonitor/<int:d>/<action>")
    def get_prop(d, action):
        if d != dev:
            return fail(ERR_NOT_IMPLEMENTED, "no such device number")
        a = action.lower()
        if a == "connected":
            return ok(monitor.is_connected())
        if a == "name":
            return ok(cfg.SERVER_NAME)
        if a in ("description", "driverinfo"):
            return ok(driver_info)
        if a == "driverversion":
            return ok(cfg.DRIVER_VERSION)
        if a == "interfaceversion":
            return ok(2)
        if a == "supportedactions":
            return ok([])
        if a == "issafe":
            if not monitor.is_connected():
                return fail(ERR_NOT_CONNECTED, "device not connected")
            return ok(bool(monitor.is_safe()))
        return fail(ERR_NOT_IMPLEMENTED, f"unknown or unsupported property: {action}")

    # --- SafetyMonitor device: PUT methods ----------------------------------
    @app.put("/api/v1/safetymonitor/<int:d>/<action>")
    def put_prop(d, action):
        if d != dev:
            return fail(ERR_NOT_IMPLEMENTED, "no such device number")
        a = action.lower()
        if a == "connected":
            raw = _ci_get(request.form, "Connected")
            if raw is None:
                return fail(ERR_INVALID_VALUE, "missing required parameter 'Connected'")
            monitor.set_connected(str(raw).strip().lower() == "true")
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

    def row(name, value, is_safe_flag):
        dot = "#1a7f37" if is_safe_flag else "#b42318"
        return (f'<tr><td>{html.escape(name)}</td><td>{html.escape(str(value))}</td>'
                f'<td><span style="color:{dot}">&#9679;</span></td></tr>')

    sun = comp["sun"]
    rows.append(row("Sun altitude",
                    "unknown" if sun["value_deg"] is None else f'{sun["value_deg"]:.1f}° '
                    f'(unsafe > {sun["threshold_deg"]:g}°)', sun["safe"]))
    hum = comp["humidity"]
    rows.append(row("Humidity",
                    "unknown" if hum["value_pct"] is None else f'{hum["value_pct"]:.0f}% '
                    f'(unsafe > {hum["threshold_pct"]:g}%)', hum["safe"]))
    rain = comp["rain"]
    if not rain.get("enabled", True):
        rv = "disabled (no WU key)"
    elif rain["latched"]:
        rv = f'RAIN — latched, {rain["seconds_remaining"] // 60} min remaining'
    elif rain["polling_active"]:
        rv = f'no rain, polling {rain["stations_live"]}/{rain["stations_total"]} stations'
    else:
        rv = "no rain, polling paused (daytime)"
    rows.append(row("Rain (WU)", rv, rain["safe"]))

    nws = comp.get("nws")
    if nws:
        if not nws.get("available"):
            nv = "unavailable / stale (not gating)"
        else:
            th = nws.get("thresholds", {})

            def _fc(h):
                return ("c%s%% p%s%% t%s%%" % (
                    h.get("cloud_cover_pct"), h.get("precip_prob_pct"),
                    h.get("thunder_prob_pct"))) if h else "?"
            nv = ("now[%s] next[%s]  (limits c>%g%% p>%g%% t>%g%%)" % (
                _fc(nws.get("now_hour")), _fc(nws.get("next_hour")),
                th.get("cloud_pct", 70), th.get("precip_prob_pct", 15),
                th.get("thunder_prob_pct", 10)))
        rows.append(row("NWS forecast (this/next hr)", nv, nws.get("safe", True)))

    glm = comp.get("glm")
    if glm:
        tk = glm.get("trigger_km", 50)
        if not glm.get("enabled"):
            gv = "disabled (numpy/netCDF4 not installed)"
        elif glm.get("latched"):
            gv = "STRIKE ≤%g km — latched, %d min remaining" % (
                tk, glm.get("seconds_remaining", 0) // 60)
        else:
            nk = glm.get("nearest_km")
            near = "no strikes seen" if nk is None else "nearest %s km %s" % (
                nk, glm.get("nearest_bearing") or "")
            state_txt = "polling" if glm.get("polling_active") else "polling paused (daytime)"
            gv = "%s; %s" % (near, state_txt)
        rows.append(row("Lightning (GLM ≤%g km)" % tk, gv, glm.get("safe", True)))

    rad = comp.get("radar")
    if rad:
        rk = rad.get("trigger_km", 50)
        if not rad.get("enabled"):
            rv = "disabled (Pillow not installed)"
        elif not rad.get("available"):
            rv = "no fresh frame (paused/daytime or unreachable)"
        elif rad.get("in_ring"):
            near = rad.get("nearest_km")
            rv = "RAIN within %g km%s" % (rk, "" if near is None else " (nearest %g km)" % near)
        else:
            rv = "no rain within %g km" % rk
        rows.append(row("Radar (MRMS ≤%g km)" % rk, rv, rad.get("safe", True)))

    conn = comp.get("connectivity")
    if conn:
        if not conn.get("probed"):
            cv = "online (not yet probed)"
        elif conn.get("online"):
            cv = "online"
        elif conn.get("safe"):
            cv = "offline %d min (grace, threshold %d min)" % (
                conn.get("offline_min", 0), conn.get("threshold_sec", 3600) // 60)
        else:
            cv = "OFFLINE %d min — no internet, failing safe" % conn.get("offline_min", 0)
        rows.append(row("Internet", cv, conn.get("safe", True)))

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
<h2>Recent events</h2><ul>{events}</ul>
<p>ASCOM Alpaca SafetyMonitor · device {cfg.DEVICE_NUMBER} · IsSafe at
<code>/api/v1/safetymonitor/{cfg.DEVICE_NUMBER}/issafe</code></p>
</body></html>"""
