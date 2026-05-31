"""CLI command registry (stages S1a + S1b) — `tx <verb>` → a small command object.

A thin façade per the design: each command parses its own argv (boundary validation lives here)
and renders `SessionService` results — ZERO business logic for the non-interactive verbs. The S1b
`attach` picker is the one interactive front-end (§1): it builds the fzf UI, feeds rows from the
service (`tx _list`), and drives the post-selection nest-attach through the `Tmux` adapter. Objects
are instantiated fresh per invocation (no daemon). S3 adds the browse-the-past verbs (`history` /
`chat ls` / `resume`) + forces ingest from `archive`; `resume` orchestrates over the frozen
`service.spawn` (there is no `service.resume` use-case and `service.py` is off-limits to S3). Still
out of scope: fork / handover / rollover (S4). Hidden seams: `_session-closed` / `_list` /
`_edit-tag` / `_init-home` (and the S0 `selfcheck` smoke test).

See tx-service-redesign.md §6 (CLI surface) + §1 ("CLI parses argv + renders") + C12 (picker keys).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import chat, claude, history, hooks, palette, sync
from .chat import ChatOps
from .events import EventLog
from .render import picker_display_rows, picker_namew, render_chats, render_history, render_ls
from .service import ServiceError, SessionService
from .session import SCHEMA_VERSION, ChatRef, Kind, Origin, Role, Session, State
from .spawn import SHELL_COMMANDS, SpawnSpec
from .storage import LocalStorage, ensure_home, tx_ide_home
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


def detect_term_cols() -> int:
    """Width of the surrounding terminal/popup (port of the bash `detect_term_cols`). Inside a tmux
    popup, `/dev/tty` is the popup's pty so this returns the popup width; falls back to $COLUMNS / 80
    when there is no controlling terminal (e.g. a non-interactive `tx _list`)."""
    try:
        with open("/dev/tty") as tty:
            return os.get_terminal_size(tty.fileno()).columns
    except OSError:
        return shutil.get_terminal_size(fallback=(80, 24)).columns


def _env_namew() -> int:
    """The NAME column width the picker exported as `$NAMEW` so a `reload-sync` subshell (`tx _list`)
    renders at the same width as the initial paint. Default 18 when run standalone (matches bash)."""
    raw = os.environ.get("NAMEW", "")
    return int(raw) if raw.isdigit() else 18


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
    summary = "Internal: ANSI fzf picker feed (the initial paint + each reload-sync of `tx attach`)."

    def run(self, argv: list[str]) -> int:
        # Lenient on argv (the picker is the only caller). Reconcile-on-read (~1 Hz from the picker's
        # refresh loop) keeps the list fresh: vanished sessions drop out below, new ones appear. The
        # NAME width comes from $NAMEW so this matches the picker's initial paint width.
        self.service.reconcile()
        live = [session for session in self.service.store.all() if session.is_alive()]
        print(picker_display_rows(live, _env_namew()))
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


# ----- history browse + resume (S3) --------------------------------------------------------
# Browse the past (read) is first-class; reviving it (write) is a deliberate, explicit step (§7).
# The everyday picker stays live-only — these are the separate "look at / bring back the past" verbs.


def _parse_history_date(raw: str | None, parser: argparse.ArgumentParser, flag: str,
                        *, end_of_day: bool = False) -> float | None:
    """Parse a `--since` / `--until` YYYY-MM-DD filter to an epoch second (local midnight, or
    23:59:59 for an inclusive `--until`). A bad date is user input at the CLI boundary → `error`."""
    if raw is None:
        return None
    try:
        moment = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        parser.error(f"{flag} expects YYYY-MM-DD, got '{raw}'")
    if end_of_day:
        moment = moment.replace(hour=23, minute=59, second=59)
    return moment.timestamp()


def _latest_chat(session: Session) -> ChatRef | None:
    """The chat `tx resume` reattaches: the last `ChatRef` carrying a real id, preferring one still
    open (`ended_at is None`). Forks / rollovers append in order, so the last is the live thread."""
    candidates = [chat for chat in session.chats if chat.id is not None]
    if not candidates:
        return None
    open_chats = [chat for chat in candidates if chat.ended_at is None]
    return (open_chats or candidates)[-1]


class HistoryCommand(Command):
    name = "history"
    summary = "List past (EXITED / ARCHIVED) sessions — filter by --tag / --cwd / --since / --until."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("--tag", help="only sessions carrying this tag")
        parser.add_argument("--cwd", help="only sessions whose cwd contains this substring")
        parser.add_argument("--since", metavar="YYYY-MM-DD", help="ended on or after this date")
        parser.add_argument("--until", metavar="YYYY-MM-DD", help="ended on or before this date")
        args = parser.parse_args(argv)
        since = _parse_history_date(args.since, parser, "--since")
        until = _parse_history_date(args.until, parser, "--until", end_of_day=True)
        # Reconcile first (§4) so a just-vanished session is stamped EXITED and surfaces here, not
        # only on the next `ls`. Terminal records are untouched by reconcile, so this is cheap.
        self.service.reconcile()
        past = self.service.store.query(
            lambda session: session.state in (State.EXITED, State.ARCHIVED)
        )
        shown = [s for s in past if self._matches(s, args.tag, args.cwd, since, until)]
        print(render_history(shown))
        return 0

    def _matches(self, session: Session, tag: str | None, cwd: str | None,
                 since: float | None, until: float | None) -> bool:
        if tag is not None and tag not in session.tags:
            return False
        if cwd is not None and cwd not in session.cwd:
            return False
        ended = session.ended_at or session.last_activity or 0
        if since is not None and ended < since:
            return False
        if until is not None and ended > until:
            return False
        return True


class ChatCommand(Command):
    name = "chat"
    summary = "Inspect a session's chats: `chat ls <session>` lists its ChatRefs + bundle paths."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("subcommand", choices=["ls"], help="ls — list the session's chats")
        parser.add_argument("session")
        args = parser.parse_args(argv)
        session = self.service.get(args.session)
        if session is None:
            print(f"tx chat ls: session '{args.session}' not found", file=sys.stderr)
            return 1
        print(render_chats(session))
        return 0


class ResumeCommand(Command):
    name = "resume"
    summary = "Re-spawn a past session + reattach its chat (claude --resume); collision-safe (§7)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("target", help="the past session to resume (id or name)")
        parser.add_argument("--as", dest="new_name", metavar="NAME",
                            help="spawn under a new name (required on a live-name clash)")
        parser.add_argument("--cwd", metavar="DIR",
                            help="override the cwd (required if the stored cwd is gone — C8)")
        args = parser.parse_args(argv)

        record = self.service.get(args.target)
        if record is None:
            print(f"tx resume: no record for '{args.target}'", file=sys.stderr)
            return 1
        chat = _latest_chat(record)
        if chat is None:
            print(f"tx resume: '{record.name}' has no chat to resume — use `tx spawn` for a fresh "
                  "session", file=sys.stderr)
            return 1

        # Name: --as wins; else reuse the stored name. tmux names must be unique among LIVE sessions
        # (D7), so a live clash is the one case that forces --as (§7).
        name = args.new_name or record.name
        if self.service.tmux.has_session(name):
            print(f"tx resume: a live session named '{name}' already exists — pass --as <new-name>",
                  file=sys.stderr)
            return 1

        # cwd: transcripts are munged-cwd-keyed (C8), so restore the stored cwd by default and error
        # for an explicit --cwd if it is gone. (Resuming into a cwd other than the chat's own may not
        # reattach the exact transcript — claude resolves --resume within the current project dir.)
        cwd = args.cwd or record.cwd
        if not Path(cwd).is_dir():
            remedy = "pass --cwd <dir>" if args.cwd is None else f"'{cwd}' is not a directory"
            print(f"tx resume: cwd '{cwd}' does not exist — {remedy} (C8)", file=sys.stderr)
            return 1
        if history.resolve_transcript(chat.id, cwd) is None:
            print(f"tx resume: warning — transcript for chat {chat.id[:8]} not found under {cwd}; "
                  "claude --resume may start a fresh conversation", file=sys.stderr)

        resume_cmd = shlex.join(claude.build_launch_command(resume=chat.id))
        spec = SpawnSpec.for_process(
            name=name, tags=list(record.tags), cwd=cwd, cmd=resume_cmd, env=dict(record.env),
        )
        new = self.service.spawn(spec)
        self._attach_resumed_chat(new, chat, cwd)
        print(f"Resumed '{record.name}' as '{new.name}' (chat {chat.id[:8]}, cwd={cwd})")
        return 0

    def _attach_resumed_chat(self, new: Session, source: ChatRef, cwd: str) -> None:
        """Record the resumed conversation on the NEW session so `tx chat ls` shows it and ingest
        mirrors it forward. role=original — it continues the source's primary conversation, not a
        fork/rollover/handover; origin.how="resume" is S3's provenance edge back to the source chat.
        bundle_path points at where THIS session's ingest will write (history/<new-tx>/<chat>/)."""
        now = time.time()
        new.chats.append(ChatRef(
            id=source.id,
            role="original",
            cwd=cwd,
            transcript_path=str(claude.transcript_path(source.id, cwd)),
            origin=Origin(how="resume", session_id=new.id, chat_id=source.id),
            bundle_path=str(claude.bundle_dir(new.id, source.id)),
            started_at=now,
        ))
        self.service.store.save(new)


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
    summary = "Retire a session (mark ARCHIVED, keep the record) + force a full history ingest."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("name")
        args = parser.parse_args(argv)
        session = self.service.archive(args.name)
        # archive = retire + ingest (§11). The Stop-path mirror is coalescing/best-effort, so a
        # retire forces a BLOCKING, complete mirror (wait=True) — the bundle is final after this.
        bundles = history.ingest_session(self.service.store, session.id, wait=True)
        suffix = f" (ingested {len(bundles)} chat bundle(s))" if bundles else ""
        print(f"Archived '{session.name}'{suffix}")
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


