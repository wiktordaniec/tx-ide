---
name: tx-sessions
description: Use when about to spawn or delegate to a tx session — an agent worker, an nvim companion, or an ad-hoc shell. Conventions for tags, naming, and the worker recipe; flag syntax is in `tx spawn --help`.
---

# tx session conventions

Syntax is the CLI's job — `tx spawn --help` and `tx spawn-nvim --help` are authoritative. This
file carries only what the flags cannot express.

## Tags

Tags are how the operator finds related sessions later. Give each session one tag naming the work
it belongs to:

```bash
tx spawn build-watch --tag wrangler-p1 --cmd 'npm run watch'
tx spawn-nvim wrangler-p1-diff --tag wrangler-p1 --diff main
```

An nvim companion takes the same tag as the session it belongs to — that is what makes the pair
surface together when the operator filters by scope.

Never tag a session with its role (`llm` / `nvim` / `shell`); the role is derived from the launch
command and already has its own column.

## Naming and groups

Names are human-readable and say what the session is for; the tag does the filtering, not the
name. Groups derive from lineage automatically — set nothing.

## Delegating to a worker

To delegate work — coding, scoping, planning, research — the standard recipe is:

```bash
tx spawn <name> --tag <scope> --cwd <project-root> \
  --engine claude --model "opus[1m]" --effort 5 \
  --role DEVELOPER --prompt "<task>"
```

- `--role` is what injects the shared conventions into the worker's system prompt — without it
  the worker sees none of them. Roles live in `~/.tx-ide/agents/`; `DEVELOPER` is the usual one.
- `--prompt` must be short and single-line — long or special-char-laden prompts crash tmux input.
- `--read-only` for an investigation that must not write; promote it later with
  `tx fork <investigation> <implementation>`, which is writable.

Every worker gets its own tx-owned worktree automatically — never create one yourself.
