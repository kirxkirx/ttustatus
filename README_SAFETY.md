# TTU Alpaca SafetyMonitor

An ASCOM Alpaca **SafetyMonitor** for the observatory, running on the Raspberry Pi
alongside `make_status_page.py`. It aggregates several inputs into a single `IsSafe`
boolean that NINA (and any Alpaca client) can read, and the status page shows the same
information plus the recent safety-event log.

## Architecture (two cooperating processes)

`make_status_page.py` is **ephemeral** (spawned every 90 s by the daemon), so the
persistent Alpaca server and the rain poller live in a **separate long-running daemon**,
`safety_monitor.py`. They share small JSON files:

```
make_status_page.py  --writes-->  /tmp/safety_inputs.json   (sun altitude, humidity)
safety_monitor.py    --reads--/
safety_monitor.py    --writes-->  /tmp/safety_state.json    (IsSafe + inputs + events)
make_status_page.py  --reads--/   (renders the "Safety monitor" page section)
```

Durable files (survive reboot): `~/safety_latch.json` (the rain latch) and
`~/safety_events.log` (the audit trail, also mirrored to stdout).

## Safety logic

`IsSafe = SAFE` only if **all** hold (anything else → `UNSAFE`, fail-safe):

| Input | Unsafe when | Source |
|------|-------------|--------|
| Sun altitude | `> 0°` (no refraction / angular-size correction) | make_status_page.py |
| Humidity | `> 95 %` (clears again below 93 %, small hysteresis) | make_status_page.py (DHT) |
| Rain | **any** WU station reports `precipRate > 0` | this daemon |
| Inputs freshness | `safety_inputs.json` missing or older than 10 min | fail-safe |

**Rain latch:** WU is polled only when the sun is **below 5°** (daytime = zero API
calls). The **first** station reporting any rain trips UNSAFE immediately — no
confirmation, no second reading — and it stays unsafe for **1 hour after the last** rain
seen at any station (the freeze time). The latch is persisted, so a daemon restart or
reboot keeps it.

More inputs (cloud sensor, ceilometer, radar, plate-solve failures) can be added later as
extra components in `safety/monitor.py`.

## Deploy on the Pi

1. Clone the repo into `~/ttustatus` (everything runs from there):
   ```bash
   cd ~ && git clone https://github.com/kirxkirx/ttustatus.git   # public: no credentials needed
   ```
