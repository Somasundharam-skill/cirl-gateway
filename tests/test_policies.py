import json

import pytest
from fastapi.testclient import TestClient

from app import cirl_client
from app.config import load_settings
from tests.conftest import (
    API_KEY,
    PRIYA_PHONE,
    UNKNOWN_PHONE,
    audit_entries,
    call,
    make_app,
)

NOT_REGISTERED = (
    "We couldn't find an eIA linked to this WhatsApp number. Please write to cirthelpdesk@cirl.co.in for help."
)
OTP_SENT_RAMESH = (
    "To protect your details, we've sent a code to your registered mobile 73XXXXXX20 and email "
    "ra*****@example.com. Please enter it here."
)
OTP_WRONG = "That code is incorrect. Please re-enter it."
OTP_EXPIRED = "That code has expired. Ask me to show your policies again and I'll send a new one."
OTP_LOCKED = "Too many incorrect attempts. Please try again after 24 hours."
NO_POLICIES = "You don't have any policies in your eIA yet."
RAMESH_POLICIES = (
    "Here are the policies in your eIA:\n\n"
    "*LIC's Jeevan Anand*\n"
    "Policy No: 36521478\n"
    "Status: Demat\n"
    "Sum Assured: Rs 5,00,000\n"
    "Next Premium Due: 15-01-2027\n\n"
    "Policy No: 2310987654\n"
    "Status: In Process"
)
RESULT_KEYS = {"ok", "status", "message_for_customer", "message_for_agent"}
ERROR_KEYS = RESULT_KEYS | {"error_code", "error_msg"}


def list_policies(client, **kw):
    return call(client, "list_policies", **kw)


def verify(client, otp, **kw):
    return call(client, "verify_identity_otp", {"otp_no": otp}, **kw)


def assert_ok(result, payload, status, message):
    assert result["isError"] is False, payload
    assert set(payload) == RESULT_KEYS
    assert payload["ok"] is True and payload["status"] == status
    assert payload["message_for_customer"] == message
    assert payload["message_for_agent"]


def assert_error(result, payload, code, status, message):
    assert result["isError"] is True, payload
    assert set(payload) == ERROR_KEYS
    assert payload["ok"] is False and payload["error_code"] == code and payload["status"] == status
    assert payload["message_for_customer"] == message
    assert payload["error_msg"] and payload["message_for_agent"]


def session(app, cid="conv-test-1"):
    return app.state.store.get(cid)


# --- who is asking -----------------------------------------------------------------------------


@pytest.mark.parametrize("phone", [UNKNOWN_PHONE, "12345", ""])
def test_unknown_phone(client, phone):
    for result, payload in (list_policies(client, phone=phone), verify(client, "123456", phone=phone)):
        assert_error(result, payload, "GW-040", "not_registered", NOT_REGISTERED)


def test_missing_phone_header(client):
    assert_error(*list_policies(client, phone=None), "GW-040", "not_registered", NOT_REGISTERED)


@pytest.mark.parametrize("phone", ["+917373345820", "7373345820", "917373345820", "07373345820", "+91 73733-45820"])
def test_phone_formats_are_normalised(client, phone):
    assert_ok(*list_policies(client, phone=phone), "otp_sent", OTP_SENT_RAMESH)


def test_test_phone_fallback(fake_clock):
    client = TestClient(make_app(TEST_PHONE="7373345820"))
    for phone in (None, ""):  # no header, or an empty one: TEST_PHONE is used
        assert_ok(*list_policies(client, phone=phone), "otp_sent", OTP_SENT_RAMESH)
    # A real header always wins over TEST_PHONE.
    assert_error(*list_policies(client, phone=UNKNOWN_PHONE), "GW-040", "not_registered", NOT_REGISTERED)
    result, payload = verify(client, "123456", phone="")
    assert_ok(result, payload, "policies", RAMESH_POLICIES)


def test_test_phone_unset_means_no_fallback(client):
    assert_error(*list_policies(client, phone=""), "GW-040", "not_registered", NOT_REGISTERED)


def test_invalid_test_phone_is_rejected():
    with pytest.raises(ValueError, match="TEST_PHONE"):
        load_settings({"GATEWAY_API_KEY": API_KEY, "TEST_PHONE": "12345"})


def test_directory_path_from_env(fake_clock, tmp_path):
    csv = tmp_path / "staff.csv"
    csv.write_text("phone,eia_no,name\n+919000000001,1921008101111,Test\n", encoding="utf-8")
    client = TestClient(make_app(EMPLOYEE_DIRECTORY_PATH=str(csv)))
    assert list_policies(client, phone=UNKNOWN_PHONE)[0]["isError"] is False
    assert_error(*list_policies(client, conversation_id="c2"), "GW-040", "not_registered", NOT_REGISTERED)


