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

if __name__ == "__main__":
    mcp.run()  # stdio by default — never reached when sidepage wraps this
