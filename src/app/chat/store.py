"""SQLite persistence for chat conversations and their message history."""

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite


TITLE_LENGTH = 80


class ConversationStore:
    """Store chat conversations; each assistant turn records its workflow thread."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def setup(self) -> None:
        await self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                thread_id TEXT,
                status TEXT,
                intent TEXT,
                route TEXT,
                citations TEXT NOT NULL DEFAULT '[]',
                suggestions TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chat_messages_conversation
                ON chat_messages (conversation_id, id);
            """
        )
        cursor = await self._connection.execute("PRAGMA table_info(chat_messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "suggestions" not in columns:
            # Databases created before follow-up suggestions existed.
            await self._connection.execute(
                "ALTER TABLE chat_messages "
                "ADD COLUMN suggestions TEXT NOT NULL DEFAULT '[]'"
            )
        await self._connection.commit()

    async def create(self, conversation_id: str, first_message: str) -> None:
        now = _now()
        title = " ".join(first_message.split())
        if len(title) > TITLE_LENGTH:
            title = title[: TITLE_LENGTH - 1].rstrip() + "…"
        await self._connection.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (conversation_id, title, now, now),
        )
        await self._connection.commit()

    async def exists(self, conversation_id: str) -> bool:
        cursor = await self._connection.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        )
        return await cursor.fetchone() is not None

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        cursor = await self._connection.execute(
            "SELECT id, title, created_at, updated_at FROM conversations "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {"id": row[0], "title": row[1], "created_at": row[2], "updated_at": row[3]}
            for row in await cursor.fetchall()
        ]

    async def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        cursor = await self._connection.execute(
            "SELECT role, content, thread_id, status, intent, route, citations, "
            "created_at, suggestions FROM chat_messages WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
        return [
            {
                "role": row[0],
                "content": row[1],
                "thread_id": row[2],
                "status": row[3],
                "intent": row[4],
                "route": row[5],
                "citations": json.loads(row[6]),
                "created_at": row[7],
                "suggestions": json.loads(row[8]),
            }
            for row in await cursor.fetchall()
        ]

    async def append(
        self,
        conversation_id: str,
        role: str,
        content: str,
        *,
        thread_id: str | None = None,
        status: str | None = None,
        intent: str | None = None,
        route: str | None = None,
        citations: list[str] | None = None,
        suggestions: list[str] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        await self._connection.execute(
            "INSERT INTO chat_messages (conversation_id, role, content, thread_id, "
            "status, intent, route, citations, suggestions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                role,
                content,
                thread_id,
                status,
                intent,
                route,
                json.dumps(citations or []),
                json.dumps(suggestions or []),
                now,
            ),
        )
        await self._connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        await self._connection.commit()
        return {
            "role": role,
            "content": content,
            "thread_id": thread_id,
            "status": status,
            "intent": intent,
            "route": route,
            "citations": citations or [],
            "suggestions": suggestions or [],
            "created_at": now,
        }

    async def delete(self, conversation_id: str) -> bool:
        await self._connection.execute(
            "DELETE FROM chat_messages WHERE conversation_id = ?", (conversation_id,)
        )
        cursor = await self._connection.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        )
        await self._connection.commit()
        return cursor.rowcount > 0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
