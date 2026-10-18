"""SQLAlchemy adapter for document.document_template."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Boolean, DateTime, Integer, String, Text, Uuid, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from document.domain.model.template import DocumentTemplate


class Base(DeclarativeBase):
    pass


class DocumentTemplateRecord(Base):
    __tablename__ = "document_template"
    __table_args__ = {"schema": "document"}

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    template_body: Mapped[str] = mapped_column(Text, nullable=False)
    output_type: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    updated_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class SqlAlchemyTemplateRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list(
        self,
        *,
        page: int,
        page_size: int,
        output_type: str | None,
        code: str | None,
        is_active: bool | None,
        sort_field: str,
        sort_direction: str,
    ) -> tuple[list[DocumentTemplate], int]:
        statement = select(DocumentTemplateRecord)
        filters = []
        if output_type:
            filters.append(DocumentTemplateRecord.output_type == output_type)
        if code:
            filters.append(DocumentTemplateRecord.code == code)
        if is_active is not None:
            filters.append(DocumentTemplateRecord.is_active == is_active)
        if filters:
            statement = statement.where(*filters)
        total = self._session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        sort_column = {
            "code": DocumentTemplateRecord.code,
            "name": DocumentTemplateRecord.name,
            "output_type": DocumentTemplateRecord.output_type,
            "version": DocumentTemplateRecord.version,
            "is_active": DocumentTemplateRecord.is_active,
            "created_at": DocumentTemplateRecord.created_at,
            "updated_at": DocumentTemplateRecord.updated_at,
        }[sort_field]
        ordering = sort_column.asc() if sort_direction == "asc" else sort_column.desc()
        records = self._session.scalars(
            statement.order_by(ordering, DocumentTemplateRecord.id.asc()).offset((page - 1) * page_size).limit(page_size)
        ).all()
        return [self._to_domain(record) for record in records], total

    def get(self, identifier: UUID | str) -> DocumentTemplate | None:
        if isinstance(identifier, UUID):
            record = self._session.get(DocumentTemplateRecord, identifier)
        else:
            record = self._session.scalar(select(DocumentTemplateRecord).where(DocumentTemplateRecord.code == identifier))
        return self._to_domain(record) if record else None

    def get_by_id(self, identifier: UUID) -> DocumentTemplate | None:
        return self.get(identifier)

    def exists_by_code(self, code: str) -> bool:
        return self._session.scalar(select(DocumentTemplateRecord.id).where(DocumentTemplateRecord.code == code).limit(1)) is not None

    def save(self, template: DocumentTemplate) -> DocumentTemplate:
        record = self._session.get(DocumentTemplateRecord, template.id)
        if record is None:
            record = DocumentTemplateRecord(id=template.id)
            self._session.add(record)
        record.code = template.code
        record.name = template.name
        record.template_body = template.template_body
        record.output_type = template.output_type
        record.version = template.version
        record.is_active = template.is_active
        record.created_at = template.created_at
        record.created_by = template.created_by
        record.updated_at = template.updated_at
        record.updated_by = template.updated_by
        self._session.commit()
        self._session.refresh(record)
        return self._to_domain(record)

    @staticmethod
    def _to_domain(record: DocumentTemplateRecord) -> DocumentTemplate:
        return DocumentTemplate(
            id=record.id,
            code=record.code,
            name=record.name,
            template_body=record.template_body,
            output_type=record.output_type,
            version=record.version,
            is_active=record.is_active,
            created_at=record.created_at,
            created_by=record.created_by,
            updated_at=record.updated_at,
            updated_by=record.updated_by,
        )
