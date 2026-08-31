---
name: sidepage-serve
description: Spin up, register, inspect, and shut down local websites and apps (static sites, scripts, Streamlit, FastAPI, Python MCP servers, Jupyter notebooks) using the `sidepage` CLI, and get back a shareable URL. Also proxies an already-running local service (npm run dev, a container, anything on a port) through the same auth/tunnel stack via `sidepage proxy`. Use this whenever the user asks to "host", "serve", "preview", "spin up", "share a link to", "demo", "deploy locally", or "tunnel" a site, app, or already-running dev server, or asks to stop/tear down/take down one that's running, check what's running (`ls`/`status`/`usage`), save a reusable launch config (`app register`), or manage sidepage secrets/domains. Trigger even if they just paste a file path/port and say "can people see this" or "give me a link" — that's a serve/proxy request. Assumes `sidepage` is already installed and on PATH via a native Python 3.12 (not necessarily behind `uv run`).
---

# sidepage-serve

Wraps the `sidepage` CLI (local-first hosting + tunneling for scripts,
static sites, notebooks, and already-running dev servers) so Claude can
bring a site up, hand back a working URL, and tear it down again —
including when running as a dispatched/background task with no
interactive terminal.

## If `sidepage` isn't on PATH

It's a real PyPI package now — `pip install sidepage` (needs Python
3.12+), then `sidepage setup` once to install `cloudflared` (only needed
for `--anon`/`--domain` tunneling; skip it if the user only wants a local
`127.0.0.1` link). Don't try `uv run sidepage` as a substitute — this
skill assumes a native install on `PATH`, and the scripts below invoke
`sidepage` directly, not through `uv run`. `sidepage` itself still shells
out to `uv` to run whatever `serve` points at (the wrapped app's own
dependencies), so `uv` needs to be on `PATH` too, separately from
installing sidepage itself.

## The one thing that will bite you: `serve` and `proxy` both block

`sidepage serve <target>` and `sidepage proxy --port <n>` both run in the
foreground and **only** exit on Ctrl+C or a `sidepage stop <app-name>`
from another terminal — there is no daemon/background mode built into the
CLI, for either command. If you run either directly in a dispatched task,
the command never returns and the task hangs forever.

**Always launch through `scripts/start_site.sh`**, which backgrounds the
process correctly (`nohup` + `disown`) and polls until it can tell you
whether the app actually came up, rather than guessing. It has three
modes, because `sidepage serve` takes exactly one positional argument
that means different things depending on whether you're serving a fresh
target or a name already saved in the registry, and `sidepage proxy`
takes no positional target at all (just `--port`):

```bash
# Fresh target, not yet registered — script fills in --name for you:
scripts/start_site.sh new <app-name> <target> [serve flags...]

# Already-registered app (see "the app registry" below):
scripts/start_site.sh registered <app-name> [override flags...]

# An already-running local service, wrapped instead of launched:
scripts/start_site.sh proxy <app-name> --port <n> [proxy flags...]
```

Examples:

```bash
scripts/start_site.sh new demo app.py --auth token --anon
scripts/start_site.sh registered abc-app --scope web
scripts/start_site.sh proxy my-vite-app --port 5173 --anon
```

This prints one line of JSON, e.g.:

```json
{"status":"running","app":"demo","pid":12345,"log":"/home/user/.local/state/sidepage/skill-logs/demo.log","url":"https://random-words.trycloudflare.com"}
```

- `status: "running"` — hand the `url` straight to the user.
- `status: "failed"` — read `log` (the script already tails the last 20
  lines into `error`) and diagnose before retrying. Common causes: target
  path wrong, `--anon`/`--domain` both passed, a `--env` secret that isn't
  in the vault, port conflict from a previous run that wasn't stopped, or
  (for `proxy`) nothing actually listening on `--port` yet.
