"""Hugging Face Spaces as a `sidepage pull` source — metadata, manifest,
and file download.

**Everything needed to plan and gate a pull comes from one metadata API
call**, before a single byte of repository content is fetched:
`GET https://huggingface.co/api/spaces/<owner>/<name>?blobs=true` returns
the SDK and its version, the declared `app_file`, the current commit sha,
the requested hardware tier, the upstream runtime stage, private/gated
flags, and the complete file list with per-file sizes and LFS digests.
That ordering is the whole reason `pull` can refuse a Docker-SDK or
GPU-only Space *without* downloading it, and can tell the user how many
bytes a pull will cost before starting it.

**Files are fetched over plain HTTPS, not `git clone`** — a deliberate
choice made against the alternative, not for lack of one:

  - It needs no `git` and, more importantly, no `git-lfs`. A `git clone`
    of an LFS-backed Space on a machine without `git-lfs` installed
    **succeeds** and silently leaves every model weight as a ~130-byte
    text pointer file. The app then fails at runtime with a baffling
    error (a Keras model "file" that's ASCII), which is precisely the
    silent-degradation failure mode this codebase refuses everywhere
    else. Reproduced on a machine with no `git-lfs`: the clone reported
    success and `stock_price_model.h5` came out 131 bytes of
    `version https://git-lfs.github.com/spec/v1`.
  - `resolve/<sha>/<path>` serves the real bytes for LFS and non-LFS
    files alike, and the metadata API hands us the LFS `sha256` up front,
    so every large file is verified against a digest the server told us
    *before* the download started.
  - Adding `git`/`git-lfs` as runtime dependencies to support one source
    type is a poor trade when the source's own API makes them
    unnecessary.

The cost, recorded rather than glossed: no local git history, and this
path is HF-specific. A future GitHub source will need its own transport
(almost certainly git), which is why source-specific knowledge lives in
this module rather than in `sidepage.core.pull`'s generic plumbing.

**No version tracking, by decision.** `pull` always fetches the current
state of the default branch. The resolved sha is recorded because it's
free and it's what makes "is this still the code you approved?" a
question we can answer at `serve` time — but there's no `--ref`, no
pinning, and no way to ask for an older commit. A re-pull is simply "get
what's there now."

**Nothing here executes anything.** This module downloads files and
parses metadata; it never runs a target's code, and the only file it
*reads* is the entrypoint, scanned as text for environment-variable names
(`sidepage.core.pull`). Everything this module returns originates with a
stranger and is treated as untrusted data — see
`sidepage.core.pull.safe_relative_path` for the containment rules applied
to any path that comes out of a manifest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from sidepage.core.exceptions import SourceError, UnrunnableSourceError

HF_API_BASE = "https://huggingface.co/api/spaces"
HF_RESOLVE_BASE = "https://huggingface.co/spaces"

# Hardware tiers `serve` can actually honor locally. An **allowlist, not a
# denylist**: HF adds accelerator tiers regularly (`zero-a10g`, `t4-small`,
# `l4x1`, `a100-large`, ...), and a denylist would silently let next
# year's tier through and try to run a GPU-only Space on a laptop. Anything
# not matching this prefix is refused by name, so a new tier produces a
# clear "sidepage can't run <tier>" rather than a mysterious runtime
# failure. Verified against live Spaces: `cpu-basic`, `cpu-upgrade` and
# `cpu-xl` are real CPU tiers; `zero-a10g` is what a ZeroGPU Space
# (black-forest-labs/FLUX.1-dev) reports.
RUNNABLE_HARDWARE_PREFIX = "cpu"

# SDKs with a real launcher behind them. `docker` is excluded on purpose
# rather than merely unimplemented: honoring a Dockerfile means running a
# container runtime sidepage neither requires nor manages.
SUPPORTED_SDKS = frozenset({"gradio", "streamlit", "static"})


@dataclass(frozen=True)
class HfFile:
    """One file in a Space, as the metadata API describes it — path, byte
    size, and (for LFS-tracked files) the sha256 the server expects the
    content to hash to."""

    path: str
    size: int
    lfs_sha256: str | None = None


@dataclass(frozen=True)
class HfSpace:
    """A Hugging Face Space's manifest and current state, as read from the
    metadata API. This is the "HF config specific class" a pulled app's
    provenance carries: fields here are HF's vocabulary (`sdk`,
    `sdk_version`, `app_file`, hardware tiers), deliberately not flattened
    into sidepage's own, so a future source type doesn't have to pretend
    its metadata looks like a Space's.

    Every string field originates in a README written by whoever owns the
    Space. Treat it as data — `sidepage.core.pull` validates `app_file`
    for containment before it ever becomes a path.
    """

    owner: str
    name: str
    sha: str
    sdk: str | None
    sdk_version: str | None
    app_file: str | None
    python_version: str | None
    hardware: str | None
    stage: str | None
    private: bool
    gated: bool
    title: str | None
    files: tuple[HfFile, ...]

    @property
    def repo_id(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"huggingface.co/spaces/{self.repo_id}"

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def file(self, path: str) -> HfFile | None:
        return next((f for f in self.files if f.path == path), None)


@dataclass(frozen=True)
class HfSpaceConfig:
    """The part of a Space's manifest worth *persisting* with a registered
    app — HF's own vocabulary, kept as its own type rather than flattened
    into sidepage's generic fields.

    Separate from `HfSpace` above, which is the full live metadata
    including the file list: that's a snapshot of a moment, too big and
    too stale-prone to keep in `registry.json`. This is the durable half —
    what the Space said it was, so `sidepage app show` can report it and a
    `serve` trust prompt can show what's about to run without another
    network call.
    """

    repo_id: str
    sdk: str | None
    sdk_version: str | None
    app_file: str | None
    python_version: str | None
    hardware: str | None


def to_config(space: HfSpace) -> HfSpaceConfig:
    return HfSpaceConfig(
        repo_id=space.repo_id,
        sdk=space.sdk,
        sdk_version=space.sdk_version,
        app_file=space.app_file,
        python_version=space.python_version,
        hardware=space.hardware,
    )


def parse_space_ref(source: str) -> tuple[str, str] | None:
    """Parse a Hugging Face Space reference into `(owner, name)`, or
    `None` if `source` isn't one.

    Accepted, all equivalent:
      - `huggingface.co/spaces/<owner>/<name>` (with or without scheme)
      - `hf:<owner>/<name>`

    Returning `None` rather than raising lets
    `sidepage.core.pull.resolve_source` try other source types in turn and
    produce one good "unsupported source" message at the end, instead of
    each parser raising its own.

    A bare `<owner>/<name>` is deliberately **not** accepted here. It's
    ambiguous with GitHub's identical shorthand, and silently resolving it
    to whichever host sidepage happens to support today would mean a typo
    or a stale habit could download a completely different stranger's code
    than the user meant. `hf:` costs three characters and removes the
    guess.
    """
    text = source.strip()
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = text.rstrip("/")

    if text.startswith("hf:"):
        rest = text[len("hf:") :]
    elif text.startswith("huggingface.co/spaces/"):
        rest = text[len("huggingface.co/spaces/") :]
    else:
        return None

    parts = [p for p in rest.split("/") if p]
    if len(parts) != 2:
        return None
    owner, name = parts
    if not owner or not name:
        return None
    return owner, name


def _card(data: dict) -> dict:
    card = data.get("cardData")
    return card if isinstance(card, dict) else {}


def _files_from(data: dict) -> tuple[HfFile, ...]:
    files: list[HfFile] = []
    for sibling in data.get("siblings") or ():
        if not isinstance(sibling, dict):
            continue
        path = sibling.get("rfilename")
        if not isinstance(path, str) or not path:
            continue
        lfs = sibling.get("lfs") if isinstance(sibling.get("lfs"), dict) else None
        files.append(
            HfFile(
                path=path,
                size=int(sibling.get("size") or 0),
                lfs_sha256=(lfs or {}).get("sha256"),
            )
        )
    return tuple(files)


def fetch_space(owner: str, name: str, *, timeout: float = 30.0) -> HfSpace:
    """Read a Space's metadata and manifest. One HTTP call, no content
    downloaded.

    Raises `sidepage.core.exceptions.SourceError` if the Space doesn't
    exist or the API can't be reached.
    """
    url = f"{HF_API_BASE}/{owner}/{name}"
    try:
        response = httpx.get(url, params={"blobs": "true"}, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise SourceError(f"couldn't reach the Hugging Face API for {owner}/{name}: {exc}") from exc

    # 401/403 as well as 404: the Hub deliberately answers an unauthenticated
    # request for a nonexistent repo and for a private one identically, so it
    # doesn't leak which private Spaces exist. Verified live — a made-up name
    # returns `401 {"error":"Invalid username or password."}`, which would be a
    # baffling thing to show someone who simply mistyped an owner.
    if response.status_code in (401, 403, 404):
        raise SourceError(
            f"no Hugging Face Space at {owner}/{name} that sidepage can read — check the owner "
            "and name for typos. A private or gated Space answers the same way, and sidepage "
            "can't authenticate to the Hub yet."
        )
    if response.status_code >= 400:
        raise SourceError(
            f"Hugging Face API returned {response.status_code} for {owner}/{name}: "
            f"{response.text[:200]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise SourceError(f"Hugging Face API returned non-JSON for {owner}/{name}") from exc

    card = _card(data)
    runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
    hardware = (runtime.get("hardware") or {}).get("requested") if runtime else None
    sha = data.get("sha")
    if not isinstance(sha, str) or not sha:
        raise SourceError(f"Hugging Face API returned no commit sha for {owner}/{name}")

    return HfSpace(
        owner=owner,
        name=name,
        sha=sha,
        # `sdk` is top-level on the API response; `cardData` carries the
        # README's own copy. They agree in practice — prefer the top-level
        # one and fall back, rather than trusting only the frontmatter.
        sdk=data.get("sdk") or card.get("sdk"),
        sdk_version=card.get("sdk_version"),
        app_file=card.get("app_file"),
        python_version=card.get("python_version"),
        hardware=hardware,
        stage=runtime.get("stage") if runtime else None,
        private=bool(data.get("private")),
        # `gated` is `false` or a string mode ("auto"/"manual") — any
        # truthy value means credentials are required.
        gated=bool(data.get("gated")),
        title=card.get("title"),
        files=_files_from(data),
    )


def check_runnable(space: HfSpace) -> None:
    """Refuse a Space sidepage can't run, from metadata alone — called
    before any file is downloaded.

    Raises `sidepage.core.exceptions.UnrunnableSourceError` naming the
    specific blocker. Everything checked here is a hard "no", not a
    warning: see `runtime_warnings` for the conditions that are worth
    telling the user about but shouldn't stop a pull.
    """
    if space.private or space.gated:
        which = "private" if space.private else "gated"
        raise UnrunnableSourceError(
            f"{space.repo_id} is {which} — it needs Hugging Face credentials, and sidepage "
            "has no Hub authentication yet. Nothing was downloaded."
        )
    if space.sdk == "docker":
        raise UnrunnableSourceError(
            f"{space.repo_id} is a Docker Space (sdk: docker). Running it means building and "
            "running a container, which sidepage doesn't do — it wraps processes, not images. "
            "Nothing was downloaded."
        )
    if space.sdk not in SUPPORTED_SDKS:
        raise UnrunnableSourceError(
            f"{space.repo_id} declares sdk: {space.sdk or '(none)'}, which sidepage doesn't "
            f"support. Supported: {', '.join(sorted(SUPPORTED_SDKS))}. Nothing was downloaded."
        )
    if space.hardware is not None and not space.hardware.startswith(RUNNABLE_HARDWARE_PREFIX):
        raise UnrunnableSourceError(
            f"{space.repo_id} requests {space.hardware} hardware — a GPU tier sidepage can't "
            "provide on this machine. Spaces built for ZeroGPU also import the `spaces` package "
            "and decorate their entrypoints, which only works on Hugging Face's own "
            "infrastructure. Nothing was downloaded."
        )


def runtime_warnings(space: HfSpace) -> tuple[str, ...]:
    """Conditions worth surfacing but not worth refusing over — the
    Space's own upstream health, and a missing `app_file` declaration.

    Upstream failure specifically is *not* a blocker: a Space that's
    broken on Hugging Face's infrastructure (evicted for storage, out of
    quota, crashed) may be exactly the one someone wants to run locally.
    """
    warnings: list[str] = []
    if space.stage and space.stage not in ("RUNNING", "SLEEPING", "BUILDING", "RUNNING_BUILDING"):
        warnings.append(
            f"Hugging Face reports this Space as {space.stage} upstream — it may not be working "
            "there either. That doesn't stop it running locally."
        )
    if space.sdk in ("gradio", "streamlit") and not space.app_file:
        warnings.append(
            "the Space declares no app_file — falling back to sidepage's own entrypoint detection."
        )
    return tuple(warnings)


def resolve_url(space: HfSpace, path: str) -> str:
    """Download URL for one file, pinned to the sha this pull resolved —
    never a branch name, so every file in a pull comes from the same
    commit even if the Space is updated mid-download."""
    return f"{HF_RESOLVE_BASE}/{space.repo_id}/resolve/{space.sha}/{path}"


def download_space(
    space: HfSpace,
    dest: Path,
    *,
    timeout: float = 120.0,
    on_file=None,
) -> None:
    """Download every file in `space` into `dest`, at the resolved sha.

    `on_file`, if given, is called with `(HfFile, index, total)` before
    each download — the hook `sidepage.commands.pull` uses for progress.

    Streams to disk rather than buffering: a Space can carry multi-GB
    weights, and holding one in memory to write it out again would be a
    pointless way to fail. LFS files are verified against the sha256 the
    metadata API reported *before* the download began, so a truncated or
    substituted file is caught rather than silently kept.

    Raises `sidepage.core.exceptions.SourceError` on any transport error
    or digest mismatch. Paths are validated by the caller
    (`sidepage.core.pull`) before this runs — a repo-supplied filename is
    never joined onto `dest` unchecked.
    """
    dest.mkdir(parents=True, exist_ok=True)
    total = len(space.files)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for index, entry in enumerate(space.files, start=1):
            if on_file is not None:
                on_file(entry, index, total)
            target = dest / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            try:
                with client.stream("GET", resolve_url(space, entry.path)) as response:
                    response.raise_for_status()
                    with target.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            digest.update(chunk)
                            handle.write(chunk)
            except httpx.HTTPError as exc:
                raise SourceError(
                    f"failed downloading {entry.path} from {space.url}: {exc}"
                ) from exc

            if entry.lfs_sha256 and digest.hexdigest() != entry.lfs_sha256:
                target.unlink(missing_ok=True)
                raise SourceError(
                    f"{entry.path} failed its checksum — Hugging Face declared sha256 "
                    f"{entry.lfs_sha256[:12]}… but the downloaded bytes hash to "
                    f"{digest.hexdigest()[:12]}…. The file was discarded."
                )
