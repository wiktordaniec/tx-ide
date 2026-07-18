#!/usr/bin/env python3.14
"""A local, read-only browser for durable tx-ide artifacts (Plan 2, Rendering §).

Reads the records under `$TX_IDE_HOME/artifacts/<id>.json` and their revision files through the
real `ArtifactStore` + `ArtifactContent` — the same primitives `tx artifact` uses, so there is no
second source of truth to drift. The store is re-read on every request, so a browser refresh always
shows current state.

Read-only in the STRICT sense: it never writes, and — unlike the CLI/agent read path — its reads do
NOT go through `ArtifactService`, so they never append to the EventLog (a polling dashboard would be
noise; EventLog read-visibility covers session/CLI reads, not this surface). It is not a tx session
and not a stored rendering — a live render over the store.

Run it from anywhere:

    python3.14 prototypes/artifact-browser/server.py [PORT [HOST]]

then open the printed URL. `$TX_IDE_HOME` overrides the home it reads, exactly as for `tx` itself.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# bin/tx points PYTHONPATH at <repo>/lib; this standalone script does the same from its own location
# (prototypes/artifact-browser/server.py → repo root is two parents up) so `import tx` resolves the
# bundled package regardless of the cwd it is launched from.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "lib"))

from tx.artifact import Artifact  # noqa: E402
from tx.artifact_store import ArtifactContent, ArtifactStore  # noqa: E402
from tx.render import actor_label, reltime  # noqa: E402
from tx.storage import artifacts_dir  # noqa: E402
from tx.store import SessionStore  # noqa: E402

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
DEFAULT_PORT = 8770

# Extensions whose working copy the page renders as markdown; anything else utf-8 is preformatted
# text, and anything that does not decode as utf-8 is offered as a binary download.
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

# A canonical artifact id is a uuid. The store joins the id into a filesystem path, so the browser
# accepts ONLY that exact token in a route — a crafted `..` / `/` (literal OR percent-encoded) can
# then never escape the artifacts directory to read an arbitrary file (QA P1: path traversal).
# Deliberately UNANCHORED + used with `fullmatch`: `match` with a trailing `$` would accept a
# canonical uuid followed by one newline (Python's `$` also matches just before a final `\n`), which
# would let a control character survive into a record-derived path (QA P2b). `fullmatch` requires the
# WHOLE string to be the token — no trailing newline, no leading or trailing anything.
_ARTIFACT_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _safe_artifact_id(raw: str) -> str | None:
    """Validate a URL path segment as a canonical artifact id BEFORE it reaches the store — boundary
    validation at the HTTP edge, so no raw route input is ever joined into a filesystem path. Decode
    percent-encoding ONCE, then require the exact uuid shape; a literal or encoded `..`/`/`, or any
    other non-uuid token, returns None (the route 404s without touching the store)."""
    decoded = urllib.parse.unquote(raw)
    return decoded if _ARTIFACT_ID_RE.fullmatch(decoded) else None


# Everything outside this set is dropped from the quoted Content-Disposition fallback. A record's
# filename is untrusted at the HTTP edge: a POSIX basename may legally contain CR/LF (which would
# inject response headers — QA P1b), quotes (which truncate the parameter), or backslashes.
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _content_disposition(filename: str) -> str:
    """A header-safe `Content-Disposition` for a download. The quoted fallback is reduced to a
    conservative ASCII token (no CR/LF, quote or backslash can survive), and the exact name rides in
    the RFC 5987 `filename*` parameter, whose percent-encoding makes a header break impossible. The
    store keeps the real filename untouched — this sanitizing belongs at the boundary that emits it."""
    fallback = _SAFE_FILENAME_RE.sub("_", filename).strip("._") or "artifact"
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


# ----- payloads — read through the store + content primitives, NEVER the logging service ---------


def _author_ids(artifact: Artifact) -> list[str]:
    """The distinct touch author ids in first-touch order (creator first) — straight off history."""
    seen: list[str] = []
    for touch in artifact.history:
        if touch.session_id not in seen:
            seen.append(touch.session_id)
    return seen


def _authors(artifact: Artifact, names: dict[str, str]) -> list[dict]:
    """Distinct authors as `{id, name}` — the id stays the durable identity, `name` is the session
    name resolved at request time (or the `user` sentinel / a short id when unresolvable)."""
    return [{"id": author, "name": actor_label(author, names)} for author in _author_ids(artifact)]


def list_payload() -> dict:
    """The list feed — every artifact with the settled columns (title, id, filename, #revs, last
    touched, touch authors), newest-touched first. Re-reads the store on every call and resolves
    author session names for just the ids that appear."""
    artifacts = sorted(ArtifactStore().all(), key=lambda artifact: artifact.updated_at, reverse=True)
    names = SessionStore().names_for(
        touch.session_id for artifact in artifacts for touch in artifact.history
    )
    return {
        "artifacts": [
            {
                "id": artifact.id,
                "title": artifact.title,
                "filename": artifact.filename,
                "revs": len(artifact.history),
                "updated_ago": reltime(artifact.updated_at),
                "authors": _authors(artifact, names),
            }
            for artifact in artifacts
        ]
    }


def detail_payload(artifact_id: str) -> dict | None:
    """One artifact's detail — metadata, the full touch/version log, and the rendered working copy.
    None when there is no such record. Reads `current` once and derives the dirty flag from it."""
    artifact = ArtifactStore().load(artifact_id)
    if artifact is None:
        return None
    content = ArtifactContent()
    current_bytes = content.read_current(artifact)
    try:
        dirty = current_bytes != content.read_rev(artifact, artifact.latest_rev)
    except FileNotFoundError:
        dirty = False  # a missing last-rev file is corruption `tx artifact doctor` owns, not a 500
    names = SessionStore().names_for(touch.session_id for touch in artifact.history)
    return {
        "id": artifact.id,
        "title": artifact.title,
        "filename": artifact.filename,
        "created_ago": reltime(artifact.created_at),
        "updated_ago": reltime(artifact.updated_at),
        "revs": len(artifact.history),
        "dirty": dirty,
        "history": [
            {
                "rev": touch.rev,
                "ago": reltime(touch.at),
                "session_id": touch.session_id,
                "actor": actor_label(touch.session_id, names),
                "changes": touch.changes,
            }
            for touch in artifact.history
        ],
        "working": _render_working_copy(artifact, current_bytes),
    }


def _render_working_copy(artifact: Artifact, raw: bytes) -> dict:
    """Classify the working copy for the detail view: markdown (rendered client-side), plain utf-8
    text (preformatted), or binary (a download link only). Classification is by extension + a utf-8
    decode probe; the store's bytes are authoritative."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"encoding": "binary", "size": len(raw)}
    suffix = Path(artifact.filename).suffix.lower()
    return {
        "encoding": "markdown" if suffix in MARKDOWN_SUFFIXES else "text",
        "text": text,
        "size": len(raw),
    }


