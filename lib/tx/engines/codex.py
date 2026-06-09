"""`CodexEngine` — the OpenAI Codex adapter (design §5), and the Codex specifics behind it.

The Codex counterpart to `engines/claude.py`: the one place Codex specifics live — the rollout-glob
transcript rule, the launch-flag builders (fresh / resume / fork / seed / distiller), the OpenAI
Responses-item message normalization, and the sidecar-free history bundle — expressed as an
`EngineAdapter` (design §2) the engine-blind core calls through the registry. The module registers
itself at import: `register(Engine.CODEX, …)` (mirroring `claude.py`).

Identity is **capture-after-launch** (design §2): the session id + transcript path are read off the
hook payload (`capture_session_id`), never pre-minted — Codex mints its own id and our hook captures
it, exactly the uniform path the README describes ("the session id is captured, not chosen").

Ground truth measured by the T2 spike (`docs/engine/verification.md`, codex-cli 0.137.0):
  - Launch:     `codex -m gpt-5.5 -c model_reasoning_effort=high
                 --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust "<seed>"`
                — the positional prompt auto-submits in the interactive TUI (Evidence 1); the
                `--dangerously-bypass-hook-trust` flag is what fires our (untrusted) hooks headlessly
                (Evidence 3/4 A/B — yolo alone stops at the trust gate). Kept on every op (design
                §4.5 / Q-D3 bypass-first); T3 does NOT pre-seed a trust hash, so do not rely on one.
  - Ops:        native `codex resume <id>` / `codex fork <id>` (both accept the bypass flags);
                handover / rollover seed a fresh `codex "<seed>"`. A fork mints a NEW id, recorded as
                `forked_from_id` on its rollout's `session_meta` (Evidence 2) — captured post-hoc.
  - Transcript: `<codex-home>/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<session-id>.jsonl`; the hook
                payload's `transcript_path` is the canonical realpath and is preferred when stored on
                the `ChatRef`, else the rollout is found by the id glob (the id is unique, so the path
                is cwd-independent — there is no separate moved-cwd fallback as Claude needs). Messages
                are OpenAI Responses items — parsed by T7's `codex_rollout` (imported, not re-derived).
  - Bundle:     the rollout JSONL **alone — no sidecar** (design §6.4): Codex inlines tool calls /
                reasoning in the rollout, so there is no sibling dir to copy (contrast Claude's
                `subagents/` + `tool-results/`). The shared copy mechanism stays in `history.py`.
  - State:      hook events drive WORKING/WAITING (design §3); there is **no session-end event** — a
                finished Codex turn rests in WAITING ("needs you"), and EXITED still comes from
                tmux-close→reconcile (already engine-agnostic). `SessionStart` is capture-only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..session import ChatRef, Engine, State
from . import codex_rollout
from .claude import bundle_dir
from .protocol import EngineAdapter, StateSource
from .registry import register

# Codex's own home (where it writes rollouts). Honors $CODEX_HOME like the CLI does
# (`os.environ.get("CODEX_HOME", "~/.codex")`, verification.md §"Method — isolation"); defaults to
# ~/.codex. This is Codex's home, NOT $TX_IDE_HOME.
CODEX_HOME_ENV = "CODEX_HOME"
DEFAULT_CODEX_HOME = "~/.codex"

CODEX_BIN = "codex"

# Codex defaults (design §4.4): model gpt-5.5 (priority-0 in the local models cache, the documented
# coding default — no `-codex` variant this generation), reasoning effort high (second-to-highest;
# the top is xhigh). Rendered the Codex way — `-m <model> -c model_reasoning_effort=<effort>` — where
# effort is a `-c` config override rather than a flag (design §3, the cross-engine effort axis).
CODEX_MODEL = "gpt-5.5"
CODEX_EFFORT = "high"
REASONING_EFFORT_KEY = "model_reasoning_effort"

# yolo (design §4.5): bypass approvals+sandbox AND hook-trust. The hook-trust bypass is INDEPENDENTLY
# required — the T2 spike's A/B proved a yolo-only codex stops at the trust gate and fires NO hooks,
# while adding this flag runs our (untrusted) hooks for the invocation (verification.md Evidence 3/4).
# Kept on every tx-built op so hooks fire; both `resume` and `fork` accept it (verified via --help).
BYPASS_APPROVALS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
BYPASS_HOOK_TRUST_FLAG = "--dangerously-bypass-hook-trust"
YOLO_FLAGS = [BYPASS_APPROVALS_FLAG, BYPASS_HOOK_TRUST_FLAG]

# The rollout tree + filename rule (design §3; verification.md §"Rollout path + transcript shape"). A
# rollout is `sessions/<Y>/<M>/<D>/rollout-<ts>-<id>.jsonl`; the id is unique across the tree, so a
# recursive `**` glob (matching zero+ intermediate date dirs) resolves it regardless of nesting.
SESSIONS_DIR = "sessions"
ROLLOUT_PREFIX = "rollout-"
TRANSCRIPT_SUFFIX = ".jsonl"


# ----- transcript / path internals ----------------------------------------------------------

def codex_home() -> Path:
    return Path(os.environ.get(CODEX_HOME_ENV, DEFAULT_CODEX_HOME)).expanduser()


def sessions_root() -> Path:
    """`<codex-home>/sessions` — the date-partitioned rollout tree."""
    return codex_home() / SESSIONS_DIR


def find_rollout(chat_id: str) -> Path | None:
    """Resolve a chat's rollout by globbing the sessions tree for `rollout-*-<id>.jsonl`, returning it
    only if present (else None). The id is unique across the tree, so a single hit is expected; the
    most recent is taken defensively if several ever match. A rollout's path is independent of the
    chat's cwd (it is date+timestamp+id, not a munged cwd like Claude), so this glob is already
    cross-project — Codex needs no separate moved-cwd resolver."""
    pattern = f"**/{ROLLOUT_PREFIX}*-{chat_id}{TRANSCRIPT_SUFFIX}"
    matches = sorted(sessions_root().glob(pattern))
    return matches[-1] if matches else None


# ----- the adapter --------------------------------------------------------------------------
# Codex renders each hook event onto the live states (design §3): the whole "working" family
# (prompt-submit, tool use, compaction, subagent start) affirms WORKING; Stop and a permission
# request yield WAITING. `SessionStart` is capture-only (it carries the id+path, not a state edge),
# and Codex has **no session-end event** — a finished turn rests in WAITING and EXITED comes from
# tmux-close→reconcile. This mirrors `claude.py`'s `_EVENT_TO_STATE`, keyed by Codex's own hook event
# names (verification.md "Hook payload field reference"). Codex carries no `*Failure` events.
_EVENT_TO_STATE: Mapping[str, State] = {
    "UserPromptSubmit": State.WORKING,
    "PreToolUse": State.WORKING,
    "PostToolUse": State.WORKING,
    "PreCompact": State.WORKING,
    "PostCompact": State.WORKING,
    "SubagentStart": State.WORKING,
    "Stop": State.WAITING,
    "PermissionRequest": State.WAITING,
}


class CodexEngine(EngineAdapter):
    """The OpenAI Codex adapter — the protocol surface (design §2/§5) over the Codex specifics above.

    Stateless: a single instance is registered for `Engine.CODEX`. The launch/ops builders return
    argv lists (the binary first) with Codex's model/effort rendering (`-m` / `-c
    model_reasoning_effort=`) and its yolo + hook-trust-bypass flags baked in, so callers stay
    engine-blind (design §4.3). Spawn wiring (`--engine codex`, `infer_role`/reconcile recognition) is
    T8 — this is the adapter T8 drives; the default engine stays Claude.
    """

    # ----- identity ------------------------------------------------------------------------

    @property
    def binary(self) -> str:
        return CODEX_BIN

    def matches_binary(self, command: str) -> bool:
        """Whether `command` invokes codex — the basename of its first token is the codex binary (a
        full path like `/opt/homebrew/bin/codex …` still matches). Mirrors `ClaudeEngine`'s test so
        `infer_role`/reconcile recognize a Codex session from its command (T8)."""
        tokens = command.split()
        return bool(tokens) and os.path.basename(tokens[0]) == CODEX_BIN

    def capture_session_id(self, hook_payload: dict) -> tuple[str, str]:
        """Read `(session_id, transcript_path)` off Codex's hook payload — both keys are present on
        every Codex hook event (verification.md "Hook payload field reference"). The id is captured,
        never minted (design §2): `hooks.py` calls this on the first capture event (`SessionStart` /
        `UserPromptSubmit`) to fill the pending `ChatRef`, exactly as for Claude."""
        return hook_payload["session_id"], hook_payload["transcript_path"]

    # ----- launch / ops --------------------------------------------------------------------

    def build_launch_command(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        initial_prompt: str | None = None,
    ) -> list[str]:
        """Assemble the argv for a fresh session: `codex -m <model> -c model_reasoning_effort=<effort>
        --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust [PROMPT]` (design §5,
        verification Evidence 1). Codex renders effort as a `-c` config override and defaults
        model/effort to gpt-5.5/high (design §4.4); a positional prompt auto-submits in the interactive
        TUI (measured), so the seed needs no send-keys."""
        command = [
            CODEX_BIN,
            "-m", model or CODEX_MODEL,
            "-c", f"{REASONING_EFFORT_KEY}={effort or CODEX_EFFORT}",
            *YOLO_FLAGS,
        ]
        if initial_prompt:
            command.append(initial_prompt)
        return command

    def resume_command(self, chat_id: str) -> list[str]:
        """`codex resume <id> --dangerously-bypass-* …` — resume a chat in place via Codex's native
        subcommand. The bypass flags ride along so our hooks still fire on the resumed session
        (verification.md note: resume accepts the same flag set)."""
        return [CODEX_BIN, "resume", chat_id, *YOLO_FLAGS]

    def fork_command(self, chat_id: str) -> list[str]:
        """`codex fork <id> --dangerously-bypass-* …` — branch a chat into a new session opening on its
        full history (Codex's native subcommand). The fork mints a NEW id (recorded as
        `forked_from_id` on its rollout's `session_meta`, Evidence 2), captured post-hoc like any
        other — no pre-mint."""
        return [CODEX_BIN, "fork", chat_id, *YOLO_FLAGS]

    def seed_command(self, prompt: str) -> list[str]:
        """A fresh `codex "<seed>"` carrying the seed as its positional prompt — handover / rollover's
        new worker (design §5). The prompt auto-submits, so no send-keys."""
        return self.build_launch_command(initial_prompt=prompt)

    def distiller_command(self) -> list[str]:
        """The throwaway distiller that summarizes a chat into a handover/rollover brief: a fresh codex
        at the default model/effort (`gpt-5.5` at high reasoning effort — design §5)."""
        return self.build_launch_command()

    # ----- transcript ----------------------------------------------------------------------

    def resolve_transcript(self, chat_id: str, cwd: str) -> Path:
        """Locate a chat's rollout by globbing `<codex-home>/sessions/**/rollout-*-<id>.jsonl` (design
        §5). A Codex rollout's path is **not** derived from the cwd (it is date+timestamp+id under the
        sessions tree), so `cwd` is unused here — the id alone resolves it. The hook-stored
        `ChatRef.transcript_path` (the canonical realpath the payload handed us) is preferred upstream
        when present; this is the by-id resolver for when it is absent or stale. Returns a non-existent
        sentinel when no rollout is on disk yet, so the caller's existence check fails cleanly (the
        protocol leaves existence to the caller)."""
        found = find_rollout(chat_id)
        if found is not None:
            return found
        return sessions_root() / f"{ROLLOUT_PREFIX}{chat_id}{TRANSCRIPT_SUFFIX}"

    def iter_messages(self, transcript: Path) -> Iterator[dict]:
        """Yield each turn of a Codex rollout as an engine-neutral message dict, normalizing the OpenAI
        Responses-item schema (unlike Claude, whose Anthropic JSONL is already the neutral form).
        Delegates parsing to T7's `codex_rollout.iter_messages` — authored and tested there (it skips
        the `session_meta`, reasoning/tool/token records and concatenates a turn's multi-block text) —
        and maps each `CodexMessage` to a dict carrying the `is_environment_context` flag so the
        messages layer can drop Codex's injected first-user `<environment_context>` turn."""
        for message in codex_rollout.iter_messages(transcript):
            yield {
                "role": message.role,
                "text": message.text,
                "is_environment_context": message.is_environment_context,
            }

    def bundle(self, chat: ChatRef) -> Path:
        """Mirror a chat into its durable history bundle and return the bundle dir. Codex's bundle is
        the rollout JSONL **alone — no sidecar** (design §6.4): Codex inlines tool calls / reasoning in
        the rollout, so there is no sibling dir to copy (contrast `ClaudeEngine.bundle`, which also
        copies `subagents/` + `tool-results/`). Delegates to history.py's shared, engine-neutral
        incremental copy — the per-engine LAYOUT (which paths get mirrored) is the only difference; the
        copy MECHANISM (append-by-offset, copy-if-absent, compaction-detection, the coalescing lock)
        stays shared there. `bundle_dir` is the engine-neutral location helper (it lives in `claude.py`
        but is not Claude-specific). Imported lazily because `history` imports the engines package."""
        from .. import history

        tx_id = Path(chat.bundle_path).parent.name
        history.ingest_chat(tx_id, chat.id, chat.cwd, Engine.CODEX, wait=True)
        return bundle_dir(tx_id, chat.id)

    # ----- hooks / state -------------------------------------------------------------------

    @property
    def event_to_state(self) -> Mapping[str, State]:
        return _EVENT_TO_STATE

    @property
    def state_source(self) -> StateSource:
        """Codex emits a `Stop` hook at turn end, so its turn-done state arrives as a hook event
        (design §2/§3) — the same capability as Claude (no statusline poll needed)."""
        return StateSource.HOOK_EVENTS


register(Engine.CODEX, CodexEngine())
