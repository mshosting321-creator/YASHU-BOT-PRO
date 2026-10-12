from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)


def ib(text: str, callback_data: str, style: str | None = None) -> InlineKeyboardButton:
    kwargs: dict = {"text": text, "callback_data": callback_data}
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def main_menu_kb(admin: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            ib("VIEW DEMO", "vdemo", "success"),
            ib("BUY DIRECTLY", "bdirect", "success"),
        ],
        [ib("Stuffs", "scatalog", "primary")],
        [
            ib("My Stuffs", "smy", "primary"),
            ib("Help", "shelp", "primary"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu_kb(admin: bool = False) -> InlineKeyboardMarkup:
    return main_menu_kb(admin)


def catalog_kb(stuffs: list[dict], admin: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for stuff in stuffs:
        price = stuff.get("price", 0)
        label = (
            f"{stuff['name']} — Rs.{int(price)}"
            if price > 0
            else f"{stuff['name']} — free"
        )
        rows.append([ib(label[:64], f"sstuf:{stuff['_id']}", "success")])
    rows.append([ib("Menu", "smenu", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stuff_user_kb(stuff: dict, has_access: bool) -> InlineKeyboardMarkup:
    lessons = stuff.get("lessons", [])
    rows: list[list[InlineKeyboardButton]] = []
    for index, lesson in enumerate(lessons):
        locked = stuff.get("price", 0) > 0 and not has_access and not lesson.get("is_demo")
        if locked:
            mark = "Locked"
        elif lesson.get("is_demo") and not has_access:
            mark = "Demo"
        else:
            mark = "Play"
        title = lesson.get("title") or f"Stuff {index + 1}"
        if len(title) > 26:
            title = title[:24] + "…"
        style = "danger" if locked else "primary"
        rows.append([ib(f"{mark} {index + 1}. {title}", f"ales:{stuff['_id']}:{index}", style)])

    if stuff.get("price", 0) > 0 and not has_access:
        rows.append(
            [
                ib(
                    f"Buy — Rs.{int(stuff['price'])}",
                    f"abuy:{stuff['_id']}",
                    "success",
                )
            ]
        )
    extras: list[InlineKeyboardButton] = []
    demo_count = sum(1 for lesson in lessons if lesson.get("is_demo"))
    if demo_count and not has_access:
        extras.append(ib("View demo", f"vdl:{stuff['_id']}", "success"))
    if stuff.get("proofs"):
        extras.append(
            ib(f"Proofs ({len(stuff['proofs'])})", f"proofs:{stuff['_id']}:0", "primary")
        )
    if extras:
        rows.append(extras)
    rows.append([ib("Stuffs", "scatalog", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def player_kb(stuff_id: str, pos: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if pos > 0:
        nav.append(ib("Back", f"ales:{stuff_id}:{pos - 1}", "primary"))
    if pos < total - 1:
        nav.append(ib("Next", f"ales:{stuff_id}:{pos + 1}", "primary"))
    if nav:
        rows.append(nav)
    rows.append([ib("Stuff page", f"sstuf:{stuff_id}", "success")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_methods_kb(
    stuff_id: str, methods: list[dict], back_cb: str | None = None
) -> InlineKeyboardMarkup:
    rows = []
    for method in methods:
        rows.append(
            [ib(method["name"][:50], f"pm:{method['_id']}:{stuff_id}", "success")]
        )
    rows.append([ib("Back", back_cb or f"sstuf:{stuff_id}", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def multi_payment_methods_kb(
    stuff_ids: str,
    methods: list[dict],
    back_cb: str,
    can_pay_balance: bool = False,
    balance_total: float = 0.0,
) -> InlineKeyboardMarkup:
    rows = []
    if can_pay_balance:
        rows.append(
            [
                ib(
                    f"Pay from balance (Rs.{balance_total:g})",
                    f"pbal:{stuff_ids}",
                    "success",
                )
            ]
        )
    for method in methods:
        rows.append(
            [ib(method["name"][:50], f"pm:{method['_id']}:{stuff_ids}", "success")]
        )
    rows.append([ib("Back", back_cb, "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def method_pay_kb(stuff_id: str, method_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("I have paid", f"paydone:{method_id}:{stuff_id}", "success")],
            [
                ib("Cancel", f"paycancel:{stuff_id}", "danger"),
                ib("Back", f"abuy:{stuff_id}", "primary"),
            ],
        ]
    )


def screenshot_prompt_kb(stuff_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Cancel payment", f"paycancel:{stuff_id}", "danger")]]
    )


def protect_kb(enabled: bool) -> InlineKeyboardMarkup:
    label = "Protection: ON" if enabled else "Protection: OFF"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib(label, "aprot", "success" if enabled else "danger")],
            [ib("Panel", "aback", "primary")],
        ]
    )


def invoice_kb(stuff_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Back", f"sstuf:{stuff_id}", "primary")]]
    )


def after_payment_kb(stuff_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Open stuff", f"sstuf:{stuff_id}", "success")]]
    )


def menu_button_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Menu", "smenu", "primary")]]
    )


# ---------------------------------------------------------------- admin


def admin_stuffs_kb(stuffs: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for stuff in stuffs:
        price = stuff.get("price", 0)
        price_text = f"Rs.{int(price)}" if price > 0 else "free"
        rows.append(
            [ib(f"{stuff['name']} — {price_text}"[:64], f"sadm:{stuff['_id']}", "primary")]
        )
    rows.append([ib("Add stuff", "apnl:add", "success")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_stuff_view_kb(stuff_id: str, lesson_count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Add videos", f"saddl:{stuff_id}", "success"),
                ib("Demo list", f"sadml:{stuff_id}", "primary"),
            ],
            [
                ib("Rename", f"sren:{stuff_id}", "primary"),
                ib("Price", f"sprice:{stuff_id}", "primary"),
            ],
            [
                ib("Showcase", f"sshow:{stuff_id}", "primary"),
                ib("Proofs", f"sproof:{stuff_id}", "primary"),
            ],
            [ib("Preview", f"sstuf:{stuff_id}", "primary")],
            [
                ib("Receive direct", f"sget:{stuff_id}", "success"),
                ib("Download ZIP", f"szipdl:{stuff_id}", "success"),
            ],
            [
                ib("Delete", f"sdel:{stuff_id}", "danger"),
                ib("Stuffs", "sadmin:stuffs", "primary"),
            ],
        ]
    )


def clmode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Same clone (copy all stuffs)", "clmode:copy", "success")],
            [ib("New database (empty)", "clmode:new", "success")],
            [ib("Cancel", "clcancel", "primary")],
        ]
    )


def clones_kb(entries: list[dict], running_tags: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for entry in entries:
        tag = str(entry.get("tag", ""))
        name = f"@{entry.get('username') or tag}" if entry.get("username") else tag
        mode = "copy" if entry.get("mode") == "copy" else "new"
        status = "running" if tag in running_tags else "stopped"
        rows.append([ib(f"{name} [{mode}] - {status}", f"clinfo:{tag}", "primary")])
        rows.append(
            [
                ib(
                    "Stop" if tag in running_tags else "Start",
                    f"cltog:{tag}",
                    "primary",
                ),
                ib("Remove", f"cldel:{tag}", "danger"),
            ]
        )
    if not entries:
        rows.append([ib("No clone bots yet", "clnone", "primary")])
    rows.append([ib("Add clone", "apnl:cladd", "success")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_lessons_kb(stuff_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Back", f"sadm:{stuff_id}", "primary")]]
    )


def admin_lessons_list_kb(stuff: dict) -> InlineKeyboardMarkup:
    rows = []
    for index, lesson in enumerate(stuff.get("lessons", [])):
        mark = "demo" if lesson.get("is_demo") else "paid"
        title = lesson.get("title") or f"Stuff {index + 1}"
        if len(title) > 28:
            title = title[:26] + "…"
        style = "success" if lesson.get("is_demo") else "danger"
        rows.append([ib(f"{mark} | {title}", f"sdemo:{stuff['_id']}:{index}", style)])
    rows.append([ib("Back", f"sadm:{stuff['_id']}", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_stuff_kb(stuff_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Delete forever", f"sdelok:{stuff_id}", "danger")],
            [ib("Back", f"sadm:{stuff_id}", "primary")],
        ]
    )


def start_msg_kb(has_media: bool, media_type: str = "") -> InlineKeyboardMarkup:
    rows = [
        [
            ib("Set text", "stext", "primary"),
            ib("Preview", "spreview", "success"),
        ],
        [
            ib("Set photo", "sphoto", "primary"),
            ib("Set video", "svideo", "primary"),
        ],
    ]
    if has_media:
        label = f"Clear {media_type or 'media'}"
        rows.append([ib(label, "sclearmedia", "danger")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def add_photo_video_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Cancel", "aback", "danger")]]
    )


def admins_kb(admins_list: list[dict], env_ids: set[int]) -> InlineKeyboardMarkup:
    rows = []
    seen: set[int] = set()
    for admin in admins_list:
        tg_id = admin.get("tg_id")
        seen.add(tg_id)
        label = f"@{admin['username']}" if admin.get("username") else str(tg_id)
        rows.append([ib(f"Remove {label}"[:64], f"admdel:{tg_id}", "danger")])
    for tg_id in sorted(env_ids - seen):
        rows.append([ib(f"Remove {tg_id} (env)", f"admdel:{tg_id}", "danger")])
    rows.append([ib("Add admin", "admadd", "success")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kind_crypto_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Crypto address (e.g. USDT BEP-20)", "kind:crypto", "primary")],
            [ib("QR code (INR / UPI)", "kind:qr", "primary")],
            [ib("Cancel", "pmaddcancel", "danger")],
        ]
    )


def note_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Skip", "noteskip", "primary")]]
    )


def payment_methods_admin_kb(methods: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for method in methods:
        status = "(on)" if method.get("is_active") else "(off)"
        rows.append(
            [ib(f"{method['name']} {status}"[:64], f"mv:{method['_id']}", "primary")]
        )
    rows.append([ib("Add payment method", "pmadd", "success")])
    rows.append([ib("Panel", "aback", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_method_view_kb(
    method_id: str, is_active: bool, kind: str = ""
) -> InlineKeyboardMarkup:
    toggle_text = "Deactivate" if is_active else "Activate"
    toggle_style = "danger" if is_active else "success"
    rows: list[list[InlineKeyboardButton]] = []
    if kind == "stars":
        rows.append(
            [ib("Star receiver @username", f"suname:{method_id}", "primary")]
        )
    rows.append([ib(toggle_text, f"mtoggle:{method_id}", toggle_style)])
    rows.append([ib("Delete method", f"mdel:{method_id}", "danger")])
    rows.append([ib("Back", "pmpanel", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_inline_kb(callback: str = "aback") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Cancel", callback, "danger")]]
    )


def log_group_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ib("Clear log group", "logclear", "danger")]]
    )


def review_request_kb(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Approve & unlock", f"payok:{request_id}", "success"),
                ib("Reject", f"payno:{request_id}", "danger"),
            ]
        ]
    )


def approve_open_kb(stuff_id: str | None) -> InlineKeyboardMarkup:
    if not stuff_id:
        return InlineKeyboardMarkup(
            inline_keyboard=[[ib("Menu", "smenu", "primary")]]
        )
    return after_payment_kb(stuff_id)


# ---------------------------------------------------------------- panel / flows


def panel_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Add stuff", "apnl:add", "success")],
            [
                ib("Stuffs", "apnl:stuffs", "primary"),
                ib("Start message", "apnl:startmsg", "primary"),
            ],
            [
                ib("Admins", "apnl:admins", "primary"),
                ib("Payment methods", "apnl:methods", "primary"),
            ],
            [
                ib("Protection", "apnl:protect", "primary"),
                ib("Log group", "apnl:loggroup", "primary"),
            ],
            [
                ib("Users", "apnl:users", "primary"),
                ib("Broadcast", "apnl:bcast", "primary"),
            ],
            [
                ib("Stats", "apnl:stats", "primary"),
                ib("VPS", "apnl:vps", "primary"),
            ],
            [ib("Clone bots", "apnl:clones", "success")],
            [ib("Menu", "smenu", "primary")],
        ]
    )


def add_stuff_choice_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Upload from ZIP", "apnl:zip", "success")],
            [ib("Direct video forwarding", "apnl:direct", "success")],
            [ib("Back", "aback", "primary")],
        ]
    )


def desc_step_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Skip", "sskip:desc", "primary"), ib("Cancel", "scancel", "danger")],
        ]
    )


def demo_step_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Done", "sdone", "success")],
            [
                ib("Skip", "sskip:demo", "primary"),
                ib("Cancel", "scancel", "danger"),
            ],
        ]
    )


def showcase_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ib("Skip", "sskip:showcase", "primary"),
                ib("Cancel", "scancel", "danger"),
            ],
        ]
    )


def proofs_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ib("Done", "sdone", "success")],
            [
                ib("Skip", "sskip:proofs", "primary"),
                ib("Cancel", "scancel", "danger"),
            ],
        ]
    )


def proof_gallery_kb(stuff_id: str, index: int, total: int) -> InlineKeyboardMarkup:
    nav: list[InlineKeyboardButton] = []
    if index > 0:
        nav.append(ib("Prev", f"proofs:{stuff_id}:{index - 1}", "primary"))
    if index < total - 1:
        nav.append(ib("Next", f"proofs:{stuff_id}:{index + 1}", "primary"))
    rows: list[list[InlineKeyboardButton]] = []
    if nav:
        rows.append(nav)
    rows.append([ib("Back to stuff", f"sstuf:{stuff_id}", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def demo_list_kb(stuffs: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for stuff in stuffs:
        count = sum(1 for lesson in stuff.get("lessons", []) if lesson.get("is_demo"))
        rows.append(
            [ib(f"{stuff['name']} ({count} demo)", f"vdl:{stuff['_id']}", "success")]
        )
    rows.append([ib("Menu", "smenu", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def demo_view_kb(
    stuff_id: str,
    prev_idx: int | None,
    next_idx: int | None,
    price: float,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if prev_idx is not None:
        nav.append(ib("Prev demo", f"vdl:{stuff_id}:{prev_idx}", "primary"))
    if next_idx is not None:
        nav.append(ib("Next demo", f"vdl:{stuff_id}:{next_idx}", "primary"))
    if nav:
        rows.append(nav)
    if price > 0:
        rows.append(
            [ib(f"Buy — Rs.{int(price)}", f"abuy:{stuff_id}", "success")]
        )
    rows.append([ib("All demos", "vdemo", "primary")])
    rows.append([ib("Menu", "smenu", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bdirect_kb(stuffs: list[dict], selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    total = 0.0
    for stuff in stuffs:
        sid = str(stuff["_id"])
        price = float(stuff.get("price", 0) or 0)
        mark = "[x]" if sid in selected else "[ ]"
        label = f"{mark} {stuff['name']}"
        if price > 0:
            label += f" — Rs.{int(price)}"
        if sid in selected:
            total += price
        rows.append([ib(label[:64], f"bd:{sid}", "primary")])
    if stuffs:
        rows.append(
            [
                ib("Select all", "bda", "success"),
                ib("Clear", "bdc", "danger"),
            ]
        )
    if selected:
        rows.append(
            [ib(f"Buy selected ({len(selected)}) — Rs.{total:g}", "bbuy", "success")]
        )
    rows.append([ib("Menu", "smenu", "primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
