# Plan 1 — Session-record schema split

Split the single `Session` dataclass into a role-discriminated type hierarchy over a shared
base, take **views out of the durable store** (they become ephemeral live-tmux objects), and
rename fields to match their real semantics.

This is a refactor of what already exists. It is independent of Plan 2 (the Artifact subsystem)
and can land on its own.

> **Rev 3** — Rev 2 was a line-by-line cross-check against HEAD (`980ad3d`) and the live
> store/tmux; Rev 3 folds in the operator's answers to all seven open decisions
> (`session-record-split-questions.md`, kept for the record). **Nothing is open — this is
> implementable as written.** Settled: v4 schema bump + migration (Q1); tests are dev-only,
> deleted before merge (Q2); `tx kill` works on views via `@tx_view`, no other view ops (Q3);
> views carry no tags at all (Q4); `last_activity` moves to `LlmSession` (Q5); `tx ls` drops the
> VIEWS section (Q6); mypy/pyright deferred, out of scope (Q7).

---

## Why

- The one `Session` shape glues together genuinely distinct structures. The llm-only fields
  (`engine`, `chats`) are `null`/`[]` for ~half of all records (re-verified at Rev 2: 327 llm vs
  316 non-llm in the live store; **0 non-llm records carry `engine` or `chats`**, and **0 llm
  records have a null `engine`** — the non-optional `LlmSession.engine` is safe).
- The engine/chat/hook machinery already *assumes* llm — it reads `.engine`/`.chats` directly and
  is protected only by runtime guards (e.g. `registry.get(session.engine)` at `hooks.py:200`
  `KeyError`s if `engine` is `None`). The type split makes that a structural impossibility —
  enforced at runtime as a loud `AttributeError` instead of a silent `None`, and by annotations
  for readers. (The repo has no type checker configured; setting up mypy/pyright is wanted but
  explicitly out of scope here — Q7.)
- `State` is already role-partitioned (`State.valid_for`, `session.py:88`) into disjoint sets that
  share only `EXITED` — a de-facto split the types would formalize.
- Views are workspace **containers** (you nest work into them), not units of work. They use none of
  the work fields and are name-keyed, not id-keyed. Re-verified: 12 view records exist (10 `shell`,
  2 `other` — *not* "always a shell home"), `engine: None`, `chats: []`, states: 11 exited + **1
  archived**. **Correction to Rev 1:** a view's record *is* read after death — the live home view
  (`upside-office`) runs against its own ARCHIVED record, which `_is_view_session` resolves on
  every picker nest-attach. That read (and the other record-based view reads listed below) must be
  replaced by the `@tx_view` marker, and the marker must be **stamped onto live views during
  migration** or nest-attach breaks the moment the records are deleted.

## Target model

```
Session (base) — in the store, uuid-keyed, id-named in tmux.  WORK ONLY.
    id · name · state · cwd · initial_cmd · tags · spawn_env
    · parent · pid · attached_to · created_at · ended_at · schema_version
  ├── LlmSession(Session)     engine · chats · last_activity · turn_started_at
  └── OtherSession(Session)   (shell / nvim / other)      # nvim's artifact binding arrives in Plan 2

View — a live tmux object, `@tx_view` marker, chrome, NOT a record, NO tags (Q4: "a simple
layout tmux session"). Fully ephemeral (recreated cheaply — `tx start` recreates `Views`; a
custom home like `upside-office` is respawned by hand via `tx spawn-view`).
```

## Record shapes — before / after, and what migration does to each

Today's store (audited: 656 records, all `schema_version: 3`) holds exactly three shapes; the
split maps them to two record shapes + one non-record. JSON keys `"cmd"`/`"env"` are unchanged
on disk (only the *attributes* rename). **Q1 settled: bump to v4** — the dead keys are dropped by
a real migration, shown below.

**1. llm record — today (v3) → `LlmSession` (v4):**

