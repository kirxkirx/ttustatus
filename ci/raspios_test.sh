#!/bin/bash
# Test-deploy ttustatus inside a THROWAWAY Raspberry Pi OS system, as root: the GitHub job
# runs it in an image's chroot under QEMU; it also runs in any Raspberry Pi OS root
# filesystem or on a scratch SD card — never on the observatory Pi (see the guard below).
#
#   1. installs what README.md's "Deploy" section tells the Pi's owner to install, from
#      the image's own apt sources — so the suite runs against the Pi's Python, Pillow,
#      numpy and netCDF4, not the newest ones pip would fetch (normal CI);
#   2. flake8 and the whole test suite, exactly as normal CI runs them;
#   3. a deployment smoke test: deploy/install.sh for a throwaway user "ttuci" (with a
#      stand-in for systemctl: no systemd runs in a chroot), then the daemon started the
#      way the installed unit starts it, and asked what NINA and the dome driver ask.
#
# usage: ci/raspios_test.sh [repo]          (default: the directory above this script)
# env:   PORT (default 11111, the unit's), SKIP_APT=1 (packages already installed)
set -euo pipefail
REPO=$(cd "${1:-$(dirname "$0")/..}" && pwd)
PORT=${PORT:-11111}
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8

# GUARD. Step 3 installs a ttu-safety systemd unit (deploy/install.sh writes
# /etc/systemd/system/ttu-safety.service) for the throwaway user U. Where a ttu-safety unit
# for anyone else exists — the observatory Pi, or a scratch card that was deployed for real
# since — that would replace the real unit, so this stops here. The check is on the unit
# ITSELF, every run: only a unit made for U by an earlier run of this script passes.
U=ttuci
UNIT=/etc/systemd/system/ttu-safety.service
if [ -e "$UNIT" ] && ! grep -qx "User=$U" "$UNIT"; then
    echo "a ttu-safety unit for another user is installed here: this script is for" \
         "throwaway systems only" >&2
    exit 2
fi
# shellcheck source=/dev/null
. /etc/os-release
echo "== $PRETTY_NAME (Debian $(cat /etc/debian_version)), $(uname -m)," \
     "$(head -1 /etc/rpi-issue 2>/dev/null || echo 'not a Raspberry Pi OS image')"

# What this script changes outside the throwaway user, undone on the way out however it
# ends: a policy-rc.d that keeps apt from starting services (a chroot has no init to stop
# them again, and one left running keeps the image busy when CI unmounts it), a tmpfs on
# /dev/shm, and the daemon with anything else of the throwaway user still running.
POLICY=/usr/sbin/policy-rc.d
CREATED_POLICY=0
SHM_MOUNTED=0
PID=
PGID=
cleanup() {
    # The daemon's process group - everything it started, and only that (setsid, below):
    # TERM, up to 30 s to go, then KILL. Nothing here is chosen by user name or UID. The
    # CI chroot shares the runner VM's process table: `pkill -u "$U"` once sent SIGTERM to
    # GitHub's own runner (U had UID 1001, the runner's), and a run that had passed
    # everything ended "cancelled" a second later.
    if [ -n "$PGID" ]; then
        kill -TERM -- "-$PGID" 2>/dev/null || true
        for _ in $(seq 1 30); do kill -0 -- "-$PGID" 2>/dev/null || break; sleep 1; done
        kill -KILL -- "-$PGID" 2>/dev/null || true
    fi
    # Never leave the tmpfs on /dev/shm: the image's /dev is the HOST's /dev, bound in, and
    # the action's own clean-up answers a /dev it cannot unmount with `fuser -k` on it.
    # Lazily, if a process still holds a file there.
    if [ "$SHM_MOUNTED" = 1 ]; then
        umount /dev/shm 2>/dev/null || umount -l /dev/shm 2>/dev/null || true
    fi
    if [ "$CREATED_POLICY" = 1 ]; then rm -f "$POLICY"; fi
}
trap cleanup EXIT

