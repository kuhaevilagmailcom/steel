import os, sys, json, time, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import deleted_message_logger_bot as b

# всё в временную базу, сеть не трогаем
test_root = __import__("pathlib").Path(tempfile.mkdtemp())
b.DATA_DIR = test_root
b.BACKUP_DIR = test_root / "backups"
b.BACKUP_DIR.mkdir()
b.DB_PATH = test_root / "test.sqlite3"
b.send_message = lambda *a, **k: None  # заглушка сети

b.init_db()
assert b.format_display_time(0, "%H:%M") == "03:00", "время диалогов показывается по Москве"
assert b.is_owner_admin(1141626866), "второй владелец имеет полный доступ владельца"

# --- подписка ---
b.register_user(111, 111)
until = b.add_days(111, 15)
assert until > time.time() and b.sub_active(111), "после покупки активна"
assert b.get_sub(111)[0] == until
b.set_until(111, 0, None)
assert not b.sub_active(111), "после снятия не активна"
assert b.owner_can_log.__doc__  # exists
# админ всегда может
b.ADMIN_USER_IDS.add(999)
assert b.sub_active(999) and b.owner_can_log(999)
assert b.owner_can_log(None)

# делегированные администраторы
b.register_user(222, 222, {"first_name": "Админ"})
ok, msg = b.add_bot_admin(999, 222)
assert ok and b.is_admin_user(222), msg
assert 222 in b.list_admin_ids()
ok, msg = b.remove_bot_admin(999, 222)
assert ok and not b.is_admin_user(222), msg
b.register_user(111, 111, {"first_name": "Тест", "last_name": "Пользователь", "username": "tester"})

# продление суммируется
t0 = b.add_days(111, 15)
t1 = b.add_days(111, 15)
assert t1 >= t0 + 15 * 86400 - 2, "продление прибавляет дни"

# --- стили общения подписки ---
style_samples = {
    "cute": ("привет, спасибо, ты где?", ("приветик", "спасибочки", "ты гдеее")),
    "vasya": ("что ты сейчас делаешь вообще?", ("чё", "щас", "ваще")),
    "brother": ("привет, спасибо, всё нормально", ("салам", "от души", "всё ровно")),
    "dumb": ("короче, я не знаю что сейчас делать", ("кароч", "я хз", "чо", "щас")),
}
for style, (source, expected_parts) in style_samples.items():
    b.set_communication_style(111, style)
    styled = b.transform_message_style(111, source)
    assert styled != source, style
    assert all(part in styled.lower() for part in expected_parts), (style, styled)
    assert b.stylize_message_text(style, styled) == styled, "стиль не должен накладываться дважды"

for protected in ("/start", "https://example.com", "@username", "+7 999 123-45-67"):
    assert b.transform_message_style(111, protected) == protected

b.set_communication_style(111, "vasya")
mixed = b.transform_message_style(111, "что сейчас? https://example.com @username")
assert "чё" in mixed and "щас" in mixed and "https://example.com" in mixed and "@username" in mixed
b.set_communication_style(111, "cute")
assert b.transform_message_style(111, "а" * 1024, 1024) == "а" * 1024, "длинная подпись доставляется"
original = "ты где, скоро придешь?"
b.set_until(111, 0, None)
assert b.transform_message_style(111, original) == original
assert b.get_communication_style(111) == "cute", "после окончания подписки стиль хранится"
b.add_days(111, 15)
assert b.transform_message_style(111, original) != original, "после продления стиль включается снова"

style_calls = []
b.telegram_call = lambda method, payload=None, timeout=30: style_calls.append((method, payload)) or True
b.apply_business_message_style({
    "business_connection_id": "connection-1", "message_id": 7,
    "from": {"id": 111}, "chat": {"id": 222}, "text": original,
}, 111)
assert style_calls[-1][0] == "editMessageText" and style_calls[-1][1]["text"] != original
b.apply_business_message_style({
    "business_connection_id": "connection-1", "message_id": 8,
    "from": {"id": 111}, "chat": {"id": 222}, "photo": [{}], "caption": "Фото для тебя",
}, 111)
assert style_calls[-1][0] == "editMessageCaption" and style_calls[-1][1]["caption"] != "Фото для тебя"

