#!/usr/bin/env python3
"""
Telegram bot UI for ranobelib-web downloader — full flow with team + chapter range.

Flow:
  /start -> main menu [Download] [Settings]
  send URL -> ask FORMAT [EPUB][FB2][TXT][HTML]
            -> ask DEVICE [XTEINK][Generic]
            -> load novel -> ask TEAM [branch buttons]  (or skip if 1 branch)
            -> ask RANGE  (text: "all" or "1-50" or "10-10")
            -> run download with choices, deliver file/link

State kept per-chat in USER_STATE (in-memory; fine for single-instance bot).
"""
import os
import sys
import logging
from pathlib import Path

# --- logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler(Path(__file__).resolve().parent / "bot.log", encoding="utf-8"),
              logging.StreamHandler()],
)
log = logging.getLogger("ranobelib_bot")

# Make src/ importable (project root has src/web_app.py)
_APP_ROOT = Path(__file__).resolve().parent.parent
_SRC = _APP_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import time
import threading
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, CallbackQuery
from aiogram import F

from web_app import (
    run_download_task, tasks, tasks_lock, DOWNLOADS_DIR, _slug_from_url,
    api,
)

import os

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024

FORMATS = [("EPUB", "epub"), ("FB2", "fb2"), ("TXT", "txt"), ("HTML", "html")]
DEVICES = [("📱 XTEINK", "x4_crosspoint"), ("💻 Generic", "generic")]

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


def _team_kb(branches):
    # branches: dict branch_id -> {name, chapter_count, team_names}
    rows = []
    for bid, info in branches.items():
        label = f"{info['name']} ({info['chapter_count']})"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"team:{bid}")])
    # also "all teams / default" option
    rows.append([InlineKeyboardButton(text="🌐 Все команды", callback_data="team:ALL")])
    return _kb(rows)


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


@dp.message(Command("cancel"))
async def cmd_cancel(m: types.Message):
    if not _allowed(m.from_user.id):
        return
    USER_STATE.pop(m.from_user.id, None)
    await m.answer("❌ Действие отменено. Пришли ссылку или нажми 📥 Скачати.")


@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    if not _allowed(m.from_user.id):
        return
    await m.answer(
        "📖 Как пользоваться:\n"
        "1. Пришли ссылку на новеллу (ranobelib.me)\n"
        "2. Выбери формат (EPUB/FB2/TXT/HTML)\n"
        "3. Выбери устройство (📱 XTEINK / 💻 Generic)\n"
        "4. Выбери команду перевода\n"
        "5. Введи диапазон глав: 'all' или '1-50'\n"
        "Команды: /start /cancel /help",
    )


@dp.message(F.text)
async def handle_text(m: types.Message):
    if not _allowed(m.from_user.id):
        return
    uid = m.from_user.id
    st = USER_STATE.get(uid, {})

    # step: waiting for chapter range
    if st.get("step") == "range":
        raw = m.text.strip().lower()
        # strictly accept only 'all' / numbers / a-b ; ignore URLs or stray text
        if "ranobelib" in raw or ("/" in raw and "-" not in raw and not raw.isdigit()):
            await m.answer("⚠️ Сейчас ожидается диапазон глав. Введи 'all' или '1-50'. Для отмены — /cancel.")
            return
        chapters = _parse_range(raw, st.get("total_chapters", 0))
        if chapters is None:
            await m.answer("❌ Не понял. Введи 'all' или диапазон, напр. '1-50'. Или /cancel.")
            return
        st["chapters"] = chapters
        st["step"] = "run"
        await m.answer("⏳ Начинаю скачивание... Это может занять несколько минут.")
        outcome = await asyncio.get_event_loop().run_in_executor(None, _do_download, uid)
        await _deliver(m, outcome)
        USER_STATE.pop(uid, None)
        return

    # default: treat as URL
    url = m.text.strip()
    if "ranobelib" not in url:
        await m.answer("Пришли ссылку на новеллу (ranobelib.me) или нажми 📥 Скачати.")
        return
    slug = _slug_from_url(url)
    if not slug:
        await m.answer("❌ Неверный формат ссылки.")
        return
    USER_STATE[uid] = {"step": "fmt", "url": url, "slug": slug}
    await m.answer(f"📕 <b>{slug}</b>\nВыбери формат:", reply_markup=_fmt_kb(), parse_mode="HTML")


def _parse_range(raw: str, total: int):
    """Return list of {volume,number} or 'ALL' marker or None if invalid."""
    if raw in ("all", "все", "*"):
        return "ALL"
    if "-" not in raw:
        try:
            n = int(raw)
            return [{"volume": "0", "number": n}]
        except ValueError:
            return None
    try:
        a, b = raw.split("-", 1)
        a, b = int(a), int(b)
        if a > b or a < 1:
            return None
        out = []
        for n in range(a, min(b, total or b) + 1):
            out.append({"volume": "0", "number": n})
        return out
    except ValueError:
        return None


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
        await c.message.answer(
            f"⚙️ Последний выбор:\nФормат: <b>{st.get('fmt','epub')}</b>\n"
            f"Устройство: <b>{st.get('device','generic')}</b>\nМеняется кнопками при загрузке.",
            parse_mode="HTML",
        )
        await c.answer()


