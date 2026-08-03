"""Auth tier definitions — backs `sidepage serve --auth` (spec v3 §4).

Declared per-app at `serve` time. Enforcement moved in v3: it's the
**local** reverse proxy (`sidepage.core.reverse_proxy`, §9) that gates
access, not the app and not Sidepage's cloud backend — the backend still
never inspects payloads (§6/§7's no-MITM stance is about the *cloud* side),
but the proxy the user's own machine runs is explicitly not held to that
line, since it's local and user-controlled.

  - **open**    — no auth. Default.
  - **network** — IP allowlist / mTLS at the tunnel edge.
  - **token**   — see `sidepage.core.token_runtime` (§8) for issuance/
                  storage, and `sidepage.core.reverse_proxy` (§9) for how
                  the gate page / header check enforces it. Sidepage itself
                  never validates payload content — the proxy only ever
                  checks the token, nothing else in the request.
  - **oauth**   — for MCP clients acting as a user with scoped access.
                  **Deferred, not implemented**: v3 explicitly drops it from
                  near-term scope pending a dedicated MCP auth model
                  discussion (§15). Kept in this enum (and as a valid
                  `--auth` choice) rather than removed, the same way
                  `--guardrail` stays present but inert — there's no
                  replacement design yet, just a decision not to build it
                  now.

See `sidepage.core.token_runtime` for the open question about agent-to-agent
signed requests as a possible future auth mechanism, orthogonal to this
tier list.
"""

from __future__ import annotations

from enum import StrEnum


class AuthTier(StrEnum):
    OPEN = "open"
    NETWORK = "network"
    TOKEN = "token"
    OAUTH = "oauth"
