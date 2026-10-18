"""Message handling policy of the pdf-renderer-worker: parse, retry with backoff and dead-letter."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from uuid import UUID

from document.application.port.outbound.event_publisher import DeadLetterPublisher
from document.application.port.outbound.render_metrics import NoopRenderMetrics, RenderMetrics
from document.application.usecase.render_document import RenderOutcome, RenderRequest
from document.domain.model.document import NonRetryableRenderError

logger = logging.getLogger("pdf_renderer_worker")

OUTCOME_SUCCESS = "success"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_FAILED = "failed"


def parse_request(body: bytes) -> RenderRequest:
    try:
        message = json.loads(body)
        data_payload = message.get("data_payload") or {}
        if not isinstance(data_payload, dict):
            raise ValueError("data_payload debe ser un objeto")
        template_code = message["template_code"]
        correlation_id = message["correlation_id"]
        if not isinstance(template_code, str) or not template_code or not isinstance(correlation_id, str) or not correlation_id:
            raise ValueError("template_code y correlation_id deben ser texto no vacío")
        return RenderRequest(
            document_id=UUID(message["document_id"]),
            template_code=template_code,
            data_payload=data_payload,
            correlation_id=correlation_id,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise NonRetryableRenderError(f"mensaje de generación inválido: {error}") from error


class GenerationMessageHandler:
    """Returns once the message is settled (processed, duplicate or dead-lettered): the caller then acks it."""

    def __init__(
        self,
        process: Callable[[RenderRequest], RenderOutcome],
        mark_failed: Callable[[RenderRequest], None],
        dead_letters: DeadLetterPublisher,
        metrics: RenderMetrics | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._process = process
        self._mark_failed = mark_failed
        self._dead_letters = dead_letters
        self._metrics = metrics or NoopRenderMetrics()
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._sleep = sleep
        self._clock = clock

    def handle(self, body: bytes, sleep: Callable[[float], None] | None = None) -> str:
        """`sleep` lets the caller wait while keeping its broker connection alive (heartbeats)."""
        wait = sleep or self._sleep
        started = self._clock()
        try:
            request = parse_request(body)
        except NonRetryableRenderError as error:
            return self._fail(body, None, str(error), started)

        retries = 0
        while True:
            try:
                outcome = self._process(request)
            except NonRetryableRenderError as error:
                return self._fail(body, request, str(error), started)
            except Exception as error:  # noqa: BLE001 - any transient failure is retried
                if retries >= self._max_retries:
                    return self._fail(body, request, f"reintentos agotados: {error}", started)
                retries += 1
                delay = min(self._backoff * (2 ** (retries - 1)), self._max_backoff)
                self._metrics.retried()
                logger.warning(
                    "render attempt failed, retrying",
                    extra={"document_id": str(request.document_id), "correlation_id": request.correlation_id,
                           "attempt": retries, "delay_seconds": delay, "error": str(error)},
                )
                wait(delay)
                continue
            result = OUTCOME_DUPLICATE if outcome.duplicate else OUTCOME_SUCCESS
            self._metrics.processed(result, self._clock() - started)
            logger.info(
                "generation message processed",
                extra={"document_id": str(request.document_id), "correlation_id": request.correlation_id,
                       "outcome": result, "version_number": outcome.version.version_number},
            )
            return result

    def _fail(self, body: bytes, request: RenderRequest | None, reason: str, started: float) -> str:
        correlation_id = request.correlation_id if request else None
        if request is not None:
            self._mark_failed(request)
        self._dead_letters.send(body, reason, correlation_id)
        self._metrics.dead_lettered()
        self._metrics.processed(OUTCOME_FAILED, self._clock() - started)
        logger.error(
            "generation message sent to the DLQ",
            extra={"document_id": str(request.document_id) if request else None, "correlation_id": correlation_id,
                   "outcome": OUTCOME_FAILED, "reason": reason},
        )
        return OUTCOME_FAILED
