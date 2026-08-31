# Development

[← back to README](../README.md)

For working on sidepage itself, not just using it — see the main
[README](../README.md) for that.

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
├── SPEC_V5_DRAFT.md   v5 proposals — timeout/lazy-start/--peer (built, this doc) plus still-parked ideas
├── DEVELOPMENT.md     This file
├── media/             README screen recording + the script that regenerates it
└── guides/            Per-feature deep dives linked from the README

skills/
└── sidepage-serve/    Packaged Claude Skill wrapping this CLI for agents
```
