from __future__ import annotations

import html
import json
import os
import random
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SAFE_HELP = """Huntergram — команды в диалоге

ИИ и диалог:
.gpt запрос — ответ ИИ
.short — краткий пересказ последних сообщений
.info — доступная информация о собеседнике
.afk [текст] — включить AFK; .afk off — выключить
.autoresponder текст — автоответ; .autoresponder off — выключить

Текст:
.bold текст · .italic текст · .underline текст
.monospace текст · .sw текст · .leet текст
.kawaii текст · .tsundere текст · .yandere текст

Остальное:
.love · .flip · .help

Команды приватности, спама, обхода ограничений и травли отключены."""

RU = "йцукенгшщзхъфывапролджэячсмитьбюЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ"
EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.QWERTYUIOP[]ASDFGHJKL:'ZXCVBNM,."
SWAP = str.maketrans(RU + EN, EN + RU)


def parse_dot_command(text: str) -> tuple[str, str] | None:
    match = re.match(r"^\.([a-zA-Z]+)(?:\s+([\s\S]*))?$", text.strip())
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


def format_command(name: str, arg: str) -> tuple[str, str] | None:
    escaped = html.escape(arg, quote=False)
    if name == "bold" and arg:
        return f"<b>{escaped}</b>", "HTML"
    if name == "italic" and arg:
        return f"<i>{escaped}</i>", "HTML"
    if name == "underline" and arg:
        return f"<u>{escaped}</u>", "HTML"
    if name == "monospace" and arg:
        return f"<code>{escaped}</code>", "HTML"
    if name == "sw" and arg:
        return arg.translate(SWAP), ""
    if name == "leet" and arg:
        table = str.maketrans({"а":"@","А":"@","е":"3","Е":"3","о":"0","О":"0","с":"$","С":"$","т":"7","Т":"7","и":"u","И":"U"})
        return arg.translate(table), ""
    if name == "kawaii" and arg:
        return f"{arg.rstrip(' .!?')}~ (｡•́‿•̀｡) ♡", ""
    if name == "tsundere" and arg:
        return f"Н-не подумай ничего такого... {arg.rstrip()}!", ""
    if name == "yandere" and arg:
        return f"{arg.rstrip()} ♡ Я ведь всегда рядом.", ""
    if name == "love":
        return "❤️", ""
    if name == "flip":
        return random.choice(("Орёл 🦅", "Решка 🪙")), ""
    if name == "help":
        return SAFE_HELP, ""
    return None


def ask_ai(prompt: str, system: str = "Отвечай кратко, точно и на русском языке.", max_tokens: int = 700) -> str:
    key = os.getenv("HCNSEC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ИИ не настроен: добавьте новый HCNSEC_API_KEY в переменные окружения")
    base = os.getenv("HCNSEC_API_BASE", "https://api.hcnsec.cn/v1").strip().rstrip("/")
    model = os.getenv("HCNSEC_MODEL", "gpt-4o-mini").strip()
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": max_tokens,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(f"{base}/chat/completions", data=payload, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json",
    }, method="POST")
    try:
        with urlopen(request, timeout=90) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"ИИ API вернул ошибку {exc.code}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("ИИ API временно недоступен") from exc
    try:
        text = str(body["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("ИИ API вернул непонятный ответ") from exc
    return text[:4000] or "ИИ не вернул текст"
