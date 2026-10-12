import shutil
import time
from datetime import datetime, timezone

import psutil

from config import BASE_DIR
from database.store import (
    backend_name,
    count_banned,
    count_methods,
    count_users,
    list_admins,
    list_methods,
    list_requests,
    list_stuffs,
    sum_balances,
)

_STARTED_AT = datetime.now(timezone.utc)


def uptime_text(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    if not days:
        parts.append(f"{secs}s")
    return " ".join(parts)


def _gb(value: float) -> str:
    return f"{value / (1024 ** 3):.1f} GB"


def vps_text() -> str:
    cpu_percent = psutil.cpu_percent(interval=0.3)
    cores = psutil.cpu_count(logical=True) or 0

    ram = psutil.virtual_memory()
    disk = shutil.disk_usage(str(BASE_DIR))

    process = psutil.Process()
    proc_mem = process.memory_info().rss
    proc_cpu = process.cpu_percent(interval=0.0)

    system_uptime = time.time() - psutil.boot_time()
    bot_uptime = (datetime.now(timezone.utc) - _STARTED_AT).total_seconds()

    disk_used_pct = disk.used / disk.total * 100 if disk.total else 0.0

    return (
        "💻 <b>VPS Usage</b>\n"
        "──────────────────────\n"
        f"🧠 CPU: <b>{cpu_percent}%</b> ({cores} cores)\n"
        f"RAM: <b>{_gb(ram.used)}</b> / {_gb(ram.total)} ({ram.percent}%)\n"
        f"💾 Disk ({BASE_DIR.drive or 'C:'}): <b>{_gb(disk.used)}</b> / "
        f"{_gb(disk.total)} ({disk_used_pct:.0f}%)\n"
        f" free: {_gb(disk.free)}\n\n"
        f"🤖 Bot process:\n"
        f"   RAM: {proc_mem / (1024 ** 2):.0f} MB, CPU: {proc_cpu}%\n"
        f"   PID: {process.pid}\n\n"
        f"🖥 System uptime: <b>{uptime_text(system_uptime)}</b>\n"
        f"⏱ Bot uptime: <b>{uptime_text(bot_uptime)}</b>\n"
        f"🗄 Database: <b>{backend_name()}</b>"
    )


async def stats_text() -> str:
    users = await count_users()
    banned = await count_banned()
    admins = len(await list_admins())
    stuffs = await list_stuffs()
    lessons = sum(len(s.get("lessons", [])) for s in stuffs)
    methods = await count_methods()
    active_methods = len(await list_methods(active_only=True))
    requests = await list_requests()
    pending = sum(1 for r in requests if r.get("status") == "pending")
    approved = sum(1 for r in requests if r.get("status") == "approved")
    rejected = sum(
        1 for r in requests if r.get("status") in {"rejected", "failed"}
    )
    revenue = sum(
        float(r.get("amount", 0) or 0)
        for r in requests
        if r.get("status") == "approved"
    )
    wallets = await sum_balances()
    bot_uptime = (datetime.now(timezone.utc) - _STARTED_AT).total_seconds()

    return (
        "📊 <b>Bot Stats</b>\n"
        "──────────────────────\n"
        f"👥 Users: <b>{users}</b> (banned: {banned})\n"
        f"👤 Admins: {admins}\n"
        f"📦 Stuffs: <b>{len(stuffs)}</b> (media: {lessons})\n"
        f"💳 Methods: {methods} (active: {active_methods})\n"
        "──────────────────────\n"
        f"🧾 Requests: pending {pending} / approved {approved} / "
        f"rejected {rejected}\n"
        f"💰 Revenue: <b>Rs.{revenue:g}</b>\n"
        f"👛 Wallet balances: Rs.{wallets:g}\n"
        "──────────────────────\n"
        f"⏱ Bot uptime: {uptime_text(bot_uptime)}\n"
        f"🗄 Database: <b>{backend_name()}</b>"
    )
