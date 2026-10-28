import json

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from tests.conftest import API_KEY, call, headers, start_to_consent
from tests.test_privacy import assert_clean

EMAIL = "rahul@gmail.com"


def assert_tool_error(result, payload, code, message_for_agent=None):
    assert result["isError"] is True
    assert payload["ok"] is False and payload["error_code"] == code and payload["error_msg"]
    if message_for_agent is not None:
        assert payload["message_for_agent"] == message_for_agent


def send(client, email=EMAIL):
    return call(client, "send_email_otp", {"email_id": email})


def verify(client, otp):
    return call(client, "verify_email_otp", {"otp_no": otp})


# --- send_email_otp --------------------------------------------------------------------


def test_send_requires_consent(client):
    result, payload = send(client)  # no session at all
    assert_tool_error(result, payload, "GW-002", "Ask the customer for consent first.")
    call(client, "start_journey")
    result, payload = send(client)
    assert_tool_error(result, payload, "GW-002", "Ask the customer for consent first.")


def test_send_refused_after_consent_refused(client):
    call(client, "start_journey")
    call(client, "record_consent", {"consent": False, "notice_version": "v1.0"})
    result, payload = send(client)
    assert_tool_error(result, payload, "GW-002")


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "", "  ", 42])
def test_send_invalid_email(client, email):
    start_to_consent(client)
    result, payload = call(client, "send_email_otp", {"email_id": email})
    assert_tool_error(result, payload, "GW-004", "Ask the customer for a valid email ID.")


def test_send_success(client, app):
    start_to_consent(client)
    result, payload = send(client)
    assert result["isError"] is False
    assert payload["ok"] is True
    assert payload["email_id"] == EMAIL
    assert payload["expires_in_seconds"] == 120
    session = app.state.store.get("conv-test-1")
    assert session.email_otp_email_id == EMAIL
    assert session.email_otp_expires_at is not None


def test_send_limit(client):
    start_to_consent(client)
    for _ in range(5):
        result, _ = send(client)
        assert result["isError"] is False
    result, payload = send(client)
    assert_tool_error(result, payload, "GW-010")


# --- verify_email_otp ------------------------------------------------------------------


def test_verify_requires_send(client):
    start_to_consent(client)
    result, payload = verify(client, "123456")
    assert_tool_error(result, payload, "GW-014")


def test_verify_success(client, app):
    start_to_consent(client)
    send(client)
    result, payload = verify(client, "123456")
    assert result["isError"] is False
    assert payload["ok"] is True and payload["email_verified"] is True
    assert app.state.store.get("conv-test-1").email_verified is True


def test_verify_wrong_code(client):
    start_to_consent(client)
    send(client)
    result, payload = verify(client, "000000")
    assert_tool_error(
        result, payload, "GW-011", "Tell the customer the code is incorrect and ask them to re-enter it."
    )
    assert payload["attempts_remaining"] == 2


def test_verify_expired_after_120s(client, fake_clock):
    start_to_consent(client)
    send(client)
    fake_clock.advance(121)
    result, payload = verify(client, "123456")
    assert_tool_error(
        result, payload, "GW-012", "Tell the customer the code has expired and offer to send a new one."
    )
    # A new code works.
    send(client)
    result, _ = verify(client, "123456")
    assert result["isError"] is False


def test_verify_valid_at_120s(client, fake_clock):
    start_to_consent(client)
    send(client)
    fake_clock.advance(120)
    result, _ = verify(client, "123456")
    assert result["isError"] is False


def test_lockout_counts_across_resends(client):
    start_to_consent(client)
    send(client)
    assert_tool_error(*verify(client, "000000"), "GW-011")
    send(client)
    assert_tool_error(*verify(client, "000001"), "GW-011")
    send(client)
    assert_tool_error(*verify(client, "000002"), "GW-013")
    # Locked for the session: the right code and new sends are refused.
    assert_tool_error(*verify(client, "123456"), "GW-013")
    assert_tool_error(*send(client), "GW-013")


def test_email_lock_survives_start_journey(client):
    start_to_consent(client)
    send(client)
    for otp in ("000000", "000001", "000002"):
        verify(client, otp)
    start_to_consent(client)  # same conversation: idempotent, lock stays
    assert_tool_error(*send(client), "GW-013")
    # A new conversation starts fresh.
    start_to_consent(client, conversation_id="conv-test-2")
    result, _ = call(client, "send_email_otp", {"email_id": EMAIL}, conversation_id="conv-test-2")
    assert result["isError"] is False


def test_mock_email_otp_from_env(fake_clock):
    app = create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "MOCK_EMAIL_OTP": "654321"}))
    client = TestClient(app)
    start_to_consent(client)
    send(client)
    assert_tool_error(*verify(client, "123456"), "GW-011")
    result, _ = verify(client, "654321")
    assert result["isError"] is False


def test_invalid_mock_email_otp_rejected():
    with pytest.raises(ValueError):
        load_settings({"MOCK_EMAIL_OTP": "12ab"})


def test_real_mode_fails_safely(fake_clock):
    app = create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "CIRL_MODE_SEND_EMAIL_OTP": "real"}))
    client = TestClient(app)
    start_to_consent(client)
    assert_tool_error(*send(client), "GW-501")


# --- privacy ---------------------------------------------------------------------------


def test_email_otp_audit_is_masked(client, app, captured_logs):
    start_to_consent(client)
    send(client)
    verify(client, "654321")
    verify(client, "123456")

    entries = client.get("/admin/audit", headers=headers()).json()["entries"]
    email_entries = [e for e in entries if e["tool"] in ("send_email_otp", "verify_email_otp")]
    assert [e["event"] for e in email_entries] == ["email_otp_sent", "tool_failed", "email_verified"]
    assert email_entries[0]["input"]["email_id"] == "ra*****@gmail.com"
    assert email_entries[0]["output"]["email_id"] == "ra*****@gmail.com"
    assert all(e["input"]["otp_no"] == "******" for e in email_entries[1:])

    for text in [json.dumps(e) for e in entries] + captured_logs:
        assert_clean(text)
        assert EMAIL not in text
        assert "654321" not in text
    stored = repr(app.state.store.get("conv-test-1"))
    assert "123456" not in stored and "654321" not in stored
