"""Authentication verification with a signed RS256 token obtained through JWKS."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from document.adapter.inbound.http.document_api import create_document_app
from document.adapter.inbound.http.template_api import create_template_app
from document.adapter.outbound.persistence.document_repository import DocumentRecord
from document.adapter.outbound.persistence.template_repository import Base


class _JwksHandler(BaseHTTPRequestHandler):
    payload = b"{}"

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        if self.path != "/api/v1/auth/.well-known/jwks.json":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, _: str, *args: object) -> None:
        return


@pytest.fixture()
def jwks_endpoint() -> tuple[str, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "iam-rsa-test", "use": "sig", "alg": "RS256"})
    _JwksHandler.payload = json.dumps({"keys": [jwk]}).encode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/v1/auth/.well-known/jwks.json", private_key
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_template_api_accepts_iam_style_rs256_jwt_from_jwks(
    monkeypatch: pytest.MonkeyPatch,
    jwks_endpoint: tuple[str, rsa.RSAPrivateKey],
) -> None:
    url, private_key = jwks_endpoint
    issuer = "https://iam.local.test"
    token = jwt.encode(
        {
            "sub": str(UUID("00000000-0000-0000-0000-000000000001")),
            "features": ["DOC_TEMPLATE_MANAGE:WRITE"],
            "iss": issuer,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "iam-rsa-test"},
    )
    monkeypatch.setenv("IAM_JWKS_URL", url)
    monkeypatch.setenv("JWT_ISSUER", issuer)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mapped_engine = engine.execution_options(schema_translate_map={"document": None})
    Base.metadata.create_all(mapped_engine)
    app = create_template_app(engine=mapped_engine)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/document-templates",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "code": "JWKS_AUTH_TEST",
                "name": "Plantilla autenticada",
                "template_body": "<p>OK</p>",
                "output_type": "PDF",
            },
        )

    assert response.status_code == 201


def _document_app_with_two_centers(monkeypatch: pytest.MonkeyPatch, url: str, issuer: str):
    center_a = UUID("aaaaaaaa-0000-0000-0000-00000000000a")
    center_b = UUID("bbbbbbbb-0000-0000-0000-00000000000b")
    monkeypatch.setenv("IAM_JWKS_URL", url)
    monkeypatch.setenv("JWT_ISSUER", issuer)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    mapped_engine = engine.execution_options(schema_translate_map={"document": None})
    Base.metadata.create_all(mapped_engine)
    now = datetime.now(UTC)
    with Session(mapped_engine) as session:
        for title, center in (("center-a", center_a), ("center-b", center_b)):
            session.add(
                DocumentRecord(
                    id=uuid4(),
                    title=title,
                    domain="SCHEDULE",
                    owner_service="scheduling-service",
                    owner_entity_id=uuid4(),
                    status="GENERATING",
                    row_version=0,
                    created_at=now,
                    created_by=UUID(int=1),
                    updated_at=now,
                    training_center_id=center,
                )
            )
        session.commit()
    return create_document_app(engine=mapped_engine, publisher=object(), storage=object()), center_a


def _token(private_key: rsa.RSAPrivateKey, issuer: str, features: list[str], training_center_id: object) -> str:
    return jwt.encode(
        {
            "sub": str(UUID(int=1)),
            "features": features,
            "training_center_id": training_center_id,
            "iss": issuer,
            "exp": datetime.now(UTC) + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "iam-rsa-test"},
    )


def test_document_api_scopes_a_real_jwt_by_training_center(
    monkeypatch: pytest.MonkeyPatch,
    jwks_endpoint: tuple[str, rsa.RSAPrivateKey],
) -> None:
    url, private_key = jwks_endpoint
    issuer = "https://iam.local.test"
    app, center_a = _document_app_with_two_centers(monkeypatch, url, issuer)

    with TestClient(app) as client:
        scoped = client.get(
            "/api/v1/documents",
            headers={"Authorization": f"Bearer {_token(private_key, issuer, ['DOC_VIEW:TRAINING_CENTER'], str(center_a))}"},
        )
        global_scope = client.get(
            "/api/v1/documents",
            headers={"Authorization": f"Bearer {_token(private_key, issuer, ['DOC_VIEW:GLOBAL'], None)}"},
        )
        no_center = client.get(
            "/api/v1/documents",
            headers={"Authorization": f"Bearer {_token(private_key, issuer, ['DOC_VIEW:TRAINING_CENTER'], None)}"},
        )

    assert [item["title"] for item in scoped.json()["data"]] == ["center-a"]
    assert {item["title"] for item in global_scope.json()["data"]} == {"center-a", "center-b"}
    assert no_center.status_code == 403


def test_unexpected_training_center_claim_does_not_reject_the_token(
    monkeypatch: pytest.MonkeyPatch,
    jwks_endpoint: tuple[str, rsa.RSAPrivateKey],
) -> None:
    url, private_key = jwks_endpoint
    issuer = "https://iam.local.test"
    app, _ = _document_app_with_two_centers(monkeypatch, url, issuer)

    with TestClient(app) as client:
        global_scope = client.get(
            "/api/v1/documents",
            headers={"Authorization": f"Bearer {_token(private_key, issuer, ['DOC_VIEW:GLOBAL'], 'tc_not-a-uuid')}"},
        )
        scoped = client.get(
            "/api/v1/documents",
            headers={"Authorization": f"Bearer {_token(private_key, issuer, ['DOC_VIEW:TRAINING_CENTER'], 'tc_not-a-uuid')}"},
        )

    assert global_scope.status_code == 200
    assert len(global_scope.json()["data"]) == 2
    assert scoped.status_code == 403
