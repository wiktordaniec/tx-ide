#!/usr/bin/env python3.14
"""Spawn / reconcile / infer_role wiring for multi-engine (task T8 — Layer 1).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_spawn_wiring.py
Exits non-zero on the first failure; prints "OK — N checks passed" (same convention as the other
gate tests). This is the behavioral gate for T8: teaching tx to SPAWN and RECOGNIZE Codex agents
while leaving Claude exactly as before.

Covers T8's acceptance checklist:
  1. `infer_role` recognizes a `codex …` command as LLM (and still claude / nvim / shell / other);
  2. `Reconciler._is_agent_command` recognizes a `codex` pane AND still claude + the version-string
     load case — including the full `reconcile()` stuck-WORKING demotion driven by it;
  3. `SpawnSpec.engine` → `service._spawn` stamps `Session.engine` (+ the original `ChatRef`):
     `--engine codex` → CODEX, an llm spawn with no engine → CLAUDE (default), a non-llm → None;
  4. `tx spawn --engine codex` builds the right command via the adapter and stamps `record.engine`
     == CODEX; `--engine claude` / no-engine agent spawns default to Claude; `--cmd` stays a verbatim
     override; a bare `tx spawn` is still a shell (zero behavior change);
  5. the Codex adapter is REGISTERED on the production import path — this test never imports
     `tx.engines.codex` directly, so `Engine.CODEX in registry.registered()` proving the spawn/reconcile
     side-effect imports populate the registry (and `engines.registry.get(CODEX)` resolves under `tx spawn`);
  6. `tx resume` of a codex session keeps `engine=codex` on the resumed record AND its ChatRef (T8
     stamping on the resumed record — resume's command build was already engine-routed in T4; a
     claude session still resumes as claude).

Hermetic: a temp `$TX_IDE_HOME` (records + log) + a fake Tmux (no live server). No real spawn, no
network, no live home touched (T8 §6 safety).
"""

from __future__ import annotations

import contextlib
import io
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

# Point the home at a temp dir BEFORE importing tx (storage reads $TX_IDE_HOME).
os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()
# Point the engines' own homes at temp dirs too: `tx resume` calls `resolve_transcript` (Codex globs
# $CODEX_HOME/sessions, Claude reads $CLAUDE_CONFIG_DIR/projects). Hermetic — never touch the real homes.
os.environ["CODEX_HOME"] = tempfile.mkdtemp()
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

# NB: we import the CLI / spawn / reconcile surface but NEVER `tx.engines.codex` — check 5 below
# asserts the Codex adapter got registered purely by the production side-effect imports.
from tx.cli import ResumeCommand, SpawnCommand  # noqa: E402  (imports tx.spawn → registers the bundled adapters)
from tx.engines import registry  # noqa: E402
from tx.reconcile import Reconciler  # noqa: E402
from tx.service import SessionService  # noqa: E402
from tx.session import ChatRef, Engine, Kind, Origin, Role, Session, State  # noqa: E402
from tx.spawn import SpawnSpec, infer_role  # noqa: E402
from tx.storage import ensure_home  # noqa: E402
from tx.store import SessionStore  # noqa: E402

ensure_home()

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


# The exact command the Codex adapter builds (design §5 / verification Evidence 1) — the headline
# acceptance string. Hard-coded (not imported from the adapter) so a regression in the adapter is
# caught here too, and so this file never imports `tx.engines.codex` (check 5).
CODEX_PREFIX = ("codex -m gpt-5.5 -c model_reasoning_effort=high "
                "--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust")
CLAUDE_PREFIX = "claude --dangerously-skip-permissions"


class CliTmux:
    """Just enough Tmux for `SpawnCommand` → `SessionService._spawn` (no live server): name-free, no
    parent, a stub pid — and it CAPTURES the launch command tmux would run (the assertion target)."""

    def __init__(self):
        self.command = None

    def has_session(self, name):
        return False

    def current_session_name(self):
        return None

    def current_pane_path(self):
        return "/tmp"

    def new_session(self, name, cwd, command, env):
        self.command = command
        return 4242

    def set_tx_id(self, name, session_id):
        pass

    def attached_to(self, name):
        return []


