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


# --- message_for_customer --------------------------------------------------------------

SENT_TEXT = (
    "Great! We've sent an Email Verification Code to rahul@gmail.com. "
    "Please enter the 6-digit OTP below (Valid for 120 seconds):"
)
VERIFIED_TEXT = (
    "Email verified! Let's establish your portal access. Please enter a secure password for your new customer account.\n"
    "Criteria:\n- At least 8 characters\n- Mixed case & 1 special character"
)
INVALID_EMAIL_TEXT = "That doesn't look like a valid email address. Please enter your valid Email ID:"
SEND_LIMIT_TEXT = "You've reached the limit for verification codes. Please try again later."
WRONG_TEXT = "That code is incorrect. Please re-enter the 6-digit OTP:"
EXPIRED_TEXT = "That code has expired. Reply RESEND and I'll send you a new one."
LOCKED_TEXT = "Too many incorrect attempts. Please start again later by sending Hi."


def test_customer_text_send_success(client):
    start_to_consent(client)
    result, payload = send(client)
    assert result["isError"] is False
    assert payload["message_for_customer"] == SENT_TEXT
    assert payload["message_for_agent"]


def test_customer_text_invalid_email(client):
    start_to_consent(client)
    result, payload = send(client, "not-an-email")
    assert_tool_error(result, payload, "GW-004", "Ask the customer for a valid email ID.")
    assert payload["message_for_customer"] == INVALID_EMAIL_TEXT


def test_customer_text_send_limit(client):
    start_to_consent(client)
    for _ in range(5):
        send(client)
    result, payload = send(client)
    assert_tool_error(result, payload, "GW-010")
    assert payload["message_for_customer"] == SEND_LIMIT_TEXT


def test_resend_without_email_reuses_stored_email(client, app):
    start_to_consent(client)
    send(client)
    for args in ({}, {"email_id": ""}):
        result, payload = call(client, "send_email_otp", args)
        assert result["isError"] is False
        assert payload["email_id"] == EMAIL
        assert payload["message_for_customer"] == SENT_TEXT
    assert app.state.store.get("conv-test-1").email_otp_send_count == 3


def test_resend_with_new_email_replaces_stored_email(client, app):
    start_to_consent(client)
    send(client)
    _, payload = send(client, "priya@example.com")
    assert payload["email_id"] == "priya@example.com"
    _, payload = call(client, "send_email_otp", {})
    assert payload["email_id"] == "priya@example.com"


def test_first_send_without_email_is_invalid(client):
    start_to_consent(client)
    result, payload = call(client, "send_email_otp", {})
    assert_tool_error(result, payload, "GW-004")
    assert payload["message_for_customer"] == INVALID_EMAIL_TEXT


def test_customer_text_verify_success(client):
    start_to_consent(client)
    send(client)
    result, payload = verify(client, "123456")
    assert result["isError"] is False
    assert payload["message_for_customer"] == VERIFIED_TEXT
    _, payload = verify(client, "123456")  # already verified
    assert payload["message_for_customer"] == VERIFIED_TEXT


def test_customer_text_wrong_code(client):
    start_to_consent(client)
    send(client)
    result, payload = verify(client, "000000")
    assert_tool_error(result, payload, "GW-011")
    assert payload["message_for_customer"] == WRONG_TEXT
    # Not 6 digits: same customer text, no attempt used.
    result, payload = verify(client, "12ab")
    assert_tool_error(result, payload, "GW-004")
    assert payload["message_for_customer"] == WRONG_TEXT
    _, payload = verify(client, "000001")
    assert payload["attempts_remaining"] == 1


def test_customer_text_expired(client, fake_clock):
    start_to_consent(client)
    send(client)
    fake_clock.advance(121)
    result, payload = verify(client, "123456")
    assert_tool_error(result, payload, "GW-012")
    assert payload["message_for_customer"] == EXPIRED_TEXT


def test_customer_text_locked(client):
    start_to_consent(client)
    send(client)
    verify(client, "000000")
    verify(client, "000001")
    for result, payload in (verify(client, "000002"), verify(client, "123456"), send(client), call(client, "send_email_otp", {})):
        assert_tool_error(result, payload, "GW-013")
        assert payload["message_for_customer"] == LOCKED_TEXT


def test_no_customer_text_where_none_is_defined(client):
    result, payload = send(client)  # GW-002, no consent
    assert_tool_error(result, payload, "GW-002")
    assert "message_for_customer" not in payload
    start_to_consent(client)
    result, payload = verify(client, "123456")  # GW-014, nothing sent
    assert_tool_error(result, payload, "GW-014")
    assert "message_for_customer" not in payload


def test_customer_text_email_is_masked_in_audit(client):
    start_to_consent(client)
    send(client)
    entry = client.get("/admin/audit", headers=headers()).json()["entries"][-1]
    assert entry["output"]["message_for_customer"] == SENT_TEXT.replace("rahul@gmail.com", "ra*****@gmail.com")
