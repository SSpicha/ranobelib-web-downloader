#!/usr/bin/env python3
"""
Telegram bot UI for ranobelib-web downloader — full flow with team + chapter range + search + i18n + persistence.

Flow:
  /start -> main menu [Download] [Search] [Settings] [Web App]
  /search <query> -> list of novel matches with one-tap select
  send URL -> ask FORMAT [EPUB][FB2][TXT][HTML]
            -> ask DEVICE [XTEINK][Kindle][Mobile][Generic]
            -> ask IMAGES [Color][Grayscale][None]
            -> load novel -> ask TEAM [branch buttons] (or skip if 1 branch)
            -> ask RANGE (preset buttons or text input)
            -> run download with choices, deliver file/link

State & settings persisted in SQLite database (user_data/bot_users.db).
"""
import os
import sys
import logging
from pathlib import Path
import time
import threading
import asyncio

# --- logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).resolve().parent / "bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ranobelib_bot")

# Make src/ importable (project root has src/web_app.py)
_APP_ROOT = Path(__file__).resolve().parent.parent
_SRC = _APP_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, CallbackQuery,
    WebAppInfo, InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
)
from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from web_app import (
    run_download_task, tasks, tasks_lock, DOWNLOADS_DIR, _slug_from_url,
    api,
)
from db import init_db, get_user_settings, save_user_settings, get_user_state, save_user_state

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
TELEGRAM_DOC_LIMIT = 50 * 1024 * 1024

FORMATS = [("EPUB", "epub"), ("FB2", "fb2"), ("TXT", "txt"), ("HTML", "html")]
DEVICES = [
    ("📱 XTEINK", "x4_crosspoint"),
    ("📖 Kindle", "kindle"),
    ("📱 Mobile/Tablet", "phone"),
    ("💻 Generic", "generic"),
]
IMAGE_MODES = [
    ("🖼️ Кольорові", "images"),
    ("🔳 Ч/Б (e-Ink)", "grayscale"),
    ("🚫 Без картинок", "no_images"),
]

