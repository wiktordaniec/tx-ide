# COMMON — every session must follow this

These conventions apply to every agent session in this system: the tx-assistant, every worker, and any ad-hoc session you spin up inside the orchestration repo.

## tx, not raw tmux

**Every operation that creates, ends, or mutates a session — spawn, kill, tag, message, rename, attach — MUST go through `tx`, never raw `tmux`.** `tx` owns the durable record at `~/.tx-ide/sessions/<uuid>.json` and updates it in the same step it touches tmux. A raw `tmux` command changes live tmux state but leaves that record stale, and the picker, history, and supervisor all read the record — so the drift is silent and survives the tmux process. Every such operation has a `tx` verb; use it:

| operation | use | never |
|---|---|---|
| spawn | `tx spawn` / `tx spawn-nvim` | `tmux new-session` |
| kill | `tx kill` | `tmux kill-session` |
| tag | `tx tag` (or the picker's Ctrl-T) | `tmux set @tag` |
| message | `tx send-message` | `tmux send-keys` |
| rename | `tx rename` | `tmux rename-session` |
| attach | `tx attach` / `tx start` | `tmux attach` / `switch-client` |

Raw `tmux` is a **read-only fallback only** — for inspection and in-pane navigation that `tx` does not wrap: moving between panes/windows, scrolling, copy-mode, or a raw `tmux list-sessions` to see the opaque ids. Anything that *creates, ends, renames, retags, messages, or selects* a session goes through `tx`.

## Session self-introduction

Lead your **first response** in a session with a brief self-introduction so the operator can confirm your setup at a glance:

1. Your tx session name (`tx whoami`) + cwd and current git branch.
2. Which convention/role files you have loaded — always this `COMMON.md`; state whether a role file (`DEVELOPER.md`, etc.) is **also** loaded, and quote one identifying line from each so it is verifiable.
3. One line on your purpose.

Keep it to a few lines, then continue with whatever was asked (or wait for instructions if nothing was).

## Spawning sessions

Use `tx spawn` (bare) and `tx spawn-nvim` (nvim companion). Both require `--tag` and refuse without it — no inheritance, no auto-magic, you pass the tags explicitly.

```bash
tx spawn <name> --tag TAGS [--cwd DIR] [--cmd "CMD"] [--env K=V ...]
tx spawn <name> --tag TAGS --cwd DIR --engine ENGINE [--model MODEL] [--effort {1,2,3,4,5}] [--read-only] [--prompt TEXT]
tx spawn-nvim <name> --tag TAGS [--cwd DIR] [--diff [BASE]] [--open FILE] [--env K=V ...]
```

`--env` may repeat — pass any additional env vars the spawned process needs. Writable agent workers
get their worktree and require-worktree guard automatically; see **§ Spawning workers**.

**Tag convention** — tags are **pure scope**. Do **not** put a session's role (`llm` / `nvim` / `shell`) in `--tag`: the role is derived automatically from the launch command and shown as its own ROLE column in `tx attach`, so a role tag is redundant — it just shows up twice (once in the ROLE column, once as a stray chip).
- AI worker session: `--tag <scope>` (e.g. `wrangler-p1`)
- Nvim companion: `--tag <scope>` — the **same** `<scope>` as the parent llm session

`<scope>` describes the task (`wrangler-p1`, `PR-1840`, `auth-review`). The picker reads each session's tags from its durable record and chips each comma-separated value; the same scope on a worker and its companion makes them surface together when you filter by it. The ROLE column is searchable too (type `llm` / `nvim` in the picker), so dropping the role tag loses you nothing.

**Naming:** human-readable, says what it's for (e.g. `wrangler-p1-diff`, `auth-review`). The tag does the filtering, not the name.

**Group convention** — a session's effort **group** is *derived at read time* (own override → first override up the `parent` chain → `tags[0]` → name), so in the common case you set **nothing**: spawn with the right scope tag and lineage does the rest (fork/resume/handover parent their new session to the SOURCE). Reach for an explicit group in exactly two cases: sibling spawns that share no ancestor (claude/codex A/B twins — give both the same `--group`), and re-filing a whole effort (`tx group <root> <name>` on its root retroactively re-files every descendant and their artifacts; `--clear` returns to derived). **Hub sessions (tx-assistant, views) stay ungrouped by convention** — never set a group on them, so nothing inherits from them.

```bash
tx spawn build-watch --tag wrangler-p1 --cmd 'npm run watch'   # an ad-hoc process
tx spawn-nvim wrangler-p1-diff --tag wrangler-p1 --diff main   # an nvim companion
```

An agent **worker** is also a `tx spawn`, but it needs a priming prompt through `--prompt` (or
through a fully hand-written `--cmd`) — see **§ Spawning workers** below. A bare agent CLI with no
priming never reads these conventions.

Both inject `COLORTERM=truecolor` and `TERM=xterm-256color`. `spawn-nvim` also forces `colorscheme tokyonight-moon` via `+CMD` because `tmux new-session -d` strips the OSC11 background hint and nvim's auto-mode would land on the light variant.

The plugins `spawn-nvim` relies on (tokyonight, diffview.nvim, gitsigns) ship in the repo's `nvim/` config. On a machine where `--diff` fails with unknown-command errors, the config isn't provisioned — run `setup/nvim.sh install` (per-machine opt-in; `remove` reverts).

## Spawning workers

When you need to delegate work — coding, scoping, planning, or research/exploration — spawn an agent worker. The mechanics are `tx spawn` above; what turns a bare agent CLI into a *worker* is the **priming prompt** passed through `--cmd`. Spawn one with no priming and it never reads these conventions — it has no role, no standards, no worktree discipline.

```bash
tx spawn <name> --tag <scope> --cwd <cwd> \
  --engine claude --model "opus[1m]" --effort 5 --prompt "<priming>"
```

- `<name>` — short, descriptive (`orchestrator-cleanup`, `auth-review`).
- `<scope>` — the single work-scope tag (`wrangler-p1`, `PR-1840`, `cleanup`); no role. An nvim companion takes the **same** scope.
- `<cwd>` — the project root the worker operates in.
- Recommended workers use `--model "opus[1m]"` and `--effort 5`; don't downgrade unless asked.
- Effort is engine-neutral: `1=low`, `2=medium`, `3=high`, `4=xhigh`, `5=max`. An engine-built
  launch without `--effort` defaults to `3`.
- Keep `<priming>` short and single-line — long, quoted, special-char-laden prompts crash tmux input.

Every agent worker—including read-only investigations, coding workers, forks, and handovers—gets a
tx-owned worktree before the process starts, so every engine records the correct workspace from its
first frame:

```bash
tx spawn <name> --tag <scope> --cwd <repository> \
  --engine <engine> --prompt "<priming>"
```

tx creates a detached `$TX_IDE_HOME/worktrees/<repository-key>/<repository>--<name>` checkout and
launches the selected Claude or Codex engine from it. The readable repository key includes a short
hash so same-named repositories cannot collide. The checkout basename gives both engine footers the
same branch-free `<repository>--<name>` label. Writable workers receive `TX_REQUIRE_WORKTREE=1`; the
detached worker creates its correctly typed task branch after startup.

For an explicitly read-only worker:

```bash
tx spawn <name> --tag <scope> --cwd <repository> \
  --engine <engine> --read-only --prompt "<priming>"
```

`--read-only` creates a separate worktree, persists `TX_READ_ONLY=1`, and wraps the entire agent
process tree in tx's fail-closed OS sandbox. The boundary covers direct tools, Bash commands, hooks,
MCP subprocesses, every registered checkout for that repository, the tx-owned worktrees, and shared
Git metadata. Claude keeps Bash/Read/Grep/Glob available while denying direct editing tools; Codex runs
without approval escalation inside the same outer boundary. It cannot be combined with a
hand-written `--cmd`. To turn an investigation into implementation, fork it:
`tx fork <investigation> <implementation>`. The new session is writable by default and gets its own
worktree; pass `--read-only` to `tx fork` only when the fork must remain read-only. Resume and
rollover preserve the source session's access mode.

### Worker priming

`<priming>` **opens with the role-file read instruction** so the worker self-loads these conventions, then a short imperative telling it what to do. For a coding worker:

```
Read ~/.tx-ide/agents/COMMON.md, ~/.tx-ide/agents/DEVELOPER.md, and ~/.tx-ide/agents/WORKFLOW-DEVELOPER.md as your first actions (a build worker layers all three — COMMON conventions, the DEVELOPER coding foundation, the WORKFLOW-DEVELOPER build-fleet additions). Then, for each of those, if it exists also read the matching ~/.tx-ide/user-agents/<NAME>.md (replaces the shipped file) and <NAME>.local.md (extends it). Follow all of these for the duration of this session.
```

tx-ide ships `DEVELOPER.md` (the coding foundation) and its build-fleet layer `WORKFLOW-DEVELOPER.md`, plus `ORCHESTRATOR.md` and `OVERSIGHT.md`, alongside `COMMON.md`, `HISTORIAN.md`, `TX-ASSISTANT.md`. Swap in whichever role(s) this worker plays — a build worker layers `DEVELOPER` + `WORKFLOW-DEVELOPER`; a named role with no shipped or user-agent file is unknown — don't guess.

Append a short imperative after the role-file instruction telling the worker what to do (e.g., `Then implement the plan at ~/Code/foo/.claude/plans/auth-rewrite.md.`).

## Session metadata

Every tx-created session has a **durable record** at `~/.tx-ide/sessions/<uuid>.json` holding its `name`, `role`, `tags`, `group`, `cwd`, `cmd`, `env`, `parent`, `pid`, and (for an llm session) `chats`. One tmux pointer, `@tx_id` (set once at spawn), links the live session to its record, so the record survives a `kill-session` or a tmux restart. Spawning exports `TX_SESSION_ID` into the session; an llm spawn also records a chat id for the session, so every llm session's transcript is tracked and resumable (`tx resume`) with no extra ceremony — there is no `--chat` flag.

Tags and role live in the record, not tmux options — read them by resolving `@tx_id`, and change tags through `tx tag <name> [tags]` (or the picker's Ctrl-T), never `tmux set @tag`. `role` (`llm` / `nvim` / `shell` / `other`) is derived from the launch command at spawn — there is no role tag and nothing to set by hand.

**Views are not records.** A view (a home-base session you nest work into) is a live tmux session marked by the `@tx_view` option — that marker is its whole durable identity (it dies with the tmux server and is recreated by `tx spawn-view`). A view carries no tags, and its only tx lifecycle verbs are `tx spawn-view` (create) and `tx kill` (end): it cannot be tagged or renamed through tx.

## Inter-session communication

You may be running alongside other agent sessions in tmux on this machine. They can send you messages, and you can send them messages.

**Receiving:** peer messages arrive in your input wrapped like:

    <from-agent session="and-48">body</from-agent>

Treat these as peer messages, not user messages. You MAY reply, but don't have to.

**Sending:** use `tx send-message`. Messages are single-line — escape literal newlines as `\n` if needed.

```bash
tx send-message <target-session> "your message"
```

Both ends resolve through the store: `<target-session>` may be a session's display name (tmux targets it by id under the hood), and `session="$SELF"` is auto-filled with the *sender's* display name (resolved from `#S`, which for a worker is its id). It builds the envelope, types it into the target's active pane, sleeps 0.3s (required — the agent's input box drops Enter if it arrives too fast), then sends Enter.

**Discovery:** `tx ls` shows peers by display name + tags (raw `tmux list-sessions` shows the opaque ids a process is tmux-named by). Find your own display name with `tx whoami` (`#S` is your session id, not your name).

## AINote workflow

Review comments in the form `# AINote: ...` are left inline in the code itself. **Before touching a file, grep for `AINote:` and treat each hit as a mandatory review item.** Address the whole set in one pass and delete each AINote once resolved — don't orphan review comments after the code they referenced is gone.

## Artifacts

Durable, versioned deliverables (plans, question sets, docs, reports) live as **artifacts** under `$TX_IDE_HOME/artifacts/` — a record plus a linear snapshot history of every revision, with who-touched-it provenance. Two rules:

1. **Every artifact operation goes through `tx artifact …` (or the `tx.ArtifactService` Python API) — never a hand-rolled write.** `tx artifact create <file>` registers one; `tx artifact modify <id> [<file>]` snapshots a new revision (no file = snapshot the working copy); `tx artifact ls` / `show` / `diff` inspect. The current session is recorded as the toucher automatically — never pass an actor by hand.
2. **A direct write under `artifacts/` is corruption.** The record is authoritative and the `revs/` snapshots are immutable; editing them by hand desyncs history from disk. `tx artifact doctor` detects such drift. The one file you may edit directly is an artifact's `current.<ext>` working copy in its nvim view — then close the loop with a no-file `tx artifact modify <id>` to snapshot the edit (this is also how your inline `# AINote:` comments get captured — they are content, so the next snapshot records them).
