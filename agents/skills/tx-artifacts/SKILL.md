---
name: tx-artifacts
description: Use when about to create or update a durable deliverable — a plan, doc, report, or question set. The tx artifact workflow; subcommand syntax is in `tx artifact --help`.
---

# tx artifact workflow

Subcommand syntax is in `tx artifact --help`. The workflow:

- Register a deliverable once with `tx artifact create <file>`, then snapshot each meaningful
  revision with `tx artifact modify <id>` (no file argument snapshots the working copy). Pass
  `--changes` so the revision log reads like history. The session that ran the command is
  recorded automatically.
- `tx artifact open <id>` opens the working copy in an nvim view already bound to the artifact —
  use it instead of hand-spawning an nvim companion on the file.
- Revisions are immutable and live under `$TX_IDE_HOME/artifacts/` — never write there by hand; a
  direct edit desyncs the history from disk.
