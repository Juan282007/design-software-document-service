from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from document.adapter.inbound.http.document_api import create_document_app
from document.adapter.inbound.http.security import Principal
from document.adapter.outbound.persistence.document_repository import DocumentRecord, DocumentVersionRecord
from document.adapter.outbound.persistence.template_repository import Base
from document.domain.model.document import StorageUnavailableError

ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
CENTER_A = UUID("aaaaaaaa-0000-0000-0000-00000000000a")
CENTER_B = UUID("bbbbbbbb-0000-0000-0000-00000000000b")
ENTITY_1 = UUID("11111111-0000-0000-0000-000000000001")
ENTITY_2 = UUID("22222222-0000-0000-0000-000000000002")
ALL_GLOBAL = {"DOC_VIEW:GLOBAL", "DOC_DOWNLOAD:GLOBAL"}


class FakeStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail = False

    def presign_download(self, storage_key: str, expires_in_seconds: int) -> str:
        if self.fail:
            raise StorageUnavailableError("storage down")
        self.calls.append((storage_key, expires_in_seconds))
        return f"https://storage.test/documents/{storage_key}?X-Amz-Expires={expires_in_seconds}&X-Amz-Signature=abc"


def _authorizer(features: set[str], training_center_id: UUID | None = None):
    def factory(feature: str):
        def dependency() -> Principal:
            if not any(item == feature or item.startswith(f"{feature}:") for item in features):
                raise HTTPException(403, {"code": "FORBIDDEN", "message": "No tiene el permiso requerido"})
            return Principal(subject=ACTOR_ID, features=frozenset(features), training_center_id=training_center_id)

        return dependency

    return factory


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
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture()
def make_client(engine, storage):
    clients: list[TestClient] = []

    def factory(features: set[str], training_center_id: UUID | None = None) -> TestClient:
        app = create_document_app(
            engine=engine,
            authorizer=_authorizer(features, training_center_id),
            publisher=object(),
            storage=storage,
            signed_url_ttl_seconds=300,
        )
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


def _seed(engine, **overrides) -> UUID:
    now = datetime.now(UTC)
    values = {
        "id": uuid4(),
        "title": "Horario.pdf",
        "domain": "SCHEDULE",
        "owner_service": "scheduling-service",
        "owner_entity_id": ENTITY_1,
        "status": "AVAILABLE",
        "storage_key": "documents/SCHEDULE/2026/10/horario.pdf",
        "mime_type": "application/pdf",
        "row_version": 0,
        "created_at": now,
        "created_by": ACTOR_ID,
        "updated_at": now,
        "training_center_id": CENTER_A,
    }
    values.update(overrides)
    with Session(engine) as session:
        session.add(DocumentRecord(**values))
        session.commit()
    return values["id"]


def _seed_version(engine, document_id: UUID, number: int, notes: str | None = None) -> UUID:
    version_id = uuid4()
    with Session(engine) as session:
        session.add(
            DocumentVersionRecord(
                id=version_id,
                document_id=document_id,
                version_number=number,
                storage_key=f"documents/secret/v{number}.pdf",
                notes=notes,
                created_at=datetime.now(UTC),
                created_by=ACTOR_ID,
            )
        )
        session.commit()
    return version_id


