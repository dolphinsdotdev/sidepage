#!/bin/bash
# Regenerates docs/media/serve-demo.gif (the README Quickstart demo).
# Needs: `sidepage` on PATH, `asciinema` (>=3), and `agg`
# (https://docs.asciinema.org/manual/agg/ — `brew install agg`).
#
# Runs sidepage serve directly in the recorded pty (not backgrounded) so
# its real colored Rich output is captured as-is; --timeout gives it a
# clean, scripted end instead of needing a manual Ctrl+C.
set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAST="$(mktemp -d)/serve-demo.cast"

demo() {
  export SIDEPAGE_HOME="$(mktemp -d)"

  DEMO_DIR=$(mktemp -d)
  cat > "$DEMO_DIR/index.html" <<'HTML'
<!doctype html>
<html>
<head><title>hello from sidepage</title></head>
<body style="font-family: system-ui; text-align:center; margin-top: 22vh;">
<h1>👋 hello from sidepage</h1>
<p>this page is being served from my laptop.</p>
</body>
</html>
HTML

  type_line() {
    printf '\033[1;36m$\033[0m '
    local text="$1" i
    for (( i=0; i<${#text}; i++ )); do
      printf '%s' "${text:$i:1}"
      sleep 0.028
    done
    printf '\n'
    sleep 0.3
  }

  clear
  type_line "sidepage serve ./my-site --name demo --anon --timeout 14"
  sidepage serve "$DEMO_DIR" --name demo --anon --timeout 14
  sleep 1.5
}
export -f demo

TERM=xterm-256color asciinema rec --headless --window-size 96x10 --idle-time-limit 2 \
  -c "bash -c demo" --overwrite "$CAST"

agg --theme github-dark --font-size 16 "$CAST" "$HERE/serve-demo.gif"

echo "wrote $HERE/serve-demo.gif"
