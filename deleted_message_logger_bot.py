from __future__ import annotations

import html
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

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "logger_data"
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "bot_test.sqlite3"
LOG_PATH = DATA_DIR / "bot.log"
LOCK_PATH = DATA_DIR / "bot.lock"
RAW_UPDATES_PATH = DATA_DIR / "raw_updates.jsonl"

DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env.deleted_logger", encoding="utf-8-sig", override=True)

BOT_TOKEN = os.getenv("LOGGER_BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("LOGGER_BOT_USERNAME", "").strip().lstrip("@")
ADMIN_USER_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_USER_IDS", "").replace(";", ",").split(",")
    if item.strip().isdigit()
}
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

if not BOT_TOKEN:
    raise RuntimeError("Set LOGGER_BOT_TOKEN in .env.deleted_logger")

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


def html_text(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


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


def register_user(user_id: int, private_chat_id: int | None = None) -> None:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, private_chat_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                private_chat_id = COALESCE(excluded.private_chat_id, users.private_chat_id),
                updated_at = excluded.updated_at
            """,
            (user_id, private_chat_id, now, now),
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
        register_user(int(owner_id), int(notify_chat_id) if notify_chat_id else None)

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


def is_admin_user(user_id: int | None) -> bool:
    return bool(user_id is not None and user_id in ADMIN_USER_IDS)


def deny_admin_command(chat_id: int) -> None:
    send_message(chat_id, "Эта команда доступна только владельцу бота.")


# ============================================================================
# ГЛАВНОЕ МЕНЮ · ПОДПИСКА (Telegram Stars) · РЕФЕРАЛКА · ПОМОЩЬ · АДМИН-ПАНЕЛЬ
# Премиум-эмодзи из пака https://t.me/addemoji/NewsEmoji — id как в
# limuzinov_shop_bot: в тексте через <tg-emoji emoji-id="…">, в кнопках через
# icon_custom_emoji_id.
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
    return get_sub(user_id)[0] > int(time.time())


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

def page_home(user_id: int) -> tuple[str, dict]:
    rows = [
        [
            btn("Купить подписку", "buy", emoji="stars", style="success"),
            btn("Пригласить друзей", "ref", emoji="invite"),
        ],
        [
            btn("Помощь", "help", emoji="support"),
            btn("Мои подключения", "conns", emoji="view"),
        ],
    ]
    if is_admin_user(user_id):
        rows.append([btn("Панель админа", "panel", emoji="admin")])
    text = (
        f"{pe('home')} <b>Holly Bot</b> — от тебя больше ничего не скроют\n\n"
        f"{pe('check')} удалённые сообщения — сохраним и пришлём\n"
        f"{pe('check')} правки сообщений — покажем «было / стало»\n"
        f"{pe('check')} сгоревшие фото и видео — в архив\n"
        f"{pe('check')} кружки, голосовые, файлы, стикеры — тоже\n\n"
        f"{pe('stars')} Подписка: {status_line(user_id)}\n"
        f"{pe('invite')} Приглашено: {ref_count(user_id)}/{REF_REQUIRED} — за {REF_REQUIRED} друзей дадим {REF_DAYS} дня бесплатно"
    )
    return text, kb(rows)


def page_buy(user_id: int) -> tuple[str, dict]:
    plans = get_plans()
    p15, p30 = plans[15], plans[30]
    text = (
        f"{pe('stars')} <b>Подписка Holly Bot</b>\n\n"
        f"Сейчас: {status_line(user_id)}\n\n"
        f"<b>15 дней</b> — {p15['rub']} ₽ или {p15['stars']} ⭐\n"
        f"<b>30 дней</b> — {p30['rub']} ₽ или {p30['stars']} ⭐\n\n"
        f"Выбери СБП или Telegram Stars — доступ продлевается сразу после подтверждения оплаты.\n"
        f"Хочешь бесплатно? Пригласи {REF_REQUIRED} друзей — {REF_DAYS} дня в подарок "
        f"(кнопка «Пригласить друзей»)."
    )
    rows = [
        [
            btn(f"15 дней — {p15['rub']} ₽", "buy:sbp:15", emoji="pay", style="success"),
            btn(f"{p15['stars']} ⭐", "buy:stars:15", emoji="stars", style="success"),
        ],
        [
            btn(f"30 дней — {p30['rub']} ₽", "buy:sbp:30", emoji="pay", style="success"),
            btn(f"{p30['stars']} ⭐", "buy:stars:30", emoji="stars", style="success"),
        ],
        [btn("Пригласить друзей", "ref", emoji="invite")],
        BACK_HOME,
    ]
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
        [btn("Скопировать ссылку", copy=link, emoji="invite", style="primary")],
        [btn("Поделиться", url=f"https://t.me/share/url?url={quote(link, safe='')}&text=Хочу%20попробовать%20Holly%20Bot", emoji="support")],
        BACK_HOME,
    ]
    return text, kb(rows)


def page_help(user_id: int) -> tuple[str, dict]:
    username = bot_username()
    plans = get_plans()
    text = (
        f"{pe('support')} <b>Как подключить бота</b>\n\n"
        f"<b>1.</b> Открой Telegram → <b>Настройки</b> → <b>Telegram Business</b>\n"
        f"<b>2.</b> Раздел <b>Chatbots</b> (помощник в личных чатах)\n"
        f"<b>3.</b> Нажми «Добавить бота» и вбей <code>@{username}</code>\n"
        f"<b>4.</b> Разреши доступ — выбери чаты, за которыми следим\n"
        f"<b>5.</b> Готово: всё удалённое и исправленное прилетает сюда\n\n"
        f"<b>Обычная группа:</b> добавь бота в группу и напиши там /watch\n"
        f"(отключить — /stop, статус — /status)\n\n"
        f"{pe('stars')} Подписка: 15 дней — {plans[15]['rub']} ₽ / {plans[15]['stars']} ⭐, "
        f"30 дней — {plans[30]['rub']} ₽ / {plans[30]['stars']} ⭐.\n"
        f"{pe('gift')} Не хочешь платить? Пригласи {REF_REQUIRED} друзей — {REF_DAYS} дня бесплатно."
    )
    rows = [
        [btn("Купить подписку", "buy", emoji="stars", style="success")],
        BACK_HOME,
    ]
    return text, kb(rows)


def page_connections(user_id: int) -> tuple[str, dict]:
    rows = list_business_connections(None if is_admin_user(user_id) else user_id)
    if rows:
        scope = "все" if is_admin_user(user_id) else "твои"
        body = "\n".join(html_text(line) for line in format_connection_lines(rows))
        text = f"{pe('view')} <b>Business-подключения</b> ({scope}):\n\n{body}"
    else:
        text = (
            f"{pe('view')} <b>Business-подключений пока нет.</b>\n\n"
            f"Подключи бота: Настройки → Telegram Business → Chatbots → @{bot_username()}\n"
            f"Инструкция с шагами — в разделе «Помощь»."
        )
    markup = kb([[btn("Включить выключенные", "restore", emoji="refresh")], BACK_HOME])
    return text, markup


def page_panel(user_id: int) -> tuple[str, dict]:
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        actives = conn.execute("SELECT COUNT(*) FROM subs WHERE until_ts > ?", (now,)).fetchone()[0]
        refs = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        pays = conn.execute("SELECT COUNT(*), COALESCE(SUM(stars), 0) FROM payments").fetchone()
        sbp = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(rub), 0) FROM sbp_payments WHERE status = 'paid'"
        ).fetchone()
    text = (
        f"{pe('admin')} <b>Админ-панель</b>\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Активных подписок: <b>{actives}</b>\n"
        f"Реферальных связок: <b>{refs}</b>\n"
        f"Stars-платежей: <b>{pays[0]}</b> на <b>{pays[1]} ⭐</b>\n"
        f"СБП-платежей: <b>{sbp[0]}</b> на <b>{sbp[1]} ₽</b>\n\n"
        f"Быстрая выдача: <code>/sub ID_ПОЛЬЗОВАТЕЛЯ ДНЕЙ</code> (например <code>/sub 123456 30</code>), "
        f"снять — <code>/sub ID_ДНЯХ 0</code>."
    )
    rows = [
        [btn("Выдать подписку", "grant", emoji="add", style="success")],
        [btn("Изменить цены", "prices", emoji="pay")],
        [btn("Обновить", "panel", emoji="refresh")],
        BACK_HOME,
    ]
    return text, kb(rows)


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
            telegram_call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                },
            )
            return
        except TelegramApiError as exc:
            if "message is not modified" in str(exc):
                return
            log(f"editMessageText failed: {exc}")
    send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)


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


def send_sbp_payment(user_id: int, chat_id: int, days: int) -> None:
    plan = get_plan(days)
    if not plan:
        send_message(chat_id, "Тариф больше не доступен. Обнови меню.")
        return
    if not ROLLYPAY_ENABLED:
        send_message(chat_id, "Оплата по СБП временно недоступна. Выбери Telegram Stars.")
        return
    rub = plan["rub"]
    order_id = f"sub-{uuid.uuid4()}"
    payload: dict[str, object] = {
        "amount": f"{rub:.2f}",
        "payment_currency": "RUB",
        "order_id": order_id,
        "description": f"Подписка Holly Bot на {days} дней",
        "customer_id": str(user_id),
        "metadata": {"telegram_user_id": str(user_id), "days": str(days)},
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
        send_message(chat_id, "Не удалось создать платёж СБП. Попробуй ещё раз через минуту.")
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO sbp_payments (payment_id, order_id, user_id, days, rub, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'created', ?)
            """,
            (payment_id, order_id, user_id, days, rub, int(time.time())),
        )
    markup = kb(
        [
            [btn(f"Оплатить {rub} ₽ по СБП", url=pay_url, emoji="pay", style="success")],
            [btn("Проверить оплату", f"sbp:check:{order_id}", emoji="refresh")],
            [btn("Назад к тарифам", "buy", emoji="home")],
        ]
    )
    send_message(
        chat_id,
        f"{pe('pay')} <b>Счёт СБП создан</b>\n\n"
        f"Тариф: <b>{days} дней</b>\nК оплате: <b>{rub} ₽</b>\n\n"
        f"После оплаты вернись сюда и нажми «Проверить оплату».",
        parse_mode="HTML",
        reply_markup=markup,
    )


