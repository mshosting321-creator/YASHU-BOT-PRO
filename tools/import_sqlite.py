"""Import an existing SQLite course_bot.db into the database configured in .env.

Usage (from the bot folder, with the target DATABASE_URL set in .env):

    python tools/import_sqlite.py /path/to/course_bot.db

- Copies settings, users, admins, stuffs, payment methods and payment requests.
- Old numeric ids are remapped, so purchased stuffs, balances and pending
  payment requests keep pointing at the right content.
- Safe to re-run: existing users are updated, existing stuffs/methods are
  matched by name and skipped, payment requests are de-duplicated.
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import store
from database.scoping import as_tag


def _iso(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _read_rows(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    return conn.execute(query).fetchall()


def _method_key(payload: dict) -> tuple:
    return (
        payload.get("kind"),
        payload.get("name") or payload.get("label") or "",
        payload.get("detail") or "",
        payload.get("username") or "",
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Path to the old course_bot.db file")
    parser.add_argument(
        "--skip-requests",
        action="store_true",
        help="Do not import payment history",
    )
    args = parser.parse_args()

    source_path = Path(args.source)
    if not source_path.exists():
        raise SystemExit(f"Source file not found: {source_path}")

    src = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row

    settings = _read_rows(src, "SELECT k, v FROM settings")
    users = _read_rows(src, "SELECT * FROM users")
    admins = _read_rows(src, "SELECT * FROM admins")
    stuffs = _read_rows(src, "SELECT * FROM stuffs")
    methods = _read_rows(src, "SELECT id, data FROM methods")
    requests = _read_rows(src, "SELECT id, data FROM requests")

    await store.connect_db()
    print(f"Target database: {store.backend_name()}")

    # ------------------------------------------------------------------ stuffs
    stuff_map: dict[tuple[str, int], str] = {}
    for row in stuffs:
        bot = row["bot"] if "bot" in row.keys() else "main"
        old_id = int(row["id"])
        with as_tag(bot):
            existing = await store.get_stuff_by_name(row["name"])
            if existing is not None:
                stuff_map[(bot, old_id)] = str(existing["_id"])
                print(f"  stuff exists, kept: {row['name']}")
                continue
            created = await store.create_stuff(
                row["name"],
                row["description"] or "",
                float(row["price"] or 0),
                json.loads(row["lessons"] or "[]"),
                row["showcase"],
                json.loads(row["proofs"] or "[]") if row["proofs"] else [],
            )
            stuff_map[(bot, old_id)] = str(created["_id"])
    print(f"Stuffs: {len(stuffs)} processed, {len(stuff_map)} mapped")

    # ----------------------------------------------------------------- methods
    method_map: dict[int, str] = {}
    for row in methods:
        payload = json.loads(row["data"] or "{}")
        bot = payload.get("bot") or "main"
        payload["bot"] = bot
        payload.pop("_id", None)
        key = _method_key(payload)
        with as_tag(bot):
            already = [m for m in await store.list_methods() if _method_key(m) == key]
        if already:
            method_map[int(row["id"])] = str(already[0]["_id"])
            continue
        with as_tag(bot):
            created = await store.insert_method(payload)
        method_map[int(row["id"])] = str(created["_id"])
    print(f"Methods: {len(methods)} processed, {len(method_map)} mapped")

    # ------------------------------------------------------------------- users
    imported_users = 0
    for row in users:
        tg_id = int(row["tg_id"])
        await store.ensure_user(tg_id, row["username"], row["full_name"])
        current = await store.find_user(tg_id) or {}
        src_balance = float(row["balance"] or 0)
        cur_balance = float(current.get("balance", 0) or 0)
        delta = src_balance - cur_balance
        if abs(delta) > 1e-9:
            await store.add_balance(tg_id, delta)
        src_banned = bool(row["banned"])
        if bool(current.get("banned")) != src_banned:
            await store.set_banned(tg_id, src_banned)
        purchased = json.loads(row["purchased"] or "[]")
        have = {str(x) for x in (current.get("purchased") or [])}
        for entry in purchased:
            raw = str(entry)
            tag, sep, sid = raw.partition(":")
            if not sep:
                tag, sid = "main", raw
            if sid.isdigit():
                new_sid = stuff_map.get((tag, int(sid)), sid)
            else:
                new_sid = sid
            key = new_sid if tag == "main" else f"{tag}:{new_sid}"
            if key in have:
                continue
            with as_tag(tag):
                await store.grant_access(tg_id, new_sid)
            have.add(key)
        imported_users += 1
    print(f"Users: {imported_users} processed")

    for row in admins:
        await store.add_admin(int(row["tg_id"]), row["username"])
    print(f"Admins: {len(admins)} processed")

    # ---------------------------------------------------------------- settings
    for row in settings:
        await store.set_setting(row["k"], row["v"])
    print(f"Settings: {len(settings)} keys copied")

    # ---------------------------------------------------------------- requests
    if args.skip_requests:
        print("Requests: skipped (--skip-requests)")
    elif requests:
        seen = set()
        for r in await store.list_requests():
            seen.add(
                (
                    r.get("user_tg_id"),
                    _iso(r.get("created_at")),
                    float(r.get("amount") or 0),
                )
            )
        imported_requests = 0
        for row in requests:
            payload = json.loads(row["data"] or "{}")
            payload.pop("_id", None)
            bot = payload.get("bot") or "main"
            payload["bot"] = bot
            stamp = (
                payload.get("user_tg_id"),
                _iso(payload.get("created_at")),
                float(payload.get("amount") or 0),
            )
            if stamp in seen:
                continue
            old_stuff_raw = payload.get("stuff_id")
            if old_stuff_raw:
                mapped = []
                for sid in str(old_stuff_raw).split(","):
                    sid = sid.strip()
                    if not sid:
                        continue
                    new_sid = stuff_map.get((bot, int(sid)), sid)
                    mapped.append(str(new_sid))
                payload["stuff_id"] = ",".join(mapped)
            old_method = payload.get("method_id")
            if old_method is not None and str(old_method).isdigit():
                payload["method_id"] = method_map.get(int(old_method), old_method)
            with as_tag(bot):
                await store.insert_request(payload)
            seen.add(stamp)
            imported_requests += 1
        print(f"Requests: {imported_requests} imported")

    await store.close_db()
    print("Import finished.")


if __name__ == "__main__":
    asyncio.run(main())
