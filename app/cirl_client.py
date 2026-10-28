"""The only module that talks to CIRL. One function per CIRL API.

Each function checks its CIRL_MODE_* flag: "mock" returns canned data shaped
like the CIRL response, "real" is not implemented yet and raises
NotImplementedError. Nothing here makes network calls.
"""

import secrets

from app import clock
from app.config import Settings
from app.masking import mask_email, mask_mobile_cirl

MOCK_EXISTING_PAN = "ABCPE1234E"
MOCK_EXISTING_EIA_NO = "2345678904567"
MOCK_OTP = "123456"
OTP_EXPIRE_SECONDS = 300
EMAIL_OTP_EXPIRE_SECONDS = 120


def new_trxn_no() -> str:
    """Client transaction number: "C1-" + 17 digits."""
    return "C1-" + "".join(secrets.choice("0123456789") for _ in range(17))


def _real(api: str):
    raise NotImplementedError(f"Real CIRL call for {api} is not implemented")


def verify_eia_account(
    settings: Settings,
    *,
    pan: str | None,
    uid: str | None,
    email_id: str,
    mobile_no: str,
    trxn_no: str,
) -> dict:
    """CIRL verifyeIAAccount."""
    if settings.cirl_mode("CHECK_EXISTING_EIA") == "real":
        _real("verifyeIAAccount")

    if pan == MOCK_EXISTING_PAN:
        return {"status": "true", "eia_no": MOCK_EXISTING_EIA_NO, "trxn_no": trxn_no, "error_code": "", "error_msg": ""}
    return {
        "status": "false",
        "eia_no": "",
        "trxn_no": trxn_no,
        "error_code": "E-102",
        "error_msg": "eIA Account does not exist.",
    }


def send_otp(settings: Settings, *, trxn_no: str, email_id: str, mobile_no: str) -> dict:
    """CIRL sendOtp."""
    if settings.cirl_mode("SEND_OTP") == "real":
        _real("sendOtp")

    start = clock.now()
    return {
        "status": "true",
        "masked_email_id": mask_email(email_id),
        "masked_mobile_no": mask_mobile_cirl(mobile_no),
        "otp_start_date": clock.cirl_datetime(start),
        "otp_end_date": clock.cirl_datetime(start + OTP_EXPIRE_SECONDS),
        "otp_expire_time": OTP_EXPIRE_SECONDS,
        "trxn_no": trxn_no,
        "error_code": "",
        "error_msg": "",
    }


def verify_otp(settings: Settings, *, trxn_no: str, otp_no: str) -> dict:
    """CIRL verifyOtp."""
    if settings.cirl_mode("VERIFY_OTP") == "real":
        _real("verifyOtp")

    if secrets.compare_digest(otp_no, MOCK_OTP):
        return {"status": "true", "trxn_no": trxn_no, "error_code": "", "error_msg": ""}
    return {"status": "false", "trxn_no": trxn_no, "error_code": "E-302", "error_msg": "Invalid OTP."}


def send_email_otp(settings: Settings, *, email_id: str) -> dict:
    """CIRL email OTP send. The mock OTP is MOCK_EMAIL_OTP."""
    if settings.cirl_mode("SEND_EMAIL_OTP") == "real":
        _real("sendEmailOtp")

    return {"status": "true", "email_id": mask_email(email_id), "expires_in_seconds": EMAIL_OTP_EXPIRE_SECONDS}


def verify_email_otp(settings: Settings, *, email_id: str, otp_no: str) -> dict:
    """CIRL email OTP verification (same CIRL_MODE_SEND_EMAIL_OTP flag as the send)."""
    if settings.cirl_mode("SEND_EMAIL_OTP") == "real":
        _real("verifyEmailOtp")

    if secrets.compare_digest(otp_no, settings.mock_email_otp):
        return {"status": "true"}
    return {"status": "false"}