def check_sbp_payment(user_id: int, order_id: str) -> tuple[bool, str]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT payment_id, user_id, days, rub, status FROM sbp_payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if not row or int(row[1]) != user_id:
        return False, "Платёж не найден"
    payment_id, _, days, rub, status = row
    if status == "paid":
        return True, "Этот платёж уже зачислен"
    try:
        payment = rollypay_call("GET", f"/api/v1/payments/{quote(payment_id, safe='')}")
    except RuntimeError as exc:
        log(f"SBP payment check failed for {payment_id}: {exc}")
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
        until = add_days(user_id, int(days))
        notify_admins_sbp_payment(user_id, int(days), int(rub))
        return True, f"Оплачено! Подписка активна до {format_until(until)}"
    return True, "Этот платёж уже зачислен"

def send_subscription_invoice(user_id: int, chat_id: int, days: int) -> None:
    plan = get_plan(days)
    if not plan:
        send_message(chat_id, "Тариф больше не доступен. Обнови меню.")
        return
    stars = plan["stars"]
    try:
        telegram_call(
            "sendInvoice",
            {
                "chat_id": chat_id,
                "title": f"Подписка Holly Bot — {days} дней",
                "description": "Доступ ко всем функциям бота. Остаток суммируется при продлении.",
                "payload": f"sub:{user_id}:{days}:{stars}",
                "provider_token": "",
                "currency": "XTR",
                "prices": [{"label": f"{days} дней подписки", "amount": stars}],
            },
        )
    except TelegramApiError as exc:
        log(f"sendInvoice failed for {user_id}: {exc}")
        send_message(chat_id, "Не удалось создать счёт на оплату. Попробуй ещё раз через минуту.")