# --- рефералка ---
b.set_until(111, 0, 0)
for inv in (222, 333):
    b.register_user(inv, inv)
    assert b.add_referral(111, inv)
    assert not b.add_referral(111, inv) is True, "дубликат не засчитан"
assert b.ref_count(111) == 2
b.check_trial(111)
assert b.get_sub(111)[0] == 0, "на 2-х триал не даётся"
b.register_user(444, 444)
b.add_referral(111, 444)
b.check_trial(111)
assert b.sub_active(111) and b.get_sub(111)[1] == 1, "триал выдан один раз"
b.check_trial(111)
left1 = b.get_sub(111)[0]
b.register_user(555, 555)
b.add_referral(111, 555)
b.check_trial(111)
assert b.get_sub(111)[0] == left1, "повторный триал не начисляется"

# self-ref guard (проверяется в handle_start, тут только БД-часть)
b.set_until(600, 0, 0); b.register_user(600, 600)
# idempotency payments
now = int(time.time())
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute("INSERT INTO payments (tg_payment_id,user_id,days,stars,payload,created_at) VALUES ('chg1',111,15,50,'sub:111:15:50',?)", (now,))
    cur = conn.execute("INSERT OR IGNORE INTO payments (tg_payment_id,user_id,days,stars,payload,created_at) VALUES ('chg1',111,15,50,'x',?)", (now,))
    assert cur.rowcount == 0, "дубликат платежа не проходит"

# --- страницы ---
cases = {
    "page_home": 111, "page_buy": 111, "page_ref": 111,
    "page_help": 111, "page_connections": 111, "page_panel": 999,
    "page_prices": 999, "page_users": 999, "page_stats": 999,
    "page_promos": 999, "page_admin_log": 999,
    "page_admins": 999, "page_support_tickets": 999,
    "page_expiring_subscriptions": 999,
}
for name, uid in cases.items():
    text, markup = getattr(b, name)(uid)
    assert "<tg-emoji" in text, name + " без премиум-эмодзи"
    assert "inline_keyboard" in markup and json.dumps(markup)
    cb = [btn.get("callback_data") for row in markup["inline_keyboard"] for btn in row]
    assert all(len((c or "").encode("utf-8")) <= 64 for c in cb if c), "callback_data <=64 bytes"
    assert len(text) <= 4096, name + " длиннее лимита Telegram"
    print(f"OK {name}: {len(text)} chars, {len(markup['inline_keyboard'])} rows")

style_text, style_markup = b.page_communication_style(111)
assert "🎭" in style_text and "Стиль общения" in style_text
assert "style:cute" in json.dumps(style_markup, ensure_ascii=False)
home_text, home_markup = b.page_home(111)
home_callbacks = json.dumps(home_markup, ensure_ascii=False)
assert "Owner ID" not in home_text and "Моя подписка" in home_callbacks
assert b.MENU_IMAGE_PATH.exists() and b.MENU_IMAGE_PATH.suffix == ".png"
assert "Стиль общения" in home_callbacks and "⭐ Моя подписка" in home_callbacks
help_text, _ = b.page_help(111)
assert "Business-подключения" not in help_text and "Как подключить Holly Bot" in help_text

users_text, _ = b.page_users(999)
assert "Тест Пользователь" in users_text and "@tester" in users_text
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    now = int(time.time())
    conn.execute("INSERT INTO chat_owners (chat_id,owner_id,created_at) VALUES (?,?,?)", (-100500, 111, now))
    conn.execute(
        "INSERT INTO business_connections (connection_id,owner_id,notify_chat_id,is_enabled,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        ("test-connection", 111, 111, 1, now, now),
    )
b.save_message("regular", {
    "message_id": 1, "from": {"id": 222, "first_name": "Обычный"},
    "chat": {"id": -100500, "type": "supergroup", "title": "Группа"}, "text": "Сообщение обычного чата",
})
b.save_message("business:test-connection", {
    "message_id": 2, "from": {"id": 333, "first_name": "Клиент", "username": "client333"},
    "chat": {"id": 333, "type": "private", "first_name": "Клиент"}, "text": "Сообщение Business-чата",
    "reply_to_message": {"message_id": 1, "from": {"id": 111, "first_name": "Владелец"}, "chat": {"id": 333, "type": "private"}, "text": "Исходное сообщение"},
})
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute(
        "UPDATE messages SET media_type='voice', media_file_id='voice-test' "
        "WHERE context='business:test-connection' AND chat_id=333 AND message_id=2"
    )
