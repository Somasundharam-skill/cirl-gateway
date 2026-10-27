"""Masked audit trail: one JSON log line per tool call plus an in-memory buffer."""

import json
import logging
import threading
from collections import deque
from typing import Any

from app import clock
from app.masking import mask_payload

logger = logging.getLogger("cirl_gateway.audit")


class AuditLog:
    def __init__(self, maxlen: int = 1000):
        self._entries: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        conversation_id: str,
        tool: str,
        tool_input: dict[str, Any],
        tool_output: dict[str, Any],
        ok: bool,
        error_code: str | None,
        event: str | None = None,
    ) -> dict:
        entry = {
            "timestamp": clock.iso(),
            "conversation_id": conversation_id,
            "tool": tool,
            "event": event,
            "input": mask_payload(tool_input),
            "output": mask_payload(tool_output),
            "ok": ok,
            "error_code": error_code,
        }
        with self._lock:
            self._entries.append(entry)
        logger.info(json.dumps(entry, separators=(",", ":")))
        return entry

    def latest(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._entries)[-n:]
