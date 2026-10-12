import asyncio
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | PHOTO_EXTENSIONS

_INFO_ALIASES = {
    "name": "name",
    "название": "name",
    "title": "name",
    "course": "name",
    "курс": "name",
    "description": "description",
    "описание": "description",
    "desc": "description",
    "price": "price",
    "цена": "price",
    "demo": "demo",
    "тип": "demo",
    "type": "demo",
}


class ZipCourseError(Exception):
    """Invalid course ZIP archive structure."""


@dataclass
class LessonFile:
    title: str
    description: str
    media_type: str
    path: Path
    is_demo: bool = False


@dataclass
class CoursePackage:
    name: str
    description: str
    price: float
    root: Path
    lessons: list[LessonFile] = field(default_factory=list)
    showcase: Path | None = None
    proofs: list[Path] = field(default_factory=list)


def _natural_key(value: str) -> list:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    ]


def _lesson_order_key(path: Path, root: Path) -> tuple:
    stem = path.stem
    numbers = [int(n) for n in re.findall(r"\d+", stem)]
    first_number = numbers[0] if numbers else float("inf")
    return (first_number, _natural_key(str(path.relative_to(root))))


def _humanize(stem: str) -> str:
    text = re.sub(r"[_\-]+", " ", stem).strip()
    return text[:1].upper() + text[1:] if text else stem


def _safe_extract(zip_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            if not member.filename:
                continue
            target = (dest / member.filename).resolve()
            if target != dest and not str(target).startswith(str(dest) + os.sep):
                raise ZipCourseError(
                    f"Unsafe path inside archive: {member.filename}"
                )
        zf.extractall(dest)


def _find_content_root(dest: Path) -> Path:
    root = dest
    while True:
        entries = [p for p in root.iterdir() if not p.name.startswith("__MACOSX")]
        files = [p for p in entries if p.is_file()]
        dirs = [p for p in entries if p.is_dir()]
        if not files and len(dirs) == 1:
            root = dirs[0]
            continue
        return root


def _find_info_file(root: Path) -> Path | None:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name.lower() == "info.txt":
            return path
    return None


def _parse_info(path: Path | None, fallback_name: str) -> dict:
    data: dict[str, str] = {}
    if path is not None:
        raw = path.read_text(encoding="utf-8", errors="replace")
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            canonical = _INFO_ALIASES.get(key.strip().lower())
            if canonical:
                data[canonical] = value.strip()

    name = data.get("name") or fallback_name
    description = data.get("description", "")

    price = 0.0
    price_raw = data.get("price", "").replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", price_raw)
    if match:
        price = float(match.group())

    demo_flag = data.get("demo", "").strip().lower()
    course_demo = demo_flag in {"1", "true", "yes", "да", "demo"}

    return {"name": name, "description": description, "price": price, "demo": course_demo}


def _read_lesson_text(media_path: Path, root: Path) -> str:
    candidates = [
        media_path.with_suffix(".txt"),
        root / (media_path.stem + ".txt"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace").strip()
    return ""


def _split_title(description: str, fallback: str) -> tuple[str, str]:
    lines = description.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip().lstrip("#").strip() or fallback
        rest = "\n".join(lines[1:]).strip()
        return title, rest
    return fallback, description


def _is_extra_file(path: Path) -> bool:
    stem = path.stem.lower()
    return (
        stem.startswith("cover")
        or stem.startswith("showcase")
        or stem.startswith("proof")
        or stem.startswith("banner")
    )


def _parse_lessons(root: Path, course_demo: bool) -> list[LessonFile]:
    media_files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in MEDIA_EXTENSIONS
        and not (p.suffix.lower() in PHOTO_EXTENSIONS and _is_extra_file(p))
    ]
    media_files.sort(key=lambda p: _lesson_order_key(p, root))

    lessons: list[LessonFile] = []
    for media_path in media_files:
        description = _read_lesson_text(media_path, root)
        fallback_title = _humanize(media_path.stem)
        title, description = _split_title(description, fallback_title)

        media_type = "video" if media_path.suffix.lower() in VIDEO_EXTENSIONS else "photo"
        is_demo = course_demo or media_path.stem.lower().startswith("demo")

        lessons.append(
            LessonFile(
                title=title,
                description=description,
                media_type=media_type,
                path=media_path,
                is_demo=is_demo,
            )
        )
    return lessons


def _parse_extras(root: Path) -> tuple[Path | None, list[Path]]:
    photos = sorted(
        (
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in PHOTO_EXTENSIONS and _is_extra_file(p)
        ),
        key=lambda p: _natural_key(p.name),
    )
    showcase: Path | None = None
    for path in photos:
        if path.stem.lower().startswith(("showcase", "cover")):
            showcase = path
            break
    proofs = [
        p
        for p in photos
        if p.stem.lower().startswith(("proof", "banner")) and p != showcase
    ]
    return showcase, proofs


def _process_sync(zip_path: str, extract_to: str) -> CoursePackage:
    zip_file = Path(zip_path)
    dest = Path(extract_to)

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    if not zipfile.is_zipfile(zip_file):
        raise ZipCourseError("The file is not a ZIP archive")

    _safe_extract(zip_file, dest)

    root = _find_content_root(dest)
    info_path = _find_info_file(root)
    if info_path is not None:
        root = info_path.parent

    info = _parse_info(info_path, fallback_name=zip_file.stem)
    lessons = _parse_lessons(root, course_demo=info["demo"])

    if not lessons:
        raise ZipCourseError("No video or image files found in the archive")

    showcase, proofs = _parse_extras(root)

    return CoursePackage(
        name=info["name"],
        description=info["description"],
        price=info["price"],
        root=root,
        lessons=lessons,
        showcase=showcase,
        proofs=proofs,
    )


async def process_course_zip(zip_path: str, extract_to: str) -> CoursePackage:
    """Extract a course ZIP and parse info.txt plus lesson file pairs."""
    return await asyncio.to_thread(_process_sync, zip_path, extract_to)


def cleanup(*paths: str | Path) -> None:
    for raw in paths:
        path = Path(raw)
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError:
            pass