# ---- 1. the packages ---------------------------------------------------------------------
if [ "${SKIP_APT:-0}" != 1 ]; then
    if [ ! -e "$POLICY" ]; then
        printf '#!/bin/sh\nexit 101\n' > "$POLICY" && chmod +x "$POLICY" && CREATED_POLICY=1
    fi
    apt-get -q update
    # README.md's line, plus the test tools (python3-pytest, flake8) and curl
    apt-get -q install -y --no-install-recommends python3-flask python3-waitress git chrony \
        python3-pil python3-numpy python3-netcdf4 python3-pytest flake8 curl
fi
# /dev/shm must be the RAM tmpfs it is on the Pi: the daemon keeps its state, the weather
# log and the radar maps there, and reads GLM granules from it. CI's chroot gets the host's
# /dev without its submounts, which leaves the image's bare, root-only /dev/shm directory.
if ! mountpoint -q /dev/shm; then
    mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm && SHM_MOUNTED=1
fi
python3 - <<'PY'
import importlib.metadata as md, sys
print("python", sys.version.split()[0], *("| %s %s" % (p, md.version(p)) for p in
      ("pillow", "numpy", "netCDF4", "flask", "waitress", "pytest")))
PY
chronyc --version | head -1

# ---- 2. lint and the test suite ------------------------------------------------------------
cd "$REPO"
flake8 --max-line-length=100 --extend-ignore=E203,E501,W503,E402 safety
python3 -m pytest safety/tests -q -p no:cacheprovider

# ---- 3. deploy and run it --------------------------------------------------------------------
# A user of its own (U, above), so no real user's home is ever touched — made afresh every
# run, so nothing of an earlier run (event log, latches, caches) can pass for this one's.
if id "$U" >/dev/null 2>&1; then userdel --remove "$U" 2>/dev/null || true; fi
# (userdel leaves a home it does not consider U's, and useradd would then reuse it as it
# is - with the old owner, unreadable for the new U)
rm -rf "/home/${U:?}"
# A UID of its own. useradd's next free one is 1001 in these images (pi has 1000), and in
# the CI chroot, which shares the runner VM's processes, 1001 is GitHub's "runner" user:
# the daemon would run AS the runner (it could signal it, read its /proc, count against
# its process limit). Nobody uses 4242 on GitHub's runners; refuse if anything here does.
U_UID=4242
if ps -eo ruid=,euid= | tr -s ' ' '\n' | grep -qx "$U_UID"; then
    echo "UID $U_UID is in use by a process on this machine: pick another U_UID" >&2
    exit 1
fi
useradd --create-home --uid "$U_UID" "$U"
APP=/home/$U/ttustatus
mkdir -p "$APP"
tar -C "$REPO" --exclude=.git -cf - . | tar -C "$APP" -xf -
chown -R "$U": "$APP"
cp "$APP/ttustatus.env.example" "/home/$U/ttustatus.env"          # README.md, step 3
# there is no real WU key here: an empty one switches rain polling off cleanly (with the
# example's placeholder, WU answers 401, which the daemon logs with a traceback)
sed -i 's/^TTU_SAFETY_WU_KEY=.*/TTU_SAFETY_WU_KEY=/' "/home/$U/ttustatus.env"
chown "$U": "/home/$U/ttustatus.env" && chmod 600 "/home/$U/ttustatus.env"

