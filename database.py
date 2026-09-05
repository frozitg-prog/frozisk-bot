from datetime import datetime

import psycopg
from psycopg.rows import dict_row

import config


def connect():
    conn = psycopg.connect(
        config.DATABASE_URL,
        row_factory=dict_row,
    )
    return conn


def init():
    conn = connect()
    with conn.cursor() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                ref_id BIGINT,
                balance DOUBLE PRECISION DEFAULT 0,
                streak INTEGER DEFAULT 0,
                streak_date TEXT,
                roulette_wins INTEGER DEFAULT 0,
                promo_last_created BIGINT DEFAULT 0,
                created_at TEXT
            )
            """
        )
        c.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS streak INTEGER DEFAULT 0"
        )
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS streak_date TEXT")
        c.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS roulette_wins INTEGER DEFAULT 0"
        )
        c.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS promo_last_created BIGINT DEFAULT 0"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                amount DOUBLE PRECISION,
                details TEXT,
                skin TEXT,
                screenshot TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
            """
        )
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
                amount DOUBLE PRECISION,
                used INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 1,
                owner_id BIGINT,
                used_by BIGINT,
                used_at TEXT,
                created_at TEXT
            )
            """
        )
        c.execute(
            "ALTER TABLE promocodes ADD COLUMN IF NOT EXISTS max_uses INTEGER DEFAULT 1"
        )
        c.execute(
            "ALTER TABLE promocodes ADD COLUMN IF NOT EXISTS owner_id BIGINT"
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_uses (
                code TEXT,
                user_id BIGINT,
                activated_at TEXT,
                PRIMARY KEY (code, user_id)
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id SERIAL PRIMARY KEY,
                sponsor TEXT,
                reward DOUBLE PRECISION,
                active INTEGER DEFAULT 1,
                created_at TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS task_dones (
                id SERIAL PRIMARY KEY,
                task_id INTEGER,
                user_id BIGINT,
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
        "INSERT INTO settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = connect()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = %s", (key,)
    ).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return int(row["value"])
    except ValueError:
        return row["value"]


def add_user(user_id, username, first_name, ref_id=None):
    conn = connect()
    row = conn.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
    is_new = row is None
    if is_new:
        conn.execute(
            "INSERT INTO users (id, username, first_name, ref_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, username, first_name, ref_id, datetime.now().isoformat()),
        )
        conn.commit()
    else:
        if username is not None:
            conn.execute("UPDATE users SET username = %s WHERE id = %s", (username, user_id))
            conn.commit()
    conn.close()
    return is_new


def get_user(user_id):
    conn = connect()
    row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    conn.close()
    return row


def get_user_by_username(username):
    conn = connect()
    row = conn.execute(
        "SELECT * FROM users WHERE lower(username) = lower(%s)",
        (username.lstrip("@"),),
    ).fetchone()
    conn.close()
    return row


def count_referrals(ref_id):
    conn = connect()
    count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE ref_id = %s", (ref_id,)
    ).fetchone()["cnt"]
    conn.close()
    return count


def add_balance(user_id, amount):
    conn = connect()
    conn.execute(
        "UPDATE users SET balance = balance + %s WHERE id = %s",
        (amount, user_id),
    )
    conn.commit()
    conn.close()


def spend_balance(user_id, amount):
    conn = connect()
    cur = conn.execute(
        "UPDATE users SET balance = balance - %s WHERE id = %s AND balance >= %s",
        (amount, user_id, amount),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def set_balance(user_id, amount):
    conn = connect()
    conn.execute(
        "UPDATE users SET balance = %s WHERE id = %s",
        (max(float(amount), 0), user_id),
    )
    conn.commit()
    conn.close()


def update_streak(user_id, days, last_date):
    conn = connect()
    conn.execute(
        "UPDATE users SET streak = %s, streak_date = %s WHERE id = %s",
        (days, last_date, user_id),
    )
    conn.commit()
    conn.close()


def add_balance_all(amount, exclude_ids):
    conn = connect()
    cur = conn.execute(
        "UPDATE users SET balance = balance + %s WHERE NOT (id = ANY(%s))",
        (amount, list(exclude_ids)),
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected


def subtract_balance_all(amount, exclude_ids):
    conn = connect()
    cur = conn.execute(
        "UPDATE users SET balance = GREATEST(balance - %s, 0) WHERE NOT (id = ANY(%s))",
        (amount, list(exclude_ids)),
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected


def reset_all_balances(exclude_ids):
    conn = connect()
    cur = conn.execute(
        "UPDATE users SET balance = 0 WHERE NOT (id = ANY(%s))",
        (list(exclude_ids),),
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()
    return affected


def add_roulette_win(user_id):
    conn = connect()
    conn.execute(
        "UPDATE users SET roulette_wins = roulette_wins + 1 WHERE id = %s",
        (user_id,),
    )
    conn.commit()
    conn.close()


def top_balance(limit=10):
    conn = connect()
    rows = conn.execute(
        "SELECT id, username, first_name, balance FROM users "
        "WHERE balance > 0 ORDER BY balance DESC LIMIT %s",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def top_referrals(limit=10):
    conn = connect()
    rows = conn.execute(
        "SELECT u.id, u.username, u.first_name, COUNT(*) AS refs "
        "FROM users u JOIN users r ON r.ref_id = u.id "
        "WHERE r.ref_id != r.id "
        "GROUP BY u.id ORDER BY refs DESC LIMIT %s",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def top_roulette(limit=10):
    conn = connect()
    rows = conn.execute(
        "SELECT id, username, first_name, roulette_wins FROM users "
        "WHERE roulette_wins > 0 ORDER BY roulette_wins DESC LIMIT %s",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def add_withdrawal(user_id, amount, details, skin=None, screenshot=None):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO withdrawals (user_id, amount, details, skin, screenshot, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, amount, details, skin, screenshot, datetime.now().isoformat()),
    )
    wd_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    return wd_id


def get_withdrawal(wd_id):
    conn = connect()
    row = conn.execute("SELECT * FROM withdrawals WHERE id = %s", (wd_id,)).fetchone()
    conn.close()
    return row


def set_withdrawal_status(wd_id, status):
    conn = connect()
    conn.execute("UPDATE withdrawals SET status = %s WHERE id = %s", (status, wd_id))
    conn.commit()
    conn.close()


def list_withdrawals(status=None, limit=15):
    conn = connect()
    if status:
        rows = conn.execute(
            "SELECT * FROM withdrawals WHERE status = %s ORDER BY id DESC LIMIT %s",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM withdrawals ORDER BY id DESC LIMIT %s", (limit,)
        ).fetchall()
    conn.close()
    return rows


def list_user_withdrawals(user_id, limit=15):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM withdrawals WHERE user_id = %s ORDER BY id DESC LIMIT %s",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def add_code(code, amount, max_uses=1, owner_id=None):
    conn = connect()
    conn.execute(
        "INSERT INTO promocodes (code, amount, max_uses, owner_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (code) DO NOTHING",
        (code, amount, max_uses, owner_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_code(code):
    conn = connect()
    row = conn.execute("SELECT * FROM promocodes WHERE code = %s", (code,)).fetchone()
    conn.close()
    return row


def use_code(code, user_id):
    conn = connect()
    with conn:
        already = conn.execute(
            "SELECT 1 FROM promo_uses WHERE code = %s AND user_id = %s",
            (code, user_id),
        ).fetchone()
        if already:
            return False
        cur = conn.execute(
            "UPDATE promocodes SET used = used + 1, used_at = %s "
            "WHERE code = %s AND (max_uses = 0 OR used < max_uses)",
            (datetime.now().isoformat(), code),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "INSERT INTO promo_uses (code, user_id, activated_at) VALUES (%s, %s, %s)",
            (code, user_id, datetime.now().isoformat()),
        )
    return True


def list_codes(limit=20):
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM promocodes ORDER BY created_at DESC, code LIMIT %s", (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_all_user_ids():
    conn = connect()
    rows = conn.execute("SELECT id FROM users").fetchall()
    conn.close()
    if not rows:
        return []
    return [row["id"] for row in rows]


def set_promo_last_created(user_id, ts):
    conn = connect()
    conn.execute(
        "UPDATE users SET promo_last_created = %s WHERE id = %s", (ts, user_id)
    )
    conn.commit()
    conn.close()


def add_task(sponsor, reward):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO tasks (sponsor, reward, created_at) VALUES (%s, %s, %s) RETURNING id",
        (sponsor, reward, datetime.now().isoformat()),
    )
    task_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    return task_id


def get_task(task_id):
    conn = connect()
    row = conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
    conn.close()
    return row


def list_tasks(active=True):
    conn = connect()
    if active is None:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE active = %s ORDER BY id DESC", (1 if active else 0,)
        ).fetchall()
    conn.close()
    return rows


def deactivate_task(task_id):
    conn = connect()
    conn.execute("UPDATE tasks SET active = 0 WHERE id = %s", (task_id,))
    conn.commit()
    conn.close()


def get_completion(task_id, user_id):
    conn = connect()
    row = conn.execute(
        "SELECT * FROM task_dones WHERE task_id = %s AND user_id = %s",
        (task_id, user_id),
    ).fetchone()
    conn.close()
    return row


def add_completion(task_id, user_id):
    conn = connect()
    conn.execute(
        "INSERT INTO task_dones (task_id, user_id, created_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (task_id, user_id) DO NOTHING",
        (task_id, user_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def set_completion_rewarded(task_id, user_id, rewarded):
    conn = connect()
    conn.execute(
        "UPDATE task_dones SET rewarded = %s WHERE task_id = %s AND user_id = %s",
        (1 if rewarded else 0, task_id, user_id),
    )
    conn.commit()
    conn.close()


def list_completions_rewarded():
    conn = connect()
    rows = conn.execute("SELECT * FROM task_dones WHERE rewarded = 1").fetchall()
    conn.close()
    return rows


def stats():
    conn = connect()
    users = conn.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"]
    total_balance = conn.execute(
        "SELECT COALESCE(SUM(balance), 0) AS s FROM users"
    ).fetchone()["s"]
    total_referrals = conn.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE ref_id IS NOT NULL AND ref_id != id"
    ).fetchone()["cnt"]
    pending_wds = conn.execute(
        "SELECT COUNT(*) AS cnt FROM withdrawals WHERE status = 'pending'"
    ).fetchone()["cnt"]
    conn.close()
    return {
        "users": users,
        "total_balance": total_balance,
        "total_referrals": total_referrals,
        "pending_wds": pending_wds,
    }