2. Install the daemon's only extra dependencies — Flask + waitress (astropy/adafruit etc.
   are only used by the page generator, not the daemon). Current Raspberry Pi OS blocks
   system-wide `pip3` (PEP 668 "externally-managed-environment"), so install via apt — then
   `/usr/bin/python3` (and the systemd unit below) find them with no path changes:
   ```bash
   sudo apt update && sudo apt install python3-flask python3-waitress
   python3 -c "import flask, waitress; print('ok')"      # verify
   ```
   Alternative — a dedicated venv (no system packages, newer Flask):
   ```bash
   python3 -m venv ~/safety-venv
   ~/safety-venv/bin/pip install flask waitress
   ```
   Then run the daemon with `~/safety-venv/bin/python` instead of `/usr/bin/python3`
   (edit that path in `run_safety_monitor.sh` and the systemd unit's `ExecStart`). A plain
   venv suffices — the daemon needs only flask + waitress + stdlib, not the Pi hardware libs.
3. Set the WU API key (see the next section), then install the single service:
   ```bash
   sudo ~/ttustatus/deploy/install.sh
   ```
   One systemd unit runs everything: the daemon spawns `make_status_page.py` every
   ~90 s as an isolated subprocess (killed after 30 min if it ever hangs; a page
   failure is logged and never affects the safety logic). Logs (rotating, via
   journald): `journalctl -u ttu-safety -f`. Disable the built-in page runner with
   `TTU_SAFETY_PAGE=0` if you prefer to schedule the page yourself.

### The WU API key (required, kept out of git)

The key is **not** in the code — set it via the environment. Create a secrets file
**outside the repo** and point the unit at it:

```bash
cp ttustatus.env.example /home/kirx/ttustatus.env   # then edit it and paste your key
chmod 600 /home/kirx/ttustatus.env
sudo systemctl daemon-reload && sudo systemctl restart ttu-safety
journalctl -u ttu-safety -n 20     # should NOT warn "TTU_SAFETY_WU_KEY is not set"
```

`ttustatus.env` contains just `TTU_SAFETY_WU_KEY=<your key>` (systemd `EnvironmentFile`
format: `KEY=value`, no `export`, no quotes). Only the daemon needs it —
`make_status_page.py` does not use the key. If the key is unset the daemon still runs
(sun/humidity protection) but rain polling is disabled and the page shows rain "off".

After editing `ttustatus.env`, apply it with `sudo systemctl restart ttu-safety`.
Quick manual test without systemd: `TTU_SAFETY_WU_KEY=<key> python3 safety_monitor.py`
(the `run_*.sh` scripts remain as manual fallbacks; `run_safety_monitor.sh` sources
`~/ttustatus.env` itself).

## Connect from NINA

Equipment → Safety Monitor → **ASCOM Alpaca**. Discovery (UDP 32227) should find
`TTU Safety Monitor`; otherwise enter the Pi's IP and port **11111**, device **0**.
Then in the Advanced Sequencer add the **"Unsafe" trigger** (park mount / close dome) so
NINA reacts automatically.

- IsSafe:   `http://<pi>:11111/api/v1/safetymonitor/0/issafe`
- Setup/status page: `http://<pi>:11111/setup`   ·   debug JSON: `/status`

## Configuration (environment variables)

All optional; defaults suit the Pi. Set them in the systemd unit or before launch.

| Var | Default | Meaning |
|-----|---------|---------|
| `TTU_SAFETY_HTTP_PORT` | `11111` | Alpaca port |
| `TTU_SAFETY_HTTP_HOST` | `0.0.0.0` | bind address (LAN) |
| `TTU_SAFETY_POLL_INTERVAL` | `600` | seconds between WU polls (night only) |
| `TTU_SAFETY_RAIN_LATCH_HOURS` | `1.0` | freeze time: how long UNSAFE persists after the last rain |
| `TTU_SAFETY_INPUTS_STALE_SEC` | `600` | older inputs → fail-safe UNSAFE |
| `TTU_SAFETY_WU_KEY` | **(required)** | Weather Underground PWS API key — set via env, never commit (see above) |
| `TTU_SAFETY_LAT` / `_LON` | (adopted from GPS) | site coordinates; unset ⇒ the daemon adopts the status page's GPS fix once at startup (rounded to ~100 m). All derived values (NWS grid, WU stations, radar/GLM rings, cached tiles) follow them |
| `TTU_SAFETY_GEO_MISMATCH_KM` | `0.1` | warn (page + /setup) when GPS and configured coords differ by more |
| `TTU_SAFETY_WU_MAX_KM` | `60` | drop "nearest" WU stations farther than this (sparse regions) |
| `TTU_SAFETY_LOCAL_TZ` | `America/Chicago` | timezone of the forecast table (invalid ⇒ loud UTC fallback) |
| `TTU_SAFETY_STATE_HEARTBEAT_SEC` | `60` | max interval between unchanged state-file writes (SD-wear throttle) |
| `TTU_SAFETY_INPUTS_FILE` / `_STATE_FILE` / `_LATCH_FILE` / `_EVENT_LOG` | see `config.py` | file paths |
| `TTU_SAFETY_NWS` | `1` | enable the NWS forecast component (`0` disables) |
| `TTU_SAFETY_NWS_UA` | `ttu-safety-monitor` | User-Agent NWS asks for (add a contact) |
| `TTU_SAFETY_NWS_GRID` | (auto) | e.g. `LUB/46,41`; skips the `/points` lookup |
| `TTU_SAFETY_NWS_POLL_INTERVAL` | `900` | seconds between NWS forecast pulls (15 min) |
| `TTU_SAFETY_NWS_CLOUD_MAX` / `_PRECIP_MAX` / `_THUNDER_MAX` | `70` / `20` / `15` | % thresholds (unsafe when exceeded, this or next hour) |
| `TTU_SAFETY_GLM` | `1` | enable the GLM lightning component (`0` disables) |
| `TTU_SAFETY_GLM_TRIGGER_KM` | `50` | flash within this radius → UNSAFE |
| `TTU_SAFETY_GLM_COOLOFF_HOURS` | `0.5` | freeze time (h) after the last nearby flash |
| `TTU_SAFETY_GLM_POLL_INTERVAL` | `300` | seconds between GLM polls (night only) |
| `TTU_SAFETY_GLM_WINDOW_MIN` | `5` | look-back minutes fetched per poll |
| `TTU_SAFETY_RADAR` | `1` | enable the MRMS radar component (`0` disables) |
| `TTU_SAFETY_RADAR_KM` | `50` | any echo within this radius → UNSAFE |
| `TTU_SAFETY_RADAR_DBZ` | `20` | reflectivity ≥ this counts as rain |
| `TTU_SAFETY_RADAR_LATCH_SEC` | `900` | freeze time (s) after the last in-ring echo; clear frames do not cancel it |
| `TTU_SAFETY_RADAR_POLL_INTERVAL` | `300` | seconds between radar polls (day and night) |
| `TTU_SAFETY_RADAR_THUMB` | `/var/www/html/ttu_radar.png` | thumbnail path (beside status.html) |
| `TTU_SAFETY_RADAR_TILE_URL` | Carto dark | night basemap tile template (OSM data) |
| `TTU_SAFETY_RADAR_TILE_URL_DAY` | Carto light | day basemap tile template (`TTU_SAFETY_RADAR_DAY=0` to skip) |
| `TTU_SAFETY_RADAR_CACHE` | `~/.cache/ttu-radar` | cached basemaps (tiles fetched once each) |
| `TTU_SAFETY_CONN` | `1` | enable the connectivity watchdog (`0` disables) |
| `TTU_SAFETY_OFFLINE_UNSAFE_SEC` | `3600` | UNSAFE after this long with no internet |
| `TTU_SAFETY_CONN_PROBE_INTERVAL` | `300` | seconds between reachability probes |

Thresholds (sun `>0°`, humidity `>95%`, WU sun-gate `<5°`) are in `safety/config.py`.

**Coverage scope:** NWS and MRMS are US products and GLM is GOES-East; at a site outside
their coverage each component **disables itself with a loud log/event** rather than
reporting a false "clear". The WU rain and Open-Meteo-style layers work globally.

**Shared files:** the page↔daemon exchange (`safety_inputs.json`, `safety_state.json`)
lives in `/dev/shm` (RAM — no SD wear, cleared on reboot). Both sides hardcode matching
defaults; when deploying this change, update the page and the daemon together (a mixed
pair fails safe: the daemon just sees missing inputs and reports UNSAFE until both match).
Make sure the user running the page loop can write `/var/www/html` (e.g.
`sudo chown kirx /var/www/html`), and consider logrotate for the shell-launcher logs.

### Connectivity watchdog

A lightweight probe (day and night, every 5 min) checks whether **any** online service host
is reachable. If **none** are reachable for **1 h**, the monitor declares **UNSAFE** — we've
been blind to rain/lightning/forecast that long and can't trust "safe". It **auto-resolves**
the instant a probe succeeds. Unlike the per-service components (which fail to *unknown*, no
veto), this is a **hard veto** — it exists to catch the case where every online layer is
silently unavailable (no internet). A response of any kind (even an HTTP error) counts as
"reachable"; only a connection/DNS/timeout failure is "offline". A fresh daemon gets a full
1 h grace from startup.

### MRMS radar component

Every 5 min — **day and night** (the data is free) — the daemon pulls the latest **MRMS composite reflectivity** (NOAA
via the Iowa Environmental Mesonet, free/no key) and declares **UNSAFE if any echo ≥ 20 dBZ
is within 50 km** of the dome — a deliberately simple radius, no upwind logic. It also
renders a **TTU-centered radar thumbnail** (dark OSM/Carto tiles, **cached to disk so they
aren't re-downloaded each cycle**) with the **50 km ring** and **10 km / 10 mi scale bars**,
written beside `status.html`; the observatory page shows it with attribution and a source
note. **Two versions are rendered — a dark map for the night page style and a light
(`ttu_radar_day.png`) map for the day style — and CSS shows whichever matches the page's
day/night toggle.** Live check: unsafe while rain is in the ring **and for 15 min after the
last in-ring detection** — a freeze that clear frames do not cancel, so the roof does not
reopen the moment a cell's edge leaves the ring (it also covers the feed going blind). A
fetch error or stale frame → *unavailable*, which does not veto on its own. The
slow tile/radar fetch + render runs in its own thread, so it never delays page/monitor
refresh. Needs Pillow (`sudo apt install python3-pil`); absent → radar disabled.

### GLM lightning component

Every 5 min **while the sun is below 5°** (night, like WU rain) the daemon fetches the last
5 min of **GOES-19 GLM total-lightning** granules from AWS Open Data (anonymous S3, no key)
and **latches UNSAFE for 30 min** (the freeze time) if any flash is within **50 km**. Parsing is **entirely in
RAM** (in-memory netCDF, `/dev/shm` fallback) — **no SD-card writes** — and the slow
S3/netCDF poll runs in its own thread, so it never blocks `evaluate()` or the status page.
Needs `numpy` + `netCDF4` (`sudo apt install python3-numpy python3-netcdf4`); if absent, GLM
is disabled and the other layers are unaffected. Both status pages show the trigger state and
the distance to the nearest strike. Fail-safe: a fetch error never clears an active latch and
never forces unsafe on its own; "no flashes" is never proof of safety.

### NWS forecast component

A pre-emptive layer: every 15 min the daemon pulls the free **NWS gridpoint forecast**
(api.weather.gov, no key) and flags **UNSAFE if THIS hour or NEXT hour** exceeds any of:
cloud cover > 70%, precip probability > 20%, thunder probability > 15%. A fetch error or a
stale forecast is treated as *unavailable* (does not by itself flip unsafe — it's a
forecast, not a local sensor); a breach in a fresh forecast does. Both the daemon's
`/setup` page and the observatory status page show the inputs, the conclusion, and (on the
observatory page) a 12-hour forecast table with a source note.

## Test / develop off the Pi

The daemon needs no Pi hardware (it only reads JSON and polls WU over the network):

```bash
pip install flask waitress pytest flake8
python -m pytest safety/tests -q                       # 20 unit tests
# smoke test: feed inputs and hit the API
TTU_SAFETY_INPUTS_FILE=/tmp/in.json TTU_SAFETY_HTTP_PORT=11234 python safety_monitor.py &
echo '{"ts":'$(date +%s)',"sun_altitude_deg":-10,"humidity_pct":40}' > /tmp/in.json
curl -X PUT localhost:11234/api/v1/safetymonitor/0/connected -d Connected=true
curl localhost:11234/api/v1/safetymonitor/0/issafe          # -> Value: true
```
