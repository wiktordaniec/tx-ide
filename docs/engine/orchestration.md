# Engine abstraction — build orchestration

The operating manual for the **orchestrator** session that builds this change with a tx-managed
crew. It executes the task graph in [`design.md`](./design.md) §7 against the integration branch
`feat/engine-abstraction`. We dogfood tx's own multi-session orchestration to build it.

Design authority is the planner session (`codex-plan`); the orchestrator **executes** and escalates
design questions back to it. The orchestrator does not redesign — it runs the board.

## Crew

| Role | Count | Authority | Cwd |
|---|---|---|---|
| **Orchestrator** | 1 | owns the board + the single draft PR; assigns tasks; **sole merge authority**; unblocks dependents | the `codex-engine` worktree (on `feat/engine-abstraction`) |
| **Developer** | ≤3 concurrent (conflict-bounded) | implements ONE task in its own worktree; atomic commits; **never merges** to the feature branch | `.claude/worktrees/<task-id>` off `feat/engine-abstraction` |
| **Validator** | 1 per finished task | independent + adversarial; runs the acceptance checks; reports PASS/FAIL; **cannot fix or merge** | the task branch (read-only review) |
| **Watcher** | 1 | **recommend-only**; reads the fleet's context size + direction; alerts the orchestrator | reads records/history; no edits |

A task's **developer and its validator are always different sessions** (no marking your own homework).

## Branch & PR model

- Integration branch `feat/engine-abstraction` (all our changes; off `main`).
- Each task → its own branch `task/<id>-<slug>` off `feat/engine-abstraction`, in its own worktree.
- Validator PASS → the **orchestrator** rebases the task branch onto the current
  `feat/engine-abstraction` (if the spine advanced), re-runs the baseline, and merges it in. Only
  the orchestrator merges.
- **One draft PR**, `feat/engine-abstraction` → `main`, created + maintained by the orchestrator
  (`gh pr create --draft`), its body updated as tasks land. No per-task PRs.

## Task board (acceptance criteria)

Full task detail in [`design.md`](./design.md); the DAG + lanes are in [`README.md`](./README.md).
Each task's acceptance criteria (the validator's checklist):

| Task | Done when |
|---|---|
| **T0 Engine contract** | `Engine` enum + protocol + registry import cleanly; schema v3 loads; the v2→v3 migrator converts a v2 fixture → v3 (`engine=claude` for llm, `None` else); 31 baseline checks green |
| **T1 ClaudeEngine extraction** | all 31 checks green + **zero behavior change**; `git grep -nE 'claude\.|\.claude/'` shows no direct engine calls outside `ClaudeEngine`/back-compat; a live Claude spawn still drives working/waiting |
| **T2 Codex spike** | the 3 questions answered with evidence (positional auto-submit; `SessionStart`/`fork` fires the hook with the new id+path; `--yolo --dangerously-bypass-hook-trust` runs our hooks) |
| **T3 Codex installer** | `setup/engines/codex.sh install --settings <copy>` writes correct `hooks.json` + marked TOML block + statusline; `uninstall` reverses exactly; idempotent; unified orchestrator drives `setup/engines/{claude,codex}.sh` |
| **T4 Generalize identity/transcript** | a simulated hook payload fills a pending `ChatRef`; `resolve_transcript` finds a rollout fixture; Claude path unchanged (checks green); conformance test green |
| **T5 Neutralizations** | envelope parses old+new (test); `git grep` clean of `from-claude` / `.claude/worktrees` / `CLAUDE_REQUIRE_WORKTREE` outside back-compat; personas read engine-neutral |
| **T6 CodexEngine** | unit tests for command building + rollout parsing (fixtures) + bundle; passes the shared protocol-conformance test |
| **T7 Codex fixtures + parser** | rollout fixtures committed; Responses-item parse tests green |
| **T8 Codex spawn wiring** | `--engine codex` builds the right command; `infer_role`/reconcile recognize `codex`; default still Claude |
| **T9 E2E verify** | a live Codex worker: working/waiting transitions, chat capture, history ingest, fork/handover/rollover, resume — observed |

**Global gates at every merge:** 31 checks green; the protocol-conformance test runs every `Engine`
through the same contract; **no unresolved `AINote:` introduced in the diff**.

## Worker priming (templates)

Developer (one task):
```
tx spawn <task-id> --tag codex --cwd <REPO> --env CLAUDE_REQUIRE_WORKTREE=1 \
  --cmd 'claude --dangerously-skip-permissions --model "opus[1m]" --effort max "<PRIMING>"'
```
`<PRIMING>` = the COMMON+DEVELOPER role-file read instruction, then:
"Read docs/engine/{README,design,orchestration}.md and your task spec at .claude/plans/tasks/<id>.md.
Implement ONLY that task on branch task/<id>-<slug> in a worktree off feat/engine-abstraction; atomic
commits; keep the 31 baseline checks green; resolve+delete any AINotes you touch; do NOT merge; when
done, write 'ready' to .claude/plans/tasks/<id>.status and message the orchestrator."

Validator (one finished task):
"Read .claude/plans/tasks/<id>.md (acceptance criteria). Check out branch task/<id>; independently run
the checks (tests, git-grep gates, AINote gate). Do NOT fix anything. Report PASS or FAIL with findings
to the orchestrator; on FAIL, encode each required change as a `# AINote:` on the branch."

Watcher (one, looping):
"Every ~10 min, for each live developer session: read its context size (transcript token counts / the
sessions-graph usage API) and its last activity. Recommend to the orchestrator: rollover/handover when
context is large, or redirect when the work drifts from the task spec. Recommend only — never act."

## Flow

1. Orchestrator writes each ready task's spec (acceptance criteria + file scope) to `.claude/plans/tasks/<id>.md`.
2. Spawns developers for **ready** tasks — deps met AND no shared-file conflict with an in-flight task.
3. Dev finishes → writes `ready` to its status file + pushes its branch + messages the orchestrator.
4. Orchestrator spawns a **validator** (a different session).
5. **PASS** → orchestrator rebases + merges into `feat/engine-abstraction`, updates the PR body,
   unblocks dependents, spawns the next ready tasks.
   **FAIL** → findings land as AINotes on the branch; the orchestrator re-engages the same developer
   (peer message) to address+delete them, then re-validates.
6. Watcher recommendations → orchestrator triggers a rollover/handover (dogfood the chat-ops) for a
   bloated worker, or redirects a drifting one with an AINote.
7. Repeat through T9, then hand to the human + planner for final review.

## Conflict avoidance

- The shared files — `session.py`, `service.py`, `hooks.py`, `messages.py`, `cli.py` — are each owned
  by **at most one in-flight task at a time**. The orchestrator never runs two tasks that touch the
  same file concurrently; it serializes them.
- When the spine merges, every in-flight parallel branch rebases onto `feat/engine-abstraction` before
  its own merge. The orchestrator re-runs the baseline after each rebase.

## Comms & done-signal

- Done-signal = the per-task **status file** (`.claude/plans/tasks/<id>.status`:
  `pending|in-progress|ready|validating|pass|fail`) the orchestrator polls, **plus** the pushed
  branch — not a bare peer message (a message into a pane can be missed).
- Coordination via `tx send-message`; liveness/activity via the durable records (working/waiting).

## Escalation

Orchestrator escalates to the planner (`codex-plan`) / human on: a task failing validation twice, a
merge conflict it can't cleanly resolve, an ambiguous or contradictory spec, or a watcher red-flag.
The planner owns design; the orchestrator owns execution.
