#!/usr/bin/env python3
"""
Telegram bot UI for ranobelib-web downloader.

Full inline-keyboard flow:
  /start -> main menu [Download] [Settings]
  send URL -> ask FORMAT [EPUB][FB2][TXT][HTML]
            -> ask DEVICE [XTEINK][Generic]
            -> run download with choices, live progress, deliver file/link

State is kept per-chat in USER_STATE (in-memory; fine for single-instance bot).
"""
import time
import threading
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup,
    FSInputFile, CallbackQuery,
)
from aiogram import F

# Reuse core download logic
from web_app import run_download_task, tasks, tasks_lock, DOWNLOADS_DIR, _slug_from_url

import os

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024

FORMATS = [("EPUB", "epub"), ("FB2", "fb2"), ("TXT", "txt"), ("HTML", "html")]
DEVICES = [("📱 XTEINK", "x4_crosspoint"), ("💻 Generic", "generic")]

# user_id -> dict(step, url, slug, fmt, device)
USER_STATE = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def _allowed(uid: int) -> bool:
    return not ALLOWED or str(uid) in ALLOWED


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt_kb():
    return _kb([[InlineKeyboardButton(text=t, callback_data=f"fmt:{v}") for t, v in FORMATS]])


def _dev_kb():
    return _kb([[InlineKeyboardButton(text=t, callback_data=f"dev:{v}") for t, v in DEVICES]])


def _main_kb():
    return _kb([
        [InlineKeyboardButton(text="📥 Скачати новеллу", callback_data="act:download")],
        [InlineKeyboardButton(text="⚙️ Налаштування", callback_data="act:settings")],
    ])


@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    if not _allowed(m.from_user.id):
        await m.answer("⛔ Доступ запрещен.")
        return
    USER_STATE.pop(m.from_user.id, None)
    await m.answer(
        "📚 RanobeLIB бот\nОбери действие или пришли ссылку на новеллу с ranobelib.me.",
        reply_markup=_main_kb(),
    )


@dp.message(F.text)
async def handle_text(m: types.Message):
    if not _allowed(m.from_user.id):
        return
    url = m.text.strip()
    if "ranobelib" not in url:
        await m.answer("Пришли ссылку на новеллу (ranobelib.me) или нажми 📥 Скачати.")
        return
    slug = _slug_from_url(url)
    if not slug:
        await m.answer("❌ Неверный формат ссылки.")
        return
    USER_STATE[m.from_user.id] = {"step": "fmt", "url": url, "slug": slug}
    await m.answer(
        f"📕 <b>{slug}</b>\nВыбери формат:",
        reply_markup=_fmt_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("act:"))
async def act_menu(c: CallbackQuery):
    if not _allowed(c.from_user.id):
        await c.answer("⛔", show_alert=True)
        return
    action = c.data.split(":", 1)[1]
    if action == "download":
        await c.message.answer("📥 Пришли ссылку на новеллу с ranobelib.me")
        await c.answer()
    elif action == "settings":
        st = USER_STATE.get(c.from_user.id, {})
        cur_fmt = st.get("fmt", "epub")
        cur_dev = st.get("device", "generic")
        await c.message.answer(
            f"⚙️ Настройки по умолчанию:\nФормат: <b>{cur_fmt}</b>\nУстройство: <b>{cur_dev}</b>\n"
            "Меняются при каждой загрузке через кнопки.",
            parse_mode="HTML",
        )
        await c.answer()


@dp.callback_query(F.data.startswith("fmt:"))
async def choose_fmt(c: CallbackQuery):
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "fmt":
        await c.answer("⚠️ Начни с ссылки.", show_alert=True)
        return
    fmt = c.data.split(":", 1)[1]
    USER_STATE[uid]["fmt"] = fmt
    USER_STATE[uid]["step"] = "dev"
    await c.message.edit_text(
        f"Формат: <b>{fmt}</b>\nТеперь выбери устройство:",
        reply_markup=_dev_kb(),
        parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data.startswith("dev:"))
async def choose_dev(c: CallbackQuery):
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "dev":
        await c.answer("⚠️ Начни с ссылки.", show_alert=True)
        return
    dev = c.data.split(":", 1)[1]
    USER_STATE[uid]["device"] = dev
    USER_STATE[uid]["step"] = "run"
    await c.message.edit_text("⏳ Начинаю скачивание... Это может занять несколько минут.")

    # run blocking work off event loop
    loop = asyncio.get_event_loop()
    outcome = await loop.run_in_executor(None, _do_download, uid)
    if outcome.startswith("__FILE__:"):
        fname = outcome.split(":", 1)[1]
        fpath = DOWNLOADS_DIR / fname
        await c.message.answer_document(FSInputFile(fpath), caption=f"✅ {fname}")
    else:
        await c.message.answer(outcome)
    USER_STATE.pop(uid, None)


def _do_download(uid: int) -> str:
    st = USER_STATE.get(uid, {})
    slug = st.get("slug")
    if not slug:
        return "❌ Нет ссылки. Начни заново."
    fmt = st.get("fmt", "epub")
    dev = st.get("device", "generic")
    task_id = f"tg_{uid}_{int(time.time()*1000)}"
    body = {
        "slug": slug,
        "format": fmt,
        "profile": dev,
        "cover": True,
        "images": True,
        "compress": True,
        "chapters": [],
    }
    with tasks_lock:
        tasks[task_id] = {
            "status": "processing", "progress": 0,
            "processed_chapters": 0, "total_chapters": 0,
            "created_at": time.time(),
        }
    threading.Thread(target=run_download_task, args=(task_id, body), daemon=True).start()

    # wait + live update
    msg = None
    deadline = time.time() + 1800
    last_pct = -1
    while time.time() < deadline:
        with tasks_lock:
            t = tasks.get(task_id)
        if not t:
            break
        if t.get("status") == "error":
            return f"❌ Ошибка: {t.get('error', 'неизвестно')}"
        if t.get("status") == "done":
            file_name = t.get("file")
            if not file_name:
                return "❌ Файл не создан."
            fpath = DOWNLOADS_DIR / file_name
            size = fpath.stat().st_size if fpath.exists() else 0
            if PUBLIC_BASE and size > TELEGRAM_DOC_LIMIT:
                return f"✅ Готово: {file_name} ({size//1024//1024} MB)\n🔗 {PUBLIC_BASE}/api/files/{file_name}"
            if size <= TELEGRAM_DOC_LIMIT:
                return f"__FILE__:{file_name}"
            return f"✅ Готово: {file_name} ({size//1024//1024} MB)\n(>50MB, качай через веб: {PUBLIC_BASE})"
        pct = t.get("progress", 0)
        if pct != last_pct:
            last_pct = pct
            try:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    _update_progress(coroutine_target=None), loop=None
                )
            except Exception:
                pass
        time.sleep(5)
    return "⏱️ Таймаут (>30 мин)."


# placeholder for progress update (kept simple: no live edit to avoid races)
async def _update_progress(_):
    return


async def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
