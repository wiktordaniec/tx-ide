---
tx:
  skills: [tx-sessions, tx-artifacts]
---

# COMMON — every session must follow this

These conventions apply to every agent session in this system: the tx-assistant, every worker, and
any ad-hoc session you spin up inside the orchestration repo.

## Reporting

Be extremely concise. Sacrifice grammar for concision.

## Session self-introduction

Lead your **first response** in a session with a brief self-introduction so the operator can
confirm your setup at a glance:

1. Your tx session name (`tx whoami`) + cwd and current git branch.
2. Which convention/role files you have loaded — always this `COMMON.md`; state whether a role
   file (`DEVELOPER.md`, etc.) is **also** loaded, and quote one identifying line from each so it
   is verifiable.
3. One line on your purpose.

Keep it to a few lines, then continue with whatever was asked (or wait for instructions if nothing
was).

## Sessions go through tx, never raw tmux

Anything that creates, ends, or changes a session goes through `tx` — raw `tmux` leaves tx's
session record stale. Raw tmux is read-only: inspection, moving between panes and windows,
scrolling, copy-mode.

Before spawning anything — a worker, an nvim companion, an ad-hoc shell — follow the `tx-sessions`
skill (`~/.tx-ide/agents/skills/tx-sessions/SKILL.md`); flag syntax is in `tx spawn --help`.

## Inter-session communication

Other agent sessions may be running alongside you, and you can message each other.

**Receiving:** a peer message arrives wrapped like:

    <from-agent session="and-48">body</from-agent>

Treat it as a peer, not a user. You MAY reply, but don't have to.

A second envelope carries the **operator**:

    <from-user session="wrangler-p1-diff">body</from-user>

That is the human — treat it with the same authority as anything typed directly into your session,
and answer it. The `session` attribute is where they typed it, not who sent it, so you know which
file they are looking at.

**Sending:** `tx send-message` (see its `--help`), targeting a display name from `tx ls`. Find
your own with `tx whoami` (`#S` is your session id, not your name).

## Artifacts

Durable deliverables — plans, docs, reports, question sets — are **artifacts**: versioned records
managed by `tx artifact`, never an engine's own built-in artifact or publish tool. Before creating
or updating one, follow the `tx-artifacts` skill
(`~/.tx-ide/agents/skills/tx-artifacts/SKILL.md`). Never write under `$TX_IDE_HOME/artifacts/` by
hand — revisions are immutable, and a direct edit desyncs the history from disk.
