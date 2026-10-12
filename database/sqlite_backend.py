import json
import sqlite3
from datetime import datetime

import aiosqlite

from database.store import DuplicateKeyError

_db: aiosqlite.Connection | None = None


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("SQLite is not connected")
    return _db


async def connect(url: str) -> None:
    global _db
    if "///" in url:
        path = url.split("///", 1)[1]
    else:
        path = "course_bot.db"
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _prepare_legacy_users()
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL)"
    )
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "tg_id INTEGER PRIMARY KEY, "
        "username TEXT, "
        "full_name TEXT, "
        "purchased TEXT NOT NULL DEFAULT '[]', "
        "balance REAL NOT NULL DEFAULT 0, "
        "banned INTEGER NOT NULL DEFAULT 0)"
    )
    for _migration in (
        "ALTER TABLE users ADD COLUMN balance REAL NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await _db.execute(_migration)
        except sqlite3.OperationalError:
            pass
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS stuffs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL UNIQUE, "
        "description TEXT, "
        "price REAL NOT NULL DEFAULT 0, "
        "lessons TEXT NOT NULL DEFAULT '[]', "
        "showcase TEXT, "
        "proofs TEXT NOT NULL DEFAULT '[]')"
    )
    for _migration in (
        "ALTER TABLE stuffs ADD COLUMN showcase TEXT",
        "ALTER TABLE stuffs ADD COLUMN proofs TEXT NOT NULL DEFAULT '[]'",
    ):
        try:
            await _db.execute(_migration)
        except sqlite3.OperationalError:
            pass
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS admins ("
        "tg_id INTEGER PRIMARY KEY, "
        "username TEXT)"
    )
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS methods ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "data TEXT NOT NULL)"
    )
    await _db.execute(
        "CREATE TABLE IF NOT EXISTS requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "data TEXT NOT NULL)"
    )
    await _migrate_legacy_data()
    await _ensure_stuffs_columns()
    await _relax_stuffs_name_unique()
    await _db.commit()


async def _ensure_stuffs_columns() -> None:
    for _migration in (
        "ALTER TABLE stuffs ADD COLUMN showcase TEXT",
        "ALTER TABLE stuffs ADD COLUMN proofs TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE stuffs ADD COLUMN bot TEXT NOT NULL DEFAULT 'main'",
    ):
        try:
            await _db.execute(_migration)
        except sqlite3.OperationalError:
            pass


async def _relax_stuffs_name_unique() -> None:
    cursor = await _db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='stuffs'"
    )
    row = await cursor.fetchone()
    if row is None or "UNIQUE" not in (row["sql"] or ""):
        return
    await _ensure_stuffs_columns()
    await _db.execute(
        "CREATE TABLE stuffs_tmp ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, "
        "description TEXT, "
        "price REAL NOT NULL DEFAULT 0, "
        "lessons TEXT NOT NULL DEFAULT '[]', "
        "showcase TEXT, "
        "proofs TEXT NOT NULL DEFAULT '[]', "
        "bot TEXT NOT NULL DEFAULT 'main')"
    )
    await _db.execute(
        "INSERT INTO stuffs_tmp "
        "(id, name, description, price, lessons, showcase, proofs, bot) "
        "SELECT id, name, description, price, lessons, showcase, proofs, bot "
        "FROM stuffs"
    )
    await _db.execute("DROP TABLE stuffs")
    await _db.execute("ALTER TABLE stuffs_tmp RENAME TO stuffs")


async def _table_exists(table: str) -> bool:
    cursor = await _db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return await cursor.fetchone() is not None


async def _table_columns(table: str) -> set[str]:
    cursor = await _db.execute(f'PRAGMA table_info("{table}")')
    rows = await cursor.fetchall()
    return {row[1] for row in rows}


async def _prepare_legacy_users() -> None:
    """Rename the old SQLAlchemy users table so the new schema can be created."""
    if not await _table_exists("users"):
        return
    columns = await _table_columns("users")
    if "telegram_id" in columns and "tg_id" not in columns:
        await _db.execute("ALTER TABLE users RENAME TO _legacy_users")


