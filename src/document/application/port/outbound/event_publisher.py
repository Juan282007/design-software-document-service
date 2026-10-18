"""Outbound ports for integration events and for the dead letter queue."""

from __future__ import annotations

from typing import Protocol

from document.domain.model.events import DomainEvent


class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> None:
        """Publish the event; raise RenderError if the broker does not accept it."""


class DeadLetterPublisher(Protocol):
    def send(self, body: bytes, reason: str, correlation_id: str | None) -> None:
        """Send the original message to the dead letter queue; raise RenderError if it cannot be stored."""
