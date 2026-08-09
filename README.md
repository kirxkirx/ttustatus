# ttustatus

Software running on the observatory Raspberry Pi. Two cooperating pieces:

1. **Status page** (`make_status_page.py`) — regenerates `/var/www/html/status.html` every
   ~90 s from the Pi's sensors: GPS, GPS-disciplined NTP/chrony, enclosure temp/humidity
   (DHT11), sun altitude/twilight (astropy), and an enclosure camera snapshot.
2. **Alpaca SafetyMonitor** (`safety_monitor.py` + `safety/`) — a small always-on daemon
   that aggregates **sun altitude**, **humidity**, and **Weather Underground rain** into a
   single `IsSafe` boolean, served as an ASCOM **Alpaca SafetyMonitor** so NINA can react
   (park/close on unsafe). The status page shows the monitor's state, endpoint, inputs, and
   log as its own section.

Safety logic (fail-safe: anything unknown/stale ⇒ unsafe): unsafe when the sun is above the
horizon (>0°), humidity >95%, or **any** nearby WU station reports rain (which then latches
unsafe for 3 h after the last rain). WU is polled only when the sun is below 5° to save API
calls. See **[README_SAFETY.md](README_SAFETY.md)** for the full design and reference.

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
cd ~ && git clone git@github.com:<you>/ttustatus.git

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
