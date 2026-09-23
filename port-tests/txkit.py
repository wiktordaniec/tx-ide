"""port-tests/txkit.py — the black-box test kit for the tx-ide Rust-port suite.

Stdlib only (D7). Tests never import `lib/tx` (D1): everything goes through the binary named by
`TX_BIN` (default `<repo>/bin/tx`) and the observable surfaces of Appendix B3 — stdout/stderr/exit
code, files under `$TX_IDE_HOME`, tmux state on a private server, fake-engine argv/env dumps, and
the git fixture.

Fixtures (each usable on its own; `TxCase` wires the standard set):

  TxHome      hermetic `$TX_IDE_HOME` + `HOME` / `CLAUDE_CONFIG_DIR` / `CODEX_HOME` in one temp tree
  TmuxServer  private `tmux -L <random>` server behind a `tmux` wrapper first on PATH (Q1)
  FakeBins    argv+env recorders for claude / codex / agy / nvim / fzf / bwrap / brew
  GitFixture  repo with one commit; `with_origin_main()`, `as_linked_worktree()`
  Records     crafted schema-6 session records and artifact-v2 records with explicit timestamps (D8)
  PtyProcess  a process on its own pty (a real `tmux attach` client, `tx attach`, the curses form)

Runner: `run_tx(...)` / `TxCase.tx(...)` execute `TX_BIN` under a scrubbed environment and return a
`Result` with ANSI-stripped and raw output. Goldens: `assert_golden(...)` compares against
`port-tests/golden/<area>/<nn>.txt`; `TX_UPDATE_GOLDEN=1` captures instead (D10 — capture once
from the Python reference, commit the files).
"""

from __future__ import annotations

import fcntl
import functools
import json
import os
import platform
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

KIT_DIR = Path(__file__).resolve().parent
REPO = KIT_DIR.parent
GOLDEN_DIR = KIT_DIR / "golden"
TX_BIN = str(Path(os.environ.get("TX_BIN", str(REPO / "bin" / "tx"))).expanduser().resolve())
REAL_TMUX = shutil.which("tmux")

# What `ensure_home` creates (T-HOME-04). `agents` and `hooks/` are the installer's, mirrored below.
HOME_DIRS = ("sessions", "history", "worktrees", "user-agents", "artifacts", "launch")

# Shim basename → `tx hook <event>` (setup/engines/claude.sh + codex.sh). Claude shims drain stdin
# except the capture/notify ones; every codex shim keeps stdin connected.
CLAUDE_HOOK_EVENTS = {
    "start": "session-start",
    "pre": "prompt-submit",
    "work": "working",
    "post": "stop",
    "notify": "notification",
    "end": "session-end",
}
CLAUDE_KEEP_STDIN = frozenset({"start", "pre", "notify"})
CODEX_HOOK_EVENTS = {
    "start": "session-start",
    "pre": "prompt-submit",
    "work": "working",
    "post": "stop",
}

FAKE_NAMES = ("claude", "codex", "agy", "nvim", "fzf", "bwrap", "brew", "zsh")
# `REPO/bin/*` helpers (`tmux-*`, `tx-assistant`, …) exposed on PATH beside a `tx` → TX_BIN link, so an
# in-pane `tx`, a pane-border `tmux-pane-session-name` and the tmux fragment's helpers resolve.
HELPER_BINS = tuple(sorted(path.name for path in (REPO / "bin").iterdir() if path.name != "tx"))
# Long-running fakes stay alive so their tmux session (and the record's liveness) persists until
# teardown; the short ones return at once.
FAKE_DEFAULT_SLEEP = {"claude": 600, "codex": 600, "agy": 600, "nvim": 600, "zsh": 600}

SCRUB_PREFIXES = ("TX_", "TXKIT_", "FZF_")
SCRUB_KEYS = ("TMUX", "TMUX_PANE", "NAMEW", "TERM_PROGRAM", "PYTHONPATH")

# The env var the PATH `tmux` wrapper keys on; without it the wrapper is a transparent pass-through.
TMUX_SOCKET_ENV = "TXKIT_TMUX_SOCKET"


class KitSafetyError(RuntimeError):
    """`tx` was about to run outside the kit's temp root or without the private tmux socket."""

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

SESSION_RECORD_CMD = "claude --model opus --effort high --dangerously-skip-permissions"


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def wait_until(predicate: Callable[[], object], timeout: float = 10.0, interval: float = 0.05, what: str = ""):
    """Poll `predicate` until truthy and return its value; `AssertionError` after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s{': ' + what if what else ''}")
        time.sleep(interval)


def munge(path: str) -> str:
    """Claude's project-dir name for a cwd: `/` and `.` become `-` (of the realpath)."""
    return os.path.realpath(path).replace("/", "-").replace(".", "-")


# ----- TxHome ----------------------------------------------------------------------------------


