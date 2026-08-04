# Open questions

Consolidated from `sidepage-cli-spec-v3.md`, plus what v1 (`sidecar-cli-spec.md`)
left open and v3 has since resolved. Each item is also referenced from the
docstring of the `core`/`commands` module it affects — this file exists so
they're visible in one place without going back to the spec docs.

A v4 delta (secrets vault, `serve --env`, BYO-domain credentials routed
through the vault, a §8 clarifying note) was applied from a 4-point summary
the user gave directly in chat, not a full v4 spec document. Items 9 and 10
below are specific to that: gaps the summary didn't cover, flagged rather
than guessed at.

A later pass turned `serve` and `secrets` from documented placeholders
into real, working code (see `docs/CHECKLIST.md` for the full breakdown).
Items 11–13 are engineering decisions and a verification limit that came
out of that pass — not spec ambiguities, but calls made without stopping
to ask, flagged here on the same principle.

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

---

### 9. Did v4 renumber §9, or does the vault sit alongside the reverse proxy at the same number?

v3 §9 is "local reverse proxy." The user's own v4 delta summary labels the
secrets vault "v4 §9" too. Since the summary was explicitly "the complete
delta" (nothing else changed), it's unclear whether v4 actually inserted a
new §9 and pushed the reverse proxy (and everything after it — inspection,
static, notebook, account, ecosystem, parked, out-of-scope) up by one, or
whether the summary's "§9" was shorthand not meant to be read literally
against v3's numbering. Docstrings in this codebase cite the reverse proxy
as "§9 (v3)" and the vault as "v4 §9" side by side rather than picking one.

**Affects:** `sidepage.core.secrets_vault`, `sidepage.core.reverse_proxy`

---

### 10. Vault namespace: flat, or scoped somehow?

`sidepage secrets set/list/remove` and `serve --env <SECRET_NAME>` were
described with no app-scoping — implemented here as one flat namespace per
identity (any `serve` call can reference any stored secret by name). Not
confirmed: whether secrets should instead be scoped per-app, per-project,
or otherwise namespaced. A flat namespace was the simpler reading of the
delta as given, not a stated design decision.

**Affects:** `sidepage.core.secrets_vault`, `sidepage.commands.secrets`

---

### 11. Secrets vault: encrypted-file only, OS keychain deferred

The spec's design is OS keychain as the *primary* backend with an
encrypted-file *fallback*. This build implements the encrypted-file
backend only, and it's the only backend in practice — not a fallback that
sits behind something more commonly used. Reasoning: the `keyring` package
triggers an interactive macOS Keychain-access permission prompt on first
use, which isn't safe to depend on in an automated CLI tool or test suite.
The public API (`set_secret`/`get_secret`/`list_secrets`/`remove_secret`)
doesn't change shape if keychain support is added later, so this isn't a
design commitment against it — just a scope decision for this pass.

**Affects:** `sidepage.core.secrets_vault`

---

### 12. Brokered/BYO-domain tunneling: not implemented because no backend exists

Distinguishing this from the rest of the "not implemented" surface: v3's
default tunnel mode (brokered, under Sidepage's own domain) requires a
Sidepage cloud backend to issue scoped tunnel tokens, and BYO-domain
requires real DNS automation against a user's Cloudflare zone. Neither can
be built by writing more `sidepage` code — they need a backend service
that doesn't exist yet, which is a different kind of gap than "not
implemented yet" (e.g. `sidepage inspect`, which could be built today).
`serve` without `--anon`/`--domain` falls back to serving on `127.0.0.1`
only rather than either failing or silently pretending brokered mode ran.

**Affects:** `sidepage.core.tunnel_manager`, `sidepage.core.process`

---

### 13. `--anon` tunnel: verified up to the sandbox's network boundary, not fully

`sidepage.core.tunnel_manager.open_anon_tunnel` was tested directly: it
spawns a real `cloudflared tunnel --url` subprocess, which successfully
connected to Cloudflare's edge and returned a genuine assigned
`*.trycloudflare.com` URL. A follow-up HTTP request to that URL from
within the sandboxed dev environment this was built in failed with a DNS
resolution error, consistent with that environment's network policy
blocking arbitrary outbound domains rather than a bug in the tunnel code.
Whether the URL is actually reachable from the open internet (i.e. from a
real browser, outside this sandbox) was not verified end-to-end.

**Affects:** `sidepage.core.tunnel_manager`