class SyncCommand(Command):
    name = "sync"
    summary = "Manual archive sync of the reproducible corpus (push/pull/status) — never hot-path."

    def run(self, argv: list[str]) -> int:
        """`tx sync push|pull|status` (§14, S7). Local (`$TX_IDE_HOME`) is always the working set;
        this is a manual archive layer over the `Storage` boundary, run only when you type it. The
        remote is `--remote PATH` (a local archive dir / the S3 proxy), `--s3 BUCKET[/PREFIX]` (the
        deferred stub), or the `sync` section of `config.json`."""
        parser = self._parser()
        parser.add_argument("action", choices=["push", "pull", "status"])
        parser.add_argument("--remote", metavar="PATH",
                            help="a local filesystem remote (archive dir / S3 dogfood proxy)")
        parser.add_argument("--s3", metavar="BUCKET[/PREFIX]",
                            help="select the S3 backend (deferred — reports 'not implemented')")
        args = parser.parse_args(argv)

        local = sync.local_storage()
        remote = self._resolve_remote(args)
        if args.action == "status":
            return self._status(local, remote)
        if remote is None:
            print("tx sync: no remote configured — pass --remote PATH / --s3 BUCKET, or set the "
                  "'sync' section in config.json", file=sys.stderr)
            return 1
        operation = sync.sync_push if args.action == "push" else sync.sync_pull
        try:
            result = operation(local, remote)
        except NotImplementedError as error:  # S3 stub — surface cleanly, no traceback (§14)
            print(f"tx sync {args.action}: {error}", file=sys.stderr)
            return 1
        self._print_result(args.action, sync.remote_label(remote), result)
        return 0

    def _resolve_remote(self, args: argparse.Namespace):
        if args.s3:
            bucket, _, prefix = args.s3.partition("/")
            return sync.remote_from_spec({"backend": "s3", "bucket": bucket, "prefix": prefix})
        if args.remote:
            return sync.remote_from_spec({"backend": "local", "path": args.remote})
        return sync.remote_from_config()

    def _status(self, local: LocalStorage, remote) -> int:
        status = sync.corpus_status(local)
        print(f"Local corpus ({sync.remote_label(local)}):")
        print(f"  records:        {status.records}")
        print(f"  history files:  {status.history_files}")
        print(f"  log.jsonl:      {'present' if status.has_log else 'missing'}")
        print(f"  config.json:    {'present' if status.has_config else 'missing'}")
        print(f"  total keys:     {status.total}")
        if remote is None:
            print("Remote: none configured (local-only; pass --remote/--s3 or set config.json).")
            return 0
        label = sync.remote_label(remote)
        try:
            to_push = sync.sync_diff(local, remote)
            to_pull = sync.sync_diff(remote, local)
        except NotImplementedError as error:
            print(f"Remote ({label}): {error}")
            return 0
        print(f"Remote ({label}): {to_push} key(s) to push, {to_pull} key(s) to pull.")
        return 0

    def _print_result(self, action: str, label: str, result: sync.SyncResult) -> None:
        direction = "→" if action == "push" else "←"
        print(f"sync {action} (local {direction} {label}): "
              f"{len(result.added)} added, {len(result.updated)} updated, "
              f"{len(result.unchanged)} unchanged"
              + (f", {len(result.kept)} kept (destination newer)" if result.kept else ""))


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


