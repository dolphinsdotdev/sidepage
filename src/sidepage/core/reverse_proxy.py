"""Local reverse proxy — backs the auth/metering/serving guarantees in spec
v3 §9, sitting between the tunnel and the wrapped app's real port.

Real implementation covers the two prioritized cases: serving a static
directory in-process, and proxying HTTP + WebSocket traffic to a wrapped
subprocess. `open`/`token` auth are enforced for real; `network`/`oauth`
are not (see `sidepage.core.auth` — `network` needs IP-allowlist/mTLS logic
this build doesn't have, `oauth` is deferred spec-wide). Graceful drain on
shutdown is still not built — teardown is immediate, per the confirmed
default.

Implementation: Starlette (ASGI app, routing, `StaticFiles`), uvicorn (ASGI
server, run in a background thread so the CLI's main thread can do its own
blocking wait for Ctrl+C), httpx (streaming HTTP proxy client, `aiter_raw`
paired with forwarded original headers so content-encoding stays
consistent), `websockets` (outbound WS client used to proxy to the wrapped
process — Starlette/httpx don't provide one).

Auth gate: `Authorization: Bearer <token>` header or `?token=` query param
for programmatic callers; a minimal HTML form (`/__sidepage_auth`, plain
urlencoded POST — deliberately not using `request.form()` to avoid a
`python-multipart` dependency for one form) sets a session cookie for
browsers, valid until the process stops (no separate timer, no rotation —
matching `sidepage.core.token_runtime`).

Usage counts (`sidepage.core.usage_reporter`) are persisted to a JSON file
per app under `sidepage.config.settings.runtime_dir()` on every request/
message — simple, not high-throughput-optimized, appropriate for a local
dev tool.

v5 adds three things, all composing with the proxy contract above rather
than replacing it:
- `ActivityTracker`/`ActivityMiddleware`: last-request timestamp, touched
  on every HTTP request and WS message, exposed on `ProxyHandle.activity`
  — backs `sidepage.core.process.serve`'s `--idle-timeout` (spec v5 §20).
- `start_proxy(..., start_upstream=...)`: when given, `subprocess.Popen`
  for a CODE/NOTEBOOK target is deferred out of `serve` entirely and fired
  once, behind a start-once lock, on the first inbound request/WS connect
  — the existing `ready`-Event/holding-page mechanism is reused verbatim,
  just triggered by traffic instead of by `serve` itself starting (spec v5
  §21 Tier 1, lazy start).
- `GET /.sidepage/peers.json` in `_build_proxy_app`'s own route table, so
  it inherits the app's `--auth` gate automatically — re-resolves each
  configured `--peer` against `sidepage.core.registry` on every request,
  so a peer that restarts mid-session with a fresh tunnel URL is never
  stale the way a boot-time env var would be (spec v5 `--peer`).

Forwarded headers (Caddy-style, not nginx's bare default): `X-Forwarded-
Host`/`-For`/`-Proto` are set on both the HTTP path (`proxy_http`) and
the WS handshake (`proxy_ws`, via `websockets.connect`'s
`additional_headers`); the real inbound `Host` itself is also passed
through as the literal `Host` header, but **HTTP only** — see
`_forwarded_headers`'s `override_host` parameter. This exists so an
upstream that's configured to trust its proxy (Django's
`USE_X_FORWARDED_HOST`, Flask's `ProxyFix`, Rails' default
`trusted_proxies`, Express's `trust proxy`, ...) generates correct
absolute URLs/redirects/cookies regardless of whether it's reached
directly or through a BYO-domain/anon tunnel — nginx's un-configured
default (nothing forwarded, `Host` silently clobbered) leaves even a
fully proxy-aware app with no header to trust, which is strictly worse,
not just equally manual. This is necessary but not sufficient: an
upstream that validates `Origin`/`Host` against a fixed allowlist (Vite's
`server.allowedHosts`, Django's `ALLOWED_HOSTS`) still needs that
allowlist updated on its own side — see `sidepage.commands.proxy` for the
user-facing caveat. The literal `Host` override is WS-exempt because it's
been verified live, against this project's own notebook fixture, to
break exactly that class of check: Jupyter (Tornado) validates `Host` on
the WS upgrade more strictly than on plain HTTP and rejected a forwarded
real hostname outright, closing the connection — the same DNS-rebinding-
style check Vite's `allowedHosts` does, just hit for real here instead of
only in theory. `X-Forwarded-Proto` passes through an already-present
value if the request arrived with one (e.g. from a fronting layer that
sets it) and otherwise falls back to this proxy's own scope scheme
(`http` — this process never terminates TLS itself); that fallback
hasn't been verified against a real `cloudflared` tunnel's actual
behavior, so treat it as the honest default rather than a
confirmed-correct one.

Upstream dial: `127.0.0.1` first, falling back to `[::1]` (IPv6 loopback)
if that never answers (`check_upstream_ready`, `UpstreamAddress`) —
resolved once during the readiness poll, then reused for every
subsequent request/connection. Exists because `sidepage.commands.proxy`
wraps a process it never launched and can't control the bind address
of; verified live that a bare `npm run dev` (no `--host`) binds Vite's
dev server to IPv6 loopback only, which `serve`'s own launchers never do
since sidepage passes an explicit `--host`/`--server.address 127.0.0.1`
at spawn time for every framework it has a real launcher for.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from sidepage.config.settings import ensure_dirs, runtime_dir
from sidepage.core import registry
from sidepage.core.exceptions import PeerNotFoundError

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}

_COOKIE_NAME = "sidepage_session"

_GATE_PAGE = """<!doctype html>
<html><head><title>sidepage — sign in</title></head>
<body style="font-family: system-ui; max-width: 420px; margin: 80px auto;">
<h2>This app is locked</h2>
<form method="post" action="/__sidepage_auth"
      style="display:flex; gap:8px;">
  <input type="password" name="token" placeholder="Access token" autofocus
         style="flex:1; padding: 8px; font-size: 16px;">
  <button type="submit" style="padding: 8px 16px;">Enter</button>
</form>
</body></html>"""

_HOLDING_PAGE = """<!doctype html>
<html><head><meta http-equiv="refresh" content="1"><title>starting…</title></head>
<body style="font-family: system-ui; max-width: 420px; margin: 80px auto;">
<p>Starting your app… this page refreshes automatically.</p>
</body></html>"""


def _parse_cookies(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookies[k] = v
    return cookies


def _forwarded_headers(
    headers: dict[str, str],
    request_headers: Headers,
    client_host: str | None,
    *,
    override_host: bool,
) -> None:
    """Mutates `headers` (already stripped of hop-by-hop names, see
    `_HOP_BY_HOP`) in place, adding the `X-Forwarded-*` trio and,
    when `override_host` is set, a Caddy-style `Host` too — see this
    module's docstring for why. Shared by `proxy_http` and `proxy_ws` so
    both the HTTP path and the WS handshake carry the same forwarded
    identity.

    `override_host=False` on the WS path specifically (see `proxy_ws`):
    verified live against this project's own notebook fixture that a real
    Tornado-based server (Jupyter) validates the literal `Host` header on
    the WS upgrade more strictly than on plain HTTP — the same
    DNS-rebinding-style allowlist check Vite's `server.allowedHosts` does
    (see `sidepage.commands.proxy`'s `--help`) — and rejects a forwarded
    real hostname outright, breaking the handshake. `X-Forwarded-Host`
    carries the real value for anything that reads it without that risk;
    only the literal `Host` override is what a naive allowlist check
    inspects, so only that one is skipped here.
    """
    inbound_host = request_headers.get("host")
    if inbound_host:
        if override_host:
            headers["host"] = inbound_host
        headers["x-forwarded-host"] = inbound_host
    if client_host:
        existing_xff = request_headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = f"{existing_xff}, {client_host}" if existing_xff else client_host
    headers["x-forwarded-proto"] = request_headers.get("x-forwarded-proto") or "http"


class ActivityTracker:
    """Last-request-or-WS-message timestamp for one served app — backs
    `--idle-timeout` (spec v5 §20). A single `float` attribute rather than
    anything lock-protected: CPython's GIL already makes a bare attribute
    assignment atomic, and the consumer (`process.serve`'s blocking loop)
    only ever needs an approximate, monotonically-fresh read, not a
    strictly synchronized one.
    """

    def __init__(self) -> None:
        self._last = time.time()

    def touch(self) -> None:
        self._last = time.time()

    def last(self) -> float:
        return self._last


class ActivityMiddleware:
    """Raw ASGI middleware, same shape as `AuthGateMiddleware` below:
    touches `tracker` once per HTTP request and once per WS *connection*.
    Message-level granularity within one long-lived WS connection (what
    `--idle-timeout`'s "resets on each proxied ... WS message" promises)
    is handled separately, by explicit `activity.touch()` calls inside
    `_build_proxy_app`'s own per-message forwarding loops — an ASGI
    `websocket` scope is only entered once per connection, so this outer
    layer alone can't see individual messages.

    Deliberately outermost (wraps `AuthGateMiddleware`, not the reverse):
    even a request that fails auth is still a human or client actively
    poking the app, so it should still count as "not idle."
    """

    def __init__(self, app, tracker: ActivityTracker) -> None:
        self.app = app
        self.tracker = tracker

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            self.tracker.touch()
        await self.app(scope, receive, send)


class AuthGateMiddleware:
    """Raw ASGI middleware — guards both `http` and `websocket` scopes with
    one auth check, since Starlette's convenience `BaseHTTPMiddleware` only
    covers `http`."""

    def __init__(self, app, *, auth: str, token: str | None) -> None:
        self.app = app
        self.auth = auth
        self.token = token

    async def __call__(self, scope, receive, send) -> None:
        if self.auth != "token" or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        cookies = _parse_cookies(headers.get("cookie", ""))
        query = parse_qs(scope.get("query_string", b"").decode())
        auth_header = headers.get("authorization", "")
        bearer = auth_header[7:] if auth_header.startswith("Bearer ") else None

        authed = (
            bearer == self.token
            or query.get("token", [None])[0] == self.token
            or cookies.get(_COOKIE_NAME) == self.token
        )

        if scope["type"] == "websocket":
            if not authed:
                websocket = WebSocket(scope, receive=receive, send=send)
                await websocket.close(code=4401)
                return
            await self.app(scope, receive, send)
            return

        if authed or scope["path"] in ("/__sidepage_auth", "/.sidepage/stop"):
            # `/.sidepage/stop` deliberately bypasses the public gate: it
            # enforces its own, separate control-token check (see
            # `_stop_route`) regardless of this app's `--auth` tier — an
            # `--auth open` app still needs `sidepage stop` to work, and
            # the control token is a stronger, single-purpose secret, not
            # a bypass of the public one.
            await self.app(scope, receive, send)
            return

        response = HTMLResponse(_GATE_PAGE, status_code=401)
        await response(scope, receive, send)


def counts_path(app_name: str) -> Path:
    ensure_dirs()
    return runtime_dir() / f"{app_name}-usage.json"


def _persist_counts(app_name: str, counts: dict[str, int]) -> None:
    counts_path(app_name).write_text(json.dumps(counts))


def _stop_route(stop_requested: threading.Event, control_token: str | None) -> Route:
    """`POST /.sidepage/stop` — the cross-platform substitute for a
    cross-process `SIGTERM` (see `sidepage.core.process` for why POSIX
    doesn't need this but Windows does: a `SIGTERM` sent via `os.kill`
    from a *different* process there calls `TerminateProcess` directly,
    never reaching this process's own Python-level signal handler). Sets
    `stop_requested`, which `process.serve`/`process.proxy`'s existing
    blocking loop already polls once a second alongside `--timeout`/
    `--idle-timeout` — same `break` -> `finally: _teardown()` path
    Ctrl+C/SIGTERM/timeout all already use, no second teardown route.

    Gated by `control_token`, not by `--auth` or by "did this arrive from
    127.0.0.1" — the latter can't be trusted here any more than it can
    for `sidepage proxy` (a request arriving through the tunnel looks
    identical to a local one by the time it reaches this process, see
    `sidepage.commands.proxy --help`). `control_token=None` (shouldn't
    happen outside tests that construct the app directly) means the route
    is permanently unreachable rather than open.
    """

    async def stop_app(request: Request) -> JSONResponse:
        header_token = request.headers.get("x-sidepage-control-token")
        if control_token is None or header_token != control_token:
            return JSONResponse({"error": "forbidden"}, status_code=403)
        stop_requested.set()
        return JSONResponse({"status": "stopping"})

    return Route("/.sidepage/stop", stop_app, methods=["POST"])


def _auth_post_handler(token: str | None):
    async def auth_post(request: Request) -> RedirectResponse | HTMLResponse:
        body = await request.body()
        submitted = parse_qs(body.decode()).get("token", [None])[0]
        if submitted == token:
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie(_COOKIE_NAME, token, httponly=True, samesite="lax")
            return resp
        return HTMLResponse(_GATE_PAGE, status_code=401)

    return auth_post


def _build_static_app(
    static_root: Path,
    *,
    auth: str,
    token: str | None,
    stop_requested: threading.Event,
    control_token: str | None,
):
    app = Starlette(
        routes=[
            Route("/__sidepage_auth", _auth_post_handler(token), methods=["POST"]),
            _stop_route(stop_requested, control_token),
        ]
    )
    app.mount("/", StaticFiles(directory=str(static_root), html=True))
    return AuthGateMiddleware(app, auth=auth, token=token)


def _build_proxy_app(
    *,
    app_name: str,
    upstream_port: int,
    upstream_host: UpstreamAddress,
    auth: str,
    token: str | None,
    ready: threading.Event,
    counts: dict[str, int],
    activity: ActivityTracker,
    stop_requested: threading.Event,
    control_token: str | None,
    start_upstream: Callable[[], None] | None = None,
    peers: tuple[tuple[str, str], ...] = (),
):
    client = httpx.AsyncClient(timeout=30.0)

    started = False
    start_lock = threading.Lock()

    def _ensure_started() -> None:
        """Lazy start (spec v5 §21 Tier 1): if `start_upstream` was given,
        the wrapped subprocess hasn't been launched at all yet — this is
        the start-once trigger, fired on the first inbound request or WS
        connect. `started` is read outside the lock as a fast path (a
        stale `False` just means an extra lock acquisition, never a
        double-start, since the check inside the lock is authoritative)
        and only ever set inside it."""
        nonlocal started
        if start_upstream is None or started:
            return
        with start_lock:
            if started:
                return
            started = True
            start_upstream()

            def _poll_ready() -> None:
                resolved = check_upstream_ready(upstream_port)
                if resolved is not None:
                    upstream_host.host = resolved
                    ready.set()

            threading.Thread(target=_poll_ready, daemon=True).start()

    async def peers_json(request: Request) -> JSONResponse:
        resolved: dict[str, str | None] = {}
        for role, peer_app_name in peers:
            try:
                resolved[role] = registry.resolve_peer_url(peer_app_name)
            except PeerNotFoundError:
                resolved[role] = None
        return JSONResponse(resolved)

    async def proxy_http(request: Request):
        _ensure_started()
        activity.touch()
        counts["http_requests"] = counts.get("http_requests", 0) + 1
        if not ready.is_set():
            _persist_counts(app_name, counts)
            return HTMLResponse(_HOLDING_PAGE)

        upstream_url = f"http://{upstream_host.host}:{upstream_port}{request.url.path}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        client_host = request.client.host if request.client is not None else None
        _forwarded_headers(headers, request.headers, client_host, override_host=True)
        body = await request.body()

        try:
            upstream_req = client.build_request(
                request.method, upstream_url, headers=headers, content=body
            )
            resp = await client.send(upstream_req, stream=True)
        except httpx.TransportError:
            _persist_counts(app_name, counts)
            return HTMLResponse(_HOLDING_PAGE, status_code=502)

        counts["http_responses"] = counts.get("http_responses", 0) + 1
        _persist_counts(app_name, counts)
        response_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return StreamingResponse(
            resp.aiter_raw(), status_code=resp.status_code, headers=response_headers
        )

    async def proxy_ws(websocket: WebSocket) -> None:
        _ensure_started()
        await websocket.accept()
        activity.touch()
        counts["ws_connections"] = counts.get("ws_connections", 0) + 1
        _persist_counts(app_name, counts)
        upstream_url = f"ws://{upstream_host.host}:{upstream_port}{websocket.url.path}"
        ws_headers: dict[str, str] = {}
        client_host = websocket.client.host if websocket.client is not None else None
        _forwarded_headers(ws_headers, websocket.headers, client_host, override_host=False)

        try:
            async with websockets.connect(
                upstream_url, additional_headers=ws_headers
            ) as upstream_ws:

                async def client_to_upstream() -> None:
                    try:
                        while True:
                            msg = await websocket.receive()
                            if msg["type"] == "websocket.disconnect":
                                break
                            if msg.get("text") is not None:
                                await upstream_ws.send(msg["text"])
                                activity.touch()
                                counts["ws_messages"] = counts.get("ws_messages", 0) + 1
                            elif msg.get("bytes") is not None:
                                await upstream_ws.send(msg["bytes"])
                                activity.touch()
                                counts["ws_messages"] = counts.get("ws_messages", 0) + 1
                    except WebSocketDisconnect:
                        pass

                async def upstream_to_client() -> None:
                    async for message in upstream_ws:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)
                        activity.touch()
                        counts["ws_messages"] = counts.get("ws_messages", 0) + 1

                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except Exception:
            pass
        finally:
            _persist_counts(app_name, counts)

    routes = [
        Route("/__sidepage_auth", _auth_post_handler(token), methods=["POST"]),
        # Ahead of the catch-all routes below so it wins the match, and
        # inside this same route table (not a separate mount) so it
        # inherits the AuthGateMiddleware wrap applied to `app` as a
        # whole — same auth tier as the rest of this app, no separate gate.
        Route("/.sidepage/peers.json", peers_json, methods=["GET"]),
        _stop_route(stop_requested, control_token),
        WebSocketRoute("/{path:path}", proxy_ws),
        Route(
            "/{path:path}",
            proxy_http,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        ),
        Route(
            "/", proxy_http, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
        ),
    ]
    app = Starlette(routes=routes)
    return AuthGateMiddleware(app, auth=auth, token=token)


@dataclass
class ProxyHandle:
    app_name: str
    listen_port: int
    upstream_port: int | None  # None for static targets (served in-process)
    server: uvicorn.Server = field(repr=False)
    thread: threading.Thread = field(repr=False)
    ready: threading.Event = field(repr=False)
    activity: ActivityTracker = field(repr=False)
    # Set by a `POST /.sidepage/stop` call (see `_stop_route`) — the
    # cross-platform substitute for a cross-process SIGTERM that
    # `sidepage.core.process`'s blocking loop polls on Windows, where a
    # signal sent via `os.kill` from another process can't reach this
    # one's own signal handler.
    stop_requested: threading.Event = field(repr=False)


_LOOPBACK_CANDIDATES = ("127.0.0.1", "[::1]")


class UpstreamAddress:
    """Resolved loopback host for one proxied upstream — `127.0.0.1` by
    default (every existing `serve` launcher already binds there), only
    ever overwritten if that default never answered and `[::1]` (IPv6
    loopback) did, per `check_upstream_ready`. A single attribute,
    GIL-atomic like `ActivityTracker` — written once by the readiness
    poll, read on every request/connection by `proxy_http`/`proxy_ws`.
    """

    def __init__(self) -> None:
        self.host = "127.0.0.1"


def check_upstream_ready(upstream_port: int, *, timeout: float = 20.0) -> str | None:
    """Real HTTP GET readiness check — not a bare TCP connect, since some
    frameworks (Streamlit included) bind the socket before they're
    actually ready to serve.

    Tries `127.0.0.1` first, falling back to `[::1]` (IPv6 loopback) if
    that one never answers — verified live that some already-running
    services `sidepage proxy` wraps (a bare `npm run dev` Vite dev
    server, with no way for sidepage to control how it was launched the
    way it can for `serve`'s own targets) bind IPv6 loopback only.
    Returns whichever host actually answered, or `None` if neither did
    within `timeout` — never guesses.
    """
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            for host in _LOOPBACK_CANDIDATES:
                try:
                    client.get(f"http://{host}:{upstream_port}/")
                    return host
                except httpx.TransportError:
                    continue
            time.sleep(0.2)
    return None


def start_proxy(
    app_name: str,
    *,
    listen_port: int,
    upstream_port: int | None = None,
    static_root: Path | None = None,
    auth: str = "open",
    token: str | None = None,
    control_token: str | None = None,
    start_upstream: Callable[[], None] | None = None,
    peers: tuple[tuple[str, str], ...] = (),
) -> ProxyHandle:
    """Start the local reverse proxy. Exactly one of `upstream_port`
    (proxy to a wrapped subprocess) or `static_root` (serve in-process)
    must be given.

    `start_upstream`, if given (CODE/NOTEBOOK only — never paired with
    `static_root`), defers launching the wrapped subprocess out of `serve`
    entirely: instead of this function kicking off the readiness-poll
    thread immediately, `_build_proxy_app` calls `start_upstream()` itself,
    once, on the first inbound request or WS connect (spec v5 §21 Tier 1).
    Until then `ready` never gets set, so callers see the same holding
    page they'd see during a slow boot either way — lazy start changes
    *when* the subprocess launches, not the readiness contract.

    `peers`, if given, backs the live `GET /.sidepage/peers.json` endpoint
    (spec v5 `--peer`) — ignored when `static_root` is set, since that
    route only exists in `_build_proxy_app`'s table.

    `control_token`, if given, is the secret `POST /.sidepage/stop`
    requires (see `_stop_route`) — `sidepage.core.process`'s cross-process
    Windows stop path. `None` (the default, and what every call site not
    passing it explicitly gets) leaves the route permanently unreachable
    rather than open, so tests/callers that don't care about this are
    unaffected.
    """
    if (upstream_port is None) == (static_root is None):
        raise ValueError("exactly one of upstream_port or static_root is required")

    ready = threading.Event()
    activity = ActivityTracker()
    stop_requested = threading.Event()
    if static_root is not None:
        app = _build_static_app(
            static_root,
            auth=auth,
            token=token,
            stop_requested=stop_requested,
            control_token=control_token,
        )
        ready.set()
    else:
        counts: dict[str, int] = {}
        upstream_host = UpstreamAddress()
        app = _build_proxy_app(
            app_name=app_name,
            upstream_port=upstream_port,
            upstream_host=upstream_host,
            auth=auth,
            token=token,
            ready=ready,
            counts=counts,
            activity=activity,
            stop_requested=stop_requested,
            control_token=control_token,
            start_upstream=start_upstream,
            peers=peers,
        )

        if start_upstream is None:

            def _poll_ready() -> None:
                resolved = check_upstream_ready(upstream_port)
                if resolved is not None:
                    upstream_host.host = resolved
                    ready.set()

            threading.Thread(target=_poll_ready, daemon=True).start()
        # else: the subprocess hasn't been launched yet at all — polling
        # starts only once `_build_proxy_app`'s `_ensure_started` fires it.

    app = ActivityMiddleware(app, activity)

    config = uvicorn.Config(app, host="127.0.0.1", port=listen_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    return ProxyHandle(
        app_name=app_name,
        listen_port=listen_port,
        upstream_port=upstream_port,
        server=server,
        thread=thread,
        ready=ready,
        activity=activity,
        stop_requested=stop_requested,
    )


def stop_proxy(handle: ProxyHandle, *, graceful: bool = False) -> None:
    """Tear down the proxy. `graceful` is accepted but always behaves as
    immediate teardown — the graceful-drain design (in-flight requests,
    open WS connections) is deferred, not implemented."""
    handle.server.should_exit = True
    handle.thread.join(timeout=5)