card_text, card_markup = b.page_user_card(999, 111)
assert "Карточка пользователя" in card_text and "useradd:111:30:0" in json.dumps(card_markup)
assert "uchats:111:0:0" in json.dumps(card_markup)
assert "uexall:t:111:0" in json.dumps(card_markup) and "uexall:m:111:0" in json.dumps(card_markup)
chats_text, chats_markup = b.page_user_chats(999, 111)
assert "Чаты пользователя" in chats_text
assert "uchat:111:-100500:0:0" in json.dumps(chats_markup)
assert "uchat:111:333:0:0" in json.dumps(chats_markup)
assert "@client333" in json.dumps(chats_markup, ensure_ascii=False)
assert "сообщ." in json.dumps(chats_markup, ensure_ascii=False)
assert "Обновить список" in json.dumps(chats_markup, ensure_ascii=False)
overview_text, overview_markup = b.page_chat_overview(999, 111, 333)
overview_buttons = json.dumps(overview_markup, ensure_ascii=False)
assert "Сообщений:" in overview_text and "umsg:111:333:0:0:0" in overview_buttons
assert "ugal:111:333:0:0:all:0" in overview_buttons and "uexport:111:333" in overview_buttons
filters_text, filters_markup = b.page_chat_filters(999, 111)
assert "Фильтры" in filters_text and "ucl:111:0:0:deleted:recent" in json.dumps(filters_markup)
messages_text, messages_markup = b.page_user_chat_messages(999, 111, 333)
assert "Сообщение Business-чата" in messages_text and "@client333" in messages_text and "Ответ на" in messages_text
assert "umedia:111:333:2" in json.dumps(messages_markup)
assert "Обновить чат" in json.dumps(messages_markup, ensure_ascii=False)
media_text, media_markup = b.page_chat_media(999, 111, 333)
assert "Медиа чата" in media_text and "umedia:111:333:2" in json.dumps(media_markup)
dates_text, dates_markup = b.page_chat_dates(999, 111, 333, 0, 0)
assert "Сообщения по дате" in dates_text and ":today" in json.dumps(dates_markup)
for dynamic_text, dynamic_markup in (
    (chats_text, chats_markup), (overview_text, overview_markup), (filters_text, filters_markup),
    (messages_text, messages_markup), (media_text, media_markup), (dates_text, dates_markup),
):
    assert len(dynamic_text) <= 4096
    assert all(
        len(str(button.get("callback_data") or "").encode("utf-8")) <= 64
        for row in dynamic_markup["inline_keyboard"] for button in row
    )
assert b.toggle_chat_pin(999, 111, 333)
assert "Открепить" in b.page_chat_overview(999, 111, 333)[0] or "Открепить" in json.dumps(b.page_chat_overview(999, 111, 333)[1], ensure_ascii=False)
b.set_user_label(999, 111, "важный")
assert "важный" in b.page_user_card(999, 111)[0]
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute("INSERT OR REPLACE INTO admin_searches (admin_id,target_id,query,updated_at) VALUES (?,?,?,?)", (999,111,"Business",int(time.time())))
search_text, search_markup = b.page_chat_search_results(999, 111)
assert "найдено: <b>1</b>" in search_text and "uchat:111:333" in json.dumps(search_markup)
export_path = b.export_chat_html(999, 111, 333)
assert export_path.exists() and __import__("zipfile").is_zipfile(export_path)
sample_media = test_root / "sample.ogg"
sample_media.write_bytes(b"test-media")
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute(
        "UPDATE messages SET local_media_path=? WHERE context='business:test-connection' AND chat_id=333 AND message_id=2",
        (str(sample_media),),
    )
