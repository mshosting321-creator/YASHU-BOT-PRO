import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING
from pymongo.errors import PyMongoError

from config import DATABASE_URL, MONGO_DB_NAME

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


class MongoUnavailableError(Exception):
    pass


async def connect_mongo() -> None:
    global _client, _db
    _client = AsyncIOMotorClient(
        DATABASE_URL,
        serverSelectionTimeoutMS=8000,
    )
    try:
        await _client.admin.command("ping")
    except PyMongoError as error:
        await close_mongo()
        raise MongoUnavailableError(
            f"Cannot reach MongoDB at DATABASE_URL ({error.__class__.__name__}).\n"
            "Install MongoDB locally or put an Atlas URI into .env:\n"
            "  DATABASE_URL=mongodb+srv://user:pass@cluster0.xxxxx.mongodb.net/course_bot"
        ) from error

    _db = _client[MONGO_DB_NAME]
    await _create_indexes()
    logger.info("MongoDB connected: db=%s", MONGO_DB_NAME)


async def _create_indexes() -> None:
    await users().create_index([("tg_id", ASCENDING)], unique=True)
    try:
        await stuffs().drop_index("name_1")
    except Exception:
        pass
    await stuffs().create_index(
        [("bot", ASCENDING), ("name", ASCENDING)], unique=True
    )
    await settings().create_index([("key", ASCENDING)], unique=True)
    await admins().create_index([("tg_id", ASCENDING)], unique=True)
    await payment_requests().create_index([("user_tg_id", ASCENDING)])
    await payment_methods().create_index([("sort_order", ASCENDING)])


async def close_mongo() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise MongoUnavailableError("MongoDB is not connected")
    return _db


def users():
    return get_db()["users"]


def stuffs():
    return get_db()["stuffs"]


def settings():
    return get_db()["settings"]


def admins():
    return get_db()["admins"]


def payment_methods():
    return get_db()["payment_methods"]


def payment_requests():
    return get_db()["payment_requests"]


async def get_setting(key: str, default: str = "") -> str:
    doc = await settings().find_one({"key": key})
    return str(doc["value"]) if doc and "value" in doc else default


async def set_setting(key: str, value: str) -> None:
    await settings().update_one(
        {"key": key}, {"$set": {"value": value}}, upsert=True
    )
