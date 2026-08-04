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
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from sidepage.config.settings import ensure_dirs, runtime_dir

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

        if authed or scope["path"] == "/__sidepage_auth":
            await self.app(scope, receive, send)
            return

        response = HTMLResponse(_GATE_PAGE, status_code=401)
        await response(scope, receive, send)


def counts_path(app_name: str) -> Path:
    ensure_dirs()
    return runtime_dir() / f"{app_name}-usage.json"


def _persist_counts(app_name: str, counts: dict[str, int]) -> None:
    counts_path(app_name).write_text(json.dumps(counts))


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


def _build_static_app(static_root: Path, *, auth: str, token: str | None):
    app = Starlette(routes=[Route("/__sidepage_auth", _auth_post_handler(token), methods=["POST"])])
    app.mount("/", StaticFiles(directory=str(static_root), html=True))
    return AuthGateMiddleware(app, auth=auth, token=token)


def _build_proxy_app(
    *,
    app_name: str,
    upstream_port: int,
    auth: str,
    token: str | None,
    ready: threading.Event,
    counts: dict[str, int],
):
    client = httpx.AsyncClient(timeout=30.0)

    async def proxy_http(request: Request):
        counts["http_requests"] = counts.get("http_requests", 0) + 1
        if not ready.is_set():
            _persist_counts(app_name, counts)
            return HTMLResponse(_HOLDING_PAGE)

        upstream_url = f"http://127.0.0.1:{upstream_port}{request.url.path}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
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
        await websocket.accept()
        counts["ws_connections"] = counts.get("ws_connections", 0) + 1
        _persist_counts(app_name, counts)
        upstream_url = f"ws://127.0.0.1:{upstream_port}{websocket.url.path}"

        try:
            async with websockets.connect(upstream_url) as upstream_ws:

                async def client_to_upstream() -> None:
                    try:
                        while True:
                            msg = await websocket.receive()
                            if msg["type"] == "websocket.disconnect":
                                break
                            if msg.get("text") is not None:
                                await upstream_ws.send(msg["text"])
                                counts["ws_messages"] = counts.get("ws_messages", 0) + 1
                            elif msg.get("bytes") is not None:
                                await upstream_ws.send(msg["bytes"])
                                counts["ws_messages"] = counts.get("ws_messages", 0) + 1
                    except WebSocketDisconnect:
                        pass

                async def upstream_to_client() -> None:
                    async for message in upstream_ws:
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)
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


def check_upstream_ready(upstream_port: int, *, timeout: float = 20.0) -> bool:
    """Real HTTP GET readiness check — not a bare TCP connect, since some
    frameworks (Streamlit included) bind the socket before they're
    actually ready to serve."""
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                client.get(f"http://127.0.0.1:{upstream_port}/")
                return True
            except httpx.TransportError:
                time.sleep(0.2)
    return False


def start_proxy(
    app_name: str,
    *,
    listen_port: int,
    upstream_port: int | None = None,
    static_root: Path | None = None,
    auth: str = "open",
    token: str | None = None,
) -> ProxyHandle:
    """Start the local reverse proxy. Exactly one of `upstream_port`
    (proxy to a wrapped subprocess) or `static_root` (serve in-process)
    must be given."""
    if (upstream_port is None) == (static_root is None):
        raise ValueError("exactly one of upstream_port or static_root is required")

    ready = threading.Event()
    if static_root is not None:
        app = _build_static_app(static_root, auth=auth, token=token)
        ready.set()
    else:
        counts: dict[str, int] = {}
        app = _build_proxy_app(
            app_name=app_name,
            upstream_port=upstream_port,
            auth=auth,
            token=token,
            ready=ready,
            counts=counts,
        )

        def _poll_ready() -> None:
            if check_upstream_ready(upstream_port):
                ready.set()

        threading.Thread(target=_poll_ready, daemon=True).start()

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
    )


def stop_proxy(handle: ProxyHandle, *, graceful: bool = False) -> None:
    """Tear down the proxy. `graceful` is accepted but always behaves as
    immediate teardown — the graceful-drain design (in-flight requests,
    open WS connections) is deferred, not implemented."""
    handle.server.should_exit = True
    handle.thread.join(timeout=5)