- `status: "starting"` — no URL yet after 30s. Not necessarily broken (first
  run resolves dependencies via `uv run`, which can be slow) — check
  `sidepage status <app-name>` or tail the log yourself before deciding
  what to do.

To stop it:

```bash
scripts/stop_site.sh <app-name>
```

This runs `sidepage stop`, then confirms with `sidepage status` rather than
assuming the teardown worked, and returns JSON. Works the same for a
`serve`d app and a `proxy`d one — but see the teardown-asymmetry warning
in the proxy section below before assuming it stops everything. Always
stop apps you started once the user is done with them or the task is
finished — a forgotten background `serve`/`proxy` keeps a tunnel and port
open indefinitely.

## Deciding what to run

`sidepage serve <target> [flags]` wraps almost anything sidepage itself
launches:

| Target | `--type` | Notes |
|---|---|---|
| directory of static files | `static` (usually auto) | |
| `.py` script, plain | `code` (auto) | generic `$PORT`-reading launch |
| `.py` script, Streamlit app | `code` (auto-detected) | launched via `streamlit run` |
| `.py` script, FastAPI app | `code` (auto-detected) | `uvicorn`; `/docs` works automatically |
| `.py` script, MCP server (official `mcp` SDK or `fastmcp`) | `code` (auto-detected) | served over real Streamable HTTP at `/mcp`, regardless of what the script's own `__main__`/`mcp.run()` would normally do |
| `.ipynb` notebook | `notebook` | full editable Jupyter Lab with a live kernel |

`--type` almost never needs to be set explicitly — trust auto-detection
unless the user is fighting a misdetection.

If the user already has something running — `npm run dev`, a container,
anything listening on a local port that *they* started, not sidepage —
don't try to feed it to `serve`. Use `sidepage proxy --port <n>` instead
(see below); it wraps the same reverse proxy/auth/tunnel stack around a
port sidepage never launched.

Key flags to reason about before launching `serve`:

- **`--name <app-name>`** — pick something short and stable; you'll need it
  for `status`/`stop`/`usage`. Default to a slug of the target if the user
  doesn't specify one.
- **`--auth open|token`** — `open` is unauthenticated. `token` gates the app
  behind a header/query param/cookie set by a gate page — use this whenever
  the content is anything other than a throwaway public demo. (`network`
  and `oauth` parse but aren't implemented — if asked for these, say so
  rather than passing them through silently.)
- **`--anon` vs `--domain <domain>`** — mutually exclusive. `--anon` gives a
  free, no-account `*.trycloudflare.com` URL immediately. `--domain`
  requires the domain to already be provisioned (see below) and gives a URL
  under the user's own domain. **Passing neither** means the app only
  listens on `127.0.0.1` — fine for "just let me look at it myself," wrong
  if the user wants to share a link.
- **`--env <SECRET_NAME>`** — repeatable; injects a named secret from the
  vault into the served process's environment. Fails loud if the name
  isn't in the vault yet — check with `sidepage secrets list` and prompt
  the user to `sidepage secrets set <NAME>` first if it's missing (that
  command is interactive/reads from stdin, so ask the user to run it
  themselves rather than trying to script a secret value into it).
- **`--token <value>`** — sets a specific auth token instead of a freshly
  generated one. Fine to pass directly to `serve`/`proxy`. **Never write a
  literal token into an `app register` string** (see below) — that's a
  hard rule in sidepage, not a style preference.
- **`--timeout <seconds>` / `--idle-timeout <seconds>`** — auto-teardown.
  `--timeout` stops the app once its total lifetime hits the limit,
  no matter what; `--idle-timeout` stops it once that many seconds pass
  with no proxied HTTP request or WebSocket message (resets on every one).
  Both compose and tear down exactly like `sidepage stop` would —
  immediate, no drain window. Useful when a user wants a demo link that
  self-destructs instead of needing a manual `stop`.