class NoReconcile:
    """`_spawn`'s `_require_name_free` calls `reconcile()`; nothing to reconcile in a fake server."""

    def reconcile(self):
        return []


class ReconcileTmux:
    """A `Tmux` that reports a fixed set of live panes for the `Reconciler` (one `list_sessions`)."""

    def __init__(self, rows):
        self._rows = rows  # [(tx_id, name, pane_current_command), …]

    def list_sessions(self, fmt):
        return [f"{tx_id}\t{name}\t{command}" for tx_id, name, command in self._rows]


class FakeLog:
    def append(self, *args):
        pass


def spawn_service():
    return SessionService(store=SessionStore(), tmux=CliTmux(), reconciler=NoReconcile())


def run_spawn(name, argv):
    """Drive the real `tx spawn` command object through a hermetic service; return (saved Session,
    the captured launch command)."""
    tmux = CliTmux()
    service = SessionService(store=SessionStore(), tmux=tmux, reconciler=NoReconcile())
    with contextlib.redirect_stdout(io.StringIO()):  # swallow the "Spawned …" line — keep gate output clean
        rc = SpawnCommand(service).run([name, *argv])
    check(f"`tx spawn {name} …` returns 0", rc == 0)
    return service.store.find_by_name(name), tmux.command


# ----- 1. infer_role: any registered engine binary → LLM; codex joins claude --------------------

check("infer_role: a codex launch command → LLM", infer_role(CODEX_PREFIX + " hello") == Role.LLM)
check("infer_role: a full-path codex command → LLM (basename match)",
      infer_role("/opt/homebrew/bin/codex resume abc") == Role.LLM)
check("infer_role: bare `codex` → LLM", infer_role("codex") == Role.LLM)
check("infer_role: claude unchanged → LLM", infer_role(CLAUDE_PREFIX) == Role.LLM)
check("infer_role: nvim → NVIM", infer_role("nvim +DiffviewOpen") == Role.NVIM)
check("infer_role: a login shell → SHELL", infer_role("zsh") == Role.SHELL)
check("infer_role: anything else → OTHER", infer_role("npm run watch") == Role.OTHER)
check("infer_role: empty command → OTHER", infer_role("") == Role.OTHER)

# ----- 2. reconcile: _is_agent_command recognizes codex + claude + the version load case --------

predicate = Reconciler(store=None, tmux=None, log=None)._is_agent_command
check("_is_agent_command: bare `codex` pane is an agent", predicate("codex") is True)
check("_is_agent_command: bare `claude` pane is an agent (unchanged)", predicate("claude") is True)
check("_is_agent_command: a Claude load version string is an agent (preserved)", predicate("2.1.138") is True)
check("_is_agent_command: a Codex-shaped version string is an agent (preserved)", predicate("0.137.0") is True)
check("_is_agent_command: a plain shell is NOT an agent", predicate("zsh") is False)
check("_is_agent_command: an unrelated process is NOT an agent", predicate("node") is False)

# Full reconcile() path: a stuck-WORKING codex session whose pane is STILL `codex` is NOT demoted
# (the agent is up); the same session whose pane has fallen back to a shell IS demoted to IDLE (C5).
stale = time.time() - 100_000  # well past the 600s default stuck threshold


def working_codex(session_id):
    store = SessionStore()
    store.save(Session(
        id=session_id, name=session_id, kind=Kind.PROCESS, role=Role.LLM, state=State.WORKING,
        cwd="/p", cmd=CODEX_PREFIX, engine=Engine.CODEX, created_at=stale, last_activity=stale))
    return store


store = working_codex("cx-up")
Reconciler(store, ReconcileTmux([("cx-up", "cx-up", "codex")]), FakeLog()).reconcile()
check("reconcile: a WORKING codex with a live `codex` pane is NOT demoted (agent recognized)",
      store.load("cx-up").state == State.WORKING)