# ----- interactive picker (S1b) ------------------------------------------------------------


class AttachCommand(Command):
    """`tx attach` — the interactive fzf picker (a faithful port of the old bash `cmd_pick`).

    The fzf machinery is preserved exactly: `--listen` + a ~1 Hz `reload-sync` refresh loop
    (reconcile-on-read), the `TX_ARM_FILE` two-press Ctrl-D kill, Ctrl-T retag-in-popup, the focus /
    armed headers, `--jump`, and `-f` prefill (C12). Rows are fed from the Python service (`tx
    _list`); the post-selection action nest-attaches the chosen session into the launching Views
    pane (else switch-client / foreground attach), preserving today's behavior (the real
    attachment-topology join is S6). Views are filtered out (§7); only CURRENT live sessions list.
    """

    name = "attach"
    summary = "Open the interactive session picker (fzf)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("-f", "--filter", dest="query", default="", metavar="QUERY",
                            help="pre-fill the search with QUERY")
        parser.add_argument("-j", "--jump", action="store_true",
                            help="Enter focuses the existing pane hosting the session instead of "
                                 "nest-attaching here (popup-friendly)")
        parser.add_argument("--host", nargs="?", const="personal", metavar="ALIAS",
                            help="pick from remote tmux on ssh ALIAS (not wired in S1b — C11/S1a)")
        parser.add_argument("--all", dest="mix", action="store_true",
                            help="pick from local + remote (not wired in S1b — C11/S1a)")
        args = parser.parse_args(argv)

        if args.host or args.mix:
            print("tx attach: the remote picker (--host/--all) is a C11 non-store passthrough "
                  "owned by S1a; it is not wired into the S1b local picker yet.", file=sys.stderr)
            return 2

        # Size the NAME column once (terminal width + longest live name) and export it so each
        # reload-sync subshell (`tx _list`) renders at the same width as the initial paint.
        self.service.reconcile()
        live = [s for s in self.service.store.all() if s.is_alive() and s.kind != Kind.VIEW]
        namew = picker_namew(detect_term_cols(), max((len(s.name) for s in live), default=0))
        os.environ["NAMEW"] = str(namew)

        # Arm file for the two-press Ctrl-D kill: holds the row armed by the last Ctrl-D (empty =
        # not armed). Cleared on every cursor move and on confirm/cancel; removed when we exit.
        handle, arm_file = tempfile.mkstemp(prefix="tx-kill-arm.")
        os.close(handle)
        os.environ["TX_ARM_FILE"] = arm_file
        try:
            return self._loop(args.jump, self._fzf_opts(namew, args.query), namew)
        finally:
            Path(arm_file).unlink(missing_ok=True)

    # ----- fzf invocation ------------------------------------------------------------------

    def _fzf_opts(self, namew: int, query: str) -> list[str]:
        """Build the fzf argv — a 1:1 port of the bash `opts` array. The `{1}` / `{2}` placeholders
        and `$TX_ARM_FILE` / `$FZF_PORT` stay literal (fzf / its child shell expand them); palette
        escapes, the column header, and the `bin/tx` path are interpolated here."""
        bin_tx = str(_repo_root() / "bin" / "tx")
        reload = f"reload-sync({bin_tx} _list)"
        # Ctrl-T's `_edit-tag` runs inside a `display-popup`, which tmux spawns in the SERVER's
        # environment — it does NOT inherit the picker's $TX_IDE_HOME the way the fzf-child paths
        # (reload-sync / kill) do. Bake the resolved home + PYTHONPATH + interpreter into the command
        # (the same C9 idea as the hook shims) so the retag edits the SAME store the picker shows —
        # otherwise it falls back to the default ~/.tx-ide and spams the v1-record warnings.
        edit_tag = (
            f"env TX_IDE_HOME={tx_ide_home()} PYTHONPATH={_repo_root() / 'lib'} "
            f"{sys.executable} -m tx _edit-tag {{1}}"
        )
        bold, reset = palette.BOLD, palette.RESET
        header_cols = f"{'NAME':<{namew}}   STARTED IDLE   TAGS"

        # Focus header: bold-accent name + bold-fg tag chips on line 1 (mirrors the active-pane
        # title), the column header on line 2. {1}=name, {2}=plain chips. `\n` stays literal so the
        # popup printf makes two lines.
        focus_cmd = (
            f"printf '{palette.ACCENT_ANSI}{bold}%s{reset} "
            f"{palette.FG_ANSI}{bold}%s{reset}\\n%s' {{1}} {{2}} '{header_cols}'"
        )
        # Armed header: line 1 swapped for a bold-yellow kill prompt — `[y/N]` (NOT `(y/N)`: a `)`
        # would close the fzf action arg); line 2 keeps the column header so the layout is stable.
        arm_cmd = (
            f"printf '{palette.WARN_ANSI}{bold} ⚠  Kill \"%s\"? [y/N]{reset}\\n%s' "
            f"{{1}} '{header_cols}'"
        )
        color = (
            f"fg:{palette.DIM_FG_HEX},pointer:{palette.ACCENT_HEX},fg+:{palette.DIM_FG_HEX}:regular,"
            f"bg+:{palette.SELECTION_BG}:regular,hl:{palette.ACCENT_HEX},hl+:{palette.ACCENT_HEX},"
            f"header:{palette.DIM_FG_HEX},footer:{palette.DIM_FG_HEX},prompt:{palette.DIM_FG_HEX},"
            f"query:{palette.FG_HEX}"
        )
        return [
            "fzf", "--exact", "--ansi", "--prompt=  ❯ ", "--height=100%", "--reverse",
            "--delimiter=\t", "--with-nth=4..", "--listen", "--track",
            f"--color={color}", f"--header={header_cols}", f"--query={query}",
            # Refresh loop (reconcile-on-read) + unbind y/n on start so they fall through to query
            # input until Ctrl-D arms a row. ESC is left untouched so it always aborts.
            f'--bind=start:execute-silent(( while sleep 1; do '
            f'curl -fsS -XPOST "localhost:$FZF_PORT" -d "{reload}" >/dev/null 2>&1 || exit 0; '
            f'done ) &)+unbind(y,n)',
            f'--bind=ctrl-r:{reload}',
            # Cursor move re-renders the focus header, clears the arm file, unbinds the confirm keys.
            f'--bind=focus:transform-header({focus_cmd})+execute-silent(: >"$TX_ARM_FILE")'
            f'+unbind(y,n)',
            # Ctrl-T: edit tags in a popup (readline pre-fill), then reload to show the new chips.
            # The popup command carries a baked $TX_IDE_HOME (see `edit_tag`) — `display-popup` does
            # not inherit fzf's environment, so without it the retag hits the default home.
            f'--bind=ctrl-t:execute(tmux display-popup -E -h 5 -w 60% "{edit_tag}")+{reload}',
            # Two-press Ctrl-D kill: arm the row, swap in the prompt header, rebind y/n. `y` drives
            # `tx kill` (record → EXITED + logged, not a raw kill-session) then reloads; `n` /
            # cursor-move cancel and restore the focus header. y/n unbind themselves after firing.
            f'--bind=ctrl-d:execute-silent(printf \'%s\' {{1}} >"$TX_ARM_FILE")'
            f'+transform-header({arm_cmd})+rebind(y,n)',
            f'--bind=y:execute-silent({bin_tx} kill {{1}} >/dev/null 2>&1; : >"$TX_ARM_FILE")'
            f'+{reload}+unbind(y,n)',
            f'--bind=n:transform-header({focus_cmd})+execute-silent(: >"$TX_ARM_FILE")+unbind(y,n)',
        ]

    def _loop(self, jump: bool, opts: list[str], namew: int) -> int:
        """Re-render the feed, run fzf, act on the selection — looping only on a recoverable miss
        (a vanished session / a failed jump), exactly like the bash `while :` loop."""
        while True:
            result = subprocess.run(opts, input=self._render_feed(namew),
                                    stdout=subprocess.PIPE, text=True)
            if result.returncode != 0 or not result.stdout.strip():
                return 0  # ESC / abort / empty list
            name = result.stdout.rstrip("\n").split("\t")[0]
            if jump:
                if self._jump_to_session(name):
                    return 0
                print(f"tx: could not jump to or switch to session {name}", file=sys.stderr)
                time.sleep(1.2)
                continue
            if self._nest_attach(name):
                return 0
            time.sleep(1.2)

    def _render_feed(self, namew: int) -> str:
        self.service.reconcile()
        live = [s for s in self.service.store.all() if s.is_alive()]
        return picker_display_rows(live, namew)

    # ----- post-selection action -----------------------------------------------------------

    def _nest_attach(self, name: str) -> bool:
        """Attach the chosen LOCAL session (cmd_pick's loop body): nest-attach into the launching
        Views pane when applicable, else switch-client / foreground attach. False = the session
        vanished (re-loop)."""
        if not self.service.tmux.has_session(name):
            print(f"tx: session '{name}' does not exist", file=sys.stderr)
            return False
        if self._respawn_into_view_pane(name):
            return True
        return self._switch_or_attach(name)

    def _jump_to_session(self, name: str) -> bool:
        """`--jump`: focus the existing pane already hosting `name` instead of nest-attaching here
        (port of `jump_to_session`). Falls back to nest-attach-into-view, then switch-client."""
        tmux = self.service.tmux
        if not tmux.has_session(name):
            print(f"tx: session '{name}' does not exist", file=sys.stderr)
            return False
        current_session = tmux.current_session_name() or ""
        if current_session == name:
            return True
        target = tmux.pane_for_session(name, current_session)
        if target and target.split(":")[0] == current_session:
            return tmux.select_window(target) and tmux.select_pane(target)
        if self._respawn_into_view_pane(name):
            return True
        try:
            tmux.switch_client(name)
            return True
        except TmuxError:
            return False

    def _respawn_into_view_pane(self, name: str) -> bool:
        """If the picker was launched from a shell pane inside a Views home, nest-attach `name` INTO
        that pane via `respawn-pane -k` (the `TMUX= tmux attach …; exec $SHELL` keeps the pane alive
        after the inner session detaches). Returns True when it did, False to fall through."""
        tmux = self.service.tmux
        if not os.environ.get("TMUX"):
            return False
        origin_pane = tmux.current_pane_id()
        current_session = tmux.current_session_name() or ""
        if not origin_pane or not self._is_view_session(current_session):
            return False
        origin_cmd = tmux.display_message("#{pane_current_command}", target=origin_pane)
        if origin_cmd not in SHELL_COMMANDS:
            return False
        quoted = shlex.quote(name)
        tmux.respawn_pane(origin_pane, f"TMUX= tmux attach -t {quoted}; exec ${{SHELL:-zsh}}")
        return True

    def _switch_or_attach(self, name: str) -> bool:
        """Switch the calling client to `name` (inside tmux) or foreground-attach (outside). A
        failed switch-client (the client may be gone) is tolerated — cmd_pick ignores its status."""
        tmux = self.service.tmux
        if os.environ.get("TMUX"):
            try:
                tmux.switch_client(name)
            except TmuxError:
                pass
        else:
            tmux.attach_session(name)
        return True

    def _is_view_session(self, name: str) -> bool:
        """Whether `name` is recorded with kind=view — gates the nest-attach (only a Views pane
        hosts nested sessions). Port of `is_view_session`, resolved through the service."""
        if not name:
            return False
        session = self.service.get(name)
        return session is not None and session.kind == Kind.VIEW


