#!/usr/bin/env python3.14
"""Artifact browser — read-only + path-safety (QA P1). Directly runnable, hermetic temp home:

    python3.14 prototypes/artifact-browser/test_server.py

Exits non-zero on the first failure; prints "OK — N checks passed".

Proves the HTTP boundary rejects a non-canonical artifact id BEFORE any store call, so a `..`
traversal (literal OR percent-encoded) cannot escape `artifacts/` to read an arbitrary file under
`$TX_IDE_HOME`. Secrets are planted one level up from `artifacts/`; every traversal must 404 and
leak nothing. Raw sockets are used so the `..` reaches the server unmodified (a normal HTTP client
would collapse it first — which is why the QA repro used a raw GET).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ["TX_IDE_HOME"] = tempfile.mkdtemp()  # before importing tx / the server module
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                    # server.py (this prototype)
sys.path.insert(0, str(HERE.parents[1] / "lib"))  # the bundled tx package

import server  # noqa: E402  — the prototype under test
from tx.artifact_service import ArtifactService  # noqa: E402
from tx.storage import ensure_home, tx_ide_home  # noqa: E402

ensure_home()
PASSED = 0


def check(label, condition):
    global PASSED
    if condition:
        PASSED += 1
        return
    print(f"FAIL: {label}")
    sys.exit(1)


# ----- setup: one legit artifact inside artifacts/, secrets planted OUTSIDE it -------------------
service = ArtifactService()
inside = service.create("s", b"# inside the store\nalpha\n", title="Inside", filename="in.md")
service.modify(inside.id, "s", b"# inside the store\nalpha\nbeta\n")  # rev1 so /diff has two revs

# a record-shaped file one level up from artifacts/ — reachable only via a `..` traversal
(tx_ide_home() / "outside-record.json").write_text(json.dumps({
    "artifact_schema_version": 1, "id": "x", "title": "SECRET RECORD", "filename": "s.md",
    "created_at": 1.0, "history": [{"session_id": "s", "at": 1.0, "rev": 0, "changes": None}],
}))
(tx_ide_home() / "outside-content").mkdir()
(tx_ide_home() / "outside-content" / "current.txt").write_text("SECRET CONTENT")

# ----- unit: _safe_artifact_id -------------------------------------------------------------------
check("a canonical uuid is accepted", server._safe_artifact_id(inside.id) == inside.id)
for bad in (
    "../outside-record", "%2e%2e%2foutside-record", "..%2Foutside-record", "%2e%2e/outside-record",
    inside.id + "/..", "outside-content", "not-a-uuid", "", "..", "../../etc/passwd",
):
    check(f"rejects non-uuid id {bad!r}", server._safe_artifact_id(bad) is None)

# ----- integration: RAW sockets (no client-side `..` normalization, as the QA repro) -------------
httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BrowserHandler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def raw_get(path):
    """Send a RAW HTTP/1.0 GET with `path` verbatim; return (status, body_bytes)."""
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection.sendall(f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode())
    raw = b""
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            break
        raw += chunk
    connection.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), body


try:
    status, body = raw_get(f"/api/artifacts/{inside.id}")
    check("a valid id serves the detail (200)", status == 200 and b"Inside" in body)
    status, body = raw_get(f"/api/artifacts/{inside.id}/raw")
    check("a valid id serves raw bytes (200)", status == 200 and b"beta" in body)
    status, body = raw_get(f"/api/artifacts/{inside.id}/diff?a=0&b=1")
    check("a valid id serves a diff (200)", status == 200 and b"+beta" in body)
    status, body = raw_get("/api/artifacts/00000000-0000-0000-0000-000000000000")
    check("a valid-but-missing uuid 404s cleanly", status == 404)

    for path in (
        "/api/artifacts/../outside-record",                       # detail, literal
        "/api/artifacts/%2e%2e%2foutside-record",                 # detail, encoded ../
        "/api/artifacts/..%2Foutside-record",                     # detail, mixed
        "/api/artifacts/../outside-record/diff?a=0&b=0",          # diff, literal
        "/api/artifacts/%2e%2e%2foutside-record/diff?a=0&b=0",    # diff, encoded
        "/api/artifacts/../outside-content/raw",                  # raw, literal
        "/api/artifacts/%2e%2e%2foutside-content/raw",            # raw, encoded
    ):
        status, body = raw_get(path)
        leaked = b"SECRET" in body or b"outside" in body.lower()
        check(f"traversal 404 + no leak: {path}", status == 404 and not leaked)
finally:
    httpd.shutdown()

print(f"OK — {PASSED} checks passed")
