"""JWT/JWKS authentication and feature authorization for document-service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from document.domain.model.document import DocumentAccess

security_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    subject: UUID
    features: frozenset[str]
    training_center_id: UUID | None = None


def _training_center_from(claim: object) -> UUID | None:
    """The claim is a UUID (null for global scope); anything else means no usable training center."""
    if not claim:
        return None
    try:
        return UUID(str(claim))
    except ValueError:
        return None


class JwtAuthenticator:
    def __init__(self, jwks_url: str, issuer: str | None = None) -> None:
        self._jwks_client = jwt.PyJWKClient(jwks_url)
        self._issuer = issuer

    def authenticate(self, token: str) -> Principal:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            options = {"verify_aud": False}
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options=options,
            )
            return Principal(
                subject=UUID(claims["sub"]),
                features=frozenset(claims.get("features", [])),
                training_center_id=_training_center_from(claims.get("training_center_id")),
            )
        except (KeyError, TypeError, ValueError, jwt.PyJWTError) as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "UNAUTHORIZED", "message": "Token JWT inválido o expirado"},
            ) from error


def _authenticator_from_environment() -> JwtAuthenticator:
    jwks_url = os.getenv("IAM_JWKS_URL")
    if not jwks_url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "CONFIGURATION_ERROR", "message": "IAM_JWKS_URL no está configurada"},
        )
    return JwtAuthenticator(jwks_url, os.getenv("JWT_ISSUER"))


def require_feature(feature: str):
    def dependency(credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme)) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "UNAUTHORIZED", "message": "Bearer token requerido"},
            )
        principal = _authenticator_from_environment().authenticate(credentials.credentials)
        if not any(item == feature or item.startswith(f"{feature}:") for item in principal.features):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "FORBIDDEN", "message": "No tiene el permiso requerido"},
            )
        return principal

    return dependency


def resolve_document_access(principal: Principal, feature: str) -> DocumentAccess:
    """Translate the FEATURE:SCOPE pairs of the token into a data scope.

    GLOBAL sees every document and TRAINING_CENTER only those of the caller's training
    center. Any other scope (or a bare feature without scope) does not grant document access.
    """
    scopes = {item.split(":", 1)[1] for item in principal.features if item.startswith(f"{feature}:")}
    if "GLOBAL" in scopes:
        return DocumentAccess(global_scope=True)
    if "TRAINING_CENTER" in scopes and principal.training_center_id is not None:
        return DocumentAccess(global_scope=False, training_center_id=principal.training_center_id)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "FORBIDDEN", "message": "El alcance del permiso no permite acceder a documentos"},
    )
