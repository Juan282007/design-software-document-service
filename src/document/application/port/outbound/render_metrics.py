"""Outbound port for the operational metrics of the renderer worker."""

from __future__ import annotations

from typing import Protocol


class RenderMetrics(Protocol):
    def processed(self, outcome: str, seconds: float) -> None:
        """Count a handled message (outcome: success, duplicate or failed) and its duration."""

    def retried(self) -> None: ...

    def dead_lettered(self) -> None: ...


class NoopRenderMetrics:
    def processed(self, outcome: str, seconds: float) -> None:
        return None

    def retried(self) -> None:
        return None

    def dead_lettered(self) -> None:
        return None
