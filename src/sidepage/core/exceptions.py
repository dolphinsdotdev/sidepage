"""Exception hierarchy shared across `sidepage.core`.

Placeholder only — no core module raises these yet. Defined up front so
command modules can already write correct `except SidepageError` handling
without forward-referencing modules that don't exist.
"""

from __future__ import annotations


class SidepageError(Exception):
    """Base class for all errors raised by the sidepage SDK."""


class TargetDetectionError(SidepageError):
    """`sidepage serve --type auto` could not determine the target kind
    (code / static / notebook)."""


class DirectoryError(SidepageError):
    """The directory service (identity/discovery, §3 and §10) was
    unreachable, rejected a request, or returned an inconsistent state.
    """


class NameCollisionError(DirectoryError):
    """Raised internally when assigning the `<app-name>-<4-char-id>` suffix
    at `serve` time hits an already-taken fully-qualified name (practically
    unlikely, per §3's 4-char dedupe suffix). No longer surfaced through a
    standalone preview command — see `sidepage.core.directory_client.check_name`.
    """


class TunnelError(SidepageError):
    """Tunnel setup, teardown, or credential handling failed — any of the
    three modes in §6 (brokered-default, BYO-domain, anonymous).
    """


class CloudflaredResolutionError(TunnelError):
    """None of the four `cloudflared` binary resolution steps (§6) produced
    a usable, version-verified binary.
    """


class AuthConfigError(SidepageError):
    """An `--auth` tier (§4) was misconfigured, e.g. `oauth` requested
    before that tier ships (parked pending §15's MCP auth model).
    """


class ScopeError(SidepageError):
    """An invalid or not-yet-supported `--scope` transition (§5) was
    requested, e.g. demoting a `web`-scoped app without an explicit path
    for that in the spec.
    """


class StaticServeError(SidepageError):
    """A static target (§11) failed to validate — e.g. no `index.html` at
    the directory root. A hard error by design, not a directory listing.
    """
