"""`Tmux` — the single adapter wrapping every `subprocess` tmux call (stage S1a).

One object owns the tmux boundary: session lifecycle (`new_session` / `has_session` /
`kill_session` / `rename_session`), the SOLE liveness scan (`list_sessions`, C1), `send_keys`,
option / hook / `@tx_id` get-set, `display_message`, and `switch_client`. Boundary validation
(parsing tmux output, distinguishing "no such session" from a real failure) lives here.

The attachment-topology join lives here (stage S6): ONE `_attachment_join` (the TTY→pane→session
pass) feeds `attachment_map` / `attached_to` / `inner_for_pane` / `pane_for_session` (jump) /
`focus_attrs` (the assistant envelope) — every topology reader on one map (M-focus,
attachment-topology.md §3/§6/§9). The pane-border helper `bin/tmux-pane-session-name` is the one
deliberate shell mirror (§6 — kept in lockstep with `_attachment_join`). `SessionService.focus_
envelope` wraps `focus_attrs` to also join the inner session's record (kind/tags).
"""

from __future__ import annotations

import os
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

    def unset_option(self, target: str, option: str, *, pane: bool = False) -> None:
        """Unset (`-u`) an option (pane option with `-p`). Tolerant (`_run_quiet`): this is used to
        clear the `@remote-session` border hint when a remote ssh-attach returns, and the pane may
        already be gone — best-effort cleanup at the tmux boundary, never an error."""
        args = ["set-option", "-t", target]
        if pane:
            args.append("-p")
        args += ["-u", option]
        self._run_quiet(args)

    def set_window_option(self, target: str, option: str, value: str) -> None:
        self._run(["set-window-option", "-t", target, option, value])

    def show_option(self, target: str, option: str) -> str | None:
        """Read one option (`-vq` = value only, quiet) or None when unset/no such session."""
        code, out = self._run_quiet(["show-options", "-vqt", target, option])
        value = out.strip()
        return value or None

    def get_tx_id(self, name: str) -> str | None:
        return self.show_option(name, "@tx_id")

    def set_tx_id(self, name: str, session_id: str) -> None:
        self.set_option(name, "@tx_id", session_id)

    def set_tx_view(self, name: str) -> None:
        """Mark a live session as a view — a home base the user lives in and nests other sessions
        into. Views are NOT store records: this `@tx_view` option is their entire durable identity
        (checked by the picker's nest-attach, the after-new-window border hook, the focus envelope,
        and `kill`'s view fallback). Like every tmux option it dies with the server, so a view is
        recreated cheaply by `tx spawn-view` after a restart."""
        self.set_option(name, "@tx_view", "1")

    def is_view(self, name: str) -> bool:
        """Whether the live session `name` carries the `@tx_view` marker (raw `@tx_view` reads live
        in the tmux config's border/nest hooks — this is the Python side of the same signal)."""
        return self.show_option(name, "@tx_view") == "1"

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

    # ----- interactive attach (S1b — additive; drives `tx attach`'s nest-attach / jump) -----

    def select_window(self, target: str) -> bool:
        """Focus a window; False when the target is gone (the bash `|| return 1`)."""
        code, _ = self._run_quiet(["select-window", "-t", target])
        return code == 0

    def select_pane(self, target: str) -> bool:
        """Focus a pane within its window; False when the target is gone."""
        code, _ = self._run_quiet(["select-pane", "-t", target])
        return code == 0

    def respawn_pane(self, pane_id: str, command: str) -> None:
        """Replace a pane's process with `command` (`respawn-pane -k`). The picker uses this to
        nest-attach a chosen session INTO the launching view pane — a `bash -c 'set -m; TMUX= tmux
        attach …; exec $SHELL'` that keeps the pane alive after detach while `pane_current_command`
        reads `tmux` during the attach (the client gets its own tty foreground pgroup)."""
        self._run(["respawn-pane", "-k", "-t", pane_id, command])

    def attach_session(self, name: str) -> int:
        """Attach `name` in the FOREGROUND from OUTSIDE tmux (the picker's no-`$TMUX` fallback).

        Inherits the real stdio so the attach takes over the terminal, and clears `$TMUX` so tmux
        does not refuse a nested client. Returns the attach exit code.
        """
        env = os.environ.copy()
        env.pop("TMUX", None)
        return subprocess.run([self.binary, "attach", "-t", name], env=env).returncode

    def pane_for_session(self, name: str, prefer: str = "") -> str | None:
        """A pane hosting `name` as `session:window.pane`, preferring one whose host session is in
        the comma-separated `prefer` set (today the caller's current view). Backs `tx attach
        --jump`'s target resolver — same signature + `session:window.pane` shape as before.

        S6: now the unified `attachment_map` join (attachment-topology §3/§9) — `bin/tmux-pane-for-
        session` is deleted. A nested attach wins; an `@remote-session` pane is the fallback
        (D-remote: out of `attached_to`, resolved here only as a jump target). None when no pane
        hosts it.
        """
        preferred = {item for item in prefer.split(",") if item}
        locations = self.attached_to(name)
        chosen = next((loc for loc in locations if loc.host in preferred), None)
        if chosen is None and locations:
            chosen = locations[0]
        if chosen is not None:
            return f"{chosen.host}:{chosen.window_index}.{chosen.pane_index}"
        return self._remote_pane_for_session(name)

    def _remote_pane_for_session(self, name: str) -> str | None:
        """An `@remote-session` pane whose ssh target is `name` (D-remote jump fallback). Such a
        pane has no nested client (list-clients can't see the remote tmux over ssh), so it is found
        by the pane override, not the TTY join."""
        _code, panes = self._run_quiet([
            "list-panes", "-a", "-F",
            "#{@remote-session}\t#{session_name}\t#{window_index}\t#{pane_index}",
        ])
        for line in panes.splitlines():
            fields = line.split("\t")
            if len(fields) != 4:
                continue
            remote, session_name, window_index, pane_index = fields
            if remote == name:
                return f"{session_name}:{window_index}.{pane_index}"
        return None

    # ----- attachment topology (S6 — one join, every reader on it; M-focus) ----------------

    def _attachment_join(self) -> list[tuple[str, Location]]:
        """One pass over the live server → (inner_session_name, Location) for every pane that is
        nest-attached to an inner session — the single TTY→pane→session join all the topology
        readers share (attachment-topology.md §3).

        A nested `tmux attach` registers as a client whose `client_tty` IS the host pane's
        `pane_tty`; so pane P surfaces inner session S ⟺ the client on P's tty views S, and S is
        not P's own (host) session. Two tmux calls + in-memory parse (sub-10ms). Kept in lockstep
        with the pane-border shell mirror `bin/tmux-pane-session-name` (§6's one exception).
        """
        _code, clients = self._run_quiet(["list-clients", "-F", "#{client_tty}\t#{client_session}"])
        _code, panes = self._run_quiet([
            "list-panes", "-a", "-F",
            "#{pane_tty}\t#{session_name}\t#{window_index}\t#{window_name}\t#{pane_id}\t#{pane_index}",
        ])
        session_by_tty: dict[str, str] = {}
        for line in clients.splitlines():
            client_tty, separator, client_session = line.partition("\t")
            if client_tty and separator:
                session_by_tty[client_tty] = client_session
        out: list[tuple[str, Location]] = []
        for line in panes.splitlines():
            fields = line.split("\t")
            if len(fields) != 6:
                continue
            pane_tty, host, window_index, window_name, pane_id, pane_index = fields
            inner = session_by_tty.get(pane_tty)
            if inner and inner != host:  # guard the degenerate self-attach loop
                out.append((inner, Location(host, window_index, window_name, pane_id, pane_index)))
        return out

    def attachment_map(self) -> dict[str, list[Location]]:
        """inner-session-name → every pane currently surfacing it (captures multi-attach). Each
        list is sorted into a stable (host, window, pane) order, so the rendered primary location
        and the jump target are deterministic and don't flicker (attachment-topology §3/§5)."""
        mapping: dict[str, list[Location]] = {}
        for inner, location in self._attachment_join():
            mapping.setdefault(inner, []).append(location)
        for locations in mapping.values():
            locations.sort(
                key=lambda loc: (loc.host, loc.window_index.zfill(8), loc.pane_index.zfill(8))
            )
        return mapping

    def attached_to(self, name: str) -> list[Location]:
        """Every pane surfacing `name`; `[]` ("attached nowhere") when detached / bare-terminal
        attached. The one source the snapshot stamped on save reads (attachment-topology §4)."""
        return self.attachment_map().get(name, [])

    def focused_session_name(self) -> str | None:
        """The inner session the user is currently LOOKING at — the session nest-attached in the
        active pane of the view host the user is driving. The mirror of the jump direction
        (`pane_for_session`): jump moves the terminal to a node, this reads which node the terminal
        is on, so the session graph can ring the focused one (M-focus, terminal → viewer).

        Same client_tty ⇒ inner-session join as `_attachment_join`, but it also needs the
        active-pane flags and per-client recency to pick *which* pane is focused, so it makes its
        own two reads rather than widening that frozen pass. Kept in lockstep with
        `_attachment_join` — if the client_tty⇒session mapping changes there, change it here too.

        The user's outer client is a real terminal, so its `client_tty` is NOT any pane's
        `pane_tty` (a nested attach's client tty IS its host pane's tty — that is the whole join).
        The focused view host is the most-recently-active such outer client's session; tmux selects
        window+pane per session, so that host has exactly one active pane (`window_active` &
        `pane_active`). The inner session on that pane's tty is the answer. None when the user is
        detached, the active pane hosts no nested session (a plain shell / the picker popup), or the
        server is gone — every "nothing is focused" case the ring should treat as no ring.
        """
        _code, clients = self._run_quiet(
            ["list-clients", "-F", "#{client_tty}\t#{client_session}\t#{client_activity}"]
        )
        _code, panes = self._run_quiet(
            ["list-panes", "-a", "-F", "#{pane_tty}\t#{session_name}\t#{window_active}\t#{pane_active}"]
        )

        session_by_tty: dict[str, str] = {}
        clients_by_recency: list[tuple[int, str, str]] = []  # (activity, client_tty, client_session)
        for line in clients.splitlines():
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            client_tty, client_session, activity = fields
            session_by_tty[client_tty] = client_session
            clients_by_recency.append((int(activity or 0), client_tty, client_session))

        pane_ttys: set[str] = set()
        active_pane_tty_by_host: dict[str, str] = {}
        for line in panes.splitlines():
            fields = line.split("\t")
            if len(fields) != 4:
                continue
            pane_tty, host, window_active, pane_active = fields
            pane_ttys.add(pane_tty)
            if window_active == "1" and pane_active == "1":
                active_pane_tty_by_host[host] = pane_tty

        outer_clients = [client for client in clients_by_recency if client[1] not in pane_ttys]
        if not outer_clients:
            return None
        focused_host = max(outer_clients, key=lambda client: client[0])[2]
        focused_pane_tty = active_pane_tty_by_host.get(focused_host)
        if focused_pane_tty is None:
            return None
        return session_by_tty.get(focused_pane_tty)

    def inner_for_pane(self, pane_id: str) -> str | None:
        """Reverse lookup (which inner session is nested in this pane), for `focus_envelope` — the
        same join, inverted (attachment-topology §3/§6)."""
        for inner, location in self._attachment_join():
            if location.pane_id == pane_id:
                return inner
        return None

    def focus_attrs(self, pane_id: str) -> dict[str, str] | None:
        """The firing pane's location attributes for the tx-assistant envelope, unified on the one
        attachment join (attachment-topology §6 — replaces `bin/tx-assistant`'s `build_envelope`).
        Pure tmux; the inner session's RECORD fields (kind/tags) are joined by `SessionService`
        (M-focus). None outside tmux / when the pane is gone."""
        fields = (
            "session_name", "window_index", "window_name", "pane_index", "pane_title",
            "pane_current_command", "pane_current_path",
        )
        line = self.display_message("|".join("#{" + name + "}" for name in fields), target=pane_id)
        if line is None:
            return None
        (session_name, window_index, window_name, pane_index, pane_title,
         pane_command, pane_path) = line.split("|")
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
            inner = self.inner_for_pane(pane_id)  # the unified join (no separate tty walk)
            if inner is not None:
                attrs["inner-session-name"] = inner
        return attrs

    def focus_envelope(self, pane_id: str) -> str:
        """The `<tx-command-prompt …/>` envelope from pure-tmux fields only (standalone use / the
        frozen signature). `SessionService.focus_envelope` is the M-focus path that ALSO joins the
        record's kind/tags. Empty string when the pane is gone."""
        attrs = self.focus_attrs(pane_id)
        return format_envelope(attrs) if attrs is not None else ""


def format_envelope(attrs: dict[str, str]) -> str:
    """Render an ordered attribute dict as a self-closing `<tx-command-prompt …/>` tag. Shared by
    `Tmux.focus_envelope` (tmux-only) and `SessionService.focus_envelope` (tmux + record join)."""
    body = "".join(f" {key}='{_xml_escape(value)}'" for key, value in attrs.items())
    return f"<tx-command-prompt{body}/>"


def _xml_escape(raw: str) -> str:
    return (
        raw.replace("&", "&amp;").replace("'", "&apos;").replace("<", "&lt;").replace(">", "&gt;")
    )
