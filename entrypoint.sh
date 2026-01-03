#!/bin/sh
set -eu

: "${CRON_SCHEDULE:=15 3 * * *}"

echo "${CRON_SCHEDULE} python /app/app.py >> /var/log/agregarr-cleanarr.log 2>&1" > /etc/crontabs/root

echo "[agregarr-cleanarr] Cron schedule: ${CRON_SCHEDULE}"
touch /var/log/agregarr-cleanarr.log

if [ "${RUN_ON_STARTUP:-false}" = "true" ]; then
  echo "[agregarr-cleanarr] RUN_ON_STARTUP=true => running once now"
  python /app/app.py || true
fi

exec crond -f -l 2