def handle_pre_checkout_query(query: dict) -> None:
    qid = str(query.get("id") or "")
    parts = str(query.get("invoice_payload") or "").split(":")
    valid = False
    if len(parts) == 4 and parts[0] == "sub" and query.get("currency") == "XTR":
        try:
            uid, days, stars = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            uid = days = stars = 0
        payer_id = (query.get("from") or {}).get("id")
        valid = (
            uid
            and uid == payer_id
            and (get_plan(days) or {}).get("stars") == stars
            and int(query.get("total_amount") or 0) == stars
        )
    if valid:
        telegram_call("answerPreCheckoutQuery", {"pre_checkout_query_id": qid, "ok": True})
    else:
        telegram_call(
            "answerPreCheckoutQuery",
            {"pre_checkout_query_id": qid, "ok": False, "error_message": "Тариф устарел — открой меню заново"},
        )


def notify_admins_payment(user_id: int, days: int, stars: int) -> None:
    line = (
        f"{pe('pay')} Оплата: <code>{user_id}</code> купил {days} дн. за {stars} ⭐"
    )
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COALESCE(SUM(stars), 0) FROM payments").fetchone()[0]
    line += f"\nВсего собрано: {total} ⭐"
    for admin_id in sorted(ADMIN_USER_IDS):
        try:
            send_message(admin_id, line, parse_mode="HTML")
        except TelegramApiError:
            pass


