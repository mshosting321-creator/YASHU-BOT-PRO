"""Feature toggles plus helpers for coupons, referral, reviews and friends."""
from __future__ import annotations

import html
import json
import uuid
from datetime import datetime, timedelta, timezone

from database.store import (
    add_balance,
    find_user,
    get_request,
    get_setting,
    set_setting,
)

FEATURES: dict[str, tuple[str, str]] = {
    "coupons": ("Discount coupons", "Buyers can apply a coupon code at checkout"),
    "referral": ("Referral rewards", "Referrer gets a bonus on the first paid order"),
    "forcejoin": ("Force join", "Buyers must join your channel before paying"),
    "checkin": ("Daily check-in", "Users claim a small bonus every day"),
    "wishlist": ("Wishlist", "Users can save stuffs for later"),
    "history": ("Purchase history", "Users can view their past orders"),
    "reviews": ("Reviews", "Users rate stuffs; approved reviews are public"),
    "categories": ("Categories", "Group the catalog into categories"),
    "welcomeback": ("Welcome back", "Greet returning users with new stuffs"),
    "faq": ("FAQ auto-reply", "Unknown texts get matched against your FAQ"),
    "autobackup": ("Auto backup", "Daily database backup posted to the log group"),
    "dailyreport": ("Daily report", "Daily revenue summary in the log group"),
    "scheduler": ("Scheduled broadcasts", "Send messages at a set date and time"),
    "maintenance": ("Maintenance mode", "Block non-admins while you work"),
    "resume": ("Resume position", "Continue a course at the last opened lesson"),
    "leaderboard": ("Leaderboard", "Top buyers ranking in the admin panel"),
    "support": ("Support inbox", "Users open tickets you answer from the panel"),
    "audit": ("Audit log", "Track important admin actions"),
    "graph": ("Stats graph", "7-day activity bars in the stats page"),
    "csv": ("CSV export", "Export the user list as a CSV file"),
    "giftcodes": ("Gift codes", "Admin-made wallet codes users redeem"),
    "search": ("Catalog search", "Users search stuffs by keyword"),
    "cashback": ("Cashback", "Buyers earn back a % of each order in wallet"),
    "receipts": ("Purchase receipts", "Buyers get a receipt after approval"),
    "autoapprove": ("Auto-approve", "Small payment claims are approved instantly"),
    "antiflood": ("Anti-flood", "Warn users who send messages too fast"),
    "stale_reject": ("Auto-reject old claims", "Expire pending claims after 48 hours"),
    "expiry": ("Access validity", "Limit purchases to N days per stuff"),
}
DEFAULT_OFF = {"maintenance", "autoapprove", "antiflood", "stale_reject"}
FLAGS_KEY = "feature_flags"

COUPONS_KEY = "coupons"
REVIEWS_KEY = "reviews"
FAQ_KEY = "faq_pairs"
TICKETS_KEY = "tickets"
SCHEDULE_KEY = "schedule"
AUDIT_KEY = "audit_log"


async def flags() -> dict[str, bool]:
    raw = await get_setting(FLAGS_KEY, "")
    try:
        stored = json.loads(raw) if raw else {}
    except ValueError:
        stored = {}
    return {
        key: bool(stored.get(key, key not in DEFAULT_OFF))
        for key in FEATURES
    }


async def is_on(key: str) -> bool:
    if key not in FEATURES:
        return False
    stored = await flags()
    return stored.get(key, key not in DEFAULT_OFF)


async def set_flag(key: str, value: bool) -> None:
    if key not in FEATURES:
        return
    stored = await flags()
    stored[key] = bool(value)
    await set_setting(FLAGS_KEY, json.dumps(stored))


def flags_text(mapping: dict[str, bool]) -> str:
    lines = ["<b>Features</b>\nTap a button to turn a feature on or off.\n"]
    for key, (label, desc) in FEATURES.items():
        state = "ON" if mapping.get(key) else "OFF"
        lines.append(f"• <b>{label}</b> [{state}]\n  <i>{desc}</i>")
    return "\n".join(lines)


# ------------------------------------------------------------------ storage