class TxHome:
    """A hermetic `$TX_IDE_HOME` (H1) plus the engine homes, all under one temp `root`.

    Layout: `<root>/home` is `$TX_IDE_HOME`; `<root>/user-home` is `HOME`; `<root>/claude-config`
    is `CLAUDE_CONFIG_DIR`; `<root>/codex` is `CODEX_HOME`. `skeleton=False` leaves the home dir
    absent (T-HOME-04); `link_agents=False` skips the `agents → <repo>/agents` symlink (Fixture R
    creates a plain dir instead); `hooks` lists the engines whose shims are written.
    """

    def __init__(
        self,
        root: Path,
        *,
        skeleton: bool = True,
        link_agents: bool = True,
        hooks: tuple[str, ...] = ("claude", "codex"),
    ):
        self.root = root
        self.path = root / "home"
        self.user_home = root / "user-home"
        self.claude_config_dir = root / "claude-config"
        self.codex_home = root / "codex"
        for directory in (self.user_home, self.claude_config_dir, self.codex_home):
            directory.mkdir(parents=True, exist_ok=True)
        if skeleton:
            for name in HOME_DIRS:
                (self.path / name).mkdir(parents=True, exist_ok=True)
        if link_agents:
            self.path.mkdir(parents=True, exist_ok=True)
            self.agents.symlink_to(REPO / "agents")
        for engine in hooks:
            self.write_hook_shims(engine)

    # paths
    @property
    def sessions_dir(self) -> Path:
        return self.path / "sessions"

    @property
    def artifacts_dir(self) -> Path:
        return self.path / "artifacts"

    @property
    def history_dir(self) -> Path:
        return self.path / "history"

    @property
    def worktrees_dir(self) -> Path:
        return self.path / "worktrees"

    @property
    def user_agents_dir(self) -> Path:
        return self.path / "user-agents"

    @property
    def launch_dir(self) -> Path:
        return self.path / "launch"

    @property
    def chat_ops_dir(self) -> Path:
        return self.path / "chat-ops"

    @property
    def hooks_dir(self) -> Path:
        return self.path / "hooks"

    @property
    def agents(self) -> Path:
        return self.path / "agents"

    @property
    def log_path(self) -> Path:
        return self.path / "log.jsonl"

    @property
    def config_path(self) -> Path:
        return self.path / "config.json"

    def entries(self) -> list[str]:
        """`ls -A $TX_IDE_HOME`, sorted."""
        return sorted(entry.name for entry in self.path.iterdir())

    def write_hook_shims(self, engine: str) -> None:
        """Write the C9-baked shims exactly as the installer would, but exec'ing `TX_BIN`."""
        directory = self.hooks_dir / engine
        directory.mkdir(parents=True, exist_ok=True)
        if engine == "claude":
            for basename, event in CLAUDE_HOOK_EVENTS.items():
                drain = (
                    "# stdin left connected — tx hook reads the JSON payload"
                    if basename in CLAUDE_KEEP_STDIN
                    else "cat >/dev/null"
                )
                self._write_shim(directory / f"{basename}.sh", drain, f"hook {event}")
            return
        if engine == "codex":
            for basename, event in CODEX_HOOK_EVENTS.items():
                self._write_shim(
                    directory / f"{basename}.sh",
                    "# stdin left connected — tx hook reads the JSON payload",
                    f"hook {event} --engine codex",
                )
            return
        raise ValueError(f"no shim recipe for engine {engine!r}")

    def _write_shim(self, path: Path, drain: str, verb: str) -> None:
        path.write_text(
            "#!/bin/bash\n"
            "# tx-ide hook shim — GENERATED by port-tests/txkit.py (mirrors setup/engines/*.sh).\n"
            f"{drain}\n"
            f'exec env TX_IDE_HOME="{self.path}" "{TX_BIN}" {verb}\n'
        )
        path.chmod(0o755)

    def write_config(self, data: dict | str) -> None:
        """`config.json`: a dict is dumped as JSON; a str is written verbatim (malformed cases)."""
        self.config_path.write_text(data if isinstance(data, str) else json.dumps(data))

    def claude_transcript_path(self, cwd: str, chat_id: str) -> Path:
        """`$CLAUDE_CONFIG_DIR/projects/<munge(realpath cwd)>/<chat>.jsonl`."""
        return self.claude_config_dir / "projects" / munge(cwd) / f"{chat_id}.jsonl"

    def env(self) -> dict[str, str]:
        return {
            "TX_IDE_HOME": str(self.path),
            "HOME": str(self.user_home),
            "CLAUDE_CONFIG_DIR": str(self.claude_config_dir),
            "CODEX_HOME": str(self.codex_home),
            "SHELL": "/bin/bash",
        }


# ----- TmuxServer ------------------------------------------------------------------------------