def notify_admins_sbp_payment(user_id: int, days: int, rub: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(rub), 0) FROM sbp_payments WHERE status = 'paid'"
        ).fetchone()[0]
    line = f"{pe('pay')} Оплата: <code>{user_id}</code> купил {days} дн. за {rub} ₽\nВсего собрано: {total} ₽"
    for admin_id in sorted(ADMIN_USER_IDS):
        send_message(admin_id, line, parse_mode="HTML")


def handle_successful_payment(message: dict) -> None:
    payment = message.get("successful_payment") or {}
    parts = str(payment.get("invoice_payload") or "").split(":")
    if len(parts) != 4 or parts[0] != "sub" or payment.get("currency") != "XTR":
        return
    try:
        uid, days, stars = int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        return
    if (get_plan(days) or {}).get("stars") != stars or int(payment.get("total_amount") or 0) != stars:
        log(f"Rejected payment payload: {payment.get('invoice_payload')}")
        return

    payer_id = int((message.get("from") or {}).get("id") or uid)
    tg_charge = str(payment.get("telegram_charge_id") or f"manual:{int(time.time())}:{payer_id}")
    now = int(time.time())
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO payments (tg_payment_id, user_id, days, stars, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tg_charge, payer_id, days, stars, payment.get("invoice_payload"), now),
        )
        if cur.rowcount == 0:
            return  # такой платёж уже обработан
    until = add_days(payer_id, days)
    chat_id = int(message.get("chat", {}).get("id") or get_private_chat_id(payer_id))
    send_message(
        chat_id,
        f"{pe('check')} <b>Оплачено!</b> Подписка активна до <b>{format_until(until)}</b>.\n"
        f"Спасибо! {pe('home')} Меню — /start",
        parse_mode="HTML",
    )
    notify_admins_payment(payer_id, days, stars)


# --- выдача подписки админом ----------------------------------------------------

def grant_subscription(admin_id: int, chat_id: int, target_id: int, days: int) -> None:
    if not 1 <= days <= 3650:
        send_message(chat_id, "Дней должно быть от 1 до 3650.")
        return
    until = add_days(target_id, days)
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


def handle_price_input(chat_id: int, text: str) -> bool:
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
    unit = "⭐" if kind == "stars" else "₽"
    send_message(chat_id, f"Цена тарифа на {days} дней изменена: {value} {unit}.")
    return True


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

    register_user(user_id, chat_id)

    page: tuple[str, dict] | None = None
    alert: str | None = None

    if data == "home":
        page = page_home(user_id)
    elif data == "buy":
        page = page_buy(user_id)
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
    elif data == "restore":
        restored = restore_business_connections(None if is_admin_user(user_id) else user_id)
        alert = f"Включил подключений: {len(restored)}" if restored else "Отключённых не нашёл"
        page = page_connections(user_id)
    elif data == "panel":
        if not is_admin_user(user_id):
            answer_callback(query_id, text="Только для админов", show_alert=True)
            return
        page = page_panel(user_id)
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


