from __future__ import annotations

import gzip
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, urlparse
from urllib.request import Request, urlopen


STATIC_DIR = Path(__file__).resolve().parent / "webapp"
AUTH_HEADER = "X-Telegram-Init-Data"
MAX_PAGE_SIZE = 100
_avatar_cache: dict[int, tuple[float, str | None]] = {}
_server: ThreadingHTTPServer | None = None


def validate_init_data(raw: str, bot_token: str, max_age: int = 86400) -> dict:
    """Validate Telegram Mini App initData and return the signed user object."""
    if not raw:
        raise PermissionError("Откройте приложение из бота")
    values = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    if not received_hash:
        raise PermissionError("Нет подписи Telegram")
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected):
        raise PermissionError("Неверная подпись Telegram")
    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError as exc:
        raise PermissionError("Неверная дата авторизации") from exc
    now = int(time.time())
    if auth_date <= 0 or auth_date > now + 60 or now - auth_date > max_age:
        raise PermissionError("Сессия устарела, откройте приложение заново")
    try:
        user = json.loads(values.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise PermissionError("Не удалось прочитать пользователя") from exc
    if not isinstance(user, dict) or not user.get("id"):
        raise PermissionError("Telegram не передал пользователя")
    return user


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plain_author(value: object, fallback: str = "Без имени") -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s*\(ID:\s*\d+\)\s*$", "", text).strip()
    return text or fallback


def _media_meta(raw: object) -> dict:
    try:
        value = json.loads(str(raw or "{}"))
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _display_user(row: sqlite3.Row) -> dict:
    name = " ".join(part for part in (row["first_name"], row["last_name"]) if part).strip()
    username = str(row["username"] or "").strip()
    return {
        "id": int(row["user_id"]),
        "name": name or (f"@{username}" if username else "Пользователь"),
        "username": username,
        "updated_at": int(row["updated_at"] or row["created_at"] or 0),
        "chat_count": int(row["chat_count"] or 0),
        "message_count": int(row["message_count"] or 0),
    }


def list_users(bot, query: str = "", limit: int = 50, offset: int = 0) -> dict:
    limit = min(MAX_PAGE_SIZE, max(1, limit))
    needle = f"%{query.strip().lower()}%"
    where = ""
    params: list[object] = []
    if query.strip():
        where = "WHERE lower(COALESCE(u.first_name,'') || ' ' || COALESCE(u.last_name,'') || ' ' || COALESCE(u.username,'') || ' ' || CAST(u.user_id AS TEXT)) LIKE ?"
        params.append(needle)
    with sqlite3.connect(bot.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            WITH owned AS (
                SELECT co.owner_id,m.chat_id FROM messages m
                JOIN chat_owners co ON m.context='regular' AND co.chat_id=m.chat_id
                UNION ALL
                SELECT bc.owner_id,m.chat_id FROM messages m
                JOIN business_connections bc ON m.context='business:' || bc.connection_id
            )
            SELECT u.user_id,u.first_name,u.last_name,u.username,u.created_at,u.updated_at,
                   COUNT(DISTINCT owned.chat_id) AS chat_count,COUNT(owned.chat_id) AS message_count
            FROM users u LEFT JOIN owned ON owned.owner_id=u.user_id
            {where}
            GROUP BY u.user_id ORDER BY u.updated_at DESC,u.user_id DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit + 1, max(0, offset)),
        ).fetchall()
    visible = [_display_user(row) for row in rows if not bot.user_chats_are_hidden(int(row["user_id"]))]
    return {"items": visible[:limit], "has_more": len(visible) > limit, "offset": offset}


