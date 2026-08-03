# sidepage

Local-first hosting and tunneling for code, static sites, and notebooks.
`sidepage new` scaffolds a static site, `sidepage serve` wraps and hosts
anything and hands you a URL — a brokered tunnel by default, ephemeral,
torn down immediately when you Ctrl+C.

> **Status: CLI scaffold only.** The full command tree from the spec is
> wired up with real argument parsing and help text, but no command does
> anything yet — each one prints a `not yet implemented` notice naming the
> `sidepage.core` module that will eventually back it. See
> [Project status](#project-status) below.

---

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+ (uv will fetch
the interpreter if you don't have it).

```bash
uv sync
```

This creates a `.venv/` and installs `sidepage` into it in editable mode.
Run the CLI through uv without activating the venv:

```bash
uv run sidepage --help
```

Or activate the venv and call it directly:

```bash
source .venv/bin/activate
sidepage --help
```

## Architecture, in short

Two pieces make v3 different from a bare tunnel wrapper:

- **Local reverse proxy** — every request goes through a proxy that runs
  on *your* machine, sitting between the tunnel and the app's real port.
  It's what enforces `--auth`, counts usage, shows a startup holding page
  while the app boots, and proxies WebSockets — the wrapped app itself
  needs zero Sidepage-specific code. This is local and user-controlled, so
  it doesn't break the "Sidepage never inspects payloads" promise, which is
  specifically about the *cloud* side.
- **Brokered tunnel by default** — `sidepage serve` doesn't require you to
  bring your own Cloudflare account. The free tier runs through a tunnel
  Sidepage's own backend brokers on its own domain; bringing your own
  domain is a premium/`--domain` opt-in, and `--anon` skips the broker
  entirely for a throwaway Quick Tunnel URL.

## Command reference

Every command below matches a numbered section in the working spec
(`sidepage-cli-spec-v3.md`); the § references point there.

### Targets (§1)

Three things `serve` knows how to wrap: **code** (any HTTP-serving
process — Flask, FastAPI, MCP over HTTP, a bare `streamlit run`, ...),
**static** (a directory, `index.html` as entry), and **notebook** (a
`.ipynb`, full Jupyter Lab exposed). No import, no code cooperation
required for `code` targets — Sidepage wraps, it doesn't generate.

```bash
sidepage new <name> --type static
```
About the only scaffolding kept: a minimal static-site skeleton. No
`--deps` — a static site has no dependencies to pin.

### Serving (§2)

```bash
sidepage serve <target> [--type auto|code|static|notebook] [--name <app-name>]
               [--domain <domain>] [--auth open|network|token|oauth] [--anon]
               [--token <value>] [--scope local|lan|intranet|web] [--guardrail <config.yaml>]
sidepage stop <app-name>
```
`serve` infers the target kind from what's passed (`--type` stays as an
override for when that's wrong). No manual port handling — Sidepage
allocates a real port and injects it via `$PORT` or a recognized launcher
flag; traffic always goes through the local reverse proxy first, never
straight to the app. Blocks the terminal; Ctrl+C tears the tunnel down
**immediately**, no grace period. `stop` is the explicit, non-interactive
teardown. `--guardrail` is a placeholder — parked, not built (see
[Open questions](#open-questions)).

### Naming & identity (§3)

`<app-name>-<4-char-id>.<domain>.<tld>` — collision-proof by construction.
The directory is the identity root (owner, creation time, scope,
teardown/health status). No grace period on name reclaim — confirmed
default, accepted risk. `--anon` apps never enter the directory at all.
(v1's `whoami`/`name check` commands are gone — see `sidepage account
status` under Account & login, §13, below.)

### Auth tiers (§4)

```bash
sidepage serve <target> --auth open|network|token|oauth
```
Declared per-app at `serve` time, enforced by the **local reverse proxy**
(not the app, not Sidepage's cloud backend). `open` is the default;
`network` is an IP allowlist/mTLS at the tunnel edge; `token` is covered
in full below (§8); `oauth` is deferred, not implemented — kept as a valid
choice pending a dedicated MCP auth model discussion. No more standing
`keys create|revoke|list` — tokens are per-`serve`, not per-account.

### Discovery & scope (§5)

```bash
sidepage serve <target> --scope local|lan|intranet|web
sidepage promote <app-name> [--scope web]
```
One directory, scope as a field — confirmed, not split per scope tier.
`promote` widens visibility without issuing a new identity.

### Tunnel architecture (§6)

No standalone `tunnel` command group in v3. Three modes, selected by what
`serve` is given:
- **Brokered (default, free tier)** — Sidepage's backend holds real
  Cloudflare credentials and issues a scoped, single-tunnel token per
  `serve` call, under Sidepage's own domain.
- **BYO-domain (premium, or `serve --domain`)** — you supply a scoped
  Zone:DNS:Edit token plus a per-tunnel token (never the global account
  key), set via `sidepage account domain set` (Account & login, §13, below).
  Credentials are stored **locally only**, never sent to the directory.
- **Anonymous (`serve --anon`)** — Cloudflare Quick Tunnel, no broker
  call, no directory entry, `*.trycloudflare.com`. Independent of
  `--auth`: `--anon --auth token` is valid.

The `cloudflared` binary itself is resolved in order: an override
path/env var, a version-checked `PATH` lookup, a local cache
(`~/.cache/sidepage/bin/cloudflared`), then download-on-first-run with
checksum verification. No Python port of `cloudflared` is assumed.

### Metering (§7)

```bash
sidepage usage <app-name>
```
HTTP request/response counts and WebSocket connection/message counts —
tracked separately, since a WS session has no request/response pairing.
Observed by the local reverse proxy, not self-reported by the app.
Resolved as the permanent billing boundary, forever. Same counters also
show up live in `sidepage inspect`.

### Token handling (§8)

`--auth token` reads `--token <value>` or `SIDEPAGE_TOKEN` (prefer the env
var — shell history / `ps aux` exposure), or generates and prints one if
neither is given. Written to a per-process runtime file
(`~/.local/state/sidepage/runtime/<app-name>-<pid>.json`, mode `0600`), no
rotation — a new value means a new `serve` call. Browser-facing targets get
a gate page that sets a session cookie valid until app stop; programmatic
callers use a header or query param instead.

### Local reverse proxy (§9)

Sits between the tunnel and the app's real port, on your own machine — see
[Architecture](#architecture-in-short) above. Owns auth enforcement, usage
counting, a startup holding page, mandatory WebSocket proxying, and
streaming passthrough. Graceful drain on `stop` (vs. today's immediate
teardown) is a deferred, unresolved design question.

### Inspection (§10)

```bash
sidepage inspect [<app-name-or-url>]
```
Interactive "Postman-for-MCP" console: browse tools, inspect schemas,
invoke calls, replay requests, view live usage counts. With no argument,
lists discoverable servers from the directory. No auth bypass for the
local operator — same credential as any caller, just auto-sourced from the
token runtime file when inspecting your own app.

```bash
sidepage ls [--scope <scope>] [--mine]
sidepage status <app-name>
```
Not a numbered section in v3 (v1 had a "Directory queries" §10; v3 doesn't
mention `ls`/`status` at all) — kept since the directory model is still
central. `status` also folds in tunnel reachability, since v3 has no
separate `tunnel status`.

### Static site serving (§11)

`StaticFiles(directory=..., html=True)`. Missing `index.html` at root is a
**hard error**, not a directory listing.

### Notebook serving (§12)

```bash
sidepage serve notebook.ipynb --auth token
```
Full Jupyter Lab exposed — editable, live kernel, execution stays on your
machine. Jupyter's own token auth is disabled because the local proxy is
now the auth boundary instead. Dependencies resolve via `uv run --with
jupyter jupyter lab` inside a project, or a sibling `pyproject.toml` for a
bare `.ipynb` (`juv` is an evaluation candidate for the fully-standalone
case, not a commitment).

### Account & login (§13)

```bash
sidepage login
sidepage account status
sidepage account domain set <domain>
```
Deliberately separate from per-app `--auth`. `account status` covers what
v1's `whoami` did; `account domain set` is the premium persistent
BYO-domain path.

### Ecosystem integration (§14)

Python: **uv is the default dependency runner**, not merely preferred (a
narrowing from v1, accepted trade-off — excludes poetry/pip-only users
unless they also maintain a `uv.lock`). JavaScript: detect-and-defer across
`package-lock.json`/`yarn.lock`/`pnpm-lock.yaml`, no canonical manager
assumed.

### Out of scope

Fleet/process management across multiple running apps is a **separate
product** (the orchestrator) — deliberately not a mode of this binary, so
`sidepage` stays single-host and single-process by contract. `--background`
is explicitly ruled out for the same reason.

## Project layout

```
src/sidepage/
├── cli.py                Root Typer app — wires every command module together
├── output.py             Shared console output helpers (not-implemented notices, etc.)
├── commands/              One module per spec area — argument parsing & help text only
│   ├── new.py              §1  sidepage new
│   ├── serve.py             §2  sidepage serve, sidepage stop
│   ├── scope.py              §5  sidepage promote
│   ├── usage.py               §7  sidepage usage
│   ├── inspect.py              §10 sidepage inspect
│   ├── directory.py             sidepage ls, sidepage status (no v3 section, see above)
│   └── account.py                §13 sidepage login, sidepage account status|domain set
├── core/                  The SDK — unimplemented placeholders (signatures + docstrings only)
│   ├── target.py            §1/§2  TargetKind, detect_target_kind, port injection contract
│   ├── scaffold.py           §1  static-only scaffold generation
│   ├── process.py             §2  serve/stop lifecycle (ServeConfig)
│   ├── auth.py                  §4  AuthTier
│   ├── directory_client.py       §3/§5  Scope, DirectoryEntry, promote/ls/status
│   ├── tunnel_manager.py          §6  brokered/BYO/anon tunnel modes, cloudflared resolution
│   ├── usage_reporter.py           §7  HTTP + WS usage counters
│   ├── token_runtime.py             §8  token generation, runtime file storage
│   ├── reverse_proxy.py              §9  the local reverse proxy
│   ├── inspector.py                   §10 MCP inspection console
│   ├── static.py                       §11 static-file serving contract
│   ├── notebook.py                      §12 Jupyter Lab launch + safety check
│   ├── account.py                        §13 login/status/domain
│   ├── ecosystem.py                       §14 uv/JS runner resolution
│   ├── guardrail.py                        parked (absent from v3, not confirmed cut)
│   └── exceptions.py                        shared exception hierarchy
└── config/                Local config/credential path constants (no logic yet)
    └── settings.py          XDG-style: ~/.config, ~/.cache, ~/.local/state

tests/
└── test_cli_smoke.py    Verifies the command tree parses correctly end to end

docs/
├── OPEN_QUESTIONS.md    Resolved-in-v3 and still-open design decisions
└── CHECKLIST.md         Running checklist of every feature's build status — update on every change
```

The split between `commands/` and `core/` is deliberate: `commands/` is the
CLI shell (argument parsing, help text, choice validation) and is fully
built out; `core/` is the SDK that will actually do the work, and is
entirely unimplemented placeholders today. Every command function calls
`sidepage.output.not_implemented(...)`, naming the exact `core` symbol that
will eventually replace that call — so implementing a command later means
filling in one `core` module and swapping one line in `commands/`, not
redesigning the CLI surface.

## Development

```bash
uv sync                  # installs runtime + dev deps (pytest, ruff)
uv run pytest            # run the smoke test suite
uv run ruff check .      # lint
uv run sidepage --help   # try the CLI
```

## Feature checklist

[`docs/CHECKLIST.md`](docs/CHECKLIST.md) tracks build status for every
command and `core` module against the spec. It's meant to be kept current —
check an item off there in the same change that implements it.

## Open questions

See [`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md) for what v3 resolved
(directory model, name-reclaim, billing boundary, session validity,
inspection auth bypass, scaffolding scope) versus what's still open
(agent-to-agent signed tokens, graceful drain on `stop`, the MCP auth
model, stdio-transport MCP servers, the orchestrator's architecture, and
guardrails' unclear status). Each is also referenced from the docstring of
the `core`/`commands` module it affects.

## Project status

This repository currently contains **CLI scaffolding, not the SDK**: every
command in the spec parses its arguments and options correctly and reports
help text, but none of them do anything — they call
`sidepage.output.not_implemented()` and exit 0. The `sidepage.core` package
holds the intended shape of the real implementation (classes, function
signatures, docstrings) with every body raising `NotImplementedError`, so
that implementing a feature is a matter of filling in one file rather than
inventing the CLI surface from scratch.
