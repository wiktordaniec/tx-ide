#!/usr/bin/env python3.14
"""A local dashboard that draws the tx-ide session forest as a parent/child graph.

Reads the durable records under `$TX_IDE_HOME/sessions/<uuid>.json` through the real
`SessionStore`, so the state model, the `needs_attention` gating, and the tag-chip colors
match the `tx` picker exactly — no second source of truth to drift. It then serves the
records to `index.html`, which lays them out as a tree (the record `parent` field, matched
by name) and lets you click any node to read its full JSON.

Run it from anywhere:

    python3.14 prototypes/sessions-graph/server.py [PORT]

then open the printed URL. The records are re-read on every request, so a browser refresh
(or the page's own auto-refresh) always shows current state. `$TX_IDE_HOME` overrides the
home it reads, exactly as for `tx` itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# bin/tx points PYTHONPATH at <repo>/lib; this standalone script does the same from its own
# location (prototypes/sessions-graph/server.py → repo root is two parents up) so `import tx`
# resolves the bundled package regardless of the cwd it is launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from tx.palette import tag_cube          # noqa: E402  — path is set on the line above
from tx.render import reltime            # noqa: E402
from tx.session import Kind, Session     # noqa: E402
from tx.storage import sessions_dir      # noqa: E402
from tx.store import SessionStore        # noqa: E402
from tx.tmux import Tmux                 # noqa: E402

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
DEFAULT_PORT = 8765
# Terminal app raised to the foreground after a focus so the user can type into the just-focused
# pane right away (the double-click happened in the browser). Override with $TX_DASH_TERMINAL_APP.
TERMINAL_APP = os.environ.get("TX_DASH_TERMINAL_APP", "iTerm")


def cube_to_hex(cube_index: int) -> str:
    """Map an xterm-256 color index to `#rrggbb`, so the browser paints tag chips in the same
    colors the terminal picker uses. `palette.tag_cube` returns these indices; the 16–231 block
    is the 6×6×6 color cube, 232–255 is the grayscale ramp."""
    if 16 <= cube_index <= 231:
        offset = cube_index - 16
        red, green, blue = offset // 36, (offset // 6) % 6, offset % 6

        def channel(step: int) -> int:
            return 0 if step == 0 else 55 + 40 * step

        return f"#{channel(red):02x}{channel(green):02x}{channel(blue):02x}"
    grey = 8 + 10 * (cube_index - 232)
    return f"#{grey:02x}{grey:02x}{grey:02x}"


def session_payload(session: Session, now: float) -> dict:
    """The per-session bundle the page consumes: the on-disk record verbatim (the graph reads
    `parent`/`name`/`state` from it), a few derived display fields, and the pretty-printed JSON
    shown in the click-through preview."""
    record = session.to_dict()
    return {
        "record": record,
        "derived": {
            "needs_attention": session.needs_attention,
            "is_terminal": session.state.is_terminal,
            "started_rel": reltime(session.created_at, now),
            "idle_rel": reltime(session.last_activity, now),
            "tag_colors": {tag: cube_to_hex(tag_cube(tag)) for tag in session.tags},
        },
        "json": json.dumps(record, indent=2),
    }


def build_feed() -> dict:
    """Load every record and shape the JSON the page renders, newest-activity first so the graph's
    sibling order matches the picker's recency ordering."""
    now = time.time()
    sessions = sorted(
        SessionStore().all(), key=lambda session: session.last_activity or 0, reverse=True
    )
    return {
        "generated_at": now,
        "home": str(sessions_dir()),
        "sessions": [session_payload(session, now) for session in sessions],
    }


def _tmux_name(session: Session) -> str:
    """The live tmux session name: the UUID `id` for a process session, the display `name`
    otherwise — process sessions live in tmux under their id. (Mirrors the newer
    `Session.tmux_name`; replicated inline because this prototype pins an older `lib/tx`.)"""
    return session.id if session.kind == Kind.PROCESS else session.name


def raise_terminal() -> None:
    """Bring the terminal app to the foreground (macOS `open -a`). The double-click happened in the
    browser, so the OS foreground is the browser, not the terminal; raising it lets the user start
    typing into the just-focused pane. Best-effort — a wrong app name is a harmless no-op."""
    subprocess.run(["open", "-a", TERMINAL_APP], capture_output=True)


