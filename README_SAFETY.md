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
make_status_page.py  --writes-->  /dev/shm/safety_inputs.json  (sun altitude, humidity)
safety_monitor.py    --reads--/
safety_monitor.py    --writes-->  /dev/shm/safety_state.json   (IsSafe + inputs + events)
make_status_page.py  --reads--/   (renders the "Safety monitor" page section)
```
(RAM-backed `/dev/shm`, deliberately: these files are rewritten every minute or two and
must never wear the SD card; both are regenerated within seconds of a restart.)

Durable files (survive reboot): `~/safety_latch.json` (the rain latch), the GLM / radar /
NWS-warning latches beside it, and `~/safety_events.log` (the audit trail, also mirrored
to stdout).

## Safety logic

`IsSafe = SAFE` only if **all** hold (anything else → `UNSAFE`, fail-safe):

| Input | Unsafe when | Source |
|------|-------------|--------|
| Sun altitude | `> 0°` (no refraction / angular-size correction) | make_status_page.py |
| Humidity | `> 95 %` (clears again below 93 %, small hysteresis) | make_status_page.py (DHT) |
| Rain | **any** WU station reports `precipRate > 0` | this daemon |
| Inputs freshness | `safety_inputs.json` missing or older than 10 min | fail-safe |
| NWS warning | a **Tornado / Dust Storm / High Wind Warning** (`TTU_SAFETY_HAZARD_VETO_EVENTS`) in effect **over the site** — NWS's point query, the warning's polygon, or one of the site's zones (TXZ035 / TXC303) — for the warning's whole duration, persisted | this daemon |

The NWS forecast, GLM lightning, MRMS radar and connectivity layers are described in their
own sections below. **Every other NWS alert and all the non-NWS hazard information** (USGS
earthquakes, NOAA HMS smoke, NIFC wildfires, SPC outlook and mesoscale discussions, storm
reports, space weather) is shown on the status page, `/setup` and the radar map but
**never affects `IsSafe`**.

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

## Clock robustness

The Pi has no battery-backed RTC: after a power cut it boots with whatever time
`fake-hwclock` last saved, which can be **months wrong** until NTP syncs (the 2026-08-25
incident: a boot under a March clock read the persisted August latches as *"rain latch
active (227329 min left)"*). The daemon defends itself:

- **Persisted latch timestamps are clamped** at load *and* at every check: a rain latch
  can never exceed one full `RAIN_LATCH_HOURS`, GLM one cool-off, the radar freeze one
  `RADAR_LATCH_SEC` — whatever a wrong clock did, the worst case is one full latch
  period, fail-safe, then normal operation.
- A **clock step is detected** (wall vs monotonic drift > 30 s), logged CRITICAL, and
  recorded as a `CLOCK-STEP` event. On a step every layer **re-polls immediately**
  (GLM/radar fetch by date — data fetched under a wrong clock came from the wrong day),
  and a latch that the step would silently *evaporate* (armed under the old clock,
  "expired" under the new one) is **re-armed for its full duration** — the rain was
  recent in real time regardless of what the clock said.
- Pollers treat a **backward** step as "poll now" instead of stalling until the wall
  clock catches up; the connectivity watchdog runs entirely on the monotonic clock, so
  steps can neither fake a huge offline window nor neutralize it.
- **NWS warning vetoes** hold the warning's *absolute* NWS end time: a backward step keeps
  each veto for exactly the time it had left, a forward step never holds one past the NWS
  end, a veto that has started stays in effect across a step, and an early release needs
  point and area queries made *after* the step. A persisted veto first seen "in the
  future" is clamped at load like the other latches. The hazard information feeds drop
  everything fetched under the old clock and re-poll.
- The NWS forecast **re-ages**: if the poller goes silent the forecast flips to
  *unavailable* within two poll intervals instead of presenting frozen data forever, a
  forecast dated in the future (wrong clock) is never presented as current, and the
  page shows *why* it is unavailable (e.g. the TLS "certificate is not yet valid"
  failures a wrong clock causes).

None of this replaces NTP — until the clock syncs, date-derived data sources (GLM S3
prefixes, MRMS frame URLs) still fetch the wrong day's data. It bounds the damage and
makes the condition loud instead of silent.

**Pi ops — keep the boot clock close:** `fake-hwclock` is NOT installed on every image
(it was absent on the observatory Pi — the 2026-08 incident's "March" boot time came from
systemd-timesyncd's `/var/lib/systemd/timesync/clock`, which older systemd only rewrites
on a *clean* shutdown, so a power cut restored the previous boot's date). Install it and
add a frequent save:
`sudo apt install fake-hwclock`, then
`echo '*/10 * * * * root /sbin/fake-hwclock save >/dev/null 2>&1' | sudo tee
/etc/cron.d/fake-hwclock-frequent`, and confirm `/etc/fake-hwclock.data` tracks
`date -u`. (If saves ever stop, check for a read-only root / Overlay FS blocking writes
to `/etc`.) The real fix is a DS3231 RTC module (`dtoverlay=i2c-rtc,ds3231`), which
survives power cuts without any writable filesystem.

## SD-card wear

The Pi runs 24/7 from an SD card on imperfect power, so every recurring write matters
(a brownout mid-write is how SD cards die). What goes where, by design:

| Path | Written | Medium |
|---|---|---|
| `/dev/shm/safety_inputs.json`, `/dev/shm/safety_state.json`, page cache | every 60–90 s | RAM |
| `/dev/shm/ttu_radar*.png` + `/dev/shm/status.html` (symlinked from `/var/www/html`) | every 90 s / 5 min | RAM |
| `~/safety_latch.json`, `~/safety_glm_latch.json`, `~/safety_radar_latch.json` | only on rain/lightning events | SD (must survive reboot) |
| `~/safety_hazard_latch.json` | only when the set of vetoing NWS warnings (or one's end time) changes — a few writes per warning; written by the alerts thread only, never on an IsSafe request; a failed write is retried at most once a minute | SD (must survive reboot) |
| `~/safety_events.log` (audit trail) | only on events | SD (its purpose) |
| `~/.cache/ttu-radar/` basemaps | once ever (per site and theme; never a partial one, never one with a refused tile) | SD (avoids re-fetching OSM tiles per boot) |
| `~/.cache/ttu-hazards/zones/<type>_<ID>.json` (NWS zone outlines), `points_<lat>_<lon>.json` (the site's zones) | once per zone (never re-fetched while the site stays put); the site's zones only if NWS changes them | SD (~200 KB for a three-state event: simplified outlines near the map, bounding boxes far away) |

The hazard information feeds (`safety/hazard_feeds.py`) write nothing at all: their state
lives in RAM. A change in the hazard overlays re-renders the two radar PNGs (into
`/dev/shm` by default, like every radar write).

The radar PNGs and the page were the big movers (~150–250 MB/day and ~30 MB/day of SD
writes respectively before the symlink scheme; ~0 after). The web-root files become
one-time **symlinks into `/dev/shm`** — Debian's Apache/nginx/lighttpd follow them out
of the box; after a reboot they dangle for at most one regeneration cycle. Opt out with
`TTU_SAFETY_RADAR_THUMB_SHM=0` / `TTU_STATUS_HTML_SHM=0`.

OS-side checklist (run on the Pi): the web server's **access log** is the big one — on
the observatory Pi, `fatrace -f W` showed Apache's access.log as essentially the only
high-frequency SD writer left. Disable it (keep error.log, which writes rarely and is
useful):
```bash
grep -Rn "CustomLog\|TransferLog" /etc/apache2/   # -R (not -r): *-enabled/ are symlinks
sudo sed -i 's|^\([[:space:]]*CustomLog\)|#\1|' /etc/apache2/sites-available/000-default.conf
sudo a2disconf other-vhosts-access-log
sudo apachectl configtest && sudo systemctl reload apache2
```
(edit files in `sites-available/`, not the `sites-enabled/` symlinks — `sed -i` would
silently replace a symlink with a regular file)
Also: bound journald (`/etc/systemd/journald.conf`: `SystemMaxUse=64M`) while keeping
`Storage=persistent` for reboot forensics; check swap (`swapon --show` — zram = RAM =
good; a `dphys-swapfile` swapfile lives ON the SD); `sudo systemctl disable --now
packagekit` on a headless Pi; confirm `noatime` on root (`findmnt -o OPTIONS /`); keep
chrony's drift file (it makes the clock accurate quickly after reboot); and re-check
empirically with `sudo fatrace -f W` for a minute. Optional, for maximum quiet — the two
remaining once-a-day bursts: `sudo systemctl mask man-db.timer` (free on a headless box)
and `sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer` (then run
`apt update` manually before installing anything). Daily-burst volume is small; neither
is required.

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
| `TTU_SAFETY_WU_BACKOFF_AFTER` / `_BACKOFF_RETRY` | `6` / `3600` | after N consecutive silent polls a station is probed only every RETRY seconds (saves API calls on dead hardware); it rejoins full cadence automatically on the first answer |
| `TTU_SAFETY_WU_EXCLUDE` | `KTXSHALL25` | comma-separated station IDs excluded from rain detection — reserve for stations reporting *bogus* data; merely dead ones are handled by the automatic backoff. Does not self-heal; set to empty to clear |
| `TTU_SAFETY_LOCAL_TZ` | `America/Chicago` | timezone of the forecast table (invalid ⇒ loud UTC fallback) |
| `TTU_SAFETY_STATE_HEARTBEAT_SEC` | `60` | max interval between unchanged state-file writes (SD-wear throttle) |
| `TTU_SAFETY_INPUTS_FILE` / `_STATE_FILE` / `_LATCH_FILE` / `_EVENT_LOG` | see `config.py` | file paths |
| `TTU_SAFETY_NWS` | `1` | enable the NWS forecast component (`0` disables) |
| `TTU_SAFETY_NWS_UA` | `ttu-safety-monitor` | User-Agent NWS asks for (add a contact) |
| `TTU_SAFETY_NWS_GRID` | (auto) | e.g. `LUB/46,41`; skips the `/points` lookup |
| `TTU_SAFETY_NWS_POLL_INTERVAL` | `900` | seconds between NWS forecast pulls (15 min) |
| `TTU_SAFETY_NWS_CLOUD_MAX` / `_PRECIP_MAX` / `_THUNDER_MAX` | `45` / `20` / `15` | % thresholds (unsafe when exceeded, this or next hour) |
| `TTU_SAFETY_GLM` | `1` | enable the GLM lightning component (`0` disables) |
| `TTU_SAFETY_GLM_TRIGGER_KM` | `50` | flash within this radius → UNSAFE |
| `TTU_SAFETY_GLM_COOLOFF_HOURS` | `0.5` | freeze time (h) after the last nearby flash |
| `TTU_SAFETY_GLM_POLL_INTERVAL` | `300` | seconds between GLM polls (night only) |
| `TTU_SAFETY_GLM_WINDOW_MIN` | `5` | look-back minutes fetched per poll |
| `TTU_SAFETY_RADAR` | `1` | enable the MRMS radar component (`0` disables) |
| `TTU_SAFETY_RADAR_KM` | `30` | any echo within this radius → UNSAFE |
| `TTU_SAFETY_RADAR_DBZ` | `20` | reflectivity ≥ this counts as rain |
| `TTU_SAFETY_RADAR_TRIGGER_AFTER` | `2` | consecutive polls that must show an in-ring echo before it triggers (`1` = trigger on a single frame) |
| `TTU_SAFETY_RADAR_LATCH_SEC` | `900` | freeze time (s) after the last in-ring echo; clear frames do not cancel it |
| `TTU_SAFETY_RADAR_POLL_INTERVAL` | `300` | seconds between radar polls (day and night) |
| `TTU_SAFETY_RADAR_THUMB` | `/var/www/html/ttu_radar.png` | thumbnail path (beside status.html) |
| `TTU_SAFETY_RADAR_TILE_URL` | OpenStreetMap standard tiles | night basemap tile template (inverted, see next rows) |
| `TTU_SAFETY_RADAR_TILE_URL_DAY` | OpenStreetMap standard tiles | day basemap tile template (`TTU_SAFETY_RADAR_DAY=0` to skip) |
| `TTU_SAFETY_RADAR_TILE_DARK_INVERT` | `1` | night map: invert the lightness of light tiles (hue kept); a dark tile set is left alone |
| `TTU_SAFETY_RADAR_CACHE` | `~/.cache/ttu-radar` | cached basemaps (tiles fetched once each) |
| `TTU_SAFETY_CONN` | `1` | enable the connectivity watchdog (`0` disables) |
| `TTU_SAFETY_OFFLINE_UNSAFE_SEC` | `3600` | UNSAFE after this long with no internet |
| `TTU_SAFETY_CONN_PROBE_INTERVAL` | `300` | seconds between reachability probes |
| `TTU_SAFETY_HAZARDS` | `1` | enable the NWS active-alerts layer (`0` disables it: no warning veto, no alert lists or map areas) |
| `TTU_SAFETY_HAZARD_VETO_EVENTS` | `Tornado Warning,Dust Storm Warning,High Wind Warning` | NWS events that make the monitor UNSAFE while in effect over the site, for the warning's duration (comma list, case-insensitive; empty = every alert display-only, announced at startup; a name NWS does not know is kept but warned about) |
| `TTU_SAFETY_HAZARD_VETO_ONSET_LEAD_SEC` | `900` | a veto warning issued ahead of its onset vetoes from this long before the onset (shown as *scheduled* until then) |
| `TTU_SAFETY_HAZARD_POINT_POLL_SEC` | `60` | `/alerts/active?point=<site>` cadence — the veto's fast path (min 30) |
| `TTU_SAFETY_HAZARD_AREA_POLL_SEC` | `120` | `/alerts/active?area=<states>` cadence — the map, the lists, the local site test (min 60) |
| `TTU_SAFETY_HAZARD_STALE_SEC` | `600` | an older query result no longer counts (unavailable; no early release); min 2 × the slower of the point and area polls |
| `TTU_SAFETY_HAZARD_LATCH_FILE` | `~/safety_hazard_latch.json` | the persisted warning veto |
| `TTU_SAFETY_HAZARD_CACHE` | `~/.cache/ttu-hazards` | zone outlines (fetched once per zone) and the site's zones |
| `TTU_SAFETY_HAZARD_AREA_STATES` | `auto` | states for the area query: `auto` = those whose bounding box meets the map (+0.5°): TX, NM, OK for TTU; or a list such as `TX,NM`. An unknown code is dropped with a warning (api.weather.gov would reject the whole query), and a list naming none of the site's states gets them added |
| `TTU_SAFETY_HAZARD_FILL_ALPHA` | `60` | map fill opacity of alert areas, 0-255 |
| `TTU_SAFETY_HAZARD_FEEDS` | `1` | enable the information-only hazard feeds (`0` disables them) |
| `TTU_SAFETY_HAZARD_FEEDS_POLL_SEC` | `600` | information-feed cadence (min 60; each feed also has its own minimum, see below) |
| `TTU_SAFETY_HAZARD_QUAKE_KM` / `_QUAKE_MIN_MAG` | `300` / `2.5` | earthquakes listed within this radius and at or above this magnitude |
| `TTU_SAFETY_HAZARD_LSR_HOURS` | `24` | storm-report look-back, hours (1-168) |

Out-of-range hazard settings are corrected to the nearest allowed value, with a warning
at startup (log + `CONFIG` event).

Thresholds (sun `>0°`, humidity `>95%`, WU sun-gate `<5°`) are in `safety/config.py`.

**Coverage scope:** NWS (forecast and alerts) and MRMS are US products and GLM is
GOES-East; at a site outside their coverage each component **disables itself with a loud
log/event** rather than reporting a false "clear". The US-only information feeds (SPC,
storm reports, wildfires; HMS smoke: North America) say "outside coverage" instead of an
empty list. The WU rain and Open-Meteo-style layers work globally.

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
is within 30 km** of the dome — a deliberately simple radius, no upwind logic. It also
renders a **TTU-centered radar thumbnail** (OpenStreetMap tiles, lightness-inverted for
the night map, **cached to disk so they aren't re-downloaded each cycle**; CARTO's free
tiles, the former default, now return an "API KEY REQUIRED" watermark, and a tile the
server marks not cacheable — OSM's "access blocked" tile — is never used) with the **30 km ring** and **10 km / 10 mi scale bars**,
written beside `status.html`; the observatory page shows it with attribution and a source
note. **Two versions are rendered — a dark map for the night page style and a light
(`ttu_radar_day.png`) map for the day style — and CSS shows whichever matches the page's
day/night toggle.** An echo must appear on **two consecutive polls** (~5 min apart) before
it triggers: MRMS composites occasionally carry a one-frame artefact — an aircraft,
anomalous propagation, ground clutter — and a single frame should not close the dome. The
first, unconfirmed frame is shown on both pages as *"echo within 30 km — unconfirmed (1 of
2 frames), not triggering"* with a grey dot, and it starts no freeze. Live check: unsafe
while a **confirmed** echo is in the ring **and for 15 min after the last one** — a freeze that clear frames do not cancel, so the roof does not
reopen the moment a cell's edge leaves the ring (it also covers the feed going blind). A
fetch error or stale frame → *unavailable*, which does not veto on its own. The
slow tile/radar fetch + render runs in its own thread, so it never delays page/monitor
refresh. Needs Pillow (`sudo apt install python3-pil`); absent → radar disabled.

**Hazard overlays.** Both hazard layers (below) are drawn on the two maps whenever
something is present — display only: the radar verdict never reads them. They go after
the radar echoes and beneath the ring, crosshair and scale bars. First the information
layers: HMS smoke as a grey veil (more opaque where denser); the SPC Day-1 outlook as
dashed outlines in SPC's colours for Marginal and above (Marginal in sand, never green;
general thunder is not drawn); SPC mesoscale discussions as purple dashed outlines;
wildfire perimeters and orange-red fire triangles; earthquakes as rings sized by
magnitude; storm reports as small ink symbols (down-triangle tornado, dot hail, square
wind, diamond flood/rain, x dust, + winter, small ring anything else). Then the NWS alert
areas: filled at `TTU_SAFETY_HAZARD_FILL_ALPHA` (60/255) under a cased outline (a
casing, then the alert colour), watches and advisories first, warnings on top, a
vetoing warning last with a thicker outline. The casing is the theme's usual one (black
at night, white by day, to separate a line from echoes of its own hue) unless the colour
is itself close to the basemap — a pale Dust Storm Warning or Tornado Watch by day, a
Flash Flood Warning's dark red at night — which gets the opposite ink instead: every
outline, SPC and fire lines included, stands out from the map by at least 3:1 (a test
checks the whole colour table). An SPC outlook hole that is the next category's own
boundary is not stroked twice (SPC's non-layered file cuts each higher risk out of the
lower one); a genuine no-risk hole still is. When the overlays change — a new or
cancelled warning, a veto starting or ending — both maps are redrawn from the last frame
within one radar-loop pass (the loop wakes every 20 s while hazard layers are enabled),
with no MRMS refetch; with nothing on the map the output is pixel-identical to the plain
radar map. The daemon keeps each map's reprojected frame in RAM (~1 MB each) for this;
with `TTU_SAFETY_RADAR_THUMB_SHM=0` every overlay change costs two extra PNG writes on the
SD card.

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
cloud cover > 45%, precip probability > 20%, thunder probability > 15%. A fetch error or a
stale forecast is treated as *unavailable* (does not by itself flip unsafe — it's a
forecast, not a local sensor); a breach in a fresh forecast does. Both the daemon's
`/setup` page and the observatory status page show the inputs, the conclusion, and (on the
observatory page) a 12-hour forecast table with a source note.

### NWS alerts component (the hazard veto)

In its own thread (`safety/nws_alerts.py`) the daemon follows the **active NWS alerts**
(api.weather.gov, free, no key). **Only the configured veto events — by default Tornado
Warning, Dust Storm Warning and High Wind Warning (`TTU_SAFETY_HAZARD_VETO_EVENTS`) — can
make the monitor UNSAFE, and only while one is in effect over the observing site, for
the duration of the warning.** Every other alert — Severe Thunderstorm Warnings, watches,
advisories, statements, the civil emergency messages NWS relays — is **information
only**: listed on the status page and `/setup` and drawn on the radar map, never part of
`IsSafe` (tests enforce this). The rain, hail and lightning of a severe storm are already
caught by the WU, radar and GLM layers.

Three queries (the third brings in civil emergency messages the other two miss):

| Query | Every | Used for |
|---|---|---|
| `/alerts/active?point=<lat>,<lon>` | 60 s | NWS's own answer to "what is in effect HERE" (a polygon warning by its polygon, a zone product by the zone containing the point). A few hundred bytes: the veto's fast path |
| `/alerts/active?area=NM,OK,TX` | 120 s | every alert in the states the map touches (`auto`); the page lists, the map, and an independent local test of the site |
| `/alerts/active?event=Civil Emergency Message,Evacuation Immediate,...` | 120 s | the civil emergency messages nationwide (usually none): local authorities' messages sent straight through IPAWS carry only SAME county codes and never appear in `?area=`. Kept when their polygon touches the map or a SAME county (`048303` = TXC303) lies in the map's states |

All three ask only for `status=actual&message_type=alert,update` and request gzip:
about 120 requests an hour (60 point, 30 area, 30 civil) and, on a quiet day, ~0.3 MB an
hour — the point answer is a few hundred bytes, the TX/NM/OK area answer ~8 KB gzipped
(49 KB plain; roughly 50-150 KB gzipped on a busy severe day), the civil answer ~200
bytes. Zone outlines add a one-time fetch per zone. Every body is capped at 16 MiB, compressed and
inflated, so a runaway answer fails the query instead of the daemon's memory.

**Over the site** means either of these (fail-safe: one is enough):
- the point query lists the warning;
- the local test on the area query. A **polygon** warning covers the site when its CAP
  polygon contains it. Only the polygon counts: a storm-based warning lists every county
  it touches, and Lubbock County is much larger than a storm. A **zone-based** warning
  (High Wind Warnings usually are) covers the site when it lists one of the site's own
  zones: forecast zone TXZ035, county TXC303 or fire zone TXZ035, from `/points`
  (cached on disk, re-checked daily).

**Active alerts.** `status` Actual, `messageType` Alert or Update, and not ended (`ends`,
else `expires`, still in the future). A Cancel ends an event, and so does a segment whose
VTEC action is CAN, EXP or UPG: NWS sends EXP as an Update and UPG as an Alert. VTEC test
products never count. An event keeps its identity across updates through its VTEC key
(office.phenomenon.significance.number, e.g. `KLUB.TO.W.0012`), or the CAP id when there
is no VTEC. The newest version replaces earlier ones, so an older copy from a lagging
feed never shortens a veto. Never shown at all: Child Abduction Emergency, Blue Alert,
test and administrative messages, and marine-only products. A configured veto event is
never filtered out.

**Duration and persistence.** A veto lasts until the warning's end (`ends`, else
`expires`); an update that extends the warning extends the veto. It is persisted to
`~/safety_hazard_latch.json`, rewritten only when the set of vetoing warnings or an end
time changes (by the alerts thread: an IsSafe request never touches the SD card; a failed
write keeps the veto in memory, is retried at most once a minute and logged at most every
10 min), and restored at start-up, so a restart or reboot mid-warning keeps the dome
closed. A warning issued **ahead of its onset** (a High Wind Warning "from 10 AM Friday"
is often issued the afternoon before) vetoes from 15 min before the onset
(`TTU_SAFETY_HAZARD_VETO_ONSET_LEAD_SEC`), not from issuance: the veto covers the
warning's duration, and the dome does not stay shut all night before a daytime wind event.
Until then the page and `/setup` show it as *scheduled*. Once a veto has started it
stays in effect until it is released. Tornado and Dust Storm Warnings take effect when
issued. The onset check also uses NWS's own `sent` times, so a Pi clock running behind
cannot delay a veto.

**Release.** At the warning's end time, or **early** only when a fresh point query AND a
fresh area query, both made after the warning was last seen, no longer list it over the
site (cancelled, expired, or its updated polygon has moved off the site). The area query
counts only once the site's zones are known. If the feeds fail or go stale the veto is
held until the warning's end time, and never beyond it. A warning without an end time is
held for 60 min after it was last seen — the last sighting is saved in the latch file, so
a restart does not start a fresh hour — and a hold on stale information is capped at
72 h. Each start, update, release and restore is recorded as a `HAZARD-VETO` event (like
`RADAR-RAIN`). The IsSafe reason reads, for example, *"NWS Tornado Warning in effect for
the site until 18:45 CDT (NWS Lubbock TX)"*.

**Availability and fail-safes.** With no veto held, the component is *unavailable* when
both queries are older than 10 min (`TTU_SAFETY_HAZARD_STALE_SEC`), and that does not veto
on its own, like the forecast and radar layers; the connectivity watchdog covers a total
outage. An unreadable latch file arms a placeholder veto, which the first fresh point and
area queries release, and which ends 60 min after it was armed whatever the restarts in
between. An enabled layer that fails to start is shown as failed ("no warning veto"),
not as switched off. A broken or missing `nws_alerts.py` is reported as a `CONFIG`
event and the daemon keeps running. If the component itself raises, the monitor keeps the
vetoes it last reported until their end time: a bug in the code that reports a tornado
warning must never be what reopens the dome. A site outside NWS coverage disables the
layer loudly.

**Zone outlines.** Zone-based alerts carry no polygon. Their outline is assembled from
their `affectedZones` (`/zones/{forecast,county,fire}/<ID>`), each zone fetched **once**
and kept in `~/.cache/ttu-hazards/zones/`: simplified to about 100 m (below one map pixel)
for zones near the map, a bounding box only for far ones. A statewide event's 51 zones
take about 200 KB instead of 14 MB. At most 40 zones or 15 s are fetched per area poll,
so a first-run backlog never delays the point query for long; an alert is drawn once all
its zones are in (its site test does not need them).

**Colours.** The official NWS map colours (the weather.gov "Watch, Warning, Advisory
Display" chart), except that **hazards are never drawn green**, because green reads as
"good" and blends into the radar's light-rain greens. The flood products are reds (Flood
Watch #E53935, Flood Warning #C62828, Flash Flood Warning #8B0000, Flood Advisory
#FA8072), and the chart's other green rows get non-green colours. Shelter In Place
Warning, which the chart also paints #FA8072, gets a plum (#804870) so a hazmat order
does not look like a minor flood. Evacuation Immediate and Civil Emergency Message rank
and draw with the warnings (their EAS level); Local Area Emergency and 911 Telephone
Outage with the statements. The map also replaces
any green-dominant colour it is handed by the fallback colour for the product type. The
page's swatches are the map's legend.

### Hazard information (information only)

A second thread (`safety/hazard_feeds.py`) gathers context for the observer. **None of
it ever affects `IsSafe`.** Its component always reports `safe: true, info_only: true`,
the monitor never reads it for the decision, and tests feed it hostile values to prove
that. Each feed is independent (its own ok / error / age; one failing never blanks the
others). All are free with no key, and all state stays in RAM (no SD writes).

| Feed | Source (attribution) | Every | Shown |
|---|---|---|---|
| Earthquakes | USGS real-time GeoJSON summary feed (past day; `2.5_day` for the default magnitude) | 10 min | M >= 2.5 within 300 km: time, magnitude, place, distance and direction. Those inside the map are drawn as rings sized by magnitude |
| Smoke | NOAA/NESDIS Hazard Mapping System analyst smoke polygons (today's KML by UTC date, else yesterday's) | 30 min | whether the site is under smoke in the file's latest analysis (the latest window of the whole file, not just of the smoke near the site), the density (Light / Medium / Heavy) and time window; smoke over the site in an earlier window is reported as earlier. The latest analysis window's polygons are drawn as a grey veil |
| Wildfires | NIFC WFIGS current incident locations and interagency perimeters | 10 min | wildfires (type WF) not out or contained, updated within 72 h, and discovered within 14 days or at least 1,000 acres, within 150 km (and anywhere on the map). Points as orange-red triangles, perimeters as outlines; a perimeter is shown with its listed incident, or on its own when it passes the same test (not out or 100 % contained, updated within 72 h) |
| SPC Day 1 outlook | NOAA Storm Prediction Center categorical outlook | 10 min | the site's category and valid window; dashed outlines for Marginal and above |
| SPC mesoscale discussions | NOAA SPC via the Iowa Environmental Mesonet (IEM) | 10 min | MDs in effect now that touch the map; purple dashed outlines |
| Local storm reports | NWS Local Storm Reports via IEM | 10 min | the last 24 h on the map: type, magnitude, place, time, remark (up to 120 characters; corrections and duplicates folded in, IEM's "Corrects previous ..." note dropped); small ink symbols |
| Space weather | NOAA Space Weather Prediction Center: planetary Kp and the NOAA R/S/G scales | 10 min | one line of text: Kp now and 24 h max, the scales now, the G forecast |

Cadence: `TTU_SAFETY_HAZARD_FEEDS_POLL_SEC` (10 min), and never faster than each feed's
own minimum (5 min for USGS and IEM, 10 min for the rest, 30 min for HMS, which is
analysed a few times per day). A failed feed is retried after 5 min. Requests are small:
radius and box filters run on the server, gzip is used where offered, and conditional
GETs return a bodiless 304 when nothing changed. That is about 60 KB for the first round,
then a few hundred bytes to a few KB per 10 min, plus 50-300 KB whenever the day's HMS
file changes.

Display rules. A feed silent for more than two of its intervals (plus 2 min; at least
`TTU_SAFETY_HAZARD_STALE_SEC`) is shown as stale and its items are withheld: old
information is never presented as current, and each product's own time (file date,
valid window, report time) travels with it. A clock step discards everything fetched
under the old clock and re-polls. HMS smoke is analysed from daytime satellite imagery
only and is smoke anywhere in the column (aloft), not surface air quality. SPC paints
general thunder and Marginal green; here they are grey (text only) and sand.

### Status page: the Hazards section

`make_status_page.py` adds a **Hazards** section below the radar map, and the safety card
gains an **NWS warnings** tile: *none* / *VETO* (event, until when) / *N/A* (alerts
unavailable) / *off*. The section shows:
- a red **UNSAFE: <event> over the site until <time>** banner for each vetoing warning,
  with the issuing office, when it was first seen and which query confirmed it, plus an
  amber *Scheduled* notice for a veto warning whose onset is still ahead;
- **NWS alerts at the site** and **NWS alerts nearby (on the map)**: the colour swatch (the
  map's legend), event, threat tag (TORNADO EMERGENCY, PDS, DESTRUCTIVE, ...), a VETO chip
  on vetoing warnings, headline, issuing office, until when, and the full text and
  instructions in a fold-out; up to 25 per list, all of them drawn on the map;
- **Other hazard information** (marked *information only*): smoke, SPC outlook and
  mesoscale discussions, storm reports, wildfires, earthquakes and space weather, each
  with its feed status and an *on map* mark for what the map shows, plus a key to the
  map's hazard layers; a list the daemon caps at 20 items shows the real count and
  "+N more not listed";
- a sentence saying that only the configured veto events over the site affect the
  monitor.

The display fails safe. If the safety state is stale (the rule the radar section uses),
the section shows "hazard information stale" and no reassuring lists; an unavailable
alert feed reads "unknown", never "no alerts"; a failed information feed is listed under
"no current data", never "none". Whether the map-area query is current is the daemon's
own verdict (it knows `TTU_SAFETY_HAZARD_STALE_SEC`). All feed text is HTML-escaped. Times are shown in
`TTU_SAFETY_LOCAL_TZ`, which the page inherits from the daemon's environment. The daemon's
`/setup` page carries the same lists in compact form, with an "NWS warnings" row in its
inputs table, the same fail-safe wording ("unknown", "no current data", "NWS alert layer
off — no veto") and a Sources line.

## Test / develop off the Pi

The daemon needs no Pi hardware (it only reads JSON and polls WU over the network):

```bash
pip install flask waitress pytest flake8
python -m pytest safety/tests -q                       # unit + smoke tests (hardware and network stubbed)
# smoke test: feed inputs and hit the API
TTU_SAFETY_INPUTS_FILE=/tmp/in.json TTU_SAFETY_HTTP_PORT=11234 python safety_monitor.py &
echo '{"ts":'$(date +%s)',"sun_altitude_deg":-10,"humidity_pct":40}' > /tmp/in.json
curl -X PUT localhost:11234/api/v1/safetymonitor/0/connected -d Connected=true
curl localhost:11234/api/v1/safetymonitor/0/issafe          # -> Value: true
```