# deploy/install.sh as `sudo ./deploy/install.sh` run by that user, systemctl stood in for
SHIM=$(mktemp -d)
printf '#!/bin/sh\necho "systemctl $*" >> %s/calls\n' "$SHIM" > "$SHIM/systemctl"
chmod +x "$SHIM/systemctl"
SUDO_USER=$U PATH="$SHIM:$PATH" "$APP/deploy/install.sh" > "$SHIM/install.out"
grep -qx "User=$U" "$UNIT"
grep -qx "WorkingDirectory=$APP" "$UNIT"
grep -qx "ExecStart=/usr/bin/python3 $APP/safety_monitor.py" "$UNIT"
grep -qx "EnvironmentFile=-/home/$U/ttustatus.env" "$UNIT"
grep -qx "systemctl enable --now ttu-safety" "$SHIM/calls"
# the journal cap, and that journald reads it: the image's own setting stays in force
# (Raspberry Pi OS: Storage=volatile) and only the size limit is added
grep -qx "SystemMaxUse=1G" /etc/systemd/journald.conf.d/60-ttu-safety.conf
if command -v systemd-analyze >/dev/null; then
    systemd-analyze cat-config systemd/journald.conf > "$SHIM/journald.conf"
    grep -qx "SystemMaxUse=1G" "$SHIM/journald.conf"
fi
echo "install.sh: unit and journal cap OK ($(grep -c . "$SHIM/calls") systemctl calls)"

# the daemon as the unit starts it: its user, its EnvironmentFile, its working directory.
# The status page generator needs the Pi's hardware (DHT11, GPS), so it is off and the
# inputs it would write are written here; every other layer runs, network included. Its
# web files go to a scratch web root, never a real /var/www/html.
RUN=/run/ttu-ci
rm -rf "${RUN:?}" && mkdir -p "$RUN/www" && chown -R "$U": "$RUN"
LOG=$RUN/daemon.log
cat > "$RUN/inputs.json" <<EOF
{"ts": $(date +%s), "sun_altitude_deg": -20.0, "humidity_pct": 40.0, "humidity_age_s": 0}
EOF
chown "$U": "$RUN/inputs.json"
# (setpriv, not runuser: it execs, so $! is the daemon itself and gets the SIGTERM below;
# env -i: a service's environment is as bare as this, plus its EnvironmentFile)
# (setsid: a session and process group of its own - the daemon's PID is its group ID - so
# cleanup can stop it together with anything it started, and only that. A background job
# of this non-interactive shell is no group leader, so setsid does not fork and $! stays
# the daemon's PID.)
setsid setpriv --reuid="$U" --regid="$U" --init-groups env -i HOME="/home/$U" USER="$U" \
    LOGNAME="$U" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 /bin/bash -c "
    set -a; . /home/$U/ttustatus.env; set +a
    export TTU_SAFETY_PAGE=0 TTU_SAFETY_HTTP_HOST=127.0.0.1 TTU_SAFETY_HTTP_PORT=$PORT
    export TTU_SAFETY_INPUTS_FILE=$RUN/inputs.json TTU_SAFETY_RADAR_THUMB=$RUN/www/ttu_radar.png
    export TTU_SAFETY_WEATHER_LOG_DIR=$RUN/www/weather_log
    # No map tiles from CI: carto without a key and without a cached map draws a plain
    # background (OpenStreetMap's tile servers are not for test traffic). And say who asks.
    export TTU_SAFETY_RADAR_BASEMAP=carto
    export TTU_SAFETY_NWS_UA='ttu-safety-monitor CI (+https://github.com/kirxkirx/ttustatus)'
    cd $APP && exec /usr/bin/python3 $APP/safety_monitor.py" > "$LOG" 2>&1 &
PID=$!
PGID=$PID                                  # (stopped by cleanup, however the script ends)
API=http://127.0.0.1:$PORT
for _ in $(seq 1 180); do                   # emulated ARM: imports take a while
    curl -sf "$API/management/apiversions" >/dev/null && break
    kill -0 "$PID" 2>/dev/null || { cat "$LOG"; echo "the daemon exited"; exit 1; }
    sleep 1
done
curl -sf "$API/management/apiversions" >/dev/null || { cat "$LOG"; echo "no answer"; exit 1; }
# what cleanup relies on: the daemon leads its own process group
if [ "$(ps -o pgid= -p "$PID" | tr -d ' ')" != "$PID" ]; then
    ps -o pid,pgid,sid,user,args -p "$PID" || true
    echo "the daemon is not in a process group of its own"
    exit 1
