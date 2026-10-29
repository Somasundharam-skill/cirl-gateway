"""Secure password-setup page, opened from a one-time link sent in the chat.

The password is typed only into this page and never passes through the chat,
Perfox or any log. The link token is the authorisation: only its SHA-256 hash
is stored, it expires after 10 minutes, it allows 5 POST attempts, and it
stops working once used or once a newer link is created for the session.
"""

import hashlib
import html
import logging
import re
import secrets
from urllib.parse import parse_qs

from argon2 import PasswordHasher
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from app import clock
from app.sessions import Session

logger = logging.getLogger("cirl_gateway.password")

LINK_VALID_SECONDS = 600
MAX_POST_ATTEMPTS = 5
MAX_PASSWORD_LENGTH = 128
MAX_BODY_BYTES = 4096

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
}

LINK_INVALID_TEXT = "This link has expired or was already used. Please go back to the chat and reply NEW LINK."

_hasher = PasswordHasher()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def link_usable(session: Session) -> bool:
    return (
        session.password_link_hash is not None
        and not session.password_link_used
        and session.password_link_attempts < MAX_POST_ATTEMPTS
        and session.password_link_expires_at is not None
        and clock.now() < session.password_link_expires_at
    )


def password_problems(password: str, confirm: str) -> list[str]:
    """Server-side criteria; the page shows the same checks live."""
    problems = []
    if len(password) < 8:
        problems.append("Use at least 8 characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"Use at most {MAX_PASSWORD_LENGTH} characters.")
    if not re.search(r"[A-Z]", password):
        problems.append("Include an uppercase letter.")
    if not re.search(r"[a-z]", password):
        problems.append("Include a lowercase letter.")
    if not re.search(r"[^A-Za-z0-9\s]", password):
        problems.append("Include a special character.")
    if not secrets.compare_digest(password.encode(), confirm.encode()):
        problems.append("The two passwords do not match.")
    return problems


# --- HTML -------------------------------------------------------------------------------

_STYLE = """
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#f3f5f8;color:#1b2430}
main{max-width:420px;margin:0 auto;padding:24px 16px}
.card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
h1{margin:0 0 4px;font-size:28px;letter-spacing:.04em;color:#0b4f8a}
h2{margin:0 0 20px;font-size:18px;font-weight:600}
label{display:block;margin:14px 0 6px;font-weight:600;font-size:15px}
input[type=password]{width:100%;padding:12px;font-size:16px;border:1px solid #c5ccd6;border-radius:8px}
input[type=password]:focus{outline:2px solid #0b4f8a;border-color:#0b4f8a}
ul{list-style:none;padding:0;margin:16px 0}
li{padding:3px 0 3px 26px;position:relative;color:#5b6675;font-size:14px}
li::before{content:"\\2715";position:absolute;left:4px;color:#b3261e}
li.ok{color:#1d6b35}
li.ok::before{content:"\\2713";color:#1d6b35}
button,.button{display:block;width:100%;padding:14px;font-size:17px;font-weight:600;text-align:center;
  border:0;border-radius:8px;background:#0b4f8a;color:#fff;text-decoration:none;cursor:pointer}
.button.wa{background:#1f8f4e;margin:20px 0 12px}
.error{background:#fdecea;color:#8a1c14;border-radius:8px;padding:12px;margin:0 0 12px}
.error ul{margin:6px 0 0}.error li{color:#8a1c14;padding-left:0}.error li::before{content:none}
.success{font-size:20px;font-weight:600;color:#1d6b35}
.muted{color:#5b6675;font-size:14px}
"""


def _page(title: str, body: str, *, body_attrs: str = "", head_extra: str = "") -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta name=\"referrer\" content=\"no-referrer\"><meta name=\"robots\" content=\"noindex\">"
        f"{head_extra}<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body{body_attrs}><main><div class=\"card\"><h1>CIRL</h1>{body}</div></main>"
        "<script src=\"/set-password.js\"></script></body></html>"
    )


def render_form(token: str, problems: list[str] | None = None) -> str:
    error = ""
    if problems:
        items = "".join(f"<li>{html.escape(p)}</li>" for p in problems)
        error = f"<div class=\"error\" role=\"alert\">Please fix the following:<ul>{items}</ul></div>"
    body = (
        "<h2>Set your portal password</h2>"
        f"{error}"
        "<form method=\"post\" action=\"/set-password\">"
        f"<input type=\"hidden\" name=\"t\" value=\"{html.escape(token)}\">"
        "<label for=\"password\">New password</label>"
        f"<input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"new-password\" required "
        f"minlength=\"8\" maxlength=\"{MAX_PASSWORD_LENGTH}\">"
        "<label for=\"confirm\">Confirm password</label>"
        f"<input id=\"confirm\" name=\"confirm\" type=\"password\" autocomplete=\"new-password\" required "
        f"maxlength=\"{MAX_PASSWORD_LENGTH}\">"
        "<ul id=\"criteria\">"
        "<li data-check=\"length\">At least 8 characters</li>"
        "<li data-check=\"upper\">An uppercase letter</li>"
        "<li data-check=\"lower\">A lowercase letter</li>"
        "<li data-check=\"special\">A special character</li>"
        "<li data-check=\"match\">Both passwords match</li>"
        "</ul>"
        "<button type=\"submit\">Set password</button>"
        "</form>"
    )
    return _page("Set your password · CIRL", body)


