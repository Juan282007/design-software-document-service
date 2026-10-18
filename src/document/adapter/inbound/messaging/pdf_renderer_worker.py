"""Composition of the pdf-renderer-worker: configuration and wiring of ports and adapters."""

from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from document.adapter.inbound.messaging.generation_consumer import GenerationQueueConsumer
from document.adapter.outbound.messaging.rabbitmq_event_publisher import (
    DEFAULT_AMQP_URL,
    DEFAULT_DLQ,
    DEFAULT_EXCHANGE,
    RabbitMqDeadLetterPublisher,
    RabbitMqEventPublisher,
)
from document.adapter.outbound.observability.prometheus_render_metrics import (
    PrometheusRenderMetrics,
    configure_logging,
)
from document.adapter.outbound.persistence.document_repository import SqlAlchemyDocumentRepository
from document.adapter.outbound.persistence.template_repository import SqlAlchemyTemplateRepository
from document.adapter.outbound.rendering.pybars_template_engine import PybarsTemplateEngine
from document.adapter.outbound.rendering.xhtml2pdf_renderer import Xhtml2PdfRenderer
from document.adapter.outbound.storage.s3_object_storage import S3ObjectStorage
from document.application.port.outbound.event_publisher import DeadLetterPublisher, EventPublisher
from document.application.port.outbound.object_storage import ObjectStorage
from document.application.port.outbound.pdf_renderer import PdfRenderer, TemplateEngine
from document.application.port.outbound.render_metrics import RenderMetrics
from document.application.usecase.handle_generation_message import GenerationMessageHandler
from document.application.usecase.render_document import RenderDocumentService, RenderOutcome, RenderRequest

logger = logging.getLogger("pdf_renderer_worker")


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    amqp_url: str = DEFAULT_AMQP_URL
    generation_queue: str = "document-generation-queue"
    dlq_queue: str = DEFAULT_DLQ
    events_exchange: str = DEFAULT_EXCHANGE
    max_retries: int = 3
    backoff_seconds: float = 1.0
    metrics_port: int = 9108
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> WorkerConfig:
        database_url = os.getenv("DB_URL") or os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DB_URL debe configurarse para pdf-renderer-worker")
        return cls(
            database_url=database_url,
            amqp_url=os.getenv("AMQP_URL", DEFAULT_AMQP_URL),
            generation_queue=os.getenv("GENERATION_QUEUE", "document-generation-queue"),
            dlq_queue=os.getenv("DLQ_QUEUE", DEFAULT_DLQ),
            events_exchange=os.getenv("EVENTS_EXCHANGE", DEFAULT_EXCHANGE),
            max_retries=int(os.getenv("RENDER_MAX_RETRIES", "3")),
            backoff_seconds=float(os.getenv("RENDER_BACKOFF_SECONDS", "1.0")),
            metrics_port=int(os.getenv("METRICS_PORT", "9108")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )


def build_handler(
    config: WorkerConfig,
    *,
    engine: Engine | None = None,
    template_engine: TemplateEngine | None = None,
    pdf_renderer: PdfRenderer | None = None,
    storage: ObjectStorage | None = None,
    events: EventPublisher | None = None,
    dead_letters: DeadLetterPublisher | None = None,
    metrics: RenderMetrics | None = None,
    sleep=None,
) -> GenerationMessageHandler:
    """Wire the handler; every collaborator can be replaced so tests exercise the real flow with fakes."""
    session_factory = sessionmaker(
        bind=engine or create_engine(config.database_url, pool_pre_ping=True), autoflush=False, expire_on_commit=False
    )
    engine_port = template_engine or PybarsTemplateEngine()
    renderer = pdf_renderer or Xhtml2PdfRenderer()
    object_storage = storage or S3ObjectStorage()
    publisher = events or RabbitMqEventPublisher(config.amqp_url, config.events_exchange)

    def service(session) -> RenderDocumentService:
        return RenderDocumentService(
            SqlAlchemyDocumentRepository(session),
            SqlAlchemyTemplateRepository(session),
            engine_port,
            renderer,
            object_storage,
            publisher,
        )

    def process(request: RenderRequest) -> RenderOutcome:
        with session_factory() as session:
            return service(session).process(request)

    def mark_failed(request: RenderRequest) -> None:
        with session_factory() as session:
            service(session).mark_failed(request)

    kwargs = {} if sleep is None else {"sleep": sleep}
    return GenerationMessageHandler(
        process,
        mark_failed,
        dead_letters or RabbitMqDeadLetterPublisher(config.amqp_url, config.dlq_queue),
        metrics,
        max_retries=config.max_retries,
        backoff_seconds=config.backoff_seconds,
        **kwargs,
    )


def run_worker(config: WorkerConfig | None = None) -> None:
    config = config or WorkerConfig.from_env()
    configure_logging(config.log_level)
    metrics = PrometheusRenderMetrics()
    metrics.serve(config.metrics_port)
    handler = build_handler(config, metrics=metrics)
    consumer = GenerationQueueConsumer(
        handler, config.amqp_url, config.generation_queue, on_queue_depth=metrics.queue_depth
    )
    stop = threading.Event()
    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), lambda *_: stop.set())
    logger.info(
        "pdf-renderer-worker started",
        extra={"queue": config.generation_queue, "dlq": config.dlq_queue, "metrics_port": config.metrics_port},
    )
    consumer.run(stop)
    logger.info("pdf-renderer-worker stopped")
