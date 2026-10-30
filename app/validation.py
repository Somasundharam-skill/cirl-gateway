"""Input formats and normalisation."""

import re

MOBILE_RE = re.compile(r"^[0-9]{10}$")
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