def list_chats(bot, admin_id: int, target_id: int, query: str = "", limit: int = 60) -> list[dict]:
    if bot.user_chats_are_hidden(target_id):
        raise PermissionError("Чаты этого пользователя скрыты")
    needle = f"%{query.strip().lower()}%"
    search_sql = ""
    search_params: tuple[object, ...] = ()
    if query.strip():
        search_sql = " AND lower(COALESCE(m.author,'') || ' ' || COALESCE(m.content,'') || ' ' || CAST(m.chat_id AS TEXT)) LIKE ?"
        search_params = (needle,)
    with sqlite3.connect(bot.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT m.chat_id,COUNT(*) AS message_count,MAX(m.updated_at) AS updated_at,
                   COALESCE(MAX(CASE WHEN m.user_id != ? THEN m.author END),MAX(m.author)) AS peer,
                   COALESCE(MAX(CASE WHEN m.user_id != ? THEN m.user_id END),m.chat_id) AS peer_id,
                   COALESCE(MAX(CASE WHEN m.updated_at=(SELECT MAX(m2.updated_at) FROM messages m2 WHERE m2.chat_id=m.chat_id) THEN m.content END),'') AS preview,
                   SUM(CASE WHEN m.updated_at > COALESCE(vs.last_seen_at,0) THEN 1 ELSE 0 END) AS unread,
                   COALESCE(MAX(vs.pinned),0) AS pinned,
                   SUM(CASE WHEN m.media_type IS NOT NULL THEN 1 ELSE 0 END) AS media_count
            FROM messages AS m
            LEFT JOIN chat_owners AS co ON m.context='regular' AND co.chat_id=m.chat_id
            LEFT JOIN business_connections AS bc ON m.context='business:' || bc.connection_id
            LEFT JOIN chat_view_state AS vs ON vs.admin_id=? AND vs.target_id=? AND vs.chat_id=m.chat_id
            WHERE (co.owner_id=? OR bc.owner_id=?)
            """ + search_sql + """
            GROUP BY m.chat_id
            ORDER BY pinned DESC,updated_at DESC
            LIMIT ?
            """,
            (target_id, target_id, admin_id, target_id, target_id, target_id, *search_params, min(MAX_PAGE_SIZE, max(1, limit))),
        ).fetchall()
    return [
        {
            "id": int(row["chat_id"]),
            "avatar_id": int(row["peer_id"] or row["chat_id"]),
            "name": _plain_author(row["peer"], f"Чат {row['chat_id']}"),
            "preview": str(row["preview"] or ""),
            "message_count": int(row["message_count"] or 0),
            "media_count": int(row["media_count"] or 0),
            "unread": int(row["unread"] or 0),
            "pinned": bool(row["pinned"]),
            "updated_at": int(row["updated_at"] or 0),
        }
        for row in rows
    ]


def list_messages(bot, target_id: int, chat_id: int, before: int = 0, limit: int = 50) -> dict:
    if bot.user_chats_are_hidden(target_id):
        raise PermissionError("Чат недоступен")
    limit = min(MAX_PAGE_SIZE, max(1, limit))
    before_sql = " AND m.updated_at < ?" if before else ""
    before_params: tuple[object, ...] = (before,) if before else ()
    with sqlite3.connect(bot.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT m.context,m.chat_id,m.message_id,m.user_id,m.author,m.content,m.media_type,
                   m.media_json,m.local_media_path,m.has_media_spoiler,m.ttl_seconds,
                   m.reply_to_message_id,m.reply_to_author,m.reply_to_content,
                   m.created_at,m.updated_at,m.deleted_at,m.edit_count,m.original_content
            """ + bot.OWNER_MESSAGES_FROM + " AND m.chat_id=?" + before_sql + """
            ORDER BY m.updated_at DESC,m.message_id DESC LIMIT ?
            """,
            (target_id, target_id, chat_id, *before_params, limit + 1),
        ).fetchall()
    page = rows[:limit]
    items = []
    for row in reversed(page):
        meta = _media_meta(row["media_json"])
        item = {
            "id": int(row["message_id"]),
            "author_id": _int(row["user_id"]),
            "author": _plain_author(row["author"]),
            "outgoing": _int(row["user_id"]) == target_id,
            "text": str(row["content"] or ""),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "deleted": bool(row["deleted_at"]),
            "edited": bool(row["edit_count"]),
            "edit_count": int(row["edit_count"] or 0),
            "original_text": str(row["original_content"] or ""),
            "reply": None,
            "media": None,
        }
        if row["reply_to_message_id"]:
            item["reply"] = {
                "id": int(row["reply_to_message_id"]),
                "author": _plain_author(row["reply_to_author"], "Сообщение"),
                "text": str(row["reply_to_content"] or ""),
            }
        if row["media_type"]:
            item["media"] = {
                "type": str(row["media_type"]),
                "url": f"/api/media/{target_id}/{chat_id}/{row['message_id']}",
                "mime_type": str(meta.get("mime_type") or ""),
                "file_name": str(meta.get("file_name") or ""),
                "duration": _int(meta.get("duration")),
                "width": _int(meta.get("width")),
                "height": _int(meta.get("height")),
                "spoiler": bool(row["has_media_spoiler"]),
                "ttl_seconds": _int(row["ttl_seconds"]),
                "animated_sticker": str(row["media_type"]) == "sticker" and (
                    str(meta.get("mime_type") or "") == "application/x-tgsticker"
                    or str(row["local_media_path"] or "").lower().endswith(".tgs")
                ),
            }
        items.append(item)
    return {
        "items": items,
        "has_more": len(rows) > limit,
        "next_before": int(page[-1]["updated_at"]) if page and len(rows) > limit else None,
    }