async def _migrate_legacy_data() -> None:
    """Copy data from old-version tables, then drop them."""
    if await _table_exists("_legacy_users"):
        columns = await _table_columns("_legacy_users")
        balance_expr = "balance" if "balance" in columns else "0"
        banned_expr = "banned" if "banned" in columns else "0"
        purchased_expr = (
            "purchased_categories" if "purchased_categories" in columns else "'[]'"
        )
        await _db.execute(
            "INSERT INTO users (tg_id, username, full_name, purchased, balance, banned) "
            f"SELECT telegram_id, username, full_name, {purchased_expr}, "
            f"{balance_expr}, {banned_expr} "
            "FROM _legacy_users "
            "WHERE telegram_id IS NOT NULL "
            "AND telegram_id NOT IN (SELECT tg_id FROM users)"
        )
        await _db.execute("DROP TABLE _legacy_users")

    if await _table_exists("payment_methods"):
        cursor = await _db.execute("SELECT * FROM payment_methods")
        legacy_methods = await cursor.fetchall()
        cursor = await _db.execute("SELECT data FROM methods")
        existing_names = {
            json.loads(row["data"]).get("name")
            for row in await cursor.fetchall()
        }
        for row in legacy_methods:
            if row["name"] in existing_names:
                continue
            doc = {
                "name": row["name"],
                "kind": row["kind"],
                "instructions": row["instructions"],
                "detail": row["detail"],
                "qr_file_id": row["qr_file_id"],
                "is_active": bool(row["is_active"]),
                "sort_order": row["sort_order"],
            }
            await _db.execute(
                "INSERT INTO methods (data) VALUES (?)",
                (json.dumps(doc, ensure_ascii=False),),
            )
        await _db.execute("DROP TABLE payment_methods")

    if await _table_exists("categories"):
        cursor = await _db.execute("SELECT * FROM categories")
        legacy_categories = await cursor.fetchall()
        lesson_map: dict[int, list[dict]] = {}
        if await _table_exists("lessons"):
            cursor = await _db.execute("SELECT * FROM lessons ORDER BY order_index")
            for row in await cursor.fetchall():
                lesson_map.setdefault(row["category_id"], []).append(
                    {
                        "title": row["title"],
                        "description": row["description"] or "",
                        "media_type": row["media_type"],
                        "file_id": row["file_id"],
                        "is_demo": bool(row["is_demo"]),
                    }
                )
        for row in legacy_categories:
            cursor = await _db.execute(
                "SELECT 1 FROM stuffs WHERE name = ?", (row["name"],)
            )
            if await cursor.fetchone():
                continue
            await _db.execute(
                "INSERT INTO stuffs (name, description, price, lessons) "
                "VALUES (?, ?, ?, ?)",
                (
                    row["name"],
                    row["description"] or "",
                    row["price"],
                    json.dumps(
                        lesson_map.get(row["id"], []), ensure_ascii=False
                    ),
                ),
            )
        await _db.execute("DROP TABLE IF EXISTS categories")
        await _db.execute("DROP TABLE IF EXISTS lessons")

    if await _table_exists("admin_settings"):
        cursor = await _db.execute("SELECT * FROM admin_settings")
        for row in await cursor.fetchall():
            await _db.execute(
                "INSERT INTO settings (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO NOTHING",
                (row["key"], row["value"]),
            )
        await _db.execute("DROP TABLE admin_settings")

    if await _table_exists("payment_requests"):
        cursor = await _db.execute("SELECT * FROM payment_requests")
        for row in await cursor.fetchall():
            created = row["created_at"]
            if isinstance(created, datetime):
                created = created.isoformat()
            doc = {
                "user_tg_id": row["user_telegram_id"],
                "user_username": row["user_username"],
                "user_full_name": row["user_full_name"],
                "stuff_id": (
                    str(row["category_id"])
                    if row["category_id"] is not None
                    else None
                ),
                "stuff_name": row["category_name"],
                "method_id": (
                    str(row["method_id"]) if row["method_id"] is not None else None
                ),
                "method_name": row["method_name"],
                "amount": row["amount"],
                "status": row["status"],
                "screenshot_file_id": row["screenshot_file_id"],
                "log_chat_id": row["log_chat_id"],
                "log_message_id": row["log_message_id"],
                "created_at": created,
            }
            await _db.execute(
                "INSERT INTO requests (data) VALUES (?)",
                (json.dumps(doc, ensure_ascii=False, default=str),),
            )
        await _db.execute("DROP TABLE payment_requests")


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
    _db = None


def _stuff_row(row) -> dict:
    keys = row.keys()
    return {
        "_id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "price": row["price"],
        "lessons": _loads_list(row["lessons"]),
        "showcase": row["showcase"] if "showcase" in keys else None,
        "proofs": _loads_list(row["proofs"]) if "proofs" in keys else [],
        "bot": row["bot"] if "bot" in keys else "main",
    }


def _method_doc(row) -> dict:
    data = json.loads(row["data"])
    data["_id"] = row["id"]
    return data


