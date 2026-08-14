---
name: tx-diff-review
description: Use when a code change is ready for the user to read — spawn a -diff nvim companion so they can review it without context-switching.
---

# Diff review companions

When a change is ready to read, spawn a `-diff` nvim companion:

```bash
SELF=$(tx whoami)
tx spawn-nvim "$SELF-diff" --tag "$(tx tag "$SELF")" --cwd <worktree> --diff <base>
```

- The companion inherits your tag, so the pair surfaces together in the operator's filters.
- Base the diff on the **merge-base**, not `main` — once main moves ahead, `--diff main` shows
  every commit you don't have as a deletion.
