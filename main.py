import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand

from config import BOT_TOKEN, MEDIA_TEMP_DIR
from database.store import backend_name, close_db, connect_db
from handlers import admin, user
from utils.clones import start_existing_clones, stop_all
from utils.payments import ensure_default_methods
from utils.scheduler import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


def _apply_restore() -> None:
    """Replace the sqlite database with a staged restore file, if present."""
    from config import DATABASE_URL

    if not DATABASE_URL.startswith("sqlite"):
        return
    path = DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")
    staged = f"{path}.restore"
    if not os.path.exists(staged):
        return
    try:
        if os.path.exists(path):
            os.replace(path, f"{path}.replaced")
        os.replace(staged, path)
        logger.info("staged database restore applied: %s", path)
    except OSError:
        logger.exception("database restore failed")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set — fill in the .env file")

    MEDIA_TEMP_DIR.mkdir(parents=True, exist_ok=True)

    _apply_restore()

    try:
        await connect_db()
    except Exception as error:
        raise SystemExit(
            f"Cannot open database ({backend_name()}): {error}\n"
            "Set DATABASE_URL in F:\\Yashu Bot\\.env — default local file:\n"
            "  DATABASE_URL=sqlite:///course_bot.db\n"
            "Or MongoDB Atlas at hosting time:\n"
            "  DATABASE_URL=mongodb+srv://user:pass@cluster0.mongodb.net/course_bot"
        ) from error

    await ensure_default_methods()
    await start_existing_clones()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(admin.router)
    dp.include_router(user.router)

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Main menu"),
                BotCommand(command="help", description="Help"),
                BotCommand(command="catalog", description="Stuffs catalog"),
                BotCommand(command="my", description="My stuffs"),
                BotCommand(command="admin", description="Admin panel"),
            ]
        )
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramUnauthorizedError:
        await bot.session.close()
        await close_db()
        raise SystemExit(
            "BOT_TOKEN is invalid (Telegram said Unauthorized).\n"
            "Fix F:\\Yashu Bot\\.env — get a fresh token from @BotFather "
            "(/mybots -> API Token),\n"
            "save the file as UTF-8 and run again."
        )
    except Exception:
        await bot.session.close()
        await close_db()
        raise

    logger.info("Bot started (database: %s)", backend_name())
    start_scheduler(bot)
    try:
        await dp.start_polling(bot)
    finally:
        await stop_scheduler()
        await stop_all()
        await bot.session.close()
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
