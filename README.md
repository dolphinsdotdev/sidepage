# sidepage

Local-first hosting and tunneling for code, static sites, and notebooks.
`sidepage serve` wraps almost anything — a script, a static site, a
Streamlit, FastAPI, or Gradio app, a Python MCP server, a Jupyter notebook
— behind a local reverse proxy and hands you a URL. `sidepage proxy` does
the same for a service you already have running (`npm run dev`, a
container, anything already listening on a port).

Everything shipped is tested end to end; anything needing a cloud backend that
doesn't exist yet says "not implemented" rather than failing silently ([Project
status](#project-status)).

## Install

```bash
pip install sidepage
sidepage setup      # installs cloudflared — needed for --anon/--domain tunneling
```

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/) on `PATH` — sidepage
shells out to it to run whatever `serve` points at. Contributing? See
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Quickstart

![sidepage serve, from command to public URL](docs/media/serve-demo.gif)

```bash
sidepage serve ./my-site --name demo              # static site
sidepage serve app.py --name demo --auth token    # gated behind a token
sidepage serve notebook.ipynb --name demo         # editable Jupyter Lab, live kernel

sidepage secrets set MY_KEY                       # inject a secret, expose publicly
sidepage serve app.py --env MY_KEY --anon

sidepage pull hf:kalpi314/tiny_notes              # pull a huggingface space and register as app
sidepage proxy --port 5173 --name my-vite-app     # wrap `npm run dev`
sidepage serve app.py --anon --pwa --qr           # installable, with a QR to scan
```

`--type` is auto-detected, and within `code` so is the framework —
Streamlit/FastAPI/MCP/Gradio each recognized by import and started with
their real launcher. That's why several lines above are an identical
`sidepage serve app.py`: dispatch reads content, not filenames. Both
commands block until Ctrl+C or `sidepage stop <app-name>`; `--detach`
backgrounds them ([below](#for-agents-and-harnesses)).

## How it works

- **A local reverse proxy** in front of the app's real port: enforces
  `--auth`, counts usage, shows a holding page while the app boots, and
  proxies HTTP + WebSockets. The wrapped app needs zero sidepage-specific
  code.
- **A tunnel**, per call: `--anon` for a free, no-account
  `*.trycloudflare.com` URL, or `--domain <domain>` for your own
  Cloudflare domain. Neither flag means `127.0.0.1` only.

## Commands

| Command | What it does |
|---|---|
| `serve <target>` | Wrap and host a static dir, script, or app. |
| `proxy --port <n>` | Wrap an already-running local service. |
| `stop <app-name>` | Tear down a running app (`serve` or `proxy`). |
| `ls` / `status` / `usage` | List apps, check one, read its request counts. |
| `inspect [<app-name>]` | Interactive HTTP console against a running app. |
| `secrets set\|list\|remove` | Encrypted local vault for standing credentials. |
| `account domain set` / `new <name>` | Provision a BYO Cloudflare domain / scaffold a static site. |
| `app register\|list\|show\|unregister\|delete` | Save and manage `serve` invocations. |
| `pull <source>` | Fetch a Hugging Face Space and register it, without running it. |
| `promote`, `login`, `account status` | Not built yet; they say so. |

```bash
sidepage serve <target> [--type auto|code|static|notebook] [--name <app-name>]
    [--auth open|token] [--anon | --domain <domain>] [--no-suffix] [--token <v>]
    [--env <SECRET_NAME>]... [--timeout <s>] [--idle-timeout <s>] [--qr]
    [--peer <role>=<app-name>]... [--pwa [--pwa-*]...] [--autoregister]
    [--detach] [--json]
```

`--auth token` gates the app behind a header, query param, or cookie set by a
gate page. `--env` is repeatable and fails loud on an unknown name. `--anon`
and `--domain` are mutually exclusive. `sidepage <command> --help` has the rest.

## Guides

- [proxy.md](docs/guides/proxy.md) — **read before proxying anything public**: forwarded headers, localhost-trust, per-framework fixes.
- [timeouts-and-peers.md](docs/guides/timeouts-and-peers.md) — auto-teardown, lazy start, wiring apps together.
- [pwa.md](docs/guides/pwa.md) — home-screen install and every `--pwa-*` flag.
- [registry.md](docs/guides/registry.md) — saving invocations, override and merge semantics.
- [byo-domain.md](docs/guides/byo-domain.md) — your own Cloudflare domain, token scopes, `--no-suffix`.
- [pull.md](docs/guides/pull.md) — Hugging Face Spaces, and the gate before running downloaded code.

## For agents and harnesses

`serve` and `proxy` block by default, which is right for a terminal and
wrong for anything automated. Pass **`--detach --json`** and they return
as soon as the app is genuinely serving — or has definitively failed —
with one parseable line on stdout:

```bash
sidepage serve app.py --name demo --anon --detach --json
```

```json
{"status":"running","app":"demo","pid":12345,"url":"https://random-words.trycloudflare.com","local_url":"http://127.0.0.1:8501","log":"~/.local/state/sidepage/logs/demo.log"}
```

Readiness is the registry entry the serving process writes once port,
subprocess, and tunnel are all up — not a URL spotted in a log — so
`"running"` means serving. A failed launch reports the real error and exits
1. Under `--json` all prose moves to stderr, so stdout pipes into a parser.

This repo is also a [plugin
marketplace](https://code.claude.com/docs/en/plugin-marketplaces): in
Claude Code, `/plugin marketplace add kalpi-4/sidepage` then `/plugin
install sidepage@sidepage`. That installs
[`plugin/skills/sidepage-serve/`](plugin/skills/sidepage-serve/), a
[Skill](https://code.claude.com/docs/en/skills) teaching an agent when to
reach for `serve` vs `proxy`, which flags matter, and what to surface
before pointing a tunnel at something. It bundles no executables, so it
also installs cleanly under organization settings. For any other harness,
copy that directory to wherever it looks for skills.

## Development

```bash
uv sync                 # install runtime + dev deps
uv run ruff check .     # lint
uv run pytest           # full suite (~2 min; mostly first-run dependency resolves)
```

Layout and test-machine requirements: [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md).

## Project status

**Real and tested end to end:** `serve`/`proxy` for static, code, and
notebook targets, `open`/`token` auth, `--env` secrets, `--anon` and
BYO-domain tunneling, `stop`/`ls`/`status`/`usage`, `inspect`, the app
registry, `--timeout`/`--idle-timeout`/`--peer`, `--pwa`/`--qr`, and
`--detach`/`--json`.

**Not implemented, and says so rather than silently no-op'ing:** brokered
tunneling, `login`/`account status`, the directory beyond this machine,
`--guardrail`, `--auth network`/`oauth`, MCP tool browsing in `inspect`,
and an OS-keychain vault backend. One known limitation, investigated not
fixed: HMR for a Vite target proxied through `--anon`.

Full breakdown in [`docs/CHECKLIST.md`](docs/CHECKLIST.md); rationale in
[`docs/OPEN_QUESTIONS.md`](docs/OPEN_QUESTIONS.md).
