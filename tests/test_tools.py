import pytest

from app.config import load_settings
from app.main import create_app
from app.tools import TOOLS
from fastapi.testclient import TestClient
from tests.conftest import ALL_TOOLS, API_KEY, call, headers, start_to_consent, start_to_otp


def assert_tool_error(result, payload, code):
    assert result["isError"] is True
    assert payload["ok"] is False
    assert payload["error_code"] == code
    assert payload["error_msg"] and payload["message_for_agent"]


# --- start_journey / consent -----------------------------------------------------------


def test_start_journey_normalises_phone(client):
    result, payload = call(client, "start_journey")
    assert result["isError"] is False
    assert payload["ok"] is True and payload["state"] == "STARTED"
    assert payload["phone"] == "+91XXXXXX5820"
    assert payload["channel"] == "whatsapp"


def test_start_journey_twice_keeps_consent(client, app):
    start_to_consent(client)
    consent_id = app.state.store.get("conv-test-1").consent_id
    result, payload = call(client, "start_journey")
    assert result["isError"] is False and payload["ok"] is True
    assert payload["state"] == "CONSENTED"
    session = app.state.store.get("conv-test-1")
    assert session.consent_id == consent_id and session.state == "CONSENTED"


def test_start_journey_keeps_progress(client, app):
    start_to_otp(client)
    result, payload = call(client, "start_journey")
    assert payload["ok"] is True and payload["state"] == "OTP_SENT"
    result, _ = call(client, "verify_otp", {"otp_no": "123456"})
    assert result["isError"] is False


def test_new_conversation_starts_fresh(client, app):
    start_to_consent(client)
    result, payload = call(client, "start_journey", conversation_id="conv-test-2")
    assert payload["state"] == "STARTED"
    assert app.state.store.get("conv-test-2").consent_id is None
    assert app.state.store.get("conv-test-1").consent_id is not None


def test_start_journey_audit_events(client):
    call(client, "start_journey")
    call(client, "start_journey")
    events = [e["event"] for e in client.get("/admin/audit", headers=headers()).json()["entries"]]
    assert events == ["journey_started", "journey_already_started"]


def test_sessions_are_per_conversation(client):
    start_to_consent(client)
    result, payload = call(client, "send_otp", conversation_id="other-conv")
    assert_tool_error(result, payload, "GW-001")


def test_session_expires_after_ttl(client, fake_clock):
    start_to_consent(client)
    fake_clock.advance(61 * 60)
    result, payload = call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "a@b.com"})
    assert_tool_error(result, payload, "GW-001")


def test_consent_recorded(client, app):
    call(client, "start_journey")
    result, payload = call(client, "record_consent", {"consent": True, "notice_version": "v1.0"})
    assert result["isError"] is False
    assert payload["consent_id"].startswith("CNS-")
    session = app.state.store.get("conv-test-1")
    assert session.notice_version == "v1.0" and session.consent_at


def test_record_consent_twice_returns_same_consent(client, app, fake_clock):
    call(client, "start_journey")
    _, first = call(client, "record_consent", {"consent": True, "notice_version": "v1.0"})
    fake_clock.advance(30)
    result, second = call(client, "record_consent", {"consent": True, "notice_version": "v2.0"})
    assert result["isError"] is False and second["ok"] is True
    assert second["consent_id"] == first["consent_id"]
    assert second["consent_at"] == first["consent_at"]
    assert second["notice_version"] == "v1.0"
    assert app.state.store.get("conv-test-1").notice_version == "v1.0"


def test_consent_creates_session_if_missing(client):
    result, payload = call(client, "record_consent", {"consent": True, "notice_version": "v1.0"})
    assert result["isError"] is False


def test_consent_refused_is_error(client):
    call(client, "start_journey")
    result, payload = call(client, "record_consent", {"consent": False, "notice_version": "v1.0"})
    assert_tool_error(result, payload, "GW-003")
    result, payload = call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "a@b.com"})
    assert_tool_error(result, payload, "GW-002")


@pytest.mark.parametrize("args", [{"consent": "yes", "notice_version": "v1"}, {"consent": True}])
def test_consent_validation(client, args):
    call(client, "start_journey")
    result, payload = call(client, "record_consent", args)
    assert_tool_error(result, payload, "GW-004")


# --- check_existing_eia ----------------------------------------------------------------


def test_eia_requires_consent(client):
    call(client, "start_journey")
    result, payload = call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "a@b.com"})
    assert_tool_error(result, payload, "GW-002")