def render_invalid() -> str:
    return _page("Link expired · CIRL", f"<h2>Link not valid</h2><p>{html.escape(LINK_INVALID_TEXT)}</p>")


def whatsapp_link(number_digits: str) -> str | None:
    return f"https://wa.me/{number_digits}?text=DONE" if number_digits else None


def render_success(wa_url: str | None, auto_redirect: bool) -> str:
    button = (
        f"<a class=\"button wa\" href=\"{html.escape(wa_url)}\" rel=\"noreferrer noopener\">Return to WhatsApp</a>"
        if wa_url
        else ""
    )
    body = (
        "<p class=\"success\">Password set successfully.</p>"
        f"{button}"
        "<p class=\"muted\">If you're on a computer, return to the chat and reply DONE.</p>"
    )
    if wa_url and auto_redirect:
        url = html.escape(wa_url)
        return _page(
            "Password set · CIRL",
            body,
            body_attrs=f" data-redirect=\"{url}\"",
            head_extra=f"<meta http-equiv=\"refresh\" content=\"2;url={url}\">",
        )
    return _page("Password set · CIRL", body)


# Served from /set-password.js because the CSP allows scripts from 'self' only.
PAGE_JS = """(function () {
  var redirect = document.body.getAttribute("data-redirect");
  if (redirect) {
    setTimeout(function () { window.location.href = redirect; }, 2000);
    return;
  }
  var pw = document.getElementById("password");
  var cf = document.getElementById("confirm");
  if (!pw || !cf) return;
  var checks = {
    length: function (p) { return p.length >= 8; },
    upper: function (p) { return /[A-Z]/.test(p); },
    lower: function (p) { return /[a-z]/.test(p); },
    special: function (p) { return /[^A-Za-z0-9\\s]/.test(p); },
    match: function (p, c) { return p.length > 0 && p === c; }
  };
  function update() {
    var items = document.querySelectorAll("[data-check]");
    for (var i = 0; i < items.length; i++) {
      var ok = checks[items[i].getAttribute("data-check")](pw.value, cf.value);
      items[i].className = ok ? "ok" : "";
    }
  }
  pw.addEventListener("input", update);
  cf.addEventListener("input", update);
  update();
})();
"""


# --- routes -----------------------------------------------------------------------------


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers=SECURITY_HEADERS)


def add_routes(app: FastAPI) -> None:
    """GET/POST /set-password. No bearer auth: the link token is the authorisation."""
    settings = app.state.settings
    store = app.state.store
    audit = app.state.audit

    def find_session(token: str) -> Session | None:
        if not token or len(token) > 128:
            return None
        return store.get_by_password_link(hash_token(token))

    def invalid(session: Session | None) -> HTMLResponse:
        if session is not None and not session.password_link_used and session.password_link_expires_at is not None:
            if clock.now() >= session.password_link_expires_at:
                audit.record(
                    conversation_id=session.conversation_id,
                    tool="set_password_page",
                    tool_input={},
                    tool_output={"ok": False},
                    ok=False,
                    error_code="GW-022",
                    event="password_link_expired",
                )
        return _html(render_invalid(), 410)

    @app.get("/set-password.js")
    def page_js():
        return Response(PAGE_JS, media_type="application/javascript", headers=SECURITY_HEADERS)

    @app.get("/set-password")
    def show_form(request: Request):
        token = request.query_params.get("t", "")
        session = find_session(token)
        if session is None or not link_usable(session):
            return invalid(session)
        return _html(render_form(token))

    @app.post("/set-password")
    async def submit(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return _html(render_invalid(), 413)
        form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        token = form.get("t", [""])[0]
        password = form.get("password", [""])[0]
        confirm = form.get("confirm", [""])[0]
        del raw, form

        session = find_session(token)
        if session is None or not link_usable(session):
            return invalid(session)

        session.password_link_attempts += 1
        problems = password_problems(password, confirm)
        if problems:
            store.save(session)
            if session.password_link_attempts >= MAX_POST_ATTEMPTS:
                return _html(render_invalid(), 410)
            return _html(render_form(token, problems), 400)

        password_hash = await run_in_threadpool(_hasher.hash, password)
        del password, confirm

        session.password_hash = password_hash
        session.password_set_at = clock.iso()
        session.password_link_used = True
        session.state = "PASSWORD_SET"
        store.save(session)
        audit.record(
            conversation_id=session.conversation_id,
            tool="set_password_page",
            tool_input={},
            tool_output={"ok": True},
            ok=True,
            error_code=None,
            event="password_set",
        )

        wa_url = whatsapp_link(settings.whatsapp_number)
        auto_redirect = (session.channel or "").strip().lower() == "whatsapp"
        return _html(render_success(wa_url, auto_redirect))
