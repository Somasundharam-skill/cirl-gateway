"""journey_turn: the whole onboarding journey as one server-side state machine.

The agent passes every customer message through unchanged; the gateway
decides what happens next and returns the exact text to send. Side effects
reuse the tool handlers in app.tools (consent, email OTP, password link, KYC
choice), so their rules (limits, expiry, lockout, hashing) apply unchanged.

The customer's message is never stored or logged: the audit redacts it, and
duplicate detection keeps only a keyed hash of the last message.
"""

import hashlib
import hmac
import re
import secrets
import threading
from typing import Any

from app import clock, tools
from app.sessions import Session
from app.tools import ToolContext, ToolError
from app.validation import EMAIL_RE

DUPLICATE_WINDOW_SECONDS = 15
MAX_MESSAGE_LENGTH = 4096
NOTICE_VERSION = "v1"

START = "START"
CONSENT = "CONSENT"
DECLINED = "DECLINED"
EMAIL = "EMAIL"
OTP = "OTP"
LOCKED = "LOCKED"
PASSWORD = "PASSWORD"
KYC_CHOICE = "KYC_CHOICE"
KYC_DOC = "KYC_DOC"
KYC_DEFERRED = "KYC_DEFERRED"

# --- customer texts ----------------------------------------------------------------------

TEXT_WELCOME = (
    "Welcome to CENTRICO Insurance Repository Limited (CIRL)! Let's set up your secure Electronic Insurance "
    "Account (eIA) in minutes.\n\n"
    "Before we begin: CIRL will use the details you share here only to open and manage your eIA, as described "
    "in CIRL's privacy notice: {privacy_notice_url}\n\n"
    "Do you agree? Reply YES to continue or NO to stop."
)
TEXT_CONSENT_REPROMPT = "Please reply YES to continue or NO to stop."
TEXT_DECLINED = (
    "No problem. We can't open an eIA without your consent. If you change your mind, just reply YES. "
    "For help, write to cirthelpdesk@cirl.co.in"
)
TEXT_ASK_EMAIL = "To begin, please enter your valid Email ID:"
TEXT_INVALID_EMAIL = "That doesn't look like a valid email address. Please enter your valid Email ID:"
TEXT_OTP_SENT = (
    "Great! We've sent an Email Verification Code to {email_id}. "
    "Please enter the 6-digit OTP below (Valid for {expires_in} seconds):"
)
TEXT_OTP_RESENT = (
    "Great! We've sent a new Email Verification Code to {email_id}. "
    "Please enter the 6-digit OTP below (Valid for {expires_in} seconds):"
)
TEXT_OTP_WRONG = "That code is incorrect. Please re-enter the 6-digit OTP:"
TEXT_OTP_EXPIRED = "That code has expired. Reply RESEND and I'll send you a new one."
TEXT_OTP_LOCKED = "Too many incorrect attempts. Please start again later."
TEXT_OTP_SEND_LIMIT = "You've reached the limit for verification codes. Please try again later."
TEXT_OTP_REPROMPT = "Please enter the 6-digit OTP sent to {email_id}, or reply RESEND for a new code."
TEXT_KYC_QUESTION = (
    "Account access generated successfully! Do you want to complete your pending KYC process now to activate "
    "your repository benefits?\nReply 1 for Complete KYC Now or 2 for Later."
)
TEXT_PASSWORD_SET = "Your password has been set successfully.\n\n" + TEXT_KYC_QUESTION
TEXT_PASSWORD_PENDING = "We haven't received your password yet. Please set it using the secure link, then reply DONE."
TEXT_PASSWORD_LINK_EXPIRED = "Your secure link has expired. Reply NEW LINK and I'll send you a fresh one."
TEXT_PASSWORD_IN_CHAT = "For your security, please don't share your password here. Use the secure link instead."
TEXT_PASSWORD_REPROMPT = "Please set your password using the secure link, then reply DONE."
TEXT_KYC_REPROMPT = "Please reply 1 for Complete KYC Now or 2 for Later."
TEXT_KYC_DOC = "Please select your preferred document for immediate validation:\nReply 1 for Aadhaar / UID or 2 for PAN."
TEXT_KYC_DEFERRED = "No problem. You can complete your KYC any time by sending Hi. Befikar Raho!"
TEXT_KYC_DOC_PLACEHOLDER = "Thanks! KYC verification is coming next in this demo."

AGENT_RECOGNISED = "Send message_for_customer to the customer exactly as given."
AGENT_UNRECOGNISED = (
    "The customer's message was not an answer to the current step. You may briefly answer their question, "
    "then send message_for_customer exactly as given."
)

