import json

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from tests.conftest import ALL_TOOLS, API_KEY, call, headers, start_to_consent
from tests.test_privacy import assert_clean

EMAIL = "rahul@gmail.com"
NOT_AVAILABLE = "Not available yet. Continue with the current step."


def assert_tool_error(result, payload, code, message_for_agent=None):
    assert result["isError"] is True
    assert payload["ok"] is False and payload["error_code"] == code and payload["error_msg"]
    if message_for_agent is not None:
        assert payload["message_for_agent"] == message_for_agent


def send(client, email=EMAIL):
    return call(client, "send_email_otp", {"email_id": email})


def resend(client, args=None):
    """The customer asked for a new code: always a fresh send."""
    return call(client, "send_email_otp", {"resend": True} if args is None else args)


def verify(client, otp):
    return call(client, "verify_email_otp", {"otp_no": otp})


# --- send_email_otp --------------------------------------------------------------------


def test_send_requires_consent(client):
    result, payload = send(client)  # no session at all
    assert_tool_error(result, payload, "GW-002", NOT_AVAILABLE)
    call(client, "start_journey")
    result, payload = send(client)
    assert_tool_error(result, payload, "GW-002", NOT_AVAILABLE)


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
    send(client)
    for _ in range(4):
        result, _ = resend(client)
        assert result["isError"] is False
    result, payload = resend(client)
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
    app = create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "MOCK_EMAIL_OTP": "654321", "ENABLED_TOOLS": ALL_TOOLS}))
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
    app = create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "CIRL_MODE_SEND_EMAIL_OTP": "real", "ENABLED_TOOLS": ALL_TOOLS}))
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
VERIFIED_TEXT = "Email verified!"
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
    send(client)
    for _ in range(4):
        resend(client)
    result, payload = resend(client)
    assert_tool_error(result, payload, "GW-010")
    assert payload["message_for_customer"] == SEND_LIMIT_TEXT


def test_resend_uses_stored_email(client, app):
    start_to_consent(client)
    send(client)
    # resend=true uses the stored email, even if another one is passed.
    for args in ({"resend": True}, {"resend": True, "email_id": ""}, {"resend": True, "email_id": "x@y.com"}):
        result, payload = resend(client, args)
        assert result["isError"] is False
        assert payload["email_id"] == EMAIL
        assert payload["message_for_customer"] == SENT_TEXT
    assert app.state.store.get("conv-test-1").email_otp_send_count == 4


def test_send_without_email_after_expiry_reuses_stored_email(client, app, fake_clock):
    start_to_consent(client)
    send(client)
    fake_clock.advance(121)
    for args in ({}, {"email_id": ""}):
        result, payload = call(client, "send_email_otp", args)
        assert result["isError"] is False and payload["email_id"] == EMAIL
    assert app.state.store.get("conv-test-1").email_otp_send_count == 2  # second {} is a duplicate


def test_new_email_replaces_stored_email(client, app):
    start_to_consent(client)
    send(client)
    _, payload = send(client, "priya@example.com")
    assert payload["email_id"] == "priya@example.com"
    _, payload = resend(client)
    assert payload["email_id"] == "priya@example.com"
    assert app.state.store.get("conv-test-1").email_otp_send_count == 3


def test_first_resend_without_stored_email_uses_given_email(client):
    start_to_consent(client)
    result, payload = call(client, "send_email_otp", {"resend": True, "email_id": EMAIL})
    assert result["isError"] is False and payload["email_id"] == EMAIL


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_resend_must_be_boolean(client, value):
    start_to_consent(client)
    result, payload = call(client, "send_email_otp", {"email_id": EMAIL, "resend": value})
    assert_tool_error(result, payload, "GW-004")


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


# --- called too early: neutral agent text, no customer text -----------------------------


def test_preconditions_have_no_customer_text(client):
    def check(tool, args, code):
        result, payload = call(client, tool, args)
        assert_tool_error(result, payload, code, NOT_AVAILABLE)
        assert "message_for_customer" not in payload, tool

    # No session yet
    check("send_email_otp", {"email_id": EMAIL}, "GW-002")
    check("create_password_link", {}, "GW-020")
    # Journey started, no consent
    call(client, "start_journey")
    check("send_email_otp", {"email_id": EMAIL}, "GW-002")
    check("verify_email_otp", {"otp_no": "123456"}, "GW-002")
    check("check_existing_eia", {"pan": "ABCPE1234E", "email_id": EMAIL}, "GW-002")
    check("create_password_link", {}, "GW-020")
    check("check_password_status", {}, "GW-023")
    # Consent recorded
    call(client, "record_consent", {"consent": True, "notice_version": "v1.0"})
    check("verify_email_otp", {"otp_no": "123456"}, "GW-014")
    check("send_otp", {}, "GW-005")
    check("create_password_link", {}, "GW-020")
    # Code sent, not verified yet
    send(client)
    check("create_password_link", {}, "GW-020")
    # eIA checked, no OTP sent
    call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": EMAIL})
    check("verify_otp", {"otp_no": "123456"}, "GW-006")


