#!/usr/bin/env python3.14
"""Timeline data layer for the artifact-browser — swimlanes of session activity over a window.

Extraction rules ported from the tx-timeline prototype:

- one row per transcript entry: ``[t_epoch_seconds, kind, short text, probe]`` with kinds
  ``u`` = the human typed; ``p`` = peer-agent mail; ``s`` = harness/system injection;
  ``a`` = agent prose; ``x`` = tool call.  Only ``u`` rows become "you spoke" dots; the
  rest exist so segments/counts see all activity.  ``probe`` is the milestone slice of a
  tool call's RAW command (``milestone_probe``), ``None`` on every other row.
- lanes: session records are MERGED when they share a transcript path (resume chains
  re-record the same conversation under a new record id).
- segments: activity runs split on > ``SEG_GAP_MIN`` minutes of silence.
- events: ``u`` rows (burst-capped per minute) + milestone tool calls (``M_PATTERNS``,
  matched against the raw command — the displayed tool line is capped and routinely cuts
  the milestone off).

Liveness contract: an in-process cache keyed by ``(size, mtime)`` per transcript;
appended JSONL bytes are tail-parsed from the last consumed newline, so a poll costs
milliseconds while the store is quiet.  The payload carries NO clock-derived fields
(no "now", no relative ages) — an idle system yields a byte-identical response, which
the page's refresh tick fingerprints to skip repaints.  Day boundaries are LOCAL
midnights (the server runs on the user's machine, in the user's timezone).

Groups are the v6 read-time resolution (``tx.grouping.GroupResolver``): explicit
``group`` override, else the parent chain, else tags/name.  Colors come from
``tx.palette.tag_cube`` so a group is painted exactly like its chip in the chats view.
"""

import datetime
import json
import os
import re
import sys
import threading
from pathlib import Path

# bin/tx points PYTHONPATH at <repo>/lib; standalone runs resolve the bundled package from this
# file's location (prototypes/artifact-browser/ -> repo root two parents up).  A checkout elsewhere
# (scratch copies) can override with $TX_REPO_LIB.
REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = REPO_ROOT / "lib"
if not (_LIB / "tx").is_dir():
    _LIB = Path(os.environ.get("TX_REPO_LIB", _LIB))
sys.path.insert(0, str(_LIB))

from tx.artifact_store import ArtifactStore  # noqa: E402
from tx.engines import claude as claude_engine  # noqa: E402
from tx.grouping import GroupResolver  # noqa: E402
from tx.palette import tag_cube  # noqa: E402
from tx.session import Role  # noqa: E402
from tx.storage import tx_ide_home  # noqa: E402
from tx.store import SessionStore  # noqa: E402

ASSISTANT_NAME = "tx-assistant"  # flagged "sys" (with tx-system-tagged lanes); hidden by default client-side

EV_TEXT = 130        # event label length
TOOL_DETAIL = 160    # tool line detail length
ROW_TEXT_CAP = 4000  # cached per-row text — the drawer shows these verbatim (event labels re-cap)
BLURB_CAP = 220
SEG_GAP_MIN = 180    # coarse tier: split an activity bar on this silent gap (minutes) — overview zoom
FSEG_GAP_MIN = 10    # fine tier: idle threshold for zoomed-in views (the client picks by zoom)
BURST_PER_MIN = 4    # collapse >N user-events in one minute (fork replays)

# Action diamonds: PRs and artifact manipulation only — spawning/killing already render as
# edges and ×, and commit-level noise drowned the milestones that matter.
M_PATTERNS = [
    (r"\bgh pr merge\b|\bgh pr ready\b", "PR merge/ready"),
    (r"\btx artifact (create|modify)\b(?!\s+--help)", "artifact"),   # help exploration ≠ an action
]
M_RX = [(re.compile(p), lab) for p, lab in M_PATTERNS]
_UUID_RX = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# `tx artifact modify` accepts an id PREFIX (agents mostly pass 8 hex chars) — the diamond must
# resolve those too, or its click-through to the artifact silently degrades to a bare tooltip.
_SHORT_ID_RX = re.compile(r"\bartifact modify\s+([0-9a-fA-F]{4,31})\b")
CREATE_MATCH_S = 180   # `tx artifact create` has no id in the command — match creator+time this close


