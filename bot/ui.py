#!/usr/bin/env python3
"""
Telegram bot UI for ranobelib-web downloader — full flow with search, multi-volume split, subscriptions, interactive settings, navigation & i18n.

Flow:
  /start -> main menu [Download] [Search] [Subscriptions] [Settings] [Web App]
  /search <query> -> paginated list of novel matches with one-tap select
  /subscriptions -> list user subscriptions with unsubscribe option
  /login <token> -> set RanobeLIB access token for restricted chapters
  send URL -> ask FORMAT [EPUB][FB2][TXT][HTML]
            -> ask DEVICE [XTEINK][Kindle][Mobile][Generic]
            -> ask IMAGES [Color][Grayscale][None]
            -> load novel -> ask TEAM [branch buttons]
            -> ask RANGE (presets, per-volume split, 150-ch chunks, or text)
            -> run download with choices, deliver file(s)

Includes navigation back buttons [⬅️ Назад] and interactive settings menu.
State & settings persisted in SQLite database (user_data/bot_users.db).
"""
import os
import sys
import socket
import logging
from pathlib import Path
import time
import threading
import asyncio
import math
from html import escape

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
    BotCommand,
)
from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from web_app import (
    run_download_task, tasks, tasks_lock, DOWNLOADS_DIR, _slug_from_url,
    api,
)
from db import (
    init_db, get_user_settings, save_user_settings, set_user_token,
    get_user_state, save_user_state, add_subscription, remove_subscription,
    get_user_subscriptions, get_all_subscriptions, update_subscription_ch,
)

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

# Per-language labels for stored enum values (avoid mixing languages in UI text).
IMG_LABELS = {
    "uk": {"images": "🖼️ Кольорові", "grayscale": "🔳 Ч/Б (e-Ink)", "no_images": "🚫 Без картинок"},
    "ru": {"images": "🖼️ Цветные", "grayscale": "🔳 Ч/Б (e-Ink)", "no_images": "🚫 Без картинок"},
    "en": {"images": "🖼️ Color", "grayscale": "🔳 Grayscale (e-Ink)", "no_images": "🚫 No images"},
}
STATUS_LABELS = {
    "uk": {2: "🔵 Завершено", 1: "🟢 Триває"},
    "ru": {2: "🔵 Завершено", 1: "🟢 Продолжается"},
    "en": {2: "🔵 Completed", 1: "🟢 Ongoing"},
}


def _img_label(mode: str, lang: str = "uk") -> str:
    return IMG_LABELS.get(lang, IMG_LABELS["uk"]).get(mode, mode)


def _status_label(status_id, lang: str = "uk") -> str:
    try:
        sid = int(status_id)
    except (TypeError, ValueError):
        sid = None
    return STATUS_LABELS.get(lang, STATUS_LABELS["uk"]).get(sid, "🟢 Триває")


DEV_LABELS = {
    "uk": {"x4_crosspoint": "📱 XTEINK", "kindle": "📖 Kindle", "phone": "📱 Телефон/Планшет", "generic": "💻 Загальний"},
    "ru": {"x4_crosspoint": "📱 XTEINK", "kindle": "📖 Kindle", "phone": "📱 Телефон/Планшет", "generic": "💻 Общий"},
    "en": {"x4_crosspoint": "📱 XTEINK", "kindle": "📖 Kindle", "phone": "📱 Phone/Tablet", "generic": "💻 Generic"},
}


