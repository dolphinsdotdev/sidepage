"""Tests for `sidepage pull`, the source-trust gate on `serve`, and
`sidepage app delete`.

**Offline by construction.** Every test here stubs `httpx` rather than
talking to Hugging Face: a test suite that silently depends on a third
party's uptime, rate limits, and a stranger's Space still existing isn't
testing sidepage. The stub payloads are copied from real API responses
(recorded from `Anvarbekkk/real-time-stock-predictor`,
`black-forest-labs/FLUX.1-dev`, and `enzostvs/deepsite`), so the shapes
being parsed are the shapes the Hub actually returns — including the
detail that a nonexistent Space answers `401`, not `404`.

The security-relevant behaviors are the point of this file: refusing the
un-runnable *before* downloading, refusing manifest paths that escape the
app directory, refusing to execute downloaded code without an explicit
per-commit approval, and refusing to delete a directory sidepage didn't
create.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from sidepage.cli import app
from sidepage.config.settings import app_source_dir, apps_dir
from sidepage.core import app_registry, hf
from sidepage.core import pull as core_pull
from sidepage.core.app_registry import AppSource
from sidepage.core.auth import AuthTier
from sidepage.core.directory_client import Scope
from sidepage.core.exceptions import (
    SourceError,
    SourceNotSupportedError,
    UnrunnableSourceError,
)
from sidepage.core.target import TargetKind

runner = CliRunner()


@pytest.fixture
def sidepage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SIDEPAGE_HOME", str(tmp_path))
    return tmp_path


APP_PY = "import gradio as gr\nimport os\nKEY = os.environ['FINNHUB_API_KEY']\n"
REQUIREMENTS = "gradio>=3.0\nyfinance==0.2.58\n# a comment\n\npandas==2.2.3\n"
WEIGHT = b"fake model weights"


def _space_payload(**overrides) -> dict:
    """A Space metadata response shaped like the real one."""
    payload = {
        "id": "someone/demo-space",
        "sdk": "gradio",
        "private": False,
        "gated": False,
        "author": "someone",
        "sha": "458cee6d4cdc468566e64e542ca4f8f950e4ff1a",
        "cardData": {
            "title": "Demo Space",
            "sdk": "gradio",
            "sdk_version": "5.29.0",
            "app_file": "app.py",
        },
        "runtime": {
            "stage": "RUNNING",
            "hardware": {"current": None, "requested": "cpu-basic"},
        },
        "siblings": [
            {"rfilename": "README.md", "blobId": "a" * 40, "size": 316},
            {"rfilename": "app.py", "blobId": "b" * 40, "size": len(APP_PY)},
            {"rfilename": "requirements.txt", "blobId": "c" * 40, "size": len(REQUIREMENTS)},
            {
                "rfilename": "model.h5",
                "blobId": "d" * 40,
                "size": len(WEIGHT),
                "lfs": {
                    "sha256": hashlib.sha256(WEIGHT).hexdigest(),
                    "size": len(WEIGHT),
                    "pointerSize": 131,
                },
            },
        ],
    }
    payload.update(overrides)
    return payload


FILE_BODIES = {
    "README.md": "---\nsdk: gradio\n---\n",
    "app.py": APP_PY,
    "requirements.txt": REQUIREMENTS,
    "model.h5": WEIGHT,
}


@pytest.fixture
def fake_hub(monkeypatch: pytest.MonkeyPatch):
    """Stub the two HTTP calls `sidepage.core.hf` makes: the metadata API
    (`httpx.get`) and the per-file download (`httpx.Client.stream`).

    Returns a mutable dict the test can adjust to change what the Hub
    "returns" — status code, payload, or file bodies.
    """
    state = {"status": 200, "payload": _space_payload(), "bodies": dict(FILE_BODIES)}

    def fake_get(url, **kwargs):
        return httpx.Response(
            status_code=state["status"],
            json=state["payload"] if state["status"] == 200 else {"error": "nope"},
            request=httpx.Request("GET", url),
        )

    class _FakeStream:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            yield self._body

    def fake_stream(self, method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        body = state["bodies"][name]
        return _FakeStream(body if isinstance(body, bytes) else body.encode())

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx.Client, "stream", fake_stream)
    return state


# --- source resolution: recognized, stubbed, and refused forms ---


@pytest.mark.parametrize(
    "source",
    [
        "huggingface.co/spaces/someone/demo-space",
        "https://huggingface.co/spaces/someone/demo-space",
        "https://huggingface.co/spaces/someone/demo-space/",
        "hf:someone/demo-space",
    ],
)
def test_space_refs_parse_to_owner_and_name(source: str) -> None:
    assert hf.parse_space_ref(source) == ("someone", "demo-space")


def test_bare_owner_slash_name_is_not_a_space_ref() -> None:
    """Ambiguous with GitHub's identical shorthand — resolving it would
    mean guessing which stranger's code to download."""
    assert hf.parse_space_ref("someone/demo-space") is None


