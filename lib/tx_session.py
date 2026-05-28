"""tx session store — Session entity + SessionStore repository over per-uuid JSON."""

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Set

SESSIONS_DIR = Path.home() / ".tx-ide" / "sessions"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Kind(StrEnum):
    PROCESS = "process"
    VIEW = "view"

    @classmethod
    def coerce(cls, value: str) -> "Kind":
        return cls.VIEW if value == cls.VIEW.value else cls.PROCESS


@dataclass
class Session:
    id: str
    name: str
    kind: Kind
    tags: List[str]
    cwd: str
    cmd: str
    env: Dict[str, str]
    parent: str
    pid: int
    created_at: str
    chats: List[str]

    LIST_FIELDS: ClassVar[Set[str]] = {"tags", "chats"}
    NUMERIC_FIELDS: ClassVar[Set[str]] = {"pid"}

    @classmethod
    def create(cls, id: str, name: str, kind: str, tags: List[str], cwd: str,
               cmd: str, env: Dict[str, str], parent: str, pid: int,
               chat: str) -> "Session":
        return cls(
            id=id or str(uuid.uuid4()),
            name=name,
            kind=Kind.coerce(kind),
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
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        values = dict(data)
        values["kind"] = Kind.coerce(values["kind"])
        return cls(**values)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SessionStore:
    def __init__(self, root: Path = SESSIONS_DIR) -> None:
        self.root = root

    def session_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def load(self, session_id: str) -> Optional[Session]:
        path = self.session_path(session_id)
        if not path.exists():
            return None
        return Session.from_dict(json.loads(path.read_text()))

    def save(self, session: Session) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.session_path(session.id)
        temp_path = path.with_name(path.name + ".tmp")
        temp_path.write_text(json.dumps(session.to_dict(), indent=2) + "\n")
        os.replace(temp_path, path)

    def all(self) -> List[Session]:
        if not self.root.exists():
            return []
        return [
            Session.from_dict(json.loads(path.read_text()))
            for path in sorted(self.root.glob("*.json"))
        ]

    def find_by_name(self, name: str) -> Optional[Session]:
        matches = [session for session in self.all() if session.name == name]
        if not matches:
            return None
        matches.sort(key=lambda session: (session.created_at, session.id))
        return matches[-1]