text_export = b.export_all_user_chats(999, 111, include_media=False)
media_export = b.export_all_user_chats(999, 111, include_media=True)
with __import__("zipfile").ZipFile(text_export) as archive:
    text_names = archive.namelist()
with __import__("zipfile").ZipFile(media_export) as archive:
    media_names = archive.namelist()
assert "index.html" in text_names and any(name.endswith("/chat.html") for name in text_names)
assert not any("/media/" in name for name in text_names)
assert any("/media/" in name for name in media_names)
sent_exports = []
original_send_document = b.send_document
b.send_document = lambda chat_id, path, caption="": sent_exports.append((chat_id, path.name, caption))
b.handle_callback_query({
    "id": "export-all", "data": "uexall:t:111:0",
    "from": {"id": 999}, "message": {"message_id": 11, "chat": {"id": 999, "type": "private"}},
})
b.send_document = original_send_document
assert sent_exports and sent_exports[0][0] == 999 and "без медиа" in sent_exports[0][2]
b.save_message("business:test-connection", {
    "message_id": 2, "from": {"id": 333, "first_name": "Клиент", "username": "client333"},
    "chat": {"id": 333, "type": "private", "first_name": "Клиент"}, "text": "Изменённое сообщение",
})
b.mark_message_deleted("regular", -100500, 1)
edited_text, edited_markup = b.page_user_chats(999, 111, filter_name="edited")
deleted_text, deleted_markup = b.page_user_chats(999, 111, filter_name="deleted")
assert "Изменённые" in edited_text and "uchat:111:333" in json.dumps(edited_markup)
assert "Удалённые" in deleted_text and "uchat:111:-100500" in json.dumps(deleted_markup)
assert "Просмотры чатов" in b.page_view_audit(999)[0]
assert "Хранение и медиа" in b.page_storage_settings(999)[0]
digest_settings_text, digest_settings_markup = b.page_digest_settings(999)
assert "5 часов" in digest_settings_text and "digset:1141626866:7732538826" in json.dumps(digest_settings_markup)
b.register_user(444, 444, {"first_name": "Скрытый"})
b.set_user_chats_hidden(999, 444, True, "тест")
assert b.user_chats_are_hidden(444) and "uchats:" not in json.dumps(b.page_user_card(999, 444)[1])
b.set_user_chats_hidden(999, 444, False)
assert not b.user_chats_are_hidden(444)
b.schedule_temporary_admin_message(999, {"message_id": 777}, 1)
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute("UPDATE temporary_admin_messages SET delete_at=0 WHERE chat_id=999 AND message_id=777")
assert b.delete_expired_temporary_messages() == 1
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    old_ts = int(time.time()) - 40 * 86400
    conn.execute("INSERT OR REPLACE INTO chat_owners (chat_id,owner_id,created_at) VALUES (?,?,?)", (-100501, 111, old_ts))
b.save_message("regular", {
    "message_id": 99, "from": {"id": 555, "first_name": "Старый"},
    "chat": {"id": -100501, "type": "supergroup", "title": "Старый чат"}, "text": "Старое сообщение",
})
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute("UPDATE messages SET created_at=?,updated_at=? WHERE chat_id=-100501", (old_ts, old_ts))
b.maintenance_set("retention_regular_days", "30")
deleted_rows, _ = b.cleanup_expired_messages()
assert deleted_rows == 1
b.maintenance_set("retention_regular_days", "0")
for callback_data in (
    "ucl:111:0:0:all:recent", "uchat:111:333:0:0", "ugal:111:333:0:0:all:0",
    "uday:111:333:0:0:0:today", "storage", "viewaudit", "digsettings",
):
    b.handle_callback_query({
        "id": "feature-route", "data": callback_data,
        "from": {"id": 999}, "message": {"message_id": 50, "chat": {"id": 999, "type": "private"}},
    })