async def load_json(key: str, default):
    raw = await get_setting(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


async def save_json(key: str, value) -> None:
    await set_setting(key, json.dumps(value, ensure_ascii=False))


# ------------------------------------------------------------------ coupons


async def get_coupons() -> list[dict]:
    data = await load_json(COUPONS_KEY, [])
    return data if isinstance(data, list) else []


async def save_coupons(coupons: list[dict]) -> None:
    await save_json(COUPONS_KEY, coupons)


async def find_coupon(code: str) -> dict | None:
    needle = (code or "").strip().lower()
    for coupon in await get_coupons():
        if str(coupon.get("code", "")).lower() == needle:
            return coupon
    return None


async def bump_coupon_usage(code: str) -> None:
    coupons = await get_coupons()
    needle = (code or "").strip().lower()
    for coupon in coupons:
        if str(coupon.get("code", "")).lower() == needle:
            coupon["used"] = int(coupon.get("used", 0) or 0) + 1
    await save_coupons(coupons)


def coupon_discount(total: float, coupon: dict | None) -> float:
    if not coupon:
        return 0.0
    percent = max(0.0, min(100.0, float(coupon.get("percent", 0) or 0)))
    return round(total * percent / 100.0, 2)


def coupon_applies(coupon: dict | None) -> bool:
    if not coupon or not coupon.get("active", True):
        return False
    max_uses = int(coupon.get("max_uses", 0) or 0)
    if max_uses and int(coupon.get("used", 0) or 0) >= max_uses:
        return False
    return True


# ------------------------------------------------------------------ referral


async def get_refdata(tg_id: int) -> dict:
    data = await load_json(f"refdata_{tg_id}", {})
    return data if isinstance(data, dict) else {}


async def set_refdata(tg_id: int, data: dict) -> None:
    await save_json(f"refdata_{tg_id}", data)


async def get_referral_bonus() -> float:
    raw = await get_setting("referral_bonus", "20")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 20.0


async def set_referral_bonus(value: float) -> None:
    await set_setting("referral_bonus", f"{value:g}")


async def get_checkin_bonus() -> float:
    raw = await get_setting("checkin_bonus", "5")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


async def set_checkin_bonus(value: float) -> None:
    await set_setting("checkin_bonus", f"{value:g}")


async def credit_referral(request_doc: dict | None) -> None:
    """Called once when an order is approved: pay the referrer."""
    if not request_doc:
        return
    try:
        if not await is_on("referral"):
            return
    except Exception:
        return
    buyer = request_doc.get("user_tg_id")
    if not buyer:
        return
    try:
        refdata = await get_refdata(int(buyer))
    except (TypeError, ValueError):
        return
    if not refdata or refdata.get("credited"):
        return
    referrer = refdata.get("by")
    if not referrer:
        return
    bonus = await get_referral_bonus()
    if bonus <= 0:
        return
    try:
        await add_balance(int(referrer), bonus)
    except Exception:
        return
    refdata["credited"] = True
    await set_refdata(int(buyer), refdata)
    buyer_user = await find_user(int(buyer))
    name = (buyer_user or {}).get("full_name") or str(buyer)
    await audit(
        f"Referral: +{bonus:g} to {referrer} (first purchase by {name})"
    )


# ------------------------------------------------------------------- audit


async def get_audit() -> list[dict]:
    data = await load_json(AUDIT_KEY, [])
    return data if isinstance(data, list) else []


async def audit(text: str) -> None:
    try:
        if not await is_on("audit"):
            return
    except Exception:
        return
    entries = await get_audit()
    entries.insert(
        0,
        {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "at": datetime.now(timezone.utc).isoformat(),
        },
    )
    await save_json(AUDIT_KEY, entries[:200])


# --------------------------------------------------------------- forcejoin


async def get_forcejoin() -> str:
    return (await get_setting("forcejoin", "")).strip()


async def set_forcejoin(value: str) -> None:
    await set_setting("forcejoin", (value or "").strip())


async def forcejoin_allowed(bot, user_id: int) -> tuple[bool, str]:
    """Returns (allowed, channel_ref). Channel ref is empty when unset/off."""
    if not await is_on("forcejoin"):
        return True, ""
    channel = await get_forcejoin()
    if not channel:
        return True, ""
    try:
        member = await bot.get_chat_member(channel, user_id)
    except Exception:
        return True, channel
    if member.status in ("member", "administrator", "creator"):
        return True, channel
    return False, channel


def join_link(channel: str) -> str:
    raw = channel.strip()
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    return ""


def join_prompt(channel: str) -> str:
    raw = channel.strip()
    if raw.startswith("@"):
        head = f"Please join {html.escape(raw)} first, then press"
    else:
        head = "Please join the channel first, then press"
    return (
        "<b>Join required</b>\n\n"
        f"{head} <b>I joined - check</b>."
    )


# ------------------------------------------------------------------ reviews


async def get_reviews() -> list[dict]:
    data = await load_json(REVIEWS_KEY, [])
    return data if isinstance(data, list) else []


async def save_reviews(reviews: list[dict]) -> None:
    await save_json(REVIEWS_KEY, reviews)


async def add_review(stuff_id, tg_id: int, stars: int, text: str) -> None:
    reviews = await get_reviews()
    reviews.append(
        {
            "id": uuid.uuid4().hex[:8],
            "stuff_id": str(stuff_id),
            "user_tg_id": tg_id,
            "stars": int(stars),
            "text": text[:500],
            "approved": False,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    await save_reviews(reviews)


async def approved_reviews(stuff_id) -> list[dict]:
    return [
        r
        for r in await get_reviews()
        if r.get("approved") and str(r.get("stuff_id")) == str(stuff_id)
    ]


def reviews_text(reviews: list[dict]) -> str:
    lines = []
    for review in reviews[-3:]:
        stars = "\u2605" * max(1, min(5, int(review.get("stars", 5))))
        body = html.escape(str(review.get("text") or ""))[:160]
        lines.append(f"{stars} — {body}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- faq


async def get_faq() -> list[dict]:
    data = await load_json(FAQ_KEY, [])
    return data if isinstance(data, list) else []


async def save_faq(pairs: list[dict]) -> None:
    await save_json(FAQ_KEY, pairs)


def faq_answer(pairs: list[dict], text: str) -> str | None:
    needle = (text or "").strip().lower()
    if not needle:
        return None
    for pair in pairs:
        question = str(pair.get("q", "")).strip().lower()
        if question and (question == needle or question in needle):
            return str(pair.get("a", ""))
    return None


# ------------------------------------------------------------------ tickets


async def get_tickets() -> list[dict]:
    data = await load_json(TICKETS_KEY, [])
    return data if isinstance(data, list) else []


async def save_tickets(tickets: list[dict]) -> None:
    await save_json(TICKETS_KEY, tickets)


async def add_ticket(user, text: str) -> dict:
    tickets = await get_tickets()
    ticket = {
        "id": uuid.uuid4().hex[:8],
        "user_tg_id": int(user.id),
        "user_name": user.full_name or str(user.id),
        "text": text[:1000],
        "status": "open",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    tickets.append(ticket)
    await save_tickets(tickets)
    return ticket


# ------------------------------------------------------------------ helpers


def _iso(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


async def purchase_history(tg_id: int, limit: int = 10) -> list[dict]:
    from database.store import list_requests

    rows = []
    for request in await list_requests():
        try:
            if int(request.get("user_tg_id") or 0) != int(tg_id):
                continue
        except (TypeError, ValueError):
            continue
        if str(request.get("status")) != "approved":
            continue
        rows.append(request)

    def sort_key(row):
        moment = _iso(row.get("created_at")) or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    rows.sort(key=sort_key, reverse=True)
    return rows[:limit]


def history_text(rows: list[dict], stuff_names: dict[str, str]) -> str:
    if not rows:
        return "<b>Purchase history</b>\n\nNo orders yet."
    lines = ["<b>Purchase history</b>\n"]
    for row in rows:
        raw = str(row.get("stuff_id", "") or "")
        names = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            names.append(stuff_names.get(part, f"Item {part}"))
        label = ", ".join(names) or "Order"
        created = _iso(row.get("created_at"))
        when = created.strftime("%d %b %Y") if created else ""
        amount = row.get("amount", "?")
        lines.append(f"• {label} — Rs.{amount} {when}")
    return "\n".join(lines)


# --------------------------------------------------------- checkin / wishlist


async def get_checkin(tg_id: int) -> str:
    return await get_setting(f"checkin_{tg_id}", "")


async def set_checkin(tg_id: int, day: str) -> None:
    await set_setting(f"checkin_{tg_id}", day)


async def get_wishlist(tg_id: int) -> list[str]:
    data = await load_json(f"wish_{tg_id}", [])
    return data if isinstance(data, list) else []


async def set_wishlist(tg_id: int, ids: list[str]) -> None:
    await save_json(f"wish_{tg_id}", [str(i) for i in ids])


async def toggle_wishlist(tg_id: int, stuff_id) -> bool:
    """Returns True when the stuff is now on the wishlist."""
    ids = await get_wishlist(tg_id)
    key = str(stuff_id)
    if key in ids:
        ids.remove(key)
        added = False
    else:
        ids.append(key)
        added = True
    await set_wishlist(tg_id, ids)
    return added


# -------------------------------------------------------------------- resume


async def get_resume(tg_id: int, stuff_id) -> int | None:
    raw = await get_setting(f"resume_{tg_id}_{stuff_id}", "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def set_resume(tg_id: int, stuff_id, index: int) -> None:
    await set_setting(f"resume_{tg_id}_{stuff_id}", str(int(index)))


# ---------------------------------------------------------------- maintenance


def maintenance_text() -> str:
    return (
        "<b>Maintenance</b>\n\n"
        "The bot is under maintenance right now. "
        "Please come back a little later."
    )


# ------------------------------------------------------------------ schedule


async def get_schedule() -> list[dict]:
    data = await load_json(SCHEDULE_KEY, [])
    return data if isinstance(data, list) else []


async def save_schedule(entries: list[dict]) -> None:
    await save_json(SCHEDULE_KEY, entries)


async def get_backup_time() -> str:
    return (await get_setting("backup_time", "03:00")).strip() or "03:00"


async def set_backup_time(value: str) -> None:
    await set_setting("backup_time", value.strip())


async def get_report_time() -> str:
    return (await get_setting("report_time", "21:00")).strip() or "21:00"


async def set_report_time(value: str) -> None:
    await set_setting("report_time", value.strip())


def valid_hhmm(value: str) -> bool:
    parts = value.strip().split(":")
    if len(parts) != 2:
        return False
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= hour < 24 and 0 <= minute < 60


async def upcoming_bcasts(now: datetime | None = None) -> list[dict]:
    """Return schedule entries whose time has passed, oldest first."""
    now = now or datetime.now(timezone.utc)
    due = []
    for entry in await get_schedule():
        raw = entry.get("at")
        moment = _iso(raw)
        if moment is None:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment <= now:
            due.append(entry)
    due.sort(key=lambda e: str(e.get("at", "")))
    return due


async def drop_bcast(entry_id: str) -> None:
    entries = [
        e for e in await get_schedule() if str(e.get("id")) != str(entry_id)
    ]
    await save_schedule(entries)


async def was_request_approved(request_id) -> bool:
    doc = await get_request(request_id)
    return bool(doc) and str(doc.get("status")) == "approved"


# --------------------------------------------------------------- gift codes

GIFTCODES_KEY = "giftcodes"


async def get_giftcodes() -> list[dict]:
    data = await load_json(GIFTCODES_KEY, [])
    return data if isinstance(data, list) else []


async def save_giftcodes(codes: list[dict]) -> None:
    await save_json(GIFTCODES_KEY, codes)


async def redeem_gift(code: str, tg_id: int) -> tuple[bool, float, str]:
    """Try to redeem a code. Returns (ok, amount, reason)."""
    if not await is_on("giftcodes"):
        return False, 0.0, "disabled"
    needle = (code or "").strip().lower()
    if not needle:
        return False, 0.0, "empty"
    codes = await get_giftcodes()
    for entry in codes:
        if str(entry.get("code", "")).lower() != needle:
            continue
        if not entry.get("active", True):
            return False, 0.0, "inactive"
        if entry.get("used"):
            return False, 0.0, "used"
        try:
            amount = float(entry.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            return False, 0.0, "empty"
        entry["used"] = True
        entry["redeemed_by"] = int(tg_id)
        entry["redeemed_at"] = datetime.now(timezone.utc).isoformat()
        await save_giftcodes(codes)
        await add_balance(int(tg_id), amount)
        await audit(f"Gift code {entry.get('code')} redeemed (+Rs.{amount:g})")
        return True, amount, "ok"
    return False, 0.0, "unknown"


# ---------------------------------------------------------------- cashback


async def get_cashback_pct() -> float:
    raw = await get_setting("cashback_pct", "0")
    try:
        return max(0.0, min(100.0, float(raw)))
    except ValueError:
        return 0.0


async def set_cashback_pct(value: float) -> None:
    await set_setting("cashback_pct", f"{value:g}")


async def credit_cashback(request_doc: dict | None) -> None:
    """Called once when an order is approved: refund a % to the buyer's wallet."""
    if not request_doc:
        return
    try:
        if not await is_on("cashback"):
            return
    except Exception:
        return
    buyer = request_doc.get("user_tg_id")
    if not buyer:
        return
    pct = await get_cashback_pct()
    if pct <= 0:
        return
    try:
        amount = float(request_doc.get("amount", 0) or 0)
    except (TypeError, ValueError):
        return
    bonus = round(amount * pct / 100.0, 2)
    if bonus <= 0:
        return
    try:
        await add_balance(int(buyer), bonus)
    except Exception:
        return
    await audit(f"Cashback: +Rs.{bonus:g} to {buyer} ({pct:g}% of Rs.{amount:g})")


# ------------------------------------------------------------------ search


def search_stuffs(stuffs: list[dict], query: str, limit: int = 20) -> list[dict]:
    needle = (query or "").strip().lower()
    if not needle:
        return []
    words = [w for w in needle.split() if w]
    matches = []
    for stuff in stuffs:
        haystack = " ".join(
            str(stuff.get(field) or "")
            for field in ("name", "description", "category")
        ).lower()
        if all(word in haystack for word in words):
            matches.append(stuff)
            if len(matches) >= limit:
                break
    return matches


# ---------------------------------------------------------------- receipts


def receipt_text(request: dict) -> str:
    order = request.get("request_no", request.get("_id"))
    created = _iso(request.get("created_at")) or datetime.now(timezone.utc)
    amount = request.get("amount", 0)
    method = request.get("method_name") or "-"
    item = request.get("stuff_name") or "Stuff"
    coupon = request.get("coupon")
    lines = [
        "<b>🧾 Payment receipt</b>\n",
        f"Order ID: <code>{html.escape(str(order))}</code>",
        f"Item: {html.escape(str(item))}",
        f"Amount: {format_amount_text(amount, str(method))}",
        f"Method: {html.escape(str(method))}",
        f"Date: {created:%d %b %Y %H:%M}",
        "Status: ✅ Paid",
    ]
    if coupon:
        lines.insert(4, f"Coupon: <code>{html.escape(str(coupon))}</code>")
    return "\n".join(lines)


def format_amount_text(amount, method_name: str = "") -> str:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    shown = int(value) if value == int(value) else value
    if "star" in method_name.lower() or "telegram" in method_name.lower():
        return f"{shown} ⭐"
    return f"Rs.{shown}"


# ------------------------------------------------------------- autoapprove


async def get_autoapprove_max() -> float:
    raw = await get_setting("autoapprove_max", "0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


async def set_autoapprove_max(value: float) -> None:
    await set_setting("autoapprove_max", f"{value:g}")


# ------------------------------------------------------------ access expiry


async def get_stuff_days(stuff_id) -> int:
    raw = await get_setting(f"days_{stuff_id}", "0")
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return 0


async def set_stuff_days(stuff_id, days: int) -> None:
    await set_setting(f"days_{stuff_id}", str(max(0, int(days))))


def parse_expiry(raw) -> datetime | None:
    moment = _iso(raw)
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


async def apply_validity(tg_id: int, stuff_id) -> None:
    """Give a limited access window after a purchase (feature: expiry)."""
    try:
        if not await is_on("expiry"):
            return
    except Exception:
        return
    days = await get_stuff_days(stuff_id)
    if days <= 0:
        return
    from database.store import purchase_key, set_purchase_expiry

    key = purchase_key(stuff_id)
    user = await find_user(int(tg_id))
    existing = parse_expiry((user or {}).get("expires", {}).get(key))
    base = datetime.now(timezone.utc)
    if existing and existing > base:
        base = existing
    until = base + timedelta(days=days)
    try:
        await set_purchase_expiry(int(tg_id), stuff_id, until.isoformat())
    except Exception:
        return


def expiry_line(user: dict | None, stuff: dict) -> str | None:
    """Human-readable validity line for a purchased stuff, or None."""
    if not user:
        return None
    from database.store import purchase_key

    raw = (user.get("expires") or {}).get(purchase_key(stuff["_id"]))
    until = parse_expiry(raw)
    if until is None:
        return None
    left = until - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return "Access expired"
    if left.days > 0:
        span = f"{left.days}d left"
    else:
        span = f"{max(1, int(left.seconds // 3600))}h left"
    return f"Access valid until {until:%d %b %Y} ({span})"
