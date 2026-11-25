from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite


class Database:
    ALLOWED_GIVEAWAY_FIELDS = {
        "title",
        "prize_text",
        "body_html",
        "photo_file_id",
        "deadline",
        "winners_count",
        "channels_json",
        "button_text",
        "target_chat_id",
        "status",
        "post_message_id",
        "photo_message_id",
        "post_chat_id",
        "closed_at",
    }

    def __init__(self, path: Path) -> None:
        self.path = path

    async def _connect(self) -> aiosqlite.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        return conn

    async def init(self) -> None:
        async with await self._connect() as conn:
            await conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS giveaways (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    prize_text TEXT NOT NULL,
                    body_html TEXT NOT NULL,
                    photo_file_id TEXT,
                    deadline TEXT NOT NULL,
                    winners_count INTEGER NOT NULL DEFAULT 1,
                    channels_json TEXT NOT NULL DEFAULT '[]',
                    button_text TEXT NOT NULL DEFAULT '🎟 Участвовать',
                    target_chat_id TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    post_message_id INTEGER,
                    photo_message_id INTEGER,
                    post_chat_id TEXT,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS participants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT NOT NULL,
                    joined_at TEXT NOT NULL,
                    UNIQUE(giveaway_id, user_id),
                    FOREIGN KEY(giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_giveaways_status_deadline
                    ON giveaways(status, deadline);
                CREATE INDEX IF NOT EXISTS idx_participants_giveaway
                    ON participants(giveaway_id);
                """
            )
            await conn.commit()

    async def get_setting(self, key: str) -> str | None:
        async with await self._connect() as conn:
            cursor = await conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return str(row["value"]) if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with await self._connect() as conn:
            await conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await conn.commit()

    async def create_giveaway(self, data: dict[str, Any]) -> int:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                """INSERT INTO giveaways (
                    title, prize_text, body_html, photo_file_id, deadline,
                    winners_count, channels_json, button_text, target_chat_id,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
                (
                    data["title"],
                    data["prize_text"],
                    data["body_html"],
                    data.get("photo_file_id"),
                    data["deadline"],
                    data["winners_count"],
                    data.get("channels_json", "[]"),
                    data.get("button_text", "🎟 Участвовать"),
                    data.get("target_chat_id"),
                    data["created_at"],
                ),
            )
            await conn.commit()
            return int(cursor.lastrowid)

    async def get_giveaway(self, giveaway_id: int) -> dict[str, Any] | None:
        async with await self._connect() as conn:
            cursor = await conn.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_current_giveaway(self) -> dict[str, Any] | None:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM giveaways WHERE status IN ('draft', 'published') ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_giveaway(self, giveaway_id: int, field: str, value: Any) -> None:
        if field not in self.ALLOWED_GIVEAWAY_FIELDS:
            raise ValueError(f"Недопустимое поле розыгрыша: {field}")
        async with await self._connect() as conn:
            await conn.execute(
                f"UPDATE giveaways SET {field} = ? WHERE id = ?",
                (value, giveaway_id),
            )
            await conn.commit()

    async def delete_draft(self, giveaway_id: int) -> bool:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                "DELETE FROM giveaways WHERE id = ? AND status = 'draft'",
                (giveaway_id,),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def add_participant(
        self,
        giveaway_id: int,
        user_id: int,
        username: str | None,
        full_name: str,
        joined_at: str,
    ) -> bool:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                """INSERT OR IGNORE INTO participants
                   (giveaway_id, user_id, username, full_name, joined_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (giveaway_id, user_id, username, full_name, joined_at),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def participant_count(self, giveaway_id: int) -> int:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS total FROM participants WHERE giveaway_id = ?",
                (giveaway_id,),
            )
            row = await cursor.fetchone()
            return int(row["total"] if row else 0)

    async def list_participants(self, giveaway_id: int) -> list[dict[str, Any]]:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM participants WHERE giveaway_id = ? ORDER BY joined_at ASC",
                (giveaway_id,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def expired_giveaways(self, now_iso: str) -> list[dict[str, Any]]:
        async with await self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM giveaways WHERE status = 'published' AND deadline <= ?",
                (now_iso,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def close_and_draw(self, giveaway_id: int, closed_at: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Atomically close a published giveaway and choose winners without replacement."""
        async with await self._connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute("SELECT * FROM giveaways WHERE id = ?", (giveaway_id,))
            giveaway_row = await cursor.fetchone()
            if not giveaway_row or giveaway_row["status"] != "published":
                await conn.rollback()
                return None

            participants_cursor = await conn.execute(
                "SELECT * FROM participants WHERE giveaway_id = ?",
                (giveaway_id,),
            )
            participants_rows = await participants_cursor.fetchall()
            participants = [dict(row) for row in participants_rows]
            winner_count = min(int(giveaway_row["winners_count"]), len(participants))
            winners = random.SystemRandom().sample(participants, winner_count) if winner_count else []

            await conn.execute(
                "UPDATE giveaways SET status = 'closed', closed_at = ? WHERE id = ?",
                (closed_at, giveaway_id),
            )
            await conn.commit()
            return dict(giveaway_row), winners
