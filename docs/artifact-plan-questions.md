# Artifact subsystem — decision record (questionnaire, SETTLED)

**Status: every question is answered and folded into `docs/artifact-subsystem-plan.md`.** This
file remains as the decision record: checked boxes are the chosen options, unchecked ones the
rejected alternatives, `notes:` the user's rationale. Implementation and QA workers: read, don't
reopen — a checked box here is settled.

---

## A. Write model & ownership

### A1 — what does `tx artifact open` edit?
Today's plan opens `revs/<latest>` — an nvim edit mutates a frozen snapshot in place.
- [x] separate `artifacts/<id>/current` working file; revs immutable; `modify` snapshots from it
- [ ] open read-only; edits go through an explicit file + `tx artifact modify`
- [ ] latest rev IS the working copy; frozen only when the next rev lands
- notes:

### A2 — canonical home of the content
`create <file>` imports a copy; the motivating plan lives in a repo and keeps being edited there.
- [x] artifact-is-home: repo-tracked files out of scope v1; artifacts hold net-new content
- [ ] mirror: repo file stays home, every edit must be re-imported via `modify` (stale by default)
- notes:

### A3 — write discipline on `artifacts/<id>/`
- [x] `modify` (CLI/API) is the only legal writer; direct edits to rev files = corruption, undefined
- [ ] tolerate direct edits somehow
- notes:

## B. Concurrency & integrity

### B1 — concurrent `modify` of the same artifact
Nothing enforces "edits are sequential"; two sessions can both write `revs/<n>`, one touch lost.
- [x] `O_EXCL` create of `revs/<n>` as the lock + clear conflict error ("artifact moved on, re-read")
- [ ] also add `modify(expected_rev=…)` CAS parameter
- [ ] accept the race for v1, document it
- notes:

### B2 — authority after a crash between rev-write and record-save
- [x] record (`<id>.json`) is authoritative; orphan `revs/<n>` ignored on read. AMENDED after QA
  P0: a claimed slot is never silently overwritten — `modify` hitting one always conflicts;
  `doctor` fix mode owns orphan removal (an in-flight writer is indistinguishable from debris)
- [ ] filesystem (highest n) is authoritative; record repaired on load
- notes:

### B3 — rev-file write atomicity
- [x] same temp-file + `os.replace` discipline as the record store
- [ ] plain write is fine
- notes:

## C. Actors & provenance

### C1 — user (non-session) touches
The motivating flow has "the user answers" — a `Touch` requires `session_id`.
- [x] sentinel actor `"user"` allowed in `Touch.session_id`
- [ ] sessions only; user edits invisible to provenance (documented gap)
- notes: 

### C2 — `tx artifact …` outside any tx session
- [x] falls back to the `"user"` sentinel (consistent with C1)
- [ ] hard error: artifact mutations require a tx session
- notes:

### C3 — decouple touches from revision numbers
Phase-2 annotations will touch without producing a rev, breaking `history[i] ⇄ revs/<i>`.
- [x] add explicit `Touch.rev: int` field now (v0 cost, kills positional coupling)
- [ ] keep positional 1:1; annotations get separate provenance later
- notes:

## D. Content metadata

### D1 — preserve filename/extension
Bare `revs/<n>` breaks nvim filetype detection and renderer mime.
- [x] store original filename on the record; name revs `<n>.<ext>`
- [ ] bare rev files are fine
- notes:

### D2 — binary & size policy
- [ ] v1 is text-only: refuse binary on `create`/`modify` (utf-8 decode check), no size cap
- [ ] accept binary; `diff` refuses on binary; no cap
- [x] other (user's pick, overrides the original pre-check): accept anything — no type or size
  gate; `diff` refuses non-utf-8; "if it becomes a problem I will try to fix it then."

### D3 — encoding
- [ ] utf-8 required (implied by D2 text-only)
- [x] bytes-clean, encoding-agnostic (follows the D2 accept-anything pick; the LLM-readability
  concern lands on the record, which is utf-8 JSON regardless; `diff` refuses non-utf-8 revs)
- notes: is there a value in having utf-8 if we accept any format? The artifacts are specifically
for the LLMs so they should be able to read the metadata.

## E. CLI & session binding

### E1 — tag for the nvim session `tx artifact open` spawns
`spawn-nvim` refuses without `--tag`; an artifact has no scope tag.
- [x] inherit the invoking session's tags; `--tag` flag overrides; bare `"artifact"` when invoked outside tx
- [ ] always require an explicit `--tag` on `open`
- notes:

### E2 — the `artifact_id` back-link on the nvim session
Plan 1 (session-record split) is now MERGED: `OtherSession` exists at session `SCHEMA_VERSION = 4`.
The back-link is a nullable `artifact_id` on `OtherSession` → a v4→v5 session-schema bump.
- [x] plan's lean confirmed: nullable `artifact_id` on `OtherSession`, no new session type; take
      the v5 bump as part of sequencing step 3 (`tx artifact open`)
- [ ] separate `ArtifactViewSession` subtype after all
- [ ] no back-link at all; the artifact's own history suffices
- notes:

### E3 — no-op `modify` (identical content)
- [x] skip: no rev, no touch, print notice
- [ ] record anyway
- notes:

### E4 — `delete` semantics — SETTLED (your comment: no delete functionality)
No `delete` in the store or CLI; discarding an artifact is a manual `rm -r` escape hatch.
Plan updated.

### E5 — `show` vs `history` overlap — SETTLED (your plan comment)
Collapsed: `show` = metadata + full touch log; `history` verb dropped. Plan updated.

## F. Schema

### F1 — `annotations` field in v1 — MOOT (annotations cut, see H)
No field, ever, unless the cut is revisited. Plan updated.

### F2 — `from_dict` invariants
- [x] validate: non-empty history, entry 0 = create, rev numbers contiguous from 0
- [ ] version check only, like today's `Session.from_dict`
- notes:

### F3 — `updated_at`
- [x] derive from `history[-1].at`; don't store (no drift)
- [ ] store it, mirror `Session`
- notes:

## G. Ecosystem fit

### G1 — `tx sync` interaction
Sync is last-writer-wins per key — safe for immutable revs, lossy for the mutable record.
- [x] include `artifacts/` in the corpus; note the record-LWW risk in the plan; revisit at S7
- [ ] exclude `artifacts/` from sync v1
- notes:

### G2 — EventLog coverage — SETTLED (your plan comment: read-visibility wanted)
Mutations AND `open`/content reads log to EventLog; reads stay out of `history` (no rev noise).
Plan updated.

### G3 — `ensure_home()`
- [x] add `artifacts_dir()` to the skeleton it creates
- [ ] leave to first-write `mkdir`
- notes:

### G4 — tests for the hard parts
- [x] add to the plan's test list: concurrent-modify conflict, crash/orphan-rev recovery,
      extension preservation, binary refusal — pinned to the A/B/D answers above
- [ ] happy-path list is enough
- notes:

## H. Annotations — MOOT (settled: annotations cut entirely)

Your call: no structured annotation layer in any phase. AINote-style comments stay plain text in
the content, typed with your vim shortcut into the working copy; the next `modify` snapshots them
(capture is free), `diff` shows them come and go, grep finds open ones. H1–H4 below are void.

### H1 — anchor model
An annotation points into the content; the content then changes underneath it.
- [x] quote-anchored: store the quoted text span + the rev it was made against; renderers re-locate
      the quote in later revs, show as "outdated" when the quote is gone (PR-review model)
- [ ] line-number + rev (simple, breaks silently on drift)
- [ ] no re-location: annotation shown only against its own rev
- notes:

### H2 — lifecycle
AINotes today are deleted when resolved. As data:
- [x] `resolved: bool` (+ who/when); resolved annotations kept — the review trail is provenance
- [ ] delete on resolve, mirroring the AINote convention exactly
- notes:

### H3 — does resolving/adding an annotation count as a Touch?
- [x] no — annotations are their own list with their own author/at; `history` stays revisions-only
      (consistent with C3)
- [ ] yes — any mutation of the record is a touch
- notes:

### H4 — capture path in the nvim surface
Answered: your manual AINotes in the working copy ARE captured — they're content, so the next
`tx artifact modify` snapshots them; no special mechanism needed.
- [x] both: `tx artifact annotate <id> "text" [--anchor "quote"]` as the API, plus a normalizer
      that sweeps AINote-style inline markers out of a modified file into annotation records
      (keeps the muscle memory, content stays clean)
- [ ] CLI/API only; inline markers stay a text convention, never normalized
- [ ] inline markers only
- notes:
