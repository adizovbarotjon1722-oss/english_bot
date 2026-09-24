"""
database.py — SQLite qatlami: users, progress, referral, premium, certificates.
"""

import sqlite3
import secrets
from datetime import date, timedelta
from contextlib import contextmanager
from typing import Optional

DB_PATH = "repetitor.db"


@contextmanager
def get_conn():
    # timeout=30 — bir vaqtda ko'p yozishda "database is locked" kamayadi
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")       # parallel o'qish/yozish
    conn.execute("PRAGMA synchronous = NORMAL")     # tezlik + yetarli xavfsizlik
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA temp_store = MEMORY")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                full_name TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                streak INTEGER DEFAULT 0,
                last_active_date TEXT,
                placement_done INTEGER DEFAULT 0,
                current_level TEXT DEFAULT 'beginner',
                xp INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                premium_until TEXT,
                referral_code TEXT UNIQUE,
                referred_by INTEGER REFERENCES users(id),
                referral_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,
                section TEXT NOT NULL,
                level TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS progress (
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,
                section TEXT NOT NULL,
                correct_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, subject, section)
            );

            CREATE TABLE IF NOT EXISTS badges (
                user_id INTEGER NOT NULL REFERENCES users(id),
                code TEXT NOT NULL,
                earned_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, code)
            );

            CREATE TABLE IF NOT EXISTS module_progress (
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,
                module_id TEXT NOT NULL,
                status TEXT DEFAULT 'locked',
                video_watched INTEGER DEFAULT 0,
                quiz_score INTEGER DEFAULT 0,
                quiz_total INTEGER DEFAULT 0,
                completed_at TEXT,
                PRIMARY KEY (user_id, subject, module_id)
            );

            CREATE TABLE IF NOT EXISTS seen_exercises (
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,
                exercise_id TEXT NOT NULL,
                seen_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, subject, exercise_id)
            );

            CREATE TABLE IF NOT EXISTS daily_activity (
                user_id INTEGER NOT NULL REFERENCES users(id),
                activity_date TEXT NOT NULL,
                xp_earned INTEGER DEFAULT 0,
                lessons_done INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, activity_date)
            );

            CREATE TABLE IF NOT EXISTS certificates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,
                level TEXT NOT NULL,
                issued_at TEXT DEFAULT (datetime('now')),
                file_path TEXT
            );

            """
        )
        # Migratsiya: eski bazaga yangi ustunlar (CREATE TABLE IF NOT EXISTS ularni qo'shmaydi)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, typ in [
            ("placement_done", "INTEGER DEFAULT 0"),
            ("current_level", "TEXT DEFAULT 'beginner'"),
            ("xp", "INTEGER DEFAULT 0"),
            ("is_premium", "INTEGER DEFAULT 0"),
            ("premium_until", "TEXT"),
            ("referral_code", "TEXT"),
            ("referred_by", "INTEGER"),
            ("referral_count", "INTEGER DEFAULT 0"),
        ]:
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass

        # Indekslar — faqat ustunlar mavjud bo'lgach (eski DB crash qilmasin)
        for stmt in [
            "CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id)",
            "CREATE INDEX IF NOT EXISTS idx_users_referral ON users(referral_code)",
            "CREATE INDEX IF NOT EXISTS idx_results_user ON results(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_results_created ON results(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_module_progress_user ON module_progress(user_id, subject)",
            "CREATE INDEX IF NOT EXISTS idx_seen_user ON seen_exercises(user_id, subject)",
            "CREATE INDEX IF NOT EXISTS idx_badges_user ON badges(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_daily_user ON daily_activity(user_id, activity_date)",
        ]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass


def _gen_ref_code() -> str:
    return secrets.token_hex(3).upper()  # 6 belgi


def get_or_create_user(
    telegram_id: int,
    username: str | None,
    full_name: str | None,
    referral_code_used: str | None = None,
) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, referral_code FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET username = ?, full_name = ? WHERE id = ?",
                (username, full_name, row["id"]),
            )
            if not row["referral_code"]:
                code = _gen_ref_code()
                while conn.execute(
                    "SELECT 1 FROM users WHERE referral_code = ?", (code,)
                ).fetchone():
                    code = _gen_ref_code()
                conn.execute(
                    "UPDATE users SET referral_code = ? WHERE id = ?", (code, row["id"])
                )
            return row["id"]

        code = _gen_ref_code()
        while conn.execute(
            "SELECT 1 FROM users WHERE referral_code = ?", (code,)
        ).fetchone():
            code = _gen_ref_code()

        referred_by = None
        if referral_code_used:
            ref = conn.execute(
                "SELECT id FROM users WHERE referral_code = ?",
                (referral_code_used.upper(),),
            ).fetchone()
            if ref:
                referred_by = ref["id"]

        cur = conn.execute(
            """
            INSERT INTO users (telegram_id, username, full_name, referral_code, referred_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_id, username, full_name, code, referred_by),
        )
        new_id = cur.lastrowid
        if referred_by:
            conn.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE id = ?",
                (referred_by,),
            )
        return new_id


