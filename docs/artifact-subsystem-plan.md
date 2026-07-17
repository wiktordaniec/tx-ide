# Plan 2 — Artifact subsystem

Add a first-class, durable **Artifact** object: a record + its file(s) under `$TX_IDE_HOME`, with
snapshot-based linear version history, session-touch provenance ("who created / who touched"), and
multi-surface rendering. This is **net-new capability** — Plan 1 (the session-record split) is
**merged** (`refactor/session-record-split`, session `SCHEMA_VERSION = 4`) — and reuses the
existing `ChatRef`/`Origin` provenance pattern generalized from immutable chat transcripts to
mutable, versioned files.

---

## Why

Sessions produce durable content — plans, question sets, docs, diffs — that **outlives the session
that made it** and is touched by many sessions over its life. We want that content tracked,
versioned, hostable, and provenance-linked. Motivating flow:

> Session A creates a plan. Session B reviews it and generates a questions file to bulletproof the
> plan. The user answers. B then modifies the plan from those answers. We want to see, for that
> plan: what created it, and everything that touched it.

## Model

```
Artifact — record at $TX_IDE_HOME/artifacts/<id>.json ; files under $TX_IDE_HOME/artifacts/<id>/
    id · title? · filename · created_at · artifact_schema_version
    history: [ {session_id, at, rev, changes?} ]   # entry 0 = create; provenance + versions in ONE list
                                                   #   Touch.rev → revs/<rev>.<ext>  (full-copy snapshot)
```

Generic object of **any type** — no `type` discriminator (settled). No `how` on the touch edge —
just `session_id` + `at` (timestamp) + `rev` (the snapshot this touch produced — explicit, not
positional) + optional `changes` (a free-text note of what changed). Touch edges and revisions are
the **same list**: every touch is a new snapshot. `session_id` may be the sentinel `"user"` — a
manual edit by the human (or a `tx artifact` call outside any tx session) is a first-class touch
(settled). `updated_at` is **derived** from `history[-1].at`, never stored (no drift). `filename`
preserves the original name so revs keep their extension (nvim filetype, future mime). No
structured annotations — review comments are plain text in the content (see Open items).

### The ChatRef/Origin parallel (prior art to reuse)

| chats (exists today) | artifacts (this plan) |
|---|---|
| `history/<tx>/<chat>/` durable bundle (immutable) | `artifacts/<id>/…` files (mutable, versioned) |
| `Origin{how, session_id, chat_id}` | history entry `{session_id, at, rev, changes?}` (no `how`) |
| `ChatRef.role` original/fork/rollover | `derived_from` (artifact↔artifact) — **deferred** |
| walk `session_id` → chats a session touched | `artifacts_for_session(session_id)` (by query) |
| (transcript append-only) | **+ snapshot revision history (new)** |

## Change tracker — snapshots (settled)

