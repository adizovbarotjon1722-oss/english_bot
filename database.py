"""
database.py — SQLite bilan ishlash uchun barcha funksiyalar.
Repetitorlik Telegram bot uchun ma'lumotlar bazasi qatlami.
"""

import sqlite3
from datetime import date, timedelta
from contextlib import contextmanager

DB_PATH = "repetitor.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Barcha jadvallarni yaratadi (agar mavjud bo'lmasa)."""
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
                last_active_date TEXT
            );

            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,        -- 'en' yoki 'ru'
                section TEXT NOT NULL,        -- 'grammar', 'vocabulary', ...
                level TEXT NOT NULL,          -- 'beginner', 'intermediate', 'advanced'
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
            """
        )


# ---------- Foydalanuvchilar ----------

def get_or_create_user(telegram_id: int, username: str, full_name: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (telegram_id, username, full_name) VALUES (?, ?, ?)",
            (telegram_id, username, full_name),
        )
        return cur.lastrowid


def update_streak(user_id: int) -> int:
    """Foydalanuvchi bugun birinchi marta test ishlasa streakni yangilaydi.
    Ketma-ket kunlarda +1, kun o'tkazib yuborilsa 1 ga tushadi.
    Qaytadigan qiymat — joriy streak."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT streak, last_active_date FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row["last_active_date"] == today:
            return row["streak"]  # bugun allaqachon hisoblangan
        new_streak = row["streak"] + 1 if row["last_active_date"] == yesterday else 1
        conn.execute(
            "UPDATE users SET streak = ?, last_active_date = ? WHERE id = ?",
            (new_streak, today, user_id),
        )
        return new_streak


# ---------- Natijalar va progress ----------

def save_result(user_id: int, subject: str, section: str, level: str, score: int, total: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO results (user_id, subject, section, level, score, total) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, subject, section, level, score, total),
        )
        conn.execute(
            """
            INSERT INTO progress (user_id, subject, section, correct_count, total_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, subject, section)
            DO UPDATE SET
                correct_count = correct_count + excluded.correct_count,
                total_count = total_count + excluded.total_count
            """,
            (user_id, subject, section, score, total),
        )


def get_user_stats(user_id: int):
    with get_conn() as conn:
        user = conn.execute(
            "SELECT streak, full_name FROM users WHERE id = ?", (user_id,)
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
        return {
            "streak": user["streak"] if user else 0,
            "tests_taken": tests["cnt"],
            "total_score": tests["total_score"],
            "total_questions": tests["total_q"],
            "weak_areas": [dict(w) for w in weak],
        }


def get_leaderboard(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.full_name, u.username,
                   COALESCE(SUM(r.score),0) AS total_score,
                   COUNT(r.id) AS tests_taken
            FROM users u
            LEFT JOIN results r ON r.user_id = u.id
            GROUP BY u.id
            ORDER BY total_score DESC, tests_taken DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- Nishonlar (badges) ----------

def award_badge(user_id: int, code: str) -> bool:
    """Nishonni beradi. Agar bu nishon avval berilgan bo'lsa False qaytaradi
    (shunda bot uni qayta e'lon qilib, foydalanuvchini zeriktirmaydi)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO badges (user_id, code) VALUES (?, ?)",
            (user_id, code),
        )
        return cur.rowcount > 0


def has_badge(user_id: int, code: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM badges WHERE user_id = ? AND code = ?", (user_id, code)
        ).fetchone()
        return row is not None


def get_user_badges(user_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM badges WHERE user_id = ? ORDER BY earned_at", (user_id,)
        ).fetchall()
        return [r["code"] for r in rows]