def _request_doc(row) -> dict:
    data = json.loads(row["data"])
    data["_id"] = row["id"]
    created = data.get("created_at")
    if isinstance(created, str):
        try:
            data["created_at"] = datetime.fromisoformat(created)
        except ValueError:
            pass
    return data


async def list_stuffs() -> list[dict]:
    cursor = await _conn().execute("SELECT * FROM stuffs ORDER BY name")
    rows = await cursor.fetchall()
    return [_stuff_row(row) for row in rows]


async def get_stuff(stuff_id) -> dict | None:
    try:
        numeric = int(stuff_id)
    except (TypeError, ValueError):
        return None
    cursor = await _conn().execute("SELECT * FROM stuffs WHERE id = ?", (numeric,))
    row = await cursor.fetchone()
    return _stuff_row(row) if row else None


async def get_stuff_by_name(name: str) -> dict | None:
    cursor = await _conn().execute("SELECT * FROM stuffs WHERE name = ?", (name,))
    row = await cursor.fetchone()
    return _stuff_row(row) if row else None


async def find_stuffs_by_name(name: str) -> list[dict]:
    cursor = await _conn().execute("SELECT * FROM stuffs WHERE name = ?", (name,))
    rows = await cursor.fetchall()
    return [_stuff_row(r) for r in rows]


async def create_stuff(
    name: str,
    description: str,
    price: float,
    lessons: list,
    showcase: str | None = None,
    proofs: list | None = None,
    bot: str = "main",
) -> dict:
    try:
        cursor = await _conn().execute(
            "INSERT INTO stuffs "
            "(name, description, price, lessons, showcase, proofs, bot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                description,
                price,
                json.dumps(lessons, ensure_ascii=False),
                showcase,
                json.dumps(proofs or [], ensure_ascii=False),
                bot,
            ),
        )
        await _conn().commit()
    except sqlite3.IntegrityError as error:
        raise DuplicateKeyError(name) from error
    return {
        "_id": cursor.lastrowid,
        "name": name,
        "description": description,
        "price": price,
        "lessons": lessons,
        "showcase": showcase,
        "proofs": proofs or [],
    }


async def set_showcase_row(stuff_id, showcase: str | None) -> None:
    await _conn().execute(
        "UPDATE stuffs SET showcase = ? WHERE id = ?", (showcase, int(stuff_id))
    )
    await _conn().commit()


async def set_proofs_row(stuff_id, proofs: list) -> None:
    await _conn().execute(
        "UPDATE stuffs SET proofs = ? WHERE id = ?",
        (json.dumps(proofs, ensure_ascii=False), int(stuff_id)),
    )
    await _conn().commit()


async def rename_stuff(stuff_id, name: str) -> None:
    try:
        await _conn().execute(
            "UPDATE stuffs SET name = ? WHERE id = ?", (name, int(stuff_id))
        )
        await _conn().commit()
    except sqlite3.IntegrityError as error:
        raise DuplicateKeyError(name) from error


async def set_stuff_price(stuff_id, price: float) -> None:
    await _conn().execute("UPDATE stuffs SET price = ? WHERE id = ?", (price, int(stuff_id)))
    await _conn().commit()


async def delete_stuff(stuff_id) -> None:
    await _conn().execute("DELETE FROM stuffs WHERE id = ?", (int(stuff_id),))
    await _conn().commit()


async def append_lessons(stuff_id, lessons: list) -> None:
    cursor = await _conn().execute("SELECT lessons FROM stuffs WHERE id = ?", (int(stuff_id),))
    row = await cursor.fetchone()
    if row is None:
        return
    current = json.loads(row["lessons"])
    current.extend(lessons)
    await _conn().execute(
        "UPDATE stuffs SET lessons = ? WHERE id = ?",
        (json.dumps(current, ensure_ascii=False), int(stuff_id)),
    )
    await _conn().commit()


async def set_lesson_demo(stuff_id, index: int, value: bool) -> dict | None:
    stuff = await get_stuff(stuff_id)
    if stuff is None or not 0 <= index < len(stuff["lessons"]):
        return None
    stuff["lessons"][index]["is_demo"] = value
    await _conn().execute(
        "UPDATE stuffs SET lessons = ? WHERE id = ?",
        (json.dumps(stuff["lessons"], ensure_ascii=False), int(stuff_id)),
    )
    await _conn().commit()
    return stuff


# ---------------------------------------------------------------- users


def _loads_list(value) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _user_row(row) -> dict:
    return {
        "_id": row["tg_id"],
        "tg_id": row["tg_id"],
        "username": row["username"],
        "full_name": row["full_name"],
        "purchased": _loads_list(row["purchased"]),
        "balance": float(row["balance"]),
        "banned": bool(row["banned"]),
    }