def _prompt_with_default(prompt: str, default: str) -> str | None:
    """Read a line with `default` pre-inserted and editable (the old `_edit-tag` readline hook —
    macOS bash 3.2 lacked `read -i`). Returns the line, or None on cancel (Ctrl-C / Ctrl-D)."""
    import readline

    def preinsert() -> None:
        readline.insert_text(default)
        readline.redisplay()

    readline.set_pre_input_hook(preinsert)
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    finally:
        readline.set_pre_input_hook(None)


class EditTagCommand(Command):
    name = "_edit-tag"
    summary = "Internal: readline tag editor for the picker's Ctrl-T popup."

    def run(self, argv: list[str]) -> int:
        """Invoked from the Ctrl-T `tmux display-popup`. Pre-fills the current tags, lets the user
        edit them, and writes back through `SessionService.tag` (empty input clears; cancel leaves
        them untouched)."""
        parser = self._parser()
        parser.add_argument("session")
        args = parser.parse_args(argv)
        session = self.service.get(args.session)
        if session is None:
            print(f"tx: session '{args.session}' not found (not tx-managed)", file=sys.stderr)
            return 1
        edited = _prompt_with_default(f"Tags for {args.session}: ", ",".join(session.tags))
        if edited is None:
            return 1  # cancelled — leave the tags untouched
        self.service.tag(session.name, _split_tags(edited))
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


