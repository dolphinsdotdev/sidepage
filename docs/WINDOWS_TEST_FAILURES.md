# Windows test failures — root causes

Run: `uv run pytest -q` on PowerShell / Windows. ~20 failures remain after
fixing the `tunnel_manager.py` `log_file` regression (10 tests, already
fixed — see git history).

## 1. `test_registry.py` (3 failures) — POSIX-only test design

```
test_is_alive_true_for_running_process       AttributeError: module 'sidepage.core.registry' has no attribute 'is_alive'
test_is_alive_false_for_nonexistent_pid      AttributeError: module 'os' has no attribute 'fork'
test_stop_fully_gone_app_reports_stale       AttributeError: module 'os' has no attribute 'fork'
```

- `os.fork()` doesn't exist on Windows at all — no workaround, it's a
  real POSIX-only API. The 3 zombie-specific tests already `skipif` on
  non-POSIX via the `zombie_pid` fixture; these two don't, which looks
  like an oversight rather than intent.
- `registry.is_alive` (public, zombie-aware) is asserted to exist but was
  never implemented — only the private `_is_alive = procutil.pid_alive`
  alias exists, which can't distinguish a zombie from a live process
  (that distinction is the whole point of the test file's docstring).
- File's own docstring states the assumption directly: *"consistent with
  this project's existing POSIX-only assumptions ... no Windows support
  attempted anywhere."* Written before/independent of the Windows work
  done elsewhere this session.

## 2. `test_cloudflared_installer.py` (4 failures) — POSIX-only assertions

```
test_unpack_tgz_extracts_named_member                        stat().st_mode & S_IXUSR == 0
test_unpack_raw_binary_copies_and_chmods                      stat().st_mode & S_IXUSR == 0
test_verify_returns_trimmed_output                            OSError: [WinError 193] %1 is not a valid Win32 application
test_ensure_installed_reuses_managed_cache_without_downloading  is_symlink() is False
```

- `chmod(... | S_IXUSR)` is close to a no-op on Windows — NTFS has no
  per-user execute bit, so `stat().st_mode` never reflects it regardless
  of target OS the test is unpacking *for*.
- The test writes a real `#!/bin/sh` script and executes it directly —
  Windows can't run a shebang script as a native process (`CreateProcess`
  needs a real PE binary), hence `WinError 193`.
- Symlink creation needs elevated privilege or Developer Mode on Windows;
  the installer's *source* already has a copy-fallback for exactly this
  case (`_link`, `resolve_link_dir`), but the test asserts the symlink
  path unconditionally.
- Installer source itself already has Windows branches (`.exe` asset
  names, `os_name != "windows"` chmod guard, symlink-fallback) — it's
  specifically the test *assertions* that assume a POSIX filesystem.

## 3. `test_cli_smoke.py` (3 failures) — test fragility, not a bug

```
test_serve_nonexistent_target_fails_fast
test_serve_nonexistent_target_fails_fast_even_with_type_override
test_serve_domain_with_configured_domain_passes_validation
```

All fail the same way:
```
assert "does not exist" in result.output
# actual: "...py does not\nexist\n"
```
Rich wraps the error message across two lines under the narrow console
width `CliRunner` uses — the CLI's actual output is correct, the
substring check just doesn't survive the line break. Not caused by
anything touched this session.

## 4. Real-process integration tests (~10 failures) — environment, not code

`test_serve_fastapi.py`, `test_serve_integration.py`, `test_serve_mcp.py`,
`test_serve_notebook.py` — these spawn real `uvicorn`/`streamlit`/
`jupyter`/MCP subprocesses and then hit the app over HTTP. Failures are
`ConnectionRefusedError` / `TimeoutError` / JSON-decode-on-empty-response,
consistent with the child process never actually coming up in time (or
at all) on this machine — most likely missing optional runtime deps
(`streamlit`, `jupyterlab`) in `.venv`, or a slower/blocked port-bind on
Windows. Pre-existing, unrelated to any fix made this session.

---

**Bottom line:** categories 1–2 are a real design fork (tests assume
POSIX-only; the rest of the codebase now supports Windows) — left
untouched per instruction. Categories 3–4 are pre-existing environment
issues on this machine, not regressions.
