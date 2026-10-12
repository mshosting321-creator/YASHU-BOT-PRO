import html
import logging
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from database.store import (
    add_balance,
    deduct_balance,
    get_setting,
    get_stuff,
    list_stuffs,
    set_setting,
)
from keyboards.inline import (
    after_payment_kb,
    bdirect_kb,
    catalog_kb,
    checkin_kb,
    coupon_entry_kb,
    demo_list_kb,
    demo_view_kb,
    forcejoin_kb,
    history_kb,
    ib,
    invoice_kb,
    main_menu_kb,
    menu_button_kb,
    method_pay_kb,
    multi_payment_methods_kb,
    payment_methods_kb,
    player_kb,
    proof_gallery_kb,
    rate_kb,
    review_request_kb,
    screenshot_prompt_kb,
    stuff_user_kb,
    wishlist_kb,
    with_coupon_button,
)
from states.admin_states import PaymentStates, UserStates
from utils.payments import (
    build_transaction_text,
    create_payment_request,
    deliver_stuff,
    get_active_methods,
    get_method,
    notify_log_group,
    save_request_log_ids,
    set_request_status,
    split_stuff_ids,
    attach_screenshot,
)
from utils.security import (
    ensure_user,
    get_help_text,
    get_start_message,
    grant_access,
    is_admin,
    is_banned,
    is_protect_content,
    user_has_access,
)
from utils import features

router = Router()

logger = logging.getLogger(__name__)

