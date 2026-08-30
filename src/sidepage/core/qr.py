"""`--qr` — prints a terminal QR code for the URL `sidepage serve` ends up
with (tunnel URL if one was opened, else the local URL). Independent of
`--pwa`: useful any time there's a URL worth scanning onto a phone, not
gated behind PWA mode.

`qrcode` is a hard dependency (not a soft/optional one — see the reference
`qr_gen.py` this mirrors, which does guard the import): confirmed with the
user that always-works-out-of-the-box beats a smaller install for a tool
whose whole pitch is "scan this to get it on your phone." `print_tty()`
alone (no `qrcode[pil]` extra) needs nothing beyond the base install.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager

import qrcode

from sidepage.output import warn


@contextmanager
def _standard_terminal_output():
    try:
        yield
    finally:
        sys.stdout.flush()


def print_qr(url: str) -> None:
    """Print a compact QR code for `url` to stdout using the terminal's
    own block-character rendering (`print_tty`) — no image file, no
    external renderer.

    `print_tty()` itself raises `OSError` outright when stdout isn't a
    real tty (piped output, output redirected to a file, a non-interactive
    CI runner) — verified live, not assumed. `--qr` failing that way would
    otherwise take the rest of a perfectly working `serve` down with it;
    caught here and downgraded to a warning instead; every command that
    matters (the actual URL) was already printed before this runs.
    """
    qr = qrcode.QRCode(version=1, box_size=1, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        with _standard_terminal_output():
            qr.print_tty()
    except OSError:
        warn("--qr: stdout isn't a terminal, can't render a QR code here")