class TmuxServer:
    """A private tmux server (H2). The Python `Tmux` adapter takes only a binary name, so the
    private socket is injected by a `tmux` wrapper script placed first on PATH (Q1); direct
    inspection from the test goes through the real binary with the same `-L`. Both pass
    `-f /dev/null` so the server starts stock (no operator / system tmux.conf) whichever call
    starts it.

    `env` is the environment every direct call runs under. The FIRST call that reaches the socket
    starts the server, and the server keeps that process's environment as its global environment
    — inherited by every pane, popup and hook it later launches. `TxCase` sets it to the scrubbed
    env (temp `HOME` / `TX_IDE_HOME`, the wrapper first on PATH), so a raw `new_session` can never
    seed the private server with the operator's home: a `tx` launched from one of its panes would
    otherwise read the REAL `~/.tx-ide` against the private socket and reconcile every live record
    to exited."""

    def __init__(self, root: Path, env: dict[str, str] | None = None):
        self.env = env
        self.socket = f"txkit-{uuid.uuid4().hex[:8]}"
        self.bin_dir = root / "tmux-bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        # Safety: `-L` is injected ONLY when TXKIT_TMUX_SOCKET is set (scrubbed_env sets it). A
        # leaked PATH in an ordinary shell then execs the real tmux untouched instead of pointing
        # reconcile at an empty private server.
        wrapper = self.bin_dir / "tmux"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'[ -n "$TXKIT_TMUX_SOCKET" ] && exec "{REAL_TMUX}" -L "$TXKIT_TMUX_SOCKET" -f /dev/null "$@"\n'
            f'exec "{REAL_TMUX}" "$@"\n'
        )
        wrapper.chmod(0o755)

    def start(self) -> None:
        """Boot the private server config-free and keep it alive with no sessions (`exit-empty
        off`), so every later call — raw or via `tx` — joins a server whose global environment is
        `env`. Call it AFTER `env` is set: the booting process's environment becomes the server's
        global environment (inherited by every pane, popup and hook). A throwaway session carries
        the boot because `start-server` does not honour `-f` on tmux 3.4."""
        self.run("new-session", "-d", "-s", "__boot", "sleep 5", check=True)
        self.run("set-option", "-g", "exit-empty", "off", check=True)
        self.run("kill-session", "-t", "__boot", check=True)

    def run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            [REAL_TMUX, "-L", self.socket, "-f", "/dev/null", *args],
            capture_output=True,
            text=True,
            check=check,
            env=self.env,
        )

    def sessions(self) -> list[str]:
        result = self.run("list-sessions", "-F", "#{session_name}")
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line]

    def has_session(self, name: str) -> bool:
        return self.run("has-session", "-t", f"={name}").returncode == 0

    def option(self, target: str, name: str, scope: str = "session") -> str | None:
        """`show-options -v` for a session / window / pane / global / global-window option; None
        when unset."""
        flag = {"session": [], "window": ["-w"], "pane": ["-p"], "global": ["-g"],
                "global-window": ["-gw"]}[scope]
        result = self.run("show-options", *flag, "-t", target, "-v", name)
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\n")

    def display(self, target: str, format_string: str) -> str:
        return self.run("display-message", "-p", "-t", target, format_string, check=True).stdout.rstrip("\n")

    def environment(self, target: str) -> dict[str, str]:
        """`show-environment -t target` as a dict (unset markers `-KEY` skipped)."""
        result = self.run("show-environment", "-t", target, check=True)
        pairs = {}
        for line in result.stdout.splitlines():
            if line.startswith("-") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            pairs[key] = value
        return pairs

    def capture(self, target: str) -> str:
        return self.run("capture-pane", "-p", "-t", target, check=True).stdout

    def new_session(
        self,
        name: str,
        cmd: str,
        tx_id: str | None = None,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """A raw live session (the RECON/RENDER "live record" recipe): `new-session -d -s name cmd`
        then `set-option @tx_id` when `tx_id` is given."""
        args = ["new-session", "-d", "-s", name]
        if cwd is not None:
            args += ["-c", cwd]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args.append(cmd)
        self.run(*args, check=True)
        if tx_id is not None:
            self.run("set-option", "-t", name, "@tx_id", tx_id, check=True)

    def kill_session(self, name: str) -> None:
        self.run("kill-session", "-t", f"={name}")

    def send_keys(self, target: str, *keys: str, literal: bool = False) -> None:
        """`send-keys -t target [-l] -- keys…` (raw driving of a pane; `literal` types verbatim)."""
        flags = ["-l"] if literal else []
        self.run("send-keys", "-t", target, *flags, "--", *keys, check=True)

    def type_line(self, target: str, line: str) -> None:
        """Type `line` verbatim into a pane's shell, then press Enter."""
        self.send_keys(target, line, literal=True)
        self.send_keys(target, "Enter")

    def clients(self) -> list[dict[str, str]]:
        """`list-clients` rows: `client_tty`, `client_session`, `client_activity`, `client_name`."""
        result = self.run("list-clients", "-F", "#{client_tty}\t#{client_session}\t#{client_activity}\t#{client_name}")
        rows = []
        for line in result.stdout.splitlines():
            tty, session, activity, name = line.split("\t")
            rows.append({"client_tty": tty, "client_session": session, "client_activity": activity, "client_name": name})
        return rows

    def panes(self, target: str | None = None) -> list[dict[str, str]]:
        """`list-panes` rows (`-a` server-wide, or `-s -t target` for one session): `pane_id`, `pane_tty`,
        `session_name`, `window_index`, `window_name`, `pane_index`, `pane_current_command`, `pane_pid`."""
        scope = ["-a"] if target is None else ["-s", "-t", target]
        fields = ("pane_id", "pane_tty", "session_name", "window_index", "window_name", "pane_index",
                  "pane_current_command", "pane_pid")
        result = self.run("list-panes", *scope, "-F", "\t".join("#{" + field + "}" for field in fields))
        return [dict(zip(fields, line.split("\t"))) for line in result.stdout.splitlines() if line]

    def pane_id(self, target: str) -> str:
        """The `%n` id of a pane target (e.g. `Views:0.0` or a session name = its active pane)."""
        return self.display(target, "#{pane_id}")

    def pane_tty(self, pane_id: str) -> str:
        return self.display(pane_id, "#{pane_tty}")

    def split_window(self, target: str, *flags: str) -> str:
        """`split-window [flags] -t target /bin/bash` → the new pane's id. Runs an explicit
        non-login bash: a command-less split starts a LOGIN shell whose /etc/profile resets PATH
        and drops the wrapper / helper dirs."""
        return self.run("split-window", *flags, "-t", target, "-P", "-F", "#{pane_id}", "/bin/bash",
                        check=True).stdout.strip()

    def new_window(self, target: str, *flags: str) -> str:
        """`new-window [flags] -t target /bin/bash` → the new window's pane id (same reason)."""
        return self.run("new-window", *flags, "-t", target, "-P", "-F", "#{pane_id}", "/bin/bash",
                        check=True).stdout.strip()

    def attach_client(self, session: str, *, env: dict[str, str], rows: int = 50, cols: int = 200,
                      timeout: float = 10.0) -> PtyProcess:
        """A real outer client: `tmux attach -t session` on a fresh pty (H2), returned once
        `list-clients` shows it. `TMUX` is dropped from `env`; the caller closes it."""
        client_env = {key: value for key, value in env.items() if key != "TMUX"}
        client_env.setdefault("TERM", "xterm-256color")
        client = PtyProcess([REAL_TMUX, "-L", self.socket, "-f", "/dev/null", "attach", "-t", session],
                            env=client_env, rows=rows, cols=cols)
        wait_until(lambda: any(row["client_tty"] == client.tty for row in self.clients()), timeout,
                   what=f"client on {client.tty} attached to {session}")
        return client

    def nest_attach(self, pane_id: str, session: str, timeout: float = 10.0) -> None:
        """Nest `session` inside a shell pane the way `tx attach` does (`TMUX= tmux attach -t …`
        typed into the pane), then wait until a client's `client_tty` == that pane's `pane_tty`."""
        tty = self.pane_tty(pane_id)
        self.type_line(pane_id, f"TMUX= tmux attach -t {shlex.quote(session)}")
        wait_until(lambda: any(row["client_tty"] == tty and row["client_session"] == session for row in self.clients()),
                   timeout, what=f"{session} nested in {pane_id}")

    def close(self) -> None:
        """Kill the private server and drop its socket file (tmux leaves it behind)."""
        self.run("kill-server")
        socket_dir = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
        (socket_dir / self.socket).unlink(missing_ok=True)


# ----- PtyProcess ------------------------------------------------------------------------------


class PtyProcess:
    """A child on its own pty (H2): a real `tmux attach` client, a `tx attach` picker outside tmux,
    or the curses form. `write` feeds keystrokes; `read`/`expect` drain what the child wrote; `tty`
    is the slave path (what tmux reports as `client_tty`). Close it (or `wait`) when done."""

    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: str | Path | None = None,
                 rows: int = 24, cols: int = 80):
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.tty = os.ttyname(slave)
        self.output = ""
        self.process = subprocess.Popen(
            argv,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            pass_fds=(slave,),
            preexec_fn=lambda: os.login_tty(slave),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(slave)

    @property
    def pid(self) -> int:
        return self.process.pid

    def write(self, text: str) -> None:
        os.write(self.master, text.encode())

    def read(self, timeout: float = 0.5) -> str:
        """Drain whatever the child wrote within `timeout` seconds; returns the new text (also
        appended to `output`)."""
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([self.master], [], [], remaining)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", "replace")
        self.output += text
        return text

    def expect(self, text: str, timeout: float = 10.0) -> str:
        """Read until `text` appears in the accumulated (ANSI-stripped) output; returns that output."""
        deadline = time.monotonic() + timeout
        while text not in strip_ansi(self.output):
            if time.monotonic() > deadline:
                raise AssertionError(f"{text!r} not seen on the pty within {timeout}s; got {strip_ansi(self.output)!r}")
            self.read(0.2)
        return strip_ansi(self.output)

    def wait(self, timeout: float = 10.0) -> int:
        """Drain output until the child exits; returns its exit code (fails after `timeout`)."""
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"pty process {self.process.args} still running after {timeout}s")
            self.read(0.1)
        self.read(0.1)
        return self.process.returncode

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        try:
            os.close(self.master)
        except OSError:
            pass


