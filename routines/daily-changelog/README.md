# daily-changelog routine

A daily, per-project changelog generator (AND-176). Each morning it collects the
previous day's git activity for each tracked project, summarizes it into a
readable "what changed" (not a raw `git log`), and prepends a dated entry to that
project's **Linear changelog doc**.

Currently tracked: **tx-ide** and **polymarket** (the `polymarket` repo's
changelog lives under the `poly-trading` Linear project).

## How it works

```
launchd (06:00 Europe/Warsaw)
   └─ run.sh
        1. git fetch each project's origin
        2. collect.py  →  digest-<date>.json   (deterministic: commits + merged PRs + URLs)
        3. tx spawn changelog-<date> --tag daily-changelog,routine --engine claude
              └─ the agent reads the digest, follows AGENT.md, and via the Linear MCP
                 prepends a "## <date>" entry to each project's changelog doc,
                 then writes done-<date>.json (the sentinel)
        4. run.sh sees the sentinel and ends the session (record kept; `tx resume` to inspect)
```

The split is deliberate: **data collection is deterministic** (`collect.py`, a
stdlib-only Python script — no `gh`, no network beyond the fetch), while the
**fuzzy summarization and the Linear write run inside a real tx-spawned Claude
session**, so the Linear MCP is available with the same auth as any interactive
session. Every run is a tracked tx session tagged `routine` — find them with
`tx history --tag routine`.

## Files

| File | Purpose |
| --- | --- |
| `config.json` | tracked projects: repo path, default branch, Linear doc id/url |
| `collect.py` | deterministic git-activity collector → JSON digest |
| `AGENT.md` | spec the spawned agent follows (summarize + Linear write + sentinel) |
| `run.sh` | launchd wrapper: fetch → collect → `tx spawn` → wait → teardown |
| `com.wiktor.daily-changelog.plist.template` | launchd LaunchAgent (rendered at install) |
| `install.sh` | copies the routine to `~/.tx-ide/routines/daily-changelog/`, renders + (optionally) loads the schedule |

## Install / enable (on the mac-mini)

```bash
# from the repo checkout:
routines/daily-changelog/install.sh          # install files + render plist (does NOT enable yet)

# validate with a manual run for a specific day:
~/.tx-ide/routines/daily-changelog/run.sh --date 2026-06-13

# once happy, enable the daily 06:00 schedule:
launchctl load ~/Library/LaunchAgents/com.wiktor.daily-changelog.plist
# (or: routines/daily-changelog/install.sh --load)
```

Disable again with `launchctl unload ~/Library/LaunchAgents/com.wiktor.daily-changelog.plist`.

## Manual / debugging runs

```bash
run.sh                      # yesterday (Europe/Warsaw), full run
run.sh --date 2026-06-13    # a specific day
run.sh --no-spawn           # collector only — print the digest, no agent, no Linear write
collect.py --config config.json --date 2026-06-13 --format markdown   # eyeball the raw activity
```

Logs: `~/.tx-ide/routines/daily-changelog/logs/launchd.log`. Per-day artifacts
(`digest-<date>.json`, `done-<date>.json`) live in `…/runs/`.

## Add or change a tracked project

Edit `config.json` (add an entry with `repo`, `branch`, and a `linear_doc_id` for
a changelog doc you've created in Linear), then re-run `install.sh`. Empty days
are skipped, so a project with no activity adds no entry.

## Requirements on the host

- `claude` CLI logged in, with the **Linear MCP authenticated** (the agent writes
  to Linear through it). If the MCP needs auth, the agent records an `error` in the
  sentinel and writes nothing — authenticate once interactively (`claude` → `/mcp`).
- `tx` on `PATH` (the routine spawns its worker through tx).
- `git`, `python3` (system Python is fine). `gh` is **not** required — merged PRs
  are detected from merge commits and squash-merge `(#N)` subjects.
- Timezone: the schedule uses local time; the box must be `Europe/Warsaw` for
  06:00 Warsaw (verify with `readlink /etc/localtime`).
