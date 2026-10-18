"""End-to-end test of HU-DOC-004 against PostgreSQL, RabbitMQ and MinIO.

Flow: POST /documents/generate (HU-DOC-002) -> document-generation-queue -> pdf-renderer-worker ->
MinIO + PostgreSQL + document-events -> GET /documents/{id}/download-url (HU-DOC-003).

Run it with TEST_POSTGRES_DB_URL, TEST_AMQP_URL and TEST_S3_ENDPOINT configured (optionally
TEST_S3_ACCESS_KEY / TEST_S3_SECRET_KEY, default minioadmin), after applying the document Liquibase
changelog (including changeset 974). It is skipped otherwise.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import threading
from uuid import UUID, uuid4

import boto3
import httpx
import pika
import pytest
from botocore.client import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from document.adapter.inbound.http.document_api import create_document_app
from document.adapter.inbound.http.security import Principal
from document.adapter.inbound.http.template_api import create_template_app
from document.adapter.inbound.messaging.generation_consumer import GenerationQueueConsumer
from document.adapter.inbound.messaging.pdf_renderer_worker import WorkerConfig, build_handler
from document.adapter.outbound.messaging.document_generation_publisher import RabbitMqDocumentGenerationPublisher
from document.adapter.outbound.rendering.xhtml2pdf_renderer import Xhtml2PdfRenderer
from document.adapter.outbound.storage.s3_object_storage import S3ObjectStorage
from document.domain.model.document import RenderError

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OWNER_ENTITY_ID = "5c1a0000-0000-0000-0000-000000000002"


def _authorizer(_: str):
    def dependency() -> Principal:
        return Principal(
            subject=ACTOR_ID,
            features=frozenset({"DOC_CREATE:GLOBAL", "DOC_VIEW:GLOBAL", "DOC_DOWNLOAD:GLOBAL"}),
        )

    return dependency


class BrokenPdfRenderer:
    def render(self, html: str) -> bytes:
        raise RenderError("pdf engine unavailable")


class FlakyPdfRenderer:
    """Fails once, then renders for real."""

    def __init__(self) -> None:
        self.calls = 0

    def render(self, html: str) -> bytes:
        self.calls += 1
        if self.calls == 1:
            raise RenderError("transient pdf engine failure")
        return Xhtml2PdfRenderer().render(html)


class Infra:
    def __init__(self, database_url: str, amqp_url: str, endpoint: str, access_key: str, secret_key: str) -> None:
        suffix = uuid4().hex[:10]
        self.amqp_url = amqp_url
        self.endpoint = endpoint
        self.queue = f"it-generation-{suffix}"
        self.dlq = f"it-dlq-{suffix}"
        self.exchange = f"it-events-{suffix}"
        self.events_queue = f"it-events-queue-{suffix}"
        self.bucket = f"it-documents-{suffix}"
        self.template_code = f"IT_HU_DOC_004_{suffix.upper()}"
        self.access_key = access_key
        self.secret_key = secret_key
        self.document_ids: list[str] = []
        self.engine = create_engine(database_url, pool_pre_ping=True)
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self.s3.create_bucket(Bucket=self.bucket)
        self.connection = pika.BlockingConnection(pika.URLParameters(amqp_url))
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange=self.exchange, exchange_type="topic", durable=True)
        self.channel.queue_declare(queue=self.queue, durable=True)
        self.channel.queue_declare(queue=self.dlq, durable=True)
        self.channel.queue_declare(queue=self.events_queue, durable=True)
        self.channel.queue_bind(queue=self.events_queue, exchange=self.exchange, routing_key="#")
        self.storage = S3ObjectStorage(
            endpoint_url=endpoint, bucket=self.bucket, access_key=access_key, secret_key=secret_key
        )
        self.documents_app = TestClient(
            create_document_app(
                engine=self.engine,
                authorizer=_authorizer,
                publisher=RabbitMqDocumentGenerationPublisher(amqp_url=amqp_url, queue=self.queue),
                storage=self.storage,
            )
        )
        self.documents_app.__enter__()

    def config(self) -> WorkerConfig:
        return WorkerConfig(
            database_url="unused",
            amqp_url=self.amqp_url,
            generation_queue=self.queue,
            dlq_queue=self.dlq,
            events_exchange=self.exchange,
            max_retries=2,
            backoff_seconds=0.0,
        )

    def create_template(self) -> None:
        with TestClient(create_template_app(engine=self.engine, authorizer=_authorizer)) as client:
            created = client.post(
                "/api/v1/document-templates",
                json={
                    "code": self.template_code,
                    "name": "Plantilla de integración HU-DOC-004",
                    "template_body": "<html><body><h1>Constancia</h1><p>Hola {{ name }}</p></body></html>",
                    "output_type": "PDF",
                },
            )
            assert created.status_code == 201

    def generate(self) -> str:
        response = self.documents_app.post(
            "/api/v1/documents/generate",
            headers={"X-Correlation-Id": f"corr-{uuid4().hex[:8]}"},
            json={
                "template_code": self.template_code,
                "domain": "SCHEDULE",
                "owner_service": "scheduling-service",
                "owner_entity_id": OWNER_ENTITY_ID,
                "title": "Horario integración.pdf",
                "data": {"name": "Ana"},
            },
        )
        assert response.status_code == 202
        document_id = response.json()["document_id"]
        self.document_ids.append(document_id)
        return document_id

    def publish_generation_message(self, document_id: str, correlation_id: str) -> bytes:
        body = json.dumps(
            {
                "document_id": document_id,
                "template_code": self.template_code,
                "data_payload": {"name": "Ana"},
                "correlation_id": correlation_id,
            }
        ).encode()
        self.channel.basic_publish(
            exchange="",
            routing_key=self.queue,
            body=body,
            properties=pika.BasicProperties(delivery_mode=pika.DeliveryMode.Persistent),
        )
        return body

    def run_worker(self, messages: int, **overrides) -> None:
        handler = build_handler(self.config(), engine=self.engine, storage=self.storage, **overrides)
        consumer = GenerationQueueConsumer(handler, self.amqp_url, self.queue)
        stop = threading.Event()
        timer = threading.Timer(60, stop.set)
        timer.start()
        try:
            assert consumer.run(stop, max_messages=messages) == messages
        finally:
            timer.cancel()

    def drain(self, queue: str) -> list[tuple[bytes, pika.BasicProperties]]:
        messages = []
        while True:
            method, properties, body = self.channel.basic_get(queue=queue, auto_ack=True)
            if method is None:
                return messages
            messages.append((body, properties))

    def events(self) -> list[dict]:
        return [json.loads(body) for body, _ in self.drain(self.events_queue)]

    def row(self, document_id: str):
        with self.engine.connect() as db:
            return db.execute(
                text("SELECT status, storage_key, mime_type, size_bytes, row_version FROM document.document WHERE id = :id"),
                {"id": document_id},
            ).one()

    def version_numbers(self, document_id: str) -> list[int]:
        with self.engine.connect() as db:
            rows = db.execute(
                text("SELECT version_number FROM document.document_version WHERE document_id = :id ORDER BY 1"),
                {"id": document_id},
            )
            return [row[0] for row in rows]

    def object_body(self, storage_key: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=storage_key)["Body"].read()

    def close(self) -> None:
        self.documents_app.__exit__(None, None, None)
        with self.engine.begin() as db:
            for document_id in self.document_ids:
                db.execute(text("DELETE FROM document.document_version WHERE document_id = :id"), {"id": document_id})
                db.execute(text("DELETE FROM document.document WHERE id = :id"), {"id": document_id})
            db.execute(text("DELETE FROM document.document_template WHERE code = :code"), {"code": self.template_code})
        for queue in (self.queue, self.dlq, self.events_queue):
            self.channel.queue_delete(queue=queue)
        self.channel.exchange_delete(exchange=self.exchange)
        self.connection.close()
        for item in self.s3.list_objects_v2(Bucket=self.bucket).get("Contents", []):
            self.s3.delete_object(Bucket=self.bucket, Key=item["Key"])
        self.s3.delete_bucket(Bucket=self.bucket)
        self.engine.dispose()


@pytest.fixture()
def infra():
    database_url = os.getenv("TEST_POSTGRES_DB_URL")
    amqp_url = os.getenv("TEST_AMQP_URL")
    endpoint = os.getenv("TEST_S3_ENDPOINT")
    if not database_url or not amqp_url or not endpoint:
        pytest.skip("TEST_POSTGRES_DB_URL, TEST_AMQP_URL y TEST_S3_ENDPOINT no están configuradas")
    resources = Infra(
        database_url,
        amqp_url,
        endpoint,
        os.getenv("TEST_S3_ACCESS_KEY", "minioadmin"),
        os.getenv("TEST_S3_SECRET_KEY", "minioadmin"),
    )
    resources.create_template()
    try:
        yield resources
    finally:
        resources.close()


@pytest.mark.integration
def test_generation_request_becomes_an_available_downloadable_document(infra) -> None:
    document_id = infra.generate()
    assert infra.row(document_id).status == "GENERATING"

    infra.run_worker(1)

    row = infra.row(document_id)
    assert row.status == "AVAILABLE"
    assert row.mime_type == "application/pdf"
    pdf = infra.object_body(row.storage_key)
    assert pdf.startswith(b"%PDF") and len(pdf) == row.size_bytes
    assert infra.version_numbers(document_id) == [1]
    events = infra.events()
    assert [event["event_type"] for event in events] == ["document.document.generated", "document.version.created"]
    for event in events:
        assert set(event) == {"event_id", "event_type", "version", "timestamp", "source_service", "correlation_id", "payload"}
        assert event["source_service"] == "document-service" and event["payload"]["document_id"] == document_id
    assert events[0]["payload"]["storage_key"] == row.storage_key
    assert infra.drain(infra.dlq) == []
    assert infra.drain(infra.queue) == []

    detail = infra.documents_app.get(f"/api/v1/documents/{document_id}").json()
    assert detail["status"] == "AVAILABLE" and detail["mime_type"] == "application/pdf"
    versions = infra.documents_app.get(f"/api/v1/documents/{document_id}/versions").json()
    assert [version["version_number"] for version in versions] == [1]
    url = infra.documents_app.get(f"/api/v1/documents/{document_id}/download-url").json()["url"]
    downloaded = httpx.get(url)
    assert downloaded.status_code == 200 and downloaded.content == pdf


@pytest.mark.integration
def test_new_version_and_redelivered_message(infra) -> None:
    document_id = infra.generate()
    infra.run_worker(1)
    infra.events()
    first_key = infra.row(document_id).storage_key

    infra.publish_generation_message(document_id, "regeneration-1")
    infra.run_worker(1)

    assert infra.version_numbers(document_id) == [1, 2]
    row = infra.row(document_id)
    assert row.storage_key != first_key and row.storage_key.endswith("-v2.pdf")
    assert infra.object_body(first_key).startswith(b"%PDF") and infra.object_body(row.storage_key).startswith(b"%PDF")
    assert [event["event_type"] for event in infra.events()] == ["document.version.created"]

    infra.publish_generation_message(document_id, "regeneration-1")
    infra.run_worker(1)

    assert infra.version_numbers(document_id) == [1, 2]
    assert infra.row(document_id).storage_key == row.storage_key
    assert infra.drain(infra.queue) == []


@pytest.mark.integration
def test_definitive_failure_marks_the_document_failed_and_sends_the_message_to_the_dlq(infra) -> None:
    document_id = infra.generate()

    infra.run_worker(1, pdf_renderer=BrokenPdfRenderer())

    row = infra.row(document_id)
    assert row.status == "GENERATION_FAILED" and row.storage_key is None
    assert infra.version_numbers(document_id) == []
    assert infra.events() == []
    dead = infra.drain(infra.dlq)
    assert len(dead) == 1
    body, properties = dead[0]
    assert json.loads(body)["document_id"] == document_id
    assert "reintentos agotados" in properties.headers["x-failure-reason"]
    assert infra.drain(infra.queue) == []
    blocked = infra.documents_app.get(f"/api/v1/documents/{document_id}/download-url")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "DOCUMENT_NOT_AVAILABLE"


@pytest.mark.integration
def test_long_retry_delay_keeps_the_broker_connection_alive(infra, caplog) -> None:
    document_id = infra.generate()
    config = dataclasses.replace(infra.config(), backoff_seconds=6.0)
    renderer = FlakyPdfRenderer()
    handler = build_handler(config, engine=infra.engine, storage=infra.storage, pdf_renderer=renderer)
    # A 2 second heartbeat: the broker drops a consumer that stays silent for about 4 seconds, so the 6 second
    # wait between attempts only survives if the worker keeps answering heartbeats while it waits.
    consumer = GenerationQueueConsumer(handler, infra.amqp_url + "?heartbeat=2", infra.queue)
    stop = threading.Event()
    timer = threading.Timer(60, stop.set)
    timer.start()
    try:
        with caplog.at_level(logging.ERROR, logger="pdf_renderer_worker"):
            assert consumer.run(stop, max_messages=1) == 1
    finally:
        timer.cancel()

    # A dropped connection would make the consumer reconnect and the broker redeliver the message.
    assert "broker connection lost" not in caplog.text
    assert renderer.calls == 2
    assert infra.row(document_id).status == "AVAILABLE"
    assert infra.drain(infra.dlq) == []
    assert infra.drain(infra.queue) == []