store = working_codex("cx-crashed")
Reconciler(store, ReconcileTmux([("cx-crashed", "cx-crashed", "zsh")]), FakeLog()).reconcile()
check("reconcile: a WORKING codex whose pane fell back to a shell IS demoted to IDLE (C5)",
      store.load("cx-crashed").state == State.IDLE)

# ----- 3. SpawnSpec.engine → _spawn stamps Session.engine (+ the original ChatRef) --------------

codex_session = spawn_service().spawn(SpawnSpec.for_process(
    name="s-codex", tags=["t"], cwd="/p", cmd=CODEX_PREFIX, engine=Engine.CODEX))
check("_spawn: --engine codex stamps record.engine == CODEX", codex_session.engine == Engine.CODEX)
check("_spawn: a codex spawn is role LLM", codex_session.role == Role.LLM)
check("_spawn: the pending original ChatRef inherits engine CODEX",
      len(codex_session.chats) == 1 and codex_session.chats[0].engine == Engine.CODEX)

default_session = spawn_service().spawn(SpawnSpec.for_process(
    name="s-default", tags=["t"], cwd="/p", cmd=CLAUDE_PREFIX))  # no engine → default
check("_spawn: an llm spawn with no engine defaults to CLAUDE", default_session.engine == Engine.CLAUDE)
check("_spawn: the default-engine original ChatRef is CLAUDE",
      default_session.chats[0].engine == Engine.CLAUDE)

explicit_claude = spawn_service().spawn(SpawnSpec.for_process(
    name="s-claude", tags=["t"], cwd="/p", cmd=CLAUDE_PREFIX, engine=Engine.CLAUDE))
check("_spawn: --engine claude stamps CLAUDE", explicit_claude.engine == Engine.CLAUDE)

shell_session = spawn_service().spawn(SpawnSpec.for_process(
    name="s-shell", tags=["t"], cwd="/p", cmd="zsh"))
check("_spawn: a shell session has no engine (None)", shell_session.engine is None)
check("_spawn: a shell session records no chat", shell_session.chats == [])

# A non-llm command never carries an engine even if one is passed (engine is meaningful only for llm).
other_session = spawn_service().spawn(SpawnSpec.for_process(
    name="s-other", tags=["t"], cwd="/p", cmd="npm run watch", engine=Engine.CODEX))
check("_spawn: a non-llm command nulls the engine even when one is passed", other_session.engine is None)

# ----- 4. `tx spawn --engine …` builds the command via the adapter + stamps the engine ----------

PRIMING_PREFIX = (
    "Read ~/.tx-ide/agents/COMMON.md and ~/.tx-ide/agents/DEVELOPER.md as your first actions. "
    "Then, if they exist, also read ~/.tx-ide/user-agents/COMMON.md, "
    "~/.tx-ide/user-agents/COMMON.local.md, ~/.tx-ide/user-agents/DEVELOPER.md, "
    "and ~/.tx-ide/user-agents/DEVELOPER.local.md (any user-agents/X.md replaces "
    "the shipped one; any user-agents/X.local.md extends it). "
    "Follow all of these for the duration of this session."
)

session, command = run_spawn("w-codex", ["--tag", "scope", "--cwd", "/p", "--engine", "codex", "--prompt", "ship-it"])
check("tx spawn --engine codex builds the exact adapter command (with default priming)",
      command == CODEX_PREFIX + " " + shlex.quote(PRIMING_PREFIX + " Then ship-it"))
check("tx spawn --engine codex stamps record.engine == CODEX", session.engine == Engine.CODEX)
check("tx spawn --engine codex is role LLM", session.role == Role.LLM)

session, command = run_spawn("w-codex-bare", ["--tag", "s", "--cwd", "/p", "--engine", "codex"])
check("tx spawn --engine codex with no --prompt builds the default codex command (with default priming)",
      command == CODEX_PREFIX + " " + shlex.quote(PRIMING_PREFIX + " Await instructions."))
