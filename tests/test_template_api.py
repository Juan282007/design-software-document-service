from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from document.adapter.inbound.http.security import Principal
from document.adapter.inbound.http.template_api import create_template_app
from document.adapter.outbound.persistence.template_repository import Base

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")


def _allow(_: str):
    def dependency() -> Principal:
        return Principal(subject=ACTOR_ID, features=frozenset())

    return dependency


@pytest.fixture()
def client() -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mapped_engine = engine.execution_options(schema_translate_map={"document": None})
    Base.metadata.create_all(mapped_engine)
    app = create_template_app(engine=mapped_engine, authorizer=_allow)
    with TestClient(app) as test_client:
        yield test_client


def _create_template(
    client: TestClient,
    *,
    code: str = "SCHEDULE_CERTIFICATE",
    name: str = "Constancia de horario",
    template_body: str = "<h1>Hola {{ learner_name }}</h1>",
) -> dict:
    response = client.post(
        "/api/v1/document-templates",
        json={
            "code": code,
            "name": name,
            "template_body": template_body,
            "output_type": "PDF",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_create_template_starts_active_at_version_one(client: TestClient) -> None:
    template = _create_template(client)

    assert template["version"] == 1
    assert template["is_active"] is True
    assert template["code"] == "SCHEDULE_CERTIFICATE"


def test_rejects_invalid_output_type(client: TestClient) -> None:
    response = client.post(
        "/api/v1/document-templates",
        json={"code": "BAD", "name": "Bad", "template_body": "<p>x</p>", "output_type": "TXT"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_preview_renders_without_creating_document_records(client: TestClient) -> None:
    template = _create_template(client)

    response = client.post(
        f"/api/v1/document-templates/{template['id']}/preview",
        json={"sample_data": {"learner_name": "Ana"}},
    )

    assert response.status_code == 200
    assert response.json() == {
        "output_type": "PDF",
        "rendered_html": "<h1>Hola Ana</h1>",
        "missing_placeholders": [],
    }


def test_update_deactivate_and_list_without_template_body(client: TestClient) -> None:
    template = _create_template(client)
    update = client.put(
        f"/api/v1/document-templates/{template['id']}",
        json={"name": "Constancia actualizada"},
    )
    assert update.status_code == 200
    assert update.json()["version"] == 2

    deactivation = client.delete(f"/api/v1/document-templates/{template['id']}")
    assert deactivation.status_code == 204

    listing = client.get("/api/v1/document-templates?is_active=false")
    assert listing.status_code == 200
    assert listing.json()["data"][0]["is_active"] is False
    assert "template_body" not in listing.json()["data"][0]


def test_list_uses_standard_pagination_and_applies_sort(client: TestClient) -> None:
    _create_template(client, code="TEMPLATE_BRAVO", name="Bravo")
    _create_template(client, code="TEMPLATE_ALPHA", name="Alpha")
    _create_template(client, code="TEMPLATE_CHARLIE", name="Charlie")

    response = client.get("/api/v1/document-templates?page=2&page_size=2&sort=name:asc")

    assert response.status_code == 200
    body = response.json()
    assert [template["name"] for template in body["data"]] == ["Charlie"]
    assert body["pagination"] == {
        "page": 2,
        "page_size": 2,
        "total_items": 3,
        "total_pages": 2,
    }


def test_list_rejects_an_unsupported_sort(client: TestClient) -> None:
    response = client.get("/api/v1/document-templates?sort=template_body:asc")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SORT"


def test_preview_supports_handlebars_blocks_and_nested_values(client: TestClient) -> None:
    template = _create_template(
        client,
        code="HANDLEBARS_TEMPLATE",
        name="Plantilla Handlebars",
        template_body="{{#if learner.active}}{{learner.name}}{{else}}Inactivo{{/if}}",
    )

    response = client.post(
        f"/api/v1/document-templates/{template['id']}/preview",
        json={"sample_data": {"learner": {"active": True, "name": "Ana"}}},
    )

    assert response.status_code == 200
    assert response.json()["rendered_html"] == "Ana"


def test_read_operations_require_doc_template_view() -> None:
    requested_features: list[str] = []

    def authorizer(feature: str):
        requested_features.append(feature)
        return _allow(feature)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mapped_engine = engine.execution_options(schema_translate_map={"document": None})
    Base.metadata.create_all(mapped_engine)
    app = create_template_app(engine=mapped_engine, authorizer=authorizer)

    with TestClient(app) as test_client:
        assert test_client.get("/api/v1/document-templates").status_code == 200

    assert requested_features == ["DOC_TEMPLATE_VIEW", "DOC_TEMPLATE_MANAGE"]
