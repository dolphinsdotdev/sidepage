# v5 draft — infra proposals

Consolidated from a design conversation covering seven ideas: an isolation
backend (Firecracker), A2A-aware serving, human-in-the-loop environments,
timeout auto-teardown, lazy start, sibling-app URL discovery for static
sites, and email ingress. Written the same way `docs/OPEN_QUESTIONS.md`
tracks unresolved design — nothing here is built, and several items
explicitly reopen a question `docs/CHECKLIST.md` had marked closed.

**Scope constraint, deliberate:** every section below is restricted to
what `sidepage` itself should provide as infra — target detection, process
lifecycle, the proxy, the registry, auth, provisioning. Anything that's a
property of the *wrapped app's own code* (task logic, message handling,
what a script does with a parsed email, whether an app tolerates being
restarted) is explicitly excluded, even where it was part of the original
conversation. That boundary is the same one already drawn for MCP: `serve`
launches the transport, never touches what rides over it.

Numbering continues from `docs/CHECKLIST.md` §16.

---

## §17 Isolation backend (Firecracker)

**Problem:** `sidepage.core.process.serve` launches CODE/NOTEBOOK targets
as bare host subprocesses (`subprocess.Popen`) — zero isolation between
wrapped code and the machine running `sidepage`. Acceptable when the
operator trusts their own script; not acceptable the moment `sidepage`
executes code it didn't author, on behalf of someone else.

**Proposal:** an optional isolation backend for the launch path —
conceptually `serve --isolate firecracker`, swapping `subprocess.Popen`
for a microVM boot while leaving everything downstream unchanged
(`upstream_port`, `check_upstream_ready`, the proxy, the tunnel, the
registry) — `sidepage` already treats "the wrapped thing listens on a
port" as the entire contract, so the isolation backend only has to
satisfy that same contract.

