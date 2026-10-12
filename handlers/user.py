import html
import logging
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

from database.store import deduct_balance, get_stuff, list_stuffs
from keyboards.inline import (
    after_payment_kb,
    bdirect_kb,
    catalog_kb,
    demo_list_kb,
    demo_view_kb,
    ib,
    invoice_kb,
    main_menu_kb,
    menu_button_kb,
    method_pay_kb,
    multi_payment_methods_kb,
    payment_methods_kb,
    player_kb,
    proof_gallery_kb,
    review_request_kb,
    screenshot_prompt_kb,
    stuff_user_kb,
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
    markup = main_menu_kb(admin)
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
    markup = main_menu_kb(admin)
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


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    try:
        await ensure_user(message.from_user)
    except Exception:
        logger.exception("ensure_user failed on /start")
    await _show_menu(message)


@router.callback_query(F.data == "smenu")
async def cb_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
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
    await callback.answer()
    await _edit_or_send(
        callback.message,
        "<b>Stuffs</b>\n\nChoose a stuff:",
        catalog_kb(items, admin=admin),
    )


@router.message(Command("catalog"))
async def cmd_catalog(message: Message) -> None:
    items = await list_stuffs()
    admin = await is_admin(message.from_user.id)
    if not items:
        await message.answer("No stuffs yet.")
        return
    await message.answer(
        "<b>Stuffs</b>\n\nChoose a stuff:",
        reply_markup=catalog_kb(items, admin=admin),
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


@router.callback_query(F.data.startswith("sstuf:"))
async def cb_stuff(callback: CallbackQuery) -> None:
    stuff_id = callback.data.split(":", 1)[1]
    stuff = await get_stuff(stuff_id)
    if stuff is None:
        await callback.answer("Stuffs deleted", show_alert=True)
        return

    user = await ensure_user(callback.from_user)
    has_access = user_has_access(user, stuff)
    await callback.answer()
    text = _stuff_info_text(stuff, has_access)
    markup = stuff_user_kb(stuff, has_access)
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

    await callback.answer()
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
        await _show_payment_methods(callback, stuffs[0])
        return
    await callback.answer()
    await _show_multi_payment_methods(callback, stuffs)


# ---------------------------------------------------------------- payments


async def _show_payment_methods(callback: CallbackQuery, stuff: dict) -> None:
    methods = await get_active_methods()
    user = await ensure_user(callback.from_user)
    balance = float(user.get("balance", 0) or 0)
    price = float(stuff.get("price", 0))
    can_pay_balance = price > 0 and balance >= price

    if not methods and not can_pay_balance:
        await _edit_or_send(
            callback.message,
            "No payment methods are configured yet. Please contact the administrator.",
            menu_button_kb(),
        )
        return

    text = _stuff_info_text(stuff, has_access=False)
    if balance > 0:
        text += f"\nYour balance: Rs.{balance:g}"
    text += "\n\n<b>Choose a payment method:</b>"

    rows = []
    if can_pay_balance:
        rows.append(
            [
                ib(
                    f"Pay from balance (Rs.{price:g})",
                    f"pbal:{stuff['_id']}",
                    "success",
                )
            ]
        )
    if methods:
        rows.extend(payment_methods_kb(str(stuff["_id"]), methods).inline_keyboard)
    rows.append([ib("Back", f"sstuf:{stuff['_id']}", "primary")])
    await _edit_or_send(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def _show_multi_payment_methods(
    callback: CallbackQuery, stuffs: list[dict]
) -> None:
    methods = await get_active_methods()
    user = await ensure_user(callback.from_user)
    balance = float(user.get("balance", 0) or 0)
    total = _total_price(stuffs)
    can_pay_balance = total > 0 and balance >= total
    ids_raw = ",".join(str(s["_id"]) for s in stuffs)

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
    if balance > 0:
        text += f"\nYour balance: Rs.{balance:g}"
    text += "\n\n<b>Choose a payment method:</b>"

    markup = multi_payment_methods_kb(
        ids_raw,
        methods,
        back_cb="bdirect",
        can_pay_balance=can_pay_balance,
        balance_total=total,
    )
    await _edit_or_send(callback.message, text, markup)


@router.callback_query(F.data.startswith("pbal:"))
async def cb_pay_from_balance(callback: CallbackQuery, state: FSMContext) -> None:
    raw_ids = callback.data.split(":", 1)[1]
    stuffs = await _fetch_stuffs(raw_ids)
    if not stuffs:
        await callback.answer("Stuffs deleted", show_alert=True)
        return

    user = await ensure_user(callback.from_user)
    price = _total_price(stuffs)
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

    await state.clear()
    for stuff in stuffs:
        await grant_access(callback.from_user.id, str(stuff["_id"]))

    method = {"name": "Wallet balance", "kind": "wallet"}
    request = await create_payment_request(
        user_tg_id=callback.from_user.id,
        user_username=callback.from_user.username,
        user_full_name=callback.from_user.full_name,
        stuffs=stuffs,
        method=method,
    )
    await set_request_status(request["_id"], "approved")
    await notify_log_group(
        callback.bot,
        text=build_transaction_text(request, "Delivered"),
    )

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
        await _show_multi_payment_methods(callback, stuffs)
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
    await _show_payment_methods(callback, stuff)


@router.callback_query(F.data.startswith("pm:"))
async def cb_payment_method(callback: CallbackQuery) -> None:
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

    first_id = str(stuffs[0]["_id"])
    total = _total_price(stuffs)
    joined_name = " + ".join(s.get("name", "Stuff") for s in stuffs)
    await callback.answer()

    if method.get("kind") == "stars":
        amount = max(1, int(total))
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

    amount_text = _price_text(total)

    if method.get("kind") == "qr" and method.get("qr_file_id"):
        caption = f"<b>{html.escape(method['name'])}</b>"
        if method.get("instructions"):
            caption += f"\n{html.escape(method['instructions'])}"
        caption += f"\n\nAmount: <b>{amount_text}</b>"
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

    request = await create_payment_request(
        user_tg_id=message.from_user.id,
        user_username=message.from_user.username,
        user_full_name=message.from_user.full_name,
        stuffs=stuffs,
        method=method,
    )
    await attach_screenshot(request["_id"], file_id)
    await state.clear()

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

    if receiver:
        request = await create_payment_request(
            user_tg_id=message.from_user.id,
            user_username=message.from_user.username,
            user_full_name=message.from_user.full_name,
            stuffs=stuffs,
            method=stars_method,
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
    )
    await set_request_status(request["_id"], "approved")

    await notify_log_group(
        message.bot,
        text=build_transaction_text(request, "Delivered"),
    )

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


# ---------------------------------------------------------------- fallback


@router.message(F.text)
async def fallback(message: Message) -> None:
    if message.from_user and await is_admin(message.from_user.id):
        return
    await message.answer(
        "Please use the buttons below the message.",
        reply_markup=menu_button_kb(),
    )
