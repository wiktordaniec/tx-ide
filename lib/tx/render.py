"""Rendering — `tx ls` sections + the `_list` picker feed (stage S1a).

Pure formatting over `Session` objects — no I/O, no tmux. `tx ls` keeps the old two-section
VIEWS / PROCESSES shape, adding the new `state` column. `_list` emits a stable tab-separated feed
for the interactive picker: **S1b builds the fzf UI on top of these rows; S6 inserts a LOCATION
column** (attachment-topology §5). Color/ANSI is the picker's job (S1b), so this stays plain and
pipeable.
"""

from __future__ import annotations

import time

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


def picker_rows(sessions: list[Session], now: float | None = None) -> str:
    """Tab-separated picker feed, newest-activity first. Columns:

        name <TAB> search-text <TAB> state <TAB> started <TAB> idle <TAB> tags-csv

    `search-text` (name + tags) is what the picker's fuzzy filter matches. Views are excluded —
    the everyday picker never lists them (§7). S1b renders/colors these rows; S6 adds LOCATION.
    """
    if now is None:
        now = time.time()
    rows: list[str] = []
    for session in _by_recent_activity(sessions):
        if session.kind == Kind.VIEW:
            continue
        rows.append("\t".join([
            session.name,
            " ".join([session.name, *session.tags]),
            session.state.value,
            reltime(session.created_at, now),
            reltime(session.last_activity, now),
            ",".join(session.tags),
        ]))
    return "\n".join(rows)
