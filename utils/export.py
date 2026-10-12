import html
import logging
import shutil
import uuid
import zipfile
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from config import MAX_ZIP_SIZE, MEDIA_TEMP_DIR

logger = logging.getLogger("export")

VIDEO_TYPES = {"video", "animation"}


def _ext_for(lesson: dict) -> str:
    if lesson.get("media_type") in VIDEO_TYPES:
        return ".mp4"
    if lesson.get("media_type") == "video_note":
        return ".mp4"
    return ".jpg"


async def _download_file(bot: Bot, file_id: str, dest: Path) -> bool:
    try:
        tg_file = await bot.get_file(file_id)
    except TelegramAPIError:
        logger.warning("get_file failed for %s", file_id)
        return False
    try:
        await bot.download(tg_file, destination=str(dest))
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        logger.exception("Download failed for %s", file_id)
        return False


def _info_text(stuff: dict) -> str:
    lines = [
        f"Name: {stuff.get('name', '')}",
        f"Price: {stuff.get('price', 0)}",
        "",
        "Description:",
        str(stuff.get("description") or "(none)"),
        "",
        "Contents:",
    ]
    for i, lesson in enumerate(stuff.get("lessons", []), 1):
        kind = "demo" if lesson.get("is_demo") else "full"
        lines.append(
            f"{i}. [{kind}] {lesson.get('media_type', 'file')} - "
            f"{lesson.get('title') or f'Stuff {i}'}"
        )
    if stuff.get("showcase"):
        lines.append("cover.jpg - showcase image")
    for i, _proof in enumerate(stuff.get("proofs") or [], 1):
        lines.append(f"proof_{i}.jpg - proof screenshot")
    return "\n".join(lines)


async def build_stuff_zip(bot: Bot, stuff: dict) -> Path:
    MEDIA_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    folder = MEDIA_TEMP_DIR / f"export_{uuid.uuid4().hex[:10]}"
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(
        c for c in str(stuff.get("name", "stuff")) if c.isalnum() or c in " _-"
    ).strip() or "stuff"
    zip_path = folder / f"{safe_name}.zip"
    skipped: list[str] = []

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("info.txt", _info_text(stuff))
        for i, lesson in enumerate(stuff.get("lessons", []), 1):
            prefix = "demo" if lesson.get("is_demo") else "video"
            ext = _ext_for(lesson)
            media_name = f"{prefix}_{i}{ext}"
            media_path = folder / media_name
            if await _download_file(bot, lesson.get("file_id", ""), media_path):
                zf.write(media_path, media_name)
                meta = [
                    f"Title: {lesson.get('title') or f'Stuff {i}'}",
                    f"Description: {lesson.get('description') or ''}",
                    f"Type: {lesson.get('media_type', 'file')}",
                    f"Demo: {'yes' if lesson.get('is_demo') else 'no'}",
                ]
                zf.writestr(f"{prefix}_{i}.txt", "\n".join(meta))
            else:
                skipped.append(media_name)
            if media_path.exists():
                media_path.unlink()

        showcase = stuff.get("showcase")
        if showcase:
            cover_path = folder / "cover.jpg"
            if await _download_file(bot, showcase, cover_path):
                zf.write(cover_path, "cover.jpg")
            else:
                skipped.append("cover.jpg")
            if cover_path.exists():
                cover_path.unlink()

        for i, proof in enumerate(stuff.get("proofs") or [], 1):
            proof_path = folder / f"proof_{i}.jpg"
            if await _download_file(bot, proof, proof_path):
                zf.write(proof_path, f"proof_{i}.jpg")
            else:
                skipped.append(f"proof_{i}.jpg")
            if proof_path.exists():
                proof_path.unlink()

        if skipped:
            zf.writestr(
                "MISSING.txt",
                "These files could not be downloaded:\n" + "\n".join(skipped),
            )

    if zip_path.stat().st_size > MAX_ZIP_SIZE:
        size = zip_path.stat().st_size
        shutil.rmtree(folder, ignore_errors=True)
        raise ValueError(
            f"ZIP is too large ({size // (1024 * 1024)} MB, "
            f"max {MAX_ZIP_SIZE // (1024 * 1024)} MB)"
        )
    return zip_path


def cleanup_export(path: Path) -> None:
    try:
        folder = path.parent
        if folder.name.startswith("export_"):
            shutil.rmtree(folder, ignore_errors=True)
    except Exception:
        logger.exception("Could not clean export folder")


async def send_stuff_direct(message: Message, stuff: dict) -> int:
    sent = 0
    name = html.escape(str(stuff.get("name") or "Stuff"))
    lessons = stuff.get("lessons") or []
    for i, lesson in enumerate(lessons, 1):
        caption = (
            f"<b>{name}</b> - Stuff {i} of {len(lessons)}\n"
            f"{html.escape(str(lesson.get('title') or ''))}"
        )[:1024]
        try:
            if lesson.get("media_type") in VIDEO_TYPES or lesson.get(
                "media_type"
            ) == "video_note":
                await message.answer_video(
                    video=lesson["file_id"], caption=caption
                )
            else:
                await message.answer_photo(photo=lesson["file_id"], caption=caption)
            sent += 1
        except TelegramAPIError:
            logger.exception("Direct send failed for lesson %s", i)
    if stuff.get("showcase"):
        try:
            await message.answer_photo(
                photo=stuff["showcase"], caption=f"<b>{name}</b> - showcase"
            )
            sent += 1
        except TelegramAPIError:
            logger.exception("Direct send failed for showcase")
    for i, proof in enumerate(stuff.get("proofs") or [], 1):
        try:
            await message.answer_photo(
                photo=proof, caption=f"<b>{name}</b> - proof {i}"
            )
            sent += 1
        except TelegramAPIError:
            logger.exception("Direct send failed for proof %s", i)
    return sent