def apply_referral_bonus(referrer_id: int, new_user_id: int) -> bool:
    """Referrer va yangi user uchun bonus nishon / XP."""
    with get_conn() as conn:
        # Faqat bir marta
        if conn.execute(
            "SELECT 1 FROM badges WHERE user_id = ? AND code = ?",
            (referrer_id, f"ref_{new_user_id}"),
        ).fetchone():
            return False
        conn.execute(
            "INSERT OR IGNORE INTO badges (user_id, code) VALUES (?, ?)",
            (referrer_id, "referral_friend"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO badges (user_id, code) VALUES (?, ?)",
            (new_user_id, "joined_via_referral"),
        )
        conn.execute("UPDATE users SET xp = xp + 50 WHERE id = ?", (referrer_id,))
        conn.execute("UPDATE users SET xp = xp + 30 WHERE id = ?", (new_user_id,))
        return True


def get_user_by_telegram(telegram_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


def get_referral_code(user_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT referral_code FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row and row["referral_code"]:
            return row["referral_code"]
        code = _gen_ref_code()
        conn.execute(
            "UPDATE users SET referral_code = ? WHERE id = ?", (code, user_id)
        )
        return code


def is_premium(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_premium, premium_until FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row or not row["is_premium"]:
            return False
        if row["premium_until"]:
            try:
                from datetime import datetime
                until = datetime.fromisoformat(row["premium_until"])
                if until.date() < date.today():
                    conn.execute(
                        "UPDATE users SET is_premium = 0 WHERE id = ?", (user_id,)
                    )
                    return False
            except ValueError:
                pass
        return True


def set_premium(user_id: int, days: int = 30):
    until = (date.today() + timedelta(days=days)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET is_premium = 1, premium_until = ? WHERE id = ?",
            (until, user_id),
        )


def set_placement_done(user_id: int, level: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET placement_done = 1, current_level = ? WHERE id = ?",
            (level, user_id),
        )


def add_xp(user_id: int, amount: int):
    today = date.today().isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (amount, user_id))
        conn.execute(
            """
            INSERT INTO daily_activity (user_id, activity_date, xp_earned, lessons_done)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(user_id, activity_date)
            DO UPDATE SET xp_earned = xp_earned + excluded.xp_earned
            """,
            (user_id, today, amount),
        )


def increment_lessons_done(user_id: int):
    today = date.today().isoformat()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO daily_activity (user_id, activity_date, xp_earned, lessons_done)
            VALUES (?, ?, 0, 1)
            ON CONFLICT(user_id, activity_date)
            DO UPDATE SET lessons_done = lessons_done + 1
            """,
            (user_id, today),
        )


def update_streak(user_id: int) -> int:
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT streak, last_active_date FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return 0
        if row["last_active_date"] == today:
            return row["streak"]
        new_streak = row["streak"] + 1 if row["last_active_date"] == yesterday else 1
        conn.execute(
            "UPDATE users SET streak = ?, last_active_date = ? WHERE id = ?",
            (new_streak, today, user_id),
        )
        return new_streak


def get_module_status(user_id: int, subject: str, module_id: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM module_progress WHERE user_id = ? AND subject = ? AND module_id = ?",
            (user_id, subject, module_id),
        ).fetchone()
        return row["status"] if row else "locked"


def unlock_module(user_id: int, subject: str, module_id: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO module_progress (user_id, subject, module_id, status)
            VALUES (?, ?, ?, 'unlocked')
            ON CONFLICT(user_id, subject, module_id)
            DO UPDATE SET status = CASE WHEN status = 'locked' THEN 'unlocked' ELSE status END
            """,
            (user_id, subject, module_id),
        )


def mark_video_watched(user_id: int, subject: str, module_id: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO module_progress (user_id, subject, module_id, status, video_watched)
            VALUES (?, ?, ?, 'video_done', 1)
            ON CONFLICT(user_id, subject, module_id)
            DO UPDATE SET video_watched = 1,
                status = CASE WHEN status IN ('locked','unlocked') THEN 'video_done' ELSE status END
            """,
            (user_id, subject, module_id),
        )


def complete_module(user_id: int, subject: str, module_id: str, quiz_score: int, quiz_total: int):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO module_progress
                (user_id, subject, module_id, status, video_watched, quiz_score, quiz_total, completed_at)
            VALUES (?, ?, ?, 'completed', 1, ?, ?, datetime('now'))
            ON CONFLICT(user_id, subject, module_id)
            DO UPDATE SET status = 'completed', quiz_score = excluded.quiz_score,
                quiz_total = excluded.quiz_total, completed_at = datetime('now')
            """,
            (user_id, subject, module_id, quiz_score, quiz_total),
        )


def get_all_module_progress(user_id: int, subject: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT module_id, status, video_watched, quiz_score, quiz_total FROM module_progress "
            "WHERE user_id = ? AND subject = ?",
            (user_id, subject),
        ).fetchall()
        return {
            r["module_id"]: {
                "status": r["status"],
                "video_watched": bool(r["video_watched"]),
                "quiz_score": r["quiz_score"],
                "quiz_total": r["quiz_total"],
            }
            for r in rows
        }


def count_completed_modules(user_id: int, subject: str, level: str = None) -> int:
    with get_conn() as conn:
        if level:
            # level filter qilinmaydi — bot tomonida
            pass
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM module_progress WHERE user_id = ? AND subject = ? AND status = 'completed'",
            (user_id, subject),
        ).fetchone()
        return row["cnt"] if row else 0


def mark_exercise_seen(user_id: int, subject: str, exercise_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_exercises (user_id, subject, exercise_id) VALUES (?, ?, ?)",
            (user_id, subject, exercise_id),
        )


def get_seen_exercises(user_id: int, subject: str) -> set:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT exercise_id FROM seen_exercises WHERE user_id = ? AND subject = ?",
            (user_id, subject),
        ).fetchall()
        return {r["exercise_id"] for r in rows}


