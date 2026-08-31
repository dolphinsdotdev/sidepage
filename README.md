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

```bash
pip install sidepage
sidepage setup      # installs cloudflared — needed for --anon/--domain tunneling
sidepage --help
```

sidepage itself just needs Python 3.12+. It still shells out to
[uv](https://docs.astral.sh/uv/) to run whatever `serve` points at — the
wrapped app's own dependencies are always resolved through `uv run`,
regardless of how sidepage itself got installed — so make sure `uv` is on
`PATH` too. `sidepage setup` only installs `cloudflared` (the one
non-Python runtime dependency tunneling needs); it's explicit and
opt-in, never triggered silently from inside `serve`.

**From source**, for working on sidepage itself rather than just using it
— see [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md):

```bash
uv sync
uv run sidepage --help   # or: source .venv/bin/activate && sidepage --help
```

## Quickstart

![sidepage serve, from command to public URL](docs/media/serve-demo.gif)

```bash
# Serve a static site
sidepage serve ./my-site --name demo

# Serve a Streamlit app, gated behind a token
sidepage serve app.py --name demo --auth token

# Serve a FastAPI app — /docs (Swagger UI) works automatically
sidepage serve app.py --name demo

# Serve a Python MCP server over real Streamable HTTP — even if its own
# __main__ only ever calls mcp.run() (stdio), sidepage never runs that
# entrypoint, so it's reachable at /mcp regardless
sidepage serve app.py --name demo

# Serve a Jupyter notebook — a full, editable Lab instance with a live
# kernel, reachable through the proxy like anything else
sidepage serve notebook.ipynb --name demo

# Inject a secret and expose it over a real public tunnel
sidepage secrets set MY_KEY
sidepage serve app.py --env MY_KEY --anon

# Auto-stop after 30 minutes of no traffic, and inject another running
# app's URL as SIDEPAGE_PEER_API_URL
sidepage serve frontend.py --idle-timeout 1800 --peer api=backend

# Proxy a service you already have running instead of one sidepage
# launches — npm run dev, a container, anything on a port
sidepage proxy --port 5173 --name my-vite-app

# Make an app installable to a phone home screen, with a terminal QR
# code to scan and install it — see PWA install and QR codes below
sidepage serve app.py --anon --pwa --qr
```

Installed from source instead? Prefix every command with `uv run` (`uv
run sidepage serve...`) or activate the venv first — see
[Install](#install); the `uv run` prefix is only needed for that
source-checkout path.

`--type` is auto-detected from what you point at (`code`, `static`,
`notebook`; within `code`, Streamlit/FastAPI/MCP are each recognized by
import) — the four `sidepage serve app.py` lines above look identical
because the actual dispatch happens by inspecting `app.py`'s content, not
its filename. Want to try these against something real without writing
your own app first? Clone this repo and swap in `tests/fixtures/static-
site`, `tests/fixtures/streamlit-app/app.py`,
`tests/fixtures/fastapi-app/app.py`, `tests/fixtures/mcp-app/app.py`, or
`tests/fixtures/notebook-app/notebook.ipynb`.

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
               [--pwa [--pwa-*]...] [--qr]
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
- `--pwa` / `--qr` — make the app installable to a phone home screen, and/or
  print a terminal QR code for the URL. See [PWA install and QR
  codes](#pwa-install-and-qr-codes) below.

Run `sidepage <command> --help` for the full flag list, including ones
that parse but aren't implemented yet (they report that clearly rather
than silently doing nothing).

## Proxying an already-running service

`sidepage proxy --port <n>` wraps a service you already have running —
`npm run dev`, a container, anything already listening on a port — with
the same reverse proxy, auth, and tunnel stack `serve` uses, but never
launches or owns the process itself (Ctrl+C/`stop` only tears down the
proxy and tunnel, not your service).

```bash
sidepage proxy --port 5173 --anon   # already running: npm run dev on 5173
```

**Read the full guide before pointing this at anything public** —
[`docs/guides/proxy.md`](docs/guides/proxy.md) covers the safety notes
(`X-Forwarded-*`, localhost-trust, OAuth/`--anon`), a one-line header fix
per framework, and a known Vite HMR limitation.

## Timeouts, lazy start, and peers

`--timeout <seconds>` / `--idle-timeout <seconds>` auto-stop a served app
(total lifetime, or no-traffic window); `code`/`notebook` targets also
lazy-start their subprocess on the first inbound request instead of at
`serve` time; `--peer <role>=<app-name>` wires one served app to
another's URL via `SIDEPAGE_PEER_<ROLE>_URL`.

```bash
sidepage serve backend.py --name backend
sidepage serve frontend.py --idle-timeout 1800 --peer api=backend
```

Full detail (live peer re-resolution via `GET /.sidepage/peers.json`,
exactly when lazy start fires, why `--peer` is `code`/`notebook`-only) in
[`docs/guides/timeouts-and-peers.md`](docs/guides/timeouts-and-peers.md).

## PWA install and QR codes

`--pwa` makes any served app installable to a phone home screen —
manifest, service worker, and HTML injection all synthesized by the
reverse proxy, the wrapped app never touched on disk. `--qr` prints a
terminal QR code for the resulting URL, independent of `--pwa`.

```bash
sidepage serve app.py --anon --pwa --qr
```

Every `--pwa-*` flag, the ephemeral-vs-durable install distinction, and
icon validation are in [`docs/guides/pwa.md`](docs/guides/pwa.md).

## Saved apps (the local registry)

Save a `serve` invocation under a short name and re-run it without
retyping flags — any flag passed at `serve` time overrides the
registered one for that run only, the saved registration is never
changed.

```bash
sidepage app register "abc.py --auth token" abc-app
sidepage serve abc-app
```

Override/merge semantics, the `--with` preview, and why a literal
`--token` is refused at registration time are in
[`docs/guides/registry.md`](docs/guides/registry.md).

## Bring your own domain

Route apps through your own Cloudflare domain instead of
`*.trycloudflare.com`:

```bash
sidepage secrets set cf-api-token
sidepage account domain set example.com --api-token-name cf-api-token
sidepage serve app.py --domain example.com
```

One-time setup needs a scoped Cloudflare API token; every app served
under the same domain then shares that one Cloudflare Tunnel — no new
resources or tokens per app. Token scopes and the shared-tunnel model are
in [`docs/guides/byo-domain.md`](docs/guides/byo-domain.md).

## For agents and harnesses

[`skills/sidepage-serve/`](skills/sidepage-serve/) is a packaged [Claude
Skill](https://docs.claude.com/en/docs/claude-code/skills) that teaches an
agent to drive `sidepage serve`/`sidepage proxy` safely — most importantly,
how to background them and get structured JSON back instead of hanging,
since neither command has a daemon mode and both block until Ctrl+C or
`sidepage stop`. Copy or symlink that directory into wherever your harness
looks for skills (e.g. `~/.claude/skills/`).

## Development

```bash
uv sync                 # install runtime + dev deps
uv run ruff check .     # lint
uv run pytest           # full suite (~4 min; mostly first-run dependency resolves)
```

Project layout, the full dependency list, and what's needed on the
machine running tests (`cloudflared`, Node.js/npm for the Vite fixture)
are in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Project status

**Real and tested end to end:** `serve`/`proxy` for static, code, and
notebook targets, `open`/`token` auth, `--env` secret injection, `--anon`
tunneling, BYO-domain tunneling, `secrets`, `stop`/`ls`/`status`/`usage`,
`inspect`, the local app registry, `--timeout`/`--idle-timeout`/`--peer`,
and `--pwa`/`--qr`.

**Not implemented, and reports that clearly rather than silently
no-op'ing:** brokered (default) tunneling, `login`/`account status`, the
discovery directory beyond this machine, `--guardrail`, `--auth
network`/`oauth`, MCP tool browsing in `inspect`, and an OS-keychain
backend for the secrets vault (encrypted-file only for now).

**One known limitation, investigated not fixed:** HMR/live-reload for a
Vite target proxied through `--anon` — see
[`docs/guides/proxy.md`](docs/guides/proxy.md).

See [`docs/CHECKLIST.md`](docs/CHECKLIST.md) for the full per-feature
breakdown.

## See also

- [`docs/guides/proxy.md`](docs/guides/proxy.md),
  [`docs/guides/timeouts-and-peers.md`](docs/guides/timeouts-and-peers.md),
  [`docs/guides/pwa.md`](docs/guides/pwa.md),
  [`docs/guides/registry.md`](docs/guides/registry.md),
  [`docs/guides/byo-domain.md`](docs/guides/byo-domain.md) — per-feature
  deep dives.
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) — project layout,
  contributor setup.
- [`docs/CHECKLIST.md`](docs/CHECKLIST.md) — full per-feature build status.
- [`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md) — design rationale
  behind what's resolved and what's still open.
