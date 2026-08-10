# TTU Alpaca SafetyMonitor

An ASCOM Alpaca **SafetyMonitor** for the observatory, running on the Raspberry Pi
alongside `make_status_page.py`. It aggregates several inputs into a single `IsSafe`
boolean that NINA (and any Alpaca client) can read, and the status page shows the same
information plus the recent safety-event log.

## Architecture (two cooperating processes)

`make_status_page.py` is **ephemeral** (run every 90 s by `run_status_page.sh`), so the
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
confirmation, no second reading — and it stays unsafe for **3 hours after the last** rain
seen at any station. The latch is persisted, so a daemon restart or reboot keeps it.

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
3. Set the WU API key (see the next section), then start the daemon via systemd (below).
   The status page runs from a `@reboot` cron:
   ```
   @reboot /home/kirx/ttustatus/run_status_page.sh >/home/kirx/statuspage.log 2>&1 &
   ```

### systemd (recommended, for the safety daemon)

```ini
# /etc/systemd/system/ttu-safety.service
[Unit]
Description=TTU Alpaca SafetyMonitor
After=network-online.target
Wants=network-online.target

[Service]
User=kirx
WorkingDirectory=/home/kirx/ttustatus
EnvironmentFile=/home/kirx/ttustatus.env
ExecStart=/usr/bin/python3 /home/kirx/ttustatus/safety_monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

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

If you use `run_safety_monitor.sh` instead of systemd, load the file before launch by
adding `set -a; . /home/kirx/ttustatus.env; set +a` above the `exec` line. Quick manual
test: `TTU_SAFETY_WU_KEY=<key> python3 safety_monitor.py`.
```bash
sudo systemctl enable --now ttu-safety
journalctl -u ttu-safety -f      # watch events live
```

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
| `TTU_SAFETY_RAIN_LATCH_HOURS` | `3.0` | how long UNSAFE persists after rain |
| `TTU_SAFETY_INPUTS_STALE_SEC` | `600` | older inputs → fail-safe UNSAFE |
| `TTU_SAFETY_WU_KEY` | **(required)** | Weather Underground PWS API key — set via env, never commit (see above) |
| `TTU_SAFETY_LAT` / `_LON` | TTU | geocode for nearest-station discovery |
| `TTU_SAFETY_INPUTS_FILE` / `_STATE_FILE` / `_LATCH_FILE` / `_EVENT_LOG` | see `config.py` | file paths |
| `TTU_SAFETY_NWS` | `1` | enable the NWS forecast component (`0` disables) |
| `TTU_SAFETY_NWS_UA` | `ttu-safety-monitor` | User-Agent NWS asks for (add a contact) |
| `TTU_SAFETY_NWS_GRID` | (auto) | e.g. `LUB/46,41`; skips the `/points` lookup |
| `TTU_SAFETY_NWS_POLL_INTERVAL` | `900` | seconds between NWS forecast pulls (15 min) |
| `TTU_SAFETY_NWS_CLOUD_MAX` / `_PRECIP_MAX` / `_THUNDER_MAX` | `70` / `15` / `10` | % thresholds (unsafe when exceeded, this or next hour) |
| `TTU_SAFETY_GLM` | `1` | enable the GLM lightning component (`0` disables) |
| `TTU_SAFETY_GLM_TRIGGER_KM` | `50` | flash within this radius → UNSAFE |
| `TTU_SAFETY_GLM_COOLOFF_HOURS` | `3.0` | latch duration after the last nearby flash |
| `TTU_SAFETY_GLM_POLL_INTERVAL` | `300` | seconds between GLM polls (night only) |
| `TTU_SAFETY_GLM_WINDOW_MIN` | `5` | look-back minutes fetched per poll |

Thresholds (sun `>0°`, humidity `>95%`, WU sun-gate `<5°`) are in `safety/config.py`.

### GLM lightning component

Every 5 min **while the sun is below 5°** (night, like WU rain) the daemon fetches the last
5 min of **GOES-19 GLM total-lightning** granules from AWS Open Data (anonymous S3, no key)
and **latches UNSAFE for 3 h** if any flash is within **50 km**. Parsing is **entirely in
RAM** (in-memory netCDF, `/dev/shm` fallback) — **no SD-card writes** — and the slow
S3/netCDF poll runs in its own thread, so it never blocks `evaluate()` or the status page.
Needs `numpy` + `netCDF4` (`sudo apt install python3-numpy python3-netcdf4`); if absent, GLM
is disabled and the other layers are unaffected. Both status pages show the trigger state and
the distance to the nearest strike. Fail-safe: a fetch error never clears an active latch and
never forces unsafe on its own; "no flashes" is never proof of safety.

### NWS forecast component

A pre-emptive layer: every 15 min the daemon pulls the free **NWS gridpoint forecast**
(api.weather.gov, no key) and flags **UNSAFE if THIS hour or NEXT hour** exceeds any of:
cloud cover > 70%, precip probability > 15%, thunder probability > 10%. A fetch error or a
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