@dp.callback_query(F.data.startswith("fmt:"))
async def choose_fmt(c: CallbackQuery):
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "fmt":
        await c.answer("⚠️ Начни с ссылки.", show_alert=True)
        return
    USER_STATE[uid]["fmt"] = c.data.split(":", 1)[1]
    USER_STATE[uid]["step"] = "dev"
    await c.message.edit_text(
        f"Формат: <b>{USER_STATE[uid]['fmt']}</b>\nВыбери устройство:",
        reply_markup=_dev_kb(), parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data.startswith("dev:"))
async def choose_dev(c: CallbackQuery):
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "dev":
        await c.answer("⚠️ Начни с ссылки.", show_alert=True)
        return
    USER_STATE[uid]["device"] = c.data.split(":", 1)[1]
    USER_STATE[uid]["step"] = "team"
    # load novel branches (teams)
    slug = USER_STATE[uid]["slug"]
    try:
        info = api.get_novel_info(slug)
        from web_app import normalize_novel_info, get_formatted_branches_with_teams
        info = normalize_novel_info(info)
        chapters = api.get_novel_chapters(slug)
        branches = get_formatted_branches_with_teams(info, chapters)
        USER_STATE[uid]["total_chapters"] = len(chapters)
        USER_STATE[uid]["branches"] = branches
    except Exception as e:
        await c.message.edit_text(f"❌ Не удалось загрузить информацию: {e}")
        await c.answer()
        return
    if not branches:
        # no teams -> skip to range
        USER_STATE[uid]["branch_id"] = None
        USER_STATE[uid]["step"] = "range"
        await c.message.edit_text(
            f"Устройство: <b>{USER_STATE[uid]['device']}</b>\nВведи диапазон глав: 'all' или '1-50'",
            parse_mode="HTML",
        )
        await c.answer()
        return
    await c.message.edit_text(
        f"Устройство: <b>{USER_STATE[uid]['device']}</b>\nВыбери команду (перевод):",
        reply_markup=_team_kb(branches), parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data.startswith("team:"))
async def choose_team(c: CallbackQuery):
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "team":
        await c.answer("⚠️ Начни с ссылки.", show_alert=True)
        return
    bid = c.data.split(":", 1)[1]
    USER_STATE[uid]["branch_id"] = None if bid == "ALL" else bid
    USER_STATE[uid]["step"] = "range"
    total = USER_STATE[uid].get("total_chapters", 0)
    await c.message.edit_text(
        f"Команда: <b>{'все' if bid=='ALL' else USER_STATE[uid]['branches'].get(bid,{}).get('name',bid)}</b>\n"
        f"Введи диапазон глав: 'all' или '1-{total}'",
        parse_mode="HTML",
    )
    await c.answer()


async def _deliver(m: types.Message, outcome: str):
    if outcome.startswith("__FILE__:"):
        fname = outcome.split(":", 1)[1]
        fpath = DOWNLOADS_DIR / fname
        if not fpath.exists():
            log.error("deliver: file missing %s", fpath)
            await m.answer("❌ Файл не найден на диске после генерации.")
            return
        await m.answer_document(FSInputFile(fpath), caption=f"✅ {fname}")
    else:
        await m.answer(outcome)


def _do_download(uid: int) -> str:
    st = USER_STATE.get(uid, {})
    slug = st.get("slug")
    if not slug:
        return "❌ Нет ссылки. Начни заново."
    fmt = st.get("fmt", "epub")
    dev = st.get("device", "generic")
    branch_id = st.get("branch_id")
    chapters = st.get("chapters", "ALL")
    log.info("download start uid=%s slug=%s fmt=%s dev=%s branch=%s ch=%s", uid, slug, fmt, dev, branch_id, chapters)
    task_id = f"tg_{uid}_{int(time.time()*1000)}"
    body = {
        "slug": slug,
        "format": fmt,
        "profile": dev,
        "cover": True,
        "images": True,
        "compress": True,
        "branch_id": branch_id,
        "chapters": [] if chapters == "ALL" else chapters,
    }
    with tasks_lock:
        tasks[task_id] = {
            "status": "processing", "progress": 0,
            "processed_chapters": 0, "total_chapters": 0,
            "created_at": time.time(),
        }
    threading.Thread(target=run_download_task, args=(task_id, body), daemon=True).start()
    deadline = time.time() + 1800
    while time.time() < deadline:
        with tasks_lock:
            t = tasks.get(task_id)
        if not t:
            break
        if t.get("status") == "error":
            log.error("download error uid=%s: %s", uid, t.get("error"))
            return f"❌ Ошибка: {t.get('error', 'неизвестно')}"
        if t.get("status") == "done":
            file_name = t.get("file")
            if not file_name:
                log.error("download done but no file uid=%s", uid)
                return "❌ Файл не создан."
            fpath = DOWNLOADS_DIR / file_name
            size = fpath.stat().st_size if fpath.exists() else 0
            log.info("download done uid=%s file=%s size=%d", uid, file_name, size)
            if PUBLIC_BASE and size > TELEGRAM_DOC_LIMIT:
                return f"✅ Готово: {file_name} ({size//1024//1024} MB)\n🔗 {PUBLIC_BASE}/api/files/{file_name}"
            if size <= TELEGRAM_DOC_LIMIT:
                return f"__FILE__:{file_name}"
            return f"✅ Готово: {file_name} ({size//1024//1024} MB)\n(>50MB, качай через веб: {PUBLIC_BASE})"
        time.sleep(5)
    log.warning("download timeout uid=%s", uid)
    return "⏱️ Таймаут (>30 мин)."


async def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