# --- duplicate sends while a code is still valid ------------------------------------------


def audit_events(client):
    return [e["event"] for e in client.get("/admin/audit", headers=headers()).json()["entries"]]


def test_duplicate_while_code_valid_is_ignored(client, app, fake_clock):
    start_to_consent(client)
    first_result, first = send(client)
    session = app.state.store.get("conv-test-1")

    for seconds in (1, 60, 58):  # up to 119 s: still valid
        fake_clock.advance(seconds)
        for args in ({"email_id": EMAIL}, {}, {"email_id": EMAIL.upper()}, {"resend": False}):
            result, payload = call(client, "send_email_otp", args)
            assert result == first_result, args
            assert payload["ok"] is True and payload["message_for_customer"] == SENT_TEXT

    after = app.state.store.get("conv-test-1")
    assert after.email_otp_send_count == 1  # not counted
    assert after.email_otp_sent_at == session.email_otp_sent_at  # no new code
    assert after.email_otp_expires_at == session.email_otp_expires_at
    events = audit_events(client)
    assert events.count("email_otp_sent") == 1
    assert events[-12:] == ["email_otp_duplicate_ignored"] * 12


def test_duplicate_at_120s_is_ignored_and_after_expiry_sends(client, app, fake_clock):
    start_to_consent(client)
    send(client)
    fake_clock.advance(120)  # still valid (verify accepts at 120 s)
    send(client)
    assert audit_events(client)[-1] == "email_otp_duplicate_ignored"
    fake_clock.advance(1)  # expired
    send(client)
    assert audit_events(client)[-1] == "email_otp_sent"
    assert app.state.store.get("conv-test-1").email_otp_send_count == 2


def test_duplicate_is_not_sent_to_cirl(client, monkeypatch):
    from app import cirl_client

    calls = []
    real = cirl_client.send_email_otp
    monkeypatch.setattr(cirl_client, "send_email_otp", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    start_to_consent(client)
    send(client)
    send(client)
    assert len(calls) == 1
    resend(client)
    assert len(calls) == 2


def test_resend_true_always_sends_and_counts(client, app):
    start_to_consent(client)
    send(client)
    result, payload = resend(client)  # code still valid, but the customer asked for a new one
    assert result["isError"] is False and payload["message_for_customer"] == SENT_TEXT
    assert audit_events(client)[-1] == "email_otp_sent"
    assert app.state.store.get("conv-test-1").email_otp_send_count == 2


def test_different_email_while_code_valid_is_a_new_send(client, app):
    start_to_consent(client)
    send(client)
    _, payload = send(client, "priya@example.com")
    assert payload["email_id"] == "priya@example.com"
    assert app.state.store.get("conv-test-1").email_otp_send_count == 2
    assert audit_events(client)[-1] == "email_otp_sent"


def test_duplicates_do_not_use_up_the_send_limit(client, app):
    start_to_consent(client)
    for _ in range(5):
        resend(client)
        result, _ = send(client)  # repeat while valid: ignored
        assert result["isError"] is False
    assert app.state.store.get("conv-test-1").email_otp_send_count == 5
    result, _ = send(client)  # still a duplicate, so not refused
    assert result["isError"] is False
    assert_tool_error(*resend(client), "GW-010")


def test_duplicate_keeps_attempt_count_and_code(client, app):
    # An ignored duplicate resets nothing: the same code still verifies.
    start_to_consent(client)
    send(client)
    assert_tool_error(*verify(client, "000000"), "GW-011")
    send(client)
    assert app.state.store.get("conv-test-1").email_otp_failed_attempts == 1
    result, _ = verify(client, "123456")
    assert result["isError"] is False


# --- email shown as entered, masked in the audit -------------------------------------------


def test_customer_text_shows_email_as_entered_and_audit_masks_it(client):
    start_to_consent(client)
    typed = "Rahul.Sharma@Gmail.com"
    _, payload = call(client, "send_email_otp", {"email_id": f"  {typed} "})
    assert payload["email_id"] == typed
    assert payload["message_for_customer"] == SENT_TEXT.replace(EMAIL, typed)
    _, dup = call(client, "send_email_otp", {"email_id": typed.lower()})  # duplicate keeps the first spelling
    assert dup["message_for_customer"] == payload["message_for_customer"]

    entries = client.get("/admin/audit", headers=headers()).json()["entries"][-2:]
    for entry in entries:
        text = json.dumps(entry)
        assert typed not in text and typed.lower() not in text
        assert "Ra*****@Gmail.com" in entry["output"]["message_for_customer"]
