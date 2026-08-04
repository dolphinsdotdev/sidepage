"""`sidepage usage` — spec v3 §7, metering (content-blind by design).

Reports HTTP request/response counts and WebSocket connection/message
counts, observed by the local reverse proxy — not self-reported by the app,
not inspected by Sidepage's cloud backend. **Resolved in v3:** this is the
permanent billing boundary, forever (v1 left open what happens if
self-reported richer usage is untrustworthy; v3 sidesteps that by never
depending on self-reporting). Same counters also surface live in `sidepage
inspect`; see `sidepage.core.usage_reporter`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from sidepage.core.exceptions import DirectoryError
from sidepage.core.usage_reporter import get_usage
from sidepage.output import error, stdout


def usage(
    app_name: Annotated[str, typer.Argument(help="App to report connection-level usage for.")],
) -> None:
    """Report connection-level metrics for an app."""
    try:
        report = get_usage(app_name)
    except DirectoryError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    stdout.print(f"http requests:  {report.http_request_count}")
    stdout.print(f"http responses: {report.http_response_count}")
    stdout.print(f"ws connections: {report.ws_connection_count}")
    stdout.print(f"ws messages:    {report.ws_message_count}")
    stdout.print(f"uptime:         {report.uptime_seconds}s")