def milestone_probe(detail):
    """The milestone slice of a tool call's RAW command — from the match onward, `TOOL_DETAIL`
    long — or None when the command is not a milestone.

    Milestones are detected HERE, on the full command, not on the row's display text: `tool_line`
    caps the display at `TOOL_DETAIL`, while agents routinely open a command with a long path
    assignment or bury `tx artifact modify …` at the end of a multi-KB heredoc, so the verb sits
    thousands of characters in and matching the capped line silently lost the action (and, with it,
    the id the diamond clicks through to).  Only the short slice is kept per row, so detecting on
    the full command costs the cache nothing.

    A command that MENTIONS a milestone inside a `tx send-message` is quoting it, not doing it — no
    probe (the guard runs on the whole command, so a quote further in than the display cap is caught
    too)."""
    flat = re.sub(r"\s+", " ", detail or "").strip()
    if "send-message" in flat:
        return None
    for rx, _lab in M_RX:
        m = rx.search(flat)
        if m:
            return flat[m.start(): m.start() + TOOL_DETAIL]
    return None


# ----- row taxonomy (ported verbatim from the prototype) -----------------------------------------

def clean_user(t):
    m = re.search(r"<command-name>\s*(/[^<\s]+)\s*</command-name>", t)
    if m:
        a = re.search(r"<command-args>([^<]*)</command-args>", t)
        return f"[cmd] {m.group(1)}" + (f" {a.group(1).strip()}" if a and a.group(1).strip() else "")
    m = re.match(r"\s*<bash-input>(.*?)</bash-input>", t, flags=re.S)
    if m:
        return "[!] " + m.group(1).strip()
    m = re.match(r"\s*<user_shell_command>.*?<command>(.*?)</command>", t, flags=re.S)
    if m:
        return "[!] " + m.group(1).strip()
    t = re.sub(r"\[Image: source: [^\]]+\]", "[screenshot]", t)
    t = re.sub(r"<image name=[^>]*>", "[screenshot] ", t)
    t = re.sub(r"^Caveat: The messages below.*?</command-message>", "", t, flags=re.S)
    t = re.sub(r"^<tx-command-prompt[^>]*>", "[pane] ", t)
    t = re.sub(r"</tx-command-prompt>\s*$", "", t)
    t = re.sub(r'^<from-agent session="([^"]+)">', r"[peer \1] ", t)
    t = re.sub(r'^<from-claude session="([^"]+)">', r"[peer \1] ", t)
    t = re.sub(r"</from-(agent|claude)>\s*$", "", t)
    return t.strip()


PEER_RX = re.compile(r"^\s*<from-(agent|claude)\b")
SYS_RX = [re.compile(r) for r in (
    r"^\s*\[SYSTEM NOTIFICATION", r"^\s*<task-notification>", r"^\s*<system-reminder>",
    r"^\s*<local-command-caveat", r"^\s*<bash-stdout", r"^\s*<bash-stderr",
    r"^\s*Caveat: The messages below", r"^\s*<local-command-stdout>",
    r"^\s*\[Request interrupted", r"^\s*This session is being continued from",
    r"^\s*<(environment_context|user_instructions|permissions|turn_context|ENVIRONMENT|turn_aborted)",
    r"^\s*# AGENTS\.md",
)]


def ukind(raw):
    """u = genuinely the human; p = peer-agent mail; s = system/injected."""
    if PEER_RX.match(raw):
        return "p"
    for rx in SYS_RX:
        if rx.match(raw):
            return "s"
    return "u"