assert {"voice", "photo", "video", "video_note", "sticker"} <= set(b.ADMIN_MEDIA_LABELS) <= set(b.MEDIA_SENDERS)
assert b.get_user_owned_saved_message(111, 333, 2)["media_file_id"] == "voice-test"
connections_text, _ = b.page_connections(111)
assert "owner_id=" not in connections_text and "notify=" not in connections_text
denied_text, _ = b.page_user_chats(222, 111)
assert "только владельцу" in denied_text
blocked_chats_text, blocked_chats_markup = b.page_user_chats(999, 8464597898)
assert "скрыты" in blocked_chats_text
blocked_card_text, blocked_card_markup = b.page_user_card(999, 8464597898)
assert "uchats:" not in json.dumps(blocked_card_markup)
ok, _ = b.add_bot_admin(999, 222)
assert ok and b.is_admin_user(222) and not b.is_owner_admin(222)
delegated_card, delegated_markup = b.page_user_card(222, 111)
assert "Карточка пользователя" in delegated_card and "uchats:" not in json.dumps(delegated_markup)
admin_card, admin_markup = b.page_user_card(999, 222)
assert "uchats:" not in json.dumps(admin_markup), "чаты администратора отсутствуют в карточке"
admin_chats_text, _ = b.page_user_chats(999, 222)
assert "скрыты" in admin_chats_text, "чаты администратора нельзя открыть прямой кнопкой"
assert b.get_user_owned_saved_message(222, 333, 2) is None

denials = []
original_answer_callback = b.answer_callback
b.answer_callback = lambda *args, **kwargs: denials.append((args, kwargs))
b.handle_callback_query({
    "id": "deny-1", "data": "uchats:111:0:0",
    "from": {"id": 222}, "message": {"message_id": 9, "chat": {"id": 222, "type": "private"}},
})
media_sends = []
original_send_saved_media = b.send_saved_media
b.send_saved_media = lambda chat_id, saved, *args, **kwargs: media_sends.append((chat_id, saved)) or True
b.handle_callback_query({
    "id": "deny-media", "data": "umedia:111:333:2",
    "from": {"id": 222}, "message": {"message_id": 9, "chat": {"id": 222, "type": "private"}},
})
b.handle_callback_query({
    "id": "open-media", "data": "umedia:111:333:2",
    "from": {"id": 999}, "message": {"message_id": 10, "chat": {"id": 999, "type": "private"}},
})
b.send_saved_media = original_send_saved_media
b.answer_callback = original_answer_callback
assert sum(bool(kwargs.get("show_alert")) for _args, kwargs in denials) >= 2, "закрытые callback недоступны делегированному админу"
assert len(media_sends) == 1 and media_sends[0][0] == 999, "медиа отправляется только владельцу"
b.remove_bot_admin(999, 222)
gift_text, gift_markup = b.page_gift_buy(111, 222)
assert "Подарочная подписка" in gift_text and "gift:stars:222:15" in json.dumps(gift_markup)

# кнопка копирования ссылки и иконки
_, m = b.page_ref(111)
row0 = m["inline_keyboard"][0][0]
assert row0["copy_text"]["text"].startswith("https://t.me/")
_, m = b.page_buy(111)
assert m["inline_keyboard"][0][0]["callback_data"] == "buy:sbp:15"
assert m["inline_keyboard"][0][1]["callback_data"] == "buy:stars:15"
assert m["inline_keyboard"][1][0]["callback_data"] == "buy:sbp:30"
assert m["inline_keyboard"][1][1]["callback_data"] == "buy:stars:30"
assert any(button.get("callback_data") == "style" for row in m["inline_keyboard"] for button in row)
b.add_days(111, 15)
active_buy_text, active_buy_markup = b.page_buy(111)
assert "Моя подписка" in active_buy_text
assert any("Продлить" in button.get("text", "") for row in active_buy_markup["inline_keyboard"] for button in row)

# тарифы
assert b.get_plans() == {
    15: {"stars": 50, "rub": 40},
    30: {"stars": 100, "rub": 80},
}
b.set_plan_price(15, "rub", 41)
assert b.get_plan(15)["rub"] == 41
b.set_plan_price(15, "rub", 40)

