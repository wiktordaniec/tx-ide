"""CLI command registry (stage S1a) — `tx <verb>` → a small command object.

A thin façade per the design: each command parses its own argv (boundary validation lives here)
and renders `SessionService` results — ZERO business logic. Objects are instantiated fresh per
invocation (no daemon). Out of scope here: the interactive `attach` picker (S1b), `hook` (S2), and
history / resume / fork / handover / rollover (S3/S4). This stage ships the **non-interactive
verbs at parity** plus the internal `_session-closed` / `_list` / `_init-home` seams (and the S0
`selfcheck` smoke test, kept hidden).

See tx-service-redesign.md §6 (CLI surface) + §1 ("CLI parses argv + renders; zero business logic").
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import claude
from .events import EventLog
from .render import picker_rows, render_ls
from .service import ServiceError, SessionService
from .session import SCHEMA_VERSION, ChatRef, Kind, Origin, Role, Session, State
from .spawn import SpawnSpec
from .storage import ensure_home, tx_ide_home
from .store import SessionStore
from .tmux import TmuxError


def _split_tags(raw: str) -> list[str]:
    return [part for part in raw.split(",") if part]


def _parse_env(pairs: list[str] | None) -> dict[str, str]:
    """`["K=V", ...]` → dict. `--env` is user input, so the `K=V` shape is validated by the CLI
    (argparse `type=`); here we just split on the first `=`."""
    env: dict[str, str] = {}
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        env[key] = value
    return env


def _env_pair(value: str) -> str:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"--env expects KEY=VALUE, got '{value}'")
    return value


def _default_shell() -> str:
    return os.environ.get("SHELL") or "zsh"


def _repo_root() -> Path:
    """The cloned repo root (for `start` to find `bin/tx-assistant`). `cli.py` → `lib/tx` → `lib`
    → repo."""
    return Path(__file__).resolve().parents[2]


class Command:
    name = ""
    summary = ""

    def __init__(self, service: SessionService):
        self.service = service

    def run(self, argv: list[str]) -> int:
        raise NotImplementedError

    def _parser(self) -> argparse.ArgumentParser:
        return argparse.ArgumentParser(prog=f"tx {self.name}", description=self.summary)

    def _default_cwd(self) -> str:
        return self.service.tmux.current_pane_path() or os.getcwd()


# ----- spawn family ------------------------------------------------------------------------


class SpawnCommand(Command):
    name = "spawn"
    summary = "Spawn a detached tmux session (--tag mandatory)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        parser.add_argument("--tag", required=True)
        parser.add_argument("--cwd")
        parser.add_argument("--cmd")
        parser.add_argument("--chat", action="store_true",
                            help="mint a chat id (TX_CHAT_ID) the command can resume")
        parser.add_argument("--env", action="append", type=_env_pair)
        args = parser.parse_args(argv)
        tags = _split_tags(args.tag)
        if not tags:
            parser.error("--tag requires at least one value")
        spec = SpawnSpec.for_process(
            name=args.name, tags=tags, cwd=args.cwd or self._default_cwd(),
            cmd=args.cmd or _default_shell(), env=_parse_env(args.env), chat=args.chat,
        )
        session = self.service.spawn(spec)
        print(f"Spawned '{session.name}' (cwd={session.cwd}, tag={args.tag})")
        return 0


class SpawnNvimCommand(Command):
    name = "spawn-nvim"
    summary = "Spawn a detached nvim companion (--diff opens a diffview, base defaults to main)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        parser.add_argument("--tag", required=True)
        parser.add_argument("--cwd")
        parser.add_argument("--diff", nargs="?", const="main", default=None)
        parser.add_argument("--env", action="append", type=_env_pair)
        args = parser.parse_args(argv)
        tags = _split_tags(args.tag)
        if not tags:
            parser.error("--tag requires at least one value")
        spec = SpawnSpec.for_nvim(
            name=args.name, tags=tags, cwd=args.cwd or self._default_cwd(),
            env=_parse_env(args.env), diff_base=args.diff,
        )
        session = self.service.spawn_nvim(spec)
        suffix = f", diff={args.diff}" if args.diff is not None else ""
        print(f"Spawned nvim '{session.name}' (cwd={session.cwd}, tag={args.tag}{suffix})")
        return 0


class SpawnViewCommand(Command):
    name = "spawn-view"
    summary = "Spawn a detached view session (kind=view); --tag defaults to 'views'."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        parser.add_argument("--tag", default="views")
        parser.add_argument("--cwd")
        parser.add_argument("--cmd")
        parser.add_argument("--env", action="append", type=_env_pair)
        args = parser.parse_args(argv)
        spec = SpawnSpec.for_view(
            name=args.name, tags=_split_tags(args.tag), cwd=args.cwd or self._default_cwd(),
            cmd=args.cmd or _default_shell(), env=_parse_env(args.env),
        )
        session = self.service.spawn_view(spec)
        print(f"Spawned view '{session.name}' (cwd={session.cwd}, tag={args.tag})")
        return 0


# ----- listing / inspection ----------------------------------------------------------------


class LsCommand(Command):
    name = "ls"
    summary = "List current (live) sessions in VIEWS / PROCESSES sections."

    def run(self, argv: list[str]) -> int:
        self._parser().parse_args(argv)  # no args; honors -h
        self.service.reconcile()  # reconcile-on-read (§4) — dead records drop out below
        live = [session for session in self.service.store.all() if session.is_alive()]
        print(render_ls(live))
        return 0


class ListCommand(Command):
    name = "_list"
    summary = "Internal: tab-separated picker feed (consumed by the S1b fzf picker)."

    def run(self, argv: list[str]) -> int:
        # Lenient on argv: the S1b picker layers global flags (--all/--host) on top of this seam.
        self.service.reconcile()
        live = [session for session in self.service.store.all() if session.is_alive()]
        print(picker_rows(live))
        return 0


class ShowCommand(Command):
    name = "show"
    summary = "Print a session record as JSON (by id or name)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("target")
        args = parser.parse_args(argv)
        session = self.service.get(args.target)
        if session is None:
            print(f"tx show: no record for '{args.target}'", file=sys.stderr)
            return 1
        print(json.dumps(session.to_dict(), indent=2))
        return 0


# ----- mutations ---------------------------------------------------------------------------


class TagCommand(Command):
    name = "tag"
    summary = "Read or set a session's tags (comma-separated)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        parser.add_argument("tags", nargs="?")
        args = parser.parse_args(argv)
        if args.tags is None:
            session = self.service.get(args.name)
            if session is None:
                print(f"tx tag: session '{args.name}' not found", file=sys.stderr)
                return 1
            print(",".join(session.tags))
            return 0
        session = self.service.tag(args.name, _split_tags(args.tags))
        print(f"Tagged '{session.name}' (tag={args.tags})")
        return 0


class RenameCommand(Command):
    name = "rename"
    summary = "Rename a session (tmux session + record)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        parser.add_argument("new_name")
        args = parser.parse_args(argv)
        session = self.service.rename(args.name, args.new_name)
        print(f"Renamed to '{session.name}'")
        return 0


class KillCommand(Command):
    name = "kill"
    summary = "End a tmux session and mark its record EXITED."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        args = parser.parse_args(argv)
        session = self.service.kill(args.name)
        print(f"Killed '{session.name}'")
        return 0


class ArchiveCommand(Command):
    name = "archive"
    summary = "Retire a session (mark ARCHIVED, keep the record)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        args = parser.parse_args(argv)
        session = self.service.archive(args.name)
        print(f"Archived '{session.name}'")
        return 0


class RmCommand(Command):
    name = "rm"
    summary = "Delete a session record (by id or name)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("target")
        args = parser.parse_args(argv)
        if self.service.remove(args.target):
            print(f"Removed record for '{args.target}'")
            return 0
        print(f"tx rm: no record for '{args.target}'", file=sys.stderr)
        return 1


class SendMessageCommand(Command):
    name = "send-message"
    summary = "Peer-message another Claude Code session."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("target")
        parser.add_argument("body")
        args = parser.parse_args(argv)
        self.service.send_message(args.target, args.body)
        return 0


class StartCommand(Command):
    name = "start"
    summary = "Ensure the Views home base + tx-assistant exist, then attach Views."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("-r", "--restart", action="store_true",
                            help="kill an existing tx-assistant first so warmup recreates it")
        args = parser.parse_args(argv)
        tmux = self.service.tmux
        repo = _repo_root()

        if args.restart and tmux.has_session("tx-assistant"):
            tmux.kill_session("tx-assistant")
        if tmux.has_session("tx-assistant"):
            print("tx-assistant already running.")
        else:
            subprocess.run([str(repo / "bin" / "tx-assistant"), "--warm"])
            print("tx-assistant session created.")

        if not tmux.has_session("Views"):
            self.service.spawn_view(SpawnSpec.for_view(
                name="Views", tags=["views"], cwd=str(repo), cmd=_default_shell(),
            ))
            print("Views session created.")

        if os.environ.get("TMUX"):
            tmux.switch_client("Views")
        else:
            subprocess.run([tmux.binary, "attach", "-t", "Views"])
        return 0


# ----- internal seams ----------------------------------------------------------------------


class SessionClosedCommand(Command):
    name = "_session-closed"
    summary = "Internal: tmux session-closed hook — stamp EXITED + ended_at (carries the uuid)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("session_id")
        args = parser.parse_args(argv)
        # No-op when the id isn't ours (D4) or the record is already terminal (C3).
        self.service.record_state(args.session_id, State.EXITED)
        return 0


class InitHomeCommand(Command):
    name = "_init-home"
    summary = "Internal: create the $TX_IDE_HOME skeleton (idempotent) — the installer's seam."

    def run(self, argv: list[str]) -> int:
        home = ensure_home()
        print(f"initialized $TX_IDE_HOME skeleton at {home}")
        return 0


class SelfCheckCommand(Command):
    name = "selfcheck"
    summary = "Internal: round-trip a record through SessionStore + exercise the C3 terminal guard."

    def run(self, argv: list[str]) -> int:
        """The S0 foundation smoke test, preserved through the S1a entry rewrite. Touches only the
        store + log (no tmux side effects); cleans up its own demo record."""
        ensure_home()
        store = SessionStore()
        log = EventLog()
        failures: list[str] = []

        def check(condition: bool, label: str) -> None:
            if not condition:
                failures.append(label)

        session_id = str(uuid.uuid4())
        now = time.time()
        cwd = str(tx_ide_home())
        demo = Session(
            id=session_id, name="s1a-selfcheck", kind=Kind.PROCESS, role=Role.LLM,
            state=State.initial_for(Role.LLM), cwd=cwd,
            cmd="claude --dangerously-skip-permissions", tags=["s1a", "selfcheck"],
            created_at=now, last_activity=now,
            chats=[ChatRef(
                id=None, role="original", cwd=cwd,
                transcript_path=str(claude.transcript_path("00000000-pending", cwd)),
                origin=Origin(how="spawn", session_id=session_id, chat_id=None), started_at=now,
            )],
        )

        check(demo.state == State.IDLE, "llm spawn state is IDLE (initial_for)")
        store.save(demo)
        log.append("selfcheck", f"created {demo.name} ({session_id})")

        loaded = store.load(session_id)
        check(loaded is not None, "load returns the saved record")
        check(loaded == demo, "save → load round-trips identically")
        check(loaded.transition_to(State.WORKING) is True, "IDLE → WORKING applies")
        check(loaded.transition_to(State.EXITED) is True, "WORKING → EXITED applies")
        check(loaded.transition_to(State.WORKING) is False, "C3: EXITED is absorbing (refused)")
        check(loaded.transition_to(State.EXITED) is False, "no-op transition reports no change")

        found = store.find_by_name("s1a-selfcheck")
        check(found is not None and found.id == session_id, "find_by_name locates the record")
        check(session_id in {s.id for s in store.all()}, "all() lists the record")
        check(store.delete(session_id) is True, "delete removes the record")
        check(store.load(session_id) is None, "record is gone after delete")

        if failures:
            print("S1a self-check FAILED ✗")
            for label in failures:
                print(f"  - {label}")
            return 1
        print("S1a self-check PASSED ✓")
        print(f"  home={tx_ide_home()}  schema v{SCHEMA_VERSION}  records now={len(store.all())}")
        return 0


PUBLIC_COMMANDS: list[type[Command]] = [
    StartCommand, LsCommand, SpawnCommand, SpawnNvimCommand, SpawnViewCommand, TagCommand,
    RenameCommand, SendMessageCommand, KillCommand, ArchiveCommand, RmCommand, ShowCommand,
]
HIDDEN_COMMANDS: list[type[Command]] = [
    ListCommand, SessionClosedCommand, InitHomeCommand, SelfCheckCommand,
]


def _registry(service: SessionService) -> dict[str, Command]:
    return {cls.name: cls(service) for cls in (*PUBLIC_COMMANDS, *HIDDEN_COMMANDS)}


def _print_help() -> None:
    print("tx — tmux + Claude Code session controller\n")
    print("usage: tx <command> [args]\n")
    print("commands:")
    for cls in PUBLIC_COMMANDS:
        print(f"  {cls.name:<13} {cls.summary}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ensure_home()
    service = SessionService()
    registry = _registry(service)

    command_name = argv[0] if argv else ""
    if command_name in ("", "-h", "--help", "help"):
        _print_help()
        return 0
    command = registry.get(command_name)
    if command is None:
        print(f"tx: unknown command: {command_name}\n", file=sys.stderr)
        _print_help()
        return 2
    try:
        return command.run(argv[1:])
    except (ServiceError, TmuxError) as error:
        print(f"tx {command_name}: {error}", file=sys.stderr)
        return 1