check("tx spawn --engine codex (no prompt) still stamps CODEX", session.engine == Engine.CODEX)

session, command = run_spawn("w-codex-me",
                             ["--tag", "s", "--cwd", "/p", "--engine", "codex",
                              "--model", "gpt-5.5-codex", "--effort", "xhigh"])
check("tx spawn --engine codex --model/--effort render the Codex way (with default priming)",
      command == "codex -m gpt-5.5-codex -c model_reasoning_effort=xhigh "
                 "--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust "
                 + shlex.quote(PRIMING_PREFIX + " Await instructions."))

session, command = run_spawn("w-claude", ["--tag", "s", "--cwd", "/p", "--engine", "claude", "--prompt", "ship-it"])
check("tx spawn --engine claude builds the claude command (with default priming)",
      command == CLAUDE_PREFIX + " " + shlex.quote(PRIMING_PREFIX + " Then ship-it"))
check("tx spawn --engine claude stamps CLAUDE", session.engine == Engine.CLAUDE)

# "default still Claude": an AGENT spawn (a --prompt, no --engine) builds claude and stamps CLAUDE.
session, command = run_spawn("w-default-agent", ["--tag", "s", "--cwd", "/p", "--prompt", "ship-it"])
check("tx spawn --prompt with no --engine defaults to the claude adapter (with default priming)",
      command == CLAUDE_PREFIX + " " + shlex.quote(PRIMING_PREFIX + " Then ship-it"))
check("tx spawn --prompt with no --engine stamps CLAUDE", session.engine == Engine.CLAUDE)

# A spaced prompt is shell-quoted by the command builder (it auto-submits as one positional).
session, command = run_spawn("w-codex-spaced", ["--tag", "s", "--cwd", "/p", "--engine", "codex", "--prompt", "do the thing"])
check("tx spawn --engine codex shell-quotes a multi-word prompt",
      command == CODEX_PREFIX + " " + shlex.quote(PRIMING_PREFIX + " Then do the thing"))

# ----- 4b. `tx spawn --engine … --no-prime` bypasses default priming ----------
session, command = run_spawn("w-codex-noprime", ["--tag", "scope", "--cwd", "/p", "--engine", "codex", "--no-prime", "--prompt", "ship-it"])
check("tx spawn --engine codex --no-prime builds the raw command",
      command == CODEX_PREFIX + " ship-it")

session, command = run_spawn("w-codex-noprime-bare", ["--tag", "s", "--cwd", "/p", "--engine", "codex", "--no-prime"])
check("tx spawn --engine codex --no-prime with no prompt builds raw bare command",
      command == CODEX_PREFIX)

session, command = run_spawn("w-claude-noprime", ["--tag", "s", "--cwd", "/p", "--engine", "claude", "--no-prime", "--prompt", "ship-it"])
check("tx spawn --engine claude --no-prime builds the raw command",
      command == CLAUDE_PREFIX + " ship-it")

# --cmd stays a verbatim override; with no --engine an llm command still defaults to CLAUDE (unchanged).
session, command = run_spawn("w-cmd-claude", ["--tag", "s", "--cwd", "/p", "--cmd", CLAUDE_PREFIX])
check("tx spawn --cmd 'claude …' is verbatim (zero behavior change)", command == CLAUDE_PREFIX)
check("tx spawn --cmd 'claude …' with no --engine stamps CLAUDE (default)", session.engine == Engine.CLAUDE)

# --cmd is the escape hatch: the user passes --engine to declare a non-default engine for the record.
session, command = run_spawn("w-cmd-codex", ["--tag", "s", "--cwd", "/p", "--engine", "codex",
                                             "--cmd", "codex resume ABC --dangerously-bypass-approvals-and-sandbox"])
check("tx spawn --cmd 'codex …' --engine codex keeps the command verbatim",
      command == "codex resume ABC --dangerously-bypass-approvals-and-sandbox")
