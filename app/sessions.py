"""Conversation sessions, keyed by Perfox conversation id.

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
    phone: str | None = None  # +91XXXXXXXXXX
    channel: str | None = None
    eia_no: str | None = None  # from the employee directory
    created_at: float = 0.0
    last_seen_at: float = 0.0

    # Identity check by OTP. The OTP itself is never stored.
    otp_trxn_no: str | None = None
    otp_expires_at: float | None = None
    masked_mobile_no: str | None = None
    masked_email_id: str | None = None
    otp_locked_until: float | None = None
    identity_verified: bool = False


class SessionStore(ABC):
    @abstractmethod
    def get(self, conversation_id: str) -> Session | None: ...

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
