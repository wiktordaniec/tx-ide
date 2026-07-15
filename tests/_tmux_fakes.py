"""Shared in-memory `Tmux` stand-in for the session-split gate tests (dev-only).

No live tmux server: `FakeTmux` records the calls the split's tests assert on (`@tx_view` stamps,
kills, new sessions, options) and answers liveness from an in-memory set. Configurable per test.
Deleted before merge along with the rest of `tests/` (the repo keeps no unit tests on main).
"""

from __future__ import annotations


class FakeTmux:
    def __init__(self, live=None, views=None, list_rows=None):
        self.live: set[str] = set(live or [])          # session names/ids that "exist"
        self.views: set[str] = set(views or [])        # sessions carrying the @tx_view marker
        self.tx_ids: dict[str, str] = {}               # name -> @tx_id
        self.options: dict[tuple, str] = {}            # (target, option[, "win"]) -> value
        self.created: list[tuple[str, str, str]] = []  # (name, cwd, command) from new_session
        self.killed: list[str] = []
        self.list_rows: list[str] = list(list_rows or [])
        self._pid = 4242

    # ----- lifecycle -----
    def has_session(self, name: str) -> bool:
        return name in self.live

    def new_session(self, *, name: str, cwd: str, command: str, env: dict) -> int:
        self.live.add(name)
        self.created.append((name, cwd, command))
        self._pid += 1
        return self._pid

    def kill_session(self, name: str) -> bool:
        self.killed.append(name)
        self.live.discard(name)
        self.views.discard(name)
        return True

    def rename_session(self, old: str, new: str) -> None:
        self.live.discard(old)
        self.live.add(new)

    def list_sessions(self, fmt: str) -> list[str]:
        return list(self.list_rows)

    # ----- options / markers -----
    def set_option(self, target: str, option: str, value: str, *, pane: bool = False) -> None:
        self.options[(target, option)] = value

    def set_window_option(self, target: str, option: str, value: str) -> None:
        self.options[(target, option, "win")] = value

    def show_option(self, target: str, option: str) -> str | None:
        return self.options.get((target, option))

    def get_tx_id(self, name: str) -> str | None:
        return self.tx_ids.get(name)

    def set_tx_id(self, name: str, session_id: str) -> None:
        self.tx_ids[name] = session_id
        self.options[(name, "@tx_id")] = session_id

    def set_tx_view(self, name: str) -> None:
        self.views.add(name)
        self.options[(name, "@tx_view")] = "1"

    def is_view(self, name: str) -> bool:
        return name in self.views

    # ----- topology / misc -----
    def current_session_name(self) -> str | None:
        return None

    def attached_to(self, name: str) -> list:
        return []

    def attachment_map(self) -> dict:
        return {}


class NoReconcile:
    """A reconciler that does nothing — `_spawn`'s `_require_name_free` calls `reconcile()`."""

    def reconcile(self):
        return []
