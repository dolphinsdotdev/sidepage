# sidepage

Local-first hosting and tunneling for code, static sites, and notebooks.
`sidepage serve` wraps almost anything — a script, a static site, a
Streamlit or FastAPI app, a Python MCP server, a Jupyter notebook —
behind a local reverse proxy and hands you a URL. `sidepage new`
scaffolds a static site to get started.

**Status:** `serve`, `secrets`, `inspect`, and bring-your-own-domain
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
```

Every `serve` call blocks the terminal until Ctrl+C (or `sidepage stop
<app-name>` from another terminal), tearing everything down immediately —
no background/daemon mode.

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
| `sidepage stop <app-name>` | Tear down a running app. |
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

Run `sidepage <command> --help` for the full flag list, including ones
that parse but aren't implemented yet (they report that clearly rather
than silently doing nothing).

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
├── fixtures/        Real apps used as test targets (static site, Streamlit, FastAPI, MCP, notebook)
└── test_*.py        Unit and integration tests

docs/
├── CHECKLIST.md       Build status for every command and core module
└── OPEN_QUESTIONS.md  Design decisions — resolved and still-open
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

## Project status

**Real and tested end to end:** `serve` for static, code, and notebook
targets (Streamlit/FastAPI/Python-MCP auto-detected, generic `$PORT`
fallback, full Jupyter Lab for `.ipynb`), `open`/`token` auth, `--env`
secret injection, `--anon` tunneling, BYO-domain tunneling (`account
domain set` + `serve --domain`), `secrets`, `stop`/`ls`/`status`/`usage`,
`inspect` for HTTP/static targets, and the local app registry (`app
register|list|show|unregister` + `serve <app-name>`, with real one-off
override merging).

**Not implemented, and reports that clearly rather than silently
no-op'ing:** brokered (default) tunneling, `login`/`account status`, the
discovery directory beyond this machine, `--guardrail`, `--auth
network`/`oauth`, MCP tool browsing in `inspect`, and the OS-keychain
backend for the secrets vault (encrypted-file only for
now).

See [`docs/CHECKLIST.md`](docs/CHECKLIST.md) for the full per-feature
breakdown, and [`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md) for
design rationale behind what's resolved and what's still open.