DEFAULT_START_TEXT = (
    "Welcome!\n\n"
    "Buy stuffs and watch them here."
)


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is not None and await is_banned(user.id) and not await is_admin(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer(
                    "You are banned from using this bot.", show_alert=True
                )
            else:
                await event.answer("You are banned from using this bot.")
            return None
        return await handler(event, data)


router.message.middleware(BanMiddleware())
router.callback_query.middleware(BanMiddleware())


class FloodMiddleware(BaseMiddleware):
    """Warns users who send more than 8 messages within 10 seconds."""

    hits: dict[int, list[float]] = {}
    warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        try:
            flood_on = await features.is_on("antiflood")
        except Exception:
            flood_on = False
        if not flood_on:
            return await handler(event, data)
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        if await is_admin(user.id):
            return await handler(event, data)
        now = time.monotonic()
        bucket = [t for t in self.hits.get(user.id, []) if now - t < 10.0]
        bucket.append(now)
        if len(self.hits) > 5000:
            self.hits.clear()
        self.hits[user.id] = bucket
        if len(bucket) <= 8:
            return await handler(event, data)
        last = self.warned.get(user.id, 0.0)
        if now - last > 60.0:
            self.warned[user.id] = now
            if len(self.warned) > 5000:
                self.warned.clear()
            try:
                await event.answer(
                    "You are sending messages too fast. Please wait a moment."
                )
            except Exception:
                pass
        return None


router.message.middleware(FloodMiddleware())


def _price_text(price: float) -> str:
    return str(int(price)) if price == int(price) else str(price)


def _parse_ids(raw: str) -> list[str]:
    return split_stuff_ids(raw)


async def _fetch_stuffs(raw: str) -> list[dict]:
    stuffs: list[dict] = []
    for sid in _parse_ids(raw):
        doc = await get_stuff(sid)
        if doc is not None:
            stuffs.append(doc)
    return stuffs


def _total_price(stuffs: list[dict]) -> float:
    return sum(float(s.get("price", 0) or 0) for s in stuffs)


async def _menu_extras() -> list[tuple[str, str]]:
    extras: list[tuple[str, str]] = []
    if await features.is_on("history"):
        extras.append(("History", "shistory"))
    if await features.is_on("wishlist"):
        extras.append(("Wishlist", "swishlist"))
    if await features.is_on("checkin"):
        extras.append(("Check-in", "scheckin"))
    if await features.is_on("support"):
        extras.append(("Support", "sticket"))
    if await features.is_on("search"):
        extras.append(("Search", "ssearch"))
    if await features.is_on("giftcodes"):
        extras.append(("Gift code", "sgift"))
    return extras


async def _maintenance_gate(message: Message | CallbackQuery) -> bool:
    """True when a non-admin is blocked by maintenance mode."""
    user = message.from_user
    if user is None or await is_admin(user.id):
        return False
    if await features.is_on("maintenance"):
        if isinstance(message, CallbackQuery):
            await message.answer(features.maintenance_text(), show_alert=True)
        else:
            await message.answer(
                features.maintenance_text(), reply_markup=menu_button_kb()
            )
        return True
    return False


async def _coupon_from_state(state: FSMContext) -> dict | None:
    if not await features.is_on("coupons"):
        return None
    data = await state.get_data()
    coupon = data.get("coupon")
    if isinstance(coupon, dict) and features.coupon_applies(coupon):
        return coupon
    return None


def _pay_total(state_data: dict, total: float) -> float:
    coupon = state_data.get("coupon")
    if isinstance(coupon, dict):
        saved = features.coupon_discount(total, coupon)
        return round(total - saved, 2)
    return total


async def _forcejoin_gate(callback: CallbackQuery) -> bool:
    allowed, channel = await features.forcejoin_allowed(
        callback.bot, callback.from_user.id
    )
    if allowed:
        return True
    await callback.answer("Join the channel first", show_alert=True)
    try:
        await callback.message.answer(
            features.join_prompt(channel),
            reply_markup=forcejoin_kb(channel),
        )
    except TelegramAPIError:
        logger.exception("Force-join prompt failed")
    return False


def _stuff_info_text(stuff: dict, has_access: bool) -> str:
    text = f"<b>{html.escape(stuff.get('name', '?'))}</b>"
    description = stuff.get("description")
    if description:
        text += f"\n\n{html.escape(description)}"
    text += f"\n\nStuffs: {len(stuff.get('lessons', []))}"
    price = stuff.get("price", 0)
    if price > 0:
        text += (
            f"\nPrice: Rs.{int(price)}"
            if not has_access
            else "\nYou have full access"
        )
    else:
        text += "\nFree"
    return text


async def _show_menu(message: Message, edit: bool = False) -> None:
    admin = await is_admin(message.from_user.id)
    start = await get_start_message()
    markup = main_menu_kb(admin, extra=await _menu_extras())
    text = start.get("text") or DEFAULT_START_TEXT

    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramAPIError:
            pass
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        edit = False

    if start["media_type"] == "photo" and start["media_id"]:
        try:
            await message.answer_photo(
                start["media_id"], caption=text[:1024], reply_markup=markup
            )
            return
        except TelegramAPIError:
            logger.exception("Start photo failed, sending text")
    if start["media_type"] == "video" and start["media_id"]:
        try:
            await message.answer_video(
                start["media_id"], caption=text[:1024], reply_markup=markup
            )
            return
        except TelegramAPIError:
            logger.exception("Start video failed, sending text")
    try:
        await message.answer(text, reply_markup=markup)
    except TelegramAPIError:
        logger.exception("Start text failed")
        await message.answer(DEFAULT_START_TEXT, reply_markup=markup)


async def _answer_menu(target: Message, admin: bool = False) -> None:
    start = await get_start_message()
    markup = main_menu_kb(admin, extra=await _menu_extras())
    text = start.get("text") or DEFAULT_START_TEXT
    if target.photo or target.video or target.document:
        try:
            await target.delete()
        except TelegramAPIError:
            pass
        try:
            await target.answer(text[:1024], reply_markup=markup)
        except TelegramAPIError:
            logger.exception("Could not resend menu")
    else:
        try:
            await target.edit_text(text, reply_markup=markup)
        except TelegramAPIError:
            await target.answer(text, reply_markup=markup)


async def _edit_or_send(target: Message, text: str, markup) -> None:
    if target.photo or target.video or target.document:
        try:
            await target.delete()
        except TelegramAPIError:
            pass
        await target.answer(text, reply_markup=markup)
    else:
        try:
            await target.edit_text(text, reply_markup=markup)
        except TelegramAPIError:
            await target.answer(text, reply_markup=markup)


async def _send_stuff_item(message: Message, stuff: dict, pos: int) -> None:
    lessons = stuff.get("lessons", [])
    lesson = lessons[pos]
    protect = await is_protect_content()

    caption = f"<b>{html.escape(lesson.get('title') or f'Stuff {pos + 1}')}</b>"
    if lesson.get("description"):
        caption += f"\n\n{html.escape(lesson['description'][:800])}"
    caption += f"\n\nStuff {pos + 1} of {len(lessons)}"
    caption = caption[:1024]

    reply_markup = player_kb(str(stuff["_id"]), pos, len(lessons))

    try:
        if lesson.get("media_type") == "video":
            await message.answer_video(
                video=lesson["file_id"],
                caption=caption,
                reply_markup=reply_markup,
                supports_streaming=True,
                protect_content=protect,
            )
        else:
            await message.answer_photo(
                photo=lesson["file_id"],
                caption=caption,
                reply_markup=reply_markup,
                protect_content=protect,
            )
    except TelegramAPIError:
        logger.exception("Failed to send stuff item %s", pos)
        await message.answer(
            "Could not load this media. Try again later.",
            reply_markup=reply_markup,
        )


# ---------------------------------------------------------------- start / menu


def _parse_ref(text: str | None) -> int | None:
    parts = (text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        try:
            return int(parts[1][4:])
        except ValueError:
            return None
    return None


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if await _maintenance_gate(message):
        return
    admin = await is_admin(message.from_user.id)
    try:
        user = await ensure_user(message.from_user)
    except Exception:
        logger.exception("ensure_user failed on /start")
        user = None

    referrer = _parse_ref(message.text)
    if referrer and referrer != message.from_user.id:
        refdata = await features.get_refdata(message.from_user.id)
        purchased = (user or {}).get("purchased") or []
        if not refdata and not purchased:
            await features.set_refdata(
                message.from_user.id, {"by": referrer, "credited": False}
            )

    new_names: list[str] = []
    if not admin and await features.is_on("welcomeback"):
        previous = await get_setting(f"laststart_{message.from_user.id}", "")
        if previous:
            try:
                moment = datetime.fromisoformat(previous)
            except ValueError:
                moment = None
            if moment is not None:
                for stuff in await list_stuffs():
                    raw = stuff.get("created_at")
                    if not raw:
                        continue
                    try:
                        created = datetime.fromisoformat(str(raw))
                    except ValueError:
                        continue
                    if created > moment:
                        new_names.append(str(stuff.get("name", "?")))
        await set_setting(
            f"laststart_{message.from_user.id}",
            datetime.now().isoformat(),
        )

    await _show_menu(message)
    if new_names:
        names = ", ".join(new_names[:5])
        await message.answer(
            f"👋 <b>Welcome back!</b>\nNew stuffs since your last visit:\n"
            f"• {html.escape(names)}"
        )


@router.callback_query(F.data == "smenu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if await _maintenance_gate(callback):
        return
    await callback.answer()
    await _answer_menu(callback.message, admin=await is_admin(callback.from_user.id))


@router.callback_query(F.data == "shelp")
async def cb_help(callback: CallbackQuery) -> None:
    text = await get_help_text()
    await callback.answer()
    await _edit_or_send(callback.message, text, menu_button_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(await get_help_text(), reply_markup=menu_button_kb())


# ---------------------------------------------------------------- catalog


@router.callback_query(F.data == "scatalog")
async def cb_catalog(callback: CallbackQuery) -> None:
    items = await list_stuffs()
    admin = await is_admin(callback.from_user.id)
    if not items:
        await callback.answer("No stuffs yet", show_alert=True)
        return
    show_category = await features.is_on("categories")
    if show_category:
        items = sorted(
            items,
            key=lambda s: (
                str(s.get("category") or "").lower(),
                str(s.get("name") or "").lower(),
            ),
        )
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "<b>Stuffs</b>\n\nChoose a stuff:",
        catalog_kb(items, admin=admin, show_category=show_category),
    )


@router.message(Command("catalog"))
async def cmd_catalog(message: Message) -> None:
    items = await list_stuffs()
    admin = await is_admin(message.from_user.id)
    if not items:
        await message.answer("No stuffs yet.")
        return
    show_category = await features.is_on("categories")
    if show_category:
        items = sorted(
            items,
            key=lambda s: (
                str(s.get("category") or "").lower(),
                str(s.get("name") or "").lower(),
            ),
        )
    await message.answer(
        "<b>Stuffs</b>\n\nChoose a stuff:",
        reply_markup=catalog_kb(items, admin=admin, show_category=show_category),
    )


@router.callback_query(F.data == "smy")
async def cb_my_stuffs(callback: CallbackQuery) -> None:
    user = await ensure_user(callback.from_user)
    purchased = {str(x) for x in user.get("purchased", [])}
    items = await list_stuffs()
    from database.store import purchase_key

    visible = [
        s
        for s in items
        if s.get("price", 0) <= 0 or purchase_key(s["_id"]) in purchased
    ]
    if not visible:
        await callback.answer("You have no stuffs yet", show_alert=True)
        return
    admin = await is_admin(callback.from_user.id)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "<b>My Stuffs</b>",
        catalog_kb(visible, admin=admin),
    )


async def _render_stuff_page(
    callback: CallbackQuery, stuff_id: str, answer: bool = True
) -> None:
    stuff = await get_stuff(stuff_id)
    if stuff is None:
        if answer:
            await callback.answer("Stuffs deleted", show_alert=True)
        return

    user = await ensure_user(callback.from_user)
    has_access = user_has_access(user, stuff)
    if answer:
        await callback.answer()
    text = _stuff_info_text(stuff, has_access)
    if has_access and float(stuff.get("price", 0) or 0) > 0:
        validity = features.expiry_line(user, stuff)
        if validity:
            text += f"\n{html.escape(validity)}"
    category = str(stuff.get("category") or "").strip()
    if category and await features.is_on("categories"):
        text = f"📁 {html.escape(category)}\n{text}"
    if await features.is_on("reviews"):
        approved = await features.approved_reviews(stuff["_id"])
        if approved:
            text += "\n\n<b>Reviews:</b>\n" + features.reviews_text(approved)
    markup = stuff_user_kb(
        stuff,
        has_access,
        show_rate=await features.is_on("reviews"),
        wish_on=await features.is_on("wishlist"),
        wished=str(stuff["_id"])
        in set(await features.get_wishlist(callback.from_user.id)),
        resume_pos=(
            await features.get_resume(callback.from_user.id, stuff["_id"])
            if has_access and await features.is_on("resume")
            else None
        ),
    )
    showcase = stuff.get("showcase")
    if showcase:
        try:
            if callback.message.photo or callback.message.video:
                await callback.message.delete()
            await callback.message.answer_photo(
                showcase, caption=text[:1024], reply_markup=markup
            )
            return
        except TelegramAPIError:
            logger.exception("Showcase send failed, falling back to text")
    await _edit_or_send(callback.message, text, markup)


@router.callback_query(F.data.startswith("sstuf:"))
async def cb_stuff(callback: CallbackQuery) -> None:
    stuff_id = callback.data.split(":", 1)[1]
    await _render_stuff_page(callback, stuff_id)


@router.callback_query(F.data.startswith("ales:"))
async def cb_stuff_item(callback: CallbackQuery) -> None:
    try:
        _, stuff_id, pos = callback.data.split(":")
        pos = int(pos)
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return

    stuff = await get_stuff(stuff_id)
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return

    lessons = stuff.get("lessons", [])
    if not 0 <= pos < len(lessons):
        await callback.answer("Not found", show_alert=True)
        return

    user = await ensure_user(callback.from_user)
    has_access = user_has_access(user, stuff)
    lesson = lessons[pos]
    if not has_access and not lesson.get("is_demo"):
        await callback.answer("This stuff requires purchase", show_alert=True)
        return

    if has_access and not lesson.get("is_demo"):
        if await features.is_on("resume"):
            await features.set_resume(callback.from_user.id, stuff_id, pos)

    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass
    await _send_stuff_item(callback.message, stuff, pos)


@router.callback_query(F.data.startswith("sresume:"))
async def cb_resume(callback: CallbackQuery) -> None:
    stuff_id = callback.data.split(":", 1)[1]
    if not await features.is_on("resume"):
        await callback.answer("Resume feature is off", show_alert=True)
        return
    stuff = await get_stuff(stuff_id)
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    pos = await features.get_resume(callback.from_user.id, stuff_id)
    lessons = stuff.get("lessons", [])
    if pos is None or not 0 <= pos < len(lessons):
        await callback.answer("No saved position yet", show_alert=True)
        return
    user = await ensure_user(callback.from_user)
    if not user_has_access(user, stuff):
        await callback.answer("This stuff requires purchase", show_alert=True)
        return
    await callback.answer(f"Resuming at #{pos + 1}")
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass
    await _send_stuff_item(callback.message, stuff, pos)


# ---------------------------------------------------------------- view demo


@router.callback_query(F.data == "vdemo")
async def cb_view_demo(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    items = [
        s for s in await list_stuffs()
        if any(lesson.get("is_demo") for lesson in s.get("lessons", []))
    ]
    if not items:
        await callback.answer("No demos available yet", show_alert=True)
        return
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "<b>View demo</b>\n\nPick a stuff:",
        demo_list_kb(items),
    )


@router.callback_query(F.data.startswith("vdl:"))
async def cb_view_demo_lesson(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    stuff_id = parts[1]
    wanted = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    stuff = await get_stuff(stuff_id)
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return

    lessons = stuff.get("lessons", [])
    demo_positions = [
        i for i, lesson in enumerate(lessons) if lesson.get("is_demo")
    ]
    if not demo_positions:
        await callback.answer("No demo available", show_alert=True)
        return

    if wanted not in demo_positions:
        wanted = demo_positions[0]
    current = demo_positions.index(wanted)
    prev_idx = demo_positions[current - 1] if current > 0 else None
    next_idx = (
        demo_positions[current + 1] if current < len(demo_positions) - 1 else None
    )

    lesson = lessons[wanted]
    protect = await is_protect_content()
    caption = (
        f"<b>{html.escape(lesson.get('title') or f'Demo {current + 1}')}</b>"
        f"\n\nDemo {current + 1} of {len(demo_positions)}"
    )
    markup = demo_view_kb(
        stuff_id, prev_idx, next_idx, float(stuff.get("price", 0) or 0)
    )
    await callback.answer()
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass
    try:
        if lesson.get("media_type") == "video":
            await callback.message.answer_video(
                video=lesson["file_id"],
                caption=caption[:1024],
                reply_markup=markup,
                supports_streaming=True,
                protect_content=protect,
            )
        else:
            await callback.message.answer_photo(
                photo=lesson["file_id"],
                caption=caption[:1024],
                reply_markup=markup,
                protect_content=protect,
            )
    except TelegramAPIError:
        logger.exception("Failed to send demo media")
        await callback.message.answer(
            "Could not load this demo. Try again later.",
            reply_markup=markup,
        )


# ---------------------------------------------------------------- proofs


@router.callback_query(F.data.startswith("proofs:"))
async def cb_proof_gallery(callback: CallbackQuery) -> None:
    try:
        _, stuff_id, index = callback.data.split(":")
        index = int(index)
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    stuff = await get_stuff(stuff_id)
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    proofs = stuff.get("proofs") or []
    if not proofs:
        await callback.answer("No proofs yet", show_alert=True)
        return
    index = max(0, min(index, len(proofs) - 1))
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"Proof {index + 1} of {len(proofs)}",
        None,
    )
    try:
        if callback.message.photo:
            await callback.message.delete()
    except TelegramAPIError:
        pass
    try:
        await callback.message.answer_photo(
            proofs[index],
            caption=f"Proof {index + 1} of {len(proofs)}",
            reply_markup=proof_gallery_kb(stuff_id, index, len(proofs)),
        )
    except TelegramAPIError:
        logger.exception("Failed to send proof")


# ---------------------------------------------------------------- buy directly


@router.callback_query(F.data == "bdirect")
async def cb_buy_directly(callback: CallbackQuery, state: FSMContext) -> None:
    items = [s for s in await list_stuffs() if float(s.get("price", 0) or 0) > 0]
    if not items:
        await callback.answer("Nothing to buy right now", show_alert=True)
        return
    await state.set_state(UserStates.bselect)
    await state.update_data(selected=[])
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "<b>Buy directly</b>\n\n"
        "Select one or more stuffs (or press Select all), "
        "then press Buy selected.",
        bdirect_kb(items, set()),
    )


async def _render_bdirect(callback: CallbackQuery, state: FSMContext) -> None:
    items = [s for s in await list_stuffs() if float(s.get("price", 0) or 0) > 0]
    data = await state.get_data()
    selected = set(data.get("selected") or [])
    await _edit_or_send(
        callback.message,
        "<b>Buy directly</b>\n\n"
        "Select one or more stuffs (or press Select all), "
        "then press Buy selected.",
        bdirect_kb(items, selected),
    )


@router.callback_query(UserStates.bselect, F.data.startswith("bd:"))
async def cb_bselect_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    stuff_id = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("selected") or [])
    if stuff_id in selected:
        selected.discard(stuff_id)
    else:
        selected.add(stuff_id)
    await state.update_data(selected=sorted(selected))
    await callback.answer()
    await _render_bdirect(callback, state)


