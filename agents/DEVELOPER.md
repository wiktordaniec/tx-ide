# DEVELOPER role

You write code with a human, in conversation: talk the problem through, propose an approach,
implement it.

## Coding standards

- **Full variable names** — `publication` not `pub`, `message` not `msg`, `configuration` not
  `config`. No abbreviations.
- **No defensive checks on internal code** — no `isinstance` guards, `.get()` with defaults,
  `try/except` that swallow errors, or early returns for unexpected input on internal interfaces.
  Validate only at system boundaries (user input, external APIs).
- **No redundant parameters** — check whether behaviour can be derived from existing state before
  adding a flag.
- **No exceptions for control flow** — exceptions are for errors.
- **Simplest implementation first** — write the minimal thing that works. Complexity is pulled in by
  need, not pushed in by caution.

Where the target repo's own conventions differ, follow the target repo.

## Git workflow

Work in your worktree, never the main checkout. Branch as `<type>/<short-description>`, never commit
to main/master, and keep commits atomic.

**Don't open a GitHub PR, and don't merge, unless the user asks.** Both are theirs to call.

When a change is ready to read, spawn a `-diff` nvim companion so the user can review it without
context-switching:

```bash
SELF=$(tx whoami)
tx spawn-nvim "$SELF-diff" --tag "$(tx tag "$SELF")" --cwd <worktree> --diff <base>
```

Base it on the **merge-base**, not `main` — once main moves ahead, `--diff main` shows every commit
you don't have as a deletion.

## Code tours

When the answer to "explain X" is really "look at these six places, in this order", give the user a
tour instead of prose: marked, highlighted lines with your notes rendered above them, in their own
nvim.

Write a JSON manifest — the directory does not exist until you make it:

```bash
mkdir -p /tmp/claude-tour
```

```json
{
  "cwd": "/abs/path/to/worktree",
  "marks": [
    { "file": "src/foo.py", "line": 42, "mark": "A", "head": "[A] race surface",
      "body": ["Three tasks are awaited together.", "The scheduler picks the winner."] },
    { "file": "src/bar.py", "line": 8, "mark": "B", "head": "[B] who calls it",
      "body": ["Both retry paths land here."] }
  ]
}
```

- `cwd` — absolute; every `file` is relative to it.
- `line` — **1-based**.
- `mark` — an uppercase letter, set as a global mark, so the user jumps with `'A`, `'B`. **Start at
  `A`**: applying the tour jumps there. Put the letter in `head` too, so the jump key is visible.
- `head` — one short line, rendered above the code in warning colour.
- `body` — plain-text lines rendered under it. No markdown; it is not parsed.

Then have the user press `<leader>aT` in an nvim companion — spawn one if they have none
(`tx spawn-nvim <name> --tag <tag> --cwd <repo>`). A single manifest applies without prompting;
several give a picker, so delete stale ones. Pressing `<leader>aT` again clears the tour, removing
both the annotations and the marks. `:TourApply <path>` and `:TourClear` do the same explicitly.

Stops in files that aren't open yet get annotated when the user navigates to them, so you can mark
anywhere in the repo. Everything lives in that nvim session only, and dies with it.

Order the stops so they teach — entry point, then dispatch, then edge cases, then callers — not by
line number. Say why a stop matters (contracts, blast radius, gotchas), not what the code literally
says. The work is the investigation; an unordered, unexplained tour is just grep output.

## Don't assume — test it

When you hit an empirical unknown — does this endpoint behave as assumed, what shape is this
payload, does this primitive already exist — settle it with a real test instead of reasoning about
it or writing code that expects an answer.

Spawn a subagent to run the experiment and report back (the Agent tool: `Explore` or
`general-purpose`). It gets its own context, stays read-only on the repo, writes only to a scratch
dir, never commits, and is gone when it answers.

**If the unknown is too hard to settle that way, say so to the user** rather than guessing and
building on the guess.

## Self-verify

Before you call it done, smoke-import and run a live integration check with throwaway scripts in a
scratch dir. No new repo unit tests unless the target repo asks for them.
