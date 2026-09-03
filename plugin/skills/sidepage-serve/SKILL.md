---
name: sidepage-serve
description: Spin up, register, inspect, and shut down local websites and apps (static sites, scripts, Streamlit, FastAPI, Python MCP servers, Gradio apps, Jupyter notebooks) using the `sidepage` CLI, and get back a shareable URL. Also proxies an already-running local service (npm run dev, a container, anything on a port) through the same auth/tunnel stack via `sidepage proxy`. Use this whenever the user asks to "host", "serve", "preview", "spin up", "share a link to", "demo", "deploy locally", or "tunnel" a site, app, or already-running dev server, or asks to stop/tear down/take down one that's running, check what's running (`ls`/`status`/`usage`), save a reusable launch config (`app register`), or manage sidepage secrets/domains. Trigger even if they just paste a file path/port and say "can people see this" or "give me a link" — that's a serve/proxy request. Assumes `sidepage` is already installed and on PATH via a native Python 3.12 (not necessarily behind `uv run`).
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
skill assumes a native install on `PATH` and invokes `sidepage` directly. `sidepage` itself still shells
out to `uv` to run whatever `serve` points at (the wrapped app's own
dependencies), so `uv` needs to be on `PATH` too, separately from
installing sidepage itself.

## Always pass `--detach --json`

`sidepage serve <target>` and `sidepage proxy --port <n>` block by
default: they run in the foreground and exit only on Ctrl+C or a
`sidepage stop <app-name>` from elsewhere. Run either one bare and the
command never returns — in a dispatched task, that hangs forever.

**`--detach` fixes this, and `--json` makes the result parseable.** Use
both, every time:

```bash
sidepage serve <target> --name <app-name> --detach --json [flags...]
sidepage serve <app-name> --detach --json [override flags...]   # registered app
sidepage proxy --port <n> --name <app-name> --detach --json [flags...]
```

Examples:

```bash
sidepage serve app.py --name demo --auth token --anon --detach --json
sidepage serve abc-app --scope web --detach --json
sidepage proxy --port 5173 --name my-vite-app --anon --detach --json
```

`--detach` returns only once the app is genuinely serving or has
definitively failed — it is asynchronous in lifetime, synchronous in
readiness — and prints one line of JSON to stdout:

```json
{"status":"running","app":"demo","pid":12345,"url":"https://random-words.trycloudflare.com","local_url":"http://127.0.0.1:8501","tunnel_url":"https://random-words.trycloudflare.com","log":"~/.local/state/sidepage/logs/demo.log"}
```

- `status: "running"` (exit 0) — hand `url` straight to the user. It is
  the tunnel URL when there is one, else the local URL.
- `status: "failed"` (exit 1) — `error` carries the child's actual error
  message, and `log` is the full output. Common causes: target path
  wrong, `--anon`/`--domain` both passed, a `--env` secret that isn't in
  the vault, a name already running, or (for `proxy`) nothing listening
  on `--port` yet.
- `status: "starting"` (exit 0) — no registry entry after 180s. Rare, and
  not necessarily broken: a first run resolves dependencies through `uv`,
  which can be slow on a cold cache. Check `sidepage status <app-name>`
  or read the log before deciding whether to stop it.

With `--json`, stdout carries *only* that line — every human-readable
message moves to stderr — so it can be piped into a parser directly.
`--json` also works without `--detach`, printing the same line the moment
the app is up and then continuing to block; use that only if you intend
to own the process yourself.

To stop it:

```bash
sidepage stop <app-name>
```

