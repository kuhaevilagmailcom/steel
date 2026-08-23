import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.getenv("DATABASE_PATH", "data/bot.db")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL NOT NULL DEFAULT 0,
            total_salary REAL NOT NULL DEFAULT 0,
            wallet TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            wallet TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT,
            processed_by INTEGER
        );

        CREATE TABLE IF NOT EXISTS salary_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            reason TEXT NOT NULL,
            admin_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """)

def now():
    return datetime.now(timezone.utc).isoformat()

def upsert_user(user):
    with db() as c:
        c.execute("""
        INSERT INTO users(telegram_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
          username=excluded.username,
          first_name=excluded.first_name
        """, (user.id, user.username or "", user.first_name or "", now()))

def get_user(tg_id):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,)).fetchone()

def set_wallet(tg_id, wallet):
    with db() as c:
        c.execute("UPDATE users SET wallet=? WHERE telegram_id=?", (wallet, tg_id))

def add_salary(tg_id, amount, reason, admin_id):
    with db() as c:
        c.execute("UPDATE users SET balance=balance+?, total_salary=total_salary+? WHERE telegram_id=?",
                  (amount, amount, tg_id))
        c.execute("""
        INSERT INTO salary_logs(telegram_id, amount, reason, admin_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (tg_id, amount, reason, admin_id, now()))

def create_withdrawal(tg_id, amount, wallet):
    with db() as c:
        row = c.execute("SELECT balance FROM users WHERE telegram_id=?", (tg_id,)).fetchone()
        if not row or row["balance"] < amount:
            return None
        c.execute("UPDATE users SET balance=balance-? WHERE telegram_id=?", (amount, tg_id))
        cur = c.execute("""
        INSERT INTO withdrawals(telegram_id, amount, wallet, created_at)
        VALUES (?, ?, ?, ?)
        """, (tg_id, amount, wallet, now()))
        return cur.lastrowid

def get_withdrawal(wid):
    with db() as c:
        return c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()

def process_withdrawal(wid, status, admin_id):
    with db() as c:
        row = c.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not row or row["status"] != "pending":
            return False
        if status == "rejected":
            c.execute("UPDATE users SET balance=balance+? WHERE telegram_id=?", (row["amount"], row["telegram_id"]))
        c.execute("""
        UPDATE withdrawals
        SET status=?, processed_at=?, processed_by=?
        WHERE id=?
        """, (status, now(), admin_id, wid))
        return True

def list_users(limit=50, offset=0):
    with db() as c:
        return c.execute("""
        SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

def count_users():
    with db() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]

def search_user(tg_id):
    return get_user(tg_id)

def list_pending_withdrawals(limit=50):
    with db() as c:
        return c.execute("""
        SELECT w.*, u.username, u.first_name
        FROM withdrawals w
        LEFT JOIN users u ON u.telegram_id=w.telegram_id
        WHERE w.status='pending'
        ORDER BY w.created_at ASC
        LIMIT ?
        """, (limit,)).fetchall()

def stats():
    with db() as c:
        return c.execute("""
        SELECT
          COUNT(*) users,
          COALESCE(SUM(balance),0) balances,
          COALESCE(SUM(total_salary),0) salaries
        FROM users
        """).fetchone()
