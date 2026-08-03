"""Interactive MCP inspection console — backs `sidepage inspect
[<app-name-or-url>]` (spec v3 §10), the "Postman-for-MCP" feature. Rides on
the directory that §3/§10 already provide, which is what makes it cheap:
with no argument, it should list running/discoverable servers to pick from
via `sidepage.core.directory_client.list_entries`.

**Resolved in v3:** no auth bypass for the local operator. Same credential
required as any caller — this was an open question in v1 and v3 confirms
the stricter answer, so the tool that verifies auth-tier compliance doesn't
itself skip the gate. It's still convenient: when inspecting one's own app,
the credential is sourced automatically from the token runtime file
(`sidepage.core.token_runtime.read_runtime_file`) rather than typed in.

Also surfaces the request/connection counts from `sidepage.core.usage_reporter`
for live observability — the same counters `sidepage usage` reports as a
standing snapshot.
"""

from __future__ import annotations


def open_console(target: str | None = None) -> None:
    """Open an interactive console against a running MCP server (local or
    Sidepage-hosted) identified by `target` (an app name or URL). With
    `target=None`, list discoverable servers first and let the user pick.

    Auto-sources credentials from the local token runtime file when
    inspecting an app owned by the current session; otherwise requires the
    same credential any other caller would need (no bypass — see this
    module's docstring).

    Should support: browsing tools, inspecting schemas, manually invoking
    calls, replaying requests, and displaying live usage counts.

    Not implemented.
    """
    raise NotImplementedError
