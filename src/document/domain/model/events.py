"""Integration events published by the document-service (standard envelope)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

SOURCE_SERVICE = "document-service"
EVENT_VERSION = "1.0"
DOCUMENT_GENERATED = "document.document.generated"
VERSION_CREATED = "document.version.created"


@dataclass(frozen=True)
class DomainEvent:
    event_id: UUID
    event_type: str
    timestamp: datetime
    correlation_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    version: str = EVENT_VERSION
    source_service: str = SOURCE_SERVICE

    def to_envelope(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type,
            "version": self.version,
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "source_service": self.source_service,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
        }