class HookCommand(Command):
    name = "hook"
    summary = "Internal: Claude/tmux hook entry — drive session state (S2)."

    def run(self, argv: list[str]) -> int:
        # The frozen contract is `tx hook <event>` (prompt-submit / stop / session-end /
        # session-closed). All mapping + no-op rules (D4/C6) live in hooks.py; this just routes.
        return hooks.dispatch(self.service, argv)


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


# ----- chat operations (S4) ----------------------------------------------------------------
# fork / handover / rollover + the hidden async-tail finish verbs. Thin façades over `ChatOps`
# (lib/tx/chat.py) — argv parsing + rendering only, zero choreography. All transcription-based,
# never `-p` (chat-ops.md CHD). See chat-ops.md §4 (fork) / §5 (handover) / §6 (rollover).


class ForkCommand(Command):
    name = "fork"
    summary = "Fork a session's chat into a NEW session that starts with the full history (§4)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("source", help="the session to fork (id or name)")
        parser.add_argument("new_name", nargs="?", help="name for the fork (default <source>-fork)")
        args = parser.parse_args(argv)
        new = ChatOps(self.service).fork(args.source, args.new_name)
        forked = chat.active_chat(new)
        chat_label = forked.id[:8] if forked and forked.id else "pending"
        print(f"Forked '{args.source}' → '{new.name}' (chat {chat_label}, cwd={new.cwd})")
        return 0


