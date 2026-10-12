from aiogram.types import User as TgUser

from config import AUTH_ADMIN_IDS
from database.store import (
    add_admin as store_add_admin,
    ensure_user as store_ensure_user,
    find_user as store_find_user,
    get_setting,
    grant_access as store_grant_access,
    has_admin as store_has_admin,
    list_admins as store_list_admins,
    remove_admin as store_remove_admin,
    set_setting,
)

PROTECT_KEY = "protect_content"
PROTECT_DEFAULT = "true"


async def is_admin(tg_id: int | None) -> bool:
    if tg_id is None:
        return False
    if tg_id in AUTH_ADMIN_IDS:
        return True
    return await store_has_admin(tg_id)


async def get_admin_list() -> list[dict]:
    return await store_list_admins()


async def add_admin(tg_id: int, username: str | None = None) -> None:
    await store_add_admin(tg_id, username)


async def remove_admin(tg_id: int) -> None:
    await store_remove_admin(tg_id)


async def ensure_user(tg_user: TgUser) -> dict:
    return await store_ensure_user(tg_user.id, tg_user.username, tg_user.full_name)


async def find_user(tg_id: int) -> dict | None:
    return await store_find_user(tg_id)


async def is_banned(tg_id: int) -> bool:
    from database.store import is_banned as store_is_banned

    return await store_is_banned(tg_id)


def user_has_access(user: dict | None, stuff: dict) -> bool:
    if stuff.get("price", 0) <= 0:
        return True
    if not user:
        return False
    from database.store import purchase_key

    return purchase_key(stuff["_id"]) in {str(x) for x in user.get("purchased", [])}


async def grant_access(tg_id: int, stuff_id: str) -> None:
    await store_grant_access(tg_id, str(stuff_id))


async def is_protect_content() -> bool:
    value = await get_setting(PROTECT_KEY, PROTECT_DEFAULT)
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def set_protect_content(value: bool) -> None:
    await set_setting(PROTECT_KEY, "true" if value else "false")


async def get_start_message() -> dict:
    text = await get_setting(
        "start_text",
        "Welcome!\n\n"
        "Buy stuffs and watch them here.",
    )
    media_type = await get_setting("start_media_type", "")
    media_id = await get_setting("start_media_id", "")
    return {
        "text": text,
        "media_type": media_type if media_id else "",
        "media_id": media_id,
    }


async def set_start_text(text: str) -> None:
    await set_setting("start_text", text)


async def set_start_media(media_type: str, media_id: str) -> None:
    await set_setting("start_media_type", media_type)
    await set_setting("start_media_id", media_id)


async def clear_start_media() -> None:
    await set_setting("start_media_type", "")
    await set_setting("start_media_id", "")