@pytest.mark.parametrize(
    "args",
    [
        {"pan": "ABCPE1234", "email_id": "rahul@gmail.com"},
        {"pan": "12345ABCDE", "email_id": "rahul@gmail.com"},
        {"uid": "1234", "email_id": "rahul@gmail.com"},
        {"pan": "ABCPE1234E", "uid": "123412341234", "email_id": "rahul@gmail.com"},
        {"email_id": "rahul@gmail.com"},
        {"pan": "ABCPE1234E", "email_id": "not-an-email"},
        {"pan": "ABCPE1234E"},
        {"pan": "ABCPE1234E", "email_id": "rahul@gmail.com", "mobile_no": "12345"},
    ],
)
def test_eia_validation_errors(client, args):
    start_to_consent(client)
    result, payload = call(client, "check_existing_eia", args)
    assert_tool_error(result, payload, "GW-004")


def test_eia_needs_mobile_when_phone_header_missing(client):
    start_to_consent(client, phone=None)
    result, payload = call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "a@b.com"}, phone=None)
    assert_tool_error(result, payload, "GW-004")
    result, payload = call(
        client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "a@b.com", "mobile_no": "9876545820"}, phone=None
    )
    assert result["isError"] is False


def test_known_pan(client, app):
    start_to_consent(client)
    result, payload = call(client, "check_existing_eia", {"pan": "abcpe1234e", "email_id": "rahul@gmail.com"})
    assert result["isError"] is False
    assert payload["ok"] is True and payload["eia_exists"] is True
    cirl = payload["cirl_response"]
    assert cirl["status"] == "true" and cirl["eia_no"] == "2345678904567"
    assert cirl["trxn_no"].startswith("C1-") and len(cirl["trxn_no"]) == 20 and cirl["trxn_no"][3:].isdigit()
    session = app.state.store.get("conv-test-1")
    assert session.client_trxn_id == cirl["trxn_no"]
    assert session.mobile_no == "9876545820"
    assert "ABCPE1234E" not in repr(session)


def test_unknown_pan_is_not_an_error(client):
    start_to_consent(client)
    result, payload = call(client, "check_existing_eia", {"pan": "ZZZPZ9999Z", "email_id": "rahul@gmail.com"})
    assert result["isError"] is False
    assert payload["ok"] is True and payload["eia_exists"] is False
    cirl = payload["cirl_response"]
    assert cirl["status"] == "false"
    assert cirl["error_code"] == "E-102" and cirl["error_msg"] == "eIA Account does not exist."


def test_uid_path(client, app):
    start_to_consent(client)
    result, payload = call(client, "check_existing_eia", {"uid": "1234 5678 9012", "email_id": "rahul@gmail.com"})
    assert result["isError"] is False and payload["eia_exists"] is False
    assert "123456789012" not in repr(app.state.store.get("conv-test-1"))


# --- OTP -------------------------------------------------------------------------------


def test_send_otp_requires_eia_check(client):
    start_to_consent(client)
    result, payload = call(client, "send_otp")
    assert_tool_error(result, payload, "GW-005")


def test_send_otp_shape(client):
    result, payload = start_to_otp(client)
    assert result["isError"] is False
    cirl = payload["cirl_response"]
    assert cirl["masked_email_id"] == "ra*****@gmail.com"
    assert cirl["masked_mobile_no"] == "98XXXXXX20"
    assert cirl["otp_expire_time"] == 300
    assert cirl["otp_start_date"] and cirl["otp_end_date"]
    assert cirl["trxn_no"].startswith("C1-")
    assert payload["sends_remaining"] == 4


def test_otp_success(client, app):
    start_to_otp(client)
    result, payload = call(client, "verify_otp", {"otp_no": "123456"})
    assert result["isError"] is False
    assert payload["ok"] is True and payload["otp_verified"] is True
    session = app.state.store.get("conv-test-1")
    assert session.otp_verified is True and session.state == "OTP_VERIFIED"


def test_verify_otp_requires_send(client):
    start_to_consent(client)
    call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "rahul@gmail.com"})
    result, payload = call(client, "verify_otp", {"otp_no": "123456"})
    assert_tool_error(result, payload, "GW-006")


def test_wrong_otp_three_times_locks(client):
    start_to_otp(client)
    result, payload = call(client, "verify_otp", {"otp_no": "000000"})
    assert_tool_error(result, payload, "E-302")
    assert payload["attempts_remaining"] == 2
    result, payload = call(client, "verify_otp", {"otp_no": "000001"})
    assert_tool_error(result, payload, "E-302")
    result, payload = call(client, "verify_otp", {"otp_no": "000002"})
    assert_tool_error(result, payload, "E-304")
    # Even the right OTP is refused once locked.
    result, payload = call(client, "verify_otp", {"otp_no": "123456"})
    assert_tool_error(result, payload, "E-304")
    # A fresh OTP unlocks.
    call(client, "send_otp")
    result, payload = call(client, "verify_otp", {"otp_no": "123456"})
    assert result["isError"] is False


