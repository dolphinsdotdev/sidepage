# sidepage

Local-first hosting and tunneling for code, static sites, and notebooks.
`sidepage new` scaffolds a static site, `sidepage serve` wraps and hosts
anything and hands you a URL — a brokered tunnel by default, ephemeral,
torn down immediately when you Ctrl+C.

> **Status: `serve` and `secrets` are real; most of the rest is scaffold.**
> `sidepage serve` genuinely wraps and hosts static sites and Streamlit
> apps behind a real local reverse proxy, with working `open`/`token` auth,
> `--env` secret injection, and a real anonymous Cloudflare tunnel.
> `sidepage secrets` is a real encrypted local vault. Everything that needs
> a Sidepage cloud backend that doesn't exist — brokered/BYO-domain
> tunneling, the directory service, account/login — still prints a
> `not yet implemented` notice. See [Project status](#project-status) for
> the full real-vs-stubbed breakdown.

## Try it

```bash
uv sync
uv run sidepage serve tests/fixtures/static-site --name demo
# → http://127.0.0.1:<port>, Ctrl+C to stop

uv run sidepage serve tests/fixtures/streamlit-app/app.py --name demo --auth token
# → wraps the real Streamlit app, gates it behind a token, prints the URL

uv run sidepage secrets set MY_KEY   # prompts, hidden input
uv run sidepage serve some_app.py --env MY_KEY --anon
# → injects MY_KEY into the process env, exposes a real *.trycloudflare.com URL
```

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

Two pieces make v3 different from a bare tunnel wrapper — both real:

- **Local reverse proxy** (Starlette + uvicorn + httpx + `websockets`) —
  every request goes through a proxy that runs on *your* machine, sitting
  between the tunnel and the app's real port. It enforces `--auth`, counts
  usage, shows a startup holding page while the app boots, and proxies
  WebSockets — the wrapped app itself needs zero Sidepage-specific code.
  This is local and user-controlled, so it doesn't break the "Sidepage
  never inspects payloads" promise, which is specifically about the
  *cloud* side.
- **Tunneling** — `--anon` gives you a real, working `*.trycloudflare.com`
  URL via a `cloudflared` subprocess, no account needed. The spec's
  *default* tunnel mode ("brokered," under Sidepage's own domain) and
  bring-your-own-domain both need a Sidepage cloud backend that doesn't
  exist to build against — `serve` without `--anon`/`--domain` today just
  serves on `127.0.0.1`, and `--domain` reports clearly that it isn't
  implemented rather than pretending to work.

## Command reference

Every command below matches a numbered section in the working spec. Most
are still v3 (`sidepage-cli-spec-v3.md`); a handful carry a **v4** note
where a later 4-point delta changed them — see
[Open questions](#open-questions) for why v4 is a partial picture here
rather than a full spec read.

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
               [--token <value>] [--env <SECRET_NAME>]... [--scope local|lan|intranet|web]
               [--guardrail <config.yaml>]
sidepage stop <app-name>
```
`serve` infers the target kind from what's passed (`--type` stays as an
override for when that's wrong). No manual port handling — Sidepage
allocates a real port and injects it via `$PORT` or a recognized launcher
flag; traffic always goes through the local reverse proxy first, never
straight to the app. Blocks the terminal; Ctrl+C (or `sidepage stop`,
sending SIGTERM) tears everything down **immediately**, no grace period.

**Real:** `static` and `code` targets (the latter detected as Streamlit via
an import scan, else a generic `$PORT`-reading fallback); `open`/`token`
auth; `--anon`; `--env`; `stop`. **Not implemented** — each reports a clear
message rather than silently no-op'ing: `notebook` targets, `--domain`,
`--scope` beyond the default `local`, `--auth network`/`oauth`,
`--guardrail` (parked, see [Open questions](#open-questions)).

**v4:** `--env <SECRET_NAME>` (repeatable) injects a named secret from the
vault (below) into the wrapped process's environment. Explicit and
per-app, not blanket — fails loud if the name isn't in the vault.

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
- **Brokered (default, free tier)** — **not implemented.** Sidepage's
  backend would hold real Cloudflare credentials and issue a scoped,
  single-tunnel token per `serve` call under Sidepage's own domain — there
  is no such backend to build against, so this isn't "not built yet" so
  much as "cannot exist until Sidepage's cloud side does." `serve` without
  `--anon`/`--domain` just serves on `127.0.0.1` instead of silently
  failing or pretending to broker.
- **BYO-domain (premium, or `serve --domain`)** — **not implemented**, for
  the same reason (needs real DNS automation against a user's own
  Cloudflare zone). `serve --domain` reports this clearly.

  **v4:** v3 left the storage mechanism for the two BYO credentials
  unspecified. v4 answers it — store both via `sidepage secrets set`, then
  point `sidepage account domain set <domain> --zone-token-name <name>
  --tunnel-token-name <name>` at them by name — and that storage/reference
  wiring is real even though opening the tunnel itself isn't yet.
- **Anonymous (`serve --anon`)** — **real.** A `cloudflared tunnel --url`
  subprocess, no account or credentials needed, genuinely reachable at the
  `*.trycloudflare.com` URL it prints. Independent of `--auth`:
  `--anon --auth token` is valid.

The `cloudflared` binary is resolved via an override path/env var, then a
`PATH` lookup (`shutil.which`) — both real. The spec's other two steps
(local cache, download-on-first-run with checksum verification) aren't
implemented; not a practical gap today since a system-installed
`cloudflared` (e.g. via Homebrew) satisfies the `PATH` step.

### Metering (§7)

```bash
sidepage usage <app-name>
```
**Real.** HTTP request/response counts and WebSocket connection/message
counts — tracked separately, since a WS session has no request/response
pairing. Observed by the local reverse proxy (persisted to a small JSON
file per app), not self-reported by the app. Resolved as the permanent
billing boundary, forever. The same counters are read by `sidepage usage`
and, live, by `sidepage inspect`'s `usage` command.

### Token handling (§8)

**Real, end to end.** `--auth token` reads `--token <value>` or
`SIDEPAGE_TOKEN` (prefer the env var — shell history / `ps aux` exposure),
or generates and prints one if neither is given. Written to a per-process
runtime file (`~/.local/state/sidepage/runtime/<app-name>-<pid>.json`, mode
`0600`), no rotation — a new value means a new `serve` call. Browser-facing
requests get a gate page that sets a session cookie valid until app stop;
`Authorization: Bearer <token>` header or `?token=` query param work too,
for programmatic callers.

**v4 clarification, not new behavior:** the same runtime file also holds
any broker-issued tunnel token from the brokered tunnel mode (§6). Both
live there because they share one ephemeral lifecycle — they die with the
`serve` process — not because they're the same kind of credential. This is
the explicit boundary against the secrets vault below: anything ephemeral
belongs in the runtime file, anything standing and user-supplied belongs in
the vault.

### Secrets vault (v4 §9)

```bash
sidepage secrets set <name>      # prompts for the value, hidden input
sidepage secrets list
sidepage secrets remove <name>
```
The major new piece in v4, and **real** — with one deliberate gap. v3 had
no concept of standing, persistent, user-supplied secrets at all;
everything was either the ephemeral auth-token runtime file above, or
hand-waved. The spec's design calls for the OS keychain as the primary
backend with an encrypted-file fallback; **this build implements the
encrypted-file backend only** (Fernet symmetric encryption, key + data
both mode `0600` under `~/.config/sidepage`). OS keychain access via the
`keyring` package triggers an interactive macOS permission prompt on first
use, which isn't safe to depend on in an automated CLI tool — deferred
rather than half-built, with the same public API so adding it later
doesn't change any caller. `secrets set` always prompts rather than taking
the value as a CLI argument — the vault holds standing credentials, so it
gets a stricter shell-history stance than the ephemeral `--token` above.
`secrets list` shows names only, never values.

Consumed by `serve --env <SECRET_NAME>` (above) and `account domain set
--zone-token-name`/`--tunnel-token-name` (§6, above) — both reference vault
entries by name, never by raw value.

> This repo's migration to v4 was done from a 4-point delta summary, not
> the full v4 spec text. The user labeled this section "v4 §9," but v3 §9
> is "local reverse proxy" — whether v4 actually renumbered that section is
> unconfirmed. See [Open questions](#open-questions).

### Local reverse proxy (§9, per v3 — see note above)

**Real.** Sits between the tunnel and the app's real port, on your own
machine — see [Architecture](#architecture-in-short) above. Auth
enforcement, usage counting, a startup holding page, and WebSocket
proxying all work; streaming passthrough uses httpx's `aiter_raw` paired
with forwarded original headers. Graceful drain on `stop` isn't
implemented — teardown is immediate, per the confirmed default (whether a
grace period gets added later is a deferred, unresolved design question).

### Inspection (§10)

```bash
sidepage inspect [<app-name-or-url>]
```
**Real, for generic HTTP/static targets — MCP tool browsing deferred.**
An interactive REPL against anything `serve` wraps: `get`/`post`/`put`/
`patch`/`delete`/`head <path> [json body]`, `header <name> <value>` to set
a session header, `replay` the last request, `info`, and live `usage`
counts. With no argument, lists this machine's running apps to pick from.
No auth bypass for the local operator — same credential as any caller,
auto-sourced from the token runtime file when inspecting an app registered
on this machine (falls back to none for `--auth open` apps or a raw URL).

The spec's actual "Postman-for-MCP" framing — browsing MCP tool schemas,
`tools/list`/`tools/call` over MCP's JSON-RPC transport — isn't built yet.
Neither of the two prioritized fixtures (static site, Streamlit app) is an
MCP server, so there was nothing real to build that piece against; see
[Open questions](#open-questions) item 14 for what's still undecided
(client library choice, whether to add an MCP fixture) when it's picked
back up.

```bash
sidepage ls [--scope <scope>] [--mine]
sidepage status <app-name>
```
**Real**, against `sidepage.core.registry` — this machine's running apps,
not a cloud directory (there isn't one). `--scope`/`--mine` are accepted
but noted as not meaningful yet rather than silently ignored. `status`
does a live reachability check against the registered local URL. Not a
numbered section in v3 either way (v1 had a "Directory queries" §10; v3
doesn't mention `ls`/`status` at all) — kept since the underlying concept
(what's running, is it reachable) is still useful without a cloud backend.

### Static site serving (§11)

**Real.** `StaticFiles(directory=..., html=True)`, mounted directly inside
the same proxy process (no extra hop). Missing `index.html` at root is a
**hard error**, not a directory listing.

### Notebook serving (§12)

```bash
sidepage serve notebook.ipynb --auth token
```
**Not implemented** — `detect_target_kind` recognizes `.ipynb` targets (so
`--type` reporting stays honest), but there's no Jupyter Lab launcher or
proxy-safety-check behind it yet. Wasn't one of the two prioritized
targets.

### Account & login (§13)

```bash
sidepage login
sidepage account status
sidepage account domain set <domain> --zone-token-name <name> --tunnel-token-name <name>
```
**Not implemented** — there's no Sidepage account backend to log in
against. `account status` would cover what v1's `whoami` did. `account
domain set` requires both `--*-token-name` flags at the CLI level (v4) but
doesn't yet check the named secrets actually exist in the vault before
reporting not-implemented — that check belongs in
`sidepage.core.account.set_default_domain`, still a placeholder.

### Ecosystem integration (§14)

**Real for Python, not implemented for JS.** `resolve_python_runner`
prefers, in order: a `.venv` sitting next to the target (an existing
project's own environment — used as-is, no `uv run` layered on top);
`uv run --with-requirements requirements.txt` (a sibling requirements
file); `uv run --with <package>` (a bare script, single best-guess
dependency). JavaScript's lockfile detection isn't built — no JS target
has been prioritized yet.

### Out of scope

Fleet/process management across multiple running apps is a **separate
product** (the orchestrator) — deliberately not a mode of this binary, so
`sidepage` stays single-host and single-process by contract. `--background`
is explicitly ruled out for the same reason.

## Project layout

`●` real and tested · `○` placeholder (`NotImplementedError`, well-documented intent)

```
src/sidepage/
├── cli.py                 Root Typer app — wires every command module together
├── output.py               Shared console output helpers (not-implemented notices, etc.)
├── commands/                One module per spec area — argument parsing & help text
│   ├── new.py                 §1  ○ sidepage new
│   ├── serve.py                §2  ● sidepage serve, sidepage stop
│   ├── scope.py                 §5  ○ sidepage promote
│   ├── usage.py                  §7  ● sidepage usage
│   ├── secrets.py                 v4 §9  ● sidepage secrets set|list|remove
│   ├── inspect.py                  §10 ● sidepage inspect (HTTP/static; MCP tool browsing deferred)
│   ├── directory.py                 ● sidepage ls, sidepage status (no v3 section, see above)
│   └── account.py                    §13 ○ sidepage login, sidepage account status|domain set
├── core/                     The SDK
│   ├── target.py                §1/§2  ● TargetKind, detect_target_kind, allocate_port, Streamlit launcher detection
│   ├── scaffold.py               §1  ○ static-only scaffold generation
│   ├── process.py                 §2  ● serve/stop orchestration — the biggest real module
│   ├── auth.py                      §4  ● AuthTier (open/token enforced; network/oauth not)
│   ├── directory_client.py           §3/§5  ○ promote; ● list_entries/get_status via registry
│   ├── tunnel_manager.py              §6  ● open_anon_tunnel + cloudflared resolution; ○ brokered/BYO
│   ├── usage_reporter.py               §7  ● get_usage, reads the proxy's persisted counters
│   ├── token_runtime.py                 §8  ● token generation, runtime file read/write
│   ├── secrets_vault.py                  v4 §9  ● encrypted-file backend (keychain deferred)
│   ├── reverse_proxy.py                   §9 (v3)  ● Starlette/httpx/websockets proxy, auth gate, usage counting
│   ├── registry.py                         ● local running-app registry (new — not a spec section, backs ls/status/stop)
│   ├── static.py                            §11 ● StaticFiles validation + mount
│   ├── inspector.py                          §10 ● generic HTTP/static console; ○ MCP tool browsing
│   ├── notebook.py                            §12 ○ Jupyter Lab launch + safety check
│   ├── account.py                              §13 ○ login/status/domain
│   ├── ecosystem.py                             §14 ● Python runner resolution (venv/uv); ○ JS
│   ├── guardrail.py                              ○ parked (absent from v3/v4, not confirmed cut)
│   └── exceptions.py                              shared exception hierarchy
└── config/                    Local config paths
    └── settings.py              ● XDG-style, SIDEPAGE_HOME-overridable: ~/.config, ~/.cache, ~/.local/state

tests/
├── fixtures/                 The two prioritized real test targets
│   ├── static-site/index.html   an actual marketing site (not a toy fixture)
│   └── streamlit-app/app.py     a real Streamlit + pandas/numpy app
├── test_cli_smoke.py         Argument parsing, help text, command wiring — fast, in-process
├── test_serve_integration.py Real end-to-end: launches the CLI as a subprocess against both fixtures
└── test_inspector.py         Real: target resolution, auth auto-sourcing, request execution

docs/
├── OPEN_QUESTIONS.md    Resolved-in-v3 and still-open design decisions
└── CHECKLIST.md         Running checklist of every feature's build status — update on every change
```

`commands/` is the CLI shell (argument parsing, help text, choice
validation) and is fully built out regardless of whether the `core`
function behind it is real yet. Every still-placeholder command calls
`sidepage.output.not_implemented(...)`, naming the exact `core` symbol that
will eventually replace that call — so implementing a command later means
filling in one `core` module and swapping one line in `commands/`, not
redesigning the CLI surface.

## Development

```bash
uv sync                                          # installs runtime + dev deps
uv run ruff check .                              # lint
uv run pytest tests/test_cli_smoke.py            # fast, in-process — argument parsing & wiring
uv run pytest tests/test_serve_integration.py    # slower — real subprocess, real HTTP/WS
uv run pytest tests/test_inspector.py            # real target resolution + request execution
uv run pytest                                    # everything (~45s, mostly Streamlit's first boot)
uv run sidepage --help                           # try the CLI
```

Runtime dependencies are real, not just named-but-uninstalled: Starlette,
uvicorn, httpx, `websockets` (the reverse proxy), and `cryptography` (the
secrets vault). `cloudflared` and, for the Streamlit fixture, either a
`.venv` next to the target or network access for `uv run` to resolve
dependencies are expected to be available in the environment running the
tests.

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

The v4 migration (secrets vault, `serve --env`, BYO-domain credentials
routed through the vault, the §8 clarifying note) was done from a 4-point
delta summary rather than a full spec document — two things that summary
didn't specify are flagged as open rather than assumed: whether v4 actually
renumbered §9 (reverse proxy) to make room for the vault, and whether
`sidepage secrets list`/`remove` need any scoping beyond a flat namespace.

## Project status

`sidepage serve` and `sidepage secrets` — the two features asked to be
prioritized — are **real, working code**, verified end to end against two
actual apps (not synthetic fixtures): a marketing site
(`tests/fixtures/static-site`) and a Streamlit + pandas/numpy app
(`tests/fixtures/streamlit-app`). `sidepage inspect` followed the same
bar, scoped to what those two fixtures could actually verify.

**What actually works today:**
- `sidepage serve <static-dir>` — real Starlette `StaticFiles`, reachable over HTTP.
- `sidepage serve <script.py>` — real subprocess wrapping, with Streamlit detected via import scan and given real port-flag injection; anything else falls back to a generic `$PORT`-reading launch.
- `--auth open` / `--auth token` — real gate: header, query param, or browser cookie, enforced by the actual proxy.
- `--env <SECRET_NAME>` (repeatable) — real injection from the vault into the wrapped process's environment, failing loud on a missing name.
- `--anon` — a real `*.trycloudflare.com` URL via a `cloudflared` subprocess. (Verified that `cloudflared` itself successfully connects and gets a real assigned URL; verifying an actual browser could reach that URL from the public internet wasn't possible from this sandboxed dev environment's network policy — the mechanism is implemented and tested up to that boundary.)
- `sidepage secrets set|list|remove` — a real encrypted local vault (Fernet, not just a stub).
- `sidepage stop` / `sidepage ls` / `sidepage status` / `sidepage usage` — real, backed by a local running-app registry (this machine only, see [Project layout](#project-layout)) and the proxy's persisted usage counters.
- `sidepage inspect` — real interactive console for HTTP/static targets: ad-hoc requests, header overrides, replay, live usage, auth auto-sourcing. Directory-aware picker when no target is given.

**What's still a documented placeholder, and why:**
- Anything needing a Sidepage cloud backend — brokered/BYO-domain tunneling, the directory service beyond `--scope local`, `login`/`account status` — because that backend doesn't exist to build against. `serve` reports these clearly rather than silently ignoring the flag or pretending to work.
- `notebook` targets, `--guardrail`, `--auth network`/`oauth` — not among the prioritized features, left as placeholders with the same documented-intent style as the rest of this package.
- MCP tool browsing within `sidepage inspect` (schemas, `tools/list`, `tools/call`) — the spec's actual "Postman-for-MCP" framing. Deferred because neither prioritized fixture is an MCP server; see [Open questions](#open-questions) item 14 for the client-library decision still pending when this gets picked up.
- OS keychain backend for the secrets vault — deferred in favor of the encrypted-file backend, which doesn't need an interactive permission prompt (see Secrets vault, v4 §9, above).

Every placeholder still calls `sidepage.output.not_implemented()` (or, in
`sidepage.core`, raises `NotImplementedError`) naming the exact symbol that
would replace it — implementing one of these is a matter of filling in a
function, not inventing the CLI surface from scratch.
