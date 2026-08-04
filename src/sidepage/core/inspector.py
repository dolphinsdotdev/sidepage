"""Interactive HTTP inspection console — backs `sidepage inspect
[<app-name-or-url>]` (spec v3 §10).

**Scope for this pass, deliberately narrowed:** generic HTTP/static
inspection only — issuing ad-hoc requests against any app `sidepage serve`
already wraps (static sites, generic code targets), browsing its live
usage counts, and replaying the last request. Full MCP tool introspection
(schemas, `tools/list`, `tools/call` over JSON-RPC) is the spec's actual
"Postman-for-MCP" framing, but needs a real MCP client and a real MCP
server fixture to build against responsibly — neither of the two
prioritized fixtures (static site, Streamlit app) is an MCP server. Parked
explicitly, not silently dropped — see `docs/CHECKLIST.md` and
`docs/OPEN_QUESTIONS.md`.

Directory-aware: with no argument, lists this machine's running apps
(`sidepage.core.registry`) to pick from — there's no cloud directory to
query (see `sidepage.core.directory_client`), the same substitution `ls`/
`status` already make.

**Resolved in v3:** no auth bypass for the local operator. Same credential
required as any caller — this was an open question in v1, and v3 confirms
the stricter answer, so the tool that verifies auth-tier compliance
doesn't itself skip the gate. It's still convenient: when inspecting an
app registered on this machine, the credential is auto-sourced from the
token runtime file (`sidepage.core.token_runtime.read_runtime_file`)
rather than typed in — the same file `serve` writes on `--auth token`,
absent for `--auth open` apps.

Also surfaces live usage counts from `sidepage.core.usage_reporter` — the
same counters `sidepage usage` reports as a standing snapshot, but for the
one app currently being inspected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from sidepage.core import registry, usage_reporter
from sidepage.core.exceptions import DirectoryError, InspectorTargetError
from sidepage.core.registry import RunningApp
from sidepage.core.token_runtime import read_runtime_file
from sidepage.output import error, info, stdout


@dataclass
class ConsoleSession:
    base_url: str
    app: RunningApp | None
    token: str | None
    extra_headers: dict[str, str] = field(default_factory=dict)
    last_request: tuple[str, str, str | None] | None = None  # method, path, body


def _auto_token(app: RunningApp) -> str | None:
    """Best-effort: the runtime token file only exists if `app` was started
    with `--auth token` — its absence just means open auth, not an error."""
    try:
        return read_runtime_file(app.name, app.pid).value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def resolve_target(target: str) -> ConsoleSession:
    """Resolve `target` — a locally-registered app name, or a raw
    `http(s)://` URL — to a console session. App names get credentials
    auto-sourced from the runtime token file; raw URLs don't (nothing to
    source them from).

    Raises `sidepage.core.exceptions.InspectorTargetError` if `target` is
    neither.
    """
    app = registry.get(target)
    if app is not None:
        return ConsoleSession(base_url=app.url, app=app, token=_auto_token(app))
    if target.startswith("http://") or target.startswith("https://"):
        return ConsoleSession(base_url=target, app=None, token=None)
    raise InspectorTargetError(f"{target!r} is neither a running app name nor an http(s):// URL")


def execute_request(
    session: ConsoleSession, method: str, path: str, body: str | None = None
) -> httpx.Response:
    """Issue one request against `session.base_url`, applying the
    auto-sourced/session token (as a Bearer header, unless a caller-set
    header already provides `Authorization`) and any extra headers set via
    the console's `header` command. Records it as the replayable last
    request."""
    url = session.base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = dict(session.extra_headers)
    lower_keys = {k.lower() for k in headers}
    if session.token and "authorization" not in lower_keys:
        headers["Authorization"] = f"Bearer {session.token}"
    if body is not None and "content-type" not in lower_keys:
        headers["Content-Type"] = "application/json"

    session.last_request = (method, path, body)
    content = body.encode() if body is not None else None
    with httpx.Client(timeout=15.0) as client:
        return client.request(method, url, headers=headers, content=content)


def _print_response(resp: httpx.Response) -> None:
    color = "green" if resp.status_code < 400 else "red"
    stdout.print(f"[{color}]{resp.status_code} {resp.reason_phrase}[/{color}]")
    for k, v in resp.headers.items():
        stdout.print(f"  [dim]{k}:[/dim] {v}")
    body = resp.text
    if not body:
        return
    try:
        stdout.print(json.dumps(json.loads(body), indent=2))
    except json.JSONDecodeError:
        stdout.print(body if len(body) < 2000 else body[:2000] + "\n[dim]... truncated[/dim]")


_HELP = """\
Commands:
  get <path>              GET request
  post <path> [json]      POST request, optional JSON body
  put <path> [json]       PUT request, optional JSON body
  patch <path> [json]     PATCH request, optional JSON body
  delete <path>           DELETE request
  head <path>             HEAD request
  header <name> <value>   Set a header applied to every request this session
  replay                  Re-issue the last request
  info                    Show target info
  usage                   Show live usage counts (registered apps only)
  help                    Show this message
  quit / exit             Leave the console"""

_METHODS = {"get", "post", "put", "patch", "delete", "head"}


def _print_info(session: ConsoleSession) -> None:
    stdout.print(f"target: {session.base_url}")
    if session.app is not None:
        stdout.print(f"app:    {session.app.name} ({session.app.target_kind})")
        stdout.print(f"pid:    {session.app.pid}")
    stdout.print(f"auth:   {'token (auto-sourced)' if session.token else 'none detected'}")


def _print_usage(session: ConsoleSession) -> None:
    if session.app is None:
        error("usage counts are only available for locally-registered apps")
        return
    try:
        report = usage_reporter.get_usage(session.app.name)
    except DirectoryError as exc:
        error(str(exc))
        return
    stdout.print(f"http requests:  {report.http_request_count}")
    stdout.print(f"http responses: {report.http_response_count}")
    stdout.print(f"ws connections: {report.ws_connection_count}")
    stdout.print(f"ws messages:    {report.ws_message_count}")
    stdout.print(f"uptime:         {report.uptime_seconds}s")


def _pick_target() -> str | None:
    apps = registry.list_running()
    if not apps:
        error("no apps running on this machine")
        return None
    stdout.print("Running apps:")
    for i, app in enumerate(apps, start=1):
        stdout.print(f"  {i}) {app.name}  [dim]{app.target_kind}[/dim]  {app.url}")
    choice = input("Pick a number (Enter to cancel): ").strip()
    if not choice:
        return None
    try:
        return apps[int(choice) - 1].name
    except (ValueError, IndexError):
        error(f"invalid choice: {choice!r}")
        return None


def open_console(target: str | None = None) -> None:
    """Open an interactive console against `target` — an app name or URL.
    With `target=None`, lists this machine's running apps to pick from.

    Real for generic HTTP/static request inspection, usage counts, and
    request replay. MCP tool browsing (schemas, `tools/list`, `tools/call`)
    is parked — see this module's docstring.
    """
    if target is None:
        target = _pick_target()
        if target is None:
            return

    session = resolve_target(target)
    info(f"inspecting {session.base_url} — type 'help' for commands, 'quit' to leave")

    while True:
        try:
            line = input(f"{target}> ").strip()
        except (EOFError, KeyboardInterrupt):
            stdout.print()
            break
        if not line:
            continue

        parts = line.split(maxsplit=2)
        cmd = parts[0].lower()

        if cmd in ("quit", "exit"):
            break
        elif cmd == "help":
            stdout.print(_HELP)
        elif cmd == "info":
            _print_info(session)
        elif cmd == "usage":
            _print_usage(session)
        elif cmd == "header":
            if len(parts) < 3:
                error("usage: header <name> <value>")
            else:
                name, value = parts[1], parts[2]
                session.extra_headers[name] = value
                info(f"header set: {name}: {value}")
        elif cmd == "replay":
            if session.last_request is None:
                error("nothing to replay yet")
            else:
                method, path, body = session.last_request
                _print_response(execute_request(session, method, path, body))
        elif cmd in _METHODS:
            if len(parts) < 2:
                error(f"usage: {cmd} <path> [json body]")
                continue
            path = parts[1]
            body = parts[2] if len(parts) > 2 else None
            try:
                _print_response(execute_request(session, cmd.upper(), path, body))
            except httpx.TransportError as exc:
                error(f"request failed: {exc}")
        else:
            error(f"unknown command: {cmd!r} — type 'help'")
