"""Notebook serving — backs the `notebook` target kind (spec v3 §12),
`sidepage serve notebook.ipynb --auth token`.

Full Jupyter Lab exposed — editable, live kernel, execution on the dev's
own machine. Confirmed in v3: Sidepage is tunnel-only, not a compute risk
surface, so this needs no elevated risk model beyond what running Lab
locally already implies.

Port is injected the same way as every other code target
(`sidepage.core.ecosystem.resolve_python_runner`), then `jupyter lab
<notebook> --port <port> --no-browser --ip=127.0.0.1 ...`. Jupyter's own
token/password auth is disabled *because* `sidepage.core.reverse_proxy` is
now the auth boundary instead — same reasoning as every other launcher,
not new to notebooks.

**Two notebook-specific wrinkles, both verified rather than assumed —
`Origin` and `Host` are checked separately by Jupyter Server, and going
through Sidepage's proxy breaks each of them for a different reason:**

1. Jupyter Server rejects cross-origin requests and WebSocket upgrades by
   default (`check_origin`), comparing the `Origin` header against its own
   `Host`. Through Sidepage's proxy, the browser's `Origin` is the *proxy's*
   own address (`http://127.0.0.1:<listen_port>`), not Jupyter's real,
   different upstream port — a mismatch that gets rejected out of the box.
   Confirmed live (a real kernel start + a real `execute_request` over a
   WebSocket carrying a deliberately-mismatched `Origin` header): the
   rejection reproduces without `--ServerApp.allow_origin` set, and passing
   `--ServerApp.disable_check_xsrf=True` alongside it fixes it — used below
   rather than assumed.

2. Separately, Jupyter Server also rejects any request whose `Host` header
   isn't "local" (`check_host`, gated by `ServerApp.allow_remote_access`,
   which defaults to `False` when bound to loopback). Sidepage's reverse
   proxy forwards the real inbound `Host` on plain HTTP requests
   (`reverse_proxy._forwarded_headers`'s `override_host=True` — deliberate,
   needed for other frameworks to generate correct absolute URLs/redirects)
   — through `--domain`/`--anon`, Jupyter sees `Host: <app>-<id>.<domain>`
   or `Host: <id>.trycloudflare.com`, neither of which look local, and
   `check_host` rejects the request outright (reproduced live: `sidepage
   serve notebook.ipynb --domain <domain> --auth token`, every static
   asset 403s with "Blocking request with non-local 'Host'"). Distinct
   from the `Origin` mismatch above — fixing that one doesn't touch this
   check at all.

**Both are allowlisted to the one real origin, not wildcarded, whenever
that origin is actually knowable at launch time** (`--domain`, or a plain
local serve — see `sidepage.core.process.serve`'s `public_origin`
computation): `--ServerApp.allow_origin=<public_origin>` for the `Origin`
check, `--ServerApp.local_hostnames=['localhost', '<host>']` for the
`Host` one — verified live against a real Jupyter Server that this rejects
a *different*, unlisted `Host`/`Origin` with a real 403 while accepting
the allowlisted one, not just silencing the check. `--anon` is the one
case with nothing to allowlist against: the `*.trycloudflare.com` hostname
isn't assigned until `cloudflared` reports it, *after* this launch command
already ran — that session falls back to `--ServerApp.allow_origin=*`
`--ServerApp.allow_remote_access=True` (both wide open), same
trust-boundary reasoning the MCP host wrapper's own
`enable_dns_rebinding_protection=False` already relies on: Sidepage's own
reverse proxy + `--auth` gate is the thing actually deciding whether a
request should reach this process at all, so a wrapped framework's own
Origin/Host check is at best redundant there — and, unlike the allowlisted
case above, there's no narrower value to give it for an as-yet-unassigned
hostname.

`--ServerApp.disable_check_xsrf=True` stays unconditional regardless of
`public_origin`: XSRF protection is a distinct, cookie-based mechanism
from either check above, and Sidepage's own `--auth` gate (not a
CSRF-style token dance with the wrapped app) is what actually protects a
mutating request here.

`--ip=127.0.0.1` (not Jupyter's own default, which isn't guaranteed across
versions) is the actual mitigation for the "what if this launch command
runs outside Sidepage's proxy" risk this module used to flag as
unmitigated — the wrapped process's bind address is fully
Sidepage-controlled by construction, same guarantee every other code
launcher already has, so there's no separate runtime check to add beyond
passing that flag ourselves.

**Dependencies via uv:** same pattern as every other code target — `uv
run --with jupyterlab jupyter lab`, with the target directory's own
`requirements.txt` (if any) layered underneath via
`sidepage.core.ecosystem.resolve_python_runner`. No standalone-vs-project
distinction needed: `resolve_python_runner` already degrades to a bare
`uv run --with jupyterlab` when there's no `requirements.txt` next to the
notebook, the same as it does for a Streamlit/FastAPI/MCP script with no
manifest of its own.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from sidepage.core import ecosystem


def build_jupyter_launch_command(
    notebook: Path, *, port: int, public_origin: str | None
) -> list[str]:
    """Construct the real `jupyter lab` launch command for `notebook`,
    binding to `port` on loopback only, with Jupyter's own auth disabled
    (the reverse proxy is the auth boundary) and its origin/XSRF/host
    checks relaxed enough to accept requests forwarded through that proxy
    — see this module's docstring for why each is needed and how
    `public_origin` (`sidepage.core.process.serve`'s computed real origin,
    or `None` for `--anon`) narrows them, verified rather than assumed.

    The caller is expected to run this with `cwd=notebook.parent` (see
    `sidepage.core.process.serve`, consistent with every other code
    target) — `notebook`'s absolute path is passed through regardless, so
    Jupyter opens it directly rather than requiring a nested lookup.
    """
    runner = ecosystem.resolve_python_runner(notebook.parent, extra_packages=["jupyterlab"])
    argv = runner + [
        "jupyter",
        "lab",
        str(notebook),
        "--port",
        str(port),
        "--no-browser",
        "--ip=127.0.0.1",
        "--ServerApp.token=",
        "--ServerApp.password=",
        "--ServerApp.disable_check_xsrf=True",
    ]
    if public_origin is not None:
        allowed_host = urlsplit(public_origin).netloc
        argv += [
            f"--ServerApp.allow_origin={public_origin}",
            # repr(), not manual string-building: this is the exact
            # syntax verified live against a real Jupyter Server for a
            # traitlets List(Unicode()) CLI value — a bare comma-joined
            # string is parsed as one literal hostname containing commas,
            # not multiple entries, and silently rejects everything.
            f"--ServerApp.local_hostnames={['localhost', allowed_host]!r}",
        ]
    else:
        argv += [
            "--ServerApp.allow_origin=*",
            "--ServerApp.allow_remote_access=True",
        ]
    return argv