def _message_media(bot, target_id: int, chat_id: int, message_id: int) -> tuple[Path | None, dict, str | None]:
    saved = bot.get_user_owned_saved_message(target_id, chat_id, message_id)
    if not saved or not saved.get("media_type"):
        raise FileNotFoundError("Медиа не найдено")
    local = Path(str(saved.get("local_media_path") or "")) if saved.get("local_media_path") else None
    if local and (not local.exists() or not local.is_file()):
        local = None
    remote_url = None
    if local is None and saved.get("media_file_id"):
        info = bot.telegram_call("getFile", {"file_id": saved["media_file_id"]}, timeout=30)
        if info and info.get("file_path"):
            remote_url = bot.FILE_API_URL + quote(str(info["file_path"]), safe="/")
    return local, saved, remote_url


def _avatar_file_id(bot, user_id: int) -> str | None:
    cached = _avatar_cache.get(user_id)
    if cached and cached[0] > time.time():
        return cached[1]
    file_id = None
    try:
        result = bot.telegram_call("getUserProfilePhotos", {"user_id": user_id, "limit": 1}, timeout=15)
        photos = (result or {}).get("photos") or []
        if photos and photos[0]:
            file_id = str(photos[0][-1].get("file_id") or "") or None
    except Exception:
        file_id = None
    _avatar_cache[user_id] = (time.time() + 3600, file_id)
    return file_id


