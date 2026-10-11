"""Huntergram .ai command (personal bot chat + Telegram Business chats).

Only text provided explicitly with .ai and, optionally, the replied-to message
is sent to OpenRouter. Existing stored conversations are not read or forwarded.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SYSTEM_PROMPT = (
    "Ты — Huntergram AI, полезный помощник в Telegram. "
    "Отвечай на языке вопроса, понятно и по делу. "
    "Не придумывай факты и не утверждай, что видел другие сообщения в чате. "
    "Если данных недостаточно, уточни вопрос."
)
AI_COMMAND_RE = re.compile(r"^\s*\.ai(?=\s|$)(?:\s+([\s\S]*?))?\s*$", re.IGNORECASE)
MAX_PROMPT_LENGTH = 2500
MAX_REPLY_LENGTH = 2000
_LOCK = threading.Lock()
_ACTIVE_USERS: set[int] = set()
_LAST_REQUEST: dict[int, float] = {}
_DAILY_USERS: dict[int, int] = {}
_DAILY_TOTAL = 0
_DAILY_DATE = ""
_ACTIVE_TOTAL = 0


def _env_int(key: str, fallback: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(key, str(fallback)))
    except ValueError:
        return fallback
    return max(low, min(value, high))


def is_ai_command(text: str) -> bool:
    """Only the dedicated '.ai' command, not '.aide' or incidental text."""
    return bool(AI_COMMAND_RE.fullmatch(text or ""))


def build_ai_prompt(message: dict) -> str:
    match = AI_COMMAND_RE.fullmatch(str(message.get("text") or ""))
    if match is None:
        return ""
    question = (match.group(1) or "").strip()[:MAX_PROMPT_LENGTH]
    replied = message.get("reply_to_message") or {}
    quoted = str(replied.get("text") or replied.get("caption") or "").strip()[:MAX_REPLY_LENGTH]
    if quoted:
        if question:
            return f"Сообщение, на которое ответил пользователь:\n{quoted}\n\nВопрос:\n{question}"
        return f"Ответь на это сообщение по существу:\n{quoted}"
    return question


class OpenRouterAIError(Exception):
    def __init__(self, user_message: str):
        self.user_message = user_message
        super().__init__(user_message)


def ask_openrouter(prompt: str, api_key: str | None = None) -> str:
    token = (api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY", "")).strip()
    if not token:
        raise OpenRouterAIError("⚠️ ИИ пока не подключён. Администратору нужно настроить OpenRouter API-ключ.")
    model = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip() or "openrouter/free"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 650,
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    req = Request(
        OPENROUTER_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://t.me/Hunterchatbbot",
            "X-Title": "Huntergram",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as response:
            result = json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise OpenRouterAIError("⚠️ Неверный или недействительный ключ OpenRouter. Сообщи администратору.") from None
        if exc.code in (402, 429):
            raise OpenRouterAIError("⏳ Бесплатный лимит ИИ исчерпан. Попробуй позже.") from None
        raise OpenRouterAIError("⚠️ Сервис ИИ временно недоступен. Попробуй позже.") from None
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        raise OpenRouterAIError("⚠️ ИИ не ответил. Попробуй чуть позже.") from None

    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise OpenRouterAIError("⚠️ ИИ вернул пустой ответ. Попробуй ещё раз.") from None
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    if not isinstance(content, str) or not content.strip():
        raise OpenRouterAIError("⚠️ ИИ вернул пустой ответ. Попробуй ещё раз.")
    return content.strip()[:3900]


def handle_ai_message(
    message: dict,
    user_id: int,
    send_reply: Callable[[str, bool], None],
) -> bool:
    """Dispatch .ai via bounded threads. send_reply(text, success) must be thread-safe."""
    global _DAILY_TOTAL, _DAILY_DATE, _ACTIVE_TOTAL
    if not is_ai_command(str(message.get("text") or "")):
        return False
    prompt = build_ai_prompt(message)
    if not prompt:
        send_reply("🤖 Использование: .ai твой вопрос\nМожно ответить на сообщение командой .ai", False)
        return True
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        send_reply("⚠️ ИИ пока не подключён. Администратору нужно добавить OpenRouter API-ключ.", False)
        return True

    now = time.monotonic()
    date = datetime.now(timezone.utc).date().isoformat()
    error = ""
    with _LOCK:
        if _DAILY_DATE != date:
            _DAILY_DATE = date
            _DAILY_USERS.clear()
            _DAILY_TOTAL = 0
        if user_id in _ACTIVE_USERS:
            error = "⏳ Подожди предыдущий ответ ИИ."
        elif _ACTIVE_TOTAL >= _env_int("AI_MAX_CONCURRENT", 3, 1, 8):
            error = "⏳ ИИ сейчас занят. Попробуй через несколько секунд."
        elif now - _LAST_REQUEST.get(user_id, -1e10) < _env_int("AI_COOLDOWN_SEC", 20, 0, 3600):
            error = "⏳ Подожди немного перед следующим вопросом."
        elif _DAILY_USERS.get(user_id, 0) >= _env_int("AI_DAILY_PER_USER", 5, 1, 100):
            error = "⏳ Твой дневной лимит ИИ исчерпан. Приходи завтра."
        elif _DAILY_TOTAL >= _env_int("AI_DAILY_TOTAL", 45, 1, 1000):
            error = "⏳ Общий дневной лимит ИИ исчерпан. Попробуй завтра."
        else:
            _ACTIVE_USERS.add(user_id)
            _ACTIVE_TOTAL += 1
            _LAST_REQUEST[user_id] = now
            _DAILY_USERS[user_id] = _DAILY_USERS.get(user_id, 0) + 1
            _DAILY_TOTAL += 1
    if error:
        send_reply(error, False)
        return True

    def worker() -> None:
        global _ACTIVE_TOTAL
        try:
            try:
                answer = ask_openrouter(prompt)
                succeeded = True
            except OpenRouterAIError as exc:
                answer = exc.user_message
                succeeded = False
            except Exception:
                answer = "⚠️ Ошибка ИИ. Попробуй позже."
                succeeded = False
            try:
                send_reply(answer, succeeded)
            except Exception:
                pass
        finally:
            with _LOCK:
                _ACTIVE_USERS.discard(user_id)
                _ACTIVE_TOTAL -= 1

    try:
        threading.Thread(target=worker, name="HuntergramAI", daemon=True).start()
    except RuntimeError:
        with _LOCK:
            _ACTIVE_USERS.discard(user_id)
            _ACTIVE_TOTAL -= 1
        send_reply("⚠️ Не удалось запустить ИИ. Попробуй позже.", False)
    return True
