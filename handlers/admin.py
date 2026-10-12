import asyncio
import csv
import html
import io
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import F, Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from config import ADMIN_IDS, AUTH_ADMIN_IDS, DATABASE_URL, MAX_ZIP_SIZE, MEDIA_TEMP_DIR
from database.scoping import as_tag
from database.store import (
    DuplicateKeyError,
    add_balance,
    append_lessons,
    count_users,
    count_users_created_after,
    count_users_seen,
    create_stuff,
    delete_method,
    delete_stuff,
    find_user,
    get_setting,
    get_stuff,
    get_stuff_by_name,
    insert_method,
    list_stuffs,
    list_requests,
    list_user_ids,
    list_user_ids_seen,
    rename_stuff,
    search_users,
    set_admin_role,
    set_banned,
    set_lesson_demo,
    set_proofs,
    set_setting,
    set_showcase,
    set_stuff_category,
    set_stuff_flags,
    set_stuff_price,
    update_method,
)
from keyboards.inline import (
    add_stuff_choice_kb,
    admin_stuffs_kb,
    admin_stuff_view_kb,
    admin_lessons_list_kb,
    admins_kb,
    after_payment_kb,
    cancel_inline_kb,
    clmode_kb,
    clones_kb,
    confirm_delete_stuff_kb,
    coupons_kb,
    desc_step_kb,
    demo_step_kb,
    faq_kb,
    features_kb,
    gifts_kb,
    ib,
    kind_crypto_kb,
    log_group_kb,
    note_skip_kb,
    panel_inline_kb,
    payment_method_view_kb,
    payment_methods_admin_kb,
    proofs_kb,
    protect_kb,
    reviews_kb,
    role_kb,
    schedule_kb,
    showcase_kb,
    start_msg_kb,
    support_kb,
)
from states.admin_states import AdminStates
from utils import clones as clone_manager
from utils import features
from utils.export import build_stuff_zip, cleanup_export, send_stuff_direct
from utils.monitoring import stats_text, vps_text, _graph_text
from utils.payments import (
    build_transaction_text,
    deliver_stuff,
    ensure_default_methods,
    get_all_methods,
    get_method,
    get_payment_request,
    method_kind_label,
    notify_log_group,
    set_request_status,
)
from utils.security import (
    add_admin,
    get_help_text,
    get_start_message,
    get_admin_list,
    is_admin,
    is_protect_content,
    remove_admin,
    set_help_text,
    set_protect_content,
    set_start_media,
    set_start_text,
    clear_start_media,
)
from utils.zip_processor import ZipCourseError, cleanup, process_course_zip

router = Router()

logger = logging.getLogger(__name__)

ZIP_INSTRUCTIONS = (
    "Upload a stuff (ZIP archive)\n\n"
    "<b>Archive structure:</b>\n"
    "<code>info.txt\n"
    "video_1.mp4\n"
    "video_1.txt\n"
    "video_2.jpg\n"
    "video_2.txt</code>\n\n"
    "<b>info.txt</b> (one “key: value” per line):\n"
    "<code>name: Stuff name\n"
    "description: Description\n"
    "price: 199</code>\n"
    "Optional: <code>demo: true</code> — every media file becomes a demo.\n"
    "Optional: photo file starting with <code>cover</code> — showcase image.\n"
    "Optional: photo files starting with <code>proof</code> — proof screenshots.\n\n"
    "Video: mp4, mov, mkv, webm, avi, m4v\n"
    "Image: jpg, jpeg, png, webp, gif\n"
    "Max size: 50 MB.\n\n"
    "Send the ZIP archive as one file."
)

ADD_STUFF_INTRO = (
    "<b>Add stuff</b>\n\n"
    "Send the <b>name</b> of the new stuff:"
)

ADD_LESSONS_INTRO = (
    "<b>Add videos</b>\n\n"
    "Forward or send <b>videos and photos</b> one by one "
    "(as many as you want).\n"
    "Caption of the first message becomes the title.\n"
    "When finished press <b>Done</b>.\n\n"
    "Press Cancel to abort."
)


def panel_text() -> str:
    return "<b>Admin panel</b>\n\nManage stuffs, payments, admins and the bot."


async def _edit_or_send(target: Message, text: str, reply_markup=None) -> None:
    if isinstance(reply_markup, ReplyKeyboardMarkup):
        if target.photo or target.video or target.document:
            try:
                await target.delete()
            except TelegramAPIError:
                pass
        await target.answer(text, reply_markup=reply_markup)
        return
    if target.photo or target.video or target.document:
        await target.delete()
        await target.answer(text, reply_markup=reply_markup)
    else:
        try:
            await target.edit_text(text, reply_markup=reply_markup)
        except TelegramAPIError:
            await target.answer(text, reply_markup=reply_markup)


def _validity_label(days: int) -> str:
    if days <= 0:
        return "forever"
    return f"{days} day(s)"


async def _stuff_kb_for(doc: dict | None) -> InlineKeyboardMarkup:
    doc = doc or {}
    return admin_stuff_view_kb(
        str(doc.get("_id", "")),
        len(doc.get("lessons") or []),
        show_ctrl=await features.is_on("stuffctrl"),
        archived=bool(doc.get("archived")),
        featured=bool(doc.get("featured")),
    )


async def _stuff_view_text(stuff: dict) -> str:
    price = stuff.get("price", 0)
    price_text = f"{int(price)} ₹" if price > 0 else "free"
    lessons = stuff.get("lessons", [])
    demo_count = sum(1 for lesson in lessons if lesson.get("is_demo"))
    badges = []
    if stuff.get("archived"):
        badges.append("ARCHIVED")
    if stuff.get("featured"):
        badges.append("PINNED")
    badge_line = ("[" + ", ".join(badges) + "]\n") if badges else ""
    text = (
        f"<b>{html.escape(stuff.get('name', '?'))}</b>\n"
        f"{badge_line}"
        f"\nPrice: {price_text}\n"
        f"Category: {html.escape(stuff.get('category') or '—')}\n"
        f"Validity: {_validity_label(await features.get_stuff_days(stuff['_id']))}\n"
        f"Stuffs: {len(lessons)} (demo: {demo_count})\n"
        f"Showcase: {'yes' if stuff.get('showcase') else 'no'}\n"
        f"Proofs: {len(stuff.get('proofs') or [])}"
    )
    description = stuff.get("description")
    if description:
        text += f"\nDescription: {html.escape(description)}"
    return text


def _extract_media(message: Message) -> tuple[str, str, str] | None:
    """Return (media_type, file_id, title) or None."""
    title = ""
    if message.caption:
        title = message.caption.strip().splitlines()[0][:80]

    if message.video:
        return ("video", message.video.file_id, title)
    if message.animation and message.animation.mime_type.startswith("video/"):
        return ("video", message.animation.file_id, title)
    if message.photo:
        return ("photo", message.photo[-1].file_id, title)
    if message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("video/"):
            return ("video", message.document.file_id, title)
        if mime.startswith("image/"):
            return ("photo", message.document.file_id, title)
    return None


def _collecting_kb(kind: str) -> InlineKeyboardMarkup:
    done_cb = "sdone" if kind == "stuff" else "saddldone"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Done", done_cb, "success")],
            [ib("Cancel", "scancel", "danger")],
        ]
    )


def _start_panel_text(start: dict) -> str:
    media = start.get("media_type") or "none"
    text = start.get("text") or ""
    preview = text if len(text) <= 300 else text[:300] + "…"
    return (
        "🌟 <b>Start message</b>\n\n"
        f"Media: <b>{media}</b>\n\n"
        f"Current text:\n{preview}\n\n"
        "Change it with the buttons below."
    )


# ---------------------------------------------------------------- panel


PROTECT_TEXT = (
    "<b>Content protection</b>\n\n"
    "When ON, stuffs are sent with forwarding and cloud-save disabled."
)

ADMINS_TEXT = (
    "👤 <b>Admins</b>\n\n"
    "ENV admins cannot be removed from the bot.\n"
    "To add: forward a message from the user or send their numeric id."
)

PAY_METHODS_TEXT = (
    "💳 <b>Payment methods</b>\n\n"
    "Active methods are shown to users at checkout."
)

ZIP_WAIT_TEXT = "Waiting for a ZIP archive. Press “Cancel” to exit."


def _log_group_prompt() -> str:
    return (
        "📁 <b>Log group</b>\n\n"
        "Payment screenshots, transactions and delivery notices go to this chat.\n\n"
        "Forward any message from your log group/channel to this bot "
        "(the bot must be a member/admin), or send the chat id "
        "(e.g. <code>-1001234567890</code>)."
    )


async def _role_of(tg_id: int) -> str:
    if tg_id in AUTH_ADMIN_IDS:
        return "full"
    for admin in await get_admin_list():
        try:
            if int(admin.get("tg_id") or 0) == tg_id:
                return str(admin.get("role") or "full")
        except (TypeError, ValueError):
            continue
    return "full"


def _role_ok(role: str, allowed: str) -> bool:
    if allowed == "all" or role == "full":
        return True
    return role in allowed.split(",")


_BRANCH_ROLES = {
    "add": "full,content",
    "stuffs": "full,content",
    "startmsg": "full,content",
    "helptext": "full,content",
    "reviews": "full,content",
    "media": "full,content",
    "support": "full,support",
    "audit": "full,support",
    "users": "full,support",
    "dash": "all",
    "search": "all",
}


def _off_notice(label: str) -> str:
    return (
        "⚠️ <b>Feature turned off</b>\n\n"
        f"{label} is disabled. Enable it on the Features page."
    )


def _back_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[ib("Panel", "aback", "primary")]])


async def _show_dashboard(target: Message) -> None:
    await _edit_or_send(target, await _dashboard_text(), _dashboard_kb())


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    role = await _role_of(message.from_user.id)
    await message.answer(panel_text(), reply_markup=panel_inline_kb(role))


@router.message(F.text == "❌ Cancel", F.from_user.id.in_(AUTH_ADMIN_IDS))
async def fsm_cancel(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    current = await state.get_state()
    known = {s.state for s in AdminStates.__all_states__}
    if current not in known:
        return
    await state.clear()
    await message.answer("Cancelled.", reply_markup=panel_inline_kb())


@router.callback_query(F.data.startswith("apnl:"))
async def cb_panel_action(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 1)[1]
    await state.clear()
    await callback.answer()
    target = callback.message

    role = await _role_of(callback.from_user.id)
    allowed = _BRANCH_ROLES.get(key, "full")
    if not _role_ok(role, allowed):
        await _edit_or_send(
            target,
            "⛔ Your admin role does not include this section.",
            _back_panel_kb(),
        )
        return

    if key == "dash":
        if await features.is_on("dash"):
            await _show_dashboard(target)
        else:
            await _edit_or_send(target, _off_notice("Dashboard"), _back_panel_kb())
    elif key == "pending":
        if await features.is_on("pending"):
            await _show_pending(target)
        else:
            await _edit_or_send(target, _off_notice("Pending inbox"), _back_panel_kb())
    elif key == "analytics":
        if await features.is_on("analytics"):
            await _show_analytics(target)
        else:
            await _edit_or_send(target, _off_notice("Analytics"), _back_panel_kb())
    elif key == "search":
        if await features.is_on("psearch"):
            await state.set_state(AdminStates.search_q)
            await _edit_or_send(
                target,
                "🔍 <b>Panel search</b>\n\n"
                "Send a name, username, user ID, order ID or coupon code:",
                cancel_inline_kb("aback"),
            )
        else:
            await _edit_or_send(target, _off_notice("Global search"), _back_panel_kb())
    elif key == "timers":
        if await features.is_on("offer"):
            await _show_timers(target)
        else:
            await _edit_or_send(target, _off_notice("Offer timers"), _back_panel_kb())
    elif key == "media":
        if await features.is_on("medialib"):
            await _show_media(target)
        else:
            await _edit_or_send(target, _off_notice("Media library"), _back_panel_kb())
    elif key == "backup":
        if await features.is_on("backup"):
            await _show_backup(target)
        else:
            await _edit_or_send(target, _off_notice("Backup"), _back_panel_kb())
    elif key == "zip":
        await state.set_state(AdminStates.waiting_zip)
        await _edit_or_send(target, ZIP_INSTRUCTIONS, cancel_inline_kb("scancel"))
    elif key == "direct":
        await state.set_state(AdminStates.add_stuff_name)
        await _edit_or_send(target, ADD_STUFF_INTRO, cancel_inline_kb("scancel"))
    elif key == "add":
        await _edit_or_send(
            target,
            "➕ <b>Add stuff</b>\n\nChoose how to add:",
            add_stuff_choice_kb(),
        )
    elif key == "stuffs":
        await _show_stuffs_list(target, state)
    elif key == "startmsg":
        start = await get_start_message()
        await _edit_or_send(
            target,
            _start_panel_text(start),
            start_msg_kb(
                bool(start.get("media_id")), start.get("media_type", "")
            ),
        )
    elif key == "admins":
        items = await get_admin_list()
        await _edit_or_send(
            target,
            ADMINS_TEXT,
            admins_kb_view(items, show_roles=await features.is_on("roles")),
        )
    elif key == "methods":
        await ensure_default_methods()
        methods = await get_all_methods()
        await _edit_or_send(target, PAY_METHODS_TEXT, payment_methods_admin_kb(methods))
    elif key == "protect":
        enabled = await is_protect_content()
        await _edit_or_send(target, PROTECT_TEXT, protect_kb(enabled))
    elif key == "loggroup":
        current = (await get_setting("log_group_id", "")).strip()
        await state.set_state(AdminStates.waiting_log_group)
        await _edit_or_send(
            target,
            _log_group_prompt(),
            log_group_kb() if current else cancel_inline_kb("aback"),
        )
    elif key == "users":
        await state.set_state(AdminStates.user_lookup)
        await _edit_or_send(
            target,
            "👥 <b>User manager</b>\n\n"
            "Send the <b>user ID</b> (numeric, e.g. <code>123456789</code>):",
            cancel_inline_kb("aback"),
        )
    elif key == "bcast":
        await state.set_state(AdminStates.broadcast_text)
        await _edit_or_send(
            target,
            "📣 <b>Broadcast</b>\n\n"
            "Send the message: <b>text, photo, video, GIF or sticker</b> "
            "(caption becomes the text; stickers go as-is).\n"
            "Next you pick the audience (everyone, buyers, non-buyers, "
            "or a stuff's buyers) and confirm a preview.",
            cancel_inline_kb("aback"),
        )
    elif key == "stats":
        await _edit_or_send(target, await stats_text(), _refresh_kb("bstats"))
    elif key == "vps":
        await _edit_or_send(target, vps_text(), _refresh_kb("bvps"))
    elif key == "clones":
        await _show_clones(target)
    elif key == "helptext":
        current = await get_help_text()
        await state.set_state(AdminStates.set_help_text)
        await _edit_or_send(
            target,
            "ℹ️ <b>Help text</b>\n\nCurrent:\n"
            + current
            + "\n\nSend the new Help text (HTML supported). "
            "It replaces what users see when they press Help.",
            cancel_inline_kb("aback"),
        )
    elif key == "cladd":
        await state.set_state(AdminStates.clone_pending)
        await _edit_or_send(
            target,
            "🤖 <b>Add clone bot</b>\n\n"
            "Send the bot token of the clone bot.\n"
            "It will use the same admin IDs and settings as this bot.",
            cancel_inline_kb("clcancel"),
        )
    elif key == "features":
        await _show_features(target)
    elif key == "coupons":
        await _show_coupons(target)
    elif key == "gifts":
        await _show_gifts(target)
    elif key == "reviews":
        await _show_reviews(target)
    elif key == "support":
        await _show_support(target)
    elif key == "sched":
        await _show_schedule(target)
    elif key == "faq":
        await _show_faq(target)
    elif key == "audit":
        await _show_audit(target)


# ---------------------------------------------------------------- clone bots


@router.message(AdminStates.set_help_text, F.text)
async def help_text_set(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text:
        return
    await set_help_text(text)
    await state.clear()
    await message.answer(
        "✅ Help text saved. Users will see it when they press Help.",
        reply_markup=panel_inline_kb(),
    )


async def _show_clones(target: Message) -> None:
    entries = await clone_manager.load_registry()
    running = {e["tag"] for e in entries if clone_manager.is_running(e.get("tag", ""))}
    mode_text = (
        "Same clone = every stuff, payment method and setting is copied "
        "from this bot.\n"
        "New database = the clone starts empty.\n\n"
    )
    text = f"🤖 <b>Clone bots</b>\n\n{mode_text}"
    if entries and await features.is_on("clonedash"):
        text += await _clone_network_text(entries, running)
    await _edit_or_send(target, text, clones_kb(entries, running))


@router.message(Command("clone"))
async def cmd_clone(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    await state.clear()
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/clone BOT_TOKEN</code>\n\n"
            "The clone uses your admin IDs. Next you choose whether to copy "
            "all stuffs or start with a new database."
        )
        return
    token = parts[1].strip()
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete the /clone message")
    me = await clone_manager.validate_token(token)
    if me is None:
        await message.answer("Invalid token or bot unreachable.")
        return
    if me["id"] == clone_manager._main_bot_id():
        await message.answer("That is the main bot token.")
        return
    await state.update_data(clone_token=token, clone_username=me.get("username") or "")
    await state.set_state(AdminStates.clone_pending)
    await message.answer(
        f"Token accepted: <b>@{html.escape(str(me.get('username') or ''))}</b>\n\n"
        "Choose the clone mode:",
        reply_markup=clmode_kb(),
    )


@router.message(AdminStates.clone_pending, F.text)
async def clone_token_text(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    token = (message.text or "").strip()
    if not token:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        logger.info("Could not delete the token message")
    me = await clone_manager.validate_token(token)
    if me is None:
        await message.answer(
            "Invalid token or bot unreachable. Send a valid bot token or "
            "press Cancel.",
            reply_markup=cancel_inline_kb("clcancel"),
        )
        return
    if me["id"] == clone_manager._main_bot_id():
        await message.answer(
            "That is the main bot token. Send a different token.",
            reply_markup=cancel_inline_kb("clcancel"),
        )
        return
    await state.update_data(clone_token=token, clone_username=me.get("username") or "")
    await message.answer(
        f"Token accepted: <b>@{html.escape(str(me.get('username') or ''))}</b>\n\n"
        "Choose the clone mode:",
        reply_markup=clmode_kb(),
    )


@router.callback_query(F.data.startswith("clmode:"))
async def cb_clone_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    mode = callback.data.split(":", 1)[1]
    data = await state.get_data()
    token = str(data.get("clone_token") or "")
    username = str(data.get("clone_username") or "")
    if not token:
        await callback.answer("Session expired, start again.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Creating clone...")
    try:
        entry = await clone_manager.add_clone(token, mode, username)
    except ValueError as error:
        await callback.message.answer(f"Could not create clone: {error}")
        return
    except Exception:
        logger.exception("add_clone failed")
        await callback.message.answer("Could not create clone (see logs).")
        return
    await clone_manager.start_clone(entry)
    await features.audit(
        f"Clone created: @{entry.get('username') or entry['tag']} ({mode})"
    )
    await callback.message.answer(
        f"Clone created: <b>@{html.escape(str(entry.get('username') or entry['tag']))}</b>\n"
        f"Mode: {'same clone (copied)' if mode == 'copy' else 'new database'}\n"
        "It is starting now."
    )
    await _show_clones(callback.message)


@router.callback_query(F.data == "clcancel")
async def cb_clone_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer("Cancelled")
    await _show_clones(callback.message)


@router.callback_query(F.data.startswith("clinfo:"))
async def cb_clone_info(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    tag = callback.data.split(":", 1)[1]
    entries = await clone_manager.load_registry()
    entry = next((e for e in entries if e.get("tag") == tag), None)
    if entry is None:
        await callback.answer("Clone not found", show_alert=True)
        return
    if not await features.is_on("clonedash"):
        status = "running" if clone_manager.is_running(tag) else "stopped"
        mode = "same clone (copied)" if entry.get("mode") == "copy" else "new database"
        await callback.answer(
            f"@{entry.get('username') or tag} - {mode} - {status}",
            show_alert=True,
        )
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        await _clone_stats_text(entry),
        _clone_dash_kb(tag),
    )


@router.callback_query(F.data.startswith("cltog:"))
async def cb_clone_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    tag = callback.data.split(":", 1)[1]
    active = not clone_manager.is_running(tag)
    await clone_manager.set_clone_active(tag, active)
    await callback.answer("Started" if active else "Stopped")
    await _show_clones(callback.message)


@router.callback_query(F.data.startswith("cldel:"))
async def cb_clone_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    tag = callback.data.split(":", 1)[1]
    if await features.is_on("dconfirm"):
        await callback.answer()
        await _edit_or_send(
            callback.message,
            f"⚠️ <b>Remove clone {html.escape(tag)}?</b>\n\n"
            "The clone bot stops and its registry entry is deleted. "
            "Database data stays.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        ib("Remove", f"cldelok:{tag}", "danger"),
                        ib("Back", "apnl:clones", "primary"),
                    ]
                ]
            ),
        )
        return
    await clone_manager.remove_clone(tag)
    await features.audit(f"Clone removed: {tag}")
    await callback.answer("Clone removed")
    await _show_clones(callback.message)


