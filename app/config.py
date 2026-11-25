from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: set[int]
    db_path: Path
    timezone: str
    default_target_chat_id: str | None
    log_level: str


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен от @BotFather."
        )

    raw_admin_ids = os.getenv("ADMIN_IDS", "").strip()
    admin_ids: set[int] = set()
    for item in raw_admin_ids.split(","):
        item = item.strip()
        if item:
            try:
                admin_ids.add(int(item))
            except ValueError as exc:
                raise RuntimeError(f"Некорректный ID администратора: {item}") from exc

    if not admin_ids:
        raise RuntimeError("Укажите хотя бы один Telegram user ID в ADMIN_IDS.")

    db_path = Path(os.getenv("DB_PATH", "data/giveaways.db")).expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    target = os.getenv("DEFAULT_TARGET_CHAT_ID", "").strip() or None

    return Settings(
        bot_token=token,
        admin_ids=admin_ids,
        db_path=db_path,
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
        default_target_chat_id=target,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