class HandoverCommand(Command):
    name = "handover"
    summary = "Distill a session's chat into a focused brief for a NEW worker session (§5)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("source", help="the session to hand over from (id or name)")
        parser.add_argument("task", help="the task to distill a brief for")
        parser.add_argument("new_name", nargs="?", help="name for the worker (default <source>-handover)")
        parser.add_argument("--self-catch-up", action="store_true",
                            help="skip the distiller — the worker reads the source bundle itself (CHD1)")
        args = parser.parse_args(argv)
        worker = ChatOps(self.service).handover(
            args.source, args.task, args.new_name, self_catch_up=args.self_catch_up
        )
        how = "self-catch-up" if args.self_catch_up else "distilling brief"
        print(f"Handover '{args.source}' → worker '{worker}' ({how}; launches when ready)")
        return 0


class RolloverCommand(Command):
    name = "rollover"
    summary = "Rotate a session onto a fresh chat in the SAME pane (context exhausted) (§6)."

    def run(self, argv: list[str]) -> int:
        parser = self._parser()
        parser.add_argument("session", nargs="?",
                            help="the session to roll over (default: the one you are in)")
        parser.add_argument("--self-catch-up", action="store_true",
                            help="skip the distiller — the successor reads the bundle itself (CHD1)")
        args = parser.parse_args(argv)
        new_chat = ChatOps(self.service).rollover(args.session, self_catch_up=args.self_catch_up)
        how = "self-catch-up" if args.self_catch_up else "summarizing first"
        print(f"Rollover scheduled ({how}); same session rotates onto chat {new_chat[:8]} when ready")
        return 0


