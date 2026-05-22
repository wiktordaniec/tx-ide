"""Shared mailbox paths for tx-mailbox and mx_speaker.py."""

import os
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
MAILBOX_DIR = CLAUDE_DIR / "mailbox"
INBOX_FILE = MAILBOX_DIR / "inbox.jsonl"
RUNNING_DIR = MAILBOX_DIR / "running.d"
CONFIG_FILE = MAILBOX_DIR / "config.json"
