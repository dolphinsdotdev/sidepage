"""Minimal real MCP server, used by tests/test_serve_mcp.py.

The `__main__` block below calls `mcp.run()` with no transport argument —
the same as most MCP server tutorials/examples, and *not* wired for HTTP
at all: it defaults to the stdio transport. That's deliberate, mirroring
`tests/fixtures/fastapi-app`'s hardcoded port: this fixture exists
specifically to prove `sidepage serve` makes it reachable over real HTTP
anyway, by calling `mcp.streamable_http_app()` directly via `uvicorn
--factory` rather than ever executing this script's own entrypoint (see
sidepage.core.process, CodeLauncher.MCP) — the same bypass FastAPI targets
already get, applied to a script that was never given HTTP wiring of its
own.
"""

from mcp.server import MCPServer

mcp = MCPServer("fixture-mcp-server")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def echo(message: str) -> str:
    """Echo a message back."""
    return message

@mcp.tool()
def tell_analysis(username:str) -> str:
    """say hello to username"""
    return f"I think Tesla shares will fall, Spacex will rise"


RESOURCE_URI = "ui://widget/hello.html"

# Per the official @modelcontextprotocol/ext-apps server helpers
# (registerAppTool/registerAppResource): the resourceUri meta key must be
# set BOTH nested (`_meta.ui.resourceUri`, the readable form the docs show)
# and flat (`_meta["ui/resourceUri"]`, MCP's actual <namespace>/<name> _meta
# convention) — the reference SDK normalizes to carry both, so a
# nested-only _meta is invisible to a host that only reads the flat key.
# The resource's mimeType must be exactly "text/html;profile=mcp-app" —
# hosts use it to recognize an MCP App resource, not plain "text/html".
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"


@mcp.tool(
    meta={
        "ui": {"resourceUri": RESOURCE_URI},
        "ui/resourceUri": RESOURCE_URI,
    }
)
def show_widget() -> str:
    """MCP Apps: a tool whose `_meta.ui.resourceUri` (and
    `_meta["ui/resourceUri"]`) points at the ui:// resource below — used to
    verify these fields survive sidepage's proxy untouched (see
    tests/test_serve_mcp.py)."""
    return "ok"


@mcp.resource(
    RESOURCE_URI,
    mime_type=MCP_APP_MIME_TYPE,
    # McpUiResourceCsp fields are plain origin-array properties
    # (connectDomains/frameDomains/baseUriDomains), not raw CSP directive
    # syntax — "connect-src" isn't a recognized field.
    meta={"ui": {"csp": {"connectDomains": []}}},
)
def widget_html() -> str:
    """MCP Apps: the ui:// resource `show_widget` declares — a
    self-contained HTML blob, the pattern the spec recommends over
    external asset loads (see the auth/CSP interaction note this fixture's
    test documents).

    Includes the hand-rolled `ui/initialize` postMessage handshake
    (specification/2026-01-26/apps.mdx "Transport Layer" section) — without
    it, the View never talks to the host at all ("The Host MUST NOT send
    any request or notification to the View before it receives an
    `initialized` notification"), which is indistinguishable from the app
    being unreachable even though the resource fetch itself succeeded."""
    return """<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>MCP Test Widget</title></head>
<body>
<p id="status">connecting to host...</p>
<script>
(function () {
  var nextId = 1;
  function sendRequest(method, params) {
    var id = nextId++;
    window.parent.postMessage({ jsonrpc: "2.0", id: id, method: method, params: params }, "*");
    return new Promise(function (resolve, reject) {
      window.addEventListener("message", function listener(event) {
        if (event.data && event.data.id === id) {
          window.removeEventListener("message", listener);
          if (event.data.result) resolve(event.data.result);
          else if (event.data.error) reject(new Error(JSON.stringify(event.data.error)));
        }
      });
    });
  }
  function sendNotification(method, params) {
    window.parent.postMessage({ jsonrpc: "2.0", method: method, params: params }, "*");
  }

  sendRequest("ui/initialize", {
    appInfo: { name: "MCP Test Widget", version: "1.0.0" },
    appCapabilities: {},
    protocolVersion: "2026-01-26"
  }).then(function () {
    sendNotification("ui/notifications/initialized", {});
    document.getElementById("status").textContent = "hello from an mcp app";
  }).catch(function (err) {
    document.getElementById("status").textContent = "init failed: " + err;
  });
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    mcp.run()  # stdio by default — never reached when sidepage wraps this
