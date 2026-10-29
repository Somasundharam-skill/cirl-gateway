import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_ENABLED_TOOLS, load_settings
from app.main import create_app
from app.tools import TOOLS
from tests.conftest import API_KEY, call, headers, rpc

DEFAULT = ["journey_turn"]
DISABLED = [
    "start_journey",
    "record_consent",
    "check_existing_eia",
    "send_otp",
    "verify_otp",
    "send_email_otp",
    "verify_email_otp",
    "create_password_link",
    "check_password_status",
    "start_kyc",
    "defer_kyc",
]


def listed(client) -> list[str]:
    return [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]]


def test_default_is_journey_turn_only(default_client):
    assert list(DEFAULT_ENABLED_TOOLS) == DEFAULT
    assert listed(default_client) == DEFAULT


@pytest.mark.parametrize("name", DISABLED)
def test_disabled_tool_is_rejected_as_unknown(default_client, name):
    body = rpc(default_client, "tools/call", {"name": name, "arguments": {}}).json()
    assert body["error"] == {"code": -32602, "message": f"Unknown tool: {name}"}
    # Rejected before the handler runs: nothing is audited.
    assert default_client.get("/admin/audit", headers=headers()).json() == {"entries": []}


def test_disabled_tool_code_is_kept():
    for name in DISABLED:
        assert name in TOOLS


def test_journey_with_defaults(default_client, default_app):
    def turn(message):
        result, payload = call(default_client, "journey_turn", {"customer_message": message})
        assert result["isError"] is False
        return payload

    assert turn("Hi")["state"] == "CONSENT"
    assert turn("yes")["state"] == "EMAIL"
    assert turn("rahul@gmail.com")["state"] == "OTP"
    assert turn("123456")["state"] == "PASSWORD"
    assert default_app.state.store.get("conv-test-1").email_verified is True


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", DEFAULT),
        ("  ,  ", DEFAULT),
        ("start_journey, record_consent", ["start_journey", "record_consent"]),
        ("journey_turn,start_kyc", ["journey_turn", "start_kyc"]),
        ("record_consent,start_journey,start_journey", ["start_journey", "record_consent"]),  # registry order
        (",".join(TOOLS), list(TOOLS)),
    ],
)
def test_enabled_tools_env(fake_clock, value, expected):
    client = TestClient(create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "ENABLED_TOOLS": value})))
    assert listed(client) == expected


def test_unknown_enabled_tool_refuses_to_start():
    with pytest.raises(ValueError, match="start_jorney"):
        create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "ENABLED_TOOLS": "start_jorney,record_consent"}))


def test_start_journey_description(client):
    tools = {t["name"]: t for t in rpc(client, "tools/list").json()["result"]["tools"]}
    description = tools["start_journey"]["description"]
    assert description == "Starts a new eIA registration for this conversation. Call this once at the start."
    assert "existing" not in description.lower() and "check" not in description.lower()
