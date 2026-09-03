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

**`--pwa`/`--pwa-*` are stored; `--timeout`/`--idle-timeout`/`--peer`/
`--qr` are not.** The dividing line is whether a flag describes what the
served app *is* or merely how this one run of it behaves. PWA settings
are the former — an installed home-screen app's name, icon, and theme are
part of its identity, so a saved config that dropped them wouldn't
reproduce the app it was saved from. The others are per-invocation
lifetime and display concerns with nothing to reproduce. `--token` is
excluded for a third, stronger reason: it's a secret (see above).

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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sidepage.config.settings import app_registry_file, ensure_dirs
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.exceptions import AppNotRegisteredError, AppRegistrationError
from sidepage.core.hf import HfSpaceConfig
from sidepage.core.pwa import PwaDisplay, PwaOptions
from sidepage.core.target import TargetKind


@dataclass(frozen=True)
class AppSource:
    """Where a registered app's code came from, when sidepage fetched it
    rather than being pointed at something already on disk.

    `None` on an ordinary `app register`/`--autoregister` entry — those
    reference a path the user already had, and sidepage has no claim over
    it. Present only for `sidepage pull`, which is also exactly the
    condition under which `sidepage app delete` may remove files: `managed`
    records that the tree under `apps_dir()` is sidepage's to delete, so a
    locally-registered app can never have its source directory removed.

    `trusted_commit` is the commit a human explicitly approved running.
    It's compared against `commit` at every `serve` — a `pull` that brings
    down different code leaves the two unequal and re-arms the prompt, so
    approval attaches to the code that was reviewed rather than to the
    name it was filed under.

    `env_requested` is names only, scanned out of the entrypoint and never
    resolved to values. Being listed here grants nothing.
    """

    kind: str  # "huggingface-space"
    url: str
    commit: str
    managed: bool
    env_requested: tuple[str, ...] = ()
    trusted_commit: str | None = None
    # Source-type-specific manifest. One field per supported source kind
    # rather than a generic bag: a Space's `sdk`/`sdk_version`/`app_file`
    # don't generalize, and pretending they do would make the next source
    # type either distort its own vocabulary or quietly reuse fields that
    # mean something different.
    hf: HfSpaceConfig | None = None


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
    # `--no-suffix` — stored for the same reason `domain` is: it decides
    # the hostname the app answers on (`<app-name>.<domain>` rather than
    # `<app-name>-<id>.<domain>`), so a saved config that dropped it
    # would replay at a different URL than the one it was saved from.
    no_suffix: bool = False
    # `--pwa`/`--pwa-*` as one unit, or None when PWA mode is off. Stored
    # (unlike `--timeout`/`--idle-timeout`/`--peer`/`--qr`, which stay
    # per-invocation) because PWA settings change what the served app
    # *is* to anyone who installs it — a saved config that silently
    # dropped them wouldn't reproduce the app it was saved from. Merged
    # as a unit too: see `sidepage.commands.app_registry.merge_with_registered`.
    pwa: PwaOptions | None = None
    # Provenance for `sidepage pull`ed apps; None for anything registered
    # against a path the user already had. See `AppSource`.
    source: AppSource | None = None


def _source_to_json(source: AppSource | None) -> dict | None:
    if source is None:
        return None
    return {
        "kind": source.kind,
        "url": source.url,
        "commit": source.commit,
        "managed": source.managed,
        "env_requested": list(source.env_requested),
        "trusted_commit": source.trusted_commit,
        "hf": None
        if source.hf is None
        else {
            "repo_id": source.hf.repo_id,
            "sdk": source.hf.sdk,
            "sdk_version": source.hf.sdk_version,
            "app_file": source.hf.app_file,
            "python_version": source.hf.python_version,
            "hardware": source.hf.hardware,
        },
    }


def _source_from_json(data: dict | None) -> AppSource | None:
    if not data:
        return None
    hf_data = data.get("hf")
    return AppSource(
        kind=data["kind"],
        url=data["url"],
        commit=data["commit"],
        managed=bool(data.get("managed")),
        env_requested=tuple(data.get("env_requested") or ()),
        trusted_commit=data.get("trusted_commit"),
        hf=None
        if not hf_data
        else HfSpaceConfig(
            repo_id=hf_data["repo_id"],
            sdk=hf_data.get("sdk"),
            sdk_version=hf_data.get("sdk_version"),
            app_file=hf_data.get("app_file"),
            python_version=hf_data.get("python_version"),
            hardware=hf_data.get("hardware"),
        ),
    )


def _pwa_to_json(pwa: PwaOptions | None) -> dict | None:
    if pwa is None:
        return None
    return {
        "name": pwa.name,
        "short_name": pwa.short_name,
        "theme": pwa.theme,
        "bg": pwa.bg,
        "icon": str(pwa.icon) if pwa.icon is not None else None,
        "display": pwa.display.value,
        "manifest": str(pwa.manifest) if pwa.manifest is not None else None,
        "force": pwa.force,
        "no_sw": pwa.no_sw,
    }


