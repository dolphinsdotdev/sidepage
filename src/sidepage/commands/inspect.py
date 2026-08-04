"""`sidepage inspect` — spec v3 §10, inspection ("Postman-for-MCP",
extended).

**Real for generic HTTP/static targets** — issue ad-hoc requests against
anything `sidepage serve` wraps, browse live usage counts, replay the last
request. **Not real yet:** MCP tool browsing (schemas, `tools/list`,
`tools/call`) — the spec's actual "Postman-for-MCP" framing, parked
pending a real MCP client and MCP server test fixture. See
`sidepage.core.inspector` and `docs/CHECKLIST.md`.

Directory-aware: with no argument, lists running/discoverable servers to
pick from. No auth bypass for the local operator — same credential
required as any caller, just auto-sourced from the token runtime file when
inspecting one's own app.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core.exceptions import InspectorTargetError
from sidepage.core.inspector import open_console
from sidepage.output import error


def inspect(
    target: Annotated[
        str | None,
        typer.Argument(help="App name or URL to inspect. Omit to pick from discoverable servers."),
    ] = None,
) -> None:
    """Open an interactive console against a running app."""
    try:
        open_console(target)
    except InspectorTargetError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
