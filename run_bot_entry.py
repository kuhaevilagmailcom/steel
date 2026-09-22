from __future__ import annotations

import deleted_message_logger_bot as bot


# Official Telegram Bot API getFile cannot download files larger than 20 MB.
# Keep file_id in the database so the bot can still try to resend media by file_id.
TELEGRAM_GETFILE_MAX_BYTES = 20 * 1024 * 1024

_original_archive_media_file = bot.archive_media_file
_original_telegram_call = bot.telegram_call
_original_handle_regular_message = bot.handle_regular_message


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



def _admin_all_connections(chat_id: int) -> None:
    rows = bot.list_business_connections(None)
    if not rows:
        bot.send_message(chat_id, "Business-подключений пока нет.")
        return

    lines = ["<b>Все Business-подключения</b>", ""]
    for row in rows[:50]:
        status = "🟢" if row.get("is_enabled") else "⚪️"
        owner_id = row.get("owner_id")
        updated = int(row.get("updated_at") or 0)
        when = bot.time.strftime("%d.%m %H:%M", bot.time.localtime(updated)) if updated else "—"
        lines.append(
            f"{status} owner <code>{owner_id}</code> · "
            f"<code>{bot.html.escape(bot.short_connection_id(row.get('connection_id')))}</code> · {when}"
        )
    if len(rows) > 50:
        lines.append(f"\nПоказаны первые 50 из {len(rows)}.")
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def _admin_all_chats(chat_id: int) -> None:
    rows = bot.list_business_connections(None)
    contexts = [f"business:{row['connection_id']}" for row in rows if row.get("connection_id")]

    with bot.sqlite3.connect(bot.DB_PATH) as conn:
        conn.row_factory = bot.sqlite3.Row
        result = []

        if contexts:
            placeholders = ",".join("?" for _ in contexts)
            result.extend(
                conn.execute(
                    f"""
                    SELECT context, chat_id, COUNT(*) AS events, MAX(updated_at) AS last_ts
                    FROM messages
                    WHERE context IN ({placeholders})
                    GROUP BY context, chat_id
                    ORDER BY last_ts DESC
                    LIMIT 100
                    """,
                    tuple(contexts),
                ).fetchall()
            )

        result.extend(
            conn.execute(
                """
                SELECT 'regular' AS context, m.chat_id, COUNT(*) AS events, MAX(m.updated_at) AS last_ts
                FROM messages m
                JOIN chat_owners c ON c.chat_id = m.chat_id
                WHERE m.context = 'regular'
                GROUP BY m.chat_id
                ORDER BY last_ts DESC
                LIMIT 100
                """
            ).fetchall()
        )

    if not result:
        bot.send_message(chat_id, "Подключённых чатов с активностью пока нет.")
        return

    ordered = sorted(result, key=lambda x: int(x["last_ts"] or 0), reverse=True)[:50]
    lines = ["<b>Все подключённые чаты</b>", ""]
    for row in ordered:
        when = bot.time.strftime("%d.%m %H:%M", bot.time.localtime(int(row["last_ts"] or 0))) if row["last_ts"] else "—"
        lines.append(
            f"• <code>{row['chat_id']}</code> · событий <b>{row['events']}</b> · {when}"
        )
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")


def handle_regular_message_guard(message: dict) -> None:
    text = str(message.get("text") or "").strip()
    user_id = int((message.get("from") or {}).get("id") or 0)
    chat = message.get("chat") or {}
    chat_id = int(chat.get("id") or 0)

    if chat.get("type") == "private" and bot.is_admin_user(user_id):
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else ""
        if command == "/all_connections":
            _admin_all_connections(chat_id)
            return
        if command == "/all_chats":
            _admin_all_chats(chat_id)
            return

    _original_handle_regular_message(message)


bot.archive_media_file = archive_media_file_guard
bot.telegram_call = telegram_call_guard
bot.handle_regular_message = handle_regular_message_guard


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
