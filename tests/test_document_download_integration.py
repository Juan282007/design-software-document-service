"""Integration test for HU-DOC-003 against PostgreSQL and an S3-compatible store (MinIO).

Run it with TEST_POSTGRES_DB_URL and TEST_S3_ENDPOINT configured (optionally
TEST_S3_ACCESS_KEY / TEST_S3_SECRET_KEY, default minioadmin), after applying the document
Liquibase changelog (including changeset 973). It is skipped otherwise.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4

import boto3
import httpx
import pytest
from botocore.client import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from document.adapter.inbound.http.document_api import create_document_app
from document.adapter.inbound.http.security import Principal
from document.adapter.outbound.storage.s3_object_storage import S3ObjectStorage

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
CENTER_ID = UUID("aaaaaaaa-0000-0000-0000-00000000000a")
CONTENT = b"%PDF-1.4 contenido de prueba HU-DOC-003"


def _global(_: str):
    def dependency() -> Principal:
        return Principal(
            subject=ACTOR_ID,
            features=frozenset({"DOC_VIEW:GLOBAL", "DOC_DOWNLOAD:GLOBAL"}),
            training_center_id=CENTER_ID,
        )

    return dependency


@pytest.mark.integration
def test_signed_url_works_against_minio_and_expires() -> None:
    database_url = os.getenv("TEST_POSTGRES_DB_URL")
    endpoint = os.getenv("TEST_S3_ENDPOINT")
    if not database_url or not endpoint:
        pytest.skip("TEST_POSTGRES_DB_URL y TEST_S3_ENDPOINT no están configuradas")
    access_key = os.getenv("TEST_S3_ACCESS_KEY", "minioadmin")
    secret_key = os.getenv("TEST_S3_SECRET_KEY", "minioadmin")

    suffix = uuid4().hex[:10]
    bucket = f"it-documents-{suffix}"
    key = f"documents/SCHEDULE/2026/10/{suffix}.pdf"
    owner_entity_id = uuid4()
    document_id = uuid4()
    generating_id = uuid4()
    version_id = uuid4()

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    engine = create_engine(database_url, pool_pre_ping=True)
    s3.create_bucket(Bucket=bucket)
    s3.put_object(Bucket=bucket, Key=key, Body=CONTENT, ContentType="application/pdf")
    now = datetime.now(UTC)
    try:
        insert_document = text(
            "INSERT INTO document.document (id, title, domain, owner_service, owner_entity_id, storage_key,"
            " mime_type, status, row_version, created_at, created_by, updated_at, training_center_id)"
            " VALUES (:id, 'Horario.pdf', 'SCHEDULE', 'scheduling-service', :owner, :key, :mime, :status,"
            " 0, :now, :actor, :now, :center)"
        )
        insert_version = text(
            "INSERT INTO document.document_version (id, document_id, version_number, storage_key, notes,"
            " created_at, created_by) VALUES (:id, :doc, 1, :key, 'v1', :now, :actor)"
        )
        with engine.begin() as db:
            for doc_id, status, storage_key, mime in (
                (document_id, "AVAILABLE", key, "application/pdf"),
                (generating_id, "GENERATING", None, None),
            ):
                db.execute(
                    insert_document,
                    {
                        "id": doc_id,
                        "owner": owner_entity_id,
                        "key": storage_key,
                        "mime": mime,
                        "status": status,
                        "now": now,
                        "actor": ACTOR_ID,
                        "center": CENTER_ID,
                    },
                )
            db.execute(insert_version, {"id": version_id, "doc": document_id, "key": key, "now": now, "actor": ACTOR_ID})

        storage = S3ObjectStorage(endpoint_url=endpoint, bucket=bucket, access_key=access_key, secret_key=secret_key)
        app = create_document_app(engine=engine, authorizer=_global, publisher=object(), storage=storage)
        with TestClient(app) as client:
            listing = client.get(
                "/api/v1/documents",
                params={"owner_service": "scheduling-service", "owner_entity_id": str(owner_entity_id), "page_size": 10},
            )
            assert listing.status_code == 200
            assert listing.json()["pagination"]["total_items"] == 2
            assert key not in listing.text

            versions = client.get(f"/api/v1/documents/{document_id}/versions")
            assert versions.status_code == 200
            assert len(versions.json()) == 1
            assert key not in versions.text

            blocked = client.get(f"/api/v1/documents/{generating_id}/download-url")
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "DOCUMENT_NOT_AVAILABLE"

            response = client.get(f"/api/v1/documents/{document_id}/download-url")
            assert response.status_code == 200
            body = response.json()
            assert set(body) == {"document_id", "url", "expires_in"}
            assert body["expires_in"] == 300
            downloaded = httpx.get(body["url"])
            assert downloaded.status_code == 200
            assert downloaded.content == CONTENT

            tampered = httpx.get(body["url"].replace("X-Amz-Signature=", "X-Amz-Signature=0"))
            assert tampered.status_code == 403

        app = create_document_app(
            engine=engine,
            authorizer=_global,
            publisher=object(),
            storage=S3ObjectStorage(endpoint_url=endpoint, bucket=bucket, access_key=access_key, secret_key=secret_key),
            signed_url_ttl_seconds=8,
        )
        with TestClient(app) as client:
            response = client.get(f"/api/v1/documents/{document_id}/download-url")
            assert response.json()["expires_in"] == 8
            url = response.json()["url"]
            assert httpx.get(url).status_code == 200
            time.sleep(10)
            expired = httpx.get(url)
            assert expired.status_code == 403
            assert "expired" in expired.text.lower()
    finally:
        with engine.begin() as db:
            db.execute(text("DELETE FROM document.document_version WHERE document_id = :id"), {"id": document_id})
            db.execute(
                text("DELETE FROM document.document WHERE id IN (:a, :b)"), {"a": document_id, "b": generating_id}
            )
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            s3.delete_bucket(Bucket=bucket)
        finally:
            engine.dispose()