# --- understanding the message ------------------------------------------------------------

YES_PHRASES = {"yes", "y", "ok", "okay", "agree", "i agree", "sure", "proceed", "continue", "haan", "yes please"}
YES_FIRST_WORDS = {"yes", "ok", "okay", "agree", "sure", "proceed", "continue", "haan"}
NO_WORDS = {"no", "n", "stop", "don't", "dont", "nahi", "nahin", "nope"}
GREETINGS = {"hi", "hello", "hey", "hii", "hi there", "hello there"}
RESEND_PHRASES = {"resend", "resend otp", "resend code", "new code", "new otp", "send again", "send a new code"}
DONE_PHRASES = {"done", "finished", "set", "password set", "i'm done", "im done", "it's done", "its done", "all done"}
NEW_LINK_PHRASES = {"new link", "newlink", "send new link", "send a new link", "new link please", "link"}
KYC_NOW_PHRASES = {"1", "now", "complete kyc now", "complete kyc", "complete", "kyc now", "yes"}
KYC_LATER_PHRASES = {"2", "later", "not now", "skip", "no", "maybe later"}

_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SIX_DIGITS = re.compile(r"(?<!\d)\d{6}(?!\d)")


def _normalise(text: str) -> str:
    text = text.replace("’", "'").lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" .!?,;:\"()[]")


