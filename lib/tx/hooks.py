"""Hook entry points — drive session `state` from Claude + tmux hooks (stage S2).

`tx hook <event>` routes here from the C9-baked shims the installer generates under
`$TX_IDE_HOME/hooks/` (`setup/agents/claude.sh`). Each shim drains the hook payload and execs
`python3.14 -m tx hook <event>` with the dev/real home + the package on `PYTHONPATH` baked in
(hooks run with a minimal env — C9). This module is pure dispatch over `SessionService`; all the
state-transition rules (C3 terminal guard, C4 dirty-check, the last_activity clock) live in
`record_state`, so the contract here is just *which event maps to which state*.

The event→state table (tx-service-redesign.md §5 + C6), broadened to the full Claude hook set so a
missed edge self-heals — only the three live states WORKING / WAITING / IDLE are used (§2):

| `tx hook` event | Claude hook(s)                                                        | state   |
|-----------------|-----------------------------------------------------------------------|---------|
| `prompt-submit` | UserPromptSubmit                                                      | WORKING |
| `working`       | PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart, PreCompact | WORKING |
| `stop`          | Stop, StopFailure, PermissionRequest                                  | WAITING |
| `notification`  | Notification — idle_prompt / permission_prompt / elicitation_dialog   | WAITING |
| `session-end`   | SessionEnd                                                            | IDLE    |

WORKING is reaffirmed continuously (the whole `working` family), so a missed UserPromptSubmit
self-heals on the first tool call. WAITING is backstopped by `notification` (idle_prompt) for the
cases where `Stop` never fires — a user interrupt (ESC) or an API-error turn end (StopFailure) — and
Claude re-fires idle_prompt ~60s while the terminal is unfocused (tx workers always are).

A real transition INTO WAITING / IDLE ALSO fires a history-ingest mirror (§11, S3) — keyed on the
transition, not the event, so a re-fired idle_prompt never re-mirrors. Ingest runs DETACHED —
`_trigger_ingest` re-invokes the hidden `ingest` pseudo-event (`tx hook ingest <tx-id>`) in a new
session — so it never lengthens the turn (C7's `timeout:10` is comfortably met). The `ingest` event
is not state-driven; it is dispatched before the state arm and does only the bundle copy.

`Stop` and `PermissionRequest` are wired to the SAME shim by the installer (both → `post.sh` →
`tx hook stop`), so C6 ("a permission-blocked session is waiting on you") needs no separate arm
here — it is one `stop` event. `session-closed` is the **one global tmux hook** (C2 revised:
a session's own `session-closed` does not fire at close on tmux 3.6a) — it is id-less and just
makes the reconcile-on-read EXITED sweep timely.

**D4 — hooks are mandatory, adoption is not.** Every Claude session now fires `tx hook`, but tx
only tracks what it spawned: the firing session's id comes from `$TX_SESSION_ID` (baked into the
env at spawn). A hand-started `claude` has none → no-op; an old-tx / other-home session carries
an id the (dev) home never recorded → `record_state` returns False → no-op. Either way the hook
exits 0 so it never disturbs a session tx doesn't own.

**Origin-aware `ChatRef` capture (CHD6 / F6, S4).** On `prompt-submit` the hook also ensures the
firing chat has a correct `ChatRef` on the record, reading provenance from the pane env (the shim
drains the payload, so env is the only channel): `TX_CHAT_ROLE` (default `original`),
`TX_CHAT_ORIGIN_TXID` (default self), `TX_CHAT_ORIGIN_CHAT`, and the pre-minted `TX_CHAT_ID`. This
is the idempotent BACKSTOP — `chat.py` writes each op's `ChatRef` synchronously as the primary, and
`SessionService._spawn` writes the `original` one for `--chat`; the hook only acts when something is
missing. Its load-bearing case is completing a fork whose snapshot-diff (chat.py) missed: the record
holds a null-id placeholder, and the hook fills the id by resolving the newest unclaimed transcript
(`chat.newest_unclaimed_transcript`). It never guesses a chat id it cannot determine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from . import chat, claude, history
from .service import SessionService
from .session import ChatRef, Origin, Session, State

# The tx session id is baked into every tx-spawned session's environment (`SessionService._spawn`
# sets `TX_SESSION_ID`), so a synchronous hook inherits it — the unambiguous key for "which record
# fired this", surviving the multi-pane / detached cases a `display-message` lookup would muddle.
SESSION_ID_ENV = "TX_SESSION_ID"

# `tx hook` event (as named on the command line by each shim) → the state it records, collapsed to
# the three live states (WORKING / WAITING / IDLE, §2). peon-ping's richer vocabulary
# (working / done / needs-approval / idle / …) maps onto these. WORKING is reaffirmed by the whole
# `working` family (PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart, PreCompact), so a
# missed UserPromptSubmit self-heals on the first tool call. `notification` is NOT in this table — it
# is payload-dependent (resolved in `_state_for_event` / `_notification_is_yield`).
STATE_FOR_EVENT: dict[str, State] = {
    "prompt-submit": State.WORKING,
    "working": State.WORKING,
    "stop": State.WAITING,
    "session-end": State.IDLE,
}

# Turn-boundary events that establish / complete a chat → run the (idempotent) ChatRef capture. The
# high-frequency `working` reaffirmations and `notification` skip it (the chat is settled by then,
# and running the load + project-dir glob on every PreToolUse would be pure waste).
CHAT_REF_EVENTS: frozenset[str] = frozenset({"prompt-submit", "stop", "session-end"})

# A real transition INTO one of these mirrors history (§11 / chat-ops §3.3): a turn ended (WAITING)
# or the session ended (IDLE). Keyed on the resulting state + an actual transition (not the event),
# so a re-fired idle_prompt never re-mirrors, and a missed Stop we learn of via idle_prompt still
# does. The mirror runs DETACHED (`_trigger_ingest`), so it never lengthens the turn.
INGEST_STATES: frozenset[State] = frozenset({State.WAITING, State.IDLE})

# `Notification` subtypes that mean "the agent yielded — needs you" → WAITING. idle_prompt is the
# missed-Stop backstop (Claude re-fires it ~60s while the terminal is unfocused, and tx workers are
# detached tmux panes — always unfocused). The rest (auth_success, elicitation_complete/response) are
# resume signals, not yields, so they are ignored.
WAITING_NOTIFICATION_TYPES: frozenset[str] = frozenset(
    {"idle_prompt", "permission_prompt", "elicitation_dialog"}
)


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

    if event == "ingest":
        # The detached child `_trigger_ingest` spawned: `tx hook ingest <tx-id>`. This is where the
        # actual (possibly slow) bundle mirror runs — already off the originating hook's latency
        # path — so it just does the work and exits.
        _run_ingest(service, argv[1] if len(argv) > 1 else os.environ.get(SESSION_ID_ENV, ""))
        return 0

    session_id = os.environ.get(SESSION_ID_ENV, "")
    if not session_id:
        return 0  # D4: hand-started session, no id baked → tx does not track it.

    # The state this event records. `notification` needs the payload to tell a yield (idle / needs
    # permission / question → WAITING) from a resume signal (auth ok / elicitation done → ignore), so
    # it reads stdin; every other event maps by name alone. None → unknown / non-yield → no-op.
    state = _state_for_event(event)
    if state is None:
        return 0

    # Origin-aware ChatRef capture (CHD6/F6) — only on the turn-boundary events that establish or
    # complete a chat, NOT on the high-frequency `working` reaffirmations / `notification`. The
    # post-first-prompt **Stop** is the load-bearing moment — on claude ≥2.1 a fork's transcript is
    # written lazily on the first prompt — so capture runs BEFORE ingest, and is guarded because a
    # hook must never fail a Claude turn (chat.py's synchronous write and the next event backstop it).
    if event in CHAT_REF_EVENTS:
        try:
            _capture_chat_ref(service, session_id)
        except Exception:
            pass

    # No-op when the id isn't ours (D4), the record is terminal (C3), or the state is unchanged (a
    # `working` reaffirmation / a re-fired idle_prompt) — `record_state` reports all of these as False.
    changed = service.record_state(session_id, state)

    if changed and state in INGEST_STATES:
        _trigger_ingest(session_id)
    return 0


def _state_for_event(event: str) -> State | None:
    """Map a `tx hook` event to the state it records. `notification` is payload-dependent (only the
    yield subtypes count); every other event is a static name → state lookup (`STATE_FOR_EVENT`)."""
    if event == "notification":
        return State.WAITING if _notification_is_yield() else None
    return STATE_FOR_EVENT.get(event)


def _notification_is_yield() -> bool:
    """Whether a `Notification` means the agent yielded and needs you, from its `notification_type`
    (the `notify` shim passes the payload through on stdin). See `WAITING_NOTIFICATION_TYPES`."""
    return _read_payload().get("notification_type", "") in WAITING_NOTIFICATION_TYPES


def _read_payload() -> dict:
    """Parse the hook's JSON payload from stdin, tolerantly — `{}` for no payload / unreadable / bad
    JSON. Only the `notify` shim leaves stdin connected; the others drain it (`cat >/dev/null`) and
    the tmux / ingest paths have none. A hook must never fail a turn, so any error degrades to `{}`
    (treated as a non-yield). `json.JSONDecodeError` is a `ValueError` subclass, so one except covers
    both."""
    try:
        raw = sys.stdin.read().strip()
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _capture_chat_ref(service: SessionService, session_id: str) -> None:
    """Ensure the firing chat has a correct `ChatRef` on its record (CHD6 backstop). Reads role +
    origin from the pane env (defaults `original`/self/none). No-op when the chat is already recorded
    (the common case: `_spawn` / chat.py wrote it). The two real actions are (a) completing a fork's
    null-id placeholder by resolving the chat from disk, and (b) creating a `ChatRef` the env names
    but nothing wrote yet (e.g. a handover worker if its synchronous write did not land)."""
    session = service.store.load(session_id)
    if session is None:
        return  # D4: not a record this home tracks.

    role = os.environ.get(chat.CHAT_ROLE_ENV, "original")
    origin_txid = os.environ.get(chat.CHAT_ORIGIN_TXID_ENV) or session_id
    origin_chat = os.environ.get(chat.CHAT_ORIGIN_CHAT_ENV) or None
    chat_id = os.environ.get(chat.CHAT_ID_ENV) or None

    if chat_id is None:
        _complete_pending(service, session, role, origin_chat)
        return
    if any(reference.id == chat_id for reference in session.chats):
        return  # already captured (spawn --chat / fork / handover / rollover wrote it) — no-op.
    _create_chat_ref(service, session, chat_id, role, origin_txid, origin_chat)


def _complete_pending(
    service: SessionService, session: Session, role: str, origin_chat: str | None
) -> None:
    """Fill a still-null `ChatRef` of this role by resolving the chat from disk — the fork backstop
    (the snapshot-diff in chat.py missed, so the id is None). The fork's transcript is the newest one
    not already claimed by another `ChatRef` or by the source chat. No pre-minted id and nothing
    pending → cannot determine the chat, so leave it (a later prompt's hook, or chat.py, catches it)."""
    pending = next(
        (reference for reference in session.chats if reference.id is None and reference.role == role),
        None,
    )
    if pending is None:
        return
    claimed = {reference.id for reference in session.chats if reference.id is not None}
    if origin_chat is not None:
        claimed.add(origin_chat)
    resolved = chat.newest_unclaimed_transcript(pending.cwd or session.cwd, claimed)
    if resolved is None:
        return  # transcript not on disk yet — the next prompt's hook completes it.
    pending.id = resolved
    pending.transcript_path = str(claude.transcript_path(resolved, pending.cwd or session.cwd))
    service.store.save(session)


def _create_chat_ref(
    service: SessionService, session: Session, chat_id: str, role: str,
    origin_txid: str, origin_chat: str | None,
) -> None:
    """Append a `ChatRef` the env describes but nothing wrote (the create backstop). `how` mirrors
    `role` for the derived ops; an `original` chat came from `spawn` (chat-ops §2 table)."""
    how = "spawn" if role == "original" else role
    session.chats.append(ChatRef(
        id=chat_id,
        role=role,
        cwd=session.cwd,
        transcript_path=str(claude.transcript_path(chat_id, session.cwd)),
        origin=Origin(how=how, session_id=origin_txid, chat_id=origin_chat),
        started_at=time.time(),
    ))
    service.store.save(session)


def _trigger_ingest(session_id: str) -> None:
    """Fire the history-ingest mirror DETACHED so the originating hook returns immediately (§11 /
    chat-ops §3.3 — ingest must never sit on the turn's latency path).

    Re-invokes `tx hook ingest <tx-id>` in a NEW session (`start_new_session=True`) with its stdio
    detached from Claude's hook pipe (everything → /dev/null). Detaching the stdio is what lets this
    process exit — ending the turn — while the mirror runs on: were the child holding the inherited
    hook pipe open, Claude would block reading it until the copy finished. The child inherits the
    C9-baked env the shim exec'd us with ($TX_IDE_HOME + PYTHONPATH), so it resolves the same home +
    package; `sys.executable` is the same python3.14 the shim ran. The coalescing `flock` in
    `history` means stacked Stops never pile up copies."""
    subprocess.Popen(
        [sys.executable, "-m", "tx", "hook", "ingest", session_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _run_ingest(service: SessionService, session_id: str) -> None:
    """The detached child's work: mirror this session's chat bundles. A no-op when the id is empty
    or isn't a record this home tracks (D4/D6 — `ingest_session` returns `[]`)."""
    if session_id:
        history.ingest_session(service.store, session_id)