fi
# The radar polls at once, day and night, and GLM too (the inputs above say "night"):
# give both a moment to read real data — a real AWS frame decoded with the Pi's Pillow,
# real GLM granules read with its netCDF4/HDF5. (Not a failure if NOAA is slow today.)
for _ in $(seq 1 180); do
    curl -sf "$API/status" | python3 -c '
import json, sys
c = json.load(sys.stdin)["components"]
sys.exit(0 if c["radar"]["available"] and (c["glm"]["available"] or c["glm"]["error"]) else 1)' \
        && break
    sleep 1
done
python3 - "$API" <<'PY'
import json, sys, urllib.parse, urllib.request
api = sys.argv[1]
get = lambda p: urllib.request.urlopen(api + p, timeout=30).read().decode()


def put(path, **form):
    req = urllib.request.Request(api + path, data=urllib.parse.urlencode(form).encode(),
                                 method="PUT")
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


devs = json.loads(get("/management/v1/configureddevices"))["Value"]
assert sorted((d["DeviceType"], d["DeviceNumber"]) for d in devs) == [
    ("SafetyMonitor", 0), ("SafetyMonitor", 1)], devs
for n in (0, 1):
    # as NINA and the dome driver do: connect first, then ask (unconnected: error 1031)
    r = put("/api/v1/safetymonitor/%d/connected" % n, Connected="true", ClientID="7",
            ClientTransactionID="5")
    assert r["ErrorNumber"] == 0, r
    r = json.loads(get("/api/v1/safetymonitor/%d/issafe?ClientTransactionID=7" % n))
    assert r["ErrorNumber"] == 0 and isinstance(r["Value"], bool), r
    print("device %d IsSafe:" % n, r["Value"])
st = json.loads(get("/status"))
clock, radar = st["components"]["clock"], st["components"]["radar"]
assert clock["enabled"] and clock["state"] in ("synchronised", "grace", "unconfirmed"), clock
print("clock:", clock["state"], "-", clock["why"] or "%s, stratum %s" % (
    clock["reference"], clock["stratum"]))
# (a chroot shares the host's network: chronyc may reach the HOST's chronyd and see it
# synchronised, or find none. Either way the verdict must agree with it; the veto itself
# is pinned by the unit tests, safety/tests/test_timesync.py.)
clock_reasons = [r for r in st["reasons"] if r.startswith("system clock not confirmed")]
if clock["state"] == "unconfirmed":
    assert st["is_safe"] is False and clock_reasons, st
else:
    assert not clock_reasons, st
print("radar:", "frame %s from %s" % (radar["frame_utc"], radar["frame_source_name"])
      if radar["available"] else "no frame yet - %s" % radar.get("why"))
glm = st["components"]["glm"]
print("GLM:", "%s granules read, nearest flash %s km" % (glm["granules_scanned"],
      glm["nearest_km"]) if glm["available"] else "no data - %s" % glm.get("error"))
print("reasons:", st["reasons"])
setup = get("/setup")
assert "System clock (chrony)" in setup and "TTU Safety Monitor" in setup
PY
# an orderly stop, as systemd does it, and nothing went wrong on the way
kill -TERM "$PID"
for _ in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
if kill -0 "$PID" 2>/dev/null; then echo "the daemon ignored SIGTERM"; exit 1; fi
PID=
grep -q "SHUTDOWN" "/home/$U/safety_events.log"
if grep -n "Traceback" "$LOG"; then cat "$LOG"; echo "a traceback in the daemon's log"; exit 1; fi
# Raspberry Pi OS's HDF5 printed an error stack for every GLM granule read from memory in
# the GLM thread (journal spam); glm_lightning reads them from /dev/shm since
if grep -n "HDF5-DIAG" "$LOG"; then echo "HDF5 error stacks in the daemon's log"; exit 1; fi
echo "== deployment smoke test passed"