@router.callback_query(F.data == "clnone")
async def cb_clone_none(callback: CallbackQuery) -> None:
    await callback.answer("Use Add clone", show_alert=True)


# ---------------------------------------------------------------- stuff export


@router.callback_query(F.data.startswith("sget:"))
async def cb_stuff_direct_send(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if not doc:
        await callback.answer("Stuff not found", show_alert=True)
        return
    await callback.answer("Sending media...")
    target = callback.message
    if target is None:
        target = await callback.bot.send_message(
            callback.from_user.id, "Media export:"
        )
    else:
        await target.answer(f"Sending media for <b>{html.escape(str(doc.get('name')))}</b>...")
    sent = await send_stuff_direct(target, doc)
    await target.answer(f"Done. Sent {sent} file(s).")


@router.callback_query(F.data.startswith("szipdl:"))
async def cb_stuff_zip_download(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if not doc:
        await callback.answer("Stuff not found", show_alert=True)
        return
    await callback.answer("Preparing ZIP...")
    target = callback.message
    if target is None:
        target = await callback.bot.send_message(
            callback.from_user.id, "ZIP export:"
        )
    wait = await target.answer("Preparing ZIP download, please wait...")
    path = None
    try:
        path = await build_stuff_zip(bot, doc)
    except ValueError as error:
        await wait.edit_text(str(error))
        return
    except Exception:
        logger.exception("ZIP export failed for %s", stuff_id)
        await wait.edit_text("ZIP export failed (see logs).")
        return
    try:
        await target.answer_document(
            FSInputFile(path),
            caption=(
                f"<b>{html.escape(str(doc.get('name')))}</b> - ZIP export\n"
                "info.txt lists all contents."
            ),
        )
        await wait.edit_text("ZIP ready, sent above.")
    except TelegramAPIError:
        logger.exception("ZIP upload failed for %s", stuff_id)
        await wait.edit_text("Could not upload the ZIP (maybe too large).")
    finally:
        cleanup_export(path)


# ---------------------------------------------------------------- ZIP upload


@router.message(AdminStates.waiting_zip, F.text)
async def zip_waiting_text(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer(ZIP_WAIT_TEXT)


@router.message(AdminStates.waiting_zip, F.document)
async def handle_zip(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return

    document = message.document
    if not document.file_name or not document.file_name.lower().endswith(".zip"):
        await message.answer(
            "❌ That is not a ZIP file.", reply_markup=panel_inline_kb()
        )
        return
    if document.file_size and document.file_size > MAX_ZIP_SIZE:
        await message.answer(
            "❌ Archive too big (limit 50 MB).", reply_markup=panel_inline_kb()
        )
        return

    await state.clear()

    temp_id = uuid.uuid4().hex
    zip_path = MEDIA_TEMP_DIR / f"{temp_id}.zip"
    extract_dir = MEDIA_TEMP_DIR / temp_id

    progress = await message.answer("⏳ Downloading archive…")

    try:
        await message.bot.download(document, destination=zip_path)

        try:
            package = await process_course_zip(str(zip_path), str(extract_dir))
        except ZipCourseError as error:
            await progress.edit_text(
                f"❌ {html.escape(str(error))}", reply_markup=panel_inline_kb()
            )
            return
        except Exception:
            logger.exception("ZIP parsing failed")
            await progress.edit_text(
                "❌ Could not parse the archive.", reply_markup=panel_inline_kb()
            )
            return

        if await get_stuff_by_name(package.name):
            await progress.edit_text(
                f"❌ Stuffs “{html.escape(package.name)}” already exists.\n"
                "Change name in info.txt and try again.",
                reply_markup=panel_inline_kb(),
            )
            return

        uploaded: list[dict] = []
        failed = 0
        total = len(package.lessons)

        for index, lesson_file in enumerate(package.lessons, start=1):
            await progress.edit_text(
                f"⏳ Uploading media {index}/{total}: {html.escape(lesson_file.title)}"
            )
            try:
                if lesson_file.media_type == "video":
                    sent = await message.answer_video(
                        video=FSInputFile(lesson_file.path),
                        caption=f"Uploading {index}/{total}",
                        supports_streaming=True,
                    )
                    file_id = sent.video.file_id
                else:
                    sent = await message.answer_photo(
                        photo=FSInputFile(lesson_file.path),
                        caption=f"Uploading {index}/{total}",
                    )
                    file_id = sent.photo[-1].file_id
                await sent.delete()
                uploaded.append(
                    {
                        "title": lesson_file.title,
                        "description": lesson_file.description,
                        "media_type": lesson_file.media_type,
                        "file_id": file_id,
                        "is_demo": lesson_file.is_demo,
                    }
                )
            except TelegramAPIError:
                logger.exception("Failed to upload %s", lesson_file.path)
                failed += 1

        if not uploaded:
            await progress.edit_text(
                "❌ No files could be uploaded to Telegram.",
                reply_markup=panel_inline_kb(),
            )
            return

        showcase_file_id: str | None = None
        if package.showcase is not None:
            await progress.edit_text("⏳ Uploading showcase image…")
            try:
                sent_photo = await message.answer_photo(
                    photo=FSInputFile(package.showcase),
                    caption="Showcase",
                )
                showcase_file_id = sent_photo.photo[-1].file_id
                await sent_photo.delete()
            except TelegramAPIError:
                logger.exception("Failed to upload showcase")

        proof_ids: list[str] = []
        for proof_index, proof_path in enumerate(package.proofs, start=1):
            await progress.edit_text(
                f"⏳ Uploading proof {proof_index}/{len(package.proofs)}…"
            )
            try:
                sent_proof = await message.answer_photo(
                    photo=FSInputFile(proof_path),
                    caption=f"Proof {proof_index}",
                )
                proof_ids.append(sent_proof.photo[-1].file_id)
                await sent_proof.delete()
            except TelegramAPIError:
                logger.exception("Failed to upload proof %s", proof_path)

        try:
            await create_stuff(
                package.name,
                package.description,
                package.price,
                uploaded,
                showcase=showcase_file_id,
                proofs=proof_ids,
            )
        except DuplicateKeyError:
            await progress.edit_text(
                f"❌ Stuffs “{html.escape(package.name)}” already exists.",
                reply_markup=panel_inline_kb(),
            )
            return

        demo_count = sum(1 for lesson in uploaded if lesson["is_demo"])
        summary = (
            "✅ <b>Stuffs uploaded</b>\n\n"
            f"Name: {html.escape(package.name)}\n"
            f"Price: {int(package.price)} ⭐\n"
            f"Stuffs: {len(uploaded)} (demo: {demo_count})\n"
            f"Showcase: {'✅' if showcase_file_id else '❌'}\n"
            f"Proofs: {len(proof_ids)}"
        )
        if failed:
            summary += f"\n⚠️ Files failed: {failed}"
        await progress.edit_text(summary, reply_markup=panel_inline_kb())

    finally:
        cleanup(zip_path, extract_dir)


# ---------------------------------------------------------------- add stuff direct


@router.message(AdminStates.add_stuff_name, F.text)
async def as_name(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    name = message.text.strip()[:100]
    if not name:
        await message.answer("Name cannot be empty. Send the name:")
        return
    if await get_stuff_by_name(name):
        await message.answer(
            f"❌ Stuffs “{html.escape(name)}” already exists. Send another name:"
        )
        return
    await state.update_data(name=name)
    await state.set_state(AdminStates.add_stuff_price)
    await message.answer(
        f"Stuffs: <b>{html.escape(name)}</b>\n\n"
        "Send the <b>price</b> as a number (e.g. <code>199</code>) "
        "or <code>0</code> for free:"
    )


@router.message(AdminStates.add_stuff_price, F.text)
async def as_price(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    text = message.text.strip().replace(",", ".")
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    try:
        price = float(digits) if digits else 0.0
    except ValueError:
        price = 0.0
    await state.update_data(
        price=price, media=[], demo=[], proofs=[], showcase=None, description=""
    )
    await state.set_state(AdminStates.add_stuff_desc)
    await message.answer(
        f"Price: <b>{int(price) if price == int(price) else price}</b>\n\n"
        "Send a <b>description</b> for the stuff, or press <b>Skip</b>.",
        reply_markup=desc_step_kb(),
    )


@router.message(AdminStates.add_stuff_desc, F.text)
async def as_desc(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(description=message.text.strip()[:500])
    await _start_showcase_step(message, state)


async def _start_showcase_step(target: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.add_stuff_showcase)
    await target.answer(
        "<b>Showcase image</b>\n\n"
        "Send one photo that represents this stuff "
        "(users see it first when they open the stuff).\n\n"
        "Press <b>Skip</b> to continue without it.",
        reply_markup=showcase_kb(),
    )


async def _start_main_media(target: Message, state: FSMContext) -> None:
    await state.set_state(AdminStates.add_stuff_media)
    await target.answer(
        "<b>Send the main videos</b>\n\n"
        "Forward or send videos/photos one by one.\n"
        "The caption of each message becomes the title.\n\n"
        "When finished press <b>Done</b>.",
        reply_markup=_collecting_kb("stuff"),
    )


async def _start_demo_step(target: Message) -> None:
    await target.answer(
        "<b>Send demo videos (optional)</b>\n\n"
        "Forward or send free preview videos/photos one by one.\n"
        "They will be marked as demo and shown to every user.\n\n"
        "Press <b>Done</b> when finished, or <b>Skip</b> to add none.",
        reply_markup=demo_step_kb(),
    )


async def _start_proofs_step(target: Message) -> None:
    await target.answer(
        "<b>Send proof screenshots (optional)</b>\n\n"
        "Forward delivery/success screenshots one by one (single or multiple).\n"
        "Press <b>Done</b> when finished, or <b>Skip</b> to add none.",
        reply_markup=proofs_kb(),
    )


@router.message(AdminStates.add_stuff_media)
async def as_media(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    media = _extract_media(message)
    if media is None:
        await message.answer(
            "Send a video or photo (or press Done / Cancel)."
        )
        return
    media_type, file_id, title = media
    data = await state.get_data()
    collected = data.get("media", [])
    index = len(collected)
    collected.append(
        {
            "title": title or f"Stuff {index + 1}",
            "description": "",
            "media_type": media_type,
            "file_id": file_id,
            "is_demo": False,
        }
    )
    await state.update_data(media=collected)
    await message.answer(
        f"✅ Added: <b>{html.escape(collected[-1]['title'])}</b>\n"
        f"Total stuffs: {len(collected)}",
        reply_markup=_collecting_kb("stuff"),
    )


@router.message(AdminStates.add_stuff_demo)
async def as_demo_media(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    media = _extract_media(message)
    if media is None:
        await message.answer(
            "Send a video or photo (or press Done / Skip / Cancel)."
        )
        return
    media_type, file_id, title = media
    data = await state.get_data()
    collected = data.get("demo", [])
    index = len(collected)
    collected.append(
        {
            "title": title or f"Demo {index + 1}",
            "description": "",
            "media_type": media_type,
            "file_id": file_id,
            "is_demo": True,
        }
    )
    await state.update_data(demo=collected)
    await message.answer(
        f"✅ Added demo: <b>{html.escape(collected[-1]['title'])}</b>\n"
        f"Total demos: {len(collected)}",
        reply_markup=demo_step_kb(),
    )


@router.message(AdminStates.add_stuff_showcase, F.photo)
async def as_showcase(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(showcase=message.photo[-1].file_id)
    await message.answer("Showcase image saved.")
    await _start_main_media(message, state)


@router.message(AdminStates.add_stuff_showcase)
async def as_showcase_invalid(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Send a photo, or press Skip / Cancel.")


@router.message(AdminStates.add_stuff_proofs, F.photo)
async def as_proofs(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    proofs = data.get("proofs", [])
    proofs.append(message.photo[-1].file_id)
    await state.update_data(proofs=proofs)
    await message.answer(
        f"✅ Proof added. Total proofs: {len(proofs)}",
        reply_markup=proofs_kb(),
    )


@router.message(AdminStates.add_stuff_proofs)
async def as_proofs_invalid(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Send a photo, or press Done / Skip / Cancel.")


async def _advance_add_stuff(target: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current == AdminStates.add_stuff_showcase.state:
        await _start_main_media(target, state)
    elif current == AdminStates.add_stuff_media.state:
        await state.set_state(AdminStates.add_stuff_demo)
        await _start_demo_step(target)
    elif current == AdminStates.add_stuff_demo.state:
        await state.set_state(AdminStates.add_stuff_proofs)
        await _start_proofs_step(target)
    elif current == AdminStates.add_stuff_proofs.state:
        await _create_stuff_from_state(target, state)
    else:
        await target.answer("Nothing to finish.")
        await state.clear()


@router.callback_query(F.data.startswith("sskip:"))
async def cb_stuff_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    step = callback.data.split(":", 1)[1]
    current = await state.get_state()
    if step == "desc" and current == AdminStates.add_stuff_desc.state:
        await callback.answer("Skipped")
        await _start_showcase_step(callback.message, state)
    elif step == "showcase" and current == AdminStates.add_stuff_showcase.state:
        await callback.answer("Skipped")
        await _start_main_media(callback.message, state)
    elif step == "demo" and current == AdminStates.add_stuff_demo.state:
        await callback.answer("Skipped")
        await state.set_state(AdminStates.add_stuff_proofs)
        await _start_proofs_step(callback.message)
    elif step == "proofs" and current == AdminStates.add_stuff_proofs.state:
        await callback.answer("Skipped")
        await _create_stuff_from_state(callback.message, state)
    else:
        await callback.answer()


@router.callback_query(F.data == "sdone")
async def cb_stuff_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    current = await state.get_state()
    if current in {
        AdminStates.add_stuff_media.state,
        AdminStates.add_stuff_demo.state,
        AdminStates.add_stuff_showcase.state,
        AdminStates.add_stuff_proofs.state,
    }:
        data = await state.get_data()
        if current == AdminStates.add_stuff_media.state and not data.get("media"):
            await callback.answer(
                "Send at least one video/photo first", show_alert=True
            )
            return
        await callback.answer()
        await _advance_add_stuff(callback.message, state)
        return
    data = await state.get_data()
    collected = data.get("media", [])
    if not collected:
        await callback.answer("Send at least one video/photo first", show_alert=True)
        return
    if "name" not in data:
        await state.clear()
        await callback.answer()
        return
    await _create_stuff_from_state(callback.message, state, callback=callback)


async def _create_stuff_from_state(
    target: Message, state: FSMContext, callback: CallbackQuery | None = None
) -> None:
    data = await state.get_data()
    name = data.get("name")
    collected = data.get("media", [])
    if not name or not collected:
        await state.clear()
        if callback:
            await callback.answer()
        else:
            await _edit_or_send(target, "❌ Cancelled.", panel_inline_kb())
        return
    lessons = list(collected) + list(data.get("demo") or [])
    try:
        await create_stuff(
            name,
            data.get("description", ""),
            float(data.get("price", 0.0)),
            lessons,
            showcase=data.get("showcase"),
            proofs=data.get("proofs") or [],
        )
    except DuplicateKeyError:
        await state.clear()
        if callback:
            await callback.answer("Name already exists", show_alert=True)
        else:
            await _edit_or_send(
                target, "❌ Name already exists.", panel_inline_kb()
            )
        return
    await state.clear()
    if callback:
        await callback.answer("Stuffs created")
    await features.audit(f"Stuff created: {name} (Rs.{data.get('price', 0)})")
    demo_count = sum(1 for lesson in lessons if lesson.get("is_demo"))
    await _edit_or_send(
        target,
        f"✅ <b>Stuffs created</b>\n\n"
        f"Name: {html.escape(name)}\n"
        f"Stuffs: {len(collected)} (demo: {demo_count})\n"
        f"Showcase: {'✅' if data.get('showcase') else '❌'}\n"
        f"Proofs: {len(data.get('proofs') or [])}",
        panel_inline_kb(),
    )


@router.callback_query(F.data == "scancel")
async def cb_stuff_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer("Cancelled")
    await _edit_or_send(callback.message, "❌ Cancelled.", panel_inline_kb())


# ---------------------------------------------------------------- stuffs list


@router.callback_query(F.data == "sadmin:stuffs")
async def cb_stuffs_list(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer()
    await _show_stuffs_list(callback.message, state)


@router.callback_query(F.data.startswith("sadm:"))
async def cb_stuff_admin_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        await _stuff_view_text(doc),
        await _stuff_kb_for(doc),
    )


@router.callback_query(F.data.startswith("saddl:"))
async def cb_add_lessons(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.add_lessons_media)
    await state.update_data(stuff_id=stuff_id, media=[])
    await callback.answer()
    await _edit_or_send(callback.message, ADD_LESSONS_INTRO, _collecting_kb("lessons"))


@router.message(AdminStates.add_lessons_media)
async def al_media(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    media = _extract_media(message)
    if media is None:
        await message.answer("Send a video or photo (or press Done / Cancel).")
        return
    media_type, file_id, title = media
    data = await state.get_data()
    collected = data.get("media", [])
    collected.append(
        {
            "title": title or f"Stuff {len(collected) + 1}",
            "description": "",
            "media_type": media_type,
            "file_id": file_id,
            "is_demo": False,
        }
    )
    await state.update_data(media=collected)
    await message.answer(
        f"✅ Added: <b>{html.escape(collected[-1]['title'])}</b>\n"
        f"Added so far: {len(collected)}",
        reply_markup=_collecting_kb("lessons"),
    )


@router.callback_query(F.data == "saddldone")
async def cb_add_lessons_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    collected = data.get("media", [])
    stuff_id = data.get("stuff_id")
    if not stuff_id:
        await state.clear()
        await callback.answer()
        return
    if collected:
        await append_lessons(stuff_id, collected)
    await state.clear()
    doc = await get_stuff(stuff_id)
    await callback.answer("Saved")
    await _edit_or_send(
        callback.message,
        await _stuff_view_text(doc or {"name": "?", "price": 0, "lessons": []}),
        await _stuff_kb_for(doc or {}),
    )


@router.callback_query(F.data.startswith("sadml:"))
async def cb_stuff_lessons(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    text = (
        f"🎁 <b>{html.escape(doc.get('name', '?'))}</b> — media list\n\n"
        "Tap an item to toggle <b>demo (free)</b> / <b>paid</b>."
    )
    await callback.answer()
    await _edit_or_send(callback.message, text, admin_lessons_list_kb(doc))


@router.callback_query(F.data.startswith("sdemo:"))
async def cb_toggle_demo(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        _, stuff_id, index = callback.data.split(":")
        index = int(index)
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return

    doc = await get_stuff(stuff_id)
    if doc is None or not 0 <= index < len(doc.get("lessons", [])):
        await callback.answer("Not found", show_alert=True)
        return

    lesson = doc["lessons"][index]
    new_value = not lesson.get("is_demo")
    doc = await set_lesson_demo(stuff_id, index, new_value) or doc
    await callback.answer("Demo ✅" if new_value else "Paid 🔒")
    await _edit_or_send(
        callback.message,
        f"🎁 <b>{html.escape(doc.get('name', '?'))}</b> — media list\n\n"
        "Tap an item to toggle <b>demo (free)</b> / <b>paid</b>.",
        admin_lessons_list_kb(doc),
    )


@router.callback_query(F.data.startswith("sren:"))
async def cb_rename_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    await state.clear()
    await state.set_state(AdminStates.rename_stuff)
    await state.update_data(stuff_id=stuff_id)
    await callback.answer()
    await _edit_or_send(callback.message, "✏️ Send the new name:", cancel_inline_kb())


@router.message(AdminStates.rename_stuff, F.text)
async def do_rename(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    name = message.text.strip()[:100]
    if not name:
        await message.answer("Name cannot be empty:")
        return
    try:
        await rename_stuff(data["stuff_id"], name)
    except DuplicateKeyError:
        await message.answer("❌ That name is already taken:")
        return
    await features.audit(f"Stuff renamed to {name}")
    await state.clear()
    doc = await get_stuff(data["stuff_id"])
    await message.answer(
        "✅ Renamed.", reply_markup=panel_inline_kb()
    )
    if doc:
        await message.answer(
            await _stuff_view_text(doc),
            reply_markup=await _stuff_kb_for(doc),
        )


@router.callback_query(F.data.startswith("sprice:"))
async def cb_price_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    await state.clear()
    await state.set_state(AdminStates.price_stuff)
    await state.update_data(stuff_id=stuff_id)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "💰 Send the new price as a number (<code>0</code> = free):",
        cancel_inline_kb(),
    )


@router.message(AdminStates.price_stuff, F.text)
async def do_price(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    text = message.text.strip().replace(",", ".")
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    try:
        price = float(digits) if digits else 0.0
    except ValueError:
        price = 0.0
    await set_stuff_price(data["stuff_id"], price)
    await features.audit(f"Stuff price set to {price:g}")
    await state.clear()
    doc = await get_stuff(data["stuff_id"])
    await message.answer("✅ Price saved.", reply_markup=panel_inline_kb())
    if doc:
        await message.answer(
            await _stuff_view_text(doc),
            reply_markup=await _stuff_kb_for(doc),
        )


@router.callback_query(F.data.startswith("sdel:"))
async def cb_delete_ask(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Already deleted", show_alert=True)
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"🗑 Delete <b>{html.escape(doc.get('name', '?'))}</b> with all media?",
        confirm_delete_stuff_kb(stuff_id),
    )


@router.callback_query(F.data.startswith("sdelok:"))
async def cb_delete_ok(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    name = (doc or {}).get("name", "?")
    await delete_stuff(stuff_id)
    items = await list_stuffs()
    await features.audit(f"Stuff deleted: {name}")
    await callback.answer("Deleted")
    await _edit_or_send(
        callback.message,
        f"🗑 Stuffs “{html.escape(name)}” deleted.",
        admin_stuffs_kb(items),
    )


# ---------------------------------------------------------------- start message


@router.callback_query(F.data == "stext")
async def cb_set_start_text(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.set_start_text)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "📝 Send the new <b>start text</b> (HTML is supported):",
        cancel_inline_kb(),
    )


@router.message(AdminStates.set_start_text, F.text)
async def do_start_text(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    await set_start_text(message.text)
    await features.audit("Start message text updated")
    await state.clear()
    start = await get_start_message()
    await message.answer("✅ Start text saved.", reply_markup=panel_inline_kb())
    await message.answer(
        _start_panel_text(start),
        reply_markup=start_msg_kb(bool(start.get("media_id")), start.get("media_type", "")),
    )


@router.callback_query(F.data == "sphoto")
async def cb_set_start_photo(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.set_start_photo)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "🖼 Send the <b>photo</b> for the start message (with caption if you want):",
        cancel_inline_kb(),
    )


@router.message(AdminStates.set_start_photo, F.photo)
async def do_start_photo(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    file_id = message.photo[-1].file_id
    await set_start_media("photo", file_id)
    if message.caption:
        await set_start_text(message.caption[:1024])
    await state.clear()
    start = await get_start_message()
    await message.answer("✅ Start photo saved.", reply_markup=panel_inline_kb())
    await message.answer(
        _start_panel_text(start),
        reply_markup=start_msg_kb(bool(start.get("media_id")), start.get("media_type", "")),
    )


@router.message(AdminStates.set_start_photo, F.text)
async def do_start_photo_waiting(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Please send a photo 🖼.")


@router.callback_query(F.data == "svideo")
async def cb_set_start_video(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.set_start_video)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "🎬 Send the <b>video</b> for the start message (with caption if you want):",
        cancel_inline_kb(),
    )


@router.message(AdminStates.set_start_video, F.video)
async def do_start_video(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    file_id = message.video.file_id
    await set_start_media("video", file_id)
    if message.caption:
        await set_start_text(message.caption[:1024])
    await state.clear()
    start = await get_start_message()
    await message.answer("✅ Start video saved.", reply_markup=panel_inline_kb())
    await message.answer(
        _start_panel_text(start),
        reply_markup=start_msg_kb(bool(start.get("media_id")), start.get("media_type", "")),
    )


@router.message(AdminStates.set_start_video, F.text)
async def do_start_video_waiting(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Please send a video 🎬.")


@router.callback_query(F.data == "sclearmedia")
async def cb_clear_start_media(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await clear_start_media()
    start = await get_start_message()
    await callback.answer("Media cleared")
    await _edit_or_send(
        callback.message,
        _start_panel_text(start),
        start_msg_kb(False, ""),
    )


@router.callback_query(F.data == "spreview")
async def cb_preview_start(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    start = await get_start_message()
    await callback.answer()
    markup = None
    from keyboards.inline import main_menu_kb as _mm

    markup = _mm(admin=True)
    if start.get("media_type") == "photo" and start.get("media_id"):
        try:
            await callback.message.answer_photo(
                start["media_id"], caption=start["text"][:1024], reply_markup=markup
            )
            return
        except TelegramAPIError:
            pass
    if start.get("media_type") == "video" and start.get("media_id"):
        try:
            await callback.message.answer_video(
                start["media_id"], caption=start["text"][:1024], reply_markup=markup
            )
            return
        except TelegramAPIError:
            pass
    await callback.message.answer(start["text"], reply_markup=markup)


# ---------------------------------------------------------------- admins


def admins_kb_view(items: list[dict], show_roles: bool = False):
    return admins_kb(items, ADMIN_IDS, show_roles=show_roles)


@router.callback_query(F.data == "admadd")
async def cb_add_admin(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.add_admin_id)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "➕ <b>Add admin</b>\n\n"
        "Forward any message from the user, or send their numeric id:",
        cancel_inline_kb(),
    )


@router.message(AdminStates.add_admin_id)
async def do_add_admin(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return

    tg_id: int | None = None
    username: str | None = None

    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "type", "") == "user":
        source = origin.sender_user
        tg_id = source.id
        username = source.username
    elif message.text and message.text.strip().lstrip("-").isdigit():
        tg_id = int(message.text.strip())
        try:
            chat = await message.bot.get_chat(tg_id)
            username = chat.username
        except TelegramAPIError:
            pass

    if tg_id is None:
        await message.answer(
            "Could not detect a user.\nForward their message or send the numeric id."
        )
        return

    await add_admin(tg_id, username)
    await features.audit(f"Admin added: {tg_id}")
    await state.clear()
    items = await get_admin_list()
    await message.answer(
        f"✅ Admin added: <code>{tg_id}</code>"
        + (f" (@{html.escape(username)})" if username else ""),
        reply_markup=panel_inline_kb(),
    )
    await message.answer(
        "👤 <b>Admins</b>",
        reply_markup=admins_kb_view(items),
    )


@router.callback_query(F.data.startswith("admdel:"))
async def cb_remove_admin(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid request", show_alert=True)
        return
    if tg_id in AUTH_ADMIN_IDS:
        await callback.answer("This admin is in .env and cannot be removed", show_alert=True)
        return
    await remove_admin(tg_id)
    await features.audit(f"Admin removed: {tg_id}")
    items = await get_admin_list()
    await callback.answer("Removed")
    await _edit_or_send(
        callback.message,
        "👤 <b>Admins</b>",
        admins_kb_view(items),
    )


# ---------------------------------------------------------------- protection


@router.callback_query(F.data == "aprot")
async def cb_toggle_protect(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    new_value = not await is_protect_content()
    await set_protect_content(new_value)
    await callback.answer("Saved" if new_value else "Protection disabled")
    await _edit_or_send(
        callback.message,
        PROTECT_TEXT,
        protect_kb(new_value),
    )


# ---------------------------------------------------------------- payment methods


@router.callback_query(F.data == "pmpanel")
async def cb_pm_panel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    methods = await get_all_methods()
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "💳 <b>Payment methods</b>",
        payment_methods_admin_kb(methods),
    )


@router.callback_query(F.data == "pmadd")
async def cb_pm_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await state.set_state(AdminStates.add_name)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "➕ <b>Add payment method</b>\n\n"
        "Send the display name, e.g. <code>USDT BEP-20</code> or <code>UPI / INR QR</code>.",
        cancel_inline_kb("pmaddcancel"),
    )


@router.callback_query(F.data == "pmaddcancel")
async def cb_pm_add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    methods = await get_all_methods()
    await callback.answer("Cancelled")
    await _edit_or_send(
        callback.message,
        "💳 <b>Payment methods</b>",
        payment_methods_admin_kb(methods),
    )


@router.message(AdminStates.add_name, F.text)
async def pm_name(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    name = message.text.strip()[:64]
    if not name:
        await message.answer("Name cannot be empty:")
        return
    await state.update_data(method_name=name)
    await state.set_state(AdminStates.choose_kind)
    await message.answer(
        f"Method “{html.escape(name)}” — choose the type:",
        reply_markup=kind_crypto_kb(),
    )


@router.message(AdminStates.choose_kind)
async def pm_kind_waiting(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Choose the type using the buttons above.")


@router.callback_query(F.data.startswith("kind:"))
async def pm_kind(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await state.get_state() != AdminStates.choose_kind.state:
        await callback.answer()
        return
    kind = callback.data.split(":", 1)[1]
    if kind not in {"crypto", "qr"}:
        await callback.answer("Invalid type", show_alert=True)
        return
    await state.update_data(kind=kind)
    if kind == "crypto":
        await state.set_state(AdminStates.add_detail)
        await callback.answer()
        await _edit_or_send(
            callback.message,
            "💎 Send the wallet address users must pay to (e.g. USDT BEP-20):",
        )
    else:
        await state.set_state(AdminStates.add_note)
        await callback.answer()
        await _edit_or_send(
            callback.message,
            "📱 Send payment instructions for the user (UPI ID, amount, note) "
            "or press <b>Skip</b>.",
            note_skip_kb(),
        )


@router.message(AdminStates.add_detail, F.text)
async def pm_detail(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    detail = message.text.strip()[:256]
    if not detail:
        await message.answer("Address cannot be empty:")
        return
    await state.update_data(detail=detail)
    await state.set_state(AdminStates.add_note)
    await message.answer(
        "Send payment instructions for the user (network, memo, amount). "
        "Or press <b>Skip</b>.",
        reply_markup=note_skip_kb(),
    )


@router.message(AdminStates.add_note, F.text)
async def pm_note(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(instructions=message.text.strip()[:1000])
    await _after_note(message, state, edit=False)


@router.callback_query(F.data == "noteskip")
async def pm_note_skip(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await state.get_state() != AdminStates.add_note.state:
        await callback.answer()
        return
    await callback.answer("Skipped")
    await _after_note(callback.message, state, edit=True)


async def _save_method(state: FSMContext) -> None:
    data = await state.get_data()
    await insert_method(
        {
            "name": data.get("method_name", "Payment"),
            "kind": data.get("kind", "crypto"),
            "instructions": data.get("instructions"),
            "detail": data.get("detail"),
            "qr_file_id": None,
            "is_active": True,
            "sort_order": 99,
        }
    )
    await state.clear()


async def _after_note(target: Message, state: FSMContext, edit: bool) -> None:
    data = await state.get_data()
    if data.get("kind") == "qr":
        await state.set_state(AdminStates.add_qr)
        text = "🖼 Send the <b>QR code image</b> (photo) users will scan to pay."
        if edit:
            await target.edit_text(text)
        else:
            await target.answer(text)
        return

    await _save_method(state)
    methods = await get_all_methods()
    text = "✅ Payment method added."
    markup = payment_methods_admin_kb(methods)
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.message(AdminStates.add_qr, F.photo)
async def pm_qr(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    await insert_method(
        {
            "name": data.get("method_name", "QR payment"),
            "kind": "qr",
            "instructions": data.get("instructions"),
            "detail": None,
            "qr_file_id": message.photo[-1].file_id,
            "is_active": True,
            "sort_order": 99,
        }
    )
    await state.clear()
    methods = await get_all_methods()
    await message.answer(
        "✅ QR payment method added.",
        reply_markup=payment_methods_admin_kb(methods),
    )


@router.message(AdminStates.add_qr, F.text)
async def pm_qr_waiting(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Please send the QR code as a <b>photo</b>.")


@router.callback_query(F.data.startswith("mv:"))
async def cb_method_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    method_id = callback.data.split(":", 1)[1]
    method = await get_method(method_id)
    if method is None:
        await callback.answer("Method deleted", show_alert=True)
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        await _method_view_text(method),
        payment_method_view_kb(
            method_id, bool(method.get("is_active")), str(method.get("kind") or "")
        ),
    )


async def _method_view_text(method: dict) -> str:
    kind = await method_kind_label(method)
    status = "🟢 active" if method.get("is_active") else "⚪ inactive"
    text = (
        f"💳 <b>{html.escape(method.get('name', '?'))}</b>\n\n"
        f"Type: {kind}\n"
        f"Status: {status}\n"
    )
    if method.get("detail"):
        text += f"Address: <code>{html.escape(method['detail'])}</code>\n"
    if method.get("kind") == "stars":
        username = method.get("username") or ""
        text += (
            f"Star receiver: @{html.escape(username)}\n"
            if username
            else "Star receiver: not set\n"
        )
        text += (
            "Buyer must open that account and send a screenshot of the /start chat.\n"
        )
    if method.get("instructions"):
        text += f"\nInstructions:\n{html.escape(method['instructions'])}\n"
    if method.get("kind") == "qr":
        text += f"\nQR image: {'attached ✅' if method.get('qr_file_id') else 'missing ❌'}"
    return text


@router.callback_query(F.data.startswith("mtoggle:"))
async def cb_method_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    method_id = callback.data.split(":", 1)[1]
    method = await get_method(method_id)
    if method is None:
        await callback.answer("Method deleted", show_alert=True)
        return
    new_value = not method.get("is_active")
    await update_method(method_id, {"is_active": new_value})
    method["is_active"] = new_value
    await features.audit(
        f"Method {method.get('name')} {'activated' if new_value else 'deactivated'}"
    )
    await callback.answer("Saved")
    await _edit_or_send(
        callback.message,
        await _method_view_text(method),
        payment_method_view_kb(method_id, new_value, str(method.get("kind") or "")),
    )


@router.callback_query(F.data.startswith("mdel:"))
async def cb_method_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    method_id = callback.data.split(":", 1)[1]
    await delete_method(method_id)
    await features.audit(f"Payment method deleted: {method_id}")
    methods = await get_all_methods()
    await callback.answer("Deleted")
    await _edit_or_send(
        callback.message,
        "💳 <b>Payment methods</b>",
        payment_methods_admin_kb(methods),
    )


# ---------------------------------------------------------------- log group


@router.message(AdminStates.waiting_log_group)
async def log_group_capture(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return

    chat_id: int | None = None
    source = None
    origin = getattr(message, "forward_origin", None)
    if origin is not None and getattr(origin, "type", "") == "chat":
        source = origin.sender_chat
    elif origin is not None and getattr(origin, "type", "") == "channel":
        source = origin.chat
    if source is None:
        source = message.forward_from_chat or message.sender_chat
    if source is not None and source.type in {"group", "supergroup", "channel"}:
        chat_id = source.id
    elif message.text and message.text.strip().lstrip("-").isdigit():
        candidate = int(message.text.strip())
        if candidate < 0:
            chat_id = candidate

    if chat_id is None:
        await message.answer(
            "Could not detect a group/channel.\n"
            "Forward a message from it, or send the numeric chat id."
        )
        return

    try:
        chat = await message.bot.get_chat(chat_id)
    except TelegramAPIError:
        await message.answer(
            "❌ Cannot access that chat. Add the bot as admin first."
        )
        return

    await set_setting("log_group_id", str(chat_id))
    await state.clear()
    await message.answer(
        f"✅ Log group set: <b>{html.escape(chat.title or str(chat_id))}</b>",
        reply_markup=panel_inline_kb(),
    )
    try:
        await message.bot.send_message(
            chat_id,
            "✅ Log group connected. Payment logs will appear here.",
        )
    except TelegramAPIError:
        await message.answer("⚠️ Could not post into that chat — add the bot as admin.")


@router.callback_query(F.data == "logclear")
async def cb_log_clear(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await set_setting("log_group_id", "")
    await state.clear()
    await callback.answer("Log group cleared")
    await _edit_or_send(
        callback.message,
        "📁 <b>Log group</b>\n\nCurrent: <code>not set</code>",
        None,
    )


# ---------------------------------------------------------------- approve / reject


@router.callback_query(F.data.startswith("payok:"))
async def cb_approve(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await _role_of(callback.from_user.id) != "full":
        await callback.answer("Only a full admin can approve payments", show_alert=True)
        return
    request_id = callback.data.split(":", 1)[1]
    request = await get_payment_request(request_id)
    if request is None:
        await callback.answer("Request not found", show_alert=True)
        return
    if request.get("status") != "pending":
        await callback.answer(f"Already {request.get('status')}", show_alert=True)
        return

    from utils.security import grant_access

    raw_ids = str(request.get("stuff_id") or "")
    stuff_ids = [sid for sid in raw_ids.split(",") if sid.strip()]
    for sid in stuff_ids:
        await grant_access(request["user_tg_id"], sid.strip())

    await set_request_status(request_id, "approved")
    await features.audit(
        f"Payment approved: Rs.{request.get('amount', '?')} from {request.get('user_tg_id')}"
    )
    await features.sale_alert(
        callback.bot,
        float(request.get("amount") or 0),
        str(request.get("user_username") or request.get("user_tg_id")),
        str(request.get("stuff_name") or "stuff"),
    )
    await notify_log_group(
        callback.bot,
        text=build_transaction_text(request, "Delivered"),
    )

    admin_name = callback.from_user.username or str(callback.from_user.id)
    caption = (
        f"✅ <b>Approved</b> by @{html.escape(admin_name)}\n"
        f"Stuffs: {html.escape(request.get('stuff_name') or '?')}\n"
        f"User: @{html.escape(request.get('user_username') or str(request.get('user_tg_id')))}"
    )
    try:
        await callback.message.edit_caption(caption=caption[:1024], reply_markup=None)
    except TelegramAPIError:
        logger.exception("Could not edit log message %s", request_id)

    try:
        await callback.bot.send_message(
            request["user_tg_id"],
            f"✅ <b>Payment approved!</b>\n\nDelivering "
            f"“{html.escape(request.get('stuff_name') or 'your stuff')}”…",
        )
    except TelegramAPIError:
        logger.exception("Could not notify user %s", request.get("user_tg_id"))

    if await features.is_on("receipts"):
        try:
            await callback.bot.send_message(
                request["user_tg_id"], features.receipt_text(request)
            )
        except TelegramAPIError:
            logger.exception("Receipt send failed for %s", request.get("user_tg_id"))

    total_sent = 0
    total_failed = 0
    first_stuff_id: str | None = None
    for sid in stuff_ids:
        doc = await get_stuff(sid.strip())
        if not doc:
            continue
        if first_stuff_id is None:
            first_stuff_id = sid.strip()
        sent, failed = await deliver_stuff(
            callback.bot, request["user_tg_id"], doc
        )
        total_sent += sent
        total_failed += failed

    if first_stuff_id:
        done = f"📦 <b>Delivered!</b> Media sent: {total_sent}."
        if total_failed:
            done += f"\n⚠️ Failed: {total_failed}."
        try:
            await callback.bot.send_message(
                request["user_tg_id"],
                done,
                reply_markup=after_payment_kb(first_stuff_id),
            )
        except TelegramAPIError:
            pass

    await callback.answer("Approved & delivered")


@router.callback_query(F.data.startswith("payno:"))
async def cb_reject(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await _role_of(callback.from_user.id) != "full":
        await callback.answer("Only a full admin can reject payments", show_alert=True)
        return
    request_id = callback.data.split(":", 1)[1]
    request = await get_payment_request(request_id)
    if request is None:
        await callback.answer("Request not found", show_alert=True)
        return
    if request.get("status") != "pending":
        await callback.answer(f"Already {request.get('status')}", show_alert=True)
        return

    await set_request_status(request_id, "rejected")
    await features.audit(
        f"Payment rejected: {request.get('user_tg_id')} (Rs.{request.get('amount', '?')})"
    )
    await notify_log_group(
        callback.bot,
        text=build_transaction_text(request, "Failed"),
    )

    admin_name = callback.from_user.username or str(callback.from_user.id)
    caption = (
        f"❌ <b>Rejected</b> by @{html.escape(admin_name)}\n"
        f"Stuffs: {html.escape(request.get('stuff_name') or '?')}\n"
        f"User: @{html.escape(request.get('user_username') or str(request.get('user_tg_id')))}"
    )
    try:
        await callback.message.edit_caption(caption=caption[:1024], reply_markup=None)
    except TelegramAPIError:
        logger.exception("Could not edit log message %s", request_id)

    try:
        await callback.bot.send_message(
            request["user_tg_id"],
            "❌ <b>Payment rejected</b>\n\n"
            "The screenshot was not accepted. If you believe this is a mistake, "
            "contact the administrator or try again.",
        )
    except TelegramAPIError:
        logger.exception("Could not notify user %s", request.get("user_tg_id"))

    await callback.answer("Rejected")


# ---------------------------------------------------------------- user manager


def _user_card_text(user: dict, tg_id: int, admin_user: bool) -> str:
    status = "🚫 Banned" if user.get("banned") else "🟢 Active"
    balance = float(user.get("balance", 0) or 0)
    from database.scoping import current_tag

    tag = current_tag()
    raw_purchased = user.get("purchased", []) or []
    if tag == "main":
        purchased = [p for p in raw_purchased if ":" not in str(p)]
    else:
        purchased = [p for p in raw_purchased if str(p).startswith(f"{tag}:")]
    username = user.get("username")
    name = user.get("full_name") or "—"
    return (
        "👤 <b>User card</b>\n"
        "──────────────────────\n"
        f"ID: <code>{tg_id}</code>\n"
        f"Name: {html.escape(str(name))}\n"
        f"Username: {('@' + html.escape(username)) if username else '—'}\n"
        f"Status: <b>{status}</b>\n"
        f"Balance: <b>Rs.{balance:g}</b>\n"
        f"Purchases: {len(purchased)} stuffs\n"
        f"Admin: {'yes' if admin_user else 'no'}"
    )


def _user_card_text_crm(user: dict, base: str, note: str, tags: list[str], history: list[dict]) -> str:
    tag_line = ", ".join(f"#{t}" for t in tags) if tags else "(none)"
    lines = [
        base,
        "──────────────────────",
        f"🏷 Tags: <b>{html.escape(tag_line)}</b>",
        f"📝 Note: {html.escape(note) if note else '(empty)'}",
    ]
    if history:
        lines.append("<b>Recent orders</b>")
        for row in history[:3]:
            moment = _event_time(row.get("created_at"))
            when = f"{moment:%d %b}" if moment else "?"
            lines.append(
                f"• {when} Rs.{float(row.get('amount', 0) or 0):g} — "
                f"{html.escape(str(row.get('stuff_name') or '?'))[:28]} "
                f"[{row.get('status')}]"
            )
    else:
        lines.append("Recent orders: none")
    return "\n".join(lines)


def _user_card_kb(
    tg_id: int,
    banned: bool,
    show_crm: bool = False,
    tags: list[str] | None = None,
) -> InlineKeyboardMarkup:
    ban_row = (
        ib("Unban", f"uunban:{tg_id}", "success")
        if banned
        else ib("Ban", f"uban:{tg_id}", "danger")
    )
    rows: list[list[InlineKeyboardButton]] = [
        [ban_row],
        [
            ib("Add balance", f"ubal:{tg_id}", "primary"),
            ib("Remove balance", f"ubalrm:{tg_id}", "primary"),
        ],
    ]
    if show_crm:
        rows.append(
            [
                ib("Note", f"unote:{tg_id}", "primary"),
                ib("Refresh", f"ucard:{tg_id}", "primary"),
            ]
        )
        tag_set = set(tags or [])
        rows.append(
            [
                ib(f"VIP [{'on' if 'VIP' in tag_set else 'off'}]", f"utag:{tg_id}:VIP", "primary"),
                ib(f"Watch [{'on' if 'Watch' in tag_set else 'off'}]", f"utag:{tg_id}:Watch", "primary"),
            ]
        )
        rows.append([ib("Back", "umgrback", "primary")])
    else:
        rows.append(
            [
                ib("Refresh", f"ucard:{tg_id}", "primary"),
                ib("Back", "umgrback", "primary"),
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_user_card(target: Message, tg_id: int) -> None:
    user = await find_user(tg_id)
    if user is None:
        from database.store import ensure_user

        user = await ensure_user(tg_id, None, None)
    admin_user = tg_id in ADMIN_IDS or any(
        a.get("tg_id") == tg_id for a in await get_admin_list()
    )
    crm = await features.is_on("crm")
    base = _user_card_text(user, tg_id, admin_user)
    if crm:
        note = await features.get_note(tg_id)
        tags = await features.get_tags(tg_id)
        history = await features.purchase_history(tg_id, 3)
        text = _user_card_text_crm(user, base, note, tags, history)
    else:
        tags = []
        text = base
    await _edit_or_send(
        target,
        text,
        _user_card_kb(tg_id, bool(user.get("banned")), show_crm=crm, tags=tags),
    )


@router.message(AdminStates.user_lookup, F.text)
async def user_lookup(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("Send a numeric user ID, e.g. <code>123456789</code>:")
        return
    tg_id = int(raw)
    await state.clear()
    await _show_user_card(message, tg_id)


@router.callback_query(F.data.startswith("ucard:"))
async def cb_user_card(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    await callback.answer()
    await _show_user_card(callback.message, tg_id)


@router.callback_query(F.data.startswith("uban:"))
async def cb_user_ban(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await _role_of(callback.from_user.id) != "full":
        await callback.answer("Only a full admin can ban users", show_alert=True)
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    if tg_id == callback.from_user.id or tg_id in ADMIN_IDS:
        await callback.answer("You cannot ban an admin", show_alert=True)
        return
    await set_banned(tg_id, True)
    await features.audit(f"User banned: {tg_id}")
    await callback.answer("User banned 🚫")
    await _show_user_card(callback.message, tg_id)


@router.callback_query(F.data.startswith("uunban:"))
async def cb_user_unban(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await _role_of(callback.from_user.id) != "full":
        await callback.answer("Only full admins can do this", show_alert=True)
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    await set_banned(tg_id, False)
    await features.audit(f"User unbanned: {tg_id}")
    await callback.answer("User unbanned ✅")
    await _show_user_card(callback.message, tg_id)


async def _ask_balance_op(
    callback: CallbackQuery, state: FSMContext, tg_id: int, op: str
) -> None:
    await state.clear()
    await state.set_state(AdminStates.balance_op)
    await state.update_data(target_id=tg_id, op=op)
    label = "ADD" if op == "add" else "REMOVE"
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"💵 Send the amount to <b>{label}</b> for user <code>{tg_id}</code>\n\n"
        "Example: <code>100</code>",
        cancel_inline_kb(),
    )


@router.callback_query(F.data.startswith("ubal:"))
async def cb_balance_add_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await _role_of(callback.from_user.id) != "full":
        await callback.answer("Only a full admin can change balances", show_alert=True)
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    await _ask_balance_op(callback, state, tg_id, "add")


@router.callback_query(F.data.startswith("ubalrm:"))
async def cb_balance_remove_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if await _role_of(callback.from_user.id) != "full":
        await callback.answer("Only a full admin can change balances", show_alert=True)
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    await _ask_balance_op(callback, state, tg_id, "remove")


@router.message(AdminStates.balance_op, F.text)
async def balance_op_apply(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = data.get("target_id")
    op = data.get("op", "add")
    if tg_id is None:
        await state.clear()
        return

    raw = message.text.strip().replace(",", ".")
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    try:
        amount = float(digits) if digits else 0.0
    except ValueError:
        amount = 0.0
    if amount <= 0:
        await message.answer("Send a positive number, e.g. <code>100</code>:")
        return

    if await features.is_on("dconfirm"):
        await state.update_data(bal_amount=amount)
        label = "ADD" if op == "add" else "REMOVE"
        await message.answer(
            f"⚠️ <b>Confirm</b>\n\n"
            f"{label} <b>Rs.{amount:g}</b> "
            f"{'to' if op == 'add' else 'from'} user <code>{tg_id}</code>?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        ib("Confirm", "uokbal", "success"),
                        ib("Cancel", "ucalbal", "danger"),
                    ]
                ]
            ),
        )
        return

    action, new_balance = await _apply_balance(int(tg_id), op, amount)

    await state.clear()
    await features.audit(f"Balance {action} for {tg_id} (now Rs.{new_balance:g})")
    await message.answer(
        f"✅ Balance updated: {action}\n"
        f"New balance of <code>{tg_id}</code>: <b>Rs.{new_balance:g}</b>",
        reply_markup=panel_inline_kb(),
    )
    await _show_user_card(message, tg_id)


@router.callback_query(F.data == "umgrback")
async def cb_user_manager_back(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await state.set_state(AdminStates.user_lookup)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "👥 <b>User manager</b>\n\n"
        "Send the <b>user ID</b> (numeric, e.g. <code>123456789</code>):",
        None,
    )


# ---------------------------------------------------------------- broadcast


def _bc_audience_kb(tag: str | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Everyone", "bcaud:all"), ib("Buyers", "bcaud:buyers")],
            [ib("Non-buyers", "bcaud:nonbuyers"), ib("A stuff's buyers", "bcaud:stuffbuyers")],
            [ib("Cancel", "bccancel", "danger")],
        ]
    )


_AUDIENCE_LABELS = {
    "all": "everyone",
    "buyers": "buyers only",
    "nonbuyers": "non-buyers",
    "stuffbuyers": "a stuff's buyers",
}


async def _ask_audience(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    tag = str(data.get("bc_tag") or "")
    media = data.get("bc_media") or {}
    kind = str(media.get("kind") or "")
    kind_label = {
        "photo": "photo",
        "video": "video",
        "animation": "GIF",
        "sticker": "sticker",
        "document": "file",
    }.get(kind, "text")
    scope = f"clone <b>@{html.escape(tag)}</b>" if tag else "this bot"
    await target.answer(
        f"📣 <b>Audience</b>\n\n"
        f"Message: <b>{kind_label}</b>\n"
        f"Send to users of {scope}:\n\n"
        "Choose who receives it:",
        reply_markup=_bc_audience_kb(tag or None),
    )


@router.message(AdminStates.broadcast_text, F.text)
async def broadcast_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    text = message.text or ""
    if not text.strip():
        await message.answer("Send a non-empty text:")
        return
    await state.update_data(bc_text=text, bc_media=None)
    await _ask_audience(message, state)


@router.message(
    AdminStates.broadcast_text,
    F.photo | F.video | F.animation | F.sticker | F.document,
)
async def broadcast_media_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    caption = (message.caption or "").strip()
    if message.sticker:
        media = {"kind": "sticker", "file_id": message.sticker.file_id}
    elif message.photo:
        media = {"kind": "photo", "file_id": message.photo[-1].file_id}
    elif message.video:
        media = {"kind": "video", "file_id": message.video.file_id}
    elif message.animation:
        media = {"kind": "animation", "file_id": message.animation.file_id}
    elif message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("video/"):
            media = {"kind": "video", "file_id": message.document.file_id}
        elif mime.startswith("image/"):
            media = {"kind": "photo", "file_id": message.document.file_id}
        else:
            await message.answer(
                "Send a photo, video, GIF or sticker (or plain text):"
            )
            return
    else:
        await message.answer("Send a photo, video, GIF, sticker or text:")
        return
    await state.update_data(
        bc_media=media, bc_text=caption, bc_text_set=bool(caption)
    )
    if not caption:
        await state.update_data(bc_text="")
    await _ask_audience(message, state)


@router.message(AdminStates.broadcast_text)
async def broadcast_invalid(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer(
        "Send text, a photo, a video, a GIF or a sticker (or press Cancel):"
    )


async def _bc_targets(data: dict) -> list[int]:
    kind = str(data.get("bc_aud") or "all")
    stuff_id = data.get("bc_stuff")
    tag = str(data.get("bc_tag") or "")
    if tag:
        with as_tag(tag):
            ids = set(await features.segment_ids(kind, stuff_id))
        seen = set(await list_user_ids_seen(tag))
        return sorted(ids & seen)
    return await features.segment_ids(kind, stuff_id)


async def _show_bc_preview(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    targets = await _bc_targets(data)
    text = str(data.get("bc_text") or "")
    media = data.get("bc_media") or {}
    kind = str(media.get("kind") or "")
    kind_label = {
        "photo": "📷 Photo",
        "video": "🎬 Video",
        "animation": "🎞 GIF",
        "sticker": "🏷 Sticker",
        "document": "📎 File",
    }.get(kind, "📝 Text")
    aud = _AUDIENCE_LABELS.get(str(data.get("bc_aud") or "all"), "everyone")
    tag = str(data.get("bc_tag") or "")
    scope = f"clone @{tag}" if tag else "this bot"
    preview = text if len(text) <= 800 else text[:800] + "…"
    body = f"{kind_label} — sent as-is"
    if preview:
        body += "\n──────────────────────\n" + preview
    elif kind == "sticker":
        body += "\n(no caption — stickers cannot carry text)"
    await target.answer(
        f"📣 <b>Broadcast preview</b>\n"
        f"Audience: <b>{aud}</b> of {scope}\n"
        f"Recipients: <b>{len(targets)}</b>\n"
        "──────────────────────\n"
        f"{body}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    ib(f"Send to {len(targets)}", "bcsend", "success"),
                    ib("Cancel", "bccancel", "danger"),
                ]
            ]
        ),
    )


@router.callback_query(F.data.startswith("bcaud:"))
async def cb_bc_audience(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    kind = callback.data.split(":", 1)[1]
    if kind not in _AUDIENCE_LABELS:
        await callback.answer("Invalid", show_alert=True)
        return
    if kind == "stuffbuyers":
        items = await list_stuffs(include_archived=True)
        if not items:
            await callback.answer("Add a stuff first", show_alert=True)
            return
        rows = [
            [ib(str(s.get("name", "?"))[:48], f"bcastf:{s['_id']}", "primary")]
            for s in items[:25]
        ]
        rows.append([ib("Cancel", "bccancel", "danger")])
        await callback.answer()
        await callback.message.answer(
            "📦 Choose the stuff whose buyers should receive this:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    await state.update_data(bc_aud=kind, bc_stuff=None)
    await callback.answer(f"Audience: {_AUDIENCE_LABELS[kind]}")
    await _show_bc_preview(callback.message, state)


@router.callback_query(F.data.startswith("bcastf:"))
async def cb_bc_stuff_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    await state.update_data(bc_aud="stuffbuyers", bc_stuff=stuff_id)
    await callback.answer(f"Audience: buyers of {doc.get('name')}")
    await _show_bc_preview(callback.message, state)


async def _send_one(bot, tg_id: int, data: dict) -> None:
    text = str(data.get("bc_text") or "")
    media = data.get("bc_media") or {}
    kind = str(media.get("kind") or "")
    file_id = str(media.get("file_id") or "")
    if kind == "photo" and file_id:
        await bot.send_photo(tg_id, file_id, caption=text or None)
    elif kind == "video" and file_id:
        await bot.send_video(tg_id, file_id, caption=text or None)
    elif kind == "animation" and file_id:
        await bot.send_animation(tg_id, file_id, caption=text or None)
    elif kind == "sticker" and file_id:
        await bot.send_sticker(tg_id, file_id)
    elif kind == "document" and file_id:
        await bot.send_document(tg_id, file_id, caption=text or None)
    else:
        await bot.send_message(tg_id, text)


@router.callback_query(F.data == "bcsend")
async def broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    text = str(data.get("bc_text") or "")
    media = data.get("bc_media") or {}
    if not text.strip() and not media.get("file_id"):
        await callback.answer("Nothing to send", show_alert=True)
        return
    tag = str(data.get("bc_tag") or "")
    targets = await _bc_targets(data)
    await state.clear()
    await callback.answer("Broadcasting…")
    await _edit_or_send(callback.message, "📣 <b>Broadcasting…</b>", None)

    send_bot = callback.bot
    own_session = None
    if tag:
        entries = await clone_manager.load_registry()
        entry = next((e for e in entries if e.get("tag") == tag), None)
        if entry is None or not entry.get("token"):
            await _edit_or_send(
                callback.message,
                "❌ Clone not found — it may have been removed.",
                _back_panel_kb(),
            )
            return
        from aiogram.client.default import DefaultBotProperties

        own_session = Bot(
            token=str(entry["token"]),
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        send_bot = own_session

    sent = 0
    failed = 0
    for tg_id in targets:
        try:
            await _send_one(send_bot, tg_id, data)
            sent += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)

    if own_session is not None:
        try:
            await own_session.session.close()
        except Exception:
            logger.exception("clone broadcast session close failed")

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[ib("Panel", "aback", "primary")]]
    )
    scope = f"clone {tag}" if tag else "main bot"
    await features.audit(
        f"Broadcast ({scope}) sent to {sent} user(s) ({failed} failed)"
    )
    await _edit_or_send(
        callback.message,
        f"📣 <b>Broadcast finished</b>\n"
        "──────────────────────\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Targeted: {len(targets)}\n"
        f"Scope: {scope}",
        markup,
    )


@router.callback_query(F.data == "bccancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    tag = str(data.get("bc_tag") or "")
    await state.clear()
    await callback.answer("Cancelled")
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[ib("Panel", "aback", "primary")]]
    )
    await _edit_or_send(callback.message, "❌ Broadcast cancelled.", markup)
    if tag:
        await _show_clones(callback.message)


# ---------------------------------------------------------------- stats / vps


def _refresh_kb(callback_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Refresh", callback_name, "primary"),
                ib("Panel", "aback", "primary"),
            ]
        ]
    )


@router.callback_query(F.data == "bstats")
async def cb_stats(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await _edit_or_send(
        callback.message, await stats_text(), _refresh_kb("bstats")
    )


async def cb_vps(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await _edit_or_send(callback.message, vps_text(), _refresh_kb("bvps"))


# ---------------------------------------------------------------- showcase / proofs


@router.callback_query(F.data.startswith("sshow:"))
async def cb_showcase_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.set_showcase)
    await state.update_data(stuff_id=stuff_id)
    await callback.answer()
    current = "set" if doc.get("showcase") else "not set"
    await _edit_or_send(
        callback.message,
        f"🖼 <b>Showcase image</b>\n\n"
        f"Current: {current}\n\n"
        f"Send a new photo to replace it.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Back", f"sadm:{stuff_id}", "primary")],
            ]
        ),
    )


@router.message(AdminStates.set_showcase, F.photo)
async def do_set_showcase(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    stuff_id = data.get("stuff_id")
    if not stuff_id:
        await state.clear()
        return
    await set_showcase(stuff_id, message.photo[-1].file_id)
    await state.clear()
    doc = await get_stuff(stuff_id)
    await message.answer(
        "Showcase image updated.",
        reply_markup=(
            await _stuff_kb_for(doc or {})
            if doc
            else None
        ),
    )


@router.message(AdminStates.set_showcase)
async def do_set_showcase_invalid(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Send a photo.")


@router.callback_query(F.data.startswith("sproof:"))
async def cb_proofs_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    proofs = doc.get("proofs") or []
    await state.clear()
    await state.set_state(AdminStates.add_proofs)
    await state.update_data(stuff_id=stuff_id)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"🧾 <b>Proof screenshots</b>\n\n"
        f"Current proofs: {len(proofs)}\n\n"
        f"Send a photo to add another proof. Existing proofs stay.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Done", f"sproofsdone:{stuff_id}", "success")],
                [ib("Back", f"sadm:{stuff_id}", "primary")],
            ]
        ),
    )


@router.message(AdminStates.add_proofs, F.photo)
async def do_add_proof(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    stuff_id = data.get("stuff_id")
    if not stuff_id:
        await state.clear()
        return
    doc = await get_stuff(stuff_id)
    if doc is None:
        await state.clear()
        await message.answer("Stuffs deleted.")
        return
    proofs = list(doc.get("proofs") or [])
    proofs.append(message.photo[-1].file_id)
    await set_proofs(stuff_id, proofs)
    await message.answer(
        f"Proof added. Total proofs: {len(proofs)}.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Done", f"sproofsdone:{stuff_id}", "success")],
                [ib("Back", f"sadm:{stuff_id}", "primary")],
            ]
        ),
    )


@router.message(AdminStates.add_proofs)
async def do_add_proof_invalid(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Send a photo, or press Done / Back.")


@router.callback_query(F.data.startswith("sproofsdone:"))
async def cb_proofs_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    await state.clear()
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    await callback.answer("Saved")
    await _edit_or_send(
        callback.message,
        await _stuff_view_text(doc),
        await _stuff_kb_for(doc),
    )


# ---------------------------------------------------------------- stars username


@router.callback_query(F.data.startswith("suname:"))
async def cb_stars_username_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    method_id = callback.data.split(":", 1)[1]
    method = await get_method(method_id)
    if method is None:
        await callback.answer("Method deleted", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.add_stars_username)
    await state.update_data(method_id=method_id)
    await callback.answer()
    current = method.get("username") or "not set"
    await _edit_or_send(
        callback.message,
        f"⭐ <b>Star receiver username</b>\n\n"
        f"Current: @{html.escape(str(current))}\n\n"
        "Send the @username of the Telegram account that receives Stars "
        "(without @), e.g. <code>my_store_account</code>.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Back", f"mv:{method_id}", "primary")],
            ]
        ),
    )


@router.message(AdminStates.add_stars_username, F.text)
async def do_stars_username(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    raw = message.text.strip().lstrip("@").strip()
    if not raw or not all(ch.isalnum() or ch == "_" for ch in raw):
        await message.answer("Send a valid username (letters, digits, underscore):")
        return
    data = await state.get_data()
    method_id = data.get("method_id")
    if not method_id:
        await state.clear()
        return
    await update_method(method_id, {"username": raw})
    await state.clear()
    method = await get_method(method_id)
    await message.answer(
        f"Star receiver set to @{html.escape(raw)}.",
        reply_markup=(
            payment_method_view_kb(
                method_id,
                bool(method.get("is_active")),
                str(method.get("kind") or ""),
            )
            if method
            else None
        ),
    )


# ---------------------------------------------------------------- back


# ---------------------------------------------------------------- features


async def _show_features(target: Message) -> None:
    mapping = await features.flags()
    entries = [
        (key, label, bool(mapping.get(key, False)))
        for key, (label, _desc) in features.FEATURES.items()
    ]
    await _edit_or_send(
        target,
        features.flags_text(mapping),
        features_kb(
            entries,
            show_leaderboard=mapping.get("leaderboard", True),
            show_audit=mapping.get("audit", True),
            show_csv=mapping.get("csv", True),
        ),
    )


@router.callback_query(F.data.startswith("feat:"))
async def cb_feat_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 1)[1]
    if key not in features.FEATURES:
        await callback.answer("Unknown feature", show_alert=True)
        return
    mapping = await features.flags()
    new_value = not mapping.get(key, key not in features.DEFAULT_OFF)
    await features.set_flag(key, new_value)
    label = features.FEATURES[key][0]
    await features.audit(f"Feature {label}: {'ON' if new_value else 'OFF'}")
    await callback.answer(f"{label}: {'ON' if new_value else 'OFF'}")
    await _show_features(callback.message)


@router.callback_query(F.data.startswith("featcfg:"))
async def cb_feat_config(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 1)[1]
    await state.clear()
    await callback.answer()
    target = callback.message
    if key == "ref":
        bonus = await features.get_referral_bonus()
        await state.set_state(AdminStates.cfg_ref_bonus)
        await _edit_or_send(
            target,
            f"🎁 <b>Referral bonus</b>\n\nCurrent: Rs.{bonus:g}\n\n"
            "Send the bonus (in Rs.) the inviter earns on a first purchase:",
            cancel_inline_kb("apnl:features"),
        )
    elif key == "checkin":
        bonus = await features.get_checkin_bonus()
        await state.set_state(AdminStates.cfg_checkin_bonus)
        await _edit_or_send(
            target,
            f"📅 <b>Check-in bonus</b>\n\nCurrent: Rs.{bonus:g}\n\n"
            "Send the daily check-in bonus (in Rs.):",
            cancel_inline_kb("apnl:features"),
        )
    elif key == "fj":
        channel = await features.get_forcejoin()
        await state.set_state(AdminStates.cfg_forcejoin)
        await _edit_or_send(
            target,
            "🔒 <b>Force-join channel</b>\n\n"
            f"Current: {html.escape(channel) or 'not set'}\n\n"
            "Send the channel <code>@username</code> or numeric chat ID.\n"
            "Send <code>off</code> to clear it.",
            cancel_inline_kb("apnl:features"),
        )
    elif key == "btime":
        current = await features.get_backup_time()
        await state.set_state(AdminStates.cfg_backup_time)
        await _edit_or_send(
            target,
            f"💾 <b>Backup time</b>\n\nCurrent: {current}\n\n"
            "Send the daily backup time as <code>HH:MM</code> (server local time):",
            cancel_inline_kb("apnl:features"),
        )
    elif key == "rtime":
        current = await features.get_report_time()
        await state.set_state(AdminStates.cfg_report_time)
        await _edit_or_send(
            target,
            f"📊 <b>Report time</b>\n\nCurrent: {current}\n\n"
            "Send the daily report time as <code>HH:MM</code> (server local time):",
            cancel_inline_kb("apnl:features"),
        )
    elif key == "cashback":
        current = await features.get_cashback_pct()
        await state.set_state(AdminStates.cfg_cashback)
        await _edit_or_send(
            target,
            f"💰 <b>Cashback %</b>\n\nCurrent: {current:g}%\n\n"
            "Send the percent (0–100) buyers get back in wallet on each "
            "approved order:",
            cancel_inline_kb("apnl:features"),
        )
    elif key == "autoapp":
        current = await features.get_autoapprove_max()
        await state.set_state(AdminStates.cfg_autoapprove)
        await _edit_or_send(
            target,
            f"⚡ <b>Auto-approve max</b>\n\nCurrent: Rs.{current:g}\n\n"
            "Payment claims up to this amount are approved and delivered "
            "instantly (screenshot still required).\n"
            "Send an amount, or <code>0</code> to disable:",
            cancel_inline_kb("apnl:features"),
        )
    elif key == "alert":
        current = await features.get_alert_min()
        await state.set_state(AdminStates.cfg_alert)
        await _edit_or_send(
            target,
            f"🚨 <b>Big-sale alert</b>\n\nCurrent: Rs.{current:g}\n\n"
            "Admins get a DM when an approved sale is at least this much "
            "(new tickets always alert when the alerts feature is on).\n"
            "Send an amount, or <code>0</code> to disable:",
            cancel_inline_kb("apnl:features"),
        )


async def _features_done(message: Message, state: FSMContext, note: str) -> None:
    await state.clear()
    await message.answer(f"✅ {note}", reply_markup=cancel_inline_kb("aback"))


@router.message(AdminStates.cfg_ref_bonus, F.text)
async def cfg_ref_bonus_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        value = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a number, e.g. 20")
        return
    if value < 0:
        await message.answer("Value must be 0 or more")
        return
    await features.set_referral_bonus(value)
    await features.audit(f"Referral bonus set to {value:g}")
    await _features_done(message, state, f"Referral bonus set to Rs.{value:g}")


@router.message(AdminStates.cfg_checkin_bonus, F.text)
async def cfg_checkin_bonus_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        value = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a number, e.g. 5")
        return
    if value < 0:
        await message.answer("Value must be 0 or more")
        return
    await features.set_checkin_bonus(value)
    await features.audit(f"Check-in bonus set to {value:g}")
    await _features_done(message, state, f"Check-in bonus set to Rs.{value:g}")


@router.message(AdminStates.cfg_forcejoin, F.text)
async def cfg_forcejoin_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if value.lower() in ("off", "none", "clear", "0"):
        await features.set_forcejoin("")
        await features.audit("Force-join channel cleared")
        await _features_done(message, state, "Force-join cleared")
        return
    await features.set_forcejoin(value)
    await features.audit(f"Force-join set to {value}")
    await _features_done(message, state, f"Force-join set to {value}")


@router.message(AdminStates.cfg_backup_time, F.text)
async def cfg_backup_time_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not features.valid_hhmm(value):
        await message.answer("Send a valid time like 03:00")
        return
    await features.set_backup_time(value)
    await features.audit(f"Backup time set to {value}")
    await _features_done(message, state, f"Backup time set to {value}")


@router.message(AdminStates.cfg_report_time, F.text)
async def cfg_report_time_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    value = (message.text or "").strip()
    if not features.valid_hhmm(value):
        await message.answer("Send a valid time like 21:00")
        return
    await features.set_report_time(value)
    await features.audit(f"Report time set to {value}")
    await _features_done(message, state, f"Report time set to {value}")


@router.message(AdminStates.cfg_cashback, F.text)
async def cfg_cashback_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        value = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a number, e.g. 10")
        return
    if not 0 <= value <= 100:
        await message.answer("Percent must be between 0 and 100")
        return
    await features.set_cashback_pct(value)
    await features.audit(f"Cashback set to {value:g}%")
    await _features_done(message, state, f"Cashback set to {value:g}%")


@router.message(AdminStates.cfg_autoapprove, F.text)
async def cfg_autoapprove_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        value = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a number, e.g. 100")
        return
    if value < 0:
        await message.answer("Value must be 0 or more")
        return
    await features.set_autoapprove_max(value)
    await features.audit(f"Auto-approve max set to Rs.{value:g}")
    note = (
        f"Auto-approve max set to Rs.{value:g}"
        if value > 0
        else "Auto-approve disabled (max = 0)"
    )
    await _features_done(message, state, note)


@router.callback_query(F.data == "featlb")
async def cb_feat_leaderboard(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("leaderboard"):
        await callback.answer("Leaderboard feature is off", show_alert=True)
        return
    totals: dict[int, dict] = {}
    for request in await list_requests():
        if str(request.get("status")) != "approved":
            continue
        try:
            uid = int(request.get("user_tg_id") or 0)
        except (TypeError, ValueError):
            continue
        if not uid:
            continue
        entry = totals.setdefault(uid, {"orders": 0, "spent": 0.0})
        entry["orders"] += 1
        try:
            entry["spent"] += float(request.get("amount", 0) or 0)
        except (TypeError, ValueError):
            pass
    ranked = sorted(
        totals.items(), key=lambda kv: (kv[1]["spent"], kv[1]["orders"]), reverse=True
    )[:10]
    lines = ["🏆 <b>Leaderboard</b>\n"]
    if not ranked:
        lines.append("No approved orders yet.")
    for position, (uid, entry) in enumerate(ranked, 1):
        user = await find_user(uid)
        name = (user or {}).get("full_name") or str(uid)
        lines.append(
            f"{position}. {html.escape(str(name))[:40]} — "
            f"{entry['orders']} order(s), Rs.{entry['spent']:g}"
        )
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "\n".join(lines),
        cancel_inline_kb("apnl:features"),
    )


@router.callback_query(F.data == "featcsv")
async def cb_feat_csv(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("csv"):
        await callback.answer("CSV export feature is off", show_alert=True)
        return
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["tg_id", "username", "full_name", "balance", "banned", "purchased"]
    )
    for tg_id in await list_user_ids():
        user = await find_user(tg_id)
        if not user:
            continue
        writer.writerow(
            [
                tg_id,
                user.get("username") or "",
                user.get("full_name") or "",
                user.get("balance", 0),
                bool(user.get("banned")),
                len(user.get("purchased") or []),
            ]
        )
    data = buffer.getvalue().encode("utf-8-sig")
    try:
        await callback.message.answer_document(
            BufferedInputFile(data, filename="users.csv"),
            caption=f"👥 Users export ({len(data)} bytes)",
        )
    except TelegramAPIError:
        logger.exception("CSV send failed")
        await callback.answer("Failed to send the file", show_alert=True)
        return
    await features.audit("Exported users CSV")
    await callback.answer("CSV ready")


# ---------------------------------------------------------------- coupons


async def _coupons_text(coupons: list[dict]) -> str:
    if not coupons:
        return (
            "🎟 <b>Coupons</b>\n\n"
            "No coupons yet. Tap Add coupon to create one.\n"
            "Buyers enter the code at checkout for a discount."
        )
    lines = ["🎟 <b>Coupons</b>\n"]
    for coupon in coupons:
        code = str(coupon.get("code", "?")).upper()
        percent = coupon.get("percent", 0)
        max_uses = int(coupon.get("max_uses", 0) or 0)
        used = int(coupon.get("used", 0) or 0)
        active = "on" if coupon.get("active", True) else "off"
        limit = f"{used}/{max_uses}" if max_uses else f"{used} uses"
        lines.append(f"• <code>{code}</code> — {percent}% off ({limit}) [{active}]")
    return "\n".join(lines)


async def _show_coupons(target: Message) -> None:
    coupons = await features.get_coupons()
    await _edit_or_send(target, await _coupons_text(coupons), coupons_kb(coupons))


@router.callback_query(F.data == "cpadd")
async def cb_coupon_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.coupon_code)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "➕ <b>Add coupon</b>\n\nSend the coupon code (3–32 letters/numbers):",
        cancel_inline_kb("apnl:coupons"),
    )


@router.callback_query(F.data == "cpnone")
async def cb_coupon_none(callback: CallbackQuery) -> None:
    await callback.answer("Press Add coupon to create one")


@router.message(AdminStates.coupon_code, F.text)
async def coupon_code_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    code = (message.text or "").strip().upper()
    if not 3 <= len(code) <= 32 or not code.replace("-", "").isalnum():
        await message.answer("Send a code like SAVE10 (3-32 letters/numbers)")
        return
    if await features.find_coupon(code):
        await message.answer("That coupon already exists")
        return
    await state.update_data(code=code)
    await state.set_state(AdminStates.coupon_percent)
    await message.answer(
        f"Code: <code>{html.escape(code)}</code>\n\n"
        "Send the discount percent (1-100):"
    )


@router.message(AdminStates.coupon_percent, F.text)
async def coupon_percent_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        percent = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a whole number, e.g. 10")
        return
    if not 1 <= percent <= 100:
        await message.answer("Percent must be between 1 and 100")
        return
    await state.update_data(percent=percent)
    await state.set_state(AdminStates.coupon_max)
    await message.answer("Send the max number of uses (send 0 for unlimited):")


@router.message(AdminStates.coupon_max, F.text)
async def coupon_max_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        max_uses = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a whole number, e.g. 50")
        return
    if max_uses < 0:
        await message.answer("Use 0 for unlimited, or a positive number")
        return
    data = await state.get_data()
    coupons = await features.get_coupons()
    coupons.append(
        {
            "code": data.get("code", ""),
            "percent": int(data.get("percent", 0)),
            "max_uses": max_uses,
            "used": 0,
            "active": True,
        }
    )
    await features.save_coupons(coupons)
    await features.audit(f"Coupon {data.get('code')} created ({data.get('percent')}%)")
    await state.clear()
    await message.answer(
        f"✅ Coupon <code>{html.escape(str(data.get('code')))}</code> created.",
        reply_markup=panel_inline_kb(),
    )
    await _show_coupons(message)


@router.callback_query(F.data.startswith("cpoff:"))
async def cb_coupon_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        index = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    coupons = await features.get_coupons()
    if not 0 <= index < len(coupons):
        await callback.answer("Coupon not found", show_alert=True)
        return
    coupons[index]["active"] = not coupons[index].get("active", True)
    await features.save_coupons(coupons)
    await callback.answer("Toggled")
    await _show_coupons(callback.message)


@router.callback_query(F.data.startswith("cpdel:"))
async def cb_coupon_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        index = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    coupons = await features.get_coupons()
    if not 0 <= index < len(coupons):
        await callback.answer("Coupon not found", show_alert=True)
        return
    removed = coupons.pop(index)
    await features.save_coupons(coupons)
    await features.audit(f"Coupon {removed.get('code')} deleted")
    await callback.answer("Deleted")
    await _show_coupons(callback.message)


# ---------------------------------------------------------------- gift codes


def _gifts_text(codes: list[dict]) -> str:
    if not codes:
        return (
            "🎁 <b>Gift codes</b>\n\n"
            "No codes yet. Tap Add gift code to create one.\n"
            "Users redeem codes from the menu and get wallet balance."
        )
    lines = ["🎁 <b>Gift codes</b>\n"]
    for entry in codes:
        code = str(entry.get("code", "?")).upper()
        try:
            amount = float(entry.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if entry.get("used"):
            state = "used"
        else:
            state = "on" if entry.get("active", True) else "off"
        lines.append(f"• <code>{code}</code> — Rs.{amount:g} [{state}]")
    return "\n".join(lines)


async def _show_gifts(target: Message) -> None:
    codes = await features.get_giftcodes()
    await _edit_or_send(target, _gifts_text(codes), gifts_kb(codes))


@router.callback_query(F.data == "gadd")
async def cb_gift_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.gift_amount)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "➕ <b>Add gift code</b>\n\nSend the wallet amount (in Rs.):",
        cancel_inline_kb("apnl:gifts"),
    )


@router.callback_query(F.data == "gnone")
async def cb_gift_none(callback: CallbackQuery) -> None:
    await callback.answer("Press Add gift code to create one")


@router.message(AdminStates.gift_amount, F.text)
async def gift_amount_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        amount = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a number, e.g. 50")
        return
    if amount <= 0:
        await message.answer("Amount must be more than 0")
        return
    await state.update_data(gift_amount=round(amount, 2))
    await state.set_state(AdminStates.gift_code)
    await message.answer(
        f"Amount: Rs.{amount:g}\n\n"
        "Send the code (3–32 letters/numbers), or <code>skip</code> "
        "to generate a random one:"
    )


@router.message(AdminStates.gift_code, F.text)
async def gift_code_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().upper()
    if raw.lower() == "skip":
        code = uuid.uuid4().hex[:10].upper()
    else:
        code = raw
        if not 3 <= len(code) <= 32 or not code.replace("-", "").isalnum():
            await message.answer(
                "Send a code like BONUS50 (3-32 letters/numbers), or skip"
            )
            return
    for entry in await features.get_giftcodes():
        if str(entry.get("code", "")).lower() == code.lower():
            await message.answer("That code already exists")
            return
    data = await state.get_data()
    codes = await features.get_giftcodes()
    codes.append(
        {
            "code": code,
            "amount": float(data.get("gift_amount", 0) or 0),
            "used": False,
            "active": True,
        }
    )
    await features.save_giftcodes(codes)
    await features.audit(
        f"Gift code {code} created (Rs.{float(data.get('gift_amount', 0) or 0):g})"
    )
    await state.clear()
    await message.answer(
        f"✅ Gift code <code>{html.escape(code)}</code> created.",
        reply_markup=panel_inline_kb(),
    )
    await _show_gifts(message)


@router.callback_query(F.data.startswith("goff:"))
async def cb_gift_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        index = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    codes = await features.get_giftcodes()
    if not 0 <= index < len(codes):
        await callback.answer("Code not found", show_alert=True)
        return
    if codes[index].get("used"):
        await callback.answer("Already redeemed", show_alert=True)
        return
    codes[index]["active"] = not codes[index].get("active", True)
    await features.save_giftcodes(codes)
    await callback.answer("Toggled")
    await _show_gifts(callback.message)


@router.callback_query(F.data.startswith("gdel:"))
async def cb_gift_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        index = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    codes = await features.get_giftcodes()
    if not 0 <= index < len(codes):
        await callback.answer("Code not found", show_alert=True)
        return
    removed = codes.pop(index)
    await features.save_giftcodes(codes)
    await features.audit(f"Gift code {removed.get('code')} deleted")
    await callback.answer("Deleted")
    await _show_gifts(callback.message)


# ---------------------------------------------------------------- reviews


def _reviews_text(reviews: list[dict]) -> str:
    pending = sum(1 for r in reviews if not r.get("approved"))
    if not reviews:
        return (
            "⭐ <b>Reviews</b>\n\n"
            "No reviews yet. Users rate stuffs from the stuff page."
        )
    lines = [
        f"⭐ <b>Reviews</b> — {len(reviews)} total, {pending} pending\n"
    ]
    for review in reviews[:8]:
        stars = max(1, min(5, int(review.get("stars", 5))))
        mark = "pending" if not review.get("approved") else "live"
        body = html.escape(str(review.get("text") or ""))[:60]
        lines.append(f"• {'★' * stars} ({mark}) {body}")
    return "\n".join(lines)


async def _show_reviews(target: Message) -> None:
    reviews = await features.get_reviews()
    ordered = sorted(reviews, key=lambda r: bool(r.get("approved")))
    await _edit_or_send(target, _reviews_text(ordered), reviews_kb(ordered))


@router.callback_query(F.data.startswith("rvap:"))
async def cb_review_approve(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    review_id = callback.data.split(":", 1)[1]
    reviews = await features.get_reviews()
    for review in reviews:
        if str(review.get("id")) == review_id:
            review["approved"] = True
            break
    else:
        await callback.answer("Review not found", show_alert=True)
        return
    await features.save_reviews(reviews)
    await features.audit(f"Review {review_id} approved")
    await callback.answer("Approved")
    await _show_reviews(callback.message)


@router.callback_query(F.data.startswith("rvdel:"))
async def cb_review_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    review_id = callback.data.split(":", 1)[1]
    reviews = [r for r in await features.get_reviews() if str(r.get("id")) != review_id]
    await features.save_reviews(reviews)
    await features.audit(f"Review {review_id} deleted")
    await callback.answer("Deleted")
    await _show_reviews(callback.message)


@router.callback_query(F.data.startswith("rvnone:"))
async def cb_review_none(callback: CallbackQuery) -> None:
    review_id = callback.data.split(":", 1)[1]
    for review in await features.get_reviews():
        if str(review.get("id")) == review_id:
            text = str(review.get("text") or "(no text)")[:180]
            await callback.answer(text, show_alert=True)
            return
    await callback.answer("Review not found", show_alert=True)


# ---------------------------------------------------------------- support


def _tickets_text(tickets: list[dict], sla: bool = False) -> str:
    open_count = sum(1 for t in tickets if t.get("status") == "open")
    if not tickets:
        return (
            "💬 <b>Support</b>\n\n"
            "No tickets yet. Users open tickets from the Support button."
        )
    lines = [f"💬 <b>Support</b> — {len(tickets)} ticket(s), {open_count} open\n"]
    for ticket in tickets[:8]:
        status = ticket.get("status", "open")
        name = html.escape(str(ticket.get("user_name", "?")))[:24]
        body = html.escape(str(ticket.get("text", "")))[:50]
        suffix = ""
        if sla and status == "open":
            age = _age_hours(ticket.get("at"))
            prio = _prio_for(age)
            suffix = f" ⏱{age:.0f}h{prio}"
        lines.append(f"• {name} [{status}]{suffix}: {body}")
    if sla:
        lines.append("\n🔴 older than 24h · 🟡 older than 6h")
    return "\n".join(lines)


async def _show_support(target: Message) -> None:
    tickets = await features.get_tickets()
    sla = await features.is_on("sla")
    if sla:
        ordered = sorted(
            tickets,
            key=lambda t: (
                t.get("status") != "open",
                -_age_hours(t.get("at")) if t.get("status") == "open" else 0,
            ),
        )
        for ticket in ordered:
            if ticket.get("status") == "open":
                ticket["_prio"] = _prio_for(_age_hours(ticket.get("at")))
    else:
        ordered = sorted(tickets, key=lambda t: t.get("status") != "open")
    canned = await features.get_canned() if sla else None
    await _edit_or_send(
        target, _tickets_text(ordered, sla=sla), support_kb(ordered, canned=canned)
    )


@router.callback_query(F.data == "tnone")
async def cb_ticket_none(callback: CallbackQuery) -> None:
    await callback.answer("No tickets yet")


@router.callback_query(F.data.startswith("tview:"))
async def cb_ticket_view(callback: CallbackQuery) -> None:
    ticket_id = callback.data.split(":", 1)[1]
    for ticket in await features.get_tickets():
        if str(ticket.get("id")) == ticket_id:
            await callback.answer(
                str(ticket.get("text") or "")[:180], show_alert=True
            )
            return
    await callback.answer("Ticket not found", show_alert=True)


@router.callback_query(F.data.startswith("treply:"))
async def cb_ticket_reply_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    ticket_id = callback.data.split(":", 1)[1]
    await state.set_state(AdminStates.ticket_reply)
    await state.update_data(ticket_id=ticket_id)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "✍️ <b>Reply to ticket</b>\n\n"
        "Send the reply text (HTML supported). "
        "It is sent to the user as a message from the bot.",
        cancel_inline_kb("apnl:support"),
    )


@router.message(AdminStates.ticket_reply, F.text)
async def ticket_reply_send(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    ticket_id = data.get("ticket_id")
    tickets = await features.get_tickets()
    ticket = next(
        (t for t in tickets if str(t.get("id")) == str(ticket_id)), None
    )
    if ticket is None:
        await state.clear()
        await message.answer("Ticket not found.", reply_markup=panel_inline_kb())
        return
    try:
        await message.bot.send_message(
            int(ticket.get("user_tg_id")),
            f"📬 <b>Support reply</b>\n\n{message.text or ''}",
        )
        sent = True
    except TelegramAPIError:
        logger.exception("Ticket reply failed")
        sent = False
    await features.audit(
        f"Ticket reply to {ticket.get('user_name')} ({'sent' if sent else 'FAILED'})"
    )
    await state.clear()
    await message.answer(
        "✅ Reply sent." if sent else "❌ Could not message that user.",
        reply_markup=panel_inline_kb(),
    )


@router.callback_query(F.data.startswith("tclose:"))
async def cb_ticket_close(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    ticket_id = callback.data.split(":", 1)[1]
    tickets = await features.get_tickets()
    found = False
    for ticket in tickets:
        if str(ticket.get("id")) == ticket_id:
            ticket["status"] = "done"
            found = True
    if not found:
        await callback.answer("Ticket not found", show_alert=True)
        return
    await features.save_tickets(tickets)
    await features.audit(f"Ticket {ticket_id} closed")
    await callback.answer("Closed")
    await _show_support(callback.message)


# --------------------------------------------------------------- schedule


def _sched_text(entries: list[dict]) -> str:
    if not entries:
        return (
            "⏰ <b>Scheduled broadcasts</b>\n\n"
            "Nothing scheduled yet.\n"
            "Use Schedule broadcast to send a message later "
            "(server local time)."
        )
    lines = ["⏰ <b>Scheduled broadcasts</b>\n"]
    for entry in entries[:10]:
        when = str(entry.get("at", "?"))[:16].replace("T", " ")
        body = html.escape(str(entry.get("text") or ""))[:40]
        lines.append(f"• {when} — {body}")
    return "\n".join(lines)


async def _show_schedule(target: Message) -> None:
    entries = await features.get_schedule()
    await _edit_or_send(target, _sched_text(entries), schedule_kb(entries))


@router.callback_query(F.data == "scnone")
async def cb_sched_none(callback: CallbackQuery) -> None:
    await callback.answer("Nothing here")


@router.callback_query(F.data == "scadd")
async def cb_sched_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.schedule_text)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "✍️ <b>Schedule broadcast</b>\n\n"
        "Send the message text (HTML supported).",
        cancel_inline_kb("apnl:sched"),
    )


@router.message(AdminStates.schedule_text, F.text)
async def sched_text_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    await state.update_data(text=message.text or "")
    await state.set_state(AdminStates.schedule_time)
    await message.answer(
        "When should it be sent?\n\n"
        "Either <code>HH:MM</code> (today, or tomorrow if already past)\n"
        "or <code>YYYY-MM-DD HH:MM</code> (server local time):"
    )


@router.message(AdminStates.schedule_time, F.text)
async def sched_time_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    local_now = datetime.now().astimezone()
    moment: datetime | None = None
    try:
        if features.valid_hhmm(raw) and " " not in raw:
            hour, minute = (int(part) for part in raw.split(":"))
            candidate = local_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= local_now:
                candidate = candidate + timedelta(days=1)
            moment = candidate
        else:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M")
            moment = parsed.replace(
                tzinfo=local_now.tzinfo
            )
    except ValueError:
        moment = None
    if moment is None:
        await message.answer(
            "Could not read that time. Send HH:MM or YYYY-MM-DD HH:MM:"
        )
        return
    data = await state.get_data()
    entries = await features.get_schedule()
    entries.append(
        {
            "id": uuid.uuid4().hex[:8],
            "at": moment.astimezone().isoformat(),
            "chat_id": message.chat.id,
            "text": str(data.get("text") or "")[:4096],
        }
    )
    await features.save_schedule(entries)
    await features.audit(f"Broadcast scheduled for {moment:%Y-%m-%d %H:%M}")
    await state.clear()
    await message.answer(
        f"✅ Scheduled for {moment:%d %b %Y %H:%M} (server time).",
        reply_markup=panel_inline_kb(),
    )


@router.callback_query(F.data.startswith("scdel:"))
async def cb_sched_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    entry_id = callback.data.split(":", 1)[1]
    entries = await features.get_schedule()
    remaining = [e for e in entries if str(e.get("id")) != entry_id]
    await features.save_schedule(remaining)
    await callback.answer("Deleted")
    await _show_schedule(callback.message)


# -------------------------------------------------------------------- faq


def _faq_text(pairs: list[dict]) -> str:
    if not pairs:
        return (
            "❓ <b>FAQ</b>\n\n"
            "No questions yet. Users' unknown texts are matched "
            "against the FAQ when the feature is ON."
        )
    lines = ["❓ <b>FAQ</b>\n"]
    for pair in pairs[:10]:
        lines.append(
            f"• <b>{html.escape(str(pair.get('q', '')))[:60]}</b> → "
            f"{html.escape(str(pair.get('a', '')))[:60]}"
        )
    return "\n".join(lines)


async def _show_faq(target: Message) -> None:
    pairs = await features.get_faq()
    await _edit_or_send(target, _faq_text(pairs), faq_kb(pairs))


@router.callback_query(F.data == "faqnone")
async def cb_faq_none(callback: CallbackQuery) -> None:
    await callback.answer("Press Add question to create one")


@router.callback_query(F.data == "faqadd")
async def cb_faq_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.set_state(AdminStates.faq_question)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "❓ <b>Add FAQ</b>\n\n"
        "Send the question — the text a user might type:",
        cancel_inline_kb("apnl:faq"),
    )


@router.message(AdminStates.faq_question, F.text)
async def faq_question_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    question = (message.text or "").strip()
    if len(question) < 2:
        await message.answer("Question is too short")
        return
    await state.update_data(q=question)
    await state.set_state(AdminStates.faq_answer)
    await message.answer("Now send the answer (HTML supported):")


@router.message(AdminStates.faq_answer, F.text)
async def faq_answer_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    pairs = await features.get_faq()
    pairs.append({"q": data.get("q", ""), "a": message.text or ""})
    await features.save_faq(pairs)
    await features.audit(f"FAQ added: {data.get('q')}")
    await state.clear()
    await message.answer("✅ FAQ added.", reply_markup=panel_inline_kb())
    await _show_faq(message)


@router.callback_query(F.data.startswith("faqdel:"))
async def cb_faq_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        index = int(callback.data.split(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer()
        return
    pairs = await features.get_faq()
    if not 0 <= index < len(pairs):
        await callback.answer("Not found", show_alert=True)
        return
    removed = pairs.pop(index)
    await features.save_faq(pairs)
    await features.audit(f"FAQ deleted: {removed.get('q')}")
    await callback.answer("Deleted")
    await _show_faq(callback.message)


# ------------------------------------------------------------------ audit


async def _show_audit(target: Message) -> None:
    entries = await features.get_audit()
    if not entries:
        text = "🧾 <b>Audit log</b>\n\nNo entries yet."
    else:
        lines = ["🧾 <b>Audit log</b>\n"]
        for entry in entries[:25]:
            moment = str(entry.get("at", ""))[:16].replace("T", " ")
            body = html.escape(str(entry.get("text", "")))[:80]
            lines.append(f"• {moment} {body}")
        text = "\n".join(lines)
    await _edit_or_send(
        target, text, cancel_inline_kb("apnl:features")
    )


# ---------------------------------------------------------------- category


@router.callback_query(F.data.startswith("scat:"))
async def cb_category_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff = await get_stuff(callback.data.split(":", 1)[1])
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    await state.set_state(AdminStates.set_category)
    await state.update_data(stuff_id=str(stuff["_id"]))
    await callback.answer()
    current = stuff.get("category") or "—"
    await _edit_or_send(
        callback.message,
        f"📁 <b>Category</b> for <b>{html.escape(stuff.get('name', '?'))}</b>\n\n"
        f"Current: {html.escape(str(current))}\n\n"
        "Send the category name (e.g. <code>Maths</code>), "
        "or <code>none</code> to remove it:",
        cancel_inline_kb(f"sadm:{stuff['_id']}"),
    )


@router.message(AdminStates.set_category, F.text)
async def category_set(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    stuff_id = data.get("stuff_id")
    raw = (message.text or "").strip()
    value = "" if raw.lower() in ("none", "clear", "off", "-") else raw[:40]
    await set_stuff_category(stuff_id, value)
    await features.audit(
        f"Stuff {stuff_id} category set to {value or 'none'}"
    )
    await state.clear()
    stuff = await get_stuff(stuff_id)
    if stuff is None:
        await message.answer("Stuffs deleted.", reply_markup=panel_inline_kb())
        return
    await message.answer(
        f"✅ Category set to <b>{html.escape(value or '—')}</b>",
    )
    await _edit_or_send(
        message, await _stuff_view_text(stuff),
        await _stuff_kb_for(stuff)
    )


# ---------------------------------------------------------------- validity


@router.callback_query(F.data.startswith("sdays:"))
async def cb_validity_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff = await get_stuff(callback.data.split(":", 1)[1])
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    days = await features.get_stuff_days(stuff["_id"])
    await state.set_state(AdminStates.set_days)
    await state.update_data(stuff_id=str(stuff["_id"]))
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"⏱ <b>Access validity</b> for "
        f"<b>{html.escape(stuff.get('name', '?'))}</b>\n\n"
        f"Current: {_validity_label(days)}\n\n"
        "Buyers get access for this many days after purchase.\n"
        "Send the number of days (1–365), or <code>0</code> for forever:",
        cancel_inline_kb(f"sadm:{stuff['_id']}"),
    )


@router.message(AdminStates.set_days, F.text)
async def validity_set(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        days = int((message.text or "").strip())
    except ValueError:
        await message.answer("Send a whole number, e.g. 30 (or 0)")
        return
    if not 0 <= days <= 365:
        await message.answer("Days must be between 0 and 365")
        return
    data = await state.get_data()
    stuff_id = data.get("stuff_id")
    await features.set_stuff_days(stuff_id, days)
    await features.audit(f"Stuff {stuff_id} validity set to {_validity_label(days)}")
    await state.clear()
    stuff = await get_stuff(stuff_id)
    if stuff is None:
        await message.answer("Stuffs deleted.", reply_markup=panel_inline_kb())
        return
    await message.answer(
        f"✅ Validity set to <b>{_validity_label(days)}</b>\n\n"
        "Note: the Access validity feature must be ON for new purchases "
        "to get a time limit.",
    )
    await _edit_or_send(
        message, await _stuff_view_text(stuff),
        await _stuff_kb_for(stuff)
    )


@router.callback_query(F.data == "aback")
async def cb_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await _edit_or_send(callback.message, panel_text(), panel_inline_kb())


# ---------------------------------------------------------------- batch 3: dashboard


def _event_time(value) -> datetime | None:
    moment = features.parse_expiry(value)
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Refresh", "bdash", "primary"), ib("Pending", "apnl:pending", "success")],
            [ib("Analytics", "apnl:analytics", "primary"), ib("Panel", "aback", "primary")],
        ]
    )


async def _dashboard_text() -> str:
    from datetime import timezone as _tz

    now = datetime.now(_tz.utc)
    today = now.date()
    requests = await list_requests()
    approved = [r for r in requests if str(r.get("status")) == "approved"]
    pending = [r for r in requests if str(r.get("status")) == "pending"]

    def _rev(rows: list[dict]) -> float:
        return sum(float(r.get("amount", 0) or 0) for r in rows)

    today_rows = [r for r in approved if (_event_time(r.get("created_at")) or now).date() == today]
    users = await count_users()
    new_today = await count_users_created_after(today.isoformat())
    buyers = {int(r.get("user_tg_id") or 0) for r in approved if r.get("user_tg_id")}
    conv = (len(buyers) / users * 100) if users else 0.0

    text = (
        "?? <b>Dashboard</b>\n"
        "----------------------\n"
        f"?? Revenue today: <b>Rs.{_rev(today_rows):g}</b> "
        f"({len(today_rows)} order(s))\n"
        f"?? Revenue total: <b>Rs.{_rev(approved):g}</b> ({len(approved)} orders)\n"
        f"? Pending claims: <b>{len(pending)}</b> (Rs.{_rev(pending):g})\n"
        f"?? Users: <b>{users}</b> (+{new_today} today)\n"
        f"?? Buyers: <b>{len(buyers)}</b> (conversion {conv:.1f}%)\n"
    )

    events: list[tuple[datetime, str]] = []
    for request in requests:
        moment = _event_time(request.get("created_at"))
        if moment is None:
            continue
        status = str(request.get("status", "?"))
        mark = {"approved": "?", "pending": "?", "rejected": "?"}.get(status, "�")
        events.append(
            (
                moment,
                f"{mark} Rs.{float(request.get('amount', 0) or 0):g} � "
                f"{html.escape(str(request.get('stuff_name') or '?'))[:40]} "
                f"(user {request.get('user_tg_id')})",
            )
        )
    if await features.is_on("support"):
        for ticket in await features.get_tickets():
            if ticket.get("status") != "open":
                continue
            moment = _event_time(ticket.get("at"))
            if moment is None:
                continue
            events.append(
                (moment, f"?? Ticket from {html.escape(str(ticket.get('user_name', '?')))[:24]}")
            )
    if await features.is_on("audit"):
        for entry in (await features.get_audit())[:6]:
            moment = _event_time(entry.get("at"))
            if moment is None:
                continue
            events.append((moment, f"?? {html.escape(str(entry.get('text', '')))[:70]}"))

    events.sort(key=lambda e: e[0], reverse=True)
    text += "----------------------\n<b>Activity feed</b>\n"
    if not events:
        text += "No recent activity."
    for moment, line in events[:10]:
        text += f"\n� {moment:%H:%M} {line}"
    return text


@router.callback_query(F.data == "bdash")
async def cb_dashboard_refresh(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("Refreshed")
    if not await features.is_on("dash"):
        await _edit_or_send(callback.message, _off_notice("Dashboard"), _back_panel_kb())
        return
    await _edit_or_send(callback.message, await _dashboard_text(), _dashboard_kb())


# ------------------------------------------------------------- pending inbox


def _pending_summary(request: dict) -> str:
    moment = _event_time(request.get("created_at"))
    when = f"{moment:%d %b %H:%M}" if moment else "?"
    method = request.get("method_name") or request.get("method") or "?"
    if isinstance(method, dict):
        method = method.get("name", "?")
    lines = [
        f"?? <b>Pending claim {request.get('_id')}</b>",
        "----------------------",
        f"Amount: <b>Rs.{float(request.get('amount', 0) or 0):g}</b>",
        f"Stuffs: {html.escape(str(request.get('stuff_name') or '?'))}",
        f"User: <code>{request.get('user_tg_id')}</code> "
        f"@{html.escape(str(request.get('user_username') or '?'))}",
        f"Method: {html.escape(str(method))}",
        f"Created: {when}",
    ]
    if request.get("coupon"):
        lines.append(f"Coupon: {html.escape(str(request.get('coupon')))}")
    return "\n".join(lines)


def _pending_kb(requests: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for request in requests:
        amount = float(request.get("amount", 0) or 0)
        label = (
            f"Rs.{amount:g} � {str(request.get('stuff_name') or '?')[:26]} "
            f"� {request.get('user_tg_id')}"
        )
        rows.append([ib(label[:64], f"pin:{request.get('_id')}", "primary")])
    if not requests:
        rows.append([ib("No pending claims", "pnone", "primary")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _pending_rows() -> list[dict]:
    rows = [r for r in await list_requests() if str(r.get("status")) == "pending"]
    rows.sort(
        key=lambda r: _event_time(r.get("created_at")) or datetime.min.replace(
            tzinfo=timezone.utc
        ),
        reverse=True,
    )
    return rows


async def _show_pending(target: Message) -> None:
    rows = await _pending_rows()
    total = len(rows)
    shown = rows[:15]
    text = f"?? <b>Pending inbox</b> � {total} waiting\n"
    if not shown:
        text += "\nAll payment claims are handled."
    else:
        text += "\nTap a claim to review and approve or reject it."
    await _edit_or_send(target, text, _pending_kb(shown))


@router.callback_query(F.data == "pnone")
async def cb_pending_none(callback: CallbackQuery) -> None:
    await callback.answer("Nothing pending")


@router.callback_query(F.data.startswith("pin:"))
async def cb_pending_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    request_id = callback.data.split(":", 1)[1]
    request = await get_payment_request(request_id)
    if request is None:
        await callback.answer("Request not found", show_alert=True)
        return
    kb_rows = [
        [
            ib("Approve & unlock", f"payok:{request_id}", "success"),
            ib("Reject", f"payno:{request_id}", "danger"),
        ],
        [ib("Back to inbox", "apnl:pending", "primary")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    screenshot = request.get("screenshot_file_id")
    text = _pending_summary(request)
    await callback.answer()
    if screenshot:
        try:
            await callback.message.answer_photo(
                screenshot, caption=text[:1024], reply_markup=markup
            )
            return
        except TelegramAPIError:
            logger.exception("Pending screenshot send failed")
    await callback.message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- analytics


def _analytics_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Refresh", "banalytics", "primary"), ib("Graph page", "bstats", "primary")],
            [ib("Panel", "aback", "primary")],
        ]
    )


async def _analytics_text() -> str:
    from datetime import timezone as _tz

    now = datetime.now(_tz.utc)
    requests = await list_requests()
    approved = [r for r in requests if str(r.get("status")) == "approved"]
    rejected = [r for r in requests if str(r.get("status")) in {"rejected", "failed"}]
    pending = [r for r in requests if str(r.get("status")) == "pending"]
    users = await count_users()
    buyers = {int(r.get("user_tg_id") or 0) for r in approved if r.get("user_tg_id")}

    def _amount(r) -> float:
        try:
            return float(r.get("amount", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    revenue = sum(_amount(r) for r in approved)

    def _since(days: int) -> float:
        cutoff = now - timedelta(days=days)
        total = 0.0
        for request in approved:
            moment = _event_time(request.get("created_at"))
            if moment is not None and moment >= cutoff:
                total += _amount(request)
        return total

    conv = (len(buyers) / users * 100) if users else 0.0
    avg = (revenue / len(approved)) if approved else 0.0
    text = (
        "?? <b>Analytics</b>\n"
        "----------------------\n"
        f"?? Users: <b>{users}</b>\n"
        f"?? Buyers: <b>{len(buyers)}</b> � conversion <b>{conv:.1f}%</b>\n"
        f"?? Orders: approved {len(approved)} / pending {len(pending)} / "
        f"rejected {len(rejected)}\n"
        f"?? Revenue: <b>Rs.{revenue:g}</b> � avg order Rs.{avg:g}\n"
        "----------------------\n"
        f"Today: <b>Rs.{_since(0):g}</b> � 7 days: <b>Rs.{_since(7):g}</b> � "
        f"30 days: <b>Rs.{_since(30):g}</b>\n"
    )

    per_stuff: dict[str, list[float]] = {}
    for request in approved:
        raw = str(request.get("stuff_id") or "")
        for sid in (p.strip() for p in raw.split(",") if p.strip()):
            bucket = per_stuff.setdefault(sid, [0.0, 0.0])
            bucket[0] += 1
            bucket[1] += _amount(request) / max(1, len([p for p in raw.split(",") if p.strip()]))
    stuffs = {str(s["_id"]): s for s in await list_stuffs(include_archived=True)}
    ranked = sorted(per_stuff.items(), key=lambda kv: kv[1][1], reverse=True)[:5]
    if ranked:
        text += "\n<b>Best sellers</b>\n"
        for sid, (count, amount) in ranked:
            name = str((stuffs.get(sid) or {}).get("name") or sid)
            text += f"� {html.escape(name)[:32]} � {int(count)} sold, Rs.{amount:g}\n"

    coupons = await features.get_coupons()
    if coupons:
        per_coupon: dict[str, list[float]] = {}
        for request in approved:
            code = str(request.get("coupon") or "").upper()
            if not code:
                continue
            bucket = per_coupon.setdefault(code, [0.0, 0.0])
            bucket[0] += 1
            bucket[1] += _amount(request)
        lines = []
        for coupon in coupons[:8]:
            code = str(coupon.get("code", "?")).upper()
            count, amount = per_coupon.get(code, [0.0, 0.0])
            if count <= 0:
                continue
            lines.append(
                f"� {html.escape(code)} � {int(count)} use(s), Rs.{amount:g}"
            )
        if lines:
            text += "\n<b>Coupon performance</b>\n" + "\n".join(lines)

    graph = await _graph_text(requests)
    if graph:
        text += "\n" + graph
    return text


async def _show_analytics(target: Message) -> None:
    await _edit_or_send(target, await _analytics_text(), _analytics_kb())


@router.callback_query(F.data == "banalytics")
async def cb_analytics_refresh(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.answer("Refreshed")
    if not await features.is_on("analytics"):
        await _edit_or_send(callback.message, _off_notice("Analytics"), _back_panel_kb())
        return
    await _edit_or_send(callback.message, await _analytics_text(), _analytics_kb())


# -------------------------------------------------------------- global search


def _search_text(
    query: str,
    users: list[dict],
    stuffs: list[dict],
    orders: list[dict],
    coupons: list[dict],
    clones: list[dict],
) -> str:
    total = len(users) + len(stuffs) + len(orders) + len(coupons) + len(clones)
    lines = [
        f"?? <b>Search: {html.escape(query)}</b> � {total} result(s)",
        "----------------------",
    ]
    for user in users[:5]:
        name = html.escape(str(user.get("full_name") or "?"))[:28]
        username = user.get("username")
        lines.append(
            f"?? {name} ({('@' + html.escape(username)) if username else 'no @'}) "
            f"� <code>{user.get('tg_id')}</code>"
        )
    for stuff in stuffs[:5]:
        price = float(stuff.get("price", 0) or 0)
        lines.append(
            f"?? {html.escape(str(stuff.get('name', '?')))[:40]} � Rs.{price:g}"
        )
    for order in orders[:5]:
        lines.append(
            f"?? Order {order.get('_id')} � Rs.{float(order.get('amount', 0) or 0):g} "
            f"� {order.get('status')} � user {order.get('user_tg_id')}"
        )
    for coupon in coupons[:5]:
        lines.append(
            f"?? {html.escape(str(coupon.get('code', '?')).upper())} � "
            f"{coupon.get('percent')}% off"
        )
    for entry in clones[:5]:
        lines.append(
            f"?? clone {html.escape(str(entry.get('username') or entry.get('tag')))}"
        )
    if total == 0:
        lines.append("Nothing matched.")
    return "\n".join(lines)


def _search_kb(
    users: list[dict],
    stuffs: list[dict],
    has_coupons: bool,
    clones: list[dict],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for user in users[:5]:
        uid = user.get("tg_id")
        name = html.escape(str(user.get("full_name") or uid))[:30]
        rows.append([ib(f"?? {name}"[:64], f"ucard:{uid}", "primary")])
    for stuff in stuffs[:5]:
        rows.append(
            [ib(f"?? {str(stuff.get('name', '?'))[:40]}"[:64], f"sadm:{stuff['_id']}", "primary")]
        )
    for entry in clones[:5]:
        rows.append(
            [ib(
                f"?? {str(entry.get('username') or entry.get('tag'))[:40]}",
                f"clinfo:{entry.get('tag')}",
                "primary",
            )]
        )
    if has_coupons:
        rows.append([ib("Open coupons", "apnl:coupons", "primary")])
    rows.append([ib("Search again", "apnl:search", "success")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(AdminStates.search_q, F.text)
async def admin_search_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Send at least 2 characters:")
        return
    await state.clear()
    needle = query.lower()
    stuffs = [
        s
        for s in await list_stuffs(include_archived=True)
        if needle in str(s.get("name", "")).lower()
    ][:6]
    users = await search_users(query, 5)
    orders = [r for r in await list_requests() if query.lower() in str(r.get("_id"))][
        :5
    ]
    coupons = [
        c for c in await features.get_coupons() if needle in str(c.get("code", "")).lower()
    ][:5]
    clones = [
        entry
        for entry in await clone_manager.load_registry()
        if needle in str(entry.get("username", "")).lower()
        or needle in str(entry.get("tag", "")).lower()
    ][:5]
    await message.answer(
        _search_text(query, users, stuffs, orders, coupons, clones),
        reply_markup=_search_kb(users, stuffs, bool(coupons), clones),
    )


# ---------------------------------------------------------------- offer timers


def _timers_kb(entries: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in entries[:20]:
        rows.append([ib(features.timer_line(entry)[:64], f"tmdel:{entry.get('id')}", "danger")])
    if not entries:
        rows.append([ib("No timers yet", "tnone2", "primary")])
    rows.append([ib("Add timer", "tadd", "success")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_timers(target: Message) -> None:
    entries = await features.get_timers()
    if not entries:
        text = (
            "? <b>Offer timers</b>\n\n"
            "Schedule automatic feature toggles and price changes "
            "(flash sales: set a sale price now, restore the old price later).\n\n"
            "No timers scheduled."
        )
    else:
        lines = [f"? <b>Offer timers</b> � {len(entries)} scheduled\n"]
        for entry in entries[:20]:
            lines.append(f"� {html.escape(features.timer_line(entry))}")
        text = "\n".join(lines)
    await _edit_or_send(target, text, _timers_kb(entries))


def _when_prompt(kind: str) -> str:
    return (
        f"?? <b>Schedule {kind}</b>\n\n"
        "Send the server-local time:\n"
        "� <code>HH:MM</code> � today, or tomorrow if already past\n"
        "� <code>YYYY-MM-DD HH:MM</code> � exact date and time"
    )


def _parse_when(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    local_now = datetime.now().astimezone()
    try:
        if features.valid_hhmm(raw) and " " not in raw:
            hour, minute = (int(part) for part in raw.split(":"))
            candidate = local_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= local_now:
                candidate = candidate + timedelta(days=1)
            return candidate
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        return parsed.replace(tzinfo=local_now.tzinfo)
    except ValueError:
        return None


@router.callback_query(F.data == "tnone2")
async def cb_timers_none(callback: CallbackQuery) -> None:
    await callback.answer("No timers scheduled")


@router.callback_query(F.data == "tadd")
async def cb_timer_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("offer"):
        await callback.answer("Offer timers are turned off", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "? <b>Add timer</b>\n\nChoose what to schedule:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Feature on/off", "tmtype:feat", "primary")],
                [ib("Price change (flash sale)", "tmtype:price", "primary")],
                [ib("Back", "apnl:timers", "primary")],
            ]
        ),
    )


@router.callback_query(F.data == "tmtype:feat")
async def cb_timer_type_feature(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows: list[list[InlineKeyboardButton]] = []
    bucket: list[InlineKeyboardButton] = []
    for key, (label, _desc) in features.FEATURES.items():
        bucket.append(ib(label[:30], f"tmf:{key}", "primary"))
        if len(bucket) == 2:
            rows.append(bucket)
            bucket = []
    if bucket:
        rows.append(bucket)
    rows.append([ib("Back", "apnl:timers", "primary")])
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "?? <b>Pick the feature</b> to toggle automatically:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "tmtype:price")
async def cb_timer_type_price(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    items = await list_stuffs(include_archived=True)
    if not items:
        await callback.answer("Add a stuff first", show_alert=True)
        return
    rows = [
        [ib(f"{s.get('name')} � Rs.{float(s.get('price', 0) or 0):g}"[:64], f"tmp:{s['_id']}", "primary")]
        for s in items[:30]
    ]
    rows.append([ib("Back", "apnl:timers", "primary")])
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "?? <b>Pick the stuff</b> whose price changes on schedule:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("tmf:"))
async def cb_timer_feature_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    key = callback.data.split(":", 1)[1]
    if key not in features.FEATURES:
        await callback.answer("Unknown feature", show_alert=True)
        return
    label = features.FEATURES[key][0]
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>{label}</b>\n\nChoose the new state:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Turn ON", f"tmv:{key}:1", "success"), ib("Turn OFF", f"tmv:{key}:0", "danger")],
                [ib("Back", "apnl:timers", "primary")],
            ]
        ),
    )
    await state.clear()
    await state.update_data(tm_kind="feature", tm_target=key)


@router.callback_query(F.data.startswith("tmv:"))
async def cb_timer_feature_value(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid", show_alert=True)
        return
    _, key, flag = parts
    await state.update_data(tm_value=flag == "1", tm_phase="")
    await state.set_state(AdminStates.timer_time)
    label = features.FEATURES.get(key, (key, ""))[0]
    state_text = "ON" if flag == "1" else "OFF"
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"? <b>{label} ? {state_text}</b>\n\n" + _when_prompt("feature toggle"),
        cancel_inline_kb("apnl:timers"),
    )


@router.callback_query(F.data.startswith("tmp:"))
async def cb_timer_stuff_pick(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        tm_kind="price",
        tm_target=str(doc["_id"]),
        tm_restore_value=float(doc.get("price", 0) or 0),
        tm_name=str(doc.get("name", "?")),
        tm_phase="",
    )
    await state.set_state(AdminStates.timer_price)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>Price timer</b> for <b>{html.escape(doc.get('name', '?'))}</b>\n\n"
        f"Current price: Rs.{float(doc.get('price', 0) or 0):g}\n\n"
        "Send the <b>new price</b> that applies at the scheduled time:",
        cancel_inline_kb("apnl:timers"),
    )


@router.message(AdminStates.timer_price, F.text)
async def timer_price_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().replace(",", ".")
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    try:
        price = float(digits) if digits else 0.0
    except ValueError:
        price = 0.0
    if price < 0:
        await message.answer("Send a valid price, e.g. <code>99</code>:")
        return
    await state.update_data(tm_value=price, tm_phase="")
    await state.set_state(AdminStates.timer_time)
    await message.answer(
        "?? <b>Price timer</b>\n\n"
        f"New price: Rs.{price:g}\n\n" + _when_prompt("price change"),
        reply_markup=cancel_inline_kb("apnl:timers"),
    )


@router.message(AdminStates.timer_time, F.text)
async def timer_time_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    moment = _parse_when(message.text or "")
    if moment is None:
        await message.answer(
            "Could not read that time. Send HH:MM or YYYY-MM-DD HH:MM:"
        )
        return
    data = await state.get_data()
    kind = str(data.get("tm_kind") or "")
    phase = str(data.get("tm_phase") or "")
    if kind == "feature":
        await features.add_timer(
            "feature",
            data.get("tm_target"),
            bool(data.get("tm_value")),
            moment.astimezone().isoformat(),
            note=str(data.get("tm_note") or ""),
        )
        await features.audit(
            f"Timer set: {data.get('tm_target')} "
            f"{'ON' if data.get('tm_value') else 'OFF'} at {moment:%d %b %H:%M}"
        )
        await state.clear()
        await message.answer(
            f"? Timer scheduled for {moment:%d %b %Y %H:%M} (server time).",
            reply_markup=panel_inline_kb(),
        )
        await _show_timers(message)
        return
    if kind != "price":
        await state.clear()
        await message.answer("Session expired, start again.", reply_markup=panel_inline_kb())
        return
    if phase == "restore":
        restore_at = moment
        main_at = features.parse_expiry(data.get("tm_at"))
        if main_at is None:
            await state.clear()
            await message.answer("Session expired, start again.", reply_markup=panel_inline_kb())
            return
        if main_at.tzinfo is None:
            main_at = main_at.replace(tzinfo=main_at.astimezone().tzinfo)
        if restore_at <= main_at:
            await message.answer(
                "Restore time must be after the sale time. Send a later HH:MM or date:"
            )
            return
        await features.add_timer(
            "price",
            data.get("tm_target"),
            float(data.get("tm_value") or 0),
            data.get("tm_at"),
            note=f"{data.get('tm_name', 'stuff')} sale price",
        )
        await features.add_timer(
            "price",
            data.get("tm_target"),
            float(data.get("tm_restore_value") or 0),
            restore_at.astimezone().isoformat(),
            note=f"{data.get('tm_name', 'stuff')} restore",
        )
        await features.audit(
            f"Flash sale timer: stuff {data.get('tm_target')} ? "
            f"Rs.{float(data.get('tm_value') or 0):g} at {main_at:%d %b %H:%M}, "
            f"restore Rs.{float(data.get('tm_restore_value') or 0):g}"
        )
        await state.clear()
        await message.answer(
            f"? Flash sale scheduled: price change {main_at:%d %b %H:%M}, "
            f"restore {restore_at:%d %b %H:%M} (server time).",
            reply_markup=panel_inline_kb(),
        )
        await _show_timers(message)
        return
    await state.update_data(tm_at=moment.astimezone().isoformat(), tm_phase="wait")
    await state.set_state(AdminStates.timer_time)
    await message.answer(
        f"?? Sale starts {moment:%d %b %H:%M}.\n\n"
        "Now schedule when the <b>old price comes back</b> "
        "(restore time), or press Skip to keep the new price forever:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Set restore time", "tmrestore", "primary")],
                [ib("Skip (keep new price)", "tmskip", "success")],
                [ib("Cancel", "apnl:timers", "danger")],
            ]
        ),
    )


@router.callback_query(F.data == "tmrestore")
async def cb_timer_restore(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.update_data(tm_phase="restore")
    await state.set_state(AdminStates.timer_time)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "?? <b>Restore time</b>\n\n" + _when_prompt("price restore"),
        cancel_inline_kb("apnl:timers"),
    )


@router.callback_query(F.data == "tmskip")
async def cb_timer_skip_restore(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    at = data.get("tm_at")
    if not at:
        await state.clear()
        await callback.answer("Session expired", show_alert=True)
        return
    await features.add_timer(
        "price",
        data.get("tm_target"),
        float(data.get("tm_value") or 0),
        at,
        note=f"{data.get('tm_name', 'stuff')} price change",
    )
    await features.audit(
        f"Timer set: stuff {data.get('tm_target')} price ? "
        f"Rs.{float(data.get('tm_value') or 0):g}"
    )
    await state.clear()
    await callback.answer("Timer scheduled")
    await _show_timers(callback.message)


@router.callback_query(F.data.startswith("tmdel:"))
async def cb_timer_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    timer_id = callback.data.split(":", 1)[1]
    await features.drop_timer(timer_id)
    await features.audit(f"Timer removed: {timer_id}")
    await callback.answer("Timer removed")
    await _show_timers(callback.message)


# ------------------------------------------------------------- media library


def _media_kb(items: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items[:20]):
        label = f"{index + 1}. {item.get('name', '?')} ({item.get('kind', '?')})"
        rows.append([ib(label[:64], f"mvi:{item.get('id')}", "primary")])
    if not items:
        rows.append([ib("Library is empty", "mnone", "primary")])
    rows.append([ib("Add media", "mad", "success")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_media(target: Message) -> None:
    items = await features.get_media()
    if items:
        text = (
            f"?? <b>Media library</b> � {len(items)} item(s)\n\n"
            "Reusable videos and photos. Tap an item to reuse it in a stuff "
            "or delete it."
        )
    else:
        text = (
            "?? <b>Media library</b>\n\n"
            "Empty. Press Add media and send a video, photo or GIF "
            "(caption becomes the title) to reuse it later in any stuff."
        )
    await _edit_or_send(target, text, _media_kb(items))


@router.callback_query(F.data == "mnone")
async def cb_media_none(callback: CallbackQuery) -> None:
    await callback.answer("Library is empty")


@router.callback_query(F.data == "mad")
async def cb_media_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("medialib"):
        await callback.answer("Media library is turned off", show_alert=True)
        return
    await state.set_state(AdminStates.media_add)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "?? <b>Add media</b>\n\n"
        "Send a <b>video, photo or GIF</b>.\n"
        "Caption becomes the item title (first line).",
        cancel_inline_kb("apnl:media"),
    )


@router.message(AdminStates.media_add, F.photo | F.video | F.animation | F.document)
async def media_add_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    extracted = _extract_media(message)
    if extracted is None and message.sticker:
        await message.answer("Send a video, photo or GIF (not a sticker):")
        return
    if extracted is None:
        await message.answer("Send a video, photo or GIF:")
        return
    kind, file_id, title = extracted
    items = await features.get_media()
    name = title or f"Media {len(items) + 1}"
    await features.add_media(name, kind, file_id)
    await features.audit(f"Media library: added {name} ({kind})")
    await state.clear()
    await message.answer(f"? Added <b>{html.escape(name)}</b> to the library.")
    await _show_media(message)


@router.message(AdminStates.media_add)
async def media_add_invalid(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Send a video, photo or GIF (or press Cancel):")


@router.callback_query(F.data.startswith("mvi:"))
async def cb_media_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    media_id = callback.data.split(":", 1)[1]
    items = await features.get_media()
    item = next((m for m in items if str(m.get("id")) == media_id), None)
    if item is None:
        await callback.answer("Item not found", show_alert=True)
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>{html.escape(str(item.get('name', '?')))}</b>\n"
        "----------------------\n"
        f"Type: {item.get('kind')}\n"
        f"Added: {str(item.get('at', '?'))[:16]}\n\n"
        "Use in a stuff to append it as a lesson, or delete it.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    ib("Use in a stuff", f"muse:{media_id}", "success"),
                    ib("Delete", f"mdel:{media_id}", "danger"),
                ],
                [ib("Back", "apnl:media", "primary")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("mdel:"))
async def cb_media_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    media_id = callback.data.split(":", 1)[1]
    await features.drop_media(media_id)
    await features.audit(f"Media library: removed {media_id}")
    await callback.answer("Deleted")
    await _show_media(callback.message)


@router.callback_query(F.data.startswith("muse:"))
async def cb_media_use(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    media_id = callback.data.split(":", 1)[1]
    items = await features.get_media()
    item = next((m for m in items if str(m.get("id")) == media_id), None)
    if item is None:
        await callback.answer("Item not found", show_alert=True)
        return
    stuffs = await list_stuffs(include_archived=True)
    if not stuffs:
        await callback.answer("Add a stuff first", show_alert=True)
        return
    rows = [
        [ib(str(s.get("name", "?"))[:48], f"musest:{media_id}:{s['_id']}", "primary")]
        for s in stuffs[:25]
    ]
    rows.append([ib("Back", "apnl:media", "primary")])
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? Choose the stuff for <b>{html.escape(str(item.get('name', '?')))}</b>:",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("musest:"))
async def cb_media_use_pick(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid", show_alert=True)
        return
    _, media_id, stuff_id = parts
    items = await features.get_media()
    item = next((m for m in items if str(m.get("id")) == media_id), None)
    doc = await get_stuff(stuff_id)
    if item is None or doc is None:
        await callback.answer("Not found", show_alert=True)
        return
    lesson = {
        "title": str(item.get("name", "Lesson"))[:80],
        "description": "",
        "media_type": str(item.get("kind", "video")),
        "file_id": str(item.get("file_id", "")),
        "is_demo": False,
    }
    await append_lessons(str(doc["_id"]), [lesson])
    await features.audit(
        f"Media library: added {item.get('name')} to stuff {doc['_id']}"
    )
    await callback.answer("Added to stuff")
    await _edit_or_send(
        callback.message,
        await _stuff_view_text(doc),
        admin_stuff_view_kb(
            str(doc["_id"]),
            len(doc.get("lessons") or []),
            show_ctrl=await features.is_on("stuffctrl"),
            archived=bool(doc.get("archived")),
            featured=bool(doc.get("featured")),
        ),
    )


# ------------------------------------------------------------- backup / restore


async def _show_backup(target: Message) -> None:
    from database.store import backend_name

    backend = backend_name()
    hint = (
        "Upload the <b>.db</b> file from a previous backup."
        if backend == "sqlite"
        else "Upload the <b>.json</b> dump from a previous backup."
    )
    text = (
        "?? <b>Backup and restore</b>\n"
        "----------------------\n"
        f"Database: <b>{backend}</b>\n\n"
        "� <b>Backup now</b> sends a fresh copy of the database "
        "(to the log group, or to your private chat).\n"
        "� <b>Restore</b> replaces the database with an uploaded backup.\n\n"
        f"{hint}\n"
        "Restoring wipes current data, so back up first.\n"
        + ("Restored changes apply after a bot restart." if backend == "sqlite" else "")
    )
    await _edit_or_send(
        target,
        text,
        InlineKeyboardMarkup(
            inline_keyboard=[
                [ib("Backup now", "bknow", "success"), ib("Restore", "brest", "danger")],
                [ib("Panel", "aback", "primary")],
            ]
        ),
    )


@router.callback_query(F.data == "bknow")
async def cb_backup_now(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("backup"):
        await callback.answer("Backup is turned off", show_alert=True)
        return
    from utils.scheduler import _backup

    await callback.answer("Creating backup�")
    await _backup(callback.bot, datetime.now(timezone.utc))
    await features.audit("Manual backup created")
    await callback.message.answer(
        "? Backup created � check your log group or private chat.",
        reply_markup=_back_panel_kb(),
    )


@router.callback_query(F.data == "brest")
async def cb_restore_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("backup"):
        await callback.answer("Backup is turned off", show_alert=True)
        return
    from database.store import backend_name

    backend = backend_name()
    hint = (
        "Send the <b>.db</b> backup file."
        if backend == "sqlite"
        else "Send the <b>.json</b> backup dump."
    )
    await state.set_state(AdminStates.restore_file)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "?? <b>Restore database</b>\n\n"
        f"{hint}\n"
        "Current data will be replaced. This cannot be undone.",
        cancel_inline_kb("apnl:backup"),
    )


@router.message(AdminStates.restore_file, F.document)
async def restore_file_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    from database.store import backend_name, restore_collections

    document = message.document
    if document is None:
        return
    size = int(document.file_size or 0)
    if size <= 0 or size > MAX_ZIP_SIZE:
        await message.answer(
            f"File too large or empty (limit {MAX_ZIP_SIZE} bytes). "
            "Send the backup file:",
            reply_markup=cancel_inline_kb("apnl:backup"),
        )
        return
    try:
        downloaded = await message.bot.download(document)
        data = downloaded.read() if downloaded is not None else None
    except TelegramAPIError:
        data = None
    if not data:
        await message.answer(
            "Could not download that file. Try again:",
            reply_markup=cancel_inline_kb("apnl:backup"),
        )
        return

    backend = backend_name()
    if backend == "sqlite":
        path = DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")
        staged = f"{path}.restore"
        try:
            with open(staged, "wb") as handle:
                handle.write(data)
        except OSError:
            logger.exception("restore staging failed")
            await state.clear()
            await message.answer(
                "? Could not write the restore file.", reply_markup=_back_panel_kb()
            )
            return
        await state.clear()
        await features.audit("Restore file staged (restart to apply)")
        await message.answer(
            "? Restore file staged.\n\n"
            "Restart the bot and it will come back with this backup. "
            "The current database is saved as "
            f"<code>{path}.replaced</code>.",
            reply_markup=_back_panel_kb(),
        )
        return

    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict) or not any(
        isinstance(payload.get(k), list)
        for k in ("users", "stuffs", "settings", "payment_requests")
    ):
        await state.clear()
        await message.answer(
            "? That does not look like a valid backup dump.",
            reply_markup=_back_panel_kb(),
        )
        return
    try:
        await restore_collections(payload)
    except Exception:
        logger.exception("mongo restore failed")
        await state.clear()
        await message.answer(
            "? Restore failed (see logs).", reply_markup=_back_panel_kb()
        )
        return
    await state.clear()
    await features.audit("Database restored from upload")
    await message.answer(
        "? Database restored from the backup.", reply_markup=_back_panel_kb()
    )


@router.message(AdminStates.restore_file)
async def restore_file_invalid(message: Message) -> None:
    if not await is_admin(message.from_user.id):
        return
    await message.answer("Send the backup file as a document (or press Cancel):")


# ------------------------------------------------------------------ bulk ops


async def _show_stuffs_list(target: Message, state: FSMContext) -> None:
    items = await list_stuffs(include_archived=True)
    data = await state.get_data()
    show_bulk = await features.is_on("bulk") and bool(items)
    bulk_mode = bool(data.get("bulk_mode")) and show_bulk
    if bulk_mode:
        selected = {str(x) for x in (data.get("bulk_ids") or [])}
        text = (
            f"?? <b>Stuffs</b> � {len(selected)} selected\n\n"
            "Tap stuffs to select them, then use the action bar below."
        )
        markup = admin_stuffs_kb(items, bulk_mode=True, selected=selected)
    else:
        text = (
            "?? <b>Stuffs</b>\n\nTap a stuff to manage it."
            if items
            else "?? <b>Stuffs</b>\n\nNo stuffs yet."
        )
        markup = admin_stuffs_kb(items, show_bulk=show_bulk)
    await _edit_or_send(target, text, markup)


async def _bulk_ids(state: FSMContext) -> list[str]:
    data = await state.get_data()
    return [str(x) for x in (data.get("bulk_ids") or [])]


@router.callback_query(F.data == "bselm")
async def cb_bulk_mode(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("bulk"):
        await callback.answer("Bulk ops are turned off", show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(bulk_mode=True, bulk_ids=[])
    await callback.answer("Select mode on")
    await _show_stuffs_list(callback.message, state)


@router.callback_query(F.data.startswith("bsel:"))
async def cb_bulk_select(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    stuff_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if not data.get("bulk_mode"):
        await callback.answer("Press Bulk select first", show_alert=True)
        return
    selected = [str(x) for x in (data.get("bulk_ids") or [])]
    if stuff_id in selected:
        selected.remove(stuff_id)
    else:
        selected.append(stuff_id)
    await state.update_data(bulk_ids=selected)
    await callback.answer(f"{len(selected)} selected")
    await _show_stuffs_list(callback.message, state)


@router.callback_query(F.data == "bscancel")
async def cb_bulk_done(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer("Selection cleared")
    await _show_stuffs_list(callback.message, state)


@router.callback_query(F.data == "sbprice")
async def cb_bulk_price_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    ids = await _bulk_ids(state)
    if not ids:
        await callback.answer("Select stuffs first", show_alert=True)
        return
    await state.set_state(AdminStates.bulk_price)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>Bulk price</b> for {len(ids)} stuff(s)\n\n"
        "Send the new price, e.g. <code>199</code>:",
        cancel_inline_kb("aback"),
    )


@router.message(AdminStates.bulk_price, F.text)
async def bulk_price_apply(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().replace(",", ".")
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    try:
        price = float(digits) if digits else 0.0
    except ValueError:
        price = 0.0
    if price < 0:
        await message.answer("Send a valid price, e.g. <code>199</code>:")
        return
    ids = await _bulk_ids(state)
    for stuff_id in ids:
        await set_stuff_price(stuff_id, price)
    await features.audit(f"Bulk price set to Rs.{price:g} for {len(ids)} stuff(s)")
    await state.set_state(None)
    await message.answer(f"? Price set for {len(ids)} stuff(s).")
    await _show_stuffs_list(message, state)


@router.callback_query(F.data == "sbcat")
async def cb_bulk_category_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    ids = await _bulk_ids(state)
    if not ids:
        await callback.answer("Select stuffs first", show_alert=True)
        return
    await state.set_state(AdminStates.bulk_cat)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>Bulk category</b> for {len(ids)} stuff(s)\n\n"
        "Send the category name (or <code>-</code> to clear):",
        cancel_inline_kb("aback"),
    )


@router.message(AdminStates.bulk_cat, F.text)
async def bulk_category_apply(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    category = "" if raw in {"-", "�"} else raw[:60]
    ids = await _bulk_ids(state)
    for stuff_id in ids:
        await set_stuff_category(stuff_id, category)
    await features.audit(
        f"Bulk category set to {category or '(cleared)'} for {len(ids)} stuff(s)"
    )
    await state.set_state(None)
    await message.answer(f"? Category set for {len(ids)} stuff(s).")
    await _show_stuffs_list(message, state)


async def _bulk_flags(state: FSMContext, callback: CallbackQuery, archived: bool) -> None:
    ids = await _bulk_ids(state)
    if not ids:
        await callback.answer("Select stuffs first", show_alert=True)
        return
    for stuff_id in ids:
        await set_stuff_flags(stuff_id, archived=archived)
    action = "archived" if archived else "restored"
    await features.audit(f"Bulk {action}: {len(ids)} stuff(s)")
    await callback.answer(f"{len(ids)} stuff(s) {action}")
    await _show_stuffs_list(callback.message, state)


@router.callback_query(F.data == "sbarch")
async def cb_bulk_archive(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _bulk_flags(state, callback, True)


@router.callback_query(F.data == "sbrst")
async def cb_bulk_restore(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _bulk_flags(state, callback, False)


async def _bulk_delete_apply(callback: CallbackQuery, state: FSMContext) -> None:
    ids = await _bulk_ids(state)
    if not ids:
        await callback.answer("Select stuffs first", show_alert=True)
        return
    for stuff_id in ids:
        await delete_stuff(stuff_id)
    await features.audit(f"Bulk deleted {len(ids)} stuff(s)")
    await state.clear()
    await callback.answer(f"Deleted {len(ids)}")
    await _show_stuffs_list(callback.message, state)


@router.callback_query(F.data == "sbdel")
async def cb_bulk_delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    ids = await _bulk_ids(state)
    if not ids:
        await callback.answer("Select stuffs first", show_alert=True)
        return
    if not await features.is_on("dconfirm"):
        await _bulk_delete_apply(callback, state)
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>Delete {len(ids)} stuff(s)?</b>\n\n"
        "Lessons and buyer history references are removed. This cannot be undone.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    ib(f"Delete {len(ids)}", "sbdelok", "danger"),
                    ib("Cancel", "bscancel", "primary"),
                ]
            ]
        ),
    )


@router.callback_query(F.data == "sbdelok")
async def cb_bulk_delete_ok(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await _bulk_delete_apply(callback, state)


# ------------------------------------------------------------ stuff controls


async def _reshow_stuff_view(target: Message, stuff_id) -> None:
    doc = await get_stuff(stuff_id)
    if doc is None:
        await _edit_or_send(target, "Stuffs deleted.", _back_panel_kb())
        return
    await _edit_or_send(
        target,
        await _stuff_view_text(doc),
        admin_stuff_view_kb(
            str(doc["_id"]),
            len(doc.get("lessons") or []),
            show_ctrl=await features.is_on("stuffctrl"),
            archived=bool(doc.get("archived")),
            featured=bool(doc.get("featured")),
        ),
    )


@router.callback_query(F.data.startswith("sarch:"))
async def cb_stuff_archive_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("stuffctrl"):
        await callback.answer("Stuffs controls are turned off", show_alert=True)
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    new_value = not bool(doc.get("archived"))
    await set_stuff_flags(stuff_id, archived=new_value)
    await features.audit(
        f"Stuff {doc.get('name')}: {'archived' if new_value else 'restored'}"
    )
    await callback.answer("Archived" if new_value else "Restored")
    await _reshow_stuff_view(callback.message, stuff_id)


@router.callback_query(F.data.startswith("spin:"))
async def cb_stuff_pin_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("stuffctrl"):
        await callback.answer("Stuffs controls are turned off", show_alert=True)
        return
    stuff_id = callback.data.split(":", 1)[1]
    doc = await get_stuff(stuff_id)
    if doc is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    new_value = not bool(doc.get("featured"))
    await set_stuff_flags(stuff_id, featured=new_value)
    await features.audit(
        f"Stuff {doc.get('name')}: {'pinned' if new_value else 'unpinned'}"
    )
    await callback.answer("Pinned" if new_value else "Unpinned")
    await _reshow_stuff_view(callback.message, stuff_id)


# ---------------------------------------------------------------- admin roles


@router.callback_query(F.data.startswith("admrole:"))
async def cb_admin_role_pick(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("roles"):
        await callback.answer("Admin roles are turned off", show_alert=True)
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid", show_alert=True)
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>Role for admin</b> <code>{tg_id}</code>\n\n"
        "� Full � everything\n"
        "� Support � users, tickets, audit\n"
        "� Content � stuffs, media, start/help text, reviews",
        role_kb(tg_id),
    )


@router.callback_query(F.data.startswith("admr:"))
async def cb_admin_role_set(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("roles"):
        await callback.answer("Admin roles are turned off", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid", show_alert=True)
        return
    _, raw_id, role = parts
    if role not in {"full", "support", "content"}:
        await callback.answer("Unknown role", show_alert=True)
        return
    try:
        tg_id = int(raw_id)
    except ValueError:
        await callback.answer("Invalid", show_alert=True)
        return
    await set_admin_role(tg_id, role)
    await features.audit(f"Admin {tg_id} role set to {role}")
    await callback.answer(f"Role: {role}")
    items = await get_admin_list()
    await _edit_or_send(
        callback.message,
        ADMINS_TEXT,
        admins_kb_view(items, show_roles=await features.is_on("roles")),
    )


# -------------------------------------------------------------- canned replies


def _age_hours(value) -> float:
    moment = _event_time(value)
    if moment is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 3600)


def _prio_for(age: float) -> str:
    if age > 24:
        return " ??"
    if age > 6:
        return " ??"
    return ""


@router.callback_query(F.data.startswith("cannone:"))
async def cb_canned_view(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    index = int(callback.data.split(":", 1)[1])
    items = await features.get_canned()
    if 0 <= index < len(items):
        await callback.answer(items[index][:180], show_alert=True)
    else:
        await callback.answer("Canned reply not found", show_alert=True)


@router.callback_query(F.data.startswith("candel:"))
async def cb_canned_delete(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("sla"):
        await callback.answer("Support priority is turned off", show_alert=True)
        return
    index = int(callback.data.split(":", 1)[1])
    items = await features.get_canned()
    if not 0 <= index < len(items):
        await callback.answer("Not found", show_alert=True)
        return
    removed = items.pop(index)
    await features.save_canned(items)
    await features.audit(f"Canned reply removed: {removed[:30]}")
    await callback.answer("Deleted")
    await _show_support(callback.message)


@router.callback_query(F.data == "canadd")
async def cb_canned_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("sla"):
        await callback.answer("Support priority is turned off", show_alert=True)
        return
    await state.set_state(AdminStates.canned_add)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "?? <b>Add canned reply</b>\n\n"
        "Send the quick reply text (HTML supported). "
        "It appears on the Support page for one-tap replies.",
        cancel_inline_kb("apnl:support"),
    )


@router.message(AdminStates.canned_add, F.text)
async def canned_add_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Send a non-empty reply:")
        return
    items = await features.get_canned()
    if len(items) >= 20:
        await state.clear()
        await message.answer("Canned list is full (20 max).", reply_markup=panel_inline_kb())
        return
    items.append(text[:500])
    await features.save_canned(items)
    await features.audit(f"Canned reply added: {text[:30]}")
    await state.clear()
    await message.answer("? Canned reply saved.")
    await _show_support(message)


@router.callback_query(F.data.startswith("tcan:"))
async def cb_ticket_canned_send(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid", show_alert=True)
        return
    _, ticket_id, index_raw = parts
    try:
        index = int(index_raw)
    except ValueError:
        await callback.answer("Invalid", show_alert=True)
        return
    items = await features.get_canned()
    if not 0 <= index < len(items):
        await callback.answer("Canned reply not found", show_alert=True)
        return
    ticket = next(
        (t for t in await features.get_tickets() if str(t.get("id")) == ticket_id),
        None,
    )
    if ticket is None:
        await callback.answer("Ticket not found", show_alert=True)
        return
    reply = items[index]
    try:
        await callback.bot.send_message(
            int(ticket.get("user_tg_id")),
            f"?? <b>Support reply</b>\n\n{reply}",
        )
        sent = True
    except TelegramAPIError:
        logger.exception("Canned ticket reply failed")
        sent = False
    await features.audit(
        f"Canned reply to {ticket.get('user_name')} ({'sent' if sent else 'FAILED'})"
    )
    await callback.answer("Reply sent" if sent else "Could not message user")


# --------------------------------------------------------------- customer CRM


@router.callback_query(F.data.startswith("unote:"))
async def cb_user_note_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("crm"):
        await callback.answer("Customer timeline is turned off", show_alert=True)
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid", show_alert=True)
        return
    current = await features.get_note(tg_id)
    await state.set_state(AdminStates.user_note)
    await state.update_data(note_user=tg_id)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>Private note</b> for <code>{tg_id}</code>\n\n"
        f"Current: {html.escape(current) if current else '(empty)'}\n\n"
        "Send the note text (or <code>-</code> to clear):",
        cancel_inline_kb(f"ucard:{tg_id}"),
    )


@router.message(AdminStates.user_note, F.text)
async def user_note_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    data = await state.get_data()
    tg_id = data.get("note_user")
    if tg_id is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    note = "" if raw in {"-", "�"} else raw[:500]
    await features.set_note(int(tg_id), note)
    await features.audit(f"Note {'cleared' if not note else 'updated'} for {tg_id}")
    await state.clear()
    await message.answer("? Note saved.")
    await _show_user_card(message, int(tg_id))


@router.callback_query(F.data.startswith("utag:"))
async def cb_user_tag_toggle(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("crm"):
        await callback.answer("Customer timeline is turned off", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Invalid", show_alert=True)
        return
    _, raw_id, tag = parts
    try:
        tg_id = int(raw_id)
    except ValueError:
        await callback.answer("Invalid", show_alert=True)
        return
    active = await features.toggle_tag(tg_id, tag)
    await features.audit(f"Tag {tag} {'on' if active else 'off'} for {tg_id}")
    await callback.answer(f"{tag}: {'on' if active else 'off'}")
    await _show_user_card(callback.message, tg_id)


# ----------------------------------------------------------- balance confirm


async def _apply_balance(tg_id: int, op: str, amount: float) -> tuple[str, float]:
    if op == "add":
        new_balance = await add_balance(tg_id, amount)
        return f"added Rs.{amount:g}", new_balance
    user = await find_user(tg_id)
    current = float((user or {}).get("balance", 0) or 0)
    actual = min(amount, current)
    new_balance = await add_balance(tg_id, -actual)
    return f"removed Rs.{actual:g}", new_balance


@router.callback_query(F.data == "uokbal")
async def cb_balance_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    tg_id = data.get("target_id")
    op = str(data.get("op") or "add")
    try:
        amount = float(data.get("bal_amount") or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if tg_id is None or amount <= 0:
        await state.clear()
        await callback.answer("Session expired", show_alert=True)
        return
    action, new_balance = await _apply_balance(int(tg_id), op, amount)
    await state.clear()
    await features.audit(
        f"Balance {action} for {tg_id} (now Rs.{new_balance:g})"
    )
    await callback.answer("Balance updated")
    await _show_user_card(callback.message, int(tg_id))


@router.callback_query(F.data == "ucalbal")
async def cb_balance_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    tg_id = data.get("target_id")
    await state.clear()
    await callback.answer("Cancelled")
    if tg_id is not None:
        await _show_user_card(callback.message, int(tg_id))


# ----------------------------------------------------------- clone dashboard


async def _clone_network_text(entries: list[dict], running: set[str]) -> str:
    if not entries:
        return ""
    lines = ["----------------------", "<b>Network summary</b>"]
    total_revenue = 0.0
    for entry in entries:
        tag = str(entry.get("tag", ""))
        with as_tag(tag):
            requests = await list_requests()
            approved = [r for r in requests if str(r.get("status")) == "approved"]
            pending = sum(1 for r in requests if str(r.get("status")) == "pending")
            revenue = sum(float(r.get("amount", 0) or 0) for r in approved)
            users = await count_users_seen(tag)
        total_revenue += revenue
        status = "running" if tag in running else "stopped"
        name = str(entry.get("username") or tag)
        lines.append(
            f"� @{name} [{status}] � Rs.{revenue:g}, {len(approved)} orders, "
            f"{users} users, {pending} pending"
        )
    lines.append(f"Total network revenue: <b>Rs.{total_revenue:g}</b>")
    return "\n".join(lines)


async def _clone_stats_text(entry: dict) -> str:
    tag = str(entry.get("tag", ""))
    status = "?? running" if clone_manager.is_running(tag) else "?? stopped"
    mode = "same clone (copied)" if entry.get("mode") == "copy" else "new database"
    with as_tag(tag):
        requests = await list_requests()
        approved = [r for r in requests if str(r.get("status")) == "approved"]
        pending = [r for r in requests if str(r.get("status")) == "pending"]
        rejected = [
            r for r in requests if str(r.get("status")) in {"rejected", "failed"}
        ]
        revenue = sum(float(r.get("amount", 0) or 0) for r in approved)
        stuffs = await list_stuffs(include_archived=True)
        users = await count_users_seen(tag)
    now = datetime.now(timezone.utc)
    today_revenue = 0.0
    today_orders = 0
    for request in approved:
        moment = _event_time(request.get("created_at"))
        if moment is not None and moment.date() == now.date():
            today_revenue += float(request.get("amount", 0) or 0)
            today_orders += 1
    return (
        f"?? <b>Clone: @{html.escape(str(entry.get('username') or tag))}</b>\n"
        "----------------------\n"
        f"Status: <b>{status}</b>\n"
        f"Mode: {mode}\n"
        f"Tag: <code>{html.escape(tag)}</code>\n"
        "----------------------\n"
        f"?? Users seen: <b>{users}</b>\n"
        f"?? Stuffs: <b>{len(stuffs)}</b>\n"
        f"?? Orders: {len(approved)} approved � {len(pending)} pending � "
        f"{len(rejected)} rejected\n"
        f"?? Revenue total: <b>Rs.{revenue:g}</b>\n"
        f"?? Revenue today: <b>Rs.{today_revenue:g}</b> ({today_orders} orders)"
    )


def _clone_dash_kb(tag: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Broadcast to clone", f"clbc:{tag}", "success"),
                ib("Stop" if clone_manager.is_running(tag) else "Start", f"cltog:{tag}"),
            ],
            [
                ib("Remove", f"cldel:{tag}", "danger"),
                ib("Back", "apnl:clones", "primary"),
            ],
        ]
    )


@router.callback_query(F.data.startswith("clbc:"))
async def cb_clone_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    if not await features.is_on("clonedash"):
        await callback.answer("Clone dashboard is turned off", show_alert=True)
        return
    tag = callback.data.split(":", 1)[1]
    entries = await clone_manager.load_registry()
    entry = next((e for e in entries if e.get("tag") == tag), None)
    if entry is None:
        await callback.answer("Clone not found", show_alert=True)
        return
    name = str(entry.get("username") or tag)
    await state.clear()
    await state.set_state(AdminStates.broadcast_text)
    await state.update_data(bc_tag=tag)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"?? <b>Broadcast to @{html.escape(name)}</b>\n\n"
        "Send the message: text, photo, video, GIF or sticker "
        "(caption = text, sent as-is).\n"
        "You will pick the audience and see a preview before sending.",
        cancel_inline_kb(f"clinfo:{tag}"),
    )


@router.callback_query(F.data.startswith("cldelok:"))
async def cb_clone_delete_ok(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    tag = callback.data.split(":", 1)[1]
    await clone_manager.remove_clone(tag)
    await features.audit(f"Clone removed: {tag}")
    await callback.answer("Clone removed")
    await _show_clones(callback.message)


@router.message(AdminStates.cfg_alert, F.text)
async def cfg_alert_save(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    try:
        value = float((message.text or "").strip())
    except ValueError:
        await message.answer("Send a number, e.g. <code>500</code>")
        return
    if value < 0:
        await message.answer("Value must be 0 or more")
        return
    await features.set_alert_min(value)
    await features.audit(f"Big-sale alert threshold set to Rs.{value:g}")
    await _features_done(
        message,
        state,
        (
            f"Big-sale alerts on from Rs.{value:g}"
            if value > 0
            else "Big-sale alerts disabled (threshold = 0)"
        ),
    )
