"""Composition root for the document-api deployable."""

from document.adapter.inbound.http.document_api import create_document_app

app = create_document_app()
