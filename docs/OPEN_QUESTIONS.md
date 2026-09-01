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

A further pass turned `inspect` real for generic HTTP/static targets,
deliberately scoped short of the spec's actual "Postman-for-MCP" framing.
Item 14 covers what's still open there.

A real-world app surfaced a genuine dependency-resolution bug (an
undeclared, hand-installed package silently missing at launch), fixed by
no longer trusting an existing project `.venv`. Item 15 covers the
residual gap that fix doesn't close.

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

### 4. stdio-transport MCP servers — resolved for the common case, still open for low-level-only servers

Originally: no port at all — breaks the "everything is a port" assumption
the whole proxy design (`sidepage.core.target`,
`sidepage.core.reverse_proxy`) rests on. Newly parked in v3 (§15).

**Resolved for the case that actually matters in practice.** `sidepage
serve` now supports Python MCP servers built on either recognized
high-level API (the official SDK's `FastMCP`/`MCPServer`, or the
third-party `fastmcp` package's `FastMCP` — see
`sidepage.core.target.detect_mcp_package`). The resolution isn't "add
stdio bridging" — it's that `serve` never executes the script's own
`__main__`/`.run()` call at all (same bypass FastAPI targets already get),
and instead launches `<var>.streamable_http_app()` / `<var>.http_app()`
directly via `uvicorn --factory`. A script whose `__main__` only ever
calls `mcp.run()` — stdio by construction, the default for both packages —
still ends up served over real Streamable HTTP, because that `__main__`
block is never reached. Verified end to end, including a script
deliberately left stdio-only in its own entrypoint
(`tests/fixtures/mcp-app`, `tests/test_serve_mcp.py`), against the
actually-resolvable current releases of both packages rather than
assumed from memory (the official SDK renamed its high-level class
between the versions checked — `FastMCP` → `MCPServer` — which is exactly
the kind of assumption worth verifying rather than trusting).

**Still open:** a server built directly on the *low-level* API
(`mcp.server.lowlevel.Server` wired to `mcp.server.stdio` by hand, with no
`FastMCP`/`MCPServer`/`.streamable_http_app()` equivalent anywhere) has no
ASGI app for `serve` to bypass into — there's genuinely no port to inject
into for that case, and it's not detected (falls through to the generic
`$PORT` fallback, which will just fail to launch it correctly). Not
encountered in practice yet; flagged rather than silently mishandled if it
comes up.

**Affects:** `sidepage.core.target`, `sidepage.core.process`, `sidepage.core.reverse_proxy`

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

### 12. Brokered tunneling: not implemented because no backend exists; BYO-domain is now real, including tunnel creation itself

