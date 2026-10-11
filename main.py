"""BotHost-compatible Python startup entrypoint.

This file starts the actual Telegram bot; it does not run unit tests.
The original executable remains deleted_message_logger_bot.py.
"""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().with_name("deleted_message_logger_bot.py")),
        run_name="__main__",
    )
