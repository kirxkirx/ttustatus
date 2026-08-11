# ttustatus

Software running on the observatory Raspberry Pi. Two cooperating pieces:

1. **Status page** (`make_status_page.py`) — regenerates `/var/www/html/status.html` every
   ~90 s from the Pi's sensors: GPS, GPS-disciplined NTP/chrony, enclosure temp/humidity
   (DHT11), sun altitude/twilight (astropy), and an enclosure camera snapshot.
2. **Alpaca SafetyMonitor** (`safety_monitor.py` + `safety/`) — a small always-on daemon
   that aggregates **sun altitude**, **humidity**, **Weather Underground rain**, the
   **NWS forecast**, **GOES GLM lightning**, **MRMS radar** and an **internet-loss
   watchdog** into a single `IsSafe` boolean, served as an ASCOM **Alpaca SafetyMonitor**
   so NINA can react (park/close on unsafe). The status page shows the monitor's state,
   endpoint, inputs, radar map, and log.

Safety logic is fail-safe: anything unknown/stale ⇒ never silently safe. See
**[README_SAFETY.md](README_SAFETY.md)** for the full design and reference.

## Hardware (TTU Skyview deployment)

A **Raspberry Pi** single-board computer running Raspberry Pi OS, on an SD card, with:

- a **GPS receiver** read via `gpsd` — it serves two roles: the site position (measured
  once; the station is static) and a PPS pulse that disciplines `chrony` into a
  stratum-1 NTP server;
- a **DHT11 temperature/humidity sensor** on GPIO 17 (enclosure conditions, and the
  humidity input of the safety monitor).

Everything here is plain Python on stock Raspberry Pi OS packages — no special HATs or
drivers are assumed beyond the above. The same code should run on any Pi-class Linux
board with those two peripherals attached.

## Site coordinates (deploying at another observatory)

The measured GPS position propagates automatically: the status page writes its GPS fix
into the shared inputs file, and the safety daemon — unless `TTU_SAFETY_LAT/LON` are set —
**adopts it once at startup**, rounded to ~100 m so GPS jitter never re-derives anything.
All derived values (NWS forecast grid, WU station set, radar/GLM rings, cached basemap
tiles) follow the adopted coordinates. If the configured and measured positions ever
disagree by more than ~100 m, a loud warning appears on the status page and `/setup` —
but it never vetoes observing by itself. Components that don't cover the site (MRMS is
CONUS-only, GLM is GOES-East) disable themselves loudly instead of reporting a false
"clear".

## SD-card wear

High-frequency transient files (safety inputs/state, page sensor cache) live in
`/dev/shm` (RAM); the daemon writes its state file only on a decision change or a slow
heartbeat. **Camera processing also runs on the RAM disk**: the night pipeline's
DNG/TIFF intermediates (GBs per cycle) are created in `/dev/shm`, converted and
averaged in batches with deletion as it goes, and ImageMagick's pixel-cache spill is
pointed there too — if the RAM disk is too small for the RAW pipeline, capture
degrades cleanly to JPEG-only stacking. The remaining regular SD writes are the page
itself, the final snapshot, and the night radar thumbnails. Consider `logrotate` (or a
size cap) for `~/statuspage.log` and `~/safety_monitor.log` if you use the shell
launchers.

## Layout

```
make_status_page.py     status-page generator (run every 90 s)
run_status_page.sh       loop launcher for the status page (via @reboot cron)
safety_monitor.py        Alpaca SafetyMonitor daemon entry point
run_safety_monitor.sh    loop launcher for the daemon (alternative to systemd)
safety/                  the daemon package (config, wu_poll, monitor, alpaca, ...)
tools/                   alpaca_discovery_proxy.py / responder (only for cross-subnet NINA)
ttustatus.env.example    template for the secrets file (WU API key)
```

## Deploy (Raspberry Pi)

```bash
# 1. dependencies (Raspberry Pi OS blocks system-wide pip; use apt)
sudo apt update && sudo apt install -y python3-flask python3-waitress git

# 2. clone
cd ~ && git clone https://github.com/kirxkirx/ttustatus.git   # public repo: no credentials needed

# 3. secrets (WU API key) — kept OUTSIDE the repo, never committed
cp ~/ttustatus/ttustatus.env.example ~/ttustatus.env
nano ~/ttustatus.env          # set TTU_SAFETY_WU_KEY=<your key>
chmod 600 ~/ttustatus.env

# 4. status page — @reboot cron (crontab -e):
#    @reboot /home/kirx/ttustatus/run_status_page.sh >/home/kirx/statuspage.log 2>&1 &

# 5. safety daemon — systemd (see README_SAFETY.md "systemd" for the unit file)
sudo systemctl enable --now ttu-safety
```

Then connect NINA to the Alpaca SafetyMonitor (device 0, port 11111). If NINA and the Pi are
on different subnets, see README_SAFETY.md ("Connecting NINA").

## Develop / test

```bash
python3 -m venv venv && venv/bin/pip install flask waitress pytest flake8
venv/bin/python -m pytest safety/tests -q        # unit + smoke tests (hardware stubbed)
venv/bin/flake8 --max-line-length=100 --extend-ignore=E203,E501,W503,E402 safety
```

CI (GitHub Actions) runs the same on every push. `make_status_page.py` is smoke-tested with
its Raspberry Pi hardware libraries (`board`, `adafruit_dht`, `gps`, `astropy`) mocked, so it
lints and its page-rendering functions run without a Pi.