USER_STATE = {}
_STATE_LOCK = threading.Lock()

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# --- i18n Localization Dictionary ---
I18N = {
    "uk": {
        "start_title": "📚 <b>RanobeLIB бот</b>\nСкачуй ранобе з ranobelib.me прямо в Telegram.\n\n"
                       "🔹 Надішли посилання або назву новели\n"
                       "🔹 Обери формат, пристрій (📱 XTEINK / Kindle), команду перекладу\n"
                       "🔹 Отримай EPUB/FB2/TXT/HTML файлом\n\n"
                       "🔗 <a href=\"https://ranobelib.me\">ranobelib.me</a>\n"
                       "Команди: /start /search /cancel /help",
        "btn_download": "📥 Надіслати посилання",
        "btn_search": "🔍 Пошук новели",
        "btn_settings": "⚙️ Налаштування",
        "btn_cancel": "❌ Скасувати",
        "btn_app": "🚀 Відкрити Web App",
        "btn_all_teams": "🌐 Усі команди",
        "btn_range_all": "📚 Усі глави",
        "cancel_msg": "❌ Дію скасовано. Надішли посилання, назву або скористайся /search.",
        "help_msg": "📖 Як користуватися:\n"
                    "1. Надішли посилання (ranobelib.me) або команду /search <назва>\n"
                    "2. Обери формат (EPUB/FB2/TXT/HTML)\n"
                    "3. Обери пристрій (📱 XTEINK / 📖 Kindle / 💻 Generic)\n"
                    "4. Обери режим зображень (Кольорові / Ч/Б e-Ink / Без картинок)\n"
                    "5. Обери команду перекладу та діапазон глав\n"
                    "Команди: /start /search /cancel /help",
        "access_denied": "⛔ Доступ заборонено.",
        "err_general": "⚠️ Сталася помилка. Спробуйте /start.",
        "ask_url": "📥 Надішли посилання на новелу з ranobelib.me або використай /search <назва>",
        "ask_search": "🔍 Введи назву новели для пошуку (наприклад: <code>/search Спадкоємець</code>):",
        "search_results": "🔍 Результати пошуку за запитом <b>{query}</b>:",
        "search_empty": "❌ Нічого не знайдено за запитом <b>{query}</b>.",
        "ask_fmt": "📕 <b>{title}</b>\nОбери формат:",
        "ask_dev": "Формат: <b>{fmt}</b>\nОбери пристрій:",
        "ask_img": "Пристрій: <b>{device}</b>\nОбери режим зображень:",
        "ask_team": "Режим зображень: <b>{img}</b>\nОбери команду (переклад):",
        "ask_range": "Команда: <b>{team}</b>{rng_hint}\nВведи діапазон глав (наприклад '1-50') або натисни кнопку нижче (всього глав: {total}):",
        "invalid_url": "❌ Невірний формат посилання. Надішли посилання з ranobelib.me.",
        "invalid_range": "❌ Незрозумілий діапазон. Введи діапазон (напр. '1-50') або обрати кнопкою.",
        "waiting_range": "⚠️ Наразі очікується діапазон глав. Введи 'all' або '1-50'. Для скасування — /cancel.",
        "novel_load_err": "❌ Не вдалося завантажити інформацію: {err}\nНатисни /start.",
        "download_start": "⏳ Починаю скачування... Це може зайняти кілька хвилин.",
        "file_missing": "❌ Файл не знайдено на диску після генерації.",
        "file_too_large": "✅ Готово: {file_name} ({size} MB)\n⚠️ Файл >50MB. Обери менший діапазон глав або режим «Без картинок».",
        "file_too_large_url": "✅ Готово: {file_name} ({size} MB)\n🔗 {url}",
        "download_timeout": "⏱️ Таймаут (>30 хв).",
        "settings_info": "⚙️ Налаштування за замовчуванням:\nФормат: <b>{fmt}</b>\nПристрій: <b>{dev}</b>\nЗображення: <b>{img}</b>",
    },
    "ru": {
        "start_title": "📚 <b>RanobeLIB бот</b>\nСкачивай ранобэ с ranobelib.me прямо в Telegram.\n\n"
                       "🔹 Пришли ссылку или название новеллы\n"
                       "🔹 Выбери формат, устройство (📱 XTEINK / Kindle), команду перевода\n"
                       "🔹 Получи EPUB/FB2/TXT/HTML файлом\n\n"
                       "🔗 <a href=\"https://ranobelib.me\">ranobelib.me</a>\n"
                       "Команды: /start /search /cancel /help",
        "btn_download": "📥 Отправить ссылку",
        "btn_search": "🔍 Поиск новеллы",
        "btn_settings": "⚙️ Настройки",
        "btn_cancel": "❌ Отмена",
        "btn_app": "🚀 Открыть Web App",
        "btn_all_teams": "🌐 Все команды",
        "btn_range_all": "📚 Все главы",
        "cancel_msg": "❌ Действие отменено. Пришли ссылку, название или воспользуйся /search.",
        "help_msg": "📖 Как пользоваться:\n"
                    "1. Пришли ссылку (ranobelib.me) или команду /search <название>\n"
                    "2. Выбери формат (EPUB/FB2/TXT/HTML)\n"
                    "3. Выбери устройство (📱 XTEINK / 📖 Kindle / 💻 Generic)\n"
                    "4. Выбери режим картинок (Цветные / Ч/Б e-Ink / Без картинок)\n"
                    "5. Выбери команду перевода и диапазон глав\n"
                    "Команды: /start /search /cancel /help",
        "access_denied": "⛔ Доступ запрещен.",
        "err_general": "⚠️ Произошла ошибка. Попробуйте /start.",
        "ask_url": "📥 Пришли ссылку на новеллу с ranobelib.me или используй /search <название>",
        "ask_search": "🔍 Введи название новеллы для поиска (например: <code>/search Наследие</code>):",
        "search_results": "🔍 Результаты поиска по запросу <b>{query}</b>:",
        "search_empty": "❌ Ничего не найдено по запросу <b>{query}</b>.",
        "ask_fmt": "📕 <b>{title}</b>\nВыбери формат:",
        "ask_dev": "Формат: <b>{fmt}</b>\nВыбери устройство:",
        "ask_img": "Устройство: <b>{device}</b>\nВыбери режим изображений:",
        "ask_team": "Режим изображений: <b>{img}</b>\nВыбери команду (перевод):",
        "ask_range": "Команда: <b>{team}</b>{rng_hint}\nВведи диапазон глав (например '1-50') или выбери кнопкой (всего глав: {total}):",
        "invalid_url": "❌ Неверный формат ссылки. Пришли ссылку с ranobelib.me.",
        "invalid_range": "❌ Не понял. Введи диапазон (напр. '1-50') или нажми кнопку.",
        "waiting_range": "⚠️ Сейчас ожидается диапазон глав. Введи 'all' или '1-50'. Для отмены — /cancel.",
        "novel_load_err": "❌ Не удалось загрузить информацию: {err}\nНажми /start.",
        "download_start": "⏳ Начинаю скачивание... Это может занять несколько минут.",
        "file_missing": "❌ Файл не найден на диске после генерации.",
        "file_too_large": "✅ Готово: {file_name} ({size} MB)\n⚠️ Файл >50MB. Выбери меньший диапазон глав или режим «Без картинок».",
        "file_too_large_url": "✅ Готово: {file_name} ({size} MB)\n🔗 {url}",
        "download_timeout": "⏱️ Таймаут (>30 мин).",
        "settings_info": "⚙️ Настройки по умолчанию:\nФормат: <b>{fmt}</b>\nУстройство: <b>{dev}</b>\nИзображения: <b>{img}</b>",
    },
    "en": {
        "start_title": "📚 <b>RanobeLIB bot</b>\nDownload light novels from ranobelib.me directly in Telegram.\n\n"
                       "🔹 Send a URL or search by title\n"
                       "🔹 Select format, device (📱 XTEINK / Kindle), translation team\n"
                       "🔹 Get your EPUB/FB2/TXT/HTML file\n\n"
                       "🔗 <a href=\"https://ranobelib.me\">ranobelib.me</a>\n"
                       "Commands: /start /search /cancel /help",
        "btn_download": "📥 Send URL",
        "btn_search": "🔍 Search novel",
        "btn_settings": "⚙️ Settings",
        "btn_cancel": "❌ Cancel",
        "btn_app": "🚀 Open Web App",
        "btn_all_teams": "🌐 All teams",
        "btn_range_all": "📚 All chapters",
        "cancel_msg": "❌ Action cancelled. Send a URL, title or use /search.",
        "help_msg": "📖 How to use:\n"
                    "1. Send novel URL (ranobelib.me) or /search <query>\n"
                    "2. Select format (EPUB/FB2/TXT/HTML)\n"
                    "3. Select device (📱 XTEINK / 📖 Kindle / 💻 Generic)\n"
                    "4. Select image mode (Color / Grayscale e-Ink / No images)\n"
                    "5. Select translation team & chapter range\n"
                    "Commands: /start /search /cancel /help",
        "access_denied": "⛔ Access denied.",
        "err_general": "⚠️ An error occurred. Please try /start.",
        "ask_url": "📥 Please send a novel URL from ranobelib.me or use /search <query>",
        "ask_search": "🔍 Enter novel title to search (e.g. <code>/search Lord</code>):",
        "search_results": "🔍 Search results for <b>{query}</b>:",
        "search_empty": "❌ Nothing found for <b>{query}</b>.",
        "ask_fmt": "📕 <b>{title}</b>\nSelect format:",
        "ask_dev": "Format: <b>{fmt}</b>\nSelect device:",
        "ask_img": "Device: <b>{device}</b>\nSelect image mode:",
        "ask_team": "Image mode: <b>{img}</b>\nSelect translation team:",
        "ask_range": "Team: <b>{team}</b>{rng_hint}\nEnter chapter range (e.g. '1-50') or choose a preset below (total: {total}):",
        "invalid_url": "❌ Invalid link format. Send a valid link from ranobelib.me.",
        "invalid_range": "❌ Invalid range. Type a range like '1-50' or tap a button.",
        "waiting_range": "⚠️ Chapter range expected. Type 'all' or '1-50'. To cancel — /cancel.",
        "novel_load_err": "❌ Failed to load novel info: {err}\nPress /start.",
        "download_start": "⏳ Starting download... This may take a few minutes.",
        "file_missing": "❌ File not found on disk after generation.",
        "file_too_large": "✅ Done: {file_name} ({size} MB)\n⚠️ File >50MB. Choose fewer chapters or 'No images' mode.",
        "file_too_large_url": "✅ Done: {file_name} ({size} MB)\n🔗 {url}",
        "download_timeout": "⏱️ Timeout (>30 min).",
        "settings_info": "⚙️ Default settings:\nFormat: <b>{fmt}</b>\nDevice: <b>{dev}</b>\nImages: <b>{img}</b>",
    }
}


