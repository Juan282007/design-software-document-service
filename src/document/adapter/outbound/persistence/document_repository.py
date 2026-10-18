"""SQLAlchemy adapter for document.document and document.document_version."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, Uuid, false, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from document.adapter.outbound.persistence.template_repository import Base
from document.domain.model.document import (
    STATUS_AVAILABLE,
    STATUS_GENERATION_FAILED,
    Document,
    DocumentAccess,
    DocumentVersion,
    DuplicateVersionError,
)


class DocumentRecord(Base):
    __tablename__ = "document"
    __table_args__ = {"schema": "document"}

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    template_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    domain: Mapped[str] = mapped_column(String(50), nullable=False)
    owner_service: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_entity_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    training_center_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class DocumentVersionRecord(Base):
    __tablename__ = "document_version"
    __table_args__ = {"schema": "document"}

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class SqlAlchemyDocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, document: Document) -> Document:
        record = DocumentRecord(
            id=document.id,
            template_id=document.template_id,
            title=document.title,
            domain=document.domain,
            owner_service=document.owner_service,
            owner_entity_id=document.owner_entity_id,
            status=document.status,
            row_version=document.row_version,
            created_at=document.created_at,
            created_by=document.created_by,
            updated_at=document.updated_at,
            updated_by=document.updated_by,
            training_center_id=document.training_center_id,
        )
        self._session.add(record)
        self._session.commit()
        self._session.refresh(record)
        return self._to_domain(record)

    def get(self, identifier: UUID) -> Document | None:
        record = self._session.get(DocumentRecord, identifier)
        return self._to_domain(record) if record else None

    def mark_generation_failed(self, identifier: UUID) -> None:
        self._session.execute(
            update(DocumentRecord)
            .where(DocumentRecord.id == identifier)
            .values(
                status=STATUS_GENERATION_FAILED,
                updated_at=datetime.now(UTC),
                row_version=DocumentRecord.row_version + 1,
            )
        )
        self._session.commit()

    def get_version_by_correlation(self, document_id: UUID, correlation_id: str) -> DocumentVersion | None:
        record = self._session.scalar(
            select(DocumentVersionRecord).where(
                DocumentVersionRecord.document_id == document_id,
                DocumentVersionRecord.correlation_id == correlation_id,
            )
        )
        return self._version_to_domain(record) if record else None

    def next_version_number(self, document_id: UUID) -> int:
        current = self._session.scalar(
            select(func.max(DocumentVersionRecord.version_number)).where(DocumentVersionRecord.document_id == document_id)
        )
        return (current or 0) + 1

    def complete_generation(
        self,
        *,
        document_id: UUID,
        version: DocumentVersion,
        mime_type: str,
        size_bytes: int,
        updated_by: UUID,
    ) -> Document:
        record = self._session.get(DocumentRecord, document_id)
        self._session.add(
            DocumentVersionRecord(
                id=version.id,
                document_id=version.document_id,
                version_number=version.version_number,
                storage_key=version.storage_key,
                notes=version.notes,
                created_at=version.created_at,
                created_by=version.created_by,
                correlation_id=version.correlation_id,
            )
        )
        record.storage_key = version.storage_key
        record.mime_type = mime_type
        record.size_bytes = size_bytes
        record.status = STATUS_AVAILABLE
        record.row_version = record.row_version + 1
        record.updated_at = datetime.now(UTC)
        record.updated_by = updated_by
        try:
            self._session.commit()
        except IntegrityError as error:
            self._session.rollback()
            if self.get_version_by_correlation(document_id, version.correlation_id or "") is not None:
                raise DuplicateVersionError() from error
            raise
        self._session.refresh(record)
        return self._to_domain(record)

    def list(
        self,
        *,
        access: DocumentAccess,
        owner_service: str | None,
        owner_entity_id: UUID | None,
        domain: str | None,
        status: str | None,
        sort_field: str,
        sort_direction: str,
        page: int,
        page_size: int,
    ) -> tuple[list[Document], int]:
        filters = []
        if not access.global_scope:
            if access.training_center_id is None:
                filters.append(false())
            else:
                filters.append(DocumentRecord.training_center_id == access.training_center_id)
        if owner_service:
            filters.append(DocumentRecord.owner_service == owner_service)
        if owner_entity_id:
            filters.append(DocumentRecord.owner_entity_id == owner_entity_id)
        if domain:
            filters.append(DocumentRecord.domain == domain)
        if status:
            filters.append(DocumentRecord.status == status)
        statement = select(DocumentRecord).where(*filters)
        total = self._session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        sort_column = {
            "created_at": DocumentRecord.created_at,
            "updated_at": DocumentRecord.updated_at,
            "title": DocumentRecord.title,
            "status": DocumentRecord.status,
            "domain": DocumentRecord.domain,
            "owner_service": DocumentRecord.owner_service,
        }[sort_field]
        ordering = sort_column.asc() if sort_direction == "asc" else sort_column.desc()
        records = self._session.scalars(
            statement.order_by(ordering, DocumentRecord.id.asc()).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return [self._to_domain(record) for record in records], total

    def list_versions(self, document_id: UUID) -> list[DocumentVersion]:
        records = self._session.scalars(
            select(DocumentVersionRecord)
            .where(DocumentVersionRecord.document_id == document_id)
            .order_by(DocumentVersionRecord.version_number.desc())
        ).all()
        return [self._version_to_domain(record) for record in records]

    def get_version(self, document_id: UUID, version_id: UUID) -> DocumentVersion | None:
        record = self._session.scalar(
            select(DocumentVersionRecord).where(
                DocumentVersionRecord.id == version_id, DocumentVersionRecord.document_id == document_id
            )
        )
        return self._version_to_domain(record) if record else None

    @staticmethod
    def _to_domain(record: DocumentRecord) -> Document:
        return Document(
            id=record.id,
            template_id=record.template_id,
            title=record.title,
            domain=record.domain,
            owner_service=record.owner_service,
            owner_entity_id=record.owner_entity_id,
            status=record.status,
            row_version=record.row_version,
            created_at=record.created_at,
            created_by=record.created_by,
            updated_at=record.updated_at,
            updated_by=record.updated_by,
            mime_type=record.mime_type,
            size_bytes=record.size_bytes,
            training_center_id=record.training_center_id,
            storage_key=record.storage_key,
        )

    @staticmethod
    def _version_to_domain(record: DocumentVersionRecord) -> DocumentVersion:
        return DocumentVersion(
            id=record.id,
            document_id=record.document_id,
            version_number=record.version_number,
            storage_key=record.storage_key,
            notes=record.notes,
            created_at=record.created_at,
            created_by=record.created_by,
            correlation_id=record.correlation_id,
        )