def _pwa_from_json(data: dict | None) -> PwaOptions | None:
    if not data:
        return None
    return PwaOptions(
        name=data.get("name"),
        short_name=data.get("short_name"),
        theme=data["theme"],
        bg=data["bg"],
        icon=Path(data["icon"]) if data.get("icon") else None,
        display=PwaDisplay(data["display"]),
        manifest=Path(data["manifest"]) if data.get("manifest") else None,
        force=data.get("force", False),
        no_sw=data.get("no_sw", False),
    )


def _to_json(r: AppRegistration) -> dict:
    return {
        "target": str(r.target),
        "type": r.target_kind.value,
        "name": r.name,
        "domain": r.domain,
        "auth": r.auth.value,
        "scope": r.scope.value,
        "anon": r.anon,
        "no_suffix": r.no_suffix,
        "env": list(r.env_secrets),
        "guardrail": str(r.guardrail) if r.guardrail is not None else None,
        "pwa": _pwa_to_json(r.pwa),
        "source": _source_to_json(r.source),
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
        # `.get` for the same reason as `pwa` below: entries written
        # before `--no-suffix` existed are valid registrations with it
        # off, not corrupt ones.
        no_suffix=data.get("no_suffix", False),
        env_secrets=tuple(data.get("env") or ()),
        guardrail=Path(data["guardrail"]) if data.get("guardrail") else None,
        # `.get`, not `["pwa"]` — entries written before PWA became a
        # stored field have no such key, and an old entry is a valid
        # registration with PWA off, not a corrupt one.
        pwa=_pwa_from_json(data.get("pwa")),
        source=_source_from_json(data.get("source")),
        registered_at=data["registered_at"],
    )


def same_config(a: AppRegistration, b: AppRegistration, *, default_name: str) -> bool:
    """True if two registrations describe the same `serve` config.

    Ignores `registered_at` (when a config was saved says nothing about
    what it is) and treats an unset `name` as `default_name` — without
    that second normalization, `serve <app-name> --autoregister` against
    an app registered with no explicit `--name` would compare a stored
    `name=None` against the live, resolved `name="<app-name>"` and call
    two identical configs different.

    Backs `--autoregister`'s "already registered with this exact config"
    check (`sidepage.core.process.serve`), which is what keeps re-serving
    the same app from either erroring or silently overwriting.
    """

    def _norm(r: AppRegistration) -> AppRegistration:
        # `source` is deliberately excluded too: provenance records where
        # code came from, not how it runs. A `serve <pulled-app>
        # --autoregister` builds its candidate from flags alone and has no
        # provenance to offer, so comparing it would report every pulled
        # app as a conflict with itself.
        return replace(r, registered_at="", name=r.name or default_name, source=None)

    return _norm(a) == _norm(b)


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
    no_suffix: bool = False,
    pwa: PwaOptions | None = None,
    source: AppSource | None = None,
    replace_existing: bool = False,
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
    # Same absolute-path reasoning as `target` (see module docstring), for
    # the same reason: `--pwa-icon`/`--pwa-manifest` are usually typed as
    # paths relative to wherever the user happened to be standing, and
    # `sidepage serve <app-name>` can be run from anywhere later. Resolved
    # here rather than at each call site so neither caller can forget.
    if pwa is not None:
        pwa = replace(
            pwa,
            icon=pwa.icon.resolve() if pwa.icon is not None else None,
            manifest=pwa.manifest.resolve() if pwa.manifest is not None else None,
        )
    store = _load()
    if app_name in store and not replace_existing:
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
        no_suffix=no_suffix,
        env_secrets=env_secrets,
        guardrail=guardrail,
        pwa=pwa,
        source=source,
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


def set_trusted_commit(app_name: str, commit: str) -> None:
    """Record that a human approved running `app_name` at `commit`.

    Written only after an explicit confirmation at `serve` time (see
    `sidepage.commands.serve._require_source_trust`). Stored on the
    registration rather than in a separate trust store so it can't drift
    out of sync with the commit it refers to: `pull` rewrites the whole
    entry, so newly downloaded code arrives with `commit` changed and
    `trusted_commit` reset, and the prompt re-arms by construction.

    Raises `sidepage.core.exceptions.AppNotRegisteredError` if `app_name`
    isn't registered, and does nothing for an app with no provenance —
    there's no remote code to trust.
    """
    store = _load()
    if app_name not in store:
        raise AppNotRegisteredError(f"no app named {app_name!r} is registered")
    registration = _from_json(store[app_name])
    if registration.source is None:
        return
    updated = replace(registration, source=replace(registration.source, trusted_commit=commit))
    store[app_name] = _to_json(updated)
    _save(store)


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
