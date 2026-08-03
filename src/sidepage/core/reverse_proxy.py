"""Local reverse proxy — backs the auth/metering/serving guarantees in spec
v3 §9. The single biggest net-new architectural piece in v3: sits between
the tunnel and the wrapped app's real port, on the user's own machine.

This is explicitly **not** the no-MITM cloud backend from §6/§7 — that
constraint is about Sidepage's *cloud* side never inspecting payloads. A
local proxy the user's own process controls, inspecting traffic before it
ever reaches the tunnel, doesn't cross that line. This is what makes
`--auth token` enforceable without the wrapped app needing any
Sidepage-specific code (§4, §8).

**Responsibilities:**
  - **Auth enforcement + gate page** (§8) — browser-facing targets (static,
    Streamlit, notebook Lab) get a minimal gate page when no valid session
    cookie is present; a valid token submit sets a session cookie scoped to
    the app's subdomain and forwards to the real target for the rest of the
    session. Session validity: **until app stop**, no separate timer —
    consistent with the no-rotation policy on the token itself
    (`sidepage.core.token_runtime`). Programmatic callers (API/MCP clients)
    use a header (`Authorization: Bearer <token>`) or query param instead —
    no page rendered. WebSocket upgrades ride on the same cookie set during
    initial page load; no separate WS auth path, since login always
    precedes WS connection open in both Streamlit and Jupyter.
  - **Request/connection counting** (§7) — the proxy is what actually
    observes traffic, which is why usage counts can be proxy-observed
    rather than self-reported; see `sidepage.core.usage_reporter`.
  - **Startup holding page** — while upstream isn't ready yet, valid-session
    requests get a lightweight auto-refreshing "starting…" page instead of
    a connection error. Client-side polling/refresh, *not* the proxy
    holding the TCP connection open — avoids client/proxy timeout issues
    during slow startups (e.g. notebook kernel boot).
  - **WebSocket proxying** — required, not optional. Streamlit's reactivity
    and Jupyter's kernel messaging both run over WS; naive HTTP-only
    forwarding silently breaks both targets.
  - **Streaming passthrough** — relay bytes as they arrive, don't buffer
    full bodies. Needed for MCP streamable-HTTP and large notebook outputs.

**Readiness check:** a real HTTP GET expecting a valid response, not a bare
TCP connect — some frameworks bind the socket before they're actually ready
to serve.

**Implementation (once built):** Starlette + `httpx.AsyncClient`, reusing
the same stack as static serving (`sidepage.core.static`) rather than
introducing a second HTTP library — Starlette has native WS support and
httpx streams responses cleanly. Not a dependency yet; named here as intent
only, consistent with how the rest of `sidepage.core` documents its
eventual implementation without installing it.

> **Open question (deferred, not blocking):** graceful drain vs. hard kill
> on `stop` — immediate stays default for now; whether a short drain window
> for in-flight requests/open WS connections gets added later is
> unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyHandle:
    app_name: str
    listen_port: int
    upstream_port: int


def start_proxy(app_name: str, *, upstream_port: int) -> ProxyHandle:
    """Start the local reverse proxy in front of `upstream_port`, wiring up
    auth enforcement, the startup holding page, WS proxying, streaming
    passthrough, and request/connection counting.

    Not implemented.
    """
    raise NotImplementedError


def check_upstream_ready(upstream_port: int) -> bool:
    """Real HTTP GET readiness check against the wrapped app — not a bare
    TCP connect, since some frameworks bind the socket before they're
    actually ready to serve.

    Not implemented.
    """
    raise NotImplementedError


def stop_proxy(handle: ProxyHandle, *, graceful: bool = False) -> None:
    """Tear down the proxy. `graceful` is accepted but currently always
    behaves as immediate teardown — the graceful-drain design (in-flight
    requests, open WS connections) is deferred, not implemented; see this
    module's docstring.

    Not implemented.
    """
    raise NotImplementedError