def _dev_label(slug: str, lang: str = "uk") -> str:
    return DEV_LABELS.get(lang, DEV_LABELS["uk"]).get(slug, slug)

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
                       "🔹 Отримай EPUB/FB2/TXT/HTML файлом (підтримка авто-розбиття по томах)\n\n"
                       "🔗 <a href=\"https://ranobelib.me\">ranobelib.me</a>\n"
                       "Команди: /start /search /subscriptions /login /cancel /help",
        "btn_download": "📥 Надіслати посилання",
        "btn_search": "🔍 Пошук новели",
        "btn_subs": "📌 Мої підписки",
        "btn_settings": "⚙️ Налаштування",
        "btn_cancel": "❌ Скасувати",
        "btn_back": "⬅️ Назад",
        "btn_app": "🚀 Відкрити Web App",
        "set_fmt": "📄 Формат",
        "set_dev": "📱 Пристрій",
        "set_img": "🖼️ Зображення",
        "set_lang": "🌐 Мова",
        "btn_all_teams": "🌐 Усі команди",
        "ch_label": "{n} глава",
        "ch_count": "{n} глав",
        "rng_hint_tmpl": " (глави {a}–{b})",
        "token_on": "✅ Налаштовано",
        "token_off": "❌ Не налаштовано (/login)",
        "btn_subscribe": "📌 Підписатися на новелу",
        "err_load": "помилка завантаження",
        "btn_range_all": "📚 Усі глави (одним файлом)",
        "btn_split_vol": "📦 Розбити по томах",
        "btn_split_chunk": "📦 Розбити по 150 глав",
        "rng_all_split": "Розбиття ({mode})",
        "rng_chapters": "глави {a}-{b}",
        "rng_selected": "вибраний діапазон",
        "split_volume": "volume",
        "split_chunk": "chunk",
        "preparing": "Підготовка...",
        "dl_error": "❌ Помилка: {err}",
        "cancel_msg": "❌ Дію скасовано. Надішли посилання, назву або скористайся /search.",
        "help_msg": "📖 Як користуватися:\n"
                    "1. Надішли посилання (ranobelib.me) або команду /search <назва>\n"
                    "2. Обери формат (EPUB/FB2/TXT/HTML)\n"
                    "3. Обери пристрій (📱 XTEINK / 📖 Kindle / 💻 Generic)\n"
                    "4. Обери режим зображень (Кольорові / Ч/Б e-Ink / Без картинок)\n"
                    "5. Обери команду перекладу та варіант розбиття глав (по томах / 150 глав / все)\n"
                    "Команди: /start /search /subscriptions /login <token> /cancel /help",
        "access_denied": "⛔ Доступ заборонено.",
        "err_general": "⚠️ Сталася помилка. Спробуйте /start.",
        "err_unknown": "невідомо",
        "ask_url": "📥 Надішли посилання на новелу з ranobelib.me або використай <code>/search назва</code>",
        "ask_search": "🔍 Введи назву новели для пошуку (наприклад: <code>/search Спадкоємець</code>):",
        "search_results": "🔍 Результати пошуку за запитом <b>{query}</b> (Стор. {page}/{total_pages}):",
        "search_empty": "❌ Нічого не знайдено за запитом <b>{query}</b>.",
        "ask_fmt": "📕 <b>{title}</b>\nОбери формат:",
        "fmt_prompt": "📕 <b>{title}</b>\n⭐ Рейтинг: <b>{rating}</b> | {status}\n📝 {summary}\n\nОбери формат:",
        "ask_dev": "Формат: <b>{fmt}</b>\nОбери пристрій:",
        "ask_img": "Пристрій: <b>{device}</b>\nОбери режим зображень:",
        "ask_team": "Режим зображень: <b>{img}</b>\nОбери команду (переклад):",
        "rating_label": "Рейтинг",
        "team_caption": "📕 <b>{title}</b>\n⭐ {rating_label}: <b>{rating}</b> | {status}\n{ask_team}",
        "ask_range": "Команда: <b>{team}</b>{rng_hint}\nВведи діапазон глав (наприклад '1-50') або натисни кнопку нижче (всього глав: {total}):",
        "invalid_url": "❌ Невірний формат посилання. Надішли посилання з ranobelib.me.",
        "invalid_range": "❌ Незрозумілий діапазон. Введи діапазон (напр. '1-50') або обрати кнопкою.",
        "waiting_range": "⚠️ Наразі очікується діапазон глав. Введи 'all' або '1-50'. Для скасування — /cancel.",
        "novel_load_err": "❌ Не вдалося завантажити інформацію: {err}\nНатисни /start.",
        "download_start": "⏳ Починаю скачування... Це може зайняти кілька хвилин.",
        "file_missing": "❌ Файл не знайдено на диску після генерації.",
        "file_too_large": "✅ Готово: {file_name} ({size} MB)\n⚠️ Файл >50MB. Обери розбиття «📦 По томах» або режим «Без картинок».",
        "file_too_large_url": "✅ Готово: {file_name} ({size} MB)\n🔗 {url}",
        "download_timeout": "⏱️ Таймаут (>30 хв).",
        "settings_info": "⚙️ <b>Інтерактивне Меню Налаштувань:</b>\nНатисніть на кнопку, щоб змінити значення за замовчуванням.\n\n"
                         "📄 Формат: <b>{fmt}</b>\n"
                         "📱 Пристрій: <b>{dev}</b>\n"
                         "🖼️ Зображення: <b>{img}</b>\n"
                         "🌐 Мова: <b>{lang}</b>\n"
                         "🔑 Токен: <b>{token_status}</b>",
        "sub_added": "📌 Успішно підписано на новелу <b>{title}</b>! Ви отримуватимете сповіщення про нові глави.",
        "sub_removed": "🔕 Підписку на <b>{title}</b> скасовано.",
        "sub_notify": "🔔 <b>Нові глави!</b>\nНовела: <b>{title}</b>\nНова глава: {max_ch}",
        "subs_list": "📌 <b>Ваші підписки:</b>\n\n{items}",
        "subs_empty": "📌 У вас немає активних підписок. Щоб підписатися, шукайте новелу через /search.",
        "token_saved": "🔑 Токен авторизації успішно збережено!",
        "token_ask": "🔑 Введіть свій RanobeLIB токен командою: <code>/login YOUR_TOKEN</code>",
    },
    "ru": {
        "start_title": "📚 <b>RanobeLIB бот</b>\nСкачивай ранобэ с ranobelib.me прямо в Telegram.\n\n"
                       "🔹 Пришли ссылку или название новеллы\n"
                       "🔹 Выбери формат, устройство (📱 XTEINK / Kindle), команду перевода\n"
                       "🔹 Получи EPUB/FB2/TXT/HTML файлом (поддержка авто-разбиения по томам)\n\n"
                       "🔗 <a href=\"https://ranobelib.me\">ranobelib.me</a>\n"
                       "Команды: /start /search /subscriptions /login /cancel /help",
        "btn_download": "📥 Отправить ссылку",
        "btn_search": "🔍 Поиск новеллы",
        "btn_subs": "📌 Мои подписки",
        "btn_settings": "⚙️ Настройки",
        "btn_cancel": "❌ Отмена",
        "btn_back": "⬅️ Назад",
        "btn_app": "🚀 Открыть Web App",
        "set_fmt": "📄 Формат",
        "set_dev": "📱 Устройство",
        "set_img": "🖼️ Изображения",
        "set_lang": "🌐 Язык",
        "btn_all_teams": "🌐 Все команды",
        "ch_label": "{n} глава",
        "ch_count": "{n} глав",
        "rng_hint_tmpl": " (главы {a}–{b})",
        "token_on": "✅ Настроено",
        "token_off": "❌ Не настроено (/login)",
        "btn_subscribe": "📌 Подписаться на новеллу",
        "err_load": "ошибка загрузки",
        "btn_range_all": "📚 Все главы (одним файлом)",
        "btn_split_vol": "📦 Разбить по томам",
        "btn_split_chunk": "📦 Разбить по 150 глав",
        "rng_all_split": "Разбиение ({mode})",
        "rng_chapters": "главы {a}-{b}",
        "rng_selected": "выбранный диапазон",
        "split_volume": "volume",
        "split_chunk": "chunk",
        "preparing": "Подготовка...",
        "dl_error": "❌ Ошибка: {err}",
        "cancel_msg": "❌ Действие отменено. Пришли ссылку, название или воспользуйся /search.",
        "help_msg": "📖 Как пользоваться:\n"
                    "1. Пришли ссылку (ranobelib.me) или команду /search <название>\n"
                    "2. Выбери формат (EPUB/FB2/TXT/HTML)\n"
                    "3. Выбери устройство (📱 XTEINK / 📖 Kindle / 💻 Generic)\n"
                    "4. Выбери режим картинок (Цветные / Ч/Б e-Ink / Без картинок)\n"
                    "5. Выбери команду перевода и вариант разбиения (по томам / 150 глав / все)\n"
                    "Команды: /start /search /subscriptions /login <token> /cancel /help",
        "access_denied": "⛔ Доступ запрещен.",
        "err_general": "⚠️ Произошла ошибка. Попробуйте /start.",
        "err_unknown": "неизвестно",
        "ask_url": "📥 Пришли ссылку на новеллу с ranobelib.me или используй <code>/search название</code>",
        "ask_search": "🔍 Введи название новеллы для поиска (например: <code>/search Наследие</code>):",
        "search_results": "🔍 Результаты поиска по запросу <b>{query}</b> (Стр. {page}/{total_pages}):",
        "search_empty": "❌ Ничего не найдено по запросу <b>{query}</b>.",
        "ask_fmt": "📕 <b>{title}</b>\nВыбери формат:",
        "fmt_prompt": "📕 <b>{title}</b>\n⭐ Рейтинг: <b>{rating}</b> | {status}\n📝 {summary}\n\nВыбери формат:",
        "ask_dev": "Формат: <b>{fmt}</b>\nВыбери устройство:",
        "ask_img": "Устройство: <b>{device}</b>\nВыбери режим изображений:",
        "ask_team": "Режим изображений: <b>{img}</b>\nВыбери команду (перевод):",
        "rating_label": "Рейтинг",
        "team_caption": "📕 <b>{title}</b>\n⭐ {rating_label}: <b>{rating}</b> | {status}\n{ask_team}",
        "ask_range": "Команда: <b>{team}</b>{rng_hint}\nВведи диапазон глав (например '1-50') или выбери кнопкой (всего глав: {total}):",
        "invalid_url": "❌ Неверный формат ссылки. Пришли ссылку с ranobelib.me.",
        "invalid_range": "❌ Не понял. Введи диапазон (напр. '1-50') или нажми кнопку.",
        "waiting_range": "⚠️ Сейчас ожидается диапазон глав. Введи 'all' или '1-50'. Для отмены — /cancel.",
        "novel_load_err": "❌ Не удалось загрузить информацию: {err}\nНажми /start.",
        "download_start": "⏳ Начинаю скачивание... Это может занять несколько минут.",
        "file_missing": "❌ Файл не найден на диске после генерации.",
        "file_too_large": "✅ Готово: {file_name} ({size} MB)\n⚠️ Файл >50MB. Выбери разбиение «📦 По томам» или режим «Без картинок».",
        "file_too_large_url": "✅ Готово: {file_name} ({size} MB)\n🔗 {url}",
        "download_timeout": "⏱️ Таймаут (>30 мин).",
        "settings_info": "⚙️ <b>Интерактивное Меню Настроек:</b>\nНажмите на кнопку, чтобы изменить значение по умолчанию.\n\n"
                         "📄 Формат: <b>{fmt}</b>\n"
                         "📱 Устройство: <b>{dev}</b>\n"
                         "🖼️ Изображения: <b>{img}</b>\n"
                         "🌐 Язык: <b>{lang}</b>\n"
                         "🔑 Токен: <b>{token_status}</b>",
        "sub_added": "📌 Успешно подписаны на новеллу <b>{title}</b>! Вы будете получать уведомления о новых главах.",
        "sub_removed": "🔕 Подписка на <b>{title}</b> отменена.",
        "sub_notify": "🔔 <b>Новые главы!</b>\nНовелла: <b>{title}</b>\nНовая глава: {max_ch}",
        "subs_list": "📌 <b>Ваши подписки:</b>\n\n{items}",
        "subs_empty": "📌 У вас нет активных подписок. Чтобы подписаться, ищите новеллу через /search.",
        "token_saved": "🔑 Токен авторизации успешно сохранен!",
        "token_ask": "🔑 Введите свой RanobeLIB токен командой: <code>/login YOUR_TOKEN</code>",
    },
    "en": {
        "start_title": "📚 <b>RanobeLIB bot</b>\nDownload light novels from ranobelib.me directly in Telegram.\n\n"
                       "🔹 Send a URL or search by title\n"
                       "🔹 Select format, device (📱 XTEINK / Kindle), translation team\n"
                       "🔹 Get your EPUB/FB2/TXT/HTML file (auto multi-volume split supported)\n\n"
                       "🔗 <a href=\"https://ranobelib.me\">ranobelib.me</a>\n"
                       "Commands: /start /search /subscriptions /login /cancel /help",
        "btn_download": "📥 Send URL",
        "btn_search": "🔍 Search novel",
        "btn_subs": "📌 My Subscriptions",
        "btn_settings": "⚙️ Settings",
        "btn_cancel": "❌ Cancel",
        "btn_back": "⬅️ Back",
        "btn_app": "🚀 Open Web App",
        "set_fmt": "📄 Format",
        "set_dev": "📱 Device",
        "set_img": "🖼️ Images",
        "set_lang": "🌐 Language",
        "btn_all_teams": "🌐 All teams",
        "ch_label": "{n} chapter",
        "ch_count": "{n} chapters",
        "rng_hint_tmpl": " (chapters {a}–{b})",
        "token_on": "✅ Set",
        "token_off": "❌ Not set (/login)",
        "btn_subscribe": "📌 Subscribe to novel",
        "err_load": "load error",
        "btn_range_all": "📚 All chapters (single file)",
        "btn_split_vol": "📦 Split by Volumes",
        "btn_split_chunk": "📦 Split per 150 chapters",
        "rng_all_split": "Split ({mode})",
        "rng_chapters": "chapters {a}-{b}",
        "rng_selected": "selected range",
        "split_volume": "volume",
        "split_chunk": "chunk",
        "preparing": "Preparing...",
        "dl_error": "❌ Error: {err}",
        "cancel_msg": "❌ Action cancelled. Send a URL, title or use /search.",
        "help_msg": "📖 How to use:\n"
                    "1. Send novel URL (ranobelib.me) or /search <query>\n"
                    "2. Select format (EPUB/FB2/TXT/HTML)\n"
                    "3. Select device (📱 XTEINK / 📖 Kindle / 💻 Generic)\n"
                    "4. Select image mode (Color / Grayscale e-Ink / No images)\n"
                    "5. Select translation team & split option (by volumes / 150 chapters / all)\n"
                    "Commands: /start /search /subscriptions /login <token> /cancel /help",
        "access_denied": "⛔ Access denied.",
        "err_general": "⚠️ An error occurred. Please try /start.",
        "err_unknown": "unknown",
        "ask_url": "📥 Please send a novel URL from ranobelib.me or use <code>/search query</code>",
        "ask_search": "🔍 Enter novel title to search (e.g. <code>/search Lord</code>):",
        "search_results": "🔍 Search results for <b>{query}</b> (Page {page}/{total_pages}):",
        "search_empty": "❌ Nothing found for <b>{query}</b>.",
        "ask_fmt": "📕 <b>{title}</b>\nSelect format:",
        "fmt_prompt": "📕 <b>{title}</b>\n⭐ Rating: <b>{rating}</b> | {status}\n📝 {summary}\n\nSelect format:",
        "ask_dev": "Format: <b>{fmt}</b>\nSelect device:",
        "ask_img": "Device: <b>{device}</b>\nSelect image mode:",
        "ask_team": "Image mode: <b>{img}</b>\nSelect translation team:",
        "rating_label": "Rating",
        "team_caption": "📕 <b>{title}</b>\n⭐ {rating_label}: <b>{rating}</b> | {status}\n{ask_team}",
        "ask_range": "Team: <b>{team}</b>{rng_hint}\nEnter chapter range (e.g. '1-50') or choose a preset below (total: {total}):",
        "invalid_url": "❌ Invalid link format. Send a valid link from ranobelib.me.",
        "invalid_range": "❌ Invalid range. Type a range like '1-50' or tap a button.",
        "waiting_range": "⚠️ Chapter range expected. Type 'all' or '1-50'. To cancel — /cancel.",
        "novel_load_err": "❌ Failed to load novel info: {err}\nPress /start.",
        "download_start": "⏳ Starting download... This may take a few minutes.",
        "file_missing": "❌ File not found on disk after generation.",
        "file_too_large": "✅ Done: {file_name} ({size} MB)\n⚠️ File >50MB. Choose '📦 Split by Volumes' or 'No images' mode.",
        "file_too_large_url": "✅ Done: {file_name} ({size} MB)\n🔗 {url}",
        "download_timeout": "⏱️ Timeout (>30 min).",
        "settings_info": "⚙️ <b>Interactive Settings Menu:</b>\nTap a button to toggle your default options.\n\n"
                         "📄 Format: <b>{fmt}</b>\n"
                         "📱 Device: <b>{dev}</b>\n"
                         "🖼️ Images: <b>{img}</b>\n"
                         "🌐 Language: <b>{lang}</b>\n"
                         "🔑 Auth Token: <b>{token_status}</b>",
        "sub_added": "📌 Successfully subscribed to <b>{title}</b>! You will receive notifications for new chapters.",
        "sub_removed": "🔕 Subscription to <b>{title}</b> cancelled.",
        "sub_notify": "🔔 <b>New chapters!</b>\nNovel: <b>{title}</b>\nNew chapter: {max_ch}",
        "subs_list": "📌 <b>Your Subscriptions:</b>\n\n{items}",
        "subs_empty": "📌 You have no active subscriptions. Use /search to find and subscribe to novels.",
        "token_saved": "🔑 Authorization token successfully saved!",
        "token_ask": "🔑 Enter your RanobeLIB token via: <code>/login YOUR_TOKEN</code>",
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


def _cancel_row(lang: str = "uk", back_step: str = None):
    row = []
    if back_step:
        row.append(InlineKeyboardButton(text=_t("btn_back", lang), callback_data=f"nav_back:{back_step}"))
    row.append(InlineKeyboardButton(text=_t("btn_cancel", lang), callback_data="act:cancel"))
    return row


def _fmt_kb(lang: str = "uk", back_step: str = None):
    rows = [[InlineKeyboardButton(text=t, callback_data=f"fmt:{v}") for t, v in FORMATS]]
    rows.append(_cancel_row(lang, back_step))
    return _kb(rows)


def _fmt_text(lang: str, st: dict) -> str:
    """Caption/text for the FORMAT step: novel info if already loaded, else just slug."""
    info = st.get("novel_info")
    if info:
        title = info.get("rus_name") or info.get("eng_name") or st.get("slug", "")
        rating = info.get("rating", {}).get("average") or "—"
        status_str = _status_label(info.get("status", {}).get("id"), lang)
        summary = (info.get("summary") or "").strip()
        summary = summary[:500] if summary else "—"
        return _t(
            "fmt_prompt", lang,
            title=escape(title), rating=rating, status=status_str, summary=escape(summary),
        )
    return _t("ask_fmt", lang, title=st.get("slug", ""))


async def _ensure_novel_loaded(uid: int, slug: str) -> bool:
    """Load novel info + branches into USER_STATE[uid]; cache so later steps skip API."""
    st = USER_STATE.get(uid) or {}
    if st.get("novel_info") and st.get("branches") is not None:
        return True
    try:
        from web_app import normalize_novel_info, get_formatted_branches_with_teams
        info = await asyncio.to_thread(api.get_novel_info, slug)
        info = normalize_novel_info(info)
        chapters = await asyncio.to_thread(api.get_novel_chapters, slug)
        branches = get_formatted_branches_with_teams(info, chapters)
        for bid in branches:
            nums = [ch.get("number") for ch in chapters
                    if any(b.get("branch_id") == bid for b in (ch.get("branches") or []))]
            branches[bid]["range"] = (min(nums), max(nums)) if nums else None
        st["total_chapters"] = len(chapters)
        st["branches"] = branches
        st["chapters_data"] = chapters
        st["novel_info"] = info
        save_user_state(uid, st)
        return True
    except Exception as e:
        log.warning("Novel load failed for %s: %s", slug, e)
        return False


def _dev_kb(lang: str = "uk", back_step: str = "fmt"):
    r1 = [InlineKeyboardButton(text=t, callback_data=f"dev:{v}") for t, v in DEVICES[:2]]
    r2 = [InlineKeyboardButton(text=t, callback_data=f"dev:{v}") for t, v in DEVICES[2:]]
    rows = [r1, r2, _cancel_row(lang, back_step)]
    return _kb(rows)


def _img_kb(lang: str = "uk", back_step: str = "dev"):
    rows = [[InlineKeyboardButton(text=t, callback_data=f"img:{v}") for t, v in IMAGE_MODES]]
    rows.append(_cancel_row(lang, back_step))
    return _kb(rows)


def _team_kb(branches, lang: str = "uk", back_step: str = "img"):
    rows = []
    for bid, info in branches.items():
        team_names = info.get("team_names") or []
        if len(team_names) > 1:
            # Multi-team branch: one button per translation team.
            rows.append([InlineKeyboardButton(
                text=f"📚 {info['name']}", callback_data="noop")])
            for ti, tname in enumerate(team_names):
                cnt = info.get("chapter_count", 0)
                rows.append([InlineKeyboardButton(
                    text=f"  • {tname} ({_t('ch_count', lang, n=cnt)})",
                    callback_data=f"team:{bid}:{ti}")])
        else:
            rng = info.get("range")
            cc = info.get("chapter_count", 0)
            if rng:
                label = f"{info['name']} ({rng[0]}–{rng[1]}, {_t('ch_count', lang, n=cc)})"
            else:
                label = f"{info['name']} ({_t('ch_count', lang, n=cc)})"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"team:{bid}")])
    rows.append([InlineKeyboardButton(text=_t("btn_all_teams", lang), callback_data="team:ALL")])
    rows.append(_cancel_row(lang, back_step))
    return _kb(rows)