def test_bare_owner_slash_name_is_refused_with_a_suggestion(sidepage_home: Path) -> None:
    with pytest.raises(SourceNotSupportedError, match="hf:someone/demo-space"):
        core_pull.resolve_source("someone/demo-space")


@pytest.mark.parametrize(
    "source, expected",
    [
        ("github.com/foo/bar", "GitHub"),
        ("gh:foo/bar", "GitHub"),
        ("mcp:some-server", "MCP"),
        ("./local/path", "local path"),
    ],
)
def test_unbuilt_sources_say_what_they_are(source: str, expected: str) -> None:
    with pytest.raises(SourceNotSupportedError, match=expected):
        core_pull.resolve_source(source)


def test_unrecognized_source_is_refused_not_guessed() -> None:
    with pytest.raises(SourceNotSupportedError, match="isn't a source sidepage recognizes"):
        core_pull.resolve_source("what even is this")


def test_missing_space_reports_a_typo_not_an_auth_error(sidepage_home: Path, fake_hub) -> None:
    """The Hub answers 401 for both nonexistent and private Spaces so it
    doesn't leak which private ones exist — surfacing its raw 'Invalid
    username or password' would send someone hunting for credentials they
    don't need."""
    fake_hub["status"] = 401
    with pytest.raises(SourceError, match="check the owner and name for typos"):
        core_pull.resolve_source("hf:someone/demo-space")


# --- refusing the un-runnable, from metadata alone ---


def test_docker_sdk_is_refused() -> None:
    space = _space_from(_space_payload(sdk="docker"))
    with pytest.raises(UnrunnableSourceError, match="Docker Space"):
        hf.check_runnable(space)


def test_gpu_hardware_is_refused_by_name() -> None:
    payload = _space_payload()
    payload["runtime"]["hardware"]["requested"] = "zero-a10g"
    with pytest.raises(UnrunnableSourceError, match="zero-a10g"):
        hf.check_runnable(_space_from(payload))


def test_unknown_future_hardware_tier_is_refused_not_allowed() -> None:
    """The hardware check is an allowlist: a tier nobody has heard of yet
    must warn, or next year's accelerator silently gets scheduled onto
    someone's laptop."""
    payload = _space_payload()
    payload["runtime"]["hardware"]["requested"] = "quantum-xxl-2029"
    with pytest.raises(UnrunnableSourceError, match="quantum-xxl-2029"):
        hf.check_runnable(_space_from(payload))


def test_hardware_refusal_is_overridable() -> None:
    """The tier is the owner's Hugging Face hosting choice, not a fact
    about this machine: a ZeroGPU Space's `@spaces.GPU` decorator is inert
    off Hugging Face, so the app really does run locally, just on CPU. The
    gate is a caution worth defaulting to, not a wall."""
    payload = _space_payload()
    payload["runtime"]["hardware"]["requested"] = "zero-a10g"
    hf.check_runnable(_space_from(payload), ignore_hardware=True)  # does not raise


def test_ignore_hardware_does_not_excuse_structural_blockers() -> None:
    """Docker and private/gated are refusals about what sidepage can do at
    all, not about how fast it would be — the override must not reach
    them."""
    with pytest.raises(UnrunnableSourceError, match="Docker Space"):
        hf.check_runnable(_space_from(_space_payload(sdk="docker")), ignore_hardware=True)
    with pytest.raises(UnrunnableSourceError, match="private"):
        hf.check_runnable(_space_from(_space_payload(private=True)), ignore_hardware=True)


def test_pull_ignore_hardware_pulls_and_says_so(sidepage_home: Path, fake_hub) -> None:
    fake_hub["payload"]["runtime"]["hardware"]["requested"] = "zero-a10g"

    refused = runner.invoke(app, ["pull", "hf:someone/demo-space"])
    assert refused.exit_code == 1
    assert "--ignore-hardware" in refused.output
    assert app_registry.get("demo-space") is None

    allowed = runner.invoke(app, ["pull", "hf:someone/demo-space", "--ignore-hardware"])
    assert allowed.exit_code == 0, allowed.output
    assert app_registry.get("demo-space") is not None
    # An overridden pull must never read as routine.
    assert "zero-a10g" in allowed.output


