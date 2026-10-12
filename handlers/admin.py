import asyncio
import html
import logging
import uuid

from aiogram import F, Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from config import ADMIN_IDS, AUTH_ADMIN_IDS, MAX_ZIP_SIZE, MEDIA_TEMP_DIR
from database.store import (
    DuplicateKeyError,
    add_balance,
    append_lessons,
    count_users,
    create_stuff,
    delete_method,
    delete_stuff,
    get_setting,
    get_stuff,
    get_stuff_by_name,
    insert_method,
    list_stuffs,
    list_user_ids,
    rename_stuff,
    set_banned,
    set_lesson_demo,
    set_proofs,
    set_setting,
    set_showcase,
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
    desc_step_kb,
    demo_step_kb,
    ib,
    kind_crypto_kb,
    log_group_kb,
    note_skip_kb,
    panel_inline_kb,
    payment_method_view_kb,
    payment_methods_admin_kb,
    proofs_kb,
    protect_kb,
    showcase_kb,
    start_msg_kb,
)
from states.admin_states import AdminStates
from utils import clones as clone_manager
from utils.export import build_stuff_zip, cleanup_export, send_stuff_direct
from utils.monitoring import stats_text, vps_text
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
    find_user,
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


async def _stuff_view_text(stuff: dict) -> str:
    price = stuff.get("price", 0)
    price_text = f"{int(price)} ⭐" if price > 0 else "free"
    lessons = stuff.get("lessons", [])
    demo_count = sum(1 for lesson in lessons if lesson.get("is_demo"))
    text = (
        f"<b>{html.escape(stuff.get('name', '?'))}</b>\n\n"
        f"Price: {price_text}\n"
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


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(panel_text(), reply_markup=panel_inline_kb())


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

    if key == "zip":
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
        items = await list_stuffs()
        text = (
            "🗂 <b>Stuffs</b>\n\nTap a stuff to manage it."
            if items
            else "🗂 <b>Stuffs</b>\n\nNo stuffs yet."
        )
        await _edit_or_send(target, text, admin_stuffs_kb(items))
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
        await _edit_or_send(target, ADMINS_TEXT, admins_kb_view(items))
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
            "Send the message text (HTML is supported).\n"
            "You will see a preview and confirm before sending.",
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
    await _edit_or_send(target, f"🤖 <b>Clone bots</b>\n\n{mode_text}", clones_kb(entries, running))


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
    if not entry:
        await callback.answer("Clone not found", show_alert=True)
        return
    status = "running" if clone_manager.is_running(tag) else "stopped"
    mode = "same clone (copied)" if entry.get("mode") == "copy" else "new database"
    await callback.answer(
        f"@{entry.get('username') or tag} - {mode} - {status}",
        show_alert=True,
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
    await clone_manager.remove_clone(tag)
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
async def cb_stuffs_list(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    items = await list_stuffs()
    text = "🗂 <b>Stuffs</b>\n\nTap a stuff to manage it." if items else "🗂 <b>Stuffs</b>\n\nNo stuffs yet."
    await callback.answer()
    await _edit_or_send(callback.message, text, admin_stuffs_kb(items))


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
        admin_stuff_view_kb(stuff_id, len(doc.get("lessons", []))),
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
        admin_stuff_view_kb(stuff_id, len((doc or {}).get("lessons", []))),
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
    await state.clear()
    doc = await get_stuff(data["stuff_id"])
    await message.answer(
        "✅ Renamed.", reply_markup=panel_inline_kb()
    )
    if doc:
        await message.answer(
            await _stuff_view_text(doc),
            reply_markup=admin_stuff_view_kb(data["stuff_id"], len(doc.get("lessons", []))),
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
    await state.clear()
    doc = await get_stuff(data["stuff_id"])
    await message.answer("✅ Price saved.", reply_markup=panel_inline_kb())
    if doc:
        await message.answer(
            await _stuff_view_text(doc),
            reply_markup=admin_stuff_view_kb(data["stuff_id"], len(doc.get("lessons", []))),
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


def admins_kb_view(items: list[dict]):
    return admins_kb(items, ADMIN_IDS)


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
    request_id = callback.data.split(":", 1)[1]
    request = await get_payment_request(request_id)
    if request is None:
        await callback.answer("Request not found", show_alert=True)
        return
    if request.get("status") != "pending":
        await callback.answer(f"Already {request.get('status')}", show_alert=True)
        return

    await set_request_status(request_id, "rejected")
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


def _user_card_kb(tg_id: int, banned: bool) -> InlineKeyboardMarkup:
    ban_row = (
        ib("Unban", f"uunban:{tg_id}", "success")
        if banned
        else ib("Ban", f"uban:{tg_id}", "danger")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ban_row],
            [
                ib("Add balance", f"ubal:{tg_id}", "primary"),
                ib("Remove balance", f"ubalrm:{tg_id}", "primary"),
            ],
            [
                ib("Refresh", f"ucard:{tg_id}", "primary"),
                ib("Back", "umgrback", "primary"),
            ],
        ]
    )


async def _show_user_card(target: Message, tg_id: int) -> None:
    user = await find_user(tg_id)
    if user is None:
        from database.store import ensure_user

        user = await ensure_user(tg_id, None, None)
    admin_user = tg_id in ADMIN_IDS or any(
        a.get("tg_id") == tg_id for a in await get_admin_list()
    )
    await _edit_or_send(
        target,
        _user_card_text(user, tg_id, admin_user),
        _user_card_kb(tg_id, bool(user.get("banned"))),
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
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    if tg_id == callback.from_user.id or tg_id in ADMIN_IDS:
        await callback.answer("You cannot ban an admin", show_alert=True)
        return
    await set_banned(tg_id, True)
    await callback.answer("User banned 🚫")
    await _show_user_card(callback.message, tg_id)


@router.callback_query(F.data.startswith("uunban:"))
async def cb_user_unban(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    try:
        tg_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    await set_banned(tg_id, False)
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

    if op == "add":
        new_balance = await add_balance(tg_id, amount)
        action = f"added Rs.{amount:g}"
    else:
        user = await find_user(tg_id)
        current = float((user or {}).get("balance", 0) or 0)
        actual = min(amount, current)
        new_balance = await add_balance(tg_id, -actual)
        action = f"removed Rs.{actual:g}"

    await state.clear()
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


@router.message(AdminStates.broadcast_text, F.text)
async def broadcast_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin(message.from_user.id):
        return
    text = message.text or ""
    if not text.strip():
        await message.answer("Send a non-empty text:")
        return
    await state.update_data(broadcast_text=text)
    recipients = await count_users()
    preview = text if len(text) <= 800 else text[:800] + "…"
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib(f"Send to {recipients} users", "bcsend", "success"),
                ib("Cancel", "bccancel", "danger"),
            ]
        ]
    )
    await message.answer(
        f"📣 <b>Broadcast preview</b>\n"
        f"Recipients: <b>{recipients}</b>\n"
        "──────────────────────\n"
        f"{preview}",
        reply_markup=markup,
    )


@router.callback_query(F.data == "bcsend")
async def broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("Nothing to send", show_alert=True)
        return
    await state.clear()
    await callback.answer("Broadcasting…")
    await _edit_or_send(callback.message, "📣 <b>Broadcasting…</b>", None)

    targets = await list_user_ids()
    sent = 0
    failed = 0
    for tg_id in targets:
        try:
            await callback.bot.send_message(tg_id, text)
            sent += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[ib("Panel", "aback", "primary")]]
    )
    await _edit_or_send(
        callback.message,
        f"📣 <b>Broadcast finished</b>\n"
        "──────────────────────\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"👥 Total: {len(targets)}",
        markup,
    )


@router.callback_query(F.data == "bccancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer("Cancelled")
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[ib("Panel", "aback", "primary")]]
    )
    await _edit_or_send(callback.message, "❌ Broadcast cancelled.", markup)


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
            admin_stuff_view_kb(stuff_id, len((doc or {}).get("lessons", [])))
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
        admin_stuff_view_kb(stuff_id, len(doc.get("lessons", []))),
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


@router.callback_query(F.data == "aback")
async def cb_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await _edit_or_send(callback.message, panel_text(), panel_inline_kb())
