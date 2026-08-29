from app import mcp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware


def make_app():
    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
    )
    return CORSMiddleware(
        app,
        allow_origins=['*'],
        allow_methods=['*'],
        allow_headers=['*'],
        expose_headers=['Mcp-Session-Id'],
    )
