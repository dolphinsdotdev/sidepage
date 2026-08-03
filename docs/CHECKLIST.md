# Feature checklist

Running checklist of every feature in the v3 spec. **Update this file in
the same change that flips a box** — that's the point of keeping status
here instead of letting it live only in commit messages.

Two layers per command:
- **CLI** — argument parsing, options, help text (`sidepage.commands`). Wiring only.
- **Core** — the actual behavior (`sidepage.core`). Check this off only when
  the function no longer raises `NotImplementedError`.

Legend: `[x]` done · `[ ]` not done

---

## §1 Targets

- [x] CLI: `sidepage new <name> --type static`
- [ ] Core: `sidepage.core.scaffold.scaffold_project`
- [ ] Core: `sidepage.core.target.detect_target_kind` (code/static/notebook inference)

## §2 Serving

- [x] CLI: `sidepage serve <target> [--type] [--name] [--domain] [--auth] [--anon] [--token] [--scope] [--guardrail]`
- [x] CLI: `sidepage stop <app-name>`
- [ ] Core: `sidepage.core.process.serve`
- [ ] Core: `sidepage.core.process.stop`
- [ ] Core: `sidepage.core.target.resolve_port_injection` ($PORT / launcher-flag injection)
- [ ] Core: immediate tunnel teardown on Ctrl+C / `stop`

## §3 Naming & identity

- [x] Resolved: no grace period on name reclaim (confirmed, accepted risk)
- [ ] Core: `sidepage.core.directory_client.check_name` (now internal-only, called during `serve`)
- [ ] Core: `--anon` apps skip directory registration entirely

## §4 Auth tiers

- [x] CLI: `sidepage serve --auth open|network|token|oauth`
- [ ] Core: `open` / `network` tier enforcement (local reverse proxy)
- [ ] Core: `token` tier — see §8 (token_runtime) + §9 (reverse_proxy enforcement)
- [ ] Parked: `oauth` — deferred pending §15 MCP auth model, not implemented
- [ ] Design: agent-to-agent signed requests (see `docs/OPEN_QUESTIONS.md`)

## §5 Discovery & scope

- [x] Resolved: one directory, scope as a field (not per-org instances)
- [x] CLI: `sidepage serve --scope local|lan|intranet|web`
- [x] CLI: `sidepage promote <app-name> [--scope web]`
- [ ] Core: `sidepage.core.directory_client.promote`
- [ ] Core: `local` / `lan` (mDNS) / `intranet` (ACL) / `web` scope handling

## §6 Tunnel architecture