- **`--peer <role>=<app-name>`** — repeatable, `code`/`notebook` targets
  only. Resolves another *currently running* served app's URL and injects
  it as `SIDEPAGE_PEER_<ROLE>_URL` in this process's environment — for a
  frontend that needs to reach a backend whose tunnel URL doesn't exist
  until it's actually served. Fails loud if the named peer isn't running
  yet. The app can also re-resolve peers live via `GET
  /.sidepage/peers.json`, so a peer that restarts mid-session is never
  stale. Serve the peer first, then the app that references it.
- **`--pwa`** — makes the app installable to a phone home screen (manifest
  + service worker + HTML injection, all at the proxy layer — the app on
  disk is never touched). Reach for this whenever the user wants to "add
  it to my home screen," "make it an app," or install a demo on their
  phone — not just for a plain sharable link, that's `--anon`/`--domain`
  alone. Common flags: `--pwa-name`/`--pwa-short-name` (default: the
  resolved app name), `--pwa-icon <path>` (square PNG, ≥512px — validated,
  fails loud with the actual dimensions if it isn't), `--pwa-theme`/
  `--pwa-bg` (hex colors). Mention up front that an `--anon` install
  breaks the moment that session ends (sidepage says so in its own
  output) — use `--domain` if the user wants the icon to survive restarts.
- **`--qr`** — prints a terminal QR code for the resulting URL. Independent
  of `--pwa`; pass it any time the user is going to want to scan a link
  onto a phone rather than type it. Only useful when run directly in an
  interactive terminal you can show the user — it degrades to a warning
  (no crash) if stdout isn't a real tty, e.g. inside `start_site.sh`'s
  backgrounded/redirected-to-a-logfile invocation, so don't rely on it
  there — hand back the plain `url` from the JSON instead.

```bash
scripts/start_site.sh new demo app.py --anon --pwa --pwa-name "Demo"
```

## Proxying an already-running service

`sidepage proxy --port <n>` wraps a service the user already has running
— `npm run dev`, a container, anything already listening on a local port
— with the same reverse proxy/auth/tunnel stack `serve` uses. The
structural difference: **sidepage never launches or owns the process**.
It only listens on the port and forwards traffic.

```bash
scripts/start_site.sh proxy <app-name> --port <n> [--domain <domain> | --anon]
                                        [--auth open|token] [--token <value>]
                                        [--timeout <seconds>] [--idle-timeout <seconds>]
```

(equivalent direct call, only safe outside a dispatched/background task:
`sidepage proxy --port <n> --name <app-name> [flags...]`)

- **`--port`** is the only required flag — assumed listening on
  `127.0.0.1`, with an automatic fallback to `[::1]` (IPv6 loopback) if
  that doesn't answer (some dev servers, e.g. a bare `npm run dev` Vite
  server, bind IPv6-only by default).
- **`--name`** defaults to `proxy-<port>` for local-only use, but is
  **required** — and rejected loud — once `--domain`/`--anon` is set,
  since it becomes part of the public hostname.
- **`--type`, `--env`, `--guardrail`, `--peer` are all rejected outright**
  with a specific error, not silently ignored — they're subprocess-launch
  concepts and `proxy` doesn't own a subprocess. Don't pass them.

**Teardown asymmetry — the one behavior genuinely different from
`serve`:** Ctrl+C / `sidepage stop <app-name>` / `scripts/stop_site.sh`
tear down the proxy, the tunnel, and the registry entry **only**. The
service on `--port` was never sidepage's to stop, and it keeps running
after teardown. Tell the user this explicitly if they ask you to "shut it
all down" — stopping the sidepage app does not stop their dev server.

**Read before pointing this at anything public** — worth surfacing to the
user proactively, not just on request:

