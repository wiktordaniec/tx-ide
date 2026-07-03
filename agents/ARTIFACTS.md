# ARTIFACTS skill — register what you deliver

Anything you produce that a human would want to find and review later is an **artifact**: a plan,
a branch diff, a walkthrough of what you changed and how you verified it, a synthesis report, a
design doc. This skill layers on top of any role (a DEVELOPER, a HISTORIAN, an orchestrator's
workers all follow it): **the moment a deliverable exists, register it** — one command, right
after you create the thing. An unregistered deliverable is invisible: the human would have to
remember which session made it and where it wrote it.

The human browses everything with `tx artifacts` (an fzf picker like `tx attach`) and presses
Enter to open your deliverable in an nvim companion — a plan opens as a file, a diff opens as a
diffview against its base. So a registration is not bookkeeping; it is how your work gets seen.

## Registering

```bash
# a file deliverable (plan / walkthrough / report / doc):
tx artifact add --type plan --title "auth rewrite plan" <path/to/plan.md>

# a branch diff (register once the PR is open; no path — it records the repo + base):
tx artifact add --type diff --base "$BASE" --title "auth rewrite (PR #123)"
```

- `--type` is one of `plan` / `diff` / `walkthrough` / `report` / `doc`. Use `walkthrough` for the
  post-completion "what changed and how I verified it" write-up, `report` for research/synthesis
  output (e.g. a HISTORIAN synthesis), `doc` for anything else worth keeping.
- **Producer and tags are automatic** — the command reads `$TX_SESSION_ID` and inherits your
  session's scope tags, so the artifact surfaces next to you when the human filters by scope.
  Pass `--tag` only to override; don't invent a scope.
- `--title` is what the human sees in the picker — say what it is, mention the PR number for a
  diff. Default is the file name, which is usually too vague.
- The repo defaults to your cwd; pass `--repo` when registering from elsewhere. For a diff
  artifact the repo matters: opening it runs a diffview *in that directory*, so register a diff
  from inside the worktree that has the branch checked out.

**When to register, by moment:**

| moment | register |
|---|---|
| you wrote (or were handed) a plan | `--type plan` with the plan path |
| your PR is open | `--type diff --base <the PR's base>` |
| you finished and wrote up what you did + how you verified it | `--type walkthrough` |
| you produced a research/synthesis document | `--type report` |

Registering **complements** the nvim companions your role may already call for (DEVELOPER
§ "nvim companions for review"): the companion is the push — review right now, next to you; the
artifact is the durable index — findable later, from anywhere. Do both in the same breath.

## Snapshot semantics (why registration is safe to do early)

A file artifact is **snapshotted** into `$TX_IDE_HOME/artifacts/<id>/` at register time, and the
original path is kept. Opening prefers the live file (keep editing your plan after registering —
the picker shows the current version) and falls back to the snapshot once the source is gone (a
plan inside a removed worktree survives as its snapshot). So register early, don't wait for the
"final" version; if the file moves, register the new location (`tx artifact rm` the stale one).

A diff artifact snapshots nothing — it opens live git state, and degrades when the worktree or
branch is gone. That is one more reason to keep a worktree around until its PR is merged.

## Housekeeping

- `tx artifact ls [--type TYPE] [--tag TAG]` — plain listing (ids, targets), pipe-friendly.
- `tx artifact rm <id-or-prefix>` — remove a stale or superseded registration (the snapshot goes
  with it). Prefer removing a superseded artifact over letting near-duplicates pile up — the
  picker's value decays with clutter.
- Every add/rm writes one provenance line to `$TX_IDE_HOME/log.jsonl`, like every tx mutation.
