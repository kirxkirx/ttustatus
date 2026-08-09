#!/bin/bash
# Regenerate the observatory status page every STATUS_PAGE_INTERVAL seconds.
# Location-independent: runs make_status_page.py from this script's own directory, so it
# works wherever the repo is cloned (e.g. ~/ttustatus). Logging is left to the caller
# (the @reboot cron redirects stdout/stderr to ~/statuspage.log).
cd "$(dirname "$0")" || exit 1

export STATUS_PAGE_INTERVAL=90

while true
do
    start=$(date +%s)
    /usr/bin/python3 ./make_status_page.py
    end=$(date +%s)
    elapsed=$((end - start))
    sleep_time=$((STATUS_PAGE_INTERVAL - elapsed))
    if [ "$sleep_time" -gt 0 ]; then
        sleep "$sleep_time"
    fi
done
