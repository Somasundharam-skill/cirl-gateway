import hashlib
import json
import logging

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import RedactQueryFilter, create_app
from tests.conftest import API_KEY, call, headers, start_to_consent

GOOD_PASSWORD = "Str0ng!pass"
BASE_URL = "https://cirl-gateway.example.com"
WA_LINK = "https://wa.me/912269986221?text=DONE"
INVALID_TEXT = "This link has expired or was already used. Please go back to the chat and reply NEW LINK."
LINK_TEXT = (
    "Let's set up your portal password. For your security, please set it on this secure page "
    "(link valid for 10 minutes):\n{url}\n\n*Criteria:*\n- At least 8 characters\n- Mixed case & 1 special character"
    "\n\nOnce done, reply DONE here."
)


@pytest.fixture
def app(fake_clock):
    return create_app(
        load_settings(
            {"GATEWAY_API_KEY": API_KEY, "PUBLIC_BASE_URL": BASE_URL + "/", "WHATSAPP_NUMBER": "+91 22699 86221"}
        )
    )


@pytest.fixture
def client(app):
    return TestClient(app)


def verified(client, channel="whatsapp"):
    hdr = {"X-Sa-Channel": channel}
    call(client, "start_journey", **hdr)
    call(client, "record_consent", {"consent": True, "notice_version": "v1.0"}, **hdr)
    call(client, "send_email_otp", {"email_id": "rahul@gmail.com"}, **hdr)
    call(client, "verify_email_otp", {"otp_no": "123456"}, **hdr)


def new_link(client) -> tuple[str, dict]:
    result, payload = call(client, "create_password_link")
    assert result["isError"] is False, payload
    return payload["url"].split("?t=", 1)[1], payload


def submit(client, token, password=GOOD_PASSWORD, confirm=None):
    return client.post(
        "/set-password", data={"t": token, "password": password, "confirm": password if confirm is None else confirm}
    )


def session(app):
    return app.state.store.get("conv-test-1")


# --- create_password_link ---------------------------------------------------------------


def test_link_requires_verified_email(client):
    result, payload = call(client, "create_password_link")  # no session
    assert result["isError"] is True and payload["error_code"] == "GW-020"
    assert payload["message_for_customer"] == "Please verify your email first."

    start_to_consent(client)
    call(client, "send_email_otp", {"email_id": "rahul@gmail.com"})
    result, payload = call(client, "create_password_link")  # sent, not verified
    assert result["isError"] is True and payload["error_code"] == "GW-020"
    assert payload["message_for_customer"] == "Please verify your email first."


def test_link_shape(client, app):
    verified(client)
    token, payload = new_link(client)
    assert payload["url"] == f"{BASE_URL}/set-password?t={token}"
    assert payload["expires_in_seconds"] == 600
    assert payload["message_for_customer"] == LINK_TEXT.format(url=payload["url"])
    assert payload["message_for_agent"] == "Password link created."
    assert len(token) >= 43  # token_urlsafe(32)
    s = session(app)
    assert s.password_link_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(s)


def test_verify_email_success_text_is_short(client):
    start_to_consent(client)
    call(client, "send_email_otp", {"email_id": "rahul@gmail.com"})
    _, payload = call(client, "verify_email_otp", {"otp_no": "123456"})
    assert payload["message_for_customer"] == "Email verified!"


# --- page and token lifecycle -----------------------------------------------------------


def test_form_page(client):
    verified(client)
    token, _ = new_link(client)
    resp = client.get(f"/set-password?t={token}")
    assert resp.status_code == 200
    page = resp.text
    assert "<h1>CIRL</h1>" in page
    assert page.count('type="password"') == 2 and page.count('autocomplete="new-password"') == 2
    for check in ("length", "upper", "lower", "special", "match"):
        assert f'data-check="{check}"' in page
    assert 'action="/set-password"' in page  # token goes in the POST body, not the URL
    assert "<script>" not in page  # CSP allows scripts from 'self' only
    assert client.get("/set-password.js").status_code == 200