def media_payload(media_type: str, value: dict, message: dict) -> dict:
    ttl_seconds = value.get("ttl_seconds") or message.get("ttl_seconds")
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
    try:
        file_info = telegram_call("getFile", {"file_id": file_id}, timeout=30)
    except TelegramApiError as exc:
        log(f"getFile failed for {media.get('type')} {chat_id}/{message_id}: {exc}")
        return None

    file_path = file_info.get("file_path")
    if not file_path:
        return None

    file_size = file_info.get("file_size") or media.get("file_size") or 0
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
    local_media_path = archive_media_file(context, chat_id, message_id, media)
    now = int(time.time())

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO messages (
                context, chat_id, message_id, user_id, author, content,
                media_type, media_file_id, media_unique_id, media_json, local_media_path,
                has_media_spoiler, ttl_seconds, created_at, updated_at, deleted_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
                has_media_spoiler, ttl_seconds, deleted_at
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
        return True
    except TelegramApiError as exc:
        log(f"{method} file_id failed for {chat_id}: {exc}")
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
    register_user(user_id, chat_id if is_private_chat(message) else None)

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

    if is_private_chat(message):
        text, markup = page_home(user_id)
        send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
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
    register_user(user_id, chat_id if is_private_chat(message) else None)

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
        status = "включен" if row.get("is_enabled") else "отключен"
        can_reply = row.get("can_reply")
        reply_text = "да" if can_reply else "нет" if can_reply == 0 else "неизвестно"
        lines.append(
            f"{index}. owner_id={row.get('owner_id')} | notify={row.get('notify_chat_id')} | "
            f"{status} | replies={reply_text} | {short_connection_id(row.get('connection_id'))}"
        )
    return lines


def handle_connections(message: dict) -> None:
    user = message.get("from") or {}
    user_id = int(user["id"])
    chat_id = int(message["chat"]["id"])

    rows = list_business_connections(None if is_admin_user(user_id) else user_id)
    if not rows:
        send_message(chat_id, "Business-подключений в базе нет.")
        return

    scope = "все подключения" if is_admin_user(user_id) else "твои подключения"
    send_message(chat_id, f"Business-подключения ({scope}):\n" + "\n".join(format_connection_lines(rows)))


def handle_restore(message: dict, restore_all: bool = False) -> None:
    user = message.get("from") or {}
    user_id = int(user["id"])
    chat_id = int(message["chat"]["id"])

    if restore_all and not is_admin_user(user_id):
        deny_admin_command(chat_id)
        return

    target_owner_id = None if restore_all else user_id
    restored = restore_business_connections(target_owner_id)
    rows = list_business_connections(target_owner_id)

    if restored:
        send_message(
            chat_id,
            f"Включил обратно подключений в базе: {len(restored)}.\n\n"
            + "\n".join(format_connection_lines(rows))
            + "\n\nЕсли аккаунт отключил бота в настройках Telegram Business, его всё равно нужно включить там вручную.",
        )
    else:
        send_message(
            chat_id,
            "Отключенных Business-подключений в базе не нашел.\n\n"
            + ("\n".join(format_connection_lines(rows)) if rows else "Подключений нет.")
            + "\n\nЕсли бот исчез из чатов после отключения в Telegram, включи его заново в Настройки -> Telegram Business -> Chatbots.",
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

    # ответ админа на выдачу подписки («ID дней») — до разбора команд
    if text and not text.startswith("/") and chat_id in PENDING_PRICE and is_admin_user(user_id):
        if handle_price_input(chat_id, text):
            return
    if text and not text.startswith("/") and chat_id in PENDING_GRANT and is_admin_user(user_id):
        if handle_grant_input(user_id, chat_id, text):
            return
    if text.strip() == "/cancel" and (chat_id in PENDING_GRANT or chat_id in PENDING_PRICE):
        PENDING_GRANT.pop(chat_id, None)
        PENDING_PRICE.pop(chat_id, None)
        send_message(chat_id, "Отменено.")
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
                send_message(chat_id, page_text, parse_mode="HTML", reply_markup=page_markup)
            else:
                send_message(chat_id, "Помощь покажу в личных сообщениях со мной.")
        elif name == "/sub":
            handle_sub_command(message, args)
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
            handle_restore(message, restore_all=True)
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


def handle_business_message(message: dict) -> None:
    raw_log("business_message", message)
    connection_id = message.get("business_connection_id")
    if not connection_id:
        return

    context = f"business:{connection_id}"
    notify_chat_id = get_business_notify_chat_id(connection_id)
    owner_id = get_business_owner_id(connection_id)
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
    try:
        telegram_call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Главное меню"},
                    {"command": "menu", "description": "Открыть меню"},
                    {"command": "help", "description": "Как подключить бота"},
                    {"command": "sub", "description": "Выдать подписку (админ)"},
                    {"command": "watch", "description": "Включить обычный чат"},
                    {"command": "status", "description": "Статус обычного чата"},
                    {"command": "list", "description": "Список обычных чатов"},
                    {"command": "stop", "description": "Отключить обычные чаты"},
                    {"command": "connections", "description": "Business-подключения"},
                    {"command": "restore", "description": "Включить свои Business-подключения"},
                    {"command": "restore_all", "description": "Включить все Business-подключения"},
                ]
            },
        )
    except TelegramApiError as exc:
        log(f"setMyCommands failed: {exc}")


def run_polling() -> None:
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
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    handle_update(update)
                except Exception as exc:
                    log(f"Update handling failed: {type(exc).__name__}: {exc}")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
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
