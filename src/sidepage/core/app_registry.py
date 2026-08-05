"""Local app registry — backs `sidepage app register|list|show|unregister`
and `sidepage serve <app-name>` (registry spec v2, `sidepage-registry-spec.md`).

Lets a user save a `serve` invocation under a short name and re-run it
without retyping flags. Local-only, no login or backend dependency.

**Distinct from `sidepage.core.registry`**, which this module's name is
easy to confuse with: that one tracks apps *currently running* on this
machine (ephemeral, pruned when the process dies, backs `ls`/`status`/
`stop`). This one is a *saved config* — whether anything is running right
now is completely unrelated to whether it's registered here.

**Structured, not raw-string**, per the spec: the stored fields are a
fully-resolved `serve` config (concrete `TargetKind`, not "auto"; concrete
`AuthTier`/`Scope`, not left implicit), not the literal invocation string
the user typed. This module deliberately doesn't know how to *parse* a
`serve`-flags string itself — that parsing happens in
`sidepage.commands.app_registry` by re-using `serve`'s own Click command
object (so a new `serve` flag is automatically registry-compatible with
zero changes here), which is also why this module has no `typer`/`click`
import: it only ever receives already-resolved, already-typed values,
keeping the CLI-parsing concern entirely in the `commands` layer where it
belongs (this module is core/SDK, same layering as everywhere else in
`sidepage.core`).

**Secrets are never stored here** — enforced at the `commands` layer (a
literal `--token <value>` in a registration invocation is rejected before
this module is ever called), not here, since rejecting it requires
inspecting the *unparsed* invocation string this module never sees.
`env_secrets` (vault secret *names*, via `--env`) are safe and stored
as-is — same reference-not-value rule the vault (`sidepage.core.secrets_vault`)
and BYO-domain config (`sidepage.core.account`) already follow.

`target` is stored as an **absolute, resolved path** — not whatever
relative string the user typed at registration time — so `sidepage serve
<app-name>` works regardless of the current working directory of whatever
shell it's run from later. The spec's own illustrative example shows a
bare relative `"target": "abc.py"`; resolving to absolute is this
implementation's own, deliberate choice on top of that, not a spec detail
left ambiguous by accident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sidepage.config.settings import app_registry_file, ensure_dirs
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.exceptions import AppNotRegisteredError, AppRegistrationError
from sidepage.core.target import TargetKind


@dataclass(frozen=True)
class AppRegistration:
    target: Path
    target_kind: TargetKind
    name: str | None
    domain: str | None
    auth: AuthTier
    scope: Scope
    anon: bool
    env_secrets: tuple[str, ...]
    guardrail: Path | None
    registered_at: str  # ISO 8601, UTC, "...Z" suffix


def _to_json(r: AppRegistration) -> dict:
    return {
        "target": str(r.target),
        "type": r.target_kind.value,
        "name": r.name,
        "domain": r.domain,
        "auth": r.auth.value,
        "scope": r.scope.value,
        "anon": r.anon,
        "env": list(r.env_secrets),
        "guardrail": str(r.guardrail) if r.guardrail is not None else None,
        "registered_at": r.registered_at,
    }


def _from_json(data: dict) -> AppRegistration:
    return AppRegistration(
        target=Path(data["target"]),
        target_kind=TargetKind(data["type"]),
        name=data.get("name"),
        domain=data.get("domain"),
        auth=AuthTier(data["auth"]),
        scope=Scope(data["scope"]),
        anon=data.get("anon", False),
        env_secrets=tuple(data.get("env") or ()),
        guardrail=Path(data["guardrail"]) if data.get("guardrail") else None,
        registered_at=data["registered_at"],
    )


def _load() -> dict[str, dict]:
    path = app_registry_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, dict]) -> None:
    ensure_dirs()
    app_registry_file().write_text(json.dumps(data, indent=2))


def register(
    app_name: str,
    *,
    target: Path,
    target_kind: TargetKind,
    name: str | None,
    domain: str | None,
    auth: AuthTier,
    scope: Scope,
    anon: bool,
    env_secrets: tuple[str, ...],
    guardrail: Path | None,
) -> AppRegistration:
    """Save a fully-resolved `serve` config under `app_name`.

    Every argument here is expected to already be validated/typed — the
    caller (`sidepage.commands.app_registry`) is responsible for parsing
    the raw invocation string via `serve`'s own Click command and
    rejecting a literal `--token` before this is ever called; this
    function doesn't re-derive or re-check any of that.

    Raises `sidepage.core.exceptions.AppRegistrationError` if `app_name`
    is already registered — matching `sidepage.core.process.serve`'s own
    "already registered" rejection for the *running*-apps registry, this
    doesn't silently overwrite; `sidepage app unregister` first, or pick a
    different name.
    """
    store = _load()
    if app_name in store:
        raise AppRegistrationError(
            f"an app named {app_name!r} is already registered — "
            f"`sidepage app unregister {app_name}` it first, or pick a different name."
        )
    registration = AppRegistration(
        target=target,
        target_kind=target_kind,
        name=name,
        domain=domain,
        auth=auth,
        scope=scope,
        anon=anon,
        env_secrets=env_secrets,
        guardrail=guardrail,
        registered_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    store[app_name] = _to_json(registration)
    _save(store)
    return registration


def get(app_name: str) -> AppRegistration | None:
    """Look up a registered app's saved config, or `None` if `app_name`
    isn't registered. Returns `None` rather than raising — this is the
    lookup `sidepage serve <app-name>` uses to decide whether its
    argument is a registered name (use the saved config) or a literal
    path (existing behavior, unchanged); an unknown name isn't an error
    at this layer, just "not found," so `serve` can fall back cleanly.
    """
    data = _load().get(app_name)
    return _from_json(data) if data is not None else None


def list_registered() -> list[str]:
    """Registered app names, sorted. Values are never returned in bulk —
    matching `sidepage.core.secrets_vault.list_secrets`'s shape, though
    for a different reason here: `sidepage app show <app-name>` is the
    real per-entry inspection command, not a listing convenience."""
    return sorted(_load())


def unregister(app_name: str) -> None:
    """Delete a registered app's saved config.

    Raises `sidepage.core.exceptions.AppNotRegisteredError` if `app_name`
    isn't registered — deliberately not a no-op like
    `sidepage.core.secrets_vault.remove_secret`'s "removing something
    already absent is fine" stance: a registry of short, user-chosen names
    is small and typo-prone enough that silently succeeding on a typo'd
    name would hide the mistake rather than surface it.
    """
    store = _load()
    if app_name not in store:
        raise AppNotRegisteredError(f"no app named {app_name!r} is registered")
    del store[app_name]
    _save(store)