def test_expired_otp(client, fake_clock):
    start_to_otp(client)
    fake_clock.advance(301)
    result, payload = call(client, "verify_otp", {"otp_no": "123456"})
    assert_tool_error(result, payload, "E-303")


def test_otp_valid_just_before_expiry(client, fake_clock):
    start_to_otp(client)
    fake_clock.advance(299)
    result, _ = call(client, "verify_otp", {"otp_no": "123456"})
    assert result["isError"] is False


def test_otp_send_limit(client):
    start_to_otp(client)  # send 1
    for _ in range(4):  # sends 2-5
        result, _ = call(client, "send_otp")
        assert result["isError"] is False
    result, payload = call(client, "send_otp")
    assert_tool_error(result, payload, "E-203")


def test_malformed_otp_is_validation_error(client):
    start_to_otp(client)
    result, payload = call(client, "verify_otp", {"otp_no": "12ab"})
    assert_tool_error(result, payload, "GW-004")


# --- real mode -------------------------------------------------------------------------


TOOL_NAMES = list(TOOLS)


def test_messages_name_no_tools(client, app, fake_clock):
    """Perfox Steps decide the journey order, so agent messages never point at other tools."""
    messages = []

    def run(tool, args=None, **kw):
        result, payload = call(client, tool, args, **kw)
        messages.append((tool, result["isError"], payload["message_for_agent"]))
        return payload

    # Failures
    run("check_existing_eia", {"pan": "ABCPE1234E", "email_id": "a@b.com"})  # GW-001
    run("start_journey")
    run("check_existing_eia", {"pan": "ABCPE1234E", "email_id": "a@b.com"})  # GW-002
    run("record_consent", {"consent": True})  # GW-004
    run("record_consent", {"consent": False, "notice_version": "v1.0"})  # GW-003
    run("record_consent", {"consent": True, "notice_version": "v1.0"})
    run("send_otp")  # GW-005
    run("verify_email_otp", {"otp_no": "123456"})  # GW-014
    run("check_existing_eia", {"pan": "ABCPE1234E", "email_id": "rahul@gmail.com"})
    run("verify_otp", {"otp_no": "123456"})  # GW-006
    run("send_otp")
    for otp in ("000000", "000001", "000002", "123456"):  # E-302, E-304
        run("verify_otp", {"otp_no": otp})
    run("send_otp")
    fake_clock.advance(301)
    run("verify_otp", {"otp_no": "123456"})  # E-303
    # Successes, including idempotent repeats
    run("start_journey")
    run("record_consent", {"consent": True, "notice_version": "v1.0"})
    run("check_existing_eia", {"pan": "ZZZPZ9999Z", "email_id": "rahul@gmail.com"})
    run("send_otp")
    run("verify_otp", {"otp_no": "123456"})
    run("verify_otp", {"otp_no": "123456"})
    run("create_password_link")  # GW-020
    run("send_email_otp", {"email_id": "rahul@gmail.com"})
    run("verify_email_otp", {"otp_no": "123456"})
    run("check_password_status")  # GW-023
    link = run("create_password_link")
    run("check_password_status")  # GW-021
    token = link["url"].split("t=", 1)[1]
    client.post("/set-password", data={"t": token, "password": "Str0ng!pass", "confirm": "Str0ng!pass"})
    run("check_password_status")
    run("create_password_link")
    fake_clock.advance(600)
    run("check_password_status")  # password already set: still ok

    successes = [m for m in messages if not m[1]]
    assert {m[0] for m in successes} == set(TOOL_NAMES)
    for tool, is_error, message in messages:
        for name in TOOL_NAMES:
            assert name not in message, (tool, is_error, message)

    assert dict((t, m) for t, e, m in successes if t in ("start_journey", "record_consent")) == {
        "start_journey": "Journey started.",
        "record_consent": "Consent recorded.",
    }


def test_real_mode_fails_safely(fake_clock):
    app = create_app(load_settings(
        {"GATEWAY_API_KEY": API_KEY, "CIRL_MODE_CHECK_EXISTING_EIA": "real", "ENABLED_TOOLS": ALL_TOOLS}
    ))
    client = TestClient(app)
    start_to_consent(client)
    result, payload = call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "rahul@gmail.com"})
    assert_tool_error(result, payload, "GW-501")
