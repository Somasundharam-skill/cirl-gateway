import json
import re

from app.masking import mask_email, mask_pan, mask_phone, mask_uid
from tests.conftest import API_KEY, call, headers, start_to_consent

FULL_PANS = ["ABCPE1234E", "ZZZPZ9999Z"]
UID = "123456789012"
TWELVE_DIGITS = re.compile(r"(?<!\d)\d{12}(?!\d)")
# Random identifiers (trxn numbers, consent ids) may contain "123456" by chance; drop them before checking.
RANDOM_IDS = re.compile(r"C1-\d{17}|CNS-[0-9A-F]{16}")


def run_full_journey(client):
    start_to_consent(client)
    call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "rahul@gmail.com"})
    call(client, "check_existing_eia", {"pan": "zzzpz9999z", "email_id": "rahul@gmail.com", "mobile_no": "9876545820"})
    call(client, "check_existing_eia", {"uid": UID, "email_id": "rahul@gmail.com"})
    call(client, "check_existing_eia", {"pan": "ABCPE1234E", "email_id": "rahul@gmail.com"})
    call(client, "send_otp")
    call(client, "verify_otp", {"otp_no": "654321"})
    call(client, "verify_otp", {"otp_no": "123456"})
    # Sensitive values in failing calls and unexpected argument names must be masked too.
    call(client, "check_existing_eia", {"pan": "ABCPE1234E", "uid": UID, "email_id": "rahul@gmail.com"})
    call(client, "verify_otp", {"otp_no": "123456", "note": f"pan ABCPE1234E uid {UID}"})


def assert_clean(text: str):
    for pan in FULL_PANS:
        assert pan not in text.upper(), text
    assert not TWELVE_DIGITS.search(text), text
    assert "123456" not in RANDOM_IDS.sub("", text), text


def test_no_sensitive_values_in_audit_or_logs(client, app, captured_logs):
    run_full_journey(client)

    entries = client.get("/admin/audit", headers=headers()).json()["entries"]
    assert len(entries) == 11
    for entry in entries:
        assert_clean(json.dumps(entry))

    assert captured_logs, "expected audit log lines"
    for line in captured_logs:
        assert_clean(line)
        assert API_KEY not in line


def test_session_does_not_hold_pan_uid_or_otp(client, app):
    run_full_journey(client)
    # The session legitimately holds the +91 phone (12 digits), so check the actual sensitive values.
    stored = RANDOM_IDS.sub("", repr(app.state.store.get("conv-test-1"))).upper()
    for value in [*FULL_PANS, UID, "123456"]:
        assert value not in stored, stored


def test_audit_entry_fields(client):
    call(client, "start_journey")
    entry = client.get("/admin/audit", headers=headers()).json()["entries"][-1]
    assert entry["event"] == "journey_started"
    assert entry["conversation_id"] == "conv-test-1"
    assert entry["tool"] == "start_journey"
    assert entry["ok"] is True and entry["error_code"] is None
    assert entry["output"]["phone"] == "+91XXXXXX5820"
    assert {"timestamp", "input", "output"} <= entry.keys()


def test_masking_formats():
    assert mask_pan("ABCPE1234F") == "ABCXXXX34F"
    assert mask_uid(UID) == "XXXXXXXX9012"
    assert mask_phone("+919876545820") == "+91XXXXXX5820"
    assert mask_email("rahul@gmail.com") == "ra*****@gmail.com"
