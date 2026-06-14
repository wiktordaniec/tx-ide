# Daily Changelog Routine — agent spec

You are an **unattended automated routine** — a tx session tagged `routine`. No
human is watching. Do not ask questions and do not improvise scope: follow this
spec exactly, then finish by writing the sentinel file.

## Inputs (given in your spawn prompt)

- **Digest JSON path** — pre-computed git activity for the target date.
- **Sentinel path** — the JSON file you must write as your final action.
- **Target date** — the day being summarized (`YYYY-MM-DD`).

## Steps

1. **Read the digest JSON.** It has `generated_for` (the date) and `projects[]`.
   Each project has `name`, `linear_doc_id`, `linear_doc_url`, `commit_count`,
   `commits[]` (each `{hash, author, subject, body, url, pull_request}`),
   `pull_requests[]` (each `{number, title, url}`), and possibly `error`.
   **Trust the digest — never run git yourself.**

2. **For each project:**
   - If `error` is set → status `error`; do **not** touch its Linear doc.
   - If `commit_count` is 0 **and** `pull_requests` is empty → status
     `skipped_no_activity`; do **not** append (keep the changelog free of empty
     days).
   - Otherwise compose a concise, human-readable entry and prepend it to the
     project's Linear changelog doc:
     1. Call the Linear **get_document** tool with `linear_doc_id` to read the
        current content.
     2. Build a `## <target date>` section (format below).
     3. Insert it **immediately after the first `---` separator** (newest on
        top). If a `## <target date>` section already exists, **replace** it so
        re-runs stay idempotent. Remove the `_No entries yet…_` placeholder line
        if present. Preserve everything else verbatim.
     4. Call the Linear **save_document** tool with the same `linear_doc_id` and
        the full updated content.

3. **Write the sentinel** (shape below) using Bash. This is your last action.

## Entry format

```
## 2026-06-13

- <theme>: one-line description of what changed ([`abc1234`](commit-url), [#62](pr-url))
- <theme>: …

_5 commits · merged PRs: [#62](pr-url)_
```

Guidance:
- **Group** related commits into a few themed bullets — do not dump one bullet
  per commit. Lead with the most significant change.
- Every bullet links at least one commit or PR using the URLs from the digest
  (this satisfies "link back to the underlying commits/PRs").
- Keep it skimmable: aim for 2–6 bullets. Use each commit's `subject`, and its
  `body` only when it adds real signal.
- The trailing italic line states the commit count and links each merged PR;
  omit the "merged PRs" clause if there were none.
- Write plainly and factually. Use only what is in the digest; never invent
  changes.

## Sentinel shape

Write valid JSON to the sentinel path, e.g.:

```json
{
  "date": "2026-06-13",
  "finished_at": "<ISO timestamp>",
  "projects": [
    {"name": "tx-ide", "status": "written", "commit_count": 5, "pull_requests": [62], "doc_url": "https://linear.app/..."},
    {"name": "polymarket", "status": "skipped_no_activity"}
  ]
}
```

`status` is one of `written`, `skipped_no_activity`, `error`. Always write the
sentinel — even when every project was skipped or errored — so the wrapper that
spawned you can finish and tear your session down.

## Rules

- Only modify the changelog docs named in the digest. Never modify other Linear
  documents or issues.
- If the Linear tools fail (e.g. the MCP needs authentication), record the
  project's status as `error` with the message, still write the sentinel, and
  stop. Do not retry endlessly.
