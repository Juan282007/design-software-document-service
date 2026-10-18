"""Outbound port that publishes generation requests to the broker."""

from __future__ import annotations

from typing import Protocol

from document.domain.model.document import GenerationRequest


class DocumentGenerationPublisher(Protocol):
    def publish(self, request: GenerationRequest) -> None:
        """Publish the request; raise GenerationQueueUnavailableError if the broker rejects it."""