- [x] CLI: `sidepage serve --domain <domain>` (BYO, premium)
- [x] CLI: `sidepage serve --anon` (Quick Tunnel)
- [ ] Core: `sidepage.core.tunnel_manager.open_brokered_tunnel` (default, free tier)
- [ ] Core: `sidepage.core.tunnel_manager.open_byo_tunnel`
- [ ] Core: `sidepage.core.tunnel_manager.open_anon_tunnel`
- [ ] Core: `sidepage.core.tunnel_manager.resolve_cloudflared_binary` (4-step resolution chain)
- [ ] Core: `sidepage.core.tunnel_manager.close_tunnel`
- [ ] Question: standalone `tunnel status`/`tunnel revoke` — folded into `status`/`account domain set`, or a real gap? (`docs/OPEN_QUESTIONS.md` #8)

## §7 Metering

- [x] CLI: `sidepage usage <app-name>`
- [x] Resolved: connection/request-count is the permanent billing boundary
- [ ] Core: `sidepage.core.usage_reporter.get_usage` (HTTP request/response + WS connection/message counts)
- [ ] Core: counts sourced from the local reverse proxy

## §8 Token handling

- [x] CLI: `sidepage serve --token <value>` (plus `SIDEPAGE_TOKEN` env var, handled in core)
- [ ] Core: `sidepage.core.token_runtime.resolve_token`
- [ ] Core: `sidepage.core.token_runtime.write_runtime_file` / `read_runtime_file`
- [x] Resolved: session validity until app stop, no rotation

## §9 Local reverse proxy

- [ ] Core: `sidepage.core.reverse_proxy.start_proxy`
- [ ] Core: `sidepage.core.reverse_proxy.check_upstream_ready` (real GET, not bare TCP connect)
- [ ] Core: `sidepage.core.reverse_proxy.stop_proxy`
- [ ] Core: auth gate page + session cookie
- [ ] Core: startup holding page (client-side polling)
- [ ] Core: WebSocket proxying
- [ ] Core: streaming passthrough (no full-body buffering)
- [ ] Design: graceful drain vs. hard kill on `stop` (deferred, `docs/OPEN_QUESTIONS.md` #2)

## §10 Inspection

- [x] CLI: `sidepage inspect [<app-name-or-url>]`
- [x] Resolved: no auth bypass for the local operator
- [ ] Core: `sidepage.core.inspector.open_console`
- [ ] Core: auto-source credentials from token runtime file
- [ ] Core: surface live usage counts

### (no v3 section — see `docs/OPEN_QUESTIONS.md` #7)

- [x] CLI: `sidepage ls [--scope <scope>] [--mine]`
- [x] CLI: `sidepage status <app-name>`
- [ ] Core: `sidepage.core.directory_client.list_entries`
- [ ] Core: `sidepage.core.directory_client.get_status` (folds in tunnel reachability)

## §11 Static site serving

- [ ] Core: `sidepage.core.static.validate_static_root` (missing `index.html` → hard error)

## §12 Notebook serving

- [x] CLI: `sidepage serve notebook.ipynb --auth token` (via generic `serve`, no dedicated flag)
- [ ] Core: `sidepage.core.notebook.build_jupyter_launch_command`
- [ ] Core: `sidepage.core.notebook.verify_proxy_fronted` (safety check, not yet designed)
- [ ] Design: `juv` for standalone `.ipynb` dependency resolution — evaluation only, not committed

## §13 Account & login

- [x] CLI: `sidepage login`
- [x] CLI: `sidepage account status`
- [x] CLI: `sidepage account domain set <domain>`
- [ ] Core: `sidepage.core.account.login`
- [ ] Core: `sidepage.core.account.current_account`
- [ ] Core: `sidepage.core.account.set_default_domain`

## §14 Ecosystem integration

- [ ] Core: `sidepage.core.ecosystem.resolve_python_runner` (uv, default)
- [ ] Core: `sidepage.core.ecosystem.detect_js_package_manager` (npm/yarn/pnpm lockfile detection)

## §15 Parked for future discussion

- [ ] Design: MCP-specific auth model (not started)
- [ ] Design: stdio-transport MCP servers (not started — breaks the "everything is a port" assumption)

## §16 Out of scope for this binary

- [ ] Orchestrator (fleet/process management) — separate product by design, not tracked here beyond noting it's not started

## Parked / unclear status (not numbered in v3)

- [ ] Guardrails & pre/post-processing — kept as a placeholder (`serve --guardrail`,
      `sidepage.core.guardrail`); v3 doesn't mention this section at all. See
      `docs/OPEN_QUESTIONS.md` #6.

## Tooling & docs

- [x] uv-managed project (`pyproject.toml`, `uv.lock`, `.python-version`)
- [x] Full CLI command tree wired (Typer), every command reachable via `--help`
- [x] Shared output helpers (`sidepage.output`)
- [x] Smoke test suite (`tests/test_cli_smoke.py`) — wiring only, no behavior yet
- [x] README with full command reference, architecture note, and project layout
- [x] Open questions doc (`docs/OPEN_QUESTIONS.md`), split into resolved (v3) / still open
- [x] This checklist, rebuilt for v3
- [ ] Real integration tests (blocked on `core` having behavior to test)
- [ ] CI workflow
