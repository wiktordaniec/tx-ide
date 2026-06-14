"""Hook entry points — drive session `state` from Claude + tmux hooks (stage S2).

`tx hook <event>` routes here from the C9-baked shims the installer generates under
`$TX_IDE_HOME/hooks/` (`setup/engines/claude.sh`). Each shim drains the hook payload and execs
`python3.14 -m tx hook <event>` with the dev/real home + the package on `PYTHONPATH` baked in
(hooks run with a minimal env — C9). This module is pure dispatch over `SessionService`; all the
state-transition rules (C3 terminal guard, C4 dirty-check, the last_activity clock) live in
`record_state`, so the contract here is just *which event maps to which state*.

The event→state table (tx-service-redesign.md §5 + C6), broadened to the full Claude hook set so a
missed edge self-heals — only the three live states WORKING / WAITING / IDLE are used (§2):

| `tx hook` event | Claude hook(s)                                                        | state   |
|-----------------|-----------------------------------------------------------------------|---------|
| `session-start` | SessionStart                                                         | — (capture only) |
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

**Chat-id capture (design §2/§7, task T4).** The session id is CAPTURED from the hook payload, never
pre-minted. Every spawn writes a PENDING `ChatRef` (`id=None`) onto the record synchronously —
`SessionService._spawn` for an `original`, `chat.py` for fork / handover / rollover — before the agent
launches. The first hook that carries the payload fills it: `session-start` at startup, else
`prompt-submit` on the first turn (the two events whose shim keeps stdin). It reads `session_id` +
`transcript_path` off the payload via `engine.capture_session_id(payload)` and stamps them onto the
pending ref (`_complete_pending`). Idempotent: once filled — or if the captured id is already
recorded — it is a no-op, so a re-fired hook never duplicates and a fork's `session-start` miss is
completed by its first `prompt-submit`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from . import history
from .engines import registry
from .service import SessionService
from .session import Session, State

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

# The capture events: those whose shim keeps stdin AND establish the chat — `session-start` (at
# startup, the earliest the id is known) and `prompt-submit` (the first-turn backstop, e.g. a fork
# whose `session-start` fired before its pending ref was written). Capture is idempotent, so the two
# together close the window. `working` / `stop` / `session-end` drain stdin (no payload to read), and
# `notification` re-fires ~60s — none capture.
CHAT_REF_EVENTS: frozenset[str] = frozenset({"session-start", "prompt-submit"})

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
        _run_ingest(
            service, argv[1] if len(argv) > 1 else os.environ.get(SESSION_ID_ENV, "")
        )
        return 0

    session_id = os.environ.get(SESSION_ID_ENV, "")
    if not session_id:
        return 0  # D4: hand-started session, no id baked → tx does not track it.

    # Chat-id capture (T4) — on the events that carry the payload + establish a chat (`session-start`
    # at startup, `prompt-submit` on the first turn). Runs BEFORE the state arm because `session-start`
    # drives no state (it would short-circuit below). Guarded: a hook must never fail a Claude turn —
    # the next capture event backstops a malformed/early payload.
    if event in CHAT_REF_EVENTS:
        try:
            _capture_chat_ref(service, session_id)
        except Exception:
            pass

    # The state this event records. `notification` needs the payload to tell a yield (idle / needs
    # permission / question → WAITING) from a resume signal (auth ok / elicitation done → ignore), so
    # it reads stdin; every other event maps by name alone. None → capture-only / unknown / non-yield.
    state = _state_for_event(event)
    if state is None:
        return 0

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
    """Fill the firing session's pending `ChatRef` from the hook payload (the universal capture path,
    design §2/§7). Reads `(session_id, transcript_path)` off the payload via the session's engine and
    stamps them onto the pending ref. No-op when the id isn't ours (D4); the engine's
    `capture_session_id` raises on a malformed/empty payload, which `dispatch` swallows (a hook must
    never fail a turn — the next capture event retries)."""
    session = service.store.load(session_id)
    if session is None:
        return  # D4: not a record this home tracks.
    captured_id, captured_path = registry.get(session.engine).capture_session_id(_read_payload())
    _complete_pending(service, session, captured_id, captured_path)


def _complete_pending(
    service: SessionService, session: Session, captured_id: str, captured_path: str
) -> None:
    """Stamp the captured `(id, transcript_path)` onto the session's latest pending `ChatRef` (id is
    None). Idempotent: skip when the id is already recorded (re-fired hook) or nothing is pending.
    Lazy-fork guard: a fork firing with an id equal to its own `origin.chat_id` is "not ready" (the
    source id, not the fork's lazily-minted one) — leave it pending for its first prompt to fill.
    Ownership guard: a captured id already recorded on ANOTHER session is a crossover — a leaked /
    stale `TX_SESSION_ID` fired this hook carrying a chat that isn't ours — so refuse it (leave the ref
    pending for the legitimate payload) rather than cross-wire two sessions onto one chat."""
    if any(reference.id == captured_id for reference in session.chats):
        return
    pending = next(
        (reference for reference in reversed(session.chats) if reference.id is None),
        None,
    )
    if pending is None:
        return
    if pending.role == "fork" and captured_id == pending.origin.chat_id:
        return
    if _owned_by_other_session(service, session.id, captured_id):
        service.log.append(
            "capture-skip",
            f"{session.name}: chat {captured_id} owned by another session — refused cross-bind",
        )
        return
    pending.id = captured_id
    pending.transcript_path = captured_path
    service.store.save(session)


def _owned_by_other_session(service: SessionService, session_id: str, captured_id: str) -> bool:
    """True when `captured_id` is already a recorded chat on a DIFFERENT session. A chat id lives on
    exactly one tx session across fork / handover / rollover (resume records a known-id ref, caught by
    the idempotency check before this runs), so any other owner means the payload belongs to that
    session, not ours — the fingerprint of a leaked / stale `TX_SESSION_ID`."""
    return any(
        other.id != session_id and any(reference.id == captured_id for reference in other.chats)
        for other in service.store.all()
    )


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
