# Timeouts, lazy start, and peers

[← back to README](../../README.md)

**Auto-teardown.** `--timeout <seconds>` stops the app once its total
lifetime (from `serve` start) reaches the limit; `--idle-timeout
<seconds>` stops it once that many seconds pass with no proxied HTTP
request or WebSocket message — the timer resets on every one. Both are
composable with each other and checked in the same blocking loop Ctrl+C
already interrupts, so an auto-stop tears down exactly like `sidepage
stop` would: immediately, no drain window.

```bash
sidepage serve demo.py --idle-timeout 1800   # stop after 30 idle minutes
sidepage serve demo.py --timeout 3600        # stop after 1 hour no matter what
```

**Lazy start.** For `code`/`notebook` targets, the wrapped process isn't
launched at `serve` time — it launches on the *first* inbound request,
behind the same "starting…" holding page a slow boot already shows. A
`serve` call that nobody ever hits never spends the CPU/memory to boot
the wrapped app at all. (`static` targets are already in-process and
instant, so there's nothing to defer there.) This is automatic — no flag.

**Peers.** `--peer <role>=<app-name>` (repeatable) resolves another
*currently running* served app's URL and injects it as
`SIDEPAGE_PEER_<ROLE>_URL` in the wrapped process's environment — useful
for a frontend that needs to reach a backend whose tunnel URL doesn't
exist until it's actually served, and changes across `--anon` runs.
Resolution fails loud (nonzero exit, clear message) if the named peer
isn't running yet. The app can also re-resolve peers live, at any point,
via `GET /.sidepage/peers.json` — gated by the app's own `--auth` tier
like any other route — so a peer that restarts mid-session with a fresh
URL is never stale the way the boot-time env var would be. `code`/
`notebook` targets only; there's no subprocess to inject into for a
`static` target, so `--peer` on one is rejected up front.

```bash
sidepage serve backend.py --name backend
sidepage serve frontend.py --peer api=backend   # $SIDEPAGE_PEER_API_URL in frontend's env
```
