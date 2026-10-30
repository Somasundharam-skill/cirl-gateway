import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import cirl_client, clock
from app.config import load_settings
from app.main import create_app

API_KEY = "test-key"
CONVERSATION_ID = "conv-test-1"
RAMESH_PHONE = "+91 73733 45820"  # two policies (data/employees.csv)
PRIYA_PHONE = "+91 98765 45820"  # no policies
UNKNOWN_PHONE = "+91 90000 00001"


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def fake_clock(monkeypatch):
    fc = FakeClock()
    monkeypatch.setattr(clock, "now", fc.now)
    return fc


@pytest.fixture(autouse=True)
def fresh_mock_cirl():
    """The mock CIRL keeps OTP state per eIA in process memory; start every test clean."""
    cirl_client.reset_mock()
    yield
    cirl_client.reset_mock()


def make_app(**env):
    return create_app(load_settings({"GATEWAY_API_KEY": API_KEY, **env}))


@pytest.fixture
def app(fake_clock):
    return make_app()


@pytest.fixture
def client(app):
    return TestClient(app)


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def captured_logs():
    """Every log line from the gateway's loggers, formatted."""
    handler = ListHandler()
    loggers = [logging.getLogger(n) for n in ("cirl_gateway", "cirl_gateway.audit", "cirl_gateway.tools")]
    for lg in loggers:
        lg.addHandler(handler)
    yield handler.lines
    for lg in loggers:
        lg.removeHandler(handler)


def headers(conversation_id: str | None = CONVERSATION_ID, phone: str | None = RAMESH_PHONE, **extra) -> dict:
    h = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json, text/event-stream"}
    if conversation_id is not None:
        h["X-Sa-Conversation-Id"] = conversation_id
    if phone is not None:
        h["X-Sa-End-User-Phone"] = phone
    h["X-Sa-Channel"] = "whatsapp"
    h.update(extra)
    return h


def rpc(client, method, params=None, req_id=1, **header_kw):
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers=headers(**header_kw))


def call(client, tool, arguments=None, **header_kw) -> tuple[dict, dict]:
    """Call a tool; return (MCP result, decoded text payload)."""
    resp = rpc(client, "tools/call", {"name": tool, "arguments": arguments or {}}, **header_kw)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "result" in body, body
    result = body["result"]
    return result, json.loads(result["content"][0]["text"])


def audit_entries(client) -> list[dict]:
    return client.get("/admin/audit", headers=headers()).json()["entries"]
