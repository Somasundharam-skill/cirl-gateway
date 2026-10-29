import json

import pytest

from tests.conftest import call, headers
from tests.test_password import GOOD_PASSWORD, app, client, new_link, session, submit, verified  # noqa: F401
from tests.test_privacy import assert_clean

NOT_AVAILABLE = "Not available yet. Continue with the current step."
KYC_STARTED_TEXT = (
    "Please select your preferred document for immediate validation:\nReply 1 for Aadhaar / UID or 2 for PAN."
)
KYC_DEFERRED_TEXT = "No problem. You can complete your KYC any time by sending Hi. Befikar Raho!"


def password_confirmed(client):
    verified(client)
    token, _ = new_link(client)
    submit(client, token)
    result, _ = call(client, "check_password_status")
    assert result["isError"] is False


def assert_too_early(result, payload):
    assert result["isError"] is True
    assert payload["ok"] is False and payload["error_code"] == "GW-030"
    assert payload["message_for_agent"] == NOT_AVAILABLE
    assert "message_for_customer" not in payload


@pytest.mark.parametrize("tool", ["start_kyc", "defer_kyc"])
def test_kyc_needs_password_confirmed(client, tool):
    assert_too_early(*call(client, tool))  # no session
    verified(client)
    assert_too_early(*call(client, tool))  # email verified, no password
    token, _ = new_link(client)
    assert_too_early(*call(client, tool))  # link sent
    call(client, "check_password_status")  # GW-021: not set yet
    assert_too_early(*call(client, tool))
    submit(client, token)
    assert_too_early(*call(client, tool))  # set on the page, but check_password_status not yet succeeded
    call(client, "check_password_status")
    result, _ = call(client, tool)
    assert result["isError"] is False


def test_start_kyc(client, app):
    password_confirmed(client)
    result, payload = call(client, "start_kyc")
    assert result["isError"] is False
    assert payload["ok"] is True and payload["kyc_choice"] == "now" and payload["kyc_choice_at"]
    assert payload["message_for_customer"] == KYC_STARTED_TEXT
    s = session(app)
    assert s.kyc_choice == "now" and s.kyc_choice_at == payload["kyc_choice_at"]


def test_start_kyc_is_idempotent(client, app, fake_clock):
    password_confirmed(client)
    first = call(client, "start_kyc")
    fake_clock.advance(60)
    second = call(client, "start_kyc")
    assert second == first  # same result, same timestamp
    assert session(app).kyc_choice_at == first[1]["kyc_choice_at"]


def test_defer_kyc(client, app):
    password_confirmed(client)
    result, payload = call(client, "defer_kyc")
    assert result["isError"] is False
    assert payload["ok"] is True and payload["kyc_choice"] == "later" and payload["kyc_choice_at"]
    assert payload["message_for_customer"] == KYC_DEFERRED_TEXT
    assert session(app).kyc_choice == "later"


def test_defer_then_start_kyc(client, app, fake_clock):
    password_confirmed(client)
    _, deferred = call(client, "defer_kyc")
    fake_clock.advance(60)
    result, started = call(client, "start_kyc")  # the customer changed their mind
    assert result["isError"] is False
    assert started["kyc_choice"] == "now" and started["message_for_customer"] == KYC_STARTED_TEXT
    assert started["kyc_choice_at"] != deferred["kyc_choice_at"]
    s = session(app)
    assert s.kyc_choice == "now" and s.kyc_choice_at == started["kyc_choice_at"]


def test_kyc_audit_has_no_personal_data(client, captured_logs):
    password_confirmed(client)
    call(client, "defer_kyc")
    call(client, "start_kyc")
    call(client, "start_kyc")
    entries = client.get("/admin/audit", headers=headers()).json()["entries"]
    kyc = [e for e in entries if e["tool"] in ("start_kyc", "defer_kyc")]
    assert [e["event"] for e in kyc] == ["kyc_deferred", "kyc_started", "kyc_started"]
    for entry in kyc:
        assert entry["input"] == {}
        assert set(entry["output"]) == {"ok", "kyc_choice", "kyc_choice_at", "message_for_agent", "message_for_customer"}
        text = json.dumps(entry)
        assert_clean(text)
        for personal in ("rahul", "@", "9876", GOOD_PASSWORD):
            assert personal not in text, personal
    assert GOOD_PASSWORD not in "\n".join(captured_logs)
