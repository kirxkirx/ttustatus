# TTU Alpaca SafetyMonitor

An ASCOM Alpaca **SafetyMonitor** daemon for the observatory, running on the Raspberry Pi
alongside `make_status_page.py`. It exposes **two SafetyMonitor devices** on the same
address and port:

- **Device 0, `TTU Safety Monitor`: the normal safety monitor.** It aggregates several
  inputs into a single `IsSafe` boolean ("is it safe to observe?") for NINA and any other
  observatory or dome control software; any Alpaca client can read it. The status page
  shows the same information plus the recent safety-event log. Everything in this
  document is about device 0 unless it says otherwise.
- **Device 1, `TTU Dew Monitor`: a special-purpose monitor.** Its `IsSafe` is `true` only
  while dew is expected to form. It is used only by this observatory's own custom
  dome-control code, to temporarily disable the dome's rain sensor, which is susceptible
  to dew. It is not a safety verdict and must not be selected in NINA or in any other
  software: see [Dew monitor](#dew-monitor-alpaca-device-1). The daemon also keeps a
  [weather log](#weather-log) for tuning it.

## Architecture (two cooperating processes)

`make_status_page.py` is **ephemeral** (spawned every 90 s by the daemon), so the
persistent Alpaca server and the rain poller live in a **separate long-running daemon**,
`safety_monitor.py`. They share small JSON files:

```
make_status_page.py  --writes-->  /dev/shm/safety_inputs.json  (sun altitude, humidity,
                                                                enclosure temperature)
safety_monitor.py    --reads--/
safety_monitor.py    --writes-->  /dev/shm/safety_state.json   (IsSafe + inputs + events)
make_status_page.py  --reads--/   (renders the "Safety monitor" page section)
```
(RAM-backed `/dev/shm`, deliberately: these files are rewritten every minute or two and
must never wear the SD card; both are regenerated within seconds of a restart.)

Durable files (survive reboot): `~/safety_latch.json` (the rain latch), the GLM / radar /
NWS-warning latches beside it, and `~/safety_events.log` (the audit trail, also mirrored
to stdout; size-capped with its one backup `~/safety_events.log.1`, see
[SD-card wear](#sd-card-wear)).

**One daemon at a time.** Before it creates any layer, starts any thread or writes any
file, the daemon takes an exclusive, non-blocking `flock` on `~/.ttu-safety.lock`
(`TTU_SAFETY_LOCK_FILE`; the run user's home, beside the latches). A second copy — e.g.
the `run_safety_monitor.sh` loop left running beside the systemd unit — logs
`another instance is running (pid N)` and exits with status 1, having started nothing: no
WU poll, no latch or state write, no weather-log flush to the SD card. Only "the lock is
held" stops it. A lock file that cannot be used (a symbolic link, which is refused, not
followed; not a regular file; another user's; a mount without `flock`) is a warning, and
the daemon then runs **without** the guard: a safety monitor should rather run unguarded
than not at all. The file is opened with `O_NOFOLLOW` and `O_CLOEXEC` (the page
subprocess never inherits the lock), created once with mode 0600 (a wider mode is
narrowed); each start rewrites only its one line, `<pid> <UTC start time>`. The lock goes
with the process, however it ends: nothing to clean up after a crash.

**Leftovers of a killed run.** Right after taking the lock, the daemon removes the temp
files a SIGKILL, an OOM kill or a power cut left behind: regular files only (`lstat`; a
link is never followed nor removed), owned by the run user, last modified more than an
hour ago, and named like this program's own temp files and nothing else —
`/dev/shm/ttu-glm-*.nc` (the GLM granule being read: every granule is written there and
read back), `.safety_state.*.tmp` beside the state file, and the page's
`safety_inputs.json.tmp`, `status_page_cache.json.tmp` and `status.html.tmp` in
`/dev/shm`. Each removal is logged.

## Safety logic

`IsSafe = SAFE` only if **all** hold (anything else → `UNSAFE`, fail-safe):

| Input | Unsafe when | Source |
|------|-------------|--------|
| System clock | chrony is **not synchronised to the GPS or an NTP server** (`chronyc tracking`), or its error bound is over 1 s — every other row is computed from the clock (see *Clock robustness*) | this daemon |
| Sun altitude | `> 0°` (no refraction / angular-size correction) | make_status_page.py |
| Humidity | `> 95 %` (clears again below 93 %, small hysteresis) | make_status_page.py (DHT) |
| Rain | **any** WU station reports `precipRate > 0` (0 of N stations reporting counts as no rain: shown as a *rain check blind* warning, never a veto — see below) | this daemon |
| Inputs freshness | `safety_inputs.json` missing or older than 10 min | fail-safe |
| NWS warning | a **Tornado / Dust Storm / High Wind Warning** (`TTU_SAFETY_HAZARD_VETO_EVENTS`) in effect **over the site** — NWS's point query, the warning's polygon, or one of the site's zones (TXZ035 / TXC303) — for the warning's whole duration, persisted; released early when fresh queries no longer list it, and at most 24 h after NWS last listed it | this daemon |

The NWS forecast, GLM lightning, MRMS radar and connectivity layers are described in their
own sections below. **Every other NWS alert and all the non-NWS hazard information** (NOAA
HMS smoke, NIFC wildfires, SPC outlook and mesoscale discussions, storm reports) is shown
on the status page, `/setup` and the radar map but **never affects `IsSafe`**.

**Rain latch:** WU is polled only when the sun is **below 5°** (daytime = zero API
calls). The **first** station reporting any rain trips UNSAFE immediately — no
confirmation, no second reading — and it stays unsafe for **1 hour after the last** rain
seen at any station (the freeze time). The latch is persisted, so a daemon restart or
reboot keeps it.

**What a WU poll makes of each station.** One station can never stop the poll: whatever
its reply or the network does, the station gets a state and the next station is asked.
`live` (a fresh observation: it votes) and `stale` (an observation older than 30 min) as
before; `offline` (HTTP 204, 404 or 410, an empty body, no observation) and `bad-reply`
(an HTTP 200 reply that cannot be used: not JSON, over 256 kB, no rain rate or no
timestamp; a NaN rain rate is no reading) count toward the dead-station backoff
(`TTU_SAFETY_WU_BACKOFF_AFTER`) — a `bad-reply` only when another station of the same
poll got a proper answer (`live`, `stale` or `offline`): when none did, the garbage is
WU's own (an HTML error page sent as HTTP 200) and counts like an `error`. `error` — an
API or network problem: HTTP 429, any 5xx
or other HTTP error, a timeout, a refused, broken or cut-short connection — says nothing
about the station and **never** counts toward the backoff, so a WU or network outage does
not leave every station backed off (and the rain layer blind) for an hour after the
recovery. A 503 or any other 5xx is that one station's `error`: the next station is asked
as usual, so rain it reports is seen in the same poll. Only HTTP 429, WU's rate limit,
ends the poll at once (the stations after it are `not-asked`). Its `Retry-After` is logged
and kept in the state (`throttle`) but never holds the next poll back: that comes at the
normal interval, so a `Retry-After` never pauses WU rain detection for longer than one
poll interval. While WU goes on answering 429 (a spent call budget), every poll stops at
the first station that gets it, and rain at the stations after it is not seen until a poll
gets through (device 1 reads `false` until then, see *Dew monitor*). Every reply
is read whole within 30 s and 256 kB. A station-discovery reply is checked entry by entry
(a station id must be a non-empty string, a distance a finite number; a bad entry is
skipped with a log line), and a discovery that fails keeps the previous station list.

**No data is not "no rain" on the page.** While polling should be running (night, key
set) and the last completed poll is older than 3 poll intervals, the rain component
reports 0 live stations and the note "stale, last poll N min ago", so the page and the
Alpaca `/setup` text say *no station data* instead of *no rain* (no veto change). When
polling was off after that poll (daytime, or the sun altitude unknown), the 3 intervals
count from when it was last seen off instead, and the note reads "stale, no WU poll
completed for N min": the morning's poll is not "stale" at dusk, before the first poll of
the night has had its 30 s to run.

**Rain check blind (a warning, not a veto — the owner's choice).** With no WU station
reporting for 3 polls in a row (or the last poll stale), while polling is active and a WU
key is set, the state carries the non-vetoing warning *"rain check blind: 0/10 WU
stations reporting for 20 min (radar still watches for rain)"* — or *"(the radar is
unavailable too — nothing watches for rain)"* when the radar layer has no current frame.
`IsSafe` is unchanged: the radar ring, GLM and the connectivity watchdog still apply. A
gap of more than 2 h between polls (the next night) starts a new count, and a
blind run from before polling was last off (the morning's) is not shown at dusk.

**A layer that breaks.** Every gating layer (rain, NWS forecast, GLM, radar,
connectivity, and the NWS alerts as before) is called inside a guard: one that raises, or
returns something that is not a component, cannot take the evaluation down (no HTTP 500
on `IsSafe`, the state file keeps updating, device 1 keeps answering). The broken layer is
reported *unavailable* with its error (and `failed: true` in its component); a warning
names it on the page and `/setup` (not for the NWS alerts layer: its error is shown in its
own NWS-warnings tile and row while no veto is held, and in the Hazards section), and the
traceback goes to the daemon log (once per
change of error, else every 10 min). On device 0 an unavailable layer does not veto on
its own (the owner's choice) — except that **a veto it last reported is held to that
veto's own end**: the rain latch, the GLM cool-off and the radar freeze keep the dome
closed for the time they had left (marked *held*), so a bug in the code that reports rain
never reopens the dome. A layer that breaks from its first call after a restart has
reported nothing yet: the latch it restored from its own latch file is held the same way.
A broken forecast is simply unavailable. A broken connectivity watchdog keeps its last
verdict (an "offline too long" stays UNSAFE) and vetoes once it has gone
`TTU_SAFETY_OFFLINE_UNSAFE_SEC` without reporting "online": a broken watchdog proves the
internet up no better than a failed probe. **Device 1 reads `false` while any of these
layers is broken**, and while the NWS alerts layer is enabled but failed to start, with a
reason naming it ("the radar check of device 0 is broken ..."): a bug in, say, the radar
code must not be what lets a dome ignore its rain sensor. A layer switched off on purpose
(`TTU_SAFETY_RADAR=0` and the like) does not count.

More inputs (cloud sensor, ceilometer, radar, plate-solve failures) can be added later as
extra components in `safety/monitor.py`.

## Dew monitor (Alpaca device 1)

**The problem.** The dome's own rain sensor is a bare plate under the night sky. On a
clear, calm, humid night dew forms on it, it reads WET, and the dome controller closes the
roof in perfectly good weather. The observatory's own dome-control software (custom code,
called "the dome driver" below) can ignore its rain sensor, but it has to be told when
that is reasonable, and the weather knowledge is here.

**The answer** is a second, entirely standard Alpaca SafetyMonitor on the same address and
port, **device number 1**. The dome driver polls it like any other SafetyMonitor:

| `IsSafe` of device 1 | Meaning | Status page shows |
|---|---|---|
| `true` | a WET reading of the rain sensor right now is dew: **the rain sensor may be ignored** | RAIN SENSOR IGNORED |
| `false` | **trust the rain sensor** (the normal state) | RAIN SENSOR TRUSTED |

It is **not an observing-safety verdict and must not be selected in NINA**: on a clear,
windy night it reads `false` although observing is perfectly safe. Selected by mistake it
errs to the cautious side, because it can never be `true` while device 0 is not. It never
opens or closes anything; `false` only hands the decision back to the rain sensor.

`IsSafe(1) = true` only while **all** of these hold:

| Check | Limit | Data |
|---|---|---|
| The main monitor | device 0 is SAFE | this daemon (every rain, lightning, radar, forecast and daylight check above) |
| Dew-point spread (air temperature minus dew point) | **at most 5 °C** | median of the WU stations |
| Wind speed | calm: **at most 8 mph**, and it stays "calm" until the wind is **above 9 mph** | median of the WU stations |
| Forecast cloud cover | **below 30 %**, this hour and the next | NWS gridpoint forecast |
| Forecast precipitation probability | **below 5 %**, this hour and the next | NWS gridpoint forecast |
| Lightning | **no flash within 60 km** of the site in the last 30 min, with the GLM layer watching for all of that time | GOES-19 GLM, the main monitor's lightning layer |

The data behind these lines must be current and the checks of device 0 alive: a stopped
WU rain check or a broken layer of device 0 also reads `false`
(see *Missing data is `false`* below).

Deliberately simple: there is no lower limit on the spread (fog is not treated as
dangerous to the equipment), no waiting period once every line holds and no memory of
earlier dew. The only state is the 8–9 mph dead band, so wind hovering at the limit does
not flip the answer with every station update (after a restart the wind must be seen at
8 mph or below once), and the lightning record of the last 30 minutes described next.

**Lightning.** This is the main monitor's lightning check with a wider ring. Device 0
goes UNSAFE for a flash within 50 km (and device 1 is then false anyway); device 1 is
also false for a flash between 50 and 60 km, because with the rain sensor ignored there
is one protection fewer against a storm's rain. A flash counts for the same 30 minutes
as the main latch (`TTU_SAFETY_GLM_COOLOFF_HOURS`), from the moment the daemon first
sees it in a GLM poll: a storm does not flash every five minutes, and one quiet poll
must not hand the roof back to "it is only dew". Distances are in kilometres from the
site coordinates, and the limit is inclusive (exactly 60 km counts as inside; the GLM
layer reports distances to 0.1 km).

"No lightning" also has to be known, not assumed. Device 1 needs 30 minutes of good GLM
polls behind it, none further apart than 15 minutes, and no time between them that the
GLM layer had to leave unread (a poll says so whenever it passes over published granules
nobody read: the oldest granules of a catch-up its time ran out on, or the minutes
before a window that reaches back only 15 minutes). After a daemon start, a correction
of the system clock, a GLM outage or such a gap nobody knows what happened in the
minutes that were not watched, so device 1 stays `false` until 30 minutes have been
watched again. (Every evening that is a few minutes after sunset, GLM polling having
started when the sun passed 5°.) None of this is persisted: no file, no SD-card write,
which is why a restart costs those 30 minutes. All of these times are measured on the
monotonic clock: a system clock that is stepped neither shortens the 30 minutes nor makes
old data look fresh.

The check sees what the GLM polls have read and no more; see
[GLM lightning component](#glm-lightning-component) for what one poll covers.

**Why spread, and why wind.** The plate radiates to the sky and runs a few degrees colder
than the air, so it collects dew once the spread is smaller than that cooling, long
before the air is saturated (5 °C of spread is roughly 70 % relative humidity). Wind keeps
the plate at air temperature. The humidity sensor of the main monitor cannot be used for
this: it sits inside the enclosure.

**Units.** The spread limit is in degrees Celsius and the wind limits are in miles per
hour. WU reports °F and mph, NWS °C and km/h; every conversion is in `safety/dew.py`
(a temperature *difference* converts without the 32° offset), and the tests pin them.

**Missing data is `false`.** Each of these alone gives "trust the rain sensor":

- fewer than 3 stations (`TTU_SAFETY_DEW_MIN_STATIONS`) with a fresh temperature and dew
  point, or fewer than 3 with a fresh wind reading. A station counts while its latest
  observation is at most 30 min old, so one failed WU poll changes nothing; a station
  without a hygrometer or an anemometer simply does not count for that check, and
  neither does an implausible value (a failed sensor's −99.9 and the like, a dew point
  well above the temperature) or an observation time-stamped in the future;
- no forecast: the last good NWS download is older than 40 min (two poll intervals plus
  ten minutes, so one failed download is tolerated), or it lacks a value for one of the
  two hours;
- no lightning data: the last good GLM poll is older than 15 min (two poll intervals plus
  five minutes, so one failed poll is tolerated). A good poll is one that read at least
  10 of the granules of the last 5 minutes (`TTU_SAFETY_DEW_GLM_MIN_GRANULES`; there
  are up to 15, the granules a poll reads further back, where the previous poll stopped,
  not counted) and is not partial (a hole or a lag in what it read, or new granules its
  75 s ran out before): a granule the GLM
  layer cannot download is read by a later poll (or given up, and reported as time no
  poll read: next line), but "no flash" in a fraction of the data is not "no lightning".
  A flash, on the other hand, counts from whatever poll
  shows it, partial or not, and missing data never ends its 30 minutes;
- lightning not watched for 30 minutes yet (a daemon start, a clock correction, good
  polls more than 15 min apart, time the GLM layer left unread: see "Lightning" above);
- no GLM layer: `TTU_SAFETY_GLM=0`, numpy / netCDF4 not installed, or a site outside
  GOES-East coverage. **Device 1 is then always `false`**, and the daemon says so at
  startup. There is no setting that drops the lightning condition;
- daytime: WU and GLM are polled only when the sun is below 5°, and device 0 is UNSAFE
  anyway;
- the WU rain check not running. A WU poll *gets through whole* when it is not cut short
  by WU's rate limit (HTTP 429) and at least one station reports; any other poll has
  failed. Device 1 reads `false` from a poll cut short by a 429 until a poll gets through
  whole (a later poll that is not cut short but that no station answers does not end
  this); after two failed polls in a row (one failed poll that a 429 did not cut short is
  tolerated, a second is not); and when no poll has got through whole for two poll
  intervals plus 5 min (25 min with `TTU_SAFETY_POLL_INTERVAL=600`: a poll loop that has
  stopped).
  With the rain sensor ignored, the WU stations are one of the protections left, so device
  1 does not lean on a rain check that has stopped, even while the stations' last readings
  still count for the spread and wind (30 min). After a clock correction it waits for the
  WU poll the correction triggers. Device 0 is unaffected: there no station reporting
  counts as no rain, with the *rain check blind* warning;
- a gating layer of device 0 that is broken (see *A layer that breaks*): device 0 treats
  it as unavailable, device 1 reads `false` until it works again, with a reason naming it.
  A broken GLM layer is also "no lightning data" at once, not "daytime", and once it works
  again the 30 minutes are watched afresh: what happened while it was broken was not
  watched;
- the dew monitor itself failing. Whatever goes wrong in it, device 0 is unaffected.

**Where to see it.** The status page has a "Dew monitor" card with each check's number
and limit and, while the answer is `false`, the reasons (the two conditions above that are
not tiles, a stopped WU rain check and a broken layer, come first: every tile can be green
while they hold the answer `false`). It also shows whether the dome driver is connected
and when it last asked. `/setup` has the same as a table, and `/status` (JSON) carries it
under `dew` (those two conditions also under `dew.blocked`). Changes of the verdict go to
the daemon log (`journalctl -u ttu-safety | grep 'dew monitor'`) and, with all the numbers
behind them, to the weather log; they are not written to `~/safety_events.log`, which is
on the SD card.

- IsSafe: `http://<pi>:11111/api/v1/safetymonitor/1/issafe` (after `PUT .../1/connected`
  with `Connected=true`; device 1 has its own Connected flag)
- Switch it off with `TTU_SAFETY_DEW=0`: device 1 then does not exist, and a driver
  polling it gets an error and keeps trusting its rain sensor.

The thresholds are a first guess to be checked against the weather log. On 8 November
2026 the roof was closed by dew at 22:07 local; Lubbock airport reported 4.4 °C of spread
and 4.6 mph of wind at 21:53, but also broken cirrus at 25,000 ft. So on nights like that
one, compare the logged `fc_cloud_pct` with the 30 % limit.

## Weather log

For every moment of a night, the log records the numbers the two verdicts were made
from. Nothing is fetched for it: these are data the daemon already has.

Two CSV tables, each file starting with a header line:

- **weather**: one row every 5 min, plus one at once whenever the verdict of device 0 or
  device 1 changes (column `event`: `interval`, `device0`, `device1`, `start`). Columns:
  UTC and local time, sun altitude, both verdicts (`safe0`, `safe1`), the station medians
  device 1 used (`spread_c`, `wind_mph`, `gust_mph`, `temp_c`, `dewpoint_c`, `rh_pct`) and
  how many stations they come from, each device 1 check (`check_spread`, `check_wind`,
  `check_cloud`, `check_precip`, `check_lightning`: 1 passes, 0 fails, blank = no data),
  the forecast for this hour and the next (`fc_cloud_pct`, `fc_precip_pct`, `fc_temp_c`,
  `fc_dewpoint_c`, `fc_wind_mph`, ...), rain latch, radar and lightning state, the
  enclosure sensor (`encl_temp_c`, `encl_rh_pct`) and both lists of reasons.
- **stations**: one row per WU station per WU poll (temperature, dew point, spread,
  humidity, wind, gust, rain rate, distance, age of the observation), to see which
  stations are representative of the site. WU is polled at night only, so there are no
  daytime rows.

Units are in the column names: temperatures in °C, wind in mph, the rain rate in inches
per hour as WU reports it. The forecast wind arrives in km/h and is logged in mph.

**Download.** The status page has a "Weather log" block with links to every file; the
directory is inside the web root:

```
/var/www/html/weather_log/
  weather_2026-11.csv   stations_2026-11.csv     one pair per month (UTC), on the SD card
  weather_current.csv   stations_current.csv     the rows not archived yet (symlinks into /dev/shm)
```

**SD-card wear.** Rows are appended to files in RAM (`/dev/shm/ttu_weather_log/`). A
separate thread copies them to the SD card in one append per monthly file:

- once every 24 h (`TTU_SAFETY_WEATHER_LOG_FLUSH_HOURS`), and only while the sun is above
  the horizon. If the sun's altitude is not known (the page script is down, or the first
  minute after a reboot) it waits, for at most a further 24 h;
- when the daemon is stopped in an orderly way: `systemctl stop` / `restart` and a reboot
  send SIGTERM, which the daemon now handles;
- at once, day or night, if the RAM files ever exceed 8 MB (a normal day is about
  0.2 MB).

A crash or restart of the daemon loses nothing, because `/dev/shm` survives it. A power
cut loses the rows not copied yet, at most a day. The copy is all-or-nothing and never
duplicates rows, even if the daemon is killed in the middle: a run that did not finish
is rolled back and repeated, and a row torn by a power cut at the end of a file is cut
off before the next append.

If the archive cannot be written (the daemon's user must be able to create
`/var/www/html/weather_log`), the page and `/setup` say so, the rows stay in RAM and the
copy is retried every 10 min in daytime; beyond 32 MB of RAM the oldest unarchived rows
are dropped.

**Growth limit.** When the archive exceeds 6 GB (`TTU_SAFETY_WEATHER_LOG_MAX_MB`) the
oldest months are deleted, never the current one, with a `WEATHER-LOG` line in the event
log. At about 0.2 MB a day that is decades away; the limit is a guarantee, not a routine.

If a later version changes the columns in the middle of a month, that month continues in
`weather_2026-11_b.csv`, so every file stays one consistent table.

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
   sudo apt update && sudo apt install python3-flask python3-waitress chrony
   python3 -c "import flask, waitress; print('ok')"      # verify
   chronyc tracking                                      # "Leap status : Normal"
   ```
   **chrony is required:** device 0 reads UNSAFE until `chronyc tracking` shows the clock
   synchronised to the GPS or an NTP server (*Clock robustness* below). Installing chrony
   replaces Raspberry Pi OS's default systemd-timesyncd.
   Alternative — a dedicated venv (no system packages, newer Flask):
   ```bash
   python3 -m venv ~/safety-venv
   ~/safety-venv/bin/pip install flask waitress
   ```
   Then run the daemon with `~/safety-venv/bin/python` instead of `/usr/bin/python3`
   (edit that path in `run_safety_monitor.sh` and the systemd unit's `ExecStart`). A plain
   venv suffices — the daemon needs only flask + waitress + stdlib, not the Pi hardware libs.
3. Fill in `~/ttustatus.env` — the WU API key and a User-Agent with your real e-mail
   address, optionally a CARTO key (see the next section) — then install the single service:
   ```bash
   sudo ~/ttustatus/deploy/install.sh
   ```
   One systemd unit runs everything: the daemon spawns `make_status_page.py` every
   ~90 s as an isolated subprocess (killed after 30 min if it ever hangs; a page
   failure is logged and never affects the safety logic). Logs (rotating, via
   journald): `journalctl -u ttu-safety -f`. Disable the built-in page runner with
   `TTU_SAFETY_PAGE=0` if you prefer to schedule the page yourself.
   Run the daemon from this unit **or** from `run_safety_monitor.sh`, not both: the
   single-instance lock (see Architecture) makes the second one exit at once with
   `another instance is running` in its log — under `run_safety_monitor.sh` every 5 s,
   under the unit every 15 s — until the other is stopped.

### `ttustatus.env`: the WU API key, the User-Agent contact, the CARTO key (kept out of git)

The keys are **not** in the code — set them via the environment. Create a secrets file
**outside the repo** and point the unit at it:

```bash
cp ttustatus.env.example /home/kirx/ttustatus.env   # then edit it (see below)
chmod 600 /home/kirx/ttustatus.env
sudo systemctl daemon-reload && sudo systemctl restart ttu-safety
journalctl -u ttu-safety -n 20     # should NOT warn "TTU_SAFETY_WU_KEY is not set"
```

`ttustatus.env` holds (systemd `EnvironmentFile` format: `KEY=value`, no `export`; a
value with spaces, `( )` or `;` goes in double quotes, which systemd and bash both strip):

- `TTU_SAFETY_WU_KEY=<your key>` — required for rain polling (the example file's
  placeholder value is flagged at startup as a `CONFIG` warning);
- `TTU_SAFETY_NWS_UA="ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; you@example.org)"`
  with `you@example.org` replaced by your own address — **absolutely crucial: a REAL
  e-mail address.** This User-Agent goes with the daemon's NWS, IEM radar, hazard-feed
  and map-tile requests. OpenStreetMap blocks a placeholder contact such as
  `you@example.org` (the example file's value, there to be replaced), so with one the
  daemon requests no OpenStreetMap tiles at all: a radar map with no CARTO basemap
  (cached on the Pi or fetched with a key) then has **no basemap**. NWS also gets the
  placeholder as its contact, and it asks for a real one. At startup the log warns about
  a placeholder (also a `CONFIG` event) or a missing address; the page and `/setup` warn
  whenever OpenStreetMap is, or would be, used without a real address;
- `TTU_SAFETY_CARTO_KEY=<key>` — optional, for CARTO radar basemaps (see the radar
  section).

Only the daemon needs them — `make_status_page.py` uses none of these. If the WU key is
unset the daemon still runs (sun/humidity protection) but rain polling is disabled and
the page shows rain "off".

After editing `ttustatus.env`, apply it with `sudo systemctl restart ttu-safety`.
Quick manual test without systemd: `TTU_SAFETY_WU_KEY=<key> python3 safety_monitor.py`
(the `run_*.sh` scripts remain as manual fallbacks; `run_safety_monitor.sh` sources
`~/ttustatus.env` itself).

## Clock robustness

The Pi has no battery-backed RTC: after a power cut it boots with whatever time
`fake-hwclock` last saved, which can be **months wrong** until NTP syncs (the 2026-08-25
incident: a boot under a March clock read the persisted August latches as *"rain latch
active (227329 min left)"*). The daemon defends itself:

- **UNSAFE until chrony confirms the clock** (`safety/timesync.py`). Every check is
  computed from the clock — the sun's altitude above all: at true noon on 13 Nov the sun
  stands at +37.6° over the site, but a clock 6 h slow computes −16.4°, "night" — so
  device 0 reads **UNSAFE** until
  `chronyc -n -c tracking` (asked every 30 s, in a thread of its own) shows all of:
  - a real reference: the GPS's reference clock (e.g. `PPS`) or an NTP server — not
    `00000000` (none) and not chrony's `local` self-reference (`7F7F0101`, the Pi
    trusting its own clock);
  - stratum 1–15 and a leap status other than *Not synchronised*;
  - chrony's own bound on the clock's error (|offset| + root delay/2 + root dispersion)
    within `TTU_SAFETY_CLOCK_MAX_ERROR_SEC` (1 s; a synchronised Pi shows microseconds with
    the GPS's PPS, tens to a few hundred milliseconds with GPS NMEA alone or internet NTP).
    The bound also grows while chrony has no source at all, so a Pi that has lost the GPS
    *and* every NTP server reads UNSAFE after roughly half a day to a few days.

  chronyc missing, chronyd not running, a timeout or unreadable output all count as *not
  confirmed*. Once the clock has been confirmed, a failed check is tolerated for
  `TTU_SAFETY_CLOCK_GRACE_SEC` (10 min: a chronyd restart during a package upgrade must
  not close the dome; a clock that was right cannot drift meaningfully in that time). A
  **clock step ends that grace at once** — the clock just changed, so it is confirmed
  anew (within a second when chrony made the step). The reason is shown on both pages
  (*"system clock not confirmed by chrony — chrony is not synchronised to any time source
  yet"*), the `/setup` page has a *System clock (chrony)* row (*"synchronised to PPS
  (stratum 1, error ≤ 11 µs)"*), and a `CLOCK-SYNC` event marks the start and the end of
  an unconfirmed spell. All timing is on the monotonic clock; nothing is persisted (a
  restarted daemon asks chrony before its first verdict). `TTU_SAFETY_CLOCK_SYNC=0`
  switches the check off (warned at startup).
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
- **The radar and GLM check the servers' clocks** (`TTU_SAFETY_SERVER_CLOCK_TOL_SEC`,
  60 s). Both radar archives and the GOES bucket keep years of data and are asked for
  "now" by *this* clock, so a boot under a March clock would read a March date's radar
  frames and lightning as current. Every reply carries the server's own time (its `Date`
  header): while it disagrees with the Pi's clock by more than the tolerance, the data
  is refused — the layer reads *unavailable*, never "clear" — and a warning on both
  pages names the offset (*"this host's clock is 158 d behind the radar servers' —
  radar frames refused until the clock is corrected"*), with a `CLOCK` event (and an
  error in the log) from the radar and from GLM when it starts and ends. Once refused,
  the clock must agree to within the tolerance **less 10 s** before data is accepted
  again: a `Date` has whole seconds, and a clock off by almost exactly the tolerance
  would otherwise be refused and accepted by turns, each flip an event on the SD card.
  (While chrony is synchronised the Pi's clock is microseconds off, so this guard is
  the second line behind the chrony check above: it catches a wrong time source.)
- Pollers treat a **backward** step as "poll now" instead of stalling until the wall
  clock catches up; the connectivity watchdog runs entirely on the monotonic clock, so
  steps can neither fake a huge offline window nor neutralize it.
- **NWS warning vetoes** hold the warning's *absolute* NWS end time: a backward step keeps
  each veto for exactly the time it had left, a forward step never holds one past the NWS
  end, a veto that has started stays in effect across a step, the 24 h release bound (a
  veto not listed for 24 h is released) keeps the time it had left, and an early release
  needs point and area queries made *after* the step. The alerts layer moves its vetoes
  the moment it sees a step itself (the wall clock against the monotonic one) — at the
  next IsSafe call or poll, before the step detector's report — each by the change of its
  own clock frame, so each is moved once whoever sees the step first, a veto NWS first
  listed after the step not at all, and a step the detector never reports (one during
  start-up) like any other; an answer that arrives after a step is read in the new clock.
  A persisted veto first seen "in the future" is clamped at load like the other latches.
  The hazard information feeds drop everything fetched under the old clock and re-poll.
- The NWS forecast **re-ages**: if the poller goes silent the forecast flips to
  *unavailable* within two poll intervals instead of presenting frozen data forever, a
  forecast dated in the future (wrong clock) is never presented as current, and the
  page shows *why* it is unavailable (e.g. the TLS "certificate is not yet valid"
  failures a wrong clock causes).

None of this replaces NTP — until the clock syncs, the radar and GLM have no data and
everything computed from the clock (the sun's altitude above all) is as wrong as the
clock; that is why device 0 stays UNSAFE until chrony confirms it. The rest bounds the
damage and makes the condition loud instead of silent.

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
| `~/safety_hazard_latch.json` | only when the set of vetoing NWS warnings (or one's end time) changes — a few writes per warning, and at most hourly while a long warning keeps being listed (its 24 h release bound is renewed), every 10 min while a warning without an end time is (its last sighting); written by the alerts thread only, never on an IsSafe request; a failed write is retried at most once a minute | SD (must survive reboot) |
| `~/safety_events.log` (audit trail) | only on events | SD (its purpose). Capped at 1 GB together with its one backup (`TTU_SAFETY_EVENT_LOG_MAX_MB`): at half the cap it is renamed `safety_events.log.1` (replacing the older backup) and a new file starts. At a few kB a day that is decades, so the cap only guards against an event storm filling the card |
| `~/.ttu-safety.lock` (single-instance lock) | created once; one short line at each daemon start | SD (a few bytes per start) |
| `/dev/shm/ttu_weather_log/` (weather-log rows not archived yet) | every 5 min, plus every WU poll | RAM |
| `/dev/shm/ttu-glm-*.nc` (each GLM granule while it is read, then deleted; read from memory only where there is no `/dev/shm`) | about 20 per GLM poll at night (0.2–0.6 MB each), up to about 45 on the first poll after a pause | RAM |
| `/var/www/html/weather_log/*.csv` (weather-log archive) | one append per monthly file every 24 h, in daytime, and when the daemon is stopped | SD (its purpose; about 0.2 MB a day) |
| `~/.cache/ttu-radar/` basemaps | once ever (per site, theme and source — CARTO or OpenStreetMap; never a partial one, never one with a refused tile) | SD (avoids re-fetching map tiles per boot) |
| `~/.cache/ttu-hazards/zones/<type>_<ID>.json` (NWS zone outlines), `points_<lat>_<lon>.json` (the site's zones) | once per zone (never re-fetched while the site stays put; a zone without a usable outline once a day); the site's zones only if NWS changes them | SD (~200 KB for a three-state event: simplified outlines near the map, bounding boxes far away) |

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
Also the journal, where the daemon's own log lines go. Raspberry Pi OS keeps it **in
RAM** (`Storage=volatile`, set by `/usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf`).
That means no SD writes and a cap of its own (10 % of the size of `/run`: tens of MB),
but it is **lost at every reboot**. `deploy/install.sh` adds
`/etc/systemd/journald.conf.d/60-ttu-safety.conf` with `SystemMaxUse=1G`. It does nothing
while the journal is in RAM, and caps it at 1 GB if you keep it on the card for reboot
forensics. To keep it on the card, add `Storage=persistent` (in a drop-in of your own,
named to sort after `40-`), then `sudo systemctl restart systemd-journald`. Expect a
steady trickle of small SD writes from then on. Check what is in force with
`systemd-analyze cat-config systemd/journald.conf | grep -E '^(Storage|SystemMaxUse)'` and
`journalctl --disk-usage`. Check swap (`swapon --show` — zram = RAM =
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

Discovery also lists `TTU Dew Monitor` (device **1**). **Do not select it in NINA**: it is
the rain-sensor override for the dome driver, not a weather verdict (see
[Dew monitor](#dew-monitor-alpaca-device-1)).

- IsSafe:   `http://<pi>:11111/api/v1/safetymonitor/0/issafe`
- Setup/status page: `http://<pi>:11111/setup`   ·   debug JSON: `/status`

## Configuration (environment variables)

All optional; defaults suit the Pi. Set them in the systemd unit or before launch.

| Var | Default | Meaning |
|-----|---------|---------|
| `TTU_SAFETY_HTTP_PORT` | `11111` | Alpaca port |
| `TTU_SAFETY_HTTP_HOST` | `0.0.0.0` | bind address (LAN) |
| `TTU_SAFETY_POLL_INTERVAL` | `600` | seconds between WU polls (night only; 60-3600). A 429's `Retry-After` never delays a poll beyond it. Device 1 reads `false` after a poll cut short by a 429 until a poll gets through whole, after two failed polls in a row, and when no poll has got through whole for two intervals plus 5 min (see *Dew monitor*) |
| `TTU_SAFETY_RAIN_LATCH_HOURS` | `1.0` | freeze time: how long UNSAFE persists after the last rain (0.1-48) |
| `TTU_SAFETY_INPUTS_STALE_SEC` | `600` | older inputs → fail-safe UNSAFE (60-3600) |
| `TTU_SAFETY_EVAL_INTERVAL` | `30` | seconds between background evaluations (1-300; every Alpaca request evaluates too) |
| `TTU_SAFETY_WU_KEY` | **(required)** | Weather Underground PWS API key — set via env, never commit (see above) |
| `TTU_SAFETY_LAT` / `_LON` | (adopted from GPS) | site coordinates; unset ⇒ the daemon adopts the status page's GPS fix once at startup (rounded to ~100 m). All derived values (NWS grid, WU stations, radar/GLM rings, cached tiles) follow them |
| `TTU_SAFETY_GEO_MISMATCH_KM` | `0.1` | warn (page + /setup) when GPS and configured coords differ by more |
| `TTU_SAFETY_WU_MAX_KM` | `60` | drop "nearest" WU stations farther than this (sparse regions; 1-500) |
| `TTU_SAFETY_WU_BACKOFF_AFTER` / `_BACKOFF_RETRY` | `6` / `3600` | after N consecutive silent polls (`offline` or `bad-reply`; an API or network `error` never counts) a station is probed only every RETRY seconds (saves API calls on dead hardware); it rejoins full cadence automatically on the first answer (1-1000 / 60-86400) |
| `TTU_SAFETY_WU_EXCLUDE` | `KTXSHALL25` | comma-separated station IDs excluded from rain detection — reserve for stations reporting *bogus* data; merely dead ones are handled by the automatic backoff. Does not self-heal; set to empty to clear |
| `TTU_SAFETY_LOCAL_TZ` | `America/Chicago` | timezone of the forecast table (invalid ⇒ loud UTC fallback) |
| `TTU_SAFETY_STATE_HEARTBEAT_SEC` | `60` | max interval between unchanged state-file writes (SD-wear throttle; 10-120) |
| `TTU_SAFETY_INPUTS_FILE` / `_STATE_FILE` / `_LATCH_FILE` / `_EVENT_LOG` | see `config.py` | file paths |
| `TTU_SAFETY_EVENT_LOG_MAX_MB` | `1024` | size cap of the event log **together with its one backup** (`.1`): at half of it the file becomes `.1` and a new one starts |
| `TTU_SAFETY_LOCK_FILE` | `~/.ttu-safety.lock` | the single-instance lock: a second daemon exits with status 1 (see Architecture); empty = the default |
| `TTU_SAFETY_NWS` | `1` | enable the NWS forecast component (`0` disables) |
| `TTU_SAFETY_NWS_UA` | `ttu-safety-monitor` | User-Agent of the NWS, IEM radar, hazard-feed and map-tile requests (WU, GLM and the connectivity probe send their own). **Absolutely crucial: put a REAL e-mail address in it**: `ttu-safety-monitor (+https://github.com/kirxkirx/ttustatus; you@example.org)` with `you@example.org` replaced by your own address (double-quoted in `ttustatus.env`). OpenStreetMap blocks a placeholder like `you@example.org` — the daemon then requests no OSM tiles, and a map with no CARTO basemap (cached or keyed) has no basemap — and NWS asks for a contact. The default names the app but has no contact (warned at startup, and on the page when OSM is used) |
| `TTU_SAFETY_NWS_GRID` | (auto) | e.g. `LUB/46,41`; skips the `/points` lookup |
| `TTU_SAFETY_NWS_POLL_INTERVAL` | `900` | seconds between NWS forecast pulls (15 min; 300-3600) |
| `TTU_SAFETY_NWS_STALE_MIN` | `150` | a forecast NWS has not updated for this many minutes is unavailable (30-1440) |
| `TTU_SAFETY_NWS_CLOUD_MAX` / `_PRECIP_MAX` / `_THUNDER_MAX` | `60` / `20` / `15` | % thresholds (unsafe when exceeded, this or next hour; 0-100, and 100 never trips) |
| `TTU_SAFETY_GLM` | `1` | enable the GLM lightning component (`0`/`off`/`false`/`no`/`disabled` disables; another word is reported at startup and leaves it on) |
| `TTU_SAFETY_GLM_TRIGGER_KM` | `50` | flash within this radius → UNSAFE (1-20000) |
| `TTU_SAFETY_GLM_COOLOFF_HOURS` | `0.5` | freeze time (h) after the last nearby flash (0.1-24) |
| `TTU_SAFETY_GLM_POLL_INTERVAL` | `300` | seconds between GLM polls (night only) |
| `TTU_SAFETY_GLM_WINDOW_MIN` | `5` | look-back minutes of the first poll after a start (1–60: only the current and the previous hour are listed); later polls read from a minute before where the previous one stopped, but never less than this and at most 15 min back, or this if longer (see "GLM lightning component") |
| `TTU_SAFETY_GLM_STALE_SEC` | `900` | a GLM poll older than this is no data (at least the poll interval plus 120 s; raised with a startup warning) |
| `TTU_SAFETY_RADAR` | `1` | enable the MRMS radar component (`0` disables) |
| `TTU_SAFETY_RADAR_KM` | `30` | any echo within this radius → UNSAFE (1-300) |
| `TTU_SAFETY_RADAR_DBZ` | `20` | reflectivity ≥ this counts as rain (5-60) |
| `TTU_SAFETY_RADAR_TRIGGER_AFTER` | `2` | consecutive frames (each counted once) that must show an in-ring echo before it triggers (`1` = trigger on a single frame; 1-10) |
| `TTU_SAFETY_RADAR_LATCH_SEC` | `900` | freeze time (s) after the last in-ring echo; clear frames do not cancel it (60-21600) |
| `TTU_SAFETY_RADAR_POLL_INTERVAL` | `300` | seconds between radar polls (day and night; 60-1800) |
| `TTU_SAFETY_RADAR_STALE_SEC` | `1200` | a frame older than this **by its own time** is "no data" (unavailable; 600-3600) |
| `TTU_SAFETY_RADAR_SOURCES` | `aws,iem` | the frame sources asked every poll, the newest frame read: `aws` = NOAA's MRMS bucket on AWS (~1 min behind), `iem` = IEM's lcref archive (~5.5 min behind). One name alone leaves no fallback; unknown names are ignored (warned) |
| `TTU_SAFETY_RADAR_AWS_URL` | `https://noaa-mrms-pds.s3.amazonaws.com` | base URL of the MRMS bucket |
| `TTU_SAFETY_RADAR_RETRY_SEC` | `60` | a poll that got no new frame is retried after this many seconds (30 … the poll interval) — the first three misses in a row; then every `TTU_SAFETY_RADAR_POLL_INTERVAL` until a frame arrives |
| `TTU_SAFETY_SERVER_CLOCK_TOL_SEC` | `60` | wrong-clock guard: radar frames and GLM granules are refused while the server's `Date` disagrees with the Pi's clock by more than this (`0` = off, warned; otherwise at least 60); once refused, accepted again only within this less 10 s |
| `TTU_SAFETY_RADAR_THUMB` | `/var/www/html/ttu_radar.png` | thumbnail path (beside status.html) |
| `TTU_SAFETY_RADAR_BASEMAP` | `auto` | basemap chain: `auto` = cached CARTO → CARTO with the key → OpenStreetMap → plain background; `carto` = never OpenStreetMap; `osm` = never CARTO, not even the cached maps (anything else ⇒ `auto`, warned) |
| `TTU_SAFETY_CARTO_KEY` | (unset) | CARTO Basemaps API key, sent as `?key=` on CARTO tile URLs — a secret: never logged, never on the page, `/setup` or in a file name. Needed only to fetch new CARTO tiles; cached CARTO maps are used without it. Free, no account: request it by e-mail at [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey) (free up to 5M requests/month, non-commercial) |
| `TTU_SAFETY_RADAR_TILE_URL` | (unset: CARTO Dark Matter) | an explicit tile template (`{z}/{x}/{y}`) for the night map, handled by its host. **A CARTO URL** (any `basemaps.cartocdn.com` subdomain or style) keeps the CARTO rules: its own cached composite, then its tiles only with `TTU_SAFETY_CARTO_KEY` and through the key check — do not put a CARTO key into the URL (one found there is moved out and used as `TTU_SAFETY_CARTO_KEY`, with a `CONFIG` warning). **An OpenStreetMap URL** means OpenStreetMap only for that map, the cached CARTO map not used (with `TTU_SAFETY_RADAR_BASEMAP=carto` it is ignored). **Any other URL** takes CARTO's place in the chain: its own cached composite, then its tiles fetched exactly as given — no key is added, so a keyed non-CARTO service needs its key in the URL (a `key=`, `apikey=` or `token=` value is redacted in logs) — with OpenStreetMap still the fallback in `auto` |
| `TTU_SAFETY_RADAR_TILE_URL_DAY` | (unset: CARTO Positron) | the same for the day map (`TTU_SAFETY_RADAR_DAY=0` to skip the day map) |
| `TTU_SAFETY_RADAR_TILE_DARK_INVERT` | `1` | night map from light tiles (OpenStreetMap, a light non-CARTO custom set): invert the lightness of each light tile (hue kept); dark tiles are left alone, and CARTO tiles are never inverted |
| `TTU_SAFETY_RADAR_CACHE` | `~/.cache/ttu-radar` | cached basemap composites (`basemap_<hash>.png`, one per site, theme and source; tiles fetched once each). The hash also covers `TTU_SAFETY_RADAR_THUMB_PX`, `TTU_SAFETY_RADAR_THUMB_HALF` and `TTU_SAFETY_RADAR_TILE_ZOOM`: changing one of them (or the site) leaves the cached CARTO maps unused. A watermarked CARTO map: remove just that file (README.md shows how to find it in the log) |
| `TTU_SAFETY_CONN` | `1` | enable the connectivity watchdog (`0` disables) |
| `TTU_SAFETY_OFFLINE_UNSAFE_SEC` | `3600` | UNSAFE after this long with no internet (600-86400, and at least two probe intervals) |
| `TTU_SAFETY_CONN_PROBE_INTERVAL` | `300` | seconds between reachability probes (30-3600) |
| `TTU_SAFETY_CLOCK_SYNC` | `1` | the clock check: device 0 UNSAFE until chrony is synchronised to the GPS or an NTP server (`0` switches it off, warned at startup) |
| `TTU_SAFETY_CLOCK_CHECK_SEC` | `30` | seconds between `chronyc tracking` checks (5 … 600) |
| `TTU_SAFETY_CLOCK_MAX_ERROR_SEC` | `1` | largest error bound chrony may report (\|offset\| + root delay/2 + root dispersion) and still count as synchronised (0.01 … 60) |
| `TTU_SAFETY_CLOCK_GRACE_SEC` | `600` | once the clock was confirmed, a failed check is tolerated this long (a chronyd restart); a clock step ends it (0 … 3600) |
| `TTU_SAFETY_CHRONYC` | `chronyc` | the chronyc program to ask |
| `TTU_SAFETY_HAZARDS` | `1` | enable the NWS active-alerts layer (`0` disables it: no warning veto, no alert lists or map areas) |
| `TTU_SAFETY_HAZARD_VETO_EVENTS` | `Tornado Warning,Dust Storm Warning,High Wind Warning` | NWS events that make the monitor UNSAFE while in effect over the site: until the warning's end, or until 24 h after NWS last listed it, whichever comes first (comma list, case-insensitive; empty = every alert display-only, announced at startup; a name NWS does not know is kept but warned about) |
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
| `TTU_SAFETY_HAZARD_LSR_HOURS` | `24` | storm-report look-back, hours (1-168) |
| `TTU_SAFETY_DEW` | `1` | enable the dew monitor, Alpaca device 1 (`0` or `off`: no such device; a value that is neither on nor off is reported at startup and the default is used) |
| `TTU_SAFETY_DEW_SPREAD_MAX_C` | `5` | dew-point spread limit, **degrees Celsius**: true only at or below |
| `TTU_SAFETY_DEW_WIND_MAX_MPH` / `_WIND_UNSAFE_ABOVE_MPH` | `8` / `9` | wind limits, **miles per hour**: calm at or below the first, no longer calm above the second (set them equal for no dead band) |
| `TTU_SAFETY_DEW_CLOUD_BELOW_PCT` / `_PRECIP_BELOW_PCT` | `30` / `5` | forecast cloud cover and precipitation probability must be **below** these, this hour and the next |
| `TTU_SAFETY_DEW_MIN_STATIONS` | `3` | WU stations needed for the spread median, and for the wind median |
| `TTU_SAFETY_DEW_OBS_MAX_AGE_MIN` | `30` | a station's observation counts this long |
| `TTU_SAFETY_DEW_FORECAST_MAX_AGE_MIN` | `40` | the last good NWS download counts this long (two poll intervals + 10 min; at most 240) |
| `TTU_SAFETY_DEW_GLM_KM` | `60` | lightning ring of device 1, **kilometres**: a GLM flash at or within this distance makes it false for `TTU_SAFETY_GLM_COOLOFF_HOURS` (30 min). A ring no wider than `TTU_SAFETY_GLM_TRIGGER_KM` adds no distance (device 0 is UNSAFE there anyway; warned at startup), but device 1 still needs current GLM data: the lightning condition cannot be switched off |
| `TTU_SAFETY_DEW_GLM_MAX_AGE_MIN` | `15` | the last good GLM poll counts this long, and good polls further apart than this are a gap in the 30-minute watch (default: two poll intervals + 5 min; at least one poll interval + 2 min, at most 120). Polls closer together are a gap too when the GLM layer gave up any published granule between them (a poll reaches back at most 15 min; see "Time no poll read") |
| `TTU_SAFETY_DEW_GLM_MIN_GRANULES` | `10` | granules of the last `TTU_SAFETY_GLM_WINDOW_MIN` minutes a GLM poll must have read to count as lightning data for device 1 (default: two per minute of `TTU_SAFETY_GLM_WINDOW_MIN`, two thirds of the look-back); a partial poll never counts |
| `TTU_SAFETY_DEW_NAME` | `TTU Dew Monitor` | Alpaca name of device 1 |
| `TTU_SAFETY_WEATHER_LOG` | `1` | enable the weather log (`0` disables it) |
| `TTU_SAFETY_WEATHER_LOG_DIR` | `/var/www/html/weather_log` | the archive, on the SD card; inside the web root so the page can link to it |
| `TTU_SAFETY_WEATHER_LOG_RAM_DIR` | `/dev/shm/ttu_weather_log` | where rows wait in RAM |
| `TTU_SAFETY_WEATHER_LOG_INTERVAL` | `300` | seconds between summary rows (min 30) |
| `TTU_SAFETY_WEATHER_LOG_STATIONS` | `1` | also log one row per WU station per poll |
| `TTU_SAFETY_WEATHER_LOG_FLUSH_HOURS` | `24` | hours between copies to the SD card |
| `TTU_SAFETY_WEATHER_LOG_FLUSH_SUN_ABOVE` | `0` | copy only while the sun is above this altitude (degrees) |
| `TTU_SAFETY_WEATHER_LOG_MAX_MB` | `6144` | archive size limit; above it the oldest months are deleted |
| `TTU_SAFETY_WEATHER_LOG_RAM_FLUSH_MB` / `_RAM_MAX_MB` | `8` / `32` | RAM size that forces a copy at once / beyond which the oldest unarchived rows are dropped |

Out-of-range settings (the ranges are in the table and in `safety/config.py`) are
corrected to the nearest allowed value, and an on/off switch reads `1`/`true`/`yes`/`on`
and `0`/`false`/`no`/`off`/`disabled` (any case; anything else keeps the default) — each
with a warning at startup (log + `CONFIG` event). The same warnings stay on the status
page and `/setup` (prefixed *configuration:*) for as long as the daemon runs with them.

Thresholds (sun `>0°`, humidity `>95%`, WU sun-gate `<5°`) are in `safety/config.py`.

**Coverage scope:** NWS (forecast and alerts) and MRMS are US products and GLM is
GOES-East; at a site outside their coverage each component **disables itself with a loud
log/event** rather than reporting a false "clear". The US-only information feeds (SPC,
storm reports, wildfires; HMS smoke: North America) say "outside coverage" instead of an
empty list. The WU rain layer works globally, and so does the system-clock check.

**Shared files:** the page↔daemon exchange (`safety_inputs.json`, `safety_state.json`)
lives in `/dev/shm` (RAM — no SD wear, cleared on reboot). Both sides hardcode matching
defaults; when deploying this change, update the page and the daemon together (a mixed
pair fails safe: the daemon just sees missing inputs and reports UNSAFE until both match).
Make sure the user running the page loop can write `/var/www/html` (e.g.
`sudo chown kirx /var/www/html`), and consider logrotate for the shell-launcher logs.
The daemon needs the same right to create `/var/www/html/weather_log` for the weather
log.

### Connectivity watchdog

A lightweight probe (day and night, every 5 min) checks whether **any** online service host
is reachable. If **none** are reachable for **1 h**, the monitor declares **UNSAFE** — we've
been blind to rain/lightning/forecast that long and can't trust "safe". It **auto-resolves**
the instant a probe succeeds. Unlike the per-service components (which fail to *unknown*, no
veto), this is a **hard veto** — it exists to catch the case where every online layer is
silently unavailable (no internet). A response of any kind (even an HTTP error) counts as
"reachable"; only a connection/DNS/timeout failure is "offline". A fresh daemon gets a full
1 h grace from startup. A watchdog that itself fails (its component raises) keeps its last
verdict and vetoes once it has gone the offline threshold without reporting "online" (see
"A layer that breaks" under Safety logic).

### MRMS radar component

Every 5 min — **day and night** (the data is free) — the daemon reads the newest **MRMS
reflectivity frame** (NOAA/NSSL's SeamlessHSR: every US weather radar's lowest unblocked
beam, fused on a ~1 km grid every 2 min; free, no key) and declares **UNSAFE if any echo
≥ 20 dBZ is within 30 km** of the dome — a deliberately simple radius, no upwind logic. It also
renders a **TTU-centered radar thumbnail** over a basemap (see *Radar basemap* below) with
the **30 km ring** and **10 km / 10 mi scale bars**, written beside `status.html`; the
observatory page shows it with a source note that names the basemap in use and the
matching attribution. **Two versions are rendered — a dark map for the night page style
and a light (`ttu_radar_day.png`) map for the day style — and CSS shows whichever matches
the page's day/night toggle.** An echo must appear on **two consecutive frames** (one per
poll, ~5 min apart) before
it triggers: MRMS composites occasionally carry a one-frame artefact — an aircraft,
anomalous propagation, ground clutter — and a single frame should not close the dome. The
first, unconfirmed frame is shown on both pages as *"echo within 30 km — unconfirmed (1 of
2 frames), not triggering"* with a grey dot, and it starts no freeze. Live check: unsafe
while a **confirmed** echo is in the ring **and for 15 min after the last one** — a freeze that clear frames do not cancel, so the roof does not
reopen the moment a cell's edge leaves the ring (it also covers the feed going blind). No
frame younger than 20 min → *unavailable*, which does not veto on its own. The
slow tile/radar fetch + render runs in its own thread, so it never delays page/monitor
refresh. Needs Pillow (`sudo apt install python3-pil`); absent → radar disabled.

**Two frame sources, the newer one wins.** The same MRMS frames come from two free
archives, and every poll asks both what they have:

| Source | What | Behind the frame time |
|---|---|---|
| NOAA Open Data on AWS (`noaa-mrms-pds`) | MRMS's own GRIB2 file (~1 MB) | ~1 min |
| Iowa Environmental Mesonet (IEM) | the same grid as an indexed PNG, `lcref` (~0.6 MB) | ~5.5 min |

The poll downloads the **newest** frame (normally the bucket's, so the radar is about 4.5
min fresher than with IEM alone) and, when that one cannot be had — the source does not
answer, lags, or its file is not what it should be — the other source's newest frame in
the same poll. The bucket's GRIB2 is converted to IEM's palette indices exactly
(`floor((dBZ + 32) × 2)`, 0 for "no echo" and "no radar coverage" alike); checked against
IEM's PNG of the same time, cell for cell over the whole grid. A file counts only when it
is MRMS's SeamlessHSR product, of the time its name gives, on IEM's grid; anything else is
refused, never guessed at. Over 120 days (2026-07-13 to 11-10) the two failed at different
times: IEM stalled for 40–70 min on 07-25, 08-05 and 10-09 while the bucket had every file
within ~1.5 min; the bucket stalled for an hour on 09-11, never received 94 min of files
on 10-07 and ran 25–30 min late on 11-10, while IEM was on time. With IEM alone those
three IEM outages left the radar blind for 34–49 min each.

- **A frame counts once**, whichever source delivered it: a stalled feed that keeps
  offering its last frame neither confirms an echo with it (a single artefact frame could
  otherwise "confirm" itself on the next poll) nor is re-downloaded. Frames either side
  of a gap longer than 20 min are not consecutive.
- **Fresh by the frame's own time** (`TTU_SAFETY_RADAR_STALE_SEC`, 20 min), not by when
  it was fetched: a stalled feed cannot keep an old frame current (with IEM alone, frames
  up to 35 min old used to count as live during its stalls).
- A poll without a new frame is **retried after a minute** (`TTU_SAFETY_RADAR_RETRY_SEC`)
  instead of five — three times in a row at most; a source that stays down or stalled is
  then asked every 5 min until a frame arrives (asking every minute walked IEM's whole
  20-min window ~480 times an hour). A host that does not answer is asked nothing more
  that poll, each source's lookup gets at most 30 s (a slow, overloaded source cannot
  starve the other), and no request starts after 120 s: a dead source costs one timeout.
- **Every reply is bounded as a whole**, not only each wait on the socket: a frame must
  arrive within 30 s, a HEAD or listing within 10 s (never more than is left of the
  lookup's 30 s or the poll's 120 s), a reply larger than 32 MB (a listing: 1 MB) is
  refused before or while it is read, and a server trickling a byte every few seconds is
  cut off at the deadline, the host then asked nothing more that poll. Before, such a
  server held the radar thread for as long as it liked, and the radar went *unavailable*
  meanwhile. The bucket's gzip'ed file (~1 MB inflated) is inflated in steps and refused
  beyond 16 MB inflated, so a few kB of gzip cannot inflate to gigabytes in the daemon.
- **Only PNG reaches the image decoder**: IEM's frame and the PNG inside the bucket's
  GRIB2 are checked for PNG's signature and opened by Pillow's PNG reader alone (a
  Pillow too old to restrict the format is refused), so a reply in another format, an EPS
  file for one, is never handed to another decoder (Ghostscript, for EPS). The image's
  size and mode are checked on its header before a pixel is decoded: anything but the
  7000 × 3500 lcref grid in palette or grey indices (the GRIB2 payload: 8 or 16-bit grey)
  is refused, and Pillow's decompression-bomb warning is an error. A PNG of 136 kB that
  declares 12000 × 12000 pixels used to cost 144 MB before it was refused.
- A file **refused for what it contains** (not MRMS's product, another grid or time,
  unreadable) is not downloaded again — the same URL gives the same bytes; the page keeps
  its reason, marked *not downloaded again*, until a newer frame appears. A download that
  failed (no answer, an HTTP error, a reply cut short) is tried again.
- The ring check and the frame's decoded area always describe **the same site**: if the
  GPS position is adopted while a poll is downloading, that frame is set aside and fetched
  again for the new site a minute later.
- The pages say where the frame came from ("frame 02:10Z from NOAA Open Data (AWS)") and
  list each source's newest frame or its problem, so a dead backup shows up before it is
  needed; a source switch is a `RADAR-SOURCE` event. A reply whose `Date` disagrees with
  the Pi's clock is refused (*Clock robustness* above).

**Radar basemap: CARTO first, OpenStreetMap as the backup.** The basemap is built once
per site and map and cached in `~/.cache/ttu-radar/` (`basemap_<hash>.png`), so tiles are
fetched a handful of times per site, never per poll. Each map (night, day) uses the first
source that works (`TTU_SAFETY_RADAR_BASEMAP=auto`, the default):

1. **CARTO, cached on the Pi** — CARTO *Dark Matter* at night, *Positron* by day, from a
   composite already in the cache. The names are the ones the code has always used, so
   the Pi's CARTO maps cached before CARTO's key requirement keep being used, with no
   network and no key. A name covers the site and `TTU_SAFETY_RADAR_THUMB_PX`,
   `TTU_SAFETY_RADAR_THUMB_HALF` and `TTU_SAFETY_RADAR_TILE_ZOOM`: change any of them
   and the cached CARTO maps are left unused (without a key, the new maps then come
   from OpenStreetMap). A map drawn before a GPS position was adopted is rebuilt for the
   adopted site right after the next radar poll.
2. **CARTO with `TTU_SAFETY_CARTO_KEY`** — only when a key is set (without one, no CARTO
   tile is ever requested). Since 2026-09 CARTO answers a tile requested without a valid
   key — none, or a bogus one — with an *"API KEY REQUIRED"* watermark under HTTP 200 and a
   6-month cache lifetime, so the headers cannot tell. The daemon therefore fetches one
   probe tile with and without the key first: identical bytes, or a keyed request CARTO
   refuses (HTTP 401/403), mean the key is not accepted; a failed fetch means it could
   not be checked. Either way it is logged as an ERROR with the key redacted, shown as a
   warning on the page and `/setup`, not re-probed for 6 h, and the chain moves on (an
   unkeyed request CARTO refuses outright only proves that CARTO enforces the key: the
   keyed tile that came through is then used). An accepted key builds the map from keyed
   tiles, each compared with its unkeyed twin as well (2 requests per tile, once per
   site), so a watermarked tile is never drawn; a complete map is cached under the same
   key-free name as step 1, so the next start is step 1 again, and an incomplete one is
   retried in 30 min. Keys are free and need no account: request one by e-mail at
   [carto.com/basemaps/apikey](https://carto.com/basemaps/apikey) (free up to 5M requests
   a month for non-commercial use).
3. **OpenStreetMap standard tiles** — key-free, the backup; the night map inverts the
   tiles' lightness (`TTU_SAFETY_RADAR_TILE_DARK_INVERT`). OSM wants a User-Agent that
   identifies the app — `TTU_SAFETY_NWS_UA`, **which must carry your real e-mail
   address** — and blocks a placeholder contact such as `you@example.org` (HTTP 200 with
   an "access blocked" tile marked not cacheable). With a placeholder UA no OSM tile is
   requested at all (a composite cached earlier is still used), and a tile the server
   marks not cacheable is never drawn.
4. **Nothing** — a plain background, retried every 30 min.

**Tiles are PNG or JPEG of 256 × 256 px** (or 512 × 512, a "@2x" tile, drawn at 256),
read with the same checks as the frames: the signature first, Pillow's PNG and JPEG
readers only, the size on the header before decoding, at most 2 MB and 15 s per tile. A
custom tile server must therefore serve PNG or JPEG (not WebP); any other reply counts as
a failed tile. The CARTO probe tile and the cached composites (PNG, at most twice the
map's size on a side) are read the same way.

`TTU_SAFETY_RADAR_BASEMAP=carto` runs steps 1, 2, 4 (never OpenStreetMap);
`=osm` runs 3, 4 (never CARTO, not even the cached maps). A custom
`TTU_SAFETY_RADAR_TILE_URL` / `_DAY` is handled by its host: another CARTO URL keeps the
CARTO rules of steps 1-2 (only ever fetched with the key, through the same check — so
set the key in `TTU_SAFETY_CARTO_KEY`, and do not put a CARTO key into the URL); an
OpenStreetMap URL means step 3 only for that map; any other URL takes CARTO's place in
steps 1-2. Only a complete basemap is cached; a map left on OpenStreetMap because the key
was rejected or the custom source is down is re-checked every 6 h (after an incomplete
build: every 30 min), so a key activated later is picked up without a restart. The
status page and `/setup` say which basemap each map shows ("Basemap: CARTO Dark Matter
(night) / Positron (day), from tiles cached on this Pi", "… fetched with the configured
CARTO API key", "OpenStreetMap standard tiles (night map colour-inverted), the key-free
fallback; for CARTO maps set TTU_SAFETY_CARTO_KEY …", or "… as set by
TTU_SAFETY_RADAR_BASEMAP=osm" when OpenStreetMap was chosen) with short notes on what
failed, and the attribution follows the maps shown: "© OpenStreetMap contributors, ©
CARTO" whenever a CARTO map is shown, "© OpenStreetMap contributors" for OpenStreetMap
alone.

A cached CARTO map that shows the watermark (one built after the key requirement began)
is removed on its own: the daemon logs the file each map uses (`journalctl -u ttu-safety
| grep 'basemap loaded from cache'`); move just that file out of `~/.cache/ttu-radar/`
and `sudo systemctl restart ttu-safety`. Without a CARTO key a removed CARTO map cannot
be rebuilt from CARTO — it comes back as OpenStreetMap — so keep the good ones
(`rm ~/.cache/ttu-radar/basemap_*.png` removes **every** cached map).

**Hazard overlays.** Both hazard layers (below) are drawn on the two maps whenever
something is present — display only: the radar verdict never reads them. They go after
the radar echoes and beneath the ring, crosshair and scale bars. First the information
layers: HMS smoke as a grey veil (more opaque where denser); the SPC Day-1 outlook as
dashed outlines in SPC's colours for Marginal and above (Marginal in sand, never green;
general thunder is not drawn); SPC mesoscale discussions as purple dashed outlines;
wildfire perimeters and orange-red fire triangles; storm reports as small ink symbols
(down-triangle tornado, dot hail, square wind, diamond flood/rain, x dust, + winter,
small ring anything else). Then the NWS alert areas: filled at
`TTU_SAFETY_HAZARD_FILL_ALPHA` (60/255) under a cased outline (a casing, then the alert
colour), watches and advisories first, warnings on top, a vetoing warning last with a
thicker outline. The casing is the theme's usual one (black
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

Every 5 min **while the sun is below 5°** (night, like WU rain) the daemon fetches the
**GOES-19 GLM total-lightning** granules published since its last poll (at least the last
5 min) from AWS Open Data (anonymous S3, no key)
and **latches UNSAFE for 30 min** (the freeze time) if any flash is within **50 km**. Parsing is **entirely in
RAM** (each granule from a file in `/dev/shm`, in-memory netCDF where there is no
`/dev/shm`) — **no SD-card writes** — and the slow S3/netCDF poll runs in its own thread, so
it never blocks `evaluate()` or the status page. (Read from memory in that thread,
Raspberry Pi OS's HDF5 printed a ~20-line error stack per granule — some 3600 journal lines
an hour at night — although the read itself worked: found by the Raspberry Pi OS test.)
Needs `numpy` + `netCDF4` (`sudo apt install python3-numpy python3-netcdf4`); if absent, GLM
is disabled and the other layers are unaffected. Both status pages show the trigger state and
the distance to the nearest strike. Fail-safe: a fetch error never clears an active latch and
never forces unsafe on its own; "no flashes" is never proof of safety.

**What one poll covers.** GLM writes a granule every 20 s, and a granule reaches S3 some
25–100 s after it starts (5–77 s after its end, over 12 hours measured in November 2026;
now and then after a newer one, by up to a minute). Each poll carries on where the
previous one stopped: it keeps a **cursor**, the start of the newest granule up to which
every granule was read, and the next poll reads from **one minute before the cursor** up
to its own time (a granule published late, after a newer one, is read then; a flash read
twice only renews the 30 minutes). A granule that cannot be downloaded or read, or that
the poll's 75 s ran out before, stops the cursor before it, so the next poll reads it
again — unless that poll gives it up (see "A slow link"). A granule missing from the
listing while newer ones are there, as long as it ended less than 3 min before the poll,
may still be published: the cursor stays at most a minute past it, so the next poll's
window reaches back to it, and if it is listed by then that poll reads it first and has
to read it (a granule in that minute is otherwise one an earlier poll read already).
One missing for longer is taken to be missing for good (GLM drops two or three in a
thousand). The first poll after a daemon start or a clock step has no cursor and reads the last
`TTU_SAFETY_GLM_WINDOW_MIN` (5) minutes; after a long pause (daytime, an outage) a poll
reaches back at most 15 minutes (or the window, if longer), and what came before that is
never read. Either way a poll reads at least the last `TTU_SAFETY_GLM_WINDOW_MIN` minutes —
typically about 20 granules (some 10 MB) at the 5-minute interval, up to about 45 on the
first poll after a pause.

**A slow link.** A poll reads the newest granules first and stops after 75 s. What that
leaves unread is normally read by the next poll (the cursor stays before it), so a poll
that one stalled download cut short loses nothing. But a poll whose backlog — from the
minute before the cursor (without a cursor, its window) — already reaches back the full
15 minutes (the catch-up after a pause, or a backlog that grew while the polls before it
were cut short) cannot catch up by reading again: the next one would be cut short the
same way. It **gives up** the older granules it did not get
to (and one per 20 s of its window plus 10 is the most a poll reads at all; what that
leaves out is given up too): the cursor moves on to the newest granule read, so the next
poll's window is back to its normal size. (Before, the cursor never moved past them: on
a slow link every poll of the night repeated the same catch-up, was cut short the same
way and was partial, and device 1 read `false` all night.) Given up means never read, by
device 0's lightning latch either: the poll that gave them up is partial and says how
much, and any time given up at all restarts device 1's watch (below). In numbers, at the 5-minute interval (on the tests'
simulated link, granules reaching S3 20 s after their end): the 15-minute catch-up of the
first poll of the night (about 45 granules) gives granules up on a link that needs more
than about 1.7 s a granule. After a failed poll the next window reaches back about 11
minutes: on a link slower than about 2 s a granule that poll is cut short too, and from
about 2.3 s a give-up follows within two polls. From about 4.7 s a granule even a normal
window (about 20 granules) cannot be read whole: a poll gives granules up every few
polls, and device 1 reads `false` all night — it does not vouch for lightning data
nobody read.

**Time no poll read** never counts as watched. What a poll leaves behind unread for good —
the oldest granules it gave up, the minutes before a window that reaches back only 15
minutes, a late granule (above) that was given up before it could be read — it reports
(`skipped_s` in the component, in whole granules of 20 s; 0 when it gave nothing up),
and the [dew monitor](#dew-monitor-alpaca-device-1) then starts its
30-minute watch again: good polls close together in time do not make what was never read
between them "watched". (After the daytime pause that is the whole day; the watch starts
afresh then anyway.) This is stricter than the rule for a poll's completeness below,
where up to 2 min unread in a row is no hole — GLM itself drops a granule now and then,
and nothing can be read where nothing was published; a granule given up was there to
be read.

**A partial poll.** A poll is *partial* when what it read leaves more than 2 min unread in
a row from where the previous poll stopped (or from its window's start, without a cursor)
— a hole: a GLM outage, a run of failed downloads, granules given up — when the newest
granule it read ends more than 3 min before the poll (a lagging feed), and whenever its
75 s ran out before it read every new granule, however few were left (they were
published: "no flash" does not cover them until the next poll reads them, or gives them
up). The minute of overlap before the cursor does not count: the previous poll read it.
A flash in
any granule that was read **still arms the latch**; but "no flash" then vouches for
nothing, so the poll is reported with a visible warning ("the last poll did not read its
whole window …"), as `partial` / `coverage` in the component, as *partial window* with
a grey dot (not the green "no strike") on the status page and `/setup`, and the
[dew monitor](#dew-monitor-alpaca-device-1) does not take it as lightning data. A
window in which no granule at all could be read is no data, never "no lightning".

**Limits.** A listing reply may be at most 2 MB and a granule 8 MB (real ones are
0.2–0.6 MB), each within 15 s; a granule that declares more than 100,000 flashes, or
whose `flash_lat` / `flash_lon` are not two 1-D arrays of one length, is unread (checked
before its values are read). A key without a readable start time is ignored. One poll
reads at most one granule per 20 s of its window plus 10 (a late granule first, then the
newest first) and stops after 75 s; what it did not get to is read by the next poll or
given up (see "A slow link"). A flash whose latitude or longitude is not a finite number
in range is dropped (counted, and logged) instead of hiding the real nearest flash.
Every granule is written to a file in
`/dev/shm` named `ttu-glm-*.nc`, read from there and deleted (read from memory only where
there is no `/dev/shm`), so one left by a killed poll is recognisable: the first daemon
start that finds it more than an hour old removes it (*Leftovers of a killed run*, under
Architecture; the automatic restart right after a kill leaves it, and a reboot empties
`/dev/shm` anyway).

**Logging.** A GLM failure is logged once when it starts, with its cause (for granules
that are listed but cannot be read, the first one's error), again when its cause changes
(say, a listing that failed after a boot, then granules that cannot be read), and once
when polls work again; a run of partial polls likewise, with what the first one did not
read (`journalctl -u ttu-safety | grep GLM`). The details of each poll are logged at
debug level only.

### NWS forecast component

A pre-emptive layer: every 15 min the daemon pulls the free **NWS gridpoint forecast**
(api.weather.gov, no key) and flags **UNSAFE if THIS hour or NEXT hour** exceeds any of:
cloud cover > 60%, precip probability > 20%, thunder probability > 15%. A fetch error or a
stale forecast is treated as *unavailable* (does not by itself flip unsafe — it's a
forecast, not a local sensor); a breach in a fresh forecast does. Every grid value is read
as a number (a number sent as text counts; anything else — text, `true`, NaN — is "no
value"). A value that reads as +infinity (`Infinity` or `1e999` in the JSON) is above every
threshold: in cloud cover and the precipitation and thunder probabilities it is read as
100 % and vetoes (the rule `safety/wu_poll.py` applies to a WU rain rate; 100 rather than
infinity keeps `/status` standard JSON); in the other series, and as −infinity anywhere,
it is "no value" (−infinity cloud would read as clear sky to the dew monitor). The grid's times are read by `safety/isotime.py`, which reads the same text alike on
every Python (a time it cannot read makes that download unavailable, never misread). The
gridpoint URL that `/points` names is followed only when it starts with
`https://api.weather.gov/gridpoints/`, and every reply is read whole within 60 s and 4 MB.
Both the daemon's
`/setup` page and the observatory status page show the inputs, the conclusion, and (on the
observatory page) a 12-hour forecast table with a source note.

### NWS alerts component (the hazard veto)

In its own thread (`safety/nws_alerts.py`) the daemon follows the **active NWS alerts**
(api.weather.gov, free, no key). **Only the configured veto events — by default Tornado
Warning, Dust Storm Warning and High Wind Warning (`TTU_SAFETY_HAZARD_VETO_EVENTS`) — can
make the monitor UNSAFE, and only while one is in effect over the observing site, for
the duration of the warning** — or until 24 h after NWS last listed it, if that comes
first (*A veto that cannot be confirmed gone*, below). Every other alert — Severe Thunderstorm Warnings, watches,
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
inflated, so a runaway answer fails the query instead of the daemon's memory; the whole
reply must also arrive within 40 s (twice the 20 s socket timeout), so a server trickling a
byte at a time cannot hold the alerts thread, and a gzip body must end where its stream
does (trailing data or a cut stream fails the query). Times (`sent`, `onset`, `ends`,
`expires`) are read by the project's own ISO-8601 reader (`safety/isotime.py`), alike on
every Python: a form that Python 3.11+ would read but 3.9 would not (basic format, week
dates) is no time at all. Every text taken from an alert (event, headline, description,
instruction, area, sender, NWS headline) is scrubbed (`safety/textclean.py`): a lone
surrogate from a JSON `\ud83d` escape becomes U+FFFD, a control character that separates
words (vertical tab, form feed, U+001C–U+001F, NEL) becomes a blank — so an event name
*"High<VT>Wind Warning"* is still a High Wind Warning, not *"HighWind Warning"* — and the
other control and bidi characters are dropped, so no alert text can stop the page write
or the `/setup` page. Inside a word such a character separates nothing, and whether it
stood between words or inside one cannot be told: the event name is matched against the
veto events both with it as a blank and with it dropped, and vetoes if either reading is a
veto event (*"Hi<VT>gh Wind Warning"* is a High Wind Warning too). The fields that are one
token — `status`, `messageType`, the VTEC string and the times — are read with it dropped:
a blank would break them (a status *"Act ual"* is not *Actual*, and a time with a blank
before its offset is no time).

**Alerts that cannot be read.** A polygon that cannot be read (a coordinate that is not a
number, a ring of fewer than 4 positions, wrong nesting) is treated as no polygon: the
alert is placed by its zones, so a storm-based warning then covers every county it
touches — the cautious side. A feature that cannot be read is never dropped: one the
reader fails on (a zone list that is not a list, ...), and equally one that is not an
object, whose `properties` are not an object, or whose `event`, `status` or `messageType`
is missing, blank or not text — read as it is, such a feature would match no veto event
or not count as in effect, and its query would look complete. What can be read of it
(event, CAP id, VTEC key, `ends`/`expires`; a missing status or type counts as Actual /
Alert) is kept, it is logged once, and the component's error says how many alerts of each
query could not be read. In the
point query such a feature is over the site (NWS listed it there): one of a veto event
vetoes as usual (until its end time if readable, else the 60-min hold), and one whose event
name cannot be read creates no veto but makes that point answer unable to confirm any
release. In the area and civil queries it cannot be placed, so that answer is blind to its
event (to every veto event when the name cannot be read, to every civil event for the
civil query): its silence never releases such a veto. Where a VTEC key has several
messages, one that can be read always wins over one that cannot for what the veto says
(event, headline, onset): an update's readable new end time is applied even when another
segment of the same key cannot be read, and an unread message stands for its key only when
no message of that key can be read. The end is the latest any of the key's messages gives,
a readable `ends`/`expires` of an unread one included: an older readable message never
sets an earlier end than a newer one that cannot be read, and such a message's later end
extends a veto already held (it never shortens one; a missing end in it counts for
nothing — no end is assumed only when no message gives one, or a readable one gives none
and every end given lies within the hour). An answer blind to the warning's event — it has
a message of that event, or of an event it cannot read, that it could not read (the area
query drops those; one whose VTEC key cannot be read stands apart) — cannot move a held
veto's end earlier: that message may be the newer one. One alert whose shape breaks the
site or map test is placed without its shape (point query: still at the site; area query:
by the site's zones only) and never fails the whole poll; a crash in one query never costs
the other its round.

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
time changes, at most hourly for the 24 h bound below, and at most every 10 min for the
last sighting of a warning without an end time (by the alerts thread: an IsSafe
request never touches the SD card; a failed
write keeps the veto in memory, is retried at most once a minute and logged at most every
10 min), and restored at start-up, so a restart or reboot mid-warning keeps the dome
closed. A veto that ran out while the daemon was not running is released at start-up with
a log line and a `HAZARD-VETO` event (*"released at start-up: ended 18:45 CDT"*), never
silently; an entry of the file that cannot be read arms the placeholder veto, as an
unreadable file does (below). A file written by a version before the 24 h bound (no bound
saved with a veto) did not keep the last sighting current: its 24 h are counted from the
restart instead (one more day at most; the file is rewritten with the bound at once), so
the upgrade restart does not drop a long warning NWS is still listing. A warning issued **ahead of its onset** (a High Wind Warning "from 10 AM Friday"
is often issued the afternoon before) vetoes from 15 min before the onset
(`TTU_SAFETY_HAZARD_VETO_ONSET_LEAD_SEC`), not from issuance: the veto covers the
warning's duration, and the dome does not stay shut all night before a daytime wind event.
Until then the page and `/setup` show it as *scheduled*. Once a veto has started it
stays in effect until it is released. Tornado and Dust Storm Warnings take effect when
issued. The onset check also uses NWS's own `sent` times, so a Pi clock running behind
cannot delay a veto.

**Release.** At the warning's end time, 24 h after NWS last listed it (next paragraph), or
**early** only when a fresh point query AND a
fresh area query, both made after the warning was last seen, no longer list it over the
site (cancelled, expired, or its updated polygon has moved off the site). The area query
counts only once the site's zones are known. A warning without an end time is held for
60 min after it was last seen — the last sighting is saved in the latch file, so a restart
does not start a fresh hour.

**A veto that cannot be confirmed gone does not hang on.** A held veto that no fresh query
has listed over the site for **24 h** is released, whatever its end time, with a log line
and the event *"released: not sighted for 24 h, could not be confirmed gone"*: a feed
outage, a wrong clock or an end time days away can hold the dome shut for a day at most
after the warning was last seen. (So a warning issued more than a day ahead of its onset
is released before it starts if no query reaches NWS in between.) A warning that NWS keeps
listing keeps vetoing to its end
(a long-fuse High Wind Warning runs about two days): every sighting renews the 24 h. The
bound is saved in the latch file in one-hour steps — it falls between 24 h and 25 h after
the last sighting, never sooner, and the file is rewritten at most hourly while a long
warning is listed — and it survives restarts and clock steps (a restart never starts a
fresh 24 h; a clock step keeps the time it had left). So with the feeds down a veto is
held until the warning's end time or 24 h after its last sighting, whichever comes first.

Clock steps cannot cut the 24 h short. A veto's own times — its first and last sighting
and its 24 h bound — are readings of the wall clock, and each veto remembers the clock
frame they were taken in (the wall clock against the monotonic one). The moment the alerts
layer sees that frame change — at the next IsSafe call, poll or answer, without waiting
for the daemon's step detector, which reports a step within 30 s
(`TTU_SAFETY_EVAL_INTERVAL`) — it moves each veto by the change of its own frame: each
keeps the time it had left, the answers fetched before the step are dropped, and both
queries re-poll. A veto is moved exactly once whoever sees the step first; one NWS first
listed after the step, in the new frame already, is not moved; and an answer that arrives
after a step (a fetch takes seconds, and chrony's step at boot comes when the network does)
is read in the new clock. Before, a Pi that booted two days behind (fake-hwclock) and was
then stepped by chrony saw the bound of a warning NWS had listed minutes before as two days
old, a step back of about a day moved a veto twice and released a warning with 40 h to
run, a veto first listed between a step and the detector's report lost the step from its
24 h, and a step during start-up — which the detector never reports — held the bound, and
with it the release at the warning's end, until the next restart. (A restart in the
seconds between a step and the alerts layer's first look after it still reads the saved
vetoes in the new clock as they were: a forward step of a day or more releases them then.)

**What is shown.** Every display gives the **warning's own end** as "until", and when the
veto will be released earlier — the 24 h bound comes first, as for every warning with more
than a day to run, even while NWS lists it every minute (each listing moves that time on,
in one-hour steps) — the time it is held to as well, never that time alone. The IsSafe
reason (the "Why unsafe" list on `/setup` and the page) reads, for example:
- *"NWS Tornado Warning in effect for the site until 18:45 CDT (NWS Lubbock TX)"*;
- *"NWS High Wind Warning in effect for the site until Wed 08:12 CDT; held until Tue 17:12
  CDT unless NWS lists it again (NWS Lubbock TX)"*, with *"; not re-confirmed by a fresh
  NWS query"* added inside the parentheses while no current query lists it (a veto held to
  the warning's own end says *"held to its end time — not re-confirmed by a fresh NWS
  query"* instead);
- *"NWS High Wind Warning in effect for the site with no end time given; held until 17:12
  CDT unless NWS lists it again (NWS Lubbock TX)"*.

The `/setup` row (*"VETO — High Wind Warning over the site until Wed 08:12 CDT (held until
Tue 17:12 CDT unless NWS lists it again)"*), the page's banner (*held until ...* beneath
it), the `HAZARD-VETO` events and the log lines give the same times; the page's tile, short
as it is, gives the warning's end. The
component's veto entries carry both: `alert_end_ts` / `alert_end_local`, the warning's own
end, and `end_ts` / `end_local`, the time the veto is held to at the latest. They also
differ while a clock step is being bridged (the veto keeps the time it had left until NWS
lists it again), and the displays say so the same way.

Each start, update, release and restore is recorded as a `HAZARD-VETO` event (like
`RADAR-RAIN`), e.g. *"unsafe until Wed 08:12 CDT; held until Tue 17:12 CDT unless NWS lists
it again"*, or for an extension *"unsafe until Wed 18:12 CDT (was Wed 08:12 CDT); held
until ..."*.

**Availability and fail-safes.** With no veto held, the component is *unavailable* when
both queries are older than 10 min (`TTU_SAFETY_HAZARD_STALE_SEC`), and that does not veto
on its own, like the forecast and radar layers; the connectivity watchdog covers a total
outage. An unreadable latch file — or an entry of it that cannot be read — arms a
placeholder veto, which the first fresh point and
area queries release, and which ends 60 min after it was armed whatever the restarts in
between. An enabled layer that fails to start is shown as failed ("no warning veto"),
not as switched off. A broken or missing `nws_alerts.py` is reported as a `CONFIG`
event and the daemon keeps running. If the component itself raises, the monitor keeps the
vetoes it last reported until the time each was held to (its `end_ts`: the warning's end,
or its 24 h bound), and at most 24 h after the layer's last
report: a bug in the code that reports a tornado warning must never be what reopens the
dome, nor what keeps it shut for good. A site outside NWS coverage disables the
layer loudly.

**Zone outlines.** Zone-based alerts carry no polygon. Their outline is assembled from
their `affectedZones` (`/zones/{forecast,county,fire}/<ID>`), each zone fetched **once**
and kept in `~/.cache/ttu-hazards/zones/`: simplified to about 100 m (below one map pixel)
for zones near the map, a bounding box only for far ones. A statewide event's 51 zones
take about 200 KB instead of 14 MB. At most 40 zones or 15 s are fetched per area poll,
so a first-run backlog never delays the point query for long; an alert is drawn once all
its zones are in (its site test does not need them). A zone reply is used only when it
names the zone asked for (`properties.id`, or its own address), and an outline with a
polygon wider than 40° or taller than 25° (larger than any zone) is not used. A zone that
has no usable outline is asked for again after a day (the fetch time is kept in its cache
file), a failed fetch after 10 min, and an unreadable cache file is fetched again.

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
| Smoke | NOAA/NESDIS Hazard Mapping System analyst smoke polygons (today's KML by UTC date, else yesterday's) | 30 min | whether the site is under smoke in the file's latest analysis (the latest window of the whole file, not just of the smoke near the site), the density (Light / Medium / Heavy) and time window; smoke over the site in an earlier window is reported as earlier. The latest analysis window's polygons are drawn as a grey veil |
| Wildfires | NIFC WFIGS current incident locations and interagency perimeters | 10 min | wildfires (type WF) not out or contained, updated within 72 h, and discovered within 14 days or at least 1,000 acres, within 150 km (and anywhere on the map). Points as orange-red triangles, perimeters as outlines; a perimeter is shown with its listed incident, or on its own when it passes the same test (not out or 100 % contained, updated within 72 h) |
| SPC Day 1 outlook | NOAA Storm Prediction Center categorical outlook | 10 min | the site's category and valid window; dashed outlines for Marginal and above |
| SPC mesoscale discussions | NOAA SPC via the Iowa Environmental Mesonet (IEM) | 10 min | MDs in effect now that touch the map; purple dashed outlines |
| Local storm reports | NWS Local Storm Reports via IEM | 10 min | the last 24 h on the map: type, magnitude, place, time, remark (up to 120 characters; corrections and duplicates folded in, IEM's "Corrects previous ..." note dropped); small ink symbols |

Cadence: `TTU_SAFETY_HAZARD_FEEDS_POLL_SEC` (10 min), and never faster than each feed's
own minimum (5 min for IEM, 10 min for the rest, 30 min for HMS, which is analysed a
few times per day). At the defaults that is 32 requests an hour: 6 each for wildfire
incidents, fire perimeters, the SPC outlook, mesoscale discussions and storm reports,
and 2 for HMS (4 while today's file is not out yet and yesterday's is read). A failed
feed is retried after 5 min. Requests are small: radius and box filters run on the
server, gzip is used where offered, and conditional GETs return a bodiless 304 when
nothing changed. The first round is under 10 KB on a quiet day plus the day's HMS file
(50-300 KB; that server does not gzip it), then a few hundred bytes to a few KB per 10
min, plus the HMS file again whenever it changes.

Display rules. A feed silent for more than two of its intervals (plus 2 min; at least
`TTU_SAFETY_HAZARD_STALE_SEC`) is shown as stale and its items are withheld: old
information is never presented as current, and each product's own time (file date,
valid window, report time) travels with it. A clock step discards everything fetched
under the old clock and re-polls. HMS smoke is analysed from daytime satellite imagery
only and is smoke anywhere in the column (aloft), not surface air quality. SPC paints
general thunder and Marginal green; here they are grey (text only) and sand.

Hostile or broken input. Every reply is read in pieces against a size cap (4 MB, 24 MB
for the HMS file) and a deadline for the whole body (60 s, 180 s for the HMS file): the
20-s socket timeout bounds one wait only, and a server trickling a byte at a time would
otherwise hold the feeds' thread indefinitely. A gzip'ed reply is inflated in 1-MB steps
against the same cap, and one that is cut short or has anything after its stream is
refused. The HMS KML is refused when it declares a DOCTYPE or an entity, in whatever
encoding it is written (an expat pass before the real parse: a UTF-16 document hides
`<!DOCTYPE` from a byte search). Every text taken from a feed is scrubbed before it is
kept (`safety/textclean.py`): a lone surrogate becomes U+FFFD, a control character that
separates words (vertical tab, form feed, U+001C–U+001F, NEL) becomes a blank, the other
control characters (C0, C1) and the bidirectional controls (which can make a text read
other than it is) are dropped, and runs of blanks and line breaks become one blank. Times are read with
`safety/isotime.py`, alike on every Python: `datetime.fromisoformat` reads forms on 3.11+
that 3.9 refuses (and "10:53.5" as 10:53:00.5); a time without an offset is UTC (IEM
writes them so). Each failure is that feed's error; the other feeds go on.

### Status page: the Hazards section

`make_status_page.py` adds a **Hazards** section below the radar map, and the safety card
gains an **NWS warnings** tile: *none* / *VETO* (event, until the warning's end) / *N/A*
(alerts unavailable) / *off*. The section shows:
- a red **UNSAFE: <event> over the site until <time>** banner for each vetoing warning —
  <time> is the warning's own end; when the veto will be released earlier (24 h after NWS
  last listed it) the banner says *held until <then> unless NWS lists it again* beneath
  it, and a warning without an end time reads *no end time given* with its 60-min hold —
  with the issuing office, when it was first seen and which query confirmed it, plus an
  amber *Scheduled* notice for a veto warning whose onset is still ahead;
- **NWS alerts at the site** and **NWS alerts nearby (on the map)**: the colour swatch (the
  map's legend), event, threat tag (TORNADO EMERGENCY, PDS, DESTRUCTIVE, ...), a VETO chip
  on vetoing warnings, headline, issuing office, until when, and the full text and
  instructions in a fold-out; up to 25 per list, all of them drawn on the map;
- **Other hazard information** (marked *information only*): smoke, SPC outlook and
  mesoscale discussions, storm reports and wildfires, each with its feed status and an
  *on map* mark for what the map shows, plus a key to the map's hazard layers; a list
  the daemon caps at 20 items shows the real count and "+N more not listed";
- a sentence saying that only the configured veto events over the site affect the
  monitor, until the warning ends or until 24 h after NWS last listed it, whichever comes
  first.

The display fails safe. If the safety state is stale (the rule the radar section uses),
the section shows "hazard information stale" and no reassuring lists; an unavailable
alert feed reads "unknown", never "no alerts"; a failed information feed is listed under
"no current data", never "none". Whether the map-area query is current is the daemon's
own verdict (it knows `TTU_SAFETY_HAZARD_STALE_SEC`). All feed text is HTML-escaped. Times are shown in
`TTU_SAFETY_LOCAL_TZ`, which the page inherits from the daemon's environment. The daemon's
`/setup` page carries the same lists in compact form, with an "NWS warnings" row in its
inputs table (each veto with the warning's end and, when it comes first, *held until ...
unless NWS lists it again*; a veto warning whose onset is still ahead as *scheduled: ...
(not a veto yet)*), the same fail-safe wording ("unknown", "no current data", "NWS alert
layer off — no veto") and a Sources line. `/setup` passes every text it shows, and then the
whole page, through `textclean.clean_text`: a lone surrogate (a JSON `\ud83d` escape in an
alert) becomes U+FFFD, a control character that separates words becomes a blank, and the
other control and bidirectional-control characters are dropped, so
such a text can no longer make the page answer HTTP 500.

### Status page: its own age, its texts and its writes

**The page dates itself.** The page is a static file, regenerated only by the daemon's
page runner; its meta refresh fetches the same file again. A stopped daemon, or page runs
that fail or hang until `TTU_SAFETY_PAGE_TIMEOUT` kills them, would leave the last page,
a green SAFE included, looking current. So the page element carries `data-generated`
(epoch seconds, by the Pi's clock), `data-stale-after` (seconds) and `data-clock` (the
daemon's clock check, `components.clock.state` of the safety state as the page was
generated), and the page's script compares the generation time with the viewer's clock on
load, every 30 s and when the tab is shown again (`pageshow`, `visibilitychange`). Past
the limit a red banner says "This page was generated N min ago and is no longer being
updated: the safety verdict shown is not current", and the SAFE / UNSAFE and dew-monitor
badges, the input tiles and the veto banners are greyed out. When the viewer's clock is
more than 5 min *behind* the generation time (this device's clock is slow, or the Pi's is
ahead), the page says that its age cannot be judged on this device instead.

The generation time comes from the Pi's own clock, so the age is only as good as that
clock. While the daemon reports it not confirmed by chrony (`data-clock="unconfirmed"`,
e.g. after a power cut, when the Pi boots with fake-hwclock's stale time and chrony has not
synchronised yet), an age past either limit is blamed on neither the page nor the viewer:
a note says that the Pi's clock was not confirmed, so the page's age cannot be judged
(with the age by this device's clock), and nothing is greyed out. The verdict shown is
UNSAFE then anyway: device 0 vetoes until chrony confirms the clock. With the clock
`synchronised`, in its `grace` period, with the check switched off
(`TTU_SAFETY_CLOCK_SYNC=0`) or with no state to read, the age is judged as above. Without
JavaScript a line gives the generation time and the limit (with an unconfirmed clock:
that the time may be wrong). The masthead time is labelled *generated*: it is the time of
the run, not a live clock.

The limit covers the longest normal gap between two pages: max(15 min, 3 ×
`STATUS_PAGE_INTERVAL`), and at least 30 min with `TTU_STATUS_CAMERA=1` (a night run
captures for 120 s, puts some 80 frames through dcraw and stacks them in batches, on the
order of ten minutes on a Pi 4, and the next run starts only after it).
`TTU_STATUS_PAGE_STALE_MIN` sets it in minutes instead (a slower Pi, a longer run).
This is a statement on the page only: the Alpaca devices judge their own inputs' ages
(the STALE badge above is the daemon state's age at generation time).

**Texts.** Every text the page reads back from the daemon's state (alert texts, forecast,
hazard information, reasons, warnings, events) passes through `safety/textclean.py` once,
as the state is read: JSON's `\ud800` escape reads back as a lone surrogate, which no UTF-8
file can hold, and the page write failed on every run until the alert expired. Control and
bidirectional characters are dropped there too (one that separates words becomes a blank). The state's **warnings** (configuration
warnings, a blind rain check, a GPS/coordinates mismatch) are listed under the verdict
whether it is SAFE, UNSAFE or STALE.

**Writes.** Every file the page writes (`status.html`, `safety_inputs.json`, its cache) is
written to a `.tmp` beside it and moved into place, with `errors="replace"` (a character
nothing could encode costs one "?", not the page); a write that fails removes its `.tmp`.
A `.tmp` left by a run that was killed mid-write (`TTU_SAFETY_PAGE_TIMEOUT`, a power cut)
is removed by the first daemon start that finds it more than an hour old (*Leftovers of a
killed run*, under Architecture); the page's own next run overwrites it anyway.

## Test / develop off the Pi

The daemon needs no Pi hardware (it only reads JSON and polls WU over the network):

```bash
pip install flask waitress pytest flake8
python -m pytest safety/tests -q                       # unit + smoke tests (hardware and network stubbed)
# smoke test: feed inputs and hit the API (CLOCK_SYNC=0: a laptop without chrony would
# otherwise read UNSAFE, "system clock not confirmed by chrony"; a lock file of its own:
# the default one, ~/.ttu-safety.lock, is a running daemon's)
TTU_SAFETY_CLOCK_SYNC=0 TTU_SAFETY_INPUTS_FILE=/tmp/in.json TTU_SAFETY_HTTP_PORT=11234 \
    TTU_SAFETY_LOCK_FILE=/tmp/ttu-test.lock python safety_monitor.py &
echo '{"ts":'$(date +%s)',"sun_altitude_deg":-10,"humidity_pct":40}' > /tmp/in.json
curl -X PUT localhost:11234/api/v1/safetymonitor/0/connected -d Connected=true
curl localhost:11234/api/v1/safetymonitor/0/issafe          # -> Value: true
```

**On Raspberry Pi OS itself.** Normal CI (`ci.yml`) runs the suite on x86 with the newest
pip packages, on Python 3.9, 3.11, 3.13 and 3.14; the 3.13 leg (the Pi's Python) also
installs numpy and netCDF4, so the GLM layer's netCDF and flash tests, which skip without
them, run there too. `raspios.yml` runs it inside real Raspberry Pi OS 64-bit images under
QEMU, with the Pi's own packages from apt, and test-deploys the daemon there:
`ci/raspios_test.sh` installs README.md's packages, runs flake8 and the suite, runs
`deploy/install.sh` for a throwaway user `ttuci` (systemctl stood in for — nothing boots in
a chroot), starts the daemon the way the installed unit does (live NOAA radar and GLM data
included; no map tiles), asks both Alpaca devices and `/setup`, and stops it with SIGTERM.
One image is pinned to the Pi's release (trixie), the other is whatever Raspberry Pi
currently publishes as its newest, so a new OS release is tested before the Pi is
upgraded. It runs on pushes to main (not for documentation-only changes), weekly and by
hand (*Actions → Raspberry Pi OS → Run workflow*). The script also runs as root in any
throwaway Raspberry Pi OS system (`sudo ci/raspios_test.sh` on a scratch SD card) — **never
on the observatory Pi**: it installs a `ttu-safety` unit, and it refuses to run where a
`ttu-safety` unit for any other user exists.
In CI the image is a plain chroot that shares the runner VM's processes, so the script
never picks processes by user: it stops only its own daemon's process group, and `ttuci`
gets a fixed UID (4242) that cannot be the GitHub runner's (1001). If a run ends
"cancelled" without anyone cancelling it, read the full job log
(`gh api repos/kirxkirx/ttustatus/actions/jobs/<job id>/logs`): `gh run view --log` drops
the line "The runner has received a shutdown signal".