- Every proxied request reaches the wrapped app from `127.0.0.1`
  (sidepage's own address). Any app-level logic that trusts "this came
  from localhost" instead of checking `X-Forwarded-For` — debug endpoints,
  admin panels, and pointedly **Flask/Werkzeug's interactive debugger, a
  known RCE if reachable** — is silently defeated, `--auth` or not. Tell
  the user to disable local-only debug/admin surfaces before proxying
  publicly.
- Real `Host`/`X-Forwarded-Host`/`X-Forwarded-Proto`/`X-Forwarded-For` are
  forwarded on HTTP requests, but only help an app configured to trust
  them (Django needs `USE_X_FORWARDED_HOST` + `SECURE_PROXY_SSL_HEADER` +
  `ALLOWED_HOSTS`; Flask needs `werkzeug.middleware.proxy_fix.ProxyFix`;
  FastAPI/Starlette needs `ProxyHeadersMiddleware`; Express needs
  `app.set('trust proxy', true)`; Rails is usually fine by default). On
  WebSocket connections only `X-Forwarded-Host` is forwarded, not a
  literal `Host` override — some WS servers (Jupyter/Tornado confirmed)
  reject a forwarded hostname on the handshake outright.
- **Vite dev servers specifically**: add the tunnel hostname to
  `server.allowedHosts` in `vite.config.js` — forwarding headers does
  **not** fix this, Vite checks the raw `Host` value. With `--anon` the
  hostname changes every run, so use a wildcard entry (`.trycloudflare.com`)
  rather than an exact match. Known gap: even with that fix, Vite
  HMR/live-reload over `--anon` doesn't reliably work (initial page load
  and `--domain` are both fine) — say so if the user hits it rather than
  trying to debug further; it's a known, investigated, unresolved limit of
  Cloudflare's Quick Tunnel edge against a browser's native WebSocket, not
  a sidepage config issue.
- OAuth/SSO login flows are effectively incompatible with `--anon` — the
  hostname changes every run and providers require an exact, pre-registered
  redirect URI. Use `--domain` for anything doing OAuth.
- HTTP/1.1 and WebSocket only — a port serving raw TCP or gRPC won't work.

```bash
# User already has: npm run dev -- --host 127.0.0.1 --port 5173
scripts/start_site.sh proxy my-vite-app --port 5173               # local only
scripts/start_site.sh proxy my-vite-app --port 5173 --domain example.com
scripts/start_site.sh proxy my-vite-app --port 5173 --anon
```

`sidepage ls`/`status` list a proxied app the same as a served one (just
tagged `external` instead of a target kind like `code`/`static`), so
checking on it or reporting usage works identically — see "Checking on
things" below.

## Reusable configs: the app registry

`sidepage app register` only covers `serve` invocations, not `proxy` —
there's no persistent target to detect/store for something sidepage never
launches. If the user wants to re-run the same `proxy` call repeatedly,
just re-issue the same `scripts/start_site.sh proxy ...` command; there's
no registry shortcut for it.

If the user wants to serve the same thing repeatedly (a recurring demo, a
personal dashboard), register it once instead of re-typing flags:

```bash
sidepage app register "<target> [serve flags...]" <app-name>
sidepage app list
sidepage app show <app-name>
sidepage app unregister <app-name>
```

`register`/`list`/`show`/`unregister` all return immediately — run them
directly, no backgrounding needed. To actually run a registered app, use
`scripts/start_site.sh registered <app-name> [override flags...]` (same
backgrounding/polling reasoning as any other `serve` call — see above).

Notes that matter when using this:

- The registration string is parsed the same way `serve` parses its own
  flags, and the *parsed* result is stored — not your literal string. So
  `sidepage app show <app-name>` reflects resolved fields, and it's safe to
  assume any flag `serve` accepts is also registerable.
- Flags passed at `serve` time **override** the registered ones for that
  run only; the stored registration is never mutated by a one-off override.
  To preview what a run will actually use before firing it, use `sidepage
  app show <app-name> --with "<flags>"` — e.g. `sidepage app show
  abc-app --with "--scope web"` — which prints the effective merged config
  without actually running it. `--timeout`/`--idle-timeout`/`--peer`/
  `--token` are never part of a registration (nothing to merge against);
  they always come from whatever is passed at `serve`/`start_site.sh` time.
