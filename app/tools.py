"""MCP tools for the WhatsApp eIA onboarding demo.

Handlers raise ToolError for business failures; call_tool() turns that into
an MCP result with isError=true and a payload of
{ok:false, error_code, error_msg, message_for_agent}. CIRL is reached only
through app.cirl_client.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from app import cirl_client, clock
from app.audit import AuditLog
from app.config import Settings
from app.masking import mask_phone
from app.sessions import Session, SessionStore
from app.validation import EMAIL_RE, MOBILE_RE, OTP_RE, PAN_RE, UID_RE, normalise_mobile, normalise_phone

logger = logging.getLogger("cirl_gateway.tools")

MAX_OTP_SENDS = 5
MAX_OTP_ATTEMPTS = 3
MAX_EMAIL_OTP_SENDS = 5
MAX_EMAIL_OTP_ATTEMPTS = 3


@dataclass
class ToolContext:
    settings: Settings
    store: SessionStore
    conversation_id: str
    end_user_phone: str | None = None
    end_user_email: str | None = None
    end_user_name: str | None = None
    channel: str | None = None
    auth_callback: str | None = None
    auth_callback_token: str | None = None


class ToolError(Exception):
    def __init__(self, error_code: str, error_msg: str, message_for_agent: str, **extra: Any):
        super().__init__(error_msg)
        self.error_code = error_code
        self.error_msg = error_msg
        self.message_for_agent = message_for_agent
        self.extra = extra

    def payload(self) -> dict:
        return {
            "ok": False,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
            "message_for_agent": self.message_for_agent,
            **self.extra,
        }


def _validation_error(error_msg: str, message_for_agent: str) -> ToolError:
    return ToolError("GW-004", error_msg, message_for_agent)


def _opt_str(args: dict, key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _validation_error(f"{key} must be a string.", f"Ask the customer for their {key} again.")
    value = value.strip()
    return value or None


def _require_session(ctx: ToolContext) -> Session:
    session = ctx.store.get(ctx.conversation_id)
    if session is None:
        raise ToolError(
            "GW-001",
            "No active journey for this conversation.",
            "The journey for this conversation has not started or has expired.",
        )
    return session


def _require_consent(ctx: ToolContext) -> Session:
    session = _require_session(ctx)
    if not session.consent_id:
        raise ToolError(
            "GW-002",
            "Customer consent has not been recorded.",
            "Ask the customer for consent first.",
        )
    return session


def _new_session(ctx: ToolContext) -> Session:
    now = clock.now()
    return Session(
        conversation_id=ctx.conversation_id,
        phone=normalise_phone(ctx.end_user_phone),
        channel=ctx.channel,
        created_at=now,
    )


def _reset_otp(session: Session) -> None:
    session.otp_send_count = 0
    session.otp_sent_at = None
    session.otp_failed_attempts = 0
    session.otp_verified = False


# --- handlers: (ctx, args) -> (result, audit event) -------------------------------------


def start_journey(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    # Idempotent within a conversation: an existing session is returned unchanged.
    # A new chat has a new conversation id, so it starts fresh.
    session = ctx.store.get(ctx.conversation_id)
    event = "journey_already_started"
    if session is None:
        session = _new_session(ctx)
        ctx.store.save(session)
        event = "journey_started"

    return {
        "ok": True,
        "state": session.state,
        "phone": mask_phone(session.phone) if session.phone else None,
        "channel": session.channel,
        "message_for_agent": "Journey started.",
    }, event


def record_consent(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    consent = args.get("consent")
    if not isinstance(consent, bool):
        raise _validation_error(
            "consent must be true or false.", "Ask the customer clearly whether they consent (yes or no)."
        )
    notice_version = _opt_str(args, "notice_version")
    if not notice_version:
        raise _validation_error(
            "notice_version is required.", "Provide the version of the privacy notice that was shown to the customer."
        )

    session = ctx.store.get(ctx.conversation_id) or _new_session(ctx)

    if not consent:
        # Refusal (or withdrawal) clears consent and everything that depended on it.
        session.consent_id = session.consent_at = session.notice_version = None
        session.client_trxn_id = session.eia_exists = None
        _reset_otp(session)
        session.state = "CONSENT_REFUSED"
        ctx.store.save(session)
        raise ToolError(
            "GW-003",
            "Customer did not give consent.",
            "The customer declined consent. Explain that onboarding cannot continue without it and ask whether "
            "they would like to consent.",
        )

    if session.consent_id:
        # Idempotent: consent already recorded, return the existing record unchanged.
        return {
            "ok": True,
            "consent_id": session.consent_id,
            "consent_at": session.consent_at,
            "notice_version": session.notice_version,
            "message_for_agent": "Consent recorded.",
        }, "consent_already_recorded"

    session.consent_id = "CNS-" + uuid.uuid4().hex[:16].upper()
    session.consent_at = clock.iso()
    session.notice_version = notice_version
    if session.state in ("STARTED", "CONSENT_REFUSED"):
        session.state = "CONSENTED"
    ctx.store.save(session)
    return {
        "ok": True,
        "consent_id": session.consent_id,
        "consent_at": session.consent_at,
        "notice_version": notice_version,
        "message_for_agent": "Consent recorded.",
    }, "consent_recorded"


def check_existing_eia(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    session = _require_consent(ctx)

    pan = _opt_str(args, "pan")
    uid = _opt_str(args, "uid")
    if bool(pan) == bool(uid):
        raise _validation_error(
            "Provide exactly one of pan or uid.", "Ask the customer for either their PAN or their Aadhaar number (not both)."
        )
    if pan is not None:
        pan = pan.replace(" ", "").upper()
        if not PAN_RE.match(pan):
            raise _validation_error(
                "Invalid PAN format.", "The PAN looks wrong. Ask the customer to re-enter it (format: ABCDE1234F)."
            )
    if uid is not None:
        uid = uid.replace(" ", "").replace("-", "")
        if not UID_RE.match(uid):
            raise _validation_error(
                "Invalid UID format.", "The Aadhaar number must be 12 digits. Ask the customer to re-enter it."
            )

    email_id = _opt_str(args, "email_id")
    if not email_id or not EMAIL_RE.match(email_id):
        raise _validation_error("Invalid email_id.", "Ask the customer for a valid email address.")

    mobile_no = _opt_str(args, "mobile_no")
    if mobile_no is not None:
        mobile_no = normalise_mobile(mobile_no)
    elif session.phone:
        mobile_no = session.phone[-10:]
    if not mobile_no or not MOBILE_RE.match(mobile_no):
        raise _validation_error("Invalid mobile_no.", "Ask the customer for their 10-digit mobile number.")

    trxn_no = cirl_client.new_trxn_no()
    response = cirl_client.verify_eia_account(
        ctx.settings, pan=pan, uid=uid, email_id=email_id, mobile_no=mobile_no, trxn_no=trxn_no
    )
    # PAN/UID are used for this single request only and never stored.
    del pan, uid

    exists = response.get("status") == "true"
    session.client_trxn_id = trxn_no
    session.eia_exists = exists
    session.email_id = email_id
    session.mobile_no = mobile_no
    _reset_otp(session)
    session.state = "EIA_CHECKED"
    ctx.store.save(session)

    return {
        "ok": True,
        "eia_exists": exists,
        "client_trxn_id": trxn_no,
        "cirl_response": response,
        "message_for_agent": "An eIA already exists for these details." if exists else "No eIA exists for these details.",
    }, "eia_checked"


def send_otp(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    session = _require_consent(ctx)
    if not session.client_trxn_id:
        raise ToolError(
            "GW-005",
            "No eIA check has been done in this journey.",
            "The customer's eIA details have not been checked yet.",
        )
    if session.otp_send_count >= MAX_OTP_SENDS:
        raise ToolError(
            "E-203",
            "Maximum OTP send limit reached.",
            "Tell the customer that no more OTPs can be sent in this conversation and they should try again later.",
        )

    response = cirl_client.send_otp(
        ctx.settings, trxn_no=session.client_trxn_id, email_id=session.email_id, mobile_no=session.mobile_no
    )

    session.otp_send_count += 1
    session.otp_sent_at = clock.now()
    session.otp_failed_attempts = 0
    session.otp_verified = False
    session.state = "OTP_SENT"
    ctx.store.save(session)
    return {
        "ok": True,
        "sends_remaining": MAX_OTP_SENDS - session.otp_send_count,
        "cirl_response": response,
        "message_for_agent": f"OTP sent to {response['masked_mobile_no']} and {response['masked_email_id']}. "
        f"It is valid for {response['otp_expire_time'] // 60} minutes.",
    }, "otp_sent"


def verify_otp(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    otp_no = _opt_str(args, "otp_no")
    if not otp_no or not OTP_RE.match(otp_no):
        raise _validation_error("otp_no must be 6 digits.", "Ask the customer to enter the 6-digit OTP they received.")

    session = _require_consent(ctx)
    if session.otp_sent_at is None:
        raise ToolError("GW-006", "No OTP has been sent.", "No OTP has been sent to the customer yet.")
    if session.otp_verified:
        return {"ok": True, "otp_verified": True, "message_for_agent": "OTP already verified."}, "otp_verified"

    sends_left = MAX_OTP_SENDS - session.otp_send_count
    resend_hint = (
        f" Offer to send a new OTP ({sends_left} left)." if sends_left > 0
        else " No more OTPs can be sent in this conversation."
    )
    if session.otp_failed_attempts >= MAX_OTP_ATTEMPTS:
        raise ToolError("E-304", "OTP verification locked.", "Too many wrong OTPs." + resend_hint)
    if clock.now() - session.otp_sent_at > cirl_client.OTP_EXPIRE_SECONDS:
        raise ToolError("E-303", "OTP has expired.", "Tell the customer the OTP has expired." + resend_hint)

    response = cirl_client.verify_otp(ctx.settings, trxn_no=session.client_trxn_id, otp_no=otp_no)
    del otp_no

    if response.get("status") != "true":
        session.otp_failed_attempts += 1
        ctx.store.save(session)
        remaining = MAX_OTP_ATTEMPTS - session.otp_failed_attempts
        if remaining <= 0:
            raise ToolError(
                "E-304",
                "OTP verification locked after 3 wrong attempts.",
                "Tell the customer the OTP is locked after 3 wrong attempts." + resend_hint,
            )
        raise ToolError(
            "E-302",
            "Invalid OTP.",
            f"The OTP is wrong. Ask the customer to check and re-enter it ({remaining} attempt(s) left).",
            attempts_remaining=remaining,
        )

    session.otp_verified = True
    session.state = "OTP_VERIFIED"
    ctx.store.save(session)
    # TODO: notify Perfox that the customer is authenticated by POSTing to the
    # auth-context callback (ctx.auth_callback, X-Sa-Auth-Callback) with
    # ctx.auth_callback_token (X-Sa-Auth-Callback-Token).
    return {
        "ok": True,
        "otp_verified": True,
        "trxn_no": session.client_trxn_id,
        "message_for_agent": "OTP verified. The customer's email and mobile are confirmed.",
    }, "otp_verified"


def _require_email_otp_consent(ctx: ToolContext) -> Session:
    session = ctx.store.get(ctx.conversation_id)
    if session is None or not session.consent_id:
        raise ToolError("GW-002", "Customer consent has not been recorded.", "Ask the customer for consent first.")
    return session


def _email_otp_locked_error() -> ToolError:
    return ToolError(
        "GW-013",
        "Email OTP verification locked after 3 wrong attempts.",
        "Tell the customer that email verification is locked for this conversation after 3 wrong codes.",
    )


def send_email_otp(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    session = _require_email_otp_consent(ctx)
    if session.email_otp_failed_attempts >= MAX_EMAIL_OTP_ATTEMPTS:
        raise _email_otp_locked_error()

    email_id = args.get("email_id")
    email_id = email_id.strip() if isinstance(email_id, str) else None
    if not email_id or not EMAIL_RE.match(email_id):
        raise _validation_error("Invalid email_id.", "Ask the customer for a valid email ID.")

    if session.email_otp_send_count >= MAX_EMAIL_OTP_SENDS:
        raise ToolError(
            "GW-010",
            "Maximum email OTP send limit reached.",
            "Tell the customer that no more email codes can be sent in this conversation and they should try again later.",
        )

    response = cirl_client.send_email_otp(ctx.settings, email_id=email_id)
    expires_in = response["expires_in_seconds"]

    session.email_otp_email_id = email_id
    session.email_otp_send_count += 1
    session.email_otp_expires_at = clock.now() + expires_in
    session.email_verified = False
    ctx.store.save(session)
    return {
        "ok": True,
        "email_id": email_id,
        "expires_in_seconds": expires_in,
        "message_for_agent": f"A code was emailed to the customer. It is valid for {expires_in} seconds.",
    }, "email_otp_sent"


def verify_email_otp(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    session = _require_email_otp_consent(ctx)
    if session.email_otp_expires_at is None:
        raise ToolError(
            "GW-014",
            "No email OTP has been sent.",
            "No email code has been sent to the customer yet.",
        )
    if session.email_verified:
        return {"ok": True, "email_verified": True, "message_for_agent": "Email already verified."}, "email_verified"
    if session.email_otp_failed_attempts >= MAX_EMAIL_OTP_ATTEMPTS:
        raise _email_otp_locked_error()

    otp_no = args.get("otp_no")
    otp_no = otp_no.strip() if isinstance(otp_no, str) else None
    if not otp_no or not OTP_RE.match(otp_no):
        raise _validation_error("otp_no must be 6 digits.", "Ask the customer to enter the 6-digit code from their email.")

    if clock.now() > session.email_otp_expires_at:
        raise ToolError(
            "GW-012",
            "Email OTP has expired.",
            "Tell the customer the code has expired and offer to send a new one.",
        )

    response = cirl_client.verify_email_otp(ctx.settings, email_id=session.email_otp_email_id, otp_no=otp_no)
    del otp_no

    if response.get("status") != "true":
        session.email_otp_failed_attempts += 1
        ctx.store.save(session)
        if session.email_otp_failed_attempts >= MAX_EMAIL_OTP_ATTEMPTS:
            raise _email_otp_locked_error()
        raise ToolError(
            "GW-011",
            "Incorrect email OTP.",
            "Tell the customer the code is incorrect and ask them to re-enter it.",
            attempts_remaining=MAX_EMAIL_OTP_ATTEMPTS - session.email_otp_failed_attempts,
        )

    session.email_verified = True
    ctx.store.save(session)
    return {
        "ok": True,
        "email_verified": True,
        "message_for_agent": "Email verified.",
    }, "email_verified"


# --- registry ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[ToolContext, dict], tuple[dict, str]]


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "start_journey",
            "Starts the eIA onboarding journey for this conversation. If a journey already exists for this "
            "conversation it is returned unchanged, so calling it again is safe. "
            "Call this first, as soon as the customer wants to open or check an eIA.",
            _schema(),
            start_journey,
        ),
        Tool(
            "record_consent",
            "Records whether the customer accepted the privacy notice and returns a consent_id. "
            "If consent is already recorded, the existing consent_id is returned. "
            "Call this after showing the notice and getting a clear yes or no; it fails if consent is false.",
            _schema(
                {
                    "consent": {"type": "boolean", "description": "true only if the customer explicitly agreed."},
                    "notice_version": {"type": "string", "description": "Version of the privacy notice shown, e.g. v1.0."},
                },
                ["consent", "notice_version"],
            ),
            record_consent,
        ),
        Tool(
            "check_existing_eia",
            "Checks with CIRL whether the customer already has an eIA, using their PAN or Aadhaar UID plus email. "
            "Call this after consent is recorded and the customer has given their PAN (or UID) and email. "
            "A 'no eIA' answer is a normal result, not an error.",
            _schema(
                {
                    "pan": {"type": "string", "description": "PAN, e.g. ABCDE1234F. Give either pan or uid."},
                    "uid": {"type": "string", "description": "12-digit Aadhaar number. Give either pan or uid."},
                    "email_id": {"type": "string", "description": "Customer's email address."},
                    "mobile_no": {
                        "type": "string",
                        "description": "10-digit mobile. Omit to use the WhatsApp number of this conversation.",
                    },
                },
                ["email_id"],
            ),
            check_existing_eia,
        ),
        Tool(
            "send_otp",
            "Sends a one-time password to the customer's email and mobile through CIRL. "
            "Call this after check_existing_eia, or when the customer asks for a new OTP. Max 5 per journey.",
            _schema(),
            send_otp,
        ),
        Tool(
            "verify_otp",
            "Verifies the OTP the customer received. "
            "Call this when the customer replies with the 6-digit code after send_otp. Locks after 3 wrong attempts.",
            _schema({"otp_no": {"type": "string", "description": "The 6-digit OTP typed by the customer."}}, ["otp_no"]),
            verify_otp,
        ),
        Tool(
            "send_email_otp",
            "Emails a one-time code to the customer's email address to verify it. "
            "Call this after consent is recorded, once the customer has given their email ID, or when they ask "
            "for a new email code. Max 5 per journey; the code is valid for 120 seconds.",
            _schema({"email_id": {"type": "string", "description": "Customer's email address."}}, ["email_id"]),
            send_email_otp,
        ),
        Tool(
            "verify_email_otp",
            "Verifies the code the customer received by email. "
            "Call this when the customer replies with the 6-digit email code after send_email_otp. "
            "Locks after 3 wrong attempts in total, including across resends.",
            _schema(
                {"otp_no": {"type": "string", "description": "The 6-digit code from the customer's email."}}, ["otp_no"]
            ),
            verify_email_otp,
        ),
    ]
}


def list_tools() -> list[dict]:
    # input_schema as specified for Perfox; inputSchema is the MCP-standard spelling.
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema, "inputSchema": t.input_schema}
        for t in TOOLS.values()
    ]


def call_tool(tool: Tool, args: dict, ctx: ToolContext, audit: AuditLog) -> dict:
    """Run a tool and return the MCP tools/call result. Every call is audited."""
    try:
        payload, event = tool.handler(ctx, args)
        is_error, error_code = False, None
    except ToolError as e:
        payload, event = e.payload(), "tool_failed"
        is_error, error_code = True, e.error_code
    except NotImplementedError:
        payload = ToolError(
            "GW-501",
            "This CIRL service is not available.",
            "Apologise to the customer: the service is temporarily unavailable. Ask them to try again later.",
        ).payload()
        event, is_error, error_code = "tool_failed", True, "GW-501"
        logger.warning("Real CIRL mode is enabled for %s but not implemented", tool.name)

    audit.record(
        conversation_id=ctx.conversation_id,
        tool=tool.name,
        tool_input=args,
        tool_output=payload,
        ok=not is_error,
        error_code=error_code,
        event=event,
    )
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}