def _range_kb(total: int, lang: str = "uk", back_step: str = "team"):
    rows = [
        [InlineKeyboardButton(text=_t("btn_range_all", lang), callback_data="rng:ALL")],
        [
            InlineKeyboardButton(text=_t("btn_split_vol", lang), callback_data="rng:SPLIT_VOL"),
            InlineKeyboardButton(text=_t("btn_split_chunk", lang), callback_data="rng:SPLIT_CHUNK"),
        ]
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
    rows.append(_cancel_row(lang, back_step))
    return _kb(rows)


def _settings_kb(uid: int, lang: str = "uk"):
    st = get_user_settings(uid)
    fmt_label = f"{_t('set_fmt', lang)}: {st.get('fmt', 'epub').upper()}"
    dev_label = f"{_t('set_dev', lang)}: {_dev_label(st.get('device', 'generic'), lang)}"
    img_label = f"{_t('set_img', lang)}: {_img_label(st.get('images_mode', 'images'), lang)}"
    lang_label = f"{_t('set_lang', lang)}: {lang.upper()}"

    rows = [
        [InlineKeyboardButton(text=fmt_label, callback_data="toggle_set:fmt")],
        [InlineKeyboardButton(text=dev_label, callback_data="toggle_set:dev")],
        [InlineKeyboardButton(text=img_label, callback_data="toggle_set:img")],
        [InlineKeyboardButton(text=lang_label, callback_data="toggle_set:lang")],
        _cancel_row(lang),
    ]
    return _kb(rows)


def _main_kb(lang: str = "uk"):
    rows = [
        [
            InlineKeyboardButton(text=_t("btn_download", lang), callback_data="act:download"),
            InlineKeyboardButton(text=_t("btn_search", lang), callback_data="act:search"),
        ],
        [
            InlineKeyboardButton(text=_t("btn_subs", lang), callback_data="act:subs"),
            InlineKeyboardButton(text=_t("btn_settings", lang), callback_data="act:settings"),
        ],
    ]
    if PUBLIC_BASE:
        rows.append([InlineKeyboardButton(text=_t("btn_app", lang), web_app=WebAppInfo(url=PUBLIC_BASE))])
    rows.append(_cancel_row(lang))
    return _kb(rows)


async def _safe_edit(c: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
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


@dp.message(Command("login"))
async def cmd_login(m: types.Message, command: CommandObject):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        return
    token = (command.args or "").strip()
    if not token:
        await m.answer(_t("token_ask", lang), parse_mode="HTML")
        return
    set_user_token(m.from_user.id, token)
    await m.answer(_t("token_saved", lang))


@dp.message(Command("search"))
async def cmd_search(m: types.Message, command: CommandObject):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        return
    query = (command.args or "").strip()
    if not query:
        await m.answer(_t("ask_search", lang), parse_mode="HTML")
        return

    await _render_search_page(m, query, page=1, lang=lang)


async def _render_search_page(m_or_c, query: str, page: int = 1, lang: str = "uk"):
    limit = 5
    results = await asyncio.to_thread(api.search_novels, query, 15)
    if not results:
        msg = _t("search_empty", lang, query=query)
        if isinstance(m_or_c, CallbackQuery):
            await _safe_edit(m_or_c, msg)
        else:
            await m_or_c.answer(msg, parse_mode="HTML")
        return

    total_pages = max(1, math.ceil(len(results) / limit))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * limit
    page_items = results[start_idx:start_idx + limit]

    rows = []
    for item in page_items:
        title = item.get("rus_name") or item.get("eng_name") or item.get("slug")
        slug = item.get("slug")
        if slug:
            rows.append([InlineKeyboardButton(text=f"📕 {title}", callback_data=f"sel_slug:{slug}")])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"spg:{page-1}:{query[:20]}"))
    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"spg:{page+1}:{query[:20]}"))
    rows.append(nav_row)
    rows.append(_cancel_row(lang))

    text = _t("search_results", lang, query=query, page=page, total_pages=total_pages)
    if isinstance(m_or_c, CallbackQuery):
        await _safe_edit(m_or_c, text, reply_markup=_kb(rows))
    else:
        await m_or_c.answer(text, reply_markup=_kb(rows), parse_mode="HTML")


