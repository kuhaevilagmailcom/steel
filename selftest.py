import os, sys, json, time, tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import deleted_message_logger_bot as b

# всё в временную базу, сеть не трогаем
b.DB_PATH = __import__("pathlib").Path(tempfile.mkdtemp()) / "test.sqlite3"
b.send_message = lambda *a, **k: None  # заглушка сети

b.init_db()

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

# продление суммируется
t0 = b.add_days(111, 15)
t1 = b.add_days(111, 15)
assert t1 >= t0 + 15 * 86400 - 2, "продление прибавляет дни"

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
    conn.execute("INSERT INTO payments VALUES ('chg1',111,15,50,'sub:111:15:50',?)", (now,))
    cur = conn.execute("INSERT OR IGNORE INTO payments VALUES ('chg1',111,15,50,'x',?)", (now,))
    assert cur.rowcount == 0, "дубликат платежа не проходит"

# --- страницы ---
cases = {
    "page_home": 111, "page_buy": 111, "page_ref": 111,
    "page_help": 111, "page_connections": 111, "page_panel": 999,
    "page_prices": 999,
}
for name, uid in cases.items():
    text, markup = getattr(b, name)(uid)
    assert "<tg-emoji" in text, name + " без премиум-эмодзи"
    assert "inline_keyboard" in markup and json.dumps(markup)
    cb = [btn.get("callback_data") for row in markup["inline_keyboard"] for btn in row]
    assert all((c or "").__len__() <= 64 for c in cb if c), "callback_data <=64"
    print(f"OK {name}: {len(text)} chars, {len(markup['inline_keyboard'])} rows")

# кнопка копирования ссылки и иконки
_, m = b.page_ref(111)
row0 = m["inline_keyboard"][0][0]
assert row0["copy_text"]["text"].startswith("https://t.me/") and row0["icon_custom_emoji_id"]
_, m = b.page_buy(111)
assert m["inline_keyboard"][0][0]["callback_data"] == "buy:sbp:15"
assert m["inline_keyboard"][0][1]["callback_data"] == "buy:stars:15"
assert m["inline_keyboard"][1][0]["callback_data"] == "buy:sbp:30"
assert m["inline_keyboard"][1][1]["callback_data"] == "buy:stars:30"

# тарифы
assert b.get_plans() == {
    15: {"stars": 50, "rub": 40},
    30: {"stars": 100, "rub": 80},
}
b.set_plan_price(15, "rub", 41)
assert b.get_plan(15)["rub"] == 41
b.set_plan_price(15, "rub", 40)

# СБП: создание, точная сверка и защита от повторного начисления
sent = []
b.send_message = lambda *a, **k: sent.append((a, k))
b.ROLLYPAY_API_KEY = "test"
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

print("ALL TESTS PASSED")
