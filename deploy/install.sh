#!/bin/bash
# Install the ttustatus service under systemd. ONE service runs everything: the safety
# daemon, which itself spawns make_status_page.py every ~90 s as a subprocess.
# Usage:  sudo ./deploy/install.sh
# Idempotent: rerun after a git pull to pick up unit-file changes.
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(stat -c %U "$REPO")}"

if [ "$(id -u)" -ne 0 ]; then
    echo "run me with sudo:  sudo $0" >&2
    exit 1
fi

echo "repo: $REPO   user: $RUN_USER"

sed -e "s|__REPO__|$REPO|g" -e "s|__USER__|$RUN_USER|g" \
    "$REPO/deploy/ttu-safety.service" > /etc/systemd/system/ttu-safety.service
echo "installed /etc/systemd/system/ttu-safety.service"

# Cap the systemd journal (where the daemon's own log lines go) at 1 GB, like the
# daemon's log files. Raspberry Pi OS keeps the journal in RAM (Storage=volatile, from
# /usr/lib/systemd/journald.conf.d/40-rpi-volatile-storage.conf): no SD-card writes, a
# cap of its own (10 % of /run), lost at a reboot, and this line changes nothing. It
# applies once the journal is kept on the SD card (Storage=persistent). Not restarted here:
# a journald restart is not needed for a RAM journal; README_SAFETY.md says when it is.
JOURNAL_CAP=/etc/systemd/journald.conf.d/60-ttu-safety.conf
mkdir -p "$(dirname "$JOURNAL_CAP")"
cat > "$JOURNAL_CAP" <<'EOF'
# Written by ttustatus deploy/install.sh (rerunning it rewrites this file; delete it to
# undo). Caps the journal at 1 GB when it is kept on the SD card (Storage=persistent).
[Journal]
SystemMaxUse=1G
EOF
echo "installed $JOURNAL_CAP (journal on the SD card: at most 1 GB)"

systemctl daemon-reload
systemctl enable --now ttu-safety
systemctl --no-pager --lines=0 status ttu-safety || true

cat <<EOF

Done. The service is enabled and running (daemon + status page in one).
  logs:     journalctl -u ttu-safety -f
  restart:  sudo systemctl restart ttu-safety
  events:   ~/safety_events.log (+ its .1 backup; together at most
            TTU_SAFETY_EVENT_LOG_MAX_MB, default 1 GB)

Migrating from an older setup? Remove leftovers:
  crontab -e                     # delete any '@reboot ... run_status_page.sh' line
  pkill -f run_status_page.sh    # stop an old page loop
  sudo systemctl disable --now ttu-statuspage 2>/dev/null || true
EOF
