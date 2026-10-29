"""Masking for audit entries and logs.

mask_payload() masks known sensitive keys, then scrub_text() runs over every
string as a safety net so a PAN or 12-digit UID never leaks through a field
we did not anticipate.
"""

import re
from typing import Any

_PAN_ANYWHERE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{5}[0-9]{4}[A-Za-z](?![A-Za-z0-9])")
_TWELVE_DIGITS = re.compile(r"(?<!\d)\d{12}(?!\d)")
_EMAIL_ANYWHERE = re.compile(r"[^\s@*]+@[^\s@]+\.[A-Za-z]{2,}")
# Password-link token in any URL: .../set-password?t=<token>
_LINK_TOKEN = re.compile(r"([?&]t=)[A-Za-z0-9_\-]+")


def mask_pan(pan: Any) -> str:
    s = str(pan).strip().upper()
    if len(s) != 10:
        return "XXXXXXXXXX"
    return s[:3] + "XXXX" + s[7:]  # ABCPE1234F -> ABCXXXX34F


def mask_uid(uid: Any) -> str:
    digits = re.sub(r"\D", "", str(uid))
    return "XXXXXXXX" + digits[-4:] if len(digits) >= 4 else "XXXX"


def mask_otp(_: Any) -> str:
    return "******"


def mask_phone(phone: Any) -> str:
    digits = re.sub(r"\D", "", str(phone))
    return "+91XXXXXX" + digits[-4:] if len(digits) >= 4 else "XXXXXXXXXX"


def mask_mobile_cirl(mobile: str) -> str:
    """CIRL sendOtp style: 9876543233 -> 98XXXXXX33."""
    digits = re.sub(r"\D", "", mobile)
    return digits[:2] + "X" * 6 + digits[-2:] if len(digits) >= 4 else "XXXXXXXXXX"


def mask_email(email: Any) -> str:
    local, _, domain = str(email).partition("@")
    if not domain:
        return "*****"
    return local[:2] + "*****@" + domain  # rahul@gmail.com -> ra*****@gmail.com


def mask_account_no(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value))
    return "X" * max(len(digits) - 4, 4) + digits[-4:] if digits else ""


_KEY_MASKERS = {
    "pan": mask_pan,
    "uid": mask_uid,
    "aadhaar": mask_uid,
    "otp": mask_otp,
    "otp_no": mask_otp,
    "password": mask_otp,
    "confirm": mask_otp,
    "token": mask_otp,
    "t": mask_otp,
    "password_hash": mask_otp,
    "phone": mask_phone,
    "mobile_no": mask_phone,
    "email": mask_email,
    "email_id": mask_email,
    "eia_no": mask_account_no,
    # journey_turn input: free text that can contain an OTP, a password or an email
    "customer_message": lambda _: "[REDACTED]",
}


def scrub_text(text: str) -> str:
    text = _LINK_TOKEN.sub(r"\1[REDACTED]", text)
    text = _PAN_ANYWHERE.sub(lambda m: mask_pan(m.group(0)), text)
    text = _EMAIL_ANYWHERE.sub(lambda m: mask_email(m.group(0)), text)
    return _TWELVE_DIGITS.sub(lambda m: mask_uid(m.group(0)), text)


def mask_payload(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: mask_payload(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_payload(v, key) for v in value]
    if value is None or value == "" or isinstance(value, bool):
        return value
    if key is not None and key.lower() in _KEY_MASKERS:
        return _KEY_MASKERS[key.lower()](value)
    if isinstance(value, str):
        return scrub_text(value)
    return value
