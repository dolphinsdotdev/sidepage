#!/usr/bin/env bash
# start_site.sh — launch `sidepage serve` or `sidepage proxy` in the
# background and report back once it's actually up (or has failed),
# instead of leaving the caller blocked on a process that never returns.
#
# `sidepage serve` takes exactly ONE positional argument, which is either
# a fresh target (script/dir/notebook) or the name of an already-registered
# app — never both. `sidepage proxy` takes no positional target at all,
# just --port. This script has three modes to match:
#
#   New target, not yet registered:
#     start_site.sh new <app-name> <target> [serve flags...]
#     -> runs: sidepage serve <target> --name <app-name> [flags...]
#
#   Already-registered app (via `sidepage app register`):
#     start_site.sh registered <app-name> [override flags...]
#     -> runs: sidepage serve <app-name> [override flags...]
#
#   Already-running local service (sidepage never launches it):
#     start_site.sh proxy <app-name> --port <n> [proxy flags...]
#     -> runs: sidepage proxy --name <app-name> --port <n> [flags...]
#
# Examples:
#   start_site.sh new demo tests/fixtures/streamlit-app/app.py --auth token --anon
#   start_site.sh registered abc-app --scope web
#   start_site.sh proxy vite-demo --port 5173 --anon
#
# Prints one line of JSON to stdout:
#   {"status":"running","app":"demo","pid":12345,"log":"/path/to/log","url":"https://...trycloudflare.com"}
#   {"status":"failed","app":"demo","pid":12345,"log":"/path/to/log","error":"last lines of log..."}
#
# Note for `proxy`: a "running" status only means the proxy/tunnel came up
# — it does not mean the service on --port exists or is healthy. A missing
# upstream still shows as "running" here (the proxy itself started fine)
# but will serve errors until something is actually listening on that port.

set -u

MODE="${1:?Usage: start_site.sh new|registered|proxy <app-name> ...}"
shift
APP_NAME="${1:?Usage: start_site.sh new|registered|proxy <app-name> ...}"
shift

case "$MODE" in
  new)
    TARGET="${1:?new mode requires a target: start_site.sh new <app-name> <target> [flags...]}"
    shift
    SIDEPAGE_CMD="serve"
    SERVE_ARGS=("$TARGET" --name "$APP_NAME" "$@")
    ;;
  registered)
    SIDEPAGE_CMD="serve"
    SERVE_ARGS=("$APP_NAME" "$@")
    ;;
  proxy)
    SIDEPAGE_CMD="proxy"
    SERVE_ARGS=(--name "$APP_NAME" "$@")
    ;;
  *)
    echo '{"status":"failed","error":"mode must be new, registered, or proxy"}' >&2
    exit 1
    ;;
esac

LOG_DIR="${SIDEPAGE_SKILL_LOG_DIR:-$HOME/.local/state/sidepage/skill-logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/${APP_NAME}.log"

# nohup + disown so the process survives this script (and this shell)
# exiting — required because neither `serve` nor `proxy` has a built-in
# daemon mode.
nohup sidepage "$SIDEPAGE_CMD" "${SERVE_ARGS[@]}" > "$LOG_FILE" 2>&1 &
PID=$!
disown "$PID" 2>/dev/null || true

# Poll the log for a sign of life. A tunnel URL, a "listening on", or an
# explicit error all count as "we know the outcome now." Cap the wait so a
# hung app doesn't block the caller forever.
MAX_WAIT=30
WAITED=0
URL=""
STATUS="starting"

while (( WAITED < MAX_WAIT )); do
  if ! kill -0 "$PID" 2>/dev/null; then
    STATUS="failed"
    break
  fi

  # Common shapes: a *.trycloudflare.com URL, a custom --domain URL, or a
  # plain http://127.0.0.1:PORT for local-only (no --anon/--domain) runs.
  FOUND_URL=$(grep -Eo 'https?://[^ ]+\.trycloudflare\.com[^ ]*|https?://[a-zA-Z0-9.-]+/[^ ]*|http://127\.0\.0\.1:[0-9]+[^ ]*' "$LOG_FILE" 2>/dev/null | tail -1)
  if [[ -n "$FOUND_URL" ]]; then
    URL="$FOUND_URL"
    STATUS="running"
    break
  fi

  if grep -qiE 'error|traceback|failed to' "$LOG_FILE" 2>/dev/null; then
    STATUS="failed"
    break
  fi

  sleep 1
  WAITED=$((WAITED + 1))
done

if [[ "$STATUS" == "running" ]]; then
  printf '{"status":"running","app":"%s","pid":%s,"log":"%s","url":"%s"}\n' \
    "$APP_NAME" "$PID" "$LOG_FILE" "$URL"
  exit 0
elif [[ "$STATUS" == "failed" ]]; then
  ERR=$(tail -20 "$LOG_FILE" 2>/dev/null | tr '\n' ' ' | sed 's/"/\\"/g')
  printf '{"status":"failed","app":"%s","pid":%s,"log":"%s","error":"%s"}\n' \
    "$APP_NAME" "$PID" "$LOG_FILE" "$ERR"
  exit 1
else
  # Still starting after MAX_WAIT — not necessarily broken (slow deps to
  # resolve via `uv run`), just inconclusive. Leave it running in the
  # background and report what we know.
  printf '{"status":"starting","app":"%s","pid":%s,"log":"%s","note":"no URL detected after %ss; check the log or run sidepage status %s"}\n' \
    "$APP_NAME" "$PID" "$LOG_FILE" "$MAX_WAIT" "$APP_NAME"
  exit 0
fi
