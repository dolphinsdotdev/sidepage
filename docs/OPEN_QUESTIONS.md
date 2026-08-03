# Open questions

Consolidated from `sidepage-cli-spec-v3.md`, plus what v1 (`sidecar-cli-spec.md`)
left open and v3 has since resolved. Each item is also referenced from the
docstring of the `core`/`commands` module it affects — this file exists so
they're visible in one place without going back to the spec docs.

---

## Resolved in v3

These were open in v1 and are now settled — kept here for provenance, not
because they still need a decision.

- **How opinionated should `new` be?** Resolved by shrinking scope, not by
  picking a point on the spectrum: v3 drops streamlit/api/mcp scaffolding
  entirely, leaving only a static-site skeleton. See `sidepage.core.scaffold`.
- **`intranet` scope: separate directory per org, or ACLs on one global
  directory?** Confirmed: one directory, scope is a field. See
  `sidepage.core.directory_client`.
- **Name reclaim after teardown: grace period or not?** Confirmed: no grace
  period, accepted risk. See `sidepage.core.directory_client`.
- **Trust model if an app under-reports usage?** Sidestepped, not
  answered directly: v3 makes the local reverse proxy the sole source of
  usage counts, so self-reporting (and its trust problem) never enters the
  picture. Billing is connection/request-count only, forever. See
  `sidepage.core.usage_reporter`.
- **Should `sidepage inspect` respect auth tiers, or bypass for the local
  operator?** Confirmed: no bypass, same credential as any caller (just
  auto-sourced for convenience). See `sidepage.core.inspector`.
- **Session validity for token auth?** Confirmed: until app stop, no
  separate timer, consistent with no token rotation. See
  `sidepage.core.reverse_proxy`, `sidepage.core.token_runtime`.

---

## Still open

### 1. Agent-to-agent signed requests as a future auth mechanism

One hosted agent calling another may want signed requests rather than a
`token`-tier shared secret, since a key living in an agent's context is a
leak vector. Deferred in both v1 and v3 — not designed.

**Affects:** `sidepage.core.token_runtime`, `sidepage.core.auth`

---

### 2. Graceful drain vs. hard kill on `stop`

Immediate teardown (no grace period) is the confirmed default. Whether a
short drain window for in-flight requests/open WebSocket connections gets
added later is explicitly unresolved — "deferred, not blocking" per v3.

**Affects:** `sidepage.core.reverse_proxy`, `sidepage.core.process`

---

### 3. MCP-specific auth model

How auth tiers apply to MCP tool calls specifically, separate from the
general HTTP token/gate-page flow. Newly parked in v3 (§15), not resolved.
`oauth` in `AuthTier` stays unimplemented pending this.

**Affects:** `sidepage.core.auth`

---

### 4. stdio-transport MCP servers

No port at all — breaks the "everything is a port" assumption the whole
proxy design (`sidepage.core.target`, `sidepage.core.reverse_proxy`) rests
on. Needs a different bridging strategy if it's ever in scope. Newly parked
in v3 (§15).

**Affects:** `sidepage.core.target`, `sidepage.core.reverse_proxy`

---

### 5. What is the orchestrator, architecturally?

Single-host process supervision (pm2-equivalent) or a multi-host/multi-user
control plane over the directory? Unchanged from v1 — still the single
largest outstanding item, still explicitly out of scope for this binary.
`--background` is ruled out on `serve` for the same reason.

**Affects:** `sidepage.core.process`, indirectly `sidepage.core.directory_client`

---

### 6. Guardrails: parked, or quietly cut?

v1 had `serve --guardrail <config.yaml>` as an explicit, deferred feature.
v3 doesn't mention guardrails anywhere — not in its numbered sections, not
in its own "parked for future discussion" list (§15). Treated in this
codebase as *not re-stated* rather than *removed*, since v3 says so
explicitly every other time it drops something (standing API keys,
streamlit/api/mcp scaffolding, `oauth`'s near-term scope). If a future spec
revision confirms this was actually cut, `sidepage.core.guardrail` and the
`--guardrail` flag on `serve` should go with it.

**Affects:** `sidepage.core.guardrail`, `sidepage.commands.serve`

---

### 7. `ls` / `status`: no v3 section at all

v1 had a numbered "Directory queries" section (§10) for `ls`/`status`. v3
jumps from §9 (local reverse proxy) straight to §10 (inspection) with no
equivalent — these two commands aren't mentioned anywhere in v3. Kept as-is
on the same "not re-stated, not cut" reasoning as guardrails, since the
underlying directory model is still very much alive in v3 (§3, §5). Unlike
guardrails, this wasn't a locked-in decision from an explicit clarifying
round — flagging it here in case it should be reconsidered.

**Affects:** `sidepage.commands.directory`, `sidepage.core.directory_client`

---

### 8. `tunnel status` / `tunnel revoke`: folded into `status`, or a real gap?

v3 has no `sidepage tunnel` command group at all — tunnel setup moved to
`login`/`account domain set`. This codebase folds tunnel reachability
reporting into the existing `sidepage status <app-name>` and drops a
standalone revoke command, on the assumption that re-running `account
domain set` covers credential replacement. Not confirmed by the spec text —
flagged as an assumption made during migration, not a resolved question.

**Affects:** `sidepage.core.tunnel_manager`, `sidepage.commands.account`