# ----- FakeBins --------------------------------------------------------------------------------


FAKE_TEMPLATE = r'''#!__PYTHON__
"""Fake `__NAME__` — GENERATED by port-tests/txkit.py (H3). Dumps argv + env as JSON, then acts
per the knobs file written by FakeBins.configure()."""
import json, os, sys, time

NAME = "__NAME__"
KNOBS = "__KNOBS__"
DEFAULT_OUT = "__OUT__"
DEFAULT_SLEEP = __SLEEP__

knobs = json.load(open(KNOBS)) if os.path.exists(KNOBS) else {}
if knobs.get("sequence"):
    # Per-invocation knobs: the n-th run takes sequence[n] (the last entry repeats).
    counter = KNOBS + ".count"
    run_index = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as handle:
        handle.write(str(run_index + 1))
    knobs = {**knobs, **knobs["sequence"][min(run_index, len(knobs["sequence"]) - 1)]}
out_dir = os.environ.get("FAKE_OUT", DEFAULT_OUT)
os.makedirs(out_dir, exist_ok=True)
key = os.environ.get("TX_SESSION_ID") or str(os.getpid())

stdin_text = None
if knobs.get("read_stdin", NAME == "fzf") and not sys.stdin.isatty():
    stdin_text = sys.stdin.read()

dump = {
    "name": NAME,
    "argv": sys.argv,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "pid": os.getpid(),
    "stdin": stdin_text,
    "at": time.time(),
}
final = os.path.join(out_dir, f"{NAME}-{key}.json")
staging = final + ".tmp"
with open(staging, "w") as handle:
    json.dump(dump, handle)
os.replace(staging, final)

if knobs.get("transcript"):
    os.makedirs(os.path.dirname(knobs["transcript"]), exist_ok=True)
    with open(knobs["transcript"], "w") as handle:
        handle.write(knobs.get("transcript_text", json.dumps({"type": "user", "sessionId": key}) + "\n"))
if knobs.get("prompt_glyph"):
    sys.stdout.write("❯ ")
    sys.stdout.flush()
if knobs.get("stdout"):
    sys.stdout.write(knobs["stdout"])
    sys.stdout.flush()

if NAME == "bwrap" and knobs.get("passthrough", True) and "--" in sys.argv:
    inner = sys.argv[sys.argv.index("--") + 1:]
    if inner:
        os.execvp(inner[0], inner)

time.sleep(knobs.get("sleep", DEFAULT_SLEEP))
sys.exit(knobs.get("exit_code", 0))
'''


