import threading
import threading
from datetime import datetime

import config

USE_PG = bool(config.DATABASE_URL and config.DATABASE_URL.strip())

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row
else:
    import sqlite3

_PH = "%s" if USE_PG else "?"


def _dict_factory(cursor, row):
    return {cursor.description[i][0]: row[i] for i in range(len(row))}


def connect():
    if USE_PG:
        return psycopg.connect(
            config.DATABASE_URL,
            row_factory=dict_row,
            connect_timeout=15,
        )

    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = _dict_factory
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _ensure_column(conn, table, column, decl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _pg_schema_ok(conn):
    rows = conn.execute(
        "SELECT table_name, string_agg(column_name, ',' ORDER BY column_name) AS cols "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' "
        "GROUP BY table_name"
    ).fetchall()
    schema = {
        "users": (
            "banned", "balance", "created_at", "first_name", "id", "muted",
            "promo_last_created", "ref_id", "roulette_wins", "streak",
            "streak_date", "username",
        ),
        "withdrawals": (
            "amount", "created_at", "details", "id", "screenshot", "skin",
            "status", "user_id",
        ),
        "settings": ("key", "value"),
        "promocodes": (
            "amount", "code", "created_at", "max_uses", "owner_id", "used",
            "used_at", "used_by",
        ),
        "promo_uses": ("activated_at", "code", "user_id"),
        "tasks": ("active", "created_at", "id", "reward", "sponsor"),
        "task_dones": ("created_at", "id", "rewarded", "task_id", "user_id"),
    }
    have = {r["table_name"]: (r["cols"] or "").split(",") for r in rows}
    for table, cols in schema.items():
        if table not in have or [c for c in cols if c not in have[table]]:
            return False
    return True


def init():
    import time as _time

    if USE_PG:
        for _attempt in range(5):
            conn = connect()
            try:
                if _pg_schema_ok(conn):
                    conn.close()
                    return
                conn.close()
                break
            except RuntimeError:
                conn.close()
                break
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                if _attempt < 4:
                    _time.sleep(2)
        print("WARNING: could not validate PostgreSQL schema; "
              "starting anyway (bot will retry DB ops per-call)")
        return
    else:
        conn = connect()
        conn.execute("PRAGMA journal_mode = WAL")
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    ref_id INTEGER,
                    balance REAL DEFAULT 0,
                    streak INTEGER DEFAULT 0,
                    streak_date TEXT,
                    roulette_wins INTEGER DEFAULT 0,
                    promo_last_created INTEGER DEFAULT 0,
                    banned INTEGER DEFAULT 0,
                    muted INTEGER DEFAULT 0,
                    created_at TEXT
                )
                """
            )
            _ensure_column(conn, "users", "streak", "INTEGER DEFAULT 0")
            _ensure_column(conn, "users", "streak_date", "TEXT")
            _ensure_column(conn, "users", "roulette_wins", "INTEGER DEFAULT 0")
            _ensure_column(conn, "users", "promo_last_created", "INTEGER DEFAULT 0")
            _ensure_column(conn, "users", "banned", "INTEGER DEFAULT 0")
            _ensure_column(conn, "users", "muted", "INTEGER DEFAULT 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    details TEXT,
                    skin TEXT,
                    screenshot TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promocodes (
                    code TEXT PRIMARY KEY,
                    amount REAL,
                    used INTEGER DEFAULT 0,
                    max_uses INTEGER DEFAULT 1,
                    owner_id INTEGER,
                    used_by INTEGER,
                    used_at TEXT,
                    created_at TEXT
                )
                """
            )
            _ensure_column(conn, "promocodes", "max_uses", "INTEGER DEFAULT 1")
            _ensure_column(conn, "promocodes", "owner_id", "INTEGER")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promo_uses (
                    code TEXT,
                    user_id INTEGER,
                    activated_at TEXT,
                    PRIMARY KEY (code, user_id)
                )
                """
            )
            conn.execute(
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
            conn.execute(
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
    conn.close()


def set_setting(key, value):
    conn = connect()
    with conn:
        conn.execute(
            f"INSERT INTO settings (key, value) VALUES ({_PH}, {_PH}) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
    conn.close()


def get_setting(key, default=None):
    conn = connect()
    row = conn.execute(
        f"SELECT value FROM settings WHERE key = {_PH}", (key,)
    ).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return int(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def add_user(user_id, username, first_name, ref_id=None):
    conn = connect()
    row = conn.execute(
        f"SELECT id FROM users WHERE id = {_PH}", (user_id,)
    ).fetchone()
    is_new = row is None
    if is_new:
        with conn:
            conn.execute(
                f"INSERT INTO users (id, username, first_name, ref_id, created_at) "
                f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
                (user_id, username, first_name, ref_id, datetime.now().isoformat()),
            )
    else:
        if username is not None:
            with conn:
                conn.execute(
                    f"UPDATE users SET username = {_PH} WHERE id = {_PH}",
                    (username, user_id),
                )
    conn.close()
    return is_new


def get_user(user_id):
    conn = connect()
    row = conn.execute(
        f"SELECT * FROM users WHERE id = {_PH}", (user_id,)
    ).fetchone()
    conn.close()
    return row


def get_user_by_username(username):
    conn = connect()
    row = conn.execute(
        f"SELECT * FROM users WHERE lower(username) = lower({_PH})",
        (username.lstrip("@"),),
    ).fetchone()
    conn.close()
    return row


def count_referrals(ref_id):
    conn = connect()
    row = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM users WHERE ref_id = {_PH}", (ref_id,)
    ).fetchone()
    conn.close()
    return row["cnt"]


def add_balance(user_id, amount):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE users SET balance = balance + {_PH} WHERE id = {_PH}",
            (amount, user_id),
        )
    conn.close()


def spend_balance(user_id, amount):
    conn = connect()
    with conn:
        if USE_PG:
            # Списываем ставку; если баланс не дотягивает до amount - берём весь остаток.
            # GREATEST/LEAST защищают от ложного "недостаточно" при float-округлении больших сумм.
            cur = conn.execute(
                f"UPDATE users SET balance = GREATEST(balance - {_PH}, 0) "
                f"WHERE id = {_PH} AND balance > 0",
                (amount, user_id),
            )
        else:
            cur = conn.execute(
                f"UPDATE users SET balance = balance - {_PH} WHERE id = {_PH} AND balance >= {_PH}",
                (amount, user_id, amount),
            )
    conn.close()
    return cur.rowcount > 0


def set_balance(user_id, amount):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE users SET balance = {_PH} WHERE id = {_PH}",
            (max(float(amount), 0), user_id),
        )
    conn.close()


def update_streak(user_id, days, last_date):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE users SET streak = {_PH}, streak_date = {_PH} WHERE id = {_PH}",
            (days, last_date, user_id),
        )
    conn.close()


def _exclude_placeholders(exclude_ids):
    ids = list(exclude_ids)
    return "(" + ",".join(_PH for _ in ids) + ")", ids


def add_balance_all(amount, exclude_ids):
    ph, ids = _exclude_placeholders(exclude_ids)
    conn = connect()
    with conn:
        cur = conn.execute(
            f"UPDATE users SET balance = balance + {_PH} WHERE id NOT IN {ph}",
            (amount, *ids),
        )
    affected = cur.rowcount
    conn.close()
    return affected


def subtract_balance_all(amount, exclude_ids):
    ph, ids = _exclude_placeholders(exclude_ids)
    conn = connect()
    with conn:
        if USE_PG:
            cur = conn.execute(
                f"UPDATE users SET balance = GREATEST(balance - {_PH}, 0) WHERE id NOT IN {ph}",
                (amount, *ids),
            )
        else:
            cur = conn.execute(
                f"UPDATE users SET balance = MAX(balance - {_PH}, 0) WHERE id NOT IN {ph}",
                (amount, *ids),
            )
    affected = cur.rowcount
    conn.close()
    return affected


def reset_all_balances(exclude_ids):
    ph, ids = _exclude_placeholders(exclude_ids)
    conn = connect()
    with conn:
        cur = conn.execute(
            f"UPDATE users SET balance = 0 WHERE id NOT IN {ph}", (*ids,)
        )
        affected = cur.rowcount
        conn.execute("DELETE FROM withdrawals WHERE status = 'pending'")
        conn.execute("DELETE FROM promo_uses")
        conn.execute("DELETE FROM promocodes")
    conn.close()
    return affected


def add_roulette_win(user_id):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE users SET roulette_wins = roulette_wins + 1 WHERE id = {_PH}",
            (user_id,),
        )
    conn.close()


def _admin_filter():
    if not config.ADMIN_IDS:
        return "", ()
    ph = ", ".join(_PH for _ in config.ADMIN_IDS)
    return f" AND id NOT IN ({ph}) ", tuple(config.ADMIN_IDS)


def top_balance(limit=10):
    conn = connect()
    adm_sql, adm_params = _admin_filter()
    rows = conn.execute(
        f"SELECT id, username, first_name, balance FROM users "
        f"WHERE balance > 0 {adm_sql}ORDER BY balance DESC LIMIT {_PH}",
        adm_params + (limit,),
    ).fetchall()
    conn.close()
    return rows


def top_referrals(limit=10):
    conn = connect()
    adm_sql, adm_params = _admin_filter()
    rows = conn.execute(
        f"SELECT u.id, u.username, u.first_name, COUNT(*) AS refs "
        f"FROM users u JOIN users r ON r.ref_id = u.id "
        f"WHERE r.ref_id != r.id {adm_sql} "
        f"GROUP BY u.id ORDER BY refs DESC LIMIT {_PH}",
        adm_params + (limit,),
    ).fetchall()
    conn.close()
    return rows


def top_roulette(limit=10):
    conn = connect()
    adm_sql, adm_params = _admin_filter()
    rows = conn.execute(
        f"SELECT id, username, first_name, roulette_wins FROM users "
        f"WHERE roulette_wins > 0 {adm_sql}ORDER BY roulette_wins DESC LIMIT {_PH}",
        adm_params + (limit,),
    ).fetchall()
    conn.close()
    return rows


def add_withdrawal(user_id, amount, details, skin=None, screenshot=None):
    conn = connect()
    with conn:
        if USE_PG:
            cur = conn.execute(
                f"INSERT INTO withdrawals (user_id, amount, details, skin, screenshot, created_at) "
                f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}) RETURNING id",
                (user_id, amount, details, skin, screenshot, datetime.now().isoformat()),
            )
            wd_id = cur.fetchone()["id"]
        else:
            cur = conn.execute(
                f"INSERT INTO withdrawals (user_id, amount, details, skin, screenshot, created_at) "
                f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
                (user_id, amount, details, skin, screenshot, datetime.now().isoformat()),
            )
            wd_id = cur.lastrowid
    conn.close()
    return wd_id


def get_withdrawal(wd_id):
    conn = connect()
    row = conn.execute(
        f"SELECT * FROM withdrawals WHERE id = {_PH}", (wd_id,)
    ).fetchone()
    conn.close()
    return row


def set_withdrawal_status(wd_id, status):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE withdrawals SET status = {_PH} WHERE id = {_PH}",
            (status, wd_id),
        )
    conn.close()


def list_withdrawals(status=None, limit=15):
    conn = connect()
    if status:
        rows = conn.execute(
            f"SELECT * FROM withdrawals WHERE status = {_PH} ORDER BY id DESC LIMIT {_PH}",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM withdrawals ORDER BY id DESC LIMIT {_PH}", (limit,)
        ).fetchall()
    conn.close()
    return rows


def list_user_withdrawals(user_id, limit=15):
    conn = connect()
    rows = conn.execute(
        f"SELECT * FROM withdrawals WHERE user_id = {_PH} ORDER BY id DESC LIMIT {_PH}",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def add_code(code, amount, max_uses=1, owner_id=None):
    conn = connect()
    with conn:
        conn.execute(
            f"INSERT INTO promocodes (code, amount, max_uses, owner_id, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}) ON CONFLICT (code) DO NOTHING",
            (code, amount, max_uses, owner_id, datetime.now().isoformat()),
        )
    conn.close()


def get_code(code):
    conn = connect()
    row = conn.execute(
        f"SELECT * FROM promocodes WHERE code = {_PH}", (code,)
    ).fetchone()
    conn.close()
    return row


def delete_code(code):
    conn = connect()
    with conn:
        conn.execute(f"DELETE FROM promo_uses WHERE code = {_PH}", (code,))
        conn.execute(f"DELETE FROM promocodes WHERE code = {_PH}", (code,))
    conn.close()


def use_code(code, user_id):
    conn = connect()
    try:
        with conn:
            already = conn.execute(
                f"SELECT 1 AS one FROM promo_uses WHERE code = {_PH} AND user_id = {_PH}",
                (code, user_id),
            ).fetchone()
            if already:
                return False
            cur = conn.execute(
                f"UPDATE promocodes SET used = used + 1, used_at = {_PH} "
                f"WHERE code = {_PH} AND (max_uses = 0 OR used < max_uses)",
                (datetime.now().isoformat(), code),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                f"INSERT INTO promo_uses (code, user_id, activated_at) VALUES ({_PH}, {_PH}, {_PH})",
                (code, user_id, datetime.now().isoformat()),
            )
            return True
    finally:
        conn.close()


def list_codes(limit=20):
    conn = connect()
    rows = conn.execute(
        f"SELECT * FROM promocodes ORDER BY created_at DESC, code LIMIT {_PH}", (limit,)
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
    with conn:
        conn.execute(
            f"UPDATE users SET promo_last_created = {_PH} WHERE id = {_PH}",
            (ts, user_id),
        )
    conn.close()


def set_ban(user_id, banned):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE users SET banned = {_PH} WHERE id = {_PH}",
            (bool(banned), user_id),
        )
    conn.close()


def set_mute(user_id, muted):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE users SET muted = {_PH} WHERE id = {_PH}",
            (bool(muted), user_id),
        )
    conn.close()


def add_task(sponsor, reward):
    conn = connect()
    with conn:
        if USE_PG:
            cur = conn.execute(
                f"INSERT INTO tasks (sponsor, reward, created_at) "
                f"VALUES ({_PH}, {_PH}, {_PH}) RETURNING id",
                (sponsor, reward, datetime.now().isoformat()),
            )
            task_id = cur.fetchone()["id"]
        else:
            cur = conn.execute(
                f"INSERT INTO tasks (sponsor, reward, created_at) "
                f"VALUES ({_PH}, {_PH}, {_PH})",
                (sponsor, reward, datetime.now().isoformat()),
            )
            task_id = cur.lastrowid
    conn.close()
    return task_id


def get_task(task_id):
    conn = connect()
    row = conn.execute(
        f"SELECT * FROM tasks WHERE id = {_PH}", (task_id,)
    ).fetchone()
    conn.close()
    return row


def list_tasks(active=True):
    conn = connect()
    if active is None:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE active = {_PH} ORDER BY id DESC",
            (1 if active else 0,),
        ).fetchall()
    conn.close()
    return rows


def deactivate_task(task_id):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE tasks SET active = 0 WHERE id = {_PH}", (task_id,)
        )
    conn.close()


def get_completion(task_id, user_id):
    conn = connect()
    row = conn.execute(
        f"SELECT * FROM task_dones WHERE task_id = {_PH} AND user_id = {_PH}",
        (task_id, user_id),
    ).fetchone()
    conn.close()
    return row


def add_completion(task_id, user_id):
    conn = connect()
    with conn:
        conn.execute(
            f"INSERT INTO task_dones (task_id, user_id, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}) ON CONFLICT (task_id, user_id) DO NOTHING",
            (task_id, user_id, datetime.now().isoformat()),
        )
    conn.close()


def set_completion_rewarded(task_id, user_id, rewarded):
    conn = connect()
    with conn:
        conn.execute(
            f"UPDATE task_dones SET rewarded = {_PH} WHERE task_id = {_PH} AND user_id = {_PH}",
            (1 if rewarded else 0, task_id, user_id),
        )
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


if USE_PG:

    def _retry(func):
        import functools

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except psycopg.OperationalError:
                return func(*args, **kwargs)

        return wrapper

    for _name in (
        "set_setting", "get_setting", "add_user", "get_user",
        "get_user_by_username", "count_referrals", "add_balance",
        "spend_balance", "set_balance", "update_streak", "add_balance_all",
        "subtract_balance_all", "reset_all_balances", "add_roulette_win",
        "top_balance", "top_referrals", "top_roulette", "add_withdrawal",
        "get_withdrawal", "set_withdrawal_status", "list_withdrawals",
        "list_user_withdrawals", "add_code", "get_code", "delete_code",
        "use_code", "list_codes", "get_all_user_ids",
        "set_promo_last_created", "set_ban", "set_mute", "add_task",
        "get_task", "list_tasks", "deactivate_task", "get_completion",
        "add_completion", "set_completion_rewarded",
        "list_completions_rewarded", "stats",
    ):
        globals()[_name] = _retry(globals()[_name])