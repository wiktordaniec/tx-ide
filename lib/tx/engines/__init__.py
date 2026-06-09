from .engine_adapter import EngineAdapter, StateSource
from .registry import EngineRegistry, registry

# The adapters self-register on the shared `registry` at import; the core imports an adapter where it
# uses one. This package deliberately does NOT import them (that would couple it to every engine).
__all__ = ["EngineAdapter", "StateSource", "EngineRegistry", "registry"]