def _get_lang(user: types.User = None) -> str:
    if not user or not user.language_code:
        return "uk"
    code = user.language_code.lower()
    if code.startswith("uk"):
        return "uk"
    elif code.startswith("ru"):
        return "ru"
    elif code.startswith("en"):
        return "en"
    return "uk"


def _t(key: str, lang: str = "uk", **kwargs) -> str:
    dict_lang = I18N.get(lang, I18N["uk"])
    text = dict_lang.get(key) or I18N["uk"].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text


@dp.error()
async def global_error_handler(event: types.ErrorEvent):
    log.exception("Unhandled exception in handler: %s", event.exception)
    try:
        user = event.update.message.from_user if event.update.message else (
            event.update.callback_query.from_user if event.update.callback_query else None
        )
        lang = _get_lang(user)
        msg = _t("err_general", lang)
        if event.update.message:
            await event.update.message.answer(msg)
        elif event.update.callback_query:
            await event.update.callback_query.message.answer(msg)
    except Exception:
        pass


def _allowed(uid: int) -> bool:
    return not ALLOWED or str(uid) in ALLOWED


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_row(lang: str = "uk"):
    return [InlineKeyboardButton(text=_t("btn_cancel", lang), callback_data="act:cancel")]


def _fmt_kb(lang: str = "uk"):
    rows = [[InlineKeyboardButton(text=t, callback_data=f"fmt:{v}") for t, v in FORMATS]]
    rows.append(_cancel_row(lang))
    return _kb(rows)