async def ensure_user_row(tg_id: int, username: str | None, full_name: str | None) -> dict:
    await _conn().execute(
        "INSERT INTO users (tg_id, username, full_name) VALUES (?, ?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username, "
        "full_name = excluded.full_name",
        (tg_id, username, full_name),
    )
    await _conn().commit()
    cursor = await _conn().execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = await cursor.fetchone()
    return _user_row(row)


async def find_user_row(tg_id: int) -> dict | None:
    cursor = await _conn().execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = await cursor.fetchone()
    return _user_row(row) if row else None


async def add_purchased(tg_id: int, stuff_id: str) -> None:
    cursor = await _conn().execute(
        "SELECT purchased FROM users WHERE tg_id = ?", (tg_id,)
    )
    row = await cursor.fetchone()
    if row is None:
        await _conn().execute(
            "INSERT INTO users (tg_id, purchased) VALUES (?, ?)",
            (tg_id, json.dumps([stuff_id])),
        )
    else:
        purchased = _loads_list(row["purchased"])
        if stuff_id not in purchased:
            purchased.append(stuff_id)
            await _conn().execute(
                "UPDATE users SET purchased = ? WHERE tg_id = ?",
                (json.dumps(purchased), tg_id),
            )
    await _conn().commit()


async def is_banned_row(tg_id: int) -> bool:
    cursor = await _conn().execute(
        "SELECT banned FROM users WHERE tg_id = ?", (tg_id,)
    )
    row = await cursor.fetchone()
    return bool(row["banned"]) if row else False


async def set_banned_row(tg_id: int, value: bool) -> None:
    await _conn().execute(
        "INSERT INTO users (tg_id, banned) VALUES (?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET banned = excluded.banned",
        (tg_id, 1 if value else 0),
    )
    await _conn().commit()


async def add_balance_row(tg_id: int, amount: float) -> float:
    await _conn().execute(
        "INSERT INTO users (tg_id, balance) VALUES (?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET balance = balance + excluded.balance",
        (tg_id, amount),
    )
    await _conn().commit()
    cursor = await _conn().execute(
        "SELECT balance FROM users WHERE tg_id = ?", (tg_id,)
    )
    row = await cursor.fetchone()
    return float(row["balance"]) if row else 0.0


async def deduct_balance_row(tg_id: int, amount: float) -> bool:
    cursor = await _conn().execute(
        "UPDATE users SET balance = balance - ? WHERE tg_id = ? AND balance >= ?",
        (amount, tg_id, amount),
    )
    await _conn().commit()
    return cursor.rowcount > 0


async def count_users_row() -> int:
    cursor = await _conn().execute("SELECT COUNT(*) AS c FROM users")
    row = await cursor.fetchone()
    return int(row["c"])


async def count_banned_row() -> int:
    cursor = await _conn().execute(
        "SELECT COUNT(*) AS c FROM users WHERE banned = 1"
    )
    row = await cursor.fetchone()
    return int(row["c"])


async def sum_balances_row() -> float:
    cursor = await _conn().execute(
        "SELECT COALESCE(SUM(balance), 0) AS s FROM users"
    )
    row = await cursor.fetchone()
    return float(row["s"])


async def list_user_ids_row() -> list[int]:
    cursor = await _conn().execute("SELECT tg_id FROM users")
    rows = await cursor.fetchall()
    return [int(row["tg_id"]) for row in rows]


async def list_request_rows() -> list[dict]:
    cursor = await _conn().execute("SELECT * FROM requests")
    rows = await cursor.fetchall()
    return [_request_doc(row) for row in rows]


# ---------------------------------------------------------------- admins


async def list_admin_rows() -> list[dict]:
    cursor = await _conn().execute("SELECT * FROM admins ORDER BY tg_id")
    rows = await cursor.fetchall()
    return [
        {"_id": row["tg_id"], "tg_id": row["tg_id"], "username": row["username"]}
        for row in rows
    ]


async def admin_exists_row(tg_id: int) -> bool:
    cursor = await _conn().execute(
        "SELECT 1 AS one FROM admins WHERE tg_id = ?", (tg_id,)
    )
    return await cursor.fetchone() is not None


async def upsert_admin_row(tg_id: int, username: str | None) -> None:
    await _conn().execute(
        "INSERT INTO admins (tg_id, username) VALUES (?, ?) "
        "ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username",
        (tg_id, username),
    )
    await _conn().commit()


async def delete_admin_row(tg_id: int) -> None:
    await _conn().execute("DELETE FROM admins WHERE tg_id = ?", (tg_id,))
    await _conn().commit()


# ---------------------------------------------------------------- settings