def clear_seen_for_module(user_id: int, subject: str, exercise_ids: list):
    if not exercise_ids:
        return
    with get_conn() as conn:
        placeholders = ",".join("?" * len(exercise_ids))
        conn.execute(
            f"DELETE FROM seen_exercises WHERE user_id = ? AND subject = ? AND exercise_id IN ({placeholders})",
            [user_id, subject] + list(exercise_ids),
        )


def save_result(user_id: int, subject: str, section: str, level: str, score: int, total: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO results (user_id, subject, section, level, score, total) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, subject, section, level, score, total),
        )
        conn.execute(
            """
            INSERT INTO progress (user_id, subject, section, correct_count, total_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, subject, section)
            DO UPDATE SET correct_count = correct_count + excluded.correct_count,
                total_count = total_count + excluded.total_count
            """,
            (user_id, subject, section, score, total),
        )


def get_user_stats(user_id: int):
    with get_conn() as conn:
        user = conn.execute(
            "SELECT streak, full_name, xp, current_level, placement_done, is_premium, "
            "referral_code, referral_count FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        tests = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(score),0) AS total_score, "
            "COALESCE(SUM(total),0) AS total_q FROM results WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        weak = conn.execute(
            """
            SELECT subject, section,
                   ROUND(100.0 * correct_count / NULLIF(total_count,0), 1) AS pct
            FROM progress WHERE user_id = ?
            ORDER BY pct ASC LIMIT 3
            """,
            (user_id,),
        ).fetchall()
        completed_modules = conn.execute(
            "SELECT COUNT(*) AS cnt FROM module_progress WHERE user_id = ? AND status = 'completed'",
            (user_id,),
        ).fetchone()
        return {
            "streak": user["streak"] if user else 0,
            "xp": user["xp"] if user else 0,
            "current_level": user["current_level"] if user else "beginner",
            "placement_done": bool(user["placement_done"]) if user else False,
            "is_premium": bool(user["is_premium"]) if user else False,
            "referral_code": user["referral_code"] if user else "",
            "referral_count": user["referral_count"] if user else 0,
            "tests_taken": tests["cnt"],
            "total_score": tests["total_score"],
            "total_questions": tests["total_q"],
            "completed_modules": completed_modules["cnt"] if completed_modules else 0,
            "weak_areas": [dict(w) for w in weak],
            "full_name": user["full_name"] if user else "",
        }


