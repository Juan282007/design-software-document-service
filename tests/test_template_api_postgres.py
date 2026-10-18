"""Optional HTTP integration test for HU-DOC-001 against PostgreSQL.

Run it with TEST_POSTGRES_DB_URL configured after applying the document
Liquibase changelog.  It is skipped in the regular unit-test suite.
"""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from document.adapter.inbound.http.security import Principal
from document.adapter.inbound.http.template_api import create_template_app


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")


def _allow(_: str):
    def dependency() -> Principal:
        return Principal(subject=ACTOR_ID, features=frozenset())

    return dependency


@pytest.mark.integration
def test_template_crud_and_preview_against_postgresql() -> None:
    database_url = os.getenv("TEST_POSTGRES_DB_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRES_DB_URL no está configurada")

    engine = create_engine(database_url, pool_pre_ping=True)
    code = f"IT_HU_DOC_001_{uuid4().hex[:12].upper()}"
    with engine.connect() as connection:
        before_documents = connection.execute(text("SELECT count(*) FROM document.document")).scalar_one()
        before_versions = connection.execute(text("SELECT count(*) FROM document.document_version")).scalar_one()

    app = create_template_app(engine=engine, authorizer=_allow)
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/v1/document-templates",
                json={
                    "code": code,
                    "name": "Plantilla de integración PostgreSQL",
                    "template_body": "<h1>{{ learner_name }}</h1>",
                    "output_type": "PDF",
                },
            )
            assert created.status_code == 201
            template = created.json()
            assert template["version"] == 1

            preview = client.post(
                f"/api/v1/document-templates/{template['id']}/preview",
                json={"sample_data": {"learner_name": "Ana"}},
            )
            assert preview.status_code == 200
            assert preview.json()["rendered_html"] == "<h1>Ana</h1>"

            updated = client.put(
                f"/api/v1/document-templates/{template['id']}",
                json={"name": "Plantilla actualizada"},
            )
            assert updated.status_code == 200
            assert updated.json()["version"] == 2

            assert client.delete(f"/api/v1/document-templates/{template['id']}").status_code == 204

        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM document.document")).scalar_one() == before_documents
            assert connection.execute(text("SELECT count(*) FROM document.document_version")).scalar_one() == before_versions
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM document.document_template WHERE code = :code"), {"code": code})
        engine.dispose()