def cap(t, n):
    t = t.strip()
    return t[:n] + f" …[+{len(t) - n} chars]" if len(t) > n else t


def tool_line(name, detail):
    d = re.sub(r"\s+", " ", detail or "").strip()
    return f"⚙ {name} · {cap(d, TOOL_DETAIL)}" if d else f"⚙ {name}"


def _iso_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _rows_from_claude(o, out):
    if o.get("isSidechain"):
        return
    ts, typ = o.get("timestamp"), o.get("type")
    if not ts or typ not in ("user", "assistant"):
        return
    t = _iso_ts(ts)
    if t is None:
        return
    c = (o.get("message") or {}).get("content")
    if typ == "user":
        # ONE row per user message: a screenshot paste arrives as several text blocks (typed text
        # + image markers) — emitting them separately doubled the dots and drawer rows.
        if isinstance(c, str):
            raw = c
        elif isinstance(c, list):
            raw = "\n".join(
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            )
        else:
            raw = ""
        if raw:
            k = ukind(raw)
            txt = clean_user(raw)
            if txt:
                # a screenshot paste is TWO transcript entries at the identical timestamp (typed
                # text + marker-only) — fold the second into the first instead of doubling the dot
                if out and k == "u" and out[-1][1] == "u" and out[-1][0] == t:
                    out[-1][2] = cap(out[-1][2] + "\n" + txt, ROW_TEXT_CAP)
                else:
                    out.append([t, k, cap(txt, ROW_TEXT_CAP), None])
    elif isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    out.append([t, "a", cap(txt, ROW_TEXT_CAP), None])
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                det = inp.get("command") or inp.get("file_path") or inp.get("prompt") \
                    or inp.get("description") or ""
                out.append([t, "x", tool_line(b.get("name", "?"), str(det)),
                            milestone_probe(str(det))])


def _rows_from_codex(o, out):
    t = _iso_ts(o.get("timestamp")) if isinstance(o.get("timestamp"), str) else None
    if t is None:
        return
    p = o.get("payload") or {}
    pt = p.get("type")
    if pt == "message":
        parts = [it.get("text", "") for it in p.get("content") or []
                 if isinstance(it, dict) and it.get("type") in ("input_text", "output_text")]
        txt = "\n".join(parts).strip()
        if not txt:
            return
        if p.get("role") == "user":
            k = ukind(txt)
            txt = clean_user(txt)
            if txt:
                out.append([t, k, cap(txt, ROW_TEXT_CAP), None])
        elif p.get("role") == "assistant":
            out.append([t, "a", cap(txt, ROW_TEXT_CAP), None])
    elif pt in ("function_call", "local_shell_call"):
        name, det = p.get("name") or "shell", ""
        args = p.get("arguments")
        if isinstance(args, str):
            try:
                aj = json.loads(args)
                cmd = aj.get("command")
                det = cmd if isinstance(cmd, str) else " ".join(cmd) if isinstance(cmd, list) else args[:200]
            except Exception:
                det = args[:200]
        act = p.get("action") or {}
        if not det and isinstance(act, dict):
            cmd = act.get("command")
            det = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
        out.append([t, "x", tool_line(name, det), milestone_probe(det)])


def _parse_line(line, out):
    try:
        o = json.loads(line)
    except Exception:
        return
    if not isinstance(o, dict):
        return
    (_rows_from_codex if "payload" in o else _rows_from_claude)(o, out)


# ----- incremental transcript cache --------------------------------------------------------------