class RolloverFinishCommand(Command):
    name = "_rollover-finish"
    summary = "Internal: the rollover async tail — respawn the pane onto the fresh chat (CHD5)."

    def run(self, argv: list[str]) -> int:
        """Called by the rollover distiller once its note is written (or fired detached for
        `--self-catch-up`). `note` is empty in the self-catch-up case (the successor reads the
        bundle instead). Does the `respawn-pane -k` + minimal seed + `ChatRef{role:rollover}`."""
        parser = self._parser()
        parser.add_argument("txid")
        parser.add_argument("source_chat")
        parser.add_argument("new_chat")
        parser.add_argument("pane")
        parser.add_argument("note", nargs="?", default="")
        args = parser.parse_args(argv)
        ChatOps(self.service).rollover_finish(
            args.txid, args.source_chat, args.new_chat, args.pane, args.note
        )
        return 0


class HandoverFinishCommand(Command):
    name = "_handover-finish"
    summary = "Internal: the handover async tail — spawn + seed the fresh worker (CHD5)."

    def run(self, argv: list[str]) -> int:
        """Called by the handover distiller once its brief is written. Spawns the pre-minted worker,
        records `ChatRef{role:handover}`, and seeds it minimally. `brief_path` empty / `--self-catch-up`
        means the worker reads the source bundle itself."""
        parser = self._parser()
        parser.add_argument("source_txid")
        parser.add_argument("source_chat")
        parser.add_argument("worker_name")
        parser.add_argument("worker_chat")
        parser.add_argument("brief_path", nargs="?", default="")
        parser.add_argument("--self-catch-up", action="store_true")
        args = parser.parse_args(argv)
        ChatOps(self.service).handover_finish(
            args.source_txid, args.source_chat, args.worker_name, args.worker_chat,
            brief_path=args.brief_path or None, self_catch_up=args.self_catch_up,
        )
        return 0


PUBLIC_COMMANDS: list[type[Command]] = [
    StartCommand, AttachCommand, LsCommand, SpawnCommand, SpawnNvimCommand, SpawnViewCommand,
    TagCommand, RenameCommand, SendMessageCommand, KillCommand, ArchiveCommand, RmCommand,
    ShowCommand, HistoryCommand, ChatCommand, ResumeCommand, SyncCommand,
    ForkCommand, HandoverCommand, RolloverCommand,
]
HIDDEN_COMMANDS: list[type[Command]] = [
    ListCommand, EditTagCommand, SessionClosedCommand, HookCommand, InitHomeCommand,
    SelfCheckCommand,
    RolloverFinishCommand, HandoverFinishCommand,
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