```jsonc
// BEFORE                                   // AFTER
{                                           {
  "schema_version": 3,                        "schema_version": 4,
  "id": "5fd7…", "name": "auth-review",       "id": "5fd7…", "name": "auth-review",
  "kind": "process",                          //  (kind gone — every record is a process)
  "role": "llm",                              "role": "llm",          // the factory discriminator
  "state": "waiting",                         "state": "waiting",
  "cwd": "…", "cmd": "claude …",              "cwd": "…", "cmd": "claude …",   // attr: initial_cmd
  "engine": "claude",                         "engine": "claude",     // non-optional on the type
  "tags": ["auth"], "env": {…},               "tags": ["auth"], "env": {…},    // attr: spawn_env
  "parent": …, "pid": …,                      "parent": …, "pid": …,
  "attached_to": […],                         "attached_to": […],
  "created_at": …, "ended_at": …,             "created_at": …, "ended_at": …,
  "last_activity": …,                         "last_activity": …,
  "chats": […]                                "chats": […],
}                                             "turn_started_at": null  // new — C5 clock
                                            }
```

**2. non-llm record (shell/nvim/other) — today (v3) → `OtherSession` (v4):** same as above minus
the llm axis — migration **drops** `"kind"`, `"engine": null`, `"chats": []`, and
`"last_activity"` (Q5 — it was always just the spawn time for these, i.e. misleading); no keys
added. (`sync.py`'s last-writer-wins clock reads raw keys with `.get()` fallback to
`created_at`, so the drop is safe there.)

**3. view record — today (v3, `"kind": "view"`) → NO record.** Migration stamps `@tx_view` on the
matching live tmux session (record name ↔ tmux name — views are human-named), then **deletes the
file**. A view's durable identity afterwards is nothing but the live tmux session + marker; dead
view records are deleted outright (no live session to stamp).

Migration is one idempotent `tx migrate` pass (existing `migrations/` pattern, replacing the
spent v2→v3 body): per file — view → stamp-and-delete; process → drop dead keys, stamp v4.
Re-running it is a no-op (v4 records and absent views are skipped).

### Decisions (settled)

- **Base `Session`** keeps the fields uniform consumers read: `id, name, state, cwd, initial_cmd,
  tags, spawn_env, parent, pid, attached_to, created_at, ended_at, schema_version`
  plus behavior `tmux_name`, `is_alive()`, `transition_to()`, `matches()`, and the `activity_at`
  recency property (see the `last_activity` decision below).
- **`LlmSession`** adds `engine: Engine` (non-optional — verified present on all 327 llm records),
  `chats: list[ChatRef]`, `last_activity: float | None` (llm-only now — Q5), and
  `turn_started_at: float | None` (the C5 stuck-`WORKING` clock, moved off `last_activity`).
- **`needs_attention` STAYS on the base** (Rev 2 change). It is already role-gated internally
  (`role == LLM and state == WAITING`) and `sessions-graph`'s `session_payload` (server.py:93)
  reads it on **every** session, pre-llm-filter — moving it to `LlmSession` breaks the graph for
  zero gain.
- **`OtherSession`** adds nothing today (shell/nvim/other work processes).
- **`role`** stays as the on-disk key (the `from_dict` discriminator) and becomes read-only in
  spirit. **Mechanics (Rev 2 — the naive property-on-base version does not run):** a read-only
  `property` on a base dataclass is a data descriptor, so a subclass dataclass *field* of the same
  name raises `AttributeError: can't set attribute` from the generated `__init__`. Therefore:
  - `LlmSession.role` → class-level read-only property returning `Role.LLM`; no field.
  - `OtherSession.role` → a plain dataclass field (nvim/shell/other, set from disk at load),
    treated as immutable by convention (nothing mutates it today; there is no setter to remove).
  - The base declares neither; uniform readers (`render.py`, `messages.py`) duck-type
    `session.role`.
