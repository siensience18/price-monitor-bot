import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import load_config
from .db import Database
from .handlers import router
from .monitor import PriceMonitor


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config()

    db = Database(config.db_path)
    await db.connect()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    monitor = PriceMonitor(bot, db, config.check_interval_minutes)

    dp = Dispatcher(db=db, config=config, monitor=monitor)
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="list", description="Мои товары"),
        BotCommand(command="check", description="Проверить цены сейчас"),
        BotCommand(command="help", description="Как пользоваться"),
    ])

    monitor_task = asyncio.create_task(monitor.run())
    try:
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