async def get_setting_row(key: str, default: str = "") -> str:
    cursor = await _conn().execute("SELECT v FROM settings WHERE k = ?", (key,))
    row = await cursor.fetchone()
    return row["v"] if row else default


async def set_setting_row(key: str, value: str) -> None:
    await _conn().execute(
        "INSERT INTO settings (k, v) VALUES (?, ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )
    await _conn().commit()


async def all_settings_rows() -> dict[str, str]:
    cursor = await _conn().execute("SELECT k, v FROM settings")
    rows = await cursor.fetchall()
    return {row["k"]: row["v"] for row in rows}


async def purge_tag_rows(tag: str) -> None:
    conn = _conn()
    await conn.execute("DELETE FROM stuffs WHERE bot = ?", (tag,))
    await conn.execute("DELETE FROM settings WHERE k LIKE ?", (f"{tag}:%",))
    for table in ("methods", "requests"):
        cursor = await conn.execute(f"SELECT id, data FROM {table}")
        rows = await cursor.fetchall()
        for row in rows:
            try:
                payload = json.loads(row["data"]) if row["data"] else {}
            except (TypeError, ValueError):
                payload = {}
            if payload.get("bot") == tag:
                await conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
    await conn.commit()


# ---------------------------------------------------------------- payment methods


async def count_methods() -> int:
    cursor = await _conn().execute("SELECT COUNT(*) AS c FROM methods")
    row = await cursor.fetchone()
    return int(row["c"])


async def insert_method_row(data: dict) -> dict:
    payload = {k: v for k, v in data.items() if k != "_id"}
    cursor = await _conn().execute(
        "INSERT INTO methods (data) VALUES (?)",
        (json.dumps(payload, ensure_ascii=False, default=str),),
    )
    await _conn().commit()
    payload["_id"] = cursor.lastrowid
    return payload


async def list_method_rows(active_only: bool = False) -> list[dict]:
    cursor = await _conn().execute("SELECT * FROM methods")
    rows = await cursor.fetchall()
    docs = [_method_doc(row) for row in rows]
    if active_only:
        docs = [d for d in docs if d.get("is_active")]
    docs.sort(key=lambda d: (d.get("sort_order", 0), d["_id"]))
    return docs


async def get_method_row(method_id) -> dict | None:
    try:
        numeric = int(method_id)
    except (TypeError, ValueError):
        return None
    cursor = await _conn().execute("SELECT * FROM methods WHERE id = ?", (numeric,))
    row = await cursor.fetchone()
    return _method_doc(row) if row else None


async def update_method_row(method_id, fields: dict) -> None:
    cursor = await _conn().execute("SELECT data FROM methods WHERE id = ?", (int(method_id),))
    row = await cursor.fetchone()
    if row is None:
        return
    data = json.loads(row["data"])
    data.update(fields)
    await _conn().execute(
        "UPDATE methods SET data = ? WHERE id = ?",
        (json.dumps(data, ensure_ascii=False, default=str), int(method_id)),
    )
    await _conn().commit()


async def delete_method_row(method_id) -> None:
    await _conn().execute("DELETE FROM methods WHERE id = ?", (int(method_id),))
    await _conn().commit()


# ---------------------------------------------------------------- payment requests


async def insert_request_row(data: dict) -> dict:
    payload = {k: v for k, v in data.items() if k != "_id"}
    created = payload.get("created_at")
    if isinstance(created, datetime):
        payload["created_at"] = created.isoformat()
    cursor = await _conn().execute(
        "INSERT INTO requests (data) VALUES (?)",
        (json.dumps(payload, ensure_ascii=False, default=str),),
    )
    await _conn().commit()
    payload["_id"] = cursor.lastrowid
    payload["created_at"] = data.get("created_at")
    return payload


async def get_request_row(request_id) -> dict | None:
    try:
        numeric = int(request_id)
    except (TypeError, ValueError):
        return None
    cursor = await _conn().execute("SELECT * FROM requests WHERE id = ?", (numeric,))
    row = await cursor.fetchone()
    return _request_doc(row) if row else None


async def update_request_row(request_id, fields: dict) -> None:
    cursor = await _conn().execute("SELECT data FROM requests WHERE id = ?", (int(request_id),))
    row = await cursor.fetchone()
    if row is None:
        return
    data = json.loads(row["data"])
    for key, value in fields.items():
        if isinstance(value, datetime):
            value = value.isoformat()
        data[key] = value
    await _conn().execute(
        "UPDATE requests SET data = ? WHERE id = ?",
        (json.dumps(data, ensure_ascii=False, default=str), int(request_id)),
    )
    await _conn().commit()
