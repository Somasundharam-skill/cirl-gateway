import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import create_app
from tests.conftest import API_KEY, call, headers, rpc

CID = "conv-test-1"
EMAIL = "rahul@gmail.com"
PASSWORD = "Str0ng!pass"

WELCOME = (
    "Welcome to CENTRICO Insurance Repository Limited (CIRL)! Let's set up your secure Electronic Insurance "
    "Account (eIA) in minutes.\n\nBefore we begin: CIRL will use the details you share here only to open and "
    "manage your eIA, as described in CIRL's privacy notice: [link]\n\nDo you agree? Reply YES to continue or "
    "NO to stop."
)
CONSENT_REPROMPT = "Please reply YES to continue or NO to stop."
DECLINED = (
    "No problem. We can't open an eIA without your consent. If you change your mind, just reply YES. "
    "For help, write to cirthelpdesk@cirl.co.in"
)
ASK_EMAIL = "To begin, please enter your valid Email ID:"
INVALID_EMAIL = "That doesn't look like a valid email address. Please enter your valid Email ID:"
OTP_SENT = (
    "Great! We've sent an Email Verification Code to {email}. "
    "Please enter the 6-digit OTP below (Valid for 120 seconds):"
)
OTP_RESENT = (
    "Great! We've sent a new Email Verification Code to {email}. "
    "Please enter the 6-digit OTP below (Valid for 120 seconds):"
)
OTP_WRONG = "That code is incorrect. Please re-enter the 6-digit OTP:"
OTP_EXPIRED = "That code has expired. Reply RESEND and I'll send you a new one."
OTP_LOCKED = "Too many incorrect attempts. Please start again later."
OTP_REPROMPT = "Please enter the 6-digit OTP sent to {email}, or reply RESEND for a new code."
LINK_BODY = (
    "{url}\n\n*Criteria:*\n- At least 8 characters\n- Mixed case & 1 special character\n\nOnce done, reply DONE here."
)
FIRST_LINK = (
    "Email verified! Let's establish your portal access.\n\n"
    "For your security, please set your password on this secure page (link valid for 10 minutes):\n" + LINK_BODY
)
NEW_LINK = "Here's your new secure link (valid for 10 minutes):\n" + LINK_BODY
KYC_QUESTION = (
    "Account access generated successfully! Do you want to complete your pending KYC process now to activate "
    "your repository benefits?\nReply 1 for Complete KYC Now or 2 for Later."
)
PASSWORD_SET = "Your password has been set successfully.\n\n" + KYC_QUESTION
PASSWORD_PENDING = "We haven't received your password yet. Please set it using the secure link, then reply DONE."
LINK_EXPIRED = "Your secure link has expired. Reply NEW LINK and I'll send you a fresh one."
PASSWORD_IN_CHAT = "For your security, please don't share your password here. Use the secure link instead."
PASSWORD_REPROMPT = "Please set your password using the secure link, then reply DONE."
KYC_REPROMPT = "Please reply 1 for Complete KYC Now or 2 for Later."
KYC_DOC = "Please select your preferred document for immediate validation:\nReply 1 for Aadhaar / UID or 2 for PAN."
KYC_DEFERRED = "No problem. You can complete your KYC any time by sending Hi. Befikar Raho!"
KYC_PLACEHOLDER = "Thanks! KYC verification is coming next in this demo."

_URL = re.compile(r"https?://\S+/set-password\?t=[A-Za-z0-9_\-]+")


@pytest.fixture
def client(default_client):
    # Default settings: journey_turn is the only enabled tool.
    return default_client


@pytest.fixture
def app(default_app):
    return default_app


def turn(client, message, **kw) -> dict:
    result, payload = call(client, "journey_turn", {"customer_message": message}, **kw)
    assert result["isError"] is False, payload
    assert set(payload) == {"ok", "state", "recognised", "message_for_agent", "message_for_customer"}
    assert payload["ok"] is True
    return payload


def expect(payload, state, message, recognised=True):
    assert (payload["state"], payload["recognised"], payload["message_for_customer"]) == (state, recognised, message)


