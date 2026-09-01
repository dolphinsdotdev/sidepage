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
- [x] Core: `sidepage.core.target.detect_target_kind` (static/code/notebook — all three servable now)
- [x] Core: `sidepage.core.target.detect_code_launcher` — Streamlit, FastAPI, Python MCP servers, and Gradio via import scan, else generic `$PORT`
- [x] Core: `sidepage.core.target.allocate_port`

## §2 Serving

- [x] CLI: `sidepage serve <target> [--type] [--name] [--domain] [--auth] [--anon] [--token] [--env]... [--scope] [--guardrail] [--timeout] [--idle-timeout] [--peer]...` (last three are v5, see below)
- [x] CLI: `sidepage stop <app-name>`
- [x] Core: `sidepage.core.process.serve` — the biggest real module; orchestrates target detection, port allocation, subprocess launch, proxy, tunnel, registry
- [x] Core: `sidepage.core.process.stop` — SIGTERM to the registered pid, routed through the same clean teardown as Ctrl+C
- [x] Core: port injection — `--server.port` flag for Streamlit, `uvicorn <module>:<app> --port` for FastAPI (bypasses a script's own `__main__` block, which real FastAPI apps often use to hardcode a port), `uvicorn <module>:<var>.<app-method> --factory --port` for MCP (bypasses the script's own `.run()` call too — see below), `$PORT` env var for the generic fallback
- [x] Core: immediate tunnel/proxy/subprocess teardown on Ctrl+C / `stop` (via a SIGTERM handler that raises `KeyboardInterrupt`)
- [x] Core: `--domain` (real, once configured — see §6/§13), non-`local` `--scope`, `--auth network`/`oauth`, `--guardrail` (the latter three still rejected up front with a clear message via `_validate_supported`, not silently ignored)
- [x] Core: `notebook` targets — real, see §12 below
- [x] CLI (v4): `--env <SECRET_NAME>` repeatable, vault injection
- [x] Core (v4): `serve` resolving each `env_secrets` name via `secrets_vault.get_secret`, fail loud (`SecretNotFoundError`) on miss
- [x] Verified end to end: static-site fixture and Streamlit fixture, both via real subprocess CLI invocation (`tests/test_serve_integration.py`)
- [x] Core: FastAPI launcher — `sidepage.core.target.detect_asgi_app_variable` (scans for `<name> = FastAPI(...)`, defaults to `app`), launched via real `uvicorn` CLI, not by running the script directly
- [x] Verified end to end: `tests/fixtures/fastapi-app` (real subprocess, `tests/test_serve_fastapi.py`) — port override, `/docs`, `/openapi.json`, a real POST endpoint, and the auth gate covering `/docs` too. Also manually verified against a real, non-fixture FastAPI app (local MLX LLM inference server) — real chat completion request succeeded through the proxy.
- [x] FastAPI `/docs`/`/redoc`/`/openapi.json` — no extra work needed, the generic HTTP proxy passthrough already covers them; `serve` prints the `/docs` URL when a FastAPI target is detected
- [x] Core: MCP (Python) launcher — `sidepage.core.target.detect_mcp_package`/`detect_mcp_app_variable` recognize two packages (official `mcp` SDK — `FastMCP` or, in the current major version, `MCPServer`, both exposing `.streamable_http_app()`; third-party `fastmcp` — `FastMCP`, exposing `.http_app()`), launched via real `uvicorn --factory <module>:<var>.<method>`, never by running the script or calling `.run()` directly. Because that entrypoint is never executed, a script whose own `__main__` only wires up the stdio transport (the default for both packages) still ends up served over real Streamable HTTP — resolves the practical case of `docs/OPEN_QUESTIONS.md` #4. Both packages' current API shape was verified live against their actually-resolvable releases, not assumed
- [x] Core: MCP/FastAPI detection precedence — a script that mounts an MCP server inside a FastAPI app (`app.mount("/mcp", mcp.streamable_http_app())`) is still detected as FASTAPI, not MCP, since the FastAPI app is the real top-level ASGI app and already serves the MCP sub-mount; only a standalone MCP script (no FastAPI import) is detected as MCP
- [x] Core: Gradio launcher — detected by import scan, launched via `uvicorn --factory` against a generated wrapper (`sidepage.core.process._GRADIO_WRAPPER_SOURCE`) that neutralizes `gradio.Blocks.launch` *before* importing the target, then mounts the captured Blocks through Gradio's own `mount_gradio_app()`. Needed more than MCP's bypass does: the canonical Gradio script calls `demo.launch()` unguarded at module level, so a plain import would block forever. The same patch disarms a hardcoded `server_port=`, `share=True`, and `ssr_mode=True`. `GRADIO_SERVER_PORT` injection and `routes.App.create_app` were both tried and rejected on evidence — see `docs/OPEN_QUESTIONS.md` #18
- [x] Core: Gradio/FastAPI detection precedence — a script that mounts its Blocks onto its own FastAPI app is detected as FASTAPI, same rule and same reason as the MCP case above
- [x] Core: Gradio needs **no** CORS/Host relaxation, unlike Streamlit/Jupyter/MCP — verified, and the reasoning is recorded in `process.py` so the absent bypass doesn't read as an oversight
- [x] Verified end to end: `tests/fixtures/gradio-app` (module-level unguarded `demo.launch(server_port=8123)`) and `tests/fixtures/gradio-guarded-app` (launch behind `__main__`, plus a decoy second Blocks), real subprocess CLI invocation (`tests/test_serve_gradio.py`) — UI render, a real prediction round trip through the actual reverse proxy, the hardcoded port confirmed never bound, and wrapper cleanup on teardown. Against gradio 6.26.0 only
- [x] Verified end to end: `tests/fixtures/mcp-app` (real `mcp` SDK server, `__main__` deliberately left stdio-only), real subprocess CLI invocation (`tests/test_serve_mcp.py`) — a genuine `initialize` handshake and `tools/call` round trip through the actual reverse proxy (not talking to the wrapped process directly), plus the auth gate covering `/mcp` too. Fast, no-subprocess detection-logic coverage in `tests/test_target.py`
- [x] Verified end to end (v5): `tests/test_serve_v5.py` — real subprocess coverage of `--timeout`/`--idle-timeout` auto-teardown (including idle-timeout resetting under continuous traffic), lazy start for code targets (a marker file proves `subprocess.Popen` doesn't run until the first request), and `--peer` (boot-time env injection, live `GET /.sidepage/peers.json`, fail-loud on an unresolvable peer); fast in-process coverage for the flag-validation rejections (negative/zero timeout values, malformed `--peer` spec, `--peer` on a static target)

## `sidepage proxy` (not in spec)

*(Not part of the v1/v3/v4/v5 spec numbering — `serve` with target
detection and subprocess launch removed, for wrapping a service the user
already has running instead of one sidepage starts itself. Reuses
`serve`'s validation/tunnel/registry/auth machinery rather than
duplicating it.)*

- [x] CLI: `sidepage proxy --port <n> [--name <app-name>] [--domain <domain> | --anon] [--auth open|token] [--token <value>] [--scope local] [--timeout <seconds>] [--idle-timeout <seconds>]`
- [x] CLI: `--name` optional — defaults to `proxy-<port>` for plain local use; required (rejected loud, before any network activity) when `--domain`/`--anon` is set, since it becomes part of the public hostname/registry entry there
- [x] CLI: `--type`/`--env`/`--guardrail`/`--peer` declared on the command but rejected outright with a specific, actionable message each — not silently accepted and ignored, since all four are subprocess-injection concepts that don't apply to a process `proxy` never launches or owns
- [x] Core: `sidepage.core.process.ProxyConfig`/`proxy` — shares `_validate_common` (extracted out of `serve`'s own `_validate_supported`) for domain/anon exclusivity, scope, auth tier, timeout/idle-timeout, and `--domain` resolution; skips target detection and subprocess launch entirely, `config.port` assumed already listening on `127.0.0.1`
- [x] Core: teardown asymmetry vs `serve`, by design — `_teardown()` never touches the wrapped service; Ctrl+C/`sidepage stop <name>` tear down only the proxy, the tunnel, and the registry entry. Surfaced as explicit runtime `warn()` lines at both startup and the moment of teardown, not just `--help` prose
- [x] Core: registered with `target_kind="external"` — a plain string on `RunningApp` (not the `TargetKind` enum), so `ls`/`status`/`stop`/`inspect` all work unchanged with zero code changes needed there
- [x] Shares `serve`'s `stop` command rather than declaring its own — `sidepage.core.process.stop` is already target-agnostic, keyed on the registry pid
- [x] `--help` documents, loud and specific rather than buried: teardown behavior; the localhost-trust security warning (every proxied request arrives from `127.0.0.1`, defeating an app's own "only localhost may reach this" checks — Flask/Werkzeug's interactive debugger named explicitly as a known RCE case); a one-time-fix table per framework (Django/Flask/FastAPI/Express/Rails/Vite) for Origin/Host/CSRF; `--anon`-specific wildcard-allowlist guidance (hostname changes every run); OAuth/SSO's structural incompatibility with `--anon`; HTTP/1.1+WS-only protocol scope
- [x] Verified end to end: `tests/test_proxy.py` (10 tests, real subprocess + fast in-process) — round trip through the proxy, forwarded headers actually landing at the upstream, `--auth token` gate, `stop` tearing down the proxy while leaving the wrapped service running (the one behavior genuinely new vs. `serve`), all four flag rejections, domain/anon validation, and a BYO-domain tunnel-wiring test reusing `test_tunnel_byo.py`'s fake-Cloudflare harness
- [x] Verified end to end against real frameworks, not just a stub echo server: `tests/test_proxy_frameworks.py` (8 tests) against `tests/fixtures/flask-app` and `tests/fixtures/vite-app` — see the reverse-proxy entries in §9 below and the Tooling & docs section for what each one proved
- [ ] Not attempted: detecting Vite's specific `allowedHosts` 403 response and printing an inline, actionable hint at the moment it happens (vs. the static `--help` table today) — discussed and agreed as the right compromise over either lying about `Host` per-framework or editing the user's `vite.config.js`, not yet built
- [ ] Investigated, not resolved: HMR/WebSocket live-reload for a Vite target doesn't reliably work over `--anon` (Cloudflare Quick Tunnel), even though the initial page/asset load does and BYO-domain doesn't have this problem. Ruled out via direct reproduction: sidepage's own `Host` handling isn't the cause (a raw WS upgrade with the tunnel hostname succeeds against sidepage directly, and `curl` reproducing the browser's exact handshake — same path, token, Host, Origin — succeeds end to end through the real Cloudflare edge + `cloudflared` + sidepage + Vite chain). The gap is specifically in how a real browser's native `WebSocket` negotiates against Cloudflare's Quick Tunnel edge, past the handshake — not isolated further from the command line

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
- [x] Core: `sidepage.core.reverse_proxy.check_upstream_ready` — real HTTP GET polling, not a bare TCP connect; tries `127.0.0.1` first, falls back to `[::1]` (IPv6 loopback) if that never answers, resolved once and reused for all subsequent proxying (`UpstreamAddress`) — added after finding a bare `npm run dev` Vite dev server binds IPv6 loopback only, and `sidepage proxy` (unlike `serve`, which always pins `127.0.0.1` explicitly for its own launchers) has no control over how an already-running service was started
- [x] Core: `sidepage.core.reverse_proxy.stop_proxy` — real
- [x] Core: auth gate page + session cookie — real
- [x] Core: startup holding page — real, auto-refreshing HTML, served while `check_upstream_ready` hasn't confirmed yet
- [x] Core: WebSocket proxying — real, via the `websockets` package as the outbound client; verified against Streamlit's `_stcore/stream` handshake
- [x] Core: streaming passthrough — real, `httpx`'s `aiter_raw` paired with forwarded original headers
- [x] Core: Caddy-style forwarded headers (`sidepage.core.reverse_proxy._forwarded_headers`) — real `Host`/`X-Forwarded-Host`/`X-Forwarded-For`/`X-Forwarded-Proto` on the HTTP path; the WS path forwards `X-Forwarded-Host` but deliberately not a literal `Host` override — verified live that Jupyter/Tornado rejects a forwarded real hostname on the WS handshake more strictly than on plain HTTP, breaking the existing notebook fixture until this was scoped HTTP-only. Benefits `serve`'s CODE/NOTEBOOK targets as much as `proxy` — not proxy-specific
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
- [x] Core: `sidepage.core.notebook.build_jupyter_launch_command` — real: `uv run --with jupyterlab jupyter lab <notebook> --port <port> --no-browser --ip=127.0.0.1`, Jupyter's own token/password auth disabled (the reverse proxy is the auth boundary, same as every other launcher), plus `--ServerApp.allow_origin=* --ServerApp.disable_check_xsrf=True`
- [x] Resolved (verified live, not assumed): Jupyter Server rejects cross-origin requests/WebSocket upgrades by default, comparing `Origin` against its own `Host` — through the proxy, the browser's `Origin` is the *proxy's* address, not Jupyter's real (different) upstream port, so this rejects out of the box without the `allow_origin`/`disable_check_xsrf` flags above. Reproduced the rejection and confirmed the fix with a real kernel + a real `execute_request` over a WebSocket carrying a deliberately-mismatched origin before committing to the launch command
- [x] Resolved: `sidepage.core.notebook.verify_proxy_fronted` placeholder removed — the flagged risk ("what if this launch command runs outside the proxy") is mitigated by `--ip=127.0.0.1` in the launch command itself, which is fully sidepage-controlled by construction; no separate runtime check adds anything beyond that, same guarantee every other code launcher already has
- [x] Resolved: no standalone-vs-project dependency distinction needed — `sidepage.core.ecosystem.resolve_python_runner` already degrades to a bare `uv run --with jupyterlab` with no `requirements.txt` present, same as any other code target; the `juv` evaluation this line used to flag is moot
- [x] Verified end to end: `tests/fixtures/notebook-app` (real subprocess, `tests/test_serve_notebook.py`) — Lab UI reachable, a genuine kernel-execution round trip (start kernel, open its WebSocket, run code, get real stdout back) driven entirely through the actual sidepage proxy port with the proxy's own origin, and the auth gate covering the Lab UI too

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

## v5: `docs/SPEC_V5_DRAFT.md`

*(Numbering continues from §16, per that document's own convention. Only
§20, §21 Tier 1, and `--peer` are built this pass — §17–19, §21 Tier 2,
§22, and §23 remain draft-only proposals, not tracked here as done/not
done since nothing about them has been implemented.)*

### §20 Timeout / auto-teardown

- [x] CLI: `sidepage serve --timeout <seconds>` / `--idle-timeout <seconds>`
- [x] Core: `sidepage.core.process.ServeConfig.timeout`/`idle_timeout`, validated (positive-only) in `_validate_supported`
- [x] Core: both checked once a second inside `serve`'s existing blocking loop, exiting through the same `_teardown()` Ctrl+C/`stop` already use — no new teardown path, no drain window (same immediate-kill semantics as everything else)
- [x] Core: `sidepage.core.reverse_proxy.ActivityTracker`/`ActivityMiddleware` — "time of last proxied request or WS message," touched on every HTTP request and (per-message, not just per-connection) WS message, exposed as `ProxyHandle.activity`; backs `--idle-timeout`
- [x] Composable: both flags can be passed together; independent conditions, either can fire first

### §21 Lazy start (Tier 1 only — Tier 2/scale-to-zero not attempted)

- [x] Core: `sidepage.core.reverse_proxy.start_proxy(..., start_upstream=...)` — when given, `_build_proxy_app` defers calling it until the first inbound HTTP request or WS connect, behind a start-once lock (`_ensure_started`); the pre-existing `ready`-Event/holding-page path is reused verbatim, not reimplemented
- [x] Core: `sidepage.core.process.serve` — CODE/NOTEBOOK's `subprocess.Popen` moved into a closure passed as `start_upstream`; no flag, automatic for both target kinds
- [x] Scope respected: STATIC untouched (already in-process, no subprocess to defer)
- [ ] Tier 2 (scale-to-zero: respawn after an idle-kill instead of tearing the whole `serve` invocation down) — **deliberately not attempted**, per the spec's own flag that it's a materially bigger, separate decision (real state-loss risk for a live Jupyter kernel in particular)

### `--peer <role>=<app-name>`

- [x] CLI: `sidepage serve --peer <role>=<app-name>` (repeatable), parsed by `sidepage.commands.serve._parse_peer` — fails loud on a malformed spec (missing `=`, empty role/name)
- [x] Core: `sidepage.core.exceptions.PeerNotFoundError` (new)
- [x] Core: `sidepage.core.registry.resolve_peer_url` — `get(app_name).tunnel_url or .url` against *live* registry state; raises `PeerNotFoundError` for a peer that isn't currently running
- [x] Core: boot-time injection — `sidepage.core.process.serve` resolves each configured peer once and injects `SIDEPAGE_PEER_<ROLE>_URL` into the CODE/NOTEBOOK subprocess env, same mechanism `--env` already uses for vault secrets
- [x] Core: live re-resolution — `GET /.sidepage/peers.json`, one more route in `_build_proxy_app`'s own table, so it inherits the app's `--auth` gate automatically with no separate gate to build; re-resolves on every request, so a peer that restarts mid-session with a fresh anon-tunnel URL is never stale the way the boot-time env var would be
- [x] Resolved (own design decision, fail-loud posture): `--peer` on a `static` target is rejected up front (`ValueError`, before target detection's result is even used further) — there's no subprocess to inject into and the live route only exists in the code/notebook proxy app, so silently accepting the flag there would have been a silent no-op
- [ ] Not attempted: `--peer` support for `static` targets (would need its own client-fetchable endpoint design — see the related, still-draft §22 sibling-discovery proposal in `docs/SPEC_V5_DRAFT.md`)

## Registry spec v2: local app registry

*(Not part of the v3/v4 spec numbering — a separate document,
`sidepage-registry-spec.md`.)*

- [x] CLI: `sidepage app register "<invocation>" <app-name>`
- [x] CLI: `sidepage app list`
- [x] CLI: `sidepage app show <app-name> [--with "<preview flags>"]`
- [x] CLI: `sidepage app unregister <app-name>`
- [x] CLI: `sidepage serve <app-name> [overrides...]` — `serve`'s existing positional `target` argument, extended: resolves against the registry first, falls back to a literal path unchanged if no such name is registered
- [x] Core: `sidepage.core.app_registry` — register/get/list_registered/unregister backed by `registry.json` (`sidepage.config.settings.app_registry_file`), distinct from `sidepage.core.registry`'s *running*-apps tracking
- [x] `serve --autoregister` — saves the running invocation to the app registry once the app is actually serving (never before: a config that fails to start is never persisted). Pre-flight runs before any port is allocated: an identical existing entry is a no-op with a `reusing existing app, next time use sidepage serve <app-name>` warning, a *different* entry under the same name is refused outright, and every flag the registry can't hold (`--token`/`--timeout`/`--idle-timeout`/`--peer`/`--qr`) is reported by name rather than silently dropped. Rejected on `proxy` with an explanation, like every other serve-only flag there
- [x] `--pwa`/`--pwa-*` are part of `AppRegistration` — stored, and merged as one unit (any explicit `--pwa*` this invocation replaces the registered PWA config wholesale). Icon/manifest paths stored absolute, same rule as `target`. Entries written before the `pwa` key existed still load, as PWA-off
- [x] `sidepage.core.app_registry.same_config` — registration equality ignoring `registered_at` and normalizing an unset `name` to the registry key, so `serve <app-name> --autoregister` against an app registered without `--name` isn't misread as a conflict
- [x] Verified: `tests/test_autoregister.py` — all three pre-flight outcomes, unregisterable-flag reporting, PWA round trip/merge/back-compat, `proxy` rejection, plus a real subprocess proving the entry is written only once the app is serving and not at all when startup fails
- [x] Resolved (spec's core design goal, verified working): registration is parsed with `serve`'s own real Click command (`sidepage.commands.app_registry._make_serve_context`, `Command.make_context`), not a hand-maintained second parser — a new `serve` flag needs zero changes here to become registerable
- [x] Resolved (hard rule, enforced at register time): a literal `--token <value>` in a registration invocation is rejected outright, before `sidepage.core.app_registry.register` is ever called; `--env <NAME>` is stored (a vault reference, not a value)
- [x] Resolved: merge semantics — an explicit command-line flag at `serve <app-name>` time overrides the registered value for that one invocation only (`sidepage.commands.app_registry.merge_with_registered`, using `ctx.get_parameter_source` to distinguish "explicitly passed" from "just the flag's natural default" — not a `None`-sentinel restructure of every flag); the stored registration itself is never mutated by a one-off override
- [x] Resolved (own design decision, beyond what the spec pinned down): the registered app's runtime `--name` defaults to the registry key itself when neither the registration nor the serving invocation set one explicitly — not the served file's stem, which could be a much less meaningful name for `sidepage ls`/`sidepage stop`
- [x] Resolved (own design decision): `target` is stored as an absolute, resolved path, not whatever relative string was typed at registration — so `serve <app-name>` works regardless of the shell's cwd later
- [x] Resolved (own design decision): re-registering an already-registered name is rejected (matching `sidepage.core.process.serve`'s own "already registered" stance for the *running*-apps registry), not a silent overwrite; `unregister`ing an unknown name is likewise rejected, not a no-op — both deliberately stricter than `sidepage.core.secrets_vault`'s idempotent-removal stance, since a small registry of user-chosen names is typo-prone enough that silent success would hide a mistake
- [x] Found and recorded (not assumed): this installed Typer version (0.27.1) fully vendors its own fork of Click (`typer._click`) rather than using the separately-installed real `click` package for command/context internals — `Command.make_context(...)` errors are `typer._click.exceptions.*`, unrelated to `click.exceptions.*`. See `sidepage.commands.app_registry`'s module docstring for why parsing failures are caught as a broad `Exception` rather than a specific Click exception type as a result
- [x] Verified end to end: `tests/test_app_registry.py` (fast, in-process — registry round trip, CLI wiring, `--token` rejection, auto-detected `--type` at registration, `show --with` preview not mutating the base) and `tests/test_serve_registry.py` (real subprocess — a registered app's stored `--auth token` actually gates it, a CLI override actually overrides it for one run and leaves the registration untouched, an unregistered name falls back to the pre-registry literal-path behavior unchanged)

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
- [x] Real subprocess integration tests for notebooks (`tests/test_serve_notebook.py`) — Lab UI reachable, a real kernel-execution round trip driven through the actual proxy port with the proxy's own (mismatched-vs-upstream) origin, auth gate on the Lab UI
- [x] Real subprocess integration tests for the app registry (`tests/test_serve_registry.py`) — a registered app's stored auth tier actually gates it, a CLI override actually overrides it for one run without mutating the registration, an unregistered name falls back to the pre-registry literal-path error
- [x] Fast in-process tests for the app registry (`tests/test_app_registry.py`) — `sidepage.core.app_registry` round trip (register/get/list/unregister, duplicate rejection, missing-name rejection), the stored JSON shape matching the registry spec's field names, the CLI's `--token` rejection and nonexistent-target rejection, `--type` auto-detection at registration, and `show --with` previewing without mutating the base
- [x] Real integration tests for `inspect` (`tests/test_inspector.py`) — target resolution, auth auto-sourcing, request execution against the static-site fixture
- [x] Real subprocess + fast in-process tests for v5 (`tests/test_serve_v5.py`) — `--timeout`/`--idle-timeout` auto-teardown (including idle-reset-under-traffic), lazy start (marker-file proof `subprocess.Popen` is deferred to the first request), `--peer` (env injection, live `peers.json`, fail-loud on a missing peer), and flag-validation rejections for all three
- [x] Fast unit tests for dependency resolution (`tests/test_ecosystem.py`) — pins the `.venv`-trust regression fix
- [x] Fast unit tests for code-launcher detection (`tests/test_target.py`) — Streamlit/FastAPI/MCP/Gradio import-scan detection for every recognized MCP import style, FastAPI-over-MCP and FastAPI-over-Gradio precedence when either is mounted inside a FastAPI app, generic-`$PORT` fallback, app-variable extraction and its defaults
- [x] Fast unit tests for BYO-domain tunneling (`tests/test_tunnel_byo.py`) — token decode (pure); mocked Cloudflare API/`cloudflared` subprocess coverage of `provision_byo_domain` and `open_byo_tunnel` (create, update, stable hostname, missing-zone error, ingress upsert idempotency, two apps sharing one ingress config); shared-process lifecycle (spawned once for two apps, killed only on the last app's teardown, stale-pidfile recovery); a real cross-thread `_domain_lock` mutual-exclusion check
- [x] Fast unit tests for the secrets vault (`tests/test_secrets_vault.py`) — round-trips `set_secret`/`get_secret` through the real encrypt-write/decrypt-read path (no in-memory cache to fake it), edge-case values (empty string, unicode, large), confirms the on-disk file doesn't contain the plaintext, multi-secret isolation, key-file reuse across writes, remove-then-get raising `SecretNotFoundError`
- [x] Real subprocess + fast in-process tests for `sidepage proxy` (`tests/test_proxy.py`, 10 tests) — round trip through the proxy, forwarded headers landing at the upstream, `--auth token` gate, teardown leaving the wrapped service running, all four declared-but-rejected flags (`--type`/`--env`/`--guardrail`/`--peer`), domain/anon validation, and BYO-domain tunnel wiring (mocked Cloudflare harness, mirroring `test_tunnel_byo.py`'s approach)
- [x] Real subprocess tests for `sidepage proxy` against real frameworks, not just a stub echo server (`tests/test_proxy_frameworks.py`, 8 tests; `tests/fixtures/flask-app`, `tests/fixtures/vite-app`) — Flask's `request.host` correct with zero app-side config, `X-Forwarded-Proto`/`.scheme` genuinely needing `ProxyFix` to be trusted; the localhost-trust security caveat demonstrated for real via a `request.remote_addr`-gated route; Vite's `allowedHosts` 403 and its wildcard fix reproduced against a real Vite dev server; the IPv6 loopback fallback proven against a bare `npm run dev`
- [x] `sidepage_proxy_new` added to the `sidepage-control` MCP fixture (`tests/fixtures/mcp-control-app/app.py`), mirroring `sidepage_serve_new` (same detach-and-poll-the-log pattern, building `--port`/`--name` argv instead of a positional target) — compiles clean, not yet exercised live: the MCP server connected to this session is spawned directly by the harness's own config (`uv run --with mcp app.py`), outside sidepage's own registry, so picking up the new tool needs a reconnect triggered from outside this session
- [x] Packaged Claude Skill for agents/harnesses (`skills/sidepage-serve/`) — wraps `serve`/`proxy`'s blocking-forever behavior with a background-and-poll script pair; verified end to end (`tests/test_skill_scripts.py`, real subprocess against `start_site.sh new|registered|proxy` + `stop_site.sh`) plus a soft drift check that its documented flags still resolve on the real CLI (`tests/test_skill_docs.py`)
- [x] Real runtime dependencies: Starlette, uvicorn, httpx, `websockets`, `cryptography` (not just named-but-uninstalled)
- [x] README with full command reference, architecture note, real-vs-stubbed project status, and project layout
- [x] Open questions doc (`docs/OPEN_QUESTIONS.md`), split into resolved (v3) / still open, plus v4-specific gaps
- [x] This checklist, updated for the real-implementation pass
- [ ] CI workflow


## Remote sources — `sidepage pull` (not a numbered spec section)

- [x] `sidepage pull <source>` — Hugging Face Spaces only; GitHub/MCP/local paths recognized and refused with a specific message rather than guessed at. A bare `<owner>/<name>` is refused as ambiguous with GitHub's identical shorthand
- [x] Metadata-first ordering: one API call resolves sdk/sdk_version/app_file/commit/hardware/file-list-with-sizes, so Docker Spaces, GPU and ZeroGPU tiers, and private/gated repos are refused **before** any content is downloaded
- [x] Hardware gating is an allowlist (`cpu-*`), not a denylist — a tier that doesn't exist yet warns rather than passing silently. Verified live: `cpu-basic`/`cpu-upgrade`/`cpu-xl` clean, `zero-a10g` warned
- [x] The hardware gate is **a caution with an override (`--ignore-hardware`), not a capability check** — corrected after verifying that `@spaces.GPU` is inert off Hugging Face (installed `spaces` outside any HF environment, decorated a function, called it: returned normally). The tier is the owner's hosting choice, not a statement about the code. Structural refusals (`sdk: docker`, private/gated) deliberately have no override, and an overridden pull keeps the caveat in its printed plan
- [x] Files fetched over HTTPS at a pinned sha, not `git clone` — needs neither `git` nor `git-lfs`, and every LFS file is verified against the sha256 the API declared before the download. Chosen after reproducing the alternative's failure: a clone without `git-lfs` *succeeds* and silently leaves weights as ~130-byte pointer files
- [x] `--dry-run` resolves and prints the full plan (including total download size) with nothing fetched and nothing registered; `--json` emits one parseable line with repo-authored strings namespaced under `source`
- [x] Provenance in the registry (`sidepage.core.app_registry.AppSource` + `sidepage.core.hf.HfSpaceConfig`) — source URL, commit, managed-directory flag, requested env names, and an HF-specific manifest block rather than flattening HF's vocabulary into generic fields
- [x] Manifest paths are treated as attacker-controlled: `app_file` is refused if absolute, containing `..`, or resolving outside the app directory once symlinks are followed
- [x] Serve-time trust gate — prints the plan and requires confirmation before executing downloaded code; approval is recorded **per commit**, so a `pull` bringing different code re-arms it. Refuses outright with no terminal to prompt at; `--trust-remote-code` is the explicit waiver
- [x] `sidepage app delete <name>` — removes the registry entry and the managed source tree, refuses while the app is running, requires confirmation (`--yes` to skip), and **never** removes files for an app registered against a path the user already had. `unregister` keeps its files-untouched meaning
- [x] Verified: `tests/test_pull.py` (58 tests, offline against a stubbed Hub shaped like real responses) plus a live end-to-end pull of `Anvarbekkk/real-time-stock-predictor` — real weights, real refusals, real gate
- [x] Gradio wrapper runs the target as `__main__` (`runpy.run_path`), matching what Hugging Face does to an `app_file` — handles the *factory* script shape (`build_ui().launch()` inside a guard) that defeats both an import and a namespace scan. Found by pulling a real Space and watching it fail; `tests/fixtures/gradio-factory-app`
- [x] Upstream readiness poll is unbounded on the lazy-start path — a fixed 20s deadline meant any app whose first run installed a large dependency tree came up fine while the proxy served its holding page permanently. Latent before `pull`, guaranteed after it
- [x] Verified live end to end: `JacobPEvans/mlx-benchmarks-viewer` pulled and served through sidepage's own proxy — real Gradio UI (213 KB), 14 live named endpoints, SSE event stream flowing
- [ ] Open items — Hub authentication, hostile dependency files, size ceiling, repo-size-vs-runtime-size, GitHub transport: `docs/OPEN_QUESTIONS.md` #19
