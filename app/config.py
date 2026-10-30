"""Environment-driven settings."""

import os
from dataclasses import dataclass, field

from app.validation import normalise_phone

# One flag per CIRL API: CIRL_MODE_<NAME>=mock|real
CIRL_APIS = ("SEND_OTP", "VERIFY_OTP", "GET_LIST_OF_POLICY")
CIRL_MODES = ("mock", "real")


@dataclass(frozen=True)
class Settings:
    gateway_api_key: str
    session_ttl_minutes: int = 60
    log_level: str = "INFO"
    cirl_modes: dict[str, str] = field(default_factory=dict)
    employee_directory_path: str = "data/employees.csv"
    test_phone: str | None = None  # +91XXXXXXXXXX; web-chat testing only, never in production

    def cirl_mode(self, api: str) -> str:
        return self.cirl_modes.get(api, "mock")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    modes = {}
    for api in CIRL_APIS:
        mode = (env.get(f"CIRL_MODE_{api}", "").strip() or "mock").lower()
        if mode not in CIRL_MODES:
            raise ValueError(f"CIRL_MODE_{api} must be one of {CIRL_MODES}, got {mode!r}")
        modes[api] = mode

    ttl = int(env.get("SESSION_TTL_MINUTES", "").strip() or "60")
    if ttl <= 0:
        raise ValueError("SESSION_TTL_MINUTES must be positive")

    raw_test_phone = env.get("TEST_PHONE", "").strip()
    test_phone = normalise_phone(raw_test_phone) if raw_test_phone else None
    if raw_test_phone and test_phone is None:
        raise ValueError("TEST_PHONE must be an Indian mobile number, e.g. +917373345820")

    return Settings(
        gateway_api_key=env.get("GATEWAY_API_KEY", ""),
        session_ttl_minutes=ttl,
        log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
        cirl_modes=modes,
        employee_directory_path=env.get("EMPLOYEE_DIRECTORY_PATH", "").strip() or "data/employees.csv",
        test_phone=test_phone,
    )