# промокоды и расчёт скидки
b.PENDING_PROMO_CREATE[999] = time.time() + 60
assert b.handle_promo_create_input(999, 999, "START20 20 30 10")
ok, promo_message = b.activate_promo(111, "start20")
assert ok and "20%" in promo_message
assert b.discounted_price(111, 40) == (32, "START20")
promo_buy_text, _ = b.page_buy(111)
assert "32 ₽" in promo_buy_text and "40 ⭐" in promo_buy_text
assert b.parse_subscription_payload("sub:111:222:15:40:START20") == (111, 222, 15, 40, "START20")
assert b.valid_subscription_amount(111, 15, 40, "START20")
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute("DELETE FROM promo_activations WHERE user_id=111")

# блокировка с причиной и разблокировка
b.PENDING_BLOCK_REASON[999] = (222, 0, time.time() + 60)
assert b.handle_block_reason_input(999, 999, "нарушение правил")
assert b.is_blocked(222) and not b.sub_active(222)
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    conn.execute("DELETE FROM blocked_users WHERE user_id=222")
assert not b.is_blocked(222)

# поддержка: обращение и ответ администратора
sent_support = []
b.send_message = lambda *a, **k: sent_support.append((a, k))
b.PENDING_SUPPORT[111] = time.time() + 60
assert b.handle_support_input(111, 111, "Нужна помощь")
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    ticket_id = conn.execute("SELECT id FROM support_tickets ORDER BY id DESC LIMIT 1").fetchone()[0]
b.PENDING_SUPPORT_REPLY[999] = (ticket_id, 111, time.time() + 60)
assert b.handle_support_reply_input(999, 999, "Всё исправили")
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    assert conn.execute("SELECT status FROM support_tickets WHERE id=?", (ticket_id,)).fetchone()[0] == "closed"

# рассылка, CSV, резервная копия и напоминания
b.BROADCAST_PREVIEWS[999] = ("all", "Тестовая рассылка", time.time() + 60)
network_calls = []
b.telegram_call = lambda method, payload=None, timeout=30: network_calls.append((method, payload)) or True
sent_count, failed_count = b.execute_broadcast(999, 999)
assert sent_count >= 1 and failed_count == 0
csv_path = b.export_users_csv(999)
assert csv_path.exists() and "user_id" in csv_path.read_text(encoding="utf-8-sig")
backup_path = b.create_backup(999, send_to_admins=False)
assert backup_path.exists()
b.set_until(111, int(time.time()) + 23 * 3600)
b.send_expiry_reminders()
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    assert conn.execute("SELECT 1 FROM subscription_reminders WHERE user_id=111 AND days_before=1").fetchone()

# СБП: создание, точная сверка и защита от повторного начисления
sent = []
b.send_message = lambda *a, **k: sent.append((a, k))
b.ROLLYPAY_API_KEY = "test"
b.ROLLYPAY_ENABLED = True
b.rollypay_call = lambda method, path, payload=None: (
    {"payment_id": "pay-1", "pay_url": "https://pay.example/1"}
    if method == "POST"
    else {
        "payment_id": "pay-1", "order_id": path.rsplit("/", 1)[-1],
        "payment_currency": "RUB", "amount": "40.00", "status": "paid",
    }
)
b.send_sbp_payment(111, 111, 15)
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    payment_id, order_id = conn.execute(
        "SELECT payment_id, order_id FROM sbp_payments"
    ).fetchone()

def paid_response(method, path, payload=None):
    return {
        "payment_id": payment_id, "order_id": order_id,
        "payment_currency": "RUB", "amount": "40.00", "status": "paid",
    }

b.rollypay_call = paid_response
b.set_until(111, 0, None)
paid, _ = b.check_sbp_payment(111, order_id)
first_until = b.get_sub(111)[0]
assert paid and first_until > time.time()
paid, _ = b.check_sbp_payment(111, order_id)
assert paid and b.get_sub(111)[0] == first_until, "СБП не начисляется повторно"

# дайджест новых сообщений за пять часов для владельцев
with __import__("sqlite3").connect(b.DB_PATH) as conn:
    now = int(time.time())
    conn.execute("INSERT OR REPLACE INTO chat_owners (chat_id,owner_id,created_at) VALUES (?,?,?)", (-100900, 7732538826, now))