- Each revision is a **full copy** of the file at that point (not a diff). **Linear history, no
  git — and linearity is enforced, not assumed:** `revs/<n>.<ext>` is created with `O_EXCL`, so of
  two concurrent `modify`s the loser gets a clear conflict error ("artifact moved on — re-read and
  retry"), never a silent overwrite or a lost touch.
- Layout: `artifacts/<id>/current.<ext>` is the **working copy** — what `open` edits and what
  reading the artifact returns. `artifacts/<id>/revs/<n>.<ext>` are **immutable** snapshots
  (v0 = create, v1, …); `modify` snapshots the new content into the next rev and refreshes
  `current`. Editing a rev file directly is corruption (write discipline — see Enforcement).
- Rev and `current` writes use the same temp-file + `os.replace` atomicity as the record.
- **The record is authoritative.** A crash between rev-write and record-save leaves an orphan rev
  file: ignored on read (the record's `history` defines what exists) and overwritten by the next
  `modify`. `tx artifact doctor` reports orphans.
- Content policy: **accept any bytes** — no type or size gate; fix it when it hurts (settled). The
  record itself stays utf-8 JSON, so metadata is always LLM-readable regardless of content.
- No-op guard: a `modify` with content identical to the last rev is skipped — no rev, no touch, a
  printed notice.
- To *show* what changed between two revisions, compute on demand with stdlib `difflib` — we store
  versions, not diffs. `diff` refuses (cleanly) when either rev does not decode as utf-8.
- **Reversible:** the `history[]` schema is identical if the snapshot backing is ever swapped for a
  git repo (rev → sha). Git buys 3-way *merge* + blame/log; the `O_EXCL` conflict error is enough
  while concurrent editing stays rare. Revisit if it stops being rare.
- Storage note: N edits = N full copies. Negligible for text; if artifacts ever get large/binary
  *and* heavily re-edited, content-address revisions by sha to dedupe.

## Components (new)

Mirror the existing `session.py` + `store.py` + `service.py` split.

### `lib/tx/artifact.py` — the entity
- `Artifact` dataclass: `id, title, filename, created_at, history: list[Touch],
  artifact_schema_version`. `updated_at` is a derived property (`history[-1].at`), not a field.
- `Touch` (history entry): `session_id: str` (session id or the `"user"` sentinel), `at: float`,
  `rev: int`, `changes: str | None`.
- `to_dict` / `from_dict` with a strict boundary (`ARTIFACT_SCHEMA_VERSION = 1`), mirroring
  `Session.from_dict` (own version line, independent of the session schema). Beyond the version
  check, `from_dict` validates the invariants: non-empty `history`, entry 0 = the create, rev
  numbers contiguous from 0.

### `lib/tx/artifact_store.py` — persistence
- `ArtifactStore` mirroring `SessionStore`: `save` (atomic temp-file + `os.replace`), `load`,
  `all` (glob `artifacts/*.json`), `query`. Uuid-sharded record files, same no-global-lock
  discipline.
- **No `delete` — settled.** Nothing in the system removes durable content; discarding an artifact
  is a manual `rm -r artifacts/<id>*` escape hatch, outside the API on purpose.
- Content lives beside the record under `artifacts/<id>/` (`current.<ext>` + `revs/<n>.<ext>`); the
  store owns record I/O, a small content helper owns working-copy/rev I/O.

### `lib/tx/storage.py` — layout helper
- Add `artifacts_dir()` = `$TX_IDE_HOME/artifacts/` alongside `sessions_dir()` / `history_dir()`,
  and export it from `tx/__init__.py`.

### `lib/tx/artifact_service.py` — the use-case core (or fold into `service.py`)
- `create(session_id, content, *, title=None, filename=None) -> Artifact` — write `revs/0.<ext>` +
  `current.<ext>`, append `Touch(rev=0)`, save record. (`session_id` from `$TX_SESSION_ID` /
  current tmux session, else the `"user"` sentinel.)
- `modify(artifact_id, session_id, content, *, changes=None) -> Artifact` — snapshot to the next
  `revs/<n>.<ext>` (`O_EXCL`; conflict error if it exists), refresh `current`, append
  `Touch(rev=n)`, save. Identical content → no-op skip.
- Readers trimmed to what the CLI and `open` consume: `content(artifact_id, rev=None)` (the working
  copy by default — one function, not `latest_content` + `revision_content`) and
  `content_path(artifact_id)` (the `current.<ext>` path, for `open`). No `touched_by` — that is
  just `artifact.history`.
- `diff(artifact_id, a, b) -> str` — `difflib` between two revs.
- **Both directions of session⇄artifact are first-class.** Session → artifacts:
  `artifacts_for_session(session_id)` — reverse lookup **by query** across the store (not
  denormalized onto the session — keeps session writes non-chatty, matching the aversion at
  `service.live_sessions`). Artifact → sessions: writers straight from `history`; **reads** are
  deliberately not history touches — they surface via the `artifact_id` back-link on view sessions
  and an EventLog line on `open`/content reads, visible without polluting the version history.
- Every mutation logs one line via the existing `EventLog` (mirror `SessionService`'s D8
  chokepoint); `open` and content reads log too (read-visibility without history noise).

### Enforcement — keeping agents on the logic path

Agents could bypass `tx artifact` with hand-rolled bash; three layers keep them honest:
1. **Convention** — the role files gain an artifacts section (COMMON.md): all artifact operations
   go through `tx artifact …` / the Python API; a direct write under `artifacts/` is corruption.
2. **Detection** — the record is authoritative; a cheap invariant check (revs on disk ⇄ `history`
   entries, mutations ⇄ EventLog lines) flags out-of-band writes. Run it in tests and expose it as
   `tx artifact doctor`.
3. **Containment** — read-only workers already sit inside the fail-closed sandbox and cannot write
   `$TX_IDE_HOME` at all; for writable workers, convention + detection is the accepted trust model
   (the same one the session store lives with today).

## CLI surface — `tx artifact …`

Minimal set (settled) — each verb backs one step of the motivating flow, nothing speculative:
- `tx artifact create <file> [--title T]` — new artifact from a file; current session = creator.
- `tx artifact modify <id> [<file>] [--changes "..."]` — new revision; **without `<file>` it
  snapshots the working copy** (the one-command close after editing `current` in the nvim view).
- `tx artifact ls [--session S]` — list artifacts (title, id, #revs, last touched); `--session`
  exposes the reverse lookup (what a session touched).
- `tx artifact show <id>` — metadata + the full touch/version log. (Absorbs the earlier separate
  `history` verb — one verb, not two.)
- `tx artifact diff <id> [<revA> <revB>]` — `difflib` diff (default: last two).
- `tx artifact open <id>` — spawn the tmux view (see below).

Session id is resolved from `$TX_SESSION_ID` (exported into every tx session) or the current tmux
session — never passed by hand. Outside any tx session (a plain terminal) the actor falls back to
the `"user"` sentinel; artifact commands never hard-fail on missing session context. No `delete`
verb (settled — manual `rm` is the escape hatch); `tx artifact doctor` (see Enforcement) rounds
out the set.

## Session binding — the tmux view

- The **tmux/nvim view of an artifact is just an nvim session opened on the artifact's working
  copy** — no transform (settled: "view the raw file"). `tx artifact open <id>` spawns an nvim
  companion via the existing `tx spawn-nvim` path, `--open content_path(id)` (= `current.<ext>`,
  never a frozen rev), carrying an `artifact_id` back-link. Its tags: inherited from the invoking
  session, `--tag` overrides, bare `artifact` when invoked outside tx (settled).
- Session typing (settled): an `OtherSession` with one nullable `artifact_id` field — no new
  subtype. `OtherSession` exists since the session-record split; the field costs a session-schema
  v4→v5 bump when step 3 lands.
- **Capture of edits:** for now, a session records a touch **explicitly** via `tx artifact modify`
  (or the Python API the agent calls). Auto-capture (watch the file / hook on write) is a later
  enhancement — do not build it first.
- **Dirty working copy is a visible state, not an error.** Edits in the nvim view land in
  `current` and are un-snapshotted until a `modify`; `show` and `doctor` flag "current differs
  from last rev". The no-file `tx artifact modify <id>` form makes closing the loop one command.
  Accepted quirk: `current` itself is last-save-wins between simultaneous writers (user in nvim +
  an agent) — the `O_EXCL` guard protects revs, and revs are the safety net.

## Rendering — the artifact browser (settled scope; mobile deferred)

Settled: one simple, **read-only browser website** for the artifacts is wanted; everything else
(mobile, write-from-web) stays deferred. It is a prototype against the store, built after the core
lands — never coupled into `lib/tx`.

- `prototypes/artifact-browser/` in the house prototype style (`prototypes/sessions-graph`,
  `prototypes/remote-control`): a stdlib `ThreadingHTTPServer` `server.py` + a single
  `index.html`, reading through the real `ArtifactStore` (no second source of truth), store
  re-read on every request. Run: `python3.14 prototypes/artifact-browser/server.py [PORT]`.
- Scope, minimal: **list view** — title, id, filename, #revs, last touched, touch authors;
  **detail view** — the working copy (rendered when markdown, else preformatted text; binary =
  download link), the full touch/version log, and an on-demand rev↔rev `difflib` diff.
- Read-only in the strict sense: it never writes, and its reads do **not** log to EventLog — a
  polling dashboard would be noise. (EventLog read-visibility covers session/CLI reads, not this.)
- It is **not a session** and not a stored rendering — a live render over the store.
- Naming: the ephemeral tmux **View** (from the session-record split) is a different concept from an artifact's rendering
  *surface*. Use "render/surface" for artifacts to avoid overloading "view".

## Schema / versioning

- `ARTIFACT_SCHEMA_VERSION = 1`, strict `from_dict` boundary (mirror `Session`). Independent of the
  session schema and its own version line.
- Bump policy: the strict boundary means **any** record-shape change — field added, removed, or
  re-meaned — bumps the version; there are no tolerated unknown fields. Planned bumps: none —
  artifact schema stays 1 through all planned steps. The session schema takes exactly one
  artifact-driven bump, v4→v5, for `OtherSession.artifact_id` (step 3).
- Artifacts are a separate store. The one touch-point with session records is the `artifact_id`
  back-link on `OtherSession` (sequencing step 3) — a session-schema v4→v5 bump, nothing else.

## Testing strategy

Hermetic against a temp `$TX_IDE_HOME` (mirror `tests/test_capture.py`'s bootstrap; directly
runnable, no pytest):

1. `create` → `revs/0.<ext>` + `current.<ext>` written + record with one `Touch(rev=0)`;
   `content` matches; extension preserved from the source filename.
2. `modify` → `revs/1.<ext>` written, `current` refreshed, `history` has two entries,
   derived `updated_at` advanced, `content` = v1. Identical content → no-op (no rev, no touch).
3. `diff(id, 0, 1)` returns a sane `difflib` output; `content(id, rev=n)` returns each version;
   `diff` refuses cleanly on non-utf-8 content.
4. `history` reflects the two sessions (incl. a `"user"`-sentinel touch);
   `artifacts_for_session` reverse-resolves by query.
5. Round-trip `Artifact.from_dict(to_dict())`; strict boundary rejects a bad version, empty
   history, and non-contiguous revs.
6. Concurrency + crash: two racing `modify`s → exactly one wins, loser gets the conflict error;
   a hand-planted orphan `revs/<n>` is ignored on read, flagged by `doctor`, overwritten by the
   next `modify`.
7. `tx artifact open <id>` spawns an nvim session bound to `artifact_id` (fake tmux), opened on
   `current.<ext>`, tags inherited from the invoker.
8. **Agent conformance (e2e, non-hermetic)** — spawn a primed worker with the updated role files
   (the COMMON.md artifacts section from Enforcement §1), instruct it to create + modify an
   artifact, and assert: record + revs + history correct, EventLog lines present, `doctor` clean.
   A separate script beside the hermetic suite — it costs a real agent run.

## Suggested sequencing

1. **Core** — `artifact.py` + `artifact_store.py` + `artifacts_dir()` (incl. `ensure_home()`) +
   `create`/`modify`/`content`/`diff` + `from_dict` + tests. (Usable from Python; no CLI yet.)
2. **CLI** — `tx artifact create/modify/ls/show/diff` + the COMMON.md artifacts section
   (Enforcement §1: agents learn the logic path when the CLI exists).
3. **Session binding** — `tx artifact open` (nvim view + `artifact_id`).
4. **Dogfood** — import this plan and its questionnaire as the first two artifacts and replay the
   motivating flow for real (create → review → user answers in the view → no-file `modify`);
   test 8 replays the same flow scripted.
5. **Browser** — `prototypes/artifact-browser/` (read-only list + detail + diff).
6. **Later** — the plan→questions `derived_from` edge; mobile; auto-capture of edits.

## Implementation handoff (build worker + codex QA)

The plan is **fully settled** — every section above carries its resolution, and the questionnaire
(`docs/artifact-plan-questions.md`, kept beside this file) is the decision record with the
rationale and rejected alternatives. Do not relitigate settled items; a genuinely new blocker goes
to the user, not into a unilateral redesign.

- **Build worker** — standard DEVELOPER flow: own tx worktree, branch `feat/artifact-subsystem`,
  sequencing steps 1–3 in order with atomic commits, draft PR + `-diff` nvim companion when open.
  Mirror the `session.py` / `store.py` / `service.py` house patterns named in Components. Tests
  are directly-runnable scripts (no pytest), hermetic against a temp `$TX_IDE_HOME`.
- **codex QA worker** — spawned via tx with `--engine codex --read-only` (fail-closed sandbox).
  Charter: (1) conformance review of the diff against this plan, section by section — deviations,
  not taste; (2) run the hermetic suite; (3) exercise the CLI end-to-end against a scratch
  `$TX_IDE_HOME` (temp env var — never the real home, which the sandbox blocks anyway);
  (4) adversarial passes on the settled hard parts: the `O_EXCL` race, orphan-rev crash recovery,
  binary content, no-op modify, tag inheritance on `open`. Deliverable: a findings report to the
  user and the build worker — no edits, no fixes.
- Both workers read this file **and** the questionnaire as their first actions (after the COMMON /
  role files) — the priming prompt must point at both.

## Open items (deliberately deferred)

- **`derived_from`** (plan → questions, artifact↔artifact edge): deferred. Both are independent
  artifacts today; the connection is already visible via the sessions that touched both. Add a
  first-class edge only when there's a need to traverse artifact→artifact.
- **Annotations — cut (settled).** No structured annotation layer, in any phase. Review comments
  are ordinary text inside the content — the existing `# AINote:` convention, typed with the
  user's own vim shortcut into the working copy. Capture is free: the next `tx artifact modify`
  snapshots them, `diff` shows notes appearing and disappearing, and grep finds the open ones —
  the AINote workflow needs no data model. Revisit only if a non-text surface ever needs
  positioned comments.
- **Auto-capture** of a session's edits as a `modify` (vs the explicit CLI/API) — later.
