# Feature checklist

Running checklist of every feature in the v3 spec, plus a v4 delta (secrets
vault and its consequences) applied from a 4-point chat summary rather than
a full v4 document, plus the real-implementation pass that made `serve` and
`secrets` — the two prioritized features — actually work. **Update this
file in the same change that flips a box** — that's the point of keeping
status here instead of letting it live only in commit messages.

Two layers per command:
- **CLI** — argument parsing, options, help text (`sidepage.commands`). Wiring only.
- **Core** — the actual behavior (`sidepage.core`). Check this off only when
  the function no longer raises `NotImplementedError`.

Legend: `[x]` done · `[ ]` not done · `[~]` real but scoped (see the note on that line)

---

## §1 Targets

- [x] CLI: `sidepage new <name> --type static`
- [ ] Core: `sidepage.core.scaffold.scaffold_project`
- [x] Core: `sidepage.core.target.detect_target_kind` (static/code/notebook — notebook recognized but not servable)
- [x] Core: `sidepage.core.target.detect_code_launcher` — Streamlit, FastAPI, and Python MCP servers via import scan, else generic `$PORT`
- [x] Core: `sidepage.core.target.allocate_port`

## §2 Serving

- [x] CLI: `sidepage serve <target> [--type] [--name] [--domain] [--auth] [--anon] [--token] [--env]... [--scope] [--guardrail]`
- [x] CLI: `sidepage stop <app-name>`
- [x] Core: `sidepage.core.process.serve` — the biggest real module; orchestrates target detection, port allocation, subprocess launch, proxy, tunnel, registry
- [x] Core: `sidepage.core.process.stop` — SIGTERM to the registered pid, routed through the same clean teardown as Ctrl+C
- [x] Core: port injection — `--server.port` flag for Streamlit, `uvicorn <module>:<app> --port` for FastAPI (bypasses a script's own `__main__` block, which real FastAPI apps often use to hardcode a port), `uvicorn <module>:<var>.<app-method> --factory --port` for MCP (bypasses the script's own `.run()` call too — see below), `$PORT` env var for the generic fallback
- [x] Core: immediate tunnel/proxy/subprocess teardown on Ctrl+C / `stop` (via a SIGTERM handler that raises `KeyboardInterrupt`)
- [x] Core: `--domain` (real, once configured — see §6/§13), non-`local` `--scope`, `--auth network`/`oauth`, `--guardrail` (the latter three still rejected up front with a clear message via `_validate_supported`, not silently ignored)
- [x] Core: `notebook` targets rejected with a clear message (detected but not servable)
- [x] CLI (v4): `--env <SECRET_NAME>` repeatable, vault injection
- [x] Core (v4): `serve` resolving each `env_secrets` name via `secrets_vault.get_secret`, fail loud (`SecretNotFoundError`) on miss
- [x] Verified end to end: static-site fixture and Streamlit fixture, both via real subprocess CLI invocation (`tests/test_serve_integration.py`)
- [x] Core: FastAPI launcher — `sidepage.core.target.detect_asgi_app_variable` (scans for `<name> = FastAPI(...)`, defaults to `app`), launched via real `uvicorn` CLI, not by running the script directly
- [x] Verified end to end: `tests/fixtures/fastapi-app` (real subprocess, `tests/test_serve_fastapi.py`) — port override, `/docs`, `/openapi.json`, a real POST endpoint, and the auth gate covering `/docs` too. Also manually verified against a real, non-fixture FastAPI app (local MLX LLM inference server) — real chat completion request succeeded through the proxy.
- [x] FastAPI `/docs`/`/redoc`/`/openapi.json` — no extra work needed, the generic HTTP proxy passthrough already covers them; `serve` prints the `/docs` URL when a FastAPI target is detected
- [x] Core: MCP (Python) launcher — `sidepage.core.target.detect_mcp_package`/`detect_mcp_app_variable` recognize two packages (official `mcp` SDK — `FastMCP` or, in the current major version, `MCPServer`, both exposing `.streamable_http_app()`; third-party `fastmcp` — `FastMCP`, exposing `.http_app()`), launched via real `uvicorn --factory <module>:<var>.<method>`, never by running the script or calling `.run()` directly. Because that entrypoint is never executed, a script whose own `__main__` only wires up the stdio transport (the default for both packages) still ends up served over real Streamable HTTP — resolves the practical case of `docs/OPEN_QUESTIONS.md` #4. Both packages' current API shape was verified live against their actually-resolvable releases, not assumed
- [x] Core: MCP/FastAPI detection precedence — a script that mounts an MCP server inside a FastAPI app (`app.mount("/mcp", mcp.streamable_http_app())`) is still detected as FASTAPI, not MCP, since the FastAPI app is the real top-level ASGI app and already serves the MCP sub-mount; only a standalone MCP script (no FastAPI import) is detected as MCP
- [x] Verified end to end: `tests/fixtures/mcp-app` (real `mcp` SDK server, `__main__` deliberately left stdio-only), real subprocess CLI invocation (`tests/test_serve_mcp.py`) — a genuine `initialize` handshake and `tools/call` round trip through the actual reverse proxy (not talking to the wrapped process directly), plus the auth gate covering `/mcp` too. Fast, no-subprocess detection-logic coverage in `tests/test_target.py`

