from datetime import datetime, timezone

from config import DATABASE_URL
from database.scoping import (
    GLOBAL_SETTING_KEYS,
    as_tag,
    current_tag,
    scoped_key,
)

_mongo_mode = False


class DuplicateKeyError(Exception):
    """Raised when a unique constraint is violated."""


async def connect_db() -> None:
    global _mongo_mode
    if DATABASE_URL.startswith("mongodb"):
        _mongo_mode = True
        from database import mongo

        await mongo.connect_mongo()
    else:
        _mongo_mode = False
        from database import sqlite_backend

        await sqlite_backend.connect(DATABASE_URL)


async def close_db() -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.close_mongo()
    else:
        from database import sqlite_backend

        await sqlite_backend.close()


def backend_name() -> str:
    return "mongodb" if _mongo_mode else "sqlite"


def _oid(value):
    from bson import ObjectId

    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- settings


async def get_setting(key: str, default: str = "") -> str:
    key = scoped_key(key)
    if _mongo_mode:
        from database import mongo

        return await mongo.get_setting(key, default)
    from database import sqlite_backend

    return await sqlite_backend.get_setting_row(key, default)


async def set_setting(key: str, value: str) -> None:
    key = scoped_key(key)
    if _mongo_mode:
        from database import mongo

        await mongo.set_setting(key, value)
        return
    from database import sqlite_backend

    await sqlite_backend.set_setting_row(key, value)


async def get_setting_global(key: str, default: str = "") -> str:
    if _mongo_mode:
        from database import mongo

        return await mongo.get_setting(key, default)
    from database import sqlite_backend

    return await sqlite_backend.get_setting_row(key, default)


async def set_setting_global(key: str, value: str) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.set_setting(key, value)
        return
    from database import sqlite_backend

    await sqlite_backend.set_setting_row(key, value)


async def all_settings() -> dict[str, str]:
    if _mongo_mode:
        from database import mongo

        docs = await mongo.settings().find({}).to_list(length=10000)
        return {str(d.get("key")): str(d.get("value", "")) for d in docs}
    from database import sqlite_backend

    return await sqlite_backend.all_settings_rows()


async def copy_main_to(tag: str) -> None:
    with as_tag("main"):
        stuffs = await list_stuffs()
        methods = await list_methods()
        settings = await all_settings()
    with as_tag(tag):
        for s in stuffs:
            await create_stuff(
                s.get("name", ""),
                s.get("description", ""),
                float(s.get("price", 0) or 0),
                s.get("lessons") or [],
                s.get("showcase"),
                s.get("proofs") or [],
            )
        for m in methods:
            payload = {k: v for k, v in m.items() if k not in ("_id", "bot")}
            await insert_method(payload)
        for k, v in settings.items():
            if k in GLOBAL_SETTING_KEYS or ":" in k:
                continue
            await set_setting(k, v)


async def purge_tag(tag: str) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.stuffs().delete_many({"bot": tag})
        await mongo.payment_methods().delete_many({"bot": tag})
        await mongo.payment_requests().delete_many({"bot": tag})
        prefix = f"{tag}:"
        keys = [
            str(d.get("key"))
            for d in await mongo.settings().find({}).to_list(length=10000)
            if str(d.get("key", "")).startswith(prefix)
        ]
        if keys:
            await mongo.settings().delete_many({"key": {"$in": keys}})
        return
    from database import sqlite_backend

    await sqlite_backend.purge_tag_rows(tag)


# ---------------------------------------------------------------- users


async def ensure_user(tg_id: int, username: str | None, full_name: str | None) -> dict:
    if _mongo_mode:
        from database import mongo

        await mongo.users().update_one(
            {"tg_id": tg_id},
            {
                "$set": {"username": username, "full_name": full_name},
                "$setOnInsert": {"purchased": [], "balance": 0, "banned": False},
            },
            upsert=True,
        )
        doc = await mongo.users().find_one({"tg_id": tg_id})
    else:
        from database import sqlite_backend

        doc = await sqlite_backend.ensure_user_row(tg_id, username, full_name)
    if doc is not None:
        doc.setdefault("balance", 0)
        doc.setdefault("banned", False)
    return doc


