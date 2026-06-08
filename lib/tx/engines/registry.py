"""The engine registry — maps an `Engine` enum value to its `EngineAdapter` (design §2).

T0 ships the **mechanism only**; the registry is **empty** (no engine registers itself yet) and
nothing reads it. T1 has `ClaudeEngine` call `register(Engine.CLAUDE, ClaudeEngine())` at import and
routes the core's call-sites through `get(...)`. Keeping the registry engine-blind and unread here
is what lets the spine land with zero behavior change.
"""

from __future__ import annotations

from ..session import Engine
from .protocol import EngineAdapter

# The single process-wide table. A plain module dict matches tx's no-daemon, per-invocation model:
# each `python -m tx …` rebuilds it as the adapter modules import. Empty until T1 registers Claude.
_ADAPTERS: dict[Engine, EngineAdapter] = {}


def register(engine: Engine, adapter: EngineAdapter) -> None:
    """Register `adapter` as the implementation for `engine`. Last registration wins, so a test or a
    user override can replace the default; re-registering the same engine is idempotent."""
    _ADAPTERS[engine] = adapter


def get(engine: Engine) -> EngineAdapter:
    """The adapter for `engine`. Raises `KeyError` if none is registered — an unregistered engine is
    a programming error (whatever set the engine should have registered an adapter), not a runtime
    condition to paper over with a default (DEVELOPER standard: no defensive fallbacks)."""
    return _ADAPTERS[engine]


def registered() -> frozenset[Engine]:
    """The engines that currently have an adapter — introspection for tests and the T1 conformance
    gate. Empty at T0."""
    return frozenset(_ADAPTERS)