def raw_bytes(artifact_id: str, rev: int | None) -> tuple[bytes, str] | None:
    """The raw bytes of a rev (or the working copy when `rev` is None) + a download filename — backs
    the binary download link. None when the artifact/rev is absent."""
    artifact = ArtifactStore().load(artifact_id)
    if artifact is None:
        return None
    content = ArtifactContent()
    try:
        data = content.read_current(artifact) if rev is None else content.read_rev(artifact, rev)
    except FileNotFoundError:
        return None
    return data, artifact.filename


def diff_payload(artifact_id: str, rev_a: int, rev_b: int) -> dict | None:
    """An on-demand `difflib` unified diff between two revs — computed HERE (never via
    `ArtifactService.diff`, which would log). Refuses cleanly when either rev is not utf-8 text.
    None when the artifact is absent."""
    artifact = ArtifactStore().load(artifact_id)
    if artifact is None:
        return None
    content = ArtifactContent()
    try:
        left = content.read_rev(artifact, rev_a).decode("utf-8")
        right = content.read_rev(artifact, rev_b).decode("utf-8")
    except FileNotFoundError:
        return {"error": f"no such revision (have 0..{artifact.latest_rev})"}
    except UnicodeDecodeError:
        return {"error": "a revision is not utf-8 text — cannot diff"}
    diff = "".join(
        difflib.unified_diff(
            left.splitlines(keepends=True),
            right.splitlines(keepends=True),
            fromfile=f"rev{rev_a}",
            tofile=f"rev{rev_b}",
        )
    )
    return {"a": rev_a, "b": rev_b, "diff": diff}


# Returned by `_int_param` when a query parameter is PRESENT but not a plain integer. It must stay
# distinct from `None` (absent): collapsing the two let a malformed `?rev=` silently fall back to the
# default instead of being rejected (QA P2).
_MALFORMED_PARAM = object()


# ----- HTTP — GET only; the browser never writes -------------------------------------------------


