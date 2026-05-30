"""`Tmux` — the single adapter wrapping every `subprocess` tmux call (stage S1a).

One object owns the tmux boundary: session lifecycle (`new_session` / `has_session` /
`kill_session` / `rename_session`), the SOLE liveness scan (`list_sessions`, C1), `send_keys`,
option / hook / `@tx_id` get-set, `display_message`, and `switch_client`. Boundary validation
(parsing tmux output, distinguishing "no such session" from a real failure) lives here.

The attachment-topology join is **frozen here as SIGNATURES only**: `attachment_map` /
`attached_to` / `inner_for_pane` return empty placeholders ({} / [] / None) until S6 builds the
real TTY→pane→session join (attachment-topology.md §3/§9). The `Location` shape is already real
(S0), so no later stage churns when S6 fills these in. `focus_envelope` (which replaces
`bin/tx-assistant`'s `build_envelope`) ships a basic working body; S6 unifies it onto
`attachment_map` and joins the inner session's record (M-focus, attachment-topology §6).
"""

from __future__ import annotations

import subprocess

from .session import Location

# Injected into every spawned session so colors render in a detached `new-session -d` (which does
# not inherit the client's terminal hints). Mirrors the old `write_session_metadata`.
TRUECOLOR_ENV = {"COLORTERM": "truecolor", "TERM": "xterm-256color"}


class TmuxError(RuntimeError):
    """A tmux invocation we expected to succeed exited nonzero (e.g. a bad cwd on `new-session`).

    Calls whose nonzero exit is a normal *answer* (`has-session`, `list-sessions` with no server)
    go through `_run_quiet` and never raise — only genuinely unexpected failures surface here.
    """