async def find_user(tg_id: int) -> dict | None:
    if _mongo_mode:
        from database import mongo

        doc = await mongo.users().find_one({"tg_id": tg_id})
    else:
        from database import sqlite_backend

        doc = await sqlite_backend.find_user_row(tg_id)
    if doc is not None:
        doc.setdefault("balance", 0)
        doc.setdefault("banned", False)
    return doc


def purchase_key(stuff_id) -> str:
    sid = str(stuff_id)
    tag = current_tag()
    if tag == "main":
        return sid
    return f"{tag}:{sid}"


async def grant_access(tg_id: int, stuff_id: str) -> None:
    key = purchase_key(stuff_id)
    if _mongo_mode:
        from database import mongo

        await mongo.users().update_one(
            {"tg_id": tg_id},
            {"$addToSet": {"purchased": key}},
            upsert=True,
        )
        return
    from database import sqlite_backend

    await sqlite_backend.add_purchased(tg_id, key)


async def is_banned(tg_id: int) -> bool:
    if _mongo_mode:
        from database import mongo

        doc = await mongo.users().find_one({"tg_id": tg_id}, {"banned": 1})
        return bool(doc and doc.get("banned"))
    from database import sqlite_backend

    return await sqlite_backend.is_banned_row(tg_id)


async def set_banned(tg_id: int, value: bool) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.users().update_one(
            {"tg_id": tg_id},
            {"$set": {"banned": value}},
            upsert=True,
        )
        return
    from database import sqlite_backend

    await sqlite_backend.set_banned_row(tg_id, value)


async def add_balance(tg_id: int, amount: float) -> float:
    if _mongo_mode:
        from database import mongo

        await mongo.users().update_one(
            {"tg_id": tg_id},
            {"$inc": {"balance": amount}},
            upsert=True,
        )
        doc = await mongo.users().find_one({"tg_id": tg_id}, {"balance": 1})
        return float((doc or {}).get("balance", 0))
    from database import sqlite_backend

    return await sqlite_backend.add_balance_row(tg_id, amount)


async def deduct_balance(tg_id: int, amount: float) -> bool:
    if _mongo_mode:
        from database import mongo

        result = await mongo.users().update_one(
            {"tg_id": tg_id, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}},
        )
        return result.modified_count > 0
    from database import sqlite_backend

    return await sqlite_backend.deduct_balance_row(tg_id, amount)


async def count_users() -> int:
    if _mongo_mode:
        from database import mongo

        return await mongo.users().count_documents({})
    from database import sqlite_backend

    return await sqlite_backend.count_users_row()


async def count_banned() -> int:
    if _mongo_mode:
        from database import mongo

        return await mongo.users().count_documents({"banned": True})
    from database import sqlite_backend

    return await sqlite_backend.count_banned_row()


