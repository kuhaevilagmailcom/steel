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

## Меню, подписка и рефералка

Главное меню (`/start`, `/menu`) с кнопками из премиум-пака
[NewsEmoji](https://t.me/addemoji/NewsEmoji):

| Кнопка | Что делает |
|---|---|
| Купить подписку | тарифы **15 дней — 50 ⭐** и **30 дней — 100 ⭐**, оплата Telegram Stars прямо в чате (`sendInvoice`, валюта `XTR`) |
| Пригласить друзей | личная ссылка `t.me/hollyboot_bot?start=ref_<ID>`, прогресс; **3 друга → 3 дня бесплатно** (одноразово) |
| Помощь | пошаговая инструкция подключения Telegram Business и /watch для групп |
| Мои подключения | список бизнес-подключений + кнопка «включить выключенные» |
| Панель админа | статистика и выдача подписки (кнопкой или `/sub <ID> <дней>`) |

Как устроено:
- Платеж: `pre_checkout_query` → проверка тарифа → `successful_payment` → дни начисляются
  (идемпотентно по `telegram_charge_id`), админы получают уведомление об оплате.
- Без активной подписки бот продолжает хранить всё в базе, но уведомления не шлёт;
  напоминание купить показывается не чаще раза в 6 часов.
  Админы (`ADMIN_USER_IDS`) — без ограничений.
- Иконки кнопок — `icon_custom_emoji_id` из пака NewsEmoji (id взяты из limuzinov_shop_bot).

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
| `REF_REQUIRED` | сколько друзей нужно для триала (по умолчанию 3) |
| `REF_DAYS` | дней пробной подписки за рефералку (по умолчанию 3) |

## Важно

- Держи **одну** копию бота запущенной (лок-файл спасает только в пределах одной папки).
- `logger_data/` — база и архив: не удаляй при обновлении кода.
- ⚠️ Репозиторий публичный и токен лежит в `.env.deleted_logger`. Если бота угнали —
  перевыпусти токен через @BotFather (`/revoke`) и обнови файл/сервис.
