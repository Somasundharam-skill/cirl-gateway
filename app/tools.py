"""MCP tools for the policy inquiry use case: list_policies and verify_identity_otp.

The customer is identified by their WhatsApp number (X-Sa-End-User-Phone),
looked up in the employee directory to find their eIA number. Policies are
shown only after the customer proves who they are with an OTP sent by CIRL
to the mobile and email registered on the eIA.

Every result is {ok, status, message_for_customer, message_for_agent}.
Business failures are returned with isError=true, plus error_code and
error_msg. CIRL is reached only through app.cirl_client.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app import cirl_client, clock
from app.audit import AuditLog
from app.cirl_client import (
    INSURER_NAME,
    NEXT_PREMIUM_DUE_DATE,
    POLICY_LIST,
    POLICY_NAME,
    POLICY_NO,
    POLICY_STATUS,
    SUM_ASSURED,
)
from app.config import Settings
from app.directory import Employee, EmployeeDirectory
from app.masking import mask_last4, redact_values
from app.sessions import Session, SessionStore
from app.validation import OTP_RE, normalise_phone

logger = logging.getLogger("cirl_gateway.tools")

OTP_LOCK_SECONDS = cirl_client.LOCK_SECONDS

# message_for_customer: exact text the agent sends to the customer.
CUSTOMER_NOT_REGISTERED = (
    "We couldn't find an eIA linked to this WhatsApp number. Please write to cirthelpdesk@cirl.co.in for help."
)
CUSTOMER_OTP_SENT = (
    "To protect your details, we've sent a code to your registered mobile {masked_mobile} and email "
    "{masked_email}. Please enter it here."
)
CUSTOMER_OTP_WRONG = "That code is incorrect. Please re-enter it."
CUSTOMER_OTP_EXPIRED = "That code has expired. Ask me to show your policies again and I'll send a new one."
CUSTOMER_OTP_LOCKED = "Too many incorrect attempts. Please try again after 24 hours."
CUSTOMER_OTP_LIMIT = "You've reached the limit for verification codes. Please try again later."
CUSTOMER_NO_OTP = "Please ask me to show your policies first, and I'll send you a code."
CUSTOMER_NO_POLICIES = "You don't have any policies in your eIA yet."
CUSTOMER_POLICIES_HEADER = "Here are the policies in your eIA:"
CUSTOMER_UNAVAILABLE = "Sorry, this service is temporarily unavailable. Please try again later."

AGENT_SEND_AS_IS = "Send message_for_customer to the customer exactly as given."


@dataclass
class ToolContext:
    settings: Settings
    store: SessionStore
    directory: EmployeeDirectory
    conversation_id: str
    end_user_phone: str | None = None
    channel: str | None = None
    # Sensitive values that appear in this call's output (e.g. policy numbers in the
    # customer text), mapped to their masked form for the audit.
    audit_redactions: dict[str, str] = field(default_factory=dict)


class ToolError(Exception):
    def __init__(
        self,
        error_code: str,
        error_msg: str,
        *,
        status: str,
        message_for_customer: str,
        message_for_agent: str = AGENT_SEND_AS_IS,
        event: str = "tool_failed",
    ):
        super().__init__(error_msg)
        self.error_code = error_code
        self.error_msg = error_msg
        self.status = status
        self.message_for_customer = message_for_customer
        self.message_for_agent = message_for_agent
        self.event = event  # audit event name

    def payload(self) -> dict:
        return {
            "ok": False,
            "status": self.status,
            "error_code": self.error_code,
            "error_msg": self.error_msg,
            "message_for_customer": self.message_for_customer,
            "message_for_agent": self.message_for_agent,
        }


def _result(status: str, message_for_customer: str, message_for_agent: str = AGENT_SEND_AS_IS) -> dict:
    return {
        "ok": True,
        "status": status,
        "message_for_customer": message_for_customer,
        "message_for_agent": message_for_agent,
    }


# --- identity -----------------------------------------------------------------------------------


def _caller_phone(ctx: ToolContext) -> str | None:
    raw = (ctx.end_user_phone or "").strip()
    if not raw and ctx.settings.test_phone:
        return ctx.settings.test_phone  # web-chat testing only
    return normalise_phone(raw)


def _not_registered(event: str = "not_registered") -> ToolError:
    return ToolError(
        "GW-040",
        "No eIA is linked to this WhatsApp number.",
        status="not_registered",
        message_for_customer=CUSTOMER_NOT_REGISTERED,
        event=event,
    )


def _identify(ctx: ToolContext) -> tuple[Session, Employee]:
    """The session for this conversation, tied to the employee behind the WhatsApp number."""
    employee = ctx.directory.lookup(_caller_phone(ctx))
    if employee is None:
        raise _not_registered()

    session = ctx.store.get(ctx.conversation_id)
    if session is None:
        session = Session(conversation_id=ctx.conversation_id, created_at=clock.now())
    if session.eia_no != employee.eia_no:
        # New conversation, or a different number on the same conversation: start the check afresh.
        session = Session(conversation_id=ctx.conversation_id, created_at=session.created_at or clock.now())
        session.eia_no = employee.eia_no
    session.phone = employee.phone
    session.channel = ctx.channel or session.channel
    return session, employee


def _locked(session: Session) -> bool:
    return session.otp_locked_until is not None and clock.now() < session.otp_locked_until


def _locked_error() -> ToolError:
    return ToolError(
        "E-304",
        "OTP verification locked.",
        status="otp_locked",
        message_for_customer=CUSTOMER_OTP_LOCKED,
        event="otp_locked",
    )


def _unavailable(code: str | None, msg: str | None) -> ToolError:
    logger.warning("Unexpected CIRL error %s: %s", code, msg)
    return ToolError(
        code or "GW-502",
        msg or "Unexpected CIRL response.",
        status="unavailable",
        message_for_customer=CUSTOMER_UNAVAILABLE,
        message_for_agent="CIRL returned an unexpected error. " + AGENT_SEND_AS_IS,
    )


def _otp_sent_result(session: Session) -> dict:
    return _result(
        "otp_sent",
        CUSTOMER_OTP_SENT.format(masked_mobile=session.masked_mobile_no, masked_email=session.masked_email_id),
        "A code was sent to the customer's registered mobile and email. Send message_for_customer exactly as "
        "given; when the customer replies with the code, call verify_identity_otp.",
    )


def _send_otp(ctx: ToolContext, session: Session) -> tuple[dict, str]:
    trxn_no = cirl_client.new_trxn_no()
    response = cirl_client.send_otp(ctx.settings, eia_no=session.eia_no, trxn_no=trxn_no)
    if response.get("status") != "true":
        code = response.get("error_code")
        if code == "E-304":
            session.otp_locked_until = clock.now() + OTP_LOCK_SECONDS
            ctx.store.save(session)
            raise _locked_error()
        if code == "E-203":
            raise ToolError(
                "E-203",
                "Maximum OTP send limit reached.",
                status="otp_limit",
                message_for_customer=CUSTOMER_OTP_LIMIT,
            )
        if code == "E-102":
            raise _not_registered("eia_not_found")
        raise _unavailable(code, response.get("error_msg"))

    session.otp_trxn_no = response["trxn_no"]
    session.otp_expires_at = clock.now() + response["otp_expire_time"]
    session.masked_mobile_no = response["masked_mobile_no"]
    session.masked_email_id = response["masked_email_id"]
    ctx.store.save(session)
    return _otp_sent_result(session), "otp_sent"


# --- policy list --------------------------------------------------------------------------------


def _rupees(value: Any) -> str:
    """500000 -> 5,00,000 (Indian digit grouping). Non-numeric values are returned as given."""
    text = str(value).strip().replace(",", "")
    whole, _, paise = text.partition(".")
    if not whole.isdigit():
        return str(value).strip()
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    grouped = ",".join(groups + [tail])
    return grouped + ("." + paise if paise.strip("0") else "")


def _format_policy(policy: dict) -> str:
    number = str(policy.get(POLICY_NO) or "").strip()
    status = str(policy.get(POLICY_STATUS) or "").strip()
    if status.lower() == "in process":
        return f"Policy No: {number}\nStatus: {status}"

    name = str(policy.get(POLICY_NAME) or "").strip() or str(policy.get(INSURER_NAME) or "").strip()
    lines = [f"*{name}*"] if name else []
    lines += [f"Policy No: {number}", f"Status: {status}"]
    if str(policy.get(SUM_ASSURED) or "").strip():
        lines.append(f"Sum Assured: Rs {_rupees(policy[SUM_ASSURED])}")
    if str(policy.get(NEXT_PREMIUM_DUE_DATE) or "").strip():
        lines.append(f"Next Premium Due: {str(policy[NEXT_PREMIUM_DUE_DATE]).strip()}")
    return "\n".join(lines)


def _policies_result(ctx: ToolContext, session: Session) -> dict:
    response = cirl_client.get_list_of_policy(ctx.settings, eia_no=session.eia_no, trxn_no=cirl_client.new_trxn_no())
    if response.get("status") != "true":
        if response.get("error_code") == "E-102":
            raise _not_registered("eia_not_found")
        raise _unavailable(response.get("error_code"), response.get("error_msg"))

    policies = response.get(POLICY_LIST) or []
    if not policies:
        return _result("no_policies", CUSTOMER_NO_POLICIES)
    for policy in policies:
        number = str(policy.get(POLICY_NO) or "").strip()
        if number:
            ctx.audit_redactions[number] = mask_last4(number)
    text = CUSTOMER_POLICIES_HEADER + "\n\n" + "\n\n".join(_format_policy(p) for p in policies)
    return _result("policies", text)


# --- handlers: (ctx, args) -> (result, audit event) --------------------------------------------------


def list_policies(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    session, _ = _identify(ctx)
    ctx.store.save(session)
    if session.identity_verified:
        return _policies_result(ctx, session), "policies_listed"
    if _locked(session):
        raise _locked_error()
    if session.otp_expires_at is not None and clock.now() <= session.otp_expires_at:
        # A code is still valid: don't send another.
        return _otp_sent_result(session), "otp_already_sent"
    return _send_otp(ctx, session)


def verify_identity_otp(ctx: ToolContext, args: dict) -> tuple[dict, str]:
    session, _ = _identify(ctx)
    ctx.store.save(session)
    if session.identity_verified:
        return _policies_result(ctx, session), "policies_listed"
    if _locked(session):
        raise _locked_error()

    otp_no = args.get("otp_no")
    otp_no = "".join(otp_no.split()) if isinstance(otp_no, str) else ""
    if not OTP_RE.match(otp_no):
        # Not 6 digits: ask again without using up an attempt.
        raise ToolError(
            "GW-004",
            "otp_no must be 6 digits.",
            status="otp_incorrect",
            message_for_customer=CUSTOMER_OTP_WRONG,
        )
    if session.otp_trxn_no is None:
        raise ToolError(
            "GW-006",
            "No OTP has been sent in this conversation.",
            status="no_otp",
            message_for_customer=CUSTOMER_NO_OTP,
        )

    response = cirl_client.verify_otp(
        ctx.settings, eia_no=session.eia_no, trxn_no=session.otp_trxn_no, otp_no=otp_no
    )
    del otp_no

    if response.get("status") != "true":
        code = response.get("error_code")
        if code == "E-302":
            raise ToolError(
                "E-302", "Invalid OTP.", status="otp_incorrect", message_for_customer=CUSTOMER_OTP_WRONG
            )
        if code == "E-303":
            session.otp_trxn_no = session.otp_expires_at = None  # the next list_policies sends a new code
            ctx.store.save(session)
            raise ToolError(
                "E-303", "OTP has expired.", status="otp_expired", message_for_customer=CUSTOMER_OTP_EXPIRED
            )
        if code == "E-304":
            session.otp_trxn_no = session.otp_expires_at = None
            session.otp_locked_until = clock.now() + OTP_LOCK_SECONDS
            ctx.store.save(session)
            raise _locked_error()
        raise _unavailable(code, response.get("error_msg"))

    session.identity_verified = True
    session.otp_trxn_no = session.otp_expires_at = None
    ctx.store.save(session)
    return _policies_result(ctx, session), "identity_verified"


# --- registry ---------------------------------------------------------------------------------


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
            "list_policies",
            "Shows the customer the insurance policies in their eIA. Call this when the customer asks about "
            "their policies. The customer is identified by their WhatsApp number. If they haven't verified "
            "their identity in this conversation yet, a code is sent to their registered mobile and email "
            "instead (status otp_sent). Send message_for_customer to the customer exactly as given.",
            _schema(),
            list_policies,
        ),
        Tool(
            "verify_identity_otp",
            "Checks the code the customer received after list_policies, and on success returns their policy "
            "list straight away. Call this when the customer replies with the code. Locks for 24 hours after "
            "3 wrong codes. Send message_for_customer to the customer exactly as given.",
            _schema(
                {"otp_no": {"type": "string", "description": "The 6-digit code the customer typed."}}, ["otp_no"]
            ),
            verify_identity_otp,
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
        payload, event = e.payload(), e.event
        is_error, error_code = True, e.error_code
    except NotImplementedError:
        payload = ToolError(
            "GW-501",
            "This CIRL service is not available.",
            status="unavailable",
            message_for_customer=CUSTOMER_UNAVAILABLE,
        ).payload()
        event, is_error, error_code = "tool_failed", True, "GW-501"
        logger.warning("Real CIRL mode is enabled for %s but not implemented", tool.name)

    audit.record(
        conversation_id=ctx.conversation_id,
        tool=tool.name,
        tool_input=args,
        tool_output=redact_values(payload, ctx.audit_redactions),
        ok=not is_error,
        error_code=error_code,
        event=event,
    )
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}