def make_handler(bot):
    class WebAppHandler(BaseHTTPRequestHandler):
        server_version = "HolyGramWeb/1.0"

        def log_message(self, fmt: str, *args) -> None:
            bot.log("WebApp " + (fmt % args))

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' https://telegram.org https://cdnjs.cloudflare.com; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'",
            )

        def _send_json(self, status: int, body: object, cookie: str = "") -> None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _session_cookie(self, user_id: int, expires: int) -> str:
            payload = f"{user_id}:{expires}"
            signature = hmac.new(bot.BOT_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
            token = base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii").rstrip("=")
            is_local_dev = os.getenv("WEBAPP_DEV_MODE", "0") == "1" and self.client_address[0] in {"127.0.0.1", "::1"}
            secure = "" if is_local_dev else "; Secure"
            return f"hg_session={token}; Path=/; Max-Age=86400; HttpOnly; SameSite=Strict{secure}"

        def _cookie_user(self) -> dict | None:
            cookies = {}
            for part in self.headers.get("Cookie", "").split(";"):
                key, separator, value = part.strip().partition("=")
                if separator:
                    cookies[key] = value
            token = cookies.get("hg_session", "")
            if not token:
                return None
            try:
                decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")
                uid, expires, signature = decoded.split(":", 2)
                payload = f"{uid}:{expires}"
                expected = hmac.new(bot.BOT_TOKEN.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
                if int(expires) < int(time.time()) or not hmac.compare_digest(signature, expected):
                    return None
                return {"id": int(uid), "first_name": "Владелец"}
            except (ValueError, UnicodeDecodeError):
                return None

        def _auth(self) -> dict:
            query_dev_id = parse_qs(urlparse(self.path).query).get("devUser", [""])[0]
            dev_id = self.headers.get("X-Holygram-Dev-User") or query_dev_id
            peer = str(self.client_address[0])
            if os.getenv("WEBAPP_DEV_MODE", "0") == "1" and peer in {"127.0.0.1", "::1"} and dev_id:
                user = {"id": _int(dev_id), "first_name": "Dev"}
            else:
                raw = self.headers.get(AUTH_HEADER, "")
                if raw:
                    max_age = max(300, _int(os.getenv("WEBAPP_AUTH_MAX_AGE", "86400"), 86400))
                    user = validate_init_data(raw, bot.BOT_TOKEN, max_age)
                else:
                    user = self._cookie_user()
                    if not user:
                        raise PermissionError("Откройте приложение из бота")
            if not bot.is_owner_admin(_int(user.get("id"))):
                raise PermissionError("Доступ есть только у владельцев бота")
            return user

        def _serve_static(self, path: str) -> None:
            name = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
            candidate = (STATIC_DIR / name).resolve()
            if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not candidate.exists() or not candidate.is_file():
                candidate = STATIC_DIR / "index.html"
            data = candidate.read_bytes()
            mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") or mime.endswith("javascript") else mime)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream_source(self, source, total: int | None, mime: str, filename: str = "") -> None:
            start = 0
            end = total - 1 if total else None
            range_header = self.headers.get("Range", "")
            if total and range_header.startswith("bytes="):
                match = re.match(r"bytes=(\d*)-(\d*)", range_header)
                if match:
                    start = _int(match.group(1), 0)
                    end = _int(match.group(2), total - 1) if match.group(2) else total - 1
                    start = min(max(0, start), total - 1)
                    end = min(max(start, end), total - 1)
            partial = bool(total and (start > 0 or end != total - 1))
            self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", mime)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=300")
            if filename:
                self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quote(filename)}")
            if total:
                length = end - start + 1
                self.send_header("Content-Length", str(length))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            if hasattr(source, "seek"):
                source.seek(start)
            remaining = (end - start + 1) if total else None
            while remaining is None or remaining > 0:
                chunk = source.read(min(256 * 1024, remaining) if remaining is not None else 256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                if remaining is not None:
                    remaining -= len(chunk)

        def _serve_media(self, target_id: int, chat_id: int, message_id: int, lottie: bool = False) -> None:
            local, saved, remote_url = _message_media(bot, target_id, chat_id, message_id)
            meta = _media_meta(saved.get("media_json"))
            mime = str(meta.get("mime_type") or "")
            filename = str(meta.get("file_name") or "")
            if local:
                mime = mime or mimetypes.guess_type(local.name)[0] or "application/octet-stream"
                filename = filename or local.name
                if lottie:
                    raw = local.read_bytes()
                    if local.suffix.lower() == ".tgs" or raw[:2] == b"\x1f\x8b":
                        raw = gzip.decompress(raw)
                    json.loads(raw.decode("utf-8"))
                    self.send_response(HTTPStatus.OK)
                    self._security_headers()
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                with local.open("rb") as stream:
                    self._stream_source(stream, local.stat().st_size, mime, filename)
                return
            if not remote_url:
                raise FileNotFoundError("Файл не сохранился и недоступен в Telegram")
            headers = {}
            if self.headers.get("Range"):
                headers["Range"] = self.headers["Range"]
            with urlopen(Request(remote_url, headers=headers), timeout=120) as response:
                remote_mime = mime or response.headers.get_content_type() or "application/octet-stream"
                total = _int(response.headers.get("Content-Length")) or None
                self._stream_source(response, total, remote_mime, filename)

        def _serve_avatar(self, user_id: int) -> None:
            file_id = _avatar_file_id(bot, user_id)
            if not file_id:
                raise FileNotFoundError("Аватар не найден")
            info = bot.telegram_call("getFile", {"file_id": file_id}, timeout=15)
            path = str((info or {}).get("file_path") or "")
            if not path:
                raise FileNotFoundError("Аватар не найден")
            with urlopen(Request(bot.FILE_API_URL + quote(path, safe="/")), timeout=30) as response:
                self._stream_source(response, _int(response.headers.get("Content-Length")) or None, response.headers.get_content_type() or "image/jpeg")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True, "service": "holy-gram-webapp"})
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self._security_headers()
                self.end_headers()
                return
            if not path.startswith("/api/"):
                self._serve_static(path)
                return
            try:
                user = self._auth()
                admin_id = _int(user.get("id"))
                query = parse_qs(parsed.query)
                if path == "/api/bootstrap":
                    cookie = "" if (os.getenv("WEBAPP_DEV_MODE", "0") == "1" and self.client_address[0] in {"127.0.0.1", "::1"}) else self._session_cookie(admin_id, int(time.time()) + 86400)
                    self._send_json(HTTPStatus.OK, {"ok": True, "owner": {"id": admin_id, "name": str(user.get("first_name") or "Владелец"), "username": str(user.get("username") or "")}, "timezone": "Europe/Moscow"}, cookie)
                elif path == "/api/users":
                    self._send_json(HTTPStatus.OK, list_users(bot, str(query.get("q", [""])[0]), _int(query.get("limit", [50])[0], 50), _int(query.get("offset", [0])[0])))
                elif re.fullmatch(r"/api/users/\d+/chats", path):
                    target_id = _int(path.split("/")[3])
                    self._send_json(HTTPStatus.OK, {"items": list_chats(bot, admin_id, target_id, str(query.get("q", [""])[0]))})
                elif re.fullmatch(r"/api/users/\d+/chats/-?\d+/messages", path):
                    parts = path.split("/")
                    target_id, chat_id = _int(parts[3]), _int(parts[5])
                    result = list_messages(bot, target_id, chat_id, _int(query.get("before", [0])[0]), _int(query.get("limit", [50])[0], 50))
                    bot.set_chat_seen(admin_id, target_id, chat_id)
                    bot.log_admin_view(admin_id, target_id, chat_id, "Web App: просмотр сообщений")
                    self._send_json(HTTPStatus.OK, result)
                elif re.fullmatch(r"/api/media/\d+/-?\d+/\d+", path):
                    parts = path.split("/")
                    self._serve_media(_int(parts[3]), _int(parts[4]), _int(parts[5]), query.get("format", [""])[0] == "lottie")
                elif re.fullmatch(r"/api/avatar/-?\d+", path):
                    self._serve_avatar(_int(path.rsplit("/", 1)[1]))
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Маршрут не найден"})
            except PermissionError as exc:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": str(exc)})
            except FileNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                bot.log(f"WebApp GET failed: {type(exc).__name__}: {exc}")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Не удалось загрузить данные"})

    return WebAppHandler


def start_webapp_server(bot, host: str = "0.0.0.0", port: int | None = None) -> ThreadingHTTPServer:
    global _server
    if _server:
        return _server
    selected_port = port or _int(os.getenv("WEBAPP_PORT") or os.getenv("PORT") or "8080", 8080)
    _server = ThreadingHTTPServer((host, selected_port), make_handler(bot))
    _server.daemon_threads = True
    thread = threading.Thread(target=_server.serve_forever, name="holygram-webapp", daemon=True)
    thread.start()
    bot.log(f"Web App listening on http://{host}:{selected_port}")
    return _server


def stop_webapp_server() -> None:
    global _server
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None