def _words(norm: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", norm)


def _is_no(norm: str) -> bool:
    return any(w in NO_WORDS for w in _words(norm))


def _is_yes(norm: str) -> bool:
    if _is_no(norm):
        return False
    words = _words(norm)
    return norm in YES_PHRASES or bool(words and words[0] in YES_FIRST_WORDS)


def _extract_email(text: str) -> str | None:
    match = _EMAIL_IN_TEXT.search(text)
    if not match:
        return None
    email = match.group(0).strip().rstrip(".")
    return email if EMAIL_RE.match(email) else None


def _extract_code(text: str) -> str | None:
    match = _SIX_DIGITS.search(re.sub(r"\s+", "", text))
    return match.group(0) if match else None


def _looks_like_password(text: str) -> bool:
    return (
        len(text) >= 8
        and not re.search(r"\s", text)
        and re.search(r"[A-Z]", text) is not None
        and re.search(r"[a-z]", text) is not None
        and re.search(r"[^A-Za-z0-9]", text) is not None
    )


def _kyc_choice(norm: str) -> str | None:
    words = _words(norm)
    if norm in KYC_LATER_PHRASES or "later" in words or "not now" in norm or ("2" in words and "1" not in words):
        return "later"
    if norm in KYC_NOW_PHRASES or "now" in words or ("1" in words and "2" not in words):
        return "now"
    return None


# --- duplicate protection ------------------------------------------------------------------


class TurnCache:
    """The last message (as a keyed hash) and result per conversation, kept for 15 seconds.

    Held in process memory only, outside the session store, because a result can
    contain the password link.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self._key = secrets.token_bytes(32)
        self._last: dict[str, tuple[bytes, float, dict]] = {}

    def digest(self, message: str) -> bytes:
        return hmac.new(self._key, message.encode("utf-8"), hashlib.sha256).digest()

    def get(self, conversation_id: str, digest: bytes, now: float) -> dict | None:
        entry = self._last.get(conversation_id)
        if entry and hmac.compare_digest(entry[0], digest) and now - entry[1] <= DUPLICATE_WINDOW_SECONDS:
            return dict(entry[2])
        return None

    def put(self, conversation_id: str, digest: bytes, now: float, result: dict) -> None:
        self._last[conversation_id] = (digest, now, dict(result))
        for cid in [c for c, e in self._last.items() if now - e[1] > DUPLICATE_WINDOW_SECONDS]:
            del self._last[cid]


# --- state machine ---------------------------------------------------------------------------

# A handler returns (next state, recognised, message_for_customer, audit event).
Outcome = tuple[str, bool, str, str]


def _welcome(ctx: ToolContext) -> str:
    return TEXT_WELCOME.format(privacy_notice_url=ctx.settings.privacy_notice_url)


def _prompt(ctx: ToolContext, session: Session) -> str:
    """The current stage's prompt, for a customer who says hi mid-journey."""
    state = session.journey_state
    if state == DECLINED:
        return TEXT_DECLINED
    if state == EMAIL:
        return TEXT_ASK_EMAIL
    if state == OTP:
        return TEXT_OTP_REPROMPT.format(email_id=session.email_otp_email_id)
    if state == LOCKED:
        return TEXT_OTP_LOCKED
    if state == PASSWORD:
        return TEXT_PASSWORD_REPROMPT
    if state == KYC_CHOICE:
        return TEXT_KYC_QUESTION
    if state == KYC_DOC:
        return TEXT_KYC_DOC
    return _welcome(ctx)


def _accept_consent(ctx: ToolContext) -> Outcome:
    _, event = tools.record_consent(ctx, {"consent": True, "notice_version": NOTICE_VERSION})
    return EMAIL, True, TEXT_ASK_EMAIL, event


def _decline_consent(ctx: ToolContext) -> Outcome:
    try:
        tools.record_consent(ctx, {"consent": False, "notice_version": NOTICE_VERSION})
    except ToolError as e:
        if e.error_code != "GW-003":
            raise
    return DECLINED, True, TEXT_DECLINED, "consent_declined"


def _on_start(ctx, session, text, norm) -> Outcome:
    return CONSENT, True, _welcome(ctx), "journey_started"


def _on_consent(ctx, session, text, norm) -> Outcome:
    if _is_no(norm):
        return _decline_consent(ctx)
    if _is_yes(norm):
        return _accept_consent(ctx)
    return CONSENT, False, TEXT_CONSENT_REPROMPT, "unrecognised"


def _on_declined(ctx, session, text, norm) -> Outcome:
    if _is_yes(norm):
        return _accept_consent(ctx)
    return DECLINED, _is_no(norm), TEXT_DECLINED, "consent_still_declined"


def _otp_send_error(e: ToolError, state: str) -> Outcome:
    if e.error_code == "GW-010":
        return state, True, TEXT_OTP_SEND_LIMIT, "email_otp_send_limit"
    if e.error_code == "GW-013":
        return LOCKED, True, TEXT_OTP_LOCKED, "email_otp_locked"
    raise e


def _on_email(ctx, session, text, norm) -> Outcome:
    email_id = _extract_email(text)
    if not email_id:
        return EMAIL, False, TEXT_INVALID_EMAIL, "email_invalid"
    try:
        payload, event = tools.send_email_otp(ctx, {"email_id": email_id})
    except ToolError as e:
        if e.error_code == "GW-004":
            return EMAIL, False, TEXT_INVALID_EMAIL, "email_invalid"
        return _otp_send_error(e, EMAIL)
    message = TEXT_OTP_SENT.format(email_id=payload["email_id"], expires_in=payload["expires_in_seconds"])
    return OTP, True, message, event


def _on_otp(ctx, session, text, norm) -> Outcome:
    code = _extract_code(text)
    if code:
        try:
            tools.verify_email_otp(ctx, {"otp_no": code})
        except ToolError as e:
            if e.error_code == "GW-011":
                return OTP, True, TEXT_OTP_WRONG, "email_otp_wrong"
            if e.error_code == "GW-012":
                return OTP, True, TEXT_OTP_EXPIRED, "email_otp_expired"
            if e.error_code == "GW-013":
                return LOCKED, True, TEXT_OTP_LOCKED, "email_otp_locked"
            raise
        del code
        payload, _ = tools.create_password_link(ctx, {})
        return PASSWORD, True, payload["message_for_customer"], "email_verified"

    if norm in RESEND_PHRASES or "resend" in _words(norm):
        try:
            payload, _ = tools.send_email_otp(ctx, {"resend": True})
        except ToolError as e:
            return _otp_send_error(e, OTP)
        message = TEXT_OTP_RESENT.format(email_id=payload["email_id"], expires_in=payload["expires_in_seconds"])
        return OTP, True, message, "email_otp_resent"

    return OTP, False, TEXT_OTP_REPROMPT.format(email_id=session.email_otp_email_id), "unrecognised"


def _password_done(ctx: ToolContext) -> Outcome:
    try:
        tools.check_password_status(ctx, {})
    except ToolError as e:
        if e.error_code == "GW-021":
            return PASSWORD, True, TEXT_PASSWORD_PENDING, "password_pending"
        if e.error_code == "GW-022":
            return PASSWORD, True, TEXT_PASSWORD_LINK_EXPIRED, "password_link_expired"
        raise
    return KYC_CHOICE, True, TEXT_PASSWORD_SET, "password_confirmed"


def _password_new_link(ctx: ToolContext) -> Outcome:
    payload, event = tools.create_password_link(ctx, {})
    return PASSWORD, True, payload["message_for_customer"], event


def _on_password(ctx, session, text, norm) -> Outcome:
    if norm in NEW_LINK_PHRASES:
        return _password_new_link(ctx)
    if norm in DONE_PHRASES:
        return _password_done(ctx)
    if _looks_like_password(text):
        # Not stored, not logged (the audit redacts customer_message), not used.
        return PASSWORD, True, TEXT_PASSWORD_IN_CHAT, "password_in_chat_ignored"
    words = _words(norm)
    if "new link" in norm or "resend" in words:
        return _password_new_link(ctx)
    if "done" in words or "finished" in words:
        return _password_done(ctx)
    return PASSWORD, False, TEXT_PASSWORD_REPROMPT, "unrecognised"


def _on_kyc_choice(ctx, session, text, norm) -> Outcome:
    choice = _kyc_choice(norm)
    if choice == "now":
        _, event = tools.start_kyc(ctx, {})
        return KYC_DOC, True, TEXT_KYC_DOC, event
    if choice == "later":
        _, event = tools.defer_kyc(ctx, {})
        return KYC_DEFERRED, True, TEXT_KYC_DEFERRED, event
    return KYC_CHOICE, False, TEXT_KYC_REPROMPT, "unrecognised"


def _on_kyc_deferred(ctx, session, text, norm) -> Outcome:
    return KYC_CHOICE, True, TEXT_KYC_QUESTION, "kyc_choice_reopened"


def _on_kyc_doc(ctx, session, text, norm) -> Outcome:
    return KYC_DOC, True, TEXT_KYC_DOC_PLACEHOLDER, "kyc_doc_placeholder"


def _on_locked(ctx, session, text, norm) -> Outcome:
    return LOCKED, True, TEXT_OTP_LOCKED, "journey_locked"


HANDLERS = {
    START: _on_start,
    CONSENT: _on_consent,
    DECLINED: _on_declined,
    EMAIL: _on_email,
    OTP: _on_otp,
    LOCKED: _on_locked,
    PASSWORD: _on_password,
    KYC_CHOICE: _on_kyc_choice,
    KYC_DEFERRED: _on_kyc_deferred,
    KYC_DOC: _on_kyc_doc,
}


def _result(state: str, recognised: bool, message_for_customer: str) -> dict:
    return {
        "ok": True,
        "state": state,
        "recognised": recognised,
        "message_for_agent": AGENT_RECOGNISED if recognised else AGENT_UNRECOGNISED,
        "message_for_customer": message_for_customer,
    }


def _turn(ctx: ToolContext, text: str) -> tuple[dict, str, str]:
    """Process one message. Returns (result, audit event, state before the turn)."""
    norm = _normalise(text)
    session = ctx.store.get(ctx.conversation_id)

    restarted = False
    if session is not None and norm == "restart":
        ctx.store.delete(ctx.conversation_id)
        session, restarted = None, True

    if session is None:
        # First message, expired session or RESTART: start again.
        session = tools._new_session(ctx)
        session.journey_state = CONSENT
        ctx.store.save(session)
        return _result(CONSENT, True, _welcome(ctx)), "journey_restarted" if restarted else "journey_started", START

    from_state = session.journey_state
    if norm in GREETINGS and from_state not in (START, KYC_DEFERRED):
        outcome: Outcome = (from_state, True, _prompt(ctx, session), "prompt_repeated")
    else:
        outcome = HANDLERS[from_state](ctx, session, text, norm)
    to_state, recognised, message, event = outcome

    # The tool handlers save their own copy of the session, so reload before moving on.
    session = ctx.store.get(ctx.conversation_id)
    session.journey_state = to_state
    ctx.store.save(session)
    return _result(to_state, recognised, message), event, from_state


def journey_turn(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    message: Any = args.get("customer_message")
    if not isinstance(message, str) or len(message) > MAX_MESSAGE_LENGTH:
        raise tools._validation_error(
            f"customer_message must be a string of at most {MAX_MESSAGE_LENGTH} characters.",
            "Pass the customer's message as customer_message.",
        )
    text = message.strip()

    cache = ctx.turn_cache
    if cache is None:
        result, event, from_state = _turn(ctx, text)
        ctx.audit_extra.update(from_state=from_state, to_state=result["state"])
        return result, event

    with cache.lock:
        now = clock.now()
        digest = cache.digest(text)
        previous = cache.get(ctx.conversation_id, digest, now)
        if previous is not None:
            # Same message again within 15 s (e.g. a retry): no side effects.
            ctx.audit_extra.update(from_state=previous["state"], to_state=previous["state"])
            return previous, "journey_duplicate_ignored"
        result, event, from_state = _turn(ctx, text)
        cache.put(ctx.conversation_id, digest, now, result)
    ctx.audit_extra.update(from_state=from_state, to_state=result["state"])
    return result, event
