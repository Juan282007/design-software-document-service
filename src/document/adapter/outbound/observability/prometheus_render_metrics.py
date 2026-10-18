"""Operational metrics (Prometheus) and structured logs of the pdf-renderer-worker."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

_STANDARD_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class PrometheusRenderMetrics:
    """Backlog and render errors can be diagnosed from these series (see the runbook alerts)."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self._messages = Counter(
            "document_render_messages_total",
            "Generation messages handled, by outcome (success, duplicate, failed).",
            ["outcome"],
            registry=self.registry,
        )
        self._duration = Histogram(
            "document_render_duration_seconds",
            "Time taken to settle a generation message, including retries.",
            registry=self.registry,
        )
        self._retries = Counter("document_render_retries_total", "Render attempts retried.", registry=self.registry)
        self._dlq = Counter(
            "document_render_dead_lettered_total", "Messages sent to the DLQ.", registry=self.registry
        )
        self._queue_depth = Gauge(
            "document_generation_queue_depth", "Messages waiting in document-generation-queue.", registry=self.registry
        )

    def processed(self, outcome: str, seconds: float) -> None:
        self._messages.labels(outcome=outcome).inc()
        self._duration.observe(seconds)

    def retried(self) -> None:
        self._retries.inc()

    def dead_lettered(self) -> None:
        self._dlq.inc()

    def queue_depth(self, depth: int) -> None:
        self._queue_depth.set(depth)

    def serve(self, port: int) -> None:
        start_http_server(port, registry=self.registry)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({key: value for key, value in record.__dict__.items() if key not in _STANDARD_RECORD_FIELDS})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    logging.getLogger("pika").setLevel(logging.WARNING)