def session(app):
    return app.state.store.get(CID)


def link_url(payload) -> str:
    return _URL.search(payload["message_for_customer"]).group(0)


def set_password(client, url, password=PASSWORD):
    token = url.split("?t=", 1)[1]
    resp = client.post("/set-password", data={"t": token, "password": password, "confirm": password})
    assert resp.status_code == 200


def to_email(client):
    turn(client, "Hi")
    turn(client, "yes")


def to_otp(client, email=EMAIL):
    to_email(client)
    turn(client, email)


def to_password(client):
    to_otp(client)
    return turn(client, "123456")


def to_kyc_choice(client):
    url = link_url(to_password(client))
    set_password(client, url)
    return turn(client, "DONE")


def audit_entries(client):
    return [e for e in client.get("/admin/audit", headers=headers()).json()["entries"] if e["tool"] == "journey_turn"]


# --- happy path ------------------------------------------------------------------------------


def test_happy_path(client, app):
    expect(turn(client, "Hi"), "CONSENT", WELCOME)
    s = session(app)
    assert s.phone == "+919876545820" and s.channel == "whatsapp"  # from the headers, as start_journey did

    expect(turn(client, "Yes"), "EMAIL", ASK_EMAIL)
    assert session(app).consent_id and session(app).notice_version == "v1"

    expect(turn(client, EMAIL), "OTP", OTP_SENT.format(email=EMAIL))

    payload = turn(client, "123456")
    url = link_url(payload)
    expect(payload, "PASSWORD", FIRST_LINK.format(url=url))
    assert payload["message_for_customer"].startswith("Email verified! Let's establish your portal access.")
    assert payload["message_for_customer"].endswith("Once done, reply DONE here.")
    assert session(app).email_verified is True

    set_password(client, url)
    expect(turn(client, "DONE"), "KYC_CHOICE", PASSWORD_SET)
    expect(turn(client, "1"), "KYC_DOC", KYC_DOC)
    assert session(app).kyc_choice == "now"
    expect(turn(client, "Aadhaar please"), "KYC_DOC", KYC_PLACEHOLDER)


def test_privacy_notice_url_from_env(fake_clock):
    app = create_app(load_settings({"GATEWAY_API_KEY": API_KEY, "PRIVACY_NOTICE_URL": "https://cirl.co.in/privacy"}))
    payload = turn(TestClient(app), "hello")
    assert "privacy notice: https://cirl.co.in/privacy\n\n" in payload["message_for_customer"]


def test_any_first_message_starts_the_journey(client):
    expect(turn(client, "I want to open an account"), "CONSENT", WELCOME)


def test_agent_message_reflects_recognised(client):
    turn(client, "Hi")
    assert turn(client, "what is CIRL?")["message_for_agent"] != turn(client, "yes")["message_for_agent"]


# --- consent -----------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["yes", "Y", "OK", "okay", "agree", "I agree", "sure", "proceed", "continue",
                                    "haan", "Yes!", "  yes please  "])
def test_yes_like_answers(client, answer):
    turn(client, "Hi")
    expect(turn(client, answer), "EMAIL", ASK_EMAIL)


@pytest.mark.parametrize("answer", ["no", "N", "stop", "don't", "Nahi", "No thanks", "I don't agree"])
def test_no_like_answers(client, app, answer):
    turn(client, "Hi")
    expect(turn(client, answer), "DECLINED", DECLINED)
    assert session(app).consent_id is None


def test_unrecognised_consent_answer(client):
    turn(client, "Hi")
    expect(turn(client, "What will you do with my data?"), "CONSENT", CONSENT_REPROMPT, recognised=False)


def test_decline_then_accept(client, app):
    turn(client, "Hi")
    expect(turn(client, "no"), "DECLINED", DECLINED)
    expect(turn(client, "why?"), "DECLINED", DECLINED, recognised=False)
    expect(turn(client, "still no"), "DECLINED", DECLINED)
    expect(turn(client, "YES"), "EMAIL", ASK_EMAIL)
    assert session(app).consent_id and session(app).notice_version == "v1"


# --- email --------------------------------------------------------------------------------------