Works the same for a `serve`d app and a `proxy`d one — but see the
teardown-asymmetry warning in the proxy section below before assuming it
stops everything. Always stop apps you started once the user is done with
them or the task is finished: a forgotten background `serve`/`proxy`
keeps a tunnel and a port open indefinitely.

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
| `.py` script, Gradio app | `code` (auto-detected) | mounted and served by sidepage; the script's own `demo.launch()` is neutralized, so a hardcoded `server_port=`/`share=True` can't fight sidepage's port or tunnel |
| Hugging Face Space | fetched by `sidepage pull` first | see **Remote apps** below — needs an explicit human confirmation before it will run |
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
- **`--no-suffix`** — `--domain` only (rejected otherwise). Serves at a bare
  `<app-name>.<domain>` instead of the default
  `<app-name>-<id>.<domain>`. Pass it when the user asks for a specific,
  clean hostname on their own domain (`docs.example.com`); don't add it
  speculatively. A name already pointed somewhere else in that zone is
  rejected loud (`an app with this name already exists`) rather than
  overwritten — relay the error's options (different `--name`, drop
  `--no-suffix`, or delete the stale DNS record) instead of retrying.
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
  onto a phone rather than type it. Combines fine with `--detach --json`:
  the code is rendered by the foreground command you ran, not by the
  backgrounded app, and under `--json` it goes to stderr so the payload on
  stdout stays parseable. It needs a real terminal to draw into, so it
  degrades to a warning (no crash) when output is piped or redirected —
  in that case hand back the plain `url` from the JSON instead.

```bash
sidepage serve app.py --name demo --anon --pwa --pwa-name "Demo" --detach --json
```

## Proxying an already-running service

`sidepage proxy --port <n>` wraps a service the user already has running
— `npm run dev`, a container, anything already listening on a local port
— with the same reverse proxy/auth/tunnel stack `serve` uses. The
structural difference: **sidepage never launches or owns the process**.
It only listens on the port and forwards traffic.

```bash
sidepage proxy --port <n> --name <app-name> --detach --json
                                        [--domain <domain> | --anon]
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
`serve`:** Ctrl+C / `sidepage stop <app-name>`
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
sidepage proxy --port 5173 --name my-vite-app --detach --json               # local only
sidepage proxy --port 5173 --name my-vite-app --domain example.com --detach --json
sidepage proxy --port 5173 --name my-vite-app --anon --detach --json
```

`sidepage ls`/`status` list a proxied app the same as a served one (just
tagged `external` instead of a target kind like `code`/`static`), so
checking on it or reporting usage works identically — see "Checking on
things" below.

## Reusable configs: the app registry

`sidepage app register` only covers `serve` invocations, not `proxy` —
there's no persistent target to detect/store for something sidepage never
launches. If the user wants to re-run the same `proxy` call repeatedly,
just re-issue the same `sidepage proxy ...` command; there's
no registry shortcut for it.

If the user wants to serve the same thing repeatedly (a recurring demo, a
personal dashboard), register it once instead of re-typing flags:

```bash
sidepage app register "<target> [serve flags...]" <app-name>
sidepage app list
sidepage app show <app-name>
sidepage app unregister <app-name>
```

Or pass **`--autoregister`** on the `serve` call itself — the same entry is
saved once the app is actually up, without composing a separate
registration string. Prefer this when you're already starting the app and
the user asks to keep it: it can't drift from what actually ran.

```bash
sidepage serve ./dashboard.py --name dash --auth token --autoregister --detach --json
```

Re-running `--autoregister` for an app that's already registered with the
identical config is safe — it writes nothing and warns that `sidepage
serve <app-name>` is enough next time. A *different* config under the same
name is refused before the app starts, so treat that error as a real
question for the user (`sidepage app show <app-name>` to compare), not
something to retry around.

