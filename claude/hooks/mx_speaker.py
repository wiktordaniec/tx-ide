#!/usr/bin/env python3
"""Watches inbox.jsonl and speaks announcements + 1m/5m reminders for new
unread entries. Decoupled from writers — every row that lands gets announced."""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import INBOX_FILE, CONFIG_FILE

HOOK_DIR = Path(__file__).resolve().parent
SPEAK_SCRIPT = HOOK_DIR / "speak.sh"
POLL_INTERVAL_SECONDS = 0.5
REMINDER_1_SECONDS = 60
REMINDER_2_SECONDS = 300


def load_entries():
    if not INBOX_FILE.exists():
        return []
    entries = []
    with INBOX_FILE.open() as inbox_handle:
        for raw_line in inbox_handle:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except Exception:
                continue
    return entries


def load_config():
    with CONFIG_FILE.open() as config_handle:
        return json.load(config_handle)


def speak(text):
    if not load_config()["tts"]["enabled"]:
        return
    subprocess.Popen(
        [str(SPEAK_SCRIPT), text],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def spoken_session(session_name):
    return session_name.replace("_", " ").replace("-", " ")


def current_mtime():
    try:
        return INBOX_FILE.stat().st_mtime
    except OSError:
        return 0.0


def main():
    entries = load_entries()
    seen_ids = {entry.get("id") for entry in entries if entry.get("id")}
    print(
        f"[mx-speaker] bootstrap: {len(seen_ids)} existing entries marked seen",
        flush=True,
    )

    # pending[id] = {"session": str, "created_at": monotonic_float, "fired": {1, 5}-subset}
    pending = {}
    last_mtime = current_mtime()

    while True:
        new_mtime = current_mtime()
        if new_mtime != last_mtime:
            entries = load_entries()
            last_mtime = new_mtime
            for entry in entries:
                entry_id = entry.get("id")
                if not entry_id or entry_id in seen_ids:
                    continue
                seen_ids.add(entry_id)
                session = entry.get("tmux_session") or ""
                if not session or session == "no-tmux":
                    continue
                if entry.get("read"):
                    continue
                name = spoken_session(session)
                print(f"[mx-speaker] new id={entry_id} session={session}", flush=True)
                speak(f"{name} is ready")
                pending[entry_id] = {
                    "session": session,
                    "created_at": time.monotonic(),
                    "fired": set(),
                }

        if pending:
            now = time.monotonic()
            entries_by_id = {e.get("id"): e for e in entries if e.get("id")}
            for entry_id in list(pending):
                entry = entries_by_id.get(entry_id)
                if entry is None or entry.get("read"):
                    del pending[entry_id]
                    continue
                state = pending[entry_id]
                age = now - state["created_at"]
                name = spoken_session(state["session"])
                if age >= REMINDER_2_SECONDS and 5 not in state["fired"]:
                    print(f"[mx-speaker] reminder@5m id={entry_id}", flush=True)
                    speak(f"{name} has been waiting for five minutes")
                    state["fired"].add(5)
                    del pending[entry_id]
                elif age >= REMINDER_1_SECONDS and 1 not in state["fired"]:
                    print(f"[mx-speaker] reminder@1m id={entry_id}", flush=True)
                    speak(f"{name} has been waiting for one minute")
                    state["fired"].add(1)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
