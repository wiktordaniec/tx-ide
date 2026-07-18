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
    # P2b: `match` + a trailing `$` would accept these — Python's `$` also matches just before one
    # final newline, so a control character could survive the validator. `fullmatch` refuses them.
    inside.id + "\n", inside.id + "%0A", inside.id + "\r", inside.id + "%0D%0A",
    "\n" + inside.id, " " + inside.id, inside.id + " ",
):
    check(f"rejects non-canonical id {bad!r}", server._safe_artifact_id(bad) is None)

# ----- integration: RAW sockets (no client-side `..` normalization, as the QA repro) -------------
httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.BrowserHandler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def raw_request(path):
    """Send a RAW HTTP/1.0 GET with `path` verbatim; return (status, header_block, body_bytes)."""
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
    return int(head.split(b" ", 2)[1]), head, body


def raw_get(path):
    """(status, body) — the common case."""
    status, _head, body = raw_request(path)
    return status, body


try:
    status, body = raw_get(f"/api/artifacts/{inside.id}")
    check("a valid id serves the detail (200)", status == 200 and b"Inside" in body)
    status, body = raw_get(f"/api/artifacts/{inside.id}/raw")
    check("a valid id serves raw bytes (200)", status == 200 and b"beta" in body)
    status, body = raw_get(f"/api/artifacts/{inside.id}/diff?a=0&b=1")
    check("a valid id serves a diff (200)", status == 200 and b"+beta" in body)
    status, body = raw_get("/api/artifacts/00000000-0000-0000-0000-000000000000")
    check("a valid-but-missing uuid 404s cleanly", status == 404)
    # P2b over HTTP: a canonical uuid carrying an encoded trailing newline must 404, not resolve.
    for suffix, label in (("%0A", "encoded LF"), ("%0D", "encoded CR"), ("%00", "encoded NUL")):
        status, _body = raw_get(f"/api/artifacts/{inside.id}{suffix}")
        check(f"uuid + {label} is rejected (404)", status == 404)
        status, _body = raw_get(f"/api/artifacts/{inside.id}{suffix}/raw")
        check(f"uuid + {label} on /raw is rejected (404)", status == 404)

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

    # ----- P1b: a record-derived filename must not inject response headers -----------------------
    # A POSIX basename may legally contain CR/LF and quotes, so `tx artifact create` can store one;
    # the raw route echoes it into Content-Disposition. It must be sanitized at that emit boundary.
    hostile_name = 'evil"\r\nX-QA-Injection: yes.md'
    hostile = service.create("s", b"payload\n", title="Hostile", filename=hostile_name)
    status, head, body = raw_request(f"/api/artifacts/{hostile.id}/raw")
    header_lines = head.split(b"\r\n")[1:]
    header_names = {line.split(b":", 1)[0].strip().lower() for line in header_lines if b":" in line}
    disposition = next(line for line in header_lines if line.lower().startswith(b"content-disposition:"))
    check("a hostile filename still downloads (200)", status == 200 and body == b"payload\n")
    check("the CRLF filename injected NO response header", b"x-qa-injection" not in header_names)
    check("Content-Disposition is one intact line (no CR/LF smuggled)",
          b"\r" not in disposition and b"\n" not in disposition)
    check("the quoted fallback is closed properly (no quote escape)",
          b'filename="' in disposition and b'"; filename*' in disposition)
    check("the exact name rides safely in RFC 5987 filename* (CRLF percent-encoded)",
          b"filename*=UTF-8''" in disposition and b"%0D%0A" in disposition)
    check("the store still holds the real filename untouched",
          service.store.load(hostile.id).filename == hostile_name)

    # ----- P2: rev strictness — absent means the working copy, malformed must be REJECTED --------
    # Presence and parse are different questions: a bad `?rev=` must not masquerade as "no rev".
    status, body = raw_get(f"/api/artifacts/{inside.id}/raw")
    check("raw with rev ABSENT serves the working copy (200)", status == 200 and b"beta" in body)
    status, body = raw_get(f"/api/artifacts/{inside.id}/raw?rev=0")
    check("raw with a VALID rev serves that snapshot (200)",
          status == 200 and b"alpha" in body and b"beta" not in body)
    for bad in ("notanumber", "1.5", "--1", "", "0x1", "1e3", "%2e%2e"):
        status, body = raw_get(f"/api/artifacts/{inside.id}/raw?rev={bad}")
        check(f"raw with MALFORMED rev={bad!r} is rejected (400)", status == 400)
    status, _body = raw_get(f"/api/artifacts/{inside.id}/diff?a=0")
    check("diff with an absent rev is still rejected (400)", status == 400)
    status, _body = raw_get(f"/api/artifacts/{inside.id}/diff?a=0&b=zzz")
    check("diff with a malformed rev is rejected (400)", status == 400)
finally:
    httpd.shutdown()

print(f"OK — {PASSED} checks passed")