- `sidepage app register` will **reject** a registration string containing
  a literal `--token <value>` — auth tokens are process-scoped and meant to
  regenerate per-serve, so this isn't a bug to work around, it's the
  registry refusing to persist a secret. If the user wants a stable token
  across runs, that's what `--env` + the vault are for, not `app register`.
- `sidepage app list` / `sidepage app show <app-name>` / `sidepage app
  unregister <app-name>` round out the registry — use `list` when the user
  asks "what do I have set up" rather than guessing from memory.

## Checking on things

- `sidepage ls` — what's running on this machine right now (`serve`d and
  `proxy`d apps both).
- `sidepage status <app-name>` — is this specific app up.
- `sidepage usage <app-name>` — request/connection counts.
- `sidepage inspect [<app-name>]` — interactive HTTP console against a
  running app; useful if the user wants to poke at endpoints rather than
  just get a URL. (MCP tool browsing inside `inspect` isn't implemented
  yet — say so if asked.)

Run these directly (they return immediately, no backgrounding needed) —
only `serve`/`proxy` themselves need `start_site.sh`.

## Bring-your-own-domain (only if the user asks for their own domain)

One-time setup, not something to do speculatively:

```bash
sidepage secrets set cf-api-token          # user runs this interactively
sidepage account domain set example.com --api-token-name cf-api-token
```

This provisions one Cloudflare Tunnel for the whole domain and stores its
run-token in the vault automatically (`cf-tunnel-token::<domain>`). After
that, `--domain example.com` on any `serve` or `proxy` call routes through
it — every app on the same domain shares the one tunnel, so there's no
per-app provisioning after the first.

## Things that aren't implemented — say so, don't fake it

`sidepage` is explicit about unfinished features rather than silently
no-op'ing, and this skill should be too. If the user asks for any of these,
tell them plainly it's not there yet rather than attempting a workaround:
brokered (default, account-based) tunneling, `login`/`account status`, the
discovery directory beyond the local machine, `--guardrail` (unbuilt for
`serve`, and rejected outright on `proxy`), `--auth network`/`oauth`, MCP
tool browsing in `inspect`, an OS-keychain backend for secrets (it's
encrypted-file only today), and `proxy` auto-detecting Vite's
`allowedHosts` rejection and printing an inline hint (it's documented in
`--help`/this skill instead, not detected live). `sidepage promote
<app-name>` also exists but isn't meaningful yet — only `local` scope
exists currently.

## End-to-end examples

User: "spin up the streamlit app in ./dash for me to share with the team,
gate it behind a token"

```bash
scripts/start_site.sh new dash ./dash/app.py --auth token --anon
```
→ report the `url` and mention the app is gated behind a token (the gate
page/cookie flow handles the token itself — no need to separately explain
header/query mechanics unless asked).

User: "I'll want to demo this same dashboard every week — save it"

```bash
sidepage app register "./dash/app.py --auth token --anon" dash-weekly
```
Next time: `scripts/start_site.sh registered dash-weekly` — no need to
retype the target or flags, and `sidepage app show dash-weekly` first if
you want to confirm what will actually run before firing it.

User: "ok take it down"

```bash
scripts/stop_site.sh dash
```
→ confirm it's stopped.

User: "I've got `npm run dev` running on 5173, can people see this?"

```bash
scripts/start_site.sh proxy vite-demo --port 5173 --anon
```
→ hand back the `url`, and mention up front that the tunnel hostname
changes every run, that Vite's dev overlay needs `server.allowedHosts` set
to see traffic from it (wildcard `.trycloudflare.com` for `--anon`), and
that HMR over `--anon` is a known unreliable case — the initial page load
will work regardless. Also mention that stopping this later
(`scripts/stop_site.sh vite-demo`) won't stop their `npm run dev` process.
