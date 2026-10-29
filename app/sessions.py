"""Journey sessions, keyed by Perfox conversation id.

Handlers depend only on SessionStore, so a database-backed store can replace
InMemorySessionStore without touching tool code. get() returns a copy:
changes only persist through save().
"""

import dataclasses
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app import clock


@dataclass
class Session:
    conversation_id: str
    state: str = "STARTED"
    phone: str | None = None  # +91XXXXXXXXXX
    channel: str | None = None
    created_at: float = 0.0
    last_seen_at: float = 0.0

    consent_id: str | None = None
    consent_at: str | None = None
    notice_version: str | None = None

    client_trxn_id: str | None = None
    eia_exists: bool | None = None
    email_id: str | None = None
    mobile_no: str | None = None  # 10 digits

    otp_send_count: int = 0
    otp_sent_at: float | None = None
    otp_failed_attempts: int = 0
    otp_verified: bool = False

    email_otp_email_id: str | None = None
    email_otp_send_count: int = 0
    email_otp_expires_at: float | None = None
    email_otp_failed_attempts: int = 0  # counted across resends
    email_verified: bool = False

    # Password setup. Only SHA-256 of the link token and an argon2 hash of the
    # password are kept; neither the token nor the password is stored.
    password_link_hash: str | None = None
    password_link_expires_at: float | None = None
    password_link_attempts: int = 0
    password_link_used: bool = False
    password_hash: str | None = None
    password_set_at: str | None = None


class SessionStore(ABC):
    @abstractmethod
    def get(self, conversation_id: str) -> Session | None: ...

    @abstractmethod
    def get_by_password_link(self, link_hash: str) -> Session | None:
        """The session whose current password link has this SHA-256 hash."""

    @abstractmethod
    def save(self, session: Session) -> None: ...

    @abstractmethod
    def delete(self, conversation_id: str) -> None: ...


class InMemorySessionStore(SessionStore):
    def __init__(self, ttl_seconds: float):
        self._ttl = ttl_seconds
        self._data: dict[str, Session] = {}
        self._lock = threading.Lock()

    def _expired(self, session: Session, now: float) -> bool:
        return now - session.last_seen_at > self._ttl

    def get(self, conversation_id: str) -> Session | None:
        now = clock.now()
        with self._lock:
            session = self._data.get(conversation_id)
            if session is None:
                return None
            if self._expired(session, now):
                del self._data[conversation_id]
                return None
            return dataclasses.replace(session)

    def get_by_password_link(self, link_hash: str) -> Session | None:
        # Linear scan is fine for an in-memory demo; a database would index this column.
        now = clock.now()
        with self._lock:
            for session in self._data.values():
                if session.password_link_hash == link_hash and not self._expired(session, now):
                    return dataclasses.replace(session)
        return None

    def save(self, session: Session) -> None:
        now = clock.now()
        session.last_seen_at = now
        with self._lock:
            self._data[session.conversation_id] = dataclasses.replace(session)
            for cid in [c for c, s in self._data.items() if self._expired(s, now)]:
                del self._data[cid]

    def delete(self, conversation_id: str) -> None:
        with self._lock:
            self._data.pop(conversation_id, None)
