#!/usr/bin/env bash
# stop_site.sh — tear down a running sidepage app and confirm it's gone.
# Works the same for an app started via `serve` or `proxy`.
#
# Note: for a `proxy` app, this only tears down sidepage's proxy/tunnel/
# registry entry — the underlying service on --port was never launched by
# sidepage and keeps running after this.
#
# Usage: stop_site.sh <app-name>

set -u
APP_NAME="${1:?Usage: stop_site.sh <app-name>}"

sidepage stop "$APP_NAME" > /tmp/sidepage-stop-"$APP_NAME".log 2>&1
STOP_EXIT=$?

sleep 1
STATUS_OUTPUT=$(sidepage status "$APP_NAME" 2>&1)

if [[ $STOP_EXIT -ne 0 ]]; then
  echo "{\"status\":\"error\",\"app\":\"$APP_NAME\",\"detail\":\"stop command exited non-zero, see /tmp/sidepage-stop-$APP_NAME.log\"}"
  exit 1
fi

echo "{\"status\":\"stopped\",\"app\":\"$APP_NAME\",\"status_check\":\"$(echo "$STATUS_OUTPUT" | tr '\n' ' ' | sed 's/"/\\"/g')\"}"
