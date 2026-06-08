"""`tx.engines` — the engine-adapter seam (design §2).

tx drives a coding-agent CLI through an `EngineAdapter` rather than calling `claude` directly. This
package holds the adapter **protocol** (`protocol.py`) and the **registry** that maps an `Engine`
enum value to its adapter (`registry.py`). The `Engine` enum itself lives in `tx.session` (a domain
entity, and housing it here would be a circular import — `engines` imports `session`).

T0 ships the surface + an empty registry only; `ClaudeEngine` and the call-site routing land in T1.
"""

from .protocol import EngineAdapter, StateSource
from .registry import get, register, registered

__all__ = ["EngineAdapter", "StateSource", "get", "register", "registered"]
