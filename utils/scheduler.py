"""Background loop: scheduled broadcasts, daily backup and daily report."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from config import AUTH_ADMIN_IDS, DATABASE_URL
from utils import features
from utils.payments import get_log_chat_id, notify_log_group

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_fired: set[str] = set()
TICK_SECONDS = 30


def start_scheduler(bot) -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(bot))


async def stop_scheduler() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _task = None


async def _loop(bot) -> None:
    while True:
        try:
            await _tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


async def _tick(bot) -> None:
    now = datetime.now(timezone.utc)
    local = now.astimezone()
    if await features.is_on("scheduler"):
        await _run_due_bcasts(bot)
    if await features.is_on("autobackup"):
        await _maybe_fire("backup", await features.get_backup_time(), local, _backup, bot)
    if await features.is_on("dailyreport"):
        await _maybe_fire("report", await features.get_report_time(), local, _report, bot)


async def _maybe_fire(action: str, hhmm: str, local: datetime, fn, bot) -> None:
    if not features.valid_hhmm(hhmm):
        return
    if local.strftime("%H:%M").strip() != hhmm.strip():
        return
    key = f"{action}:{local.strftime('%Y-%m-%d')}"
    if key in _fired:
        return
    _fired.add(key)
    if len(_fired) > 200:
        _fired.clear()
        _fired.add(key)
    try:
        await fn(bot, local)
    except Exception:
        logger.exception("scheduled %s failed", action)


async def _run_due_bcasts(bot) -> None:
    for entry in await features.upcoming_bcasts():
        chat_id = entry.get("chat_id")
        text = str(entry.get("text") or "")
        if chat_id is None or not text:
            await features.drop_bcast(str(entry.get("id")))
            continue
        try:
            await bot.send_message(int(chat_id), text[:4096])
        except Exception:
            logger.exception("scheduled broadcast failed")
        await features.drop_bcast(str(entry.get("id")))


async def _backup(bot, moment: datetime) -> None:
    stamp = moment.strftime("%Y%m%d_%H%M")
    if DATABASE_URL.startswith("mongodb"):
        data = await _mongo_dump()
        if data is None:
            return
        await _send_document(
            bot, f"backup_{stamp}.json", data, f"Auto backup {moment:%d %b %Y %H:%M}"
        )
        return
    path = DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        logger.exception("backup read failed")
        return
    await _send_document(bot, f"backup_{stamp}.db", data, f"Auto backup {moment:%d %b %Y %H:%M}")


async def _send_document(bot, filename: str, data: bytes, caption: str) -> None:
    from aiogram.types import BufferedInputFile

    document = BufferedInputFile(data, filename=filename)
    chat_id = await get_log_chat_id()
    targets = [chat_id] if chat_id is not None else list(AUTH_ADMIN_IDS)
    for target in targets:
        try:
            await bot.send_document(target, document, caption=caption[:1024])
            return
        except Exception:
            logger.exception("backup document send failed")


async def _mongo_dump() -> bytes | None:
    try:
        from database import mongo

        payload = {}
        for name, getter in (
            ("users", mongo.users),
            ("stuffs", mongo.stuffs),
            ("settings", mongo.settings),
            ("payment_methods", mongo.payment_methods),
            ("payment_requests", mongo.payment_requests),
            ("admins", mongo.admins),
        ):
            docs = await getter().find({}).to_list(length=100000)
            payload[name] = [_stringify(d) for d in docs]
        return json.dumps(payload, ensure_ascii=False, default=str).encode()
    except Exception:
        logger.exception("mongo dump failed")
        return None


def _stringify(doc: dict) -> dict:
    return {("_id" if str(k) == "_id" else k): v for k, v in doc.items()}


async def _report(bot, moment: datetime) -> None:
    from database.store import count_users, list_requests, list_stuffs

    day_start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    revenue = 0.0
    orders = 0
    for request in await list_requests():
        if str(request.get("status")) != "approved":
            continue
        created = request.get("created_at")
        if isinstance(created, str):
            try:
                created = datetime.fromisoformat(created)
            except ValueError:
                created = None
        if isinstance(created, datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created.astimezone() >= day_start:
                revenue += float(request.get("amount", 0) or 0)
                orders += 1
    users = await count_users()
    stuffs = await list_stuffs()
    text = (
        f"<b>Daily report — {moment:%d %b %Y}</b>\n\n"
        f"Orders today: {orders}\n"
        f"Revenue today: Rs.{revenue:g}\n"
        f"Total users: {users}\n"
        f"Stuffs: {len(stuffs)}"
    )
    await notify_log_group(bot, text=text)
