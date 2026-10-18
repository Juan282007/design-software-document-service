"""Composition root for the template-api deployable."""

from document.adapter.inbound.http.template_api import create_template_app

app = create_template_app()