@pytest.mark.parametrize("message", ["abc", "rahul@gmail", "my email is rahul at gmail dot com", "@gmail.com"])
def test_invalid_email(client, app, message):
    to_email(client)
    expect(turn(client, message), "EMAIL", INVALID_EMAIL, recognised=False)
    assert session(app).email_otp_send_count == 0


def test_email_inside_a_sentence(client, app):
    to_email(client)
    payload = turn(client, "Sure, my email is Rahul.Sharma@Gmail.com. Thanks!")
    expect(payload, "OTP", OTP_SENT.format(email="Rahul.Sharma@Gmail.com"))  # as typed
    assert session(app).email_otp_email_id == "Rahul.Sharma@Gmail.com"


def test_first_email_is_used(client, app):
    to_email(client)
    turn(client, "rahul@gmail.com or priya@example.com")
    assert session(app).email_otp_email_id == EMAIL


# --- OTP ------------------------------------------------------------------------------------------


def test_wrong_otp(client, app):
    to_otp(client)
    expect(turn(client, "000000"), "OTP", OTP_WRONG)
    assert session(app).email_otp_failed_attempts == 1
    assert turn(client, "123456")["state"] == "PASSWORD"


def test_otp_with_spaces_and_text(client):
    to_otp(client)
    assert turn(client, "my code is 123 456")["state"] == "PASSWORD"


def test_expired_otp(client, fake_clock):
    to_otp(client)
    fake_clock.advance(121)
    expect(turn(client, "123456"), "OTP", OTP_EXPIRED)


def test_locked_after_three_wrong_codes(client, app):
    to_otp(client)
    turn(client, "000000")
    turn(client, "000001")
    expect(turn(client, "000002"), "LOCKED", OTP_LOCKED)
    expect(turn(client, "123456"), "LOCKED", OTP_LOCKED)  # even the right code
    expect(turn(client, "RESEND"), "LOCKED", OTP_LOCKED)
    expect(turn(client, "hi"), "LOCKED", OTP_LOCKED)
    assert session(app).email_verified is False


def test_lock_counts_across_resends(client):
    to_otp(client)
    turn(client, "000000")
    turn(client, "RESEND")
    turn(client, "000001")
    expect(turn(client, "000002"), "LOCKED", OTP_LOCKED)


def test_resend(client, app, fake_clock):
    to_otp(client)
    expect(turn(client, "RESEND"), "OTP", OTP_RESENT.format(email=EMAIL))
    assert session(app).email_otp_send_count == 2  # a fresh code, even though the first was still valid
    fake_clock.advance(16)
    expect(turn(client, "resend"), "OTP", OTP_RESENT.format(email=EMAIL))
    assert session(app).email_otp_send_count == 3


def test_resend_after_expiry(client, fake_clock):
    to_otp(client)
    fake_clock.advance(121)
    turn(client, "123456")  # expired
    expect(turn(client, "RESEND"), "OTP", OTP_RESENT.format(email=EMAIL))
    assert turn(client, "123456")["state"] == "PASSWORD"


def test_resend_limit(client, fake_clock):
    to_otp(client)
    for _ in range(4):
        fake_clock.advance(16)
        turn(client, "RESEND")
    fake_clock.advance(16)
    expect(
        turn(client, "RESEND"), "OTP", "You've reached the limit for verification codes. Please try again later."
    )


def test_no_code_in_message(client):
    to_otp(client)
    expect(turn(client, "I didn't get it"), "OTP", OTP_REPROMPT.format(email=EMAIL), recognised=False)


# --- password -------------------------------------------------------------------------------------


def test_done_before_and_after_setting_the_password(client, app, fake_clock):
    url = link_url(to_password(client))
    expect(turn(client, "DONE"), "PASSWORD", PASSWORD_PENDING)
    set_password(client, url)
    fake_clock.advance(16)
    expect(turn(client, "DONE"), "KYC_CHOICE", PASSWORD_SET)
    assert session(app).password_confirmed is True