class _TranscriptCache:
    """Rows per transcript, tail-parsed.  JSONL transcripts are append-only in the normal case;
    a shrink or in-place rewrite (size mismatch below the recorded offset) forces a full reparse.
    ``offset`` tracks bytes consumed through the last complete newline, so a partially flushed
    trailing line is left for the next poll rather than half-parsed."""

    def __init__(self):
        self._entries = {}

    def rows(self, path):
        key = str(path)
        try:
            st = path.stat()
        except OSError:
            self._entries.pop(key, None)
            return []
        e = self._entries.get(key)
        if e and e["size"] == st.st_size and e["mtime"] == st.st_mtime:
            return e["rows"]
        if e and st.st_size >= e["offset"]:
            start, rows = e["offset"], e["rows"]
        else:
            start, rows = 0, []
        try:
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read()
        except OSError:
            self._entries.pop(key, None)
            return []
        nl = chunk.rfind(b"\n")
        consumed = nl + 1 if nl >= 0 else 0
        for line in chunk[:consumed].decode("utf-8", errors="replace").splitlines():
            _parse_line(line, rows)
        self._entries[key] = {
            "size": st.st_size, "mtime": st.st_mtime, "offset": start + consumed, "rows": rows,
        }
        return rows


_CACHE = _TranscriptCache()
_LOCK = threading.Lock()  # ThreadingHTTPServer: one build at a time, cache mutations included

# ----- user-dragged group order (mirrors sessions-graph's layout persistence) --------------------
# The page reorders groups by drag&drop; the full sequence is saved here and applied at build time.
# Atomic replace + tolerant read: absent/corrupt reads as "no custom order" (chronological).
ORDER_FILE = tx_ide_home() / "timeline.group-order.json"
_ORDER_LOCK = threading.Lock()


def _load_group_order():
    try:
        value = json.loads(ORDER_FILE.read_text())
    except (OSError, ValueError):
        return []
    return [k for k in value if isinstance(k, str)] if isinstance(value, list) else []


def save_group_order(order):
    with _ORDER_LOCK:
        tmp = ORDER_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(order, ensure_ascii=False))
        os.replace(tmp, ORDER_FILE)


# ----- payload build -----------------------------------------------------------------------------

def _transcript_paths(session):
    """First existing candidate per chat: the recorded live path, else the ingested bundle copy
    (mirrors server.py's `_transcript_candidates` preference order)."""
    found = []
    for chat in session.chats:
        candidates = []
        if chat.transcript_path:
            candidates.append(Path(chat.transcript_path))
        if chat.id is not None:
            if chat.bundle_path:
                candidates.append(Path(chat.bundle_path) / claude_engine.BUNDLE_TRANSCRIPT_NAME)
            else:
                candidates.append(claude_engine.bundle_transcript_path(session.id, chat.id))
        for c in candidates:
            if c.is_file():
                found.append(c)
                break
    return found


def _local_midnight(d):
    return datetime.datetime.combine(d, datetime.time(0)).astimezone()


