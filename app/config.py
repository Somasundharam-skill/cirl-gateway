"""Environment-driven settings."""

import os
import re
from dataclasses import dataclass, field

# One flag per CIRL API: CIRL_MODE_<NAME>=mock|real
CIRL_APIS = ("CHECK_EXISTING_EIA", "SEND_OTP", "VERIFY_OTP", "SEND_EMAIL_OTP")
CIRL_MODES = ("mock", "real")

# Tools listed and callable. For now the journey is registration only; the
# other tools (check_existing_eia, send_otp, verify_otp) stay in the code but
# are disabled unless named in ENABLED_TOOLS.
DEFAULT_ENABLED_TOOLS = (
    "start_journey",
    "record_consent",
    "send_email_otp",
    "verify_email_otp",
    "create_password_link",
    "check_password_status",
    "start_kyc",
    "defer_kyc",
)


@dataclass(frozen=True)
class Settings:
    gateway_api_key: str
    session_ttl_minutes: int = 60
    log_level: str = "INFO"
    cirl_modes: dict[str, str] = field(default_factory=dict)
    mock_email_otp: str = "123456"
    enabled_tools: tuple[str, ...] = DEFAULT_ENABLED_TOOLS
    public_base_url: str = "http://localhost:8000"
    whatsapp_number: str = ""  # digits only

    def cirl_mode(self, api: str) -> str:
        return self.cirl_modes.get(api, "mock")


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    modes = {}
    for api in CIRL_APIS:
        mode = env.get(f"CIRL_MODE_{api}", "mock").strip().lower()
        if mode not in CIRL_MODES:
            raise ValueError(f"CIRL_MODE_{api} must be one of {CIRL_MODES}, got {mode!r}")
        modes[api] = mode

    ttl = int(env.get("SESSION_TTL_MINUTES", "60"))
    if ttl <= 0:
        raise ValueError("SESSION_TTL_MINUTES must be positive")

    mock_email_otp = env.get("MOCK_EMAIL_OTP", "123456").strip()
    if not re.fullmatch(r"[0-9]{6}", mock_email_otp):
        raise ValueError("MOCK_EMAIL_OTP must be 6 digits")

    # Comma-separated; empty or unset means the default. Unknown names are
    # rejected at startup by app.tools.check_enabled_tools.
    enabled = tuple(dict.fromkeys(n.strip() for n in env.get("ENABLED_TOOLS", "").split(",") if n.strip()))

    # Where the customer opens the password page. Render sets RENDER_EXTERNAL_URL itself.
    public_base_url = (
        env.get("PUBLIC_BASE_URL", "").strip() or env.get("RENDER_EXTERNAL_URL", "").strip() or "http://localhost:8000"
    ).rstrip("/")
    whatsapp_number = re.sub(r"\D", "", env.get("WHATSAPP_NUMBER", ""))

    return Settings(
        gateway_api_key=env.get("GATEWAY_API_KEY", ""),
        session_ttl_minutes=ttl,
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        cirl_modes=modes,
        mock_email_otp=mock_email_otp,
        enabled_tools=enabled or DEFAULT_ENABLED_TOOLS,
        public_base_url=public_base_url,
        whatsapp_number=whatsapp_number,
    )