def test_link_valid_at_9_59_and_expired_at_10_minutes(client, fake_clock):
    verified(client)
    token, _ = new_link(client)
    fake_clock.advance(599)
    assert client.get(f"/set-password?t={token}").status_code == 200
    fake_clock.advance(1)
    resp = client.get(f"/set-password?t={token}")
    assert resp.status_code == 410 and INVALID_TEXT in resp.text
    assert submit(client, token).status_code == 410


@pytest.mark.parametrize("token", ["", "nope", "x" * 500])
def test_unknown_token_rejected(client, token):
    resp = client.get(f"/set-password?t={token}")
    assert resp.status_code == 410 and INVALID_TEXT in resp.text


def test_reused_token_rejected(client):
    verified(client)
    token, _ = new_link(client)
    assert submit(client, token).status_code == 200
    resp = client.get(f"/set-password?t={token}")
    assert resp.status_code == 410 and INVALID_TEXT in resp.text
    assert submit(client, token, "An0ther!pass").status_code == 410


def test_new_link_invalidates_old(client):
    verified(client)
    old, _ = new_link(client)
    new, _ = new_link(client)
    assert old != new
    assert client.get(f"/set-password?t={old}").status_code == 410
    assert submit(client, old).status_code == 410
    assert client.get(f"/set-password?t={new}").status_code == 200
    assert submit(client, new).status_code == 200


@pytest.mark.parametrize(
    "password",
    ["Sh0rt!", "alllower1!", "ALLUPPER1!", "NoSpecial123", "        ", "Aa!" + "x" * 200],
)
def test_weak_password_rejected(client, app, password):
    verified(client)
    token, _ = new_link(client)
    resp = submit(client, token, password)
    assert resp.status_code == 400 and "Please fix the following" in resp.text
    assert session(app).password_hash is None
    assert password not in resp.text


def test_mismatched_confirm_rejected(client, app):
    verified(client)
    token, _ = new_link(client)
    resp = submit(client, token, GOOD_PASSWORD, "Str0ng!pas")
    assert resp.status_code == 400 and "do not match" in resp.text
    assert session(app).password_hash is None
    assert submit(client, token).status_code == 200  # the link still works after a failed attempt


def test_max_five_post_attempts(client, app):
    verified(client)
    token, _ = new_link(client)
    for _ in range(4):
        assert submit(client, token, "weak").status_code == 400
    assert submit(client, token, "weak").status_code == 410  # 5th attempt uses up the link
    assert submit(client, token).status_code == 410
    assert session(app).password_hash is None


def test_only_argon2_hash_is_stored(client, app):
    verified(client)
    token, _ = new_link(client)
    submit(client, token)
    s = session(app)
    assert s.password_hash.startswith("$argon2id$")
    assert PasswordHasher().verify(s.password_hash, GOOD_PASSWORD)
    assert s.password_link_used is True and s.state == "PASSWORD_SET"
    stored = repr(s)
    assert GOOD_PASSWORD not in stored and token not in stored


# --- check_password_status --------------------------------------------------------------


def test_check_password_status_before_and_after(client):
    verified(client)
    result, payload = call(client, "check_password_status")
    assert result["isError"] is True and payload["error_code"] == "GW-023"  # no link yet

    token, _ = new_link(client)
    result, payload = call(client, "check_password_status")
    assert result["isError"] is True and payload["error_code"] == "GW-021"
    assert payload["message_for_customer"] == (
        "We haven't received your password yet. Please set it using the secure link, then reply DONE."
    )

    submit(client, token)
    result, payload = call(client, "check_password_status")
    assert result["isError"] is False and payload["ok"] is True
    assert payload["message_for_customer"] == "Your password has been set successfully."