- **`chats` uniformity shim, same mechanics:** the real `chats: list[ChatRef]` field lives on
  `LlmSession` **only**; `OtherSession.chats` is a read-only property returning `[]`. The base
  declares neither. `render.py`/`messages.py`/`sessions-graph` loops that read
  `len(session.chats)` / iterate `session.chats` on every row stay untouched.
- **`kind` is deleted** from the in-memory model and the `Kind` enum removed. Its only non-process
  value was `VIEW`; views leave the store, so every record is a process → `tmux_name` is always
  `id` (the id-vs-name branch at `session.py:256` disappears). On disk the `"kind"` key is
  dropped by the v4 migration (Q1 settled — see Migration).
- **Renames keep their JSON keys** — attribute `cmd` → `initial_cmd` (JSON stays `"cmd"`), attribute
  `env` → `spawn_env` (JSON stays `"env"`).
- **`last_activity` moves to `LlmSession`** (Q5 settled). For non-llm sessions it was always
  just the spawn time (they never enter `record_state` — no hooks fire for them), so keeping it
  on a generic object is misleading; remove it rather than fake it. Uniform consumers get a
  read-only base property **`activity_at`** — `LlmSession`: `last_activity or created_at`;
  `OtherSession`: `created_at` — used by every recency sort (`render._by_recent_activity`,
  `render_history`'s tiebreak, `messages._role_resolver`). The picker/ls IDLE cell renders the
  real `last_activity` for llm rows and `—` for non-llm rows (honest: tx has no activity signal
  there). The llm turn-clock is the separate `LlmSession.turn_started_at`.

## File-by-file changes

Line anchors re-confirmed at `980ad3d`; re-confirm again on implementation.

### `lib/tx/session.py`
- Split `Session` into base `Session` + `LlmSession(Session)` + `OtherSession(Session)` with the
  field/property mechanics above.
- Remove the `Kind` enum and the `kind` field. `tmux_name` returns `id` unconditionally.
- Move `engine`, `chats`, **and `last_activity`** to `LlmSession`; add `LlmSession.turn_started_at`.
  Add the base `activity_at` property (`created_at` for `OtherSession`, `last_activity or
  created_at` for `LlmSession`). `needs_attention` and `read_only` stay on the base (both are
  internally role-gated already).
- Rename attributes `cmd`→`initial_cmd`, `env`→`spawn_env`; `to_dict` keeps JSON keys `"cmd"`/`"env"`.
- `from_dict` becomes a **factory**: read `role`; `llm` → `LlmSession`, else `OtherSession`.
  Validates `schema_version == 4` at the boundary (`SCHEMA_VERSION` bumps — Q1 settled). Reads
  `turn_started_at` via `.get()` — v4 records written before a session's first post-deploy turn
  may lack it (an allowed boundary default). No legacy-key handling needed: v3 records are
  refused by the version gate until `tx migrate` runs.
- `matches()` drops `self.kind.value` from the haystack (session.py:306).
- `State.valid_for` / `initial_for` unchanged.

### `lib/tx/store.py`
- No structural change: `save`/`load`/`all`/`query` operate on the base type and dispatch through
  the new `Session.from_dict` factory. Views are simply never saved here anymore.

### Net deletions — code the split retires (investigated at `980ad3d`)

Deleted outright:
- `Kind` enum (session.py:42-46) + the `kind` field/param and every import of it
  (session/spawn/service/render/cli + both prototypes).
- `SpawnSpec.kind` field (spawn.py:52) + the `kind=` argument in all three builders — every spec
  is a process now.
- `Session.tmux_name`'s id-vs-name branch (session.py:260-261) → `return self.id`; same for the
  copies at `prototypes/remote-control/server.py:98` / `sessions-graph:691`.
- `service._spawn`'s `tmux_name = id if PROCESS else name` (service.py:223) and `rename`'s view
  branch (service.py:355-356).
- `render_ls`'s per-row kind branch (render.py:93) and `picker_display_rows`' view filter
  (render.py:173); cli.py:869's `kind != Kind.VIEW` guard.
- **`PaneKindCommand`** (`_pane-kind`, cli.py:1220-1232 + registration) — its only caller,
  `tx-ide.tmux:84`, switches to a direct `#{@tx_view}` check, which also removes a Python launch
  from the `after-new-window` hook path.
- `_is_view_session`'s service/store resolution (cli.py:1106-1112) → one tmux option read.
- `matches()`'s kind haystack entry (session.py:306).

Retirable in the same pass (one-time tools whose job is verifiably done — confirm & delete rather
than port to the subtypes):
- **`MigrateTmuxNamesCommand`** (cli.py:1559) — the id-naming cutover has fully run (every live
  tmux session is uuid-named; verified). Deleting it also deletes a `Session(...)` construction.
- **`FlipRederiveCommand` + the flip re-derivation path** (cli.py:1354-1557) — the v1→v2 Flip is
  long done; its `Session(...)` constructions would otherwise need subtype updates for nothing.
- **`migrate_record_v2_to_v3`** (migrations/__init__.py) — all 656 records are v3; the v3→v4
  migrator (Q1 b) replaces it in the same file, same pattern. The loader refuses non-current
  versions anyway, so no chain is needed.

(If the retirements are declined, those three get mechanical subtype/constructor updates instead —
they are the only reason cli.py's `Session(...)` call sites appear in this plan at all.)

### `lib/tx/service.py`
- `_spawn` constructs the right subtype: `llm` → `LlmSession` (with `engine` + the pending
  `original` `ChatRef` + `turn_started_at=None`), else `OtherSession`.
- `spawn_view` **stops calling `_spawn` and stops saving a record** — it must therefore replicate
  what `_spawn` provided: the `tmux.has_session` duplicate guard and **one EventLog line** (view
  spawns must not silently vanish from the event history). It creates the tmux session, sets the
  `@tx_view` marker option, and applies the chrome (status bar + pane-border-status) it already
  sets at `:209-210`. Views no longer get `TX_SESSION_ID`/`@tx_id` (nothing depends on it — the
  reconciler keys on `@tx_id`, so it now never sees views; re-grep at impl time). Return type: no
  `Session` exists — return the spec/name for the CLI print at `cli.py:288`.
- **`focus_envelope` / `_add_record_attrs` (`:434-463`) — missed in Rev 1, and load-bearing:** it
  joins `record.kind.value` + tags into the assistant envelope (`session-kind`/`session-tag`,
  documented as contract in `TX-ASSISTANT.md:20`), and the *outer* session in M-focus is almost
  always the view. Replace: `session-kind` is derived live — `"view"` when the session carries
  `@tx_view`, else `"process"` from the record. **Q4 settled: views have no tags** — a view host
  gets `session-kind="view"` and *no* `session-tag` attr; the record join runs only for process
  sessions.
- `record_state`: on `WORKING`, set `turn_started_at = now` and bump `last_activity` (both now
  `LlmSession` fields — this path is llm-only by construction; narrow the loaded record). Other
  transitions unchanged. `_spawn` sets `last_activity` only on `LlmSession` construction.
- `kill` / `tag` / `rename` / `send_message` / `_resolve` operate on the base. `rename`'s
  `kind == VIEW` branch (`:355`) is deleted. **Q3 settled: `kill` — and only `kill` — gets a
  live-`@tx_view` fallback:** when `_resolve` finds no record, `kill` checks whether a live tmux
  session by that name carries `@tx_view` and kills it (one log line), so `tx kill <view>` keeps
  working and the assistant retains a tx verb for ending views (COMMON.md's "every kill goes
  through tx" holds). `rename`/`tag` on views are gone — a view has no tags (Q4) and renaming
  one is not a tx concern.

### `lib/tx/spawn.py`
- `SpawnSpec` loses `kind` (see Net deletions); `for_view` still parses argv into a spec, but
  `service.spawn_view` now realizes it as a live tmux view (marker + chrome), not a record.
  **`for_view` drops its `tags` parameter** (Q4 — views carry no tags; the CLI's
  `spawn-view --tag` flag and its `default="views"` go with it). `infer_role` unchanged.

### `lib/tx/reconcile.py`
- `_demote_if_stuck` reads `turn_started_at` instead of `last_activity` (the C5 clock). A record
  that was `WORKING` across the deploy has `turn_started_at=None` → the demote is skipped until its
  next turn (accepted: self-heals on the first new turn). The reconciler now only ever sees process
  records; liveness sweep unchanged.

> **Annotated code tour:** every change site below is annotated in the source of the
> `session-split-review` worktree with a `# TOUR(n)` comment (30 stops, data-flow order). Index +
> jump instructions: `docs/session-record-split-tour.md` there — open via the
> `session-split-review-tour` nvim, then `:silent grep "TOUR(" lib prototypes tmux | copen`.

### `lib/tx/render.py`
- `render_ls`: **the VIEWS section is deleted outright** (Q6 settled — "the sessions are what's
  important"; views are visible in tmux itself). `render_ls` becomes a single PROCESSES listing;
  no tmux-sourced views helper is needed anywhere in render.
- Recency: `_by_recent_activity` (`:77`) and `render_history`'s sort/`when` cells
  (`:193`, `:200`) switch from `session.last_activity` to the base `activity_at` property.
- IDLE cells (`tx ls` row `:94`, picker row via `:174`): real `last_activity` age for llm rows,
  `—` for non-llm rows (no fake activity signal).
- `picker_display_rows`: drop the `session.kind != Kind.VIEW` filter (`:173`) — the store has no
  views. `role` read via duck-typed attribute as today.
- `render_chats`: unchanged (`LlmSession` only in practice; `OtherSession.chats → []` covers the
  general path).

### `lib/tx/hooks.py`
- The chat-capture path (`_capture_chat_ref`, `_complete_pending`) narrows to `LlmSession`
  (engine/chats guaranteed). `dispatch` loads a base record by id, then narrows to `LlmSession`
  (isinstance) before the capture + `record_state` arms — this also fixes the latent bug where a
  hand-run `claude` inside a tx shell session could drive that shell record to `WORKING`; the
  `session-closed → reconcile` arm stays role-agnostic.
- `registry.get(session.engine)` is now structurally safe.

### `lib/tx/chat.py`, `lib/tx/history.py`, `lib/tx/engines/*`
- Retype signatures `Session` → `LlmSession` where they touch `engine`/`chats` (they are already
  only ever called with llm sessions). **Rev 2 — the rename churn is real, not just annotations:**
  `.cmd` reads at `chat.py:189`, `chat.py:447`; `.env` reads at `chat.py:87`, `chat.py:444`.
  The engine adapters never take a `Session`, so they are untouched beyond the `State` values they
  reference.

### `lib/tx/cli.py`
- `spawn-view` drops its `--tag` flag (Q4 — views carry no tags); print path at `:288` takes the
  new return shape.
- `_is_view_session(name)` → read the `@tx_view` tmux option instead of `record.kind == VIEW`
  (`:1106-1112`). A live read — simpler, and it stops depending on the archived-record quirk that
  makes nest-attach work today.
- **`PaneKindCommand` (`_pane-kind`, `:1220-1232`) is deleted** — see the tmux config change below;
  its only caller goes direct.
- `StartCommand` already gates on `has_session("Views")`; unchanged.
- Name-width sizing (`:869`) drops the `kind != Kind.VIEW` guard (store is process-only).
- `_migrate-tmux-names` (`:1576` reads `record.kind != Kind.PROCESS`) / flip-rederive paths: drop
  view handling; update the `Session(...)` constructions (`:1294`, `:1499`, `:1514`, `:1522`) to
  the subtypes (or retire the one-time flip/cutover commands outright if the cutover is done —
  decide at impl time).
- `selfcheck` demo `Session(...)` → `LlmSession(...)`.
- `.env` read at `:518` → `spawn_env`.

### `tmux/tx-ide.tmux` + `tmux/tmux.conf` — missed in Rev 1
- `tx-ide.tmux:84` (`after-new-window` hook) currently shells out to `tx _pane-kind "#{@tx_id}"`
  to set `pane-border-status top` for new windows in views. Views lose both the record and
  `@tx_id`, so this silently dies. Replace with a direct `if-shell` on `#{@tx_view}` — no Python
  launch on the hook path anymore (strictly faster).
- `tmux.conf:38` comment references `@kind=view` — update wording.

### `lib/tx/messages.py`
- `_role_resolver` reads `session.role` (duck-typed — unchanged behavior) and its recency read
  (`:274`, `session.last_activity or 0.0`) switches to `activity_at`. `session.chats` iterations
  served by the `OtherSession` property. `.cmd` reads at `:255`, `:266` → `initial_cmd`.

### Prototypes — `prototypes/remote-control/server.py`, `prototypes/sessions-graph/server.py`
- `_tmux_name` (`server.py:98`, `sessions-graph:691`) → always `id` (records are processes).
- `needs_attention` stays on the base, so `session_payload` (sessions-graph:93) is untouched.
- `sessions-graph:179` iterates `other.chats` over ALL sessions (pre-llm-filter — Rev 1's
  "touched only after the llm filter" was wrong for this site); served by the `OtherSession.chats`
  property.
- `.cmd` reads (`remote-control:445,468,665,740,752`, `sessions-graph:327`) → `initial_cmd` —
  note `:740`'s `"[1m]" in session.cmd` context-window sizing.
- The `role != Role.LLM` filters keep working; narrow post-filter variables to `LlmSession`.

### Docs
- `TX-ASSISTANT.md` — the envelope attr contract (`session-kind`, `:20`; views carry no
  `session-tag` anymore) and the Views section (`:68`, "record `kind=view`") must be rewritten to
  the `@tx_view` model.
- `COMMON.md` — note that view lifecycle through tx is `tx spawn-view` / `tx kill` only
  (no tag/rename; views carry no tags).

### Tests — dev-only (Q2 settled)
Commit `574a840` (PR #84) deleted the repo's remaining test suite + fixtures deliberately —
**the repo does not keep unit tests on main.** Per Q2: write the coverage below to drive the
refactor, run it green, then **delete `tests/` in the final commit before merging** (it stays
recoverable in the branch history).

1. **Persistence round-trip:** every shape through the new `from_dict` factory — llm →
   `LlmSession`, non-llm → `OtherSession`, correct `initial_cmd`/`spawn_env` from `"cmd"`/`"env"`,
   `turn_started_at` default, v3 records refused by the version gate.
2. **Spawn:** llm → `LlmSession` with engine + pending chat + `turn_started_at=None`; shell/nvim
   → `OtherSession` (no `last_activity`); `spawn-view` → live tmux session with `@tx_view`,
   **no record**, no tags, one event log line.
3. **Reconcile / render / cli:** stuck-`WORKING` demote reads `turn_started_at`; `render_ls` is
   processes-only; picker sorts by `activity_at` and shows `—` idle for non-llm; `_is_view_session`
   reads the option; `tx kill <view>` works via the `@tx_view` fallback.
4. **Migration:** v3 fixture store (llm + non-llm + view records) through `tx migrate` → v4 keys
   dropped, view records gone, `@tx_view` stamped on a matching live session; re-run is a no-op.

Hermetic against a temp `$TX_IDE_HOME`; the old `test_capture.py` bootstrap is recoverable from
git history (`git show 574a840^:tests/test_capture.py`). Repo convention: directly runnable
`python3.14 tests/test_*.py`, no pytest.

## Migration / compatibility — v4 (Q1 settled)

Bump `SCHEMA_VERSION` to 4 and fold everything into one idempotent `tx migrate` v3→v4 pass using
the existing `migrations/` machinery (replacing the spent v2→v3 body):

1. process records: drop `"kind"`, drop non-llm `"engine"`/`"chats"`/`"last_activity"`, stamp
   `schema_version: 4`;
2. **stamp `@tx_view` on every LIVE tmux session whose record is `kind: "view"`** (match record
   name ↔ tmux session name — views are human-named) — this is what keeps `upside-office`
   nest-attach/border-chrome working through the cutover;
3. delete the 12 view records (they are *not* "unreadable" post-split — the role-dispatching
   factory would load them as terminal `OtherSession` rows and pollute `tx history` with 12
   phantoms; deletion is mandatory and ordering-sensitive, hence inside the same migrate step).

There is no separate "view-record cleanup" script — it's step 2+3 of the migration. Rollback
story: v4 records are refused loudly by an old binary's version gate (`UnsupportedRecordError`),
never silently skipped or crashed on — the failure mode the rejected "two shapes under v3"
variant had.

## Testing strategy

1. **Round-trip smoke** over a *copy* of the real store: migrate it, then load every record via
   the new factory; assert no crashes, correct subtype, and `initial_cmd`/`spawn_env` populated
   from `cmd`/`env`. Re-run the store audit at impl time (counts at Rev 2: 327 llm / 316 non-llm
   / 12 views).
2. The dev-only unit coverage listed under **Tests** above.
3. **Live smoke after migrate:** picker nest-attach from the home view still works
   (`_is_view_session` via `@tx_view`), new window in a view gets its border label
   (`after-new-window` hook), M-focus envelope carries `session-kind="view"` (and no
   `session-tag`), `tx kill <view>` works, `tx ls` lists processes only.

## Suggested sequencing

0. ~~Resolve Q1–Q7~~ — done (see the Rev 3 note); all decisions are inlined above.
1. `session.py` — base + subtypes + factory + renames + the property mechanics above, `v4`
   version gate. (Green the module + the round-trip test first.)
2. `store.py` + `service.py` + `spawn.py` — construction fork + views-out-of-store (incl. the
   `spawn_view` guard/log, no-tags, `kill`'s `@tx_view` fallback, `focus_envelope` rework) +
   `turn_started_at`/`last_activity` moves.
3. `reconcile.py` + `hooks.py` + `chat.py` + `history.py` + `engines/*` — retype to `LlmSession`,
   rename churn.
4. `render.py` + `cli.py` + `messages.py` + `tmux/*.tmux` + prototypes + docs — `activity_at`
   reads, VIEWS section removed, `@tx_view` gates, hook rewrite, net deletions.
5. Tests + the `tx migrate` v3→v4 pass (or compat keys per Q1) + live smoke.

## Risks / notes

- The `render.py`/`messages.py` uniformity reads (`role.value`, `len(chats)` per row) are the
  reason `OtherSession` carries the `role` field and the `chats` property — this is the deliberate
  cost of keeping the picker/ls a single loop. Do **not** try to remove `role`/`chats`
  reachability from both subtypes.
- The property/field mechanics in **Decisions** are load-bearing — a read-only property on the
  base with a same-named subclass dataclass field does not construct. Follow the per-subtype
  layout exactly.
- `tx-assistant` and coexistence sessions are ordinary `process`/`llm` records (verified — created
  via `tx spawn`), id-named and name-resolved through the store. Unaffected.
- Every view gate (`_is_view_session`, `kill`'s fallback, the `after-new-window` hook, the focus
  envelope) must key on the `@tx_view` marker, never a literal name — multiple named views exist
  (`Views`, `Views-Left`, `upside-office`, …).
- `@tx_view` (like all tmux options) dies with the tmux server; after a restart views must be
  respawned via `tx spawn-view` (which stamps it) — that is the "ephemeral, recreated cheaply"
  contract made explicit.
- The dev-only test suite must actually be deleted in the final pre-merge commit (Q2) — leaving
  it in the PR is a review-blocker per repo policy.
