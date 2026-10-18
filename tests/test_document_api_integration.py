"""Integration test for HU-DOC-002 against PostgreSQL and RabbitMQ.

Run it with TEST_POSTGRES_DB_URL and TEST_AMQP_URL configured, after applying the
document Liquibase changelog (including changeset 972). It is skipped otherwise.
"""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pika
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from document.adapter.inbound.http.document_api import create_document_app
from document.adapter.inbound.http.security import Principal
from document.adapter.inbound.http.template_api import create_template_app
from document.adapter.outbound.messaging.document_generation_publisher import RabbitMqDocumentGenerationPublisher

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OWNER_ENTITY_ID = "5c1a0000-0000-0000-0000-000000000002"


def _allow(_: str):
    def dependency() -> Principal:
        return Principal(subject=ACTOR_ID, features=frozenset({"DOC_CREATE:GLOBAL", "DOC_VIEW:GLOBAL", "DOC_DOWNLOAD:GLOBAL"}))

    return dependency


@pytest.mark.integration
def test_generate_persists_generating_and_publishes_to_rabbitmq() -> None:
    database_url = os.getenv("TEST_POSTGRES_DB_URL")
    amqp_url = os.getenv("TEST_AMQP_URL")
    if not database_url or not amqp_url:
        pytest.skip("TEST_POSTGRES_DB_URL y TEST_AMQP_URL no están configuradas")

    engine = create_engine(database_url, pool_pre_ping=True)
    suffix = uuid4().hex[:12].upper()
    code = f"IT_HU_DOC_002_{suffix}"
    queue = f"it-document-generation-{suffix.lower()}"
    correlation_id = f"corr-{suffix}"
    document_id: str | None = None

    publisher = RabbitMqDocumentGenerationPublisher(amqp_url=amqp_url, queue=queue)
    templates_app = create_template_app(engine=engine, authorizer=_allow)
    documents_app = create_document_app(engine=engine, authorizer=_allow, publisher=publisher)
    connection = pika.BlockingConnection(pika.URLParameters(amqp_url))
    try:
        with TestClient(templates_app) as templates, TestClient(documents_app) as documents:
            created = templates.post(
                "/api/v1/document-templates",
                json={
                    "code": code,
                    "name": "Plantilla de integración",
                    "template_body": "<h1>{{ name }}</h1>",
                    "output_type": "PDF",
                },
            )
            assert created.status_code == 201

            response = documents.post(
                "/api/v1/documents/generate",
                headers={"X-Correlation-Id": correlation_id},
                json={
                    "template_code": code,
                    "domain": "SCHEDULE",
                    "owner_service": "scheduling-service",
                    "owner_entity_id": OWNER_ENTITY_ID,
                    "title": "Horario integración.pdf",
                    "data": {"name": "Ana"},
                },
            )
            assert response.status_code == 202
            document_id = response.json()["document_id"]
            assert response.json()["status"] == "GENERATING"

            detail = documents.get(f"/api/v1/documents/{document_id}")
            assert detail.status_code == 200
            assert detail.json()["status"] == "GENERATING"
            assert "storage_key" not in detail.json() and "url" not in detail.json()

            missing = documents.post(
                "/api/v1/documents/generate",
                json={
                    "template_code": f"MISSING_{suffix}",
                    "domain": "SCHEDULE",
                    "owner_service": "scheduling-service",
                    "owner_entity_id": OWNER_ENTITY_ID,
                    "title": "x",
                    "data": {},
                },
            )
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"

        with engine.connect() as db:
            row = db.execute(
                text("SELECT status, storage_key, mime_type, domain FROM document.document WHERE id = :id"),
                {"id": document_id},
            ).one()
        assert row.status == "GENERATING"
        assert row.storage_key is None and row.mime_type is None
        assert row.domain == "SCHEDULE"

        channel = connection.channel()
        method, properties, body = channel.basic_get(queue=queue, auto_ack=True)
        assert method is not None, "no se publicó ningún mensaje en la cola"
        message = json.loads(body)
        assert message == {
            "document_id": document_id,
            "template_code": code,
            "data_payload": {"name": "Ana"},
            "correlation_id": correlation_id,
        }
        assert properties.correlation_id == correlation_id
        assert channel.basic_get(queue=queue, auto_ack=True)[0] is None, "solo debía publicarse un mensaje"
    finally:
        try:
            connection.channel().queue_delete(queue=queue)
        finally:
            connection.close()
        with engine.begin() as db:
            if document_id:
                db.execute(text("DELETE FROM document.document WHERE id = :id"), {"id": document_id})
            db.execute(text("DELETE FROM document.document_template WHERE code = :code"), {"code": code})
        engine.dispose()
