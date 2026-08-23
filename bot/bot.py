#!/usr/bin/env python3
"""
Telegram bot for ranobelib-web downloader.

Runs alongside the web app on the same Oracle instance. Reuses the existing
core: `run_download_task` from web_app builds the book exactly like the web UI.

Env vars:
  TELEGRAM_BOT_TOKEN  - from @BotFather (required)
  ALLOWED_CHAT_IDS     - comma-separated chat_ids allowed to use the bot (security)
  PUBLIC_BASE_URL      - http://<instance-ip> (used to send file links when >50MB)
  DOWNLOAD_FORMAT      - epub|fb2|html|txt (default epub)
"""
import os
import sys
import time
import threading
import asyncio
from pathlib import Path

# Make src importable (same layout as web_app)
APP_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = APP_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from web_app import run_download_task, tasks, tasks_lock, DOWNLOADS_DIR, _slug_from_url

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram import F

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
FMT = os.environ.get("DOWNLOAD_FORMAT", "epub").lower()

TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024  # 50 MB

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def _allowed(user_id: int) -> bool:
    if not ALLOWED:
        return True  # no allowlist configured = open (not recommended)
    return str(user_id) in ALLOWED


def _wait_for_task(task_id: str, timeout: int = 1800) -> dict:
    """Poll tasks dict until done/error. Returns the task entry."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with tasks_lock:
            t = tasks.get(task_id)
            if t and t.get("status") in ("done", "error"):
                return t
        time.sleep(5)
    return {}


def _do_download(url: str) -> str:
    """Returns a human-readable status / file link."""
    slug = _slug_from_url(url)
    if not slug:
        return "❌ Неверный формат ссылки. Пришли URL вида https://ranobelib.me/ru/book/<slug>"
    task_id = f"tg_{int(time.time()*1000)}"
    body = {
        "slug": slug,
        "format": FMT,
        "profile": "generic",
        "cover": True,
        "images": True,
        "compress": True,
        "chapters": [],
    }
    with tasks_lock:
        tasks[task_id] = {
            "status": "processing",
            "progress": 0,
            "processed_chapters": 0,
            "total_chapters": 0,
            "created_at": time.time(),
        }
    # Run blocking build in a thread (same as web app)
    threading.Thread(target=run_download_task, args=(task_id, body), daemon=True).start()

    result = _wait_for_task(task_id)
    if not result:
        return "⏱️ Таймаут генерации (более 30 мин). Попробуй позже или меньшую новеллу."
    if result.get("status") == "error":
        return f"❌ Ошибка: {result.get('error', 'неизвестно')}"

    file_name = result.get("file")
    if not file_name:
        return "❌ Файл не создан."
    fpath = DOWNLOADS_DIR / file_name
    size = fpath.stat().st_size if fpath.exists() else 0

    if PUBLIC_BASE and size > TELEGRAM_DOC_LIMIT:
        return f"✅ Готово: {file_name} ({size//1024//1024} MB)\n🔗 {PUBLIC_BASE}/api/files/{file_name}"
    if size <= TELEGRAM_DOC_LIMIT:
        return f"__FILE__:{file_name}"  # signal to send as document
    return f"✅ Готово: {file_name} ({size//1024//1024} MB)\n(файл >50MB, скачай через веб: {PUBLIC_BASE})"


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not _allowed(message.from_user.id):
        await message.answer("⛔ Доступ запрещен.")
        return
    await message.answer(
        "📚 RanobeLIB бот.\nПришли ссылку на новеллу с ranobelib.me — и я скачаю её в EPUB.\n"
        "Например: https://ranobelib.me/ru/book/tainted-requiem"
    )


@dp.message(F.text)
async def handle_url(message: types.Message):
    if not _allowed(message.from_user.id):
        return
    url = message.text.strip()
    if "ranobelib" not in url:
        await message.answer("Пришли ссылку на новеллу (ranobelib.me).")
        return
    await message.answer("⏳ Начинаю скачивание... Это может занять несколько минут.")
    # run blocking work off the event loop
    loop = asyncio.get_event_loop()
    outcome = await loop.run_in_executor(None, _do_download, url)
    if outcome.startswith("__FILE__:"):
        fname = outcome.split(":", 1)[1]
        fpath = DOWNLOADS_DIR / fname
        await message.answer_document(types.FSInputFile(fpath), caption=f"✅ {fname}")
    else:
        await message.answer(outcome)


async def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