**Hard constraint, not a scoping choice:** Firecracker needs KVM — Linux
with virtualization access. Not available on the platform `serve` actually
runs on today (a local CLI on someone's laptop, frequently macOS). Not
buildable as a default local backend under any effort level.

**Status:** contingent, not "not implemented yet." Only becomes buildable
once `sidepage` runs *something* on Linux infrastructure it controls —
i.e., gated behind the same nonexistent hosted backend that blocks
brokered tunneling (`docs/OPEN_QUESTIONS.md` #12). Record as a placeholder
of that same shape, not a near-term item.

**Affects:** `sidepage.core.process`, `sidepage.core.ecosystem`

---

## §18 A2A-aware serving

**Problem:** `sidepage.core.target` already special-cases MCP servers
(`detect_mcp_package`, bypassing the script's own `.run()` to launch
`.streamable_http_app()`/`.http_app()` directly). A2A agent servers (e.g.
Python `a2a-sdk`'s `A2AStarletteApplication`) have the identical shape —
an ASGI app normally wrapped in a blocking run call — but aren't
recognized, so they fall through to the generic `$PORT` launcher and fail
the same way any undetected framework does.

**Proposal:**
- `target.py`: add a new `CodeLauncher.A2A`, detected via source scan for
  the A2A SDK import (mirrors `detect_mcp_package`'s pattern exactly);
  launch its ASGI app directly via `uvicorn --factory`, same bypass.
- Local discovery: extend `sidepage.core.registry`/`sidepage ls` to fetch
  and surface each running A2A app's Agent Card
  (`/.well-known/agent.json`). `sidepage` already tracks name/port/URL for
  everything it launched — this is one more cheap fetch, not A2A protocol
  awareness.
- Auth: agent-to-agent signed requests as a new `AuthTier` — already
  tracked as `docs/OPEN_QUESTIONS.md` #1. A2A's own Agent Card
  `securitySchemes` convention (bearer/OAuth2/mTLS) is a ready-made model
  to adopt rather than inventing one from scratch.

**Explicitly excluded:** task lifecycle, message handling, delegation
decisions, anything resembling `tools/call` semantics — same boundary
already drawn for MCP.

**Affects:** `sidepage.core.target`, `sidepage.core.registry`,
`sidepage.core.auth`, `sidepage.core.token_runtime`

**Open, not decided:** does Agent Card data belong directly in `sidepage
ls` output, or a separate `sidepage agents` subcommand?

---

## §19 Programmatic lifecycle control

**Problem:** `process.serve()` is a blocking call that owns the
foreground and tears down only via Ctrl+C or SIGTERM
(`process.py:339`). Anything where something *other than a human at a
terminal* needs to start an app and stop it later — a script spinning up
a task-scoped human-facing environment, a demo chaining multiple `serve`
calls — has no hook today; it has to shell out to the CLI as a subprocess
and manage that subprocess itself from outside.

**This directly reopens §16** ("Out of scope for this binary...
`--background` is explicitly ruled out"). Flagged as reopening a closed
question, not a quiet reversal of it.

**Proposal, deliberately narrow — not an orchestrator:**
- `serve --detach`: returns once the proxy/tunnel are up and the app is
  registered, instead of blocking. No health-restart, no supervision, no
  fleet management — literally just "don't block the caller." Teardown
  stays exactly `sidepage stop <name>`, already real.
- A generic on-teardown callback: `serve --on-stop-webhook <url>`, fired
  once per app instance end (timeout, explicit `stop`, or the wrapped
  process exiting on its own) with `{app_name, reason, started_at,
  stopped_at}`. Protocol-agnostic — `_teardown()` already knows when and
  why an app ends; this just reports it. Deliberately *not* "notify when
  the task is done," since "done" is app-defined and out of scope here.

**Explicitly excluded:** any decision logic about *when* to spin up an
environment or what to do with a human's response — that's the
orchestrating script's job, not sidepage's.

**Affects:** `sidepage.core.process` (`serve`, `_teardown`),
`sidepage.commands.serve`

**Open, not decided:** does `sidepage ls` need to distinguish detached
apps from interactive ones?

---

## §20 Timeout / auto-teardown

**Problem:** no expiry mechanism exists. An app runs until explicitly
stopped, indefinitely, even fully unattended.

**Proposal:** `serve --timeout <seconds>` (absolute, measured from
`started_at`, already recorded on every `RunningApp`) and/or `serve
--idle-timeout <seconds>` (resets on each proxied request/WS message,
since the proxy's usage counters already observe every one). Both checked
inside the existing blocking loop in `process.serve`, both exiting through
the same `_teardown()` Ctrl+C and `stop` already use — no new teardown
path. If §19's webhook exists, a timeout-triggered teardown reports
through it like any other reason.

**Open, not decided:** absolute and idle timeout are different features
with different failure modes — ship both as independent, composable
flags, or pick one first? Leaning both, not settled.

**Unchanged by this feature:** no drain window — a timeout firing
mid-request kills it exactly as hard as `stop` does today
(`docs/OPEN_QUESTIONS.md` #2 still applies, untouched).

**Affects:** `sidepage.core.process`, `sidepage.commands.serve`,
`sidepage.core.reverse_proxy` (idle variant needs "time of last request"
exposed from the proxy)

---

## §21 Lazy start / scale-to-zero

**Problem:** CODE/NOTEBOOK subprocesses launch unconditionally at `serve`
time, whether or not anyone ever sends a request.

**Proposal, two tiers of very different size:**

- **Tier 1 — lazy start.** Defer `subprocess.Popen` out of
  `process.serve()` into the proxy's request handler, fired once on first
  inbound request behind a start-once lock. Reuses the existing
  `ready`-Event/holding-page mechanism (`reverse_proxy.check_upstream_ready`)
  verbatim — same "app is booting" page, just triggered by a request
  instead of by `serve` itself starting. Small, self-contained.
- **Tier 2 — scale-to-zero.** After an idle-kill (§20), respawn on the
  *next* request instead of tearing the whole `serve` invocation down.
  Needs `registry.RunningApp.pid` to tolerate being updated across
  respawns — currently assumed stable for an app's whole life, read
  directly by `ls`/`stop`/`usage`. Materially bigger than Tier 1, not a
  natural extension of it.

**Explicitly flagged, not silently accepted:** Tier 2 drops in-memory
state on every idle-kill. For a stateless FastAPI target that's fine; for
a live Jupyter kernel (notebook target) it's a real UX trap, not an
implementation detail — recommend rejecting `--idle-timeout` on notebook
targets outright, or requiring an explicit acknowledgment flag, rather
than silently respawning a fresh kernel under the same URL.

**Explicitly excluded:** whether the wrapped app itself is written to
tolerate a restart / recover its own state — sidepage can't and shouldn't
try to compensate for that.

**Scope:** CODE/NOTEBOOK only. STATIC is already in-process and
effectively instant — no lazy-start story needed there.

**Affects:** `sidepage.core.process`, `sidepage.core.reverse_proxy`,
`sidepage.core.registry`

**Recommendation:** ship Tier 1 alone first; Tier 2 is a separate,
larger decision. Not settled.

---

## §22 Sibling-app URL discovery for static targets

**Problem:** `sidepage.core.static` mounts Starlette's
`StaticFiles(directory=..., html=True)` directly — raw bytes off disk, no
templating anywhere in the path. A static frontend has no sidepage-
provided way to learn another served app's URL, which doesn't exist until
tunnel-open time and changes across `--anon` runs.

Bundling frontend and API into a single served app (same-origin, relative
paths, no lookup ever needed) sidesteps this entirely — but that's an
application-architecture choice, out of scope for this spec by the
stated constraint. What follows is the infra path for a genuinely
decoupled static site.

**Proposal, recommended:** a dynamic endpoint alongside the `StaticFiles`
mount in `_build_static_app` — e.g. `GET /.sidepage/config.json` —
returning JSON built from `sidepage.core.registry` data (a configured
sibling app's current `url`/`tunnel_url`, via a new `serve --link
<app-name>` flag). No file mutation, nothing ever baked stale onto disk,
works whether the sibling app already exists or starts later.

**Heavier alternative, only if literal substitution is actually wanted:**
a build-time rewrite pass — copy the static root to a temp dir at `serve`
time, string-replace a defined placeholder (e.g. `{{SIDEPAGE:<app-name>}}`)
across an explicit text-file extension allowlist (never binaries), mount
`StaticFiles` on the copy instead of the original. Real, but meaningfully
more invasive than the config-endpoint option — recommend only if one
extra fetch from the page's JS genuinely isn't acceptable.

**Explicitly excluded:** CORS headers between two separately-served
apps' origins — a per-app configuration choice for whichever app is the
backend, not something sidepage should inject unasked.

**Affects:** `sidepage.core.static`, `sidepage.core.reverse_proxy`,
`sidepage.core.registry`

**Open, not decided:** should `--link` be required (explicit, one named
sibling) or should the config endpoint list every currently-running app
by default? The latter is more convenient and a real information-
disclosure question under `--auth open` — flagged, not resolved.

---

## §23 Email ingress

**Problem:** no path exists for a served app to receive inbound email —
categorically outside the HTTP(S)+WS / `cloudflared --url` model sidepage
is built on. Real SMTP needs MX records plus a stable public IP; `serve`
running on a laptop can't provide either (and residential ISPs block
inbound port 25 regardless).

**Proposal — an extension of BYO-domain provisioning, not a new
subsystem** (BYO-domain already requires a Cloudflare-managed zone, which
is the one precondition this also needs):
- Extend `account domain set` / `provision_byo_domain` to optionally
  enable Cloudflare Email Routing on the same zone, plus deploy a small
  relay Worker whose only job is: receive the raw MIME message, POST it
  to a fixed sidepage-owned path on the addressed app's existing public
  URL, carrying a shared secret sidepage generates and the wrapped app
  can verify.
- Extend the existing per-app ingress-rule provisioning (already does
  hostname→app routing for HTTP, via GET-modify-PUT against the Tunnel
  configurations API) with an address→app mapping: `<app-name>@domain` →
  that app's ingress hostname, provisioned the same way.
- Requires widening the Cloudflare API token scope (Email Routing:Edit)
  beyond what BYO-domain requests today (Tunnel:Edit, DNS:Edit,
  Zone:Read).

**Explicitly excluded:** what the app does with a parsed email —
attachments, threading, reply logic. Sidepage's job ends at delivering one
authenticated HTTP POST.

**Not buildable without new infra:** the relay Worker component doesn't
exist yet and has to be written and hosted — same shape of gap as
brokered tunneling needing a backend that doesn't exist
(`docs/OPEN_QUESTIONS.md` #12), though smaller in scope (one Worker, not
an account/billing backend).

**Affects:** `sidepage.core.account`, `sidepage.core.tunnel_manager`,
`sidepage.core.secrets_vault` (new relay shared-secret, same storage
pattern as existing vault-held tunnel run-tokens)

---

## Cross-cutting notes

- **§19 and §21 compose.** Lazy start + idle-timeout is scale-to-zero,
  which only pays for itself the moment §17's isolation backend exists —
  cold-start-on-demand is exactly why microVM-per-app is economical at
  Fly.io/Lambda scale. On a single local `serve` call, §21 Tier 1 alone
  is still worth it (don't boot what nobody's opened); Tier 2 mostly
  matters once there's a hosted backend.
- **§18 and §23 both want the on-stop webhook shape from §19** — an A2A
  agent's teardown and an inbound-email delivery failure are both "sidepage
  knows an event happened, the app-layer cares what it means" cases.
- None of §17–§23 requires touching `sidepage.core.reverse_proxy`'s
  fundamental proxy contract (byte streaming, WS passthrough) — every
  proposal here composes with what's already real rather than replacing
  it.
