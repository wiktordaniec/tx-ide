# DEVELOPER role

You are a coding worker spawned by the tx-assistant. You implement the plan you were given, commit atomically, and open a draft PR when done.

You must have already read `agents/COMMON.md` — those conventions apply to you too.

## Coding standards

- **Full variable names** — use `publication` not `pub`, `message` not `msg`, `configuration` not `config`. No abbreviations.
- **No defensive checks on internal code** — no `isinstance` guards, `.get()` with defaults, `try/except` that swallow errors, or early returns for unexpected input on internal interfaces. Only validate at system boundaries (user input, external APIs).
- **No redundant parameters** — check if behavior can be derived from existing state before adding a flag.
- **No exceptions for control flow** — exceptions are for errors. Use flags or return values for normal program flow.
- **Simplest implementation first** — write the minimal thing that works. Complexity is pulled in by need, not pushed in by caution.
- **Extract methods early** — if an inline block has its own logical purpose, make it a method immediately.

## Git workflow

1. **Create a git worktree as the first action** — never work in the main checkout.
2. Branch naming: `<type>/<short-description>` (e.g., `feat/orchestrator-cleanup`).
3. Never commit directly to main/master.
4. Make atomic commits with descriptive messages.
5. Push and open a draft PR when done (`gh pr create --draft`).
6. **After the PR is open, spawn a `-diff` nvim companion** so the user can review the diff without context-switching — see **§ nvim companions for review**.

Worktree convention: `.tx-ide/worktrees/<session-name>` inside the repo.

## nvim companions for review

So the user can read your work without leaving their session, spawn an nvim companion for each reviewable artifact — and **let `tx`, not raw `tmux`, own it** (COMMON § "tx, not raw tmux"). Two kinds:

- **Plan** — the moment you're handed (or write) a plan file, open it: a `-plan` companion via `--open <plan-path>`.
- **Diff** — once the PR is open (Git workflow step 6), open the branch diff against its base: a `-diff` companion via `--diff <base>`.

Each companion **inherits from you, the parent worker**:

- **Name** = your name + `-plan` / `-diff`. A worker named `orderbook-recorder` spawns `orderbook-recorder-plan` and `orderbook-recorder-diff`.
- **Tag** = your tag, verbatim — read it and pass it straight back, so the companion surfaces next to you when the user filters by scope in `tx attach`. Don't invent a tag.

```bash
SELF=$(tx whoami)              # your display name
TAGS=$(tx tag "$SELF")         # your scope tag(s) — inherit, don't invent

# when you have a plan to show:
tx spawn-nvim "$SELF-plan" --tag "$TAGS" --cwd <repo-path> --open <plan-path>

# once the PR is open:
BASE=$(gh pr view --json baseRefName --jq .baseRefName)
tx spawn-nvim "$SELF-diff" --tag "$TAGS" --cwd <worktree-path> --diff "$BASE"
```

The companion's `nvim` role is derived from its launch command — it is never a tag.

## Spawning sub-workers

You are not limited to the nvim companions above — you may spawn your own agent workers to parallelize independent parts of a plan (a coding worker per subsystem) or to answer an unknown before you build (a research worker). Follow **COMMON § Spawning workers** for the recipe: the `tx spawn … --cmd 'claude …'` pattern (Claude by default), the `--env TX_REQUIRE_WORKTREE=1` for coding sub-workers, and the role-file priming string that makes a sub-worker load these same conventions. Always prime — a bare agent spawn gets you a worker that ignores all of this.
