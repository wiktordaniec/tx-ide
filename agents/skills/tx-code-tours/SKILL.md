---
name: tx-code-tours
description: Use when the user asks for a tour of the code — annotate your reasoning as nvim marks on the code itself. Never offer one unprompted.
---

# Code tours

A tour shows your reasoning **on the code** rather than in chat: the places you looked, in the
order you thought about them, each annotated with what you concluded there.

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

Then apply it yourself — don't make the user press anything. Spawn an nvim companion if they have
none (`tx spawn-nvim <name> --tag <tag> --cwd <repo>`), resolve its socket, and send `:TourApply`:

```bash
pane_pid=$(tmux list-panes -st <tmux-session-id> -F '#{pane_pid}' | head -1)
nvim_pid=$(pgrep -P "$pane_pid" | head -1)
socket=$(lsof -U -a -p "$nvim_pid" | awk '{print $NF}' | grep nvim | head -1)
nvim --server "$socket" --remote-send ':TourApply /tmp/claude-tour/<name>.json<CR>'
```

`<tmux-session-id>` is the companion's record uuid, not its display name. `:TourClear` removes the
tour. Tell the user which marks to jump to; the tour opens on `A`.

Stops in unopened files are annotated when the user reaches them, so mark anywhere in the repo. The
tour lives in that nvim session only and dies with it.

Order the stops the way you actually reasoned — where you started, what that forced you to check
next — and put your conclusion in each note, not a description of the code. A stop that restates
the line it sits on is wasted; say why it mattered to you.
