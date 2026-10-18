"""HTML to PDF with xhtml2pdf (pure Python: no system libraries or headless browser required)."""

from __future__ import annotations

import io

from xhtml2pdf import pisa
from xhtml2pdf.config.resources import ResourceAccessPolicy

from document.domain.model.document import RenderError


def strict_resource_policy() -> ResourceAccessPolicy:
    """What a rendered template may load: nothing outside the document itself.

    The HTML decides what the renderer fetches (images, stylesheets, fonts), so a template could point it at
    an internal URL (SSRF) or at a file of the worker (settings, credentials). Only embedded `data:` resources
    are allowed: no network, no local files, and the size and time limits of the library's server preset.
    A refused resource is left out of the PDF and logged by the renderer.
    """
    return ResourceAccessPolicy.server(base_dir=None, allow_remote=False)


class Xhtml2PdfRenderer:
    def __init__(self, policy: ResourceAccessPolicy | None = None) -> None:
        self._policy = policy or strict_resource_policy()

    def render(self, html: str) -> bytes:
        buffer = io.BytesIO()
        try:
            result = pisa.CreatePDF(html, dest=buffer, encoding="utf-8", resource_policy=self._policy)
        except Exception as error:  # noqa: BLE001 - the engine raises many kinds of errors
            raise RenderError("el motor de PDF falló") from error
        if result.err:
            raise RenderError("el motor de PDF reportó errores al convertir el HTML")
        return buffer.getvalue()
