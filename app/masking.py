"""Masking for audit entries and logs.

mask_payload() masks known sensitive keys, then scrub_text() runs over every
string as a safety net so a PAN, UID, eIA number or email never leaks
through a field we did not anticipate. Values that only the tool knows are
sensitive (policy numbers inside customer text) are redacted by the caller.
"""

import re
from typing import Any

_PAN_ANYWHERE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{5}[0-9]{4}[A-Za-z](?![A-Za-z0-9])")
_TWELVE_DIGITS = re.compile(r"(?<!\d)\d{12}(?!\d)")  # Aadhaar UID
_THIRTEEN_DIGITS = re.compile(r"(?<!\d)\d{13}(?!\d)")  # eIA number
_EMAIL_ANYWHERE = re.compile(r"[^\s@*]+@[^\s@]+\.[A-Za-z]{2,}")


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


def mask_last4(value: Any) -> str:
    """eIA and policy numbers: only the last 4 characters are kept."""
    s = re.sub(r"\s", "", str(value))
    return "X" * max(len(s) - 4, 4) + s[-4:] if s else ""


_KEY_MASKERS = {
    "pan": mask_pan,
    "uid": mask_uid,
    "otp": mask_otp,
    "otp_no": mask_otp,
    "phone": mask_phone,
    "mobile_no": mask_phone,
    "email": mask_email,
    "email_id": mask_email,
    "eia_no": mask_last4,
    "policy_no": mask_last4,
}


def scrub_text(text: str) -> str:
    text = _PAN_ANYWHERE.sub(lambda m: mask_pan(m.group(0)), text)
    text = _EMAIL_ANYWHERE.sub(lambda m: mask_email(m.group(0)), text)
    text = _THIRTEEN_DIGITS.sub(lambda m: mask_last4(m.group(0)), text)
    return _TWELVE_DIGITS.sub(lambda m: mask_uid(m.group(0)), text)


def redact_values(value: Any, replacements: dict[str, str]) -> Any:
    """Replace each sensitive value (e.g. a policy number) wherever it appears in a payload."""
    if not replacements:
        return value
    if isinstance(value, dict):
        return {k: redact_values(v, replacements) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_values(v, replacements) for v in value]
    if isinstance(value, str):
        for secret in sorted(replacements, key=len, reverse=True):  # longest first
            value = value.replace(secret, replacements[secret])
    return value


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
