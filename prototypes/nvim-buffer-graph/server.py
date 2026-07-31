#!/usr/bin/env python3.14
"""nvim-buffer-graph — visualize the files buffered in a live nvim as a call graph.

Every running nvim auto-creates an RPC socket under the user's run directory
(``$TMPDIR/nvim.<user>/<hash>/nvim.<pid>.0``). This server discovers those
sockets, asks each instance over ``nvim --server <socket> --remote-expr`` for
its ``$TX_SESSION_ID`` and buffer list, and joins the answers with the durable
tx session records so the picker shows real session names and tags. For the
selected session it parses the buffered Python files (stdlib ``ast``, see
``callgraph.py``) and serves a symbol-level cross-file call graph: which
function or method of one buffered file calls, instantiates, holds, or
inherits which class/function/method of another.

Read-only by design: it types nothing into tmux and never mutates a session —
its only writes are the RPC queries answered by nvim itself.

Run it from anywhere:

    python3.14 prototypes/nvim-buffer-graph/server.py            # port 8767
    python3.14 prototypes/nvim-buffer-graph/server.py 9000
    python3.14 prototypes/nvim-buffer-graph/server.py 9000 0.0.0.0
"""

import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Make ``import tx`` work when run standalone from anywhere (same trick as the
# sibling prototypes: repo root is two levels up, package lives in lib/).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

import callgraph
from tx.palette import tag_cube
from tx.store import SessionStore

DEFAULT_PORT = 8767
DEFAULT_HOST = "127.0.0.1"

INDEX_FILE = Path(__file__).resolve().parent / "index.html"

RPC_TIMEOUT_SECONDS = 3.0
SCAN_WORKERS = 8

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# One expression fetches everything the picker and the graph need in a single
# round-trip into the target nvim.
NVIM_PROBE_EXPR = (
    'json_encode({"tx": getenv("TX_SESSION_ID"), "cwd": getcwd(),'
    ' "bufs": map(getbufinfo({"buflisted": 1}),'
    ' {_, b -> {"path": b.name, "changed": b.changed, "lastused": b.lastused}})})'
)


def cube_to_hex(cube_index):
    """Map a tag_cube 6x6x6 cube index to its hex color (mirrors sessions-graph)."""
    levels = (0, 95, 135, 175, 215, 255)
    index = cube_index - 16
    red = levels[index // 36]
    green = levels[(index % 36) // 6]
    blue = levels[index % 6]
    return f"#{red:02x}{green:02x}{blue:02x}"


def socket_roots():
    roots = [Path(tempfile.gettempdir())]
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        roots.append(Path(xdg_runtime))
    return roots


def discover_sockets():
    """All live-looking nvim RPC sockets for this user."""
    user = getpass.getuser()
    sockets = []
    for root in socket_roots():
        sockets.extend(root.glob(f"nvim.{user}/*/nvim.*.0"))
    return sorted(set(sockets))


def probe_socket(socket_path):
    """Ask one nvim for its tx id, cwd, and buffer list. None if unreachable."""
    try:
        process = subprocess.run(
            ["nvim", "--server", str(socket_path), "--remote-expr", NVIM_PROBE_EXPR],
            capture_output=True, text=True, timeout=RPC_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if process.returncode != 0:
        return None
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError:
        return None
    payload["socket"] = str(socket_path)
    payload["bufs"] = [entry for entry in payload.get("bufs", []) if entry.get("path")]
    return payload


def scan_nvims():
    """Probe every discovered socket concurrently."""
    sockets = discover_sockets()
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        probes = list(pool.map(probe_socket, sockets))
    return [probe for probe in probes if probe is not None]


def session_records():
    """Durable records by id, freshly read (records may change between polls)."""
    return {record.id: record for record in SessionStore().all()}


def build_nvim_feed():
    records = session_records()
    entries = []
    for probe in scan_nvims():
        record = records.get(probe.get("tx") or "")
        tags = list(record.tags) if record else []
        entries.append({
            "key": record.id if record else probe["socket"],
            "socket": probe["socket"],
            "tx_id": record.id if record else None,
            "name": record.name if record else Path(probe["socket"]).name,
            "tags": tags,
            "tag_colors": {tag: cube_to_hex(tag_cube(tag)) for tag in tags},
            "cwd": probe.get("cwd"),
            "buffer_count": len(probe["bufs"]),
            "tracked": record is not None,
        })
    entries.sort(key=lambda entry: (not entry["tracked"], entry["name"]))
    return {"nvims": entries}


def find_probe(key):
    """Re-scan and locate the nvim identified by a tx session id or socket path.

    The key is matched only against freshly discovered sockets — a caller can
    never steer the subprocess at an arbitrary path.
    """
    is_tx_id = bool(UUID_RE.match(key))
    for socket_path in discover_sockets():
        if not is_tx_id and str(socket_path) != key:
            continue
        probe = probe_socket(socket_path)
        if probe is None:
            continue
        if is_tx_id and probe.get("tx") != key:
            continue
        return probe
    return None


def build_graph_payload(key):
    probe = find_probe(key)
    if probe is None:
        return None
    records = session_records()
    record = records.get(probe.get("tx") or "")
    tags = list(record.tags) if record else []
    graph = callgraph.build_graph(probe["bufs"], probe.get("cwd") or "/")
    graph["session"] = {
        "key": record.id if record else probe["socket"],
        "name": record.name if record else Path(probe["socket"]).name,
        "tags": tags,
        "tag_colors": {tag: cube_to_hex(tag_cube(tag)) for tag in tags},
        "cwd": probe.get("cwd"),
        "socket": probe["socket"],
        "tracked": record is not None,
    }
    return graph


class Handler(BaseHTTPRequestHandler):
    """Routes:

    GET /                     — the single-page UI (index.html off disk)
    GET /api/nvims            — running nvim instances joined with tx records
    GET /api/graph?session=K  — buffer call graph; K is a tx session id (UUID)
                                or a socket path previously served by /api/nvims
    """

    server_version = "nvim-buffer-graph/0.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
        elif parsed.path == "/api/nvims":
            self._send_json(build_nvim_feed())
        elif parsed.path == "/api/graph":
            query = parse_qs(parsed.query)
            key = (query.get("session") or [""])[0]
            if not key:
                self._send_json({"error": "missing session parameter"}, status=400)
                return
            payload = build_graph_payload(key)
            if payload is None:
                self._send_json({"error": "nvim not found (it may have exited)"},
                                status=404)
            else:
                self._send_json(payload)
        else:
            self._send_json({"error": "not found"}, status=404)

    def _send_file(self, path, content_type):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {args[0]}")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    host = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_HOST
    if host not in ("127.0.0.1", "localhost"):
        print(f"warning: binding to {host} exposes your nvim buffer contents "
              "beyond this machine")
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as error:
        raise SystemExit(
            f"cannot bind {host}:{port} ({error}); pass a free port, e.g. "
            f"python3.14 {sys.argv[0]} {port + 1}")
    print(f"nvim-buffer-graph on http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
