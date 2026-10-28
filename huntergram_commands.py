from __future__ import annotations

import html
import json
import os
import random
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, quote_plus, urlparse
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

Открытый поиск:
.osint — список безопасных команд
.search запрос · .social username · .domain example.com
.news запрос · .image ссылка · .email адрес
.local текст — поиск в сохранённом текущем чате

Команды приватности, спама, обхода ограничений и травли отключены."""

RU = "йцукенгшщзхъфывапролджэячсмитьбюЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ"
EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.QWERTYUIOP[]ASDFGHJKL:'ZXCVBNM,."
SWAP = str.maketrans(RU + EN, EN + RU)
HCNSEC_SYSTEM_PROMPT = (
    "Отвечай на русском языке живо, прямо и по делу. "
    "Можешь естественно использовать мат, сленг и грубые выражения, если они подходят контексту. "
    "Не запикивай и не заменяй мат звёздочками. Не превращай каждый ответ в поток ругани и не выдумывай факты."
)


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


def safe_osint_command(name: str, arg: str) -> str | None:
    if name == "osint":
        return (
            "Открытый поиск\n\n"
            ".search имя или запрос\n.social username\n.domain example.com\n"
            ".news запрос\n.image ссылка на изображение\n.email адрес\n"
            ".local текст — только сохранённый текущий чат"
        )
    if name in {"search", "news"}:
        if not arg:
            return f"Использование: .{name} запрос"
        query = quote_plus(arg[:200])
        if name == "news":
            return f"Новости по запросу «{arg[:120]}»:\nGoogle News: https://news.google.com/search?q={query}\nЯндекс: https://yandex.ru/news/search?text={query}"
        return f"Публичный поиск «{arg[:120]}»:\nGoogle: https://www.google.com/search?q={query}\nЯндекс: https://yandex.ru/search/?text={query}\nBing: https://www.bing.com/search?q={query}"
    if name == "social":
        username = arg.strip().lstrip("@").replace(" ", "")[:64]
        if not username or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
            return "Использование: .social username"
        encoded = quote(username, safe="")
        return (
            f"Публичные профили @{username}:\n"
            f"Telegram: https://t.me/{encoded}\nGitHub: https://github.com/{encoded}\n"
            f"VK: https://vk.com/{encoded}\nTikTok: https://www.tiktok.com/@{encoded}\n"
            f"X: https://x.com/{encoded}"
        )
    if name == "domain":
        domain = arg.strip().lower()
        if "://" in domain:
            domain = urlparse(domain).hostname or ""
        domain = domain.strip(". ")[:253]
        if not domain or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}", domain):
            return "Использование: .domain example.com"
        encoded = quote(domain, safe="")
        return (
            f"Публичная проверка домена {domain}:\n"
            f"ICANN Lookup: https://lookup.icann.org/en/lookup?name={encoded}\n"
            f"VirusTotal: https://www.virustotal.com/gui/domain/{encoded}\n"
            f"Web Archive: https://web.archive.org/web/*/{encoded}"
        )
    if name == "image":
        url = arg.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return "Использование: .image https://адрес-картинки"
        encoded = quote(url[:1500], safe="")
        return (
            "Обратный поиск изображения:\n"
            f"Google Lens: https://lens.google.com/uploadbyurl?url={encoded}\n"
            f"Яндекс.Картинки: https://yandex.ru/images/search?rpt=imageview&url={encoded}"
        )
    if name in {"email", "breach"}:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", arg.strip()):
            return f"Использование: .{name} name@example.com"
        return (
            "Проверяйте только свой адрес. Email не отправлен сторонним сервисам.\n"
            "Mozilla Monitor: https://monitor.mozilla.org/\n"
            "Have I Been Pwned: https://haveibeenpwned.com/"
        )
    return None


def ask_ai(prompt: str, system: str = HCNSEC_SYSTEM_PROMPT, max_tokens: int = 450) -> str:
    key = os.getenv("HCNSEC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ИИ не настроен: добавьте новый HCNSEC_API_KEY в переменные окружения")
    base = os.getenv("HCNSEC_API_BASE", "https://api.hcnsec.cn/v1").strip().rstrip("/")
    model = os.getenv("HCNSEC_MODEL", "gpt-4o-mini").strip()
    if system == HCNSEC_SYSTEM_PROMPT:
        system = os.getenv("HCNSEC_SYSTEM_PROMPT", system).strip() or system
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
        with urlopen(request, timeout=45) as response:
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