@pytest.mark.parametrize("tier", ["cpu-basic", "cpu-upgrade", "cpu-xl"])
def test_cpu_tiers_are_runnable(tier: str) -> None:
    payload = _space_payload()
    payload["runtime"]["hardware"]["requested"] = tier
    hf.check_runnable(_space_from(payload))  # does not raise


def test_private_and_gated_spaces_are_refused() -> None:
    with pytest.raises(UnrunnableSourceError, match="private"):
        hf.check_runnable(_space_from(_space_payload(private=True)))
    with pytest.raises(UnrunnableSourceError, match="gated"):
        hf.check_runnable(_space_from(_space_payload(gated="auto")))


def test_upstream_failure_warns_but_does_not_block() -> None:
    """A Space broken on HF's own infrastructure may be exactly the one
    you want to run locally."""
    payload = _space_payload()
    payload["runtime"]["stage"] = "RUNTIME_ERROR"
    space = _space_from(payload)
    hf.check_runnable(space)  # does not raise
    assert any("RUNTIME_ERROR" in w for w in hf.runtime_warnings(space))


def _space_from(payload: dict) -> hf.HfSpace:
    """Build an `HfSpace` from a payload without going through HTTP."""
    card = payload.get("cardData") or {}
    runtime = payload.get("runtime") or {}
    return hf.HfSpace(
        owner="someone",
        name="demo-space",
        sha=payload["sha"],
        sdk=payload.get("sdk"),
        sdk_version=card.get("sdk_version"),
        app_file=card.get("app_file"),
        python_version=card.get("python_version"),
        hardware=(runtime.get("hardware") or {}).get("requested"),
        stage=runtime.get("stage"),
        private=bool(payload.get("private")),
        gated=bool(payload.get("gated")),
        title=card.get("title"),
        files=tuple(
            hf.HfFile(
                path=s["rfilename"],
                size=s.get("size", 0),
                lfs_sha256=(s.get("lfs") or {}).get("sha256"),
            )
            for s in payload["siblings"]
        ),
    )


# --- manifest paths are attacker-controlled ---


@pytest.mark.parametrize(
    "app_file",
    [
        "../../../../etc/passwd",
        "/etc/passwd",
        "sub/../../escape.py",
    ],
)
def test_manifest_path_escaping_the_app_dir_is_refused(app_file: str, tmp_path: Path) -> None:
    payload = _space_payload()
    payload["cardData"]["app_file"] = app_file
    with pytest.raises(SourceError):
        core_pull.plan_from_space(_space_from(payload))


