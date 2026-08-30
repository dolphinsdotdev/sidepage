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


class CloudflaredInstallError(TunnelError):
    """`sidepage setup` (`sidepage.core.cloudflared_installer`) could not
    detect the platform, find a matching release asset, download it,
    unpack it, or get a working `--version` out of the result.
    """


class TunnelProvisioningError(TunnelError):
    """`sidepage account domain set` created a tunnel on Cloudflare but a
    later step (almost always persisting its run-token to the vault)
    failed. Carries `tunnel_id` and `internal_secret_name` so the CLI can
    tell the user exactly what's now orphaned — the run-token is returned
    by Cloudflare exactly once at creation time, so if it wasn't saved
    there's no way to fetch it again; the tunnel has to be deleted (or the
    token re-derived by recreating it) by hand.
    """

    def __init__(self, message: str, *, tunnel_id: str, internal_secret_name: str) -> None:
        super().__init__(message)
        self.tunnel_id = tunnel_id
        self.internal_secret_name = internal_secret_name


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


class VaultError(SidepageError):
    """The secrets vault (v4 §9) — OS keychain or its encrypted-file
    fallback — was unreachable or failed to read/write.
    """


class SecretNotFoundError(VaultError):
    """`serve --env <SECRET_NAME>` or a BYO-domain `--*-token-name` flag
    referenced a name that isn't in the vault. Fails loud by design — no
    silent skip, no blanket passthrough.
    """


class InspectorTargetError(SidepageError):
    """`sidepage inspect <target>` was given something that's neither a
    locally-registered app name nor an `http(s)://` URL.
    """


class AppRegistryError(SidepageError):
    """The local app registry (`sidepage app register|list|show|unregister`,
    `sidepage.core.app_registry`) — a saved `serve` invocation under a
    short name — failed. Distinct from `sidepage.core.registry`, which
    tracks apps *currently running*, not saved configs.
    """


class AppRegistrationError(AppRegistryError):
    """`sidepage app register` was given an invocation string that failed
    to parse against `serve`'s own flags, pointed at a target that doesn't
    exist, or contained a literal `--token <value>` — rejected outright
    per the registry spec's hard rule that auth tokens (process-scoped,
    regenerated per `serve` call) never persist outside the runtime file.
    """


class AppNotRegisteredError(AppRegistryError):
    """`sidepage app show|unregister <app-name>` referenced a name that
    isn't in the registry. (`sidepage serve <app-name>` deliberately does
    *not* raise this for an unknown name — it falls back to treating the
    argument as a literal target path instead, see
    `sidepage.commands.serve`.)
    """


class PwaConfigError(SidepageError):
    """`sidepage serve --pwa` (spec: `--pwa`/`--pwa-*` flags) was given an
    invalid combination or value — a non-hex `--pwa-theme`/`--pwa-bg`, a
    `--pwa-icon` that isn't a readable square PNG >=512px, or a
    `--pwa-manifest` file that doesn't parse as JSON. Always names the
    actual problem found (the bad value, or the actual dimensions/type
    read from the file), never a generic "invalid" message.
    """


class PeerNotFoundError(DirectoryError):
    """`serve --peer <role>=<app-name>` (v5, `--peer`) or a live `GET
    /.sidepage/peers.json` request referenced an app name that isn't
    currently running. Peers are resolved against
    `sidepage.core.registry`'s *live* state, not a saved config — there's
    no persisted peer target to fall back to, so an unresolvable name
    fails loud rather than injecting a stale or empty URL.
    """