b.save_message("regular", {
    "message_id": 9, "from": {"id": 444, "first_name": "Собеседник", "username": "digest_user"},
    "chat": {"id": -100900, "type": "supergroup", "title": "Дайджест"}, "text": "Новое сообщение",
})
digest_sent = []
b.send_message = lambda *a, **k: digest_sent.append((a, k))
b.maintenance_set("message_digest_5h_7732538826", str(int(time.time()) - b.MESSAGE_DIGEST_INTERVAL_SEC - 10))
b.send_message_digest()
assert {item[0][0] for item in digest_sent} == {1141626866, 8464597898}
assert "5 часов" in digest_sent[0][0][1] and "@digest_user" in digest_sent[0][0][1]
assert "digtog:7732538826" in json.dumps(digest_sent[0][1]["reply_markup"])
assert "digmedia:7732538826" in json.dumps(digest_sent[0][1]["reply_markup"])
digest_sent.clear()
b.send_message_digest()
assert not digest_sent, "повторный отчёт раньше пяти часов не отправляется"
assert 1141626866 in b.ADMIN_USER_IDS and 8464597898 in b.ADMIN_USER_IDS

# Web App: подпись Telegram, приватность, списки чатов и сообщений
import hashlib, hmac
from urllib.parse import urlencode
import webapp_server as web

web_user = json.dumps({"id": 999, "first_name": "Owner"}, ensure_ascii=False, separators=(",", ":"))
web_fields = {"auth_date": str(int(time.time())), "query_id": "test-query", "user": web_user}
web_check = "\n".join(f"{key}={web_fields[key]}" for key in sorted(web_fields))
web_secret = hmac.new(b"WebAppData", b.BOT_TOKEN.encode(), hashlib.sha256).digest()
web_fields["hash"] = hmac.new(web_secret, web_check.encode(), hashlib.sha256).hexdigest()
assert web.validate_init_data(urlencode(web_fields), b.BOT_TOKEN)["id"] == 999
bad_fields = dict(web_fields); bad_fields["hash"] = "0" * 64
try:
    web.validate_init_data(urlencode(bad_fields), b.BOT_TOKEN)
    raise AssertionError("поддельная подпись Web App принята")
except PermissionError:
    pass
web_users = web.list_users(b)
assert any(item["id"] == 111 and item["chat_count"] >= 1 for item in web_users["items"])
assert all(not b.user_chats_are_hidden(item["id"]) for item in web_users["items"])
web_chats = web.list_chats(b, 999, 111)
assert any(item["id"] == 333 and item["message_count"] >= 1 for item in web_chats)
web_messages = web.list_messages(b, 111, 333)
assert web_messages["items"] and any(item["media"] for item in web_messages["items"])
panel_markup = b.page_panel(999)[1]
assert any(button.get("web_app", {}).get("url") == b.WEBAPP_URL for row in panel_markup["inline_keyboard"] for button in row)

# миграция существующей базы без потери старых таблиц
legacy_db = test_root / "legacy.sqlite3"
with __import__("sqlite3").connect(legacy_db) as conn:
    conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, private_chat_id INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)")
    conn.execute("CREATE TABLE payments (tg_payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, days INTEGER NOT NULL, stars INTEGER NOT NULL, payload TEXT, created_at INTEGER NOT NULL)")
    conn.execute("CREATE TABLE sbp_payments (payment_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE, user_id INTEGER NOT NULL, days INTEGER NOT NULL, rub INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'created', created_at INTEGER NOT NULL, paid_at INTEGER)")
b.DB_PATH = legacy_db
b.init_db()
with __import__("sqlite3").connect(legacy_db) as conn:
    assert {"first_name", "last_name", "username", "communication_style"}.issubset({row[1] for row in conn.execute("PRAGMA table_info(users)")})
    assert {"payer_id", "promo_code"}.issubset({row[1] for row in conn.execute("PRAGMA table_info(payments)")})
    assert {"payer_id", "promo_code"}.issubset({row[1] for row in conn.execute("PRAGMA table_info(sbp_payments)")})
    assert {"edit_count", "original_content"}.issubset({row[1] for row in conn.execute("PRAGMA table_info(messages)")})
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"chat_view_state", "admin_user_labels", "hidden_chat_users", "admin_view_log", "temporary_admin_messages", "digest_settings"}.issubset(tables)

print("ALL TESTS PASSED")