class BrowserHandler(BaseHTTPRequestHandler):
    """Read-only routes: `/` (the page), `/api/artifacts` (the list), `/api/artifacts/<id>` (detail),
    `/api/artifacts/<id>/raw?rev=N` (raw bytes / binary download), `/api/artifacts/<id>/diff?a=X&b=Y`
    (on-demand difflib diff). GET only — there is no write path. Anything else 404s."""

    def _respond(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # a live render — never serve a stale copy
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._respond(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        # keep_blank_values so `?rev=` counts as PRESENT-but-empty (a malformed value to reject),
        # not as absent — presence and parse must stay distinguishable (QA P2).
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if path in ("/", "/index.html"):
            self._respond(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/artifacts":
            self._json(200, list_payload())
        elif path.startswith("/api/artifacts/"):
            self._route_artifact(path[len("/api/artifacts/") :], query)
        else:
            self._respond(404, b"not found\n", "text/plain; charset=utf-8")

    def _route_artifact(self, rest: str, query: dict) -> None:
        """`<id>` (detail) | `<id>/raw` (bytes) | `<id>/diff` (difflib). The id is validated to a
        canonical uuid at this boundary — a non-uuid (a traversal payload) 404s before the store is
        ever touched; rev params must be plain integers (`_int_param`)."""
        if rest.endswith("/raw"):
            artifact_id = _safe_artifact_id(rest[: -len("/raw")])
            rev = self._int_param(query, "rev")  # absent => the working copy; malformed => reject
            if rev is _MALFORMED_PARAM:
                self._respond(400, b"rev must be a plain integer\n", "text/plain; charset=utf-8")
                return
            result = raw_bytes(artifact_id, rev) if artifact_id else None
            if result is None:
                self._respond(404, b"not found\n", "text/plain; charset=utf-8")
                return
            data, filename = result
            self._respond(
                200, data, "application/octet-stream",
                {"Content-Disposition": _content_disposition(filename)},
            )
        elif rest.endswith("/diff"):
            artifact_id = _safe_artifact_id(rest[: -len("/diff")])
            if artifact_id is None:
                self._json(404, {"error": "no such artifact"})
                return
            # Both revs are REQUIRED here, so absent (None) and malformed are equally a 400 — an
            # `isinstance` check covers both without collapsing them upstream.
            rev_a, rev_b = self._int_param(query, "a"), self._int_param(query, "b")
            if not isinstance(rev_a, int) or not isinstance(rev_b, int):
                self._json(400, {"error": "diff needs integer rev params a and b"})
                return
            payload = diff_payload(artifact_id, rev_a, rev_b)
            self._json(200 if payload is not None else 404, payload or {"error": "no such artifact"})
        else:
            artifact_id = _safe_artifact_id(rest)
            payload = detail_payload(artifact_id) if artifact_id is not None else None
            self._json(200 if payload is not None else 404, payload or {"error": "no such artifact"})

    def _int_param(self, query: dict, name: str):
        """Three-state, because PRESENCE and PARSE are different questions (QA P2): `None` when the
        key is ABSENT (the caller supplies its own default), `_MALFORMED_PARAM` when it is PRESENT
        but not a plain integer (the caller must reject it), else the int. Collapsing the two let a
        bad `?rev=` masquerade as "no rev given". `int()` is the parse, so `--5`, `1.5`, `0x1` and an
        empty value are all refused rather than crashing or silently passing."""
        values = query.get(name)
        if not values:
            return None
        try:
            return int(values[0])
        except ValueError:
            return _MALFORMED_PARAM

    def log_message(self, message_format: str, *args) -> None:
        # One quiet line per request — the default handler logs more than a prototype needs.
        sys.stderr.write(f"  {self.command} {self.path}\n")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    # Default stays loopback-only; pass a HOST (e.g. a Tailscale IP) to read it from another device.
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    try:
        server = ThreadingHTTPServer((host, port), BrowserHandler)
    except OSError as error:
        raise SystemExit(
            f"cannot bind {host}:{port} — {error}.\n"
            f"pass a free port: python3.14 {Path(__file__).name} <PORT>"
        )
    url = f"http://{host}:{port}/"
    print(f"tx-ide artifact browser  →  {url}")
    if host != "127.0.0.1":
        print(f"NOTE: bound to {host} — anyone who can reach this address can read your artifacts.")
    print(f"reading artifacts from  {artifacts_dir()}")
    print("read-only: never writes, never logs to the EventLog. Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