@pytest.mark.parametrize("message", ["done", "Done!", "finished", "set", "I'm done"])
def test_done_variants(client, message):
    set_password(client, link_url(to_password(client)))
    assert turn(client, message)["state"] == "KYC_CHOICE"


def test_done_after_link_expired(client, fake_clock):
    to_password(client)
    fake_clock.advance(600)
    expect(turn(client, "DONE"), "PASSWORD", LINK_EXPIRED)


def test_new_link(client, fake_clock):
    old_url = link_url(to_password(client))
    fake_clock.advance(600)
    payload = turn(client, "NEW LINK")
    new_url = link_url(payload)
    expect(payload, "PASSWORD", NEW_LINK.format(url=new_url))
    assert new_url != old_url
    old_token = old_url.split("?t=", 1)[1]
    assert client.get(f"/set-password?t={old_token}").status_code == 410  # the old link no longer works
    set_password(client, new_url)
    fake_clock.advance(16)
    assert turn(client, "DONE")["state"] == "KYC_CHOICE"


def test_password_typed_in_chat(client, app, captured_logs):
    to_password(client)
    expect(turn(client, PASSWORD), "PASSWORD", PASSWORD_IN_CHAT)
    s = session(app)
    assert s.password_hash is None and s.password_set_at is None
    audit = json.dumps(client.get("/admin/audit", headers=headers()).json())
    assert PASSWORD not in audit
    assert PASSWORD not in "\n".join(captured_logs)
    assert PASSWORD not in json.dumps(s.__dict__)


def test_unrecognised_message_while_waiting_for_password(client):
    to_password(client)
    expect(turn(client, "how long does this take?"), "PASSWORD", PASSWORD_REPROMPT, recognised=False)


# --- KYC choice ------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["1", "1.", "now", "Complete KYC Now"])
def test_kyc_now(client, app, answer):
    to_kyc_choice(client)
    expect(turn(client, answer), "KYC_DOC", KYC_DOC)
    assert session(app).kyc_choice == "now"


@pytest.mark.parametrize("answer", ["2", "later", "Later please", "not now"])
def test_kyc_later(client, app, answer):
    to_kyc_choice(client)
    expect(turn(client, answer), "KYC_DEFERRED", KYC_DEFERRED)
    assert session(app).kyc_choice == "later"


def test_kyc_unrecognised(client):
    to_kyc_choice(client)
    expect(turn(client, "what is KYC?"), "KYC_CHOICE", KYC_REPROMPT, recognised=False)


def test_deferred_then_back_and_choose_now(client, app):
    to_kyc_choice(client)
    turn(client, "2")
    expect(turn(client, "Hi"), "KYC_CHOICE", KYC_QUESTION)  # without the password line
    expect(turn(client, "1"), "KYC_DOC", KYC_DOC)
    assert session(app).kyc_choice == "now"


# --- global commands -------------------------------------------------------------------------------


def test_hi_mid_journey_resumes(client, app, fake_clock):
    turn(client, "Hi")
    fake_clock.advance(16)
    expect(turn(client, "Hi"), "CONSENT", WELCOME)
    turn(client, "yes")
    expect(turn(client, "hello"), "EMAIL", ASK_EMAIL)
    turn(client, EMAIL)
    expect(turn(client, "Hi"), "OTP", OTP_REPROMPT.format(email=EMAIL))
    assert session(app).email_otp_send_count == 1  # resuming doesn't resend
    turn(client, "123456")
    expect(turn(client, "hello"), "PASSWORD", PASSWORD_REPROMPT)


def test_restart(client, app):
    to_otp(client)
    expect(turn(client, "RESTART"), "CONSENT", WELCOME)
    s = session(app)
    assert s.consent_id is None and s.email_otp_email_id is None and s.email_otp_send_count == 0
    expect(turn(client, "yes"), "EMAIL", ASK_EMAIL)


def test_expired_session_starts_again(client, fake_clock):
    to_otp(client)
    fake_clock.advance(61 * 60)
    expect(turn(client, "123456"), "CONSENT", WELCOME)