def test_bad_directory_row_stops_startup(tmp_path):
    csv = tmp_path / "staff.csv"
    csv.write_text("phone,eia_no,name\nnot-a-phone,123,X\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        make_app(EMPLOYEE_DIRECTORY_PATH=str(csv))


# --- OTP --------------------------------------------------------------------------------------------


def test_otp_sent(client, app):
    result, payload = list_policies(client)
    assert_ok(result, payload, "otp_sent", OTP_SENT_RAMESH)
    assert "verify_identity_otp" in payload["message_for_agent"]
    s = session(app)
    assert s.eia_no == "1921008101111" and s.phone == "+917373345820" and s.channel == "whatsapp"
    assert s.otp_trxn_no.startswith("C1-") and s.identity_verified is False


def test_no_resend_while_code_valid(client, app, fake_clock, monkeypatch):
    sends = []
    real = cirl_client.send_otp
    monkeypatch.setattr(cirl_client, "send_otp", lambda *a, **kw: sends.append(1) or real(*a, **kw))

    list_policies(client)
    trxn = session(app).otp_trxn_no
    for seconds in (1, 100, 199):  # up to 300 s: still valid
        fake_clock.advance(seconds)
        assert_ok(*list_policies(client), "otp_sent", OTP_SENT_RAMESH)
    assert len(sends) == 1 and session(app).otp_trxn_no == trxn
    assert [e["event"] for e in audit_entries(client)] == ["otp_sent"] + ["otp_already_sent"] * 3

    fake_clock.advance(1)  # 301 s: expired, so a new code is sent
    list_policies(client)
    assert len(sends) == 2 and session(app).otp_trxn_no != trxn


def test_wrong_otp(client, app):
    list_policies(client)
    assert_error(*verify(client, "000000"), "E-302", "otp_incorrect", OTP_WRONG)
    assert session(app).identity_verified is False
    assert_ok(*verify(client, "123456"), "policies", RAMESH_POLICIES)


@pytest.mark.parametrize("otp", ["12345", "abcdef", "", "1234567"])
def test_malformed_otp_uses_no_attempt(client, otp):
    list_policies(client)
    for _ in range(3):
        assert_error(*verify(client, otp), "GW-004", "otp_incorrect", OTP_WRONG)
    assert_ok(*verify(client, "123456"), "policies", RAMESH_POLICIES)


def test_otp_with_spaces(client):
    list_policies(client)
    assert_ok(*verify(client, "123 456"), "policies", RAMESH_POLICIES)


def test_expired_otp(client, app, fake_clock):
    list_policies(client)
    fake_clock.advance(301)
    assert_error(*verify(client, "123456"), "E-303", "otp_expired", OTP_EXPIRED)
    # Asking again sends a fresh code, which works.
    assert_ok(*list_policies(client), "otp_sent", OTP_SENT_RAMESH)
    assert_ok(*verify(client, "123456"), "policies", RAMESH_POLICIES)


def test_valid_at_300_seconds(client, fake_clock):
    list_policies(client)
    fake_clock.advance(300)
    assert_ok(*verify(client, "123456"), "policies", RAMESH_POLICIES)


def test_locked_after_three_wrong(client, app, fake_clock):
    list_policies(client)
    assert_error(*verify(client, "000000"), "E-302", "otp_incorrect", OTP_WRONG)
    assert_error(*verify(client, "000001"), "E-302", "otp_incorrect", OTP_WRONG)
    assert_error(*verify(client, "000002"), "E-304", "otp_locked", OTP_LOCKED)
    # Locked for 24 hours: the right code and new codes are refused.
    assert_error(*verify(client, "123456"), "E-304", "otp_locked", OTP_LOCKED)
    assert_error(*list_policies(client), "E-304", "otp_locked", OTP_LOCKED)
    # The lock is CIRL's, per eIA: a new conversation is locked too.
    assert_error(*list_policies(client, conversation_id="conv-test-2"), "E-304", "otp_locked", OTP_LOCKED)

    fake_clock.advance(24 * 60 * 60)
    assert_ok(*list_policies(client, conversation_id="conv-test-3"), "otp_sent", OTP_SENT_RAMESH)
    assert_ok(*verify(client, "123456", conversation_id="conv-test-3"), "policies", RAMESH_POLICIES)


def test_send_limit(client, fake_clock):
    for _ in range(5):
        assert list_policies(client)[0]["isError"] is False
        fake_clock.advance(301)
    assert_error(
        *list_policies(client),
        "E-203",
        "otp_limit",
        "You've reached the limit for verification codes. Please try again later.",
    )


def test_verify_before_any_code(client):
    assert_error(
        *verify(client, "123456"),
        "GW-006",
        "no_otp",
        "Please ask me to show your policies first, and I'll send you a code.",
    )


# --- policy list ------------------------------------------------------------------------------------


def test_verified_list(client, app):
    list_policies(client)
    result, payload = verify(client, "123456")
    assert_ok(result, payload, "policies", RAMESH_POLICIES)
    assert session(app).identity_verified is True
    # Verified for the rest of the conversation: no new code, the list straight away.
    assert_ok(*list_policies(client), "policies", RAMESH_POLICIES)
    assert_ok(*verify(client, "999999"), "policies", RAMESH_POLICIES)
    assert [e["event"] for e in audit_entries(client)] == [
        "otp_sent",
        "identity_verified",
        "policies_listed",
        "policies_listed",
    ]


def test_empty_list(client):
    assert_ok(
        *list_policies(client, phone=PRIYA_PHONE),
        "otp_sent",
        "To protect your details, we've sent a code to your registered mobile 98XXXXXX20 and email "
        "pr*****@example.com. Please enter it here.",
    )
    assert_ok(*verify(client, "123456", phone=PRIYA_PHONE), "no_policies", NO_POLICIES)


def test_verification_is_per_conversation(client):
    list_policies(client)
    verify(client, "123456")
    assert_ok(*list_policies(client, conversation_id="conv-test-2"), "otp_sent", OTP_SENT_RAMESH)


def test_different_number_on_same_conversation_must_verify(client, app):
    list_policies(client)
    verify(client, "123456")
    assert_ok(*list_policies(client, phone=PRIYA_PHONE), "otp_sent", OTP_SENT_RAMESH.replace(
        "73XXXXXX20", "98XXXXXX20").replace("ra*****", "pr*****"))
    assert session(app).identity_verified is False


def test_session_expires(client, fake_clock):
    list_policies(client)
    verify(client, "123456")
    fake_clock.advance(61 * 60)
    assert_ok(*list_policies(client), "otp_sent", OTP_SENT_RAMESH)


def test_policy_formatting(client, monkeypatch):
    policies = [
        {"policy_no": "P1", "policy_name": "", "insurer_name": "Star Health", "policy_status": "Demat",
         "sum_assured": "12500000.00", "next_premium_due_date": ""},
        {"policy_no": "P2", "policy_name": "Term Plan", "insurer_name": "X", "policy_status": "Demat",
         "sum_assured": "", "next_premium_due_date": "01-02-2027"},
        {"policy_no": "P3", "policy_name": "Hidden", "insurer_name": "Y", "policy_status": "In Process",
         "sum_assured": "100000", "next_premium_due_date": "01-01-2027"},
    ]
    monkeypatch.setitem(cirl_client.MOCK_POLICIES, "1921008101111", policies)
    list_policies(client)
    _, payload = verify(client, "123456")
    assert payload["message_for_customer"] == (
        "Here are the policies in your eIA:\n\n"
        "*Star Health*\nPolicy No: P1\nStatus: Demat\nSum Assured: Rs 1,25,00,000\n\n"
        "*Term Plan*\nPolicy No: P2\nStatus: Demat\nNext Premium Due: 01-02-2027\n\n"
        "Policy No: P3\nStatus: In Process"
    )


@pytest.mark.parametrize(
    "flag, tool",
    [("CIRL_MODE_SEND_OTP", "list"), ("CIRL_MODE_VERIFY_OTP", "verify"), ("CIRL_MODE_GET_LIST_OF_POLICY", "verify")],
)
def test_real_mode_fails_safely(fake_clock, flag, tool):
    client = TestClient(make_app(**{flag: "real"}))
    result, payload = list_policies(client)
    if tool == "verify":
        result, payload = verify(client, "123456")
    assert result["isError"] is True and payload["error_code"] == "GW-501" and payload["status"] == "unavailable"


def test_old_flags_and_tools_are_gone(client):
    from tests.conftest import rpc

    for name in ("journey_turn", "start_journey", "send_email_otp", "create_password_link", "start_kyc"):
        assert rpc(client, "tools/call", {"name": name, "arguments": {}}).json()["error"]["code"] == -32602
    assert client.get("/set-password?t=x").status_code == 404


# --- audit -------------------------------------------------------------------------------------------


def test_audit_is_masked(client, captured_logs):
    list_policies(client)
    verify(client, "000000")
    verify(client, "123456")
    list_policies(client)

    entries = audit_entries(client)
    assert entries[1]["input"] == {"otp_no": "******"}
    listed = entries[2]["output"]["message_for_customer"]
    assert "Policy No: XXXX1478" in listed and "Policy No: XXXXXX7654" in listed

    text = json.dumps(entries) + "\n".join(captured_logs)
    for secret in ("123456", "000000", "36521478", "2310987654", "1921008101111", "7373345820",
                   "ramesh.kumar@example.com", "Ramesh"):
        assert secret not in text, secret
    # The customer still gets the full numbers.
    assert "Policy No: 36521478" in verify(client, "123456")[1]["message_for_customer"]


def test_audit_entry_fields(client):
    list_policies(client)
    entry = audit_entries(client)[0]
    assert set(entry) == {"timestamp", "conversation_id", "tool", "event", "input", "output", "ok", "error_code"}
    assert entry["tool"] == "list_policies" and entry["ok"] is True and entry["error_code"] is None
