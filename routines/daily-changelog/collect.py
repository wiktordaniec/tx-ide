#!/usr/bin/env python3
"""Collect the previous day's git activity for each configured project.

Deterministic and stdlib-only, so it runs under the macOS system ``python3``
with no install step. It emits a JSON digest that the daily-changelog routine
agent turns into a human-readable changelog entry — the agent never runs git
itself, it only summarizes what this script produced.

Per project, for one calendar day (default: yesterday in the configured
timezone) it gathers, from ``origin/<branch>`` (so it sees what actually
landed), the commits and merged pull requests of that day, each with a web URL
back to GitHub.

Usage::

    collect.py --config CONFIG [--date YYYY-MM-DD] [--output FILE]
               [--format json|markdown] [--fetch]
    collect.py --config CONFIG --print-date     # the default target date
    collect.py --config CONFIG --print-repos    # configured repo paths, one per line
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # zoneinfo is stdlib on Python 3.9+; fall back to naive local time
    ZoneInfo = None

# Control characters used to delimit git-log output unambiguously: a commit body
# can contain newlines and almost anything else, but not these.
FIELD_SEPARATOR = "\x1f"
RECORD_SEPARATOR = "\x1e"

PULL_REQUEST_IN_SUBJECT = re.compile(r"\(#(\d+)\)")
MERGE_COMMIT_SUBJECT = re.compile(r"Merge pull request #(\d+) from \S+")


def resolve_timezone(timezone_name):
    if ZoneInfo is None:
        return None
    return ZoneInfo(timezone_name)


def default_date(timezone_name):
    """Yesterday, as a date, in the configured timezone."""
    timezone = resolve_timezone(timezone_name)
    now = datetime.now(timezone) if timezone else datetime.now()
    return (now - timedelta(days=1)).date()


def day_window(date, timezone_name):
    """The ``--since`` / ``--until`` ISO strings spanning one calendar day."""
    timezone = resolve_timezone(timezone_name)
    start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=timezone)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def git(repo, *arguments):
    """Run a git command in ``repo``; return ``(ok, stdout, stderr)``.

    Git is a system boundary, so a non-zero exit is reported, not raised.
    """
    completed = subprocess.run(
        ["git", "-C", repo, *arguments],
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0, completed.stdout, completed.stderr


def web_url_from_remote(repo):
    """The https web base for the origin remote, or None."""
    ok, stdout, _ = git(repo, "remote", "get-url", "origin")
    if not ok:
        return None
    remote = stdout.strip()
    if remote.endswith(".git"):
        remote = remote[: -len(".git")]
    scp_like = re.match(r"^git@([^:]+):(.+)$", remote)
    if scp_like:
        return f"https://{scp_like.group(1)}/{scp_like.group(2)}"
    ssh_url = re.match(r"^ssh://git@([^/]+)/(.+)$", remote)
    if ssh_url:
        return f"https://{ssh_url.group(1)}/{ssh_url.group(2)}"
    return remote


def resolve_ref(repo, branch):
    """Prefer ``origin/<branch>`` (what landed on the remote) over the local branch."""
    for candidate in (f"origin/{branch}", branch, "HEAD"):
        ok, _, _ = git(repo, "rev-parse", "--verify", "--quiet", candidate)
        if ok:
            return candidate
    return "HEAD"


def parse_records(stdout, field_count):
    """Split git-log output formatted with our record/field separators."""
    records = []
    for raw in stdout.split(RECORD_SEPARATOR):
        raw = raw.strip("\n")
        if not raw:
            continue
        fields = raw.split(FIELD_SEPARATOR)
        if len(fields) >= field_count:
            records.append(fields)
    return records


def collect_commits(repo, ref, since, until, web_url):
    pretty = FIELD_SEPARATOR.join(["%H", "%h", "%an", "%cI", "%s", "%b"]) + RECORD_SEPARATOR
    ok, stdout, stderr = git(
        repo, "log", ref,
        f"--since={since}", f"--until={until}",
        "--no-merges", "--date=iso-strict", f"--pretty=format:{pretty}",
    )
    if not ok:
        return [], stderr.strip()
    commits = []
    for fields in parse_records(stdout, 6):
        full_hash, short_hash, author, committed_at, subject, body = fields[:6]
        pull_request = PULL_REQUEST_IN_SUBJECT.search(subject)
        commits.append({
            "hash": short_hash,
            "author": author,
            "committed_at": committed_at,
            "subject": subject.strip(),
            "body": body.strip(),
            "url": f"{web_url}/commit/{full_hash}" if web_url else None,
            "pull_request": int(pull_request.group(1)) if pull_request else None,
        })
    return commits, None


def collect_merge_commits(repo, ref, since, until):
    """Return ``{pr_number: title}`` for GitHub merge-commit style merges."""
    pretty = FIELD_SEPARATOR.join(["%s", "%b"]) + RECORD_SEPARATOR
    ok, stdout, _ = git(
        repo, "log", ref, "--merges", "--first-parent",
        f"--since={since}", f"--until={until}", f"--pretty=format:{pretty}",
    )
    titles = {}
    if not ok:
        return titles
    for fields in parse_records(stdout, 1):
        subject = fields[0]
        body = fields[1] if len(fields) > 1 else ""
        match = MERGE_COMMIT_SUBJECT.search(subject)
        if not match:
            continue
        number = int(match.group(1))
        body_title = next((line.strip() for line in body.splitlines() if line.strip()), "")
        titles[number] = body_title or subject.strip()
    return titles


def merged_pull_requests(commits, merge_titles, web_url):
    """Unify PRs from merge commits and from squash-merge ``(#N)`` subjects."""
    pull_requests = {}
    for number, title in merge_titles.items():
        pull_requests[number] = title
    for commit in commits:
        number = commit["pull_request"]
        if number and number not in pull_requests:
            pull_requests[number] = commit["subject"]
    return [
        {
            "number": number,
            "title": pull_requests[number],
            "url": f"{web_url}/pull/{number}" if web_url else None,
        }
        for number in sorted(pull_requests, reverse=True)
    ]


def collect_project(project, since, until, do_fetch):
    repo = os.path.expanduser(project["repo"])
    entry = {
        "name": project["name"],
        "repo": repo,
        "branch": project.get("branch"),
        "linear_project": project.get("linear_project"),
        "linear_doc_id": project["linear_doc_id"],
        "linear_doc_url": project.get("linear_doc_url"),
        "commit_count": 0,
        "commits": [],
        "pull_requests": [],
        "error": None,
    }

    if not os.path.isdir(repo):
        entry["error"] = f"repo path not found: {repo}"
        return entry
    is_repo, _, _ = git(repo, "rev-parse", "--git-dir")
    if not is_repo:
        entry["error"] = f"not a git repository: {repo}"
        return entry

    if do_fetch:
        git(repo, "fetch", "--quiet", "--prune", "origin")

    branch = project.get("branch") or "HEAD"
    ref = resolve_ref(repo, branch)
    web_url = web_url_from_remote(repo)
    entry["ref"] = ref
    entry["web_url"] = web_url

    commits, error = collect_commits(repo, ref, since, until, web_url)
    if error:
        entry["error"] = error
        return entry
    merge_titles = collect_merge_commits(repo, ref, since, until)
    entry["commits"] = commits
    entry["commit_count"] = len(commits)
    entry["pull_requests"] = merged_pull_requests(commits, merge_titles, web_url)
    return entry


def build_digest(config, date, do_fetch):
    timezone_name = config.get("timezone", "Europe/Warsaw")
    since, until = day_window(date, timezone_name)
    generated_timezone = resolve_timezone(timezone_name)
    return {
        "generated_for": date.isoformat(),
        "generated_at": datetime.now(generated_timezone).isoformat(),
        "timezone": timezone_name,
        "since": since,
        "until": until,
        "projects": [collect_project(p, since, until, do_fetch) for p in config["projects"]],
    }


def render_markdown(digest):
    lines = [f"# Git activity for {digest['generated_for']}", ""]
    for project in digest["projects"]:
        lines.append(f"## {project['name']}")
        if project["error"]:
            lines.append(f"_error: {project['error']}_")
            lines.append("")
            continue
        if project["commit_count"] == 0 and not project["pull_requests"]:
            lines.append("_no activity_")
            lines.append("")
            continue
        for pull_request in project["pull_requests"]:
            lines.append(f"- PR #{pull_request['number']}: {pull_request['title']} ({pull_request['url']})")
        for commit in project["commits"]:
            lines.append(f"- {commit['hash']} {commit['subject']} ({commit['url']})")
        lines.append("")
        lines.append(f"_{project['commit_count']} commits_")
        lines.append("")
    return "\n".join(lines)


def load_config(path):
    with open(path) as handle:
        return json.load(handle)


def main(argv):
    parser = argparse.ArgumentParser(description="Collect a project's daily git activity.")
    parser.add_argument("--config", required=True, help="path to config.json")
    parser.add_argument("--date", help="target day YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--output", help="write the digest here (default: stdout)")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--fetch", action="store_true", help="git fetch each repo first")
    parser.add_argument("--print-date", action="store_true", help="print the default target date and exit")
    parser.add_argument("--print-repos", action="store_true", help="print configured repo paths and exit")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    timezone_name = config.get("timezone", "Europe/Warsaw")

    if args.print_date:
        print(default_date(timezone_name).isoformat())
        return 0
    if args.print_repos:
        for project in config["projects"]:
            print(os.path.expanduser(project["repo"]))
        return 0

    if args.date:
        date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        date = default_date(timezone_name)

    digest = build_digest(config, date, args.fetch)
    rendered = render_markdown(digest) if args.format == "markdown" else json.dumps(digest, indent=2)

    if args.output:
        with open(args.output, "w") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
