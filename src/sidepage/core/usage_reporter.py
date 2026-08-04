"""Metering — backs `sidepage usage <app-name>` (spec v3 §7), and the same
counters surfaced live by `sidepage inspect` (`sidepage.core.inspector`).

Content-blind by design, but on stronger footing than v1: counts are
observed by the **local reverse proxy** (`sidepage.core.reverse_proxy`),
not self-reported by the app and not inspected by Sidepage's cloud
backend — the proxy genuinely sees connection-level activity without
reading payload content.

**Resolved in v3:** the billing/trust boundary is connection/request-count
only, **forever** — no payload inspection, no app self-reporting required.
This replaces v1's open question about what happens if a self-reporting app
under-reports; v3 sidesteps it by never depending on self-reporting at all.

Two counters, not one — HTTP and WebSocket targets don't share a shape:
  - **HTTP targets** (code, static): request/response counts.
  - **WebSocket targets** (Streamlit, notebook Lab): connection count +
    message count. A WS session has no discrete request/response pairing;
    forcing it into the HTTP counter would misrepresent usage.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from sidepage.core import registry
from sidepage.core.exceptions import DirectoryError
from sidepage.core.reverse_proxy import counts_path


@dataclass(frozen=True)
class UsageReport:
    app_name: str
    http_request_count: int
    http_response_count: int
    ws_connection_count: int
    ws_message_count: int
    uptime_seconds: int


def get_usage(app_name: str) -> UsageReport:
    """Backs `sidepage usage <app-name>` and the counts panel in `sidepage
    inspect` — same counters, two read paths, both sourced from the counts
    file `sidepage.core.reverse_proxy` writes on every request/message.

    Raises `sidepage.core.exceptions.DirectoryError` if `app_name` isn't a
    currently-running app.
    """
    app = registry.get(app_name)
    if app is None:
        raise DirectoryError(f"no running app named {app_name!r}")

    counts_file = counts_path(app_name)
    counts: dict[str, int] = {}
    if counts_file.exists():
        try:
            counts = json.loads(counts_file.read_text())
        except (json.JSONDecodeError, OSError):
            counts = {}

    return UsageReport(
        app_name=app_name,
        http_request_count=counts.get("http_requests", 0),
        http_response_count=counts.get("http_responses", 0),
        ws_connection_count=counts.get("ws_connections", 0),
        ws_message_count=counts.get("ws_messages", 0),
        uptime_seconds=int(time.time() - app.started_at),
    )