def _dev_kb(lang: str = "uk"):
    r1 = [InlineKeyboardButton(text=t, callback_data=f"dev:{v}") for t, v in DEVICES[:2]]
    r2 = [InlineKeyboardButton(text=t, callback_data=f"dev:{v}") for t, v in DEVICES[2:]]
    rows = [r1, r2, _cancel_row(lang)]
    return _kb(rows)


def _img_kb(lang: str = "uk"):
    rows = [[InlineKeyboardButton(text=t, callback_data=f"img:{v}") for t, v in IMAGE_MODES]]
    rows.append(_cancel_row(lang))
    return _kb(rows)


def _team_kb(branches, lang: str = "uk"):
    rows = []
    for bid, info in branches.items():
        rng = info.get("range")
        if rng:
            label = f"{info['name']} ({rng[0]}–{rng[1]}, {info['chapter_count']} глав)"
        else:
            label = f"{info['name']} ({info['chapter_count']} глав)"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"team:{bid}")])
    rows.append([InlineKeyboardButton(text=_t("btn_all_teams", lang), callback_data="team:ALL")])
    rows.append(_cancel_row(lang))
    return _kb(rows)


def _range_kb(total: int, lang: str = "uk"):
    rows = [
        [InlineKeyboardButton(text=_t("btn_range_all", lang), callback_data="rng:ALL")]
    ]
    if total >= 50:
        r2 = [InlineKeyboardButton(text="📖 1-50", callback_data="rng:1-50")]
        if total >= 100:
            r2.append(InlineKeyboardButton(text="📖 1-100", callback_data="rng:1-100"))
        rows.append(r2)
    if total >= 200:
        rows.append([
            InlineKeyboardButton(text="📖 1-200", callback_data="rng:1-200"),
            InlineKeyboardButton(text=f"📖 100-{total}", callback_data=f"rng:100-{total}"),
        ])
    rows.append(_cancel_row(lang))
    return _kb(rows)


def _main_kb(lang: str = "uk"):
    rows = [
        [
            InlineKeyboardButton(text=_t("btn_download", lang), callback_data="act:download"),
            InlineKeyboardButton(text=_t("btn_search", lang), callback_data="act:search"),
        ],
        [InlineKeyboardButton(text=_t("btn_settings", lang), callback_data="act:settings")],
    ]
    if PUBLIC_BASE:
        rows.append([InlineKeyboardButton(text=_t("btn_app", lang), web_app=WebAppInfo(url=PUBLIC_BASE))])
    rows.append(_cancel_row(lang))
    return _kb(rows)


async def _safe_edit(c: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    """Safely edit text or caption depending on whether message contains media."""
    try:
        if c.message.photo or c.message.video or c.message.document:
            await c.message.edit_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await c.message.edit_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        log.warning("Safe edit TelegramBadRequest: %s", e)
        try:
            await c.message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            pass
    except Exception as e:
        log.warning("Safe edit failed: %s", e)


@dp.message(Command("start"))
async def cmd_start(m: types.Message):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        await m.answer(_t("access_denied", lang))
        return
    USER_STATE.pop(m.from_user.id, None)
    save_user_state(m.from_user.id, None)
    await m.answer(
        _t("start_title", lang),
        reply_markup=_main_kb(lang),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message(Command("cancel"))
async def cmd_cancel(m: types.Message):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        return
    USER_STATE.pop(m.from_user.id, None)
    save_user_state(m.from_user.id, None)
    await m.answer(_t("cancel_msg", lang))


@dp.message(Command("help"))
async def cmd_help(m: types.Message):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        return
    await m.answer(_t("help_msg", lang))


@dp.message(Command("search"))
async def cmd_search(m: types.Message, command: CommandObject):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        return
    query = (command.args or "").strip()
    if not query:
        await m.answer(_t("ask_search", lang), parse_mode="HTML")
        return

    await m.answer(f"⏳ Пошук: <b>{query}</b>...", parse_mode="HTML")
    results = await asyncio.to_thread(api.search_novels, query, 8)
    if not results:
        await m.answer(_t("search_empty", lang, query=query), parse_mode="HTML")
        return

    rows = []
    for item in results:
        title = item.get("rus_name") or item.get("eng_name") or item.get("slug")
        slug = item.get("slug")
        if slug:
            rows.append([InlineKeyboardButton(text=f"📕 {title}", callback_data=f"sel_slug:{slug}")])
    rows.append(_cancel_row(lang))

    await m.answer(
        _t("search_results", lang, query=query),
        reply_markup=_kb(rows),
        parse_mode="HTML",
    )


@dp.inline_query()
async def inline_search(iq: InlineQuery):
    query = iq.query.strip()
    if not query or len(query) < 2:
        return
    results = await asyncio.to_thread(api.search_novels, query, 5)
    articles = []
    for idx, item in enumerate(results):
        title = item.get("rus_name") or item.get("eng_name") or item.get("slug")
        slug = item.get("slug")
        url = f"https://ranobelib.me/ru/book/{slug}" if slug else ""
        cover = (item.get("cover") or {}).get("default")
        desc = item.get("summary", "")[:100] if item.get("summary") else "RanobeLIB novel"
        articles.append(
            InlineQueryResultArticle(
                id=str(idx),
                title=title,
                description=desc,
                thumbnail_url=cover,
                input_message_content=InputTextMessageContent(
                    message_text=url,
                    disable_web_page_preview=False,
                ),
            )
        )
    await iq.answer(articles, cache_time=60)


@dp.callback_query(F.data.startswith("sel_slug:"))
async def select_searched_slug(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    slug = c.data.split(":", 1)[1]
    url = f"https://ranobelib.me/ru/book/{slug}"

    USER_STATE[uid] = {"step": "fmt", "url": url, "slug": slug, "lang": lang}
    save_user_state(uid, USER_STATE[uid])

    await _safe_edit(
        c,
        _t("ask_fmt", lang, title=slug),
        reply_markup=_fmt_kb(lang),
    )
    await c.answer()


@dp.message(F.text)
async def handle_text(m: types.Message):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        return
    uid = m.from_user.id
    st = USER_STATE.get(uid) or get_user_state(uid) or {}

    # step: waiting for chapter range
    if st.get("step") == "range":
        raw = m.text.strip().lower()
        if "ranobelib" in raw or ("/" in raw and "-" not in raw and not raw.isdigit()):
            await m.answer(_t("waiting_range", lang))
            return
        chapters = _parse_range(raw, st.get("total_chapters", 0))
        if chapters is None:
            await m.answer(_t("invalid_range", lang))
            return
        st["chapters"] = chapters
        st["step"] = "run"
        await m.answer(_t("download_start", lang))
        loop = asyncio.get_event_loop()
        outcome = await loop.run_in_executor(None, _do_download, uid, m, loop, lang)
        await _deliver(m, outcome, lang)
        USER_STATE.pop(uid, None)
        save_user_state(uid, None)
        return

    # default: treat as URL or Search Query
    text = m.text.strip()
    if "ranobelib" in text:
        slug = _slug_from_url(text)
        if not slug:
            await m.answer(_t("invalid_url", lang))
            return
        USER_STATE[uid] = {"step": "fmt", "url": text, "slug": slug, "lang": lang}
        save_user_state(uid, USER_STATE[uid])
        await m.answer(_t("ask_fmt", lang, title=slug), reply_markup=_fmt_kb(lang), parse_mode="HTML")
    else:
        # Auto search if query is non-URL text
        results = await asyncio.to_thread(api.search_novels, text, 5)
        if results:
            rows = []
            for item in results:
                title = item.get("rus_name") or item.get("eng_name") or item.get("slug")
                slug = item.get("slug")
                if slug:
                    rows.append([InlineKeyboardButton(text=f"📕 {title}", callback_data=f"sel_slug:{slug}")])
            rows.append(_cancel_row(lang))
            await m.answer(_t("search_results", lang, query=text), reply_markup=_kb(rows), parse_mode="HTML")
        else:
            await m.answer(_t("ask_url", lang))


def _parse_range(raw: str, total: int):
    """Return list of {volume,number} or 'ALL' marker or None if invalid."""
    if raw in ("all", "все", "усі", "*"):
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
    lang = _get_lang(c.from_user)
    if not _allowed(c.from_user.id):
        await c.answer("⛔", show_alert=True)
        return
    action = c.data.split(":", 1)[1]
    if action == "cancel":
        USER_STATE.pop(c.from_user.id, None)
        save_user_state(c.from_user.id, None)
        await _safe_edit(c, _t("cancel_msg", lang))
        await c.answer()
        return
    if action == "download":
        await c.message.answer(_t("ask_url", lang))
        await c.answer()
    elif action == "search":
        await c.message.answer(_t("ask_search", lang), parse_mode="HTML")
        await c.answer()
    elif action == "settings":
        st = get_user_settings(c.from_user.id)
        await c.message.answer(
            _t("settings_info", lang, fmt=st.get('fmt', 'epub').upper(), dev=st.get('device', 'generic'), img=st.get('images_mode', 'images')),
            parse_mode="HTML",
        )
        await c.answer()


@dp.callback_query(F.data.startswith("fmt:"))
async def choose_fmt(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "fmt":
        await c.answer(_t("invalid_url", lang), show_alert=True)
        return
    USER_STATE[uid]["fmt"] = c.data.split(":", 1)[1]
    USER_STATE[uid]["step"] = "dev"
    save_user_state(uid, USER_STATE[uid])

    await _safe_edit(
        c,
        _t("ask_dev", lang, fmt=USER_STATE[uid]['fmt'].upper()),
        reply_markup=_dev_kb(lang),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("dev:"))
async def choose_dev(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "dev":
        await c.answer(_t("invalid_url", lang), show_alert=True)
        return
    USER_STATE[uid]["device"] = c.data.split(":", 1)[1]
    USER_STATE[uid]["step"] = "img"
    save_user_state(uid, USER_STATE[uid])

    await _safe_edit(
        c,
        _t("ask_img", lang, device=USER_STATE[uid]['device']),
        reply_markup=_img_kb(lang),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("img:"))
async def choose_img(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "img":
        await c.answer(_t("invalid_url", lang), show_alert=True)
        return
    img_mode = c.data.split(":", 1)[1]
    USER_STATE[uid]["images_mode"] = img_mode
    USER_STATE[uid]["step"] = "team"
    save_user_state(uid, USER_STATE[uid])

    slug = USER_STATE[uid]["slug"]
    try:
        info = await asyncio.to_thread(api.get_novel_info, slug)
        from web_app import normalize_novel_info, get_formatted_branches_with_teams
        info = normalize_novel_info(info)
        chapters = await asyncio.to_thread(api.get_novel_chapters, slug)
        branches = get_formatted_branches_with_teams(info, chapters)
        for bid in branches:
            nums = [ch.get("number") for ch in chapters
                    if any(b.get("branch_id") == bid for b in (ch.get("branches") or []))]
            branches[bid]["range"] = (min(nums), max(nums)) if nums else None
        USER_STATE[uid]["total_chapters"] = len(chapters)
        USER_STATE[uid]["branches"] = branches
        USER_STATE[uid]["chapters_data"] = chapters
        USER_STATE[uid]["novel_info"] = info
    except Exception as e:
        USER_STATE.pop(uid, None)
        save_user_state(uid, None)
        await _safe_edit(c, _t("novel_load_err", lang, err=e))
        await c.answer()
        return

    if not branches:
        USER_STATE[uid]["branch_id"] = None
        USER_STATE[uid]["step"] = "range"
        total = USER_STATE[uid].get("total_chapters", 0)
        await _safe_edit(
            c,
            _t("ask_range", lang, team="—", rng_hint="", total=total),
            reply_markup=_range_kb(total, lang),
        )
        await c.answer()
        return

    cover = (info.get("cover") or {}).get("default") or (info.get("cover") or {}).get("thumbnail")
    title = info.get("rus_name") or info.get("eng_name") or USER_STATE[uid]["slug"]
    cap = f"📕 <b>{title}</b>\n" + _t("ask_team", lang, img=USER_STATE[uid]['images_mode'])

    try:
        if cover:
            await c.message.answer_photo(cover, caption=cap, reply_markup=_team_kb(branches, lang), parse_mode="HTML")
        else:
            await c.message.answer(cap, reply_markup=_team_kb(branches, lang), parse_mode="HTML")
    except Exception:
        await c.message.answer(cap, reply_markup=_team_kb(branches, lang), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("team:"))
async def choose_team(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "team":
        await c.answer(_t("invalid_url", lang), show_alert=True)
        return
    bid = c.data.split(":", 1)[1]
    if bid == "ALL":
        USER_STATE[uid]["branch_id"] = None
        USER_STATE[uid]["team_name"] = None
    else:
        USER_STATE[uid]["branch_id"] = bid
        USER_STATE[uid]["team_name"] = USER_STATE[uid]["branches"].get(bid, {}).get("name")
    USER_STATE[uid]["step"] = "range"
    save_user_state(uid, USER_STATE[uid])

    total = USER_STATE[uid].get("total_chapters", 0)
    br = USER_STATE[uid]["branches"].get(bid, {}) if bid != "ALL" else {}
    rng = br.get("range")
    rng_hint = f" (главы {rng[0]}–{rng[1]})" if rng else ""
    name = _t("btn_all_teams", lang) if bid == "ALL" else br.get("name", bid)

    await _safe_edit(
        c,
        _t("ask_range", lang, team=name, rng_hint=rng_hint, total=total),
        reply_markup=_range_kb(total, lang),
    )
    await c.answer()


@dp.callback_query(F.data.startswith("rng:"))
async def choose_range_preset(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "range":
        await c.answer(_t("invalid_url", lang), show_alert=True)
        return

    val = c.data.split(":", 1)[1]
    chapters = _parse_range(val, USER_STATE[uid].get("total_chapters", 0))
    if chapters is None:
        await c.answer(_t("invalid_range", lang), show_alert=True)
        return

    USER_STATE[uid]["chapters"] = chapters
    USER_STATE[uid]["step"] = "run"
    await _safe_edit(c, _t("download_start", lang))
    await c.answer()

    loop = asyncio.get_event_loop()
    outcome = await loop.run_in_executor(None, _do_download, uid, c.message, loop, lang)
    await _deliver(c.message, outcome, lang)
    USER_STATE.pop(uid, None)
    save_user_state(uid, None)


async def _deliver(m: types.Message, outcome: str, lang: str = "uk"):
    if outcome.startswith("__FILE__:"):
        fname = outcome.split(":", 1)[1]
        fpath = DOWNLOADS_DIR / fname
        if not fpath.exists():
            log.error("deliver: file missing %s", fpath)
            await m.answer(_t("file_missing", lang))
            return
        await m.answer_document(FSInputFile(fpath), caption=f"✅ {fname}")
    else:
        await m.answer(outcome)


def _do_download(uid: int, m: types.Message = None, loop=None, lang: str = "uk") -> str:
    with _STATE_LOCK:
        st = USER_STATE.get(uid, {}).copy()
    slug = st.get("slug")
    if not slug:
        return _t("invalid_url", lang)
    fmt = st.get("fmt", "epub")
    dev = st.get("device", "generic")
    img_mode = st.get("images_mode", "images")
    branch_id = st.get("branch_id")
    chapters = st.get("chapters", "ALL")

    branches = st.get("branches", {})
    if branch_id is not None and branch_id != "ALL" and branch_id not in branches:
        log.warning("invalid branch_id %s, falling back to None", branch_id)
        branch_id = None

    if branch_id and branch_id in branches:
        team_name = branches[branch_id].get("name", branch_id)
    elif branch_id is None and branches:
        team_name = _t("btn_all_teams", lang)
    else:
        team_name = "—"

    if chapters == "ALL":
        rng = _t("btn_range_all", lang)
    elif isinstance(chapters, list):
        nums = [c.get("number") for c in chapters if isinstance(c, dict)]
        rng = f"глави {nums[0]}-{nums[-1]}" if nums else "выбранный диапазон"
    else:
        rng = "выбранный диапазон"

    log.info("download start uid=%s slug=%s fmt=%s dev=%s img=%s branch=%s ch=%s", uid, slug, fmt, dev, img_mode, branch_id, chapters)

    def _bar(pct: int) -> str:
        pct = max(0, min(100, pct))
        filled = pct // 10
        return "[" + "▓" * filled + "░" * (10 - filled) + f"] {pct}%"

    last_edit_time = 0.0

    def _edit(text):
        nonlocal last_edit_time
        now = time.time()
        if now - last_edit_time < 3.5 and "100%" not in text:
            return
        last_edit_time = now

        if m is not None and loop is not None:
            try:
                if m.photo or m.video or m.document:
                    asyncio.run_coroutine_threadsafe(m.edit_caption(caption=text, parse_mode="HTML"), loop)
                else:
                    asyncio.run_coroutine_threadsafe(m.edit_text(text, parse_mode="HTML"), loop)
            except Exception as e:
                log.debug("Progress edit skipped: %s", e)

    task_id = f"tg_{uid}_{int(time.time()*1000)}"
    selected_team_keys = (
        [f"{st['team_name']}::{branch_id}"] if (st.get("team_name") and branch_id) else []
    )

    images_enabled = img_mode != "no_images"
    grayscale_enabled = img_mode == "grayscale"

    body = {
        "slug": slug,
        "format": fmt,
        "profile": dev,
        "cover": True,
        "images": images_enabled,
        "grayscale": grayscale_enabled,
        "compress": True,
        "branch_id": branch_id,
        "selected_team_keys": selected_team_keys,
        "chapters": [] if chapters == "ALL" else chapters,
    }

    # Save user preferences for future downloads
    save_user_settings(uid, {"lang": lang, "fmt": fmt, "device": dev, "images_mode": img_mode})

    with tasks_lock:
        tasks[task_id] = {
            "status": "processing", "progress": 0,
            "processed_chapters": 0, "total_chapters": 0,
            "created_at": time.time(),
        }
    threading.Thread(target=run_download_task, args=(task_id, body), daemon=True).start()
    _edit(f"⏳ <b>{team_name}</b> | {rng}\nПодготовка...")
    deadline = time.time() + 1800
    last_pct = -1

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
                return _t("file_missing", lang)
            fpath = DOWNLOADS_DIR / file_name
            size = fpath.stat().st_size if fpath.exists() else 0
            log.info("download done uid=%s file=%s size=%d", uid, file_name, size)
            if PUBLIC_BASE and size > TELEGRAM_DOC_LIMIT:
                return _t("file_too_large_url", lang, file_name=file_name, size=size//1024//1024, url=f"{PUBLIC_BASE}/api/files/{file_name}")
            if size <= TELEGRAM_DOC_LIMIT:
                return f"__FILE__:{file_name}"
            return _t("file_too_large", lang, file_name=file_name, size=size//1024//1024)
        pct = t.get("progress", 0)
        if pct != last_pct:
            last_pct = pct
            _edit(f"⏳ <b>{team_name}</b> | {rng}\n{_bar(pct)}")
        time.sleep(3)

    log.warning("download timeout uid=%s", uid)
    return _t("download_timeout", lang)


async def main():
    init_db()
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    log.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
