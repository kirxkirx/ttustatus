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

systemctl daemon-reload
systemctl enable --now ttu-safety
systemctl --no-pager --lines=0 status ttu-safety || true

cat <<EOF

Done. The service is enabled and running (daemon + status page in one).
  logs:     journalctl -u ttu-safety -f
  restart:  sudo systemctl restart ttu-safety

Migrating from an older setup? Remove leftovers:
  crontab -e                     # delete any '@reboot ... run_status_page.sh' line
  pkill -f run_status_page.sh    # stop an old page loop
  sudo systemctl disable --now ttu-statuspage 2>/dev/null || true
EOF
