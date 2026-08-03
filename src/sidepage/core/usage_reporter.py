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

from dataclasses import dataclass


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
    inspect` — same counters, two read paths, both sourced from
    `sidepage.core.reverse_proxy`.

    Not implemented.
    """
    raise NotImplementedError
