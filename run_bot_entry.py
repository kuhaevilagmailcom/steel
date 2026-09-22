from __future__ import annotations

import deleted_message_logger_bot as bot


# Official Telegram Bot API getFile cannot download files larger than 20 MB.
# Keep file_id in the database so the bot can still try to resend media by file_id.
TELEGRAM_GETFILE_MAX_BYTES = 20 * 1024 * 1024

_original_archive_media_file = bot.archive_media_file
_original_telegram_call = bot.telegram_call


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


bot.archive_media_file = archive_media_file_guard
bot.telegram_call = telegram_call_guard


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
