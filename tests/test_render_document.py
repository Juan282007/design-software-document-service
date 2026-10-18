from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from document.adapter.inbound.messaging.pdf_renderer_worker import WorkerConfig, build_handler
from document.adapter.outbound.observability.prometheus_render_metrics import (
    JsonLogFormatter,
    PrometheusRenderMetrics,
)
from document.adapter.outbound.persistence.document_repository import (
    DocumentRecord,
    DocumentVersionRecord,
    SqlAlchemyDocumentRepository,
)
from document.adapter.outbound.persistence.template_repository import Base, SqlAlchemyTemplateRepository
from document.adapter.outbound.rendering.pybars_template_engine import PybarsTemplateEngine
from document.adapter.outbound.rendering.xhtml2pdf_renderer import Xhtml2PdfRenderer
from document.application.usecase.render_document import build_storage_key
from document.domain.model.document import (
    SYSTEM_ACTOR_ID,
    Document,
    NonRetryableRenderError,
    RenderError,
    StorageUnavailableError,
)
from document.domain.model.template import DocumentTemplate

OWNER_ENTITY_ID = UUID("5c1a0000-0000-0000-0000-000000000002")
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
FAKE_PDF = b"%PDF-1.4 fake document"


class FakeRenderer:
    def __init__(self) -> None:
        self.failures = 0
        self.always_fail = False
        self.calls: list[str] = []

    def render(self, html: str) -> bytes:
        self.calls.append(html)
        if self.always_fail or self.failures > 0:
            self.failures = max(0, self.failures - 1)
            raise RenderError("engine down")
        return FAKE_PDF


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail = False

    def put(self, storage_key: str, content: bytes, content_type: str) -> None:
        if self.fail:
            raise StorageUnavailableError("storage down")
        self.objects[storage_key] = (content, content_type)

    def presign_download(self, storage_key: str, expires_in_seconds: int) -> str:
        return "unused"


class FakeEvents:
    def __init__(self) -> None:
        self.events: list = []
        self.fail_next = 0

    def publish(self, event) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RenderError("broker down")
        self.events.append(event)


class FakeDeadLetters:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, str, str | None]] = []

    def send(self, body: bytes, reason: str, correlation_id: str | None) -> None:
        self.sent.append((body, reason, correlation_id))


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    mapped = engine.execution_options(schema_translate_map={"document": None})
    Base.metadata.create_all(mapped)
    return mapped


class Harness:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.renderer = FakeRenderer()
        self.storage = FakeStorage()
        self.events = FakeEvents()
        self.dead_letters = FakeDeadLetters()
        self.sleeps: list[float] = []
        self.metrics = PrometheusRenderMetrics()
        self.handler = build_handler(
            WorkerConfig(database_url="unused", max_retries=3, backoff_seconds=1.0),
            engine=engine,
            template_engine=PybarsTemplateEngine(),
            pdf_renderer=self.renderer,
            storage=self.storage,
            events=self.events,
            dead_letters=self.dead_letters,
            metrics=self.metrics,
            sleep=self.sleeps.append,
        )

    def add_template(self, code="SCHEDULE_CERTIFICATE", output_type="PDF", active=True, body="<h1>{{ name }}</h1>") -> None:
        now = datetime.now(UTC)
        with Session(self.engine) as session:
            SqlAlchemyTemplateRepository(session).save(
                DocumentTemplate(
                    id=uuid4(), code=code, name="Constancia", template_body=body, output_type=output_type,
                    version=1, is_active=active, created_at=now, created_by=ACTOR_ID, updated_at=now, updated_by=ACTOR_ID,
                )
            )

    def add_document(self, status="GENERATING", **overrides) -> Document:
        now = datetime.now(UTC)
        values = dict(
            id=uuid4(), template_id=None, title="Horario.pdf", domain="SCHEDULE", owner_service="scheduling-service",
            owner_entity_id=OWNER_ENTITY_ID, status=status, row_version=0, created_at=now, created_by=ACTOR_ID,
            updated_at=now, updated_by=ACTOR_ID,
        )
        values.update(overrides)
        with Session(self.engine) as session:
            return SqlAlchemyDocumentRepository(session).add(Document(**values))

    def message(self, document: Document, correlation_id="corr-1", **overrides) -> bytes:
        payload = {
            "document_id": str(document.id), "template_code": "SCHEDULE_CERTIFICATE",
            "data_payload": {"name": "Ana"}, "correlation_id": correlation_id,
        }
        payload.update(overrides)
        return json.dumps(payload).encode()

    def document(self, document_id: UUID) -> DocumentRecord:
        with Session(self.engine) as session:
            return session.get(DocumentRecord, document_id)

    def versions(self, document_id: UUID) -> list[DocumentVersionRecord]:
        with Session(self.engine) as session:
            return list(
                session.scalars(
                    select(DocumentVersionRecord)
                    .where(DocumentVersionRecord.document_id == document_id)
                    .order_by(DocumentVersionRecord.version_number)
                )
            )

    def event_types(self) -> list[str]:
        return [event.event_type for event in self.events.events]