def _cube_to_hex(cube_index):
    """xterm-256 index -> #rrggbb (same mapping as server.py's tag chips)."""
    if 16 <= cube_index <= 231:
        offset = cube_index - 16
        steps = (offset // 36, (offset // 6) % 6, offset % 6)
        return "#" + "".join(f"{0 if s == 0 else 55 + 40 * s:02x}" for s in steps)
    grey = 8 + 10 * (cube_index - 232)
    return f"#{grey:02x}{grey:02x}{grey:02x}"


def _group_color(name):
    return _cube_to_hex(tag_cube(name))




def _lanes(cut_ts):
    """Window-filtered llm records merged into lanes (records sharing a transcript path are one
    resume chain), same-name dead lanes disambiguated with a @MonDD suffix."""
    store = SessionStore()
    everyone = store.all()
    names = {s.id: s.name for s in everyone}
    artifacts = ArtifactStore().all()
    resolver = GroupResolver(everyone, artifacts)
    records = [
        s for s in everyone
        if s.role == Role.LLM and "temporary" not in s.tags
        and (s.activity_at >= cut_ts or s.created_at >= cut_ts)
    ]
    lanes = []
    for rec in sorted(records, key=lambda s: s.created_at):
        paths = {c.transcript_path for c in rec.chats if c.transcript_path}
        for lane in lanes:
            if lane["name"] == rec.name and (paths & lane["tpaths"] or not paths and not lane["tpaths"]):
                lane["recs"].append(rec)
                lane["tpaths"] |= paths
                break
        else:
            lanes.append({"name": rec.name, "recs": [rec], "tpaths": set(paths)})
    byname = {}
    for lane in lanes:
        byname.setdefault(lane["name"], []).append(lane)
    for name, group in byname.items():
        if len(group) > 1:
            for lane in group:
                if not any(r.is_alive() for r in lane["recs"]):
                    d = datetime.datetime.fromtimestamp(lane["recs"][0].created_at).astimezone()
                    lane["name"] = f"{name}@{d.strftime('%b%d')}"
    return lanes, names, resolver, artifacts


def _lane_rows(lane):
    """All parsed rows for a lane's merged records, time-sorted, paths deduplicated."""
    rows, seen = [], set()
    for rec in lane["recs"]:
        for p in _transcript_paths(rec):
            if str(p) not in seen:
                seen.add(str(p))
                rows.extend(_CACHE.rows(p))
    return sorted(rows, key=lambda r: r[0])


def timeline_payload(days=7.0):
    with _LOCK:
        return _build(days)


def lane_dialogue(session_id, days=7.0):
    """The drawer feed: every parsed row of the lane containing `session_id`, timestamps in
    epoch SECONDS (the client converts against the payload's epoch_ms).  None -> 404."""
    with _LOCK:
        now = datetime.datetime.now().astimezone()
        cut_ts = _local_midnight((now - datetime.timedelta(days=days)).date()).timestamp()
        lanes, _names, _resolver, _artifacts = _lanes(cut_ts)
        for lane in lanes:
            recs = lane["recs"]
            if not any(r.id == session_id for r in recs):
                continue
            rows = _lane_rows(lane)
            live = any(r.is_alive() for r in recs)
            primary = max(recs, key=lambda r: (r.is_alive(), r.activity_at))
            state = primary.state.value if live else "killed"
            nmsg = sum(1 for r in rows if r[1] in "upa")
            return {
                "sid": primary.id,
                "name": lane["name"],
                "meta": f"{state} · {nmsg} msgs",
                "rows": [[round(t, 2), k, txt] for t, k, txt, _probe in rows],
            }
        return None


def _build(days):
    now = datetime.datetime.now().astimezone()
    epoch_dt = _local_midnight((now - datetime.timedelta(days=days)).date())
    epoch_ts = epoch_dt.timestamp()
    cut_ts = epoch_ts

    def mins(ts):
        return (ts - epoch_ts) / 60.0

    lanes, names, resolver, artifacts = _lanes(cut_ts)
    # creation touches by creator session — resolves `tx artifact create` diamonds to their artifact
    created = {}
    art_titles = {}
    for art in artifacts:
        art_titles[art.id] = art.title or art.filename
        if art.history:
            created.setdefault(art.history[0].session_id, []).append((art.history[0].at, art.id))


    sessions, lane_peers, spawns = [], [], []
    for lane in lanes:
        recs = lane["recs"]
        tags = sorted({t for r in recs for t in r.tags})
        live = any(r.is_alive() for r in recs)
        primary = max(recs, key=lambda r: (r.is_alive(), r.activity_at))
        kill_ts = None if live else max((r.ended_at or r.activity_at) for r in recs)
        engines = sorted({
            (c.engine.value if getattr(c, "engine", None) is not None else "?")
            for r in recs for c in r.chats
        }) or ["?"]

        rows = _lane_rows(lane)

        times = [r[0] for r in rows]
        if not times:
            a0 = min(r.created_at for r in recs)
            b0 = max(r.activity_at for r in recs)
            times = [a0, max(b0, a0 + 120)]

        def _segments(gap_seconds):
            segs, s0, prev = [], times[0], times[0]
            for t in times[1:]:
                if t - prev > gap_seconds:
                    segs.append([s0, max(prev, s0 + 60)])
                    s0 = t
                prev = t
            segs.append([s0, max(prev, s0 + 60)])
            return [[max(mins(a), 0.0), mins(b)] for a, b in segs if mins(b) >= 0]

        vis_segs = _segments(SEG_GAP_MIN * 60)
        vis_fsegs = _segments(FSEG_GAP_MIN * 60)

        ev, permin = [], {}
        for t, k, txt, probe in rows:
            m = mins(t)
            if m < 0:
                continue
            if k == "u":
                key = int(m)
                permin[key] = permin.get(key, 0) + 1
                if permin[key] > BURST_PER_MIN:
                    if permin[key] == BURST_PER_MIN + 1:
                        ev.append([round(m, 2), "u", "(burst: more messages this minute — open chat)"])
                    continue
                ev.append([round(m, 2), "u", cap(txt, EV_TEXT)])
            elif k == "x" and probe:
                # the probe is the RAW command from the milestone verb onward (`milestone_probe`),
                # never the capped display line — that is what makes a buried action visible
                for rx, lab in M_RX:
                    if not rx.search(probe):
                        continue
                    entry = [round(m, 2), "m", cap(f"{lab}: {probe}", EV_TEXT)]
                    if lab == "artifact":
                        um = _UUID_RX.search(probe)
                        # a uuid in the command that is not an artifact id is a path (the scratchpad
                        # carries the CHAT uuid) — linking the diamond to it would 404
                        aid = um.group(0).lower() if um and um.group(0).lower() in art_titles else None
                        if aid is None:
                            pm = _SHORT_ID_RX.search(probe)
                            if pm:
                                hits = [k for k in art_titles if k.startswith(pm.group(1).lower())]
                                if len(hits) == 1:   # ambiguous prefixes stay unresolved
                                    aid = hits[0]
                        if aid is None:   # a create: nearest same-lane creation touch in time
                            best = None
                            for rec in recs:
                                for at, art_id in created.get(rec.id, ()):
                                    d = abs(at - t)
                                    if d <= CREATE_MATCH_S and (best is None or d < best[0]):
                                        best = (d, art_id)
                            aid = best[1] if best else None
                        verb = "modified" if "artifact modify" in probe else "created"
                        if aid:
                            # preview reads as what happened to WHICH artifact, not the raw command
                            title = art_titles.get(aid, aid[:8])
                            entry[2] = cap(f"artifact {verb}: {title}", EV_TEXT)
                            entry.append(aid)
                        else:
                            entry[2] = cap(f"artifact {verb}: {probe}", EV_TEXT)
                    ev.append(entry)
                    break

        for ri, rec in enumerate(recs):
            sm = mins(rec.created_at)
            if sm < 0 or not rec.parent:
                continue
            spawns.append([round(sm, 2), rec.parent, len(sessions), "spawn" if ri == 0 else "resume"])

        peers = []
        for t, k, txt, _probe in rows:
            m = mins(t)
            if m < 0 or k != "p":
                continue
            pm = re.match(r"\[peer ([^\]]+)\]\s*(.*)", txt, flags=re.S)
            if pm:
                peers.append([round(m, 2), pm.group(1).strip(), cap(pm.group(2), 120)])
        lane_peers.append(peers)

        first_u = next((r[2] for r in rows if r[1] == "u"), "")
        nmsg = sum(1 for r in rows if r[1] in "upa")
        state = primary.state.value if live else "killed"
        sessions.append({
            "_pi": len(sessions),
            "sid": primary.id,
            "id": lane["name"],
            "g": resolver.session_group(primary),
            "meta": f"{'/'.join(engines)} · {state} · {nmsg} msgs",
            "blurb": cap(first_u, BLURB_CAP) or "(no user messages recorded)",
            "state": state,
            "sys": 1 if ("tx-system" in tags or any(r.name == ASSISTANT_NAME for r in recs)) else 0,
            "live": 1 if live else 0,
            "kill": round(mins(kill_ts), 2) if kill_ts is not None else None,
            "a": round(max(mins(times[0]), 0.0), 2),
            "b": round(mins(times[-1]), 2),
            "segs": [[round(a, 2), round(b, 2)] for a, b in vis_segs] or [[0.0, 1.0]],
            "fsegs": [[round(a, 2), round(b, 2)] for a, b in vis_fsegs] or [[0.0, 1.0]],
            "ev": ev,
        })

    # peer-message edges: resolve the envelope's sender token (display name, base name before a
    # @MonDD suffix, or record uuid) to a lane; -1 = sender has no lane (tx-assistant, out of window)
    resolve = {}
    for idx, lane in enumerate(lanes):
        for rec in lane["recs"]:
            resolve[rec.id] = idx
    for idx, lane in enumerate(lanes):
        resolve.setdefault(lane["name"], idx)
        resolve.setdefault(re.sub(r"@\w+$", "", lane["name"]), idx)
    edges = [
        [m, resolve.get(token, -1), to_idx, token, snippet, "msg"]
        for to_idx, peers in enumerate(lane_peers) for m, token, snippet in peers
    ]
    for m, parent_id, to_idx, kind in spawns:
        edges.append([m, resolve.get(parent_id, -1), to_idx,
                      names.get(parent_id, parent_id[:8]), "", kind])

    sessions.sort(key=lambda s: s["a"])
    perm = {s["_pi"]: i for i, s in enumerate(sessions)}
    for s in sessions:
        del s["_pi"]
    edges = sorted(
        [[m, perm[f] if f >= 0 else -1, perm[t], token, snip, kind]
         for m, f, t, token, snip, kind in edges],
        key=lambda e: e[0],
    )
    gmin = {}
    for s in sessions:
        gmin.setdefault(s["g"], s["a"])
    groups = [
        {"k": k, "color": _group_color(k)}
        for k in sorted(gmin, key=lambda k: gmin[k])
    ]
    saved = _load_group_order()
    if saved:
        pos = {k: i for i, k in enumerate(saved)}
        groups.sort(key=lambda g: pos.get(g["k"], len(pos)))   # stable: unknown groups keep chrono order after

    day_list, i = [], 0
    while True:
        d = _local_midnight(epoch_dt.date() + datetime.timedelta(days=i))
        if d > now:
            break
        day_list.append({"o": round(mins(d.timestamp()), 1),
                         "label": d.strftime("%a %b %d"),
                         "wk": 1 if d.weekday() >= 5 else 0})
        i += 1

    # t1 is DATA-max only — the client extends the viewport to its own clock.  Including "now"
    # here would change the payload every poll and defeat the refresh tick's fingerprint.
    t1 = max([s["b"] for s in sessions] + [d["o"] for d in day_list] + [0.0])
    return {
        "epoch_ms": int(epoch_ts * 1000),
        "t1": round(t1, 1),
        "days": day_list,
        "groups": groups,
        "sessions": sessions,
        "edges": edges,
    }


if __name__ == "__main__":
    import time as _time
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 7.0
    t0 = _time.monotonic()
    p1 = timeline_payload(days)
    t1 = _time.monotonic()
    p2 = timeline_payload(days)
    t2 = _time.monotonic()
    ev = sum(len(s["ev"]) for s in p1["sessions"])
    size = len(json.dumps(p1, ensure_ascii=False))
    print(f"lanes={len(p1['sessions'])} groups={len(p1['groups'])} events={ev} "
          f"payload={size / 1e3:.0f}KB cold={1000 * (t1 - t0):.0f}ms warm={1000 * (t2 - t1):.0f}ms")
    stable = json.dumps(p1, sort_keys=True) == json.dumps(p2, sort_keys=True)
    print(f"fingerprint stable across idle rebuilds: {stable}")
