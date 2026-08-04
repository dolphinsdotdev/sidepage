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
- [x] Core: `sidepage.core.target.detect_code_launcher` — Streamlit via import scan, else generic `$PORT`
- [x] Core: `sidepage.core.target.allocate_port`

## §2 Serving

- [x] CLI: `sidepage serve <target> [--type] [--name] [--domain] [--auth] [--anon] [--token] [--env]... [--scope] [--guardrail]`
- [x] CLI: `sidepage stop <app-name>`
- [x] Core: `sidepage.core.process.serve` — the biggest real module; orchestrates target detection, port allocation, subprocess launch, proxy, tunnel, registry
- [x] Core: `sidepage.core.process.stop` — SIGTERM to the registered pid, routed through the same clean teardown as Ctrl+C
- [x] Core: port injection — `--server.port` flag for Streamlit, `$PORT` env var for the generic fallback
- [x] Core: immediate tunnel/proxy/subprocess teardown on Ctrl+C / `stop` (via a SIGTERM handler that raises `KeyboardInterrupt`)
- [x] Core: `--domain`, non-`local` `--scope`, `--auth network`/`oauth`, `--guardrail` all rejected up front with a clear message (`_validate_supported`), not silently ignored
- [x] Core: `notebook` targets rejected with a clear message (detected but not servable)
- [x] CLI (v4): `--env <SECRET_NAME>` repeatable, vault injection
- [x] Core (v4): `serve` resolving each `env_secrets` name via `secrets_vault.get_secret`, fail loud (`SecretNotFoundError`) on miss
- [x] Verified end to end: static-site fixture and Streamlit fixture, both via real subprocess CLI invocation (`tests/test_serve_integration.py`)

## §3 Naming & identity

- [x] Resolved: no grace period on name reclaim (confirmed, accepted risk)
- [ ] Core: `sidepage.core.directory_client.check_name` — not called; `serve` uses `--name` or the target's filename directly, no collision-suffix assignment (no cloud directory to check collisions against)
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
- [ ] Core: `sidepage.core.tunnel_manager.open_byo_tunnel` — not implemented (needs real Cloudflare Zone/DNS automation); `zone_token_name`/`tunnel_token_name` plumbing exists on the CLI (v4) but nothing resolves them yet
- [x] Core: `sidepage.core.tunnel_manager.open_anon_tunnel` — real `cloudflared tunnel --url` subprocess, parses a genuine `*.trycloudflare.com` URL. Verified: cloudflared connects and gets a real URL back; verifying public reachability from a browser wasn't possible from this sandboxed environment's network policy
- [x] Core: `sidepage.core.tunnel_manager.resolve_cloudflared_binary` — real for 2 of 4 spec steps (override path, `PATH` lookup); local-cache/download-on-first-run not implemented, not a practical gap while `cloudflared` is on `PATH`
- [x] Core: `sidepage.core.tunnel_manager.close_tunnel` — real, terminates the `cloudflared` subprocess
- [ ] Question: standalone `tunnel status`/`tunnel revoke` — folded into `status`/`account domain set`, or a real gap? (`docs/OPEN_QUESTIONS.md` #8)
- [x] Resolved (v4): BYO-domain credential storage mechanism — routes through the secrets vault by name (storage is real; opening a BYO tunnel with those credentials is not)

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
- [ ] Core: `sidepage.core.inspector.open_console` — not implemented, not one of the two prioritized features
- [ ] Core: auto-source credentials from token runtime file
- [ ] Core: surface live usage counts

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
- [x] CLI: `sidepage account domain set <domain> --zone-token-name --tunnel-token-name` (v4: token-name flags required at the CLI level)
- [ ] Core: `sidepage.core.account.login` — not implemented, no account backend
- [ ] Core: `sidepage.core.account.current_account` — not implemented
- [ ] Core: `sidepage.core.account.set_default_domain` (v4: would store vault secret names, not raw credentials) — not implemented; doesn't yet check the named secrets exist in the vault before reporting not-implemented

## §14 Ecosystem integration

- [x] Core: `sidepage.core.ecosystem.resolve_python_runner` — real: prefers a sibling `.venv`, then `uv run --with-requirements requirements.txt`, then `uv run --with <package>`
- [ ] Core: `sidepage.core.ecosystem.detect_js_package_manager` (npm/yarn/pnpm lockfile detection) — not implemented, no JS target prioritized

## §15 Parked for future discussion

- [ ] Design: MCP-specific auth model (not started)
- [ ] Design: stdio-transport MCP servers (not started — breaks the "everything is a port" assumption)

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
- [x] Real runtime dependencies: Starlette, uvicorn, httpx, `websockets`, `cryptography` (not just named-but-uninstalled)
- [x] README with full command reference, architecture note, real-vs-stubbed project status, and project layout
- [x] Open questions doc (`docs/OPEN_QUESTIONS.md`), split into resolved (v3) / still open, plus v4-specific gaps
- [x] This checklist, updated for the real-implementation pass
- [ ] CI workflow