@router.callback_query(UserStates.bselect, F.data == "bda")
async def cb_bselect_all(callback: CallbackQuery, state: FSMContext) -> None:
    items = [s for s in await list_stuffs() if float(s.get("price", 0) or 0) > 0]
    await state.update_data(selected=[str(s["_id"]) for s in items])
    await callback.answer("All selected")
    await _render_bdirect(callback, state)


@router.callback_query(UserStates.bselect, F.data == "bdc")
async def cb_bselect_clear(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(selected=[])
    await callback.answer("Cleared")
    await _render_bdirect(callback, state)


@router.callback_query(UserStates.bselect, F.data == "bbuy")
async def cb_bselect_buy(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    selected = data.get("selected") or []
    if not selected:
        await callback.answer("Select at least one stuff", show_alert=True)
        return
    stuffs = await _fetch_stuffs(",".join(selected))
    if not stuffs:
        await callback.answer("Selected stuffs are unavailable", show_alert=True)
        return
    if len(stuffs) == 1:
        await state.clear()
        await callback.answer()
        user = await ensure_user(callback.from_user)
        if user_has_access(user, stuffs[0]):
            await callback.answer("You already have access", show_alert=True)
            return
        await _show_payment_methods(callback, stuffs[0], state)
        return
    await callback.answer()
    await _show_multi_payment_methods(callback, stuffs, state)


# ---------------------------------------------------------------- payments


async def _show_payment_methods(
    callback: CallbackQuery, stuff: dict, state: FSMContext
) -> None:
    methods = await get_active_methods()
    user = await ensure_user(callback.from_user)
    balance = float(user.get("balance", 0) or 0)
    price = float(stuff.get("price", 0))
    data = await state.get_data()
    coupon = await _coupon_from_state(state)
    paid = _pay_total(data, price) if coupon else price
    can_pay_balance = paid > 0 and balance >= paid
    await state.update_data(cids=str(stuff["_id"]), cmode="one")

    if not methods and not can_pay_balance:
        await _edit_or_send(
            callback.message,
            "No payment methods are configured yet. Please contact the administrator.",
            menu_button_kb(),
        )
        return

    text = _stuff_info_text(stuff, has_access=False)
    if coupon:
        saved = features.coupon_discount(price, coupon)
        text += (
            f"\nCoupon <code>{html.escape(str(coupon.get('code')))}</code>: "
            f"−Rs.{saved:g}"
        )
        if paid != price:
            text += f"\n<b>To pay: Rs.{_price_text(paid)}</b>"
    if balance > 0:
        text += f"\nYour balance: Rs.{balance:g}"
    text += "\n\n<b>Choose a payment method:</b>"

    rows = []
    if can_pay_balance:
        rows.append(
            [
                ib(
                    f"Pay from balance (Rs.{_price_text(paid)})",
                    f"pbal:{stuff['_id']}",
                    "success",
                )
            ]
        )
    if methods:
        rows.extend(payment_methods_kb(str(stuff["_id"]), methods).inline_keyboard)
    rows.append([ib("Back", f"sstuf:{stuff['_id']}", "primary")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if await features.is_on("coupons"):
        markup = with_coupon_button(
            markup,
            (
                f"Coupon {html.escape(str(coupon.get('code')))} — change"
                if coupon
                else "Apply coupon"
            ),
        )
    await _edit_or_send(callback.message, text, markup)


async def _show_multi_payment_methods(
    callback: CallbackQuery, stuffs: list[dict], state: FSMContext
) -> None:
    methods = await get_active_methods()
    user = await ensure_user(callback.from_user)
    balance = float(user.get("balance", 0) or 0)
    total = _total_price(stuffs)
    data = await state.get_data()
    coupon = await _coupon_from_state(state)
    paid = _pay_total(data, total) if coupon else total
    can_pay_balance = paid > 0 and balance >= paid
    ids_raw = ",".join(str(s["_id"]) for s in stuffs)
    await state.update_data(cids=ids_raw, cmode="multi")

    if not methods and not can_pay_balance:
        await _edit_or_send(
            callback.message,
            "No payment methods are configured yet. Please contact the administrator.",
            bdirect_kb(stuffs, set(ids_raw.split(","))),
        )
        return

    names = "\n".join(f"• {html.escape(s.get('name', '?'))}" for s in stuffs)
    text = (
        "<b>Buy selected stuffs</b>\n\n"
        f"{names}\n\n"
        f"Total: <b>Rs.{_price_text(total)}</b>"
    )
    if coupon:
        saved = features.coupon_discount(total, coupon)
        text += (
            f"\nCoupon <code>{html.escape(str(coupon.get('code')))}</code>: "
            f"−Rs.{saved:g}"
        )
        text += f"\n<b>To pay: Rs.{_price_text(paid)}</b>"
    if balance > 0:
        text += f"\nYour balance: Rs.{balance:g}"
    text += "\n\n<b>Choose a payment method:</b>"

    markup = multi_payment_methods_kb(
        ids_raw,
        methods,
        back_cb="bdirect",
        can_pay_balance=can_pay_balance,
        balance_total=paid,
    )
    if await features.is_on("coupons"):
        markup = with_coupon_button(
            markup,
            (
                f"Coupon {html.escape(str(coupon.get('code')))} — change"
                if coupon
                else "Apply coupon"
            ),
        )
    await _edit_or_send(callback.message, text, markup)


@router.callback_query(F.data.startswith("pbal:"))
async def cb_pay_from_balance(callback: CallbackQuery, state: FSMContext) -> None:
    raw_ids = callback.data.split(":", 1)[1]
    stuffs = await _fetch_stuffs(raw_ids)
    if not stuffs:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    if not await _forcejoin_gate(callback):
        return

    user = await ensure_user(callback.from_user)
    price = _total_price(stuffs)
    data = await state.get_data()
    coupon = await _coupon_from_state(state)
    if coupon:
        price = _pay_total(data, price)
    if price <= 0:
        await callback.answer("These stuffs are free", show_alert=True)
        return
    owned = [
        s for s in stuffs if user_has_access(user, s)
    ]
    if len(owned) == len(stuffs):
        await callback.answer("You already have access", show_alert=True)
        return

    balance = float(user.get("balance", 0) or 0)
    if balance < price:
        await callback.answer("Insufficient balance", show_alert=True)
        return

    if not await deduct_balance(callback.from_user.id, price):
        await callback.answer("Insufficient balance", show_alert=True)
        return

    coupon_code = str(coupon.get("code")) if coupon else None
    await state.clear()
    if coupon_code:
        await features.bump_coupon_usage(coupon_code)
    for stuff in stuffs:
        await grant_access(callback.from_user.id, str(stuff["_id"]))

    method = {"name": "Wallet balance", "kind": "wallet"}
    request = await create_payment_request(
        user_tg_id=callback.from_user.id,
        user_username=callback.from_user.username,
        user_full_name=callback.from_user.full_name,
        stuffs=stuffs,
        method=method,
        amount_override=price,
        coupon=coupon_code,
    )
    await set_request_status(request["_id"], "approved")
    await notify_log_group(
        callback.bot,
        text=build_transaction_text(request, "Delivered"),
    )
    if await features.is_on("receipts"):
        try:
            await callback.message.answer(features.receipt_text(request))
        except TelegramAPIError:
            logger.exception("Receipt send failed")

    await callback.answer("Paid from balance")
    await _edit_or_send(
        callback.message,
        f"<b>Paid Rs.{price:g} from your balance</b>\n\n"
        f"Delivering “{html.escape(', '.join(s.get('name', '?') for s in stuffs))}”…",
        menu_button_kb(),
    )
    total_sent = 0
    total_failed = 0
    for stuff in stuffs:
        sent, failed = await deliver_stuff(
            callback.bot, callback.message.chat.id, stuff
        )
        total_sent += sent
        total_failed += failed
    done = f"<b>Delivered!</b> Media sent: {total_sent}."
    if total_failed:
        done += f"\nFailed: {total_failed}."
    await callback.message.answer(
        done, reply_markup=after_payment_kb(str(stuffs[0]["_id"]))
    )


@router.callback_query(F.data.startswith("abuy:"))
async def cb_buy(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    raw_ids = callback.data.split(":", 1)[1]
    if "," in raw_ids:
        stuffs = await _fetch_stuffs(raw_ids)
        if not stuffs:
            await callback.answer("Stuffs deleted", show_alert=True)
            return
        await callback.answer()
        await _show_multi_payment_methods(callback, stuffs, state)
        return
    stuff = await get_stuff(raw_ids)
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return

    user = await ensure_user(callback.from_user)
    if user_has_access(user, stuff):
        await callback.answer("You already have access", show_alert=True)
        return
    if stuff.get("price", 0) <= 0:
        await callback.answer("This stuff is free", show_alert=True)
        return

    await callback.answer()
    await _show_payment_methods(callback, stuff, state)


@router.callback_query(F.data.startswith("pm:"))
async def cb_payment_method(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, method_id, raw_ids = callback.data.split(":")
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return

    method = await get_method(method_id)
    stuffs = await _fetch_stuffs(raw_ids)
    if method is None or not method.get("is_active"):
        await callback.answer("This method is unavailable", show_alert=True)
        return
    if not stuffs:
        await callback.answer("Stuffs deleted", show_alert=True)
        return
    if not await _forcejoin_gate(callback):
        return

    first_id = str(stuffs[0]["_id"])
    data = await state.get_data()
    coupon = await _coupon_from_state(state)
    total = _total_price(stuffs)
    paid = _pay_total(data, total) if coupon else total
    joined_name = " + ".join(s.get("name", "Stuff") for s in stuffs)
    await callback.answer()

    if method.get("kind") == "stars":
        amount = max(1, int(paid))
        await callback.bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=joined_name[:255],
            description=(
                stuffs[0].get("description") or "Stuffs purchase"
            )[:255],
            payload=f"stuff_{raw_ids}",
            currency="XTR",
            provider_token="",
            prices=[LabeledPrice(label=joined_name[:32], amount=amount)],
            reply_markup=invoice_kb(first_id),
            protect_content=await is_protect_content(),
        )
        return

    amount_text = _price_text(paid)
    coupon_line = ""
    if coupon and paid != total:
        saved = features.coupon_discount(total, coupon)
        coupon_line = (
            f"\nCoupon <code>{html.escape(str(coupon.get('code')))}</code>: "
            f"−Rs.{saved:g}"
        )

    if method.get("kind") == "qr" and method.get("qr_file_id"):
        caption = f"<b>{html.escape(method['name'])}</b>"
        if method.get("instructions"):
            caption += f"\n{html.escape(method['instructions'])}"
        caption += f"\n\nAmount: <b>{amount_text}</b>"
        caption += coupon_line
        caption += (
            "\n\nAfter paying, press <b>“I have paid”</b> "
            "and send your payment screenshot."
        )
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
        await callback.bot.send_photo(
            callback.message.chat.id,
            method["qr_file_id"],
            caption=caption[:1024],
            reply_markup=method_pay_kb(raw_ids, str(method["_id"])),
        )
        return

    text = f"<b>{html.escape(method['name'])}</b>"
    if method.get("instructions"):
        text += f"\n{html.escape(method['instructions'])}"
    if method.get("detail"):
        text += f"\n\nAddress:\n<code>{html.escape(method['detail'])}</code>"
    text += f"\n\nAmount: <b>{amount_text}</b>"
    text += coupon_line
    text += (
        "\n\nAfter paying, press <b>“I have paid”</b> "
        "and send your payment screenshot."
    )

    kb = method_pay_kb(raw_ids, str(method["_id"]))
    await _edit_or_send(callback.message, text, kb)


@router.callback_query(F.data.startswith("paydone:"))
async def cb_paid(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        _, method_id, raw_ids = callback.data.split(":")
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return

    await state.set_state(PaymentStates.waiting_screenshot)
    await state.update_data(method_id=method_id, stuff_id=raw_ids)
    await callback.answer()

    text = (
        "<b>Send your payment screenshot</b>\n\n"
        "Send a photo (screenshot) of the successful payment.\n"
        "Stuffs are delivered instantly after approval."
    )
    await _edit_or_send(callback.message, text, screenshot_prompt_kb(raw_ids))


@router.callback_query(F.data.startswith("paycancel:"))
async def cb_pay_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    raw_ids = callback.data.split(":", 1)[1]
    await state.clear()
    await callback.answer("Payment cancelled")
    text = "Payment cancelled.\n\nYou can try again any time."
    first_id = _parse_ids(raw_ids)
    back_id = first_id[0] if first_id else ""
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[ib("Back", f"sstuf:{back_id}", "primary")]]
    )
    await _edit_or_send(callback.message, text, markup)


@router.message(PaymentStates.waiting_screenshot, F.text)
async def screenshot_waiting_text(message: Message) -> None:
    await message.answer(
        "Please send a <b>photo</b> of your payment screenshot, "
        "or press “Cancel payment”."
    )


@router.message(PaymentStates.waiting_screenshot, F.photo)
async def on_screenshot(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw_ids = data.get("stuff_id") or ""
    method_id = data.get("method_id")
    file_id = message.photo[-1].file_id

    if data.get("stars_proof"):
        await _handle_stars_proof(message, state, data, file_id)
        return

    stuffs = await _fetch_stuffs(raw_ids)
    method = await get_method(method_id) if method_id else None
    if method is None:
        method = {"name": "Unknown", "kind": "crypto"}

    total = _total_price(stuffs)
    paid = _pay_total(data, total)
    coupon_code = None
    coupon = data.get("coupon")
    if isinstance(coupon, dict) and paid != total:
        coupon_code = str(coupon.get("code"))
        await features.bump_coupon_usage(coupon_code)

    request = await create_payment_request(
        user_tg_id=message.from_user.id,
        user_username=message.from_user.username,
        user_full_name=message.from_user.full_name,
        stuffs=stuffs,
        method=method,
        amount_override=paid if coupon_code else None,
        coupon=coupon_code,
    )
    await attach_screenshot(request["_id"], file_id)
    await state.clear()

    if await features.is_on("autoapprove"):
        limit = await features.get_autoapprove_max()
        try:
            amount = float(request.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if limit > 0 and amount <= limit:
            await _auto_approve_request(message, request, stuffs)
            return

    user_line = (
        f"@{html.escape(message.from_user.username)}"
        if message.from_user.username
        else "(no username)"
    )
    user_line = f"{user_line} (id <code>{message.from_user.id}</code>)"
    caption = (
        "<b>New payment claim</b>\n\n"
        f"User: {user_line}\n"
        f"Stuffs: {html.escape(request.get('stuff_name') or '?')} "
        f"({_price_text(request.get('amount', 0))})\n"
        f"Method: {html.escape(request.get('method_name') or '?')}\n"
        "Status: pending review\n"
        f"Order ID: {request['_id']}"
    )

    sent = await notify_log_group(
        message.bot,
        photo_file_id=file_id,
        caption=caption[:1024],
        reply_markup=review_request_kb(str(request["_id"])),
    )
    if sent is not None:
        await save_request_log_ids(request["_id"], sent.chat.id, sent.message_id)

    await message.answer(
        "<b>Screenshot received!</b>\n\n"
        "Your payment is awaiting review. Stuffs will be delivered "
        "to you instantly after approval.",
        reply_markup=menu_button_kb(),
    )


async def _auto_approve_request(
    message: Message, request: dict, stuffs: list[dict]
) -> None:
    """Approve and deliver a claim that is under the auto-approve limit."""
    for stuff in stuffs:
        await grant_access(message.from_user.id, str(stuff["_id"]))
    await set_request_status(request["_id"], "approved")
    await features.audit(
        f"Auto-approved: Rs.{request.get('amount', '?')} from "
        f"{request.get('user_tg_id')}"
    )
    await notify_log_group(
        message.bot,
        text=build_transaction_text(request, "Auto-approved"),
    )
    await message.answer(
        "✅ <b>Payment auto-approved!</b>\n\n"
        f"Delivering “{html.escape(request.get('stuff_name') or 'your stuff')}”…",
        reply_markup=menu_button_kb(),
    )
    if await features.is_on("receipts"):
        try:
            await message.answer(features.receipt_text(request))
        except TelegramAPIError:
            logger.exception("Receipt send failed")
    total_sent = 0
    total_failed = 0
    for stuff in stuffs:
        sent, failed = await deliver_stuff(message.bot, message.chat.id, stuff)
        total_sent += sent
        total_failed += failed
    if stuffs:
        done = f"<b>Delivered!</b> Media sent: {total_sent}."
        if total_failed:
            done += f"\nFailed: {total_failed}."
        await message.answer(
            done, reply_markup=after_payment_kb(str(stuffs[0]["_id"]))
        )


# ---------------------------------------------------------------- stars


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True, error_message="Payment failed, please try again later.")


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, state: FSMContext) -> None:
    payment = message.successful_payment
    payload = payment.invoice_payload

    if not payload.startswith("stuff_"):
        await message.answer("Payment received.")
        return

    raw_ids = payload.split("_", 1)[1]
    stuffs = await _fetch_stuffs(raw_ids)

    methods = await get_active_methods()
    stars_method = next((m for m in methods if m.get("kind") == "stars"), None)
    if stars_method is None:
        stars_method = {"name": "Telegram Stars", "kind": "stars"}
    receiver = str(stars_method.get("username") or "").strip().lstrip("@")

    name = " + ".join(s.get("name", "stuff") for s in stuffs) or "stuff"
    stars = payment.total_amount

    state_data = await state.get_data()
    full_total = _total_price(stuffs)
    paid_total = _pay_total(state_data, full_total)
    coupon_code = None
    if isinstance(state_data.get("coupon"), dict) and paid_total != full_total:
        coupon_code = str(state_data["coupon"].get("code"))
        await features.bump_coupon_usage(coupon_code)
    override = float(stars) if coupon_code else None

    if receiver:
        request = await create_payment_request(
            user_tg_id=message.from_user.id,
            user_username=message.from_user.username,
            user_full_name=message.from_user.full_name,
            stuffs=stuffs,
            method=stars_method,
            amount_override=override,
            coupon=coupon_code,
        )
        await notify_log_group(
            message.bot,
            text=build_transaction_text(request, "Awaiting buyer proof"),
            reply_markup=review_request_kb(str(request["_id"])),
        )
        await state.set_state(PaymentStates.waiting_screenshot)
        await state.update_data(
            stars_proof=True,
            request_id=str(request["_id"]),
            stuff_id=raw_ids,
            method_id=str(stars_method.get("_id") or ""),
        )
        await message.answer(
            f"<b>Payment successful!</b>\n\n"
            f"Charged: {stars} ⭐ for “{html.escape(name)}”.\n\n"
            f"<b>Important:</b> open @{html.escape(receiver)} in Telegram, "
            f"press <b>START</b> and send a screenshot of that chat here.\n"
            f"Your stuff is delivered right after the screenshot is reviewed.",
            reply_markup=screenshot_prompt_kb(raw_ids),
        )
        return

    for stuff in stuffs:
        await grant_access(message.from_user.id, str(stuff["_id"]))

    request = await create_payment_request(
        user_tg_id=message.from_user.id,
        user_username=message.from_user.username,
        user_full_name=message.from_user.full_name,
        stuffs=stuffs,
        method=stars_method,
        amount_override=override,
        coupon=coupon_code,
    )
    await set_request_status(request["_id"], "approved")

    await notify_log_group(
        message.bot,
        text=build_transaction_text(request, "Delivered"),
    )
    if await features.is_on("receipts"):
        try:
            await message.answer(features.receipt_text(request))
        except TelegramAPIError:
            logger.exception("Receipt send failed")

    await message.answer(
        f"<b>Payment successful!</b>\n\n"
        f"Delivering “{html.escape(name)}”…\nCharged: {stars} ⭐",
        reply_markup=menu_button_kb(),
    )
    total_sent = 0
    total_failed = 0
    for stuff in stuffs:
        sent, failed = await deliver_stuff(message.bot, message.chat.id, stuff)
        total_sent += sent
        total_failed += failed
    if stuffs:
        done = f"<b>Delivered!</b> Media sent: {total_sent}."
        if total_failed:
            done += f"\nFailed: {total_failed}."
        await message.answer(
            done, reply_markup=after_payment_kb(str(stuffs[0]["_id"]))
        )


async def _handle_stars_proof(
    message: Message, state: FSMContext, data: dict, file_id: str
) -> None:
    request_id = data.get("request_id")
    await state.clear()
    if not request_id:
        await message.answer("Screenshot received.")
        return

    from utils.payments import get_payment_request

    request = await get_payment_request(request_id)
    if request is None:
        await message.answer("Screenshot received.")
        return

    await attach_screenshot(request_id, file_id)

    if request.get("status") == "approved":
        await message.answer(
            "Screenshot received. Your stuff was already delivered.",
            reply_markup=menu_button_kb(),
        )
        return

    caption = (
        "<b>Stars payment — buyer proof</b>\n\n"
        f"User: @{html.escape(message.from_user.username or '?')} "
        f"(id <code>{message.from_user.id}</code>)\n"
        f"Stuffs: {html.escape(request.get('stuff_name') or '?')}\n"
        f"Amount: {_price_text(request.get('amount', 0))} ⭐\n"
        "Status: pending review\n"
        f"Order ID: {request['_id']}"
    )
    sent = await notify_log_group(
        message.bot,
        photo_file_id=file_id,
        caption=caption[:1024],
        reply_markup=review_request_kb(str(request["_id"])),
    )
    if sent is not None:
        await save_request_log_ids(request_id, sent.chat.id, sent.message_id)

    await message.answer(
        "<b>Screenshot received!</b>\n\n"
        "Your payment is awaiting review. Stuffs will be delivered "
        "to you right after approval.",
        reply_markup=menu_button_kb(),
    )


# ---------------------------------------------------------------- features


class _MsgAsCallback:
    """Shim so payment renderers can be reused from plain messages."""

    def __init__(self, message: Message) -> None:
        self.message = message
        self.from_user = message.from_user


async def _methods_fresh(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cids = str(data.get("cids") or "")
    stuffs = await _fetch_stuffs(cids)
    if not stuffs:
        await message.answer(
            "These stuffs are no longer available.",
            reply_markup=menu_button_kb(),
        )
        return
    shim = _MsgAsCallback(message)
    if data.get("cmode") == "multi" or "," in cids:
        await _show_multi_payment_methods(shim, stuffs, state)
    else:
        await _show_payment_methods(shim, stuffs[0], state)


async def _rerender_methods(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    cids = str(data.get("cids") or "")
    stuffs = await _fetch_stuffs(cids)
    if not stuffs:
        await _edit_or_send(
            callback.message,
            "These stuffs are no longer available.",
            menu_button_kb(),
        )
        return
    if data.get("cmode") == "multi" or "," in cids:
        await _show_multi_payment_methods(callback, stuffs, state)
    else:
        await _show_payment_methods(callback, stuffs[0], state)


@router.callback_query(F.data == "scoupon")
async def cb_coupon_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await features.is_on("coupons"):
        await callback.answer("Coupons are disabled", show_alert=True)
        return
    data = await state.get_data()
    if data.get("coupon"):
        await state.update_data(coupon=None)
        await callback.answer("Coupon removed")
        await _rerender_methods(callback, state)
        return
    await state.set_state(UserStates.coupon_code)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "🎟 <b>Apply coupon</b>\n\nSend your coupon code:",
        coupon_entry_kb(),
    )


@router.callback_query(F.data == "scouponback")
async def cb_coupon_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _rerender_methods(callback, state)


@router.message(UserStates.coupon_code, F.text)
async def coupon_code_receive(message: Message, state: FSMContext) -> None:
    if not await features.is_on("coupons"):
        await state.clear()
        await message.answer("Coupons are disabled.", reply_markup=menu_button_kb())
        return
    code = (message.text or "").strip()
    coupon = await features.find_coupon(code)
    if coupon is None:
        await message.answer("That coupon does not exist. Check the code:")
        return
    if not features.coupon_applies(coupon):
        await message.answer("That coupon is no longer valid. Send another code:")
        return
    await state.update_data(coupon=dict(coupon))
    await state.set_state(None)
    data = await state.get_data()
    stuffs = await _fetch_stuffs(str(data.get("cids") or ""))
    total = _total_price(stuffs)
    saved = features.coupon_discount(total, coupon)
    await message.answer(
        f"✅ Coupon <code>{html.escape(str(coupon.get('code')))}</code> applied — "
        f"you save <b>Rs.{saved:g}</b>.\n\n"
        "Tap a payment method below:"
    )
    await _methods_fresh(message, state)


# ---------------------------------------------------------------- check-in


@router.callback_query(F.data == "scheckin")
async def cb_checkin(callback: CallbackQuery) -> None:
    if not await features.is_on("checkin"):
        await callback.answer("Check-in is disabled", show_alert=True)
        return
    today = datetime.now().strftime("%Y-%m-%d")
    claimed = await features.get_checkin(callback.from_user.id) == today
    bonus = await features.get_checkin_bonus()
    await callback.answer()
    text = (
        f"📅 <b>Daily check-in</b>\n\n"
        f"Bonus: <b>Rs.{bonus:g}</b>\n\n"
        + ("You already claimed today — come back tomorrow!"
           if claimed else "Tap <b>Claim bonus</b> to collect.")
    )
    await _edit_or_send(callback.message, text, checkin_kb(claimed))


@router.callback_query(F.data == "schecknone")
async def cb_checkin_none(callback: CallbackQuery) -> None:
    await callback.answer("Come back tomorrow")


@router.callback_query(F.data == "scheckok")
async def cb_checkin_claim(callback: CallbackQuery) -> None:
    if not await features.is_on("checkin"):
        await callback.answer("Check-in is disabled", show_alert=True)
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if await features.get_checkin(callback.from_user.id) == today:
        await callback.answer("Already claimed today", show_alert=True)
        return
    bonus = await features.get_checkin_bonus()
    await features.set_checkin(callback.from_user.id, today)
    await add_balance(callback.from_user.id, bonus)
    await callback.answer(f"Claimed Rs.{bonus:g}")
    await _edit_or_send(
        callback.message,
        f"✅ <b>Check-in claimed</b>\n\nRs.{bonus:g} added to your balance.",
        checkin_kb(True),
    )


# --------------------------------------------------------------- wishlist


@router.callback_query(F.data == "swishlist")
async def cb_wishlist(callback: CallbackQuery) -> None:
    if not await features.is_on("wishlist"):
        await callback.answer("Wishlist is disabled", show_alert=True)
        return
    ids = await features.get_wishlist(callback.from_user.id)
    stuffs = []
    for stuff_id in ids:
        doc = await get_stuff(stuff_id)
        if doc is not None:
            stuffs.append(doc)
    await callback.answer()
    if not stuffs:
        await _edit_or_send(
            callback.message,
            "💛 <b>Wishlist</b>\n\nYour wishlist is empty. "
            "Open a stuff and press Wishlist +.",
            menu_button_kb(),
        )
        return
    await _edit_or_send(
        callback.message,
        f"💛 <b>Wishlist</b> ({len(stuffs)})",
        wishlist_kb(stuffs),
    )


@router.callback_query(F.data.startswith("wish:"))
async def cb_wish_toggle(callback: CallbackQuery) -> None:
    if not await features.is_on("wishlist"):
        await callback.answer("Wishlist is disabled", show_alert=True)
        return
    stuff_id = callback.data.split(":", 1)[1]
    added = await features.toggle_wishlist(callback.from_user.id, stuff_id)
    await callback.answer("Added to wishlist" if added else "Removed from wishlist")
    await _render_stuff_page(callback, stuff_id, answer=False)


# ---------------------------------------------------------------- history


@router.callback_query(F.data == "shistory")
async def cb_history(callback: CallbackQuery) -> None:
    if not await features.is_on("history"):
        await callback.answer("History is disabled", show_alert=True)
        return
    rows = await features.purchase_history(callback.from_user.id)
    names = {str(s["_id"]): str(s.get("name", "?")) for s in await list_stuffs()}
    await callback.answer()
    await _edit_or_send(
        callback.message,
        features.history_text(rows, names),
        history_kb(),
    )


# ---------------------------------------------------------------- reviews


@router.callback_query(F.data.startswith("srate:"))
async def cb_rate_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await features.is_on("reviews"):
        await callback.answer("Reviews are disabled", show_alert=True)
        return
    stuff_id = callback.data.split(":", 1)[1]
    await state.update_data(rate_stuff=str(stuff_id))
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "⭐ <b>Rate this stuff</b>\n\nHow many stars?",
        rate_kb(str(stuff_id)),
    )


@router.callback_query(F.data.startswith("rvs:"))
async def cb_rate_stars(callback: CallbackQuery, state: FSMContext) -> None:
    if not await features.is_on("reviews"):
        await callback.answer("Reviews are disabled", show_alert=True)
        return
    try:
        _, stuff_id, stars_raw = callback.data.split(":")
        stars = int(stars_raw)
    except ValueError:
        await callback.answer("Invalid request", show_alert=True)
        return
    await state.update_data(rate_stuff=str(stuff_id), rate_stars=stars)
    await state.set_state(UserStates.rate_text)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        f"⭐ You chose <b>{stars}</b> star(s).\n\n"
        "Send a short review text (or send <code>/skip</code>):",
        menu_button_kb(),
    )


@router.message(UserStates.rate_text, F.text)
async def rate_text_receive(message: Message, state: FSMContext) -> None:
    if not await features.is_on("reviews"):
        await state.clear()
        await message.answer("Reviews are disabled.", reply_markup=menu_button_kb())
        return
    data = await state.get_data()
    stuff_id = str(data.get("rate_stuff") or "")
    stars = int(data.get("rate_stars") or 5)
    text = (message.text or "").strip()
    if text.lower() in ("/skip", "skip"):
        text = ""
    if not stuff_id:
        await state.clear()
        await message.answer("Session expired.", reply_markup=menu_button_kb())
        return
    await features.add_review(stuff_id, message.from_user.id, stars, text)
    await features.audit(
        f"Review submitted: {stars} star(s) for stuff {stuff_id}"
    )
    await state.clear()
    await message.answer(
        "✅ Thanks! Your review is waiting for approval.",
        reply_markup=menu_button_kb(),
    )


# ---------------------------------------------------------------- support


@router.callback_query(F.data == "sticket")
async def cb_support_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await features.is_on("support"):
        await callback.answer("Support is disabled", show_alert=True)
        return
    await state.set_state(UserStates.support_text)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "💬 <b>Support</b>\n\n"
        "Write your message and send it — the admin will reply "
        "here in the bot.",
        menu_button_kb(),
    )


@router.message(UserStates.support_text, F.text)
async def support_receive(message: Message, state: FSMContext) -> None:
    if not await features.is_on("support"):
        await state.clear()
        await message.answer("Support is disabled.", reply_markup=menu_button_kb())
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Send a message:")
        return
    await features.add_ticket(message.from_user, text)
    await features.audit(f"Support ticket from {message.from_user.id}")
    await state.clear()
    await message.answer(
        "✅ Ticket created! The admin will reply here.",
        reply_markup=menu_button_kb(),
    )


# ------------------------------------------------------------------ search


@router.callback_query(F.data == "ssearch")
async def cb_search_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await features.is_on("search"):
        await callback.answer("Search is disabled", show_alert=True)
        return
    await state.set_state(UserStates.search_query)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "🔍 <b>Search</b>\n\nSend a keyword to find stuffs:",
        menu_button_kb(),
    )


@router.message(UserStates.search_query, F.text)
async def search_receive(message: Message, state: FSMContext) -> None:
    if not await features.is_on("search"):
        await state.clear()
        await message.answer("Search is disabled.", reply_markup=menu_button_kb())
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Send a keyword to search:")
        return
    matches = features.search_stuffs(await list_stuffs(), query)
    await state.clear()
    if not matches:
        await message.answer(
            f"No stuffs match “{html.escape(query)}”.",
            reply_markup=menu_button_kb(),
        )
        return
    admin = await is_admin(message.from_user.id)
    await message.answer(
        f"🔍 Results for <b>{html.escape(query)}</b> ({len(matches)}):",
        reply_markup=catalog_kb(matches, admin=admin),
    )


# --------------------------------------------------------------- gift codes


@router.callback_query(F.data == "sgift")
async def cb_gift_ask(callback: CallbackQuery, state: FSMContext) -> None:
    if not await features.is_on("giftcodes"):
        await callback.answer("Gift codes are disabled", show_alert=True)
        return
    await state.set_state(UserStates.gift_code)
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "🎁 <b>Gift code</b>\n\nSend the code to redeem it for wallet balance:",
        menu_button_kb(),
    )


