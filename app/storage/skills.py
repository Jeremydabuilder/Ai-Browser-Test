"""Custom Skills persisted in SQLite. Built-in Skills never appear here -
see app/agent/skills.py's BUILTIN_SKILLS - so there is nothing in this
store to accidentally overwrite or delete for one; the only way from a
built-in to something this store holds is duplicate().
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.storage.database import Database

if TYPE_CHECKING:
    from app.agent.skills import Skill

MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 400
MAX_INSTRUCTIONS_CHARS = 8000


def _row_to_skill(row) -> "Skill":
    # Imported lazily, not at module level: app.agent (via app.agent.session
    # -> app.agent.tools -> app.browser.controller -> ... -> app.missions ->
    # app.storage.database) is reached again while app.storage's own
    # __init__ is still mid-import the first time anything imports
    # app.storage - a real circular import, not merely a style preference.
    from app.agent.skills import Skill

    allowed_tools = row["allowed_tools"]
    output_schema = row["output_schema"]
    return Skill(
        id=row["id"], name=row["name"], description=row["description"],
        instructions=row["instructions"],
        allowed_tools=tuple(json.loads(allowed_tools)) if allowed_tools is not None else None,
        output_schema=json.loads(output_schema) if output_schema is not None else None,
        preferred_provider=row["preferred_provider"], preferred_model=row["preferred_model"],
        default_context_kinds=tuple(json.loads(row["default_context_kinds"])),
        builtin=False, version=row["schema_version"],
    )


class SkillStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def all(self) -> list[Skill]:
        rows = self._db.query("SELECT * FROM skills ORDER BY name COLLATE NOCASE")
        return [_row_to_skill(row) for row in rows]

    def get(self, skill_id: str) -> Skill | None:
        row = self._db.query_one("SELECT * FROM skills WHERE id = ?", (skill_id,))
        return _row_to_skill(row) if row is not None else None

    def contains(self, skill_id: str) -> bool:
        return self._db.query_one("SELECT 1 FROM skills WHERE id = ?", (skill_id,)) is not None

    def save(self, skill: "Skill") -> "Skill | None":
        """Insert or update. Refuses a blank name - a nameless Skill in the
        library helps no one find it again."""
        from app.agent.skills import SKILL_SCHEMA_VERSION

        name = (skill.name or "").strip()[:MAX_NAME_CHARS]
        if not name:
            return None
        description = (skill.description or "").strip()[:MAX_DESCRIPTION_CHARS]
        instructions = (skill.instructions or "").strip()[:MAX_INSTRUCTIONS_CHARS]
        allowed_tools = (json.dumps(list(skill.allowed_tools))
                        if skill.allowed_tools is not None else None)
        output_schema = json.dumps(skill.output_schema) if skill.output_schema is not None else None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing = self.get(skill.id)
        created_at = now if existing is None else self._db.query_one(
            "SELECT created_at FROM skills WHERE id = ?", (skill.id,))["created_at"]
        self._db.execute(
            "INSERT INTO skills (id, name, description, instructions, allowed_tools, "
            "output_schema, preferred_provider, preferred_model, default_context_kinds, "
            "schema_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, description=excluded.description, "
            "instructions=excluded.instructions, allowed_tools=excluded.allowed_tools, "
            "output_schema=excluded.output_schema, preferred_provider=excluded.preferred_provider, "
            "preferred_model=excluded.preferred_model, "
            "default_context_kinds=excluded.default_context_kinds, "
            "schema_version=excluded.schema_version, updated_at=excluded.updated_at",
            (skill.id, name, description, instructions, allowed_tools, output_schema,
             skill.preferred_provider, skill.preferred_model,
             json.dumps(list(skill.default_context_kinds)), SKILL_SCHEMA_VERSION,
             created_at, now))
        return self.get(skill.id)

    def remove(self, skill_id: str) -> None:
        self._db.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
