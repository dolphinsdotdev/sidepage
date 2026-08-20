# sidepage

Local-first hosting and tunneling for code, static sites, and notebooks.
`sidepage serve` wraps almost anything — a script, a static site, a
Streamlit or FastAPI app, a Python MCP server, a Jupyter notebook —
behind a local reverse proxy and hands you a URL. `sidepage proxy` does
the same for a service you already have running (`npm run dev`, a
container, anything already listening on a port) instead of one sidepage
launches itself. `sidepage new` scaffolds a static site to get started.

**Status:** `serve`, `proxy`, `secrets`, `inspect`, and bring-your-own-domain
tunneling are real and tested end to end. Features that need a Sidepage
cloud backend that doesn't exist yet (brokered tunneling, account login,
the directory beyond this machine) print a clear "not implemented"
message instead of failing silently or being left out of the CLI. See
[Project status](#project-status) for the full breakdown.

## Install

Requires [uv](https://docs.astral.sh/uv/) (it fetches Python 3.12+ for you
if needed).

```bash
uv sync
uv run sidepage --help
```

Or activate the venv and call it directly: `source .venv/bin/activate &&
sidepage --help`.

## Quickstart

```bash
# Serve a static site
uv run sidepage serve tests/fixtures/static-site --name demo

# Serve a Streamlit app, gated behind a token
uv run sidepage serve tests/fixtures/streamlit-app/app.py --name demo --auth token

# Serve a FastAPI app — /docs (Swagger UI) works automatically
uv run sidepage serve tests/fixtures/fastapi-app/app.py --name demo

# Serve a Python MCP server over real Streamable HTTP — even if its own
# __main__ only ever calls mcp.run() (stdio), sidepage never runs that
# entrypoint, so it's reachable at /mcp regardless
uv run sidepage serve tests/fixtures/mcp-app/app.py --name demo

# Serve a Jupyter notebook — a full, editable Lab instance with a live
# kernel, reachable through the proxy like anything else
uv run sidepage serve tests/fixtures/notebook-app/notebook.ipynb --name demo

# Inject a secret and expose it over a real public tunnel
uv run sidepage secrets set MY_KEY
uv run sidepage serve some_app.py --env MY_KEY --anon

# Auto-stop after 30 minutes of no traffic, and inject another running
# app's URL as SIDEPAGE_PEER_API_URL
uv run sidepage serve frontend.py --idle-timeout 1800 --peer api=backend

# Proxy a service you already have running instead of one sidepage
# launches — npm run dev, a container, anything on a port
uv run sidepage proxy --port 5173 --name my-vite-app
```

Every `serve`/`proxy` call blocks the terminal until Ctrl+C (or `sidepage
stop <app-name>` from another terminal) — no background/daemon mode.
`serve` tears down the process it launched too; `proxy` never launched
anything, so Ctrl+C/`stop` only tears down the proxy and tunnel — the
service you pointed it at keeps running (see [Proxying an already-running
service](#proxying-an-already-running-service)).

## How it works

Two things sit between "just run a script" and what `serve` does:

- **A local reverse proxy** runs on your machine in front of the app's
  real port. It enforces `--auth`, counts usage, shows a holding page
  while the app boots, and proxies HTTP + WebSockets — the wrapped app
  itself needs zero Sidepage-specific code.
- **A tunnel**, chosen per call: `--anon` for a free, no-account
  `*.trycloudflare.com` URL, or `--domain <domain>` for your own
  Cloudflare domain (see [Bring your own domain](#bring-your-own-domain)
  below). Without either flag, `serve` just listens on `127.0.0.1`.

## Commands

| Command | What it does |
|---|---|
| `sidepage serve <target>` | Wrap and host a static dir, script, or app — see flags below. |
| `sidepage proxy --port <n>` | Wrap an already-running local service instead of one `serve` launches — see below. |
| `sidepage stop <app-name>` | Tear down a running app (`serve` or `proxy`). |
| `sidepage ls` / `sidepage status <app-name>` | List / check apps running on this machine. |
| `sidepage usage <app-name>` | Request and connection counts for an app. |
| `sidepage inspect [<app-name>]` | Interactive HTTP console against a running app. |
| `sidepage secrets set\|list\|remove` | Encrypted local vault for standing credentials. |
| `sidepage account domain set` | Provision a BYO Cloudflare domain — see below. |
| `sidepage new <name>` | Scaffold a static site. |
| `sidepage app register "<invocation>" <name>` | Save a `serve` invocation under a short name. |
| `sidepage app list` / `show <name>` / `unregister <name>` | Manage saved apps — see below. |
| `sidepage promote <app-name>` | Widen an app's discovery scope. Not yet meaningful — only `local` scope exists today. |
| `sidepage login` / `sidepage account status` | Not implemented — no Sidepage account backend to talk to yet. |

`serve`'s main flags:

```bash
sidepage serve <target> [--type auto|code|static|notebook] [--name <app-name>]
               [--auth open|token] [--anon | --domain <domain>]
               [--token <value>] [--env <SECRET_NAME>]...
               [--timeout <seconds>] [--idle-timeout <seconds>]
               [--peer <role>=<app-name>]...
```

- `--type` is usually inferred: `code` targets are auto-detected as
  Streamlit, FastAPI, or a Python MCP server (official `mcp` SDK or the
  third-party `fastmcp` package) and launched with their real launcher
  (`streamlit run`, `uvicorn <module>:<app>`, or `uvicorn --factory
  <module>:<mcp-var>.<app-method>`); anything else falls back to a
  generic `$PORT`-reading launch. `notebook` (`.ipynb`) targets get a
  full, editable Jupyter Lab instance with a live kernel.
  MCP servers are launched by bypassing their own entrypoint entirely
  (same trick as FastAPI) — a script whose `__main__` only calls
  `mcp.run()` (stdio, the default) still ends up served over real
  Streamable HTTP at `/mcp`, since that entrypoint is never executed.
- `--auth open|token` — `token` gates the app behind a header, query
  param, or browser cookie set by a gate page. (`network`/`oauth` parse
  but aren't built.)
- `--env <SECRET_NAME>` — repeatable; injects a named vault secret into
  the wrapped process's environment. Fails loud if the name isn't stored.
- `--anon` / `--domain` are mutually exclusive — see [How it
  works](#how-it-works).
- `--timeout <seconds>` / `--idle-timeout <seconds>` — auto-teardown; see
  [Timeouts, lazy start, and peers](#timeouts-lazy-start-and-peers) below.
- `--peer <role>=<app-name>` — repeatable; wire one served app to
  another's URL. Same section below.

Run `sidepage <command> --help` for the full flag list, including ones
that parse but aren't implemented yet (they report that clearly rather
than silently doing nothing).

## Proxying an already-running service

`sidepage proxy` wraps a service you already have running — `npm run
dev`, a container, anything already listening on a port — with the same
reverse proxy, auth, and tunnel stack `serve` uses, minus one thing:
sidepage never launches, owns, or manages the process's lifecycle.

```bash
sidepage proxy --port <n> [--name <app-name>] [--domain <domain> | --anon]
               [--auth open|token] [--token <value>]
               [--timeout <seconds>] [--idle-timeout <seconds>]
```

- `--port` is the only required flag — always dialed on `127.0.0.1`, with
  an automatic fallback to `[::1]` (IPv6 loopback) if that doesn't
  answer, since `proxy` can't control how the wrapped service was bound
  the way `serve` can for its own launchers.
- `--name` defaults to `proxy-<port>` for plain local use; it's required
  (and rejected loud if missing) once `--domain`/`--anon` is set, since it
  becomes part of the public hostname there.
- `--type`, `--env`, `--guardrail`, `--peer` aren't accepted at all — each
  gives a specific, actionable error instead of being silently ignored,
  since they're all about a subprocess `proxy` doesn't own.

**The one behavior that's genuinely different from `serve`:** Ctrl+C /
`sidepage stop <name>` tear down the proxy, the tunnel, and the registry
entry only. The service you pointed `--port` at was never sidepage's to
stop, and it doesn't.

**Read `sidepage proxy --help` before pointing this at anything public** —
it documents, loudly, three things worth knowing up front:
- Every proxied request reaches the wrapped app from `127.0.0.1`
  (sidepage's own address) — any app-level logic that trusts "this came
  from localhost" instead of checking `X-Forwarded-For` (debug endpoints,
  admin panels, and pointedly Flask/Werkzeug's interactive debugger — a
  known RCE if reachable) is silently defeated, `--auth` or not.
- The real `Host`/`X-Forwarded-Host`/`X-Forwarded-Proto`/`X-Forwarded-For`
  are forwarded on HTTP requests (WebSocket connections carry
  `X-Forwarded-Host` only, not a literal `Host` override — some WS
  servers, Jupyter/Tornado confirmed live, reject a forwarded real
  hostname on the handshake) — but that only helps an app that's
  configured to trust them. `--help` has a one-line fix per framework
  (Django, Flask, FastAPI/Starlette, Express, Rails, Vite).
- OAuth/SSO logins are effectively incompatible with `--anon`, since the
  hostname changes every run and providers require an exact,
  pre-registered redirect URI — use `--domain` for anything doing OAuth.

```bash
# Already running: npm run dev -- --host 127.0.0.1 --port 5173
sidepage proxy --port 5173                        # local only
sidepage proxy --port 5173 --domain example.com    # your own domain
sidepage proxy --port 5173 --anon                  # *.trycloudflare.com
```

One known gap: HMR/live-reload for a Vite dev server proxied through
`--anon` doesn't reliably work (initial page load and `--domain` are both
unaffected) — see [Project status](#project-status).

## Timeouts, lazy start, and peers

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

## Saved apps (the local registry)

Save a `serve` invocation under a short name and re-run it without
retyping flags:

```bash
sidepage app register "abc.py --auth token" abc-app
sidepage serve abc-app
```

Any flag passed at `serve` time overrides the registered one **for that
one run only** — the saved registration itself is never changed:

```bash
sidepage serve abc-app --scope web   # runs with --auth token (registered)
                                      # but --scope web for just this run
```

`sidepage app show abc-app` prints the saved config; add `--with "<flags>"`
to preview the effective merged config before actually running it, e.g.
`sidepage app show abc-app --with "--scope web"`.

A registered app's target is resolved once, at registration time — so
`--type` is stored as a concrete value (`code`, `static`, `notebook`),
never "auto." `sidepage app register` **refuses** a literal `--token
<value>`: auth tokens are per-process and regenerate on every `serve`
call, so storing one would defeat the point of them being ephemeral.
`--env <SECRET_NAME>` is fine to save — it's a reference to a vault entry,
never the secret value itself.

```bash
sidepage app list
sidepage app unregister abc-app
```

## Bring your own domain

Route apps through your own Cloudflare domain instead of
`*.trycloudflare.com`. One-time setup:

1. Create a Cloudflare API token (dashboard → My Profile → API Tokens)
   scoped to:
   - Account → Cloudflare Tunnel → Edit
   - Zone → DNS → Edit
   - Zone → Zone → Read
2. Store it in the vault, then provision the domain:
   ```bash
   sidepage secrets set cf-api-token
   sidepage account domain set example.com --api-token-name cf-api-token
   ```
   This creates one Cloudflare Tunnel for the whole domain and stores its
   run-token in the vault automatically — the CLI prints the vault name it
   landed under (`cf-tunnel-token::example.com`), since it was never typed
   by you.
3. Serve apps through it:
   ```bash
   sidepage serve app.py --domain example.com
   ```

Every app served under the same domain shares that one tunnel — no new
Cloudflare resources or tokens per app. The shared `cloudflared` process
starts with the first app on a domain and stops with the last.

## Project layout

```
src/sidepage/
├── cli.py           Root Typer app
├── commands/        Argument parsing & help text — one module per command group
├── core/            The SDK: serve/tunnel/proxy orchestration, secrets vault, running-app registry, saved-app registry
└── config/          Local config paths (XDG-style, overridable via SIDEPAGE_HOME)

tests/
├── fixtures/        Real apps used as test targets (static site, Streamlit, FastAPI, MCP, notebook, Flask, Vite)
└── test_*.py        Unit and integration tests

docs/
├── CHECKLIST.md       Build status for every command and core module
├── OPEN_QUESTIONS.md  Design decisions — resolved and still-open
└── SPEC_V5_DRAFT.md   v5 proposals — timeout/lazy-start/--peer (built, this doc) plus still-parked ideas
```

## Development

```bash
uv sync                 # install runtime + dev deps
uv run ruff check .     # lint
uv run pytest           # full suite (~4 min; mostly first-run dependency resolves)
```

Runtime dependencies are real, not stubs: Starlette, uvicorn, httpx, and
`websockets` back the reverse proxy; `cryptography` backs the secrets
vault. `cloudflared` and network access (for `uv run` to resolve wrapped
apps' dependencies) are expected to be available wherever tests run.
Node.js/npm are needed too, for the Vite fixture tests
(`tests/test_proxy_frameworks.py`) — `npm install` runs automatically
against `tests/fixtures/vite-app` the first time those tests run.

## Project status

**Real and tested end to end:** `serve` for static, code, and notebook
targets (Streamlit/FastAPI/Python-MCP auto-detected, generic `$PORT`
fallback, full Jupyter Lab for `.ipynb`), `proxy` for an already-running
service (own `--name` default, teardown that never touches the wrapped
service, Caddy-style forwarded headers, and an IPv6 loopback fallback —
the latter two shared with `serve` too), `open`/`token` auth, `--env`
secret injection, `--anon` tunneling, BYO-domain tunneling (`account
domain set` + `serve`/`proxy --domain`), `secrets`,
`stop`/`ls`/`status`/`usage`, `inspect` for HTTP/static targets, the
local app registry (`app register|list|show|unregister` + `serve
<app-name>`, with real one-off override merging), `--timeout`/
`--idle-timeout` auto-teardown, lazy start for code/notebook targets
(subprocess deferred to the first request), and `--peer
<role>=<app-name>` (boot-time env injection plus a live `GET
/.sidepage/peers.json`).

**Not implemented, and reports that clearly rather than silently
no-op'ing:** brokered (default) tunneling, `login`/`account status`, the
discovery directory beyond this machine, `--guardrail`, `--auth
network`/`oauth`, MCP tool browsing in `inspect`, `proxy` detecting
Vite's `allowedHosts` rejection and printing an inline hint (documented
in `--help` instead, see [Proxying an already-running
service](#proxying-an-already-running-service)), and the OS-keychain
backend for the secrets vault (encrypted-file only for now).

**Known limitation, investigated not fixed:** HMR/live-reload for a Vite
target proxied through `--anon` doesn't reliably work, even though the
initial page load and BYO-domain are both unaffected — ruled out
sidepage's own header forwarding/routing as the cause (the exact browser
handshake, reproduced with `curl`, succeeds through the real Cloudflare
edge + `cloudflared` + sidepage + Vite chain end to end); the gap is
somewhere in how a real browser's `WebSocket` negotiates against
Cloudflare's Quick Tunnel edge specifically, not isolated further.

See [`docs/CHECKLIST.md`](docs/CHECKLIST.md) for the full per-feature
breakdown, and [`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md) for
design rationale behind what's resolved and what's still open.
