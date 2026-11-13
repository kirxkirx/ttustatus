[![CI](https://github.com/kirxkirx/ttustatus/actions/workflows/ci.yml/badge.svg)](https://github.com/kirxkirx/ttustatus/actions/workflows/ci.yml)
[![Raspberry Pi OS](https://github.com/kirxkirx/ttustatus/actions/workflows/raspios.yml/badge.svg)](https://github.com/kirxkirx/ttustatus/actions/workflows/raspios.yml)

# ttustatus

Software running on the observatory Raspberry Pi. Two cooperating pieces:

1. **Status page** (`make_status_page.py`) — regenerates `/var/www/html/status.html` every
   ~90 s from the Pi's sensors: GPS, GPS-disciplined NTP/chrony, enclosure temp/humidity
   (DHT11), sun altitude/twilight (astropy), and — optionally — an enclosure camera
   snapshot (disabled by default; `TTU_STATUS_CAMERA=1` re-enables it).
2. **Alpaca SafetyMonitor** (`safety_monitor.py` + `safety/`) — a small always-on daemon
   that aggregates **sun altitude**, **humidity**, **Weather Underground rain**, the
   **NWS forecast**, **GOES GLM lightning**, **MRMS radar** (from NOAA's AWS bucket or the
   Iowa Environmental Mesonet, whichever has the newer frame), **NWS warnings** (a Tornado,
   Dust Storm or High Wind Warning over the site, for the warning's duration), an
   **internet-loss watchdog** and a **clock check** (unsafe until chrony is synchronised
   to the GPS or an NTP server) into a single `IsSafe` boolean, served as an ASCOM **Alpaca
   SafetyMonitor** (device 0) so NINA can react (park/close on unsafe). The status page
   shows the monitor's state, endpoint, inputs, radar map, and log, plus a **Hazards** section:
   every active NWS alert on the map, and information-only smoke, wildfires, SPC outlook
   and mesoscale discussions and storm reports, also drawn on the radar map. Only those
   three (configurable) warnings can close the roof; everything else is shown, never
   gated on.

Safety logic is fail-safe: anything unknown/stale ⇒ never silently safe. See
**[README_SAFETY.md](README_SAFETY.md)** for the full design and reference.

## The two Alpaca SafetyMonitor devices

The daemon exposes **two** ASCOM Alpaca SafetyMonitor devices on the same address and
port (11111). They answer different questions, and only device 0 is a safety verdict.

| | Device 0: `TTU Safety Monitor` | Device 1: `TTU Dew Monitor` |
|---|---|---|
| What it is | the normal safety monitor | a special-purpose monitor |
| `IsSafe` is `true` | while it is safe to observe: the sun is down, the Pi's own readings are current and none of the checks (humidity, rain, forecast, lightning, radar, NWS warnings, internet link) reports a hazard | only while dew is expected to form: device 0 is SAFE **and** the night is clear, calm and humid, with no rain in the forecast and no lightning within 60 km |
| Who uses it | **NINA and any other observatory or dome control software** | **only this observatory's own custom dome-control code** (the status page calls it "the dome driver") |
| What for | parking the telescope and closing the roof when conditions turn unsafe | temporarily disabling the dome's rain sensor, which is susceptible to dew |

**Device 0 is the one to select** in NINA and in any other software that needs to know
whether observing is safe.

**Device 1 is not a safety verdict.** The dome has a rain sensor of its own, a bare
plate under the night sky. Dew forms on it on a clear, calm, humid night; the sensor
then reads "wet" and the roof closes in perfectly good weather. Device 1 turns `true`
only when dew is expected, and this observatory's own dome-control code reads it to
disable (ignore) that rain sensor for as long as it stays `true`. Nothing else should
use it, and in particular not the dome or safety-monitor settings of NINA:

- Its `false` does not mean "unsafe". On a dry or windy night, in perfectly good
  observing weather, it reads `false`.
- Its `true` does not mean more than device 0's: it is never `true` while device 0 is
  not. Selected by mistake as a safety monitor it therefore errs to the cautious side
  (lost observing time, never a roof left open).
- It never opens or closes anything. `false` only hands the decision back to the rain
  sensor.
- An observatory with no use for it removes it with `TTU_SAFETY_DEW=0`.

The exact rule of each device is in README_SAFETY.md: "Safety logic" for device 0,
"Dew monitor (Alpaca device 1)" for device 1.

## Sister projects

- [local-weather-awareness](https://github.com/kirxkirx/local-weather-awareness): local
  weather pages for any list of US places, from public data: NWS forecasts and alerts, MRMS
  radar maps, New Mexico emergency road closures. It is derived from this repository (the
  page style, the MRMS radar and basemap code, the NWS client). Example:
  https://tau.kirx.net/myweather/.
- [vlbaweather](https://github.com/kirxkirx/vlbaweather): the weather at the ten VLBA
  antenna sites, the VLA and the GBT, with a GOOD / BAD verdict per site that applies this
  repository's weather cuts to public data. Live page: https://tau.kirx.net/vlbaweather/.

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
tiles) follow the adopted coordinates: a radar map drawn before the adoption is rebuilt
for the adopted site right after the next radar poll, from that site's cached map where
there is one. A new site has no cached CARTO map, so it needs `TTU_SAFETY_CARTO_KEY` for
one; otherwise the radar maps use OpenStreetMap. If the configured and measured positions ever
disagree by more than ~100 m, a loud warning appears on the status page and `/setup` —
but it never vetoes observing by itself. Components that don't cover the site (MRMS is
CONUS-only, GLM is GOES-East) disable themselves loudly instead of reporting a false
"clear".

## SD-card wear

High-frequency transient files (safety inputs/state, page sensor cache) live in
`/dev/shm` (RAM); the daemon writes its state file only on a decision change or a slow
heartbeat. **The camera is disabled by default** (`TTU_STATUS_CAMERA=1` in `ttustatus.env`
re-enables it) — while off there are no captures, no stacking and no image writes at
all. When enabled, **camera processing runs on the RAM disk**: the night pipeline's
DNG/TIFF intermediates (GBs per cycle) are created in `/dev/shm`, converted and
averaged in batches with deletion as it goes, and ImageMagick's pixel-cache spill is
pointed there too — if the RAM disk is too small for the RAW pipeline, capture
degrades cleanly to JPEG-only stacking. The remaining regular SD writes are the page
itself, the final snapshot, and the radar thumbnails (two ~80 KB PNGs per 5 min, plus a
redraw when the hazard overlays change; like the page, they go to `/dev/shm` behind a
symlink by default — see README_SAFETY.md). The NWS-warning latch
(`~/safety_hazard_latch.json`) is written only when a vetoing warning starts, changes or
ends; NWS zone outlines are cached once per zone in `~/.cache/ttu-hazards/`; the hazard
information feeds keep everything in RAM. The weather log collects its rows in
`/dev/shm` and appends them to monthly CSV files in `/var/www/html/weather_log/` once a
day, in daytime, and when the daemon is stopped (README_SAFETY.md, "Weather log").
Consider `logrotate` (or a size cap) for `~/statuspage.log` and `~/safety_monitor.log` if
you use the shell launchers.

## Layout

```
make_status_page.py     status-page generator (run every 90 s)
run_status_page.sh       manual fallback loop for the status page (normally not needed)
safety_monitor.py        Alpaca SafetyMonitor daemon entry point
run_safety_monitor.sh    loop launcher for the daemon (alternative to systemd)
safety/                  the daemon package (config, wu_poll, monitor, alpaca, ...)
  nws_alerts.py          NWS active alerts: the Tornado/Dust Storm/High Wind Warning veto + map/list
  hazard_feeds.py        information-only hazard feeds (HMS, NIFC, SPC, LSR)
  dew.py                 the dew monitor (Alpaca device 1), for the observatory's own dome code
  weatherlog.py          weather log (CSV): collected in RAM, copied to the SD card once a day
tools/                   alpaca_discovery_proxy.py / responder (only for cross-subnet NINA)
ttustatus.env.example    template for the secrets file (WU key, User-Agent contact, CARTO key)
```

## Deploy (Raspberry Pi)

```bash
# 1. dependencies (Raspberry Pi OS blocks system-wide pip; use apt). chrony is required:
#    the safety monitor reads UNSAFE until `chronyc tracking` shows the clock synchronised
#    to the GPS or an NTP server. python3-pil / -numpy / -netcdf4: the radar and GLM layers.
sudo apt update && sudo apt install -y python3-flask python3-waitress git chrony \
    python3-pil python3-numpy python3-netcdf4

# 2. clone
cd ~ && git clone https://github.com/kirxkirx/ttustatus.git   # public repo: no credentials needed

# 3. secrets + contact — kept OUTSIDE the repo, never committed
cp ~/ttustatus/ttustatus.env.example ~/ttustatus.env
nano ~/ttustatus.env          # set TTU_SAFETY_WU_KEY=<your key>
                              # uncomment TTU_SAFETY_NWS_UA and put YOUR REAL e-mail in it
                              # optional: TTU_SAFETY_CARTO_KEY=<key> (CARTO radar maps)
chmod 600 ~/ttustatus.env

# 4. install ONE systemd service — the daemon also runs the status page:
sudo ~/ttustatus/deploy/install.sh
```

The env file (step 3), in short — details in README_SAFETY.md:

- **`TTU_SAFETY_NWS_UA` with a real e-mail address is crucial.** It is the User-Agent of
  the daemon's NWS, radar, hazard-feed and map-tile requests:
  `TTU_SAFETY_NWS_UA="ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; you@example.org)"`
  with `you@example.org` **replaced by your own address** (keep the double quotes).
  OpenStreetMap blocks a placeholder such as `you@example.org`, so the daemon then
  requests no OpenStreetMap tiles at all and a radar map with no CARTO basemap is left
  without one; NWS asks for a contact too. The daemon's log warns at startup while the
  address is missing or a placeholder; the status page and `/setup` warn whenever
  OpenStreetMap is (or would be) used without a real address.
- **Radar basemap: CARTO first, OpenStreetMap as the backup.** CARTO maps already cached
  on the Pi (`~/.cache/ttu-radar/basemap_*.png`) keep being used with no network and no
  key. New CARTO tiles need a free CARTO API key (`TTU_SAFETY_CARTO_KEY`, requested by
  e-mail at [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey)); without one
  CARTO serves only an "API KEY REQUIRED" watermark, so the daemon never asks it and uses
  OpenStreetMap tiles instead. The status page says which basemap each map shows. A
  cached map's file name depends on the site and on `TTU_SAFETY_RADAR_THUMB_PX`,
  `TTU_SAFETY_RADAR_THUMB_HALF` and `TTU_SAFETY_RADAR_TILE_ZOOM`: change any of them and
  the old CARTO maps are left unused (without a key the new ones come from
  OpenStreetMap).
- **A cached CARTO map that shows the watermark** (one built after CARTO began requiring
  a key): delete **only that file**. The daemon logs which file each map uses:
  ```bash
  journalctl -u ttu-safety | grep 'basemap loaded from cache' | tail -2
  # radar night basemap loaded from cache /home/kirx/.cache/ttu-radar/basemap_<hash>.png ...
  mkdir -p ~/basemap-watermarked && mv ~/.cache/ttu-radar/basemap_<hash>.png ~/basemap-watermarked/
  sudo systemctl restart ttu-safety
  ```
  Keep the good ones: without `TTU_SAFETY_CARTO_KEY` a deleted CARTO map cannot be
  rebuilt from CARTO — it comes back as OpenStreetMap. (`rm ~/.cache/ttu-radar/basemap_*.png`
  starts over with every map, the good CARTO ones included.)

That's the whole deployment: **one service**. The safety daemon spawns
`make_status_page.py` every ~90 s as an isolated subprocess (a page crash or hang is
logged and killed after 30 min — it can never take the safety logic down). Logs go to
journald (`journalctl -u ttu-safety -f`) with automatic rotation. A code update needs
only `sudo systemctl restart ttu-safety`; rerun the installer if the unit file changed.
(Migrating from the old setup: remove the `@reboot` line from `crontab -e` and
`pkill -f run_status_page.sh`.)

Then connect NINA to the Alpaca SafetyMonitor **device 0** (`TTU Safety Monitor`, port
11111). If NINA and the Pi are on different subnets, see README_SAFETY.md ("Connecting
NINA").

Alpaca discovery also lists **device 1**, `TTU Dew Monitor`. Do not select it in NINA: it
is the special-purpose dew monitor described under
[The two Alpaca SafetyMonitor devices](#the-two-alpaca-safetymonitor-devices), not a
safety verdict. See README_SAFETY.md ("Dew monitor") for its rule and ("Weather log") for
the downloadable CSV log that goes with it.

## Develop / test

```bash
python3 -m venv venv && venv/bin/pip install flask waitress pytest flake8
venv/bin/python -m pytest safety/tests -q        # unit + smoke tests (hardware stubbed)
venv/bin/flake8 --max-line-length=100 --extend-ignore=E203,E501,W503,E402 safety
```

CI (GitHub Actions) runs the same on every push. `make_status_page.py` is smoke-tested with
its Raspberry Pi hardware libraries (`board`, `adafruit_dht`, `gps`, `astropy`) mocked, so it
lints and its page-rendering functions run without a Pi.

## License

[0BSD](LICENSE), the BSD Zero Clause License: anyone may use, copy, modify and distribute
this code for any purpose, with or without fee and with no conditions attached; it comes
without any warranty. The weather data and map tiles it fetches are not covered by this:
they come from their providers under their own terms.
