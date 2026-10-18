"""The PDF engine must not load anything outside the template: no network and no local files."""

from __future__ import annotations

import base64
import io
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from PIL import Image

from document.adapter.outbound.rendering.xhtml2pdf_renderer import Xhtml2PdfRenderer, strict_resource_policy


def _noise_png() -> bytes:
    """An image that is large once embedded, so a loaded image shows in the size of the PDF."""
    image = Image.frombytes("RGB", (96, 96), os.urandom(96 * 96 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _page(img_src: str | None) -> str:
    image = f'<img src="{img_src}" width="96" height="96"/>' if img_src else ""
    return f"<html><body><h1>Constancia</h1><p>Hola Ana</p>{image}</body></html>"


class _Recorder(BaseHTTPRequestHandler):
    hits: list[str] = []
    png = b""

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        type(self).hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.png)))
        self.end_headers()
        self.wfile.write(self.png)

    def log_message(self, *_: object) -> None:
        return


@pytest.fixture()
def image_server():
    _Recorder.hits = []
    _Recorder.png = _noise_png()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/logo.png", _Recorder
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_embedded_data_images_are_still_rendered() -> None:
    png = _noise_png()
    renderer = Xhtml2PdfRenderer()

    plain = renderer.render(_page(None))
    embedded = renderer.render(_page("data:image/png;base64," + base64.b64encode(png).decode()))

    assert embedded.startswith(b"%PDF")
    assert len(embedded) - len(plain) > len(png) // 2


def test_remote_resources_are_not_fetched(image_server) -> None:
    url, recorder = image_server
    renderer = Xhtml2PdfRenderer()

    pdf = renderer.render(_page(url))

    assert pdf.startswith(b"%PDF")
    assert recorder.hits == []
    assert len(pdf) < len(renderer.render(_page(None))) + len(recorder.png) // 2


def test_local_files_are_not_read(tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(_noise_png())
    renderer = Xhtml2PdfRenderer()

    plain = renderer.render(_page(None))
    with_path = renderer.render(_page(str(secret)))
    with_file_uri = renderer.render(_page(secret.as_uri()))

    for pdf in (with_path, with_file_uri):
        assert pdf.startswith(b"%PDF")
        assert len(pdf) - len(plain) < secret.stat().st_size // 4


def test_the_default_policy_is_the_strict_one() -> None:
    policy = strict_resource_policy()

    assert policy.allow_remote is False
    assert policy.base_dir is None
    assert policy.max_render_seconds is not None and policy.max_resource_bytes is not None
