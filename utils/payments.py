import logging
from datetime import datetime, timezone

from aiogram.exceptions import TelegramAPIError

from config import AUTH_ADMIN_IDS
from database.store import (
    count_methods,
    delete_method,
    get_method,
    get_request,
    get_setting,
    insert_method,
    insert_request,
    list_methods,
    update_method,
    update_request,
)
from utils.security import is_protect_content

logger = logging.getLogger(__name__)

LOG_GROUP_KEY = "log_group_id"

LOG_TITLE = "𝘓𝘪𝘷𝘦 𝘓𝘰𝘨𝘴"
LOG_LINE = "──────────────────────"


async def ensure_default_methods() -> None:
    if await count_methods() == 0:
        await insert_method(
            {
                "name": "Telegram Stars",
                "kind": "stars",
                "instructions": "Pay directly inside Telegram with Stars.",
                "detail": None,
                "qr_file_id": None,
                "is_active": True,
                "sort_order": 0,
            }
        )


async def get_active_methods() -> list[dict]:
    return await list_methods(active_only=True)


async def get_all_methods() -> list[dict]:
    return await list_methods()


async def method_exists(method_id) -> bool:
    return await get_method(method_id) is not None


async def method_kind_label(method: dict) -> str:
    return {
        "stars": "Telegram Stars",
        "crypto": "Crypto",
        "qr": "QR code",
    }.get(method.get("kind", ""), method.get("kind", ""))


async def set_method_active(method_id, active: bool) -> None:
    await update_method(method_id, {"is_active": active})


async def remove_method(method_id) -> None:
    await delete_method(method_id)


async def create_payment_request(
    *,
    user_tg_id: int,
    user_username: str | None,
    user_full_name: str | None,
    stuffs: list[dict],
    method: dict,
    amount_override: float | None = None,
    coupon: str | None = None,
) -> dict:
    stuff_ids = (
        ",".join(str(s["_id"]) for s in stuffs) if stuffs else None
    )
    stuff_names = (
        " + ".join(str(s.get("name") or "?") for s in stuffs) if stuffs else None
    )
    amount = sum(float(s.get("price", 0) or 0) for s in stuffs)
    if amount_override is not None:
        amount = float(amount_override)
    doc = {
        "user_tg_id": user_tg_id,
        "user_username": user_username,
        "user_full_name": user_full_name,
        "stuff_id": stuff_ids,
        "stuff_name": stuff_names,
        "method_id": str(method["_id"]) if method.get("_id") else None,
        "method_name": method.get("name"),
        "amount": amount,
        "coupon": coupon,
        "status": "pending",
        "screenshot_file_id": None,
        "log_chat_id": None,
        "log_message_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    return await insert_request(doc)


def split_stuff_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part for part in str(raw).split(",") if part]


async def attach_screenshot(request_id, file_id: str) -> None:
    await update_request(request_id, {"screenshot_file_id": file_id})


async def get_payment_request(request_id) -> dict | None:
    return await get_request(request_id)


async def save_request_log_ids(request_id, chat_id: int, message_id: int) -> None:
    await update_request(
        request_id, {"log_chat_id": chat_id, "log_message_id": message_id}
    )


async def set_request_status(request_id, status: str) -> dict | None:
    await update_request(request_id, {"status": status})
    doc = await get_request(request_id)
    if doc and str(status) == "approved":
        try:
            from utils import features

            await features.credit_referral(doc)
        except Exception:
            logger.exception("Referral credit failed")
    return doc


async def get_log_chat_id() -> int | None:
    raw = (await get_setting(LOG_GROUP_KEY, "")).strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


async def notify_log_group(
    bot,
    *,
    text: str | None = None,
    photo_file_id: str | None = None,
    caption: str | None = None,
    reply_markup=None,
    fallback_admins: bool = True,
):
    """Send a message to the configured log group; fall back to admin DMs."""
    chat_id = await get_log_chat_id()
    targets: list[int] = [chat_id] if chat_id is not None else []
    if not targets and fallback_admins:
        targets = sorted(AUTH_ADMIN_IDS)

    for target in targets:
        try:
            if photo_file_id:
                return await bot.send_photo(
                    target,
                    photo_file_id,
                    caption=caption,
                    reply_markup=reply_markup,
                )
            return await bot.send_message(
                target,
                text or "",
                reply_markup=reply_markup,
            )
        except TelegramAPIError:
            logger.exception("Failed to notify log target %s", target)
            continue
    return None


def format_amount(amount: float, method_name: str | None = None) -> str:
    value = int(amount) if amount == int(amount) else amount
    if (method_name or "").lower().startswith("telegram"):
        return f"{value} ⭐"
    return f"Rs.{value}"


def build_transaction_text(request: dict, status: str) -> str:
    created = request.get("created_at") or datetime.now(timezone.utc)
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            created = datetime.now(timezone.utc)
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        moment = created.astimezone().replace(tzinfo=None)
    else:
        moment = datetime.now()
    amount = format_amount(request.get("amount", 0), request.get("method_name"))
    return (
        f"[{moment.strftime('%d-%m-%Y %H:%M')}] {LOG_TITLE}: New Transaction\n"
        f"{LOG_LINE}\n"
        f"• Amount: {amount}\n"
        f"• Payment Method: {request.get('method_name') or '-'}\n"
        f"• Status: {status}\n"
        f"{LOG_LINE}\n"
        f"Order ID: {request.get('request_no', request.get('_id'))}"
    )


async def deliver_stuff(bot, chat_id: int, stuff: dict) -> tuple[int, int]:
    """Send all media of a stuff to a chat instantly (by file_id)."""
    protect = await is_protect_content()
    sent = 0
    failed = 0
    lessons = stuff.get("lessons", [])
    for index, lesson in enumerate(lessons, start=1):
        caption = f"<b>{lesson.get('title') or f'Stuff {index}'}</b>"
        description = lesson.get("description") or ""
        if description:
            caption += f"\n\n{description[:800]}"
        caption += f"\n\n📌 {index}/{len(lessons)}"
        caption = caption[:1024]
        try:
            if lesson.get("media_type") == "video":
                await bot.send_video(
                    chat_id,
                    lesson["file_id"],
                    caption=caption,
                    supports_streaming=True,
                    protect_content=protect,
                )
            else:
                await bot.send_photo(
                    chat_id,
                    lesson["file_id"],
                    caption=caption,
                    protect_content=protect,
                )
            sent += 1
        except TelegramAPIError:
            logger.exception(
                "Failed to deliver item %s of stuff %s", index, stuff.get("name")
            )
            failed += 1
    return sent, failed