async def sum_balances() -> float:
    if _mongo_mode:
        from database import mongo

        pipeline = [{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]
        rows = await mongo.users().aggregate(pipeline).to_list(length=1)
        return float(rows[0]["total"]) if rows else 0.0
    from database import sqlite_backend

    return await sqlite_backend.sum_balances_row()


async def list_user_ids() -> list[int]:
    if _mongo_mode:
        from database import mongo

        cursor = mongo.users().find({}, {"tg_id": 1})
        docs = await cursor.to_list(length=100000)
        return [int(d["tg_id"]) for d in docs]
    from database import sqlite_backend

    return await sqlite_backend.list_user_ids_row()


async def list_requests() -> list[dict]:
    if _mongo_mode:
        from database import mongo

        rows = await mongo.payment_requests().find({}).to_list(length=100000)
    else:
        from database import sqlite_backend

        rows = await sqlite_backend.list_request_rows()
    tag = current_tag()
    return [r for r in rows if (r.get("bot") or "main") == tag]


# ---------------------------------------------------------------- admins


async def list_admins() -> list[dict]:
    if _mongo_mode:
        from database import mongo

        return await mongo.admins().find({}).to_list(length=1000)
    from database import sqlite_backend

    return await sqlite_backend.list_admin_rows()


async def has_admin(tg_id: int) -> bool:
    if _mongo_mode:
        from database import mongo

        return await mongo.admins().find_one({"tg_id": tg_id}) is not None
    from database import sqlite_backend

    return await sqlite_backend.admin_exists_row(tg_id)


async def add_admin(tg_id: int, username: str | None) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.admins().update_one(
            {"tg_id": tg_id},
            {"$set": {"tg_id": tg_id, "username": username}},
            upsert=True,
        )
        return
    from database import sqlite_backend

    await sqlite_backend.upsert_admin_row(tg_id, username)


async def remove_admin(tg_id: int) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.admins().delete_one({"tg_id": tg_id})
        return
    from database import sqlite_backend

    await sqlite_backend.delete_admin_row(tg_id)


# ---------------------------------------------------------------- stuffs


async def list_stuffs() -> list[dict]:
    if _mongo_mode:
        from database import mongo

        rows = await mongo.stuffs().find({}).sort("name", 1).to_list(length=1000)
    else:
        from database import sqlite_backend

        rows = await sqlite_backend.list_stuffs()
    tag = current_tag()
    return [s for s in rows if (s.get("bot") or "main") == tag]


async def get_stuff(stuff_id) -> dict | None:
    if _mongo_mode:
        from database import mongo

        oid = _oid(stuff_id)
        if oid is None:
            return None
        doc = await mongo.stuffs().find_one({"_id": oid})
    else:
        from database import sqlite_backend

        doc = await sqlite_backend.get_stuff(stuff_id)
    if doc is None or (doc.get("bot") or "main") != current_tag():
        return None
    return doc


async def get_stuff_by_name(name: str) -> dict | None:
    if _mongo_mode:
        from database import mongo

        rows = await mongo.stuffs().find({"name": name}).to_list(length=50)
    else:
        from database import sqlite_backend

        rows = await sqlite_backend.find_stuffs_by_name(name)
    tag = current_tag()
    for row in rows:
        if (row.get("bot") or "main") == tag:
            return row
    return None


async def create_stuff(
    name: str,
    description: str,
    price: float,
    lessons: list[dict],
    showcase: str | None = None,
    proofs: list[str] | None = None,
) -> dict:
    if _mongo_mode:
        from pymongo.errors import DuplicateKeyError as MongoDuplicateKeyError

        from database import mongo

        doc = {
            "name": name,
            "description": description,
            "price": price,
            "lessons": lessons,
            "showcase": showcase,
            "proofs": proofs or [],
            "bot": current_tag(),
        }
        try:
            result = await mongo.stuffs().insert_one(doc)
        except MongoDuplicateKeyError as error:
            raise DuplicateKeyError(name) from error
        doc["_id"] = result.inserted_id
        return doc
    from database import sqlite_backend

    return await sqlite_backend.create_stuff(
        name, description, price, lessons, showcase, proofs, current_tag()
    )


async def set_showcase(stuff_id, showcase: str | None) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.stuffs().update_one(
            {"_id": _oid(stuff_id)}, {"$set": {"showcase": showcase}}
        )
        return
    from database import sqlite_backend

    await sqlite_backend.set_showcase_row(stuff_id, showcase)


async def set_proofs(stuff_id, proofs: list[str]) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.stuffs().update_one(
            {"_id": _oid(stuff_id)}, {"$set": {"proofs": proofs}}
        )
        return
    from database import sqlite_backend

    await sqlite_backend.set_proofs_row(stuff_id, proofs)


async def rename_stuff(stuff_id, name: str) -> None:
    if _mongo_mode:
        from pymongo.errors import DuplicateKeyError as MongoDuplicateKeyError

        from database import mongo

        try:
            await mongo.stuffs().update_one(
                {"_id": _oid(stuff_id)}, {"$set": {"name": name}}
            )
        except MongoDuplicateKeyError as error:
            raise DuplicateKeyError(name) from error
        return
    from database import sqlite_backend

    await sqlite_backend.rename_stuff(stuff_id, name)


async def set_stuff_price(stuff_id, price: float) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.stuffs().update_one(
            {"_id": _oid(stuff_id)}, {"$set": {"price": price}}
        )
        return
    from database import sqlite_backend

    await sqlite_backend.set_stuff_price(stuff_id, price)


async def delete_stuff(stuff_id) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.stuffs().delete_one({"_id": _oid(stuff_id)})
        return
    from database import sqlite_backend

    await sqlite_backend.delete_stuff(stuff_id)


async def append_lessons(stuff_id, lessons: list[dict]) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.stuffs().update_one(
            {"_id": _oid(stuff_id)}, {"$push": {"lessons": {"$each": lessons}}}
        )
        return
    from database import sqlite_backend

    await sqlite_backend.append_lessons(stuff_id, lessons)


async def set_lesson_demo(stuff_id, index: int, value: bool) -> dict | None:
    if _mongo_mode:
        from database import mongo

        await mongo.stuffs().update_one(
            {"_id": _oid(stuff_id)}, {"$set": {f"lessons.{index}.is_demo": value}}
        )
        return await mongo.stuffs().find_one({"_id": _oid(stuff_id)})
    from database import sqlite_backend

    return await sqlite_backend.set_lesson_demo(stuff_id, index, value)


# ---------------------------------------------------------------- payment methods


async def count_methods() -> int:
    return len(await list_methods())


async def insert_method(data: dict) -> dict:
    data = {k: v for k, v in data.items() if k != "_id"}
    data["bot"] = current_tag()
    if _mongo_mode:
        from database import mongo

        payload = dict(data)
        result = await mongo.payment_methods().insert_one(payload)
        payload["_id"] = result.inserted_id
        return payload
    from database import sqlite_backend

    return await sqlite_backend.insert_method_row(data)


async def list_methods(active_only: bool = False) -> list[dict]:
    if _mongo_mode:
        from database import mongo

        query = {"is_active": True} if active_only else {}
        cursor = mongo.payment_methods().find(query).sort("sort_order", 1)
        rows = await cursor.to_list(length=500)
    else:
        from database import sqlite_backend

        rows = await sqlite_backend.list_method_rows(False)
    tag = current_tag()
    rows = [m for m in rows if (m.get("bot") or "main") == tag]
    if active_only:
        rows = [m for m in rows if m.get("is_active")]
    rows.sort(key=lambda d: (d.get("sort_order", 0), d.get("_id", 0)))
    return rows


async def get_method(method_id) -> dict | None:
    if _mongo_mode:
        from database import mongo

        oid = _oid(method_id)
        if oid is None:
            return None
        doc = await mongo.payment_methods().find_one({"_id": oid})
    else:
        from database import sqlite_backend

        doc = await sqlite_backend.get_method_row(method_id)
    if doc is None or (doc.get("bot") or "main") != current_tag():
        return None
    return doc


async def update_method(method_id, fields: dict) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.payment_methods().update_one(
            {"_id": _oid(method_id)}, {"$set": fields}
        )
        return
    from database import sqlite_backend

    await sqlite_backend.update_method_row(method_id, fields)


async def delete_method(method_id) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.payment_methods().delete_one({"_id": _oid(method_id)})
        return
    from database import sqlite_backend

    await sqlite_backend.delete_method_row(method_id)


# ---------------------------------------------------------------- payment requests


async def insert_request(data: dict) -> dict:
    data = {k: v for k, v in data.items() if k != "_id"}
    data["bot"] = current_tag()
    if _mongo_mode:
        from database import mongo

        payload = dict(data)
        result = await mongo.payment_requests().insert_one(payload)
        payload["_id"] = result.inserted_id
        return payload
    from database import sqlite_backend

    return await sqlite_backend.insert_request_row(data)


async def get_request(request_id) -> dict | None:
    if _mongo_mode:
        from database import mongo

        oid = _oid(request_id)
        if oid is None:
            return None
        doc = await mongo.payment_requests().find_one({"_id": oid})
    else:
        from database import sqlite_backend

        doc = await sqlite_backend.get_request_row(request_id)
    if doc is None or (doc.get("bot") or "main") != current_tag():
        return None
    return doc


async def update_request(request_id, fields: dict) -> None:
    if _mongo_mode:
        from database import mongo

        await mongo.payment_requests().update_one(
            {"_id": _oid(request_id)}, {"$set": fields}
        )
        return
    from database import sqlite_backend

    await sqlite_backend.update_request_row(request_id, fields)
