# COMMON — every session must follow this

These conventions apply to every agent session in this system: the tx-assistant, every worker, and any ad-hoc session you spin up inside the orchestration repo.

## Session self-introduction

Lead your **first response** in a session with a brief self-introduction so the operator can confirm your setup at a glance:

1. Your tx session name (`tx whoami`) + cwd and current git branch.
2. Which convention/role files you have loaded — always this `COMMON.md`; state whether a role file (`DEVELOPER.md`, etc.) is **also** loaded, and quote one identifying line from each so it is verifiable.
3. One line on your purpose.

Keep it to a few lines, then continue with whatever was asked (or wait for instructions if nothing was).

## Spawning sessions

Spawning goes through `tx`, never raw `tmux` — and so does anything else that creates, ends, or
changes a session, because raw `tmux` leaves tx's session record stale. Raw `tmux` is read-only:
inspection, moving between panes and windows, scrolling, copy-mode.

Three kinds of spawn — the role in each comment is derived from the launch command, not passed:

```bash
# shell / other — an ad-hoc process
tx spawn <name> --tag TAGS [--cwd DIR] [--cmd "CMD"] [--env K=V ...]

# llm — an agent worker
tx spawn <name> --tag TAGS --cwd DIR --engine ENGINE [--model MODEL] [--effort {1,2,3,4,5}] [--role NAME[,NAME…]] [--read-only] [--prompt TEXT]

# nvim — a companion for reading files and diffs
tx spawn-nvim <name> --tag TAGS [--cwd DIR] [--diff [BASE]] [--open FILE] [--env K=V ...]
```

All three require `--tag`.

**Tags** are how the operator finds related sessions later. Give each session one tag naming the
work it belongs to:

```bash
tx spawn build-watch --tag wrangler-p1 --cmd 'npm run watch'
tx spawn-nvim wrangler-p1-diff --tag wrangler-p1 --diff main
```

**An nvim companion takes the same tag as the session it belongs to** — that is what makes the pair
surface together when the operator filters by scope.

Never tag a session with its role (`llm` / `nvim` / `shell`); the role is derived from the launch
command and already has its own column.

**Naming:** human-readable, says what it's for. The tag does the filtering, not the name. Groups
derive from lineage automatically — set nothing.

To delegate work — coding, scoping, planning, research — fill in the `llm` form:

```bash
tx spawn <name> --tag <scope> --cwd <project-root> \
  --engine claude --model "opus[1m]" --effort 5 \
  --role DEVELOPER --prompt "<task>"
```

- `--role` — **what makes the worker read these conventions**: it injects the role files into the
  spawned session's system prompt, so without it the worker sees none of this. Roles live in
  `~/.tx-ide/agents/`; `DEVELOPER` is the usual one, and an unknown name fails the spawn.
- `--effort` — `1` to `5`, low to max, defaulting to `3`.
- `--prompt` — the task, short and single-line. Long or special-char-laden prompts crash tmux input.
- `--read-only` — for an investigation that must not write; not combinable with `--cmd`. Promote it
  later with `tx fork <investigation> <implementation>`, which is writable.

Every worker gets its own tx-owned worktree automatically — never create one yourself.

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

**Sending:** `tx send-message <target> "<body>"`, where `<target>` is the display name from `tx ls`.
Single-line only — escape literal newlines as `\n`.

**Discovery:** `tx ls` shows peers by display name + tags. Find your own with `tx whoami` (`#S` is
your session id, not your name).

## Artifacts

Durable deliverables — plans, docs, reports, question sets — are **artifacts**: a versioned record
under `$TX_IDE_HOME/artifacts/`. Always use these; never an engine's own built-in artifact or
publish tool.

```bash
tx artifact create <file>           # register a new one
tx artifact modify <id> [<file>]    # snapshot a revision (no file = the working copy)
tx artifact ls | show <id> | diff <id> | open <id>
```

Never write under `artifacts/` by hand — revisions are immutable, and a direct edit desyncs the
history from disk. The session that ran the command is recorded automatically.
