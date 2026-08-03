"""`sidepage inspect` — spec v3 §10, inspection ("Postman-for-MCP",
extended).

Opens an interactive console against a running MCP server (local or
Sidepage-hosted): browse tools, inspect schemas, manually invoke calls,
replay requests, and view live usage counts. Directory-aware: with no
argument, lists running/discoverable servers to pick from. **Resolved in
v3:** no auth bypass for the local operator — same credential required as
any caller, just auto-sourced from the token runtime file when inspecting
one's own app. See `sidepage.core.inspector`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.output import not_implemented


def inspect(
    target: Annotated[
        str | None,
        typer.Argument(help="App name or URL to inspect. Omit to pick from discoverable servers."),
    ] = None,
) -> None:
    """Open an interactive console against a running MCP server."""
    not_implemented("sidepage inspect", implemented_by="sidepage.core.inspector.open_console")
