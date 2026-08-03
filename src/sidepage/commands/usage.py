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

from sidepage.output import not_implemented


def usage(
    app_name: Annotated[str, typer.Argument(help="App to report connection-level usage for.")],
) -> None:
    """Report connection-level metrics for an app."""
    not_implemented("sidepage usage", implemented_by="sidepage.core.usage_reporter.get_usage")
