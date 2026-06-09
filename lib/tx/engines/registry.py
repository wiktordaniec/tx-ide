from __future__ import annotations

from ..session import Engine
from .engine_adapter import EngineAdapter


class EngineRegistry:
    """Maps each Engine to its adapter. One process-wide instance (`registry`), rebuilt each
    `python -m tx …` run as the adapter modules self-register at import."""

    def __init__(self) -> None:
        self._adapters: dict[Engine, EngineAdapter] = {}

    def register(self, engine: Engine, adapter: EngineAdapter) -> None:
        self._adapters[engine] = adapter  # last registration wins — lets a test swap the default

    def get(self, engine: Engine) -> EngineAdapter:
        return self._adapters[engine]  # KeyError on an unregistered engine, by design — no fallback

    def registered(self) -> frozenset[Engine]:
        return frozenset(self._adapters)


registry = EngineRegistry()
