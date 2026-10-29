"""JSON-RPC 2.0 dispatch for the MCP endpoint.

JSON-RPC errors are used only for protocol problems (bad JSON, unknown
method or tool, bad params, missing conversation id). Business failures are
returned by tools as normal results with isError=true.
"""

from dataclasses import dataclass, field
from typing import Any

from app.audit import AuditLog
from app.config import Settings
from app.sessions import SessionStore
from app.tools import ToolContext, call_tool, enabled_tool, list_tools

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "cirl-gateway", "version": "0.1.0"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


@dataclass
class RpcOutcome:
    status_code: int = 200
    body: dict | None = None
    headers: dict[str, str] = field(default_factory=dict)


def rpc_error(request_id: Any, code: int, message: str, status_code: int = 200) -> RpcOutcome:
    return RpcOutcome(status_code, {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _result(request_id: Any, result: dict, headers: dict | None = None) -> RpcOutcome:
    return RpcOutcome(200, {"jsonrpc": "2.0", "id": request_id, "result": result}, headers or {})


def handle(
    message: Any,
    headers: dict[str, str],
    *,
    settings: Settings,
    store: SessionStore,
    audit: AuditLog,
    new_session_id,
    turn_cache=None,
) -> RpcOutcome:
    """Handle one decoded JSON-RPC message. `headers` must be case-insensitive."""
    if isinstance(message, list):
        return rpc_error(None, INVALID_REQUEST, "Batch requests are not supported", 400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        req_id = message.get("id") if isinstance(message, dict) else None
        return rpc_error(req_id, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request", 400)

    method = message["method"]
    params = message.get("params") or {}

    # Notifications (e.g. notifications/initialized) get no response body.
    if "id" not in message:
        return RpcOutcome(202)
    req_id = message["id"]

    if not isinstance(params, dict):
        return rpc_error(req_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        return _result(
            req_id,
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO},
            {"mcp-session-id": new_session_id()},
        )
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": list_tools(settings)})
    if method == "tools/call":
        return _tools_call(req_id, params, headers, settings, store, audit, turn_cache)
    return rpc_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")


def _tools_call(req_id, params, headers, settings, store, audit, turn_cache=None) -> RpcOutcome:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(name, str):
        return rpc_error(req_id, INVALID_PARAMS, "params.name must be a string")
    if not isinstance(args, dict):
        return rpc_error(req_id, INVALID_PARAMS, "params.arguments must be an object")
    tool = enabled_tool(settings, name)
    if tool is None:  # a disabled tool is treated exactly like an unknown one
        return rpc_error(req_id, INVALID_PARAMS, f"Unknown tool: {name}")

    conversation_id = (headers.get("x-sa-conversation-id") or "").strip()
    if not conversation_id:
        return rpc_error(
            req_id,
            INVALID_PARAMS,
            "Missing X-Sa-Conversation-Id header: every tools/call must identify the conversation.",
        )

    ctx = ToolContext(
        settings=settings,
        store=store,
        conversation_id=conversation_id,
        end_user_phone=headers.get("x-sa-end-user-phone"),
        end_user_email=headers.get("x-sa-end-user-email"),
        end_user_name=headers.get("x-sa-end-user-name"),
        channel=headers.get("x-sa-channel"),
        auth_callback=headers.get("x-sa-auth-callback"),
        auth_callback_token=headers.get("x-sa-auth-callback-token"),
        turn_cache=turn_cache,
    )
    return _result(req_id, call_tool(tool, args, ctx, audit))