check("tx spawn --cmd … --engine codex stamps the declared engine CODEX", session.engine == Engine.CODEX)

# A bare `tx spawn` (no --cmd, no --engine) is still a login shell, engine-less — zero behavior change.
session, command = run_spawn("w-bare", ["--tag", "s", "--cwd", "/p"])
check("tx spawn (bare) is a login shell (unchanged)",
      command == (os.environ.get("SHELL") or "zsh"))
check("tx spawn (bare) has no engine", session.engine is None)
check("tx spawn (bare) is role SHELL", session.role == Role.SHELL)

# --prompt/--model/--effort contradict a full --cmd override → a clean boundary error (exit, no spawn).
try:
    with contextlib.redirect_stderr(io.StringIO()):  # swallow argparse's usage/error banner
        SpawnCommand(spawn_service()).run(["w-bad", "--tag", "s", "--cwd", "/p", "--cmd", "zsh", "--prompt", "x"])
    check("tx spawn --cmd + --prompt is rejected", False)
except SystemExit as exit_error:
    check("tx spawn --cmd + --prompt is rejected (argparse error exit)", exit_error.code != 0)

# ----- 5. the Codex adapter is registered on the PRODUCTION import path (no direct import here) --

check("registry: Claude is registered", Engine.CLAUDE in registry.registered())
check("registry: Codex is registered via the spawn/reconcile side-effect imports (not imported here)",
      Engine.CODEX in registry.registered())

# ----- 6. resume-of-codex keeps the record's engine (T8 stamping on the resumed record) ---------
# `tx resume` builds its command via `record.engine` (T4) AND now stamps that engine on the new
# record (T8). A resumed codex session must stay codex — otherwise `_attach_resumed_chat` resolves a
# Claude transcript path and the record mis-reports its engine (a real T9 resume-of-codex bug).


def resume_record(session_id, name, engine, chat_id, cwd):
    """Store a past (EXITED) llm session with one resumable chat, return a hermetic service over it."""
    store = SessionStore()
    store.save(Session(
        id=session_id, name=name, kind=Kind.PROCESS, role=Role.LLM, state=State.EXITED,
        cwd=cwd, cmd="(past)", engine=engine, created_at=stale,
        chats=[ChatRef(id=chat_id, role="original", cwd=cwd, transcript_path="",
                       origin=Origin(how="spawn", session_id=session_id, chat_id=None),
                       started_at=stale, engine=engine)]))
    return SessionService(store=store, tmux=CliTmux(), reconciler=NoReconcile())


resume_cwd = tempfile.mkdtemp()  # a real dir — `tx resume` requires Path(cwd).is_dir()

codex_svc = resume_record("past-codex", "past-codex", Engine.CODEX, "CX-CHAT", resume_cwd)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    rc = ResumeCommand(codex_svc).run(["past-codex", "--as", "codex-resumed", "--cwd", resume_cwd])
check("resume-of-codex returns 0", rc == 0)
resumed = codex_svc.store.find_by_name("codex-resumed")
check("resume-of-codex stamps the resumed record engine == CODEX", resumed.engine == Engine.CODEX)
check("resume-of-codex builds a `codex resume …` command (T4 routing intact)",
      resumed.cmd.startswith("codex resume CX-CHAT"))
check("resume-of-codex stamps the resumed ChatRef engine == CODEX",
      bool(resumed.chats) and resumed.chats[-1].engine == Engine.CODEX)

claude_svc = resume_record("past-claude", "past-claude", Engine.CLAUDE, "CL-CHAT", resume_cwd)
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    ResumeCommand(claude_svc).run(["past-claude", "--as", "claude-resumed", "--cwd", resume_cwd])
check("resume-of-claude keeps engine == CLAUDE (unchanged)",
      claude_svc.store.find_by_name("claude-resumed").engine == Engine.CLAUDE)

print(f"OK — {PASSED} checks passed")
