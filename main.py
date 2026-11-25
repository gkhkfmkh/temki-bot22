from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.bot import deadline_worker, router
from app.config import load_settings
from app.database import Database


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    db = Database(settings.db_path)
    await db.init()
    if settings.default_target_chat_id and not await db.get_setting("target_chat_id"):
        await db.set_setting("target_chat_id", settings.default_target_chat_id)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.workflow_data.update(db=db, settings=settings)

    worker = asyncio.create_task(deadline_worker(bot, db, settings), name="giveaway-deadline-worker")
    try:
        me = await bot.get_me()
        await bot.set_my_commands(
            commands=[
                BotCommand(command="start", description="Открыть бота"),
                BotCommand(command="admin", description="Панель администратора"),
                BotCommand(command="id", description="Показать ID чата"),
                BotCommand(command="cancel", description="Отменить действие"),
            ]
        )
        logging.getLogger(__name__).info("Bot started: @%s (id=%s)", me.username, me.id)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
