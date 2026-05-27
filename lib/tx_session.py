"""tx session store — Session entity + SessionStore repository over per-uuid JSON."""

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path.home() / ".tx-ide" / "sessions"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Session:
    id: str = ""
    name: str = ""
    kind: str = ""
    tags: list = field(default_factory=list)
    cwd: str = ""
    cmd: str = ""
    env: dict = field(default_factory=dict)
    parent: str = ""
    pid: int = 0
    created_at: str = ""
    restarts: int = 0
    handover: str = ""
    chats: list = field(default_factory=list)

    LIST_FIELDS = {"tags", "chats"}
    NUMERIC_FIELDS = {"pid", "restarts"}

    @classmethod
    def create(cls, id, name, kind, tags, cwd, cmd, env, parent, pid, chat):
        return cls(
            id=id or str(uuid.uuid4()),
            name=name,
            kind=kind,
            tags=tags,
            cwd=cwd,
            cmd=cmd,
            env=env,
            parent=parent,
            pid=pid,
            created_at=now_iso(),
            chats=[chat] if chat else [],
        )

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    def to_dict(self):
        return asdict(self)

    def bump(self, pid, chat, handover):
        self.restarts += 1
        self.pid = pid
        if chat:
            self.chats.append(chat)
        if handover:
            self.handover = handover


class SessionStore:
    def __init__(self, root=SESSIONS_DIR):
        self.root = root

    def session_path(self, session_id):
        return self.root / f"{session_id}.json"

    def load(self, session_id):
        path = self.session_path(session_id)
        if not path.exists():
            return None
        return Session.from_dict(json.loads(path.read_text()))

    def save(self, session):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.session_path(session.id)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(json.dumps(session.to_dict(), indent=2) + "\n")
        os.replace(temp_path, path)

    def all(self):
        if not self.root.exists():
            return []
        return [
            Session.from_dict(json.loads(path.read_text()))
            for path in sorted(self.root.glob("*.json"))
        ]

    def find_by_name(self, name):
        matches = [session for session in self.all() if session.name == name]
        if not matches:
            return None
        matches.sort(key=lambda session: (session.created_at, session.id))
        return matches[-1]
