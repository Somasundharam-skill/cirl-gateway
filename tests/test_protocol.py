from tests.conftest import API_KEY, headers, rpc


def test_health_needs_no_auth(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_mcp_requires_bearer(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert client.post("/mcp", json=body).status_code == 401
    assert client.post("/mcp", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/mcp", json=body, headers={"Authorization": API_KEY}).status_code == 401
    assert client.post("/mcp", json=body, headers=headers()).status_code == 200


def test_admin_audit_requires_bearer(client):
    assert client.get("/admin/audit").status_code == 401
    assert client.get("/admin/audit", headers=headers()).json() == {"entries": []}


def test_initialize_shape(client):
    resp = rpc(client, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers["mcp-session-id"]
    body = resp.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    result = body["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "cirl-gateway"


def test_initialized_notification_accepted(client):
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers())
    assert resp.status_code == 202


def test_tools_list_shape(client):
    tools = rpc(client, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == ["list_policies", "verify_identity_otp"]
    for t in tools:
        assert t["description"] and "call" in t["description"].lower()
        assert t["input_schema"]["type"] == "object" and t["inputSchema"] == t["input_schema"]


def test_unknown_method_is_jsonrpc_error(client):
    assert rpc(client, "nope").json()["error"]["code"] == -32601


def test_unknown_tool_is_jsonrpc_error(client):
    body = rpc(client, "tools/call", {"name": "nope", "arguments": {}}).json()
    assert body["error"]["code"] == -32602


def test_malformed_requests(client):
    h = headers()
    h["Content-Type"] = "application/json"
    resp = client.post("/mcp", content=b"{not json", headers=h)
    assert resp.status_code == 400 and resp.json()["error"]["code"] == -32700
    resp = client.post("/mcp", json={"id": 1, "method": "tools/list"}, headers=h)
    assert resp.json()["error"]["code"] == -32600
    resp = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=h)
    assert resp.json()["error"]["code"] == -32600


def test_missing_conversation_id(client):
    body = rpc(client, "tools/call", {"name": "list_policies", "arguments": {}}, conversation_id=None).json()
    assert body["error"]["code"] == -32602
    assert "X-Sa-Conversation-Id" in body["error"]["message"]