def test_list_filters_by_owner_and_paginates(make_client, engine) -> None:
    for index in range(3):
        _seed(engine, owner_entity_id=ENTITY_1, title=f"mine-{index}", created_at=datetime.now(UTC) + timedelta(seconds=index))
    _seed(engine, owner_entity_id=ENTITY_2, title="other")
    _seed(engine, owner_entity_id=ENTITY_1, owner_service="ficha-service", title="other-service")
    client = make_client(ALL_GLOBAL)

    response = client.get(
        "/api/v1/documents",
        params={"owner_service": "scheduling-service", "owner_entity_id": str(ENTITY_1), "page": 1, "page_size": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["title"] for item in body["data"]] == ["mine-2", "mine-1"]
    assert body["pagination"] == {"page": 1, "page_size": 2, "total_items": 3, "total_pages": 2}
    assert all("storage_key" not in item for item in body["data"])

    second = client.get(
        "/api/v1/documents",
        params={"owner_service": "scheduling-service", "owner_entity_id": str(ENTITY_1), "page": 2, "page_size": 2},
    ).json()
    assert [item["title"] for item in second["data"]] == ["mine-0"]


def test_list_filters_by_domain_and_status(make_client, engine) -> None:
    _seed(engine, domain="CERTIFICATE", status="AVAILABLE", title="cert-ok")
    _seed(engine, domain="CERTIFICATE", status="GENERATING", storage_key=None, mime_type=None, title="cert-pending")
    _seed(engine, domain="REPORT", status="AVAILABLE", title="report")
    client = make_client(ALL_GLOBAL)

    response = client.get("/api/v1/documents", params={"domain": "CERTIFICATE", "status": "AVAILABLE"})

    assert [item["title"] for item in response.json()["data"]] == ["cert-ok"]


def test_list_applies_sort_and_defaults_to_newest_first(make_client, engine) -> None:
    now = datetime.now(UTC)
    _seed(engine, title="b-title", created_at=now)
    _seed(engine, title="a-title", created_at=now + timedelta(seconds=1))
    _seed(engine, title="c-title", created_at=now + timedelta(seconds=2))
    client = make_client(ALL_GLOBAL)

    default = [item["title"] for item in client.get("/api/v1/documents").json()["data"]]
    by_title = [item["title"] for item in client.get("/api/v1/documents", params={"sort": "title:asc"}).json()["data"]]
    by_title_desc = [
        item["title"] for item in client.get("/api/v1/documents", params={"sort": "title:desc"}).json()["data"]
    ]

    assert default == ["c-title", "a-title", "b-title"]
    assert by_title == ["a-title", "b-title", "c-title"]
    assert by_title_desc == ["c-title", "b-title", "a-title"]


@pytest.mark.parametrize("sort", ["storage_key:asc", "title", "title:up", "id:asc"])
def test_list_rejects_unsupported_sort(make_client, sort) -> None:
    response = make_client(ALL_GLOBAL).get("/api/v1/documents", params={"sort": sort})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SORT"


def test_document_response_matches_the_contract_fields(make_client, engine) -> None:
    document_id = _seed(engine)

    body = make_client(ALL_GLOBAL).get(f"/api/v1/documents/{document_id}").json()

    for field in (
        "id", "title", "domain", "owner_service", "owner_entity_id", "mime_type", "status",
        "row_version", "created_by", "created_at", "updated_at",
    ):
        assert field in body
    assert body["created_by"] == str(ACTOR_ID)


@pytest.mark.parametrize(
    "params",
    [{"domain": "UNKNOWN"}, {"status": "DONE"}, {"page": 0}, {"page_size": 101}, {"owner_entity_id": "not-a-uuid"}],
)
def test_list_rejects_invalid_filters(make_client, params) -> None:
    response = make_client(ALL_GLOBAL).get("/api/v1/documents", params=params)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_training_center_scope_only_lists_own_center(make_client, engine) -> None:
    _seed(engine, title="center-a", training_center_id=CENTER_A)
    _seed(engine, title="center-b", training_center_id=CENTER_B)
    _seed(engine, title="no-center", training_center_id=None)

    own = make_client({"DOC_VIEW:TRAINING_CENTER"}, CENTER_A).get("/api/v1/documents").json()
    everything = make_client({"DOC_VIEW:GLOBAL"}).get("/api/v1/documents").json()

    assert [item["title"] for item in own["data"]] == ["center-a"]
    assert own["pagination"]["total_items"] == 1
    assert {item["title"] for item in everything["data"]} == {"center-a", "center-b", "no-center"}


def test_unusable_scope_is_forbidden(make_client, engine) -> None:
    _seed(engine)

    assert make_client({"DOC_VIEW:OWN_FICHAS"}).get("/api/v1/documents").status_code == 403
    assert make_client({"DOC_VIEW:TRAINING_CENTER"}, None).get("/api/v1/documents").status_code == 403
    assert make_client({"DOC_VIEW"}).get("/api/v1/documents").status_code == 403


def test_missing_view_feature_is_forbidden(make_client) -> None:
    response = make_client({"DOC_DOWNLOAD:GLOBAL"}).get("/api/v1/documents")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_detail_out_of_scope_looks_like_a_missing_document(make_client, engine) -> None:
    foreign = _seed(engine, training_center_id=CENTER_B)
    own = _seed(engine, training_center_id=CENTER_A)
    client = make_client({"DOC_VIEW:TRAINING_CENTER"}, CENTER_A)

    assert client.get(f"/api/v1/documents/{own}").status_code == 200
    out_of_scope = client.get(f"/api/v1/documents/{foreign}")
    unknown = client.get(f"/api/v1/documents/{uuid4()}")
    assert out_of_scope.status_code == 404
    assert out_of_scope.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert unknown.status_code == 404
    assert unknown.json() == out_of_scope.json()


def test_download_url_for_available_document(make_client, engine, storage) -> None:
    document_id = _seed(engine)

    response = make_client(ALL_GLOBAL).get(f"/api/v1/documents/{document_id}/download-url")

    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == str(document_id)
    assert body["expires_in"] == 300
    assert body["url"].startswith("https://storage.test/")
    assert set(body) == {"document_id", "url", "expires_in"}
    assert storage.calls == [("documents/SCHEDULE/2026/10/horario.pdf", 300)]


@pytest.mark.parametrize("document_status", ["GENERATING", "GENERATION_FAILED", "ARCHIVED", "EXPIRED"])
def test_download_is_blocked_when_not_available(make_client, engine, storage, document_status) -> None:
    document_id = _seed(engine, status=document_status, storage_key=None, mime_type=None)

    response = make_client(ALL_GLOBAL).get(f"/api/v1/documents/{document_id}/download-url")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_AVAILABLE"
    assert storage.calls == []


def test_download_requires_doc_download_and_scope(make_client, engine, storage) -> None:
    own = _seed(engine, training_center_id=CENTER_A)
    foreign = _seed(engine, training_center_id=CENTER_B)

    only_view = make_client({"DOC_VIEW:GLOBAL"}).get(f"/api/v1/documents/{own}/download-url")
    scoped = make_client({"DOC_DOWNLOAD:TRAINING_CENTER"}, CENTER_A)
    allowed = scoped.get(f"/api/v1/documents/{own}/download-url")
    denied = scoped.get(f"/api/v1/documents/{foreign}/download-url")

    assert only_view.status_code == 403
    assert allowed.status_code == 200
    assert denied.status_code == 404
    assert len(storage.calls) == 1


def test_download_unknown_document_is_not_found(make_client) -> None:
    assert make_client(ALL_GLOBAL).get(f"/api/v1/documents/{uuid4()}/download-url").status_code == 404


def test_storage_failure_returns_503(make_client, engine, storage) -> None:
    document_id = _seed(engine)
    storage.fail = True

    response = make_client(ALL_GLOBAL).get(f"/api/v1/documents/{document_id}/download-url")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "STORAGE_UNAVAILABLE"


def test_versions_history_and_detail_never_expose_storage_key(make_client, engine) -> None:
    document_id = _seed(engine)
    first = _seed_version(engine, document_id, 1, "first")
    _seed_version(engine, document_id, 2, "second")
    client = make_client(ALL_GLOBAL)

    history = client.get(f"/api/v1/documents/{document_id}/versions")
    detail = client.get(f"/api/v1/documents/{document_id}/versions/{first}")

    assert history.status_code == 200
    assert [item["version_number"] for item in history.json()] == [2, 1]
    assert detail.status_code == 200
    assert detail.json()["notes"] == "first"
    assert "storage_key" not in history.text and "storage_key" not in detail.text
    assert "secret" not in history.text and "secret" not in detail.text


def test_versions_respect_scope_and_existence(make_client, engine) -> None:
    foreign = _seed(engine, training_center_id=CENTER_B)
    version = _seed_version(engine, foreign, 1)
    own = _seed(engine, training_center_id=CENTER_A)
    client = make_client({"DOC_VIEW:TRAINING_CENTER"}, CENTER_A)

    assert client.get(f"/api/v1/documents/{foreign}/versions").status_code == 404
    assert client.get(f"/api/v1/documents/{foreign}/versions/{version}").status_code == 404
    assert client.get(f"/api/v1/documents/{own}/versions").json() == []
    missing = client.get(f"/api/v1/documents/{own}/versions/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "DOCUMENT_VERSION_NOT_FOUND"
    assert client.get(f"/api/v1/documents/{uuid4()}/versions").status_code == 404


def test_missing_bearer_token_returns_401(engine, storage) -> None:
    with TestClient(create_document_app(engine=engine, publisher=object(), storage=storage)) as client:
        assert client.get("/api/v1/documents").status_code == 401
        assert client.get(f"/api/v1/documents/{uuid4()}/download-url").status_code == 401
