"""`tx.engines` — the engine-adapter seam (design §2).

tx drives a coding-agent CLI through an `EngineAdapter` rather than calling `claude` directly. This
package holds the adapter **protocol** (`protocol.py`), the **registry** that maps an `Engine` enum
value to its adapter (`registry.py`), and the concrete adapters (`claude.py`). The `Engine` enum
itself lives in `tx.session` (a domain entity, and housing it here would be a circular import —
`engines` imports `session`).

Each adapter module **registers itself at import** (`claude.py` ends with
`register(Engine.CLAUDE, ClaudeEngine())`), so importing an adapter — which the core does via
`from .engines import claude` for its still-transitional path helpers — populates the registry. The
package __init__ deliberately does NOT import the adapters (that would couple the surface to every
engine, and the old top-level module name is gated against re-introduction); the core imports them
where it uses them.
"""

from .protocol import EngineAdapter, StateSource
from .registry import get, register, registered

__all__ = ["EngineAdapter", "StateSource", "get", "register", "registered"]
