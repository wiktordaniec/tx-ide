"""Provenance log (§16, D8) — "just logs of what happened," enough to recreate the action.

One append-only file, `$TX_IDE_HOME/log.jsonl`. Each mutation logs **one line** (the settled §19
answer: default is one line per mutation), shaped per **D8**:

    {"ts": <epoch seconds>, "actor": "<TX_SESSION_ID>", "type": "<short type>", "msg": "<text>"}

`actor` is the tx session id that did it (`$TX_SESSION_ID`) — it distinguishes the user vs the
tx-assistant vs a worker vs a hook. `type` is a short tag (`spawn`, `state`, `tag`, `kill`, …);
`msg` is a short human description.

**Atomicity without a lock.** Each line is written with a single `os.write` to an `O_APPEND`
descriptor. POSIX guarantees an `O_APPEND` write up to `PIPE_BUF` (512 bytes on macOS) lands
atomically, so concurrent writers (hooks, the ~1 Hz picker reload, the user's `tx`) interleave
whole lines and never tear one — the proper version of what `inbox.jsonl` botched (no lock, never
rewritten). Callers therefore keep lines short and must **not** dump a full `cmd`/`env`; `_encode`
truncates the `msg` as a safety net if a line would exceed `PIPE_BUF`.

Logging is internal to the python classes (written from the `SessionService` mutation chokepoint,
S1a) — there is no `tx log` verb (§16); reading is `cat`/grep or the HISTORIAN.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .storage import log_path

ACTOR_ENV = "TX_SESSION_ID"
# macOS PIPE_BUF; an O_APPEND write within this many bytes is atomic. The newline is included in
# the budget so the whole line (including its terminator) fits in one atomic write.
PIPE_BUF = 512


class EventLog:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else log_path()

    def append(self, type: str, msg: str, *, actor: str | None = None) -> None:
        """Append one provenance line. `actor` defaults to `$TX_SESSION_ID` (empty if unset, e.g.
        a hand-started session). `ts` is epoch seconds."""
        if actor is None:
            actor = os.environ.get(ACTOR_ENV, "")
        line = self._encode({"ts": time.time(), "actor": actor, "type": type, "msg": msg})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(descriptor, line)
        finally:
            os.close(descriptor)

    def _encode(self, record: dict) -> bytes:
        """Compact JSON + newline, kept within PIPE_BUF so the append stays atomic. If the line is
        too long, the `msg` field is truncated to fit (provenance, not payload — losing the tail
        of an over-long message is acceptable; tearing a concurrent write is not)."""
        line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) <= PIPE_BUF:
            return line
        skeleton = json.dumps({**record, "msg": ""}, separators=(",", ":")) + "\n"
        budget = max(0, PIPE_BUF - len(skeleton.encode("utf-8")))
        trimmed = record["msg"].encode("utf-8")[:budget].decode("utf-8", "ignore")
        return (json.dumps({**record, "msg": trimmed}, separators=(",", ":")) + "\n").encode("utf-8")
