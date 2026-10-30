"""The only module that talks to CIRL. One function per CIRL API.

Each function checks its CIRL_MODE_* flag: "mock" returns canned data shaped
like the CIRL response, "real" is not implemented yet and raises
NotImplementedError. Nothing here makes network calls.

The mock keeps the OTP state that CIRL would keep on its side (send count,
expiry, wrong attempts, lock), per eIA number, in process memory.
"""

import secrets
import threading
from dataclasses import dataclass

from app import clock
from app.config import Settings
from app.masking import mask_email, mask_mobile_cirl

MOCK_OTP = "123456"
OTP_EXPIRE_SECONDS = 300
MAX_OTP_SENDS = 5  # per eIA in a 24-hour window
MAX_OTP_ATTEMPTS = 3
LOCK_SECONDS = 24 * 60 * 60

# Contact details CIRL holds for each mock eIA (the OTP goes to these).
MOCK_ACCOUNTS = {
    "1921008101111": {"mobile_no": "7373345820", "email_id": "ramesh.kumar@example.com"},  # Ramesh
    "1921008102222": {"mobile_no": "9876545820", "email_id": "priya.sharma@example.com"},  # Priya, no policies
}

# getListOfPolicy sample data.
# ASSUMED SHAPE: field names and sample policies are placeholders until they are
# replaced with the exact response from CIRL's API document. app.tools reads
# only the POLICY_* fields below, so a rename is a change in this file only.
POLICY_LIST = "policy_list"
POLICY_NO = "policy_no"
POLICY_NAME = "policy_name"
INSURER_NAME = "insurer_name"
POLICY_STATUS = "policy_status"
SUM_ASSURED = "sum_assured"
NEXT_PREMIUM_DUE_DATE = "next_premium_due_date"

MOCK_POLICIES = {
    "1921008101111": [
        {
            POLICY_NO: "36521478",
            POLICY_NAME: "LIC's Jeevan Anand",
            INSURER_NAME: "Life Insurance Corporation of India",
            "policy_type": "Life",
            POLICY_STATUS: "Demat",
            SUM_ASSURED: "500000",
            NEXT_PREMIUM_DUE_DATE: "15-01-2027",
        },
        {
            POLICY_NO: "2310987654",
            POLICY_NAME: "",
            INSURER_NAME: "HDFC Life Insurance Company Limited",
            "policy_type": "Life",
            POLICY_STATUS: "In Process",
            SUM_ASSURED: "",
            NEXT_PREMIUM_DUE_DATE: "",
        },
    ],
    "1921008102222": [],
}


def new_trxn_no() -> str:
    """Client transaction number: "C1-" + 17 digits."""
    return "C1-" + "".join(secrets.choice("0123456789") for _ in range(17))


def _real(api: str):
    raise NotImplementedError(f"Real CIRL call for {api} is not implemented")


def _ok(**fields) -> dict:
    return {"status": "true", **fields, "error_code": "", "error_msg": ""}


def _error(code: str, msg: str, **fields) -> dict:
    return {"status": "false", **fields, "error_code": code, "error_msg": msg}


# --- mock CIRL-side OTP state ----------------------------------------------------------------


@dataclass
class _OtpState:
    window_start: float = 0.0
    send_count: int = 0
    trxn_no: str | None = None
    expires_at: float | None = None
    failed_attempts: int = 0
    locked_until: float | None = None


_mock_otp: dict[str, _OtpState] = {}
_mock_lock = threading.Lock()


def reset_mock() -> None:
    """Forget all mock OTP state (tests)."""
    with _mock_lock:
        _mock_otp.clear()


def _otp_state(eia_no: str, now: float) -> _OtpState:
    state = _mock_otp.setdefault(eia_no, _OtpState(window_start=now))
    if now - state.window_start >= LOCK_SECONDS:
        state.window_start, state.send_count = now, 0
    if state.locked_until is not None and now >= state.locked_until:
        state.locked_until, state.failed_attempts = None, 0
    return state


# --- CIRL APIs --------------------------------------------------------------------------------


def send_otp(settings: Settings, *, eia_no: str, trxn_no: str) -> dict:
    """CIRL sendOtp: sends an OTP to the mobile and email registered on the eIA."""
    if settings.cirl_mode("SEND_OTP") == "real":
        _real("sendOtp")

    account = MOCK_ACCOUNTS.get(eia_no)
    if account is None:
        return _error("E-102", "eIA Account does not exist.", trxn_no=trxn_no)
    now = clock.now()
    with _mock_lock:
        state = _otp_state(eia_no, now)
        if state.locked_until is not None:
            return _error("E-304", "OTP verification locked.", trxn_no=trxn_no)
        if state.send_count >= MAX_OTP_SENDS:
            return _error("E-203", "Maximum OTP send limit reached.", trxn_no=trxn_no)
        state.send_count += 1
        state.trxn_no, state.expires_at, state.failed_attempts = trxn_no, now + OTP_EXPIRE_SECONDS, 0

    return _ok(
        masked_email_id=mask_email(account["email_id"]),
        masked_mobile_no=mask_mobile_cirl(account["mobile_no"]),
        otp_start_date=clock.cirl_datetime(now),
        otp_end_date=clock.cirl_datetime(now + OTP_EXPIRE_SECONDS),
        otp_expire_time=OTP_EXPIRE_SECONDS,
        trxn_no=trxn_no,
    )


def verify_otp(settings: Settings, *, eia_no: str, trxn_no: str, otp_no: str) -> dict:
    """CIRL verifyOtp. The mock code is always 123456."""
    if settings.cirl_mode("VERIFY_OTP") == "real":
        _real("verifyOtp")

    now = clock.now()
    with _mock_lock:
        state = _otp_state(eia_no, now)
        if state.locked_until is not None:
            return _error("E-304", "OTP verification locked.", trxn_no=trxn_no)
        if state.trxn_no != trxn_no or state.expires_at is None or now > state.expires_at:
            return _error("E-303", "OTP has expired.", trxn_no=trxn_no)
        if not secrets.compare_digest(otp_no, MOCK_OTP):
            state.failed_attempts += 1
            if state.failed_attempts >= MAX_OTP_ATTEMPTS:
                state.locked_until, state.expires_at = now + LOCK_SECONDS, None
                return _error("E-304", "OTP verification locked.", trxn_no=trxn_no)
            return _error("E-302", "Invalid OTP.", trxn_no=trxn_no)
        state.trxn_no = state.expires_at = None
        state.failed_attempts = 0
    return _ok(trxn_no=trxn_no)


def get_list_of_policy(settings: Settings, *, eia_no: str, trxn_no: str) -> dict:
    """CIRL getListOfPolicy: the policies held in the eIA."""
    if settings.cirl_mode("GET_LIST_OF_POLICY") == "real":
        _real("getListOfPolicy")

    if eia_no not in MOCK_POLICIES:
        return _error("E-102", "eIA Account does not exist.", eia_no=eia_no, trxn_no=trxn_no)
    return _ok(eia_no=eia_no, trxn_no=trxn_no, **{POLICY_LIST: [dict(p) for p in MOCK_POLICIES[eia_no]]})
