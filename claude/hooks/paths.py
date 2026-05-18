"""Shared mailbox paths for mx.py and mx_speaker.py."""

from pathlib import Path

MAILBOX_DIR = Path.home() / ".claude" / "mailbox"
INBOX_FILE = MAILBOX_DIR / "inbox.jsonl"
RUNNING_DIR = MAILBOX_DIR / "running.d"
CONFIG_FILE = MAILBOX_DIR / "config.json"