def test_session_survives_repeated_calls(client, app, fake_clock):
    to_email(client)
    consent_id = session(app).consent_id
    for i in range(10):
        fake_clock.advance(50 * 60)  # under the 60-minute TTL each time
        expect(turn(client, f"hmm {i}"), "EMAIL", INVALID_EMAIL, recognised=False)
    assert session(app).consent_id == consent_id
    assert turn(client, EMAIL)["state"] == "OTP"


def test_conversations_are_independent(client):
    to_otp(client)
    expect(turn(client, "Hi", conversation_id="conv-test-2"), "CONSENT", WELCOME)
    assert turn(client, "123456")["state"] == "PASSWORD"


# --- duplicate protection ---------------------------------------------------------------------------


def test_duplicate_within_15s_has_no_side_effects(client, app, fake_clock):
    to_otp(client)
    first = turn(client, "000000")
    fake_clock.advance(15)
    assert turn(client, "000000") == first
    assert session(app).email_otp_failed_attempts == 1
    assert audit_entries(client)[-1]["event"] == "journey_duplicate_ignored"

    fake_clock.advance(1)  # 16 s after the first: processed again
    turn(client, "000000")
    assert session(app).email_otp_failed_attempts == 2


def test_duplicate_email_does_not_send_twice(client, app):
    to_email(client)
    first = turn(client, EMAIL)
    assert turn(client, EMAIL) == first
    assert session(app).email_otp_send_count == 1


def test_duplicate_of_new_link_returns_the_same_link(client, app, fake_clock):
    to_password(client)
    fake_clock.advance(16)
    first = turn(client, "NEW LINK")
    link_hash = session(app).password_link_hash
    assert turn(client, "NEW LINK") == first
    assert session(app).password_link_hash == link_hash


def test_duplicate_is_per_conversation(client, app):
    turn(client, "Hi")
    turn(client, "Hi", conversation_id="conv-test-2")
    assert app.state.store.get("conv-test-2") is not None


def test_only_the_last_message_counts_as_duplicate(client, app):
    to_otp(client)
    turn(client, "000000")
    turn(client, "000001")
    turn(client, "000000")  # not the previous message: processed
    assert session(app).journey_state == "LOCKED"


# --- validation and audit ---------------------------------------------------------------------------


@pytest.mark.parametrize("args", [{}, {"customer_message": 42}, {"customer_message": "x" * 5000}])
def test_invalid_customer_message(client, args):
    result, payload = call(client, "journey_turn", args)
    assert result["isError"] is True and payload["error_code"] == "GW-004"


def test_needs_conversation_id(client):
    body = rpc(client, "tools/call", {"name": "journey_turn", "arguments": {"customer_message": "Hi"}},
               conversation_id=None).json()
    assert body["error"]["code"] == -32602


def test_audit_records_state_changes_with_masked_values(client, captured_logs, fake_clock):
    to_otp(client, "Rahul.Sharma@Gmail.com")
    turn(client, "000000")
    url = link_url(turn(client, "123456"))
    token = url.split("?t=", 1)[1]
    turn(client, PASSWORD)
    set_password(client, url)
    turn(client, "DONE")
    turn(client, "2")

    entries = audit_entries(client)
    assert [(e["from_state"], e["to_state"], e["event"]) for e in entries] == [
        ("START", "CONSENT", "journey_started"),
        ("CONSENT", "EMAIL", "consent_recorded"),
        ("EMAIL", "OTP", "email_otp_sent"),
        ("OTP", "OTP", "email_otp_wrong"),
        ("OTP", "PASSWORD", "email_verified"),
        ("PASSWORD", "PASSWORD", "password_in_chat_ignored"),
        ("PASSWORD", "KYC_CHOICE", "password_confirmed"),
        ("KYC_CHOICE", "KYC_DEFERRED", "kyc_deferred"),
    ]
    for entry in entries:
        assert entry["input"] == {"customer_message": "[REDACTED]"}

    text = json.dumps(client.get("/admin/audit", headers=headers()).json()) + "\n".join(captured_logs)
    for secret in ("Rahul.Sharma@Gmail.com", "123456", "000000", PASSWORD, token):
        assert secret not in text, secret
    assert "Ra*****@Gmail.com" in text
