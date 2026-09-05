import sqlite3
from datetime import datetime

import config


def connect():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    conn = connect()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            ref_id INTEGER,
            balance REAL DEFAULT 0,
            created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            phone TEXT,
            comment TEXT,
            status TEXT DEFAULT 'new',
            created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            details TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
        """
    )
    cols = [row[1] for row in c.execute("PRAGMA table_info(withdrawals)").fetchall()]
    if "skin" not in cols:
        c.execute("ALTER TABLE withdrawals ADD COLUMN skin TEXT")
    if "screenshot" not in cols:
        c.execute("ALTER TABLE withdrawals ADD COLUMN screenshot TEXT")
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            amount REAL,
            used INTEGER DEFAULT 0,
            used_by INTEGER,
            used_at TEXT,
            created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sponsor TEXT,
            reward REAL,
            active INTEGER DEFAULT 1,
            created_at TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS task_dones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            user_id INTEGER,
            rewarded INTEGER DEFAULT 1,
            created_at TEXT,
            UNIQUE(task_id, user_id)
        )
        """
    )
    conn.commit()
    conn.close()


def set_setting(key, value):
    conn = connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return int(row["value"])
    except ValueError:
        return row["value"]


def add_user(user_id, username, first_name, ref_id=None):
    conn = connect()
    row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    is_new = row is None
    if is_new:
        conn.execute(
            "INSERT INTO users (id, username, first_name, ref_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, first_name, ref_id, datetime.now().isoformat()),
        )
        conn.commit()
    else:
        if username is not None:
            conn.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
            conn.commit()
    conn.close()
    return is_new


def get_user(user_id):
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row


def count_referrals(ref_id):
    conn = connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM users WHERE ref_id = ?", (ref_id,)
    ).fetchone()[0]
    conn.close()
    return count


def add_balance(user_id, amount):
    conn = connect()
    conn.execute(
        "UPDATE users SET balance = balance + ? WHERE id = ?",
        (amount, user_id),
    )
    conn.commit()
    conn.close()


def spend_balance(user_id, amount):
    conn = connect()
    cur = conn.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?",
        (amount, user_id, amount),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def set_balance(user_id, amount):
    conn = connect()
    conn.execute(
        "UPDATE users SET balance = ? WHERE id = ?",
        (max(float(amount), 0), user_id),
    )
    conn.commit()
    conn.close()


def add_request(user_id, name, phone, comment):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO requests (user_id, name, phone, comment, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, name, phone, comment, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def get_request(req_id):
    conn = connect()
    row = conn.execute("SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
    conn.close()
    return row


def set_request_status(req_id, status):
    conn = connect()
    conn.execute("UPDATE requests SET status = ? WHERE id = ?", (status, req_id))
    conn.commit()
    conn.close()


def list_requests(status=None, limit=10):
    conn = connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM requests WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return rows


def add_code(code, amount):
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO promocodes (code, amount, created_at) VALUES (?, ?, ?)",
        (code, amount, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_code(code):
    conn = connect()
    row = conn.execute("SELECT * FROM promocodes WHERE code = ?", (code,)).fetchone()
    conn.close()
    return row


def use_code(code, user_id):
    conn = connect()
    cur = conn.execute(
        "UPDATE promocodes SET used = 1, used_by = ?, used_at = ? "
        "WHERE code = ? AND used = 0",
        (user_id, datetime.now().isoformat(), code),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_codes(limit=20):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM promocodes ORDER BY created_at DESC, code LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def add_withdrawal(user_id, amount, details, skin=None, screenshot=None):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO withdrawals (user_id, amount, details, skin, screenshot, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, amount, details, skin, screenshot, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def get_withdrawal(wd_id):
    conn = connect()
    row = conn.execute("SELECT * FROM withdrawals WHERE id = ?", (wd_id,)).fetchone()
    conn.close()
    return row


def set_withdrawal_status(wd_id, status):
    conn = connect()
    conn.execute("UPDATE withdrawals SET status = ? WHERE id = ?", (status, wd_id))
    conn.commit()
    conn.close()


def list_withdrawals(status=None, limit=15):
    conn = connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM withdrawals WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM withdrawals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return rows


def list_user_withdrawals(user_id, limit=15):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def list_user_requests(user_id, limit=15):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def stats():
    conn = connect()
    users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    new_requests = conn.execute("SELECT COUNT(*) FROM requests WHERE status = 'new'").fetchone()[0]
    all_requests = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    pending_wds = conn.execute(
        "SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'"
    ).fetchone()[0]
    conn.close()
    return {
        "users": users,
        "new_requests": new_requests,
        "all_requests": all_requests,
        "pending_wds": pending_wds,
    }


def add_task(sponsor, reward):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO tasks (sponsor, reward, created_at) VALUES (?, ?, ?)",
        (sponsor, reward, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return cur.lastrowid


def get_task(task_id):
    conn = connect()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    conn.close()
    return row


def list_tasks(active=True):
    conn = connect()
    if active is None:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE active = ? ORDER BY id DESC", (1 if active else 0,)
        ).fetchall()
    conn.close()
    return rows


def deactivate_task(task_id):
    conn = connect()
    conn.execute("UPDATE tasks SET active = 0 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


def get_completion(task_id, user_id):
    conn = connect()
    row = conn.execute(
        "SELECT * FROM task_dones WHERE task_id = ? AND user_id = ?",
        (task_id, user_id),
    ).fetchone()
    conn.close()
    return row


def add_completion(task_id, user_id):
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO task_dones (task_id, user_id, created_at) VALUES (?, ?, ?)",
        (task_id, user_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def set_completion_rewarded(task_id, user_id, rewarded):
    conn = connect()
    conn.execute(
        "UPDATE task_dones SET rewarded = ? WHERE task_id = ? AND user_id = ?",
        (1 if rewarded else 0, task_id, user_id),
    )
    conn.commit()
    conn.close()


def list_completions_rewarded():
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM task_dones WHERE rewarded = 1"
    ).fetchall()
    conn.close()
    return rows