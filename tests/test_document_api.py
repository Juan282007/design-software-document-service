from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from document.adapter.inbound.http import security
from document.adapter.inbound.http.document_api import create_document_app
from document.adapter.inbound.http.security import Principal
from document.adapter.inbound.http.template_api import create_template_app
from document.adapter.outbound.persistence.document_repository import DocumentRecord
from document.adapter.outbound.persistence.template_repository import Base
from document.domain.model.document import GenerationQueueUnavailableError, GenerationRequest

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OWNER_ENTITY_ID = "5c1a0000-0000-0000-0000-000000000002"


def _allow(_: str):
    def dependency() -> Principal:
        return Principal(subject=ACTOR_ID, features=frozenset({"DOC_CREATE:GLOBAL", "DOC_VIEW:GLOBAL", "DOC_DOWNLOAD:GLOBAL"}))

    return dependency


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[GenerationRequest] = []
        self.fail = False

    def publish(self, request: GenerationRequest) -> None:
        if self.fail:
            raise GenerationQueueUnavailableError("broker down")
        self.messages.append(request)


@pytest.fixture()
def engine():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mapped_engine = engine.execution_options(schema_translate_map={"document": None})
    Base.metadata.create_all(mapped_engine)
    return mapped_engine


@pytest.fixture()
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture()
def client(engine, publisher) -> TestClient:
    with TestClient(create_document_app(engine=engine, authorizer=_allow, publisher=publisher)) as test_client:
        yield test_client


@pytest.fixture()
def templates(engine) -> TestClient:
    with TestClient(create_template_app(engine=engine, authorizer=_allow)) as test_client:
        yield test_client


def _create_template(templates: TestClient, code: str = "SCHEDULE_CERTIFICATE") -> dict:
    response = templates.post(
        "/api/v1/document-templates",
        json={"code": code, "name": "Constancia", "template_body": "<h1>{{ name }}</h1>", "output_type": "PDF"},
    )
    assert response.status_code == 201
    return response.json()


def _payload(**overrides) -> dict:
    payload = {
        "template_code": "SCHEDULE_CERTIFICATE",
        "domain": "SCHEDULE",
        "owner_service": "scheduling-service",
        "owner_entity_id": OWNER_ENTITY_ID,
        "title": "Horario grupo 2026-1.pdf",
        "data": {"name": "Ana"},
    }
    payload.update(overrides)
    return payload


def _documents(engine) -> list[DocumentRecord]:
    with Session(engine) as session:
        return list(session.scalars(select(DocumentRecord)))


def test_valid_request_returns_202_persists_generating_and_publishes(client, templates, publisher, engine) -> None:
    template = _create_template(templates)

    response = client.post(
        "/api/v1/documents/generate", json=_payload(), headers={"X-Correlation-Id": "corr-123"}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "GENERATING"
    assert response.headers["X-Correlation-Id"] == "corr-123"
    records = _documents(engine)
    assert len(records) == 1
    assert str(records[0].id) == body["document_id"]
    assert records[0].status == "GENERATING"
    assert records[0].storage_key is None and records[0].mime_type is None
    assert str(records[0].template_id) == template["id"]
    assert records[0].created_by == ACTOR_ID
    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert str(message.document_id) == body["document_id"]
    assert message.template_code == "SCHEDULE_CERTIFICATE"
    assert message.data_payload == {"name": "Ana"}
    assert message.correlation_id == "corr-123"


def test_data_is_optional_and_defaults_to_an_empty_object(client, templates, publisher) -> None:
    _create_template(templates)
    payload = _payload()
    del payload["data"]

    response = client.post("/api/v1/documents/generate", json=payload)

    assert response.status_code == 202
    assert publisher.messages[0].data_payload == {}


def test_correlation_id_is_generated_when_missing(client, templates, publisher) -> None:
    _create_template(templates)

    response = client.post("/api/v1/documents/generate", json=_payload())

    assert response.status_code == 202
    assert response.headers["X-Correlation-Id"]
    assert publisher.messages[0].correlation_id == response.headers["X-Correlation-Id"]


def test_unknown_template_returns_template_not_found_and_does_not_enqueue(client, publisher, engine) -> None:
    response = client.post("/api/v1/documents/generate", json=_payload(template_code="MISSING"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"
    assert publisher.messages == []
    assert _documents(engine) == []


def test_inactive_template_returns_template_not_found(client, templates, publisher, engine) -> None:
    template = _create_template(templates)
    assert templates.delete(f"/api/v1/document-templates/{template['id']}").status_code == 204

    response = client.post("/api/v1/documents/generate", json=_payload())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"
    assert publisher.messages == []
    assert _documents(engine) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"domain": "UNKNOWN"},
        {"owner_entity_id": "not-a-uuid"},
        {"title": ""},
        {"data": "not-an-object"},
        {"template_code": ""},
    ],
)
def test_invalid_payload_returns_validation_error(client, templates, publisher, engine, overrides) -> None:
    _create_template(templates)

    response = client.post("/api/v1/documents/generate", json=_payload(**overrides))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert publisher.messages == []
    assert _documents(engine) == []


