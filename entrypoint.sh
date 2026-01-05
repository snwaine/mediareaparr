#!/bin/sh
set -eu

: "${WEBUI_PORT:=7575}"
: "${CONFIG_DIR:=/config}"
: "${CRON_SCHEDULE:=15 3 * * *}"
: "${LOG_PATH:=/var/log/mediareaparr.log}"

mkdir -p "$CONFIG_DIR"
touch "$LOG_PATH"

# Write initial cron file
echo "${CRON_SCHEDULE} python /app/app.py >> ${LOG_PATH} 2>&1" > /etc/crontabs/root

echo "[mediareaparr] WebUI on :${WEBUI_PORT}"
echo "[mediareaparr] Cron schedule: ${CRON_SCHEDULE}"
echo "[mediareaparr] Log: ${LOG_PATH}"

# Start WebUI in background
python /app/webui.py --host 0.0.0.0 --port "${WEBUI_PORT}" >/dev/null 2>&1 &

# Optional run on startup
if [ "${RUN_ON_STARTUP:-false}" = "true" ]; then
  echo "[mediareaparr] RUN_ON_STARTUP=true => running once now"
  python /app/app.py || true
fi

# Background watcher: if /config/run_now.flag appears, run immediately
(
  while true; do
    if [ -f "${CONFIG_DIR}/run_now.flag" ]; then
      rm -f "${CONFIG_DIR}/run_now.flag"
      echo "[mediareaparr] Run Now triggered"
      python /app/app.py || true
    fi
    sleep 5
  done
) &

# Start cron in foreground (PID 1) so SIGHUP reload works
echo "[mediareaparr] Starting cron"
exec crond -f -l 2