## §3 Naming & identity

- [x] Resolved: no grace period on name reclaim (confirmed, accepted risk)
- [x] Core: `sidepage.core.directory_client.check_name` — real for BYO-domain hostnames: assigns and persists a random 4-char alphanumeric suffix per app name (`<app-name>-<id>`), stable across `serve` restarts, stored locally in `name_bindings.json`. Still not called for the local/`--name` path — `serve` uses `--name` or the target's filename directly there, since there's no cloud directory to check collisions against outside BYO-domain
- [x] Core: `--anon` apps skip directory registration (there's no directory to register with regardless — see §5)

## §4 Auth tiers

- [x] CLI: `sidepage serve --auth open|network|token|oauth`
- [x] Core: `open` tier — real passthrough, no gate
- [x] Core: `token` tier — real: header, query param, and cookie-based gate page, enforced by `sidepage.core.reverse_proxy`
- [ ] Core: `network` tier (IP allowlist/mTLS) — rejected with a clear message, not implemented
- [ ] Parked: `oauth` — deferred pending §15 MCP auth model, rejected with a clear message
- [ ] Design: agent-to-agent signed requests (see `docs/OPEN_QUESTIONS.md`)

## §5 Discovery & scope

- [x] Resolved: one directory, scope as a field (not per-org instances) — moot in practice since only `local` is servable
- [x] CLI: `sidepage serve --scope local|lan|intranet|web`
- [x] CLI: `sidepage promote <app-name> [--scope web]`
- [ ] Core: `sidepage.core.directory_client.promote` — not implemented, no directory to promote within
- [x] Core: `local` scope — real (the only one; it's simply "don't register anywhere," which is what `serve` already does)
- [ ] Core: `lan` (mDNS) / `intranet` (ACL) / `web` scope handling — rejected with a clear message

## §6 Tunnel architecture

- [x] CLI: `sidepage serve --domain <domain>` (BYO, premium)
- [x] CLI: `sidepage serve --anon` (Quick Tunnel)
- [ ] Core: `sidepage.core.tunnel_manager.open_brokered_tunnel` (default, free tier) — **not buildable**, needs a Sidepage cloud backend that doesn't exist, not "not implemented yet"
- [x] Core: `sidepage.core.tunnel_manager.provision_byo_domain` — real. One-time setup for `account domain set`: resolves the Cloudflare zone (and, from the same response, the owning account ID), then creates one Cloudflare Tunnel (`config_src: "cloudflare"`, remotely-managed ingress) meant to serve every app later run under the domain — not one tunnel per app. Superseded the earlier two-token "run an existing, out-of-band-created tunnel" design once it became clear a single, more broadly-scoped API token could create the tunnel itself — see `docs/OPEN_QUESTIONS.md` #12.
- [x] Core: `sidepage.core.tunnel_manager.open_byo_tunnel` — real. Resolves the API token and the tunnel run-token from the vault, assigns a stable hostname via `check_name`, upserts a proxied CNAME pointing at `<tunnel-id>.cfargotunnel.com`, ensures the domain's shared `cloudflared` process is running (starting it on first use), and adds/replaces this hostname's ingress rule via GET-modify-PUT against the Tunnel configurations API (never a blind PUT — see `_upsert_ingress_rule`). All three of the shared-process check, the CNAME upsert, and the ingress upsert happen under one per-domain advisory file lock (`_domain_lock`) so two `serve` calls racing on the same domain can't double-spawn `cloudflared` or clobber each other's ingress rule. Covered by mocked unit tests (`tests/test_tunnel_byo.py`), including the shared-process spawn-once/kill-on-last-down behavior and ingress idempotency; not yet verified against a real Cloudflare account (pending user-run end-to-end check, since real tokens can't be handled here)
- [x] Core: `sidepage.core.tunnel_manager.open_anon_tunnel` — real `cloudflared tunnel --url` subprocess, parses a genuine `*.trycloudflare.com` URL. Verified: cloudflared connects and gets a real URL back; verifying public reachability from a browser wasn't possible from this sandboxed environment's network policy
- [x] Core: `sidepage.core.tunnel_manager.resolve_cloudflared_binary` — real for 2 of 4 spec steps (override path, `PATH` lookup); local-cache/download-on-first-run not implemented, not a practical gap while `cloudflared` is on `PATH`
- [x] Core: `sidepage.core.tunnel_manager.close_tunnel` — real; for BYO-domain, removes this app's ingress rule then kills the domain's shared `cloudflared` process only if no other running app (per `sidepage.core.registry.list_running_for_domain`) still needs it — **caller contract**: the app being stopped must already be unregistered before this runs, or the last app's teardown would never see zero and the process would leak (see `sidepage.core.process._teardown`, which unregisters before calling this)
- [ ] Question: standalone `tunnel status`/`tunnel revoke` — folded into `status`/`account domain set`, or a real gap? (`docs/OPEN_QUESTIONS.md` #8)
- [x] Resolved (v4): BYO-domain credential storage mechanism — routes through the secrets vault by name, real end to end: `account domain set` validates the API token name resolves, provisions the tunnel, and stores its run-token under a reserved name (`cf-tunnel-token::<domain>`) it logs explicitly; `open_byo_tunnel` reads both back at `serve` time
- [x] Resolved: shared-tunnel process lifecycle — reference-counted against the local registry (`RunningApp.domain`), not tracked per-handle, since one `cloudflared` process now serves every app on a domain rather than one process per app; guarded by a per-domain file lock against concurrent `serve`/`stop` races (`tests/test_tunnel_byo.py`)

## §7 Metering

- [x] CLI: `sidepage usage <app-name>`
- [x] Resolved: connection/request-count is the permanent billing boundary
- [x] Core: `sidepage.core.usage_reporter.get_usage` — real, reads the proxy's persisted counts + registry `started_at` for uptime
- [x] Core: counts sourced from the local reverse proxy — real, incremented on every HTTP request/response and WS message

## §8 Token handling

- [x] CLI: `sidepage serve --token <value>` (plus `SIDEPAGE_TOKEN` env var, handled in core)
- [x] Core: `sidepage.core.token_runtime.resolve_token` — real
- [x] Core: `sidepage.core.token_runtime.write_runtime_file` / `read_runtime_file` — real, mode 0600 under `~/.local/state/sidepage/runtime`
- [x] Resolved: session validity until app stop, no rotation — real (cookie == token value, checked directly)
- [x] Clarified (v4, not new behavior): runtime file also holds broker-issued tunnel tokens, grouped by ephemeral lifecycle not function — `RuntimeToken.broker_tunnel_token` field exists; unused since brokered tunneling isn't implemented

## v4 §9 Secrets vault

*(section number possibly collides with v3 §9 local reverse proxy — see `docs/OPEN_QUESTIONS.md` #9)*

- [x] CLI: `sidepage secrets set <name>` (hidden prompt, confirmation prompt)
- [x] CLI: `sidepage secrets list`
- [x] CLI: `sidepage secrets remove <name>`
- [x] Core: `sidepage.core.secrets_vault.set_secret` — real
- [x] Core: `sidepage.core.secrets_vault.get_secret` — real, fails loud via `SecretNotFoundError`
- [x] Core: `sidepage.core.secrets_vault.list_secrets` — real, names only
- [x] Core: `sidepage.core.secrets_vault.remove_secret` — real
- [ ] Core: OS keychain backend (`keyring`) — **deliberately deferred**, not merely unbuilt: triggers an interactive macOS permission prompt unsafe for automated use. Same public API either way, so this can be layered in later without touching callers.
- [x] Core: encrypted-file fallback (`~/.config/sidepage/vault.enc` + `vault.key`, Fernet, both mode 0600) — real, and currently the *only* backend, not a fallback in practice
- [ ] Question: flat namespace vs. per-app scoping (`docs/OPEN_QUESTIONS.md` #10) — implemented as flat

## §9 (v3) Local reverse proxy

- [x] Core: `sidepage.core.reverse_proxy.start_proxy` — real, Starlette + uvicorn in a background thread
- [x] Core: `sidepage.core.reverse_proxy.check_upstream_ready` — real HTTP GET polling, not a bare TCP connect
- [x] Core: `sidepage.core.reverse_proxy.stop_proxy` — real
- [x] Core: auth gate page + session cookie — real
- [x] Core: startup holding page — real, auto-refreshing HTML, served while `check_upstream_ready` hasn't confirmed yet
- [x] Core: WebSocket proxying — real, via the `websockets` package as the outbound client; verified against Streamlit's `_stcore/stream` handshake
- [x] Core: streaming passthrough — real, `httpx`'s `aiter_raw` paired with forwarded original headers
- [ ] Design: graceful drain vs. hard kill on `stop` (deferred, `docs/OPEN_QUESTIONS.md` #2) — teardown is immediate, real, and intentional; a drain window isn't built

## §10 Inspection

- [x] CLI: `sidepage inspect [<app-name-or-url>]`
- [x] Resolved: no auth bypass for the local operator
- [x] Core: `sidepage.core.inspector.open_console` — real, generic HTTP/static console (REPL: get/post/put/patch/delete/head, header, replay, info, usage, help, quit)
- [x] Core: `resolve_target` — real, app name via `sidepage.core.registry` or raw `http(s)://` URL, `InspectorTargetError` otherwise
- [x] Core: auto-source credentials from token runtime file — real (`_auto_token`, falls back to none when no runtime file exists, i.e. `--auth open`)
- [x] Core: surface live usage counts — real, via `sidepage.core.usage_reporter.get_usage`
- [x] Core: replay the last request — real (`session.last_request`)
- [x] Directory-aware picker with no argument — real, lists `sidepage.core.registry.list_running()`
- [x] Verified end to end against the static-site fixture, including auth auto-sourcing (`tests/test_inspector.py`)
- [ ] **MCP tool browsing — deliberately parked, not built this pass.** Schemas, `tools/list`, `tools/call` over the MCP JSON-RPC/streamable-HTTP transport. Needs a real MCP client (decision pending: official `mcp` SDK vs. hand-rolled JSON-RPC — see `docs/OPEN_QUESTIONS.md` #14) and a real MCP server test fixture (neither existing fixture is an MCP server). The "Postman-for-MCP" framing in the spec is this piece specifically, not the generic HTTP console above.

### (no v3 section — see `docs/OPEN_QUESTIONS.md` #7)

- [x] CLI: `sidepage ls [--scope <scope>] [--mine]`
- [x] CLI: `sidepage status <app-name>`
- [x] Core: `sidepage.core.registry.list_running` — real, backs `ls` (not `directory_client.list_entries`, which stays unimplemented — there's no cloud directory, only this machine's registry)
- [x] Core: `sidepage.core.registry.get` + a live reachability check — real, backs `status`
- [~] `--scope`/`--mine` on `ls` — accepted, but noted as not meaningful yet rather than silently ignored (no cloud directory to filter against)

## §11 Static site serving

- [x] Core: `sidepage.core.static.validate_static_root` — real, missing `index.html` → `StaticServeError`
- [x] Core: mounted directly in the proxy process via Starlette `StaticFiles` — real, no extra hop
- [x] Verified end to end against the real static-site fixture (`tests/fixtures/static-site`)

## §12 Notebook serving

- [x] CLI: `sidepage serve notebook.ipynb --auth token` (via generic `serve`, no dedicated flag)
- [x] Core: `.ipynb` recognized by `detect_target_kind` (so `--type` reporting stays honest)
- [ ] Core: `sidepage.core.notebook.build_jupyter_launch_command` — not implemented, not one of the two prioritized targets
- [ ] Core: `sidepage.core.notebook.verify_proxy_fronted` (safety check, not yet designed)
- [ ] Design: `juv` for standalone `.ipynb` dependency resolution — evaluation only, not committed

## §13 Account & login

- [x] CLI: `sidepage login`
- [x] CLI: `sidepage account status`
- [x] CLI: `sidepage account domain set <domain> --api-token-name <name>` (v4 delta: a single token-name flag, superseding the earlier two-flag design — see `docs/OPEN_QUESTIONS.md` #12)
- [ ] Core: `sidepage.core.account.login` — not implemented, no account backend
- [ ] Core: `sidepage.core.account.current_account` — not implemented
- [x] Core: `sidepage.core.account.configure_domain` (v4: takes one vault secret name, not raw credentials or two names) — real. Provisions via `tunnel_manager.provision_byo_domain`, stores the returned run-token under `internal_tunnel_token_name(domain)`, persists the result to `account.json`. Idempotent — a domain already on file is returned as-is rather than re-provisioned. Raises `TunnelProvisioningError` (carrying the orphaned `tunnel_id` and the internal secret name) if the tunnel was created but the vault write then failed, since the run-token can't be fetched a second time
- [x] Core: `sidepage.core.account.internal_tunnel_token_name` — real, deterministic (`cf-tunnel-token::<domain>`) — reserved-prefixed so it's unambiguous in `sidepage secrets list`; `sidepage.commands.account.domain_set` always logs it on success, and it's embedded in the error message on failure
- [x] Core: `sidepage.core.account.get_default_domain` — real, used by `serve`'s `_validate_supported` to resolve `--domain` against the persisted config

## §14 Ecosystem integration

- [x] Core: `sidepage.core.ecosystem.resolve_python_runner` — real: always `uv run`, layering `--with-requirements requirements.txt` (if present) with `--with <package>` for each detected launcher package (multiple, e.g. FastAPI needs both `fastapi` and `uvicorn`) — never trusts an existing `.venv` directly (fixed a real bug where a hand-installed, undeclared dependency in a project's own `.venv` silently produced `ModuleNotFoundError`; see `docs/OPEN_QUESTIONS.md` #15 for the residual gap)
- [x] Regression tests (`tests/test_ecosystem.py`) — pin that an existing `.venv` is never used directly, and that the detected launcher package always layers on top of `requirements.txt`
- [ ] Core: `sidepage.core.ecosystem.detect_js_package_manager` (npm/yarn/pnpm lockfile detection) — not implemented, no JS target prioritized

## §15 Parked for future discussion

- [ ] Design: MCP-specific auth model (not started)
- [x] Resolved for the common case: stdio-transport MCP servers — `serve` bypasses the script's own `.run()` entirely and serves `.streamable_http_app()`/`.http_app()` directly, so a script only ever wired for stdio is still reachable over real HTTP (see §1/§2 above, `docs/OPEN_QUESTIONS.md` #4). Still genuinely unsupported: a server built directly on the low-level `mcp.server.lowlevel.Server` API with no high-level `FastMCP`/`MCPServer` ASGI equivalent at all — there's no app for `serve` to bypass into there

## §16 Out of scope for this binary

- [ ] Orchestrator (fleet/process management) — separate product by design, not tracked here beyond noting it's not started

## Parked / unclear status (not numbered in v3)

- [ ] Guardrails & pre/post-processing — kept as a placeholder (`serve --guardrail`,
      `sidepage.core.guardrail`); v3 doesn't mention this section at all. See
      `docs/OPEN_QUESTIONS.md` #6. `serve --guardrail` is real *rejection* (clear
      message, checked before target detection) even though the feature isn't built.

## Tooling & docs

- [x] uv-managed project (`pyproject.toml`, `uv.lock`, `.python-version`)
- [x] Full CLI command tree wired (Typer), every command reachable via `--help`
- [x] Shared output helpers (`sidepage.output`)
- [x] Fast in-process smoke tests (`tests/test_cli_smoke.py`) — argument parsing, help text, wiring
- [x] Real subprocess integration tests (`tests/test_serve_integration.py`) — static site + Streamlit fixtures, auth gate, `--env` injection, stop/teardown
- [x] Real subprocess integration tests for FastAPI (`tests/test_serve_fastapi.py`) — port override, `/docs`, `/openapi.json`, POST endpoint, auth gate on docs
- [x] Real subprocess integration tests for MCP (`tests/test_serve_mcp.py`) — real `initialize` handshake and `tools/call` round trip over Streamable HTTP through the actual proxy, against a fixture deliberately left stdio-only in its own `__main__`; auth gate on `/mcp`
- [x] Real integration tests for `inspect` (`tests/test_inspector.py`) — target resolution, auth auto-sourcing, request execution against the static-site fixture
- [x] Fast unit tests for dependency resolution (`tests/test_ecosystem.py`) — pins the `.venv`-trust regression fix
- [x] Fast unit tests for code-launcher detection (`tests/test_target.py`) — Streamlit/FastAPI/MCP import-scan detection for every recognized MCP import style, FastAPI-over-MCP precedence when MCP is mounted inside a FastAPI app, generic-`$PORT` fallback, app-variable extraction and its defaults
- [x] Fast unit tests for BYO-domain tunneling (`tests/test_tunnel_byo.py`) — token decode (pure); mocked Cloudflare API/`cloudflared` subprocess coverage of `provision_byo_domain` and `open_byo_tunnel` (create, update, stable hostname, missing-zone error, ingress upsert idempotency, two apps sharing one ingress config); shared-process lifecycle (spawned once for two apps, killed only on the last app's teardown, stale-pidfile recovery); a real cross-thread `_domain_lock` mutual-exclusion check
- [x] Fast unit tests for the secrets vault (`tests/test_secrets_vault.py`) — round-trips `set_secret`/`get_secret` through the real encrypt-write/decrypt-read path (no in-memory cache to fake it), edge-case values (empty string, unicode, large), confirms the on-disk file doesn't contain the plaintext, multi-secret isolation, key-file reuse across writes, remove-then-get raising `SecretNotFoundError`
- [x] Real runtime dependencies: Starlette, uvicorn, httpx, `websockets`, `cryptography` (not just named-but-uninstalled)
- [x] README with full command reference, architecture note, real-vs-stubbed project status, and project layout
- [x] Open questions doc (`docs/OPEN_QUESTIONS.md`), split into resolved (v3) / still open, plus v4-specific gaps
- [x] This checklist, updated for the real-implementation pass
- [ ] CI workflow