@router.message(UserStates.gift_code, F.text)
async def gift_redeem(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    await state.clear()
    if not await features.is_on("giftcodes"):
        await message.answer(
            "Gift codes are disabled.", reply_markup=menu_button_kb()
        )
        return
    ok, amount, reason = await features.redeem_gift(code, message.from_user.id)
    if ok:
        await message.answer(
            f"✅ Gift code redeemed!\n\nRs.{amount:g} added to your balance.",
            reply_markup=menu_button_kb(),
        )
        return
    texts = {
        "used": "That code was already used.",
        "inactive": "That code is currently disabled.",
        "unknown": "Invalid code.",
    }
    await message.answer(
        texts.get(reason, "Invalid code."), reply_markup=menu_button_kb()
    )


# --------------------------------------------------------------- forcejoin


@router.callback_query(F.data == "fjcheck")
async def cb_forcejoin_check(callback: CallbackQuery) -> None:
    allowed, _channel = await features.forcejoin_allowed(
        callback.bot, callback.from_user.id
    )
    if allowed:
        await callback.answer(
            "Joined — tap the button again to continue", show_alert=True
        )
    else:
        await callback.answer("You have not joined yet", show_alert=True)


# ---------------------------------------------------------------- fallback


@router.message(F.text)
async def fallback(message: Message) -> None:
    if message.from_user and await is_admin(message.from_user.id):
        return
    if await _maintenance_gate(message):
        return
    if await features.is_on("faq"):
        answer = features.faq_answer(
            await features.get_faq(), message.text or ""
        )
        if answer:
            await message.answer(answer, reply_markup=menu_button_kb())
            return
    await message.answer(
        "Please use the buttons below the message.",
        reply_markup=menu_button_kb(),
    )
