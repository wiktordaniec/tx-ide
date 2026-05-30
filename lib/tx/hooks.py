"""Hook entry points — drive session `state` from Claude + tmux hooks (stage S2).

`tx hook <event>` routes here from the C9-baked shims the installer generates under
`$TX_IDE_HOME/hooks/` (`setup/agents/claude.sh`). Each shim drains the hook payload and execs
`python3.14 -m tx hook <event>` with the dev/real home + the package on `PYTHONPATH` baked in
(hooks run with a minimal env — C9). This module is pure dispatch over `SessionService`; all the
state-transition rules (C3 terminal guard, C4 dirty-check, the last_activity clock) live in
`record_state`, so the contract here is just *which event maps to which state*.

The event→state table (tx-service-redesign.md §5 + C6):

| `tx hook` event  | Claude hook                     | effect                                   |
|------------------|---------------------------------|------------------------------------------|
| `prompt-submit`  | UserPromptSubmit                | `state = WORKING` (+ bump last_activity) |
| `stop`           | Stop **and** PermissionRequest  | `state = WAITING` — needs you (C6)       |
| `session-end`    | SessionEnd                      | `state = IDLE` (NOT exited — §2) + ingest|

`Stop` and `PermissionRequest` are wired to the SAME shim by the installer (both → `post.sh` →
`tx hook stop`), so C6 ("a permission-blocked session is waiting on you") needs no separate arm
here — it is one `stop` event. `session-closed` is the **one global tmux hook** (C2 revised:
a session's own `session-closed` does not fire at close on tmux 3.6a) — it is id-less and just
makes the reconcile-on-read EXITED sweep timely.

**D4 — hooks are mandatory, adoption is not.** Every Claude session now fires `tx hook`, but tx
only tracks what it spawned: the firing session's id comes from `$TX_SESSION_ID` (baked into the
env at spawn). A hand-started `claude` has none → no-op; an old-tx / other-home session carries
an id the (dev) home never recorded → `record_state` returns False → no-op. Either way the hook
exits 0 so it never disturbs a session tx doesn't own. Origin-aware fork env (CHD6
`TX_CHAT_ROLE` / `TX_CHAT_ORIGIN_*`) is S4 — not read here.
"""

from __future__ import annotations

import os

from .service import SessionService
from .session import State

# The tx session id is baked into every tx-spawned session's environment (`SessionService._spawn`
# sets `TX_SESSION_ID`), so a synchronous hook inherits it — the unambiguous key for "which record
# fired this", surviving the multi-pane / detached cases a `display-message` lookup would muddle.
SESSION_ID_ENV = "TX_SESSION_ID"

# Claude hook event (as named on the `tx hook` command line) → the state it records. Stop and
# PermissionRequest share the `stop` event (the installer points both at one shim), so C6 is folded
# in here rather than carried as a distinct event.
STATE_FOR_EVENT: dict[str, State] = {
    "prompt-submit": State.WORKING,
    "stop": State.WAITING,
    "session-end": State.IDLE,
}


def dispatch(service: SessionService, argv: list[str]) -> int:
    """Apply the hook named by `argv[0]`. Always returns 0 — a hook must never fail a Claude turn
    or a tmux session-close, and "not ours" (D4) is a success, not an error."""
    event = argv[0] if argv else ""

    # The global tmux `session-closed` hook (C2): id-less, fired for ANY session close. It only
    # needs to trigger the bounded reconcile sweep (one `list-sessions` diff) so a vanished record
    # is stamped EXITED promptly instead of waiting for the next picker read.
    if event == "session-closed":
        service.reconcile()
        return 0

    session_id = os.environ.get(SESSION_ID_ENV, "")
    if not session_id:
        return 0  # D4: hand-started session, no id baked → tx does not track it.

    state = STATE_FOR_EVENT.get(event)
    if state is None:
        return 0  # Unknown event (e.g. a future hook wired before its arm exists) → no-op.

    # No-op when the id isn't in this home's store (D4 — old-tx / other-home session) or the record
    # is already terminal (C3); `record_state` reports both as False, which the hook ignores.
    service.record_state(session_id, state)

    if event == "session-end":
        _ingest_chat(service, session_id)
    return 0


def _ingest_chat(service: SessionService, session_id: str) -> None:
    """History-ingest trigger point for `SessionEnd` (§11). The ingest itself — mirror the chat
    bundle into `$TX_IDE_HOME/history/<tx-id>/<chat-uuid>/` — is **S3**; this is the no-op seam it
    plugs into so the hook table is complete now and S3 changes only one call site."""
    return