class Tmux:
    def __init__(self, binary: str = "tmux"):
        self.binary = binary

    # ----- raw subprocess plumbing ---------------------------------------------------------

    def _run(self, args: list[str]) -> str:
        """Run a tmux call that must succeed; raise `TmuxError` on a nonzero exit."""
        result = subprocess.run([self.binary, *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def _run_quiet(self, args: list[str]) -> tuple[int, str]:
        """Run a call whose nonzero exit is a normal answer; return (returncode, stdout)."""
        result = subprocess.run([self.binary, *args], capture_output=True, text=True)
        return result.returncode, result.stdout

    # ----- session lifecycle ---------------------------------------------------------------

    def has_session(self, name: str) -> bool:
        """Whether a session named exactly `name` is live. `=name` is an exact-match target so a
        prefix never false-matches (tmux defaults to prefix matching)."""
        code, _ = self._run_quiet(["has-session", "-t", f"={name}"])
        return code == 0

    def new_session(self, *, name: str, cwd: str, command: str, env: dict[str, str]) -> int:
        """Create a detached session running `command`, returning its first pane's pid (provenance
        only — liveness is `has-session`, C1). Caller `env` overrides the truecolor defaults."""
        args = ["new-session", "-d", "-P", "-F", "#{pane_pid}", "-s", name, "-c", cwd]
        for key, value in {**TRUECOLOR_ENV, **env}.items():
            args += ["-e", f"{key}={value}"]
        args.append(command)
        return int(self._run(args).strip())

    def kill_session(self, name: str) -> bool:
        code, _ = self._run_quiet(["kill-session", "-t", name])
        return code == 0

    def rename_session(self, old: str, new: str) -> None:
        self._run(["rename-session", "-t", old, new])

    def list_sessions(self, fmt: str) -> list[str]:
        """The single `list-sessions -F fmt` scan — the SOLE liveness signal (C1) and the only
        server sweep the Reconciler makes (C4). No server / no sessions → []."""
        code, out = self._run_quiet(["list-sessions", "-F", fmt])
        if code != 0:
            return []
        return [line for line in out.splitlines() if line]

    # ----- keys / options / hooks ----------------------------------------------------------

    def send_keys(self, target: str, keys: str, *, literal: bool = False) -> None:
        """Send `keys` to a session/pane. `literal=False` lets key names through (so `"Enter"`
        sends the Enter key); `literal=True` types the string verbatim."""
        args = ["send-keys", "-t", target]
        if literal:
            args.append("-l")
        args += ["--", keys]
        self._run(args)

    def set_option(self, target: str, option: str, value: str, *, pane: bool = False) -> None:
        args = ["set-option", "-t", target]
        if pane:
            args.append("-p")
        args += [option, value]
        self._run(args)

    def set_window_option(self, target: str, option: str, value: str) -> None:
        self._run(["set-window-option", "-t", target, option, value])

    def show_option(self, target: str, option: str) -> str | None:
        """Read one option (`-vq` = value only, quiet) or None when unset/no such session."""
        code, out = self._run_quiet(["show-options", "-vqt", target, option])
        value = out.strip()
        return value or None

    def set_hook(self, target: str, hook: str, command: str) -> None:
        self._run(["set-hook", "-t", target, hook, command])

    def get_tx_id(self, name: str) -> str | None:
        return self.show_option(name, "@tx_id")

    def set_tx_id(self, name: str, session_id: str) -> None:
        self.set_option(name, "@tx_id", session_id)

    def switch_client(self, name: str) -> None:
        self._run(["switch-client", "-t", name])

    # ----- introspection -------------------------------------------------------------------

    def display_message(self, fmt: str, *, target: str | None = None) -> str | None:
        """Expand a tmux format against `target` (or the calling client). None outside tmux / on
        an empty expansion."""
        args = ["display-message"]
        if target is not None:
            args += ["-t", target]
        args += ["-p", fmt]
        _code, out = self._run_quiet(args)
        value = out.strip()
        return value or None

    def current_session_name(self) -> str | None:
        return self.display_message("#S")

    def current_pane_id(self) -> str | None:
        return self.display_message("#{pane_id}")

    def current_pane_path(self) -> str | None:
        return self.display_message("#{pane_current_path}")

    # ----- attachment topology (SIGNATURES frozen here; bodies are S6) ---------------------

    def attachment_map(self) -> dict[str, list[Location]]:
        """inner-session-name → every pane currently surfacing it (captures multi-attach).
        PLACEHOLDER ({}) until S6 builds the join (attachment-topology §3). The `Location` shape is
        already real (S0), so consumers don't churn when S6 fills this in."""
        return {}

    def attached_to(self, name: str) -> list[Location]:
        """Where `name` is surfaced. PLACEHOLDER ([]) until S6 — `[]` ("attached nowhere") is the
        correct default for the snapshot stamped on every save (attachment-topology §4)."""
        return []

    def inner_for_pane(self, pane_id: str) -> str | None:
        """Reverse lookup (which inner session is nested in this pane), for `focus_envelope`.
        PLACEHOLDER (None) until S6 unifies it onto `attachment_map`."""
        return None

    def focus_envelope(self, pane_id: str) -> str:
        """Build the `<tx-command-prompt …/>` envelope for the firing pane (replaces
        `bin/tx-assistant`'s `build_envelope`). BASIC working version: emits the pane's own
        location plus, when it hosts a nested `tmux attach`, the inner session's name. S6 unifies
        this onto `attachment_map()` and joins the inner session's record (kind/tags)."""
        fields = (
            "session_name", "window_index", "window_name", "pane_index", "pane_title",
            "pane_current_command", "pane_current_path", "pane_tty",
        )
        line = self.display_message("|".join("#{" + name + "}" for name in fields), target=pane_id)
        if line is None:
            return ""
        (session_name, window_index, window_name, pane_index, pane_title,
         pane_command, pane_path, pane_tty) = line.split("|")
        attrs = {
            "session-name": session_name, "window-index": window_index,
            "window-name": window_name, "pane-id": pane_id, "pane-index": pane_index,
            "pane-title": pane_title, "pane-cmd": pane_command, "pane-path": pane_path,
        }
        remote = self.show_option(pane_id, "@remote-session")
        if remote is not None:
            # ssh attach: list-clients can't see the remote client, so trust the pane override
            # (D-remote — handled outside attached_to, attachment-topology §1).
            attrs["inner-remote"] = "1"
            attrs["inner-session-name"] = remote
        else:
            inner = self._inner_session_for_tty(pane_tty)
            if inner is not None:
                attrs["inner-session-name"] = inner
        body = "".join(f" {key}='{_xml_escape(value)}'" for key, value in attrs.items())
        return f"<tx-command-prompt{body}/>"

    def _inner_session_for_tty(self, pane_tty: str) -> str | None:
        """Minimal client-tty → session join (the inline shape S6 replaces with the unified
        `attachment_map`): a nested `tmux attach` registers a client whose tty IS the host pane's
        `pane_tty` (attachment-topology §1)."""
        code, out = self._run_quiet(["list-clients", "-F", "#{client_tty} #{client_session}"])
        if code != 0:
            return None
        for entry in out.splitlines():
            client_tty, _, client_session = entry.partition(" ")
            if client_tty == pane_tty:
                return client_session
        return None


def _xml_escape(raw: str) -> str:
    return (
        raw.replace("&", "&amp;").replace("'", "&apos;").replace("<", "&lt;").replace(">", "&gt;")
    )