def test_missing_required_fields_return_validation_error(client) -> None:
    response = client.post("/api/v1/documents/generate", json={"template_code": "X"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_get_generating_document_has_no_download_url_or_storage_key(client, templates) -> None:
    _create_template(templates)
    document_id = client.post("/api/v1/documents/generate", json=_payload()).json()["document_id"]

    response = client.get(f"/api/v1/documents/{document_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "GENERATING"
    assert body["id"] == document_id
    assert "download_url" not in body and "url" not in body and "storage_key" not in body


def test_get_unknown_document_returns_not_found(client) -> None:
    response = client.get(f"/api/v1/documents/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_broker_failure_returns_503_and_marks_document_failed(client, templates, publisher, engine) -> None:
    _create_template(templates)
    publisher.fail = True

    response = client.post("/api/v1/documents/generate", json=_payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "GENERATION_QUEUE_UNAVAILABLE"
    records = _documents(engine)
    assert len(records) == 1
    assert records[0].status == "GENERATION_FAILED"
    assert publisher.messages == []


def test_missing_bearer_token_returns_401(engine, publisher) -> None:
    with TestClient(create_document_app(engine=engine, publisher=publisher)) as unauthenticated:
        response = unauthenticated.post("/api/v1/documents/generate", json=_payload())

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


class _FakeAuthenticator:
    def __init__(self, features: frozenset[str]) -> None:
        self._features = features

    def authenticate(self, _: str) -> Principal:
        return Principal(subject=ACTOR_ID, features=self._features)


def _client_with_features(monkeypatch, engine, publisher, features: set[str]) -> TestClient:
    monkeypatch.setattr(security, "_authenticator_from_environment", lambda: _FakeAuthenticator(frozenset(features)))
    return TestClient(create_document_app(engine=engine, publisher=publisher))


def test_token_without_doc_create_is_forbidden(monkeypatch, engine, publisher, templates) -> None:
    _create_template(templates)
    with _client_with_features(monkeypatch, engine, publisher, {"DOC_VIEW:GLOBAL"}) as client:
        response = client.post(
            "/api/v1/documents/generate", json=_payload(), headers={"Authorization": "Bearer token"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert publisher.messages == []


def test_token_with_doc_create_scope_is_accepted(monkeypatch, engine, publisher, templates) -> None:
    _create_template(templates)
    with _client_with_features(monkeypatch, engine, publisher, {"DOC_CREATE:GLOBAL"}) as client:
        response = client.post(
            "/api/v1/documents/generate", json=_payload(), headers={"Authorization": "Bearer token"}
        )

    assert response.status_code == 202
    assert len(publisher.messages) == 1


def test_generate_stores_training_center_of_the_caller(engine, publisher, templates) -> None:
    center = UUID("aaaaaaaa-0000-0000-0000-00000000000a")

    def authorizer(_: str):
        def dependency() -> Principal:
            return Principal(subject=ACTOR_ID, features=frozenset(), training_center_id=center)

        return dependency

    _create_template(templates)
    with TestClient(create_document_app(engine=engine, authorizer=authorizer, publisher=publisher)) as client:
        response = client.post("/api/v1/documents/generate", json=_payload())

    assert response.status_code == 202
    assert _documents(engine)[0].training_center_id == center