`register`/`list`/`show`/`unregister` all return immediately — run them
directly, no backgrounding needed. To actually run a registered app, use
`sidepage serve <app-name> --detach --json [override flags...]` (same
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
  `--qr`/`--token` are never part of a registration (nothing to merge
  against); they always come from whatever is passed at
  `serve` time. `--pwa`/`--pwa-*` **are** stored, and merge
  as one unit: passing any `--pwa*` flag at `serve <app-name>` time
  replaces the saved PWA config wholesale rather than field by field.
- `sidepage app register` will **reject** a registration string containing
  a literal `--token <value>` — auth tokens are process-scoped and meant to
  regenerate per-serve, so this isn't a bug to work around, it's the
  registry refusing to persist a secret. If the user wants a stable token
  across runs, that's what `--env` + the vault are for, not `app register`.
  `--autoregister` is the one place a `--token` doesn't abort anything: it
  saves the rest of the invocation and prints what it dropped. Don't read
  that warning as a failure — the app is serving and the entry is saved.
- `sidepage app list` / `sidepage app show <app-name>` / `sidepage app
  unregister <app-name>` round out the registry — use `list` when the user
  asks "what do I have set up" rather than guessing from memory.

## Remote apps: `sidepage pull`

To host someone else's Hugging Face Space, fetch it first — this
downloads and registers it but **runs nothing**:

```bash
sidepage pull huggingface.co/spaces/<owner>/<name>     # or hf:<owner>/<name>
sidepage pull hf:<owner>/<name> --dry-run              # plan + download size, fetches nothing
sidepage pull hf:<owner>/<name> --json                 # one parseable line
```

Use `--dry-run` first when the user hasn't seen the Space before: it
reports the total download size without paying for it, which matters
because Spaces routinely carry multi-gigabyte model weights.

**You cannot serve a pulled app on the user's behalf.** `serve` prints
what it's about to run and asks for confirmation, and in a non-interactive
context — which is what you are — it refuses outright and exits 1. That is
deliberate: it's the difference between a convenience and a remote-code
execution path. When the user asks you to run a pulled app:

1. Run `sidepage pull ...` and show them the plan.
2. Tell them the exact command to run themselves, including any `--env`
   names the Space requested:
   `sidepage serve <app-name> --env SOME_KEY`
3. Do **not** pass `--trust-remote-code` to work around the prompt. Only
   the user can make that call, and only after reading the code. If they
   explicitly instruct you to, quote what the app is and where it came
   from first.

Requested env names are shown as `(requested — not granted)`. Nothing is
bound until the user passes `--env`, and sidepage will never auto-grant a
vault secret a downloaded manifest asks for.

`pull` refuses Docker Spaces, GPU/ZeroGPU hardware tiers, and
private/gated repos before downloading anything — those errors are final,
not something to retry differently.

To remove a pulled app and its downloaded source:

```bash
sidepage app delete <app-name> --yes
```

`app unregister` only forgets the config and leaves the files;
`app delete` removes both. Neither will ever delete files for an app that
was registered against a path the user already had.

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
only `serve`/`proxy` themselves need `--detach`.

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
per-app provisioning after the first. Apps land at
`<app-name>-<id>.<domain>`; add `--no-suffix` for a bare
`<app-name>.<domain>` when the user wants a specific hostname.

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
sidepage serve ./dash/app.py --name dash --auth token --anon --detach --json
```
→ report the `url` and mention the app is gated behind a token (the gate
page/cookie flow handles the token itself — no need to separately explain
header/query mechanics unless asked).

User: "I'll want to demo this same dashboard every week — save it"

```bash
sidepage app register "./dash/app.py --auth token --anon" dash-weekly
```
Next time: `sidepage serve dash-weekly --detach --json` — no need to
retype the target or flags, and `sidepage app show dash-weekly` first if
you want to confirm what will actually run before firing it.

User: "ok take it down"

```bash
sidepage stop dash
```
→ confirm it's stopped.

User: "I've got `npm run dev` running on 5173, can people see this?"

```bash
sidepage proxy --port 5173 --name vite-demo --anon --detach --json
```
→ hand back the `url`, and mention up front that the tunnel hostname
changes every run, that Vite's dev overlay needs `server.allowedHosts` set
to see traffic from it (wildcard `.trycloudflare.com` for `--anon`), and
that HMR over `--anon` is a known unreliable case — the initial page load
will work regardless. Also mention that stopping this later
(`sidepage stop vite-demo`) won't stop their `npm run dev` process.