@pytest.fixture()
def harness(engine) -> Harness:
    return Harness(engine)


def test_first_generation_stores_the_pdf_marks_available_and_publishes_both_events(harness) -> None:
    harness.add_template()
    document = harness.add_document()

    outcome = harness.handler.handle(harness.message(document))

    assert outcome == "success"
    stored = harness.document(document.id)
    assert stored.status == "AVAILABLE"
    assert stored.mime_type == "application/pdf"
    assert stored.size_bytes == len(FAKE_PDF)
    assert stored.row_version == 1
    assert stored.updated_by == SYSTEM_ACTOR_ID
    expected_key = f"SCHEDULE/{document.created_at:%Y}/{document.created_at:%m}/{OWNER_ENTITY_ID}/{document.id}-v1.pdf"
    assert stored.storage_key == expected_key
    assert harness.storage.objects == {expected_key: (FAKE_PDF, "application/pdf")}
    assert "<h1>Ana</h1>" in harness.renderer.calls[0]
    versions = harness.versions(document.id)
    assert [(v.version_number, v.storage_key, v.correlation_id, v.created_by) for v in versions] == [
        (1, expected_key, "corr-1", SYSTEM_ACTOR_ID)
    ]
    assert harness.event_types() == ["document.document.generated", "document.version.created"]
    generated, created = harness.events.events
    envelope = generated.to_envelope()
    assert set(envelope) == {"event_id", "event_type", "version", "timestamp", "source_service", "correlation_id", "payload"}
    assert envelope["source_service"] == "document-service" and envelope["version"] == "1.0"
    assert envelope["correlation_id"] == "corr-1"
    assert generated.payload["document_id"] == str(document.id)
    assert generated.payload["storage_key"] == expected_key
    assert generated.payload["size_bytes"] == len(FAKE_PDF)
    assert generated.payload["generated_by"] == "pdf-renderer-worker"
    assert created.payload["version_number"] == 1 and created.payload["version_id"] == str(versions[0].id)
    assert harness.dead_letters.sent == []


def test_new_version_points_the_document_to_it_and_only_publishes_version_created(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.handler.handle(harness.message(document, "corr-1"))
    harness.events.events.clear()

    outcome = harness.handler.handle(harness.message(document, "corr-2"))

    assert outcome == "success"
    versions = harness.versions(document.id)
    assert [v.version_number for v in versions] == [1, 2]
    stored = harness.document(document.id)
    assert stored.storage_key == versions[1].storage_key == build_storage_key(document, 2)
    assert stored.status == "AVAILABLE" and stored.row_version == 2
    assert harness.event_types() == ["document.version.created"]
    assert harness.events.events[0].payload["version_number"] == 2
    assert len(harness.storage.objects) == 2


def test_redelivered_message_does_not_render_or_version_again(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    message = harness.message(document, "corr-1")
    harness.handler.handle(message)
    first_ids = [event.event_id for event in harness.events.events]

    outcome = harness.handler.handle(message)

    assert outcome == "duplicate"
    assert len(harness.versions(document.id)) == 1
    assert len(harness.renderer.calls) == 1
    assert len(harness.storage.objects) == 1
    republished_ids = [event.event_id for event in harness.events.events[len(first_ids):]]
    assert republished_ids == first_ids


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param({"template": None}, id="missing-template"),
        pytest.param({"template": {"active": False}}, id="inactive-template"),
        pytest.param({"template": {"output_type": "EXCEL"}}, id="unsupported-output-type"),
        pytest.param({"template": {"body": "{{/if}}"}}, id="invalid-template-syntax"),
        pytest.param({"template": {"body": "{{ unclosed"}}, id="template-renders-empty"),
    ],
)
def test_non_retryable_errors_go_straight_to_the_dlq(harness, setup) -> None:
    if setup["template"] is not None:
        harness.add_template(**setup["template"])
    document = harness.add_document()

    outcome = harness.handler.handle(harness.message(document))

    assert outcome == "failed"
    assert harness.sleeps == []
    assert harness.document(document.id).status == "GENERATION_FAILED"
    assert harness.versions(document.id) == []
    assert harness.storage.objects == {}
    assert "document.document.generated" not in harness.event_types()
    assert len(harness.dead_letters.sent) == 1
    body, reason, correlation_id = harness.dead_letters.sent[0]
    assert json.loads(body)["document_id"] == str(document.id)
    assert correlation_id == "corr-1" and reason


