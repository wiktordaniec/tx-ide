#!/usr/bin/env python3.14
"""Claude no-regression differential for the T8b chat-ops engine routing (MANDATORY — the V-T8b hinge).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_chatops_differential.py
Exits non-zero on the first failure; prints "OK — N checks passed" (the repo convention).

T8b moved the Claude persona parse + fork/handover/rollover/distiller command derivations out of
`chat.py` (claude-shaped string transforms) and into `ClaudeEngine`, and routed `chat.py` to dispatch
on `record.engine`. This task changes **live-crew rollover/handover persona derivation**, so the
acceptance hinge is a byte-identical differential: the NEW `ClaudeEngine.<op>_command(...)` (joined as
`chat.py` joins it) must render **byte-for-byte identical** to the OLD `chat.py` string-transform
output, across model / effort / system-prompt / `--settings` / unknown value-flags / bare flags /
compound-shell / baked-positional / identity-stripping cases. Any divergence = a regression = FAIL.

Three layers:
  - **frozen golden old-vs-new** — the OLD `chat.py` derivations are frozen verbatim below (the spec
    of pre-T8b behavior) and asserted byte-identical to the new adapter across the corpus;
  - **#50 / eae09f3 regression (the gap codex-plan's grep found)** — `--append-system-prompt-file
    <path>` (and any unknown value-flag) survives a fork AND a handover AND a rollover;
  - **live dispatch wiring** — the actual `ChatOps` fork / handover-finish / rollover-finish /
    distiller-spawn sites produce the expected command (right engine method, `shlex.join`, the
    rollover `_env_prefix` string-prepend preserved), via a minimal fake service — no real spawn.

A best-effort git cross-check proves the frozen golden is a faithful copy of the ACTUAL pre-T8b
`chat.py` (loaded from the T8b base ref); it is skipped (printed, not failed) when that ref is
unavailable, so the frozen golden stands alone as a permanent, self-contained gate.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

# Safety (T8b §5): never let a path helper resolve the live ~/.tx-ide. The differential is pure, but
# the dispatch layer touches `history_dir()` for bundle paths — pin it to a throwaway home.
os.environ.setdefault("TX_IDE_HOME", tempfile.mkdtemp())

from tx.chat import ChatOps, ChatOpSpec, _env_prefix  # noqa: E402
from tx.engines import claude as _claude_engine_module  # noqa: E402,F401 — registers ClaudeEngine
from tx.engines import registry  # noqa: E402
from tx.session import ChatRef, Engine, Kind, Origin, Role, Session, State  # noqa: E402
from tx.spawn import SpawnSpec  # noqa: E402,F401
import tx.chat as chat_module  # noqa: E402

claude = registry.get(Engine.CLAUDE)

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


# =================================================================================================
# FROZEN GOLDEN — the pre-T8b `chat.py` derivations, copied VERBATIM (the `eae09f3` / #50 behavior).
# These are the SPEC of the old behavior; the new ClaudeEngine must reproduce them byte-for-byte. Do
# not "improve" them — they are a fixture, not live code. (The git cross-check at the end proves this
# copy is faithful to the actual pre-T8b chat.py.)
# =================================================================================================

_OLD_IDENTITY_VALUE_FLAGS = frozenset({"--session-id", "--resume"})
_OLD_IDENTITY_BARE_FLAGS = frozenset({"--fork-session", "--continue", "-c"})
_OLD_BARE_FLAGS = frozenset({
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--verbose",
    "--print", "-p", "--ide", "--tmux", "--strict-mcp-config", "--no-session-persistence",
    "--exclude-dynamic-system-prompt-sections", "--replay-user-messages",
    "--include-partial-messages", "--include-hook-events", "--disable-slash-commands",
    "--chrome", "--no-chrome",
})
_OLD_SHELL_CONTROL_TOKENS = frozenset({";", "&", "&&", "||", "|", "|&", "&>", "&>>", "(", ")", "{", "}"})
_OLD_DISTILLER_COMMAND = "claude --model opus --effort medium --dangerously-skip-permissions"


def _old_is_shell_control(token):
    return token in _OLD_SHELL_CONTROL_TOKENS or token[:1] in ("<", ">")


def _old_strip_identity(source_cmd):
    tokens = shlex.split(source_cmd)
    binary = tokens[0] if tokens else "claude"
    inherited = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if _old_is_shell_control(token):
            break
        if token in _OLD_IDENTITY_VALUE_FLAGS:
            index += 2
            continue
        if token in _OLD_IDENTITY_BARE_FLAGS:
            index += 1
            continue
        if token in _OLD_BARE_FLAGS:
            inherited.append(token)
            index += 1
            continue
        if token.startswith("-"):
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("-") \
                    and not _old_is_shell_control(tokens[index + 1]):
                inherited.extend(tokens[index:index + 2])
                index += 2
            else:
                inherited.append(token)
                index += 1
            continue
        index += 1
    return binary, inherited


def _old_ensure_skip_permissions(command):
    if "--dangerously-skip-permissions" not in command:
        command.append("--dangerously-skip-permissions")
    return command


def _old_fork_command(source_cmd, source_chat):
    binary, inherited = _old_strip_identity(source_cmd)
    command = _old_ensure_skip_permissions(
        [binary, "--resume", source_chat, "--fork-session", *inherited]
    )
    return shlex.join(command)


def _old_fresh_command(source_cmd):
    binary, inherited = _old_strip_identity(source_cmd)
    command = _old_ensure_skip_permissions([binary, *inherited])
    return shlex.join(command)


def _old_seeded_command(base_command, seed):
    return f"{base_command} {shlex.quote(seed)}"


# The old composed expressions the four chat.py call-sites used (chat.py:297/476/516/627):
def _old_handover_launch(source_cmd, seed):
    return _old_seeded_command(_old_fresh_command(source_cmd), seed)


def _old_rollover_command(env, source_cmd, seed):
    return _env_prefix(env) + _old_seeded_command(_old_fresh_command(source_cmd), seed)


def _old_distiller_launch(seed):
    return _old_seeded_command(_OLD_DISTILLER_COMMAND, seed)


# =================================================================================================
# CORPUS — representative source commands (the spec's named set + the edges that exercise the parse).
# =================================================================================================

CHAT_ID = "11111111-2222-3333-4444-555555555555"

CORPUS = [
    # bare / yolo-only
    "claude",
    "claude --dangerously-skip-permissions",
    # model / effort (the common persona)
    "claude --model opus",
    "claude --model opus --effort high",
    "claude --model 'opus[1m]' --effort max --dangerously-skip-permissions",
    # system prompt + --settings (value flags)
    "claude --model opus --append-system-prompt 'be terse and exact'",
    "claude --settings /tmp/settings.json --model sonnet --effort medium",
    # the #50 / eae09f3 flag + a second unknown value-flag (the regression class)
    "claude --append-system-prompt-file /home/me/sys-prompt.md",
    "claude --model opus --append-system-prompt-file /tmp/sys.md --some-future-flag a-value --effort max",
    # a dangling unknown value-flag (end of argv) — the else-branch
    "claude --model opus --append-system-prompt-file",
    # identity flags present (a forked / resumed / continued source cmd) — must be stripped
    "claude --resume OLDID --fork-session --model opus --effort high --dangerously-skip-permissions",
    "claude --session-id ABC-123 --model opus",
    "claude --continue --model opus",
    "claude -c --model opus",  # -c == --continue short form (a BARE identity for Claude)
    # baked positional prompt — must be dropped (the op seeds its own)
    "claude --model opus 'do the original thing'",
    "claude --dangerously-skip-permissions 'an old baked prompt'",
    # multiple bare flags interleaved
    "claude --verbose --model opus --ide --strict-mcp-config",
    "claude --model opus --include-partial-messages --include-hook-events",
    # compound shell wrapping — dropped from the first control token (bug #2b)
    "claude --model opus && echo done",
    "claude --model opus ; rm -rf /tmp/x",
    "claude --model opus | tee /tmp/log",
    # full-path binary (matches infer_role on the basename)
    "/usr/local/bin/claude --model opus --effort high",
]

SEEDS = [
    "read the brief and begin",
    "read the brief & then go",            # needs shell quoting
    "Your task: «do X»\nand then Y",        # newline + unicode
    "it's a single-quote test",            # embedded single quote
    'a "double-quote" test',               # embedded double quote
    "",                                     # empty seed (edge — both render `''`)
]

ENVS = [
    {},
    {"TX_SESSION_ID": "src-123"},
    {"TX_SESSION_ID": "id with spaces", "FOO": "bar=baz"},
]


# =================================================================================================
# LAYER 1 — byte-identical differential (fork / handover / rollover / distiller)
# =================================================================================================

for source_cmd in CORPUS:
    new_fork = shlex.join(claude.fork_command(source_cmd, CHAT_ID))
    check(f"differential/fork byte-identical for: {source_cmd!r}",
          new_fork == _old_fork_command(source_cmd, CHAT_ID))

    for seed in SEEDS:
        # handover finish (chat.py:476): seeded_command(fresh_command(cmd), seed)
        new_handover = shlex.join(claude.seed_command(source_cmd, seed))
        check(f"differential/handover byte-identical for cmd={source_cmd!r} seed={seed!r}",
              new_handover == _old_handover_launch(source_cmd, seed))

        # rollover finish (chat.py:516): _env_prefix(env) + seeded_command(fresh_command(cmd), seed)
        for env in ENVS:
            new_rollover = _env_prefix(env) + shlex.join(claude.seed_command(source_cmd, seed))
            check(f"differential/rollover byte-identical for cmd={source_cmd!r} env={env} seed={seed!r}",
                  new_rollover == _old_rollover_command(env, source_cmd, seed))

# distiller (chat.py:627): seeded_command(DISTILLER_COMMAND, seed) — a FIXED command (no source persona)
for seed in SEEDS:
    new_distiller = shlex.join(claude.distiller_command(seed))
    check(f"differential/distiller byte-identical for seed={seed!r}",
          new_distiller == _old_distiller_launch(seed))
# R2: the claude-source distiller stays opus, unchanged.
check("distiller is opus at medium effort (R2 — unchanged)",
      shlex.join(claude.distiller_command("x")).startswith(
          "claude --model opus --effort medium --dangerously-skip-permissions "))


# =================================================================================================
# LAYER 2 — #50 / eae09f3 regression (the gap codex-plan's grep found: NO test guarded eae09f3).
# An unknown value-flag + its value must ride through a fork AND a handover AND a rollover — never be
# mistaken for the positional prompt and dropped (which would leave a dangling flag to swallow the seed).
# =================================================================================================

UNKNOWN_VALUE_FLAGS = [
    ("--append-system-prompt-file", "/home/me/sys-prompt.md"),  # the actual eae09f3 flag
    ("--some-future-value-flag", "an-unrecognised-value"),       # any future unknown value-flag
]
SEED_50 = "read the brief and continue"

for flag, value in UNKNOWN_VALUE_FLAGS:
    source_cmd = f"claude --model opus {flag} {value} --dangerously-skip-permissions 'a baked prompt'"

    fork = claude.fork_command(source_cmd, CHAT_ID)
    check(f"#50 fork: {flag} survives WITH its value",
          flag in fork and fork[fork.index(flag) + 1] == value)
    check(f"#50 fork ({flag}): the baked positional prompt is NOT carried forward",
          "a baked prompt" not in fork)

    # handover AND rollover both derive via seed_command — assert through both call shapes.
    handover = claude.seed_command(source_cmd, SEED_50)
    check(f"#50 handover: {flag} survives WITH its value",
          flag in handover and handover[handover.index(flag) + 1] == value)
    check(f"#50 handover ({flag}): the seed is the positional tail (not the inherited flag's value)",
          handover[-1] == SEED_50)

    rollover_cmd = _env_prefix({"TX_SESSION_ID": "src"}) + shlex.join(claude.seed_command(source_cmd, SEED_50))
    check(f"#50 rollover: {flag} {value} survives after the env-prefix",
          f"{flag} {value}" in rollover_cmd and rollover_cmd.startswith("env TX_SESSION_ID=src "))


# =================================================================================================
# LAYER 3 — live dispatch wiring (the four ChatOps sites), via a minimal fake service. This proves
# the engine routing is wired correctly end-to-end (right method, shlex.join, env-prefix preserved)
# without a real spawn — the live-crew path the differential above proves is byte-identical.
# =================================================================================================

class _FakeStore:
    def __init__(self):
        self.by_id = {}

    def load(self, txid):
        return self.by_id[txid]

    def save(self, session):
        self.by_id[session.id] = session

    def all(self):
        return list(self.by_id.values())


class _FakeLog:
    def append(self, *args):
        pass


class _FakeTmux:
    def __init__(self):
        self.respawned = []

    def current_session_name(self):
        return None

    def display_message(self, fmt, target=None):
        return "%1"

    def respawn_pane(self, pane, command):
        self.respawned.append((pane, command))


class _FakeService:
    """Just enough of `SessionService` for the four dispatch sites — captures every spawned SpawnSpec
    + every respawn command. No tmux, no disk, no real spawn (T8b §5)."""

    def __init__(self):
        self.store = _FakeStore()
        self.log = _FakeLog()
        self.tmux = _FakeTmux()
        self.spawned = []
        self._by_name = {}

    def register(self, session):
        self.store.save(session)
        self._by_name[session.name] = session

    def get(self, name):
        return self._by_name.get(name)

    def reconcile(self):
        pass

    def kill(self, name):
        pass

    def spawn(self, spec):
        self.spawned.append(spec)
        engine = Engine.CLAUDE if spec.role == Role.LLM else None
        new = Session(
            id="ID-" + spec.name, name=spec.name, kind=spec.kind, role=spec.role,
            state=State.initial_for(spec.role), cwd=spec.cwd, cmd=spec.cmd, engine=engine,
            tags=list(spec.tags), env=dict(spec.env),
        )
        self.register(new)
        return new

    def next_worker_name(self, starting_directory, base_name):
        return base_name

    def spawn_worker(self, spec, **kwargs):
        before_spawn = kwargs.get("before_spawn")
        if before_spawn is not None:
            before_spawn(spec)
        return self.spawn(spec)

    def spawn_internal(self, spec):
        return self.spawn(spec)


def _source_session(txid, name, cmd):
    return Session(
        id=txid, name=name, kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE,
        cwd="/work", cmd=cmd, engine=Engine.CLAUDE,
        chats=[ChatRef(id=CHAT_ID, role="original", cwd="/work", transcript_path="",
                       origin=Origin(how="spawn", session_id=txid, chat_id=None),
                       engine=Engine.CLAUDE)],
    )


PERSONA_CMD = "claude --model opus --effort high --append-system-prompt-file /tmp/x.md"

# --- fork (chat.py:297) ---
service = _FakeService()
service.register(_source_session("SRC", "src", PERSONA_CMD))
ChatOps(service).fork("src", "myfork")
fork_spec = service.spawned[-1]
check("dispatch/fork: cmd is byte-identical to the old fork derivation",
      fork_spec.cmd == _old_fork_command(PERSONA_CMD, CHAT_ID))
check("dispatch/fork: #50 flag survived the LIVE fork path",
      "--append-system-prompt-file /tmp/x.md" in fork_spec.cmd)

# --- handover finish (chat.py:476) ---
# The artifact files must EXIST: the finish seeds the note/brief pointer only for an artifact the
# distiller actually wrote (a missing one falls back to the self-catch-up seed — AND-171), and in
# the live flow the finish always runs after the distiller's write.
_ARTIFACTS = Path(tempfile.mkdtemp())
_BRIEF = _ARTIFACTS / "brief.md"
_BRIEF.write_text("# brief")
_NOTE = _ARTIFACTS / "note.md"
_NOTE.write_text("# note")

service = _FakeService()
service.register(_source_session("SRC2", "src2", PERSONA_CMD))
handover_spec = ChatOpSpec(op_id="op-h", kind="handover", source_txid="SRC2", source_chat=CHAT_ID,
                           cwd="/work", artifact_path=str(_BRIEF), worker_name="hw")
ChatOps(service)._finish_handover(handover_spec)
handover_launch = service.spawned[-1].cmd
check("dispatch/handover: inherits the full source persona before the seed",
      handover_launch.startswith(
          "claude --model opus --effort high --append-system-prompt-file /tmp/x.md "
          "--dangerously-skip-permissions "))
check("dispatch/handover: the launch is a valid shlex string ending on the brief seed",
      shlex.split(handover_launch)[0] == "claude" and str(_BRIEF) in shlex.split(handover_launch)[-1])

# --- rollover finish (chat.py:516) — env-prefix MUST be preserved ---
_orig_ingest = chat_module.history.ingest_session
chat_module.history.ingest_session = lambda *a, **k: None
try:
    service = _FakeService()
    service.register(_source_session("SRC3", "src3", PERSONA_CMD))
    rollover_spec = ChatOpSpec(op_id="op-r", kind="rollover", source_txid="SRC3", source_chat=CHAT_ID,
                               cwd="/work", artifact_path=str(_NOTE), pane="%9")
    ChatOps(service)._finish_rollover(rollover_spec)
    pane, rollover_command = service.tmux.respawned[-1]
    check("dispatch/rollover: respawns the resolved pane", pane == "%9")
    check("dispatch/rollover: the _env_prefix string-prepend is preserved (TX_SESSION_ID baked in)",
          rollover_command.startswith("env TX_SESSION_ID=SRC3 "))
    check("dispatch/rollover: source persona + #50 flag survive after the env prefix",
          "--model opus --effort high --append-system-prompt-file /tmp/x.md" in rollover_command)

    read_only_source = _source_session(
        "SRC4", "src4",
        shlex.join(claude.build_launch_command(model="opus", read_only=True)),
    )
    read_only_source.env["TX_READ_ONLY"] = "1"
    service = _FakeService()
    service.register(read_only_source)
    read_only_spec = ChatOpSpec(
        op_id="op-ro", kind="rollover", source_txid="SRC4", source_chat=CHAT_ID,
        cwd="/work", artifact_path=str(_NOTE), pane="%10",
    )
    ChatOps(service)._finish_rollover(read_only_spec)
    _, read_only_command = service.tmux.respawned[-1]
    check("dispatch/rollover: read-only marker survives the pane respawn",
          "TX_READ_ONLY=1" in read_only_command)
    check("dispatch/rollover: read-only Claude controls survive the pane respawn",
          "--permission-mode plan" in read_only_command
          and "--dangerously-skip-permissions" not in read_only_command)
finally:
    chat_module.history.ingest_session = _orig_ingest

# --- distiller spawn (chat.py:627) — fixed per-engine command, dispatched on the source engine ---
service = _FakeService()
ChatOps(service)._spawn_distiller("distill", "handover", "/work", "summarise this chat", Engine.CLAUDE)
distiller_spec = service.spawned[-1]
check("dispatch/distiller (claude): byte-identical to the old DISTILLER_COMMAND derivation",
      distiller_spec.cmd == _old_distiller_launch("summarise this chat"))
check("dispatch/distiller (claude): opus, unchanged (R2)",
      "claude --model opus --effort medium" in distiller_spec.cmd)


# =================================================================================================
# LAYER 4 — git cross-check (best-effort): prove the frozen golden == the ACTUAL pre-T8b chat.py.
# =================================================================================================

def _load_pre_t8b_derivations():
    """The pre-T8b chat.py derivations, loaded from the T8b base ref, or None when unavailable.

    The base chat.py imports tx modules we cannot satisfy standalone, but the derivation functions we
    call need only `shlex` (+ `claude.CLAUDE_BIN`, injected). Relative imports are neutralised to
    `pass`; `from __future__ import annotations` (preserved) keeps every annotation a string, so the
    other defs never evaluate the removed names at import. Returns None on a shallow clone / once the
    ref has advanced past the retirement (then the frozen golden stands alone)."""
    repo = str(Path(__file__).resolve().parents[1])
    for ref in ("feat/engine-abstraction", "origin/feat/engine-abstraction"):
        result = subprocess.run(["git", "show", f"{ref}:lib/tx/chat.py"], cwd=repo,
                                capture_output=True, text=True)
        if result.returncode != 0:
            continue
        text = re.sub(r"^from \.[^\n]*$", "pass", result.stdout, flags=re.M)
        namespace = {"shlex": shlex, "claude": types.SimpleNamespace(CLAUDE_BIN="claude")}
        exec(compile(text, "<pre-t8b-chat>", "exec"), namespace)
        if "fork_command" in namespace and "seeded_command" in namespace:
            return namespace
    return None


_old = _load_pre_t8b_derivations()
if _old is None:
    print("  (skip git cross-check: pre-T8b chat.py derivations unavailable — frozen golden stands alone)")
else:
    for source_cmd in CORPUS:
        check("git-integrity/fork: frozen golden == actual pre-T8b chat.py",
              _old_fork_command(source_cmd, CHAT_ID) == _old["fork_command"](source_cmd, CHAT_ID))
        for seed in SEEDS:
            check("git-integrity/handover: frozen golden == actual pre-T8b chat.py",
                  _old_handover_launch(source_cmd, seed)
                  == _old["seeded_command"](_old["fresh_command"](source_cmd), seed))
    for seed in SEEDS:
        check("git-integrity/distiller: frozen golden == actual pre-T8b chat.py",
              _old_distiller_launch(seed) == _old["seeded_command"](_old["DISTILLER_COMMAND"], seed))
    print("  (git cross-check OK: the frozen golden matches the actual pre-T8b chat.py derivations)")


print(f"OK — {PASSED} checks passed")