def test_symlinked_entrypoint_pointing_outside_is_refused(tmp_path: Path) -> None:
    """A string-only check passes here — `link.py` looks perfectly
    ordinary. Only resolving it catches that the repo contains a symlink
    aimed outside the app directory."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("SECRET = 1\n")
    (app_dir / "link.py").symlink_to(outside)

    with pytest.raises(SourceError, match="resolves outside the app directory"):
        core_pull.safe_relative_path("link.py", base=app_dir, label="app_file")


def test_ordinary_relative_entrypoint_is_accepted(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    (app_dir / "src").mkdir(parents=True)
    (app_dir / "src" / "main.py").write_text("x = 1\n")
    assert core_pull.safe_relative_path("src/main.py", base=app_dir, label="app_file") == Path(
        "src/main.py"
    )


@pytest.mark.parametrize("name", ["../escape", "with/slash", ".", "..", "", "a" * 200])
def test_invalid_app_names_are_rejected(name: str) -> None:
    assert not core_pull.valid_app_name(name)


def test_valid_app_names_are_accepted() -> None:
    assert core_pull.valid_app_name("real-time-stock-predictor")
    assert core_pull.valid_app_name("demo_app.v2")


# --- env-name scanning: names only, never values ---


def test_env_scan_finds_declared_names() -> None:
    source = (
        "import os\n"
        "a = os.environ['FINNHUB_API_KEY']\n"
        "b = os.environ.get('OPENAI_API_KEY')\n"
        "c = os.getenv('HF_TOKEN')\n"
    )
    assert core_pull.scan_env_names(source) == ("FINNHUB_API_KEY", "HF_TOKEN", "OPENAI_API_KEY")


def test_env_scan_drops_port_which_sidepage_injects_itself() -> None:
    assert core_pull.scan_env_names("import os\nos.environ.get('PORT')\n") == ()


def test_env_scan_ignores_names_that_arent_env_shaped() -> None:
    """The scan's output is displayed to humans and parsed by agents — a
    'name' carrying punctuation or prose has no business in either."""
    assert core_pull.scan_env_names("os.environ.get('lower_case')") == ()


# --- the pull command, end to end against the stubbed hub ---


def test_pull_downloads_registers_and_reports(sidepage_home: Path, fake_hub) -> None:
    result = runner.invoke(app, ["pull", "hf:someone/demo-space"])
    assert result.exit_code == 0, result.output

    registered = app_registry.get("demo-space")
    assert registered is not None
    assert registered.source is not None
    assert registered.source.commit == "458cee6d4cdc468566e64e542ca4f8f950e4ff1a"
    assert registered.source.managed is True
    assert registered.source.hf is not None
    assert registered.source.hf.sdk == "gradio"
    assert registered.source.hf.sdk_version == "5.29.0"
    # Requested, recorded, and not granted.
    assert registered.source.env_requested == ("FINNHUB_API_KEY",)
    assert registered.env_secrets == ()
    # Nothing is trusted until a human says so.
    assert registered.source.trusted_commit is None

    app_dir = app_source_dir("demo-space")
    assert (app_dir / "app.py").read_text() == APP_PY
    assert (app_dir / "model.h5").read_bytes() == WEIGHT
    assert registered.target == app_dir / "app.py"


def test_pull_reports_the_requested_env_name_without_granting_it(
    sidepage_home: Path, fake_hub
) -> None:
    result = runner.invoke(app, ["pull", "hf:someone/demo-space"])
    assert "FINNHUB_API_KEY" in result.output
    assert "not granted" in result.output
    assert "--env FINNHUB_API_KEY" in result.output


def test_dry_run_downloads_nothing_and_registers_nothing(sidepage_home: Path, fake_hub) -> None:
    result = runner.invoke(app, ["pull", "hf:someone/demo-space", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert app_registry.get("demo-space") is None
    assert not app_source_dir("demo-space").exists()
    assert "would pull" in result.output


def test_dry_run_reports_download_size_before_paying_for_it(
    sidepage_home: Path, fake_hub
) -> None:
    """The whole point against a Space carrying tens of gigabytes of
    weights: the metadata knows the size, so the cost is answerable
    without incurring it."""
    fake_hub["payload"]["siblings"][3]["size"] = 40 * 1024**3
    result = runner.invoke(app, ["pull", "hf:someone/demo-space", "--dry-run"])
    assert "40.0 GB" in result.output
    assert not app_source_dir("demo-space").exists()


def test_lfs_checksum_mismatch_is_caught_and_the_file_discarded(
    sidepage_home: Path, fake_hub
) -> None:
    fake_hub["bodies"]["model.h5"] = b"substituted content"
    result = runner.invoke(app, ["pull", "hf:someone/demo-space"])
    assert result.exit_code == 1
    assert "failed its checksum" in result.output
    assert app_registry.get("demo-space") is None


def test_pull_refuses_an_existing_name_without_force(sidepage_home: Path, fake_hub) -> None:
    runner.invoke(app, ["pull", "hf:someone/demo-space"])
    result = runner.invoke(app, ["pull", "hf:someone/demo-space"])
    assert result.exit_code == 1
    assert "already registered" in result.output


def test_pull_force_replaces_and_re_arms_trust(sidepage_home: Path, fake_hub) -> None:
    runner.invoke(app, ["pull", "hf:someone/demo-space"])
    app_registry.set_trusted_commit("demo-space", "458cee6d4cdc468566e64e542ca4f8f950e4ff1a")
    assert app_registry.get("demo-space").source.trusted_commit is not None

    fake_hub["payload"]["sha"] = "9" * 40
    result = runner.invoke(app, ["pull", "hf:someone/demo-space", "--force"])
    assert result.exit_code == 0, result.output

    updated = app_registry.get("demo-space")
    assert updated.source.commit == "9" * 40
    # New code, so the previous approval no longer applies.
    assert updated.source.trusted_commit is None


def test_pull_as_name_overrides_the_default(sidepage_home: Path, fake_hub) -> None:
    result = runner.invoke(app, ["pull", "hf:someone/demo-space", "--as", "stocks"])
    assert result.exit_code == 0, result.output
    assert app_registry.get("stocks") is not None
    assert app_source_dir("stocks").exists()


def test_json_output_is_one_parseable_line(sidepage_home: Path, fake_hub) -> None:
    result = runner.invoke(app, ["pull", "hf:someone/demo-space", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["app"] == "demo-space"
    assert payload["executed"] is False
    assert payload["plan"]["dependencies_installed"] is False
    assert payload["plan"]["env_requested"] == ["FINNHUB_API_KEY"]
    assert payload["plan"]["env_granted"] == []
    # Repo-authored strings live under `source`, so an agent can tell
    # sidepage's own output from a stranger's.
    assert payload["source"]["url"] == "huggingface.co/spaces/someone/demo-space"


def test_pull_never_writes_outside_the_managed_apps_dir(sidepage_home: Path, fake_hub) -> None:
    runner.invoke(app, ["pull", "hf:someone/demo-space"])
    assert app_source_dir("demo-space").parent == apps_dir()


def test_re_pull_replaces_the_tree_rather_than_merging(sidepage_home: Path, fake_hub) -> None:
    """`pull` always fetches the source's current state — a file the
    remote deleted must not survive locally."""
    runner.invoke(app, ["pull", "hf:someone/demo-space"])
    stale = app_source_dir("demo-space") / "leftover.py"
    stale.write_text("# from an older pull\n")

    result = runner.invoke(app, ["pull", "hf:someone/demo-space", "--force"])
    assert result.exit_code == 0, result.output
    assert not stale.exists()


# --- the serve-time trust gate ---


def _pull_then(sidepage_home: Path) -> None:
    runner.invoke(app, ["pull", "hf:someone/demo-space"])


def test_serve_refuses_downloaded_code_without_a_terminal(sidepage_home: Path, fake_hub) -> None:
    """No prompt means no execution: an agent that can `serve` an
    arbitrary pulled app without a gate is a remote-code-execution path."""
    _pull_then(sidepage_home)
    result = runner.invoke(app, ["serve", "demo-space"], input="")
    assert result.exit_code == 1
    assert "won't execute it" in result.output
    assert app_registry.get("demo-space").source.trusted_commit is None


def test_serve_declined_at_the_prompt_does_not_run(
    sidepage_home: Path, fake_hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pull_then(sidepage_home)
    monkeypatch.setattr("sidepage.output.is_interactive", lambda: True)
    result = runner.invoke(app, ["serve", "demo-space"], input="n\n")
    assert result.exit_code == 1
    assert app_registry.get("demo-space").source.trusted_commit is None


def test_trust_prompt_shows_what_is_about_to_run(
    sidepage_home: Path, fake_hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pull_then(sidepage_home)
    monkeypatch.setattr("sidepage.output.is_interactive", lambda: True)
    result = runner.invoke(app, ["serve", "demo-space"], input="n\n")
    assert "huggingface.co/spaces/someone/demo-space" in result.output
    assert "458cee6" in result.output
    assert "FINNHUB_API_KEY" in result.output


def test_local_apps_are_never_gated(sidepage_home: Path, tmp_path: Path) -> None:
    """Gating a file the user wrote themselves would be theater — the gate
    exists for code sidepage downloaded."""
    target = tmp_path / "site"
    target.mkdir()
    (target / "index.html").write_text("<h1>mine</h1>")
    app_registry.register(
        "local-app",
        target=target,
        target_kind=TargetKind.STATIC,
        name=None,
        domain=None,
        auth=AuthTier.OPEN,
        scope=Scope.LOCAL,
        anon=False,
        env_secrets=(),
        guardrail=None,
    )
    from sidepage.commands.serve import _require_source_trust

    _require_source_trust("local-app", app_registry.get("local-app"), waived=False)


def test_trust_is_recorded_per_commit_and_re_arms_when_code_changes(
    sidepage_home: Path, fake_hub
) -> None:
    _pull_then(sidepage_home)
    from sidepage.commands.serve import _require_source_trust

    registration = app_registry.get("demo-space")
    _require_source_trust("demo-space", registration, waived=True)
    trusted = app_registry.get("demo-space")
    assert trusted.source.trusted_commit == trusted.source.commit

    # Same commit: no further prompting, nothing raised.
    _require_source_trust("demo-space", trusted, waived=False)

    # Different commit: the recorded approval no longer matches.
    changed = AppSource(
        kind=trusted.source.kind,
        url=trusted.source.url,
        commit="f" * 40,
        managed=True,
        env_requested=trusted.source.env_requested,
        trusted_commit=trusted.source.trusted_commit,
    )
    from dataclasses import replace as dc_replace

    with pytest.raises(Exception, match="hasn't been approved"):
        _require_source_trust(
            "demo-space", dc_replace(trusted, source=changed), waived=False
        )


# --- app delete ---


def test_delete_removes_registry_entry_and_source_tree(sidepage_home: Path, fake_hub) -> None:
    _pull_then(sidepage_home)
    assert app_source_dir("demo-space").exists()

    result = runner.invoke(app, ["app", "delete", "demo-space", "--yes"])
    assert result.exit_code == 0, result.output
    assert app_registry.get("demo-space") is None
    assert not app_source_dir("demo-space").exists()


def test_delete_needs_confirmation_without_a_terminal(sidepage_home: Path, fake_hub) -> None:
    _pull_then(sidepage_home)
    result = runner.invoke(app, ["app", "delete", "demo-space"], input="")
    assert result.exit_code == 1
    assert app_source_dir("demo-space").exists()
    assert app_registry.get("demo-space") is not None


def test_delete_declined_at_the_prompt_removes_nothing(
    sidepage_home: Path, fake_hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pull_then(sidepage_home)
    monkeypatch.setattr("sidepage.output.is_interactive", lambda: True)
    result = runner.invoke(app, ["app", "delete", "demo-space"], input="n\n")
    assert result.exit_code == 0
    assert app_source_dir("demo-space").exists()
    assert app_registry.get("demo-space") is not None


def test_delete_never_touches_a_locally_registered_apps_files(
    sidepage_home: Path, tmp_path: Path
) -> None:
    """The rule that matters most here: an app registered against a path
    the user already had has no managed tree, and `delete` must remove the
    registry entry only. Deleting someone's project because they typed
    `delete` instead of `unregister` is not a mistake this is willing to
    make."""
    project = tmp_path / "my-project"
    project.mkdir()
    (project / "index.html").write_text("<h1>my own work</h1>")
    app_registry.register(
        "mine",
        target=project,
        target_kind=TargetKind.STATIC,
        name=None,
        domain=None,
        auth=AuthTier.OPEN,
        scope=Scope.LOCAL,
        anon=False,
        env_secrets=(),
        guardrail=None,
    )

    result = runner.invoke(app, ["app", "delete", "mine", "--yes"])
    assert result.exit_code == 0, result.output
    assert app_registry.get("mine") is None
    assert (project / "index.html").read_text() == "<h1>my own work</h1>"
    assert "no downloaded source" in result.output


def test_remove_source_tree_refuses_paths_outside_the_managed_dir(
    sidepage_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces under the command-layer check: even called
    directly, the core helper won't delete outside `apps_dir()`."""
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    monkeypatch.setattr(core_pull, "app_source_dir", lambda name: victim)
    with pytest.raises(SourceError, match="refusing to delete"):
        core_pull.remove_source_tree("anything")
    assert victim.exists()


def test_delete_refuses_while_the_app_is_running(
    sidepage_home: Path, fake_hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pull_then(sidepage_home)
    from sidepage.core import registry

    monkeypatch.setattr(
        registry,
        "get",
        lambda name: registry.RunningApp(
            name=name,
            pid=1,
            target="x",
            target_kind="code",
            listen_port=1234,
            url="http://127.0.0.1:1234",
            tunnel_url=None,
            started_at=0.0,
        ),
    )
    monkeypatch.setattr(registry, "is_alive", lambda pid: True)

    result = runner.invoke(app, ["app", "delete", "demo-space", "--yes"])
    assert result.exit_code == 1
    assert "currently running" in result.output
    assert app_source_dir("demo-space").exists()


def test_unregister_leaves_the_source_tree_alone(sidepage_home: Path, fake_hub) -> None:
    """`unregister` forgets the config; `delete` is the one that removes
    files. Keeping them distinct means neither is a surprise."""
    _pull_then(sidepage_home)
    result = runner.invoke(app, ["app", "unregister", "demo-space"])
    assert result.exit_code == 0, result.output
    assert app_registry.get("demo-space") is None
    assert app_source_dir("demo-space").exists()