def focus_session(session_id: str) -> tuple[int, dict]:
    """Resolve a record id to its live pane and focus the user's tmux on it (backs the page's
    double-click-to-jump). Read-only on the store; the only state-changing tmux calls are
    select-window / select-pane — no switch-client, since the user's outer client already owns the
    `Views` host that holds the pane (so selecting the window+pane moves exactly that client)."""
    session = next((candidate for candidate in SessionStore().all() if candidate.id == session_id), None)
    if session is None:
        return 404, {"ok": False, "error": "no such session"}

    tmux = Tmux()
    target_name = _tmux_name(session)
    if not tmux.has_session(target_name):
        return 200, {"ok": False, "name": session.name, "error": "not live"}

    locations = tmux.attached_to(target_name)
    if not locations:
        return 200, {"ok": False, "name": session.name, "error": "open in no pane"}

    location = locations[0]                       # primary pane = what the picker would jump to
    target = f"{location.host}:{location.window_index}.{location.pane_index}"
    if not (tmux.select_window(target) and tmux.select_pane(target)):
        return 200, {"ok": False, "name": session.name, "error": "pane vanished"}

    raise_terminal()                              # bring the terminal forward, ready to type
    return 200, {"ok": True, "name": session.name, "location": location.to_dict()}


ASSISTANT_NAME = "tx-assistant"


def _live_tx_assistant() -> Session | None:
    """The LIVE tx-assistant, resolved by liveness — NOT by a name lookup. Two records share the
    name 'tx-assistant' (one exited); a name lookup returns the id-sorted first, which is not
    reliably the live one (the same two-records-by-name trap the focus feature hit)."""
    tmux = Tmux()
    for session in SessionStore().all():
        if session.name == ASSISTANT_NAME and session.is_alive() and tmux.has_session(_tmux_name(session)):
            return session
    return None


def _compose_message(target: Session, request: str) -> str:
    """One single-line user instruction for tx-assistant: lead with the unambiguous id, carry
    context, then the user's free-text. Collapsed to a single line (send-keys is one line)."""
    request = " ".join(request.split())
    tags = ",".join(target.tags) or "-"
    return (
        f'Act on tx session id={target.id} name="{target.name}" kind={target.kind.value} '
        f'role={target.role.value} state={target.state.value} tags={tags} cwd={target.cwd} '
        f'parent={target.parent or "-"}. Use the id (not the name) as the tx target. '
        f'User request: {request}'
    )


def message_assistant(target_id: str, request: str) -> tuple[int, dict]:
    """Compose a context+request line for the dashboard-selected session and type it into the LIVE
    tx-assistant's pane (send-keys + the 0.3s Enter pause, exactly as bin/tx-assistant does). It is
    delivered as a plain user line — not a <from-claude> peer message — so the one-shot assistant
    treats it as a command. The target's id is embedded so the assistant's own `tx` call is
    unambiguous despite the two-records-by-name collision."""
    if not (request or "").strip():
        return 400, {"ok": False, "error": "empty request"}

    target = next((session for session in SessionStore().all() if session.id == target_id), None)
    if target is None:
        return 404, {"ok": False, "error": "no such session"}

    assistant = _live_tx_assistant()
    if assistant is None:
        return 200, {"ok": False, "error": "tx-assistant is not live"}

    line = _compose_message(target, request)
    assistant_target = _tmux_name(assistant)
    subprocess.run(["tmux", "send-keys", "-t", assistant_target, "-l", "--", line])
    time.sleep(0.3)                               # the input box drops an Enter that arrives too fast
    subprocess.run(["tmux", "send-keys", "-t", assistant_target, "Enter"])
    return 200, {"ok": True, "name": target.name, "assistant": assistant.id}


class DashboardHandler(BaseHTTPRequestHandler):
    """Routes: `/api/sessions` (live JSON feed), `POST /api/focus` (jump the user's tmux to a
    session's pane), and `/` (the page). Everything else 404s."""

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/sessions"):
            self._respond(200, json.dumps(build_feed()).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            self._respond(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if self.path.startswith("/api/") else {}
        if self.path.startswith("/api/focus"):
            status, payload = focus_session(str(body.get("id", "")))
        elif self.path.startswith("/api/message"):
            status, payload = message_assistant(str(body.get("id", "")), str(body.get("request", "")))
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")
            return
        self._respond(status, json.dumps(payload).encode(), "application/json")

    def log_message(self, message_format: str, *args) -> None:
        # One quiet line per request — the default handler logs more than a prototype needs.
        sys.stderr.write(f"  {self.command} {self.path}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    url = f"http://127.0.0.1:{port}/"
    print(f"tx-ide session graph  →  {url}")
    print(f"reading records from  {sessions_dir()}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
