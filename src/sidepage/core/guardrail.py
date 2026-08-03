"""Guardrails & pre/post-processing — future, backs `sidepage serve
--guardrail <config.yaml>`.

Present in the original spec (§8: "not available day 1... genuinely just
deferred, no architecture conflict"). **v3 doesn't mention guardrails
anywhere** — not in its numbered sections, not in its "parked for future
discussion" list (§15). Treated here as *not re-stated*, not *dropped*:
v3 cut several things outright (standing API keys, streamlit/api/mcp
scaffolding) and said so explicitly each time, which this section doesn't
get. Kept as a placeholder, flagged in `docs/OPEN_QUESTIONS.md` so it isn't
silently lost — if a future spec revision confirms this was actually cut,
this module and the `--guardrail` flag on `serve` should go with it.

When built, this belongs at the SDK/wrapping layer, before traffic even
reaches `sidepage.core.reverse_proxy` — consistent with the no-MITM stance
on Sidepage's cloud backend (§6/§7): guardrails are app-side pre/post
processing, not something the proxy or tunnel do on the app's behalf.
"""

from __future__ import annotations

from pathlib import Path


def load_guardrail_config(path: Path) -> None:
    """Parse a guardrail config file for use by `sidepage serve --guardrail`.

    Not implemented — no config schema has been designed yet.
    """
    raise NotImplementedError