@dp.callback_query(F.data.startswith("spg:"))
async def search_page_callback(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    parts = c.data.split(":", 2)
    page = int(parts[1])
    query = parts[2]
    await _render_search_page(c, query, page=page, lang=lang)
    await c.answer()


@dp.message(Command("subscriptions"))
@dp.message(Command("subs"))
async def cmd_subscriptions(m: types.Message):
    lang = _get_lang(m.from_user)
    if not _allowed(m.from_user.id):
        return
    subs = get_user_subscriptions(m.from_user.id)
    if not subs:
        await m.answer(_t("subs_empty", lang))
        return
    lines = []
    rows = []
    for s in subs:
        lines.append(f"• <b>{s['title']}</b> ({_t('ch_label', lang, n=s['last_chapter_number'])})")
        rows.append([
            InlineKeyboardButton(text=f"📖 {s['title'][:20]}...", callback_data=f"sel_slug:{s['slug']}"),
            InlineKeyboardButton(text="🔕", callback_data=f"unsub:{s['slug']}"),
        ])
    rows.append(_cancel_row(lang))
    text = _t("subs_list", lang, items="\n".join(lines))
    await m.answer(text, reply_markup=_kb(rows), parse_mode="HTML")


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


@dp.callback_query(F.data.startswith("sub:"))
async def toggle_subscribe(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    slug = c.data.split(":", 1)[1]
    st = USER_STATE.get(c.from_user.id, {})
    info = st.get("novel_info") or {}
    title = info.get("rus_name") or info.get("eng_name") or slug
    last_ch = st.get("total_chapters", 0)
    if add_subscription(c.from_user.id, slug, title, last_ch):
        await c.answer(_t("sub_added", lang, title=title), show_alert=True)
    else:
        await c.answer("⚠️ Помилка", show_alert=True)


@dp.callback_query(F.data.startswith("unsub:"))
async def toggle_unsubscribe(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    slug = c.data.split(":", 1)[1]
    remove_subscription(c.from_user.id, slug)
    await c.answer(_t("sub_removed", lang, title=slug), show_alert=True)
    await cmd_subscriptions(c.message)


@dp.callback_query(F.data.startswith("sel_slug:"))
async def select_searched_slug(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    slug = c.data.split(":", 1)[1]
    url = f"https://ranobelib.me/ru/book/{slug}"

    USER_STATE[uid] = {"step": "fmt", "url": url, "slug": slug, "lang": lang}
    save_user_state(uid, USER_STATE[uid])
    await _ensure_novel_loaded(uid, slug)
    st = USER_STATE[uid]
    cover = (st.get("novel_info") or {}).get("cover")
    if isinstance(cover, dict):
        cover = cover.get("default") or cover.get("thumbnail")
    try:
        if cover:
            await c.message.answer_photo(
                cover, caption=_fmt_text(lang, st), reply_markup=_fmt_kb(lang), parse_mode="HTML"
            )
        else:
            await c.message.answer(_fmt_text(lang, st), reply_markup=_fmt_kb(lang), parse_mode="HTML")
    except Exception:
        await _safe_edit(c, _fmt_text(lang, st), reply_markup=_fmt_kb(lang))


@dp.callback_query(F.data.startswith("nav_back:"))
async def navigate_back(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    target = c.data.split(":", 1)[1]
    st = USER_STATE.get(uid) or {}

    if target == "fmt":
        st["step"] = "fmt"
        save_user_state(uid, st)
        cover = (st.get("novel_info") or {}).get("cover")
        if isinstance(cover, dict):
            cover = cover.get("default") or cover.get("thumbnail")
        try:
            if cover:
                await c.message.answer_photo(
                    cover, caption=_fmt_text(lang, st), reply_markup=_fmt_kb(lang), parse_mode="HTML"
                )
            else:
                await c.message.answer(_fmt_text(lang, st), reply_markup=_fmt_kb(lang), parse_mode="HTML")
        except Exception:
            await _safe_edit(c, _fmt_text(lang, st), reply_markup=_fmt_kb(lang))
    elif target == "dev":
        st["step"] = "dev"
        save_user_state(uid, st)
        await _safe_edit(c, _t("ask_dev", lang, fmt=st.get("fmt", "epub").upper()), reply_markup=_dev_kb(lang))
    elif target == "img":
        st["step"] = "img"
        save_user_state(uid, st)
        await _safe_edit(c, _t("ask_img", lang, device=st.get("device", "generic")), reply_markup=_img_kb(lang))
    elif target == "team":
        st["step"] = "team"
        save_user_state(uid, st)
        branches = st.get("branches", {})
        await _safe_edit(c, _t("ask_team", lang, img=_img_label(st.get("images_mode", "images"), lang)), reply_markup=_team_kb(branches, lang))
    await c.answer()


@dp.callback_query(F.data.startswith("toggle_set:"))
async def toggle_setting(c: CallbackQuery):
    uid = c.from_user.id
    lang = _get_lang(c.from_user)
    setting_type = c.data.split(":", 1)[1]
    st = get_user_settings(uid)

    if setting_type == "fmt":
        fmt_list = ["epub", "fb2", "txt", "html"]
        curr = st.get("fmt", "epub")
        next_val = fmt_list[(fmt_list.index(curr) + 1) % len(fmt_list)] if curr in fmt_list else "epub"
        st["fmt"] = next_val
    elif setting_type == "dev":
        dev_list = ["x4_crosspoint", "kindle", "phone", "generic"]
        curr = st.get("device", "generic")
        next_val = dev_list[(dev_list.index(curr) + 1) % len(dev_list)] if curr in dev_list else "generic"
        st["device"] = next_val
    elif setting_type == "img":
        img_list = ["images", "grayscale", "no_images"]
        curr = st.get("images_mode", "images")
        next_val = img_list[(img_list.index(curr) + 1) % len(img_list)] if curr in img_list else "images"
        st["images_mode"] = next_val
    elif setting_type == "lang":
        lang_list = ["uk", "ru", "en"]
        next_val = lang_list[(lang_list.index(lang) + 1) % len(lang_list)] if lang in lang_list else "uk"
        st["lang"] = next_val
        lang = next_val

    save_user_settings(uid, st)
    token_str = _t("token_on", lang) if st.get("token") else _t("token_off", lang)
    await _safe_edit(
        c,
        _t("settings_info", lang, fmt=st.get('fmt', 'epub').upper(), dev=_dev_label(st.get('device', 'generic'), lang), img=_img_label(st.get('images_mode', 'images'), lang), lang=lang, token_status=token_str),
        reply_markup=_settings_kb(uid, lang),
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
        st["split_mode"] = "none"
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
        ok = await _ensure_novel_loaded(uid, slug)
        st = USER_STATE[uid]
        cover = (st.get("novel_info") or {}).get("cover")
        if isinstance(cover, dict):
            cover = cover.get("default") or cover.get("thumbnail")
        try:
            if cover:
                await m.answer_photo(
                    cover,
                    caption=_fmt_text(lang, st),
                    reply_markup=_fmt_kb(lang),
                    parse_mode="HTML",
                )
            else:
                await m.answer(_fmt_text(lang, st), reply_markup=_fmt_kb(lang), parse_mode="HTML")
        except Exception:
            await m.answer(_fmt_text(lang, st), reply_markup=_fmt_kb(lang), parse_mode="HTML")
    else:
        await _render_search_page(m, text, page=1, lang=lang)


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
        await c.message.answer(_t("ask_url", lang), parse_mode="HTML")
        await c.answer()
    elif action == "search":
        await c.message.answer(_t("ask_search", lang), parse_mode="HTML")
        await c.answer()
    elif action == "subs":
        await cmd_subscriptions(c.message)
        await c.answer()
    elif action == "settings":
        st = get_user_settings(c.from_user.id)
        token_str = _t("token_on", lang) if st.get("token") else _t("token_off", lang)
        await c.message.answer(
            _t("settings_info", lang, fmt=st.get('fmt', 'epub').upper(), dev=_dev_label(st.get('device', 'generic'), lang), img=_img_label(st.get('images_mode', 'images'), lang), lang=lang, token_status=token_str),
            reply_markup=_settings_kb(c.from_user.id, lang),
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
        reply_markup=_dev_kb(lang, back_step="fmt"),
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
        _t("ask_img", lang, device=_dev_label(USER_STATE[uid]['device'], lang)),
        reply_markup=_img_kb(lang, back_step="dev"),
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

    st = USER_STATE[uid]
    # Info already loaded at FORMAT step; only (re)load if missing.
    if not (st.get("novel_info") and st.get("branches") is not None):
        if not await _ensure_novel_loaded(uid, st["slug"]):
            USER_STATE.pop(uid, None)
            save_user_state(uid, None)
            await _safe_edit(c, _t("novel_load_err", lang, err=_t("err_load", lang)))
            await c.answer()
            return
        st = USER_STATE[uid]

    branches = st.get("branches", {})
    info = st.get("novel_info", {})
    slug = st.get("slug", "")

    if not branches:
        USER_STATE[uid]["branch_id"] = None
        USER_STATE[uid]["step"] = "range"
        total = USER_STATE[uid].get("total_chapters", 0)
        await _safe_edit(
            c,
            _t("ask_range", lang, team="—", rng_hint="", total=total),
            reply_markup=_range_kb(total, lang, back_step="img"),
        )
        await c.answer()
        return

    cover = (info.get("cover") or {}).get("default") or (info.get("cover") or {}).get("thumbnail")
    title = info.get("rus_name") or info.get("eng_name") or USER_STATE[uid]["slug"]
    rating = info.get("rating", {}).get("average") or "—"
    status_str = _status_label(info.get("status", {}).get("id"), lang)

    cap = _t(
        "team_caption", lang,
        title=escape(title), rating=rating, status=status_str,
        ask_team=_t("ask_team", lang, img=_img_label(USER_STATE[uid]['images_mode'], lang)),
    )

    team_kb = _team_kb(branches, lang, back_step="img")
    team_kb.inline_keyboard.insert(0, [InlineKeyboardButton(text=_t("btn_subscribe", lang), callback_data=f"sub:{slug}")])

    try:
        if cover:
            await c.message.answer_photo(cover, caption=cap, reply_markup=team_kb, parse_mode="HTML")
        else:
            await c.message.answer(cap, reply_markup=team_kb, parse_mode="HTML")
    except Exception:
        await c.message.answer(cap, reply_markup=team_kb, parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("team:"))
async def choose_team(c: CallbackQuery):
    lang = _get_lang(c.from_user)
    uid = c.from_user.id
    if uid not in USER_STATE or USER_STATE[uid].get("step") != "team":
        await c.answer(_t("invalid_url", lang), show_alert=True)
        return
    parts = c.data.split(":", 2)
    bid = parts[1]
    if bid == "ALL":
        USER_STATE[uid]["branch_id"] = None
        USER_STATE[uid]["team_name"] = None
    elif len(parts) == 3:
        # Multi-team branch: a specific team was chosen by index.
        idx = parts[2]
        branch_info = USER_STATE[uid]["branches"].get(bid, {})
        team_names = branch_info.get("team_names") or []
        try:
            tname = team_names[int(idx)]
        except (ValueError, IndexError):
            tname = branch_info.get("name")
        USER_STATE[uid]["branch_id"] = bid
        USER_STATE[uid]["team_name"] = tname
    else:
        USER_STATE[uid]["branch_id"] = bid
        USER_STATE[uid]["team_name"] = USER_STATE[uid]["branches"].get(bid, {}).get("name")
    USER_STATE[uid]["step"] = "range"
    save_user_state(uid, USER_STATE[uid])

    total = USER_STATE[uid].get("total_chapters", 0)
    br = USER_STATE[uid]["branches"].get(bid, {}) if bid != "ALL" else {}
    rng = br.get("range")
    rng_hint = _t("rng_hint_tmpl", lang, a=rng[0], b=rng[1]) if rng else ""
    name = _t("btn_all_teams", lang) if bid == "ALL" else br.get("name", bid)

    await _safe_edit(
        c,
        _t("ask_range", lang, team=name, rng_hint=rng_hint, total=total),
        reply_markup=_range_kb(total, lang, back_step="team"),
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
    split_mode = "none"
    if val == "SPLIT_VOL":
        split_mode = "volume"
        chapters = "ALL"
    elif val == "SPLIT_CHUNK":
        split_mode = "chunk"
        chapters = "ALL"
    else:
        chapters = _parse_range(val, USER_STATE[uid].get("total_chapters", 0))
        if chapters is None:
            await c.answer(_t("invalid_range", lang), show_alert=True)
            return

    USER_STATE[uid]["chapters"] = chapters
    USER_STATE[uid]["split_mode"] = split_mode
    USER_STATE[uid]["step"] = "run"
    await _safe_edit(c, _t("download_start", lang))
    await c.answer()

    loop = asyncio.get_event_loop()
    outcome = await loop.run_in_executor(None, _do_download, uid, c.message, loop, lang)
    await _deliver(c.message, outcome, lang)
    USER_STATE.pop(uid, None)
    save_user_state(uid, None)


async def _deliver(m: types.Message, outcome: str, lang: str = "uk"):
    if outcome.startswith("__FILES__:"):
        filenames = [f for f in outcome.split(":", 1)[1].split("|") if f]
        for fname in filenames:
            fpath = DOWNLOADS_DIR / fname
            if fpath.exists():
                await m.answer_document(FSInputFile(fpath), caption=f"✅ {fname}")
    elif outcome.startswith("__FILE__:"):
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
    split_mode = st.get("split_mode", "none")

    user_settings = get_user_settings(uid)
    token = user_settings.get("token", "")

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
        split_label = _t("split_volume", lang) if split_mode == "volume" else _t("split_chunk", lang)
        rng = _t("rng_all_split", lang, mode=split_label) if split_mode != "none" else _t("btn_range_all", lang)
    elif isinstance(chapters, list):
        nums = [c.get("number") for c in chapters if isinstance(c, dict)]
        rng = _t("rng_chapters", lang, a=nums[0], b=nums[-1]) if nums else _t("rng_selected", lang)
    else:
        rng = _t("rng_selected", lang)

    log.info("download start uid=%s slug=%s fmt=%s dev=%s img=%s split=%s branch=%s", uid, slug, fmt, dev, img_mode, split_mode, branch_id)

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
        "token": token,
        "format": fmt,
        "profile": dev,
        "cover": True,
        "images": images_enabled,
        "grayscale": grayscale_enabled,
        "compress": True,
        "branch_id": branch_id,
        "selected_team_keys": selected_team_keys,
        "chapters": [] if chapters == "ALL" else chapters,
        "split_mode": split_mode,
        "chunk_size": 150,
    }

    save_user_settings(uid, {"lang": lang, "fmt": fmt, "device": dev, "images_mode": img_mode, "token": token})

    with tasks_lock:
        tasks[task_id] = {
            "status": "processing", "progress": 0,
            "processed_chapters": 0, "total_chapters": 0,
            "created_at": time.time(),
        }
    threading.Thread(target=run_download_task, args=(task_id, body), daemon=True).start()
    _edit(f"⏳ <b>{team_name}</b> | {rng}\n{_t('preparing', lang)}")
    deadline = time.time() + 1800
    last_pct = -1

    while time.time() < deadline:
        with tasks_lock:
            t = tasks.get(task_id)
        if not t:
            break
        if t.get("status") == "error":
            log.error("download error uid=%s: %s", uid, t.get("error"))
            return _t("dl_error", lang, err=t.get("error", _t("err_unknown", lang)))
        if t.get("status") == "done":
            file_list = t.get("file_list") or []
            file_name = t.get("file")
            if file_list and len(file_list) > 1:
                log.info("download done uid=%s multi-files=%s", uid, len(file_list))
                return f"__FILES__:{'|'.join(file_list)}"
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


async def _subscription_checker_loop():
    """Background task checking subscriptions every 30 minutes for new chapters."""
    log.info("Subscription checker loop started.")
    while True:
        try:
            await asyncio.sleep(1800)
            subs = get_all_subscriptions()
            if not subs or not bot:
                continue
            log.info("Checking %d subscriptions...", len(subs))
            by_slug = {}
            for s in subs:
                by_slug.setdefault(s["slug"], []).append(s)

            for slug, sub_group in by_slug.items():
                try:
                    chapters = await asyncio.to_thread(api.get_novel_chapters, slug)
                    if not chapters:
                        continue
                    max_ch = max([float(c.get("number", 0)) for c in chapters if c.get("number") is not None] or [0])
                    for sub in sub_group:
                        last_ch = float(sub.get("last_chapter_number", 0))
                        if max_ch > last_ch:
                            update_subscription_ch(sub["user_id"], slug, max_ch)
                            sub_lang = get_user_settings(sub["user_id"]).get("lang", "uk")
                            msg = _t("sub_notify", sub_lang, title=sub["title"], max_ch=max_ch)
                            try:
                                await bot.send_message(sub["user_id"], msg, parse_mode="HTML")
                            except Exception as ex:
                                log.warning("Failed to notify user %s: %s", sub["user_id"], ex)
                except Exception as ex:
                    log.warning("Error checking subscription for %s: %s", slug, ex)
        except Exception as e:
            log.error("Error in subscription checker loop: %s", e)
            await asyncio.sleep(60)


async def main():
    init_db()
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    log.info("Starting Telegram bot polling...")
    # Register /commands menu (localized). Shown when user types "/".
    _COMMANDS = {
        "uk": [
            BotCommand(command="start", description="Головне меню"),
            BotCommand(command="search", description="Пошук новели за назвою"),
            BotCommand(command="subscriptions", description="Мої підписки"),
            BotCommand(command="login", description="Токен RanobeLIB (доступ до глав)"),
            BotCommand(command="cancel", description="Скасувати поточну дію"),
            BotCommand(command="help", description="Довідка та список команд"),
        ],
        "ru": [
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="search", description="Поиск новеллы по названию"),
            BotCommand(command="subscriptions", description="Мои подписки"),
            BotCommand(command="login", description="Токен RanobeLIB (доступ к главам)"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
            BotCommand(command="help", description="Справка и список команд"),
        ],
        "en": [
            BotCommand(command="start", description="Main menu"),
            BotCommand(command="search", description="Search novel by title"),
            BotCommand(command="subscriptions", description="My subscriptions"),
            BotCommand(command="login", description="RanobeLIB token (chapter access)"),
            BotCommand(command="cancel", description="Cancel current action"),
            BotCommand(command="help", description="Help and command list"),
        ],
    }
    for lang_code, cmds in _COMMANDS.items():
        try:
            await bot.set_my_commands(cmds, language_code=lang_code)
        except Exception as e:
            log.warning("set_my_commands failed for %s: %s", lang_code, e)
    # Default (fallback) commands for any other locale.
    try:
        await bot.set_my_commands(_COMMANDS["uk"])
    except Exception as e:
        log.warning("set_my_commands (default) failed: %s", e)
    asyncio.create_task(_subscription_checker_loop())
    # Single-instance guard: bind a localhost socket for the process lifetime.
    # A second launch will fail to bind and exit immediately instead of
    # spawning a duplicate that fights this instance (TelegramConflictError).
    _instance_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _instance_lock.bind(("127.0.0.1", 8765))
        _instance_lock.listen(1)
    except OSError:
        log.error("Another bot instance is already running (port 8765). Exiting.")
        sys.exit(1)

    # Clear any lingering getUpdates session left by a killed predecessor so
    # we don't fight it for the polling slot (drop_pending_updates=True tears
    # down the old session on Telegram's side before we start).
    async def _on_startup(**kwargs):
        b = kwargs.get("bot") or bot
        try:
            await b.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            log.warning("delete_webhook failed: %s", e)

    dp.startup.register(_on_startup)
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
