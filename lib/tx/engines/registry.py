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

    def engine_for_command(self, command: str) -> Engine | None:
        """The registered engine whose binary `command` invokes (by adapter `matches_binary`), or
        None when none matches — lets a hand-written `--cmd` spawn stamp the right engine instead of
        blind-defaulting to Claude (e.g. `--cmd 'codex …'` without `--engine codex`)."""
        for engine, adapter in self._adapters.items():
            if adapter.matches_binary(command):
                return engine
        return None


registry = EngineRegistry()
