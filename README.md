# Holly Bot — логгер удалённых сообщений (Telegram Business)

Бот на чистом Python (stdlib + `python-dotenv`), который следит за Telegram Business
подключениями: логирует удалённые сообщения, пересылает медиа в архив, ведёт статистику.
Работает через long-polling (`getUpdates`), webhook не нужен.

## Возможности

- Перехват `deleted_business_messages` и сохранение удалённых сообщений в SQLite (`logger_data/bot_test.sqlite3`).
- Архив медиа-файлов в `logger_data/media` с лимитом `MAX_MEDIA_ARCHIVE_MB`.
- Пересылка медиа бизнес-таймеров (`FORWARD_TIMER_MEDIA`).
- Админ-команды в ЛС для пользователей из `ADMIN_USER_IDS`.
- Защита от двойного запуска через lock-файл (`logger_data/bot.lock`).

## Быстрый старт (Ubuntu 24)

```bash
git clone https://github.com/kuhaevilagmailcom/steel.git && cd steel
chmod +x install_ubuntu24.sh run_bot.sh
./install_ubuntu24.sh
```

Скрипт создаёт venv, ставит зависимости и systemd-сервис `mishka-business-bot`
(автозапуск + перезапуск при падении).

### Вручную

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.deleted_logger.example .env.deleted_logger   # заполнить токен/юзернейм
.venv/bin/python deleted_message_logger_bot.py
```

Токен, id админов и прочие настройки читаются из `.env.deleted_logger`
(в этом репозитории он уже заполнен под @hollyboot_bot).

### Windows

```bat
pip install -r requirements.txt
python deleted_message_logger_bot.py
```

## Docker

```bash
docker build -t hollybot .
docker run -d --name hollybot -v %cd%/logger_data:/app/logger_data hollybot   # Windows
docker run -d --name hollybot -v $PWD/logger_data:/app/logger_data hollybot   # Linux
```

## Переменные окружения

| Переменная | Что делает |
|---|---|
| `LOGGER_BOT_TOKEN` | Токен бота от @BotFather |
| `LOGGER_BOT_USERNAME` | Username бота без `@` |
| `ADMIN_USER_IDS` | ID админов через запятую |
| `MAX_MEDIA_ARCHIVE_MB` | Лимит архива медиа (по умолчанию 50) |
| `FORWARD_TIMER_MEDIA` | 1/0 — пересылать медиа таймеров |
| `FORWARD_ALL_BUSINESS_MEDIA` | 1/0 — пересылать всё бизнес-медиа |

## Важно

- Держи **одну** копию бота запущенной (лок-файл спасает только в пределах одной папки).
- `logger_data/` — база и архив: не удаляй при обновлении кода.
- ⚠️ Репозиторий публичный и токен лежит в `.env.deleted_logger`. Если бота угнали —
  перевыпусти токен через @BotFather (`/revoke`) и обнови файл/сервис.
