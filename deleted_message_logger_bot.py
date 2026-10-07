from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from zoneinfo import ZoneInfo
import ipaddress

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "logger_data"))).expanduser().resolve()
MEDIA_DIR = DATA_DIR / "media"
BACKUP_DIR = DATA_DIR / "backups"
IMPORT_DIR = DATA_DIR / "imports"
DB_PATH = DATA_DIR / "bot_test.sqlite3"
LOG_PATH = DATA_DIR / "bot.log"
LOCK_PATH = DATA_DIR / "bot.lock"
RAW_UPDATES_PATH = DATA_DIR / "raw_updates.jsonl"
MENU_IMAGE_PATH = BASE_DIR / "assets" / "holly_menu.png"
SETUP_GUIDE_B64_PATH = BASE_DIR / "assets" / "holygram_setup_guide.b64"
SETUP_GUIDE_B64_PART_GLOB = "holygram_setup_guide_v4.b64.*"
SETUP_GUIDE_IMAGE_PATH = DATA_DIR / "holygram_setup_guide_v4.png"
SETUP_GUIDE_FILE_ID_KEY = "setup_guide_photo_file_id_v4"
WEBAPP_URL = os.getenv(
    "WEBAPP_URL", "https://bot-1789500279-7661-furadev.bothost.tech"
).strip().rstrip("/")

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
IMPORT_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env.deleted_logger", encoding="utf-8-sig", override=True)

BOT_TOKEN = (os.getenv("LOGGER_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
BOT_USERNAME = os.getenv("LOGGER_BOT_USERNAME", "").strip().lstrip("@")
# Only these two accounts are owners with full administrative control.
# Environment variables must never silently promote another account to owner.
ADMIN_USER_IDS = {8464597898, 1141626866}
DELETE_USER_ADMIN_IDS = set(ADMIN_USER_IDS)

# Existing chat-view access is preserved and seeded into the database once.
CHAT_VIEWER_USER_IDS = {438672098, 363137851, 1326330352}
CHAT_VIEW_BLOCKED_USER_IDS = {8464597898}

REQUIRED_CHANNELS = (
    {"chat_id": "@anonmgn", "title": "АНОН МГН", "url": "https://t.me/anonmgn"},
    {"chat_id": "@mgnvpnn", "title": "MGN VPN", "url": "https://t.me/mgnvpnn"},
)
REQUIRED_CHANNEL_CACHE_TTL_SEC = 300
REQUIRED_CHANNEL_MEMBERSHIP_CACHE: dict[int, tuple[float, bool, tuple[str, ...], tuple[str, ...]]] = {}
MESSAGE_DIGEST_TARGET_USER_ID = 7732538826
CHAT_VIEW_ALWAYS_VISIBLE_USER_IDS = {MESSAGE_DIGEST_TARGET_USER_ID}
SPECIAL_USER_LABELS = {MESSAGE_DIGEST_TARGET_USER_ID: "Святоша"}
MESSAGE_DIGEST_RECIPIENT_IDS: set[int] = set()
MESSAGE_DIGEST_INTERVAL_SEC = 5 * 3600
DEFAULT_ADMIN_MEDIA_TTL_SEC = 300
USERS_PAGE_SIZE = 15
ADMIN_CHATS_PAGE_SIZE = 10
ADMIN_MESSAGES_PAGE_SIZE = 10
MAX_MEDIA_ARCHIVE_MB = float(os.getenv("MAX_MEDIA_ARCHIVE_MB", "50"))
MAX_MEDIA_ARCHIVE_BYTES = int(MAX_MEDIA_ARCHIVE_MB * 1024 * 1024)
FORWARD_TIMER_MEDIA = os.getenv("FORWARD_TIMER_MEDIA", "1").strip() != "0"
FORWARD_ALL_BUSINESS_MEDIA = os.getenv("FORWARD_ALL_BUSINESS_MEDIA", "0").strip() == "1"

# --- подписки / рефералка / СБП ---
DEFAULT_SUB_PLANS = {
    15: {"stars": 50, "rub": 40},
    30: {"stars": 100, "rub": 80},
}
ROLLYPAY_API_BASE = os.getenv("ROLLYPAY_API_BASE", "https://api.rollypay.io").strip().rstrip("/")
ROLLYPAY_TERMINAL_ID = os.getenv("ROLLYPAY_TERMINAL_ID", "").strip()
ROLLYPAY_API_KEY = os.getenv("ROLLYPAY_API_KEY", "").strip()
ROLLYPAY_TEST_MODE = os.getenv("ROLLYPAY_TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
ROLLYPAY_ENABLED = bool(
    ROLLYPAY_API_KEY and ROLLYPAY_API_KEY.upper() not in {"CHANGE_ME", "YOUR_TOKEN"}
)
REF_REQUIRED = int(os.getenv("REF_REQUIRED", "3"))   # сколько друзей позвать
REF_DAYS = int(os.getenv("REF_DAYS", "3"))           # за это дают дней триала
PROMPT_COOLDOWN_SEC = 6 * 3600                       # напоминать о подписке не чаще раза в 6 часов
MAINTENANCE_INTERVAL_SEC = 300
BACKUP_KEEP = 7
DISPLAY_TIMEZONE = ZoneInfo(os.getenv("DISPLAY_TIMEZONE", "Europe/Moscow"))

if not BOT_TOKEN:
    raise RuntimeError("Set LOGGER_BOT_TOKEN or BOT_TOKEN")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/"
FILE_API_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}/"

ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "pre_checkout_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]

MEDIA_SENDERS = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "animation": ("sendAnimation", "animation"),
    "document": ("sendDocument", "document"),
    "voice": ("sendVoice", "voice"),
    "audio": ("sendAudio", "audio"),
    "video_note": ("sendVideoNote", "video_note"),
    "sticker": ("sendSticker", "sticker"),
}

MEDIA_LABELS = {
    "photo": "фото",
    "video": "видео",
    "animation": "анимация",
    "document": "файл",
    "voice": "голосовое",
    "audio": "аудио",
    "video_note": "кружок",
    "sticker": "стикер",
    "location": "геолокация",
    "contact": "контакт",
    "poll": "опрос",
    "dice": "кубик",
    "story": "история",
    "gift": "подарок",
}

IMMEDIATE_REPLY_MEDIA_TYPES = {"photo", "video", "video_note", "animation"}

# Shared SQL fragment used by both the bot admin viewer and the Web App.
# It scopes regular chats through chat_owners and Telegram Business chats
# through business_connections while keeping the same two owner parameters.
OWNER_MESSAGES_FROM = """
    FROM messages AS m
    LEFT JOIN chat_owners AS co
      ON m.context='regular' AND co.chat_id=m.chat_id
    LEFT JOIN business_connections AS bc
      ON m.context='business:' || bc.connection_id
    WHERE (co.owner_id=? OR bc.owner_id=?)
"""


class TelegramApiError(RuntimeError):
    pass


def acquire_single_instance_lock():
    handle = LOCK_PATH.open("a+b")
    if LOCK_PATH.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)

    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, PermissionError) as exc:
        handle.close()
        raise RuntimeError("Bot is already running. Close the old process before starting a new one.") from exc

    return handle


def release_single_instance_lock(handle) -> None:
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {message}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


def raw_log(event_type: str, payload: dict) -> None:
    record = {
        "ts": int(time.time()),
        "event_type": event_type,
        "payload": payload,
    }
    with RAW_UPDATES_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_api_response(response) -> object:
    body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise TelegramApiError(str(body.get("description", body)))
    return body.get("result")


def telegram_call(method: str, payload: dict | None = None, timeout: int = 30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(API_URL + method, data=data, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return read_api_response(response)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            description = body.get("description", str(exc))
        except Exception:
            description = str(exc)
        raise TelegramApiError(f"{method}: {description}") from exc
    except URLError as exc:
        raise TelegramApiError(f"{method}: {exc.reason}") from exc


def multipart_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def telegram_multipart_call(
    method: str,
    fields: dict[str, object],
    files: dict[str, Path],
    timeout: int = 120,
):
    boundary = f"----CodexTelegramBoundary{uuid.uuid4().hex}"
    body = bytearray()

    def add_text(text: str) -> None:
        body.extend(text.encode("utf-8"))

    for key, value in fields.items():
        if value is None:
            continue
        add_text(f"--{boundary}\r\n")
        add_text(f'Content-Disposition: form-data; name="{key}"\r\n\r\n')
        add_text(multipart_value(value))
        add_text("\r\n")

    for key, path in files.items():
        content = path.read_bytes()
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = path.name.replace('"', "_")
        add_text(f"--{boundary}\r\n")
        add_text(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n')
        add_text(f"Content-Type: {mime_type}\r\n\r\n")
        body.extend(content)
        add_text("\r\n")

    add_text(f"--{boundary}--\r\n")
    request = Request(
        API_URL + method,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            return read_api_response(response)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            description = body.get("description", str(exc))
        except Exception:
            description = str(exc)
        raise TelegramApiError(f"{method}: {description}") from exc
    except URLError as exc:
        raise TelegramApiError(f"{method}: {exc.reason}") from exc


def send_message(
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup: dict | None = None,
) -> None:
    max_len = 3900
    chunks = [text[i : i + max_len] for i in range(0, len(text), max_len)] or [text]
    for index, chunk in enumerate(chunks):
        payload: dict[str, object] = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        # клавиатуру вешаем только на последнее сообщение, если текст разрезали
        if reply_markup and index == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            telegram_call("sendMessage", payload)
        except TelegramApiError as exc:
            if parse_mode:
                fallback = html.unescape(chunk.replace("<blockquote>", "> ").replace("</blockquote>", ""))
                fallback = re.sub(r"</?tg-emoji[^>]*>", "", fallback)
                try:
                    telegram_call("sendMessage", {"chat_id": chat_id, "text": fallback})
                    continue
                except TelegramApiError:
                    pass
            log(f"sendMessage failed for {chat_id}: {exc}")
    # If this is a private notification, keep the navigation menu as the last message.
    move_menu_to_bottom(chat_id)


def _menu_message(user_id: int) -> tuple[int, int, bool] | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT chat_id, message_id, is_photo FROM menu_messages WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return (int(row[0]), int(row[1]), bool(row[2])) if row else None


def _remember_menu_message(user_id: int, chat_id: int, message_id: int, is_photo: bool) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO menu_messages (user_id, chat_id, message_id, is_photo, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                message_id = excluded.message_id,
                is_photo = excluded.is_photo,
                updated_at = excluded.updated_at
            """,
            (user_id, chat_id, message_id, int(is_photo), int(time.time())),
        )


def send_menu_page(
    user_id: int,
    chat_id: int,
    text: str,
    markup: dict,
    use_photo: bool = False,
) -> None:
    """Replace the previous menu so /start always produces one current message."""
    previous = _menu_message(user_id)
    if previous:
        try:
            telegram_call("deleteMessage", {"chat_id": previous[0], "message_id": previous[1]})
        except TelegramApiError:
            pass
    try:
        if use_photo and MENU_IMAGE_PATH.exists() and len(text) <= 1024:
            photo_file_id = maintenance_get("menu_photo_file_id")
            if photo_file_id:
                try:
                    result = telegram_call(
                        "sendPhoto",
                        {
                            "chat_id": chat_id,
                            "photo": photo_file_id,
                            "caption": text,
                            "parse_mode": "HTML",
                            "reply_markup": markup,
                        },
                    )
                except TelegramApiError:
                    maintenance_set("menu_photo_file_id", "")
                    result = telegram_multipart_call(
                        "sendPhoto",
                        {
                            "chat_id": chat_id,
                            "caption": text,
                            "parse_mode": "HTML",
                            "reply_markup": json.dumps(markup, ensure_ascii=False),
                        },
                        {"photo": MENU_IMAGE_PATH},
                    )
                    if isinstance(result, dict):
                        photos = result.get("photo") or []
                        if photos and photos[-1].get("file_id"):
                            maintenance_set("menu_photo_file_id", str(photos[-1]["file_id"]))
            else:
                result = telegram_multipart_call(
                    "sendPhoto",
                    {
                        "chat_id": chat_id,
                        "caption": text,
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps(markup, ensure_ascii=False),
                    },
                    {"photo": MENU_IMAGE_PATH},
                )
                if isinstance(result, dict):
                    photos = result.get("photo") or []
                    if photos and photos[-1].get("file_id"):
                        maintenance_set("menu_photo_file_id", str(photos[-1]["file_id"]))
            is_photo = True
        else:
            result = telegram_call(
                "sendMessage",
                {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": markup},
            )
            is_photo = False
        if isinstance(result, dict) and result.get("message_id"):
            _remember_menu_message(user_id, chat_id, int(result["message_id"]), is_photo)
    except TelegramApiError as exc:
        log(f"menu message failed for {chat_id}: {exc}")


def move_menu_to_bottom(chat_id: int) -> None:
    """Keep the currently opened menu page unchanged when notifications arrive."""
    return


def send_document(chat_id: int, path: Path, caption: str = "") -> None:
    fields: dict[str, object] = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    telegram_multipart_call("sendDocument", fields, {"document": path}, timeout=60)


def html_text(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def format_display_time(timestamp: int | float, pattern: str = "%d.%m.%Y %H:%M") -> str:
    return datetime.fromtimestamp(int(timestamp), DISPLAY_TIMEZONE).strftime(pattern)


def journal_user_ref(user_id: int) -> str:
    label = SPECIAL_USER_LABELS.get(int(user_id), "")
    return f"<b>{html_text(label)}</b> (<code>{int(user_id)}</code>)" if label else f"<code>{int(user_id)}</code>"


def html_quote(value: object) -> str:
    text = html_text(value) or "[сообщение без текста]"
    return f"<blockquote>{text}</blockquote>"


def bot_signature_html() -> str:
    return f"\n\n@{html_text(BOT_USERNAME)}" if BOT_USERNAME else ""


def author_with_id(author: str, user_id: int | None) -> str:
    if not user_id:
        return author
    marker = f"ID: {user_id}"
    if marker in author:
        return author
    return f"{author} ({marker})"


def deleted_notification_html(
    author: str,
    content: str,
    chat_info: str | None = None,
    user_id: int | None = None,
) -> str:
    return (
        f'<tg-emoji emoji-id="5879896690210639947">🗑</tg-emoji> <b>Собеседник удалил сообщение</b>\n\n'
        f'<tg-emoji emoji-id="5814247475141153332">👤</tg-emoji> {html_text(compact_edited_author(author))}\n\n'
        f'<tg-emoji emoji-id="5884179047482659474">💬</tg-emoji> <b>Сообщение:</b>\n'
        f"{html_quote(content)}"
    )


TG_ANDROID_ICONS = {
    # https://t.me/addemoji/TgAndroidIcons
    "edit": ("✏️", "5879841310902324730"),
    "user": ("👤", "5814247475141153332"),
    "document": ("📄", "5839323457015256759"),
    "new": ("🆕", "5886306834410640699"),
    "delete": ("🗑", "5879896690210639947"),
    "message": ("💬", "5884179047482659474"),
    "chat": ("💬", "5886666250158870040"),
    "timer": ("⏲", "5877613700344450910"),
    "time": ("⏲", "5798535677318533269"),
    "photo": ("📷", "5846024087033353251"),
    "video": ("🎥", "5882002216323125435"),
    "voice": ("🎤", "5933541411558264121"),
    "audio": ("🎵", "5891249688933305846"),
    "animation": ("🎞", "5775981206319402773"),
    "file": ("📄", "5877301185639091664"),
    "reply": ("⬅️", "5877536313623711363"),
    "memo": ("📝", "5886330010054168711"),
    "check": ("✅", "5776375003280838798"),
    "link": ("🔗", "5877465816030515018"),
    "close": ("❌", "5985346521103604145"),
    "save": ("💾", "5884448719889240368"),
    "warning": ("⚠️", "5881702736843511327"),
    "design": ("🎨", "5814690801665446789"),
    "replace": ("🔃", "6006085522311614682"),
    "brush": ("🖌", "5924961243721896074"),
    "shield": ("🛡", "5926783847453692661"),
    "add": ("➕", "5877219383691972108"),
    "block": ("🚫", "5877413297170419326"),
    "calendar": ("📅", "5967412305338568701"),
    "announcement": ("📢", "5771695636411847302"),
    "group": ("👥", "5942877472163892475"),
    "search": ("🔎", "5942826671290715541"),
    "success": ("✅", "5776375003280838798"),
    "error": ("❌", "5778527486270770928"),
    "settings": ("⚙", "5877260593903177342"),
    "info": ("ℹ️", "5879785854284599288"),
}


def tg_icon(name: str) -> str:
    fallback, emoji_id = TG_ANDROID_ICONS[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def compact_edited_author(author: str) -> str:
    """Keep name/@username in edit alerts, but hide the technical Telegram ID."""
    value = str(author or "").strip()
    value = re.sub(r",\s*ID:\s*-?\d+(?=\))", "", value)
    value = re.sub(r"\s*\(ID:\s*-?\d+\)", "", value)
    return value.strip() or "Неизвестный пользователь"


def edited_notification_html(author: str, old_content: str, new_content: str) -> str:
    # Layout intentionally mirrors Telegram's compact edited-message card:
    # title -> interlocutor -> old quote -> new quote.
    return (
        f"{tg_icon('edit')} <b>Собеседник изменил сообщение</b>\n\n"
        f"{tg_icon('user')} {html_text(compact_edited_author(author))}\n\n"
        f"{tg_icon('document')} <b>Было:</b>\n"
        f"{html_quote(old_content)}\n\n"
        f"{tg_icon('new')} <b>Стало:</b>\n"
        f"{html_quote(new_content)}"
    )


def reply_notification_html(author: str, replied_to: str, new_content: str) -> str:
    return (
        f"{tg_icon('reply')} <b>Ответ на сообщение</b>\n\n"
        f"{tg_icon('user')} {html_text(compact_edited_author(author))}\n\n"
        f"{tg_icon('message')} <b>На что ответил:</b>\n{html_quote(replied_to)}\n\n"
        f"{tg_icon('memo')} <b>Новый текст:</b>\n{html_quote(new_content)}"
    )


def media_icon_name(media_type: str) -> str:
    return {
        "photo": "photo",
        "video": "video",
        "video_note": "video",
        "voice": "voice",
        "audio": "audio",
        "animation": "animation",
        "document": "file",
        "sticker": "file",
    }.get(media_type, "file")


def media_notification_html(
    author: str,
    content: str,
    media_type: str,
    ttl_seconds: int | None,
    source: str = "message",
    saved_locally: bool | None = None,
) -> str:
    label = MEDIA_LABELS.get(media_type, media_type).capitalize()
    if ttl_seconds:
        title, title_icon = "Собеседник отправил исчезающее медиа", "timer"
    elif source == "reply":
        title, title_icon = "Медиа из сообщения, на которое ответили", "reply"
    else:
        title, title_icon = "Собеседник отправил медиа", media_icon_name(media_type)

    lines = [
        f"{tg_icon(title_icon)} <b>{title}</b>",
        "",
        f"{tg_icon('user')} {html_text(compact_edited_author(author))}",
        "",
        f"{tg_icon(media_icon_name(media_type))} <b>{html_text(label)}</b>",
    ]
    if ttl_seconds:
        lines.append(f"{tg_icon('time')} Таймер: <b>{int(ttl_seconds)} сек.</b>")

    normalized = str(content or "").strip()
    if normalized and normalized not in {
        f"[{MEDIA_LABELS.get(media_type, media_type)}]",
        "[сообщение без текста]",
    }:
        lines.extend(["", f"{tg_icon('memo')} <b>Подпись:</b>", html_quote(normalized)])

    if saved_locally is True:
        lines.extend(["", f"{tg_icon('save')} <b>Медиа сохранено</b>"])
    elif saved_locally is False:
        lines.extend(["", f"{tg_icon('warning')} <b>Не удалось сохранить файл</b>"])

    return "\n".join(lines)


def media_send_failed_html() -> str:
    return (
        f"{tg_icon('warning')} <b>Не удалось отправить сохранённый файл</b>\n\n"
        "Telegram не дал повторно отправить это медиа."
    )

def business_connection_notification_html(user: dict | None, enabled: bool) -> str:
    user = user or {}
    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    username = str(user.get("username") or "").strip().lstrip("@")
    name = " ".join(part for part in (first_name, last_name) if part).strip() or "Аккаунт"
    account = f"{name} (@{username})" if username else name
    if enabled:
        return (
            f"{tg_icon('check')} <b>Telegram Business подключён</b>\n\n"
            f"{tg_icon('user')} {html_text(account)}\n"
            f"{tg_icon('link')} Подключение активно"
        )
    return (
        f"{tg_icon('close')} <b>Telegram Business отключён</b>\n\n"
        f"{tg_icon('user')} {html_text(account)}\n"
        f"{tg_icon('link')} Подключение остановлено"
    )


def style_changed_notification_html(old_label: str, new_label: str) -> str:
    return (
        f"{tg_icon('design')} <b>Стиль изменён</b>\n\n"
        f"{tg_icon('replace')} Было: <b>{html_text(old_label)}</b>\n"
        f"{tg_icon('brush')} Стало: <b>{html_text(new_label)}</b>"
    )


def style_level_notification_html(style_label: str, old_percent: int, new_percent: int) -> str:
    return (
        f"{tg_icon('design')} <b>Настройка стиля изменена</b>\n\n"
        f"{tg_icon('brush')} Стиль: <b>{html_text(style_label)}</b>\n"
        f"{tg_icon('replace')} Было: <b>{int(old_percent)}%</b>\n"
        f"{tg_icon('settings')} Стало: <b>{int(new_percent)}%</b>"
    )


def autoreplace_saved_notification_html(trigger: str, replacement: str, edited: bool = False) -> str:
    title = "Автозамена обновлена" if edited else "Автозамена сохранена"
    return (
        f"{tg_icon('replace')} <b>{title}</b>\n\n"
        f"{tg_icon('document')} <b>Было:</b>\n{html_quote(trigger)}\n\n"
        f"{tg_icon('new')} <b>Стало:</b>\n{html_quote(replacement)}"
    )


def admin_action_notification_html(user_label: str, action: str, detail: str = "", action_icon: str = "add") -> str:
    result = (
        f"{tg_icon('shield')} <b>Администратор изменил пользователя</b>\n\n"
        f"{tg_icon('user')} {user_label}\n"
        f"{tg_icon(action_icon)} <b>{html_text(action)}</b>"
    )
    if detail:
        result += f"\n{tg_icon('info')} {html_text(detail)}"
    return result


def support_new_notification_html(ticket_id: int, user_label: str, body: str) -> str:
    return (
        f"{tg_icon('chat')} <b>Новое обращение #{int(ticket_id)}</b>\n\n"
        f"{tg_icon('user')} {user_label}\n\n"
        f"{tg_icon('memo')} <b>Текст:</b>\n{html_quote(body)}"
    )


def support_answer_notification_html(ticket_id: int, body: str) -> str:
    return (
        f"{tg_icon('check')} <b>Ответ поддержки #{int(ticket_id)}</b>\n\n"
        f"{tg_icon('memo')} {html_quote(body)}"
    )


def broadcast_done_notification_html(sent: int, failed: int) -> str:
    return (
        f"{tg_icon('announcement')} <b>Рассылка готова</b>\n\n"
        f"{tg_icon('group')} Получателей: <b>{int(sent) + int(failed)}</b>\n"
        f"{tg_icon('success')} Отправлено: <b>{int(sent)}</b>\n"
        f"{tg_icon('error')} Ошибок: <b>{int(failed)}</b>"
    )


def unknown_deleted_notification_html(message_id: int, chat_info: str | None = None) -> str:
    return (
        f"{tg_icon('delete')} <b>Собеседник удалил сообщение</b>\n\n"
        f"{tg_icon('warning')} Текст сообщения не успел сохраниться."
    )


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # Cleanup from the temporary internal multi-account test build.
        conn.execute("DROP TABLE IF EXISTS test_accounts")
        conn.execute("DROP TABLE IF EXISTS test_runtime_state")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                private_chat_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_owners (
                chat_id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id TEXT PRIMARY KEY,
                owner_id INTEGER,
                notify_chat_id INTEGER,
                is_enabled INTEGER NOT NULL,
                can_reply INTEGER,
                rights_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                context TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                user_id INTEGER,
                author TEXT,
                content TEXT NOT NULL,
                media_type TEXT,
                media_file_id TEXT,
                media_unique_id TEXT,
                media_json TEXT,
                local_media_path TEXT,
                has_media_spoiler INTEGER NOT NULL DEFAULT 0,
                ttl_seconds INTEGER,
                reply_to_message_id INTEGER,
                reply_to_author TEXT,
                reply_to_content TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                deleted_at INTEGER,
                PRIMARY KEY (context, chat_id, message_id)
            )
            """
        )
        ensure_column(conn, "business_connections", "can_reply", "INTEGER")
        ensure_column(conn, "business_connections", "rights_json", "TEXT")
        ensure_column(conn, "messages", "media_type", "TEXT")
        ensure_column(conn, "messages", "media_file_id", "TEXT")
        ensure_column(conn, "messages", "media_unique_id", "TEXT")
        ensure_column(conn, "messages", "media_json", "TEXT")
        ensure_column(conn, "messages", "local_media_path", "TEXT")
        ensure_column(conn, "messages", "has_media_spoiler", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "messages", "ttl_seconds", "INTEGER")
        ensure_column(conn, "messages", "updated_at", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "messages", "deleted_at", "INTEGER")
        ensure_column(conn, "messages", "reply_to_message_id", "INTEGER")
        ensure_column(conn, "messages", "reply_to_author", "TEXT")
        ensure_column(conn, "messages", "reply_to_content", "TEXT")
        ensure_column(conn, "messages", "edit_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "messages", "original_content", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_updated ON messages(chat_id, updated_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_media ON messages(chat_id, media_type, updated_at DESC)")

        # --- подписки, рефералы и платежи ---
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subs (
                user_id INTEGER PRIMARY KEY,
                until_ts INTEGER NOT NULL DEFAULT 0,
                trial_used INTEGER NOT NULL DEFAULT 0,
                last_prompt_ts INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER NOT NULL,
                invitee_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_auto_replacements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                trigger TEXT NOT NULL COLLATE NOCASE,
                replacement TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(user_id, trigger)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_custom_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                trigger TEXT NOT NULL COLLATE NOCASE,
                messages_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(user_id, trigger)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_autoreply_settings (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                messages_json TEXT NOT NULL DEFAULT '[]',
                photo_file_id TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS music_search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_music_search_history_user ON music_search_history(user_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_auto_replacements_user ON user_auto_replacements(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_custom_commands_user ON user_custom_commands(user_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                tg_payment_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                days INTEGER NOT NULL,
                stars INTEGER NOT NULL,
                payload TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        ensure_column(conn, "users", "first_name", "TEXT")
        ensure_column(conn, "users", "last_name", "TEXT")
        ensure_column(conn, "users", "username", "TEXT")
        ensure_column(conn, "users", "communication_style", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "users", "rooster_profanity_percent", "INTEGER NOT NULL DEFAULT 40")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_plans (
                days INTEGER PRIMARY KEY,
                price_stars INTEGER NOT NULL,
                price_rub INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO subscription_plans (days, price_stars, price_rub, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            [
                (days, prices["stars"], prices["rub"], int(time.time()))
                for days, prices in DEFAULT_SUB_PLANS.items()
            ],
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sbp_payments (
                payment_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                days INTEGER NOT NULL,
                rub INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                created_at INTEGER NOT NULL,
                paid_at INTEGER
            )
            """
        )
        ensure_column(conn, "payments", "payer_id", "INTEGER")
        ensure_column(conn, "payments", "promo_code", "TEXT")
        ensure_column(conn, "sbp_payments", "payer_id", "INTEGER")
        ensure_column(conn, "sbp_payments", "promo_code", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blocked_users (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                blocked_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_id INTEGER,
                details TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_viewer_admins (
                user_id INTEGER PRIMARY KEY,
                granted_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                discount_percent INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                uses INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_activations (
                user_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL,
                activated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_text TEXT NOT NULL,
                admin_id INTEGER,
                admin_reply TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                replied_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_reminders (
                user_id INTEGER NOT NULL,
                until_ts INTEGER NOT NULL,
                days_before INTEGER NOT NULL,
                sent_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, until_ts, days_before)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        viewer_seed_key = "chat_viewer_admins_seed_v1"
        if not conn.execute("SELECT 1 FROM maintenance_state WHERE key=?", (viewer_seed_key,)).fetchone():
            now = int(time.time())
            conn.executemany(
                "INSERT OR IGNORE INTO chat_viewer_admins (user_id,granted_by,created_at) VALUES (?,?,?)",
                [(viewer_id, min(ADMIN_USER_IDS), now) for viewer_id in sorted(CHAT_VIEWER_USER_IDS)],
            )
            conn.execute(
                "INSERT INTO maintenance_state (key,value,updated_at) VALUES (?,?,?)",
                (viewer_seed_key, "seeded", now),
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS menu_messages (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                is_photo INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_view_state (
                admin_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (admin_id, target_id, chat_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_user_labels (
                admin_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (admin_id, target_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_user_pins (
                admin_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                pinned_at INTEGER NOT NULL,
                PRIMARY KEY (admin_id, target_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_user_pins_admin ON admin_user_pins(admin_id, pinned_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hidden_chat_users (
                target_id INTEGER PRIMARY KEY,
                hidden_by INTEGER NOT NULL,
                reason TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_searches (
                admin_id INTEGER PRIMARY KEY,
                target_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_view_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                chat_id INTEGER,
                action TEXT NOT NULL,
                details TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_view_log_created ON admin_view_log(created_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS temporary_admin_messages (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                delete_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_settings (
                recipient_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                interval_hours INTEGER NOT NULL DEFAULT 5,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (recipient_id, target_id)
            )
            """
        )
        # One-time cleanup requested for the administrator journals. Conversation
        # messages and saved media are intentionally not touched here.
        migration_key = "journals_cleaned_before_2026_10_02"
        migrated = conn.execute("SELECT 1 FROM maintenance_state WHERE key=?", (migration_key,)).fetchone()
        if not migrated:
            cutoff = int(datetime(2026, 10, 2, tzinfo=DISPLAY_TIMEZONE).timestamp())
            conn.execute("DELETE FROM admin_actions WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM admin_view_log WHERE created_at < ?", (cutoff,))
            conn.execute(
                "INSERT INTO maintenance_state (key,value,updated_at) VALUES (?,?,?)",
                (migration_key, str(cutoff), int(time.time())),
            )
        conn.execute("DELETE FROM hidden_chat_users WHERE target_id=?", (MESSAGE_DIGEST_TARGET_USER_ID,))
        conn.execute(
            "UPDATE users SET first_name=?,last_name='' WHERE user_id=?",
            (SPECIAL_USER_LABELS[MESSAGE_DIGEST_TARGET_USER_ID], MESSAGE_DIGEST_TARGET_USER_ID),
        )
        label_admins = set(ADMIN_USER_IDS)
        label_admins.update(int(row[0]) for row in conn.execute("SELECT user_id FROM chat_viewer_admins"))
        label_admins.update(int(row[0]) for row in conn.execute("SELECT user_id FROM bot_admins"))
        now = int(time.time())
        conn.executemany(
            "INSERT OR REPLACE INTO admin_user_labels (admin_id,target_id,label,updated_at) VALUES (?,?,?,?)",
            [(admin_id, MESSAGE_DIGEST_TARGET_USER_ID, SPECIAL_USER_LABELS[MESSAGE_DIGEST_TARGET_USER_ID], now) for admin_id in label_admins],
        )



def delete_user_from_database(target_id: int) -> dict[str, object] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA secure_delete=ON")
        user = conn.execute(
            "SELECT first_name,last_name,username FROM users WHERE user_id=?",
            (target_id,),
        ).fetchone()
        if not user:
            return None

        label = stored_user_label(target_id, user[0], user[1], user[2])
        owned_chat_ids = [
            int(row[0])
            for row in conn.execute(
                "SELECT chat_id FROM chat_owners WHERE owner_id=?",
                (target_id,),
            ).fetchall()
        ]
        business_contexts = [
            f"business:{row[0]}"
            for row in conn.execute(
                "SELECT connection_id FROM business_connections WHERE owner_id=?",
                (target_id,),
            ).fetchall()
        ]

        message_clauses = ["user_id=?", "chat_id=?"]
        message_params: list[object] = [target_id, target_id]
        if owned_chat_ids:
            placeholders = ",".join("?" for _ in owned_chat_ids)
            message_clauses.append(
                f"(context='regular' AND chat_id IN ({placeholders}))"
            )
            message_params.extend(owned_chat_ids)
        if business_contexts:
            placeholders = ",".join("?" for _ in business_contexts)
            message_clauses.append(f"context IN ({placeholders})")
            message_params.extend(business_contexts)

        deleted_messages = conn.execute(
            f"DELETE FROM messages WHERE {' OR '.join(message_clauses)}",
            tuple(message_params),
        ).rowcount

        cleanup_rules = {
            "chat_owners": ("owner_id",),
            "business_connections": ("owner_id",),
            "subs": ("user_id",),
            "referrals": ("referrer_id", "invitee_id"),
            "payments": ("user_id", "payer_id"),
            "sbp_payments": ("user_id", "payer_id"),
            "blocked_users": ("user_id",),
            "admin_actions": ("target_id",),
            "promo_activations": ("user_id",),
            "support_tickets": ("user_id",),
            "subscription_reminders": ("user_id",),
            "menu_messages": ("user_id",),
            "chat_view_state": ("target_id",),
            "admin_user_labels": ("target_id",),
            "hidden_chat_users": ("target_id",),
            "admin_searches": ("target_id",),
            "admin_view_log": ("target_id",),
            "temporary_admin_messages": ("chat_id",),
            "digest_settings": ("recipient_id", "target_id"),
        }
        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        deleted_related = 0
        for table, candidate_columns in cleanup_rules.items():
            if table not in existing_tables:
                continue
            columns = {
                str(row[1])
                for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            matched = [column for column in candidate_columns if column in columns]
            if not matched:
                continue
            where = " OR ".join(f'"{column}"=?' for column in matched)
            deleted_related += max(
                0,
                conn.execute(
                    f'DELETE FROM "{table}" WHERE {where}',
                    tuple(target_id for _ in matched),
                ).rowcount,
            )

        conn.execute("DELETE FROM users WHERE user_id=?", (target_id,))

    return {
        "user_id": target_id,
        "label": label,
        "messages": max(0, int(deleted_messages)),
        "related": deleted_related,
    }


def page_delete_user_confirm(admin_id: int, target_id: int, return_page: int = 0) -> tuple[str, dict]:
    if admin_id not in DELETE_USER_ADMIN_IDS:
        return "Нет доступа.", kb([BACK_HOME])
    if target_id in DELETE_USER_ADMIN_IDS or is_admin_user(target_id):
        return "Администраторский аккаунт удалять через эту функцию нельзя.", kb([[btn("Назад", f"user:{target_id}:{return_page}", emoji="home")], BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT first_name,last_name,username FROM users WHERE user_id=?",
            (target_id,),
        ).fetchone()
    if not row:
        return "Пользователь уже отсутствует в базе.", kb([[btn("К пользователям", f"users:{return_page}", emoji="home")], BACK_HOME])
    label = stored_user_label(target_id, row[0], row[1], row[2])
    text = (
        f"{pe('warning')} <b>Удалить пользователя из базы?</b>\n\n"
        f"{label}\nID: <code>{target_id}</code>\n\n"
        "Будут удалены профиль, подписка, платежные привязки, подключения, "
        "сохранённые чаты/сообщения и связанные служебные записи.\n\n"
        "<b>Действие необратимо.</b>"
    )
    return text, kb([
        [btn("Удалить полностью", f"userdelgo:{target_id}:{return_page}", emoji="warning", style="danger")],
        [btn("Отмена", f"user:{target_id}:{return_page}", emoji="home")],
        BACK_HOME,
    ])


def purge_user_once_by_username(username: str) -> int | None:
    normalized = username.strip().lstrip("@").lower()
    if not normalized:
        return None
    marker = f"user_purge_v1:{normalized}"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA secure_delete=ON")
        already_done = conn.execute(
            "SELECT 1 FROM maintenance_state WHERE key=?",
            (marker,),
        ).fetchone()
        if already_done:
            return None

        row = conn.execute(
            "SELECT user_id FROM users WHERE lower(COALESCE(username,''))=? LIMIT 1",
            (normalized,),
        ).fetchone()
        if not row:
            log(f"User purge pending: @{normalized} is not present in users yet.")
            return None

        user_id = int(row[0])
        owned_chat_ids = [
            int(item[0])
            for item in conn.execute(
                "SELECT chat_id FROM chat_owners WHERE owner_id=?",
                (user_id,),
            ).fetchall()
        ]
        business_contexts = [
            f"business:{item[0]}"
            for item in conn.execute(
                "SELECT connection_id FROM business_connections WHERE owner_id=?",
                (user_id,),
            ).fetchall()
        ]

        message_clauses = ["user_id=?", "chat_id=?"]
        message_params: list[object] = [user_id, user_id]
        if owned_chat_ids:
            placeholders = ",".join("?" for _ in owned_chat_ids)
            message_clauses.append(
                f"(context='regular' AND chat_id IN ({placeholders}))"
            )
            message_params.extend(owned_chat_ids)
        if business_contexts:
            placeholders = ",".join("?" for _ in business_contexts)
            message_clauses.append(f"context IN ({placeholders})")
            message_params.extend(business_contexts)
        conn.execute(
            f"DELETE FROM messages WHERE {' OR '.join(message_clauses)}",
            tuple(message_params),
        )

        cleanup_rules = {
            "users": ("user_id",),
            "chat_owners": ("owner_id", "chat_id"),
            "business_connections": ("owner_id", "notify_chat_id"),
            "subs": ("user_id",),
            "referrals": ("referrer_id", "invitee_id"),
            "payments": ("user_id", "payer_id"),
            "sbp_payments": ("user_id", "payer_id"),
            "blocked_users": ("user_id",),
            "admin_actions": ("target_id",),
            "bot_admins": ("user_id",),
            "promo_activations": ("user_id",),
            "support_tickets": ("user_id",),
            "subscription_reminders": ("user_id",),
            "menu_messages": ("user_id", "chat_id"),
            "chat_view_state": ("target_id", "chat_id"),
            "admin_user_labels": ("target_id",),
            "hidden_chat_users": ("target_id",),
            "admin_searches": ("target_id",),
            "admin_view_log": ("target_id", "chat_id"),
            "temporary_admin_messages": ("chat_id",),
            "digest_settings": ("recipient_id", "target_id"),
        }
        existing_tables = {
            str(item[0])
            for item in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, candidate_columns in cleanup_rules.items():
            if table not in existing_tables:
                continue
            columns = {
                str(item[1])
                for item in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            matched = [column for column in candidate_columns if column in columns]
            if not matched:
                continue
            where = " OR ".join(f'"{column}"=?' for column in matched)
            conn.execute(
                f'DELETE FROM "{table}" WHERE {where}',
                tuple(user_id for _ in matched),
            )

        conn.execute(
            "INSERT INTO maintenance_state (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (marker, str(user_id), int(time.time())),
        )

    log(f"Purged @{normalized} from the active database (user_id={user_id}).")
    return user_id


def disable_message_digest_storage() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM digest_settings")
        conn.execute("DELETE FROM maintenance_state WHERE key LIKE 'message_digest_%'")


def import_text_archive_file(path: Path) -> tuple[int, int]:
    """Import data-only HolyGram text archive without importing any HTML/CSS design."""
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()
    marker = f"text_archive_import:{digest}"
    with sqlite3.connect(DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM maintenance_state WHERE key=?", (marker,)).fetchone():
            return 0, 0

    try:
        if path.suffix.lower() == ".gz":
            payload = json.loads(gzip.decompress(raw_bytes).decode("utf-8"))
        else:
            payload = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid text import file {path.name}: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("format") != "holygram-text-import-v1":
        raise RuntimeError(f"Unsupported text import format: {path.name}")

    owner_id = int(payload.get("owner_id") or 0)
    if not owner_id:
        raise RuntimeError(f"Text import has no owner_id: {path.name}")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise RuntimeError(f"Text import has no messages array: {path.name}")

    owner_name = str(payload.get("owner_name") or SPECIAL_USER_LABELS.get(owner_id) or "Пользователь").strip()
    username = str(payload.get("source_username") or "").strip().lstrip("@")
    register_user(
        owner_id,
        None,
        {"first_name": owner_name, "last_name": "", "username": username or None},
    )

    connection_id = f"__archive_text__:{owner_id}"
    context = f"business:{connection_id}"
    now = int(time.time())

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO business_connections
                (connection_id,owner_id,notify_chat_id,is_enabled,can_reply,rights_json,created_at,updated_at)
            VALUES (?,?,NULL,0,0,?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                owner_id=excluded.owner_id,
                is_enabled=0,
                can_reply=0,
                rights_json=excluded.rights_json,
                updated_at=excluded.updated_at
            """,
            (connection_id, owner_id, json.dumps({"archive_only": True}, ensure_ascii=False), now, now),
        )

        existing_rows = conn.execute(
            """
            SELECT m.chat_id,m.user_id,m.content,m.updated_at
            FROM messages AS m
            LEFT JOIN chat_owners AS co
              ON m.context='regular' AND co.chat_id=m.chat_id
            LEFT JOIN business_connections AS bc
              ON m.context='business:' || bc.connection_id
            WHERE co.owner_id=? OR bc.owner_id=?
            """,
            (owner_id, owner_id),
        ).fetchall()

        existing_counts: dict[tuple[int, int, str, int], int] = {}
        for chat_id, user_id, content, updated_at in existing_rows:
            key = (int(chat_id), int(user_id or 0), str(content or ""), int(updated_at or 0) // 60)
            existing_counts[key] = existing_counts.get(key, 0) + 1

        seen_counts: dict[tuple[int, int, str, int], int] = {}
        inserted = 0
        skipped = 0

        for item in messages:
            if not isinstance(item, dict):
                skipped += 1
                continue
            try:
                chat_id = int(item.get("chat_id"))
                sender_id = int(item.get("sender_id"))
                timestamp = int(item.get("timestamp") or 0)
                seq = int(item.get("message_seq") or 0)
            except (TypeError, ValueError):
                skipped += 1
                continue

            text = str(item.get("text") or "")
            if not text or not timestamp:
                skipped += 1
                continue
            sender = str(item.get("sender") or ("Святоша" if sender_id == owner_id else f"Пользователь {sender_id}"))
            key = (chat_id, sender_id, text, timestamp // 60)
            seen_counts[key] = seen_counts.get(key, 0) + 1
            if existing_counts.get(key, 0) >= seen_counts[key]:
                skipped += 1
                continue

            # Negative IDs keep imported archive rows separate from real Telegram message IDs.
            synthetic_id = -(timestamp * 100000 + max(1, seq))
            reply_text = str(item.get("reply_text") or "").strip()
            reply_author = str(item.get("reply_author") or "").strip()
            reply_id = -1 if reply_text else None

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    context,chat_id,message_id,user_id,author,content,
                    media_type,media_file_id,media_unique_id,media_json,local_media_path,
                    has_media_spoiler,ttl_seconds,reply_to_message_id,reply_to_author,reply_to_content,
                    created_at,updated_at,deleted_at,original_content,edit_count
                ) VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,0,NULL,?,?,?,?,?,NULL,?,0)
                """,
                (
                    context,
                    chat_id,
                    synthetic_id,
                    sender_id,
                    sender,
                    text,
                    reply_id,
                    reply_author or None,
                    reply_text or None,
                    timestamp,
                    timestamp,
                    text,
                ),
            )
            if cursor.rowcount > 0:
                inserted += 1
                existing_counts[key] = existing_counts.get(key, 0) + 1
            else:
                skipped += 1

        conn.execute(
            """
            INSERT INTO maintenance_state (key,value,updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (marker, json.dumps({"file": path.name, "inserted": inserted, "skipped": skipped}, ensure_ascii=False), now),
        )

    log(f"Text archive imported: {path.name}; inserted={inserted}; skipped={skipped}; owner={owner_id}")
    return inserted, skipped


def import_pending_text_archives() -> None:
    for path in sorted(IMPORT_DIR.glob("*.json*")):
        if not path.is_file():
            continue
        try:
            import_text_archive_file(path)
        except Exception as exc:
            log(f"Text archive import failed for {path.name}: {type(exc).__name__}: {exc}")
            report_technical_issue("text_archive_import", f"{path.name}: {type(exc).__name__}: {exc}")


def register_user(
    user_id: int,
    private_chat_id: int | None = None,
    profile: dict | None = None,
) -> None:
    now = int(time.time())
    profile = profile or {}
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (
                user_id, private_chat_id, created_at, updated_at,
                first_name, last_name, username
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                private_chat_id = COALESCE(excluded.private_chat_id, users.private_chat_id),
                first_name = COALESCE(excluded.first_name, users.first_name),
                last_name = COALESCE(excluded.last_name, users.last_name),
                username = COALESCE(excluded.username, users.username),
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                private_chat_id,
                now,
                now,
                profile.get("first_name"),
                profile.get("last_name"),
                profile.get("username"),
            ),
        )


def set_chat_owner(chat_id: int, owner_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO chat_owners (chat_id, owner_id, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                created_at = excluded.created_at
            """,
            (chat_id, owner_id, int(time.time())),
        )


def get_chat_owner(chat_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT owner_id FROM chat_owners WHERE chat_id = ?", (chat_id,)).fetchone()
    return int(row[0]) if row else None


def remove_chat_owner(chat_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM chat_owners WHERE chat_id = ?", (chat_id,))


def get_user_chats(user_id: int) -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT chat_id FROM chat_owners WHERE owner_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def get_private_chat_id(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT private_chat_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return int(row[0]) if row and row[0] else user_id


def save_business_connection(connection: dict) -> bool | None:
    user = connection.get("user") or {}
    owner_id = user.get("id")
    notify_chat_id = connection.get("user_chat_id") or owner_id
    is_enabled = 1 if connection.get("is_enabled", True) else 0
    connection_rights = connection.get("rights") if isinstance(connection.get("rights"), dict) else {}
    can_reply = connection.get("can_reply")
    if can_reply is None:
        can_reply = connection_rights.get("can_reply")
    rights = {
        key: value
        for key, value in connection.items()
        if key.startswith("can_") or key in {"rights", "permissions"}
    }
    now = int(time.time())

    if owner_id:
        register_user(int(owner_id), int(notify_chat_id) if notify_chat_id else None, user)

    with sqlite3.connect(DB_PATH) as conn:
        previous = conn.execute(
            "SELECT is_enabled FROM business_connections WHERE connection_id = ?",
            (connection["id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO business_connections
                (connection_id, owner_id, notify_chat_id, is_enabled, can_reply, rights_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                owner_id = excluded.owner_id,
                notify_chat_id = excluded.notify_chat_id,
                is_enabled = excluded.is_enabled,
                can_reply = excluded.can_reply,
                rights_json = excluded.rights_json,
                updated_at = excluded.updated_at
            """,
            (
                connection["id"],
                owner_id,
                notify_chat_id,
                is_enabled,
                1 if can_reply else 0 if can_reply is not None else None,
                json.dumps(rights, ensure_ascii=False),
                connection.get("date", now),
                now,
            ),
        )
    return bool(previous[0]) if previous else None


def notify_admins_business_connection(connection: dict, previous_enabled: bool | None) -> None:
    user = connection.get("user") or {}
    owner_id = user.get("id")
    if not owner_id:
        return

    enabled = bool(connection.get("is_enabled", True))
    if previous_enabled is not None and previous_enabled == enabled:
        return

    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip() or "Без имени"
    username = str(user.get("username") or "").strip().lstrip("@")
    username_text = f"@{html_text(username)}" if username else "нет"
    action = "🟢 <b>Подключил(а) бота к Telegram Business</b>" if enabled else "🔴 <b>Отключил(а) бота от Telegram Business</b>"
    connection_id = short_connection_id(connection.get("id"))

    text = business_connection_notification_html(user, enabled) + (
        f"\n{tg_icon('info')} ID: <code>{int(owner_id)}</code>"
    )
    for admin_id in list_admin_ids():
        try:
            send_message(admin_id, text, parse_mode="HTML")
        except TelegramApiError as exc:
            log(f"Business connection admin notice failed for {admin_id}: {exc}")


def get_business_notify_chat_id(connection_id: str) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT notify_chat_id, owner_id, is_enabled
            FROM business_connections
            WHERE connection_id = ?
            """,
            (connection_id,),
        ).fetchone()
    if not row or not row[2]:
        return None
    return int(row[0] or row[1]) if row[0] or row[1] else None


def get_business_owner_id(connection_id: str) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT owner_id, is_enabled
            FROM business_connections
            WHERE connection_id = ?
            """,
            (connection_id,),
        ).fetchone()
    if not row or not row[1] or not row[0]:
        return None
    return int(row[0])


def list_business_connections(owner_id: int | None = None) -> list[dict]:
    query = """
        SELECT connection_id, owner_id, notify_chat_id, is_enabled, can_reply, created_at, updated_at
        FROM business_connections
        WHERE connection_id NOT LIKE '__archive_text__:%'
    """
    params: tuple[object, ...] = ()
    if owner_id is not None:
        query += " AND owner_id = ?"
        params = (owner_id,)
    query += " ORDER BY is_enabled DESC, updated_at DESC"

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def restore_business_connections(owner_id: int | None = None) -> list[dict]:
    rows = list_business_connections(owner_id)
    disabled = [row for row in rows if not row.get("is_enabled")]
    if not disabled:
        return []

    now = int(time.time())
    ids = [row["connection_id"] for row in disabled]
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE business_connections SET is_enabled = 1, updated_at = ? WHERE connection_id IN ({placeholders})",
            (now, *ids),
        )
    return disabled


def short_connection_id(connection_id: object) -> str:
    text = str(connection_id or "")
    if len(text) <= 16:
        return text
    return f"{text[:8]}...{text[-6:]}"


def same_user_id(left: object, right: object) -> bool:
    if left is None or right is None:
        return False
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return False


def saved_message_is_from_user(saved_message: dict | None, user_id: int | None) -> bool:
    return bool(saved_message and same_user_id(saved_message.get("user_id"), user_id))


def message_is_from_user(message: dict | None, user_id: int | None) -> bool:
    if not isinstance(message, dict):
        return False
    return same_user_id((message.get("from") or {}).get("id"), user_id)


def is_owner_admin(user_id: int | None) -> bool:
    return bool(user_id is not None and int(user_id) in ADMIN_USER_IDS)


def chat_viewing_enabled() -> bool:
    """Global kill switch for every stored-chat viewer surface."""
    try:
        return maintenance_get("chat_viewing_enabled") != "0"
    except sqlite3.Error:
        return True


def set_chat_viewing_enabled(enabled: bool, admin_id: int | None = None) -> bool:
    maintenance_set("chat_viewing_enabled", "1" if enabled else "0")
    if admin_id is not None:
        audit_admin(
            int(admin_id),
            "просмотр чатов включён" if enabled else "просмотр чатов выключен",
        )
    return enabled


def list_chat_viewer_ids() -> set[int]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return {
                int(row[0])
                for row in conn.execute("SELECT user_id FROM chat_viewer_admins").fetchall()
            }
    except sqlite3.Error:
        # Safe startup fallback before init_db has created the table.
        return set(CHAT_VIEWER_USER_IDS)


def grant_chat_view_access(owner_id: int, target_id: int) -> tuple[bool, str]:
    if not is_owner_admin(owner_id):
        return False, "Выдавать просмотр чатов может только владелец."
    if is_owner_admin(target_id):
        return False, "У владельца уже есть полный доступ к чатам."
    if not user_exists(target_id):
        return False, "Пользователь должен хотя бы один раз открыть бота."
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO chat_viewer_admins (user_id,granted_by,created_at) VALUES (?,?,?)",
            (target_id, owner_id, now),
        )
    if cur.rowcount == 0:
        return False, f"У пользователя {target_id} уже есть просмотр чатов."
    audit_admin(owner_id, "выдача просмотра чатов", target_id)
    return True, f"Просмотр чатов выдан пользователю {target_id}."


def revoke_chat_view_access(owner_id: int, target_id: int) -> tuple[bool, str]:
    if not is_owner_admin(owner_id):
        return False, "Забирать просмотр чатов может только владелец."
    if is_owner_admin(target_id):
        return False, "У владельца нельзя забрать полный доступ."
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM chat_viewer_admins WHERE user_id=?", (target_id,))
    if cur.rowcount == 0:
        return False, f"У пользователя {target_id} нет отдельного доступа к чатам."
    audit_admin(owner_id, "снятие просмотра чатов", target_id)
    return True, f"Просмотр чатов снят у пользователя {target_id}."


def can_view_user_chats(user_id: int | None) -> bool:
    return bool(
        chat_viewing_enabled()
        and user_id is not None
        and (is_owner_admin(user_id) or int(user_id) in list_chat_viewer_ids())
    )


def is_admin_user(user_id: int | None) -> bool:
    if user_id is None:
        return False
    user_id = int(user_id)
    if is_owner_admin(user_id) or user_id in list_chat_viewer_ids():
        return True
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute(
                "SELECT 1 FROM bot_admins WHERE user_id = ?", (user_id,)
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def user_chats_are_hidden(user_id: int | None) -> bool:
    """Never expose stored conversations that belong to an administrator."""
    if user_id is None:
        return False
    user_id = int(user_id)
    if user_id in CHAT_VIEW_ALWAYS_VISIBLE_USER_IDS:
        return False
    if user_id in CHAT_VIEW_BLOCKED_USER_IDS or is_admin_user(user_id):
        return True
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute(
                "SELECT 1 FROM hidden_chat_users WHERE target_id=?", (user_id,)
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def set_user_chats_hidden(admin_id: int, target_id: int, hidden: bool, reason: str = "") -> None:
    if target_id in CHAT_VIEW_ALWAYS_VISIBLE_USER_IDS and hidden:
        return
    if is_admin_user(target_id) and not hidden:
        return
    with sqlite3.connect(DB_PATH) as conn:
        if hidden:
            conn.execute(
                "INSERT OR REPLACE INTO hidden_chat_users (target_id,hidden_by,reason,created_at) VALUES (?,?,?,?)",
                (target_id, admin_id, reason[:200], int(time.time())),
            )
        else:
            conn.execute("DELETE FROM hidden_chat_users WHERE target_id=?", (target_id,))
    audit_admin(admin_id, "скрытие чатов" if hidden else "возврат чатов", target_id, reason[:200])


def log_admin_view(admin_id: int, target_id: int, chat_id: int | None, action: str, details: str = "") -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO admin_view_log (admin_id,target_id,chat_id,action,details,created_at) VALUES (?,?,?,?,?,?)",
            (admin_id, target_id, chat_id, action, details[:300], int(time.time())),
        )


def get_user_label(admin_id: int, target_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT label FROM admin_user_labels WHERE admin_id=? AND target_id=?",
            (admin_id, target_id),
        ).fetchone()
    return str(row[0]) if row else ""


def set_user_label(admin_id: int, target_id: int, label: str) -> None:
    label = label.strip()[:60]
    with sqlite3.connect(DB_PATH) as conn:
        if label:
            conn.execute(
                "INSERT OR REPLACE INTO admin_user_labels (admin_id,target_id,label,updated_at) VALUES (?,?,?,?)",
                (admin_id, target_id, label, int(time.time())),
            )
        else:
            conn.execute(
                "DELETE FROM admin_user_labels WHERE admin_id=? AND target_id=?",
                (admin_id, target_id),
            )
    audit_admin(admin_id, "метка пользователя", target_id, label or "удалена")


def set_chat_seen(admin_id: int, target_id: int, chat_id: int, seen_at: int | None = None) -> None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO chat_view_state (admin_id,target_id,chat_id,last_seen_at,pinned,updated_at)
            VALUES (?,?,?,?,0,?)
            ON CONFLICT(admin_id,target_id,chat_id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at
            """,
            (admin_id, target_id, chat_id, int(seen_at or now), now),
        )


def toggle_chat_pin(admin_id: int, target_id: int, chat_id: int) -> bool:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        current = conn.execute(
            "SELECT pinned FROM chat_view_state WHERE admin_id=? AND target_id=? AND chat_id=?",
            (admin_id, target_id, chat_id),
        ).fetchone()
        pinned = 0 if current and int(current[0]) else 1
        conn.execute(
            """
            INSERT INTO chat_view_state (admin_id,target_id,chat_id,last_seen_at,pinned,updated_at)
            VALUES (?,?,?,0,?,?)
            ON CONFLICT(admin_id,target_id,chat_id) DO UPDATE SET pinned=excluded.pinned, updated_at=excluded.updated_at
            """,
            (admin_id, target_id, chat_id, pinned, now),
        )
    return bool(pinned)


def list_admin_ids() -> list[int]:
    ids = set(ADMIN_USER_IDS) | list_chat_viewer_ids()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            ids.update(
                int(row[0])
                for row in conn.execute("SELECT user_id FROM bot_admins").fetchall()
            )
    except sqlite3.Error:
        pass
    return sorted(ids)


def list_delegated_admins() -> list[tuple[int, int, int]]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return [
                (int(row[0]), int(row[1]), int(row[2]))
                for row in conn.execute(
                    "SELECT user_id, added_by, created_at FROM bot_admins ORDER BY created_at DESC"
                ).fetchall()
            ]
    except sqlite3.Error:
        return []


def add_bot_admin(owner_id: int, target_id: int) -> tuple[bool, str]:
    if not is_owner_admin(owner_id):
        return False, "Выдавать админку может только владелец."
    if target_id in ADMIN_USER_IDS:
        return False, "Этот пользователь уже владелец бота."
    if not user_exists(target_id):
        return False, "Пользователь должен хотя бы один раз открыть бота."
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bot_admins (user_id, added_by, created_at) VALUES (?, ?, ?)",
            (target_id, owner_id, int(time.time())),
        )
    if cur.rowcount == 0:
        return False, "У пользователя уже есть админка."
    audit_admin(owner_id, "выдача админки", target_id)
    return True, f"Админка выдана пользователю {target_id}."


def remove_bot_admin(owner_id: int, target_id: int) -> tuple[bool, str]:
    if not is_owner_admin(owner_id):
        return False, "Снимать админку может только владелец."
    if target_id in ADMIN_USER_IDS:
        return False, "Владельца нельзя снять через бота."
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM bot_admins WHERE user_id = ?", (target_id,))
    if cur.rowcount == 0:
        return False, "У пользователя нет выданной админки."
    audit_admin(owner_id, "снятие админки", target_id)
    return True, f"Админка снята у пользователя {target_id}."

def deny_admin_command(chat_id: int) -> None:
    send_message(chat_id, "Эта команда доступна только администраторам.")


# ============================================================================
# ГЛАВНОЕ МЕНЮ · ПОДПИСКА · ПОМОЩЬ · АДМИН-ПАНЕЛЬ
# Premium emoji pack used both in text and inline keyboard buttons.
# ============================================================================

NEWS = {
    "home":    ("🏠", "5416041192905265756"),
    "stars":   ("⭐️", "5438496463044752972"),
    "invite":  ("🔗", "5271604874419647061"),
    "support": ("💬", "5443038326535759644"),
    "check":   ("✔️", "5206607081334906820"),
    "pay":     ("💵", "5409048419211682843"),
    "gift":    ("🎉", "5461151367559141950"),
    "admin":   ("⚙️", "5341715473882955310"),
    "view":    ("👀", "5210956306952758910"),
    "refresh": ("🔄", "5375338737028841420"),
    "bonus":   ("💎", "5427168083074628963"),
    "promo":   ("💯", "5341498088408234504"),
    "history": ("📊", "5231200819986047254"),
    "add":     ("➕", "5397916757333654639"),
    "warning": ("⚠️", "5447644880824181073"),
    "back":    ("➡️", "5416117059207572332"),
    "orders":  ("🛍", "5229064374403998351"),
    "profile": ("🙂", "5461117441612462242"),
}


def pe(name: str) -> str:
    """Премиум-эмодзи пака News в HTML-тексте с unicode-фолбэком."""
    fallback, emoji_id = NEWS[name]
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


_ME_USERNAME: str | None = None


def bot_username() -> str:
    global _ME_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    if _ME_USERNAME is None:
        try:
            _ME_USERNAME = str((telegram_call("getMe") or {}).get("username") or "")
        except TelegramApiError:
            _ME_USERNAME = ""
    return _ME_USERNAME


def btn(
    text: str,
    cb: str | None = None,
    url: str | None = None,
    copy: str | None = None,
    emoji: str | None = None,
    style: str | None = None,
    web_app: str | None = None,
) -> dict:
    item: dict[str, object] = {"text": text}
    if cb:
        item["callback_data"] = cb
    if url:
        item["url"] = url
    if copy:
        # Bot API требует RichText-объект, а не голую строку
        item["copy_text"] = {"text": copy}
    if emoji:
        item["icon_custom_emoji_id"] = NEWS[emoji][1]
    if style:
        item["style"] = style
    if web_app:
        item["web_app"] = {"url": web_app}
    return item


def kb(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


BACK_HOME = [btn("Назад", "home", emoji="back")]
BACK_PANEL = [
    btn("Назад", "panel", emoji="back"),
    btn("Главное меню", "home", emoji="home"),
]

# chat_id -> до этого момента ждём «ID дней» от админа
PENDING_GRANT: dict[int, float] = {}
# chat_id -> (дни, stars|rub, срок ожидания)
PENDING_PRICE: dict[int, tuple[int, str, float]] = {}
# admin chat_id -> (scope, deadline); preview is stored after the message arrives
PENDING_BROADCAST: dict[int, tuple[str, float]] = {}
PENDING_BROADCAST_BUTTON: dict[int, float] = {}
BROADCAST_PREVIEWS: dict[int, dict[str, object]] = {}
PENDING_PROMO_CREATE: dict[int, float] = {}
PENDING_PROMO_ACTIVATE: dict[int, float] = {}
PENDING_GIFT: dict[int, float] = {}
PENDING_SUPPORT: dict[int, float] = {}
PENDING_SUPPORT_REPLY: dict[int, tuple[int, int, float]] = {}
PENDING_BLOCK_REASON: dict[int, tuple[int, int, float]] = {}
PENDING_ADMIN_ADD: dict[int, float] = {}
PENDING_CHAT_VIEW_ACCESS: dict[int, tuple[str, float]] = {}
PENDING_CHAT_SEARCH: dict[int, tuple[int, int, float]] = {}
PENDING_CHAT_DATE: dict[int, tuple[int, int, int, int, float]] = {}
PENDING_USER_LABEL: dict[int, tuple[int, int, float]] = {}
PENDING_HIDE_CHAT_USER: dict[int, float] = {}
# chat_id -> (stage, item_id, temporary_value, deadline)
PENDING_AUTOREPLACE: dict[int, tuple[str, int | None, str | None, float]] = {}
PENDING_CUSTOM_COMMAND: dict[int, tuple[str, int | None, str | None, float]] = {}
PENDING_AUTOREPLY_MESSAGES: dict[int, float] = {}
PENDING_AUTOREPLY_PHOTO: dict[int, float] = {}
# chat_id -> (mode, deadline)
PENDING_TEXT_TOOL: dict[int, tuple[str, float]] = {}
PENDING_LINK_TOOL: dict[int, tuple[str, float]] = {}
STYLE_CHANGE_NOTICE: dict[int, tuple[str, str]] = {}
STYLE_LEVEL_NOTICE: dict[int, tuple[str, int, int]] = {}
# token -> (expires_at, owner_user_id, payload)
QUICK_ACTION_CACHE: dict[str, tuple[float, int, dict]] = {}
QUICK_ACTION_TTL_SEC = 15 * 60
LAST_MAINTENANCE_TS = 0.0
POLLING_ERROR_COUNT = 0


def required_channel_membership(
    user_id: int,
    force: bool = False,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Check required channels without treating Bot API errors as a fake unsubscribe."""
    user_id = int(user_id)
    now = time.time()
    cached = REQUIRED_CHANNEL_MEMBERSHIP_CACHE.get(user_id)
    if not force and cached and now - cached[0] < REQUIRED_CHANNEL_CACHE_TTL_SEC:
        return cached[1], cached[2], cached[3]

    missing: list[str] = []
    errors: list[str] = []

    for channel in REQUIRED_CHANNELS:
        chat_ref: object = channel["chat_id"]
        try:
            # Resolve the public @username first. This also catches a renamed/deleted channel
            # separately from a user's membership state.
            chat = telegram_call("getChat", {"chat_id": channel["chat_id"]}, timeout=15)
            if isinstance(chat, dict) and chat.get("id") is not None:
                chat_ref = int(chat["id"])

            # Administrators/creator should pass even if ordinary member lookup is restricted.
            try:
                administrators = telegram_call("getChatAdministrators", {"chat_id": chat_ref}, timeout=15) or []
                if any(
                    int(((item or {}).get("user") or {}).get("id") or 0) == user_id
                    for item in administrators
                    if isinstance(item, dict)
                ):
                    continue
            except TelegramApiError as exc:
                log(f"Required channel admin lookup failed for {user_id} in {channel['chat_id']}: {exc}")

            member = telegram_call(
                "getChatMember",
                {"chat_id": chat_ref, "user_id": user_id},
                timeout=15,
            )
            status = str((member or {}).get("status") or "")
            is_member = status in {"creator", "administrator", "member"} or (
                status == "restricted" and bool((member or {}).get("is_member"))
            )
            if not is_member:
                missing.append(str(channel["chat_id"]))
        except TelegramApiError as exc:
            log(f"Required channel check failed for {user_id} in {channel['chat_id']}: {exc}")
            errors.append(str(channel["chat_id"]))

    result = not missing and not errors
    missing_tuple = tuple(missing)
    errors_tuple = tuple(errors)
    REQUIRED_CHANNEL_MEMBERSHIP_CACHE[user_id] = (now, result, missing_tuple, errors_tuple)
    return result, missing_tuple, errors_tuple

def page_required_channels(user_id: int, force: bool = False) -> tuple[str, dict]:
    subscribed, missing, errors = required_channel_membership(user_id, force=force)
    if subscribed:
        return page_home(user_id)

    missing_set = set(missing)
    error_set = set(errors)
    rows: list[list[dict]] = []
    for channel in REQUIRED_CHANNELS:
        if channel["chat_id"] in error_set:
            marker = "⚠️ "
        elif channel["chat_id"] in missing_set:
            marker = "❗ "
        else:
            marker = "✅ "
        rows.append([btn(marker + channel["title"], url=channel["url"])])
    rows.append([btn("✅ Проверить подписку", "required:check", style="success")])
    rows.append([btn("Назад", "home", emoji="back")])

    if errors:
        issue = (
            "\n\n⚠️ <b>Telegram не дал проверить один из каналов.</b> "
            "Добавь бота администратором в оба канала, затем снова нажми «Проверить подписку»."
        )
    else:
        issue = ""

    text = (
        "🔒 <b>Для использования HolyGram подпишитесь на каналы</b>\n\n"
        "HolyGram бесплатный. Нужно быть подписанным на оба обязательных канала:\n"
        "• <b>АНОН МГН</b> — @anonmgn\n"
        "• <b>MGN VPN</b> — @mgnvpnn\n\n"
        "После подписки нажмите <b>«Проверить подписку»</b>."
        f"{issue}"
    )
    return text, kb(rows)


def require_channels_for_private_action(user_id: int, chat_id: int, force: bool = False) -> bool:
    subscribed, _, _ = required_channel_membership(user_id, force=force)
    if subscribed:
        return True
    text, markup = page_required_channels(user_id, force=False)
    send_menu_page(user_id, chat_id, text, markup)
    return False

def referral_link(user_id: int) -> str:
    username = bot_username() or "hollyboot_bot"
    return f"https://t.me/{username}?start=ref_{user_id}"


# --- подписка ----------------------------------------------------------------

def get_plans() -> dict[int, dict[str, int]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT days, price_stars, price_rub FROM subscription_plans ORDER BY days"
        ).fetchall()
    return {int(days): {"stars": int(stars), "rub": int(rub)} for days, stars, rub in rows}


def get_plan(days: int) -> dict[str, int] | None:
    return get_plans().get(days)


def set_plan_price(days: int, kind: str, value: int) -> None:
    column = "price_stars" if kind == "stars" else "price_rub"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE subscription_plans SET {column} = ?, updated_at = ? WHERE days = ?",
            (value, int(time.time()), days),
        )

def get_sub(user_id: int) -> tuple[int, int]:
    """(until_ts, trial_used)"""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT until_ts, trial_used FROM subs WHERE user_id = ?", (user_id,)
        ).fetchone()
    return (int(row[0]), int(row[1] or 0)) if row else (0, 0)


def set_until(user_id: int, until_ts: int, trial_used: int | None = None) -> None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO subs (user_id, until_ts, trial_used, last_prompt_ts, updated_at)
            VALUES (?, ?, COALESCE(?, 0), 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                until_ts = excluded.until_ts,
                trial_used = COALESCE(?, subs.trial_used),
                updated_at = excluded.updated_at
            """,
            (user_id, until_ts, trial_used, now, trial_used),
        )


def add_days(user_id: int, days: int) -> int:
    current, trial_used = get_sub(user_id)
    until = max(int(time.time()), current) + days * 86400
    set_until(user_id, until, trial_used)
    return until


def sub_active(user_id: int | None) -> bool:
    """Free access is enabled after the required channel subscriptions are verified."""
    if user_id is None:
        return True
    if is_blocked(user_id):
        return False
    subscribed, _, _ = required_channel_membership(int(user_id))
    return subscribed


STYLE_LABELS = {
    "cute": "🎀 Няшный",
    "dumb": "🧠 Тупой",
    "vasya": "🧢 Вася",
    "brother": "🤝 Брат",
    "rooster": "🐓 Петух",
    "schizo": "👁 Шизик",
}

STYLE_EXAMPLES = {
    "cute": "приветикк, ты гдеее? я уже соскучилась 🥺♡",
    "dumb": "кароч я щас хз чо делать, типо потом разберёмся",
    "vasya": "вась, ты щас где? го потом, а то ваще дел много",
    "brother": "брат, салам. от души, давай потом спокойно решим",
    "rooster": "бля, ну ты где нахуй? я уже заебался ждать",
    "schizo": "я скоро автобус огурцы домой макароны земля папа лампа",
}

# CuteMessages-inspired building blocks from the supplied plugin.
CUTE_STYLE_EMOJIS = ("🥰", "😊", "💕", "💖", "💗", "🌸", "✨", "🎀", "🥺", "🫶", "🫧", "🦋", "🐰", "🌷")
CUTE_STYLE_KAOMOJI = (
    "(◕‿◕)", "♡(˘▽˘)♡", "(つ≧▽≦)つ", "(≧◡≦)", "(っ˘ω˘ς)",
    "(´｡• ω •｡)", "ʕ•ᴥ•ʔ", "(っ•ᴗ•)っ", "૮₍˶ᵔ ᵕ ᵔ˶₎ა", "(◕ᴗ◕✿)"
)
CUTE_STYLE_SUFFIXES = ("~", " nya~", " uwu", " owo", " :3", " hehe~", " nyaa", " ♡")
CUTE_STYLE_ACTIONS = ("*обнимает*", "*хихикает*", "*улыбается*", "*машет лапкой*", "*краснеет*")
CUTE_STYLE_VOWELS = "аеёиоуыэюяaeiouy"
CUTE_STYLE_CONSONANTS_RU = "бвгджзйклмнпрстфхцчшщ"
CUTE_STYLE_PUNCT_QUESTION = ("?🥺", "?♡", "?✨", "?~")
CUTE_STYLE_PUNCT_EXCLAMATION = ("!✨", "!💖", "!♡", "~!")
CUTE_STYLE_PUNCT_PERIOD = (".~", ".♡", ".✨")

ROOSTER_PROFANITY_LEVELS = (10, 20, 40, 60, 100)
ROOSTER_PROFANITY_WORDS = (
    "бля", "блядь", "сука", "нахуй", "пиздец", "хуйня", "ебать", "ёбаный",
    "ебучий", "заебал", "заебись", "охуеть", "охуенно", "нихуя", "похуй",
    "хуёво", "пиздато", "до хуя", "хуй знает", "ёб твою мать", "ебануться",
    "какого хуя", "в пизду", "нахуя"
)
ROOSTER_PROFANITY_PREFIXES = ("бля", "сука", "ебать", "пиздец", "охуеть", "какого хуя")
ROOSTER_PROFANITY_SUFFIXES = ("нахуй", "блядь", "сука", "пиздец", "в пизду", "и похуй")

SCHIZO_WORDS = (
    "автобус", "макароны", "огурцы", "земля", "мама", "папа", "лампа", "диван",
    "чайник", "тапки", "гараж", "кирпич", "кошка", "маршрутка", "пылесос", "ложка",
    "хлеб", "телевизор", "картошка", "арбуз", "подъезд", "лифт", "носок", "стол",
    "окно", "сосиска", "сахар", "батарея", "пакет", "дверь", "асфальт", "трамвай",
    "кастрюля", "банан", "шкаф", "подушка", "морковь", "ведро", "кран", "пельмени",
    "зонт", "балкон", "кефир", "бензин", "шампунь", "лестница", "сыр", "трава",
    "коробка", "молоко", "розетка", "карандаш", "снег", "печенье", "кроссовок",
    "гречка", "холодильник", "провод", "помидор", "облако", "двор", "кактус",
    "штора", "рюкзак", "тарелка", "метро", "собака", "ковёр", "майонез", "мыло",
)
SCHIZO_GLUE = ("", ",", "...", " короче", " почему-то", " буквально", " вообще")
SCHIZO_MAX_INSERTIONS = 7

STYLE_PROTECTED_RE = re.compile(
    r"(?i)(?:https?://\S+|tg://\S+|www\.\S+|(?:t\.me|telegram\.me)/\S+|"
    r"@[a-z0-9_]{3,32}\b|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?|"
    r"(?<!\d)\+?\d[\d\s()\-]{6,}\d(?!\d))"
)


MAX_USER_AUTOREPLACE_RULES = 50
MAX_USER_COMMANDS = 30
MAX_USER_COMMAND_MESSAGES = 20


def list_auto_replacements(user_id: int) -> list[tuple[int, str, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        return [
            (int(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                "SELECT id,trigger,replacement FROM user_auto_replacements WHERE user_id=? ORDER BY updated_at DESC,id DESC",
                (user_id,),
            ).fetchall()
        ]


def get_auto_replacement(user_id: int, rule_id: int) -> tuple[int, str, str] | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id,trigger,replacement FROM user_auto_replacements WHERE user_id=? AND id=?",
            (user_id, rule_id),
        ).fetchone()
    return (int(row[0]), str(row[1]), str(row[2])) if row else None


def save_auto_replacement(user_id: int, trigger: str, replacement: str, rule_id: int | None = None) -> tuple[bool, str]:
    trigger = re.sub(r"\s+", " ", trigger.strip())
    replacement = replacement.strip()
    if not 1 <= len(trigger) <= 64:
        return False, "Слово или фраза должны быть длиной от 1 до 64 символов."
    if not 1 <= len(replacement) <= 500:
        return False, "Текст замены должен быть длиной от 1 до 500 символов."
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        if rule_id is None:
            count = int(conn.execute("SELECT COUNT(*) FROM user_auto_replacements WHERE user_id=?", (user_id,)).fetchone()[0])
            if count >= MAX_USER_AUTOREPLACE_RULES:
                return False, f"Можно создать максимум {MAX_USER_AUTOREPLACE_RULES} автозамен."
            try:
                conn.execute(
                    "INSERT INTO user_auto_replacements (user_id,trigger,replacement,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (user_id, trigger, replacement, now, now),
                )
            except sqlite3.IntegrityError:
                return False, "Такая автозамена уже существует."
        else:
            try:
                cur = conn.execute(
                    "UPDATE user_auto_replacements SET trigger=?,replacement=?,updated_at=? WHERE user_id=? AND id=?",
                    (trigger, replacement, now, user_id, rule_id),
                )
            except sqlite3.IntegrityError:
                return False, "Автозамена с таким словом уже существует."
            if cur.rowcount == 0:
                return False, "Автозамена не найдена."
    return True, "Автозамена сохранена."


def delete_auto_replacement(user_id: int, rule_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM user_auto_replacements WHERE user_id=? AND id=?", (user_id, rule_id))
    return cur.rowcount > 0


def _replace_user_segment(segment: str, rules: list[tuple[int, str, str]]) -> str:
    result = segment
    for _rule_id, trigger, replacement in sorted(rules, key=lambda row: len(row[1]), reverse=True):
        pattern = re.compile(r"(?iu)(?<!\w)" + re.escape(trigger) + r"(?!\w)")
        result = pattern.sub(lambda _match, repl=replacement: repl, result)
    return result


def apply_user_auto_replacements(user_id: int, text: str) -> str:
    if not text or text.lstrip().startswith(("/", ".")):
        return text
    rules = list_auto_replacements(user_id)
    if not rules:
        return text
    parts: list[str] = []
    last = 0
    for match in STYLE_PROTECTED_RE.finditer(text):
        parts.append(_replace_user_segment(text[last:match.start()], rules))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_replace_user_segment(text[last:], rules))
    return "".join(parts)


def normalize_custom_command_trigger(value: str) -> str:
    value = re.sub(r"\s+", "", value.strip().lower())
    if value and not value.startswith("."):
        value = "." + value
    return value


def list_custom_commands(user_id: int) -> list[tuple[int, str, list[str]]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id,trigger,messages_json FROM user_custom_commands WHERE user_id=? ORDER BY updated_at DESC,id DESC",
            (user_id,),
        ).fetchall()
    result: list[tuple[int, str, list[str]]] = []
    for command_id, trigger, raw in rows:
        try:
            messages = json.loads(str(raw))
        except json.JSONDecodeError:
            messages = []
        if not isinstance(messages, list):
            messages = []
        result.append((int(command_id), str(trigger), [str(item) for item in messages if str(item).strip()]))
    return result


def get_custom_command(user_id: int, command_id: int) -> tuple[int, str, list[str]] | None:
    for row in list_custom_commands(user_id):
        if row[0] == command_id:
            return row
    return None


def find_custom_command(user_id: int, trigger: str) -> tuple[int, str, list[str]] | None:
    normalized = normalize_custom_command_trigger(trigger)
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT id,trigger,messages_json FROM user_custom_commands WHERE user_id=? AND trigger=? COLLATE NOCASE",
            (user_id, normalized),
        ).fetchone()
    if not row:
        return None
    try:
        messages = json.loads(str(row[2]))
    except json.JSONDecodeError:
        messages = []
    return (int(row[0]), str(row[1]), [str(item) for item in messages if str(item).strip()])


def save_custom_command(user_id: int, trigger: str, messages: list[str], command_id: int | None = None) -> tuple[bool, str]:
    trigger = normalize_custom_command_trigger(trigger)
    messages = [str(item).strip() for item in messages if str(item).strip()]
    if not re.fullmatch(r"\.[\wа-яё-]{1,31}", trigger, flags=re.IGNORECASE):
        return False, "Команда: только буквы, цифры, _ или -. Например: .привет"
    if not messages:
        return False, "Добавь хотя бы одно сообщение."
    if len(messages) > MAX_USER_COMMAND_MESSAGES:
        return False, f"В одной команде максимум {MAX_USER_COMMAND_MESSAGES} сообщений."
    if any(len(item) > 1000 for item in messages):
        return False, "Каждое сообщение команды — максимум 1000 символов."
    now = int(time.time())
    raw = json.dumps(messages, ensure_ascii=False)
    with sqlite3.connect(DB_PATH) as conn:
        if command_id is None:
            count = int(conn.execute("SELECT COUNT(*) FROM user_custom_commands WHERE user_id=?", (user_id,)).fetchone()[0])
            if count >= MAX_USER_COMMANDS:
                return False, f"Можно создать максимум {MAX_USER_COMMANDS} команд."
            try:
                conn.execute(
                    "INSERT INTO user_custom_commands (user_id,trigger,messages_json,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (user_id, trigger, raw, now, now),
                )
            except sqlite3.IntegrityError:
                return False, "Такая команда уже существует."
        else:
            try:
                cur = conn.execute(
                    "UPDATE user_custom_commands SET trigger=?,messages_json=?,updated_at=? WHERE user_id=? AND id=?",
                    (trigger, raw, now, user_id, command_id),
                )
            except sqlite3.IntegrityError:
                return False, "Команда с таким названием уже существует."
            if cur.rowcount == 0:
                return False, "Команда не найдена."
    return True, "Команда сохранена."


def delete_custom_command(user_id: int, command_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("DELETE FROM user_custom_commands WHERE user_id=? AND id=?", (user_id, command_id))
    return cur.rowcount > 0


MAX_AUTOREPLY_MESSAGES = 10
MAX_AUTOREPLY_MESSAGE_LENGTH = 900


def get_autoreply_settings(user_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT enabled,messages_json,photo_file_id FROM user_autoreply_settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"enabled": False, "messages": [], "photo_file_id": ""}
    try:
        messages = json.loads(str(row[1] or "[]"))
    except json.JSONDecodeError:
        messages = []
    if not isinstance(messages, list):
        messages = []
    clean_messages = [
        str(item).strip()
        for item in messages
        if str(item).strip()
    ][:MAX_AUTOREPLY_MESSAGES]
    return {
        "enabled": bool(row[0]),
        "messages": clean_messages,
        "photo_file_id": str(row[2] or ""),
    }


def _save_autoreply_settings(user_id: int, enabled: bool, messages: list[str], photo_file_id: str) -> None:
    clean_messages = [str(item).strip()[:MAX_AUTOREPLY_MESSAGE_LENGTH] for item in messages if str(item).strip()]
    clean_messages = clean_messages[:MAX_AUTOREPLY_MESSAGES]
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO user_autoreply_settings (user_id,enabled,messages_json,photo_file_id,updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled=excluded.enabled,
                messages_json=excluded.messages_json,
                photo_file_id=excluded.photo_file_id,
                updated_at=excluded.updated_at
            """,
            (user_id, 1 if enabled else 0, json.dumps(clean_messages, ensure_ascii=False), photo_file_id or None, now),
        )


def set_autoreply_enabled(user_id: int, enabled: bool) -> tuple[bool, str]:
    settings = get_autoreply_settings(user_id)
    if enabled and not settings["messages"] and not settings["photo_file_id"]:
        return False, "Сначала добавь хотя бы одно сообщение или картинку."
    _save_autoreply_settings(user_id, enabled, settings["messages"], settings["photo_file_id"])
    return True, "Автоответчик включён." if enabled else "Автоответчик выключен."


def set_autoreply_messages(user_id: int, messages: list[str]) -> tuple[bool, str]:
    cleaned = [re.sub(r"\s+$", "", str(item)).strip() for item in messages if str(item).strip()]
    if not cleaned:
        return False, "Добавь хотя бы одно сообщение."
    if len(cleaned) > MAX_AUTOREPLY_MESSAGES:
        return False, f"Можно сохранить максимум {MAX_AUTOREPLY_MESSAGES} вариантов."
    if any(len(item) > MAX_AUTOREPLY_MESSAGE_LENGTH for item in cleaned):
        return False, f"Каждый вариант — максимум {MAX_AUTOREPLY_MESSAGE_LENGTH} символов."
    settings = get_autoreply_settings(user_id)
    _save_autoreply_settings(user_id, settings["enabled"], cleaned, settings["photo_file_id"])
    return True, f"Сохранено вариантов: {len(cleaned)}."


def clear_autoreply_messages(user_id: int) -> None:
    settings = get_autoreply_settings(user_id)
    enabled = settings["enabled"] and bool(settings["photo_file_id"])
    _save_autoreply_settings(user_id, enabled, [], settings["photo_file_id"])


def set_autoreply_photo(user_id: int, file_id: str) -> None:
    settings = get_autoreply_settings(user_id)
    _save_autoreply_settings(user_id, settings["enabled"], settings["messages"], str(file_id or ""))


def clear_autoreply_photo(user_id: int) -> None:
    settings = get_autoreply_settings(user_id)
    enabled = settings["enabled"] and bool(settings["messages"])
    _save_autoreply_settings(user_id, enabled, settings["messages"], "")


def parse_autoreply_messages(raw: str) -> list[str]:
    raw = str(raw or "").strip()
    if not raw:
        return []
    if re.search(r"\n\s*---+\s*\n", raw):
        parts = re.split(r"\n\s*---+\s*\n", raw)
    else:
        parts = raw.splitlines()
    return [part.strip() for part in parts if part.strip()]


def choose_autoreply_message(settings: dict, incoming_message: dict) -> str:
    messages = list(settings.get("messages") or [])
    if not messages:
        return ""
    try:
        message_id = int(incoming_message.get("message_id") or 0)
        chat_id = int((incoming_message.get("chat") or {}).get("id") or 0)
    except (TypeError, ValueError):
        message_id = 0
        chat_id = 0
    index = abs((message_id * 1315423911) ^ chat_id) % len(messages)
    return str(messages[index])


def maybe_send_business_autoreply(message: dict, owner_id: int | None) -> bool:
    if owner_id is None or message.get("sender_business_bot") or message_is_from_user(message, owner_id):
        return False
    connection_id = str(message.get("business_connection_id") or "")
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    if not connection_id or not chat_id:
        return False

    settings = get_autoreply_settings(owner_id)
    if not settings["enabled"]:
        return False
    text = choose_autoreply_message(settings, message)
    photo_file_id = str(settings.get("photo_file_id") or "")
    if not text and not photo_file_id:
        return False

    try:
        if photo_file_id:
            payload: dict[str, object] = {
                "business_connection_id": connection_id,
                "chat_id": chat_id,
                "photo": photo_file_id,
            }
            if text:
                payload["caption"] = text[:1024]
            telegram_call("sendPhoto", payload)
        else:
            telegram_call(
                "sendMessage",
                {
                    "business_connection_id": connection_id,
                    "chat_id": chat_id,
                    "text": text[:4096],
                },
            )
        return True
    except TelegramApiError as exc:
        log(f"Business autoresponder failed for {owner_id}/{chat_id}: {exc}")
        return False


def get_communication_style(user_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT communication_style FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    return str(row[0] or "") if row else ""


def set_communication_style(user_id: int, style: str) -> None:
    if style not in {"", *STYLE_LABELS}:
        raise ValueError("Неизвестный стиль общения")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET communication_style = ?, updated_at = ? WHERE user_id = ?",
            (style, int(time.time()), user_id),
        )


def get_rooster_profanity_percent(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT rooster_profanity_percent FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    value = int(row[0] or 40) if row else 40
    return value if value in ROOSTER_PROFANITY_LEVELS else 40


def set_rooster_profanity_percent(user_id: int, percent: int) -> None:
    if percent not in ROOSTER_PROFANITY_LEVELS:
        raise ValueError("Недопустимый уровень мата")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET rooster_profanity_percent = ?, updated_at = ? WHERE user_id = ?",
            (percent, int(time.time()), user_id),
        )


def _match_case(source: str, replacement: str) -> str:
    if not source:
        return replacement
    if source.isupper():
        return replacement.upper()
    if source[0].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _replace_style_phrases(text: str, replacements: dict[str, str]) -> str:
    result = text
    # Longer phrases first so "потому что" wins over a possible "что".
    for source in sorted(replacements, key=len, reverse=True):
        target = replacements[source]
        pattern = re.compile(r"(?iu)(?<!\w)" + re.escape(source) + r"(?!\w)")
        result = pattern.sub(lambda m: _match_case(m.group(0), target), result)
    return result


def _schizo_pick(items: tuple[str, ...]) -> str:
    if not items:
        return ""
    return items[uuid.uuid4().int % len(items)]


def _schizo_transform_segment(text: str) -> str:
    if not text.strip():
        return text
    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):] if text.rstrip() != text else ""
    body = text.strip()
    tokens = body.split()
    if not tokens:
        return text

    insertion_count = min(
        SCHIZO_MAX_INSERTIONS,
        max(2, 2 + len(tokens) // 3 + (uuid.uuid4().int % 3)),
    )
    output = list(tokens)

    for _ in range(insertion_count):
        word = _schizo_pick(SCHIZO_WORDS)
        position = uuid.uuid4().int % (len(output) + 1)
        glue = _schizo_pick(SCHIZO_GLUE)
        if glue and position > 0:
            word = glue + " " + word
        output.insert(position, word)

    if len(output) >= 4 and uuid.uuid4().int % 100 < 45:
        position = uuid.uuid4().int % len(output)
        output.insert(position, _schizo_pick(SCHIZO_WORDS))

    result = " ".join(output)
    result = re.sub(r"\s+([,.!?])", r"\1", result)
    if uuid.uuid4().int % 100 < 35:
        result = result.rstrip(" .!?") + _schizo_pick(("...", "?", ".", " вообще.", " почему."))
    return leading + result + trailing


def _style_plain_segment(style: str, segment: str) -> str:
    if not segment or not segment.strip():
        return segment

    if style == "schizo":
        return segment

    if style == "cute":
        replacements = {
            "ты где": "ты гдеее",
            "доброе утро": "доброе утречко",
            "спокойной ночи": "сладких снов",
            "пожалуйста": "пожалуйстаа",
            "спасибо": "спасибочки",
            "большое спасибо": "спасибочки большое",
            "привет": "приветик",
            "здравствуй": "приветик",
            "пока": "поки",
            "хорошо": "хорошоо",
            "отлично": "супер",
            "скучаю": "скучаюю",
            "люблю": "люблюю",
            "да": "ага",
            "нет": "неа",
        }
        return _replace_style_phrases(segment, replacements)

    if style == "rooster":
        replacements = {
            "пожалуйста": "давай",
            "спасибо": "спасибо",
            "привет": "здоров",
            "здравствуй": "здоров",
            "пока": "давай",
            "хорошо": "нормально",
            "отлично": "нормально",
            "сейчас": "щас",
            "вообще": "ваще",
            "ничего": "ничё",
            "что-то": "чё-то",
            "что-нибудь": "чё-нибудь",
        }
        return _replace_style_phrases(segment, replacements)

    if style == "vasya":
        replacements = {
            "потому что": "потому шо",
            "что-нибудь": "чё-нибудь",
            "что-то": "чё-то",
            "ничего": "ничё",
            "сейчас": "щас",
            "вообще": "ваще",
            "конечно": "канеш",
            "пожалуйста": "будь добр",
            "нормально": "норм",
            "хорошо": "норм",
            "здесь": "тут",
            "теперь": "терь",
            "что": "чё",
        }
        result = _replace_style_phrases(segment, replacements)
        return re.sub(r"(?iu)\bне знаю\b", "хз", result)

    if style == "brother":
        replacements = {
            "большое спасибо": "от души",
            "спасибо": "от души",
            "пожалуйста": "будь добр, брат",
            "привет": "салам",
            "здравствуй": "салам",
            "хорошо": "договорились",
            "отлично": "красиво",
            "друг": "бро",
            "дружище": "братан",
            "не переживай": "не кипишуй",
            "всё нормально": "всё ровно",
        }
        return _replace_style_phrases(segment, replacements)

    replacements = {
        "потому что": "потому что, получается",
        "получается": "выходит",
        "что-нибудь": "чо-нибудь",
        "что-то": "чо-то",
        "ничего": "ничо",
        "сейчас": "щас",
        "вообще": "ваще",
        "конечно": "ну да, наверное",
        "что": "чо",
        "зачем": "зачем это",
        "почему": "почему это",
        "хорошо": "ну, нормально",
    }
    result = _replace_style_phrases(segment, replacements)
    result = re.sub(r"(?iu)\bя не знаю\b", "я это... не знаю", result)
    return re.sub(r"(?iu)(?<!это\.\.\. )\bне знаю\b", "как бы не знаю", result)


def _style_intent(text: str) -> str:
    plain = text.strip().lower()
    if "?" in plain or re.match(r"^(где|когда|куда|почему|зачем|как|кто|что|ты|вы)\b", plain):
        return "question"
    if re.search(r"\b(спасибо|благодарю|от души|спасибочки)\b", plain):
        return "thanks"
    if re.search(r"\b(привет|здравствуй|доброе утро|салам|приветик)\b", plain):
        return "greeting"
    if re.search(r"\b(пожалуйста|можешь|сделай|пришли|позвони|напиши)\b", plain):
        return "request"
    return "statement"


def _cute_seed(source: str) -> int:
    return sum((index + 1) * ord(char) for index, char in enumerate(source.lower()))


def _stable_roll(seed: int, salt: int, modulo: int = 100) -> int:
    return (seed * 131 + salt * 977 + salt * salt * 17) % modulo


def _cute_stretch_word(word: str, seed: int, salt: int) -> str:
    vowel_positions = [i for i, char in enumerate(word.lower()) if char in CUTE_STYLE_VOWELS]
    if not vowel_positions:
        return word
    position = vowel_positions[_stable_roll(seed, salt, len(vowel_positions))]
    char = word[position]
    extra = 1 + (_stable_roll(seed, salt + 7, 2))
    return word[: position + 1] + char * extra + word[position + 1 :]


def _cute_transform_segment(text: str, source: str) -> str:
    if not text.strip():
        return text
    seed = _cute_seed(source)
    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):] if text.rstrip() != text else ""
    body = text.strip()

    # Lowercase is one of the plugin's optional transformations. Preserve obvious acronyms.
    if not re.search(r"\b[A-ZА-ЯЁ]{2,}\b", body):
        body = body.lower()

    token_matches = list(re.finditer(r"(?iu)\b[а-яёa-z]{2,}\b", body))
    offset = 0
    stretched = False
    stuttered = False
    for index, match in enumerate(token_matches):
        start = match.start() + offset
        end = match.end() + offset
        word = body[start:end]
        changed = word

        if not stuttered and len(word) >= 4 and _stable_roll(seed, index + 11) < 16:
            changed = word[0] + "-" + word
            stuttered = True

        if not stretched and len(word) >= 3 and _stable_roll(seed, index + 23) < 38:
            changed = _cute_stretch_word(changed, seed, index + 31)
            stretched = True

        if (
            len(changed) >= 3
            and re.fullmatch(r"(?iu)[а-яё-]+", changed)
            and changed[-1].lower() in CUTE_STYLE_CONSONANTS_RU
            and _stable_roll(seed, index + 47) < 9
        ):
            changed += "ь"

        if changed != word:
            body = body[:start] + changed + body[end:]
            offset += len(changed) - len(word)

    # Classic-effects feel: decorate some words, but cap it to keep messages readable.
    words = body.split()
    max_emojis = min(4, max(1, len(words) // 3)) if words else 0
    used = 0
    decorated: list[str] = []
    for index, word in enumerate(words):
        decorated.append(word)
        if used < max_emojis and _stable_roll(seed, index + 101) < 34:
            decorated.append(CUTE_STYLE_EMOJIS[_stable_roll(seed, index + 113, len(CUTE_STYLE_EMOJIS))])
            used += 1
    body = " ".join(decorated)

    if body.endswith("?"):
        if _stable_roll(seed, 201) < 55:
            body = body[:-1] + CUTE_STYLE_PUNCT_QUESTION[_stable_roll(seed, 202, len(CUTE_STYLE_PUNCT_QUESTION))]
    elif body.endswith("!"):
        if _stable_roll(seed, 203) < 45:
            body = body[:-1] + CUTE_STYLE_PUNCT_EXCLAMATION[_stable_roll(seed, 204, len(CUTE_STYLE_PUNCT_EXCLAMATION))]
    elif body.endswith(".") and not body.endswith("..."):
        if _stable_roll(seed, 205) < 35:
            body = body[:-1] + CUTE_STYLE_PUNCT_PERIOD[_stable_roll(seed, 206, len(CUTE_STYLE_PUNCT_PERIOD))]

    if _stable_roll(seed, 221) < 42:
        body = body.rstrip() + CUTE_STYLE_SUFFIXES[_stable_roll(seed, 222, len(CUTE_STYLE_SUFFIXES))]
    elif _stable_roll(seed, 223) < 32:
        body = body.rstrip() + " " + CUTE_STYLE_KAOMOJI[_stable_roll(seed, 224, len(CUTE_STYLE_KAOMOJI))]

    if len(body) > 24 and _stable_roll(seed, 241) < 12:
        body += "\n" + CUTE_STYLE_ACTIONS[_stable_roll(seed, 242, len(CUTE_STYLE_ACTIONS))]

    return leading + body + trailing


def _apply_rooster_phrase_replacements(text: str, percent: int, seed: int) -> str:
    result = text
    replacements = [
        ("не знаю", "хуй знает", 20),
        ("зачем", "нахуя", 20),
        ("плохо", "хуёво", 40),
        ("очень", "пиздец как", 40),
        ("круто", "охуенно", 40),
        ("отлично", "охуенно", 60),
        ("надоело", "заебало", 60),
        ("достало", "заебало", 60),
        ("ерунда", "хуйня", 60),
        ("фигня", "хуйня", 60),
        ("без разницы", "похуй", 60),
    ]
    for index, (source, replacement, minimum) in enumerate(replacements):
        if percent < minimum:
            continue
        chance = min(100, percent + 15)
        if _stable_roll(seed, 300 + index) >= chance:
            continue
        pattern = re.compile(r"(?iu)(?<!\w)" + re.escape(source) + r"(?!\w)")
        result = pattern.sub(replacement, result)
    return result


def _apply_rooster_profanity(text: str, percent: int, source: str) -> str:
    if not text.strip() or percent <= 0:
        return text
    seed = _cute_seed(source)
    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()):] if text.rstrip() != text else ""
    body = text.strip()
    body = _apply_rooster_phrase_replacements(body, percent, seed)

    words = body.split()
    if not words:
        return text

    # The percentage controls how many available insertion slots are filled.
    slots = min(10, max(1, len(words) // 2 + 1))
    target = int(round(slots * percent / 100))
    if target == 0 and _stable_roll(seed, 351) < percent:
        target = 1

    candidate_positions = list(range(len(words)))
    candidate_positions.sort(key=lambda pos: _stable_roll(seed, 400 + pos, 1000))
    chosen = set(candidate_positions[:target])
    output: list[str] = []
    for index, word in enumerate(words):
        if index in chosen and _stable_roll(seed, 500 + index) < 45:
            output.append(ROOSTER_PROFANITY_PREFIXES[_stable_roll(seed, 520 + index, len(ROOSTER_PROFANITY_PREFIXES))])
        output.append(word)
        if index in chosen and _stable_roll(seed, 540 + index) >= 45:
            output.append(ROOSTER_PROFANITY_WORDS[_stable_roll(seed, 560 + index, len(ROOSTER_PROFANITY_WORDS))])

    body = " ".join(output)

    if percent >= 40 and _stable_roll(seed, 601) < percent:
        body = ROOSTER_PROFANITY_PREFIXES[_stable_roll(seed, 602, len(ROOSTER_PROFANITY_PREFIXES))] + ", " + body
    if percent >= 60 and _stable_roll(seed, 603) < percent:
        body = body.rstrip(" .,!?:;") + ", " + ROOSTER_PROFANITY_SUFFIXES[_stable_roll(seed, 604, len(ROOSTER_PROFANITY_SUFFIXES))]

    return leading + body + trailing


def _add_style_flavour(style: str, text: str, source: str, user_id: int | None = None) -> str:
    stripped = text.strip()
    if not stripped:
        return text
    intent = _style_intent(source)
    score = _cute_seed(source)
    leading = text[: len(text) - len(text.lstrip())]
    body = text.strip()

    if style == "cute":
        return _cute_transform_segment(text, source)

    if style == "schizo":
        return _schizo_transform_segment(text)

    if style == "rooster":
        percent = get_rooster_profanity_percent(user_id) if user_id is not None else 40
        body = _apply_rooster_profanity(body.lower(), percent, source)
    elif style == "vasya":
        if intent == "question" and len(body) > 15 and not re.match(r"(?iu)^(вась|слушай)\b", body):
            body = ("вась, " if score % 2 else "слушай, ") + body[:1].lower() + body[1:]
        elif intent == "statement" and len(body) > 32 and score % 4 == 0 and "ну ты понял" not in body.lower():
            body = body.rstrip(" .") + ", ну ты понял"
        elif text == source and len(body) > 5 and not re.search(r"(?iu)\b(вась|слушай|прикинь|ну ты понял)\b", body):
            if score % 3 == 0:
                body = "слушай, " + body[:1].lower() + body[1:]
            elif score % 3 == 1:
                body = body.rstrip(" .") + ", прикинь"
            else:
                body = "вась, " + body[:1].lower() + body[1:]
    elif style == "brother":
        if intent in {"question", "request"} and not re.search(r"(?iu)\b(брат|братан|бро|родной)\b", body):
            address = ("брат" if score % 3 == 0 else "бро" if score % 3 == 1 else "родной")
            body = f"{address}, " + body[:1].lower() + body[1:]
        elif intent == "thanks" and not re.search(r"(?iu)\b(брат|братан|бро|родной)\b", body):
            body = body.rstrip(" .!") + ", брат"
        elif text == source and len(body) > 5 and not re.search(r"(?iu)\b(брат|братан|бро|родной)\b", body):
            address = ("брат" if score % 3 == 0 else "бро" if score % 3 == 1 else "родной")
            body = body.rstrip(" .") + f", {address}"
    elif style == "dumb":
        if intent == "question" and not re.match(r"(?iu)^(это|слушай|короче)\b", body):
            body = "это... " + body[:1].lower() + body[1:]
        elif len(body) > 24 and score % 3 == 0 and not re.search(r"(?iu)\b(получается|как бы|это\.\.\.)\b", body):
            split = re.search(r"[,;]", body)
            if split:
                pos = split.end()
                body = body[:pos] + " получается," + body[pos:]
            else:
                body = "короче, " + body[:1].lower() + body[1:]
        elif text == source and len(body) > 5 and not re.match(r"(?iu)^(это|слушай|короче|ну это)\b", body):
            body = "ну это... " + body[:1].lower() + body[1:]
    return leading + body


def stylize_message_text(style: str, text: str, user_id: int | None = None) -> str:
    if style not in STYLE_LABELS or not text or not text.strip() or text.lstrip().startswith(("/", ".")):
        return text
    if not STYLE_PROTECTED_RE.sub("", text).strip():
        return text

    parts: list[str] = []
    last = 0
    for match in STYLE_PROTECTED_RE.finditer(text):
        plain = _style_plain_segment(style, text[last:match.start()])
        parts.append(_add_style_flavour(style, plain, text[last:match.start()], user_id))
        parts.append(match.group(0))
        last = match.end()

    plain = _style_plain_segment(style, text[last:])
    parts.append(_add_style_flavour(style, plain, text[last:], user_id))
    result = "".join(parts)

    result = re.sub(r"(?:\s+:3){2,}\s*$", " :3", result)
    result = re.sub(r"(?:\s+♡){2,}\s*$", " ♡", result)
    return result


def transform_message_style(user_id: int, text: str, max_length: int = 4096) -> str:
    if not sub_active(user_id):
        return text
    style = get_communication_style(user_id)
    if not style:
        return text
    result = stylize_message_text(style, text, user_id)
    return text if not result or len(result) > max_length else result


def sub_days_left(user_id: int) -> int:
    left = get_sub(user_id)[0] - int(time.time())
    return max(0, (left + 86399) // 86400)


def format_until(until_ts: int) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(until_ts)) if until_ts else "—"


def status_line(user_id: int) -> str:
    return f"{pe('check')} <b>бесплатно для всех</b>"


def user_exists(user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is not None


def is_blocked(user_id: int) -> bool:
    if is_admin_user(user_id):
        return False
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (user_id,)).fetchone() is not None


def audit_admin(admin_id: int, action: str, target_id: int | None = None, details: str = "") -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO admin_actions (admin_id, action, target_id, details, created_at) VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, target_id, details[:1000], int(time.time())),
        )


def active_promo(user_id: int) -> tuple[str, int] | None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT p.code, p.discount_percent
            FROM promo_activations AS a
            JOIN promo_codes AS p ON p.code = a.code
            WHERE a.user_id = ? AND p.active = 1 AND p.expires_at >= ? AND p.uses < p.max_uses
            """,
            (user_id, now),
        ).fetchone()
    return (str(row[0]), int(row[1])) if row else None


def discounted_price(user_id: int, base_price: int) -> tuple[int, str | None]:
    promo = active_promo(user_id)
    if not promo:
        return base_price, None
    code, discount = promo
    return max(1, (base_price * (100 - discount) + 99) // 100), code


def activate_promo(user_id: int, code: str) -> tuple[bool, str]:
    code = code.strip().upper()
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT discount_percent, expires_at, max_uses, uses, active FROM promo_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if not row or not int(row[4]) or int(row[1]) < now or int(row[3]) >= int(row[2]):
            return False, "Промокод не найден, закончился или исчерпан."
        conn.execute(
            """
            INSERT INTO promo_activations (user_id, code, activated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET code = excluded.code, activated_at = excluded.activated_at
            """,
            (user_id, code, now),
        )
    return True, f"Промокод {code} активирован: скидка {int(row[0])}%."


def consume_promo(user_id: int, code: str | None) -> None:
    if not code:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE promo_codes SET uses = uses + 1
            WHERE code = ? AND active = 1 AND uses < max_uses AND expires_at >= ?
            """,
            (code, int(time.time())),
        )
        conn.execute("DELETE FROM promo_activations WHERE user_id = ? AND code = ?", (user_id, code))


def owner_can_log(owner_id: int | None) -> bool:
    """Logging is free, but the owner must stay subscribed to required channels."""
    return owner_id is None or sub_active(owner_id)


# --- рефералы -----------------------------------------------------------------

def ref_count(user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,)).fetchone()
    return int(row[0])


def add_referral(referrer_id: int, invitee_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO referrals (referrer_id, invitee_id, created_at) VALUES (?, ?, ?)",
            (referrer_id, invitee_id, int(time.time())),
        )
        return cur.rowcount > 0


def check_trial(referrer_id: int) -> None:
    """Legacy no-op: HolyGram no longer has trials or paid subscriptions."""
    return


# --- поиск музыки -------------------------------------------------------------

MUSIC_SEARCH_CACHE_TTL_SEC = 300
MUSIC_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
MUSIC_RESULT_CACHE: dict[str, tuple[float, int, str, list[dict]]] = {}


def _music_cache_cleanup() -> None:
    now = time.time()
    for key, value in list(MUSIC_SEARCH_CACHE.items()):
        if value[0] < now:
            MUSIC_SEARCH_CACHE.pop(key, None)
    for key, value in list(MUSIC_RESULT_CACHE.items()):
        if value[0] < now:
            MUSIC_RESULT_CACHE.pop(key, None)


MUSIC_RESULTS_PAGE_SIZE = 6
MUSIC_SEARCH_LIMIT = 30


def search_music_tracks(query: str, limit: int = MUSIC_SEARCH_LIMIT) -> list[dict]:
    query = re.sub(r"\s+", " ", str(query or "").strip())[:120]
    if len(query) < 2:
        return []

    _music_cache_cleanup()
    limit = max(1, min(int(limit), 50))
    cache_key = f"{query.casefold()}:{limit}"
    cached = MUSIC_SEARCH_CACHE.get(cache_key)
    if cached and cached[0] >= time.time():
        return [dict(item) for item in cached[1]]

    params = (
        "term=" + quote(query, safe="")
        + "&country=US&media=music&entity=song"
        + f"&limit={limit}&explicit=No"
    )
    request = Request(
        "https://itunes.apple.com/search?" + params,
        headers={"User-Agent": "HolyGram-MusicSearch/1.0"},
    )
    try:
        with urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        log(f"Music search failed for {query!r}: {type(exc).__name__}: {exc}")
        return []

    rows = payload.get("results") if isinstance(payload, dict) else []
    results: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        track = str(row.get("trackName") or "").strip()
        artist = str(row.get("artistName") or "").strip()
        if not track or not artist:
            continue
        preview_url = str(row.get("previewUrl") or "").strip()
        if not preview_url.startswith(("https://", "http://")):
            continue
        results.append(
            {
                "track": track,
                "artist": artist,
                "album": str(row.get("collectionName") or "").strip(),
                "preview_url": preview_url,
                "artwork": str(row.get("artworkUrl100") or "").strip(),
                "duration_ms": int(row.get("trackTimeMillis") or 0),
                "genre": str(row.get("primaryGenreName") or "").strip(),
                "release_date": str(row.get("releaseDate") or "").strip(),
            }
        )
        if len(results) >= limit:
            break

    MUSIC_SEARCH_CACHE[cache_key] = (time.time() + MUSIC_SEARCH_CACHE_TTL_SEC, results)
    return [dict(item) for item in results]


def _music_duration(ms: int) -> str:
    seconds = max(0, int(ms or 0) // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "—"


def _music_token(user_id: int, query: str, results: list[dict]) -> str:
    token = uuid.uuid4().hex[:10]
    MUSIC_RESULT_CACHE[token] = (
        time.time() + MUSIC_SEARCH_CACHE_TTL_SEC,
        int(user_id),
        query,
        [dict(item) for item in results],
    )
    return token


def _music_cached(user_id: int, token: str) -> tuple[str, list[dict]] | None:
    _music_cache_cleanup()
    cached = MUSIC_RESULT_CACHE.get(token)
    if not cached or int(cached[1]) != int(user_id):
        return None
    return str(cached[2]), [dict(item) for item in cached[3]]


def remember_music_search(user_id: int, query: str) -> None:
    query = re.sub(r"\s+", " ", str(query or "").strip())[:120]
    if not query:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO music_search_history (user_id,query,created_at) VALUES (?,?,?)",
            (user_id, query, int(time.time())),
        )
        conn.execute(
            """
            DELETE FROM music_search_history
            WHERE user_id=? AND id NOT IN (
                SELECT id FROM music_search_history
                WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 30
            )
            """,
            (user_id, user_id),
        )


def recent_music_searches(user_id: int, limit: int = 8) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT query FROM music_search_history WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 30))),
        ).fetchall()
    seen: set[str] = set()
    result: list[str] = []
    for (query,) in rows:
        value = str(query or "").strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def page_music_results(
    user_id: int,
    query: str,
    results: list[dict],
    token: str | None = None,
    page_number: int = 0,
) -> tuple[str, dict]:
    token = token or _music_token(user_id, query, results)
    if not results:
        return (
            "🎵 <b>Ничего не нашёл</b>\n\n"
            f"Запрос: <code>{html_text(query)}</code>\n\n"
            "Попробуй написать название немного иначе или добавь исполнителя.",
            kb([
                [btn("🔎 Новый поиск", "music:prompt", style="primary")],
                [btn("Главное меню", "home", emoji="home")],
            ]),
        )

    page_count = max(1, (len(results) + MUSIC_RESULTS_PAGE_SIZE - 1) // MUSIC_RESULTS_PAGE_SIZE)
    page_number = min(max(0, int(page_number)), page_count - 1)
    start_index = page_number * MUSIC_RESULTS_PAGE_SIZE
    page_items = results[start_index : start_index + MUSIC_RESULTS_PAGE_SIZE]

    rows: list[list[dict]] = []
    for local_index, item in enumerate(page_items, start=1):
        global_index = start_index + local_index - 1
        title = f"{item['artist']} — {item['track']}"
        if item.get("album"):
            title += f" · {item['album']}"
        button_label = title if len(title) <= 52 else title[:49] + "…"
        rows.append([
            btn(
                f"🎧 {button_label}",
                f"music:send:{token}:{global_index}:{page_number}",
                emoji="view",
            )
        ])

    navigation: list[dict] = []
    if page_number > 0:
        navigation.append(btn("← Назад", f"music:results:{token}:{page_number - 1}", emoji="back"))
    if page_number + 1 < page_count:
        navigation.append(btn("Дальше →", f"music:results:{token}:{page_number + 1}", emoji="view"))
    if navigation:
        rows.append(navigation)

    rows.extend([
        [btn("🔎 Новый поиск", "music:prompt", style="primary")],
        [btn("Главное меню", "home", emoji="home")],
    ])

    text = (
        "🎵 <b>Результаты поиска</b>\n"
        f"Запрос: <code>{html_text(query)}</code>\n"
        f"Найдено: <b>{len(results)}</b> · страница <b>{page_number + 1}/{page_count}</b>\n\n"
        "Выбери трек кнопкой ниже — бот отправит аудио прямо в чат."
    )
    return text, kb(rows)


def _safe_music_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', " ", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:110] or "track")



def send_music_preview_audio(
    user_id: int,
    chat_id: int,
    token: str,
    index: int,
    page_number: int = 0,
) -> tuple[bool, str]:
    cached = _music_cached(user_id, token)
    if not cached:
        return False, "Поиск устарел. Выполни новый поиск."

    _query, results = cached
    if index < 0 or index >= len(results):
        return False, "Трек не найден."

    item = results[index]
    preview_url = str(item.get("preview_url") or "").strip()
    if not preview_url.startswith(("https://", "http://")):
        return False, "Для этого трека аудио недоступно."

    title = str(item.get("track") or "Трек").strip()[:64]
    performer = str(item.get("artist") or "Исполнитель").strip()[:64]
    caption = "🎵 Найдено через HolyGram"

    suffix = Path(preview_url.split("?", 1)[0]).suffix.lower()
    if suffix not in {".m4a", ".mp3", ".aac"}:
        suffix = ".m4a"

    temp_dir = DATA_DIR / "music_tmp" / uuid.uuid4().hex
    temp_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_music_filename(f"{performer} - {title}") + suffix
    temp_path = temp_dir / filename

    try:
        request = Request(preview_url, headers={"User-Agent": "HolyGram-MusicSearch/1.0"})
        total = 0
        with urlopen(request, timeout=20) as response, temp_path.open("wb") as output:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > 15 * 1024 * 1024:
                    raise ValueError("preview too large")
                output.write(chunk)

        result_markup = kb([[
            btn("← К результатам", f"music:results:{token}:{max(0, page_number)}", emoji="back"),
            btn("🔎 Новый поиск", "music:prompt", emoji="refresh"),
        ]])

        telegram_multipart_call(
            "sendAudio",
            {
                "chat_id": chat_id,
                "title": title,
                "performer": performer,
                "caption": caption,
                "reply_markup": json.dumps(result_markup, ensure_ascii=False),
            },
            {"audio": temp_path},
            timeout=60,
        )
        return True, "Аудио отправлено"
    except Exception as exc:
        log(f"Music preview upload failed for {user_id}: {type(exc).__name__}: {exc}")
        return False, "Не удалось отправить аудио этого трека. Попробуй другой результат."
    finally:
        try:
            temp_path.unlink(missing_ok=True)
            temp_dir.rmdir()
        except Exception:
            pass



def send_music_search_results(user_id: int, chat_id: int, query: str) -> None:
    query = re.sub(r"\s+", " ", str(query or "").strip())[:120]
    if len(query) < 2:
        send_message(chat_id, "Напиши название песни или исполнителя.")
        return
    send_message(chat_id, f"🔎 Ищу: <b>{html_text(query)}</b>", parse_mode="HTML")
    remember_music_search(user_id, query)
    results = search_music_tracks(query, MUSIC_SEARCH_LIMIT)
    token = _music_token(user_id, query, results)
    text, markup = page_music_results(user_id, query, results, token, 0)
    send_menu_page(user_id, chat_id, text, markup, use_photo=False)



# --- HolyGram инструменты и быстрые действия ----------------------------------

def _quick_cleanup() -> None:
    now = time.time()
    for token, item in list(QUICK_ACTION_CACHE.items()):
        if item[0] < now:
            QUICK_ACTION_CACHE.pop(token, None)


def _quick_put(user_id: int, payload: dict) -> str:
    _quick_cleanup()
    token = uuid.uuid4().hex[:10]
    QUICK_ACTION_CACHE[token] = (time.time() + QUICK_ACTION_TTL_SEC, int(user_id), dict(payload))
    return token


def _quick_get(user_id: int, token: str) -> dict | None:
    _quick_cleanup()
    item = QUICK_ACTION_CACHE.get(str(token))
    if not item or int(item[1]) != int(user_id):
        return None
    return dict(item[2])


def _tool_temp_dir(prefix: str, user_id: int) -> Path:
    root = DATA_DIR / "tools_tmp"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe_part(prefix)}_{user_id}_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cleanup_tool_dir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def download_telegram_tool_file(file_id: str, target_dir: Path, preferred_name: str = "file") -> Path:
    info = telegram_call("getFile", {"file_id": file_id}, timeout=30)
    file_path = str((info or {}).get("file_path") or "")
    if not file_path:
        raise RuntimeError("Telegram не вернул путь к файлу")
    suffix = Path(file_path).suffix or Path(preferred_name).suffix or ".bin"
    stem = Path(preferred_name).stem or "file"
    target = target_dir / (safe_part(stem, "file") + suffix)
    request = Request(FILE_API_URL + quote(file_path, safe="/"))
    total = 0
    with urlopen(request, timeout=120) as response, target.open("wb") as output:
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 50 * 1024 * 1024:
                raise ValueError("Файл слишком большой для инструмента")
            output.write(chunk)
    return target


def _ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg"))


def _stt_available() -> bool:
    return bool(
        (os.getenv("HOLYGRAM_STT_API_URL", "").strip() and os.getenv("HOLYGRAM_STT_API_KEY", "").strip())
        or os.getenv("GROQ_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )


def _tool_capabilities() -> dict[str, bool]:
    return {
        "ffmpeg": _ffmpeg_available(),
        "stt": _stt_available(),
    }


def _run_ffmpeg(args: list[str], timeout: int = 180) -> tuple[bool, str]:
    binary = shutil.which("ffmpeg")
    if not binary:
        return False, "На сервере не найден ffmpeg."
    proc = subprocess.run(
        [binary, "-y", "-hide_banner", "-loglevel", "error", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="ignore").strip()
        return False, (error[-700:] if error else "ffmpeg завершился с ошибкой")
    return True, ""


def _send_tool_document(chat_id: int, path: Path, caption: str = "") -> None:
    fields: dict[str, object] = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    telegram_multipart_call("sendDocument", fields, {"document": path}, timeout=120)


def _send_tool_audio(chat_id: int, path: Path, title: str = "", performer: str = "HolyGram", caption: str = "") -> None:
    fields: dict[str, object] = {"chat_id": chat_id}
    if title:
        fields["title"] = title[:64]
    if performer:
        fields["performer"] = performer[:64]
    if caption:
        fields["caption"] = caption
    telegram_multipart_call("sendAudio", fields, {"audio": path}, timeout=120)


def _send_tool_voice(chat_id: int, path: Path, caption: str = "") -> None:
    fields: dict[str, object] = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    telegram_multipart_call("sendVoice", fields, {"voice": path}, timeout=120)


def _send_tool_video(chat_id: int, path: Path, caption: str = "") -> None:
    fields: dict[str, object] = {"chat_id": chat_id, "supports_streaming": True}
    if caption:
        fields["caption"] = caption
    telegram_multipart_call("sendVideo", fields, {"video": path}, timeout=180)


def _send_tool_video_note(chat_id: int, path: Path) -> None:
    telegram_multipart_call("sendVideoNote", {"chat_id": chat_id}, {"video_note": path}, timeout=180)


def _stt_multipart(url: str, api_key: str, model: str, path: Path) -> str | None:
    boundary = f"----HolyGramSTT{uuid.uuid4().hex}"
    body = bytearray()

    def add(value: str) -> None:
        body.extend(value.encode("utf-8"))

    add(f"--{boundary}\r\n")
    add('Content-Disposition: form-data; name="model"\r\n\r\n')
    add(model)
    add("\r\n")

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    add(f"--{boundary}\r\n")
    add(f'Content-Disposition: form-data; name="file"; filename="{path.name.replace(chr(34), "_")}"\r\n')
    add(f"Content-Type: {mime}\r\n\r\n")
    body.extend(path.read_bytes())
    add("\r\n")
    add(f"--{boundary}--\r\n")

    request = Request(
        url,
        data=bytes(body),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urlopen(request, timeout=120) as response:
        data = json.loads(response.read().decode("utf-8"))
    text_value = str((data or {}).get("text") or "").strip()
    return text_value or None


def transcribe_voice_file(path: Path) -> tuple[bool, str]:
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    custom_url = os.getenv("HOLYGRAM_STT_API_URL", "").strip()
    custom_key = os.getenv("HOLYGRAM_STT_API_KEY", "").strip()
    custom_model = os.getenv("HOLYGRAM_STT_MODEL", "whisper-large-v3-turbo").strip()

    providers: list[tuple[str, str, str]] = []
    if custom_url and custom_key:
        providers.append((custom_url, custom_key, custom_model))
    if groq_key:
        providers.append(("https://api.groq.com/openai/v1/audio/transcriptions", groq_key, "whisper-large-v3-turbo"))
    if openai_key:
        providers.append(("https://api.openai.com/v1/audio/transcriptions", openai_key, "gpt-4o-mini-transcribe"))

    if not providers:
        return False, (
            "Расшифровка подготовлена, но на сервере не настроен STT. "
            "Добавь GROQ_API_KEY, OPENAI_API_KEY или HOLYGRAM_STT_API_URL/HOLYGRAM_STT_API_KEY."
        )

    last_error = ""
    for url, key, model in providers:
        try:
            transcript = _stt_multipart(url, key, model, path)
            if transcript:
                return True, transcript
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log(f"STT provider failed: {last_error}")
    return False, "Не удалось расшифровать голосовое." + (f" {last_error[:180]}" if last_error else "")


def summarize_text_local(text: str, max_sentences: int = 3) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return ""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if len(sentences) <= max_sentences:
        return cleaned
    chosen = sentences[:max_sentences]
    return " ".join(chosen)


def translate_text_free(text: str, target: str | None = None) -> tuple[bool, str]:
    value = str(text or "").strip()
    if not value:
        return False, "Пустой текст."
    if target is None:
        target = "en" if re.search(r"[А-Яа-яЁё]", value) else "ru"
    params = (
        "client=gtx&sl=auto&tl=" + quote(target, safe="")
        + "&dt=t&q=" + quote(value[:3500], safe="")
    )
    try:
        with urlopen(Request("https://translate.googleapis.com/translate_a/single?" + params, headers={"User-Agent": "HolyGram/1.0"}), timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        translated = "".join(str(chunk[0]) for chunk in (data[0] or []) if chunk and chunk[0]).strip()
        return (True, translated) if translated else (False, "Перевод не получен.")
    except Exception as exc:
        log(f"Translate failed: {type(exc).__name__}: {exc}")
        return False, "Не удалось выполнить перевод."


TRANSLIT_MAP = str.maketrans({
    "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"yo","ж":"zh","з":"z","и":"i","й":"y",
    "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
    "х":"kh","ц":"ts","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    "А":"A","Б":"B","В":"V","Г":"G","Д":"D","Е":"E","Ё":"Yo","Ж":"Zh","З":"Z","И":"I","Й":"Y",
    "К":"K","Л":"L","М":"M","Н":"N","О":"O","П":"P","Р":"R","С":"S","Т":"T","У":"U","Ф":"F",
    "Х":"Kh","Ц":"Ts","Ч":"Ch","Ш":"Sh","Щ":"Sch","Ъ":"","Ы":"Y","Ь":"","Э":"E","Ю":"Yu","Я":"Ya",
})


def text_tool_transform(mode: str, text: str) -> tuple[bool, str]:
    value = str(text or "").strip()
    if not value:
        return False, "Отправь непустой текст."

    if mode == "fix":
        result = re.sub(r"[ \t]+", " ", value)
        result = re.sub(r"\s+([,.;:!?])", r"\1", result)
        result = re.sub(r"([,.;:!?])(?=[^\s\n])", r"\1 ", result)
        result = re.sub(r"\s*\n\s*", "\n", result)
        result = result[:1].upper() + result[1:] if result else result
        return True, result
    if mode == "short":
        return True, summarize_text_local(value, 2)
    if mode == "official":
        replacements = {
            r"(?iu)\bпривет\b": "Здравствуйте",
            r"(?iu)\bпока\b": "Всего доброго",
            r"(?iu)\bхочу\b": "хотел(а) бы",
            r"(?iu)\bнадо\b": "необходимо",
            r"(?iu)\bскинь\b": "отправьте",
            r"(?iu)\bго\b": "предлагаю",
        }
        result = value
        for pattern, replacement in replacements.items():
            result = re.sub(pattern, replacement, result)
        if result and result[-1] not in ".!?":
            result += "."
        return True, result
    if mode == "simple":
        replacements = {
            "осуществить": "сделать", "произвести": "сделать", "необходимо": "нужно",
            "вследствие": "из-за", "посредством": "через", "предоставить": "дать",
            "использовать": "применять",
        }
        result = value
        for source, replacement in replacements.items():
            result = re.sub(r"(?iu)(?<!\w)" + re.escape(source) + r"(?!\w)", replacement, result)
        return True, result
    if mode == "funny":
        tails = (" 😄", " — ну ты понял 😎", " ✨", " ахах")
        return True, value + tails[sum(ord(ch) for ch in value) % len(tails)]
    if mode == "translate":
        return translate_text_free(value)
    if mode == "translit":
        return True, value.translate(TRANSLIT_MAP)
    if mode == "upper":
        return True, value.upper()
    if mode == "lower":
        return True, value.lower()
    if mode == "title":
        return True, value.title()
    return False, "Неизвестный текстовый инструмент."


URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>()]+")
def extract_first_url(text: str) -> str | None:
    match = URL_RE.search(str(text or ""))
    return match.group(0).rstrip(".,!?;:") if match else None


def safe_public_url(url: str) -> tuple[bool, str]:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "Нужна обычная http/https ссылка."
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                return False, "Локальные и приватные адреса не поддерживаются."
    except Exception:
        return False, "Не удалось проверить адрес ссылки."
    return True, value


def shorten_url(url: str) -> tuple[bool, str]:
    ok, value = safe_public_url(url)
    if not ok:
        return False, value
    try:
        endpoint = "https://is.gd/create.php?format=simple&url=" + quote(value, safe="")
        with urlopen(Request(endpoint, headers={"User-Agent": "HolyGram/1.0"}), timeout=15) as response:
            short = response.read().decode("utf-8", errors="ignore").strip()
        if short.startswith("http"):
            return True, short
    except Exception as exc:
        log(f"URL shorten failed: {type(exc).__name__}: {exc}")
    return False, "Не удалось сократить ссылку."


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_url(url: str) -> tuple[bool, str]:
    current = str(url or "").strip()
    opener = build_opener(_NoRedirectHandler())
    try:
        for _ in range(6):
            ok, checked = safe_public_url(current)
            if not ok:
                return False, checked
            request = Request(checked, headers={"User-Agent": "HolyGram/1.0"}, method="HEAD")
            try:
                response = opener.open(request, timeout=12)
                final = str(response.geturl() or checked)
                response.close()
                final_ok, final_checked = safe_public_url(final)
                return (True, final_checked) if final_ok else (False, final_checked)
            except HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = str(exc.headers.get("Location") or "").strip()
                    if not location:
                        return False, "Редирект без адреса назначения."
                    if location.startswith("/"):
                        parsed = urlparse(checked)
                        location = f"{parsed.scheme}://{parsed.netloc}{location}"
                    current = location
                    continue
                if exc.code in {405, 501}:
                    request = Request(checked, headers={"User-Agent": "HolyGram/1.0"})
                    response = opener.open(request, timeout=12)
                    final = str(response.geturl() or checked)
                    response.close()
                    final_ok, final_checked = safe_public_url(final)
                    return (True, final_checked) if final_ok else (False, final_checked)
                raise
        return False, "Слишком много перенаправлений."
    except Exception as exc:
        log(f"URL resolve failed: {type(exc).__name__}: {exc}")
        return False, "Не удалось открыть ссылку для проверки."


def send_qr_for_url(chat_id: int, url: str) -> tuple[bool, str]:
    ok, value = safe_public_url(url)
    if not ok:
        return False, value
    qr_url = "https://api.qrserver.com/v1/create-qr-code/?size=700x700&data=" + quote(value, safe="")
    try:
        telegram_call(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": qr_url,
                "caption": "🔗 QR-код · HolyGram",
                "reply_markup": kb([[btn("Скопировать ссылку", copy=value, emoji="view")]]),
            },
            timeout=45,
        )
        return True, "QR-код отправлен"
    except TelegramApiError as exc:
        log(f"QR send failed: {exc}")
        return False, "Не удалось создать QR-код."


def quick_actions_for_message(user_id: int, chat_id: int, message: dict) -> bool:
    media = message_media(message)
    if media and media.get("file_id"):
        media_type = str(media.get("type") or "")
        payload = {"kind": "media", "media": media}
        token = _quick_put(user_id, payload)
        if media_type in {"voice", "audio"}:
            caps = _tool_capabilities()
            rows: list[list[dict]] = []
            if caps["stt"]:
                rows.append([btn("📝 В текст", f"qa:v:text:{token}"), btn("🧾 Кратко", f"qa:v:sum:{token}")])
                rows.append([btn("🌐 Перевести", f"qa:v:tr:{token}")])
            if caps["ffmpeg"]:
                rows.append([btn("🎚 Скорость", f"qa:v:speeds:{token}"), btn("🔊 Нормализовать", f"qa:v:norm:{token}")])
                rows.append([btn("🎵 MP3", f"qa:v:mp3:{token}")])
            rows.append([btn("⬇️ Скачать", f"qa:v:download:{token}")])
            send_message(chat_id, "🎙 <b>Голосовое получено.</b> Что сделать?", parse_mode="HTML", reply_markup=kb(rows))
            return True
        if media_type == "video_note":
            rows: list[list[dict]] = []
            if _ffmpeg_available():
                rows.append([btn("🎬 Обычное видео", f"qa:c:video:{token}"), btn("🎵 Вытащить звук", f"qa:c:audio:{token}")])
            rows.append([btn("⬇️ Скачать", f"qa:c:download:{token}")])
            send_message(chat_id, "⭕ <b>Кружок получен.</b> Что сделать?", parse_mode="HTML", reply_markup=kb(rows))
            return True
        if media_type == "video":
            rows: list[list[dict]] = []
            if _ffmpeg_available():
                rows.append([btn("🎵 Вытащить звук", f"qa:vid:audio:{token}"), btn("🗜 Сжать", f"qa:vid:compress:{token}")])
                rows.append([btn("⭕ В кружок", f"qa:vid:circle:{token}")])
            rows.append([btn("⬇️ Скачать", f"qa:vid:download:{token}")])
            send_message(chat_id, "🎬 <b>Видео получено.</b> Что сделать?", parse_mode="HTML", reply_markup=kb(rows))
            return True

    url = extract_first_url(str(message.get("text") or ""))
    if url:
        token = _quick_put(user_id, {"kind": "link", "url": url})
        rows = [
            [btn("▦ QR-код", f"qa:l:qr:{token}"), btn("🔗 Сократить", f"qa:l:short:{token}")],
            [btn("🛡 Проверить", f"qa:l:check:{token}"), btn("✨ Оформить", f"qa:l:pretty:{token}")],
        ]
        send_message(chat_id, "🔥 <b>Быстрые действия со ссылкой</b>", parse_mode="HTML", reply_markup=kb(rows))
        return True
    return False


def process_media_quick_action(user_id: int, chat_id: int, action: str, token: str, extra: str = "") -> tuple[bool, str]:
    if action in {"voice_text", "voice_summary", "voice_translate"} and not _stt_available():
        return False, "Эта функция отключена."
    if action in {"voice_mp3", "voice_norm", "voice_speed", "circle_audio", "circle_video", "video_audio", "video_compress", "video_circle"} and not _ffmpeg_available():
        return False, "Эта функция отключена."
    payload = _quick_get(user_id, token)
    if not payload or payload.get("kind") != "media":
        return False, "Действие устарело. Отправь файл ещё раз."
    media = payload.get("media") or {}
    file_id = str(media.get("file_id") or "")
    if not file_id:
        return False, "Файл недоступен."

    work = _tool_temp_dir("media", user_id)
    try:
        preferred = str(media.get("file_name") or f"{media.get('type') or 'media'}")
        source = download_telegram_tool_file(file_id, work, preferred)

        if action in {"voice_download", "circle_download", "video_download"}:
            _send_tool_document(chat_id, source, "⬇️ Файл · HolyGram")
            return True, "Файл отправлен"

        if action in {"voice_text", "voice_summary", "voice_translate"}:
            ok, transcript = transcribe_voice_file(source)
            if not ok:
                return False, transcript
            if action == "voice_text":
                send_message(chat_id, "📝 <b>Расшифровка:</b>\n\n" + html_quote(transcript), parse_mode="HTML")
                return True, "Готово"
            if action == "voice_summary":
                summary = summarize_text_local(transcript, 3)
                send_message(chat_id, "🧾 <b>Кратко:</b>\n\n" + html_quote(summary), parse_mode="HTML")
                return True, "Готово"
            tr_ok, translated = translate_text_free(transcript)
            if not tr_ok:
                return False, translated
            send_message(chat_id, "🌐 <b>Перевод:</b>\n\n" + html_quote(translated), parse_mode="HTML")
            return True, "Готово"

        if action == "voice_mp3":
            output = work / "HolyGram_voice.mp3"
            ok, error = _run_ffmpeg(["-i", str(source), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(output)])
            if not ok:
                return False, error
            _send_tool_audio(chat_id, output, "Voice", "HolyGram", "🎵 Конвертировано через HolyGram")
            return True, "MP3 отправлен"

        if action == "voice_norm":
            output = work / "HolyGram_normalized.ogg"
            ok, error = _run_ffmpeg(["-i", str(source), "-af", "loudnorm", "-c:a", "libopus", "-b:a", "64k", str(output)])
            if not ok:
                return False, error
            _send_tool_voice(chat_id, output, "🔊 Громкость нормализована · HolyGram")
            return True, "Готово"

        if action == "voice_speed":
            try:
                speed = float(extra)
            except ValueError:
                return False, "Неверная скорость."
            if speed not in {0.75, 1.25, 1.5, 2.0}:
                return False, "Недоступная скорость."
            output = work / f"HolyGram_{str(speed).replace('.', '_')}x.ogg"
            ok, error = _run_ffmpeg(["-i", str(source), "-filter:a", f"atempo={speed}", "-c:a", "libopus", "-b:a", "64k", str(output)])
            if not ok:
                return False, error
            _send_tool_voice(chat_id, output, f"🎚 Скорость {speed:g}× · HolyGram")
            return True, "Готово"

        if action in {"circle_audio", "video_audio"}:
            output = work / "HolyGram_audio.mp3"
            ok, error = _run_ffmpeg(["-i", str(source), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(output)])
            if not ok:
                return False, error
            _send_tool_audio(chat_id, output, "Extracted audio", "HolyGram", "🎵 Звук извлечён через HolyGram")
            return True, "Аудио отправлено"

        if action == "circle_video":
            output = work / "HolyGram_circle.mp4"
            ok, error = _run_ffmpeg(["-i", str(source), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-movflags", "+faststart", str(output)])
            if not ok:
                return False, error
            _send_tool_video(chat_id, output, "⭕ Кружок как обычное видео · HolyGram")
            return True, "Видео отправлено"

        if action == "video_compress":
            output = work / "HolyGram_compressed.mp4"
            ok, error = _run_ffmpeg([
                "-i", str(source),
                "-vf", "scale='trunc(min(1280,iw)/2)*2':-2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "29",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
            ])
            if not ok:
                return False, error
            _send_tool_video(chat_id, output, "🗜 Сжато через HolyGram")
            return True, "Сжатое видео отправлено"

        if action == "video_circle":
            output = work / "HolyGram_video_note.mp4"
            ok, error = _run_ffmpeg([
                "-i", str(source),
                "-t", "60",
                "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=640:640",
                "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0",
                "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "24",
                "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(output),
            ])
            if not ok:
                return False, error
            _send_tool_video_note(chat_id, output)
            return True, "Кружок отправлен"
        return False, "Неизвестное действие."
    except Exception as exc:
        log(f"Tool media action failed {action}: {type(exc).__name__}: {exc}")
        return False, "Не удалось обработать файл."
    finally:
        _cleanup_tool_dir(work)


def process_link_quick_action(user_id: int, chat_id: int, action: str, token: str) -> tuple[bool, str]:
    payload = _quick_get(user_id, token)
    if not payload or payload.get("kind") != "link":
        return False, "Действие устарело. Отправь ссылку ещё раз."
    url = str(payload.get("url") or "")
    if action == "qr":
        return send_qr_for_url(chat_id, url)
    if action == "short":
        ok, result = shorten_url(url)
        if ok:
            send_message(chat_id, f"🔗 <b>Короткая ссылка:</b>\n<code>{html_text(result)}</code>", parse_mode="HTML", reply_markup=kb([[btn("Скопировать", copy=result, emoji="view")]]))
        return ok, result
    if action == "check":
        ok, result = resolve_url(url)
        if ok:
            changed = result != url
            text_value = (
                f"🛡 <b>Проверка ссылки</b>\n\n"
                f"Исходная:\n<code>{html_text(url)}</code>\n\n"
                f"{'После переходов:' if changed else 'Адрес:'}\n<code>{html_text(result)}</code>"
            )
            send_message(chat_id, text_value, parse_mode="HTML")
        return ok, result
    if action == "pretty":
        ok, checked = safe_public_url(url)
        if not ok:
            return False, checked
        send_message(
            chat_id,
            f"🔗 <b>Ссылка</b>\n\n<code>{html_text(url)}</code>",
            parse_mode="HTML",
            reply_markup=kb([[btn("Открыть", url=url, emoji="view"), btn("Скопировать", copy=url, emoji="view")]]),
        )
        return True, "Готово"
    return False, "Неизвестное действие."


# --- страницы меню ------------------------------------------------------------

def bottom_navigation() -> list[list[dict]]:
    """Compact navigation for user-facing submenus."""
    return [[btn("Назад", "home", emoji="back")]]

def page_home(user_id: int) -> tuple[str, dict]:
    rows = [
        [
            btn("🎵 Музыка", "hub:music", style="primary"),
            btn("🧰 Инструменты", "hub:tools"),
        ],
        [
            btn("📂 Моё", "hub:mine"),
            btn("🎲 Развлечения", "hub:fun"),
        ],
        [btn("⚙️ Настройки", "hub:settings")],
    ]
    if is_admin_user(user_id):
        rows.append([btn("🛡 Админ-панель", "panel", emoji="admin", style="success")])

    text = (
        "✨ <b>HolyGram</b>\n\n"
        "Одна экосистема внутри Telegram: музыка, полезные инструменты, "
        "личные функции, развлечения и настройки.\n\n"
        "Выбери раздел:"
    )
    return text, kb(rows)


def page_hub_music(user_id: int) -> tuple[str, dict]:
    history = recent_music_searches(user_id, 5)
    history_text = (
        "\n".join(f"• {html_text(query)}" for query in history)
        if history else "Пока ничего не искал."
    )
    text = (
        "🎵 <b>Музыка</b>\n\n"
        "Ищи треки по названию или исполнителю. HolyGram покажет результаты страницами "
        "и отправит выбранное аудио прямо в Telegram.\n\n"
        f"<b>Недавние запросы:</b>\n{history_text}"
    )
    rows = [
        [btn("🔎 Найти музыку", "music:prompt", style="primary")],
        [btn("Как искать", "music:help", emoji="support")],
        [btn("Назад", "home", emoji="back")],
    ]
    return text, kb(rows)


def page_hub_tools(user_id: int) -> tuple[str, dict]:
    text = (
        "🧰 <b>Инструменты HolyGram</b>\n\n"
        "Отправляй контент прямо боту — HolyGram сам определит тип и покажет быстрые действия.\n\n"
        "Выбери раздел:"
    )
    rows = [
        [
            btn("🎙 Голосовые", "tools:voice"),
            btn("⭕ Кружки", "tools:circles"),
        ],
        [
            btn("📝 Текст", "tools:text"),
            btn("🔗 Ссылки", "tools:links"),
        ],
        [btn("🔥 Быстрые действия", "tools:quick", style="primary")],
        [btn("💬 Telegram Business", "hub:settings", emoji="view")],
        [btn("Назад", "home", emoji="back")],
    ]
    return text, kb(rows)


def page_tools_voice() -> tuple[str, dict]:
    caps = _tool_capabilities()
    available = ["• скачать исходный файл"]
    if caps["stt"]:
        available[:0] = ["• расшифровка в текст", "• краткое содержание", "• перевод расшифровки"]
    if caps["ffmpeg"]:
        available[:0] = ["• скорость 0.75× / 1.25× / 1.5× / 2×", "• нормализация громкости", "• MP3"]
    text = (
        "🎙 <b>Голосовые</b>\n\n"
        "Отправь голосовое — HolyGram покажет только те действия, которые реально доступны на сервере.\n\n"
        + "\n".join(available)
    )
    return text, kb([[btn("Назад в инструменты", "hub:tools", emoji="back")]])



def page_tools_circles() -> tuple[str, dict]:
    if _ffmpeg_available():
        body = (
            "Отправь кружок — можно скачать его обычным видео или вытащить звук.\n\n"
            "Отправь обычное видео — можно вытащить звук, сжать или превратить в кружок."
        )
    else:
        body = (
            "На сервере сейчас недоступна обработка видео, поэтому HolyGram не показывает неработающие кнопки.\n\n"
            "Кружки и видео можно скачать исходным файлом."
        )
    return (
        "⭕ <b>Кружки и видео</b>\n\n" + body,
        kb([[btn("Назад в инструменты", "hub:tools", emoji="back")]]),
    )



def page_tools_text() -> tuple[str, dict]:
    text = (
        "📝 <b>Текстовые инструменты</b>\n\n"
        "Выбери действие, затем отправь текст."
    )
    rows = [
        [btn("✅ Исправить", "txt:fix"), btn("✂️ Сократить", "txt:short")],
        [btn("👔 Официальнее", "txt:official"), btn("💡 Проще", "txt:simple")],
        [btn("😄 Смешнее", "txt:funny"), btn("🌐 Перевести", "txt:translate")],
        [btn("🔤 Транслит", "txt:translit"), btn("AA ВЕРХНИЙ", "txt:upper")],
        [btn("aa нижний", "txt:lower"), btn("Aa Заголовок", "txt:title")],
        [btn("Назад в инструменты", "hub:tools", emoji="back")],
    ]
    return text, kb(rows)


def page_tools_links() -> tuple[str, dict]:
    text = (
        "🔗 <b>Инструменты для ссылок</b>\n\n"
        "Выбери действие и отправь ссылку. Если просто скинуть ссылку в HolyGram, "
        "быстрые кнопки появятся автоматически."
    )
    rows = [
        [btn("▦ QR-код", "link:prompt:qr"), btn("🔗 Сократить", "link:prompt:short")],
        [btn("🛡 Проверить переход", "link:prompt:check"), btn("✨ Оформить", "link:prompt:pretty")],
        [btn("Назад в инструменты", "hub:tools", emoji="back")],
    ]
    return text, kb(rows)


def page_tools_quick() -> tuple[str, dict]:
    caps = _tool_capabilities()
    lines = [
        "🔗 Ссылка → QR / Сократить / Проверить / Оформить",
        "🎙 Голосовое → Скачать",
        "⭕ Кружок → Скачать",
        "🎬 Видео → Скачать",
    ]
    if caps["stt"]:
        lines[1] = "🎙 Голосовое → В текст / Кратко / Перевести / Скачать"
    if caps["ffmpeg"]:
        lines[1] = lines[1].replace(" / Скачать", " / Скорость / MP3 / Скачать")
        lines[2] = "⭕ Кружок → Видео / Звук / Скачать"
        lines[3] = "🎬 Видео → Звук / Сжать / В кружок / Скачать"
    return (
        "🔥 <b>Быстрые действия</b>\n\n"
        "HolyGram автоматически показывает только рабочие действия — кнопок, которым нужна ненастроенная интеграция, больше нет.\n\n"
        + "\n".join(lines),
        kb([[btn("Назад в инструменты", "hub:tools", emoji="back")]]),
    )



def page_hub_mine(user_id: int) -> tuple[str, dict]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT first_name,last_name,username FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    first, last, username = row or ("", "", "")
    name = " ".join(part for part in (str(first or ""), str(last or "")) if part).strip() or "Пользователь"
    mention = f"@{str(username).lstrip('@')}" if username else "без username"
    history_count = len(recent_music_searches(user_id, 30))
    text = (
        "📂 <b>Моё</b>\n\n"
        f"👤 <b>{html_text(name)}</b> · {html_text(mention)}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🎵 Запросов в истории: <b>{history_count}</b>\n"
        f"👥 Приглашено друзей: <b>{ref_count(user_id)}</b>\n\n"
        "Здесь собраны твои личные разделы HolyGram."
    )
    rows = [
        [btn("🎵 История музыки", "mine:music", emoji="history")],
        [btn("👥 Реферальная система", "ref", emoji="invite")],
        [btn("💬 Поддержка", "support", emoji="support")],
        [btn("Назад", "home", emoji="back")],
    ]
    return text, kb(rows)


def page_my_music_history(user_id: int) -> tuple[str, dict]:
    history = recent_music_searches(user_id, 15)
    body = "\n".join(f"{index}. {html_text(query)}" for index, query in enumerate(history, 1)) if history else "История пока пустая."
    return (
        "🎵 <b>История музыки</b>\n\n" + body,
        kb([
            [btn("🔎 Новый поиск", "music:prompt", style="primary")],
            [btn("Назад в «Моё»", "hub:mine", emoji="back")],
        ]),
    )


def page_hub_fun(user_id: int, result: str | None = None) -> tuple[str, dict]:
    text = (
        "🎲 <b>Развлечения</b>\n\n"
        "Быстрые штуки, когда хочется просто потыкать HolyGram."
    )
    if result:
        text += f"\n\n{result}"
    rows = [
        [
            btn("🪙 Монетка", "fun:coin"),
            btn("🎲 Кубик", "fun:dice"),
        ],
        [
            btn("🔢 Число 1–100", "fun:number"),
            btn("✨ Кто ты сегодня", "fun:today"),
        ],
        [btn("Назад", "home", emoji="back")],
    ]
    return text, kb(rows)


def page_hub_settings(user_id: int) -> tuple[str, dict]:
    style = STYLE_LABELS.get(get_communication_style(user_id), "Отключён")
    autoreply = get_autoreply_settings(user_id)
    autoreply_status = "включён" if autoreply["enabled"] else "выключен"
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Стиль общения: <b>{html_text(style)}</b>\n"
        f"Автоответчик: <b>{autoreply_status}</b>\n"
        f"Автозамен: <b>{len(list_auto_replacements(user_id))}</b>\n"
        f"Команд: <b>{len(list_custom_commands(user_id))}</b>\n\n"
        "Настрой HolyGram под себя."
    )
    rows = [
        [
            btn("🎭 Стиль", "style", emoji="profile"),
            btn("🤖 Автоответчик", "autoreply", emoji="refresh"),
        ],
        [
            btn("🔁 Автозамена", "autoreplace", emoji="refresh"),
            btn("⚡ Команды", "commands", emoji="history"),
        ],
        [btn("💬 Чаты", "conns", emoji="view")],
        [btn("Как подключить", "help", emoji="support")],
        [btn("Назад", "home", emoji="back")],
    ]
    return text, kb(rows)



def page_music_help(user_id: int) -> tuple[str, dict]:
    text = (
        "🎧 <b>Как искать музыку</b>\n\n"
        "1. Отправь название трека или исполнителя.\n"
        "2. Выбери нужный трек из результатов.\n"
        "3. Бот отправит аудиофайл с нормальным названием прямо в чат.\n"
        "4. Если результатов много — листай страницы кнопками «Назад / Дальше».\n\n"
        "Чем точнее запрос, тем точнее результат."
    )
    return text, kb([
        [btn("🔎 Найти музыку", "music:prompt", style="primary")],
        [btn("Назад в музыку", "hub:music", emoji="back")],
    ])


def page_buy(user_id: int) -> tuple[str, dict]:
    text = (
        f"{pe('check')} <b>HolyGram бесплатный</b>\n\n"
        "Подписка больше не нужна. Покупки, продления, подарочные подписки, "
        "промокоды на оплату и платные тарифы отключены.\n\n"
        "Все доступные функции работают бесплатно."
    )
    return text, kb(bottom_navigation())


def page_functions(user_id: int) -> tuple[str, dict]:
    connections = list_business_connections(user_id)
    regular_chats = get_user_chats(user_id)
    enabled_count = sum(1 for row in connections if row.get("is_enabled")) + len(regular_chats)
    replacements_count = len(list_auto_replacements(user_id))
    commands_count = len(list_custom_commands(user_id))
    current_style = STYLE_LABELS.get(get_communication_style(user_id), "Отключён")
    autoreply = get_autoreply_settings(user_id)
    rows = [
        [
            btn("Подключённые чаты", "conns", emoji="view"),
            btn("Стили общения", "style", emoji="profile"),
        ],
        [
            btn("Автоответчик", "autoreply", emoji="refresh"),
            btn("Автозамена", "autoreplace", emoji="refresh"),
        ],
        [btn("Мои команды", "commands", emoji="history")],
        [btn("Назад", "home", emoji="back")],
    ]
    text = (
        "⚙️ <b>Функции бота</b>\n\n"
        f"Подключено чатов: <b>{enabled_count}</b>\n"
        f"Стиль: <b>{html_text(current_style)}</b>\n"
        f"Автоответчик: <b>{'включён' if autoreply['enabled'] else 'выключен'}</b>\n"
        f"Автозамен: <b>{replacements_count}</b>\n"
        f"Команд: <b>{commands_count}</b>\n\n"
        "Здесь собраны функции, которые меняют и автоматизируют исходящие сообщения."
    )
    return text, kb(rows)


def page_autoreply(user_id: int) -> tuple[str, dict]:
    settings = get_autoreply_settings(user_id)
    messages = list(settings["messages"])
    enabled = bool(settings["enabled"])
    has_photo = bool(settings["photo_file_id"])
    preview_lines = []
    for index, message in enumerate(messages[:3], start=1):
        compact = re.sub(r"\s+", " ", message)
        if len(compact) > 90:
            compact = compact[:87] + "…"
        preview_lines.append(f"{index}. {html_text(compact)}")
    preview = "\n".join(preview_lines) if preview_lines else "Сообщения пока не добавлены."
    status_icon = "✅" if enabled else "❌"
    rows = [
        [
            btn(
                "Выключить" if enabled else "Включить",
                "autoreply:toggle",
                emoji="warning" if enabled else "check",
                style="danger" if enabled else "success",
            )
        ],
        [
            btn("Сообщения", "autoreply:messages", emoji="refresh"),
            btn("Картинка", "autoreply:photo", emoji="view"),
        ],
    ]
    if messages or has_photo:
        cleanup = []
        if messages:
            cleanup.append(btn("Очистить сообщения", "autoreply:messages:clear", emoji="warning"))
        if has_photo:
            cleanup.append(btn("Удалить картинку", "autoreply:photo:clear", emoji="warning"))
        rows.append(cleanup)
    rows.append([btn("Назад в настройки", "hub:settings", emoji="back")])
    text = (
        f"🤖 <b>Автоответчик</b>\n\n"
        f"Статус: <b>{status_icon} {'включён' if enabled else 'выключен'}</b>\n"
        f"Вариантов ответа: <b>{len(messages)}/{MAX_AUTOREPLY_MESSAGES}</b>\n"
        f"Картинка: <b>{'добавлена' if has_photo else 'нет'}</b>\n\n"
        "Когда другой пользователь пишет в подключённый Telegram Business-чат, "
        "HolyGram автоматически отправляет один из сохранённых вариантов. "
        "Если добавлена картинка, она отправляется вместе с выбранным текстом.\n\n"
        f"<b>Пример вариантов:</b>\n{preview}"
    )
    return text, kb(rows)


def page_auto_replacements(user_id: int) -> tuple[str, dict]:
    rules = list_auto_replacements(user_id)
    rows: list[list[dict]] = []
    for rule_id, trigger, replacement in rules[:25]:
        compact_replacement = re.sub(r"\s+", " ", replacement)
        label = f"{trigger} → {compact_replacement}"
        if len(label) > 42:
            label = label[:39] + "…"
        rows.append([btn(label, f"ar:item:{rule_id}")])
    rows.extend([
        [btn("Добавить автозамену", "ar:add", emoji="add", style="success")],
        [btn("Назад в настройки", "hub:settings", emoji="back")],
    ])
    text = (
        "🔁 <b>Автозамена</b>\n\n"
        "Добавь слово или фразу и текст, на который HolyGram должен её менять. "
        "Правила личные — у каждого пользователя свой список.\n\n"
        f"Создано: <b>{len(rules)}/{MAX_USER_AUTOREPLACE_RULES}</b>"
    )
    return text, kb(rows)


def page_auto_replacement_item(user_id: int, rule_id: int) -> tuple[str, dict]:
    rule = get_auto_replacement(user_id, rule_id)
    if not rule:
        return page_auto_replacements(user_id)
    _rule_id, trigger, replacement = rule
    text = (
        "🔁 <b>Автозамена</b>\n\n"
        f"Что искать:\n<code>{html_text(trigger)}</code>\n\n"
        f"На что менять:\n<blockquote>{html_text(replacement)}</blockquote>"
    )
    rows = [
        [btn("Изменить", f"ar:edit:{rule_id}", emoji="refresh")],
        [btn("Удалить", f"ar:delete:{rule_id}", emoji="warning", style="danger")],
        [btn("Назад", "autoreplace", emoji="back")],
    ]
    return text, kb(rows)


def page_custom_commands(user_id: int) -> tuple[str, dict]:
    commands = list_custom_commands(user_id)
    rows: list[list[dict]] = []
    for command_id, trigger, messages in commands[:25]:
        rows.append([btn(f"{trigger} · {len(messages)} сообщ.", f"cmd:item:{command_id}")])
    rows.extend([
        [btn("Создать команду", "cmd:add", emoji="add", style="success")],
        [btn("Назад в настройки", "hub:settings", emoji="back")],
    ])
    text = (
        "⌨️ <b>Мои команды</b>\n\n"
        "Команды работают в подключённых Telegram Business-чатах. "
        "Например, создай <code>.привет</code>, а затем просто отправь <code>.привет</code> в нужном диалоге — "
        "HolyGram отправит настроенную последовательность сообщений.\n\n"
        f"Создано: <b>{len(commands)}/{MAX_USER_COMMANDS}</b>"
    )
    return text, kb(rows)


def page_custom_command_item(user_id: int, command_id: int) -> tuple[str, dict]:
    command = get_custom_command(user_id, command_id)
    if not command:
        return page_custom_commands(user_id)
    _command_id, trigger, messages = command
    preview = "\n".join(
        f"{index}. {html_text(message[:180])}{'…' if len(message) > 180 else ''}"
        for index, message in enumerate(messages, start=1)
    )
    text = (
        "⌨️ <b>Пользовательская команда</b>\n\n"
        f"Команда: <code>{html_text(trigger)}</code>\n"
        f"Сообщений: <b>{len(messages)}</b>\n\n"
        f"{preview or 'Сообщений нет.'}"
    )
    rows = [
        [
            btn("Изменить команду", f"cmd:name:{command_id}", emoji="refresh"),
            btn("Изменить сообщения", f"cmd:messages:{command_id}", emoji="view"),
        ],
        [btn("Удалить", f"cmd:delete:{command_id}", emoji="warning", style="danger")],
        [btn("Назад", "commands", emoji="back")],
    ]
    return text, kb(rows)


def page_communication_style(user_id: int) -> tuple[str, dict]:
    current = get_communication_style(user_id)
    current_label = STYLE_LABELS.get(current, "🚫 Отключён")
    change_notice = STYLE_CHANGE_NOTICE.pop(user_id, None)

    def style_button(key: str, label: str) -> dict:
        return btn(
            ("✓ " if current == key else "") + label,
            f"style:{key}",
            style="success" if current == key else None,
        )

    rows = [
        [
            style_button("cute", "🎀 Няшный"),
            style_button("dumb", "🧠 Тупой"),
        ],
        [
            style_button("vasya", "🧢 Вася"),
            style_button("brother", "🤝 Брат"),
        ],
        [
            style_button("rooster", "🐓 Петух"),
            style_button("schizo", "👁 Шизик"),
        ],
    ]
    if current == "rooster":
        rows.append([btn("Настройки мата", "rooster:settings", emoji="refresh", style="primary")])
    rows.extend([
        [btn("Отключить стиль", "style:off", emoji="warning", style="danger")],
        [btn("Назад в настройки", "hub:settings", emoji="back")],
    ])
    examples = "\n".join(
        f"{'→' if key == current else '•'} <b>{label}</b>: {html_text(STYLE_EXAMPLES[key])}"
        for key, label in STYLE_LABELS.items()
    )
    change_text = style_changed_notification_html(*change_notice) + "\n\n" if change_notice else ""
    text = (
        change_text
        + f"{tg_icon('design')} <b>Стиль общения</b>\n"
        f"{tg_icon('brush')} Сейчас: <b>{current_label}</b>\n\n"
        "Выбери стиль — он будет применяться автоматически к исходящим Business-сообщениям.\n"
        "🎀 Няшный повторяет механику CuteMessages: строчные буквы, растягивание гласных, "
        "заикание, эмодзи, каомодзи, суффиксы, мягкие окончания и милая пунктуация.\n"
        "🐓 У «Петуха» отдельно настраивается количество мата: 10 / 20 / 40 / 60 / 100%.\n"
        "👁 «Шизик» специально ломает смысл фразы случайными несвязанными словами: автобус, макароны, огурцы, земля и чем угодно ещё.\n"
        "Ссылки, @username, номера телефонов и команды с точки не меняются.\n\n"
        f"<b>Примеры:</b>\n{examples}"
    )
    return text, kb(rows)

def page_rooster_settings(user_id: int) -> tuple[str, dict]:
    current = get_rooster_profanity_percent(user_id)
    style_notice = STYLE_CHANGE_NOTICE.pop(user_id, None)
    level_notice = STYLE_LEVEL_NOTICE.pop(user_id, None)

    def level_button(percent: int) -> dict:
        return btn(
            ("✓ " if current == percent else "") + f"{percent}%",
            f"rooster:level:{percent}",
            style="success" if current == percent else None,
        )

    rows = [
        [level_button(10), level_button(20), level_button(40)],
        [level_button(60), level_button(100)],
        [btn("Назад к стилям", "style", emoji="back")],
    ]
    notices = []
    if style_notice:
        notices.append(style_changed_notification_html(*style_notice))
    if level_notice:
        notices.append(style_level_notification_html(*level_notice))
    notice_text = ("\n\n".join(notices) + "\n\n") if notices else ""
    text = (
        notice_text
        + f"{tg_icon('design')} <b>Настройка стиля «Петух»</b>\n\n"
        f"{tg_icon('brush')} Стиль: <b>Петух</b>\n"
        f"{tg_icon('settings')} Уровень: <b>{current}%</b>\n\n"
        "Чем выше процент, тем чаще HolyGram заменяет обычные слова обсценными вариантами "
        "и добавляет матерные вставки. На 100% стиль специально становится максимально насыщенным."
    )
    return text, kb(rows)


def page_ref(user_id: int) -> tuple[str, dict]:
    link = referral_link(user_id)
    count = ref_count(user_id)
    text = (
        f"{pe('invite')} <b>Пригласить друзей</b>\n\n"
        f"Приглашено: <b>{count}</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        "HolyGram бесплатный — просто отправь ссылку другу."
    )
    rows = [
        [
            btn("Скопировать", copy=link, emoji="view", style="primary"),
            btn(
                "Поделиться",
                url=f"https://t.me/share/url?url={quote(link, safe='')}&text=Попробуй%20HolyGram%20—%20он%20бесплатный",
                emoji="invite",
            ),
        ],
        [btn("Назад в «Моё»", "hub:mine", emoji="back")],
    ]
    return text, kb(rows)

def page_help(user_id: int) -> tuple[str, dict]:
    username = bot_username()
    text = (
        f"{pe('support')} <b>Настроить за минуту</b>\n\n"
        "На картинке показано, куда нажать, чтобы подключить HolyGram.\n\n"
        f"Найди <code>@{username}</code>, добавь бота и выбери нужные чаты.\n"
        f"{pe('check')} Telegram Premium для подключения не нужен."
    )
    rows = [
        [btn("Проверить подключение", "conns", emoji="refresh", style="primary")],
        [btn("Назад", "home", emoji="back")],
    ]
    return text, kb(rows)


def ensure_setup_guide_image() -> Path | None:
    if SETUP_GUIDE_IMAGE_PATH.exists() and SETUP_GUIDE_IMAGE_PATH.stat().st_size > 0:
        return SETUP_GUIDE_IMAGE_PATH

    try:
        if SETUP_GUIDE_B64_PATH.exists():
            encoded = SETUP_GUIDE_B64_PATH.read_text(encoding="ascii")
        else:
            parts = sorted((BASE_DIR / "assets").glob(SETUP_GUIDE_B64_PART_GLOB))
            if not parts:
                log("setup guide asset missing")
                return None
            encoded = "".join(part.read_text(encoding="ascii").strip() for part in parts)

        raw = base64.b64decode(encoded, validate=True)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("setup guide is not a PNG")
        SETUP_GUIDE_IMAGE_PATH.write_bytes(raw)
        return SETUP_GUIDE_IMAGE_PATH
    except Exception as exc:
        log(f"setup guide decode failed: {type(exc).__name__}: {exc}")
        return None


def send_setup_guide(user_id: int, chat_id: int) -> None:
    caption, markup = page_help(user_id)
    image_path = ensure_setup_guide_image()
    if image_path is None:
        send_menu_page(user_id, chat_id, caption, markup)
        return

    previous = _menu_message(user_id)
    if previous:
        try:
            telegram_call("deleteMessage", {"chat_id": previous[0], "message_id": previous[1]})
        except TelegramApiError:
            pass

    try:
        photo_file_id = maintenance_get(SETUP_GUIDE_FILE_ID_KEY)
        if photo_file_id:
            try:
                result = telegram_call(
                    "sendPhoto",
                    {
                        "chat_id": chat_id,
                        "photo": photo_file_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                        "reply_markup": markup,
                    },
                )
            except TelegramApiError:
                maintenance_set(SETUP_GUIDE_FILE_ID_KEY, "")
                photo_file_id = ""

        if not photo_file_id:
            result = telegram_multipart_call(
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(markup, ensure_ascii=False),
                },
                {"photo": image_path},
                timeout=60,
            )
            if isinstance(result, dict):
                photos = result.get("photo") or []
                if photos and photos[-1].get("file_id"):
                    maintenance_set(SETUP_GUIDE_FILE_ID_KEY, str(photos[-1]["file_id"]))

        if isinstance(result, dict) and result.get("message_id"):
            _remember_menu_message(user_id, chat_id, int(result["message_id"]), True)
    except TelegramApiError as exc:
        log(f"setup guide send failed for {chat_id}: {exc}")
        send_menu_page(user_id, chat_id, caption, markup)

def page_connections(user_id: int) -> tuple[str, dict]:
    connections = list_business_connections(user_id)
    regular_chats = get_user_chats(user_id)
    if connections or regular_chats:
        business_enabled = sum(1 for row in connections if row.get("is_enabled"))
        enabled = business_enabled + len(regular_chats)
        disabled = len(connections) - business_enabled
        text = (
            f"{pe('view')} <b>Подключённые чаты</b>\n\n"
            f"{pe('check')} Работают: <b>{enabled}</b>\n"
            f"{pe('warning')} Приостановлены: <b>{disabled}</b>\n\n"
            "Новые сообщения сохраняются автоматически."
        )
    else:
        text = (
            f"{pe('view')} <b>Подключённые чаты</b>\n\n"
            "Пока ничего не подключено. Добавь HolyGram в Telegram Business."
        )

    rows: list[list[dict]] = []
    if connections and any(not row.get("is_enabled") for row in connections):
        rows.append([btn("Включить приостановленные", "restore", emoji="check", style="success")])
    rows.extend([
        [btn("Добавить или изменить чаты", "help", emoji="add")],
        [btn("Обновить", "conns", emoji="refresh")],
        [btn("Назад в настройки", "hub:settings", emoji="back")],
    ])
    return text, kb(rows)

def stored_user_label(
    user_id: int,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
) -> str:
    special = SPECIAL_USER_LABELS.get(int(user_id), "")
    if special:
        return html_text(special)
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    if username:
        mention = f"@{html_text(username)}"
        return f"{html_text(name)} ({mention})" if name else mention
    return html_text(name) if name else f"Пользователь {user_id}"


def stored_user_notification_label(user_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return stored_user_label(user_id, *(row or (None, None, None)))


def stored_user_payment_label(user_id: int) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    label = stored_user_label(user_id, *(row or (None, None, None)))
    return f"{label}\nID: <code>{user_id}</code>"


def chat_participant_label(author: str | None, user_id: int | None = None) -> str:
    raw = str(author or "").strip()
    username = re.search(r"@[A-Za-z0-9_]{3,}", raw)
    if username:
        return username.group(0)
    name = raw.split(" (", 1)[0].strip()
    return name or (f"Пользователь {user_id}" if user_id else "Собеседник")


def page_users(user_id: int, page_number: int = 0) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Эта страница доступна только администраторам.", kb([BACK_HOME])

    with sqlite3.connect(DB_PATH) as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        page_count = max(1, (total + USERS_PAGE_SIZE - 1) // USERS_PAGE_SIZE)
        page_number = min(max(0, page_number), page_count - 1)
        rows = conn.execute(
            """
            SELECT u.user_id, u.first_name, u.last_name, u.username,
                   u.created_at, u.updated_at, COALESCE(l.label,'')
            FROM users AS u
            LEFT JOIN admin_user_labels AS l ON l.admin_id=? AND l.target_id=u.user_id
            ORDER BY u.updated_at DESC, u.user_id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, USERS_PAGE_SIZE, page_number * USERS_PAGE_SIZE),
        ).fetchall()

    lines = []
    for index, row in enumerate(rows, start=page_number * USERS_PAGE_SIZE + 1):
        uid, first_name, last_name, username, created_at, updated_at, label = row
        lines.append(
            f"<b>{index}.</b> {stored_user_label(int(uid), first_name, last_name, username)}\n"
            f"ID: <code>{uid}</code> · "
            f"заходил: {time.strftime('%d.%m.%Y', time.localtime(int(updated_at or created_at)))}"
            f"{f' · метка: <b>{html_text(label)}</b>' if label else ''}"
        )

    body = "\n\n".join(lines) if lines else "Пользователей пока нет."
    text = (
        f"{pe('view')} <b>Все пользователи</b>\n"
        f"Всего: <b>{total}</b> · страница {page_number + 1}/{page_count}\n\n{body}"
    )
    navigation: list[dict] = []
    if page_number > 0:
        navigation.append(btn("← Пред.", f"users:{page_number - 1}", emoji="back"))
    if page_number + 1 < page_count:
        navigation.append(btn("След. →", f"users:{page_number + 1}", emoji="view"))
    buttons: list[list[dict]] = []
    for row in rows:
        uid, first_name, last_name, username, *_ = row
        short_name = f"@{username}" if username else (first_name or str(uid))
        buttons.append([btn(f"{short_name} · {uid}", f"user:{uid}:{page_number}", emoji="profile")])
    if navigation:
        buttons.append(navigation)
    buttons.extend([
        [btn("Обновить", f"users:{page_number}", emoji="refresh")],
        BACK_PANEL,
    ])
    return text, kb(buttons)

def page_user_card(admin_id: int, target_id: int, return_page: int = 0) -> tuple[str, dict]:
    if not is_admin_user(admin_id):
        return "Эта страница доступна только администраторам.", kb([BACK_HOME])
    log_admin_view(admin_id, target_id, None, "проверка пользователя")
    chats_hidden = user_chats_are_hidden(target_id)
    with sqlite3.connect(DB_PATH) as conn:
        user = conn.execute(
            "SELECT first_name, last_name, username, created_at, updated_at FROM users WHERE user_id = ?",
            (target_id,),
        ).fetchone()
        regular_chats = conn.execute("SELECT COUNT(*) FROM chat_owners WHERE owner_id = ?", (target_id,)).fetchone()[0]
        business_chats = conn.execute("SELECT COUNT(*) FROM business_connections WHERE owner_id = ?", (target_id,)).fetchone()[0]
        blocked = conn.execute("SELECT reason FROM blocked_users WHERE user_id = ?", (target_id,)).fetchone()
        chat_stats = (0, 0, 0, 0, 0, 0)
        if not chats_hidden:
            chat_stats = conn.execute(
                """
                SELECT COUNT(DISTINCT m.chat_id), COUNT(*),
                       COALESCE(SUM(CASE WHEN m.deleted_at IS NOT NULL THEN 1 ELSE 0 END),0),
                       COALESCE(SUM(CASE WHEN m.edit_count > 0 THEN 1 ELSE 0 END),0),
                       COALESCE(SUM(CASE WHEN m.media_type IS NOT NULL THEN 1 ELSE 0 END),0),
                       COALESCE(MAX(m.updated_at),0)
                """ + OWNER_MESSAGES_FROM,
                (target_id, target_id),
            ).fetchone()
    if not user:
        return "Пользователь не найден.", kb([[btn("Назад", f"users:{return_page}", emoji="home")]])

    label = get_user_label(admin_id, target_id)
    if chats_hidden:
        chats_line = "История чатов: <b>скрыта</b>\n"
    else:
        chat_count, message_count, deleted_count, edited_count, media_count, last_activity = map(int, chat_stats)
        last_text = format_display_time(last_activity) if last_activity else "нет сообщений"
        chats_line = (
            f"Чатов: <b>{chat_count}</b> · сообщений: <b>{message_count}</b>\n"
            f"Удалено: <b>{deleted_count}</b> · изменено: <b>{edited_count}</b> · медиа: <b>{media_count}</b>\n"
            f"Последняя активность: <b>{last_text}</b>\n"
        )
    text = (
        f"{pe('view')} <b>Карточка пользователя</b>\n\n"
        f"{stored_user_label(target_id, user[0], user[1], user[2])}\n"
        f"ID: <code>{target_id}</code>\n"
        f"Доступ: <b>бесплатный</b>\n"
        f"Статус: <b>{'заблокирован' if blocked else 'активен'}</b>"
        f"{f' ({html_text(blocked[0])})' if blocked and blocked[0] else ''}\n"
        f"Подключений: <b>{int(regular_chats) + int(business_chats)}</b> "
        f"(обычных {regular_chats}, Business {business_chats})\n"
        f"{chats_line}"
        f"Метка: <b>{html_text(label) if label else 'нет'}</b>\n"
        f"Рефералов: <b>{ref_count(target_id)}</b>"
    )
    block_button = btn("Разблокировать", f"unblock:{target_id}:{return_page}", emoji="check", style="success") if blocked else btn("Заблокировать", f"block:{target_id}:{return_page}", emoji="warning", style="danger")
    rows = [
        [btn("Изменить метку", f"ulabel:{target_id}:{return_page}", emoji="profile")],
        [block_button],
        [btn("Назад к пользователям", f"users:{return_page}", emoji="home")],
        BACK_HOME,
    ]
    if admin_id in DELETE_USER_ADMIN_IDS and target_id not in DELETE_USER_ADMIN_IDS and not is_admin_user(target_id):
        rows.insert(-2, [btn("Удалить из базы", f"userdel:{target_id}:{return_page}", emoji="warning", style="danger")])
    if can_view_user_chats(admin_id) and not user_chats_are_hidden(target_id):
        rows.insert(0, [
            btn("Чаты", f"uchats:{target_id}:{return_page}:0", emoji="view"),
            btn("Поиск", f"usearch:{target_id}:{return_page}", emoji="view"),
        ])
        rows.insert(1, [
            btn("Экспорт без медиа", f"uexall:t:{target_id}:{return_page}", emoji="history"),
            btn("Экспорт с медиа", f"uexall:m:{target_id}:{return_page}", emoji="view"),
        ])
    return text, kb(rows)


CHAT_FILTER_LABELS = {
    "all": "Все",
    "new": "Новые",
    "deleted": "Удалённые",
    "edited": "Изменённые",
    "media": "С медиа",
    "today": "Сегодня",
}


def page_chat_filters(admin_id: int, target_id: int, return_page: int = 0) -> tuple[str, dict]:
    if not can_view_user_chats(admin_id) or user_chats_are_hidden(target_id):
        return "Чаты недоступны.", kb([BACK_HOME])
    rows = [
        [btn("Все", f"ucl:{target_id}:{return_page}:0:all:recent", emoji="view"), btn("Новые", f"ucl:{target_id}:{return_page}:0:new:recent", emoji="refresh")],
        [btn("Удалённые", f"ucl:{target_id}:{return_page}:0:deleted:recent", emoji="warning"), btn("Изменённые", f"ucl:{target_id}:{return_page}:0:edited:recent", emoji="history")],
        [btn("С медиа", f"ucl:{target_id}:{return_page}:0:media:recent", emoji="view"), btn("Сегодня", f"ucl:{target_id}:{return_page}:0:today:recent", emoji="check")],
        [btn("По активности", f"ucl:{target_id}:{return_page}:0:all:recent", emoji="refresh"), btn("По сообщениям", f"ucl:{target_id}:{return_page}:0:all:count", emoji="history")],
        [btn("Назад к чатам", f"uchats:{target_id}:{return_page}:0", emoji="home")],
        BACK_HOME,
    ]
    return f"{pe('view')} <b>Фильтры чатов</b>\n\nВыбери, какие диалоги показать и как их отсортировать.", kb(rows)


def page_user_chats(
    admin_id: int,
    target_id: int,
    return_page: int = 0,
    page_number: int = 0,
    filter_name: str = "all",
    sort_name: str = "recent",
) -> tuple[str, dict]:
    if not can_view_user_chats(admin_id):
        return "Эта страница доступна только администраторам чатов.", kb([BACK_HOME])
    if user_chats_are_hidden(target_id):
        return "Чаты этого пользователя скрыты.", kb([[btn("К пользователям", f"users:{return_page}", emoji="home")], BACK_HOME])
    filter_name = filter_name if filter_name in CHAT_FILTER_LABELS else "all"
    sort_name = sort_name if sort_name in {"recent", "count"} else "recent"
    today_start = int(datetime.now(DISPLAY_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    filter_sql = {
        "all": "1=1",
        "new": "m.updated_at > COALESCE(vs.last_seen_at,0)",
        "deleted": "m.deleted_at IS NOT NULL",
        "edited": "m.edit_count > 0",
        "media": "m.media_type IS NOT NULL",
        "today": "m.updated_at >= ?",
    }[filter_name]
    filter_params: tuple[int, ...] = (today_start,) if filter_name == "today" else ()
    chat_from = """
        FROM messages AS m
        LEFT JOIN chat_owners AS co ON m.context='regular' AND co.chat_id=m.chat_id
        LEFT JOIN business_connections AS bc ON m.context='business:' || bc.connection_id
        LEFT JOIN chat_view_state AS vs
          ON vs.admin_id=? AND vs.target_id=? AND vs.chat_id=m.chat_id
        WHERE (co.owner_id=? OR bc.owner_id=?) AND
    """
    with sqlite3.connect(DB_PATH) as conn:
        target_user = conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (target_id,),
        ).fetchone()
        query_params = (admin_id, target_id, target_id, target_id, *filter_params)
        total = int(conn.execute(
            "SELECT COUNT(DISTINCT m.chat_id) " + chat_from + filter_sql,
            query_params,
        ).fetchone()[0])
        page_count = max(1, (total + ADMIN_CHATS_PAGE_SIZE - 1) // ADMIN_CHATS_PAGE_SIZE)
        page_number = min(max(0, page_number), page_count - 1)
        chats = conn.execute(
            """
            SELECT m.chat_id, COUNT(*), COUNT(DISTINCT m.user_id), MAX(m.updated_at),
                   COALESCE(MAX(CASE WHEN m.user_id != ? THEN m.author END), MAX(m.author)),
                   COALESCE(MAX(vs.pinned),0),
                   SUM(CASE WHEN m.updated_at > COALESCE(vs.last_seen_at,0) THEN 1 ELSE 0 END)
            """ + chat_from + filter_sql + """
            GROUP BY m.chat_id
            ORDER BY COALESCE(MAX(vs.pinned),0) DESC,
                     """ + ("COUNT(*) DESC, MAX(m.updated_at) DESC" if sort_name == "count" else "MAX(m.updated_at) DESC") + """
            LIMIT ? OFFSET ?
            """,
            (target_id, *query_params, ADMIN_CHATS_PAGE_SIZE, page_number * ADMIN_CHATS_PAGE_SIZE),
        ).fetchall()
    text = (
        f"{pe('view')} <b>Чаты пользователя</b>\n"
        f"{stored_user_label(target_id, *(target_user or (None, None, None)))}\n"
        f"ID: <code>{target_id}</code> · всего: <b>{total}</b> · "
        f"страница {page_number + 1}/{page_count}\n\n"
        f"Фильтр: <b>{CHAT_FILTER_LABELS[filter_name]}</b> · "
        f"сортировка: <b>{'по сообщениям' if sort_name == 'count' else 'по активности'}</b>\n"
        "Нажми на чат, чтобы открыть его карточку."
    )
    rows = [
        [btn(
            f"{'📌 ' if pinned else ''}{chat_participant_label(author)[:20]} · {message_count} сообщ. · +{new_count} · {format_display_time(_updated_at, '%d.%m %H:%M')}",
            f"uchat:{target_id}:{chat_id}:{return_page}:{page_number}",
            emoji="view",
        )]
        for chat_id, message_count, _participants, _updated_at, author, pinned, new_count in chats
    ]
    navigation = []
    if page_number > 0:
        navigation.append(btn("← Пред.", f"ucl:{target_id}:{return_page}:{page_number - 1}:{filter_name}:{sort_name}", emoji="back"))
    if page_number + 1 < page_count:
        navigation.append(btn("След. →", f"ucl:{target_id}:{return_page}:{page_number + 1}:{filter_name}:{sort_name}", emoji="view"))
    if navigation:
        rows.append(navigation)
    rows.extend([
        [btn("Фильтры", f"ucfilters:{target_id}:{return_page}", emoji="view"), btn("Поиск", f"usearch:{target_id}:{return_page}", emoji="view")],
        [btn("Обновить список", f"ucl:{target_id}:{return_page}:{page_number}:{filter_name}:{sort_name}", emoji="refresh")],
        [btn("Назад", f"user:{target_id}:{return_page}", emoji="back")],
        [btn("Главное меню", "home", emoji="home")],
    ])
    return text, kb(rows)


def page_user_chat_messages(
    admin_id: int,
    target_id: int,
    chat_id: int,
    return_page: int = 0,
    chats_page: int = 0,
    page_number: int = 0,
    period: str = "all",
) -> tuple[str, dict]:
    if not can_view_user_chats(admin_id):
        return "Эта страница доступна только администраторам чатов.", kb([BACK_HOME])
    if user_chats_are_hidden(target_id):
        return "Чаты этого пользователя скрыты.", kb([[btn("К пользователям", f"users:{return_page}", emoji="home")], BACK_HOME])
    now_dt = datetime.now(DISPLAY_TIMEZONE)
    day_start = int(now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    period_sql = ""
    period_params: tuple[int, ...] = ()
    period_label = "вся история"
    if period == "today":
        period_sql, period_params, period_label = " AND m.updated_at>=?", (day_start,), "сегодня"
    elif period == "yesterday":
        period_sql, period_params, period_label = " AND m.updated_at>=? AND m.updated_at<?", (day_start - 86400, day_start), "вчера"
    elif period == "7d":
        period_sql, period_params, period_label = " AND m.updated_at>=?", (int(time.time()) - 7 * 86400,), "7 дней"
    elif re.fullmatch(r"d\d{8}", period):
        try:
            selected = datetime.strptime(period[1:], "%Y%m%d").replace(tzinfo=DISPLAY_TIMEZONE)
            selected_start = int(selected.timestamp())
            period_sql, period_params = " AND m.updated_at>=? AND m.updated_at<?", (selected_start, selected_start + 86400)
            period_label = selected.strftime("%d.%m.%Y")
        except ValueError:
            period = "all"
    with sqlite3.connect(DB_PATH) as conn:
        target_user = conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (target_id,),
        ).fetchone()
        params = (target_id, target_id, chat_id)
        total = int(conn.execute(
            "SELECT COUNT(*) " + OWNER_MESSAGES_FROM + " AND m.chat_id = ?" + period_sql,
            (*params, *period_params),
        ).fetchone()[0])
        page_count = max(1, (total + ADMIN_MESSAGES_PAGE_SIZE - 1) // ADMIN_MESSAGES_PAGE_SIZE)
        page_number = min(max(0, page_number), page_count - 1)
        messages = conn.execute(
            """
            SELECT m.message_id, m.user_id, m.author, m.content, m.media_type,
                   m.updated_at, m.deleted_at, m.reply_to_message_id,
                   m.reply_to_author, m.reply_to_content
            """ + OWNER_MESSAGES_FROM + """
            AND m.chat_id = ?
            """ + period_sql + """
            ORDER BY m.updated_at DESC, m.message_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, *period_params, ADMIN_MESSAGES_PAGE_SIZE, page_number * ADMIN_MESSAGES_PAGE_SIZE),
        ).fetchall()
    chat_label = next(
        (chat_participant_label(row[2], row[1]) for row in messages if row[1] != target_id and row[2]),
        f"Чат {chat_id}",
    )
    lines = []
    for message_id, sender_id, author, content, media_type, updated_at, deleted_at, reply_id, reply_author, reply_content in messages:
        body = str(content or "[без текста]")
        if len(body) > 300:
            body = body[:297] + "..."
        flags = []
        if media_type:
            flags.append(str(media_type))
        if deleted_at:
            flags.append("удалено")
        suffix = f" · {', '.join(flags)}" if flags else ""
        sender_label = chat_participant_label(author, sender_id)
        reply_line = ""
        if reply_id:
            reply_preview = str(reply_content or "[сообщение]")
            if len(reply_preview) > 180:
                reply_preview = reply_preview[:177] + "..."
            reply_line = f"\n↩ Ответ на {html_text(chat_participant_label(reply_author))}: {html_quote(reply_preview)}"
        lines.append(
            f"<b>{html_text(sender_label)}</b>"
            f"{f' · <code>{sender_id}</code>' if sender_id and sender_id != target_id else ''}\n"
            f"{format_display_time(updated_at)}"
            f" · ID {message_id}{suffix}\n{html_quote(body)}{reply_line}"
        )
    text = (
        f"{pe('history')} <b>Сообщения чата</b>\n"
        f"Пользователь: {stored_user_label(target_id, *(target_user or (None, None, None)))}\n"
        f"Чат: <b>{html_text(chat_label)}</b> · <code>{chat_id}</code>\n"
        f"Период: <b>{period_label}</b> · сообщений: <b>{total}</b> · страница {page_number + 1}/{page_count}\n\n"
        + ("\n\n".join(lines) if lines else "Сохранённых сообщений нет.")
    )
    navigation = []
    callback_prefix = "umsg" if period == "all" else "uday"
    callback_suffix = "" if period == "all" else f":{period}"
    if page_number > 0:
        navigation.append(btn("Новее", f"{callback_prefix}:{target_id}:{chat_id}:{return_page}:{chats_page}:{page_number - 1}{callback_suffix}", emoji="refresh"))
    if page_number + 1 < page_count:
        navigation.append(btn("Раньше", f"{callback_prefix}:{target_id}:{chat_id}:{return_page}:{chats_page}:{page_number + 1}{callback_suffix}", emoji="history"))
    rows = [
        [btn(
            f"{ADMIN_MEDIA_LABELS[media_type]} · {str(author or sender_id or 'без имени')[:32]}",
            f"umedia:{target_id}:{chat_id}:{message_id}",
        )]
        for message_id, sender_id, author, _content, media_type, _updated_at, _deleted_at, _reply_id, _reply_author, _reply_content in messages
        if media_type in ADMIN_MEDIA_LABELS
    ]
    if navigation:
        rows.append(navigation)
    rows.extend([
        [btn("Обновить чат", f"{callback_prefix}:{target_id}:{chat_id}:{return_page}:{chats_page}:{page_number}{callback_suffix}", emoji="refresh")],
        [btn("Медиа", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:all:0", emoji="view"), btn("По дате", f"udates:{target_id}:{chat_id}:{return_page}:{chats_page}", emoji="history")],
        [btn("Назад", f"uchat:{target_id}:{chat_id}:{return_page}:{chats_page}", emoji="back")],
        [btn("К списку чатов", f"uchats:{target_id}:{return_page}:{chats_page}", emoji="view")],
        [btn("Главное меню", "home", emoji="home")],
    ])
    set_chat_seen(admin_id, target_id, chat_id)
    log_admin_view(admin_id, target_id, chat_id, "просмотр сообщений", period_label)
    return text, kb(rows)


def page_chat_overview(
    admin_id: int, target_id: int, chat_id: int, return_page: int = 0, chats_page: int = 0
) -> tuple[str, dict]:
    if not can_view_user_chats(admin_id) or user_chats_are_hidden(target_id):
        return "Чат недоступен.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN m.deleted_at IS NOT NULL THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN m.edit_count > 0 THEN 1 ELSE 0 END),0),
                   COALESCE(SUM(CASE WHEN m.media_type IS NOT NULL THEN 1 ELSE 0 END),0),
                   MIN(m.created_at), MAX(m.updated_at),
                   COALESCE(MAX(CASE WHEN m.user_id != ? THEN m.author END),MAX(m.author)),
                   SUM(CASE WHEN m.updated_at > COALESCE(vs.last_seen_at,0) THEN 1 ELSE 0 END),
                   COALESCE(MAX(vs.pinned),0)
            FROM messages m
            LEFT JOIN chat_owners co ON m.context='regular' AND co.chat_id=m.chat_id
            LEFT JOIN business_connections bc ON m.context='business:' || bc.connection_id
            LEFT JOIN chat_view_state vs ON vs.admin_id=? AND vs.target_id=? AND vs.chat_id=m.chat_id
            WHERE (co.owner_id=? OR bc.owner_id=?) AND m.chat_id=?
            """,
            (target_id, admin_id, target_id, target_id, target_id, chat_id),
        ).fetchone()
        media_rows = conn.execute(
            """
            SELECT m.media_type,COUNT(*) FROM messages m
            LEFT JOIN chat_owners co ON m.context='regular' AND co.chat_id=m.chat_id
            LEFT JOIN business_connections bc ON m.context='business:' || bc.connection_id
            WHERE (co.owner_id=? OR bc.owner_id=?) AND m.chat_id=? AND m.media_type IS NOT NULL
            GROUP BY m.media_type ORDER BY COUNT(*) DESC
            """,
            (target_id, target_id, chat_id),
        ).fetchall()
        first_message = conn.execute(
            "SELECT m.content " + OWNER_MESSAGES_FROM + " AND m.chat_id=? ORDER BY m.created_at,m.message_id LIMIT 1",
            (target_id, target_id, chat_id),
        ).fetchone()
        last_message = conn.execute(
            "SELECT m.content " + OWNER_MESSAGES_FROM + " AND m.chat_id=? ORDER BY m.updated_at DESC,m.message_id DESC LIMIT 1",
            (target_id, target_id, chat_id),
        ).fetchone()
    total, deleted, edited, media, first_at, last_at, author, new_count, pinned = row or (0,) * 9
    label = chat_participant_label(author)
    media_text = " · ".join(f"{MEDIA_LABELS.get(kind, kind)}: {count}" for kind, count in media_rows) or "нет"
    text = (
        f"{pe('view')} <b>{html_text(label)}</b>\n"
        f"Чат: <code>{chat_id}</code>\n\n"
        f"Сообщений: <b>{int(total)}</b> · новых: <b>{int(new_count or 0)}</b>\n"
        f"Удалено: <b>{int(deleted or 0)}</b> · изменено: <b>{int(edited or 0)}</b>\n"
        f"Медиа: <b>{int(media or 0)}</b> ({html_text(media_text)})\n"
        f"Первое: <b>{format_display_time(first_at) if first_at else '—'}</b>\n"
        f"{html_quote(str(first_message[0])[:120]) if first_message else ''}\n"
        f"Последнее: <b>{format_display_time(last_at) if last_at else '—'}</b>\n"
        f"{html_quote(str(last_message[0])[:120]) if last_message else ''}"
    )
    log_admin_view(admin_id, target_id, chat_id, "карточка чата")
    rows = [
        [btn("Сообщения", f"umsg:{target_id}:{chat_id}:{return_page}:{chats_page}:0", emoji="view"), btn("Медиа", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:all:0", emoji="view")],
        [btn("По дате", f"udates:{target_id}:{chat_id}:{return_page}:{chats_page}", emoji="history"), btn("Экспорт", f"uexport:{target_id}:{chat_id}", emoji="view")],
        [btn("Открепить" if pinned else "Закрепить", f"upin:{target_id}:{chat_id}:{return_page}:{chats_page}", emoji="promo")],
        [btn("Назад к чатам", f"uchats:{target_id}:{return_page}:{chats_page}", emoji="home")],
        BACK_HOME,
    ]
    return text, kb(rows)


def get_user_owned_saved_message(target_id: int, chat_id: int, message_id: int) -> dict | None:
    if user_chats_are_hidden(target_id):
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT m.user_id, m.author, m.content,
                   m.media_type, m.media_file_id, m.media_unique_id, m.media_json,
                   m.local_media_path, m.has_media_spoiler, m.ttl_seconds, m.deleted_at
            """ + OWNER_MESSAGES_FROM + """
            AND m.chat_id = ? AND m.message_id = ?
            ORDER BY m.updated_at DESC
            LIMIT 1
            """,
            (target_id, target_id, chat_id, message_id),
        ).fetchone()
    return dict(row) if row else None


MEDIA_FILTER_CODES = {
    "all": None,
    "photo": "photo",
    "video": "video",
    "voice": "voice",
    "round": "video_note",
    "sticker": "sticker",
    "file": "document",
}


def page_chat_media(
    admin_id: int,
    target_id: int,
    chat_id: int,
    return_page: int = 0,
    chats_page: int = 0,
    media_filter: str = "all",
    page_number: int = 0,
) -> tuple[str, dict]:
    if not can_view_user_chats(admin_id) or user_chats_are_hidden(target_id):
        return "Медиа недоступно.", kb([BACK_HOME])
    media_filter = media_filter if media_filter in MEDIA_FILTER_CODES else "all"
    media_type = MEDIA_FILTER_CODES[media_filter]
    type_sql = " AND m.media_type=?" if media_type else ""
    type_params: tuple[str, ...] = (media_type,) if media_type else ()
    with sqlite3.connect(DB_PATH) as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) " + OWNER_MESSAGES_FROM + " AND m.chat_id=? AND m.media_type IS NOT NULL" + type_sql,
            (target_id, target_id, chat_id, *type_params),
        ).fetchone()[0])
        page_count = max(1, (total + ADMIN_MESSAGES_PAGE_SIZE - 1) // ADMIN_MESSAGES_PAGE_SIZE)
        page_number = min(max(0, page_number), page_count - 1)
        rows_data = conn.execute(
            """
            SELECT m.message_id,m.author,m.media_type,m.updated_at,m.content
            """ + OWNER_MESSAGES_FROM + " AND m.chat_id=? AND m.media_type IS NOT NULL" + type_sql + """
            ORDER BY m.updated_at DESC LIMIT ? OFFSET ?
            """,
            (target_id, target_id, chat_id, *type_params, ADMIN_MESSAGES_PAGE_SIZE, page_number * ADMIN_MESSAGES_PAGE_SIZE),
        ).fetchall()
    filter_label = "все" if media_type is None else MEDIA_LABELS.get(media_type, media_type)
    lines = [
        f"{format_display_time(updated_at, '%d.%m %H:%M')} · <b>{html_text(chat_participant_label(author))}</b> · "
        f"{html_text(MEDIA_LABELS.get(kind, kind))}{f' · {html_text(content[:80])}' if content and not content.startswith('[') else ''}"
        for message_id, author, kind, updated_at, content in rows_data
    ]
    text = (
        f"{pe('view')} <b>Медиа чата</b>\n"
        f"Фильтр: <b>{html_text(filter_label)}</b> · файлов: <b>{total}</b> · страница {page_number + 1}/{page_count}\n"
        f"Открытые файлы удаляются из админского диалога автоматически.\n\n"
        + ("\n".join(lines) if lines else "Медиа не найдено.")
    )
    rows = [
        [btn(
            f"{ADMIN_MEDIA_LABELS.get(kind, MEDIA_LABELS.get(kind, kind))} · {format_display_time(updated_at, '%d.%m %H:%M')}",
            f"umedia:{target_id}:{chat_id}:{message_id}",
        )]
        for message_id, _author, kind, updated_at, _content in rows_data
        if kind in MEDIA_SENDERS
    ]
    navigation = []
    if page_number > 0:
        navigation.append(btn("Новее", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:{media_filter}:{page_number - 1}", emoji="refresh"))
    if page_number + 1 < page_count:
        navigation.append(btn("Раньше", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:{media_filter}:{page_number + 1}", emoji="history"))
    if navigation:
        rows.append(navigation)
    rows.extend([
        [btn("Все", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:all:0"), btn("Фото", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:photo:0")],
        [btn("Видео", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:video:0"), btn("Голосовые", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:voice:0")],
        [btn("Кружки", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:round:0"), btn("Стикеры", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:sticker:0")],
        [btn("Файлы", f"ugal:{target_id}:{chat_id}:{return_page}:{chats_page}:file:0")],
        [btn("Назад", f"uchat:{target_id}:{chat_id}:{return_page}:{chats_page}", emoji="back")],
        [btn("Главное меню", "home", emoji="home")],
    ])
    log_admin_view(admin_id, target_id, chat_id, "просмотр медиа", filter_label)
    return text, kb(rows)


def page_chat_dates(admin_id: int, target_id: int, chat_id: int, return_page: int, chats_page: int) -> tuple[str, dict]:
    if not can_view_user_chats(admin_id) or user_chats_are_hidden(target_id):
        return "Чат недоступен.", kb([BACK_HOME])
    rows = [
        [btn("Сегодня", f"uday:{target_id}:{chat_id}:{return_page}:{chats_page}:0:today", emoji="check"), btn("Вчера", f"uday:{target_id}:{chat_id}:{return_page}:{chats_page}:0:yesterday", emoji="history")],
        [btn("Последние 7 дней", f"uday:{target_id}:{chat_id}:{return_page}:{chats_page}:0:7d", emoji="history")],
        [btn("Выбрать дату", f"udatein:{target_id}:{chat_id}:{return_page}:{chats_page}", emoji="view")],
        [btn("Вся история", f"umsg:{target_id}:{chat_id}:{return_page}:{chats_page}:0", emoji="refresh")],
        [btn("Назад", f"uchat:{target_id}:{chat_id}:{return_page}:{chats_page}", emoji="back")],
        [btn("Главное меню", "home", emoji="home")],
    ]
    return f"{pe('history')} <b>Сообщения по дате</b>\n\nВыбери нужный период.", kb(rows)


def page_chat_search_results(admin_id: int, target_id: int, return_page: int = 0, page_number: int = 0) -> tuple[str, dict]:
    if not can_view_user_chats(admin_id) or user_chats_are_hidden(target_id):
        return "Поиск недоступен.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        search = conn.execute(
            "SELECT query FROM admin_searches WHERE admin_id=? AND target_id=?",
            (admin_id, target_id),
        ).fetchone()
        if not search:
            return "Поисковый запрос не найден.", kb([[btn("Назад", f"user:{target_id}:{return_page}", emoji="home")], BACK_HOME])
        query = str(search[0])
        pattern = f"%{query}%"
        where = " AND (m.content LIKE ? OR m.author LIKE ? OR m.reply_to_content LIKE ? OR CAST(m.chat_id AS TEXT)=? OR CAST(m.user_id AS TEXT)=?)"
        params = (target_id, target_id, pattern, pattern, pattern, query, query)
        total = int(conn.execute("SELECT COUNT(*) " + OWNER_MESSAGES_FROM + where, params).fetchone()[0])
        page_count = max(1, (total + ADMIN_MESSAGES_PAGE_SIZE - 1) // ADMIN_MESSAGES_PAGE_SIZE)
        page_number = min(max(0, page_number), page_count - 1)
        found = conn.execute(
            "SELECT m.chat_id,m.message_id,m.author,m.content,m.updated_at " + OWNER_MESSAGES_FROM + where +
            " ORDER BY m.updated_at DESC LIMIT ? OFFSET ?",
            (*params, ADMIN_MESSAGES_PAGE_SIZE, page_number * ADMIN_MESSAGES_PAGE_SIZE),
        ).fetchall()
    lines = [
        f"<b>{html_text(chat_participant_label(author))}</b> · {format_display_time(updated_at)}\n"
        f"{html_quote((content or '[без текста]')[:220])}"
        for chat_id, message_id, author, content, updated_at in found
    ]
    text = (
        f"{tg_icon('search')} <b>Найдено сообщений: {total}</b>\n\n"
        f"{tg_icon('memo')} Запрос: <code>{html_text(query)}</code>\n\n"
        + ("\n\n".join(lines) if lines else f"{tg_icon('warning')} Совпадений нет.")
    )
    rows = [[btn(f"Открыть · {chat_participant_label(author)[:28]}", f"uchat:{target_id}:{chat_id}:{return_page}:0", emoji="view")]
            for chat_id, _message_id, author, _content, _updated_at in found]
    navigation = []
    if page_number > 0:
        navigation.append(btn("← Пред.", f"usres:{target_id}:{return_page}:{page_number - 1}", emoji="back"))
    if page_number + 1 < page_count:
        navigation.append(btn("След. →", f"usres:{target_id}:{return_page}:{page_number + 1}", emoji="view"))
    if navigation:
        rows.append(navigation)
    rows.extend([
        [btn("Новый поиск", f"usearch:{target_id}:{return_page}", emoji="refresh")],
        [btn("Назад", f"user:{target_id}:{return_page}", emoji="back")],
        [btn("Главное меню", "home", emoji="home")],
    ])
    log_admin_view(admin_id, target_id, None, "поиск сообщений", query)
    return text, kb(rows)


def page_chat_privacy(user_id: int) -> tuple[str, dict]:
    if not is_owner_admin(user_id):
        return "Только для владельца.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        extra = conn.execute(
            "SELECT target_id,reason FROM hidden_chat_users ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    owners = ", ".join(f"<code>{uid}</code>" for uid in sorted(ADMIN_USER_IDS)) or "—"
    delegated = ", ".join(f"<code>{uid}</code>" for uid, _, _ in list_delegated_admins()) or "—"
    extra_lines = "\n".join(f"• <code>{uid}</code>{f' — {html_text(reason)}' if reason else ''}" for uid, reason in extra) or "• нет"
    text = (
        f"{pe('warning')} <b>Приватность чатов</b>\n\n"
        f"Чаты владельцев всегда скрыты: {owners}\n"
        f"Чаты администраторов всегда скрыты: {delegated}\n\n"
        f"<b>Дополнительно скрыты:</b>\n{extra_lines}"
    )
    rows = [[btn("Скрыть ещё пользователя", "privacy:add", emoji="add")]]
    rows.extend([[btn(f"Вернуть · {uid}", f"privacy:remove:{uid}", emoji="refresh")] for uid, _ in extra[:15]])
    rows.extend([BACK_PANEL])
    return text, kb(rows)


def page_digest_settings(user_id: int) -> tuple[str, dict]:
    if not is_owner_admin(user_id):
        return "Только для владельца.", kb([BACK_HOME])
    states = []
    with sqlite3.connect(DB_PATH) as conn:
        for recipient_id in sorted(MESSAGE_DIGEST_RECIPIENT_IDS):
            row = conn.execute(
                "SELECT enabled FROM digest_settings WHERE recipient_id=? AND target_id=?",
                (recipient_id, MESSAGE_DIGEST_TARGET_USER_ID),
            ).fetchone()
            states.append((recipient_id, True if row is None else bool(row[0])))
    lines = [f"• <code>{recipient}</code>: <b>{'включён' if enabled else 'выключен'}</b>" for recipient, enabled in states]
    text = (
        f"{pe('history')} <b>Пятичасовой отчёт</b>\n\n"
        f"Пользователь: <code>{MESSAGE_DIGEST_TARGET_USER_ID}</code>\n"
        "Интервал: <b>5 часов</b>\n\n" + "\n".join(lines)
    )
    rows = [[btn(
        f"{'Отключить' if enabled else 'Включить'} · {recipient}",
        f"digset:{recipient}:{MESSAGE_DIGEST_TARGET_USER_ID}",
        emoji="warning" if enabled else "check",
    )] for recipient, enabled in states]
    rows.extend([BACK_PANEL])
    return text, kb(rows)


def page_view_audit(user_id: int) -> tuple[str, dict]:
    if not is_owner_admin(user_id):
        return "Только для владельца.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT admin_id,target_id,chat_id,action,details,created_at FROM admin_view_log ORDER BY id DESC LIMIT 20"
        ).fetchall()
    lines = [
        f"{format_display_time(created_at, '%d.%m %H:%M')} · <code>{admin_id}</code> · {html_text(action)} · пользователь {journal_user_ref(target_id)}"
        f"{f' · чат <code>{chat_id}</code>' if chat_id is not None else ''}{f' · {html_text(str(details)[:80])}' if details else ''}"
        for admin_id, target_id, chat_id, action, details, created_at in rows
    ]
    return f"{pe('history')} <b>Просмотры чатов</b>\n\n" + ("\n".join(lines) if lines else "Просмотров пока нет."), kb([[btn("Обновить", "viewaudit", emoji="refresh")], [btn("Админ-панель", "panel", emoji="home")], BACK_HOME])


def storage_setting_int(key: str, default: int = 0) -> int:
    try:
        return int(maintenance_get(key) or default)
    except (TypeError, ValueError, sqlite3.Error):
        return default


def page_storage_settings(user_id: int) -> tuple[str, dict]:
    if not is_owner_admin(user_id):
        return "Только для владельца.", kb([BACK_HOME])
    regular = storage_setting_int("retention_regular_days", 0)
    deleted = storage_setting_int("retention_deleted_days", 0)
    media_ttl = storage_setting_int("admin_media_ttl", DEFAULT_ADMIN_MEDIA_TTL_SEC)
    label = lambda value: "бессрочно" if value == 0 else f"{value} дней"
    text = (
        f"{pe('admin')} <b>Хранение и медиа</b>\n\n"
        f"Обычные сообщения: <b>{label(regular)}</b>\n"
        f"Удалённые сообщения: <b>{label(deleted)}</b>\n"
        f"Просмотренное медиа удаляется из админского диалога через: <b>{'никогда' if media_ttl == 0 else str(media_ttl // 60) + ' мин.'}</b>\n\n"
        "Архивные файлы удаляются только после выбора срока хранения."
    )
    rows = [
        [btn("Обычные: 30", "retain:r:30"), btn("90", "retain:r:90"), btn("180", "retain:r:180"), btn("∞", "retain:r:0")],
        [btn("Удалённые: 30", "retain:d:30"), btn("90", "retain:d:90"), btn("180", "retain:d:180"), btn("∞", "retain:d:0")],
        [btn("Медиа: 5 мин", "retain:m:300"), btn("15 мин", "retain:m:900"), btn("60 мин", "retain:m:3600"), btn("∞", "retain:m:0")],
        [btn("Админ-панель", "panel", emoji="home")],
        BACK_HOME,
    ]
    return text, kb(rows)


TELEGRAM_EXPORT_CSS = """
:root{color-scheme:dark;--bg:#0e1621;--panel:#17212b;--panel2:#202b36;--line:#0b141d;--text:#f5f7fa;--muted:#8193a5;--accent:#5aa7e8;--selected:#2b5278;--in:#182533;--out:#2b5278}
*{box-sizing:border-box}html,body{height:100%;margin:0}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:var(--bg);color:var(--text)}a{color:inherit}.shell{min-height:100%;display:grid;grid-template-columns:360px 1fr}.sidebar{min-width:0;background:var(--panel);border-right:1px solid var(--line)}.side-head,.chat-head{height:64px;display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}.side-head{justify-content:space-between}.side-head h1{font-size:18px;margin:0}.mode{color:var(--muted);font-size:11px}.owner{display:flex;align-items:center;gap:10px;padding:14px 16px}.avatar{width:44px;height:44px;display:grid;place-items:center;flex:0 0 auto;border-radius:50%;background:linear-gradient(145deg,#69b8f4,#387dba);font-weight:750}.owner b,.owner span{display:block}.owner span,.chat-meta{color:var(--muted);font-size:12px;margin-top:2px}.search{height:38px;margin:0 12px 10px;padding:0 12px;display:flex;align-items:center;border-radius:8px;background:var(--panel2);color:var(--muted);font-size:13px}.chat-list{padding-bottom:20px}.chat-row{min-height:70px;display:flex;align-items:center;gap:11px;padding:9px 13px;text-decoration:none}.chat-row:hover,.chat-row.active{background:var(--selected)}.chat-row .body{min-width:0;flex:1}.line{display:flex;justify-content:space-between;gap:10px}.name{font-size:14px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.time{flex:0 0 auto;color:#9fb0c0;font-size:11px}.preview{display:flex;justify-content:space-between;gap:8px;margin-top:5px;color:var(--muted);font-size:12px}.preview span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.count{min-width:21px;height:21px;display:grid;place-items:center;padding:0 6px;border-radius:11px;background:var(--accent);color:white;font-size:10px}.conversation{min-width:0;display:flex;min-height:100vh;flex-direction:column}.chat-head{box-shadow:0 1px 4px #0004}.chat-head .back{display:none;color:var(--accent);text-decoration:none;font-size:24px}.chat-title{min-width:0}.chat-title b,.chat-title span{display:block}.messages{flex:1;padding:20px clamp(14px,6vw,88px);background-color:var(--bg);background-image:linear-gradient(#0e1621ed,#0e1621ed),radial-gradient(circle at 12px 12px,#7a90a516 1px,transparent 1.5px);background-size:auto,28px 28px}.day{width:max-content;margin:5px auto 15px;padding:5px 11px;border-radius:13px;background:#1b2b39e8;color:#d6e1ea;font-size:11px;font-weight:650}.message{display:flex;margin:4px 0}.message.out{justify-content:flex-end}.bubble{position:relative;max-width:min(76%,680px);padding:7px 9px 5px;border-radius:10px 10px 10px 3px;background:var(--in);box-shadow:0 1px 2px #0005}.bubble:before{content:"";position:absolute;left:-7px;bottom:0;border-width:0 8px 8px 0;border-style:solid;border-color:transparent var(--in) transparent transparent}.out .bubble{border-radius:10px 10px 3px 10px;background:var(--out)}.out .bubble:before{left:auto;right:-7px;border-width:0 0 8px 8px;border-color:transparent transparent transparent var(--out)}.author{color:#68b5f2;font-size:12px;font-weight:700;margin-bottom:4px}.reply{margin-bottom:6px;padding:5px 8px;border-left:3px solid #64b5f6;border-radius:3px;background:#08121b55;font-size:11px}.reply b,.reply span{display:block}.reply b{color:#7fc4f8}.reply span{color:#cbd7e0;margin-top:2px}.text{font-size:14px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere}.meta{display:flex;justify-content:flex-end;gap:4px;margin:3px 0 0 15px;color:#aec0cf;font-size:9px}.out .meta{color:#c8e2f6}.checks{color:#72c4ff;letter-spacing:-3px;padding-right:3px}.deleted{opacity:.68;font-style:italic}.media{margin:-3px -5px 6px;overflow:hidden;border-radius:7px}.media img,.media video{display:block;max-width:100%;width:100%;max-height:520px;object-fit:contain;background:#091019}.media video.round{width:220px;height:220px;border-radius:50%;object-fit:cover;border:3px solid #64b5f6}.media audio{display:block;width:min(350px,70vw);height:42px}.media.sticker img,.media.sticker video{width:190px;height:190px;object-fit:contain;background:transparent}.file,.media-missing{display:flex;align-items:center;gap:9px;min-width:240px;padding:9px;border-radius:8px;background:#07111a55;text-decoration:none}.file-icon{width:38px;height:38px;display:grid;place-items:center;border-radius:50%;background:var(--accent);font-weight:800}.media-missing{color:#9eb0c0;font-size:12px}.summary{padding:30px;color:var(--muted);text-align:center}.summary b{display:block;color:var(--text);font-size:22px;margin-bottom:8px}
@media(max-width:760px){.shell{display:block}.sidebar{min-height:100vh;border:0}.conversation .chat-head .back{display:block}.conversation .messages{padding:14px 10px}.bubble{max-width:88%}.chat-page .sidebar{display:none}.index-page .conversation{display:none}}
"""


def _export_initials(value: object) -> str:
    parts = re.findall(r"[\wА-Яа-яЁё]+", chat_participant_label(value))
    return "".join(part[0] for part in parts[:2]).upper() or "?"


def _export_day(timestamp: int) -> str:
    value = datetime.fromtimestamp(int(timestamp), DISPLAY_TIMEZONE)
    months = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
    return f"{value.day} {months[value.month - 1]}{f' {value.year}' if value.year != datetime.now(DISPLAY_TIMEZONE).year else ''}"


def _export_media_markup(media_type: str | None, archive_name: str | None, file_name: str = "") -> str:
    label = html.escape(MEDIA_LABELS.get(media_type, media_type or "медиа"))
    if not archive_name:
        return f'<div class="media-missing"><span class="file-icon">×</span><span>{label} не включено в этот экспорт</span></div>' if media_type else ""
    href = html.escape(archive_name, quote=True)
    suffix = Path(file_name).suffix.lower()
    if media_type == "photo":
        return f'<div class="media"><a href="{href}"><img src="{href}" alt="{label}" loading="lazy"></a></div>'
    if media_type in {"video", "animation"}:
        return f'<div class="media"><video src="{href}" controls preload="metadata"></video></div>'
    if media_type == "video_note":
        return f'<div class="media"><video class="round" src="{href}" controls preload="metadata"></video></div>'
    if media_type in {"voice", "audio"}:
        return f'<div class="media"><audio src="{href}" controls preload="metadata"></audio></div>'
    if media_type == "sticker" and suffix == ".webm":
        return f'<div class="media sticker"><video src="{href}" autoplay loop muted playsinline></video></div>'
    if media_type == "sticker" and suffix in {".webp", ".png", ".jpg", ".jpeg", ".gif"}:
        return f'<div class="media sticker"><img src="{href}" alt="Стикер" loading="lazy"></div>'
    return f'<a class="file" href="{href}"><span class="file-icon">↓</span><span>{html.escape(file_name or label)}</span></a>'


def export_chat_html(
    admin_id: int,
    target_id: int,
    chat_id: int,
    audit: bool = True,
    include_media: bool = True,
    media_limit_bytes: int = 45 * 1024 * 1024,
    back_href: str = "",
) -> Path:
    if not can_view_user_chats(admin_id) or user_chats_are_hidden(target_id):
        raise PermissionError("Чат недоступен")
    export_dir = DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = export_dir / f"chat-{target_id}-{chat_id}-{stamp}.zip"
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT m.message_id,m.author,m.content,m.media_type,m.local_media_path,m.user_id,
                   m.created_at,m.updated_at,m.deleted_at,m.reply_to_author,m.reply_to_content,m.edit_count
            """ + OWNER_MESSAGES_FROM + " AND m.chat_id=? ORDER BY m.updated_at",
            (target_id, target_id, chat_id),
        ).fetchall()
    blocks = []
    attachments: list[tuple[Path, str]] = []
    total_attachment_bytes = 0
    last_day = ""
    participant = next((chat_participant_label(row[1]) for row in rows if not same_user_id(row[5], target_id)), f"Чат {chat_id}")
    for message_id, author, content, media_type, local_path, message_user_id, created_at, updated_at, deleted_at, reply_author, reply_content, edit_count in rows:
        archive_name = None
        candidate_name = ""
        if local_path and include_media:
            candidate = Path(str(local_path))
            if candidate.exists() and candidate.is_file() and total_attachment_bytes + candidate.stat().st_size <= media_limit_bytes:
                archive_name = f"media/{message_id}-{candidate.name}"
                candidate_name = candidate.name
                attachments.append((candidate, archive_name))
                total_attachment_bytes += candidate.stat().st_size
        day = _export_day(updated_at)
        if day != last_day:
            blocks.append(f'<div class="day">{html.escape(day)}</div>')
            last_day = day
        reply_html = f'<div class="reply"><b>{html.escape(str(reply_author or "Ответ"))}</b><span>{html.escape(str(reply_content or "Сообщение"))}</span></div>' if reply_content else ""
        flags = []
        if deleted_at:
            flags.append("удалено")
        if edit_count:
            flags.append(f"изменено {edit_count} раз")
        outgoing = same_user_id(message_user_id, target_id)
        media_html = _export_media_markup(media_type, archive_name, candidate_name or str(local_path or ""))
        text_html = f'<div class="text">{html.escape(str(content))}</div>' if content and not str(content).startswith("[") else ""
        author_html = "" if outgoing else f'<div class="author">{html.escape(str(author or participant))}</div>'
        checks_html = '<span class="checks">✓✓</span>' if outgoing else ""
        direction_class = "out" if outgoing else "in"
        deleted_class = "deleted" if deleted_at else ""
        blocks.append(f'<div class="message {"out" if outgoing else "in"}"><article class="bubble {"deleted" if deleted_at else ""}">'
                      f'{author_html}'
                      f'{reply_html}{media_html}{text_html}'
                      f'<div class="meta">{" · ".join(flags)} <time>{html.escape(format_display_time(updated_at, "%H:%M"))}</time>{checks_html}</div>'
                      f'</article></div>')
    back_link = f'<a class="back" href="{html.escape(back_href, quote=True)}">‹</a>' if back_href else ""
    empty_messages = '<div class="summary"><b>Сообщений нет</b>В этом диалоге архив пуст.</div>'
    messages_html = "".join(blocks) if blocks else empty_messages
    document = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(participant)} — экспорт чата</title><style>{TELEGRAM_EXPORT_CSS}</style></head>'
        f'<body class="chat-page"><main class="conversation"><header class="chat-head">{back_link}<div class="avatar">{html.escape(_export_initials(participant))}</div>'
        f'<div class="chat-title"><b>{html.escape(participant)}</b><span class="chat-meta">{len(rows)} сообщений · {"с медиа" if include_media else "без медиа"}</span></div></header>'
        f'<section class="messages">{messages_html}</section></main></body></html>'
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("chat.html", document)
        for source, archive_name in attachments:
            archive.write(source, archive_name)
    if audit:
        log_admin_view(admin_id, target_id, chat_id, "экспорт диалога", path.name)
    return path


def export_all_user_chats(admin_id: int, target_id: int, include_media: bool = False) -> Path:
    if not can_view_user_chats(admin_id) or user_chats_are_hidden(target_id):
        raise PermissionError("Чаты недоступны")
    export_dir = DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    mode = "with-media" if include_media else "text-only"
    path = export_dir / f"all-chats-{mode}-{target_id}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with sqlite3.connect(DB_PATH) as conn:
        chats = conn.execute(
            """
            SELECT m.chat_id, COUNT(*),
                   COALESCE(MAX(CASE WHEN m.user_id != ? THEN m.author END), MAX(m.author)),
                   MAX(m.updated_at), COALESCE(MAX(m.content), '')
            """ + OWNER_MESSAGES_FROM + """
            GROUP BY m.chat_id ORDER BY MAX(m.updated_at) DESC
            """,
            (target_id, target_id, target_id),
        ).fetchall()
        owner_row = conn.execute(
            "SELECT first_name,last_name,username FROM users WHERE user_id=?", (target_id,)
        ).fetchone()

    owner_name = " ".join(str(part).strip() for part in (owner_row or ())[:2] if part).strip()
    if not owner_name and owner_row and owner_row[2]:
        owner_name = f"@{owner_row[2]}"
    owner_name = owner_name or f"Пользователь {target_id}"

    index_rows = []
    included_media_bytes = 0
    media_limit = 45 * 1024 * 1024
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as destination:
        for index, (chat_id, message_count, author, updated_at, preview) in enumerate(chats):
            folder = f"chats/chat_{chat_id}"
            remaining_media_bytes = max(0, media_limit - included_media_bytes)
            chat_archive = export_chat_html(
                admin_id,
                target_id,
                int(chat_id),
                audit=False,
                include_media=include_media,
                media_limit_bytes=remaining_media_bytes,
                back_href="../../index.html",
            )
            try:
                with zipfile.ZipFile(chat_archive, "r") as source:
                    for info in source.infolist():
                        if info.is_dir():
                            continue
                        if info.filename.startswith("media/"):
                            if included_media_bytes + info.file_size > media_limit:
                                continue
                            included_media_bytes += info.file_size
                        destination.writestr(f"{folder}/{info.filename}", source.read(info.filename))
            finally:
                chat_archive.unlink(missing_ok=True)
            label = chat_participant_label(author)
            safe_preview = str(preview or "Без текста")
            if safe_preview.startswith("["):
                safe_preview = MEDIA_LABELS.get(safe_preview.strip("[]"), safe_preview.strip("[]").capitalize())
            index_rows.append(
                f'<a class="chat-row{" active" if index == 0 else ""}" href="{folder}/chat.html">'
                f'<span class="avatar">{html.escape(_export_initials(label))}</span><span class="body">'
                f'<span class="line"><span class="name">{html.escape(label)}</span><span class="time">{html.escape(format_display_time(updated_at, "%d.%m %H:%M"))}</span></span>'
                f'<span class="preview"><span>{html.escape(safe_preview[:90])}</span><span class="count">{int(message_count)}</span></span></span></a>'
            )
        index_document = (
            '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Экспорт чатов — {html.escape(owner_name)}</title><style>{TELEGRAM_EXPORT_CSS}</style></head>'
            f'<body class="index-page"><div class="shell"><aside class="sidebar"><header class="side-head"><h1>Экспорт чатов</h1>'
            f'<span class="mode">{"С медиа" if include_media else "Без медиа"}</span></header>'
            f'<div class="owner"><span class="avatar">{html.escape(_export_initials(owner_name))}</span><div><b>{html.escape(owner_name)}</b><span>{len(chats)} чатов</span></div></div>'
            f'<div class="search">Поиск недоступен в офлайн-архиве</div><nav class="chat-list">{"".join(index_rows)}</nav></aside>'
            f'<section class="conversation"><header class="chat-head"><div class="avatar">{html.escape(_export_initials(owner_name))}</div><div class="chat-title"><b>{html.escape(owner_name)}</b><span class="chat-meta">Экспорт переписок</span></div></header>'
            f'<div class="messages"><div class="summary"><b>Выберите диалог</b>Откройте любой чат в списке слева.</div></div></section></div></body></html>'
        )
        destination.writestr("index.html", index_document)
    log_admin_view(admin_id, target_id, None, "экспорт всех чатов", path.name)
    return path


def page_stats(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для админов.", kb([BACK_HOME])
    now = int(time.time())
    day_start = now - (now % 86400)
    with sqlite3.connect(DB_PATH) as conn:
        total_users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        business_users = int(conn.execute("SELECT COUNT(DISTINCT owner_id) FROM business_connections WHERE owner_id IS NOT NULL AND is_enabled=1").fetchone()[0])
        stored_chats = int(conn.execute("SELECT COUNT(DISTINCT chat_id) FROM messages").fetchone()[0])
        stored_messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        periods = []
        for label, seconds in (("24 часа", 86400), ("7 дней", 7 * 86400), ("30 дней", 30 * 86400)):
            count = int(conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (now - seconds,)).fetchone()[0])
            periods.append(f"Новые за {label}: <b>{count}</b>")
        daily = []
        for ago in range(6, -1, -1):
            start = day_start - ago * 86400
            users_count = int(conn.execute(
                "SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?",
                (start, start + 86400),
            ).fetchone()[0])
            daily.append((start, users_count))
    peak = max([count for _, count in daily] + [1])
    chart = "\n".join(
        f"{time.strftime('%d.%m', time.localtime(ts))}  {'█' * max(1, round(count / peak * 10)) if count else '·'} {count}"
        for ts, count in daily
    )
    text = (
        f"{pe('admin')} <b>Статистика</b>\n\n" + "\n".join(periods) +
        f"\nВсего пользователей: <b>{total_users}</b>\n"
        f"Активных Business-подключений: <b>{business_users}</b>\n"
        f"Сохранённых чатов: <b>{stored_chats}</b>\n"
        f"Сохранённых сообщений: <b>{stored_messages}</b>\n"
        f"Доступ: <b>бесплатный для всех</b>\n\n"
        f"<b>Новые пользователи за 7 дней</b>\n<code>{chart}</code>"
    )
    return text, kb([
        [btn("Обновить", "stats", emoji="refresh")],
        BACK_PANEL,
    ])

def page_promos(user_id: int) -> tuple[str, dict]:
    return (
        f"{pe('check')} <b>HolyGram бесплатный</b>\n\nПромокоды на подписку больше не используются.",
        kb([BACK_PANEL]),
    )

def clear_admin_logs(owner_id: int) -> tuple[int, int]:
    if not is_owner_admin(owner_id):
        raise PermissionError("Только владелец может очищать журналы.")
    with sqlite3.connect(DB_PATH) as conn:
        actions = int(conn.execute("SELECT COUNT(*) FROM admin_actions").fetchone()[0])
        views = int(conn.execute("SELECT COUNT(*) FROM admin_view_log").fetchone()[0])
        conn.execute("DELETE FROM admin_actions")
        conn.execute("DELETE FROM admin_view_log")
    # Keep one explicit audit marker so it is always visible who cleared the journals.
    audit_admin(owner_id, "очистка журналов", None, f"удалено действий: {actions}, просмотров: {views}")
    log(f"Admin journals cleared by owner {owner_id}: actions={actions}, views={views}")
    return actions, views


def page_clear_logs_confirm(user_id: int) -> tuple[str, dict]:
    if not is_owner_admin(user_id):
        return "Только для владельца.", kb([BACK_HOME])
    return (
        f"{pe('warning')} <b>Очистить журналы администраторов?</b>\n\n"
        "Будут удалены записи о действиях и просмотрах чатов. "
        "После очистки останется только запись о том, кто выполнил очистку.",
        kb([
            [btn("Да, очистить", "logs:clear:confirm", emoji="warning", style="danger")],
            [btn("Отмена", "admins", emoji="back")],
            BACK_HOME,
        ]),
    )


ADMIN_JOURNAL_PAGE_SIZE = 8
ADMIN_JOURNAL_FILTERS = {
    "all": "Все",
    "edits": "Изменения",
    "views": "Просмотры",
    "checks": "Проверки",
    "actions": "Действия",
}


def _journal_category(kind: str, action: str) -> str:
    action_lower = str(action or "").lower()
    if "редакт" in action_lower or "изменение сообщения" in action_lower:
        return "edits"
    if "открыл просмотр чатов" in action_lower:
        return "views"
    if kind == "view" and (
        "поиск" in action_lower
        or "провер" in action_lower
        or "экспорт" in action_lower
    ):
        return "checks"
    if kind == "view":
        return "views"
    return "actions"


def _journal_profiles(user_ids: set[int]) -> dict[int, tuple[str, str, str]]:
    clean_ids = sorted({int(uid) for uid in user_ids if uid})
    if not clean_ids:
        return {}
    placeholders = ",".join("?" for _ in clean_ids)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT user_id,first_name,last_name,username FROM users WHERE user_id IN ({placeholders})",
            clean_ids,
        ).fetchall()
    return {
        int(uid): (str(first or ""), str(last or ""), str(username or ""))
        for uid, first, last, username in rows
    }


def _journal_identity(user_id: int | None, profiles: dict[int, tuple[str, str, str]]) -> str:
    if not user_id:
        return "—"
    uid = int(user_id)
    first, last, username = profiles.get(uid, ("", "", ""))
    name = " ".join(part for part in (first, last) if part).strip()
    pieces: list[str] = []
    if username:
        pieces.append(f"<b>@{html_text(username.lstrip('@'))}</b>")
    elif name:
        pieces.append(f"<b>{html_text(name)}</b>")
    else:
        pieces.append("<b>Без username</b>")
    if name and username:
        pieces.append(html_text(name))
    pieces.append(f"ID <code>{uid}</code>")
    return " · ".join(pieces)


def _load_admin_journal(filter_key: str) -> list[tuple[str, int, str, int | None, int | None, str, int]]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT kind,admin_id,action,target_id,chat_id,COALESCE(details,''),created_at
            FROM (
                SELECT 'action' AS kind,admin_id,action,target_id,NULL AS chat_id,details,created_at,id AS source_id
                FROM admin_actions
                UNION ALL
                SELECT 'view' AS kind,admin_id,action,target_id,chat_id,details,created_at,id AS source_id
                FROM admin_view_log
            )
            ORDER BY created_at DESC, source_id DESC
            LIMIT 1000
            """
        ).fetchall()

    result: list[tuple[str, int, str, int | None, int | None, str, int]] = []
    for kind, admin_id, action, target_id, chat_id, details, created_at in rows:
        kind = str(kind)
        action = str(action or "")
        category = _journal_category(kind, action)

        # Web App edits are written both to the view log and to admin_actions.
        # Show the action copy only, otherwise one edit appears twice.
        if kind == "view" and category == "edits":
            continue
        if filter_key != "all" and category != filter_key:
            continue
        result.append(
            (
                kind,
                int(admin_id),
                action,
                int(target_id) if target_id is not None else None,
                int(chat_id) if chat_id is not None else None,
                str(details or ""),
                int(created_at),
            )
        )
    return result


def page_admin_log(user_id: int, filter_key: str = "all", page_number: int = 0) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для админов.", kb([BACK_HOME])
    if filter_key not in ADMIN_JOURNAL_FILTERS:
        filter_key = "all"

    all_rows = _load_admin_journal(filter_key)
    page_count = max(1, (len(all_rows) + ADMIN_JOURNAL_PAGE_SIZE - 1) // ADMIN_JOURNAL_PAGE_SIZE)
    page_number = min(max(0, int(page_number)), page_count - 1)
    start_index = page_number * ADMIN_JOURNAL_PAGE_SIZE
    rows = all_rows[start_index : start_index + ADMIN_JOURNAL_PAGE_SIZE]

    ids: set[int] = set()
    for _kind, admin_id, _action, target_id, _chat_id, _details, _created_at in rows:
        ids.add(admin_id)
        if target_id:
            ids.add(target_id)
    profiles = _journal_profiles(ids)

    lines: list[str] = []
    for kind, admin_id, action, target_id, chat_id, details, created_at in rows:
        category = _journal_category(kind, action)
        icon = {
            "edits": "✏️",
            "views": "👁",
            "checks": "🔎",
            "actions": "⚙️",
        }.get(category, "•")
        lines.append(
            f"{icon} <b>{html_text(action)}</b>\n"
            f"👤 {_journal_identity(admin_id, profiles)}\n"
            f"🕒 {format_display_time(created_at, '%d.%m.%Y %H:%M')}"
            + (f"\n🎯 Пользователь: {_journal_identity(target_id, profiles)}" if target_id else "")
            + (f"\n💬 Чат: <code>{chat_id}</code>" if chat_id is not None else "")
            + (f"\nℹ️ {html_text(details[:140])}" if details else "")
        )

    body = "\n\n".join(lines) if lines else "В этой категории записей пока нет."
    title = ADMIN_JOURNAL_FILTERS[filter_key]
    text = (
        f"{pe('admin')} <b>Журнал действий · {html_text(title)}</b>\n"
        f"Записей: <b>{len(all_rows)}</b> · страница <b>{page_number + 1}/{page_count}</b>\n\n"
        f"{body}"
    )

    filter_rows = [
        [
            btn(("✓ " if filter_key == "all" else "") + "Все", "audit:all:0"),
            btn(("✓ " if filter_key == "edits" else "") + "Изменения", "audit:edits:0"),
        ],
        [
            btn(("✓ " if filter_key == "views" else "") + "Просмотры", "audit:views:0"),
            btn(("✓ " if filter_key == "checks" else "") + "Проверки", "audit:checks:0"),
        ],
        [btn(("✓ " if filter_key == "actions" else "") + "Действия", "audit:actions:0")],
    ]

    navigation: list[dict] = []
    if page_number > 0:
        navigation.append(btn("← Назад", f"audit:{filter_key}:{page_number - 1}", emoji="back"))
    if page_number + 1 < page_count:
        navigation.append(btn("Вперёд →", f"audit:{filter_key}:{page_number + 1}", emoji="view"))
    if navigation:
        filter_rows.append(navigation)

    filter_rows.extend([
        [btn("Обновить", f"audit:{filter_key}:{page_number}", emoji="refresh")],
        [btn("Админ-панель", "panel", emoji="home")],
    ])
    return text, kb(filter_rows)


def page_gift_buy(payer_id: int, target_id: int) -> tuple[str, dict]:
    plans = get_plans()
    target = stored_user_payment_label(target_id)
    rows = []
    for days, prices in plans.items():
        rub, _ = discounted_price(payer_id, prices["rub"])
        stars, _ = discounted_price(payer_id, prices["stars"])
        rows.append([btn(f"{days} дн. · {rub} ₽", f"gift:sbp:{target_id}:{days}", emoji="pay"), btn(f"{stars} ⭐", f"gift:stars:{target_id}:{days}", emoji="stars")])
    rows.extend([[btn("Другой получатель", "gift:start", emoji="gift")], [btn("К тарифам", "buy", emoji="home")], BACK_HOME])
    return f"{pe('gift')} <b>Подарочная подписка</b>\n\nПолучатель:\n{target}\n\nВыбери тариф и способ оплаты.", kb(rows)


def page_panel(user_id: int) -> tuple[str, dict]:
    with sqlite3.connect(DB_PATH) as conn:
        users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        open_tickets = int(conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0])

    role = (
        "Владелец" if is_owner_admin(user_id)
        else "Администратор чатов" if can_view_user_chats(user_id)
        else "Администратор"
    )
    viewing = "включён" if chat_viewing_enabled() else "выключен"

    text = (
        f"{pe('admin')} <b>HolyGram · Админ-панель</b>\n\n"
        f"Роль: <b>{role}</b>\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Открытых обращений: <b>{open_tickets}</b>\n"
        f"Просмотр чатов: <b>{viewing}</b>\n\n"
        "Выбери раздел:"
    )

    rows = [
        [
            btn("Пользователи", "users:0", emoji="view"),
            btn("Обращения", "tickets", emoji="support"),
        ],
        [
            btn("Статистика", "stats", emoji="history"),
            btn("Рассылка", "broadcast", emoji="support"),
        ],
    ]

    if can_view_user_chats(user_id):
        rows.append([btn("Открыть чаты", web_app=WEBAPP_URL, emoji="view", style="primary")])

    rows.extend([
        [
            btn("Экспорт CSV", "export", emoji="view"),
            btn("Диагностика", "health", emoji="check"),
        ],
        [btn("Журнал действий", "audit", emoji="history")],
    ])

    if is_owner_admin(user_id):
        rows.extend([
            [
                btn("Администраторы", "admins", emoji="admin"),
                btn("Приватность", "privacy", emoji="warning"),
            ],
            [btn(
                "Выключить просмотр чатов" if chat_viewing_enabled() else "Включить просмотр чатов",
                "chats:toggle",
                emoji="warning" if chat_viewing_enabled() else "check",
                style="danger" if chat_viewing_enabled() else "success",
            )],
            [
                btn("История просмотров", "viewaudit", emoji="history"),
                btn("Хранение", "storage", emoji="admin"),
            ],
            [btn("Резервная копия", "backup", emoji="refresh")],
        ])

    rows.extend([
        [
            btn("Обновить", "panel", emoji="refresh"),
            btn("Назад", "home", emoji="back"),
        ],
    ])
    return text, kb(rows)

def page_admins(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для администраторов.", kb([BACK_HOME])

    delegated = list_delegated_admins()
    owner_lines = [f"• <code>{uid}</code> — владелец" for uid in sorted(ADMIN_USER_IDS)]
    viewer_lines = [f"• <code>{uid}</code> — администратор чатов" for uid in sorted(list_chat_viewer_ids())]
    admin_lines = [
        f"• <code>{uid}</code> — добавил <code>{added_by}</code>, "
        f"{time.strftime('%d.%m.%Y', time.localtime(created_at))}"
        for uid, added_by, created_at in delegated
    ]
    body = "\n".join(owner_lines + viewer_lines + admin_lines) or "Администраторов пока нет."
    text = (
        f"{pe('admin')} <b>Администраторы</b>\n\n{body}\n\n"
        "Администраторы получают доступ к пользователям, поддержке, статистике и служебным разделам. "
        "Выдавать и снимать админку может только владелец."
    )

    rows: list[list[dict]] = []
    if is_owner_admin(user_id):
        rows.append([btn("Добавить админа", "admin:add", emoji="add", style="success")])
        rows.append([
            btn("Выдать просмотр чатов", "chatview:add", emoji="view", style="success"),
            btn("Забрать просмотр чатов", "chatview:remove", emoji="warning", style="danger"),
        ])
        rows.append([btn("Очистить логи", "logs:clear", emoji="warning", style="danger")])
        for uid, _, _ in delegated[:20]:
            rows.append([btn(f"Удалить админа · {uid}", f"admin:remove:{uid}", emoji="warning", style="danger")])
    rows.extend([BACK_PANEL])
    return text, kb(rows)


def page_support_tickets(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для администраторов.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        open_count = int(conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0])
        rows = conn.execute(
            "SELECT id, user_id, user_text, created_at FROM support_tickets "
            "WHERE status='open' ORDER BY id DESC LIMIT 12"
        ).fetchall()

    lines = [
        f"<b>#{ticket_id}</b> · <code>{target_id}</code> · "
        f"{time.strftime('%d.%m %H:%M', time.localtime(created_at))}\n"
        f"{html_text(text[:180])}{'…' if len(text) > 180 else ''}"
        for ticket_id, target_id, text, created_at in rows
    ]
    text = (
        f"{pe('support')} <b>Обращения поддержки</b>\n"
        f"Открытых: <b>{open_count}</b>\n\n"
        + ("\n\n".join(lines) if lines else "Новых обращений нет.")
    )
    buttons = [
        [btn(f"Ответить на #{ticket_id}", f"support:reply:{ticket_id}:{target_id}", emoji="support")]
        for ticket_id, target_id, _, _ in rows[:8]
    ]
    buttons.extend([
        [btn("Обновить", "tickets", emoji="refresh")],
        BACK_PANEL,
    ])
    return text, kb(buttons)


def page_expiring_subscriptions(user_id: int) -> tuple[str, dict]:
    return (
        f"{pe('check')} <b>HolyGram бесплатный</b>\n\nСроков подписки больше нет.",
        kb([BACK_PANEL]),
    )

def page_prices(user_id: int) -> tuple[str, dict]:
    return (
        f"{pe('check')} <b>HolyGram бесплатный</b>\n\nТарифы и цены отключены.",
        kb([BACK_PANEL]),
    )


# --- транспорт для кнопок ------------------------------------------------------

def answer_callback(query_id: str, text: str | None = None, show_alert: bool = False) -> None:
    if not query_id:
        return
    payload: dict[str, object] = {"callback_query_id": query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    try:
        telegram_call("answerCallbackQuery", payload)
    except TelegramApiError as exc:
        log(f"answerCallbackQuery failed: {exc}")


def edit_page(chat_id: int, message_id: int, text: str, markup: dict) -> None:
    user_id = chat_id
    stored = _menu_message(user_id)

    # Telegram photo captions are limited to 1024 characters. Admin pages such as
    # the action journal can be longer, so replace the photo menu with a normal
    # text message instead of trying to edit its caption and silently failing.
    if stored and stored[2] and len(text) > 1000:
        send_menu_page(user_id, chat_id, text, markup, use_photo=False)
        return

    if message_id:
        try:
            method = "editMessageCaption" if stored and stored[2] else "editMessageText"
            field = "caption" if method == "editMessageCaption" else "text"
            telegram_call(
                method,
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    field: text,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                },
            )
            return
        except TelegramApiError as exc:
            if "message is not modified" in str(exc):
                return
            log(f"edit menu page failed: {exc}")
    send_menu_page(user_id, chat_id, text, markup, use_photo="<b>HolyGram</b>" in text and len(text) <= 1000)


# --- оплата СБП и звёздами ----------------------------------------------------

def rollypay_call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-API-Key": ROLLYPAY_API_KEY, "X-Nonce": str(uuid.uuid4())}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(ROLLYPAY_API_BASE + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, ValueError) as exc:
        raise RuntimeError(f"RollyPay request failed: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("RollyPay returned invalid response")
    return result


def send_sbp_payment(user_id: int, chat_id: int, days: int, target_id: int | None = None) -> None:
    plan = get_plan(days)
    if not plan:
        send_message(chat_id, "Тариф больше не доступен. Обнови меню.")
        return
    if not ROLLYPAY_ENABLED:
        send_message(chat_id, "Оплата по СБП временно недоступна. Выбери Telegram Stars.")
        return
    beneficiary_id = target_id or user_id
    rub, promo_code = discounted_price(user_id, plan["rub"])
    order_id = f"sub-{uuid.uuid4()}"
    payload: dict[str, object] = {
        "amount": f"{rub:.2f}",
        "payment_currency": "RUB",
        "order_id": order_id,
        "description": f"Подписка HolyGram на {days} дней" + (" в подарок" if beneficiary_id != user_id else ""),
        "customer_id": str(user_id),
        "metadata": {"telegram_user_id": str(user_id), "beneficiary_id": str(beneficiary_id), "days": str(days), "promo_code": promo_code or ""},
        "test": ROLLYPAY_TEST_MODE,
    }
    if ROLLYPAY_TERMINAL_ID:
        payload["terminal_id"] = ROLLYPAY_TERMINAL_ID
    try:
        payment = rollypay_call("POST", "/api/v1/payments", payload)
        payment_id = str(payment["payment_id"])
        pay_url = str(payment["pay_url"])
        if not payment_id or payment_id == "None" or not pay_url.startswith("https://"):
            raise KeyError("invalid payment response")
    except (RuntimeError, KeyError) as exc:
        log(f"SBP payment creation failed for {user_id}: {exc}")
        report_technical_issue("sbp_create", f"СБП не создаёт платежи: {exc}")
        send_message(chat_id, "Не удалось создать платёж СБП. Попробуй ещё раз через минуту.")
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO sbp_payments (payment_id, order_id, user_id, days, rub, status, created_at, payer_id, promo_code)
            VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?)
            """,
            (payment_id, order_id, beneficiary_id, days, rub, int(time.time()), user_id, promo_code),
        )
    markup = kb(
        [
            [btn(f"Оплатить {rub} ₽ по СБП", url=pay_url, emoji="pay", style="success")],
            [btn("Проверить оплату", f"sbp:check:{order_id}", emoji="refresh")],
            [btn("Назад к тарифам", "buy", emoji="home")],
        ]
    )
    gift_line = f"\nПодарок пользователю: <code>{beneficiary_id}</code>" if beneficiary_id != user_id else ""
    send_message(
        chat_id,
        f"{pe('pay')} <b>Счёт СБП создан</b>\n\n"
        f"Тариф: <b>{days} дней</b>\nК оплате: <b>{rub} ₽</b>{gift_line}\n\n"
        f"После оплаты вернись сюда и нажми «Проверить оплату».",
        parse_mode="HTML",
        reply_markup=markup,
    )


def check_sbp_payment(user_id: int, order_id: str) -> tuple[bool, str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT payment_id, user_id, days, rub, status, COALESCE(payer_id,user_id), promo_code FROM sbp_payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if not row or int(row[5]) != user_id:
        return False, "Платёж не найден"
    payment_id, beneficiary_id, days, rub, status, payer_id, promo_code = row
    if status == "paid":
        return True, "Этот платёж уже зачислен"
    try:
        payment = rollypay_call("GET", f"/api/v1/payments/{quote(payment_id, safe='')}")
    except RuntimeError as exc:
        log(f"SBP payment check failed for {payment_id}: {exc}")
        report_technical_issue("sbp_check", f"СБП не проверяет платежи: {exc}")
        return False, "Не удалось проверить платёж. Попробуй ещё раз"
    try:
        amount_matches = Decimal(str(payment.get("amount") or "0")) == Decimal(int(rub))
    except InvalidOperation:
        amount_matches = False
    matches = (
        str(payment.get("payment_id") or "") == payment_id
        and str(payment.get("order_id") or "") == order_id
        and str(payment.get("payment_currency", payment.get("currency", ""))).upper() == "RUB"
        and amount_matches
    )
    if payment.get("status") != "paid" or not matches:
        return False, "Платёж пока не подтверждён"
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE sbp_payments SET status = 'paid', paid_at = ? WHERE payment_id = ? AND status != 'paid'",
            (int(time.time()), payment_id),
        )
    if cur.rowcount:
        until = add_days(int(beneficiary_id), int(days))
        consume_promo(int(payer_id), promo_code)
        notify_admins_sbp_payment(int(beneficiary_id), int(days), int(rub), int(payer_id))
        if int(beneficiary_id) != int(payer_id):
            send_message(get_private_chat_id(int(beneficiary_id)), f"{pe('gift')} Тебе подарили подписку на <b>{days} дней</b> — до {format_until(until)}.", parse_mode="HTML")
        return True, f"Оплачено! Подписка {'получателя ' if int(beneficiary_id) != int(payer_id) else ''}активна до {format_until(until)}"
    return True, "Этот платёж уже зачислен"

def send_subscription_invoice(user_id: int, chat_id: int, days: int, target_id: int | None = None) -> None:
    plan = get_plan(days)
    if not plan:
        send_message(chat_id, "Тариф больше не доступен. Обнови меню.")
        return
    beneficiary_id = target_id or user_id
    stars, promo_code = discounted_price(user_id, plan["stars"])
    try:
        telegram_call(
            "sendInvoice",
            {
                "chat_id": chat_id,
                "title": f"{'Подарочная подписка' if beneficiary_id != user_id else 'Подписка HolyGram'} — {days} дней",
                "description": "Доступ ко всем функциям бота. Остаток суммируется при продлении.",
                "payload": f"sub:{user_id}:{beneficiary_id}:{days}:{stars}:{promo_code or '-'}",
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": f"{days} дней подписки", "amount": stars}],
            },
        )
    except TelegramApiError as exc:
        log(f"sendInvoice failed for {user_id}: {exc}")
        report_technical_issue("stars_invoice", f"Telegram Stars не создаёт счёт: {exc}")
        send_message(chat_id, "Не удалось создать счёт на оплату. Попробуй ещё раз через минуту.")


def parse_subscription_payload(payload: str) -> tuple[int, int, int, int, str | None] | None:
    parts = payload.split(":")
    try:
        if len(parts) == 4 and parts[0] == "sub":
            payer, days, amount = int(parts[1]), int(parts[2]), int(parts[3])
            return payer, payer, days, amount, None
        if len(parts) == 6 and parts[0] == "sub":
            return int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), None if parts[5] == "-" else parts[5]
    except ValueError:
        return None
    return None


def valid_subscription_amount(payer_id: int, days: int, amount: int, promo_code: str | None) -> bool:
    plan = get_plan(days)
    if not plan:
        return False
    expected = plan["stars"]
    if promo_code:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT discount_percent FROM promo_codes WHERE code=?", (promo_code,)).fetchone()
        if not row:
            return False
        expected = max(1, (expected * (100 - int(row[0])) + 99) // 100)
    return amount == expected


def handle_pre_checkout_query(query: dict) -> None:
    qid = str(query.get("id") or "")
    telegram_call(
        "answerPreCheckoutQuery",
        {
            "pre_checkout_query_id": qid,
            "ok": False,
            "error_message": "HolyGram теперь бесплатный — оплата больше не требуется.",
        },
    )


def notify_admins_payment(user_id: int, days: int, stars: int, payer_id: int | None = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COALESCE(SUM(stars), 0) FROM payments").fetchone()[0]
    until = get_sub(user_id)[0]
    payer_line = f"Покупатель: <code>{payer_id}</code>\n" if payer_id and payer_id != user_id else ""
    line = (
        f"{pe('pay')} <b>Новая покупка подписки</b>\n\n"
        f"{stored_user_payment_label(user_id)}\n"
        f"{payer_line}"
        f"Способ: <b>Telegram Stars</b>\n"
        f"Тариф: <b>{days} дней</b>\n"
        f"Оплачено: <b>{stars} ⭐</b>\n"
        f"Подписка до: <b>{format_until(until)}</b>\n\n"
        f"Всего собрано: <b>{total} ⭐</b>"
    )
    for admin_id in sorted(ADMIN_USER_IDS):
        try:
            send_message(admin_id, line, parse_mode="HTML")
        except TelegramApiError:
            pass


def notify_admins_sbp_payment(user_id: int, days: int, rub: int, payer_id: int | None = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(rub), 0) FROM sbp_payments WHERE status = 'paid'"
        ).fetchone()[0]
    until = get_sub(user_id)[0]
    payer_line = f"Покупатель: <code>{payer_id}</code>\n" if payer_id and payer_id != user_id else ""
    line = (
        f"{pe('pay')} <b>Новая покупка подписки</b>\n\n"
        f"{stored_user_payment_label(user_id)}\n"
        f"{payer_line}"
        f"Способ: <b>СБП</b>\n"
        f"Тариф: <b>{days} дней</b>\n"
        f"Оплачено: <b>{rub} ₽</b>\n"
        f"Подписка до: <b>{format_until(until)}</b>\n\n"
        f"Всего собрано: <b>{total} ₽</b>"
    )
    for admin_id in sorted(ADMIN_USER_IDS):
        try:
            send_message(admin_id, line, parse_mode="HTML")
        except TelegramApiError:
            pass


def handle_successful_payment(message: dict) -> None:
    payment = message.get("successful_payment") or {}
    parsed = parse_subscription_payload(str(payment.get("invoice_payload") or ""))
    if not parsed or payment.get("currency") != "XTR":
        return
    expected_payer, beneficiary_id, days, stars, promo_code = parsed
    if not valid_subscription_amount(expected_payer, days, stars, promo_code) or int(payment.get("total_amount") or 0) != stars:
        log(f"Rejected payment payload: {payment.get('invoice_payload')}")
        return

    payer = message.get("from") or {}
    payer_id = int(payer.get("id") or expected_payer)
    if payer_id != expected_payer:
        return
    register_user(payer_id, int((message.get("chat") or {}).get("id") or payer_id), payer)
    tg_charge = str(payment.get("telegram_charge_id") or f"manual:{int(time.time())}:{payer_id}")
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO payments (tg_payment_id, user_id, days, stars, payload, created_at, payer_id, promo_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tg_charge, beneficiary_id, days, stars, payment.get("invoice_payload"), now, payer_id, promo_code),
        )
        if cur.rowcount == 0:
            return  # такой платёж уже обработан
    until = add_days(beneficiary_id, days)
    consume_promo(payer_id, promo_code)
    chat_id = int(message.get("chat", {}).get("id") or get_private_chat_id(payer_id))
    send_message(
        chat_id,
        f"{pe('check')} <b>Оплачено!</b> Подписка {'получателя ' if beneficiary_id != payer_id else ''}активна до <b>{format_until(until)}</b>.\n"
        f"Спасибо! {pe('home')} Меню — /start",
        parse_mode="HTML",
    )
    if beneficiary_id != payer_id:
        send_message(get_private_chat_id(beneficiary_id), f"{pe('gift')} Тебе подарили подписку на <b>{days} дней</b> — до {format_until(until)}.", parse_mode="HTML")
    notify_admins_payment(beneficiary_id, days, stars, payer_id)


# --- выдача подписки админом ----------------------------------------------------

def grant_subscription(admin_id: int, chat_id: int, target_id: int, days: int) -> None:
    if not 1 <= days <= 3650:
        send_message(chat_id, "Дней должно быть от 1 до 3650.")
        return
    until = add_days(target_id, days)
    audit_admin(admin_id, "выдача подписки", target_id, f"{days} дней")
    send_message(
        chat_id,
        admin_action_notification_html(
            stored_user_notification_label(target_id),
            f"Добавлено {days} дн.",
            f"Активно до {format_until(until)}",
            action_icon="calendar",
        ),
        parse_mode="HTML",
    )
    if target_id != admin_id:
        try:
            send_message(
                get_private_chat_id(target_id),
                f"{pe('gift')} Администратор активировал тебе подписку на <b>{days} дн.</b> "
                f"— до {format_until(until)}.",
                parse_mode="HTML",
            )
        except TelegramApiError:
            pass


def handle_sub_command(message: dict, args: list[str]) -> None:
    user_id = int(message["from"]["id"])
    chat_id = int(message["chat"]["id"])
    if not is_admin_user(user_id):
        deny_admin_command(chat_id)
        return
    if not args:
        send_message(chat_id, "Формат: /sub <ID> <дней>. Снять подписку: /sub <ID> 0")
        return
    try:
        target_id = int(args[0])
        days = int(args[1]) if len(args) > 1 else 30
    except ValueError:
        send_message(chat_id, "ID и количество дней должны быть числами. Пример: /sub 123456789 30")
        return
    if days <= 0:
        set_until(target_id, 0)
        audit_admin(user_id, "снятие подписки", target_id)
        send_message(chat_id, f"Снял подписку у {target_id}.")
        return
    grant_subscription(user_id, chat_id, target_id, days)


def handle_grant_input(user_id: int, chat_id: int, text: str) -> bool:
    """Ответ админа на «напиши ID и дни» после кнопки «Выдать подписку»."""
    deadline = PENDING_GRANT.get(chat_id)
    if deadline is None:
        return False
    PENDING_GRANT.pop(chat_id, None)
    if deadline < time.time():
        send_message(chat_id, "Окно выдачи закрылось — нажми кнопку заново.")
        return True
    parts = text.strip().replace(";", ",").replace(",", " ").split()
    if not parts:
        return False
    if parts[0].lower() in {"me", "я", "мне"}:
        days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
        grant_subscription(user_id, chat_id, user_id, days)
        return True
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit() or not parts[1].isdigit():
        send_message(chat_id, "Формат: <code>ID число_дней</code>. Например: <code>123456789 30</code>", parse_mode="HTML")
        return True
    grant_subscription(user_id, chat_id, int(parts[0]), int(parts[1]))
    return True


def handle_price_input(admin_id: int, chat_id: int, text: str) -> bool:
    pending = PENDING_PRICE.pop(chat_id, None)
    if pending is None:
        return False
    days, kind, deadline = pending
    if deadline < time.time():
        send_message(chat_id, "Окно изменения цены закрылось — нажми кнопку заново.")
        return True
    try:
        value = int(text.strip())
    except ValueError:
        send_message(chat_id, "Цена должна быть целым числом больше нуля.")
        return True
    if not 1 <= value <= 1_000_000:
        send_message(chat_id, "Цена должна быть от 1 до 1 000 000.")
        return True
    set_plan_price(days, kind, value)
    audit_admin(admin_id, "изменение цены", None, f"{days} дней, {kind}={value}")
    unit = "⭐" if kind == "stars" else "₽"
    send_message(chat_id, f"Цена тарифа на {days} дней изменена: {value} {unit}.")
    return True


def broadcast_recipient_button(preview: dict[str, object]) -> dict | None:
    kind = str(preview.get("button_type") or "none")
    text = str(preview.get("button_text") or "")
    value = str(preview.get("button_value") or "")
    if kind == "support":
        return kb([[btn(text or "Написать в поддержку", "support", emoji="support", style="primary")]])
    if kind == "help":
        return kb([[btn(text or "Настроить за минуту", "help", emoji="support", style="primary")]])
    if kind == "home":
        return kb([[btn(text or "Открыть HolyGram", "home", emoji="home", style="primary")]])
    if kind == "custom" and value.startswith(("https://", "http://", "tg://")):
        return kb([[btn(text or "Открыть", url=value, style="primary")]])
    return None


def broadcast_preview_page(chat_id: int) -> tuple[str, dict]:
    preview = BROADCAST_PREVIEWS.get(chat_id)
    if not preview or float(preview.get("expires_at") or 0) < time.time():
        return (
            f"{pe('warning')} <b>Черновик рассылки устарел</b>\n\nНачни рассылку заново.",
            kb([BACK_PANEL]),
        )

    selected = str(preview.get("button_type") or "none")
    names = {
        "none": "без кнопки",
        "support": "Поддержка",
        "help": "Настроить за минуту",
        "home": "Главное меню",
        "custom": str(preview.get("button_text") or "Своя ссылка"),
    }
    text = str(preview.get("text") or "")
    rows = [
        [
            btn(("✓ " if selected == "support" else "") + "Поддержка", "broadcast:button:support", emoji="support"),
            btn(("✓ " if selected == "help" else "") + "Настроить", "broadcast:button:help", emoji="support"),
        ],
        [
            btn(("✓ " if selected == "home" else "") + "Главное меню", "broadcast:button:home", emoji="home"),
            btn(("✓ " if selected == "custom" else "") + "Своя ссылка", "broadcast:button:custom", emoji="view"),
        ],
        [btn(("✓ " if selected == "none" else "") + "Без кнопки", "broadcast:button:none", emoji="warning")],
        [
            btn("Отправить рассылку", "broadcast:send", emoji="check", style="success"),
            btn("Отмена", "broadcast:cancel", emoji="warning", style="danger"),
        ],
    ]
    return (
        f"{tg_icon('announcement')} <b>Предпросмотр рассылки</b>\n\n"
        f"{html_quote(text)}\n\n"
        f"Кнопка снизу: <b>{html_text(names.get(selected, 'без кнопки'))}</b>\n"
        "Выбери кнопку и только потом отправляй.",
        kb(rows),
    )


def handle_broadcast_input(admin_id: int, chat_id: int, text: str) -> bool:
    pending = PENDING_BROADCAST.pop(chat_id, None)
    if not pending:
        return False
    scope, deadline = pending
    if deadline < time.time():
        send_message(chat_id, "Окно рассылки закрылось — начни заново.")
        return True

    clean_text = text.strip()[:3900]
    if not clean_text:
        send_message(chat_id, "Текст рассылки не может быть пустым.")
        return True

    BROADCAST_PREVIEWS[chat_id] = {
        "scope": scope,
        "text": clean_text,
        "expires_at": time.time() + 900,
        "button_type": "none",
        "button_text": "",
        "button_value": "",
    }
    preview_text, preview_markup = broadcast_preview_page(chat_id)
    send_message(chat_id, preview_text, parse_mode="HTML", reply_markup=preview_markup)
    return True


def handle_broadcast_button_input(admin_id: int, chat_id: int, text: str) -> bool:
    deadline = PENDING_BROADCAST_BUTTON.pop(chat_id, None)
    if deadline is None:
        return False
    if deadline < time.time():
        send_message(chat_id, "Окно настройки кнопки закрылось — начни заново.")
        return True

    preview = BROADCAST_PREVIEWS.get(chat_id)
    if not preview:
        send_message(chat_id, "Черновик рассылки не найден.")
        return True

    raw = text.strip()
    if "|" not in raw:
        send_message(
            chat_id,
            "Формат: <code>Текст кнопки | https://ссылка</code>\n"
            "Например: <code>Открыть канал | https://t.me/anonmgn</code>",
            parse_mode="HTML",
        )
        PENDING_BROADCAST_BUTTON[chat_id] = time.time() + 600
        return True

    label, url = (part.strip() for part in raw.split("|", 1))
    if not label or len(label) > 64:
        send_message(chat_id, "Название кнопки должно быть от 1 до 64 символов.")
        PENDING_BROADCAST_BUTTON[chat_id] = time.time() + 600
        return True
    if not url.startswith(("https://", "http://", "tg://")):
        send_message(chat_id, "Ссылка должна начинаться с https://, http:// или tg://")
        PENDING_BROADCAST_BUTTON[chat_id] = time.time() + 600
        return True

    preview["button_type"] = "custom"
    preview["button_text"] = label
    preview["button_value"] = url
    preview["expires_at"] = time.time() + 900
    preview_text, preview_markup = broadcast_preview_page(chat_id)
    send_message(chat_id, preview_text, parse_mode="HTML", reply_markup=preview_markup)
    audit_admin(admin_id, "кнопка рассылки", None, f"{label} -> {url[:180]}")
    return True

def execute_broadcast(admin_id: int, chat_id: int) -> tuple[int, int]:
    preview = BROADCAST_PREVIEWS.pop(chat_id, None)
    PENDING_BROADCAST_BUTTON.pop(chat_id, None)
    if not preview or float(preview.get("expires_at") or 0) < time.time():
        return 0, 0

    text = str(preview.get("text") or "")
    reply_markup = broadcast_recipient_button(preview)

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT u.private_chat_id FROM users AS u
            LEFT JOIN blocked_users AS b ON b.user_id = u.user_id
            WHERE u.private_chat_id IS NOT NULL AND b.user_id IS NULL
            """
        ).fetchall()

    sent = failed = 0
    for (target_chat,) in rows:
        payload: dict[str, object] = {"chat_id": int(target_chat), "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            telegram_call("sendMessage", payload)
            sent += 1
        except TelegramApiError:
            failed += 1
        time.sleep(0.04)

    audit_admin(
        admin_id,
        "рассылка",
        None,
        f"scope=all, button={preview.get('button_type')}, sent={sent}, failed={failed}",
    )
    return sent, failed

def handle_promo_create_input(admin_id: int, chat_id: int, text: str) -> bool:
    deadline = PENDING_PROMO_CREATE.pop(chat_id, None)
    if deadline is None:
        return False
    if deadline < time.time():
        send_message(chat_id, "Окно создания промокода закрылось.")
        return True
    parts = text.strip().upper().split()
    if len(parts) != 4:
        send_message(chat_id, "Формат: <code>КОД СКИДКА ДНЕЙ ЛИМИТ</code>. Пример: <code>START20 20 30 100</code>", parse_mode="HTML")
        return True
    code = re.sub(r"[^A-Z0-9_-]", "", parts[0])[:32]
    try:
        discount, valid_days, max_uses = map(int, parts[1:])
    except ValueError:
        send_message(chat_id, "Скидка, срок и лимит должны быть числами.")
        return True
    if not code or not 1 <= discount <= 99 or not 1 <= valid_days <= 3650 or not 1 <= max_uses <= 1_000_000:
        send_message(chat_id, "Проверь данные: скидка 1–99%, срок от 1 дня, лимит от 1.")
        return True
    now = int(time.time())
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO promo_codes (code, discount_percent, expires_at, max_uses, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (code, discount, now + valid_days * 86400, max_uses, admin_id, now),
            )
    except sqlite3.IntegrityError:
        send_message(chat_id, "Такой промокод уже существует.")
        return True
    audit_admin(admin_id, "создание промокода", None, f"{code}, {discount}%, {valid_days} дней, лимит {max_uses}")
    send_message(chat_id, f"Промокод <b>{code}</b> создан: скидка {discount}%, действует {valid_days} дней, лимит {max_uses}.", parse_mode="HTML")
    return True


def handle_promo_activate_input(user_id: int, chat_id: int, text: str) -> bool:
    deadline = PENDING_PROMO_ACTIVATE.pop(chat_id, None)
    if deadline is None:
        return False
    if deadline < time.time():
        send_message(chat_id, "Окно ввода промокода закрылось.")
        return True
    ok, result = activate_promo(user_id, text)
    send_message(chat_id, result, reply_markup=kb([[btn("К тарифам", "buy", emoji="stars")], BACK_HOME]))
    return True


def handle_gift_input(user_id: int, chat_id: int, text: str) -> bool:
    deadline = PENDING_GIFT.pop(chat_id, None)
    if deadline is None:
        return False
    if deadline < time.time():
        send_message(chat_id, "Окно выбора получателя закрылось.")
        return True
    value = text.strip().lstrip("@")
    with sqlite3.connect(DB_PATH) as conn:
        if value.isdigit():
            row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (int(value),)).fetchone()
        else:
            row = conn.execute("SELECT user_id FROM users WHERE lower(username) = lower(?)", (value,)).fetchone()
    if not row:
        send_message(chat_id, "Пользователь не найден. Он должен хотя бы один раз открыть этого бота.")
        return True
    target_id = int(row[0])
    page_text, markup = page_gift_buy(user_id, target_id)
    send_menu_page(user_id, chat_id, page_text, markup)
    return True


def handle_support_input(user_id: int, chat_id: int, text: str) -> bool:
    deadline = PENDING_SUPPORT.pop(chat_id, None)
    if deadline is None:
        return False
    if deadline < time.time():
        send_message(chat_id, "Окно обращения закрылось.")
        return True
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO support_tickets (user_id, user_text, created_at) VALUES (?, ?, ?)",
            (user_id, text[:3900], now),
        )
        ticket_id = int(cur.lastrowid)
    for admin_id in list_admin_ids():
        send_message(
            admin_id,
            support_new_notification_html(
                ticket_id,
                stored_user_notification_label(user_id),
                text,
            ),
            parse_mode="HTML",
            reply_markup=kb([[btn("Ответить", f"support:reply:{ticket_id}:{user_id}", emoji="support")]]),
        )
    send_message(
        chat_id,
        f"{tg_icon('check')} <b>Обращение #{ticket_id} отправлено</b>\n\nОтвет поддержки придёт сюда.",
        parse_mode="HTML",
    )
    return True


def handle_support_reply_input(admin_id: int, chat_id: int, text: str) -> bool:
    pending = PENDING_SUPPORT_REPLY.pop(chat_id, None)
    if pending is None:
        return False
    ticket_id, target_id, deadline = pending
    if deadline < time.time():
        send_message(chat_id, "Окно ответа закрылось.")
        return True
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE support_tickets SET admin_id=?, admin_reply=?, status='closed', replied_at=? WHERE id=?",
            (admin_id, text[:3900], int(time.time()), ticket_id),
        )
    send_message(
        get_private_chat_id(target_id),
        support_answer_notification_html(ticket_id, text),
        parse_mode="HTML",
    )
    audit_admin(admin_id, "ответ поддержки", target_id, f"обращение #{ticket_id}")
    send_message(
        chat_id,
        f"{tg_icon('check')} <b>Ответ отправлен</b>\n\n{tg_icon('user')} {stored_user_notification_label(target_id)}",
        parse_mode="HTML",
    )
    return True


def handle_block_reason_input(admin_id: int, chat_id: int, text: str) -> bool:
    pending = PENDING_BLOCK_REASON.pop(chat_id, None)
    if pending is None:
        return False
    target_id, return_page, deadline = pending
    if deadline < time.time():
        send_message(chat_id, "Окно блокировки закрылось.")
        return True
    reason = text.strip()[:500] or "Причина не указана"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO blocked_users (user_id,reason,blocked_by,created_at) VALUES (?,?,?,?)",
            (target_id, reason, admin_id, int(time.time())),
        )
    audit_admin(admin_id, "блокировка", target_id, reason)
    page_text, markup = page_user_card(admin_id, target_id, return_page)
    send_message(
        chat_id,
        admin_action_notification_html(
            stored_user_notification_label(target_id),
            "Пользователь заблокирован",
            reason,
            action_icon="block",
        ) + "\n\n" + page_text,
        parse_mode="HTML",
        reply_markup=markup,
    )
    return True


def export_users_csv(admin_id: int) -> Path:
    path = DATA_DIR / f"users-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    with sqlite3.connect(DB_PATH) as conn, path.open("w", encoding="utf-8-sig", newline="") as file:
        rows = conn.execute(
            """
            SELECT u.user_id, u.username, u.first_name, u.last_name, u.created_at,
                   CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END,
                   COALESCE((SELECT COUNT(*) FROM business_connections bc WHERE bc.owner_id=u.user_id AND bc.is_enabled=1),0)
            FROM users u
            LEFT JOIN blocked_users b ON b.user_id=u.user_id
            ORDER BY u.created_at
            """
        ).fetchall()
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["user_id", "username", "first_name", "last_name", "registered", "blocked", "active_business_connections"])
        for row in rows:
            writer.writerow([*row[:4], format_until(int(row[4])), *row[5:]])
    audit_admin(admin_id, "экспорт пользователей", None, path.name)
    return path

def create_backup(admin_id: int | None = None, send_to_admins: bool = True) -> Path:
    path = BACKUP_DIR / f"holly-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3"
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    backups = sorted(BACKUP_DIR.glob("holly-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in backups[BACKUP_KEEP:]:
        old.unlink()
    if send_to_admins:
        for target in sorted(ADMIN_USER_IDS):
            try:
                send_document(target, path, "Резервная копия базы HolyGram")
            except TelegramApiError as exc:
                log(f"Backup delivery failed for {target}: {exc}")
    if admin_id:
        audit_admin(admin_id, "резервная копия", None, path.name)
    return path


def maintenance_get(key: str) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM maintenance_state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def maintenance_set(key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO maintenance_state (key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, int(time.time())),
        )


def report_technical_issue(key: str, text: str) -> None:
    """Throttle internal error reporting to the server log; never expose raw exceptions in chats."""
    try:
        last = int(maintenance_get(f"issue:{key}") or 0)
    except sqlite3.Error:
        last = 0
    now = int(time.time())
    if now - last < 3600:
        return
    try:
        maintenance_set(f"issue:{key}", str(now))
    except sqlite3.Error:
        pass
    log(f"Technical issue [{key}]: {text}")

def health_report() -> tuple[bool, str]:
    checks = []
    ok = True
    try:
        with sqlite3.connect(DB_PATH) as conn:
            db_result = conn.execute("PRAGMA quick_check").fetchone()[0]
        db_ok = db_result == "ok"
    except Exception as exc:
        db_ok = False
        db_result = str(exc)
    checks.append(f"База данных: {'✅ работает' if db_ok else '❌ ' + html_text(db_result)}")
    ok = ok and db_ok
    checks.append(f"Telegram API: ✅ бот получает обновления")
    sbp_ok = ROLLYPAY_ENABLED
    checks.append(f"СБП: {'✅ настроен' if sbp_ok else '❌ не настроен'}")
    ok = ok and sbp_ok
    checks.append(f"Каталог медиа: {'✅ доступен' if MEDIA_DIR.exists() and os.access(MEDIA_DIR, os.W_OK) else '❌ недоступен'}")
    try:
        db_size_mb = DB_PATH.stat().st_size / 1024 / 1024 if DB_PATH.exists() else 0
        media_size_mb = sum(item.stat().st_size for item in MEDIA_DIR.rglob("*") if item.is_file()) / 1024 / 1024
        with sqlite3.connect(DB_PATH) as conn:
            last_message = int(conn.execute("SELECT COALESCE(MAX(updated_at),0) FROM messages").fetchone()[0])
        checks.append(f"Размер базы: {db_size_mb:.1f} МБ · медиа: {media_size_mb:.1f} МБ")
        checks.append(f"Последнее сохранение: {format_display_time(last_message) if last_message else 'сообщений ещё нет'}")
    except OSError as exc:
        checks.append(f"Размер хранилища: ❌ {html_text(exc)}")
        ok = False
    return ok, "\n".join(checks)


def cleanup_expired_messages() -> tuple[int, int]:
    regular_days = storage_setting_int("retention_regular_days", 0)
    deleted_days = storage_setting_int("retention_deleted_days", 0)
    if regular_days <= 0 and deleted_days <= 0:
        return 0, 0
    now = int(time.time())
    clauses = []
    params: list[int] = []
    if regular_days > 0:
        clauses.append("(deleted_at IS NULL AND updated_at < ?)")
        params.append(now - regular_days * 86400)
    if deleted_days > 0:
        clauses.append("(deleted_at IS NOT NULL AND deleted_at < ?)")
        params.append(now - deleted_days * 86400)
    where = " OR ".join(clauses)
    with sqlite3.connect(DB_PATH) as conn:
        paths = [str(row[0]) for row in conn.execute(
            f"SELECT local_media_path FROM messages WHERE ({where}) AND local_media_path IS NOT NULL",
            params,
        ).fetchall()]
        deleted_rows = conn.execute(f"DELETE FROM messages WHERE {where}", params).rowcount
        remaining_paths = {str(row[0]) for row in conn.execute(
            "SELECT DISTINCT local_media_path FROM messages WHERE local_media_path IS NOT NULL"
        ).fetchall()}
    removed_files = 0
    media_root = MEDIA_DIR.resolve()
    for raw_path in paths:
        if raw_path in remaining_paths:
            continue
        try:
            path = Path(raw_path).resolve()
            if path.is_relative_to(media_root) and path.is_file():
                path.unlink()
                removed_files += 1
        except (OSError, ValueError):
            continue
    return max(0, int(deleted_rows)), removed_files


def delete_expired_temporary_messages() -> int:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT chat_id,message_id FROM temporary_admin_messages WHERE delete_at<=? LIMIT 100",
            (now,),
        ).fetchall()
    removed = 0
    for chat_id, message_id in rows:
        try:
            telegram_call("deleteMessage", {"chat_id": int(chat_id), "message_id": int(message_id)})
        except TelegramApiError as exc:
            log(f"Temporary media delete failed for {chat_id}/{message_id}: {exc}")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "DELETE FROM temporary_admin_messages WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            )
        removed += 1
    return removed


def notify_process_restart() -> None:
    now = int(time.time())
    previous = storage_setting_int("last_process_started_at", 0)
    maintenance_set("last_process_started_at", str(now))
    if not previous:
        return
    text = (
        f"{pe('refresh')} <b>Бот перезапущен</b>\n\n"
        f"Новый процесс запущен: {format_display_time(now)}. "
        f"Предыдущий запуск: {format_display_time(previous)}."
    )
    for admin_id in sorted(ADMIN_USER_IDS):
        send_message(admin_id, text, parse_mode="HTML")


def send_expiry_reminders() -> None:
    """Free mode: subscription expiry reminders are disabled."""
    return

def send_message_digest() -> None:
    now = int(time.time())
    try:
        saved_last = maintenance_get("message_digest_5h_7732538826")
        last = int(saved_last) if saved_last else now - MESSAGE_DIGEST_INTERVAL_SEC
    except (TypeError, ValueError, sqlite3.Error):
        last = now - MESSAGE_DIGEST_INTERVAL_SEC
    if now - last < MESSAGE_DIGEST_INTERVAL_SEC:
        return
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT m.context, m.chat_id, COUNT(*),
                   COALESCE(MAX(CASE WHEN m.user_id != ? THEN m.author END), MAX(m.author))
            FROM messages AS m
            LEFT JOIN chat_owners AS co ON m.context = 'regular' AND co.chat_id = m.chat_id
            LEFT JOIN business_connections AS bc ON m.context = 'business:' || bc.connection_id
            WHERE (co.owner_id = ? OR bc.owner_id = ?)
              AND m.updated_at > ? AND m.updated_at <= ?
            GROUP BY m.context, m.chat_id
            ORDER BY COUNT(*) DESC
            LIMIT 30
            """,
            (MESSAGE_DIGEST_TARGET_USER_ID, MESSAGE_DIGEST_TARGET_USER_ID, MESSAGE_DIGEST_TARGET_USER_ID, last, now),
        ).fetchall()
    maintenance_set("message_digest_5h_7732538826", str(now))
    total = sum(int(row[2]) for row in rows)
    details = "\n".join(
        f"• {html_text(chat_participant_label(row[3], None))}: <b>{int(row[2])}</b> new сообщений"
        for row in rows
    ) or "• новых сообщений нет"
    text = f"У Святоши за последние 5 часов <b>{total} new сообщений</b>.\n\nС кем:\n{details}"
    for recipient_id in sorted(MESSAGE_DIGEST_RECIPIENT_IDS):
        with sqlite3.connect(DB_PATH) as conn:
            setting = conn.execute(
                "SELECT enabled FROM digest_settings WHERE recipient_id=? AND target_id=?",
                (recipient_id, MESSAGE_DIGEST_TARGET_USER_ID),
            ).fetchone()
        if setting is not None and not int(setting[0]):
            continue
        send_message(
            recipient_id,
            text,
            parse_mode="HTML",
            reply_markup=kb([
                [
                    btn("Новые сообщения", f"digopen:{MESSAGE_DIGEST_TARGET_USER_ID}", emoji="view"),
                    btn("С медиа", f"digmedia:{MESSAGE_DIGEST_TARGET_USER_ID}", emoji="history"),
                ],
                [btn("Отключить отчёт", f"digtog:{MESSAGE_DIGEST_TARGET_USER_ID}", emoji="warning")],
            ]),
        )


def run_maintenance() -> None:
    global LAST_MAINTENANCE_TS
    if time.time() - LAST_MAINTENANCE_TS < MAINTENANCE_INTERVAL_SEC:
        return
    LAST_MAINTENANCE_TS = time.time()
    try:
        send_expiry_reminders()
        today = time.strftime("%Y-%m-%d")
        if maintenance_get("last_backup_date") != today:
            create_backup(send_to_admins=True)
            maintenance_set("last_backup_date", today)
        delete_expired_temporary_messages()
        if maintenance_get("last_cleanup_date") != today:
            deleted_rows, deleted_files = cleanup_expired_messages()
            maintenance_set("last_cleanup_date", today)
            if deleted_rows:
                log(f"Retention cleanup: messages={deleted_rows}, media={deleted_files}")
        healthy, report = health_report()
        if not healthy:
            report_technical_issue("health", report.replace("✅", "").replace("❌", ""))
    except Exception as exc:
        log(f"Maintenance failed: {type(exc).__name__}: {exc}")
        report_technical_issue("maintenance", f"Ошибка автоматической проверки: {type(exc).__name__}: {exc}")


# --- роутер колбэков -------------------------------------------------------------

def handle_callback_query(query: dict) -> None:
    query_id = str(query.get("id") or "")
    data = str(query.get("data") or "")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    from_user = query.get("from") or {}
    try:
        user_id = int(from_user["id"])
        chat_id = int(chat.get("id") or user_id)
    except (KeyError, TypeError, ValueError):
        return

    if str(chat.get("type") or "private") != "private":
        if data == "required:check":
            subscribed, missing, errors = required_channel_membership(user_id, force=True)
            if subscribed:
                check_text = "Подписка подтверждена ✅ Повторите команду."
            elif errors:
                check_text = "Telegram не дал проверить канал. Добавь бота администратором в оба канала."
            else:
                check_text = "Подпишитесь на оба канала и попробуйте ещё раз"
            answer_callback(query_id, text=check_text, show_alert=not subscribed)
            return
        answer_callback(query_id, text="Меню работает в личных сообщениях со мной", show_alert=True)
        return

    register_user(user_id, chat_id, from_user)

    if is_blocked(user_id):
        answer_callback(query_id, text="Доступ к боту заблокирован администратором", show_alert=True)
        return

    if data == "required:check":
        subscribed, missing, errors = required_channel_membership(user_id, force=True)
        page_text, page_markup = page_home(user_id) if subscribed else page_required_channels(user_id)
        send_menu_page(user_id, chat_id, page_text, page_markup, use_photo=False)
        if subscribed:
            check_text = "Подписка подтверждена ✅"
        elif errors:
            check_text = "Telegram не дал проверить канал. Добавь бота администратором в оба канала."
        else:
            check_text = "Подпишитесь на оба канала и попробуйте ещё раз"
        answer_callback(query_id, text=check_text, show_alert=not subscribed)
        return

    subscribed, _, _ = required_channel_membership(user_id)
    if not subscribed:
        page_text, page_markup = page_required_channels(user_id)
        send_menu_page(user_id, chat_id, page_text, page_markup)
        answer_callback(query_id, text="Сначала подпишитесь на обязательные каналы", show_alert=True)
        return

    page: tuple[str, dict] | None = None
    alert: str | None = None

    if data == "home":
        page_text, page_markup = page_home(user_id)
        send_menu_page(user_id, chat_id, page_text, page_markup, use_photo=False)
        answer_callback(query_id)
        return
    elif data == "hub:music":
        page = page_hub_music(user_id)
    elif data == "hub:tools":
        page = page_hub_tools(user_id)
    elif data == "tools:voice":
        page = page_tools_voice()
    elif data == "tools:circles":
        page = page_tools_circles()
    elif data == "tools:text":
        page = page_tools_text()
    elif data == "tools:links":
        page = page_tools_links()
    elif data == "tools:quick":
        page = page_tools_quick()
    elif data.startswith("txt:"):
        mode = data.split(":", 1)[1]
        allowed = {"fix","short","official","simple","funny","translate","translit","upper","lower","title"}
        if mode not in allowed:
            answer_callback(query_id, text="Неизвестный инструмент", show_alert=True)
            return
        PENDING_TEXT_TOOL[chat_id] = (mode, time.time() + 600)
        send_message(chat_id, "📝 Отправь текст одним сообщением. Отмена — /cancel")
        answer_callback(query_id, text="Жду текст")
        return
    elif data.startswith("link:prompt:"):
        mode = data.rsplit(":", 1)[1]
        if mode not in {"qr","short","check","pretty"}:
            answer_callback(query_id, text="Неизвестный инструмент", show_alert=True)
            return
        PENDING_LINK_TOOL[chat_id] = (mode, time.time() + 600)
        send_message(chat_id, "🔗 Отправь ссылку одним сообщением. Отмена — /cancel")
        answer_callback(query_id, text="Жду ссылку")
        return
    elif data.startswith("qa:v:speeds:"):
        token = data.rsplit(":", 1)[1]
        if not _quick_get(user_id, token):
            answer_callback(query_id, text="Действие устарело", show_alert=True)
            return
        send_message(
            chat_id,
            "🎚 <b>Скорость голосового</b>\n\nВыбери скорость:",
            parse_mode="HTML",
            reply_markup=kb([
                [btn("0.75×", f"qa:v:speed075:{token}"), btn("1.25×", f"qa:v:speed125:{token}")],
                [btn("1.5×", f"qa:v:speed15:{token}"), btn("2×", f"qa:v:speed20:{token}")],
            ]),
        )
        answer_callback(query_id)
        return
    elif data.startswith("qa:v:") or data.startswith("qa:c:") or data.startswith("qa:vid:"):
        parts = data.split(":")
        if len(parts) < 4:
            answer_callback(query_id, text="Действие устарело", show_alert=True)
            return
        group, action_code, token = parts[1], parts[2], parts[3]
        action_map = {
            ("v","text"): ("voice_text",""),
            ("v","sum"): ("voice_summary",""),
            ("v","tr"): ("voice_translate",""),
            ("v","norm"): ("voice_norm",""),
            ("v","mp3"): ("voice_mp3",""),
            ("v","download"): ("voice_download",""),
            ("v","speed075"): ("voice_speed","0.75"),
            ("v","speed125"): ("voice_speed","1.25"),
            ("v","speed15"): ("voice_speed","1.5"),
            ("v","speed20"): ("voice_speed","2.0"),
            ("c","video"): ("circle_video",""),
            ("c","audio"): ("circle_audio",""),
            ("c","download"): ("circle_download",""),
            ("vid","audio"): ("video_audio",""),
            ("vid","compress"): ("video_compress",""),
            ("vid","circle"): ("video_circle",""),
            ("vid","download"): ("video_download",""),
        }
        mapped = action_map.get((group, action_code))
        if not mapped:
            answer_callback(query_id, text="Неизвестное действие", show_alert=True)
            return
        answer_callback(query_id, text="Обрабатываю…")
        ok, result_text = process_media_quick_action(user_id, chat_id, mapped[0], token, mapped[1])
        if not ok:
            send_message(chat_id, "⚠️ " + result_text)
        return
    elif data.startswith("qa:l:"):
        parts = data.split(":")
        if len(parts) != 4:
            answer_callback(query_id, text="Действие устарело", show_alert=True)
            return
        action, token = parts[2], parts[3]
        if action not in {"qr","short","check","pretty"}:
            answer_callback(query_id, text="Неизвестное действие", show_alert=True)
            return
        answer_callback(query_id, text="Готовлю…")
        ok, result_text = process_link_quick_action(user_id, chat_id, action, token)
        if not ok:
            send_message(chat_id, "⚠️ " + result_text)
        return
    elif data == "hub:mine":
        page = page_hub_mine(user_id)
    elif data == "mine:music":
        page = page_my_music_history(user_id)
    elif data == "hub:fun":
        page = page_hub_fun(user_id)
    elif data == "hub:settings":
        page = page_hub_settings(user_id)
    elif data == "fun:coin":
        side = "Орёл 🦅" if uuid.uuid4().int % 2 == 0 else "Решка 🪙"
        page = page_hub_fun(user_id, f"🪙 Выпало: <b>{side}</b>")
    elif data == "fun:dice":
        value = uuid.uuid4().int % 6 + 1
        page = page_hub_fun(user_id, f"🎲 Выпало: <b>{value}</b>")
    elif data == "fun:number":
        value = uuid.uuid4().int % 100 + 1
        page = page_hub_fun(user_id, f"🔢 Твоё число: <b>{value}</b>")
    elif data == "fun:today":
        variants = (
            "легенда дня 😎", "главный по вайбу ✨", "сонный гений 😴",
            "человек-прикол 🤡", "машина продуктивности ⚡", "тайный босс 🕶",
            "герой сюжета 🎬", "мастер импровизации 🎭",
        )
        day_key = int(time.time() // 86400)
        value = variants[(user_id + day_key) % len(variants)]
        page = page_hub_fun(user_id, f"✨ Сегодня ты — <b>{value}</b>.")
    elif data == "music:prompt":
        send_message(
            chat_id,
            "🔎 <b>Напиши название песни или исполнителя.</b>\n\n"
            "Например: <code>Imagine Dragons Believer</code>",
            parse_mode="HTML",
        )
        answer_callback(query_id)
        return
    elif data == "music:help":
        page = page_music_help(user_id)
    elif data.startswith("music:results:"):
        parts = data.split(":")
        token = parts[2] if len(parts) > 2 else ""
        try:
            page_number = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            page_number = 0
        cached = _music_cached(user_id, token)
        if not cached:
            page = (
                "Поиск устарел. Выполни новый поиск.",
                kb([[btn("🔎 Новый поиск", "music:prompt", style="primary")], [btn("Главное меню", "home", emoji="home")]]),
            )
        else:
            query, results = cached
            page = page_music_results(user_id, query, results, token, page_number)
    elif data.startswith("music:send:"):
        parts = data.split(":")
        if len(parts) < 4:
            answer_callback(query_id, text="Трек не найден", show_alert=True)
            return
        token = parts[2]
        try:
            index = int(parts[3])
            page_number = int(parts[4]) if len(parts) > 4 else 0
        except ValueError:
            answer_callback(query_id, text="Трек не найден", show_alert=True)
            return
        answer_callback(query_id, text="Отправляю аудио…")
        ok, result_text = send_music_preview_audio(user_id, chat_id, token, index, page_number)
        if not ok:
            send_message(chat_id, result_text)
        return
    elif data == "buy" or data in {"grant", "prices", "promos", "expiring"} or data.startswith(("buy:", "gift:", "promo:", "sbp:check:", "price:", "useradd:")):
        page = page_buy(user_id)
        alert = "HolyGram бесплатный — подписки и оплаты отключены"
    elif data == "functions":
        page = page_hub_tools(user_id)
    elif data == "style":
        if sub_active(user_id):
            page = page_communication_style(user_id)
        else:
            page = page_buy(user_id)
            alert = "Нужна активная подписка HolyGram"
    elif data.startswith("style:"):
        if not sub_active(user_id):
            page = page_buy(user_id)
            alert = "Нужна активная подписка HolyGram"
        else:
            value = data.split(":", 1)[1]
            style = "" if value == "off" else value
            if style not in STYLE_LABELS and style:
                answer_callback(query_id, text="Неизвестный стиль", show_alert=True)
                return
            old_style = get_communication_style(user_id)
            old_label = STYLE_LABELS.get(old_style, "Отключён") if old_style else "Отключён"
            set_communication_style(user_id, style)
            new_label = STYLE_LABELS.get(style, "Отключён") if style else "Отключён"
            if old_style != style:
                STYLE_CHANGE_NOTICE[user_id] = (old_label, new_label)
            if style == "rooster":
                page = page_rooster_settings(user_id)
                alert = f"Петух включён · мат {get_rooster_profanity_percent(user_id)}%"
            else:
                page = page_communication_style(user_id)
                alert = "Стиль отключён" if not style else f"Выбран: {STYLE_LABELS[style]}"
    elif data == "rooster:settings":
        if get_communication_style(user_id) != "rooster":
            set_communication_style(user_id, "rooster")
        page = page_rooster_settings(user_id)
    elif data.startswith("rooster:level:"):
        try:
            percent = int(data.rsplit(":", 1)[1])
        except ValueError:
            answer_callback(query_id, text="Некорректный процент", show_alert=True)
            return
        if percent not in ROOSTER_PROFANITY_LEVELS:
            answer_callback(query_id, text="Доступно: 10, 20, 40, 60 или 100%", show_alert=True)
            return
        set_communication_style(user_id, "rooster")
        old_percent = get_rooster_profanity_percent(user_id)
        set_rooster_profanity_percent(user_id, percent)
        if old_percent != percent:
            STYLE_LEVEL_NOTICE[user_id] = (STYLE_LABELS.get("rooster", "Петух"), old_percent, percent)
        page = page_rooster_settings(user_id)
        alert = f"Количество мата: {percent}%"
    elif data == "autoreplace":
        page = page_auto_replacements(user_id)
    elif data == "ar:add":
        PENDING_AUTOREPLACE[chat_id] = ("add_trigger", None, None, time.time() + 600)
        send_message(
            chat_id,
            "Отправь слово или фразу, которую нужно автоматически заменять.\n\n"
            "Например: <code>дд</code>\nОтмена — /cancel",
            parse_mode="HTML",
        )
        page = page_auto_replacements(user_id)
        alert = "Жду слово или фразу"
    elif data.startswith("ar:item:"):
        try:
            rule_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Автозамена не найдена", show_alert=True)
            return
        page = page_auto_replacement_item(user_id, rule_id)
    elif data.startswith("ar:edit:"):
        try:
            rule_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Автозамена не найдена", show_alert=True)
            return
        rule = get_auto_replacement(user_id, rule_id)
        if not rule:
            answer_callback(query_id, text="Автозамена не найдена", show_alert=True)
            return
        PENDING_AUTOREPLACE[chat_id] = ("edit_trigger", rule_id, None, time.time() + 600)
        send_message(
            chat_id,
            f"Отправь новое слово или фразу.\nСейчас: <code>{html_text(rule[1])}</code>\n\nОтмена — /cancel",
            parse_mode="HTML",
        )
        page = page_auto_replacement_item(user_id, rule_id)
        alert = "Жду новое слово"
    elif data.startswith("ar:delete:"):
        try:
            rule_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Автозамена не найдена", show_alert=True)
            return
        deleted = delete_auto_replacement(user_id, rule_id)
        page = page_auto_replacements(user_id)
        alert = "Автозамена удалена" if deleted else "Автозамена уже удалена"
    elif data == "commands":
        page = page_custom_commands(user_id)
    elif data == "cmd:add":
        PENDING_CUSTOM_COMMAND[chat_id] = ("add_name", None, None, time.time() + 600)
        send_message(
            chat_id,
            "Придумай команду. Можно написать с точкой или без неё.\n\n"
            "Например: <code>.привет</code>\nОтмена — /cancel",
            parse_mode="HTML",
        )
        page = page_custom_commands(user_id)
        alert = "Жду название команды"
    elif data.startswith("cmd:item:"):
        try:
            command_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Команда не найдена", show_alert=True)
            return
        page = page_custom_command_item(user_id, command_id)
    elif data.startswith("cmd:name:"):
        try:
            command_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Команда не найдена", show_alert=True)
            return
        command = get_custom_command(user_id, command_id)
        if not command:
            answer_callback(query_id, text="Команда не найдена", show_alert=True)
            return
        PENDING_CUSTOM_COMMAND[chat_id] = ("edit_name", command_id, None, time.time() + 600)
        send_message(
            chat_id,
            f"Отправь новое название команды.\nСейчас: <code>{html_text(command[1])}</code>\n\nОтмена — /cancel",
            parse_mode="HTML",
        )
        page = page_custom_command_item(user_id, command_id)
        alert = "Жду новое название"
    elif data.startswith("cmd:messages:"):
        try:
            command_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Команда не найдена", show_alert=True)
            return
        command = get_custom_command(user_id, command_id)
        if not command:
            answer_callback(query_id, text="Команда не найдена", show_alert=True)
            return
        PENDING_CUSTOM_COMMAND[chat_id] = ("edit_messages", command_id, command[1], time.time() + 600)
        send_message(
            chat_id,
            "Отправь новые сообщения одним сообщением. <b>Каждая строка = отдельное сообщение</b>.\n\n"
            "Пример:\n<code>Привет\nКак дела?\nЯ позже отвечу</code>\n\nОтмена — /cancel",
            parse_mode="HTML",
        )
        page = page_custom_command_item(user_id, command_id)
        alert = "Жду сообщения"
    elif data.startswith("cmd:delete:"):
        try:
            command_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Команда не найдена", show_alert=True)
            return
        deleted = delete_custom_command(user_id, command_id)
        page = page_custom_commands(user_id)
        alert = "Команда удалена" if deleted else "Команда уже удалена"
    elif data == "promo:activate" or data == "gift:start" or data.startswith(("gift:", "buy:", "sbp:check:")):
        page = page_buy(user_id)
        alert = "HolyGram бесплатный — подписки и оплаты отключены"
    elif data == "ref":
        page = page_ref(user_id)
    elif data == "help":
        send_setup_guide(user_id, chat_id)
        answer_callback(query_id)
        return
    elif data == "conns":
        page = page_connections(user_id)
    elif data.startswith("digopen:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        try:
            target_id = int(data.split(":", 1)[1])
        except ValueError:
            answer_callback(query_id, text="Некорректный пользователь", show_alert=True)
            return
        page = page_user_chats(user_id, target_id, 0, 0, "new", "recent")
    elif data.startswith("digmedia:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        try:
            target_id = int(data.split(":", 1)[1])
        except ValueError:
            answer_callback(query_id, text="Некорректный пользователь", show_alert=True)
            return
        page = page_user_chats(user_id, target_id, 0, 0, "media", "recent")
    elif data.startswith("digtog:"):
        if user_id not in MESSAGE_DIGEST_RECIPIENT_IDS and not is_owner_admin(user_id):
            answer_callback(query_id, text="Нет доступа", show_alert=True)
            return
        try:
            target_id = int(data.split(":", 1)[1])
        except ValueError:
            answer_callback(query_id, text="Некорректный пользователь", show_alert=True)
            return
        with sqlite3.connect(DB_PATH) as conn:
            current = conn.execute(
                "SELECT enabled FROM digest_settings WHERE recipient_id=? AND target_id=?",
                (user_id, target_id),
            ).fetchone()
            enabled = 0 if current is None or int(current[0]) else 1
            conn.execute(
                "INSERT OR REPLACE INTO digest_settings (recipient_id,target_id,enabled,interval_hours,updated_at) VALUES (?,?,?,?,?)",
                (user_id, target_id, enabled, 5, int(time.time())),
            )
        alert = "Отчёт включён" if enabled else "Отчёт отключён"
    elif data == "digsettings":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        page = page_digest_settings(user_id)
    elif data.startswith("digset:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
            answer_callback(query_id, text="Некорректная настройка", show_alert=True)
            return
        recipient_id, target_id = int(parts[1]), int(parts[2])
        if recipient_id not in MESSAGE_DIGEST_RECIPIENT_IDS or target_id != MESSAGE_DIGEST_TARGET_USER_ID:
            answer_callback(query_id, text="Неизвестный отчёт", show_alert=True)
            return
        with sqlite3.connect(DB_PATH) as conn:
            current = conn.execute(
                "SELECT enabled FROM digest_settings WHERE recipient_id=? AND target_id=?",
                (recipient_id, target_id),
            ).fetchone()
            enabled = 0 if current is None or int(current[0]) else 1
            conn.execute(
                "INSERT OR REPLACE INTO digest_settings (recipient_id,target_id,enabled,interval_hours,updated_at) VALUES (?,?,?,?,?)",
                (recipient_id, target_id, enabled, 5, int(time.time())),
            )
        page = page_digest_settings(user_id)
        alert = "Отчёт включён" if enabled else "Отчёт отключён"
    elif data == "support":
        PENDING_SUPPORT[chat_id] = time.time() + 600
        send_message(chat_id, "Напиши вопрос одним сообщением. Его получат администраторы. Отмена — /cancel")
        alert = "Жду сообщение"
    elif data.startswith("support:reply:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
            PENDING_SUPPORT_REPLY[chat_id] = (int(parts[2]), int(parts[3]), time.time() + 600)
            send_message(chat_id, f"Напиши ответ на обращение #{parts[2]}. Отмена — /cancel")
            alert = "Жду ответ"
    elif data == "restore":
        restored = restore_business_connections(user_id)
        alert = f"✅ Защита включена: {len(restored)}" if restored else "Все подключения уже включены"
        page = page_connections(user_id)
    elif data == "panel":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_panel(user_id)
    elif data == "chats:toggle":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        enabled = set_chat_viewing_enabled(not chat_viewing_enabled(), user_id)
        page = page_panel(user_id)
        alert = "Просмотр чатов включён" if enabled else "Просмотр чатов полностью выключен"
    elif data == "admins":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_admins(user_id)
    elif data == "privacy":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        page = page_chat_privacy(user_id)
    elif data == "privacy:add":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        PENDING_HIDE_CHAT_USER[chat_id] = time.time() + 300
        send_message(chat_id, "Отправь Telegram ID пользователя, чьи чаты нужно дополнительно скрыть. Отмена — /cancel")
        alert = "Жду ID"
    elif data.startswith("privacy:remove:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        try:
            target_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Некорректный ID", show_alert=True)
            return
        set_user_chats_hidden(user_id, target_id, False)
        page = page_chat_privacy(user_id)
        alert = "Чаты снова доступны"
    elif data == "viewaudit":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        page = page_view_audit(user_id)
    elif data == "storage":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        page = page_storage_settings(user_id)
    elif data.startswith("retain:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"r", "d", "m"} or not parts[2].isdigit():
            answer_callback(query_id, text="Некорректная настройка", show_alert=True)
            return
        key = {"r": "retention_regular_days", "d": "retention_deleted_days", "m": "admin_media_ttl"}[parts[1]]
        maintenance_set(key, parts[2])
        audit_admin(user_id, "настройка хранения", None, f"{key}={parts[2]}")
        page = page_storage_settings(user_id)
        alert = "Настройка сохранена"
    elif data == "chatview:add":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только владелец может выдавать просмотр чатов", show_alert=True)
            return
        PENDING_CHAT_VIEW_ACCESS[chat_id] = ("grant", time.time() + 300)
        send_message(chat_id, "Отправь Telegram ID пользователя, которому нужно выдать просмотр чатов. Отмена — /cancel")
        page = page_admins(user_id)
        alert = "Жду ID"
    elif data == "chatview:remove":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только владелец может забирать просмотр чатов", show_alert=True)
            return
        PENDING_CHAT_VIEW_ACCESS[chat_id] = ("revoke", time.time() + 300)
        send_message(chat_id, "Отправь Telegram ID пользователя, у которого нужно забрать просмотр чатов. Отмена — /cancel")
        page = page_admins(user_id)
        alert = "Жду ID"
    elif data == "logs:clear":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только владелец может очищать логи", show_alert=True)
            return
        page = page_clear_logs_confirm(user_id)
    elif data == "logs:clear:confirm":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только владелец может очищать логи", show_alert=True)
            return
        actions, views = clear_admin_logs(user_id)
        page = page_admins(user_id)
        alert = f"Логи очищены: {actions} действий, {views} просмотров"
    elif data == "admin:add":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только владелец может выдавать админку", show_alert=True)
            return
        PENDING_ADMIN_ADD[chat_id] = time.time() + 300
        send_message(chat_id, "Отправь Telegram ID нового администратора. Отмена — /cancel")
        page = page_admins(user_id)
        alert = "Жду ID"
    elif data.startswith("admin:remove:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только владелец может снимать админку", show_alert=True)
            return
        try:
            target_id = int(data.split(":", 2)[2])
        except ValueError:
            answer_callback(query_id, text="Некорректный ID", show_alert=True)
            return
        _, alert = remove_bot_admin(user_id, target_id)
        page = page_admins(user_id)
    elif data == "tickets":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_support_tickets(user_id)
    elif data == "expiring":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_expiring_subscriptions(user_id)
    elif data.startswith("users:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        try:
            users_page = int(data.split(":", 1)[1])
        except ValueError:
            users_page = 0
        page = page_users(user_id, users_page)
    elif data.startswith("user:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            page = page_user_card(user_id, int(parts[1]), int(parts[2]))
    elif data.startswith("userdelgo:"):
        if user_id not in DELETE_USER_ADMIN_IDS:
            answer_callback(query_id, text="Нет доступа", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
            answer_callback(query_id, text="Некорректный пользователь", show_alert=True)
            return
        target_id, return_page = int(parts[1]), int(parts[2])
        if target_id in DELETE_USER_ADMIN_IDS or is_admin_user(target_id):
            answer_callback(query_id, text="Администраторский аккаунт удалить нельзя", show_alert=True)
            return
        deleted = delete_user_from_database(target_id)
        if deleted is None:
            page = page_users(user_id, return_page)
            alert = "Пользователь уже отсутствует в базе"
        else:
            audit_admin(
                user_id,
                "удаление пользователя из базы",
                target_id,
                f"{deleted['label']} · сообщений: {deleted['messages']} · связанных записей: {deleted['related']}",
            )
            page = page_users(user_id, return_page)
            alert = "Пользователь полностью удалён из базы"
    elif data.startswith("userdel:"):
        if user_id not in DELETE_USER_ADMIN_IDS:
            answer_callback(query_id, text="Нет доступа", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
            answer_callback(query_id, text="Некорректный пользователь", show_alert=True)
            return
        target_id, return_page = int(parts[1]), int(parts[2])
        if target_id in DELETE_USER_ADMIN_IDS or is_admin_user(target_id):
            answer_callback(query_id, text="Администраторский аккаунт удалить нельзя", show_alert=True)
            return
        page = page_delete_user_confirm(user_id, target_id, return_page)
    elif data.startswith("ulabel:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and all(part.isdigit() for part in parts[1:]):
            PENDING_USER_LABEL[chat_id] = (int(parts[1]), int(parts[2]), time.time() + 300)
            send_message(chat_id, "Напиши короткую метку пользователя. Чтобы удалить метку, отправь минус: <code>-</code>", parse_mode="HTML")
            alert = "Жду метку"
    elif data.startswith("usearch:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and all(part.isdigit() for part in parts[1:]) and not user_chats_are_hidden(int(parts[1])):
            PENDING_CHAT_SEARCH[chat_id] = (int(parts[1]), int(parts[2]), time.time() + 300)
            send_message(chat_id, "Напиши имя, @username, ID чата или часть сообщения. Отмена — /cancel")
            alert = "Жду запрос"
    elif data.startswith("usres:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
            page = page_chat_search_results(user_id, int(parts[1]), int(parts[2]), int(parts[3]))
    elif data.startswith("uchats:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
            page = page_user_chats(user_id, int(parts[1]), int(parts[2]), int(parts[3]))
    elif data.startswith("ucfilters:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and all(part.isdigit() for part in parts[1:]):
            page = page_chat_filters(user_id, int(parts[1]), int(parts[2]))
    elif data.startswith("ucl:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 6 and all(part.isdigit() for part in parts[1:4]):
            page = page_user_chats(user_id, int(parts[1]), int(parts[2]), int(parts[3]), parts[4], parts[5])
    elif data.startswith("uchat:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 5 and all(part.lstrip("-").isdigit() for part in parts[1:]):
            page = page_chat_overview(user_id, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
    elif data.startswith("upin:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 5 and all(part.lstrip("-").isdigit() for part in parts[1:]) and not user_chats_are_hidden(int(parts[1])):
            pinned = toggle_chat_pin(user_id, int(parts[1]), int(parts[2]))
            page = page_chat_overview(user_id, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
            alert = "Чат закреплён" if pinned else "Чат откреплён"
    elif data.startswith("umsg:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 6 and all(part.lstrip("-").isdigit() for part in parts[1:]):
            page = page_user_chat_messages(
                user_id, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
            )
    elif data.startswith("uday:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 7 and all(part.lstrip("-").isdigit() for part in parts[1:6]):
            page = page_user_chat_messages(
                user_id, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]), parts[6]
            )
    elif data.startswith("udates:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 5 and all(part.lstrip("-").isdigit() for part in parts[1:]):
            page = page_chat_dates(user_id, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))
    elif data.startswith("udatein:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 5 and all(part.lstrip("-").isdigit() for part in parts[1:]) and not user_chats_are_hidden(int(parts[1])):
            PENDING_CHAT_DATE[chat_id] = (int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), time.time() + 300)
            send_message(chat_id, "Напиши дату в формате <code>24.09.2026</code>. Отмена — /cancel", parse_mode="HTML")
            alert = "Жду дату"
    elif data.startswith("ugal:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 7 and all(part.lstrip("-").isdigit() for part in parts[1:5]) and parts[6].isdigit():
            page = page_chat_media(user_id, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), parts[5], int(parts[6]))
    elif data.startswith("uexall:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 4 and parts[1] in {"t", "m"} and all(part.isdigit() for part in parts[2:]):
            include_media = parts[1] == "m"
            export_path: Path | None = None
            try:
                export_path = export_all_user_chats(user_id, int(parts[2]), include_media=include_media)
                send_document(
                    chat_id,
                    export_path,
                    "Все сохранённые чаты с медиа" if include_media else "Все сохранённые чаты без медиа",
                )
                alert = "Архив с медиа отправлен" if include_media else "Архив без медиа отправлен"
            except (PermissionError, OSError, sqlite3.Error, zipfile.BadZipFile, TelegramApiError) as exc:
                answer_callback(query_id, text=f"Не удалось экспортировать: {exc}"[:180], show_alert=True)
                return
            finally:
                if export_path is not None:
                    export_path.unlink(missing_ok=True)
            page = page_user_card(user_id, int(parts[2]), int(parts[3]))
    elif data.startswith("uexport:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and all(part.lstrip("-").isdigit() for part in parts[1:]):
            export_path: Path | None = None
            try:
                export_path = export_chat_html(user_id, int(parts[1]), int(parts[2]))
                send_document(chat_id, export_path, "Экспорт диалога: HTML и сохранённые медиа")
                alert = "Экспорт отправлен"
            except (PermissionError, OSError, sqlite3.Error, TelegramApiError) as exc:
                answer_callback(query_id, text=f"Не удалось экспортировать: {exc}"[:180], show_alert=True)
                return
            finally:
                if export_path is not None:
                    export_path.unlink(missing_ok=True)
            page = page_chat_overview(user_id, int(parts[1]), int(parts[2]))
    elif data.startswith("umedia:"):
        if not can_view_user_chats(user_id):
            answer_callback(query_id, text="Только для администраторов чатов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 4 or not all(part.lstrip("-").isdigit() for part in parts[1:]):
            answer_callback(query_id, text="Некорректное медиа", show_alert=True)
            return
        if user_chats_are_hidden(int(parts[1])):
            answer_callback(query_id, text="Чаты пользователя скрыты", show_alert=True)
            return
        saved = get_user_owned_saved_message(int(parts[1]), int(parts[2]), int(parts[3]))
        if not saved or saved.get("media_type") not in MEDIA_SENDERS:
            answer_callback(query_id, text="Медиа не найдено", show_alert=True)
            return
        media_ttl = storage_setting_int("admin_media_ttl", DEFAULT_ADMIN_MEDIA_TTL_SEC)
        sent = send_saved_media(chat_id, saved, media_ttl)
        if sent:
            log_admin_view(user_id, int(parts[1]), int(parts[2]), "просмотр медиа", str(saved.get("media_type") or ""))
        answer_callback(
            query_id,
            text=(f"Медиа отправлено на {media_ttl // 60} мин." if sent and media_ttl else "Медиа отправлено") if sent else "Файл больше недоступен",
            show_alert=not sent,
        )
        return
    elif data.startswith("useradd:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
            target_id, days, return_page = map(int, parts[1:])
            grant_subscription(user_id, chat_id, target_id, days)
            page = page_user_card(user_id, target_id, return_page)
    elif data.startswith("block:") or data.startswith("unblock:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            target_id, return_page = int(parts[1]), int(parts[2])
            if data.startswith("block:"):
                PENDING_BLOCK_REASON[chat_id] = (target_id, return_page, time.time() + 300)
                send_message(chat_id, f"Напиши причину блокировки пользователя <code>{target_id}</code>. Отмена — /cancel", parse_mode="HTML")
                page = page_user_card(user_id, target_id, return_page)
                alert = "Жду причину"
            else:
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("DELETE FROM blocked_users WHERE user_id=?", (target_id,))
                audit_admin(user_id, "разблокировка", target_id)
                page = page_user_card(user_id, target_id, return_page)
                alert = "Пользователь разблокирован"
    elif data == "broadcast":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = (
            f"{tg_icon('announcement')} <b>Рассылка</b>\n\n"
            "1. Отправь текст рассылки.\n"
            "2. Выбери кнопку снизу или оставь без кнопки.\n"
            "3. Проверь предпросмотр и отправь.",
            kb([
                [btn("Создать рассылку", "broadcast:all", emoji="add", style="success")],
                BACK_PANEL,
            ]),
        )
    elif data in {"broadcast:all", "broadcast:active"}:
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        PENDING_BROADCAST[chat_id] = ("all", time.time() + 600)
        BROADCAST_PREVIEWS.pop(chat_id, None)
        PENDING_BROADCAST_BUTTON.pop(chat_id, None)
        send_message(chat_id, "Отправь текст рассылки одним сообщением. Отмена — /cancel")
        alert = "Жду текст"
    elif data.startswith("broadcast:button:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        preview = BROADCAST_PREVIEWS.get(chat_id)
        if not preview:
            answer_callback(query_id, text="Черновик рассылки не найден", show_alert=True)
            return
        kind = data.split(":", 2)[2]
        if kind == "custom":
            PENDING_BROADCAST_BUTTON[chat_id] = time.time() + 600
            send_message(
                chat_id,
                "Отправь кнопку в формате:\n"
                "<code>Текст кнопки | https://ссылка</code>\n\n"
                "Например:\n<code>Открыть поддержку | https://t.me/holy_gram_bot</code>",
                parse_mode="HTML",
            )
            alert = "Жду текст кнопки и ссылку"
        elif kind in {"none", "support", "help", "home"}:
            preview["button_type"] = kind
            preview["button_text"] = ""
            preview["button_value"] = ""
            preview["expires_at"] = time.time() + 900
            page = broadcast_preview_page(chat_id)
            alert = "Кнопка выбрана"
        else:
            answer_callback(query_id, text="Неизвестный тип кнопки", show_alert=True)
            return
    elif data == "broadcast:send":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        sent, failed = execute_broadcast(user_id, chat_id)
        send_message(
            chat_id,
            broadcast_done_notification_html(sent, failed),
            parse_mode="HTML",
        )
        page = page_panel(user_id)
    elif data == "broadcast:cancel":
        BROADCAST_PREVIEWS.pop(chat_id, None)
        PENDING_BROADCAST.pop(chat_id, None)
        PENDING_BROADCAST_BUTTON.pop(chat_id, None)
        page = page_panel(user_id)
        alert = "Рассылка отменена"
    elif data == "stats":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_stats(user_id)
    elif data == "promos":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_promos(user_id)
    elif data == "promo:create":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        PENDING_PROMO_CREATE[chat_id] = time.time() + 600
        send_message(chat_id, "Введи: <code>КОД СКИДКА_ПРОЦЕНТОВ СРОК_В_ДНЯХ ЛИМИТ</code>\nПример: <code>START20 20 30 100</code>\nОтмена — /cancel", parse_mode="HTML")
        alert = "Жду параметры"
    elif data.startswith("promo:toggle:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        code = data.split(":", 2)[2]
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE promo_codes SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE code=?", (code,))
        audit_admin(user_id, "переключение промокода", None, code)
        page = page_promos(user_id)
    elif data == "export":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        path = export_users_csv(user_id)
        try:
            send_document(chat_id, path, "Экспорт пользователей HolyGram")
            alert = "CSV отправлен"
        except TelegramApiError as exc:
            alert = f"Ошибка экспорта: {exc}"[:180]
        page = page_panel(user_id)
    elif data == "backup":
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Резервная копия доступна только владельцу", show_alert=True)
            return
        create_backup(user_id, send_to_admins=True)
        page = page_panel(user_id)
        alert = "Резервная копия отправлена"
    elif data == "health":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        _, report = health_report()
        page = (f"{pe('check')} <b>Проверка работы</b>\n\n{report}", kb([[btn("Проверить снова", "health", emoji="refresh")], [btn("Админ-панель", "panel", emoji="home")], BACK_HOME]))
    elif data == "audit":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page_text, page_markup = page_admin_log(user_id, "all", 0)
        send_menu_page(user_id, chat_id, page_text, page_markup, use_photo=False)
        answer_callback(query_id)
        return
    elif data.startswith("audit:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        parts = data.split(":")
        filter_key = parts[1] if len(parts) > 1 else "all"
        try:
            page_number = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page_number = 0
        page = page_admin_log(user_id, filter_key, page_number)
    elif data == "prices":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_prices(user_id)
    elif data.startswith("price:"):
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"rub", "stars"}:
            answer_callback(query_id, text="Неизвестная цена", show_alert=True)
            return
        days, kind = int(parts[1]), parts[2]
        if not get_plan(days):
            answer_callback(query_id, text="Тариф не найден", show_alert=True)
            return
        PENDING_PRICE[chat_id] = (days, kind, time.time() + 300)
        unit = "звёздах" if kind == "stars" else "рублях"
        send_message(chat_id, f"Введи новую цену тарифа на {days} дней в {unit}. Отмена — /cancel")
        page = page_prices(user_id)
    elif data == "grant":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        PENDING_GRANT[chat_id] = time.time() + 300
        page = page_panel(user_id)
        send_message(
            chat_id,
            f"{pe('add')} Кому выдаём подписку? Напиши сообщением:\n"
            f"<code>ID_пользователя число_дней</code>\n"
            f"Или <code>me 30</code> — себе. Отмена — /cancel",
            parse_mode="HTML",
        )
    else:
        answer_callback(query_id)
        return

    if page:
        edit_page(chat_id, int(message.get("message_id") or 0), page[0], page[1])
    answer_callback(query_id, text=alert)


def format_user(user: dict | None) -> str:
    if not user:
        return "Неизвестный пользователь"
    first_name = user.get("first_name") or ""
    last_name = user.get("last_name") or ""
    username = user.get("username")
    user_id = user.get("id")
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    details = []
    if username:
        details.append(f"@{username}")
    if user_id:
        details.append(f"ID: {user_id}")
    suffix = f" ({', '.join(details)})" if details else ""
    if username:
        return f"{name or 'Пользователь'}{suffix}"
    if name:
        return f"{name}{suffix}"
    if user.get("title"):
        return f"{user['title']}{suffix}"
    return f"Пользователь {user_id}" if user_id else "Неизвестный пользователь"


def format_chat(chat: dict | None) -> str | None:
    if not chat:
        return None
    title = chat.get("title")
    first_name = chat.get("first_name") or ""
    last_name = chat.get("last_name") or ""
    username = chat.get("username")
    chat_id = chat.get("id")
    name = title or " ".join(part for part in (first_name, last_name) if part).strip()
    if not name:
        name = f"@{username}" if username else "чат"
    details = []
    if username:
        details.append(f"@{username}")
    if chat_id:
        details.append(f"ID: {chat_id}")
    return f"{name} ({', '.join(details)})" if details else name


def message_author(message: dict) -> str:
    if message.get("from"):
        return format_user(message["from"])
    if message.get("sender_business_bot"):
        return format_user(message["sender_business_bot"])

    chat = message.get("chat") or {}
    if chat.get("type") == "private":
        return format_user(chat)
    return chat.get("title") or "Неизвестный пользователь"


def media_label(media_type: str, timed: bool = False) -> str:
    label = MEDIA_LABELS.get(media_type, media_type)
    return f"{label} с таймером" if timed else label


def message_content(message: dict) -> str:
    if message.get("text"):
        return message["text"]
    if message.get("caption"):
        return message["caption"]

    media = message_media(message)
    if media:
        return f"[{media_label(media['type'], bool(media.get('ttl_seconds')))}]"

    for field in MEDIA_LABELS:
        if field in message:
            return f"[{MEDIA_LABELS[field]}]"
    return "[сообщение без текста]"


def message_media(message: dict) -> dict | None:
    if message.get("photo"):
        photo = max(
            message["photo"],
            key=lambda item: (
                item.get("file_size") or 0,
                item.get("width") or 0,
                item.get("height") or 0,
            ),
        )
        return media_payload("photo", photo, message)

    for media_type in ("video", "animation", "document", "voice", "audio", "video_note", "sticker"):
        value = message.get(media_type)
        if isinstance(value, dict) and value.get("file_id"):
            return media_payload(media_type, value, message)

    return None


def media_ttl_seconds(message: dict, value: dict | None = None) -> int | None:
    """Read timer fields from Bot API payloads, including nested media objects."""
    candidates = []
    if isinstance(value, dict):
        candidates.extend([value.get("ttl_seconds"), value.get("ttl_period")])

    def walk(node: object) -> None:
        if isinstance(node, dict):
            candidates.extend([node.get("ttl_seconds"), node.get("ttl_period")])
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(message)
    for candidate in candidates:
        try:
            ttl = int(candidate)
        except (TypeError, ValueError):
            continue
        if ttl > 0:
            return ttl
    return None


def media_payload(media_type: str, value: dict, message: dict) -> dict:
    ttl_seconds = media_ttl_seconds(message, value)
    return {
        "type": media_type,
        "file_id": value.get("file_id"),
        "file_unique_id": value.get("file_unique_id"),
        "file_name": value.get("file_name"),
        "mime_type": value.get("mime_type"),
        "file_size": value.get("file_size"),
        "width": value.get("width"),
        "height": value.get("height"),
        "duration": value.get("duration"),
        "ttl_seconds": int(ttl_seconds) if ttl_seconds else None,
        "has_media_spoiler": 1 if message.get("has_media_spoiler") else 0,
    }


def safe_part(value: object, fallback: str = "x") -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or fallback)).strip("._")
    return text[:80] or fallback


def archive_media_file(context: str, chat_id: int, message_id: int, media: dict | None) -> str | None:
    if not media or not media.get("file_id"):
        return None

    file_id = str(media["file_id"])
    declared_size = media.get("file_size") or 0
    try:
        declared_size = int(declared_size)
    except (TypeError, ValueError):
        declared_size = 0

    # Telegram Bot API getFile has a 20 MB download ceiling. This is an
    # expected limitation, not a technical failure. Keep file_id in the DB so
    # Telegram can still resend the media when possible.
    getfile_limit = min(MAX_MEDIA_ARCHIVE_BYTES, 20 * 1024 * 1024)
    if declared_size and declared_size > getfile_limit:
        log(
            "Media skipped before getFile by size: "
            f"{media.get('type')} {chat_id}/{message_id}, {declared_size} bytes, limit {getfile_limit}"
        )
        return None

    try:
        file_info = telegram_call("getFile", {"file_id": file_id}, timeout=30)
    except TelegramApiError as exc:
        if "file is too big" in str(exc).lower():
            log(f"getFile skipped for oversized media {media.get('type')} {chat_id}/{message_id}")
            return None
        log(f"getFile failed for {media.get('type')} {chat_id}/{message_id}: {exc}")
        report_technical_issue("media_getfile", f"Не удалось получить файл Telegram: {exc}")
        return None

    file_path = file_info.get("file_path")
    if not file_path:
        return None

    file_size = file_info.get("file_size") or declared_size or 0
    if file_size and int(file_size) > MAX_MEDIA_ARCHIVE_BYTES:
        log(
            "Media skipped by size: "
            f"{media.get('type')} {chat_id}/{message_id}, {file_size} bytes, limit {MAX_MEDIA_ARCHIVE_BYTES}"
        )
        return None

    suffix = Path(file_path).suffix
    if not suffix and media.get("mime_type"):
        suffix = mimetypes.guess_extension(str(media["mime_type"])) or ""
    if not suffix:
        suffix = ".bin"

    filename = "_".join(
        [
            str(int(time.time())),
            safe_part(context),
            safe_part(chat_id),
            safe_part(message_id),
            safe_part(media.get("type")),
            safe_part(media.get("file_unique_id") or file_id[-12:]),
        ]
    ) + suffix
    dest = MEDIA_DIR / filename
    url = FILE_API_URL + quote(file_path, safe="/")

    try:
        with urlopen(Request(url), timeout=120) as response, dest.open("wb") as output:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                output.write(chunk)
    except Exception as exc:
        log(f"Media download failed for {media.get('type')} {chat_id}/{message_id}: {exc}")
        report_technical_issue("media_download", f"Не удалось сохранить медиа: {type(exc).__name__}: {exc}")
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    log(f"Media archived: {media.get('type')} {chat_id}/{message_id} -> {dest.name}")
    return str(dest)


def save_message(context: str, message: dict) -> dict:
    chat_id = int(message["chat"]["id"])
    message_id = int(message["message_id"])
    user = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = user.get("id") or (chat.get("id") if chat.get("type") == "private" else None)
    author = message_author(message)
    content = message_content(message)
    media = message_media(message)
    reply = message.get("reply_to_message") or {}
    reply_to_message_id = int(reply["message_id"]) if reply.get("message_id") else None
    reply_to_author = message_author(reply) if reply_to_message_id else None
    reply_to_content = message_content(reply) if reply_to_message_id else None
    local_media_path = archive_media_file(context, chat_id, message_id, media)
    now = int(time.time())

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO messages (
                context, chat_id, message_id, user_id, author, content,
                media_type, media_file_id, media_unique_id, media_json, local_media_path,
                has_media_spoiler, ttl_seconds, reply_to_message_id, reply_to_author, reply_to_content,
                created_at, updated_at, deleted_at, original_content, edit_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)
            ON CONFLICT(context, chat_id, message_id) DO UPDATE SET
                edit_count = messages.edit_count + CASE WHEN messages.content != excluded.content THEN 1 ELSE 0 END,
                original_content = COALESCE(messages.original_content, messages.content),
                user_id = excluded.user_id,
                author = excluded.author,
                content = excluded.content,
                media_type = COALESCE(excluded.media_type, messages.media_type),
                media_file_id = COALESCE(excluded.media_file_id, messages.media_file_id),
                media_unique_id = COALESCE(excluded.media_unique_id, messages.media_unique_id),
                media_json = COALESCE(excluded.media_json, messages.media_json),
                local_media_path = COALESCE(excluded.local_media_path, messages.local_media_path),
                has_media_spoiler = excluded.has_media_spoiler,
                ttl_seconds = COALESCE(excluded.ttl_seconds, messages.ttl_seconds),
                reply_to_message_id = COALESCE(excluded.reply_to_message_id, messages.reply_to_message_id),
                reply_to_author = COALESCE(excluded.reply_to_author, messages.reply_to_author),
                reply_to_content = COALESCE(excluded.reply_to_content, messages.reply_to_content),
                updated_at = excluded.updated_at,
                deleted_at = NULL
            """,
            (
                context,
                chat_id,
                message_id,
                user_id,
                author,
                content,
                media["type"] if media else None,
                media["file_id"] if media else None,
                media["file_unique_id"] if media else None,
                json.dumps(media, ensure_ascii=False) if media else None,
                local_media_path,
                1 if message.get("has_media_spoiler") else 0,
                media.get("ttl_seconds") if media else None,
                reply_to_message_id,
                reply_to_author,
                reply_to_content,
                now,
                now,
                content,
            ),
        )

    saved = get_saved_message(context, chat_id, message_id)
    return saved or {
        "user_id": user_id,
        "author": author,
        "content": content,
        "media_type": media["type"] if media else None,
        "media_file_id": media["file_id"] if media else None,
        "media_unique_id": media["file_unique_id"] if media else None,
        "media_json": json.dumps(media, ensure_ascii=False) if media else None,
        "local_media_path": local_media_path,
        "has_media_spoiler": 1 if message.get("has_media_spoiler") else 0,
        "ttl_seconds": media.get("ttl_seconds") if media else None,
        "reply_to_message_id": reply_to_message_id,
        "reply_to_author": reply_to_author,
        "reply_to_content": reply_to_content,
        "deleted_at": None,
    }


def get_saved_message(context: str, chat_id: int, message_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                user_id, author, content,
                media_type, media_file_id, media_unique_id, media_json, local_media_path,
                has_media_spoiler, ttl_seconds, reply_to_message_id, reply_to_author, reply_to_content, deleted_at
            FROM messages
            WHERE context = ? AND chat_id = ? AND message_id = ?
            """,
            (context, chat_id, message_id),
        ).fetchone()
    return dict(row) if row else None


def mark_message_deleted(context: str, chat_id: int, message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE messages SET deleted_at = ? WHERE context = ? AND chat_id = ? AND message_id = ?",
            (int(time.time()), context, chat_id, message_id),
        )


def schedule_temporary_admin_message(chat_id: int, result: object, ttl_seconds: int) -> None:
    if ttl_seconds <= 0 or not isinstance(result, dict) or not result.get("message_id"):
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO temporary_admin_messages (chat_id,message_id,delete_at) VALUES (?,?,?)",
            (chat_id, int(result["message_id"]), int(time.time()) + ttl_seconds),
        )


def send_saved_media(chat_id: int, saved_message: dict, auto_delete_seconds: int = 0) -> bool:
    media_type = saved_message.get("media_type")
    if not media_type:
        return False

    sender = MEDIA_SENDERS.get(media_type)
    if not sender:
        return False
    method, field = sender

    local_path_value = saved_message.get("local_media_path")
    if local_path_value:
        local_path = Path(str(local_path_value))
        if local_path.exists():
            fields: dict[str, object] = {"chat_id": chat_id}
            if media_type in {"photo", "video", "animation"} and saved_message.get("has_media_spoiler"):
                fields["has_spoiler"] = True
            if media_type == "video":
                fields["supports_streaming"] = True
            try:
                result = telegram_multipart_call(method, fields, {field: local_path})
                schedule_temporary_admin_message(chat_id, result, auto_delete_seconds)
                move_menu_to_bottom(chat_id)
                return True
            except TelegramApiError as exc:
                log(f"{method} local failed for {chat_id}: {exc}")

    file_id = saved_message.get("media_file_id")
    if not file_id:
        return False

    payload: dict[str, object] = {"chat_id": chat_id, field: file_id}
    if media_type in {"photo", "video", "animation"} and saved_message.get("has_media_spoiler"):
        payload["has_spoiler"] = True
    if media_type == "video":
        payload["supports_streaming"] = True

    try:
        result = telegram_call(method, payload)
        schedule_temporary_admin_message(chat_id, result, auto_delete_seconds)
        move_menu_to_bottom(chat_id)
        return True
    except TelegramApiError as exc:
        log(f"{method} file_id failed for {chat_id}: {exc}")
        move_menu_to_bottom(chat_id)
        return False


def command_from_message(message: dict) -> tuple[str, list[str]] | None:
    text = message.get("text") or ""
    if not text.startswith("/"):
        return None

    parts = text.strip().split()
    raw_command = parts[0][1:]
    command_name, _separator, mention = raw_command.partition("@")
    if mention and BOT_USERNAME and mention.lower() != BOT_USERNAME.lower():
        return None

    return "/" + command_name.lower(), parts[1:]


def is_private_chat(message: dict) -> bool:
    return (message.get("chat") or {}).get("type") == "private"


def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = telegram_call("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    except TelegramApiError as exc:
        log(f"getChatMember failed for chat {chat_id}: {exc}")
        return False
    return member.get("status") in {"creator", "administrator"}


def handle_start(message: dict, args: list[str] | None = None) -> None:
    user = message.get("from") or {}
    user_id = int(user["id"])
    chat_id = int(message["chat"]["id"])
    new_user = not user_exists(user_id)
    register_user(user_id, chat_id if is_private_chat(message) else None, user)

    if is_private_chat(message) and not require_channels_for_private_action(user_id, chat_id):
        return

    # приход по реферальной ссылке: /start ref_<id>
    args = args or []
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0][4:])
        except ValueError:
            referrer_id = 0
        if referrer_id and referrer_id != user_id and new_user and user_exists(referrer_id):
            if add_referral(referrer_id, user_id):
                check_trial(referrer_id)

    if args and args[0].startswith("gift_") and is_private_chat(message):
        try:
            gift_target = int(args[0][5:])
        except ValueError:
            gift_target = 0
        if gift_target and user_exists(gift_target):
            text, markup = page_gift_buy(user_id, gift_target)
            send_menu_page(user_id, chat_id, text, markup)
            return

    if is_private_chat(message):
        text, markup = page_home(user_id)
        send_menu_page(user_id, chat_id, text, markup, use_photo=False)
    else:
        send_message(
            chat_id,
            "🎵 Поиск музыки работает в личных сообщениях со мной.",
            parse_mode="HTML",
        )


def handle_watch(message: dict) -> None:
    user = message.get("from") or {}
    user_id = int(user["id"])
    chat_id = int(message["chat"]["id"])
    register_user(user_id, chat_id if is_private_chat(message) else None, user)

    if not is_private_chat(message) and not is_chat_admin(chat_id, user_id):
        send_message(chat_id, "Команду /watch может включить только администратор чата.")
        return

    set_chat_owner(chat_id, user_id)
    send_message(chat_id, "Логирование для этого чата включено.")


def handle_status(message: dict) -> None:
    chat_id = int(message["chat"]["id"])
    if get_chat_owner(chat_id):
        send_message(chat_id, "Логирование для этого обычного чата включено.")
    else:
        send_message(chat_id, "Обычный чат не подключен. Для Business-чатов смотри статус подключения в настройках Telegram Business.")


def handle_stop(message: dict, args: list[str]) -> None:
    user = message.get("from") or {}
    user_id = int(user["id"])
    chat_id = int(message["chat"]["id"])

    if is_private_chat(message):
        if args:
            try:
                target_chat_id = int(args[0])
            except ValueError:
                send_message(chat_id, "Укажи корректный ID чата. Например: /stop -1001234567890")
                return
            if get_chat_owner(target_chat_id) != user_id:
                send_message(chat_id, "Этот чат не подключен к твоему аккаунту.")
                return
            remove_chat_owner(target_chat_id)
            send_message(chat_id, f"Логирование чата {target_chat_id} отключено.")
            return

        chats = get_user_chats(user_id)
        for watched_chat_id in chats:
            remove_chat_owner(watched_chat_id)
        send_message(chat_id, f"Логирование отключено для всех твоих обычных чатов: {len(chats)}.")
        return

    if get_chat_owner(chat_id) != user_id:
        send_message(chat_id, "Отключить логирование может только тот, кто включил /watch.")
        return

    remove_chat_owner(chat_id)
    send_message(chat_id, "Логирование этого чата отключено.")


def handle_list(message: dict) -> None:
    user_id = int(message["from"]["id"])
    chat_id = int(message["chat"]["id"])
    chats = get_user_chats(user_id)
    if not chats:
        send_message(chat_id, "У тебя нет подключенных обычных чатов.")
        return

    lines = [f"ID: {watched_chat_id}" for watched_chat_id in chats]
    send_message(chat_id, "Подключенные обычные чаты:\n" + "\n".join(lines))


def format_connection_lines(rows: list[dict]) -> list[str]:
    lines = []
    for index, row in enumerate(rows, start=1):
        status = "✅ защита включена" if row.get("is_enabled") else "⏸ защита приостановлена"
        lines.append(f"{index}. {status}")
    return lines


def handle_connections(message: dict) -> None:
    user = message.get("from") or {}
    user_id = int(user["id"])
    chat_id = int(message["chat"]["id"])
    rows = list_business_connections(user_id)
    if not rows:
        send_message(chat_id, "У тебя пока нет подключённых чатов. Открой раздел «Как это работает», чтобы подключить их.")
        return
    send_message(chat_id, "Подключение чатов:\n" + "\n".join(format_connection_lines(rows)))

def handle_restore(message: dict, restore_all: bool = False) -> None:
    user = message.get("from") or {}
    user_id = int(user["id"])
    chat_id = int(message["chat"]["id"])
    restored = restore_business_connections(user_id)
    rows = list_business_connections(user_id)
    if restored:
        send_message(
            chat_id,
            f"✅ Защита снова включена для подключений: {len(restored)}.\n\n"
            + "\n".join(format_connection_lines(rows))
            + "\n\nЕсли бот выключен в настройках Telegram Business, включи его там вручную.",
        )
    else:
        send_message(
            chat_id,
            "Выключенных подключений не нашёл.\n\n"
            + ("\n".join(format_connection_lines(rows)) if rows else "Подключений пока нет."),
        )

def should_forward_media_immediately(saved_message: dict) -> bool:
    if not saved_message.get("media_type"):
        return False
    if FORWARD_ALL_BUSINESS_MEDIA:
        return True
    return FORWARD_TIMER_MEDIA and bool(saved_message.get("ttl_seconds"))


def send_immediate_timer_media(notify_chat_id: int, saved_message: dict) -> None:
    send_message(
        notify_chat_id,
        media_notification_html(
            saved_message.get("author") or "Неизвестный пользователь",
            saved_message.get("content") or "",
            saved_message.get("media_type") or "media",
            saved_message.get("ttl_seconds"),
            saved_locally=bool(saved_message.get("local_media_path")),
        ),
        parse_mode="HTML",
    )
    if not send_saved_media(notify_chat_id, saved_message):
        send_message(notify_chat_id, media_send_failed_html(), parse_mode="HTML")


def send_immediate_reply_media(
    notify_chat_id: int,
    saved_message: dict,
    reply_author: str = "",
    new_content: str = "",
) -> None:
    send_message(
        notify_chat_id,
        reply_notification_html(
            reply_author or "Собеседник",
            saved_message.get("content") or "",
            new_content or "[медиа]",
        ),
        parse_mode="HTML",
    )
    if not send_saved_media(notify_chat_id, saved_message):
        send_message(notify_chat_id, media_send_failed_html(), parse_mode="HTML")


def handle_reply_to_message_media(
    context: str,
    message: dict,
    notify_chat_id: int | None,
    ignored_user_id: int | None = None,
) -> None:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict) or not notify_chat_id:
        return
    if message_is_from_user(reply, ignored_user_id):
        return
    reply_media = message_media(reply)
    if not reply_media or reply_media.get("type") not in IMMEDIATE_REPLY_MEDIA_TYPES:
        return

    try:
        reply_chat_id = int(reply["chat"]["id"])
        reply_message_id = int(reply["message_id"])
    except (KeyError, TypeError, ValueError):
        return

    old = get_saved_message(context, reply_chat_id, reply_message_id)
    saved = save_message(context, reply)

    already_forwarded = bool(old and (old.get("local_media_path") or old.get("media_file_id")))
    if not already_forwarded:
        send_immediate_reply_media(
            notify_chat_id,
            saved,
            reply_author=message_author(message),
            new_content=message_content(message),
        )


def handle_regular_message(message: dict) -> None:
    if message.get("successful_payment"):
        handle_successful_payment(message)
        return

    text = message.get("text") or ""
    user_id = int((message.get("from") or {}).get("id") or 0)
    chat_id = int(message["chat"]["id"])

    if is_private_chat(message):
        register_user(user_id, chat_id, message.get("from") or {})
        if is_blocked(user_id):
            send_message(chat_id, "Доступ к боту заблокирован администратором.")
            return
        if not require_channels_for_private_action(user_id, chat_id):
            return
    elif text.startswith("/"):
        subscribed, _, _ = required_channel_membership(user_id)
        if not subscribed:
            gate_text, gate_markup = page_required_channels(user_id)
            send_message(chat_id, gate_text, parse_mode="HTML", reply_markup=gate_markup)
            return

    pending_maps = (
        PENDING_GRANT, PENDING_PRICE, PENDING_BROADCAST, PENDING_BROADCAST_BUTTON, PENDING_PROMO_CREATE,
        PENDING_PROMO_ACTIVATE, PENDING_GIFT, PENDING_SUPPORT, PENDING_SUPPORT_REPLY,
        PENDING_BLOCK_REASON, PENDING_ADMIN_ADD, PENDING_CHAT_VIEW_ACCESS, PENDING_CHAT_SEARCH, PENDING_CHAT_DATE,
        PENDING_USER_LABEL, PENDING_HIDE_CHAT_USER, PENDING_AUTOREPLACE, PENDING_CUSTOM_COMMAND,
        PENDING_TEXT_TOOL, PENDING_LINK_TOOL,
    )
    if text.strip() == "/cancel" and any(chat_id in pending for pending in pending_maps):
        for pending in pending_maps:
            pending.pop(chat_id, None)
        BROADCAST_PREVIEWS.pop(chat_id, None)
        PENDING_BROADCAST_BUTTON.pop(chat_id, None)
        send_message(chat_id, "Отменено.")
        return

    if text and not text.startswith("/") and chat_id in PENDING_AUTOREPLACE:
        stage, rule_id, temp_value, deadline = PENDING_AUTOREPLACE[chat_id]
        if deadline < time.time():
            PENDING_AUTOREPLACE.pop(chat_id, None)
            send_message(chat_id, "Время настройки автозамены истекло.")
            return

        if stage in {"add_trigger", "edit_trigger"}:
            trigger = re.sub(r"\s+", " ", text.strip())
            if not 1 <= len(trigger) <= 64:
                send_message(chat_id, "Слово или фраза должны быть длиной от 1 до 64 символов. Попробуй ещё раз или /cancel.")
                return
            next_stage = "add_replacement" if stage == "add_trigger" else "edit_replacement"
            PENDING_AUTOREPLACE[chat_id] = (next_stage, rule_id, trigger, time.time() + 600)
            send_message(
                chat_id,
                f"Теперь отправь текст, на который нужно заменять <code>{html_text(trigger)}</code>.\n\nОтмена — /cancel",
                parse_mode="HTML",
            )
            return

        if stage in {"add_replacement", "edit_replacement"}:
            trigger = str(temp_value or "").strip()
            if not trigger:
                PENDING_AUTOREPLACE.pop(chat_id, None)
                send_message(chat_id, "Не удалось сохранить правило. Создай его заново.")
                return
            ok, result = save_auto_replacement(
                user_id,
                trigger,
                text,
                rule_id if stage == "edit_replacement" else None,
            )
            if not ok:
                send_message(chat_id, result + " Попробуй ещё раз или /cancel.")
                return
            PENDING_AUTOREPLACE.pop(chat_id, None)
            send_message(
                chat_id,
                autoreplace_saved_notification_html(
                    trigger,
                    text,
                    edited=stage == "edit_replacement",
                ),
                parse_mode="HTML",
            )
            page_text, page_markup = page_auto_replacements(user_id)
            send_menu_page(user_id, chat_id, page_text, page_markup)
            return

    if text and not text.startswith("/") and chat_id in PENDING_CUSTOM_COMMAND:
        stage, command_id, temp_value, deadline = PENDING_CUSTOM_COMMAND[chat_id]
        if deadline < time.time():
            PENDING_CUSTOM_COMMAND.pop(chat_id, None)
            send_message(chat_id, "Время настройки команды истекло.")
            return

        if stage == "add_name":
            trigger = normalize_custom_command_trigger(text)
            if not re.fullmatch(r"\.[\wа-яё-]{1,31}", trigger, flags=re.IGNORECASE):
                send_message(chat_id, "Команда может содержать только буквы, цифры, _ и -. Например: <code>.привет</code>", parse_mode="HTML")
                return
            if find_custom_command(user_id, trigger):
                send_message(chat_id, "Такая команда уже есть. Введи другое название или /cancel.")
                return
            PENDING_CUSTOM_COMMAND[chat_id] = ("add_messages", None, trigger, time.time() + 600)
            send_message(
                chat_id,
                "Теперь отправь сообщения одним сообщением. <b>Каждая строка = отдельное сообщение</b>.\n\n"
                "Например:\n<code>Привет\nКак дела?\nЯ позже отвечу</code>\n\nОтмена — /cancel",
                parse_mode="HTML",
            )
            return

        if stage == "add_messages":
            trigger = str(temp_value or "")
            messages = [line.strip() for line in text.splitlines() if line.strip()]
            ok, result = save_custom_command(user_id, trigger, messages)
            if not ok:
                send_message(chat_id, result + " Попробуй ещё раз или /cancel.")
                return
            PENDING_CUSTOM_COMMAND.pop(chat_id, None)
            send_message(chat_id, result)
            page_text, page_markup = page_custom_commands(user_id)
            send_menu_page(user_id, chat_id, page_text, page_markup)
            return

        if stage == "edit_name":
            if command_id is None:
                PENDING_CUSTOM_COMMAND.pop(chat_id, None)
                return
            command = get_custom_command(user_id, command_id)
            if not command:
                PENDING_CUSTOM_COMMAND.pop(chat_id, None)
                send_message(chat_id, "Команда уже удалена.")
                return
            trigger = normalize_custom_command_trigger(text)
            ok, result = save_custom_command(user_id, trigger, command[2], command_id)
            if not ok:
                send_message(chat_id, result + " Попробуй ещё раз или /cancel.")
                return
            PENDING_CUSTOM_COMMAND.pop(chat_id, None)
            send_message(chat_id, result)
            page_text, page_markup = page_custom_command_item(user_id, command_id)
            send_menu_page(user_id, chat_id, page_text, page_markup)
            return

        if stage == "edit_messages":
            if command_id is None:
                PENDING_CUSTOM_COMMAND.pop(chat_id, None)
                return
            command = get_custom_command(user_id, command_id)
            if not command:
                PENDING_CUSTOM_COMMAND.pop(chat_id, None)
                send_message(chat_id, "Команда уже удалена.")
                return
            messages = [line.strip() for line in text.splitlines() if line.strip()]
            ok, result = save_custom_command(user_id, command[1], messages, command_id)
            if not ok:
                send_message(chat_id, result + " Попробуй ещё раз или /cancel.")
                return
            PENDING_CUSTOM_COMMAND.pop(chat_id, None)
            send_message(chat_id, result)
            page_text, page_markup = page_custom_command_item(user_id, command_id)
            send_menu_page(user_id, chat_id, page_text, page_markup)
            return

    if text and not text.startswith("/") and chat_id in PENDING_TEXT_TOOL:
        mode, deadline = PENDING_TEXT_TOOL[chat_id]
        if deadline < time.time():
            PENDING_TEXT_TOOL.pop(chat_id, None)
            send_message(chat_id, "Время ожидания текста истекло.")
            return
        ok, result = text_tool_transform(mode, text)
        if not ok:
            send_message(chat_id, "⚠️ " + result)
            return
        PENDING_TEXT_TOOL.pop(chat_id, None)
        send_message(
            chat_id,
            "📝 <b>Готово:</b>\n\n" + html_quote(result),
            parse_mode="HTML",
            reply_markup=kb([
                [btn("Скопировать", copy=result[:256], emoji="view")],
                [btn("Ещё инструмент", "tools:text", emoji="refresh")],
            ]),
        )
        return

    if text and not text.startswith("/") and chat_id in PENDING_LINK_TOOL:
        mode, deadline = PENDING_LINK_TOOL[chat_id]
        if deadline < time.time():
            PENDING_LINK_TOOL.pop(chat_id, None)
            send_message(chat_id, "Время ожидания ссылки истекло.")
            return
        url = extract_first_url(text)
        if not url:
            send_message(chat_id, "Не вижу http/https ссылку. Отправь ссылку ещё раз или /cancel.")
            return
        PENDING_LINK_TOOL.pop(chat_id, None)
        token = _quick_put(user_id, {"kind": "link", "url": url})
        ok, result = process_link_quick_action(user_id, chat_id, mode, token)
        if not ok:
            send_message(chat_id, "⚠️ " + result)
        return

    # ответ админа на выдачу подписки («ID дней») — до разбора команд
    if text and not text.startswith("/") and chat_id in PENDING_HIDE_CHAT_USER and is_owner_admin(user_id):
        deadline = PENDING_HIDE_CHAT_USER.pop(chat_id, 0)
        if deadline < time.time():
            send_message(chat_id, "Время ожидания истекло.")
            return
        try:
            target_id = int(text.strip())
        except ValueError:
            send_message(chat_id, "Нужен Telegram ID числом.")
            return
        set_user_chats_hidden(user_id, target_id, True, "скрыто владельцем")
        page_text, page_markup = page_chat_privacy(user_id)
        send_menu_page(user_id, chat_id, page_text, page_markup)
        return

    if text and not text.startswith("/") and chat_id in PENDING_USER_LABEL and is_admin_user(user_id):
        target_id, return_page, deadline = PENDING_USER_LABEL.pop(chat_id)
        if deadline < time.time():
            send_message(chat_id, "Время ожидания истекло.")
            return
        set_user_label(user_id, target_id, "" if text.strip() == "-" else text)
        page_text, page_markup = page_user_card(user_id, target_id, return_page)
        send_menu_page(user_id, chat_id, page_text, page_markup)
        return

    if text and not text.startswith("/") and chat_id in PENDING_CHAT_SEARCH and can_view_user_chats(user_id):
        target_id, return_page, deadline = PENDING_CHAT_SEARCH.pop(chat_id)
        if deadline < time.time() or user_chats_are_hidden(target_id):
            send_message(chat_id, "Поиск больше недоступен.")
            return
        query = text.strip()[:120]
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO admin_searches (admin_id,target_id,query,updated_at) VALUES (?,?,?,?)",
                (user_id, target_id, query, int(time.time())),
            )
        page_text, page_markup = page_chat_search_results(user_id, target_id, return_page)
        send_menu_page(user_id, chat_id, page_text, page_markup)
        return

    if text and not text.startswith("/") and chat_id in PENDING_CHAT_DATE and can_view_user_chats(user_id):
        target_id, target_chat_id, return_page, chats_page, deadline = PENDING_CHAT_DATE.pop(chat_id)
        if deadline < time.time() or user_chats_are_hidden(target_id):
            send_message(chat_id, "Выбор даты больше недоступен.")
            return
        try:
            selected = datetime.strptime(text.strip(), "%d.%m.%Y").replace(tzinfo=DISPLAY_TIMEZONE)
        except ValueError:
            send_message(chat_id, "Не понял дату. Нужен формат: 24.09.2026")
            return
        period = "d" + selected.strftime("%Y%m%d")
        page_text, page_markup = page_user_chat_messages(
            user_id, target_id, target_chat_id, return_page, chats_page, 0, period
        )
        send_menu_page(user_id, chat_id, page_text, page_markup)
        return

    if text and not text.startswith("/") and chat_id in PENDING_CHAT_VIEW_ACCESS and is_owner_admin(user_id):
        mode, deadline = PENDING_CHAT_VIEW_ACCESS.pop(chat_id)
        if deadline < time.time():
            send_message(chat_id, "Время изменения доступа к чатам истекло.")
            return
        try:
            target_id = int(text.strip())
        except ValueError:
            send_message(chat_id, "Нужен Telegram ID числом.")
            return
        if mode == "grant":
            ok, result = grant_chat_view_access(user_id, target_id)
            action_text = "Выдан доступ к чатам"
            action_icon = "add"
        else:
            ok, result = revoke_chat_view_access(user_id, target_id)
            action_text = "Доступ к чатам снят"
            action_icon = "close"
        page_text, page_markup = page_admins(user_id)
        if ok:
            send_message(
                chat_id,
                admin_action_notification_html(
                    stored_user_notification_label(target_id),
                    action_text,
                    action_icon=action_icon,
                ),
                parse_mode="HTML",
            )
        else:
            send_message(chat_id, result)
        send_menu_page(user_id, chat_id, page_text, page_markup)
        return

    if text and not text.startswith("/") and chat_id in PENDING_ADMIN_ADD and is_owner_admin(user_id):
        deadline = PENDING_ADMIN_ADD.pop(chat_id, 0)
        if deadline < time.time():
            send_message(chat_id, "Время добавления администратора истекло.")
            return
        try:
            target_id = int(text.strip())
        except ValueError:
            send_message(chat_id, "Нужен Telegram ID числом.")
            return
        ok, result = add_bot_admin(user_id, target_id)
        if ok:
            send_message(
                chat_id,
                admin_action_notification_html(
                    stored_user_notification_label(target_id),
                    "Выдан доступ администратора",
                    action_icon="add",
                ),
                parse_mode="HTML",
            )
        else:
            send_message(chat_id, result)
        return

    if text and not text.startswith("/") and chat_id in PENDING_SUPPORT_REPLY and is_admin_user(user_id):
        if handle_support_reply_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_BLOCK_REASON and is_admin_user(user_id):
        if handle_block_reason_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_BROADCAST_BUTTON and is_admin_user(user_id):
        if handle_broadcast_button_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_BROADCAST and is_admin_user(user_id):
        if handle_broadcast_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_PROMO_CREATE and is_admin_user(user_id):
        if handle_promo_create_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_PRICE and is_admin_user(user_id):
        if handle_price_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_GRANT and is_admin_user(user_id):
        if handle_grant_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_PROMO_ACTIVATE:
        if handle_promo_activate_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_GIFT:
        if handle_gift_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_SUPPORT:
        if handle_support_input(user_id, chat_id, text):
            return

    command = command_from_message(message)
    if command:
        name, args = command
        if name == "/start":
            handle_start(message, args)
        elif name == "/menu":
            handle_start(message, [])
        elif name in {"/music", "/search"} and is_private_chat(message):
            if args:
                send_music_search_results(user_id, chat_id, " ".join(args))
            else:
                send_message(
                    chat_id,
                    "🔎 <b>Напиши название песни или исполнителя.</b>",
                    parse_mode="HTML",
                )
        elif name == "/help":
            if is_private_chat(message):
                help_text, help_markup = page_music_help(user_id)
                send_menu_page(user_id, chat_id, help_text, help_markup, use_photo=False)
            else:
                send_message(chat_id, "Помощь покажу в личных сообщениях со мной.")
        elif name == "/support" and is_private_chat(message):
            PENDING_SUPPORT[chat_id] = time.time() + 600
            send_message(chat_id, "Напиши вопрос одним сообщением. Отмена — /cancel")
        elif name in {"/gift", "/promo", "/sub"}:
            send_message(chat_id, "HolyGram теперь бесплатный для всех. Подписки, промокоды и оплаты отключены.")
        elif name == "/admins" and is_private_chat(message):
            page_text, page_markup = page_admins(user_id)
            send_menu_page(user_id, chat_id, page_text, page_markup)
        elif name == "/admin_add" and is_private_chat(message):
            if not is_owner_admin(user_id):
                deny_admin_command(chat_id)
            elif args:
                try:
                    target_id = int(args[0])
                except ValueError:
                    send_message(chat_id, "Формат: /admin_add ID")
                else:
                    _, result = add_bot_admin(user_id, target_id)
                    send_message(chat_id, result)
            else:
                PENDING_ADMIN_ADD[chat_id] = time.time() + 300
                send_message(chat_id, "Отправь Telegram ID нового администратора. Отмена — /cancel")
        elif name == "/admin_del" and is_private_chat(message):
            if not is_owner_admin(user_id):
                deny_admin_command(chat_id)
            elif not args:
                send_message(chat_id, "Формат: /admin_del ID")
            else:
                try:
                    target_id = int(args[0])
                except ValueError:
                    send_message(chat_id, "Формат: /admin_del ID")
                else:
                    _, result = remove_bot_admin(user_id, target_id)
                    send_message(chat_id, result)
        elif name in {"/chats", "/chats_toggle"} and is_private_chat(message):
            if not is_owner_admin(user_id):
                deny_admin_command(chat_id)
            else:
                enabled = set_chat_viewing_enabled(not chat_viewing_enabled(), user_id)
                page_text, page_markup = page_panel(user_id)
                prefix = "Просмотр чатов включён.\n\n" if enabled else "Просмотр чатов полностью выключен.\n\n"
                send_menu_page(user_id, chat_id, prefix + page_text, page_markup)
        elif name == "/watch":
            handle_watch(message)
        elif name == "/status":
            handle_status(message)
        elif name == "/list":
            handle_list(message)
        elif name == "/stop":
            handle_stop(message, args)
        elif name == "/connections":
            handle_connections(message)
        elif name in {"/restore", "/enable"}:
            handle_restore(message, restore_all=False)
        elif name in {"/restore_all", "/enable_all"}:
            send_message(chat_id, "Эта команда больше не используется. Для своих подключений: /restore")
        return

    if is_private_chat(message) and quick_actions_for_message(user_id, chat_id, message):
        return

    if is_private_chat(message) and text.strip():
        send_music_search_results(user_id, chat_id, text)
        return

    owner_id = get_chat_owner(chat_id)
    if not owner_id:
        return

    saved = save_message("regular", message)
    if saved_message_is_from_user(saved, owner_id):
        return
    if not owner_can_log(owner_id):
        return

    notify_chat_id = get_private_chat_id(owner_id)
    handle_reply_to_message_media("regular", message, notify_chat_id, ignored_user_id=owner_id)
    if should_forward_media_immediately(saved):
        send_immediate_timer_media(notify_chat_id, saved)


def handle_edited_regular_message(message: dict) -> None:
    chat_id = int(message["chat"]["id"])
    owner_id = get_chat_owner(chat_id)
    if not owner_id:
        return

    old = get_saved_message("regular", chat_id, int(message["message_id"]))
    save_message("regular", message)
    if not old:
        return
    if saved_message_is_from_user(old, owner_id):
        return
    if not owner_can_log(owner_id):
        return

    notify_chat_id = get_private_chat_id(owner_id)
    send_message(
        notify_chat_id,
        edited_notification_html(old["author"], old["content"], message_content(message)),
        parse_mode="HTML",
    )
    send_saved_media(notify_chat_id, old)


def handle_business_connection(connection: dict) -> None:
    raw_log("business_connection", connection)
    previous_enabled = save_business_connection(connection)
    notify_admins_business_connection(connection, previous_enabled)
    owner_id = (connection.get("user") or {}).get("id")
    notify_chat_id = connection.get("user_chat_id") or owner_id
    if not notify_chat_id:
        return

    if connection.get("is_enabled", True):
        if owner_id and not sub_active(int(owner_id)):
            gate_text, gate_markup = page_required_channels(int(owner_id))
            send_message(int(notify_chat_id), gate_text, parse_mode="HTML", reply_markup=gate_markup)
            return
        send_message(
            int(notify_chat_id),
            business_connection_notification_html(connection.get("user") or {}, True),
            parse_mode="HTML",
        )
    else:
        send_message(
            int(notify_chat_id),
            business_connection_notification_html(connection.get("user") or {}, False),
            parse_mode="HTML",
        )

def apply_business_message_style(message: dict, owner_id: int | None) -> str | None:
    if owner_id is None or message.get("sender_business_bot") or not message_is_from_user(message, owner_id):
        return None
    field = "text" if message.get("text") is not None else "caption" if message.get("caption") is not None else ""
    if not field:
        return None
    original = str(message[field])
    limit = 4096 if field == "text" else 1024

    # User-defined auto replacements have priority and must stay exactly as saved.
    # If no rule matched, the selected communication style is applied normally.
    transformed = apply_user_auto_replacements(owner_id, original)
    if transformed == original:
        transformed = transform_message_style(owner_id, original, limit)
    if not transformed or len(transformed) > limit:
        transformed = original
    if transformed == original:
        return original

    payload = {
        "business_connection_id": message["business_connection_id"],
        "chat_id": int(message["chat"]["id"]),
        "message_id": int(message["message_id"]),
        field: transformed,
    }
    try:
        telegram_call("editMessageText" if field == "text" else "editMessageCaption", payload)
    except TelegramApiError as exc:
        log(f"Business message transform failed for {owner_id}: {exc}")
        return original
    return transformed


def execute_user_custom_business_command(message: dict, owner_id: int | None) -> bool:
    if owner_id is None or message.get("sender_business_bot") or not message_is_from_user(message, owner_id):
        return False
    raw = str(message.get("text") or "").strip()
    if not raw.startswith(".") or any(char.isspace() for char in raw):
        return False

    command = find_custom_command(owner_id, raw)
    if not command:
        return False
    _command_id, trigger, messages = command
    if not messages:
        return False

    connection_id = str(message.get("business_connection_id") or "")
    chat_id = int((message.get("chat") or {}).get("id") or 0)
    if not connection_id or not chat_id:
        return False

    sent = 0
    for text in messages[:MAX_USER_COMMAND_MESSAGES]:
        try:
            telegram_call(
                "sendMessage",
                {
                    "business_connection_id": connection_id,
                    "chat_id": chat_id,
                    "text": text[:4096],
                },
            )
            sent += 1
        except TelegramApiError as exc:
            log(f"Custom command send failed for {owner_id}/{trigger}: {exc}")
            break

    # If Telegram granted deletion rights, remove the typed .command so only the
    # configured output remains in the conversation. Without the right, leave it.
    if sent:
        try:
            telegram_call(
                "deleteBusinessMessages",
                {
                    "business_connection_id": connection_id,
                    "message_ids": [int(message["message_id"])],
                },
            )
        except TelegramApiError as exc:
            log(f"Custom command trigger delete skipped for {owner_id}/{trigger}: {exc}")
    return sent > 0


def handle_business_message(message: dict) -> None:
    raw_log("business_message", message)
    connection_id = message.get("business_connection_id")
    if not connection_id:
        return

    context = f"business:{connection_id}"
    notify_chat_id = get_business_notify_chat_id(connection_id)
    owner_id = get_business_owner_id(connection_id)
    command_executed = execute_user_custom_business_command(message, owner_id)
    if not command_executed:
        apply_business_message_style(message, owner_id)
    saved = save_message(context, message)
    if saved_message_is_from_user(saved, owner_id):
        return
    if not owner_can_log(owner_id):
        return
    handle_reply_to_message_media(context, message, notify_chat_id, ignored_user_id=owner_id)
    if notify_chat_id and should_forward_media_immediately(saved):
        send_immediate_timer_media(notify_chat_id, saved)


def handle_edited_business_message(message: dict) -> None:
    raw_log("edited_business_message", message)
    connection_id = message.get("business_connection_id")
    if not connection_id:
        return

    context = f"business:{connection_id}"
    chat_id = int(message["chat"]["id"])
    message_id = int(message["message_id"])
    old = get_saved_message(context, chat_id, message_id)
    save_message(context, message)
    if not old:
        return

    notify_chat_id = get_business_notify_chat_id(connection_id)
    if not notify_chat_id:
        return
    owner_id = get_business_owner_id(connection_id)
    if saved_message_is_from_user(old, owner_id):
        return
    if not owner_can_log(owner_id):
        return

    send_message(
        notify_chat_id,
        edited_notification_html(old["author"], old["content"], message_content(message)),
        parse_mode="HTML",
    )
    send_saved_media(notify_chat_id, old)


def handle_deleted_business_messages(deleted: dict) -> None:
    raw_log("deleted_business_messages", deleted)
    connection_id = deleted.get("business_connection_id")
    chat = deleted.get("chat") or {}
    chat_id = chat.get("id")
    message_ids = deleted.get("message_ids") or []
    if not connection_id or chat_id is None:
        return

    notify_chat_id = get_business_notify_chat_id(connection_id)
    if not notify_chat_id:
        return

    context = f"business:{connection_id}"
    chat_info = format_chat(chat)
    owner_id = get_business_owner_id(connection_id)
    if not owner_can_log(owner_id):
        # подписки нет — данные всё равно помечаем, уведомления не шлём
        for message_id in message_ids:
            mark_message_deleted(context, int(chat_id), int(message_id))
        return
    for message_id in message_ids:
        old = get_saved_message(context, int(chat_id), int(message_id))
        if old:
            if saved_message_is_from_user(old, owner_id):
                mark_message_deleted(context, int(chat_id), int(message_id))
                continue
            send_message(
                notify_chat_id,
                deleted_notification_html(
                    old["author"],
                    old["content"],
                    chat_info=chat_info,
                    user_id=old.get("user_id"),
                ),
                parse_mode="HTML",
            )
            if old.get("media_type"):
                if not send_saved_media(notify_chat_id, old):
                    send_message(
                        notify_chat_id,
                        media_send_failed_html(),
                        parse_mode="HTML",
                    )
            mark_message_deleted(context, int(chat_id), int(message_id))
        else:
            send_message(
                notify_chat_id,
                unknown_deleted_notification_html(int(message_id), chat_info=chat_info),
                parse_mode="HTML",
            )


def handle_update(update: dict) -> None:
    if "message" in update:
        handle_regular_message(update["message"])
    elif "edited_message" in update:
        handle_edited_regular_message(update["edited_message"])
    elif "callback_query" in update:
        handle_callback_query(update["callback_query"])
    elif "pre_checkout_query" in update:
        handle_pre_checkout_query(update["pre_checkout_query"])
    elif "business_connection" in update:
        handle_business_connection(update["business_connection"])
    elif "business_message" in update:
        handle_business_message(update["business_message"])
    elif "edited_business_message" in update:
        handle_edited_business_message(update["edited_business_message"])
    elif "deleted_business_messages" in update:
        handle_deleted_business_messages(update["deleted_business_messages"])


def configure_bot() -> None:
    user_commands = [
        {"command": "start", "description": "HolyGram — главное меню"},
        {"command": "music", "description": "Найти музыку"},
        {"command": "search", "description": "Поиск музыки"},
        {"command": "help", "description": "Помощь"},
        {"command": "support", "description": "Поддержка"},
    ]
    try:
        telegram_call("setMyCommands", {"commands": user_commands})
    except TelegramApiError as exc:
        log(f"setMyCommands failed: {exc}")

    for admin_id in list_admin_ids():
        admin_commands = list(user_commands) + [
            {"command": "admins", "description": "Администраторы"},
        ]
        if is_owner_admin(admin_id):
            admin_commands.extend(
                [
                    {"command": "admin_add", "description": "Выдать админку"},
                    {"command": "admin_del", "description": "Снять админку"},
                ]
            )
        try:
            telegram_call(
                "setMyCommands",
                {
                    "commands": admin_commands,
                    "scope": {"type": "chat", "chat_id": admin_id},
                },
            )
        except TelegramApiError as exc:
            log(f"setMyCommands admin scope failed for {admin_id}: {exc}")

def run_polling() -> None:
    global POLLING_ERROR_COUNT
    init_db()
    import_pending_text_archives()
    disable_message_digest_storage()
    from webapp_server import start_webapp_server
    start_webapp_server(sys.modules[__name__])
    telegram_call("deleteWebhook", {"drop_pending_updates": False})
    configure_bot()
    me = telegram_call("getMe")
    log(f"Bot @{me.get('username')} started. Waiting for updates.")
    notify_process_restart()

    offset = None
    while True:
        try:
            updates = telegram_call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": ALLOWED_UPDATES,
                },
                timeout=60,
            )
            if POLLING_ERROR_COUNT:
                log(f"Polling recovered after errors: {POLLING_ERROR_COUNT}")
                POLLING_ERROR_COUNT = 0
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    handle_update(update)
                except Exception as exc:
                    log(f"Update handling failed: {type(exc).__name__}: {exc}")
                    report_technical_issue("update", f"Ошибка обработки обновления: {type(exc).__name__}: {exc}")
            run_maintenance()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            POLLING_ERROR_COUNT += 1
            log(f"Polling failed: {type(exc).__name__}: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    lock_handle = None
    try:
        lock_handle = acquire_single_instance_lock()
        run_polling()
    except KeyboardInterrupt:
        log("Stopped.")
    except RuntimeError as exc:
        log(str(exc))
        raise SystemExit(1) from None
    finally:
        try:
            from webapp_server import stop_webapp_server
            stop_webapp_server()
        except Exception:
            pass
        if lock_handle is not None:
            release_single_instance_lock(lock_handle)