def get_leaderboard(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.full_name, u.username, u.xp,
                   COALESCE(SUM(r.score),0) AS total_score,
                   COUNT(r.id) AS tests_taken
            FROM users u
            LEFT JOIN results r ON r.user_id = u.id
            GROUP BY u.id
            ORDER BY u.xp DESC, total_score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def award_badge(user_id: int, code: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO badges (user_id, code) VALUES (?, ?)",
            (user_id, code),
        )
        return cur.rowcount > 0


def get_user_badges(user_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM badges WHERE user_id = ? ORDER BY earned_at", (user_id,)
        ).fetchall()
        return [r["code"] for r in rows]


def save_certificate(user_id: int, subject: str, level: str, file_path: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO certificates (user_id, subject, level, file_path) VALUES (?, ?, ?, ?)",
            (user_id, subject, level, file_path),
        )


def has_certificate(user_id: int, subject: str, level: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM certificates WHERE user_id = ? AND subject = ? AND level = ?",
            (user_id, subject, level),
        ).fetchone()
        return row is not None


# ---------- Admin helpers ----------

def count_users() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def list_users(limit: int = 20, offset: int = 0) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, telegram_id, username, full_name, xp, streak, current_level,
                   is_premium, placement_done, referral_count, created_at
            FROM users
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_detail(user_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        u = dict(row)
        mods = conn.execute(
            "SELECT subject, module_id, status, quiz_score, quiz_total, completed_at "
            "FROM module_progress WHERE user_id = ? ORDER BY subject, module_id",
            (user_id,),
        ).fetchall()
        u["modules"] = [dict(m) for m in mods]
        recent = conn.execute(
            "SELECT subject, section, level, score, total, created_at FROM results "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        u["recent_results"] = [dict(r) for r in recent]
        return u


def find_user_by_telegram(telegram_id: int) -> Optional[dict]:
    return get_user_by_telegram(telegram_id)


def find_user_by_username(username: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
            (username.lstrip("@"),),
        ).fetchone()
        return dict(row) if row else None


def admin_stats_overview() -> dict:
    with get_conn() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        premium = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE is_premium = 1"
        ).fetchone()["c"]
        tests = conn.execute("SELECT COUNT(*) AS c FROM results").fetchone()["c"]
        completed_mods = conn.execute(
            "SELECT COUNT(*) AS c FROM module_progress WHERE status = 'completed'"
        ).fetchone()["c"]
        today = date.today().isoformat()
        active_today = conn.execute(
            "SELECT COUNT(*) AS c FROM users WHERE last_active_date = ?", (today,)
        ).fetchone()["c"]
        return {
            "users": users,
            "premium": premium,
            "tests": tests,
            "completed_modules": completed_mods,
            "active_today": active_today,
        }


def reset_user_module(user_id: int, subject: str, module_id: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM module_progress WHERE user_id = ? AND subject = ? AND module_id = ?",
            (user_id, subject, module_id),
        )


def force_complete_module(user_id: int, subject: str, module_id: str):
    complete_module(user_id, subject, module_id, 100, 100)


def force_unlock_module(user_id: int, subject: str, module_id: str):
    unlock_module(user_id, subject, module_id)


def set_user_level(user_id: int, level: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET current_level = ?, placement_done = 1 WHERE id = ?",
            (level, user_id),
        )


def ban_note_placeholder():
    pass  # kelajakda ban jadvali