def test_check_password_status_expired(client, fake_clock):
    verified(client)
    new_link(client)
    fake_clock.advance(600)
    result, payload = call(client, "check_password_status")
    assert result["isError"] is True and payload["error_code"] == "GW-022"
    assert payload["message_for_customer"] == "Your secure link has expired. Reply NEW LINK and I'll send you a fresh one."
    # NEW LINK: a fresh link works again.
    token, _ = new_link(client)
    assert submit(client, token).status_code == 200


# --- success page -----------------------------------------------------------------------


def test_success_page_whatsapp_auto_redirect(client):
    verified(client, channel="whatsapp")
    token, _ = new_link(client)
    page = submit(client, token).text
    assert "Password set successfully." in page
    assert f'href="{WA_LINK}"' in page and "Return to WhatsApp" in page
    assert "If you're on a computer, return to the chat and reply DONE." in page
    assert f'<meta http-equiv="refresh" content="2;url={WA_LINK}">' in page
    assert f'data-redirect="{WA_LINK}"' in page


def test_success_page_no_auto_redirect_for_other_channels(client):
    verified(client, channel="web")
    token, _ = new_link(client)
    page = submit(client, token).text
    assert f'href="{WA_LINK}"' in page  # button is always shown
    assert "http-equiv=\"refresh\"" not in page and "data-redirect" not in page


def test_success_page_without_whatsapp_number(fake_clock):
    client = TestClient(create_app(load_settings({"GATEWAY_API_KEY": API_KEY})))
    verified(client)
    token, _ = new_link(client)
    page = submit(client, token).text
    assert "Password set successfully." in page
    assert "wa.me" not in page and "refresh" not in page


# --- security ---------------------------------------------------------------------------


def test_security_headers_on_every_page(client):
    verified(client)
    token, _ = new_link(client)
    responses = [
        client.get(f"/set-password?t={token}"),
        submit(client, token, "weak"),
        submit(client, token),
        client.get(f"/set-password?t={token}"),  # used -> error page
        client.get("/set-password.js"),
    ]
    for resp in responses:
        assert resp.headers["Referrer-Policy"] == "no-referrer"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-Robots-Tag"] == "noindex"
        csp = resp.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "'unsafe-inline'" in csp
        assert "script-src 'self' 'unsafe-inline'" not in csp


def test_set_password_needs_no_bearer(client):
    verified(client)
    token, _ = new_link(client)
    assert client.get(f"/set-password?t={token}", headers={}).status_code == 200


def test_no_token_or_password_in_logs_or_audit(client, fake_clock, captured_logs):
    verified(client)
    old, _ = new_link(client)
    token, _ = new_link(client)
    submit(client, token, "weak")
    submit(client, token, GOOD_PASSWORD, "mismatch!A1")
    submit(client, token)
    call(client, "check_password_status")
    expired, _ = new_link(client)
    fake_clock.advance(600)
    call(client, "check_password_status")
    client.get(f"/set-password?t={expired}")

    entries = client.get("/admin/audit", headers=headers()).json()["entries"]
    events = [e["event"] for e in entries]
    for event in ("password_link_created", "password_set", "password_link_expired"):
        assert event in events
    for text in [json.dumps(e) for e in entries] + captured_logs:
        for secret in (old, token, expired, GOOD_PASSWORD, "mismatch!A1", "$argon2"):
            assert secret not in text, text
    created = next(e for e in entries if e["event"] == "password_link_created")
    assert created["output"]["url"] == f"{BASE_URL}/set-password?t=[REDACTED]"


def test_access_log_drops_set_password_query():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/set-password?t=SECRETTOKEN", "1.1", 200), None,
    )
    assert RedactQueryFilter().filter(record) is True
    assert record.getMessage() == '1.2.3.4:5 - "GET /set-password HTTP/1.1" 200'

    other = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/health?x=1", "1.1", 200), None,
    )
    RedactQueryFilter().filter(other)
    assert "/health?x=1" in other.getMessage()


def test_access_log_filter_installed(app):
    assert any(isinstance(f, RedactQueryFilter) for f in logging.getLogger("uvicorn.access").filters)