class FakeBins:
    """A PATH dir of argv+env recorders (H3). Each run writes
    `$FAKE_OUT/<basename>-<TX_SESSION_ID or pid>.json` with `argv`, `env`, `cwd`, `pid`, `stdin`.

    Knobs (per basename, set before the run): `sleep` seconds, `exit_code`, `transcript` (write a
    fake transcript at this path; `transcript_text` for its body), `prompt_glyph` (echo `❯ ` to
    stdout), `stdout` (extra text), `read_stdin` (default only for `fzf`), `passthrough` (bwrap:
    exec the command after `--`, default on), `sequence` (a list of per-invocation knob dicts —
    the n-th run merges sequence[n], the last entry repeats; e.g. fzf printing a row once, then 130).

    `helpers_dir` (after the fakes on PATH) links `tx` → `TX_BIN` and every `REPO/bin/*` helper, so
    an in-pane `tx`, the pane-border `tmux-pane-session-name`, `tx-assistant` etc. resolve.
    `add(name)` writes a recorder for a new basename (e.g. `ssh`); `write_script(name, body)`
    installs a hand-written executable instead.
    """

    def __init__(self, root: Path, names: tuple[str, ...] = FAKE_NAMES):
        self.bin_dir = root / "fake-bin"
        self.out_dir = root / "fake-out"
        self.knobs_dir = root / "fake-knobs"
        for directory in (self.bin_dir, self.out_dir, self.knobs_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.names = names
        for name in names:
            self._write(name)
        self.helpers_dir = root / "helper-bin"
        self.helpers_dir.mkdir(parents=True, exist_ok=True)
        (self.helpers_dir / "tx").symlink_to(TX_BIN)
        for helper in HELPER_BINS:
            (self.helpers_dir / helper).symlink_to(REPO / "bin" / helper)

    def add(self, name: str) -> None:
        """Install the standard recorder under a new basename (short-lived unless `sleep` is set)."""
        self._write(name)

    def write_script(self, name: str, body: str) -> Path:
        """Install a hand-written executable (`#!` line included) as `name` on the fakes PATH."""
        path = self.bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def _write(self, name: str) -> None:
        script = (
            FAKE_TEMPLATE.replace("__PYTHON__", sys.executable)
            .replace("__NAME__", name)
            .replace("__KNOBS__", str(self.knobs_dir / f"{name}.json"))
            .replace("__OUT__", str(self.out_dir))
            .replace("__SLEEP__", str(FAKE_DEFAULT_SLEEP.get(name, 0)))
        )
        path = self.bin_dir / name
        path.write_text(script)
        path.chmod(0o755)

    def configure(self, name: str, **knobs) -> None:
        """Set knobs for `name` (replaces earlier knobs for that basename; a `sequence` restarts
        at its first entry)."""
        (self.knobs_dir / f"{name}.json").write_text(json.dumps(knobs))
        (self.knobs_dir / f"{name}.json.count").unlink(missing_ok=True)

    def remove(self, name: str) -> None:
        """Take a fake off PATH (the "engine CLI absent" cases)."""
        (self.bin_dir / name).unlink()

    def dumps(self, name: str) -> list[dict]:
        """Every dump for `name`, oldest first."""
        paths = sorted(self.out_dir.glob(f"{name}-*.json"), key=lambda path: path.stat().st_mtime)
        return [json.loads(path.read_text()) for path in paths]

    def dump_path(self, name: str, key: str) -> Path:
        return self.out_dir / f"{name}-{key}.json"

    def wait_dump(self, name: str, key: str | None = None, timeout: float = 10.0) -> dict:
        """Block until a dump for `name` (and `key` = TX_SESSION_ID or pid, if given) appears."""
        deadline = time.monotonic() + timeout
        while True:
            if key is None:
                found = self.dumps(name)
                if found:
                    return found[-1]
            elif self.dump_path(name, key).exists():
                return json.loads(self.dump_path(name, key).read_text())
            if time.monotonic() > deadline:
                raise AssertionError(f"no {name} dump for {key or 'any key'} within {timeout}s")
            time.sleep(0.05)

    def env(self) -> dict[str, str]:
        return {"FAKE_OUT": str(self.out_dir)}


# ----- GitFixture ------------------------------------------------------------------------------


class GitFixture:
    """A git repo with one commit on `main` (H4)."""

    def __init__(self, root: Path, name: str = "repo"):
        self.root = root
        self.name = name
        self.path = root / name
        self.path.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        (self.path / "README.md").write_text("fixture\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "initial")

    def git(self, *args: str, cwd: Path | None = None) -> str:
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "txkit",
            "GIT_AUTHOR_EMAIL": "txkit@example.invalid",
            "GIT_COMMITTER_NAME": "txkit",
            "GIT_COMMITTER_EMAIL": "txkit@example.invalid",
        }
        result = subprocess.run(
            ["git", "-C", str(cwd or self.path), *args],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        return result.stdout

    def with_origin_main(self) -> GitFixture:
        """Add a bare `origin` with `main` pushed, so `origin/main` exists."""
        bare = self.root / f"{self.name}-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare))
        self.git("push", "-q", "-u", "origin", "main")
        return self

    def as_linked_worktree(self, branch: str = "linked") -> Path:
        """A linked worktree of this repo (the "cwd is itself a linked worktree" variant)."""
        linked = self.root / f"{self.name}-{branch}"
        self.git("worktree", "add", "-q", "-b", branch, str(linked))
        return linked

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def worktrees(self) -> list[str]:
        """Worktree paths from `git worktree list --porcelain` (main checkout first)."""
        return [
            line.split(" ", 1)[1]
            for line in self.git("worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")
        ]


# ----- Records ---------------------------------------------------------------------------------


class Records:
    """Crafted on-disk records — the only way RECON/RENDER ages are produced (D8): pass explicit
    past `created_at` / `last_activity` / `turn_started_at` values."""

    def __init__(self, home: TxHome):
        self.home = home

    def path(self, session_id: str) -> Path:
        return self.home.sessions_dir / f"{session_id}.json"

    def load(self, session_id: str) -> dict:
        return json.loads(self.path(session_id).read_text())

    def write(self, record: dict) -> Path:
        """Write a session record in the store's own format (indent 2, no trailing newline)."""
        self.home.sessions_dir.mkdir(parents=True, exist_ok=True)
        path = self.path(record["id"])
        path.write_text(json.dumps(record, indent=2))
        return path

    def chat_ref(
        self,
        *,
        session_id: str,
        id: str | None = None,
        role: str = "original",
        cwd: str = "",
        transcript_path: str = "",
        how: str = "spawn",
        chat_id: str | None = None,
        bundle_path: str | None = None,
        started_at: float | None = None,
        ended_at: float | None = None,
        summary: str = "",
        engine: str | None = "claude",
    ) -> dict:
        return {
            "id": id,
            "role": role,
            "cwd": cwd,
            "transcript_path": transcript_path,
            "origin": {"how": how, "session_id": session_id, "chat_id": chat_id},
            "bundle_path": bundle_path,
            "started_at": started_at,
            "ended_at": ended_at,
            "summary": summary,
            "engine": engine,
        }

    def llm(
        self,
        *,
        id: str | None = None,
        name: str = "worker",
        state: str = "idle",
        cwd: str = "",
        cmd: str = SESSION_RECORD_CMD,
        tags: tuple[str, ...] = ("t",),
        group: str | None = None,
        env: dict[str, str] | None = None,
        parent: str | None = None,
        pid: int | None = None,
        attached_to: tuple[dict, ...] = (),
        created_at: float | None = None,
        ended_at: float | None = None,
        engine: str = "claude",
        last_activity: float | None = None,
        chats: list[dict] | None = None,
        turn_started_at: float | None = None,
    ) -> str:
        """Write a schema-6 llm record; returns its id. `chats` defaults to one pending original
        ChatRef (what a plain spawn leaves behind)."""
        session_id = id or str(uuid.uuid4())
        created_at = time.time() if created_at is None else created_at
        cwd = cwd or str(self.home.root)
        if chats is None:
            chats = [
                self.chat_ref(
                    session_id=session_id, cwd=cwd, started_at=created_at, engine=engine
                )
            ]
        self.write(
            {
                "schema_version": 6,
                "id": session_id,
                "name": name,
                "role": "llm",
                "state": state,
                "cwd": cwd,
                "cmd": cmd,
                "tags": list(tags),
                "group": group,
                "env": dict(env or {}),
                "parent": parent,
                "pid": pid,
                "attached_to": list(attached_to),
                "created_at": created_at,
                "ended_at": ended_at,
                "engine": engine,
                "last_activity": last_activity,
                "chats": chats,
                "turn_started_at": turn_started_at,
            }
        )
        return session_id

    def other(
        self,
        *,
        id: str | None = None,
        name: str = "shell",
        role: str = "shell",
        state: str = "alive",
        cwd: str = "",
        cmd: str = "bash",
        tags: tuple[str, ...] = ("t",),
        group: str | None = None,
        env: dict[str, str] | None = None,
        parent: str | None = None,
        pid: int | None = None,
        attached_to: tuple[dict, ...] = (),
        created_at: float | None = None,
        ended_at: float | None = None,
        artifact_id: str | None = None,
    ) -> str:
        """Write a schema-6 non-llm record (shell / nvim / other); returns its id."""
        session_id = id or str(uuid.uuid4())
        self.write(
            {
                "schema_version": 6,
                "id": session_id,
                "name": name,
                "role": role,
                "state": state,
                "cwd": cwd or str(self.home.root),
                "cmd": cmd,
                "tags": list(tags),
                "group": group,
                "env": dict(env or {}),
                "parent": parent,
                "pid": pid,
                "attached_to": list(attached_to),
                "created_at": time.time() if created_at is None else created_at,
                "ended_at": ended_at,
                "artifact_id": artifact_id,
            }
        )
        return session_id

    def artifact_record_path(self, artifact_id: str) -> Path:
        return self.home.artifacts_dir / f"{artifact_id}.json"

    def artifact_dir(self, artifact_id: str) -> Path:
        return self.home.artifacts_dir / artifact_id

    def artifact(
        self,
        *,
        id: str | None = None,
        title: str | None = "Plan",
        filename: str = "plan.md",
        created_at: float | None = None,
        group: str | None = None,
        history: list[dict] | None = None,
        revs: dict[int, str] | None = None,
        current: str | None = None,
    ) -> str:
        """Write an artifact-v2 record plus `<id>/revs/<n>.<ext>` and `<id>/current.<ext>`.
        `history` defaults to the single create touch; `revs` to `{0: "body\\n"}`; `current` to
        the highest rev's content."""
        artifact_id = id or str(uuid.uuid4())
        created_at = time.time() if created_at is None else created_at
        if history is None:
            history = [{"session_id": "s1", "at": created_at, "rev": 0, "changes": None}]
        if revs is None:
            revs = {0: "body\n"}
        extension = Path(filename).suffix
        directory = self.artifact_dir(artifact_id)
        (directory / "revs").mkdir(parents=True, exist_ok=True)
        for rev, content in revs.items():
            (directory / "revs" / f"{rev}{extension}").write_text(content)
        if current is None:
            current = revs[max(revs)]
        (directory / f"current{extension}").write_text(current)
        self.home.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_record_path(artifact_id).write_text(
            json.dumps(
                {
                    "artifact_schema_version": 2,
                    "id": artifact_id,
                    "title": title,
                    "filename": filename,
                    "created_at": created_at,
                    "group": group,
                    "history": history,
                },
                indent=2,
            )
        )
        return artifact_id


# ----- running tx ------------------------------------------------------------------------------


@dataclass
class Result:
    code: int
    out: str  # ANSI-stripped stdout
    err: str  # ANSI-stripped stderr
    raw_out: str
    raw_err: str

    @property
    def lines(self) -> list[str]:
        return self.out.splitlines()


def scrubbed_env(
    home: TxHome,
    tmux: TmuxServer | None = None,
    fakes: FakeBins | None = None,
    extra: dict[str, str | None] | None = None,
) -> dict[str, str]:
    """The environment `tx` runs under: inherited env minus `TX_*`, `TMUX`, `TMUX_PANE`, `NAMEW`,
    `FZF_*`; plus the home's vars, `FAKE_OUT`, and PATH with the tmux wrapper, the fakes, then the
    `tx` + `bin/` helper links first.
    `extra` overrides; a `None` value unsets."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(SCRUB_PREFIXES) and key not in SCRUB_KEYS
    }
    env.update(home.env())
    path_dirs = []
    if tmux is not None:
        path_dirs.append(str(tmux.bin_dir))
        env[TMUX_SOCKET_ENV] = tmux.socket
    if fakes is not None:
        path_dirs.append(str(fakes.bin_dir))
        path_dirs.append(str(fakes.helpers_dir))
        env.update(fakes.env())
    env["PATH"] = os.pathsep.join([*path_dirs, os.environ.get("PATH", "")])
    for key, value in (extra or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    _check_safe(home, env)
    return env


def _check_safe(home: TxHome, env: dict[str, str]) -> None:
    """Refuse to let `TX_BIN` run against anything but a kit temp home on a private tmux socket.
    Checked on the FINAL env (after `extra` overrides)."""
    temp_root = Path(tempfile.gettempdir()).resolve()
    kit_root = home.root.resolve()
    target = Path(env.get("TX_IDE_HOME", "")).expanduser().resolve()
    if not kit_root.is_relative_to(temp_root) or not target.is_relative_to(kit_root):
        raise KitSafetyError(
            f"refusing to run tx: TX_IDE_HOME={target} is not under the kit temp root {kit_root}"
        )
    if not env.get(TMUX_SOCKET_ENV):
        raise KitSafetyError(
            f"refusing to run tx: {TMUX_SOCKET_ENV} is unset — pass a TmuxServer so tmux is private"
        )


def run_tx(
    argv: list[str],
    *,
    home: TxHome,
    tmux: TmuxServer | None = None,
    fakes: FakeBins | None = None,
    env: dict[str, str | None] | None = None,
    stdin: str | None = None,
    cwd: str | Path | None = None,
    timeout: float = 60.0,
) -> Result:
    """Run `TX_BIN argv` under `scrubbed_env`. `stdin=None` feeds an empty stdin (EOF)."""
    completed = subprocess.run(
        [TX_BIN, *argv],
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        env=scrubbed_env(home, tmux, fakes, env),
        cwd=str(cwd) if cwd is not None else None,
        timeout=timeout,
    )
    return Result(
        code=completed.returncode,
        out=strip_ansi(completed.stdout),
        err=strip_ansi(completed.stderr),
        raw_out=completed.stdout,
        raw_err=completed.stderr,
    )


def log_lines(home: TxHome) -> list[dict]:
    """Parsed `log.jsonl` (empty when absent)."""
    if not home.log_path.exists():
        return []
    return [json.loads(line) for line in home.log_path.read_text().splitlines() if line]


def log_tail(home: TxHome, count: int = 1) -> list[dict]:
    return log_lines(home)[-count:] if count else []


# ----- goldens ---------------------------------------------------------------------------------

UPDATE_GOLDEN = os.environ.get("TX_UPDATE_GOLDEN") == "1"


def golden_path(name: str) -> Path:
    """`port-tests/golden/<area>/<nn>.txt` for `name` = `"<area>/<nn>"`."""
    return GOLDEN_DIR / f"{name}.txt"


def golden(name: str) -> str | None:
    """The stored golden text, or None when it has not been captured yet."""
    path = golden_path(name)
    if not path.exists():
        return None
    return path.read_text()


def assert_golden(case: unittest.TestCase, name: str, actual: str) -> None:
    """Compare `actual` to the golden; with `TX_UPDATE_GOLDEN=1` write it instead. A missing golden
    skips (H6: the reference may be absent), never fails."""
    path = golden_path(name)
    if UPDATE_GOLDEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual)
        return
    expected = golden(name)
    if expected is None:
        case.skipTest(f"golden {name} not captured — run once with TX_UPDATE_GOLDEN=1 against the Python tx")
    case.assertEqual(expected, actual, f"golden {name} differs")


# ----- markers ---------------------------------------------------------------------------------


@functools.cache
def is_python_reference() -> bool:
    """Whether `TX_BIN` is the Python reference. `TX_IMPL=python|rust` decides explicitly; otherwise
    the shim is recognised by its `python3.14 -m tx` line (`bin/tx`)."""
    implementation = os.environ.get("TX_IMPL")
    if implementation in ("python", "rust"):
        return implementation == "python"
    if implementation:
        raise ValueError(f"TX_IMPL must be 'python' or 'rust', got {implementation!r}")
    try:
        text = Path(TX_BIN).read_text(errors="ignore")
    except OSError:
        return False
    return "-m tx" in text


def expected_failure_on_python(test: Callable) -> Callable:
    """D9: the case asserts the FIXED behaviour of a quirk; skipped against the Python reference."""
    return unittest.skipIf(
        is_python_reference(), "asserts the fixed behaviour of a quirk (D9); Python reference keeps the quirk"
    )(test)


@functools.cache
def tmux_version() -> tuple[int, int] | None:
    if REAL_TMUX is None:
        return None
    output = subprocess.run([REAL_TMUX, "-V"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)", output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def requires_tmux(min: str | None = None) -> Callable:
    """Skip without tmux, or below `min` (`"3.6"`)."""
    version = tmux_version()
    if version is None:
        return unittest.skip("tmux not installed")
    if min is not None:
        floor = tuple(int(part) for part in min.split("."))
        if version < floor:
            return unittest.skip(f"tmux {version[0]}.{version[1]} < {min}")
    return lambda test: test


def requires_bin(name: str) -> Callable:
    return unittest.skipIf(shutil.which(name) is None, f"{name} not on PATH")


def platform_only(name: str) -> Callable:
    """`"linux"` or `"darwin"`."""
    return unittest.skipIf(platform.system().lower() != name, f"{name}-only case")


# ----- TxCase ----------------------------------------------------------------------------------


class TxCase(unittest.TestCase):
    """The standard fixture set: temp root, private tmux (always — so `tx` can never reach the
    operator's live server), fakes, home, records. Override `home_options` on a subclass to change
    how the home is built (e.g. `{"hooks": ()}` for T-SPAWN-13)."""

    home_options: dict = {}

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="txkit-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tmux = TmuxServer(self.root)
        self.addCleanup(self.tmux.close)
        self.fakes = FakeBins(self.root)
        self.home = TxHome(self.root, **self.home_options)
        self.records = Records(self.home)
        # Every direct tmux call carries the hermetic env, and the server is booted here, after the
        # env exists, so its global environment is the temp home's — see TmuxServer.start.
        self.tmux.env = self.env()
        self.tmux.start()
        self._git: GitFixture | None = None

    @property
    def git(self) -> GitFixture:
        """A lazily created `GitFixture` under the temp root."""
        if self._git is None:
            self._git = GitFixture(self.root)
        return self._git

    def tx(
        self,
        argv: list[str],
        *,
        env: dict[str, str | None] | None = None,
        stdin: str | None = None,
        cwd: str | Path | None = None,
    ) -> Result:
        return run_tx(
            argv,
            home=self.home,
            tmux=self.tmux,
            fakes=self.fakes,
            env=env,
            stdin=stdin,
            cwd=cwd if cwd is not None else self.root,
        )

    def env(self, extra: dict[str, str | None] | None = None) -> dict[str, str]:
        """The scrubbed environment `self.tx` runs under (for pty clients / hand-run helpers)."""
        return scrubbed_env(self.home, self.tmux, self.fakes, extra)

    def tx_pty(self, argv: list[str], *, env: dict[str, str | None] | None = None,
               cwd: str | Path | None = None, rows: int = 50, cols: int = 200) -> PtyProcess:
        """Run `TX_BIN argv` on its own pty (the picker / curses cases); closed at teardown."""
        process = PtyProcess([TX_BIN, *argv], env=self.env(env), cwd=cwd if cwd is not None else self.root,
                             rows=rows, cols=cols)
        self.addCleanup(process.close)
        return process

    def attach_client(self, session: str, *, rows: int = 50, cols: int = 200) -> PtyProcess:
        """An outer pty client on `session` (see `TmuxServer.attach_client`); closed at teardown."""
        client = self.tmux.attach_client(session, env=self.env(), rows=rows, cols=cols)
        self.addCleanup(client.close)
        return client

    def run_in_pane(self, target: str, command: str, timeout: float = 30.0) -> Result:
        """Type `command` into a shell pane (redirected to files under the temp root) and wait for
        its exit code — how a case runs `tx` INSIDE tmux (`$TMUX`, `#S`, `$TX_SESSION_ID` set)."""
        directory = self.root / "pane-runs" / uuid.uuid4().hex[:8]
        directory.mkdir(parents=True)
        out, err, code = directory / "out", directory / "err", directory / "code"
        self.tmux.type_line(
            target,
            f"{command} >{shlex.quote(str(out))} 2>{shlex.quote(str(err))}; echo $? >{shlex.quote(str(code))}",
        )
        wait_until(lambda: code.exists() and code.read_text().strip() != "", timeout, what=f"{command} in {target}")
        return Result(
            code=int(code.read_text().strip()),
            out=strip_ansi(out.read_text()),
            err=strip_ansi(err.read_text()),
            raw_out=out.read_text(),
            raw_err=err.read_text(),
        )

    def spawn_process(self, name: str, *, cmd: str = "sleep 300", tag: str = "t",
                      cwd: str | Path | None = None, extra: tuple[str, ...] = ()) -> dict:
        """`tx spawn name --tag tag --cwd cwd --cmd cmd …` (a direct, non-worker spawn) → the record."""
        result = self.tx(["spawn", name, "--tag", tag, "--cwd", str(cwd if cwd is not None else self.root),
                          "--cmd", cmd, *extra])
        self.assertEqual(result.code, 0, result.err)
        return json.loads(self.tx(["show", name]).out)

    def spawn_view(self, name: str, *, cwd: str | Path | None = None, cmd: str | None = None) -> None:
        """`tx spawn-view name --cwd cwd [--cmd cmd]` (default cmd: `$SHELL` = /bin/bash)."""
        argv = ["spawn-view", name, "--cwd", str(cwd if cwd is not None else self.root)]
        if cmd is not None:
            argv += ["--cmd", cmd]
        result = self.tx(argv)
        self.assertEqual(result.code, 0, result.err)

    def log_lines(self) -> list[dict]:
        return log_lines(self.home)

    def log_tail(self, count: int = 1) -> list[dict]:
        return log_tail(self.home, count)

    def assert_golden(self, name: str, actual: str) -> None:
        assert_golden(self, name, actual)

    def wait_until(self, predicate: Callable[[], object], timeout: float = 10.0, interval: float = 0.05):
        """Poll `predicate` until truthy; returns its value. Fails after `timeout` seconds."""
        try:
            return wait_until(predicate, timeout, interval)
        except AssertionError as error:
            self.fail(str(error))
