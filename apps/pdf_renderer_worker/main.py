"""Composition root for the pdf-renderer-worker deployable (python -m apps.pdf_renderer_worker.main)."""

from document.adapter.inbound.messaging.pdf_renderer_worker import run_worker

if __name__ == "__main__":
    run_worker()
