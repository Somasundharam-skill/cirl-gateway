import pytest

from app.config import load_settings
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import API_KEY, call, start_to_consent, start_to_otp


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


def test_start_journey_resets(client, app):
    start_to_otp(client)
    assert app.state.store.get("conv-test-1").state == "OTP_SENT"
    call(client, "start_journey")
    session = app.state.store.get("conv-test-1")
    assert session.state == "STARTED" and session.consent_id is None and session.client_trxn_id is None
    result, payload = call(client, "send_otp")
    assert_tool_error(result, payload, "GW-002")


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


def test_real_mode_fails_safely(fake_clock):
    app = create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "CIRL_MODE_CHECK_EXISTING_EIA": "real"}))
    client = TestClient(app)
    start_to_consent(client)
    result, payload = call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "rahul@gmail.com"})
    assert_tool_error(result, payload, "GW-501")
