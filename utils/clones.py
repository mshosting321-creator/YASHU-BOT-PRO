import asyncio
import importlib.util
import json
import logging
import pathlib

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from database.scoping import as_tag, reset_bot_tag, set_bot_tag
from database.store import (
    copy_main_to,
    get_setting_global,
    purge_tag,
    set_setting_global,
)

logger = logging.getLogger("clones")

_BASE = pathlib.Path(__file__).resolve().parent.parent
_instances: dict[str, dict] = {}
_starting: set[str] = set()


def _main_bot_id() -> int:
    try:
        return int(BOT_TOKEN.split(":", 1)[0])
    except ValueError:
        return 0


async def validate_token(token: str) -> dict | None:
    try:
        bot = Bot(
            token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )
        try:
            me = await bot.get_me()
            return {"id": me.id, "username": me.username}
        finally:
            await bot.session.close()
    except Exception:
        return None


async def load_registry() -> list[dict]:
    raw = await get_setting_global("clones", "[]")
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        data = []
    return [e for e in data if isinstance(e, dict)]


async def save_registry(entries: list[dict]) -> None:
    await set_setting_global("clones", json.dumps(entries, ensure_ascii=False))


def _load_fresh_routers(tag: str) -> list:
    routers = []
    for module_path in ("handlers/admin.py", "handlers/user.py"):
        unique = f"clone_{tag}_{module_path.replace('/', '_').replace('.py', '')}"
        path = _BASE / module_path
        spec = importlib.util.spec_from_file_location(unique, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        routers.append(module.router)
    return routers


async def _run(entry: dict) -> None:
    tag = str(entry.get("tag"))
    token = str(entry.get("token", ""))
    if not token:
        logger.error("Clone %s has no token, stopping", tag)
        return
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    for router in _load_fresh_routers(tag):
        dp.include_router(router)
    token_ctx = set_bot_tag(tag)
    logger.info("Clone bot %s (@%s) starting", tag, entry.get("username", "?"))
    try:
        await dp.start_polling(bot, handle_signals=False)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Clone bot %s crashed", tag)
    finally:
        reset_bot_tag(token_ctx)
        logger.info("Clone bot %s stopped", tag)


def is_running(tag: str) -> bool:
    task = _instances.get(tag, {}).get("task")
    return task is not None and not task.done()


async def start_clone(entry: dict) -> None:
    tag = str(entry.get("tag"))
    if tag in _starting or is_running(tag):
        return
    _starting.add(tag)
    try:
        task = asyncio.create_task(_run(entry), name=f"clone-{tag}")
        _instances[tag] = {"task": task, "entry": entry}
    finally:
        _starting.discard(tag)


async def stop_clone(tag: str) -> None:
    data = _instances.pop(tag, None)
    if not data:
        return
    task = data.get("task")
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def add_clone(token: str, mode: str, username: str | None) -> dict:
    token = token.strip()
    if mode not in ("copy", "new"):
        raise ValueError("mode must be copy or new")
    me = await validate_token(token)
    if me is None:
        raise ValueError("Invalid token or bot unreachable")
    if me["id"] == _main_bot_id():
        raise ValueError("This is the main bot token")
    entries = await load_registry()
    if any(int(e.get("bot_id") or 0) == me["id"] for e in entries):
        raise ValueError("This bot is already added")
    seq_raw = await get_setting_global("clone_seq", "0")
    try:
        seq = int(seq_raw or "0") + 1
    except ValueError:
        seq = 1
    await set_setting_global("clone_seq", str(seq))
    tag = f"c{seq}"
    if mode == "copy":
        with as_tag(tag):
            await copy_main_to(tag)
    entry = {
        "tag": tag,
        "token": token,
        "username": username or me.get("username") or "",
        "bot_id": me["id"],
        "mode": mode,
        "active": True,
    }
    entries.append(entry)
    await save_registry(entries)
    return entry


async def remove_clone(tag: str) -> None:
    await stop_clone(tag)
    entries = [e for e in await load_registry() if e.get("tag") != tag]
    await save_registry(entries)
    await purge_tag(tag)


async def set_clone_active(tag: str, active: bool) -> bool:
    entries = await load_registry()
    found = False
    for entry in entries:
        if entry.get("tag") == tag:
            entry["active"] = bool(active)
            found = True
    if found:
        await save_registry(entries)
        if active:
            for entry in entries:
                if entry.get("tag") == tag:
                    await start_clone(entry)
                    break
        else:
            await stop_clone(tag)
    return found


async def start_existing_clones() -> None:
    try:
        entries = await load_registry()
    except Exception:
        logger.exception("Could not load clone registry")
        return
    for entry in entries:
        if entry.get("active", True) and entry.get("token"):
            await start_clone(entry)


async def stop_all() -> None:
    for tag in list(_instances):
        await stop_clone(tag)
