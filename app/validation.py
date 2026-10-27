"""Input formats and normalisation."""

import re

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
UID_RE = re.compile(r"^[0-9]{12}$")
MOBILE_RE = re.compile(r"^[0-9]{10}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
OTP_RE = re.compile(r"^[0-9]{6}$")


def normalise_phone(raw: str | None) -> str | None:
    """Return +91XXXXXXXXXX, or None if the value is not an Indian mobile number."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not MOBILE_RE.match(digits):
        return None
    return "+91" + digits


def normalise_mobile(raw: str) -> str:
    """Best-effort reduction of a customer-typed mobile to 10 digits (validate afterwards)."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits
