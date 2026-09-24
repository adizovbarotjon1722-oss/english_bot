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

            -- Har bir (foydalanuvchi, til, bo'lim) uchun qaysi modulgacha ochilgani.
            -- unlocked_order = foydalanuvchi kira oladigan eng yuqori modul tartib raqami
            -- (0 dan boshlanadi). Undan kichik tartiblar — o'tilgan (✅), katta — qulf (🔒).
            CREATE TABLE IF NOT EXISTS course_progress (
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,
                section TEXT NOT NULL,
                unlocked_order INTEGER DEFAULT 0,
                placement_done INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, subject, section)
            );

            -- O'qituvchi yubordigan video/audio darsliklar (fayl ID'lari) shu yerda saqlanadi.
            CREATE TABLE IF NOT EXISTS module_media (
                subject TEXT NOT NULL,
                section TEXT NOT NULL,
                module_id TEXT NOT NULL,
                video_file_id TEXT,
                audio_file_id TEXT,
                PRIMARY KEY (subject, section, module_id)
            );

            -- Foydalanuvchi videoni "ko'rdim" deb tasdiqlagan modullar.
            CREATE TABLE IF NOT EXISTS video_watched (
                user_id INTEGER NOT NULL REFERENCES users(id),
                subject TEXT NOT NULL,
                section TEXT NOT NULL,
                module_id TEXT NOT NULL,
                PRIMARY KEY (user_id, subject, section, module_id)
            );

            -- Har bir savol foydalanuvchi uchun necha marta to'g'ri/noto'g'ri javoblanganini
            -- kuzatadi — shu orqali (a) mashqlar takrorlanib qolmaydi, (b) xato qilingan
            -- savollarni alohida qayta mashq qilish (/review) imkoni bo'ladi.
            CREATE TABLE IF NOT EXISTS seen_questions (
                user_id INTEGER NOT NULL REFERENCES users(id),
                question_id TEXT NOT NULL,
                correct_count INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                last_seen TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, question_id)
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
    Ketma-ket kunlarda +1. 2-3 kun tanaffusda "yumshoq qo'nish" — streak butunlay
    0 ga emas, yarmiga tushadi (Duolingo'dagi "streak freeze" tamoyiliga o'xshash,
    foydalanuvchini butunlay yo'qotib qo'ymaslik uchun). 3 kundan ortiq tanaffusda
    streak 1 dan boshlanadi. Qaytadigan qiymat — joriy streak."""
    today = date.today()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT streak, last_active_date FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        last_str = row["last_active_date"]
        if last_str == today.isoformat():
            return row["streak"]  # bugun allaqachon hisoblangan

        if last_str:
            gap = (today - date.fromisoformat(last_str)).days
        else:
            gap = None

        if gap == 1:
            new_streak = row["streak"] + 1
        elif gap in (2, 3):
            new_streak = max(1, row["streak"] // 2)  # yumshoq qo'nish
        else:
            new_streak = 1

        conn.execute(
            "UPDATE users SET streak = ?, last_active_date = ? WHERE id = ?",
            (new_streak, today.isoformat(), user_id),
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


def get_weekly_leaderboard(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.full_name, u.username,
                   COALESCE(SUM(r.score),0) AS total_score,
                   COUNT(r.id) AS tests_taken
            FROM users u
            JOIN results r ON r.user_id = u.id
            WHERE r.created_at >= datetime('now', '-7 days')
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


# ---------- Modul progressi (skill-tree uslubidagi ketma-ket ochilish) ----------

def get_course_progress(user_id: int, subject: str, section: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT unlocked_order, placement_done FROM course_progress "
            "WHERE user_id=? AND subject=? AND section=?",
            (user_id, subject, section),
        ).fetchone()
        return dict(row) if row else None


def ensure_course_progress(user_id: int, subject: str, section: str, start_order: int = 0):
    """Progress qatori mavjud bo'lmasa yaratadi (masalan birinchi marta kirganda
    yoki joylashtirish testisiz to'g'ridan-to'g'ri 1-moduldan boshlaganda)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO course_progress (user_id, subject, section, unlocked_order) "
            "VALUES (?, ?, ?, ?)",
            (user_id, subject, section, start_order),
        )


def set_placement_done(user_id: int, subject: str, section: str, unlocked_order: int):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO course_progress (user_id, subject, section, unlocked_order, placement_done)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_id, subject, section)
            DO UPDATE SET unlocked_order = excluded.unlocked_order, placement_done = 1
            """,
            (user_id, subject, section, unlocked_order),
        )


def advance_module(user_id: int, subject: str, section: str, new_order: int):
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE course_progress SET unlocked_order = ?
            WHERE user_id=? AND subject=? AND section=? AND unlocked_order < ?
            """,
            (new_order, user_id, subject, section, new_order),
        )


# ---------- Video/audio darsliklar (o'qituvchi tomonidan yuklanadi) ----------

def set_module_media(subject: str, section: str, module_id: str, *, video_file_id=None, audio_file_id=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO module_media (subject, section, module_id, video_file_id, audio_file_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(subject, section, module_id) DO UPDATE SET "
            + ("video_file_id = excluded.video_file_id" if video_file_id is not None else "audio_file_id = excluded.audio_file_id"),
            (subject, section, module_id, video_file_id, audio_file_id),
        )


def get_module_media(subject: str, section: str, module_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT video_file_id, audio_file_id FROM module_media "
            "WHERE subject=? AND section=? AND module_id=?",
            (subject, section, module_id),
        ).fetchone()
        return dict(row) if row else {"video_file_id": None, "audio_file_id": None}


def mark_video_watched(user_id: int, subject: str, section: str, module_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO video_watched (user_id, subject, section, module_id) "
            "VALUES (?, ?, ?, ?)",
            (user_id, subject, section, module_id),
        )


def is_video_watched(user_id: int, subject: str, section: str, module_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM video_watched WHERE user_id=? AND subject=? AND section=? AND module_id=?",
            (user_id, subject, section, module_id),
        ).fetchone()
        return row is not None


# ---------- Ko'rilgan savollar (takrorlanmaslik + xatolarni qayta mashq qilish) ----------

def record_seen_question(user_id: int, question_id: str, correct: bool):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO seen_questions (user_id, question_id, correct_count, wrong_count, last_seen)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(user_id, question_id) DO UPDATE SET
                correct_count = correct_count + excluded.correct_count,
                wrong_count = wrong_count + excluded.wrong_count,
                last_seen = datetime('now')
            """,
            (user_id, question_id, 1 if correct else 0, 0 if correct else 1),
        )


def get_seen_question_ids(user_id: int) -> set:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT question_id FROM seen_questions WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {r["question_id"] for r in rows}


def get_mistaken_question_ids(user_id: int, limit: int = 30):
    """Foydalanuvchi to'g'ridan ko'ra ko'proq xato qilgan savollar — /review uchun,
    eng ko'p xato qilinganlar birinchi o'rinda."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT question_id FROM seen_questions
            WHERE user_id = ? AND wrong_count > correct_count
            ORDER BY wrong_count DESC, last_seen ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [r["question_id"] for r in rows]
