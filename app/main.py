"""CIRL gateway: mock MCP server for the WhatsApp eIA onboarding demo."""

import json
import logging
import os
import secrets
import sys
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app import mcp, password_page
from app.audit import AuditLog
from app.config import Settings, load_settings
from app.sessions import InMemorySessionStore
from app.tools import check_enabled_tools

logger = logging.getLogger("cirl_gateway")


class RedactQueryFilter(logging.Filter):
    """Strip the query string from uvicorn access-log lines for /set-password (the link token is in it)."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access args: (client_addr, method, full_path, http_version, status_code)
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            if args[2].startswith("/set-password"):
                record.args = args[:2] + (args[2].split("?", 1)[0],) + args[3:]
        return True


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("cirl_gateway").setLevel(level)

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactQueryFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactQueryFilter())

    # Audit lines are emitted as bare JSON, one per line.
    audit_logger = logging.getLogger("cirl_gateway.audit")
    if not any(getattr(h, "_cirl_audit", False) for h in audit_logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._cirl_audit = True
        audit_logger.addHandler(handler)
    audit_logger.propagate = False


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    check_enabled_tools(settings)
    configure_logging(settings.log_level)
    if not settings.gateway_api_key:
        logger.warning("GATEWAY_API_KEY is not set: all /mcp and /admin requests will be rejected")

    app = FastAPI(title="CIRL Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = InMemorySessionStore(ttl_seconds=settings.session_ttl_minutes * 60)
    app.state.audit = AuditLog()
    password_page.add_routes(app)
    if not settings.whatsapp_number:
        logger.warning("WHATSAPP_NUMBER is not set: the password success page will have no 'Return to WhatsApp' button")

    def authorized(request: Request) -> bool:
        expected = settings.gateway_api_key
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return bool(expected) and scheme.lower() == "bearer" and secrets.compare_digest(token.strip(), expected)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        if not authorized(request):
            return _unauthorized()
        # Responses are always application/json, whether the client accepts
        # application/json, text/event-stream or both.
        try:
            message = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            outcome = mcp.rpc_error(None, mcp.PARSE_ERROR, "Parse error", 400)
        else:
            try:
                outcome = mcp.handle(
                    message,
                    request.headers,
                    settings=settings,
                    store=app.state.store,
                    audit=app.state.audit,
                    new_session_id=lambda: uuid.uuid4().hex,
                )
            except Exception:
                logger.exception("Unhandled error in /mcp")
                req_id = message.get("id") if isinstance(message, dict) else None
                outcome = mcp.rpc_error(req_id, mcp.INTERNAL_ERROR, "Internal error", 500)

        if outcome.body is None:
            return Response(status_code=outcome.status_code, headers=outcome.headers)
        return JSONResponse(outcome.body, status_code=outcome.status_code, headers=outcome.headers)

    @app.get("/admin/audit")
    def admin_audit(request: Request):
        if not authorized(request):
            return _unauthorized()
        return {"entries": app.state.audit.latest(200)}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
