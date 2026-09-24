"""
database.py — SQLite ma'lumotlar bazasi qatlami.
Repetitorlik Telegram bot: foydalanuvchilar, progress, natijalar, nishonlar.
"""

import sqlite3
from datetime import date, timedelta
from contextlib import contextmanager
from typing import Optional

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
                last_active_date TEXT,
                placement_done INTEGER DEFAULT 0,
                current_level TEXT DEFAULT 'beginner',
                xp INTEGER DEFAULT 0
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
            """
        )


def get_or_create_user(telegram_id: int, username: str | None, full_name: str | None) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET username = ?, full_name = ? WHERE id = ?",
                (username, full_name, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (telegram_id, username, full_name) VALUES (?, ?, ?)",
            (telegram_id, username, full_name),
        )
        return cur.lastrowid


def get_user_by_telegram(telegram_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
        return dict(row) if row else None


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
            DO UPDATE SET status = CASE
                WHEN status = 'locked' THEN 'unlocked'
                ELSE status
            END
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
            DO UPDATE SET
                status = 'completed',
                quiz_score = excluded.quiz_score,
                quiz_total = excluded.quiz_total,
                completed_at = datetime('now')
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


def is_module_completed(user_id: int, subject: str, module_id: str) -> bool:
    return get_module_status(user_id, subject, module_id) == "completed"


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
            "SELECT streak, full_name, xp, current_level, placement_done FROM users WHERE id = ?",
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
            "tests_taken": tests["cnt"],
            "total_score": tests["total_score"],
            "total_questions": tests["total_q"],
            "completed_modules": completed_modules["cnt"] if completed_modules else 0,
            "weak_areas": [dict(w) for w in weak],
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
