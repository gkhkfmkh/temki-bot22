from __future__ import annotations

import random
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


class Database:
    """Асинхронный слой SQLite для Telegram Giveaway Bot."""

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

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Открывает одно соединение и гарантированно закрывает его.

        Важно: соединение создаётся через `await aiosqlite.connect(...)`,
        после чего НЕ оборачивается повторно в `async with conn`. Иначе
        aiosqlite может попытаться запустить внутренний поток второй раз.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self.path))
        try:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            await conn.close()

    async def init(self) -> None:
        async with self._connect() as conn:
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
                    FOREIGN KEY(giveaway_id)
                        REFERENCES giveaways(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_giveaways_status_deadline
                    ON giveaways(status, deadline);
                CREATE INDEX IF NOT EXISTS idx_participants_giveaway
                    ON participants(giveaway_id);
