#!/bin/sh
set -eu

# Default: run daily at 03:15
: "${CRON_SCHEDULE:=15 3 * * *}"

# Write crontab
echo "${CRON_SCHEDULE} python /app/app.py >> /var/log/autodeletarr.log 2>&1" > /etc/crontabs/root

echo "[autodeletarr] Cron schedule: ${CRON_SCHEDULE}"
echo "[autodeletarr] Starting cron (busybox crond)"
touch /var/log/radarr-autodelete.log

# Run once on start if requested
if [ "${RUN_ON_STARTUP:-false}" = "true" ]; then
  echo "[autodeletarr] RUN_ON_STARTUP=true => running once now"
  python /app/app.py || true
fi

exec crond -f -l 2