Distinguishing this from the rest of the "not implemented" surface: v3's
default tunnel mode (brokered, under Sidepage's own domain) requires a
Sidepage cloud backend to issue scoped tunnel tokens. That can't be built
by writing more `sidepage` code — it needs a backend service that doesn't
exist yet, a different kind of gap than "not implemented yet" (e.g.
`sidepage inspect`'s generic-HTTP mode, which *was* buildable and is now
real — see item 14 for the one piece of `inspect` that has the same
"needs something else first" shape). `serve` without `--anon`/`--domain`
falls back to serving on `127.0.0.1` only rather than either failing or
silently pretending brokered mode ran.

BYO-domain, unlike brokered, turned out to be buildable against a user's
own Cloudflare account with no Sidepage backend involved, and is now real
(`sidepage.core.tunnel_manager.provision_byo_domain`/`open_byo_tunnel`,
`sidepage.core.account`, `sidepage.core.directory_client.check_name`; see
`docs/CHECKLIST.md` §6).

**Revised scope boundary (supersedes an earlier, narrower design).** The
original two-token design (`Zone:DNS:Edit` + a separately-sourced
per-tunnel run-token) deliberately had `open_byo_tunnel` **run** a tunnel
the user already created out-of-band (`cloudflared tunnel create` or the
dashboard), not **create** one — creating a tunnel needs
`Cloudflare Tunnel:Edit`, a broader scope than that two-token model
covered, and adding a third token type just to save one manual step wasn't
judged worth it at the time.

That call was revisited: a single Cloudflare API token scoped to
Account→Tunnel:Edit, Zone→DNS:Edit, Zone→Zone:Read turned out to be no
harder for the user to create than the old two-token pair (arguably
easier — it's one token, not two independently-sourced ones), while
letting `sidepage account domain set` provision the tunnel itself via the
API (`provision_byo_domain`) rather than requiring a manual out-of-band
step first. That in turn made it practical to move from "one `cloudflared`
process + one tunnel per app" to **one shared tunnel per domain**, with
per-app routing done via the Cloudflare Tunnel configurations API
(GET-modify-PUT ingress rules) instead of the `--url` flag, which only
ever supports a single hostname per process. The shared process is
reference-counted against the local registry (`sidepage.core.registry`)
and started/stopped only on the first/last app using a given domain, under
a per-domain advisory file lock so concurrent `serve`/`stop` calls on the
same domain can't race each other.

Not yet verified: an actual end-to-end run against a real Cloudflare
account (real zone, real tunnel, a browser reaching the resulting
hostname). The logic is covered by mocked unit tests
(`tests/test_tunnel_byo.py`) proving the request-building/response-handling,
shared-process lifecycle, and locking are correct, but real credentials
are needed for a live check — and per this project's working agreement,
real API tokens are never pasted into chat or handled by the assistant
directly, so this verification has to happen with the user running the
commands themselves and reporting back.

**Affects:** `sidepage.core.tunnel_manager`, `sidepage.core.process`, `sidepage.core.account`, `sidepage.core.directory_client`, `sidepage.core.registry`

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

---

### 14. `sidepage inspect`: MCP tool browsing deferred, client library choice not made — a real MCP fixture now exists, just not wired into `inspect`

The spec frames `inspect` as "Postman-for-MCP" — browsing MCP tools,
schemas, invoking calls over the MCP JSON-RPC/streamable-HTTP transport.
Still scoped down to generic HTTP/static request inspection (real, see
`docs/CHECKLIST.md` §10) — this item is specifically about `inspect`'s
tool-browsing UI, not about MCP serving support, which is now real (see
`docs/CHECKLIST.md` §1/§2, `sidepage.core.target`'s MCP detection).

The original reasoning no longer fully applies: `tests/fixtures/mcp-app`
is now a real MCP server fixture, and `sidepage serve` genuinely wraps it.
What's still missing is `inspect`'s own MCP-aware mode — browsing
`tools/list` output, invoking `tools/call` interactively, the way it
already does ad-hoc HTTP requests. The client implementation is still an
open choice: the official `mcp` Python SDK (correct, spec-compliant, but a
new `sidepage` *runtime* dependency — note this is different from `serve`,
which never imports `mcp` itself, only launches it inside the wrapped
process's own `uv run` environment) vs. a hand-rolled JSON-RPC client over
the already-present `httpx`, the approach `tests/test_serve_mcp.py` uses
for test purposes and which could plausibly move into `sidepage.core`
instead of staying test-only.

**Affects:** `sidepage.core.inspector`, `sidepage.commands.inspect`

---

### 15. Dependency resolution: only the *detected launcher's* package is guaranteed, not every undeclared import

`sidepage.core.ecosystem.resolve_python_runner` used to prefer an existing
`.venv` next to the target, on the assumption it was already correctly set
up. In practice, a real app (`chat-with-openrouter`) had a `.venv` built
from a `requirements.txt` that only listed `openai` — Streamlit had been
installed by hand at some point and never captured — so trusting that venv
produced `ModuleNotFoundError: No module named 'streamlit'` at launch, no
attempt to recover. Fixed: every launch now goes through `uv run`, layering
`--with <package>` for each package `sidepage.core.target.detect_code_launcher`
detected (Streamlit → `streamlit`; FastAPI → `fastapi` and `uvicorn`; MCP →
`mcp` or `fastmcp` plus `uvicorn`, depending on which package the target
imports; notebook → `jupyterlab`) on top of `--with-requirements
requirements.txt`, so the dependencies Sidepage itself knows about are
never silently missing.

What's still open: this only covers the *detected launcher's* package(s).
`detect_code_launcher` doesn't do full static-import analysis — it
recognizes a fixed, growing set of frameworks (Streamlit, FastAPI, two MCP
packages) via a source scan, plus notebook detection by file extension. If
a target imports some *other* undeclared package (not one of those
frameworks' own deps, not in `requirements.txt`/`pyproject.toml`, not
something Sidepage's own detection knows to look for), it'll still fail
the same way, just for a dependency Sidepage has no way to know it needs.
Broader fixes would mean either scanning all imports in the target (real
static analysis, meaningfully more work), detecting more frameworks one at
a time as they come up (the pattern so far), or just accepting this as a
documented limitation of a heuristic-based approach — not decided.

**Affects:** `sidepage.core.ecosystem`, `sidepage.core.target`, `sidepage.core.process`

---

### 16. Notebook serving: real now, and Jupyter's default origin/XSRF rejection behind a reverse proxy was a real, non-obvious blocker

Previously parked (not one of the two originally-prioritized targets,
alongside stdio MCP servers under "everything is a port"). Now real —
`sidepage.core.notebook.build_jupyter_launch_command` launches `jupyter
lab` bound to loopback with its own token/password auth disabled (the
reverse proxy is the auth boundary, same as every other launcher).

Worth recording because it wasn't obvious going in and would have been an
easy thing to ship broken: Jupyter Server's `check_origin` rejects
cross-origin HTTP requests and WebSocket upgrades by default, comparing
the request's `Origin` header against its own `Host`. Through *any*
reverse proxy — not specific to Sidepage's — the browser's `Origin` is the
proxy's own address, while Jupyter's real upstream port is different by
construction (the proxy fronts one port, the wrapped process listens on
another). That mismatch gets rejected out of the box, which would have
meant the Lab UI's own API/WebSocket calls silently failing once actually
opened in a browser, despite the initial page load looking fine. Confirmed
by reproducing the rejection directly (a real kernel start plus a real
`execute_request` over a WebSocket carrying a deliberately-mismatched
`Origin` header) before adding `--ServerApp.allow_origin=*
--ServerApp.disable_check_xsrf=True` to the launch command and confirming
that fixes it — not assumed from documentation. `tests/test_serve_notebook.py`
reproduces the same shape of check (proxy port, proxy's own origin, real
kernel execution) as a regression guard.

**Affects:** `sidepage.core.notebook`, `sidepage.core.process`, `sidepage.core.reverse_proxy`

---

### 17. Local app registry (registry spec v2): real, and a Typer internals finding worth recording

Separate document from the v3/v4 spec (`sidepage-registry-spec.md`), now
real: `sidepage app register|list|show|unregister` plus `sidepage serve
<app-name>` (`sidepage.core.app_registry`, `sidepage.commands.app_registry`).

**A few judgment calls the spec left implicit, resolved here:**
- `target` is stored as an absolute, resolved path (the spec's own
  illustrative example shows a bare relative `"target": "abc.py"`, which
  reads as illustrative rather than a deliberate relative-path decision)
  — so `serve <app-name>` works from any shell's cwd later, not just the
  one it was registered from.
- Re-registering an already-registered name is rejected, not a silent
  overwrite — matching `sidepage.core.process.serve`'s existing
  "already registered" stance for the separate *running*-apps registry.
  `unregister`ing an unknown name is likewise rejected rather than the
  vault's idempotent-removal stance (`sidepage.core.secrets_vault.remove_secret`)
  — a judgment call that a small, user-named registry is typo-prone
  enough that silent success on a typo'd name would hide the mistake.
- A registered app's runtime `--name` (what shows up in `sidepage
  ls`/`stop`) defaults to the registry key itself when neither the
  registration nor the serving invocation set one explicitly, not the
  served file's basename stem (`sidepage.core.process.serve`'s own
  default for a literal-path invocation) — `serve abc-app` producing a
  running app confusingly named `abc` (from `abc.py`'s stem) would be a
  worse default than the name the user actually typed.
- `--env`'s merge semantics (an explicit `--env` at `serve <app-name>`
  time replaces the registered list rather than appending to it) — the
  spec's own merge example only exercises a scalar flag (`--scope`), so
  list-valued-flag semantics were genuinely unspecified; replace was
  chosen for consistency with every other field rather than inventing a
  separate accumulate rule for the one repeatable flag.

**A real finding, not a design call:** this installed Typer version
(0.27.1) fully vendors its own fork of Click (`typer._click`) rather than
depending on the separately-installed, genuine `click` package (8.4.2,
also present) for command/context internals. `click.Command.make_context`
reuse — the mechanism that makes "a new `serve` flag is automatically
registry-compatible" true rather than aspirational — works fine against
Typer's `typer.core.TyperCommand` objects, but exceptions raised during
parsing are `typer._click.exceptions.*` instances, unrelated to the real
`click.exceptions.*` hierarchy (confirmed live: `except click.ClickException`
did not catch a bad-flag `BadParameter` raised this way). There's no
public Typer symbol for that private exception base to catch instead, so
`sidepage.commands.app_registry` catches a broad `Exception` scoped
tightly around the one `make_context` call rather than depending on a
private, underscore-prefixed module path that could change across Typer
releases.

**Affects:** `sidepage.core.app_registry`, `sidepage.commands.app_registry`, `sidepage.commands.serve`

---

### 18. Gradio serving: real, verified against 6.x only — older majors untested

`sidepage serve` recognizes Gradio targets (`sidepage.core.target.CodeLauncher.GRADIO`)
and launches them through a generated wrapper module that neutralizes
`gradio.Blocks.launch` before importing the target, then mounts whichever
Blocks it captured via Gradio's own `gradio.mount_gradio_app()` — see
`sidepage.core.process._GRADIO_WRAPPER_SOURCE`. Verified end to end
against **gradio 6.26.0**: the UI renders and a real prediction round trip
completes through sidepage's own reverse proxy
(`tests/test_serve_gradio.py`, against `tests/fixtures/gradio-app` — whose
`demo.launch(server_port=8123)` is module-level and unguarded, and whose
hardcoded port is confirmed never bound).

Three findings worth recording, each of which decided the design:

- **`GRADIO_SERVER_PORT` injection was rejected on evidence.** Gradio
  treats that variable as the *start* of a 100-port search
  (`GRADIO_NUM_PORTS`, `gradio/http_server.py`), so a busy port silently
  moves the app somewhere sidepage isn't proxying — and an explicit
  `launch(server_port=...)` in the script ignores the env var entirely.
  Same class of problem as FastAPI scripts hardcoding `uvicorn.run(...)`,
  which is why both get the bypass treatment.
- **`gradio.routes.App.create_app(blocks)` alone is not enough.** Served
  directly under `uvicorn --factory`, its API routes answer but `GET /`
  returns 500 — the index template renders against a `config` that is
  only populated by the launch/mount path (`jinja2.UndefinedError: 'None'
  has no attribute 'get'`). `mount_gradio_app` is the supported
  entrypoint and does that setup.
- **Gradio needs no CORS/Host relaxation**, unlike every other wrapped
  framework here (Streamlit, Jupyter, both MCP packages). Its
  `CustomCORSMiddleware` only rejects when the `Host` the wrapped process
  sees is a loopback alias, and in that case the browser's `Origin` is
  that same loopback address. `strict_cors` is deliberately left at its
  default; the reasoning is recorded in `process.py` so the absent bypass
  doesn't read as an oversight.

**Still open:** only gradio 6.26.0 has actually been run. `mount_gradio_app`
is long-standing public API and `ssr_mode` exists on it in 6.x, but
whether the wrapper works unchanged against gradio 4.x and 5.x is
untested, and `Blocks.launch`'s `(app, local_url, share_url)` return shape
(which the capturing stand-in mimics) has not been checked on those
majors either. The `--with gradio` requirement is deliberately left
unpinned, matching every other launcher's package spec, so a target whose
own `requirements.txt` pins an older Gradio will resolve to that version —
i.e. the untested path is reachable today, it just isn't claimed as
supported. Resolution is to run the existing fixtures against one older
major and either widen the claim or pin a floor.

**Affects:** `sidepage.core.target`, `sidepage.core.process`, `sidepage.core.ecosystem`

---

### 19. `sidepage pull`: remote sources, and the security surface they open

`sidepage pull <source>` fetches a Hugging Face Space into
`SIDEPAGE_HOME/apps/<name>`, resolves a run plan, registers it with
provenance, and prints the plan. It executes nothing. `serve` then gates
execution behind a per-commit confirmation
(`sidepage.commands.serve._require_source_trust`). Verified end to end
against a live Space (`Anvarbekkk/real-time-stock-predictor`).

**Findings that decided the design**, all verified rather than assumed:

- **The metadata API answers every gating question before any content is
  downloaded** — `sdk`, `sdk_version`, `app_file`, commit sha, requested
  hardware tier, upstream stage, private/gated, and the full file list
  with per-file sizes and LFS digests. That's what makes "refuse a Docker
  or GPU Space without fetching it" and "report the download size first"
  possible rather than aspirational.
- **`git clone` was rejected on evidence.** On a machine without
  `git-lfs` installed, cloning an LFS-backed Space *succeeds* and leaves
  every model weight as a ~130-byte text pointer; the app then fails at
  runtime with a nonsensical error. Reproduced directly. Fetching over
  `resolve/<sha>/<path>` returns the real bytes, needs neither `git` nor
  `git-lfs`, and lets each large file be checked against the sha256 the
  API declared beforehand.
- **A nonexistent Space returns `401`, not `404`** — the Hub answers
  identically for missing and private repos so it doesn't leak which
  private ones exist. Surfacing its raw "Invalid username or password"
  would send someone hunting for credentials they don't need.
- **Hardware is allowlisted (`cpu-*`), not denylisted.** Verified tiers:
  `cpu-basic`, `cpu-upgrade`, `cpu-xl` runnable; `zero-a10g` is ZeroGPU.
  A denylist would silently pass whatever accelerator tier ships next.

**Deliberately not done** (each a decision, not an oversight): no version
tracking — `pull` always fetches the source's current state, there's no
`--ref` and no pinning; no dependency installation or resolution —
sidepage is the installer, not the dependency manager, so a Space's
`sdk_version` is displayed but never forced onto the launcher, and a slow
first `serve` for an 80-package TensorFlow app is the app's own weight;
no secret granting — requested env names are displayed, nothing is bound
until `serve --env`.

**Open items, carried deliberately:**

1. **Symlink containment is enforced, but the general "shortcut" surface
   isn't closed.** `pull.safe_relative_path` resolves an `app_file` and
   refuses one landing outside the app directory, symlinks included. Not
   yet considered: hardlinks, a repo shipping a `.pth` file that mutates
   `sys.path` at interpreter start, a `sitecustomize.py`, or a
   `pyproject.toml`/`requirements.txt` whose *contents* point at a local
   path or a VCS URL. Sidepage hands the dependency file to `uv`
   unexamined by design, so a hostile requirements file is currently a
   real, unmitigated path. The trust gate is what stands between that and
   execution — which is why the gate is per-commit and refuses
   non-interactively rather than being a one-time formality.
2. **The env-name scan is a heuristic.** `os.environ[...]`/`os.getenv(...)`
   with a literal name only. A name built at runtime isn't reported, and
   a name in dead code is. It exists to make a request *visible*, never
   to be exhaustive — nothing is granted on its basis either way.
3. **No Hub authentication**, so private and gated Spaces are refused
   rather than supported. Adding it means holding an HF token, which
   belongs in the vault by name like every other credential — the design
   is obvious, it just isn't built.
4. **Only Hugging Face.** GitHub, MCP registry names, and local paths are
   recognized well enough to refuse with a specific message. GitHub in
   particular will need a genuinely different transport (git), which is
   why source-specific knowledge lives in `sidepage.core.hf` rather than
   in `pull`'s generic plumbing.
5. **Gradio version fidelity is now on the critical path**, not a filed
   caveat. Real Spaces pin `4.43.0`, `5.25.2`, `5.29.0` — see #18. A
   pulled Space whose `requirements.txt` pins Gradio 4.x will exercise
   the wrapper against a major that has never been tested.
6. **No size ceiling.** `pull` reports the download size and `--dry-run`
   shows it without fetching, but nothing refuses an 80 GB Space. A
   threshold with an explicit override is the obvious next step.
7. **The hardware gate reads `runtime.hardware.requested`, which is a
   hosting choice, not a requirement.** Shipped as a hard refusal first;
   corrected to a caution with `--ignore-hardware` after verifying that
   `@spaces.GPU` is inert off Hugging Face — a ZeroGPU Space really does
   run locally, on CPU. What sidepage would actually like to know is
   "will this fit on this machine", and the tier is only a weak proxy:
   it says nothing about model size, and a `cpu-basic` Space can still be
   far too slow to use. A sharper signal is available and unused — the
   dependency file and the LFS blob sizes are both visible in the
   metadata *before* download, so "this Space carries 40 GB of weights"
   is answerable directly rather than inferred from a billing tier. The
   related idea of a declared local hardware profile (tell sidepage what
   the machine has, let it decide) is deliberately not built: there's no
   consumer for it yet beyond this one check, and inventing a hardware
   description format for a single warning would be the wrong shape.
8. **Repository size is not runtime size, and the plan can't say so.**
   Most Spaces are a few KB of code that call `snapshot_download` or
   `from_pretrained` on first request — `mlx-community/supertonic-3` is
   28 KB of repo and an unknown number of GB once running. `pull
   --dry-run` honestly reports what *it* will fetch; it cannot report
   what the app will fetch when it runs, and shouldn't pretend to. Worth
   stating in the output rather than leaving a user to infer that "28 KB"
   means the whole cost.

**Two bugs found by pulling a real Space and running it**, both of which
only surface once sidepage is asked to run code it didn't write:

- **A third Gradio script shape defeated the wrapper.** Beyond the
  unguarded `demo.launch()` and the guarded one, real Spaces use a
  *factory*: `def build_ui(): ... return demo` with
  `if __name__ == "__main__": build_ui().launch()`. Nothing at module
  level ever holds a Blocks, so both an import and a namespace scan find
  nothing (`JacobPEvans/mlx-benchmarks-viewer`). Fixed by running the
  target the way `python <file>` would — `runpy.run_path(...,
  run_name="__main__")` with `launch` already patched — which is also
  exactly what Hugging Face itself does to an `app_file`, so it's the
  most faithful emulation rather than a new special case.
- **The upstream readiness poll gave up after 20 seconds, permanently.**
  `check_upstream_ready` had a fixed deadline and was never retried, so
  any app whose first run installs a large dependency tree came up fine
  while the proxy served its "starting…" holding page *forever*. Latent
  before `pull` — a local Streamlit app resolves in seconds — and
  guaranteed after it, since pulled Spaces routinely pin heavy stacks.
  The lazy-start path now polls without a deadline; a wall-clock limit
  was simply the wrong shape for "waiting on `uv`".

**Affects:** `sidepage.core.hf`, `sidepage.core.pull`,
`sidepage.commands.pull`, `sidepage.commands.serve`,
`sidepage.core.app_registry`, `sidepage.core.process`,
`sidepage.core.reverse_proxy`
