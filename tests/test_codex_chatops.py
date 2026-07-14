#!/usr/bin/env python3.14
"""Codex chat-op command derivation + R1 unknown-flag survival (T8b).

No pytest in this repo, so this is directly runnable:  python3.14 tests/test_codex_chatops.py
Exits non-zero on the first failure; prints "OK — N checks passed" (the repo convention).

T8b gave `CodexEngine` its OWN persona parse (codex flag grammar) so chat-ops fork/handover/rollover/
distiller route through it. Codex's grammar differs from Claude's: the identity is a positional
SUBCOMMAND (`codex resume <id>` / `codex fork <id>`), not a `--flag`, and `-c KEY=VALUE` is a VALUE
flag (contrast Claude's `-c` == `--continue`, a BARE flag) — the collision the spec calls out. This
suite covers:

  - **op argv per design §5** — fork = `codex fork <id>` + inherited persona + bypass flags; handover
    / rollover = a fresh `codex` + persona + the seed (NO identity subcommand); distiller = the FIXED
    `codex -m gpt-5.5 -c model_reasoning_effort=high` + bypass + seed (no source persona);
  - **persona parse** — `-m VALUE` / `-c KEY=VALUE` / the bypass flags honored; `-c` read as a VALUE
    flag (not bare); a leading `resume`/`fork` subcommand + its id stripped;
  - **R1 (MANDATORY) — Codex unknown-flag survival (#50 parity)** — an unrecognised codex value-flag
    (an unknown `-c some_key=val`, and a future `--flag value`) rides through a fork AND a handover
    AND a rollover, never mistaken for the positional prompt and dropped. Without this, Codex would
    reintroduce the exact #50 bug class;
  - **dispatch routing** — `ChatOps` with a Codex-engine source routes to `CodexEngine` (via a minimal
    fake service — no real spawn).
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

# Safety (T8b §5): a throwaway home so the dispatch layer's bundle-path helpers never resolve live state.
os.environ.setdefault("TX_IDE_HOME", tempfile.mkdtemp())

from tx.chat import ChatOps, _env_prefix  # noqa: E402
from tx.engines import codex as _codex_engine_module  # noqa: E402,F401 — registers CodexEngine
from tx.engines import registry  # noqa: E402
from tx.session import ChatRef, Engine, Kind, Origin, Role, Session, State  # noqa: E402

codex = registry.get(Engine.CODEX)

BYPASS_A = "--dangerously-bypass-approvals-and-sandbox"
BYPASS_H = "--dangerously-bypass-hook-trust"
BYPASS = [BYPASS_A, BYPASS_H]
CHAT_ID = "019ea7f9-5334-7221-a09e-f7891025114c"

# A canonical fresh-codex source command (what a real Codex session's `cmd` looks like, design §5).
SOURCE = f"codex -m gpt-5.5 -c model_reasoning_effort=high {BYPASS_A} {BYPASS_H}"

PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


# ----- op argv per design §5 ----------------------------------------------------------------

fork = codex.fork_command(SOURCE, CHAT_ID)
check("fork: native `codex fork <id>` subcommand",
      fork[:3] == ["codex", "fork", CHAT_ID])
check("fork: inherits the source persona (-m gpt-5.5 -c model_reasoning_effort=high)",
      fork[3:7] == ["-m", "gpt-5.5", "-c", "model_reasoning_effort=high"])
check("fork: keeps both bypass flags (no duplication)",
      fork.count(BYPASS_A) == 1 and fork.count(BYPASS_H) == 1)

seed = codex.seed_command(SOURCE, "read the brief and begin")
check("seed: a fresh codex (NO fork/resume identity subcommand)",
      seed[0] == "codex" and "fork" not in seed and "resume" not in seed)
check("seed: inherits the source persona", seed[1:5] == ["-m", "gpt-5.5", "-c", "model_reasoning_effort=high"])
check("seed: both bypass flags present", BYPASS_A in seed and BYPASS_H in seed)
check("seed: the seed is the positional tail", seed[-1] == "read the brief and begin")

distiller = codex.distiller_command("summarise this chat")
check("distiller: the FIXED gpt-5.5/high command (no source persona)",
      distiller[:5] == ["codex", "-m", "gpt-5.5", "-c", "model_reasoning_effort=high"])
check("distiller: both bypass flags present", BYPASS_A in distiller and BYPASS_H in distiller)
check("distiller: the seed is the positional tail", distiller[-1] == "summarise this chat")


# ----- persona parse: -m / -c KEY=VALUE honored; -c read as VALUE (not bare); subcommand stripped --

# A non-default persona is carried verbatim (model + effort override).
override_src = f"codex -m gpt-5.5-codex -c model_reasoning_effort=xhigh {BYPASS_A} {BYPASS_H}"
override_seed = codex.seed_command(override_src, "go")
check("parse: -m override carried (gpt-5.5-codex)",
      override_seed[1:3] == ["-m", "gpt-5.5-codex"])
check("parse: -c model_reasoning_effort=xhigh carried as a VALUE flag (KEY=VALUE intact)",
      "-c" in override_seed and override_seed[override_seed.index("-c") + 1] == "model_reasoning_effort=xhigh")
check("parse: -c is NOT misread as a bare flag (its value is not dropped/treated as the prompt)",
      override_seed[-1] == "go" and "model_reasoning_effort=xhigh" in override_seed)

# A baked positional prompt on the source is dropped (the op seeds its own).
baked_src = f'codex -m gpt-5.5 {BYPASS_A} {BYPASS_H} "the original baked prompt"'
check("parse: the source's baked positional prompt is dropped",
      "the original baked prompt" not in codex.seed_command(baked_src, "fresh seed"))

# A forked/resumed source `cmd` — the identity subcommand + its id are stripped, persona kept.
forked_src = f"codex fork SOME-OLD-ID -m gpt-5.5 -c model_reasoning_effort=high {BYPASS_A} {BYPASS_H}"
forked_reseed = codex.seed_command(forked_src, "continue")
check("parse: a `fork <id>` identity subcommand is stripped (no stale id, no nested fork)",
      "SOME-OLD-ID" not in forked_reseed and "fork" not in forked_reseed)
check("parse: persona survives the subcommand strip", forked_reseed[1:5] == ["-m", "gpt-5.5", "-c", "model_reasoning_effort=high"])
resumed_src = f"codex resume SOME-OLD-ID -m gpt-5.5 {BYPASS_A} {BYPASS_H}"
check("parse: a `resume <id>` identity subcommand is stripped too",
      "SOME-OLD-ID" not in codex.fork_command(resumed_src, CHAT_ID)
      and codex.fork_command(resumed_src, CHAT_ID)[:3] == ["codex", "fork", CHAT_ID])

# A bare `codex` source still yields a runnable op (ensure_yolo supplies the bypass flags).
bare_seed = codex.seed_command("codex", "go")
check("parse: a bare `codex` source still gets both bypass flags ensured",
      BYPASS_A in bare_seed and BYPASS_H in bare_seed and bare_seed[-1] == "go")


# ----- R1 (MANDATORY): Codex unknown-flag survival — the #50 analog for Codex --------------------
# An unrecognised codex value-flag (and its value) must ride through a fork AND a handover AND a
# rollover. The "unknown value-flag survives" safe default (every non-bare -flag is value-by-default)
# is what prevents Codex reintroducing the exact #50 bug class.

UNKNOWN_VALUE_FLAGS = [
    ("-c", "some_unrecognised_key=a-value"),     # an unknown `-c KEY=VALUE` config override
    ("--some-future-codex-flag", "a-value"),      # a future unknown value-flag
]
SEED = "continue the task"

for flag, value in UNKNOWN_VALUE_FLAGS:
    # Place the unknown flag amid the known persona so the parse can't trivially "keep the tail".
    source_cmd = f'codex -m gpt-5.5 {flag} {value} -c model_reasoning_effort=high {BYPASS_A} {BYPASS_H} "baked"'

    fork = codex.fork_command(source_cmd, CHAT_ID)
    check(f"R1 fork: unknown {flag} survives WITH its value",
          flag in fork and value in fork and fork[fork.index(value) - 1] == flag)
    check(f"R1 fork ({flag}): the baked positional prompt is NOT carried forward", "baked" not in fork)

    # handover AND rollover both derive via seed_command.
    handover = codex.seed_command(source_cmd, SEED)
    check(f"R1 handover: unknown {flag} survives WITH its value",
          flag in handover and value in handover and handover[handover.index(value) - 1] == flag)
    check(f"R1 handover ({flag}): the seed is the positional tail (not swallowed by the unknown flag)",
          handover[-1] == SEED)

    rollover_cmd = _env_prefix({"TX_SESSION_ID": "src"}) + shlex.join(codex.seed_command(source_cmd, SEED))
    check(f"R1 rollover: unknown {flag} {value} survives after the env-prefix",
          f"{flag} {value}" in rollover_cmd and rollover_cmd.startswith("env TX_SESSION_ID=src "))


# ----- dispatch routing: ChatOps with a Codex-engine source routes to CodexEngine ----------------
# A minimal fake service captures the spawned command (no tmux, no disk, no real spawn — T8b §5),
# proving `chat.py`'s `registry.get(record.engine).<op>(...)` selects Codex for a Codex-engine record.

class _FakeStore:
    def __init__(self):
        self.by_id = {}

    def load(self, txid):
        return self.by_id[txid]

    def save(self, session):
        self.by_id[session.id] = session

    def all(self):
        return list(self.by_id.values())


class _FakeService:
    def __init__(self):
        self.store = _FakeStore()
        self.log = type("L", (), {"append": lambda *a, **k: None})()
        self.spawned = []
        self._by_name = {}

    def register(self, session):
        self.store.save(session)
        self._by_name[session.name] = session

    def get(self, name):
        return self._by_name.get(name)

    def reconcile(self):
        pass

    def spawn(self, spec):
        self.spawned.append(spec)
        new = Session(id="ID-" + spec.name, name=spec.name, kind=spec.kind, role=spec.role,
                      state=State.initial_for(spec.role), cwd=spec.cwd, cmd=spec.cmd,
                      tags=list(spec.tags), env=dict(spec.env))
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


service = _FakeService()
service.register(Session(
    id="CODEX-SRC", name="cx", kind=Kind.PROCESS, role=Role.LLM, state=State.IDLE, cwd="/work",
    cmd=SOURCE, engine=Engine.CODEX,
    chats=[ChatRef(id=CHAT_ID, role="original", cwd="/work", transcript_path="",
                   origin=Origin(how="spawn", session_id="CODEX-SRC", chat_id=None),
                   engine=Engine.CODEX)],
))
ChatOps(service).fork("cx", "cx-fork")
fork_cmd = service.spawned[-1].cmd
check("dispatch/fork routes to Codex: cmd is a `codex fork <id>` command",
      fork_cmd == shlex.join(codex.fork_command(SOURCE, CHAT_ID))
      and fork_cmd.startswith(f"codex fork {CHAT_ID} "))

service = _FakeService()
ChatOps(service)._spawn_distiller("cx-distill", "rollover", "/work", "summarise", Engine.CODEX)
distiller_cmd = service.spawned[-1].cmd
check("dispatch/distiller routes to Codex: gpt-5.5/high fixed command",
      distiller_cmd == shlex.join(codex.distiller_command("summarise"))
      and distiller_cmd.startswith("codex -m gpt-5.5 -c model_reasoning_effort=high "))


print(f"OK — {PASSED} checks passed")
