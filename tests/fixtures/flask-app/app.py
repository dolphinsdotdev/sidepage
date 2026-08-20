"""Fixture for `tests/test_proxy_frameworks.py` — a real, minimal Flask app
standing in for "a service the user already has running" that `sidepage
proxy` wraps.

Started directly (`python app.py`, reading `$PORT`), never through
`sidepage serve` — the whole point is that sidepage never launches or
configures it, matching what `sidepage proxy` actually wraps.

`TRUST_PROXY=1` toggles `werkzeug.middleware.proxy_fix.ProxyFix` — this is
the one-line fix `sidepage proxy --help`'s Origin/Host/CSRF section
recommends for Flask. Without it, `/whoami` reflects the raw upstream
connection (`127.0.0.1:<port>`, `http`) even though sidepage forwards the
real `Host`/`X-Forwarded-*` correctly; with it, `/whoami` reflects the
real values — demonstrating that forwarding alone is necessary but not
sufficient, the app has to opt in.

`/debug/admin` stands in for the class of app-level "only localhost may
reach this" surface the `--help` security warning is about (Werkzeug's
own interactive debugger is the concrete, RCE-relevant real example) — it
checks `request.remote_addr` directly, the same thing such real
protections check, deliberately not `X-Forwarded-For`.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request

app = Flask(__name__)

if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


@app.get("/")
def index() -> str:
    return "flask fixture ok"


@app.get("/whoami")
def whoami():
    return jsonify(
        host=request.host,
        scheme=request.scheme,
        remote_addr=request.remote_addr,
        url_root=request.url_root,
    )


@app.get("/debug/admin")
def admin():
    if request.remote_addr != "127.0.0.1":
        return "forbidden", 403
    return "admin panel: only reachable from localhost -- by request.remote_addr, not headers"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
