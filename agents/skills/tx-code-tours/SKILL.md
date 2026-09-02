---
name: tx-code-tours
description: Use when the user asks for a tour or walkthrough of code. Investigate the code, then annotate an operator's live nvim with session-local vim.diagnostic notes and ordered quickfix navigation. Never offer a tour unprompted.
---

# Code tours with nvim diagnostics

Show reasoning on the code using the bundled `scripts/apply_tour.py` runtime. Do not inline the runtime or use the JSON/`:TourApply` workflow.

## Build the tour

Investigate first. Order stops pedagogically—entry point, dispatch, edge cases, callers/tests—not by filename. Explain why each location matters rather than restating its code.

Write a Lua data file under a scratch directory. Use absolute paths, 1-based lines, labels of about five words prefixed `N/M`, plain-text bodies, and severity `WARN`, `INFO`, or `HINT`:

```lua
return {
  {
    file = "/absolute/path/to/source.ts",
    line = 42,
    label = "1/3 entry point",
    message = [[This boundary chooses the route tree.

Its result controls every downstream match.]],
    severity = "INFO",
  },
}
```

## Apply the tour

Use an existing nvim companion. If none exists, read the `tx-sessions` skill and create one through `tx`. Never create or mutate sessions with raw tmux.

Resolve the companion's live tx record UUID, then inspect its pane read-only to find the nvim socket:

```bash
pane_process_id=$(tmux list-panes -st <tx-record-uuid> -F '#{pane_pid}' | head -1)
nvim_process_id=$(pgrep -P "$pane_process_id" | head -1)
socket_path=$(lsof -U -a -p "$nvim_process_id" | awk '{print $NF}' | grep nvim | head -1)
```

Run the bundled helper with the socket and stop-data file:

```bash
python3 <skill-directory>/scripts/apply_tour.py "$socket_path" /tmp/code-tour/stops.lua
```

Apply it yourself. It opens at stop 1. Tell the operator: `]n` / `[n` navigate, `<leader>cn` opens the note. `<leader>cN` restarts, `<leader>cl` lists stops, and `:TourClear` clears the tour.

Keep all effects session-local. Never write repository files through RPC or globally reconfigure LSP diagnostics. Tours disappear on nvim restart.