def test_transient_failures_are_retried_with_exponential_backoff(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.renderer.failures = 2

    outcome = harness.handler.handle(harness.message(document))

    assert outcome == "success"
    assert harness.sleeps == [1.0, 2.0]
    assert harness.document(document.id).status == "AVAILABLE"
    assert len(harness.versions(document.id)) == 1
    assert harness.dead_letters.sent == []


def test_the_caller_can_provide_the_function_used_to_wait_between_retries(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.renderer.failures = 1
    caller_sleeps: list[float] = []

    outcome = harness.handler.handle(harness.message(document), sleep=caller_sleeps.append)

    assert outcome == "success"
    assert caller_sleeps == [1.0]
    assert harness.sleeps == []


def test_exhausted_retries_mark_generation_failed_and_dead_letter_the_message(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.renderer.always_fail = True

    outcome = harness.handler.handle(harness.message(document))

    assert outcome == "failed"
    assert harness.sleeps == [1.0, 2.0, 4.0]
    assert len(harness.renderer.calls) == 4
    assert harness.document(document.id).status == "GENERATION_FAILED"
    assert harness.event_types() == []
    assert len(harness.dead_letters.sent) == 1
    assert "reintentos agotados" in harness.dead_letters.sent[0][1]


def test_failed_regeneration_keeps_the_current_version_available(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.handler.handle(harness.message(document, "corr-1"))
    harness.events.events.clear()
    harness.renderer.always_fail = True

    outcome = harness.handler.handle(harness.message(document, "corr-2"))

    assert outcome == "failed"
    stored = harness.document(document.id)
    assert stored.status == "AVAILABLE"
    assert stored.storage_key == build_storage_key(document, 1)
    assert len(harness.versions(document.id)) == 1
    assert harness.event_types() == []
    assert len(harness.dead_letters.sent) == 1


def test_storage_failure_is_retried(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.storage.fail = True

    outcome = harness.handler.handle(harness.message(document))

    assert outcome == "failed"
    assert harness.sleeps == [1.0, 2.0, 4.0]
    assert harness.document(document.id).status == "GENERATION_FAILED"


def test_events_lost_by_a_broker_failure_are_republished_on_retry(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.events.fail_next = 1

    outcome = harness.handler.handle(harness.message(document))

    assert outcome == "duplicate"
    assert harness.sleeps == [1.0]
    assert harness.document(document.id).status == "AVAILABLE"
    assert len(harness.versions(document.id)) == 1
    assert harness.event_types() == ["document.document.generated", "document.version.created"]
    assert harness.dead_letters.sent == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"template_code": "X", "correlation_id": "c"}).encode(),
        json.dumps({"document_id": "not-a-uuid", "template_code": "X", "correlation_id": "c"}).encode(),
        json.dumps({"document_id": str(uuid4()), "template_code": "", "correlation_id": "c"}).encode(),
        json.dumps({"document_id": str(uuid4()), "template_code": "X", "correlation_id": "c", "data_payload": 3}).encode(),
    ],
)
def test_invalid_messages_are_dead_lettered(harness, body) -> None:
    outcome = harness.handler.handle(body)

    assert outcome == "failed"
    assert harness.dead_letters.sent[0][0] == body
    assert harness.sleeps == []


def test_message_for_an_unknown_document_is_dead_lettered(harness) -> None:
    harness.add_template()
    ghost = Document(
        id=uuid4(), template_id=None, title="x", domain="SCHEDULE", owner_service="s", owner_entity_id=OWNER_ENTITY_ID,
        status="GENERATING", row_version=0, created_at=datetime.now(UTC), created_by=ACTOR_ID,
        updated_at=datetime.now(UTC), updated_by=None,
    )

    assert harness.handler.handle(harness.message(ghost)) == "failed"
    assert len(harness.dead_letters.sent) == 1


def test_missing_data_payload_defaults_to_an_empty_object(harness) -> None:
    harness.add_template(body="<p>{{ name }}</p>")
    document = harness.add_document()
    body = json.dumps({"document_id": str(document.id), "template_code": "SCHEDULE_CERTIFICATE", "correlation_id": "c"})

    assert harness.handler.handle(body.encode()) == "success"
    assert harness.renderer.calls == ["<p></p>"]


def test_metrics_count_outcomes_retries_and_dead_letters(harness) -> None:
    harness.add_template()
    document = harness.add_document()
    harness.renderer.failures = 1
    harness.handler.handle(harness.message(document, "ok"))
    harness.handler.handle(harness.message(document, "ok"))
    other = harness.add_document()
    harness.renderer.always_fail = True
    harness.handler.handle(harness.message(other, "bad"))

    registry = harness.metrics.registry
    assert registry.get_sample_value("document_render_messages_total", {"outcome": "success"}) == 1
    assert registry.get_sample_value("document_render_messages_total", {"outcome": "duplicate"}) == 1
    assert registry.get_sample_value("document_render_messages_total", {"outcome": "failed"}) == 1
    assert registry.get_sample_value("document_render_retries_total") == 1 + 3
    assert registry.get_sample_value("document_render_dead_lettered_total") == 1
    assert registry.get_sample_value("document_render_duration_seconds_count") == 3
    harness.metrics.queue_depth(7)
    assert registry.get_sample_value("document_generation_queue_depth") == 7


def test_storage_key_follows_the_documented_convention() -> None:
    created = datetime(2026, 6, 3, tzinfo=UTC)
    document = Document(
        id=UUID("8f3c0000-0000-0000-0000-000000000001"), template_id=None, title="t", domain="FICHA",
        owner_service="s", owner_entity_id=OWNER_ENTITY_ID, status="GENERATING", row_version=0, created_at=created,
        created_by=ACTOR_ID, updated_at=created, updated_by=None,
    )

    assert build_storage_key(document, 3) == f"FICHA/2026/06/{OWNER_ENTITY_ID}/8f3c0000-0000-0000-0000-000000000001-v3.pdf"


def test_handlebars_engine_renders_nested_values_and_blocks() -> None:
    html = PybarsTemplateEngine().render(
        "<p>{{ student.name }}</p>{{#each days}}<li>{{this}}</li>{{/each}}",
        {"student": {"name": "Ana"}, "days": ["Lunes", "Martes"]},
    )

    assert html == "<p>Ana</p><li>Lunes</li><li>Martes</li>"


def test_handlebars_engine_rejects_invalid_syntax() -> None:
    with pytest.raises(NonRetryableRenderError):
        PybarsTemplateEngine().render("{{/if}}", {})


def test_xhtml2pdf_engine_produces_a_real_pdf() -> None:
    pdf = Xhtml2PdfRenderer().render("<html><body><h1>Constancia</h1><p>Hola Ana</p></body></html>")

    assert pdf.startswith(b"%PDF") and len(pdf) > 500


def test_json_log_formatter_includes_the_structured_fields() -> None:
    record = logging.LogRecord("pdf_renderer_worker", logging.ERROR, __file__, 1, "sent to the DLQ", None, None)
    record.document_id = "doc-1"
    record.correlation_id = "corr-1"

    entry = json.loads(JsonLogFormatter().format(record))

    assert entry["level"] == "ERROR" and entry["message"] == "sent to the DLQ"
    assert entry["document_id"] == "doc-1" and entry["correlation_id"] == "corr-1"
    assert entry["timestamp"].endswith("Z")
