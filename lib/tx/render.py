"""Rendering — `tx ls` sections + the `_list` picker feed (stages S1a + S1b).

Pure formatting over `Session` objects — no I/O, no tmux. `tx ls` keeps the old two-section
VIEWS / PROCESSES shape (plain, pipe-friendly). **`picker_display_rows` is S1b's fzf feed**: a
faithful port of the old bash `sessions_with_meta` — the same tab-separated 4-column shape with a
padded name, the STARTED / IDLE columns, and per-tag colored chips, so the Python picker renders
identically to `cmd_pick`. The NAME column width (`picker_namew`) is sized once by the picker and
threaded through so the initial feed and the `reload-sync` subshells line up. S6 inserts a LOCATION
column into the picker row.
"""

from __future__ import annotations

import time

from .palette import RESET_FG, WARN_ANSI, tag_ansi
from .session import Kind, Session


def reltime(epoch: float | None, now: float | None = None) -> str:
    """Compact relative age (`-`, `5s`, `3m`, `2h`, `4d`) — port of the old bash `reltime`."""
    if not epoch:
        return "-"
    if now is None:
        now = time.time()
    delta = max(0, int(now - epoch))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _chips(tags: list[str]) -> str:
    return "".join(f" [{tag}]" for tag in tags)


def _by_recent_activity(sessions: list[Session]) -> list[Session]:
    return sorted(sessions, key=lambda session: session.last_activity or 0, reverse=True)


def render_ls(sessions: list[Session], now: float | None = None) -> str:
    """Two sections — VIEWS (kind == view) and PROCESSES — newest-activity first. Plain stdout,
    pipe-friendly. Columns: name, state, idle, tag chips."""
    if now is None:
        now = time.time()
    views: list[str] = []
    processes: list[str] = []
    for session in _by_recent_activity(sessions):
        row = "  {:<24} {:<8} {:<6}{}".format(
            session.name, session.state.value, reltime(session.last_activity, now),
            _chips(session.tags),
        )
        (views if session.kind == Kind.VIEW else processes).append(row)
    return "\n".join(["VIEWS", *views, "", "PROCESSES", *processes])


# ----- fzf picker feed (S1b) ---------------------------------------------------------------------

# Width budget past the NAME column: 3-space gap + 7 STARTED + 1 + 6 IDLE + ~14 tag chips + ~2 fzf
# gutter (the old bash `compute_namew` overhead). Names are truncated to fit; floor 12, ceiling 60.
_NAMEW_OVERHEAD = 33
_NAMEW_FLOOR = 12
_NAMEW_CEILING = 60


def picker_namew(cols: int, longest: int) -> int:
    """The NAME column width: fit the widest name, capped so STARTED / IDLE / chips still fit in
    `cols` columns. Port of the bash `compute_namew` — clamp the ceiling to [12, 60], then fit the
    longest name within it (floor 12). The picker computes this once and exports it so every
    `reload-sync` subshell renders at the same width."""
    ceiling = max(_NAMEW_FLOOR, min(_NAMEW_CEILING, cols - _NAMEW_OVERHEAD))
    return min(max(_NAMEW_FLOOR, longest), ceiling)


def _trunc(text: str, width: int) -> str:
    """Truncate to `width` display columns with a trailing `…` — port of the bash `trunc`."""
    if len(text) > width:
        return text[: width - 1] + "…"
    return text


def _picker_row(
    name: str, tags: list[str], created_at: float | None, last_activity: float | None,
    namew: int, now: float, origin: str = "L",
) -> str:
    """One tab-separated fzf row (the bash `sessions_with_meta` printf):

        name <TAB> plain_chips <TAB> origin <TAB> visual

    Field 1 is the selection key (the real session name); field 2 (`plain_chips`, ` [tag]…`) feeds
    the focus / arm headers; field 3 is the origin (`L` local); field 4 is the visual — a padded
    name, STARTED, the IDLE column in WARN yellow, and the per-tag colored chips. The picker
    displays field 4 (`--with-nth=4..`) and searches the visible text.
    """
    prefix = "(r) " if origin == "R" else ""
    name_disp = _trunc(name, namew - len(prefix))
    pad = max(0, namew - len(name_disp) - len(prefix))
    plain_chips = "".join(f" [{tag}]" for tag in tags)
    colored_chips = "".join(f" {tag_ansi(tag)}[{tag}]{RESET_FG}" for tag in tags)
    started = reltime(created_at, now)
    idle = reltime(last_activity, now)
    visual = (
        f"{prefix}{name_disp}{' ' * pad}   "
        f"{started:<7} {WARN_ANSI}{idle:<6}{RESET_FG}{colored_chips}"
    )
    return "\t".join([name, plain_chips, origin, visual])


def picker_display_rows(sessions: list[Session], namew: int, now: float | None = None) -> str:
    """The fzf picker feed, newest-activity first, views excluded (§7 — the everyday picker never
    lists the home base). Consumed by both the initial paint and each `reload-sync` (`tx _list`)."""
    if now is None:
        now = time.time()
    rows = [
        _picker_row(
            session.name, session.tags, session.created_at, session.last_activity, namew, now,
        )
        for session in _by_recent_activity(sessions)
        if session.kind != Kind.VIEW
    ]
    return "\n".join(rows)
