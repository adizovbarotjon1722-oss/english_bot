"""English Path A1 Telegram bot and teacher API.

The course is authored in course.json. Keep secrets in environment variables.
"""
from __future__ import annotations

import asyncio
import html
import hmac
import json
import logging
import math
import os
import random
import re
import secrets
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from openai import AsyncOpenAI
from redis.asyncio import Redis
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, cast, delete, func, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
import uvicorn

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("english_path")
BASE = Path(__file__).resolve().parent
# Uzbekistan uses UTC+05:00 year-round. A fixed offset works on Windows even
# when the optional IANA timezone database (tzdata) is not installed.
TASHKENT = timezone(timedelta(hours=5), "Asia/Tashkent")
DATABASE_READY = asyncio.Event()
COURSE = json.loads((BASE / "course.json").read_text(encoding="utf-8"))
LESSON_LINKS = json.loads((BASE / "lesson_links.json").read_text(encoding="utf-8"))
LESSON_GUIDES = json.loads((BASE / "lesson_guides.json").read_text(encoding="utf-8"))
LEARNING_PATHWAYS = json.loads((BASE / "learning_pathways.json").read_text(encoding="utf-8"))
CAMPAIGNS = json.loads((BASE / "campaigns.json").read_text(encoding="utf-8"))
QUESTION_BANKS = json.loads((BASE / "question_banks.json").read_text(encoding="utf-8"))
ACTIVITY_BANKS = json.loads((BASE / "activities.json").read_text(encoding="utf-8"))
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
EXPECTED_BOT_USERNAME = os.getenv("EXPECTED_BOT_USERNAME", "").strip().lstrip("@").lower()
DB_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./english_path.db")
REDIS_URL = os.getenv("REDIS_URL", "memory://")
engine = create_async_engine(DB_URL, pool_size=15, max_overflow=25, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)


class MemoryKV:
    """Small, process-local state store for a one-machine development run."""
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float | None]] = {}

    def _current(self, key: str) -> str | None:
        item = self.values.get(key)
        if not item:
            return None
        value, expires = item
        if expires is not None and expires <= time.monotonic():
            self.values.pop(key, None)
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._current(key)

    async def setex(self, key: str, seconds: int, value: str) -> None:
        self.values[key] = (str(value), time.monotonic() + seconds)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool | None:
        if nx and self._current(key) is not None:
            return None
        expires = time.monotonic() + ex if ex is not None else None
        self.values[key] = (str(value), expires)
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def aclose(self) -> None:
        self.values.clear()


redis = Redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL.startswith("redis://") or REDIS_URL.startswith("rediss://") else MemoryKV()
bot: Bot | None = None
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY")) if os.getenv("OPENAI_API_KEY") else None


class Base(DeclarativeBase):
    pass


class Learner(Base):
    __tablename__ = "learners"
    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    username: Mapped[str] = mapped_column(String(80), default="")
    authorized: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    path_goal: Mapped[str] = mapped_column(String(16), default="CEFR", server_default="CEFR")
    ielts_module: Mapped[str] = mapped_column(String(16), default="", server_default="")
    target_band: Mapped[str] = mapped_column(String(8), default="", server_default="")
    daily_goal_lessons: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    reminder_hour: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Completion(Base):
    __tablename__ = "lesson_completions"
    __table_args__ = (UniqueConstraint("telegram_id", "lesson_key", name="uq_learner_lesson"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("learners.telegram_id", ondelete="CASCADE"), index=True)
    lesson_key: Mapped[str] = mapped_column(String(12), index=True)
    score: Mapped[int] = mapped_column(Integer)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuizRun(Base):
    __tablename__ = "quiz_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("learners.telegram_id", ondelete="CASCADE"), index=True)
    lesson_key: Mapped[str] = mapped_column(String(12), index=True)
    score: Mapped[int] = mapped_column(Integer)
    max_score: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))
    passed: Mapped[bool] = mapped_column(Boolean, index=True)
    question_ids: Mapped[str] = mapped_column(String(1000), default="", server_default=text("''"))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ActivitySeen(Base):
    __tablename__ = "activity_seen"
    __table_args__ = (UniqueConstraint("telegram_id", "lesson_key", "activity_id", name="uq_learner_lesson_activity"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("learners.telegram_id", ondelete="CASCADE"), index=True)
    lesson_key: Mapped[str] = mapped_column(String(12), index=True)
    activity_id: Mapped[str] = mapped_column(String(8))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuizQuestionSeen(Base):
    __tablename__ = "quiz_question_seen"
    __table_args__ = (UniqueConstraint("telegram_id", "lesson_key", "question_id", name="uq_learner_lesson_question"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("learners.telegram_id", ondelete="CASCADE"), index=True)
    lesson_key: Mapped[str] = mapped_column(String(12), index=True)
    question_id: Mapped[str] = mapped_column(String(12))
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Video(Base):
    __tablename__ = "lesson_videos"
    lesson_key: Mapped[str] = mapped_column(String(12), primary_key=True)
    locator: Mapped[str] = mapped_column(String(500))  # Telegram file_id or HTTPS URL
    updated_by: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QuestionLog(Base):
    __tablename__ = "learner_questions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    question: Mapped[str] = mapped_column(String(1500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class HomeworkSubmission(Base):
    __tablename__ = "homework_submissions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(ForeignKey("learners.telegram_id", ondelete="CASCADE"), index=True)
    lesson_key: Mapped[str] = mapped_column(String(12), index=True)
    answer: Mapped[str] = mapped_column(String(4000), default="")
    file_id: Mapped[str] = mapped_column(String(500), default="")
    teacher_note: Mapped[str] = mapped_column(String(1000), default="")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InviteCode(Base):
    __tablename__ = "invite_codes"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    uses_left: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_by: Mapped[int] = mapped_column(Integer)


def lesson_index(key: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d+)\.(\d+)", key)
    if not match:
        return None
    unit, lesson = map(int, match.groups())
    if unit < 1 or unit > len(COURSE["units"]):
        return None
    if lesson < 1 or lesson > len(COURSE["units"][unit - 1]["lessons"]):
        return None
    return unit, lesson


def key_for(unit: int, lesson: int) -> str:
    return f"{unit}.{lesson}"


def lesson_at(unit: int, lesson: int) -> dict[str, Any]:
    return COURSE["units"][unit - 1]["lessons"][lesson - 1]


def lesson_order() -> list[str]:
    return [key_for(u["id"], l["id"]) for u in COURSE["units"] for l in u["lessons"]]


def next_lesson(done: set[str]) -> str | None:
    return next((key for key in lesson_order() if key not in done), None)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def learner_ok(user_id: int, user: Any = None) -> bool:
    async with Session() as db:
        row = await db.get(Learner, user_id)
        if not row or not row.authorized:
            return False
        row.last_seen = datetime.now(timezone.utc)
        if user:
            row.name = (user.full_name or "")[:160]
            row.username = (user.username or "")[:80]
        await db.commit()
        return True


async def completed_set(user_id: int) -> set[str]:
    async with Session() as db:
        rows = await db.scalars(select(Completion.lesson_key).where(Completion.telegram_id == user_id))
        return set(rows.all())


def local_date(moment: datetime) -> Any:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(TASHKENT).date()


async def learner_day_stats(user_id: int) -> tuple[int, int]:
    async with Session() as db:
        moments = (await db.scalars(select(Completion.completed_at).where(
            Completion.telegram_id == user_id).order_by(Completion.completed_at.desc()).limit(500))).all()
    today = datetime.now(TASHKENT).date()
    days = {local_date(moment) for moment in moments if moment}
    completed_today = sum(local_date(moment) == today for moment in moments if moment)
    current = today if today in days else today - timedelta(days=1)
    if current not in days:
        return completed_today, 0
    streak = 0
    while current in days:
        streak += 1
        current -= timedelta(days=1)
    return completed_today, streak


async def learner_streak(user_id: int) -> int:
    return (await learner_day_stats(user_id))[1]


def home_keyboard(admin: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="▶️ Davom etish", callback_data="next")],
            [InlineKeyboardButton(text="📚 Lug‘atlar", callback_data="vocab")],
            [InlineKeyboardButton(text="📈 Mening natijam", callback_data="myprogress")],
            [InlineKeyboardButton(text="🧭 CEFR / IELTS maqsadim", callback_data="pathprofile")],
            [InlineKeyboardButton(text="🎯 Kunlik reja va eslatma", callback_data="dailysettings")],
            [InlineKeyboardButton(text="🙋 Savol berish", callback_data="askhelp")]]
    if admin:
        rows.append([InlineKeyboardButton(text="🛠 O‘qituvchi paneli", callback_data="adminpanel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_home(message: Message) -> None:
    if not await learner_ok(message.from_user.id, message.from_user):
        await message.answer(f"🔒 Kurs yopiq. O‘qituvchingizdan taklif kodi oling va /join KOD yuboring.\n\nAgar kod berilmagan bo‘lsa, o‘qituvchiga Telegram ID’ingizni yuboring: <code>{message.from_user.id}</code>\nO‘qituvchi sizni /grant ID bilan qo‘shadi.", parse_mode="HTML")
        return
    done = await completed_set(message.from_user.id)
    opened = next_lesson(done)
    completed_today, streak = await learner_day_stats(message.from_user.id)
    async with Session() as db:
        learner = await db.get(Learner, message.from_user.id)
        goal = learner.path_goal if learner else "CEFR"
        daily_goal = learner.daily_goal_lessons if learner else 1
    route = "CEFR bo‘yicha umumiy ingliz tili" if goal == "CEFR" else "IELTS maqsadi · avval CEFR til poydevori"
    intro = ("Assalomu alaykum! English Path A1 kursiga xush kelibsiz.\n\n"
             f"🎯 Siz tanlagan yo‘nalish: {route}. Hozir botda A1 bosqichi mavjud.\n"
             f"🔥 Seriya: {streak} kun · Bugungi maqsad: {min(completed_today, daily_goal)}/{daily_goal} dars\n"
             "Darslar navbat bilan ochiladi; bo‘limni tugatish uchun test bilan birga amaliy vazifa ham bajariladi. "
             "CEFR / IELTS tugmasida uzoq muddatli maqsadingizni tanlang.")
    if opened:
        intro += f"\n\nHozir ochiq dars: {opened}"
    else:
        intro += "\n\n🎉 A1 bosqichini tugatdingiz! Bu A1 bosqichi yakunlanganini bildiradi; CEFR sertifikati yoki IELTS bandini anglatmaydi. Keyingi kurs o‘qituvchi tomonidan tayyorlanib tasdiqlangach ochiladi."
    await message.answer(intro, reply_markup=home_keyboard(is_admin(message.from_user.id)))


@dp.message(Command("start"))
async def start(message: Message) -> None:
    await send_home(message)


@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    if await learner_ok(message.from_user.id, message.from_user):
        await message.answer("Darsni davom ettirish uchun /start ni yuboring. O‘qituvchiga savol uchun /ask savolingiz.")
    else:
        await message.answer("Botdan foydalanish uchun o‘qituvchidan kirish ruxsatini oling.")


@dp.message(Command("id"))
async def show_id(message: Message) -> None:
    await message.answer(f"Sizning Telegram ID: <code>{message.from_user.id}</code>\nBu raqamni o‘qituvchiga yuboring.", parse_mode="HTML")


@dp.message(Command("join"))
async def join_course(message: Message, command: CommandObject) -> None:
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer("O‘qituvchi bergan kodni yuboring: /join KOD"); return
    now = datetime.now(timezone.utc)
    async with Session() as db:
        result = await db.execute(update(InviteCode).where(
            InviteCode.code == code, InviteCode.uses_left > 0, InviteCode.expires_at > now
        ).values(uses_left=InviteCode.uses_left - 1))
        if result.rowcount != 1:
            await db.rollback()
            await message.answer("Bu taklif kodi yaroqsiz, muddati tugagan yoki ishlatib bo‘lingan. O‘qituvchingizdan yangi kod oling."); return
        learner = await db.get(Learner, message.from_user.id)
        if learner:
            learner.authorized = True
            learner.name = (message.from_user.full_name or "")[:160]
            learner.username = (message.from_user.username or "")[:80]
        else:
            db.add(Learner(telegram_id=message.from_user.id, name=(message.from_user.full_name or "")[:160],
                           username=(message.from_user.username or "")[:80], authorized=True))
        await db.commit()
    await message.answer("🎓 Taklif tasdiqlandi! Kursni boshlash uchun /start yuboring.")
    if bot:
        for admin_id in ADMIN_IDS:
            try: await bot.send_message(admin_id, f"🟢 Yangi o‘quvchi qo‘shildi: {message.from_user.full_name} · ID {message.from_user.id}")
            except Exception: log.warning("Could not notify teacher about new learner")


async def open_current(user_id: int, target: Message | CallbackQuery) -> None:
    done = await completed_set(user_id)
    key = next_lesson(done)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if not key:
        await msg.answer("🏆 Siz A1 kursining barcha darslarini tugatdingiz. O‘qituvchi keyingi darajani ochadi.")
        return
    u, n = lesson_index(key)  # type: ignore[misc]
    unit = COURSE["units"][u - 1]
    lesson = lesson_at(u, n)
    linked = LESSON_LINKS["lessons"][key]
    homework_text = re.sub(r"^Darslik\s+[^.]*betdagi\s*", "", linked["homework"], flags=re.IGNORECASE)
    body = (f"{unit['icon']} <b>Bo‘lim {u}: {unit['title']}</b>\n"
            f"<b>Dars {n}/4 · {lesson['title']}</b>\n\n"
            f"🎯 <b>Dars maqsadi</b>\n{lesson['goal']}\n\n"
            f"📚 Darslik: {linked['coursebook_pages']}-bet · Mashqlar daftari: {linked['workbook_pages']}-bet\n"
            "Rasmlarni yuqoridan ochib, shu sahifalardagi misollarni ko‘ring. Keyin quyidagi bosqichlardan o‘ting:\n"
            "1️⃣ Qisqa qo‘llanmani o‘qing\n2️⃣ So‘z kartochkalarini eslab ko‘ring\n3️⃣ O‘yin-mashq bajaring\n4️⃣ Mini-testni yeching\n\n"
            f"🏠 <b>Uyga vazifa</b>\nDarslik {linked['coursebook_pages']}-bet: {homework_text}\n"
            f"Mashqlar daftari {linked['workbook_pages']}-bet: shu mavzuga tegishli mashqlarni bajaring.")
    if n == 1:
        campaign = CAMPAIGNS[str(u)]
        body += f"\n\n🗺️ <b>Yangi sarguzasht: {campaign['title']}</b>\n{campaign['intro']}"
    episode = CAMPAIGNS[str(u)]["episodes"][n - 1]
    body += f"\n\n🎯 <b>Missiya {n}/4</b>\n{episode}"
    if n == 4:
        bonus = LESSON_LINKS["unit_bonus"][str(u)]
        body += f"\n\n🎬 <b>Bo‘lim bonus videosi</b>\n{bonus['video']}\n{bonus['mission']}"
    if bot:
        page_media: list[InputMediaPhoto] = []
        for kind, pages, label in (
            ("coursebook", linked["coursebook_pages"], "📘 Darslik"),
            ("workbook", linked["workbook_pages"], "📙 Mashqlar daftari"),
        ):
            for page in re.findall(r"\d+", str(pages)):
                image_path = BASE / "assets" / "lesson_pages" / f"{key.replace('.', '_')}_{kind}_{page}.jpg"
                if image_path.exists():
                    page_media.append(InputMediaPhoto(media=FSInputFile(image_path), caption=f"{label} · {page}-bet"))
        if page_media:
            try:
                await bot.send_media_group(msg.chat.id, media=page_media)
            except Exception:
                log.exception("Could not deliver reference pages for lesson %s", key)
                await msg.answer("📚 Kitob sahifalari rasmini yuborishda muammo bo‘ldi. O‘qituvchiga xabar berildi.")
    if n == 1 and bot:
        cover_path = BASE / "assets" / "covers" / f"unit_{u:02d}.png"
        if cover_path.exists():
            campaign = CAMPAIGNS[str(u)]
            await bot.send_photo(msg.chat.id, FSInputFile(cover_path), caption=f"🗺️ {campaign['title']} · yangi sarguzasht boshlandi!")
    if bot:
        async with Session() as db:
            video = await db.get(Video, key)
        if video:
            try:
                if video.locator.startswith("https://"):
                    await bot.send_video(msg.chat.id, video=video.locator, caption=f"🎬 {lesson['title']}")
                else:
                    await bot.send_video(msg.chat.id, video=video.locator, caption=f"🎬 {lesson['title']}")
            except Exception:
                log.exception("Could not deliver video %s", key)
                await msg.answer("🎬 Dars videosini hozir yuklab bo‘lmadi. O‘qituvchiga xabar berildi.")
    await msg.answer(body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Darsni o‘rganish", callback_data=f"guide:{key}")],
        [InlineKeyboardButton(text="🔤 So‘z kartochkalari", callback_data=f"flash:{key}:0")],
        [InlineKeyboardButton(text="🧠 5 ta yangi savol", callback_data=f"quiz:{key}:0")],
        [InlineKeyboardButton(text="🎲 Yangi mashg‘ulot olish", callback_data=f"challenge:{key}")],
        [InlineKeyboardButton(text="📩 Uy vazifasini yuborish", callback_data=f"homework:{key}")],
        [InlineKeyboardButton(text="📚 Bo‘lim lug‘atlari", callback_data=f"unitv:{u}")],
        [InlineKeyboardButton(text="🏠 Menyu", callback_data="menu")]]))


@dp.callback_query(F.data == "next")
async def next_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id, call.from_user):
        await call.answer("Kirish ruxsati kerak.", show_alert=True)
        return
    await call.answer()
    await open_current(call.from_user.id, call)


async def lesson_accessible(user_id: int, key: str) -> bool:
    if lesson_index(key) is None:
        return False
    done = await completed_set(user_id)
    return key in done or key == next_lesson(done)


@dp.callback_query(F.data.startswith("guide:"))
async def guide_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    key = call.data.split(":", 1)[1]
    if not await lesson_accessible(call.from_user.id, key):
        await call.answer("Avval oldingi darslarni yakunlang.", show_alert=True); return
    u, n = lesson_index(key)  # type: ignore[misc]
    lesson = lesson_at(u, n)
    guide = LESSON_GUIDES[key]
    linked = LESSON_LINKS["lessons"][key]
    examples = "\n".join(f"• {html.escape(str(x))}" for x in guide["examples"])
    body = (f"📖 <b>{html.escape(lesson['title'])} · qisqa qo‘llanma</b>\n\n"
            f"🎯 <b>Maqsad:</b> {html.escape(lesson['goal'])}\n\n"
            f"🧩 <b>{html.escape(str(lesson['focus']))}</b>\n{html.escape(str(guide['explain']))}\n\n"
            f"🗣 <b>Namunalar</b>\n{examples}\n\n"
            f"✍️ <b>O‘zingizni sinang</b>\n{html.escape(str(guide['try']))}\n\n"
            f"📘 Kitob sahifalari: darslik {linked['coursebook_pages']}-bet, mashqlar daftari {linked['workbook_pages']}-bet."
            " Yuqoridagi sahifa rasmlaridan foydalaning.")
    await call.message.answer(body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔤 So‘z kartochkalari", callback_data=f"flash:{key}:0")],
        [InlineKeyboardButton(text="🎲 Mashq", callback_data=f"challenge:{key}")],
        [InlineKeyboardButton(text="🧠 Mini-test", callback_data=f"quiz:{key}:0")],
    ]))
    await call.answer()


@dp.callback_query(F.data.startswith("flash:"))
async def flashcard_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    parts = call.data.split(":")
    if len(parts) != 3 or not await lesson_accessible(call.from_user.id, parts[1]):
        await call.answer("Bu dars hozircha ochiq emas.", show_alert=True); return
    key = parts[1]
    try: index = int(parts[2])
    except ValueError:
        await call.answer("Kartochka raqami xato.", show_alert=True); return
    cards = LESSON_GUIDES[key]["vocab"]
    if not cards:
        await call.answer("Bu dars uchun so‘z yo‘q.", show_alert=True); return
    if index < 0 or index >= len(cards):
        await call.message.answer("🎉 Kartochkalar tugadi! Birozdan keyin yana eslab ko‘ring.")
        await call.answer(); return
    word, meaning = cards[index]
    keyboard = [[InlineKeyboardButton(text="👁 Ma’nosini ko‘r", callback_data=f"flashshow:{key}:{index}")]]
    await call.message.answer(f"🔤 <b>So‘z kartochkasi {index + 1}/{len(cards)}</b>\n\n<b>{html.escape(str(word))}</b>\n\nAvval tarjimasini eslab ko‘ring.",
                              parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await call.answer()


@dp.callback_query(F.data.startswith("flashshow:"))
async def flashcard_reveal_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    parts = call.data.split(":")
    try: key, index = parts[1], int(parts[2])
    except (IndexError, ValueError):
        await call.answer("Kartochka topilmadi.", show_alert=True); return
    if not await lesson_accessible(call.from_user.id, key) or index < 0 or index >= len(LESSON_GUIDES[key]["vocab"]):
        await call.answer("Kartochka topilmadi.", show_alert=True); return
    word, meaning = LESSON_GUIDES[key]["vocab"][index]
    next_index = index + 1
    await call.message.answer(f"✅ <b>{html.escape(str(word))}</b> — {html.escape(str(meaning))}", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Keyingi so‘z ➡️" if next_index < len(LESSON_GUIDES[key]["vocab"]) else "Yana bir bor 🔁",
            callback_data=f"flash:{key}:{next_index if next_index < len(LESSON_GUIDES[key]['vocab']) else 0}")]]))
    await call.answer()


@dp.callback_query(F.data == "dailysettings")
async def daily_settings_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    today, _ = await learner_day_stats(call.from_user.id)
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        goal, reminder_hour = (learner.daily_goal_lessons, learner.reminder_hour) if learner else (1, 0)
    reminder = f"har kuni {reminder_hour:02d}:00" if reminder_hour else "o‘chirilgan"
    text_body = (f"🎯 <b>Kunlik o‘quv rejangiz</b>\nBugun: {min(today, goal)}/{goal} dars\n"
                 f"Eslatma: {reminder} (Toshkent vaqti)\n\nMaqsadni tanlang:")
    rows = [[InlineKeyboardButton(text=f"{'✅ ' if goal == value else ''}{value} dars / kun", callback_data=f"dailygoal:{value}")]
            for value in (1, 2, 3)]
    rows.append([InlineKeyboardButton(text="18:00", callback_data="dailyreminder:18"),
                 InlineKeyboardButton(text="19:00", callback_data="dailyreminder:19"),
                 InlineKeyboardButton(text="20:00", callback_data="dailyreminder:20")])
    rows.append([InlineKeyboardButton(text="Eslatmani o‘chirish", callback_data="dailyreminder:0")])
    await call.message.answer(text_body, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("dailygoal:"))
async def daily_goal_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    try: value = int(call.data.split(":", 1)[1])
    except ValueError: value = 0
    if value not in (1, 2, 3):
        await call.answer("Kunlik maqsad noto‘g‘ri.", show_alert=True); return
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        if learner:
            learner.daily_goal_lessons = value
            await db.commit()
    await daily_settings_callback(call)


@dp.callback_query(F.data.startswith("dailyreminder:"))
async def daily_reminder_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    try: value = int(call.data.split(":", 1)[1])
    except ValueError: value = -1
    if value not in (0, 18, 19, 20):
        await call.answer("Eslatma vaqti noto‘g‘ri.", show_alert=True); return
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        if learner:
            learner.reminder_hour = value
            await db.commit()
    await daily_settings_callback(call)


@dp.callback_query(F.data.startswith("quiz:"))
async def quiz_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True)
        return
    parts = call.data.split(":")
    if len(parts) not in (3, 4):
        await call.answer("Test tugmasi yaroqsiz.", show_alert=True); return
    _, key, qstr = parts[:3]
    parsed_key = lesson_index(key)
    if parsed_key is None:
        await call.answer("Dars kodi yaroqsiz.", show_alert=True); return
    done = await completed_set(call.from_user.id)
    if key != next_lesson(done):
        await call.answer("Bu dars hozircha ochiq emas.", show_alert=True)
        return
    try: q = int(qstr)
    except ValueError:
        await call.answer("Savol raqami yaroqsiz.", show_alert=True); return
    if q < 0:
        await call.answer("Savol raqami yaroqsiz.", show_alert=True); return
    question_bank = QUESTION_BANKS.get(key, [])
    question_map = {item["id"]: item for item in question_bank}
    if q == 0:
        active_state_raw = await redis.get(f"quiz:{call.from_user.id}")
        if active_state_raw:
            try:
                active_state = json.loads(active_state_raw)
            except json.JSONDecodeError:
                active_state = None
            if active_state and active_state.get("key") == key and "question_ids" in active_state:
                await call.answer("Bu test boshlangan. Joriy savolga javob bering.", show_alert=True)
                return
        async with Session() as db:
            seen = set((await db.scalars(select(QuizQuestionSeen.question_id).where(
                QuizQuestionSeen.telegram_id == call.from_user.id, QuizQuestionSeen.lesson_key == key
            ))).all())
        unseen_ids = [item["id"] for item in question_bank if item["id"] not in seen]
        if not unseen_ids:
            async with Session() as db:
                await db.execute(delete(QuizQuestionSeen).where(
                    QuizQuestionSeen.telegram_id == call.from_user.id, QuizQuestionSeen.lesson_key == key
                ))
                await db.commit()
            unseen_ids = [item["id"] for item in question_bank]
        question_ids = random.sample(unseen_ids, min(5, len(unseen_ids)))
        state = {"key": key, "index": 0, "score": 0, "question_ids": question_ids}
        async with Session() as db:
            db.add_all(QuizQuestionSeen(telegram_id=call.from_user.id, lesson_key=key, question_id=question_id)
                       for question_id in question_ids)
            await db.commit()
        await redis.setex(f"quiz:{call.from_user.id}", 3600, json.dumps(state))
    state_raw = await redis.get(f"quiz:{call.from_user.id}")
    state = json.loads(state_raw) if state_raw else None
    if state is None or "question_ids" not in state:
        await call.answer("Testni avval boshidan boshlang.", show_alert=True); return
    if state["key"] != key:
        await call.answer("Test muddati tugagan. Qaytadan boshlang.", show_alert=True)
        return
    question_ids = state["question_ids"]
    if q > len(question_ids):
        await call.answer("Savol raqami yaroqsiz.", show_alert=True); return
    if q > 0:
        if len(parts) != 4 or q != state["index"] + 1:
            await call.answer("Bu savol navbatda emas. Joriy dars testidan boshlang.", show_alert=True); return
        prior = question_map[question_ids[q - 1]]
        try:
            selected = int(parts[3])
        except ValueError:
            await call.answer("Javob tanlovi yaroqsiz.", show_alert=True); return
        if selected < 0 or selected >= 3:
            await call.answer("Javob tanlovi yaroqsiz.", show_alert=True); return
        is_correct = selected == prior["answer"]
        state["score"] += int(is_correct)
        state["index"] = q
        answer_feedback = (f"✅ To‘g‘ri: {prior['options'][selected]}" if is_correct
                           else f"💡 To‘g‘ri javob: {prior['options'][prior['answer']]}. Yodingizda saqlang!")
        await call.message.answer(answer_feedback)
    if q >= len(question_ids):
        u, n = parsed_key
        score = state["score"]
        max_score = len(question_ids)
        pass_mark = max(1, math.ceil(max_score * LEARNING_PATHWAYS["assessment"]["lesson_pass_percent"] / 100))
        passed = score >= pass_mark
        await redis.delete(f"quiz:{call.from_user.id}")
        async with Session() as db:
            db.add(QuizRun(telegram_id=call.from_user.id, lesson_key=key, score=score, max_score=max_score,
                           passed=passed, question_ids=json.dumps(question_ids)))
            await db.commit()
        if passed:
            xp_earned = 10 + round(15 * score / max_score)
            async with Session() as db:
                if not await db.scalar(select(Completion.id).where(Completion.telegram_id == call.from_user.id, Completion.lesson_key == key)):
                    db.add(Completion(telegram_id=call.from_user.id, lesson_key=key, score=score))
                    learner = await db.get(Learner, call.from_user.id)
                    if learner:
                        learner.xp += xp_earned
                    await db.commit()
            badge = " 🌟 Mukammal natija!" if score == max_score else ""
            if n == 4:
                badge += " 🏅 Bo‘lim yakunlandi!"
            if u == 10 and n == 4:
                badge += " 🏁 A1 kursi tugadi!"
            text = f"✅ Natija: {score}/{max_score}. +{xp_earned} XP!{badge}\nHar dars — yangi qadam!"
            course_lesson_number = (u - 1) * 4 + n
            if course_lesson_number % 3 == 0:
                text += "\n\n📚 Uch dars yakunlandi! Quyidagi tugmada o‘tilgan lug‘atlarni takrorlang."
            if u == 10 and n == 4:
                text += "\n\nA1 kursini yakunlash CEFR sertifikati yoki IELTS bandi berilganini anglatmaydi. Keyingi daraja tasdiqlangach ochiladi."
                buttons = [[InlineKeyboardButton(text="🧭 CEFR / IELTS yo‘lim", callback_data="pathprofile")],
                           [InlineKeyboardButton(text="🏠 Menyu", callback_data="menu")]]
            else:
                text += "\n\nKeyingi dars ochildi."
                buttons = [[InlineKeyboardButton(text="📚 Lug‘atlarni takrorlash", callback_data=f"pack:{course_lesson_number}")],
                           [InlineKeyboardButton(text="▶️ Keyingi dars", callback_data="next")]]
            await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        else:
            await call.message.answer(f"🔁 Natija: {score}/{max_score}. O‘tish uchun kamida {pass_mark} ta to‘g‘ri javob kerak. Keyingi urinishda boshqa savollar beriladi.",
                                      reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Qayta urinish", callback_data=f"quiz:{key}:0")]]))
        await call.answer()
        return
    item = question_map[question_ids[q]]
    buttons = [[InlineKeyboardButton(text=choice, callback_data=f"quiz:{key}:{q+1}:{i}")] for i, choice in enumerate(item["options"])]
    if q > 0:
        await redis.setex(f"quiz:{call.from_user.id}", 3600, json.dumps(state))
    await call.message.answer(f"❔ {q+1}/{len(question_ids)} · {item['question']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@dp.callback_query(F.data == "vocab")
async def vocab_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    done = await completed_set(call.from_user.id)
    unlocked_units = {int(k.split(".")[0]) for k in done} | {1}
    rows = []
    for unit in COURSE["units"]:
        if unit["id"] in unlocked_units:
            rows.append([InlineKeyboardButton(text=f"{unit['icon']} {unit['id']}-bo‘lim · {unit['title']}", callback_data=f"unitv:{unit['id']}")])
    await call.message.answer("O‘tilgan bo‘lim lug‘atini tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


async def send_unit_vocab(chat_id: int, unit_id: int, through: int | None = None) -> None:
    unit = COURSE["units"][unit_id - 1]
    limit = through or 4
    pairs = unit["vocab"] if limit >= 3 else unit["vocab"][:8]
    text = f"📚 <b>{unit['title']} · Lug‘at</b>\n\n" + "\n".join(f"• <b>{a}</b> — {b}" for a, b in pairs)
    text += "\n\n💡 Inglizcha so‘zni yoping va o‘zbekcha ma'nosini eslab ko‘ring."
    await bot.send_message(chat_id, text, parse_mode="HTML")  # type: ignore[union-attr]


async def send_vocab_pack(chat_id: int, completed_count: int) -> None:
    keys = lesson_order()[max(0, completed_count - 3):completed_count]
    words: list[tuple[str, str]] = []
    for key in keys:
        words.extend(LESSON_GUIDES[key]["vocab"])
    text = "📚 <b>Oxirgi uch dars · Lug‘at takrori</b>\n\n" + "\n".join(f"• <b>{a}</b> — {b}" for a, b in words)
    text += "\n\n💡 Inglizcha so‘zni yoping va o‘zbekcha ma'nosini eslab ko‘ring."
    await bot.send_message(chat_id, text, parse_mode="HTML")  # type: ignore[union-attr]


@dp.callback_query(F.data.startswith("unitv:") | F.data.startswith("pack:"))
async def vocab_detail(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    parts = call.data.split(":")
    done = await completed_set(call.from_user.id)
    allowed = {int(k.split(".")[0]) for k in done} | {1}
    if parts[0] == "pack":
        count = int(parts[1])
        if count % 3 != 0 or len(done) < count:
            await call.answer("Uchta darsni tugatgandan keyin ochiladi.", show_alert=True); return
        await send_vocab_pack(call.message.chat.id, count)
    else:
        unit_id = int(parts[1])
        if unit_id not in allowed:
            await call.answer("Avval bo‘lim darslarini bajaring.", show_alert=True); return
        await send_unit_vocab(call.message.chat.id, unit_id)
    await call.answer()


def path_goal_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌍 CEFR · umumiy ingliz tili", callback_data="pathgoal:CEFR")],
        [InlineKeyboardButton(text="🎓 IELTS imtihoniga tayyorlanish", callback_data="pathgoal:IELTS")],
    ])


async def show_path_summary(call: CallbackQuery) -> None:
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        goal = learner.path_goal if learner else "CEFR"
        module = learner.ielts_module if learner else ""
        band = learner.target_band if learner else ""
    done = await completed_set(call.from_user.id)
    cefr_levels = " → ".join(stage["level"] for stage in LEARNING_PATHWAYS["cefr_route"])
    if goal == "IELTS":
        module_name = {"academic": "Academic", "general": "General Training"}.get(module, "hali tanlanmagan")
        target = band or "hali belgilanmagan"
        ielts_stages = " → ".join(stage["name"] for stage in LEARNING_PATHWAYS["ielts_route"]["stages"])
        message = (
            f"🎓 <b>Sizning IELTS maqsadingiz</b>\nYo‘nalish: IELTS {module_name}\nMaqsadli band: {target}\n\n"
            f"🧭 <b>Professional tayyorgarlik yo‘li</b>\n{cefr_levels} → B1 tayyorgarlik tekshiruvi → {ielts_stages}.\n\n"
            "Hozir botda A1 kursi mavjud. Yuqori darajalar to‘liq dars, audio, javob kaliti va o‘qituvchi tekshiruviga ega bo‘lmaguncha ochilmaydi. IELTS maqsadi rasmiy band kafolati emas."
        )
    else:
        message = (
            f"🌍 <b>Sizning CEFR yo‘lingiz</b>\n{cefr_levels}\n\n"
            f"Hozirgi faol kurs: A1 · bajarilgan {len(done)}/40 dars.\n\n"
            "Har bir daraja tinglab tushunish, o‘qish, gapirish va yozish bo‘yicha amaliy mezonlar bilan tekshiriladi. Hozir botda A1 darslari mavjud; keyingi darajalar tayyorlangach o‘qituvchi ochadi."
        )
    await call.message.answer(message, parse_mode="HTML", reply_markup=path_goal_keyboard())


@dp.callback_query(F.data == "pathprofile")
async def path_profile(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    await show_path_summary(call)
    await call.answer()


@dp.callback_query(F.data.startswith("pathgoal:"))
async def choose_path_goal(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    choice = call.data.split(":", 1)[1]
    if choice not in {"CEFR", "IELTS"}:
        await call.answer("Yo‘nalish yaroqsiz.", show_alert=True); return
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        if learner:
            learner.path_goal = choice
            learner.ielts_module = ""
            learner.target_band = ""
            await db.commit()
    if choice == "CEFR":
        await show_path_summary(call)
    else:
        await call.message.answer(
            "IELTS’ning qaysi turi sizga kerak? O‘qish yoki kasbiy ro‘yxatdan o‘tish uchun ko‘pincha Academic tanlanadi; ayrim ish/migratsiya maqsadlari uchun General Training talab qilinishi mumkin. Talabni topshiradigan tashkilotingizdan tekshiring.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎓 Academic", callback_data="ieltsmodule:academic")],
                [InlineKeyboardButton(text="🧳 General Training", callback_data="ieltsmodule:general")],
            ]))
    await call.answer()


@dp.callback_query(F.data.startswith("ieltsmodule:"))
async def choose_ielts_module(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    module = call.data.split(":", 1)[1]
    if module not in {"academic", "general"}:
        await call.answer("IELTS turi yaroqsiz.", show_alert=True); return
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        if learner:
            learner.path_goal = "IELTS"
            learner.ielts_module = module
            await db.commit()
    await call.message.answer("Hozircha qaysi maqsadli bandni ko‘zlayapsiz? Bu faqat rejalashtirish maqsadi; bot bandni kafolatlamaydi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="3.0", callback_data="ieltsband:3_0"), InlineKeyboardButton(text="3.5", callback_data="ieltsband:3_5"), InlineKeyboardButton(text="4.0", callback_data="ieltsband:4_0")],
            [InlineKeyboardButton(text="4.5", callback_data="ieltsband:4_5"), InlineKeyboardButton(text="5.0", callback_data="ieltsband:5_0"), InlineKeyboardButton(text="5.5", callback_data="ieltsband:5_5")],
            [InlineKeyboardButton(text="6.0", callback_data="ieltsband:6_0"), InlineKeyboardButton(text="6.5", callback_data="ieltsband:6_5"), InlineKeyboardButton(text="7.0", callback_data="ieltsband:7_0")],
            [InlineKeyboardButton(text="7.5", callback_data="ieltsband:7_5"), InlineKeyboardButton(text="8.0", callback_data="ieltsband:8_0"), InlineKeyboardButton(text="8.5", callback_data="ieltsband:8_5"), InlineKeyboardButton(text="9.0", callback_data="ieltsband:9_0")],
        ]))
    await call.answer()


@dp.callback_query(F.data.startswith("ieltsband:"))
async def choose_ielts_band(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    band = call.data.split(":", 1)[1].replace("_", ".").replace(".plus", "+")
    if band not in {f"{whole}.{half}" for whole in range(3, 10) for half in (0, 5)}:
        await call.answer("Band maqsadi yaroqsiz.", show_alert=True); return
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        if learner:
            learner.path_goal = "IELTS"
            learner.target_band = band
            await db.commit()
    await show_path_summary(call)
    await call.answer("Maqsadingiz saqlandi.")


@dp.callback_query(F.data == "myprogress")
async def progress_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    done = await completed_set(call.from_user.id)
    async with Session() as db:
        learner = await db.get(Learner, call.from_user.id)
        xp = learner.xp if learner else 0
        goal = learner.path_goal if learner else "CEFR"
        daily_goal = learner.daily_goal_lessons if learner else 1
    streak = await learner_streak(call.from_user.id)
    completed_today, _ = await learner_day_stats(call.from_user.id)
    total = len(lesson_order())
    pct = round(100 * len(done) / total)
    bar = "🟩" * (pct // 10) + "⬜" * (10 - pct // 10)
    goal_label = "IELTS maqsad yo‘li" if goal == "IELTS" else "CEFR · A1 bosqichi"
    await call.message.answer(f"📈 <b>Sizning yo‘lingiz</b>\n{goal_label}\n\n{bar} {pct}%\nBajarildi: {len(done)}/{total} dars\n🎯 Bugungi maqsad: {min(completed_today, daily_goal)}/{daily_goal} dars\n✨ Tajriba: {xp} XP\n🔥 Faollik ketma-ketligi: {streak} kun\n🏅 Belgilar: {len(done)//4} ta bo‘lim nishoni", parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "menu")
async def menu_callback(call: CallbackQuery) -> None:
    if await learner_ok(call.from_user.id, call.from_user):
        await call.message.answer("Menyu", reply_markup=home_keyboard(is_admin(call.from_user.id)))
    else:
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    await call.answer()


@dp.callback_query(F.data == "askhelp")
async def ask_help_callback(call: CallbackQuery) -> None:
    await call.message.answer("Savolingizni /ask bu yerda yozing. Masalan: /ask when ishlatiladi?")
    await call.answer()


@dp.callback_query(F.data.startswith("challenge:"))
async def challenge_callback(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    key = call.data.split(":", 1)[1]
    if key != next_lesson(await completed_set(call.from_user.id)):
        await call.answer("Avval ochiq darsni bajaring.", show_alert=True); return
    bank = ACTIVITY_BANKS.get(key, [])
    if not bank:
        await call.answer("O‘yin topilmadi.", show_alert=True); return
    async with Session() as db:
        seen = set((await db.scalars(select(ActivitySeen.activity_id).where(
            ActivitySeen.telegram_id == call.from_user.id, ActivitySeen.lesson_key == key
        ))).all())
        unseen = [item for item in bank if item["id"] not in seen]
        if not unseen:
            await db.execute(delete(ActivitySeen).where(
                ActivitySeen.telegram_id == call.from_user.id, ActivitySeen.lesson_key == key
            ))
            unseen = bank
            seen.clear()
        activity = random.choice(unseen)
        db.add(ActivitySeen(telegram_id=call.from_user.id, lesson_key=key, activity_id=activity["id"]))
        await db.commit()
    await call.message.answer(
        f"🎲 <b>{activity['title']} · {key} dars</b>\n"
        f"Mashg‘ulot {len(seen) + 1}/25\n\n{html.escape(activity['prompt'])}\n\n"
        "✅ Bajarganingizdan so‘ng ishni uy vazifasi tugmasi orqali yuboring.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Yana yangi mashg‘ulot", callback_data=f"challenge:{key}")],
            [InlineKeyboardButton(text="📩 Natijani yuborish", callback_data=f"homework:{key}")],
        ]),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("homework:"))
async def homework_start(call: CallbackQuery) -> None:
    if not await learner_ok(call.from_user.id):
        await call.answer("Kirish ruxsati kerak.", show_alert=True); return
    key = call.data.split(":", 1)[1]
    if key != next_lesson(await completed_set(call.from_user.id)):
        await call.answer("Uy vazifasini joriy ochiq dars uchun yuboring.", show_alert=True); return
    await redis.setex(f"homework-await:{call.from_user.id}", 86400, key)
    await call.message.answer(f"📩 <b>{key} dars uy vazifasi</b>\nYozma javob, sahifa rasmi yoki gapirish topshirig‘i uchun ovozli xabar yuboring. O‘qituvchingiz ko‘rib, fikr bildiradi.", parse_mode="HTML")
    await call.answer()


@dp.message(Command("ask"))
async def ask_ai(message: Message, command: CommandObject) -> None:
    if not await learner_ok(message.from_user.id, message.from_user):
        await message.answer("Kurs savollarini yuborish uchun o‘qituvchidan ruxsat oling."); return
    question = (command.args or "").strip()
    if len(question) < 3:
        await message.answer("Savolingizni yozing: /ask How do I use can?"); return
    if len(question) > 1200:
        await message.answer("Savol 1200 belgidan qisqa bo‘lsin."); return
    accepted = await redis.set(f"ask-limit:{message.from_user.id}", "1", ex=45, nx=True)
    if not accepted:
        await message.answer("Savolni qabul qildim. Yangi savol yuborishdan oldin 45 soniya kuting."); return
    async with Session() as db:
        db.add(QuestionLog(telegram_id=message.from_user.id, question=question))
        learner = await db.get(Learner, message.from_user.id)
        learner_goal = learner.path_goal if learner else "CEFR"
        await db.commit()
    if ai_client:
        try:
            goal_context = ("The learner's long-term goal is IELTS, but the active course may still be beginner CEFR. "
                            "Teach at the current level; do not give an official band score or imply the IELTS course is active."
                            if learner_goal == "IELTS" else "The learner's goal is general CEFR English.")
            response = await ai_client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), max_tokens=350,
                messages=[{"role":"system","content":"You are a kind English A1 tutor. Reply in Uzbek, use very simple English examples, correct gently, stay within beginner English, never claim to change course settings or learner access. Never certify CEFR level or predict official IELTS band scores. " + goal_context},
                          {"role":"user","content":question}])
            answer = response.choices[0].message.content or "Savolni biroz boshqacha yozib ko‘ring."
            await message.answer("🤖 " + answer[:3500])
        except Exception:
            log.exception("AI tutor request failed")
            await message.answer("AI yordamchi vaqtincha javob bera olmadi. Savolingiz o‘qituvchiga yuborildi.")
    else:
        await message.answer("Savolingiz o‘qituvchiga yuborildi. AI yordamchini yoqish uchun OPENAI_API_KEY sozlanadi.")
    if bot:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, f"🙋 O‘quvchi savoli · ID {message.from_user.id}\n{question[:1000]}")
            except Exception:
                log.warning("Could not notify teacher %s", admin_id)


def admin_guard(user_id: int) -> None:
    if not is_admin(user_id):
        raise PermissionError("Admin ruxsati kerak.")


@dp.message(Command("admin"))
async def admin_cmd(message: Message) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat o‘qituvchi administrator uchun."); return
    await message.answer("🛠 <b>O‘qituvchi paneli</b>\n\n/invite — 3 kunlik taklif kodi\n/grant ID — bevosita ruxsat berish\n/revoke ID — kirishni yopish\n/video 1.1 — video yuklash\n/stats — umumiy tahlil\n/learners — o‘quvchilar\n/homeworks — topshiriqlar\n/questions — so‘nggi savollar\n/report — darslar tahlili", parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📊 Tahlil", callback_data="adminstats")],
                         [InlineKeyboardButton(text="👥 O‘quvchilar", callback_data="adminlearners")],
                         [InlineKeyboardButton(text="📩 Uy vazifalari", callback_data="adminhomeworks")],
                         [InlineKeyboardButton(text="❓ Savollar", callback_data="adminquestions")]]))


@dp.callback_query(F.data.startswith("admin"))
async def admin_panel_callback(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Admin ruxsati kerak.", show_alert=True); return
    if call.data in ("adminstats",):
        await send_stats(call.message)
    elif call.data == "adminlearners":
        await send_learners(call.message)
    elif call.data == "adminquestions":
        await send_questions(call.message)
    elif call.data == "adminhomeworks":
        await send_homeworks(call.message)
    else:
        await call.message.answer("🛠 Admin panel: /grant, /revoke, /video, /stats, /learners, /questions")
    await call.answer()


@dp.message(Command("grant"))
async def grant(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo‘q."); return
    try: target_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Foydalanish: /grant TELEGRAM_ID"); return
    async with Session() as db:
        row = await db.get(Learner, target_id)
        if row: row.authorized = True
        else: db.add(Learner(telegram_id=target_id, authorized=True))
        await db.commit()
    await message.answer(f"✅ {target_id} uchun kursga kirish ochildi.")
    if bot:
        try: await bot.send_message(target_id, "🎓 O‘qituvchi sizga English Path A1 kursiga kirish berdi. /start ni yuboring.")
        except Exception: pass


@dp.message(Command("invite"))
async def create_invite(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo‘q."); return
    try:
        uses = int((command.args or "1").strip())
    except ValueError:
        await message.answer("Foydalanish: /invite yoki /invite 10 (foydalanish soni)"); return
    if not 1 <= uses <= 100:
        await message.answer("Taklif 1–100 marta ishlatilishi mumkin."); return
    code = secrets.token_urlsafe(7).replace("-", "A").replace("_", "B").upper()
    async with Session() as db:
        db.add(InviteCode(code=code, uses_left=uses, expires_at=datetime.now(timezone.utc) + timedelta(days=3), created_by=message.from_user.id))
        await db.commit()
    await message.answer(f"🔑 Taklif kodi: <code>{code}</code>\nIshlatish soni: {uses}\nAmal qilish muddati: 3 kun\nO‘quvchi botga /join {code} yuborsin.", parse_mode="HTML")


@dp.message(Command("revoke"))
async def revoke(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo‘q."); return
    try: target_id = int((command.args or "").strip())
    except ValueError:
        await message.answer("Foydalanish: /revoke TELEGRAM_ID"); return
    async with Session() as db:
        row = await db.get(Learner, target_id)
        if row: row.authorized = False; await db.commit()
    await message.answer(f"🔒 {target_id} uchun kirish yopildi.")


async def send_stats(message: Message) -> None:
    active_since = datetime.now(timezone.utc) - timedelta(days=7)
    async with Session() as db:
        total = await db.scalar(select(func.count()).select_from(Learner).where(Learner.authorized.is_(True))) or 0
        active = await db.scalar(select(func.count()).select_from(Learner).where(Learner.authorized.is_(True), Learner.last_seen >= active_since)) or 0
        completed = await db.scalar(select(func.count()).select_from(Completion)) or 0
        questions = await db.scalar(select(func.count()).select_from(QuestionLog)) or 0
    await message.answer(f"📊 <b>Kurs tahlili</b>\n\n👥 Ruxsatli o‘quvchilar: {total}\n⚡ So‘nggi 7 kunda faol: {active}\n✅ Yakunlangan darslar: {completed}\n🙋 Jami savollar: {questions}", parse_mode="HTML")


async def send_learners(message: Message) -> None:
    async with Session() as db:
        rows = (await db.scalars(select(Learner).order_by(Learner.last_seen.desc()).limit(30))).all()
        comps = await db.execute(select(Completion.telegram_id, func.count()).group_by(Completion.telegram_id))
        counts = dict(comps.all())
    if not rows:
        await message.answer("Hozircha o‘quvchilar yo‘q."); return
    text = "👥 <b>Oxirgi faol o‘quvchilar</b>\n\n"
    for row in rows:
        text += f"{'✅' if row.authorized else '🔒'} {html.escape(row.name or 'O‘quvchi')} · ID <code>{row.telegram_id}</code> · {counts.get(row.telegram_id,0)}/40\n"
    await message.answer(text[:3900], parse_mode="HTML")


async def send_questions(message: Message) -> None:
    async with Session() as db:
        rows = (await db.scalars(select(QuestionLog).order_by(QuestionLog.created_at.desc()).limit(10))).all()
    if not rows:
        await message.answer("Hozircha savollar yo‘q."); return
    await message.answer("❓ <b>So‘nggi savollar</b>\n\n" + "\n\n".join(f"ID {x.telegram_id}: {html.escape(x.question[:240])}" for x in rows), parse_mode="HTML")


async def teacher_report() -> tuple[str, bool]:
    inactive_since = datetime.now(timezone.utc) - timedelta(days=7)
    async with Session() as db:
        inactive = await db.scalar(select(func.count()).select_from(Learner).where(
            Learner.authorized.is_(True), Learner.last_seen < inactive_since)) or 0
        pending = await db.scalar(select(func.count()).select_from(HomeworkSubmission).where(
            HomeworkSubmission.reviewed.is_(False))) or 0
        grouped = await db.execute(select(
            QuizRun.lesson_key, func.count(QuizRun.id), func.avg(QuizRun.score * 3.0 / QuizRun.max_score),
            func.avg(cast(QuizRun.passed, Integer))
        ).group_by(QuizRun.lesson_key))
        results = grouped.all()
    weakest = sorted((r for r in results if r[1] >= 3), key=lambda r: (r[3], r[2]))[:5]
    report = f"📊 O‘qituvchi tahlili\n\n🔕 7 kundan beri faol emas: {inactive}\n📩 Tekshirilmagan uy vazifasi: {pending}\n"
    if weakest:
        report += "\n🧩 Ko‘proq yordam kerak bo‘lishi mumkin bo‘lgan darslar (kamida 3 urinish):\n"
        for key, attempts, avg_score, pass_rate in weakest:
            report += f"• {key}: {round(float(pass_rate) * 100)}% o‘tish, {attempts} urinish\n"
    else:
        report += "\nDarslar bo‘yicha xulosa uchun hozircha 3 tadan kam test natijasi bor."
    actionable = bool(inactive or pending or weakest)
    if ai_client and actionable:
        try:
            response = await ai_client.chat.completions.create(
                model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"), max_tokens=180,
                messages=[
                    {"role":"system","content":"You are an English course operations assistant. Summarize only the aggregate metrics in Uzbek and suggest at most three concrete teacher actions. Do not invent facts, identify individual students, change settings, or claim actions were taken."},
                    {"role":"user","content":report}])
            summary = response.choices[0].message.content
            if summary:
                report += "\n🤖 AI tavsiyasi\n" + summary[:1200]
        except Exception:
            log.exception("Teacher report AI summary failed")
    return report, actionable


@dp.message(Command("report"))
async def report_command(message: Message) -> None:
    if not is_admin(message.from_user.id): await message.answer("⛔ Ruxsat yo‘q."); return
    report, _ = await teacher_report()
    await message.answer(report)


@dp.message(Command("stats"))
async def stats_command(message: Message) -> None:
    if not is_admin(message.from_user.id): await message.answer("⛔ Ruxsat yo‘q."); return
    await send_stats(message)


@dp.message(Command("learners"))
async def learners_command(message: Message) -> None:
    if not is_admin(message.from_user.id): await message.answer("⛔ Ruxsat yo‘q."); return
    await send_learners(message)


@dp.message(Command("questions"))
async def questions_command(message: Message) -> None:
    if not is_admin(message.from_user.id): await message.answer("⛔ Ruxsat yo‘q."); return
    await send_questions(message)


async def send_homeworks(message: Message) -> None:
    async with Session() as db:
        rows = (await db.scalars(select(HomeworkSubmission).where(HomeworkSubmission.reviewed.is_(False)).order_by(HomeworkSubmission.created_at.desc()).limit(10))).all()
    if not rows:
        await message.answer("✅ Tekshirilishi kerak bo‘lgan uy vazifasi yo‘q."); return
    for row in rows:
        note = f"📩 <b>Uy vazifasi #{row.id}</b> · dars {row.lesson_key}\nO‘quvchi ID: <code>{row.telegram_id}</code>\n\n{html.escape(row.answer[:1200]) or '(rasm yuborildi)'}\n\nJavob: /feedback {row.id} fikringiz"
        await message.answer(note, parse_mode="HTML")
        if row.file_id and bot:
            kind, _, media_id = row.file_id.partition("|")
            if not media_id:
                kind, media_id = "photo", kind  # Existing submissions from the earlier revision.
            try:
                if kind == "voice": await bot.send_voice(message.chat.id, media_id, caption=f"Uy vazifasi #{row.id}")
                elif kind == "document": await bot.send_document(message.chat.id, media_id, caption=f"Uy vazifasi #{row.id}")
                else: await bot.send_photo(message.chat.id, media_id, caption=f"Uy vazifasi #{row.id}")
            except Exception: log.exception("Could not forward homework media %s", row.id)


@dp.message(Command("homeworks"))
async def homeworks_command(message: Message) -> None:
    if not is_admin(message.from_user.id): await message.answer("⛔ Ruxsat yo‘q."); return
    await send_homeworks(message)


@dp.message(Command("feedback"))
async def feedback_command(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id): await message.answer("⛔ Ruxsat yo‘q."); return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Foydalanish: /feedback TOPSHIRIQ_ID fikringiz"); return
    try: submission_id = int(parts[0])
    except ValueError:
        await message.answer("Topshiriq ID raqam bo‘lishi kerak."); return
    async with Session() as db:
        row = await db.get(HomeworkSubmission, submission_id)
        if not row:
            await message.answer("Topshiriq topilmadi."); return
        row.teacher_note = parts[1][:1000]
        row.reviewed = True
        learner_id = row.telegram_id
        key = row.lesson_key
        await db.commit()
    if bot:
        try: await bot.send_message(learner_id, f"🧑‍🏫 O‘qituvchi fikri · {key}\n\n{parts[1][:1000]}")
        except Exception: await message.answer("Topshiriq tekshirildi, lekin o‘quvchiga xabar yetmadi.")
    await message.answer(f"✅ #{submission_id} topshiriqqa fikr yuborildi.")


@dp.message(Command("video"))
async def set_video(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Ruxsat yo‘q."); return
    args = (command.args or "").split(maxsplit=1)
    if len(args) == 1 and lesson_index(args[0]):
        await redis.setex(f"video-await:{message.from_user.id}", 600, args[0])
        await message.answer(f"🎬 {args[0]} uchun videoni shu chatga yuboring. 10 daqiqa ichida jo‘nating."); return
    if len(args) != 2 or not lesson_index(args[0]):
        await message.answer("Foydalanish: /video 1.1 yuborish (keyin videoni jo‘nating) yoki /video 1.1 https://…"); return
    key, locator = args
    if locator.startswith("http://") or (locator.startswith("https://") and len(locator) > 8):
        if not locator.startswith("https://"):
            await message.answer("Video havolasi HTTPS bo‘lishi kerak."); return
    elif not re.fullmatch(r"[A-Za-z0-9_\-:.]{20,500}", locator):
        await message.answer("Video Telegram file_id yoki HTTPS havola bo‘lishi kerak."); return
    async with Session() as db:
        row = await db.get(Video, key)
        if row: row.locator = locator; row.updated_by = message.from_user.id; row.updated_at = datetime.now(timezone.utc)
        else: db.add(Video(lesson_key=key, locator=locator, updated_by=message.from_user.id))
        await db.commit()
    await message.answer(f"🎬 {key} dars videosi saqlandi.")


@dp.message(F.video)
async def receive_video(message: Message) -> None:
    if not is_admin(message.from_user.id):
        return
    key = await redis.get(f"video-await:{message.from_user.id}")
    if not key:
        await message.answer("Avval /video 1.1 kabi dars raqamini yuboring."); return
    async with Session() as db:
        row = await db.get(Video, key)
        if row: row.locator = message.video.file_id; row.updated_by = message.from_user.id; row.updated_at = datetime.now(timezone.utc)
        else: db.add(Video(lesson_key=key, locator=message.video.file_id, updated_by=message.from_user.id))
        await db.commit()
    await redis.delete(f"video-await:{message.from_user.id}")
    await message.answer(f"✅ Video {key} darsiga biriktirildi.")


@dp.message()
async def fallback(message: Message) -> None:
    if await learner_ok(message.from_user.id, message.from_user):
        pending = await redis.get(f"homework-await:{message.from_user.id}")
        if pending:
            answer = (message.text or message.caption or "").strip()
            file_id = ""
            if message.photo: file_id = "photo|" + message.photo[-1].file_id
            elif message.voice: file_id = "voice|" + message.voice.file_id
            elif message.document: file_id = "document|" + message.document.file_id
            if len(answer) > 3900:
                await message.answer("Javob 3900 belgidan oshmasin. Qisqartirib yuboring."); return
            if not answer and not file_id:
                await message.answer("Javobni matn yoki rasm/fayl qilib yuboring."); return
            async with Session() as db:
                db.add(HomeworkSubmission(telegram_id=message.from_user.id, lesson_key=pending, answer=answer[:3900], file_id=file_id))
                await db.commit()
            await redis.delete(f"homework-await:{message.from_user.id}")
            await message.answer("📬 Uy vazifangiz o‘qituvchiga yuborildi. Fikri shu bot orqali keladi.")
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, f"📩 Yangi uy vazifasi · ID {message.from_user.id} · dars {pending}\n/homeworks buyrug‘i bilan ko‘ring.")  # type: ignore[union-attr]
                except Exception: log.warning("Could not notify teacher about homework")
            return
        await message.answer("Xabar qabul qilindi. Dars menyusi uchun /start, savol uchun /ask savolingiz.")
    else:
        await message.answer("🔒 Kursga kirish uchun o‘qituvchingizdan ruxsat so‘rang.")


ADMIN_HTML = (BASE / "teacher.html").read_text(encoding="utf-8")


def api_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("ADMIN_API_TOKEN", "")
    if not expected or not hmac.compare_digest(authorization or "", f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Unauthorized")


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add profile/progress columns when upgrading a local MVP database.
        columns = await conn.run_sync(lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("learners")})
        learner_migrations = {
            "xp": "INTEGER NOT NULL DEFAULT 0",
            "path_goal": "VARCHAR(16) NOT NULL DEFAULT 'CEFR'",
            "ielts_module": "VARCHAR(16) NOT NULL DEFAULT ''",
            "target_band": "VARCHAR(8) NOT NULL DEFAULT ''",
            "daily_goal_lessons": "INTEGER NOT NULL DEFAULT 1",
            "reminder_hour": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in learner_migrations.items():
            if column not in columns:
                await conn.execute(text(f"ALTER TABLE learners ADD COLUMN {column} {definition}"))
        quiz_columns = await conn.run_sync(lambda sync_conn: {col["name"] for col in inspect(sync_conn).get_columns("quiz_runs")})
        if "max_score" not in quiz_columns:
            await conn.execute(text("ALTER TABLE quiz_runs ADD COLUMN max_score INTEGER NOT NULL DEFAULT 3"))
        if "question_ids" not in quiz_columns:
            await conn.execute(text("ALTER TABLE quiz_runs ADD COLUMN question_ids VARCHAR(1000) NOT NULL DEFAULT ''"))
    DATABASE_READY.set()
    yield


api = FastAPI(title="English Path A1 Teacher API", docs_url=None, redoc_url=None, lifespan=lifespan)


@api.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return ADMIN_HTML


@api.get("/api/overview", dependencies=[Depends(api_auth)])
async def overview() -> dict[str, int]:
    active_since = datetime.now(timezone.utc) - timedelta(days=7)
    async with Session() as db:
        authorized = await db.scalar(select(func.count()).select_from(Learner).where(Learner.authorized.is_(True))) or 0
        active = await db.scalar(select(func.count()).select_from(Learner).where(Learner.authorized.is_(True), Learner.last_seen >= active_since)) or 0
        completions = await db.scalar(select(func.count()).select_from(Completion)) or 0
        questions = await db.scalar(select(func.count()).select_from(QuestionLog)) or 0
        pending = await db.scalar(select(func.count()).select_from(HomeworkSubmission).where(HomeworkSubmission.reviewed.is_(False))) or 0
        cefr_goals = await db.scalar(select(func.count()).select_from(Learner).where(Learner.authorized.is_(True), Learner.path_goal == "CEFR")) or 0
        ielts_goals = await db.scalar(select(func.count()).select_from(Learner).where(Learner.authorized.is_(True), Learner.path_goal == "IELTS")) or 0
    return {"authorized": authorized, "active": active, "completions": completions, "questions": questions,
            "pending_homeworks": pending, "cefr_goals": cefr_goals, "ielts_goals": ielts_goals}


@api.get("/api/pathways", dependencies=[Depends(api_auth)])
async def pathways() -> dict[str, Any]:
    return LEARNING_PATHWAYS


@api.get("/api/learners", dependencies=[Depends(api_auth)])
async def learners() -> dict[str, Any]:
    async with Session() as db:
        rows = (await db.scalars(select(Learner).order_by(Learner.last_seen.desc()).limit(200))).all()
        result = await db.execute(select(Completion.telegram_id, func.count()).group_by(Completion.telegram_id))
        counts = dict(result.all())
    return {"items": [{"id": r.telegram_id, "name": html.escape(r.name, quote=True), "username": r.username, "authorized": r.authorized,
                       "done": counts.get(r.telegram_id, 0), "xp": r.xp, "path_goal": r.path_goal,
                       "ielts_module": r.ielts_module, "target_band": r.target_band,
                       "last_seen": r.last_seen.isoformat() if r.last_seen else None} for r in rows]}


@api.get("/api/lesson-stats", dependencies=[Depends(api_auth)])
async def lesson_stats() -> dict[str, Any]:
    async with Session() as db:
        rows = await db.execute(select(
            QuizRun.lesson_key, func.count(QuizRun.id), func.avg(QuizRun.score * 100.0 / QuizRun.max_score),
            func.avg(cast(QuizRun.passed, Integer))
        ).group_by(QuizRun.lesson_key))
    stats = {r[0]: (r[1], r[2], r[3]) for r in rows.all()}
    items = []
    for unit in COURSE["units"]:
        for lesson in unit["lessons"]:
            key = key_for(unit["id"], lesson["id"])
            attempts, avg_score, pass_rate = stats.get(key, (0, 0, 0))
            items.append({"key": key, "title": lesson["title"], "attempts": attempts,
                          "avg_score": round(float(avg_score or 0), 2),
                          "pass_rate": round(float(pass_rate or 0) * 100)})
    return {"items": items}


@api.get("/api/homeworks", dependencies=[Depends(api_auth)])
async def pending_homeworks() -> dict[str, Any]:
    async with Session() as db:
        rows = await db.execute(select(HomeworkSubmission, Learner.name).join(
            Learner, Learner.telegram_id == HomeworkSubmission.telegram_id
        ).where(HomeworkSubmission.reviewed.is_(False)).order_by(HomeworkSubmission.created_at.desc()).limit(30))
    items = []
    for submission, learner_name in rows.all():
        kind = submission.file_id.split("|", 1)[0] if submission.file_id else ""
        items.append({"id": submission.id, "learner_id": submission.telegram_id,
                      "name": html.escape(learner_name or "O‘quvchi", quote=True),
                      "lesson_key": submission.lesson_key, "answer": submission.answer,
                      "has_media": bool(submission.file_id), "media_type": kind,
                      "created_at": submission.created_at.isoformat() if submission.created_at else None})
    return {"items": items}


@api.get("/api/report", dependencies=[Depends(api_auth)])
async def report_api() -> dict[str, str]:
    report, _ = await teacher_report()
    return {"text": report}


@api.post("/api/invites", dependencies=[Depends(api_auth)])
async def invite_api(payload: dict[str, Any]) -> dict[str, Any]:
    try: uses = int(payload.get("uses", 1))
    except (ValueError, TypeError): raise HTTPException(status_code=400, detail="uses must be an integer")
    if not 1 <= uses <= 100: raise HTTPException(status_code=400, detail="uses must be between 1 and 100")
    code = secrets.token_urlsafe(7).replace("-", "A").replace("_", "B").upper()
    async with Session() as db:
        db.add(InviteCode(code=code, uses_left=uses,
                          expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                          created_by=next(iter(ADMIN_IDS))))
        await db.commit()
    return {"code": code, "uses": uses}


@api.post("/api/homework-feedback", dependencies=[Depends(api_auth)])
async def homework_feedback_api(payload: dict[str, Any]) -> dict[str, bool]:
    try:
        submission_id = int(payload["id"])
        note = str(payload["note"]).strip()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid payload")
    if submission_id <= 0 or not note or len(note) > 1000:
        raise HTTPException(status_code=400, detail="Feedback must be 1–1000 characters")
    async with Session() as db:
        submission = await db.get(HomeworkSubmission, submission_id)
        if not submission:
            raise HTTPException(status_code=404, detail="Homework not found")
        submission.teacher_note = note
        submission.reviewed = True
        learner_id, lesson_key = submission.telegram_id, submission.lesson_key
        await db.commit()
    if bot:
        try: await bot.send_message(learner_id, f"🧑‍🏫 O‘qituvchi fikri · {lesson_key}\n\n{note}")
        except Exception: log.exception("Could not deliver teacher feedback to learner %s", learner_id)
    return {"ok": True}


@api.post("/api/access", dependencies=[Depends(api_auth)])
async def change_access(payload: dict[str, Any]) -> dict[str, bool]:
    try:
        learner_id = int(payload["id"])
        authorized = payload["authorized"]
        if not isinstance(authorized, bool) or learner_id <= 0:
            raise ValueError("Invalid values")
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid payload")
    async with Session() as db:
        row = await db.get(Learner, learner_id)
        if row: row.authorized = authorized
        elif authorized: db.add(Learner(telegram_id=learner_id, authorized=True))
        await db.commit()
    return {"ok": True}


async def teacher_reminders() -> None:
    """Daily aggregated teacher report; no message when nothing needs attention."""
    while True:
        await asyncio.sleep(24 * 60 * 60)
        if not bot or not ADMIN_IDS:
            continue
        try:
            report, actionable = await teacher_report()
            if actionable:
                for admin_id in ADMIN_IDS:
                    await bot.send_message(admin_id, "🔔 Kunlik kurs holati\n\n" + report)
        except Exception:
            log.exception("Reminder job failed")


async def learner_daily_reminders() -> None:
    """Send one optional, learner-configured daily nudge in Tashkent time."""
    await DATABASE_READY.wait()
    while True:
        now = datetime.now(TASHKENT)
        if bot:
            try:
                async with Session() as db:
                    learners = (await db.scalars(select(Learner).where(
                        Learner.authorized.is_(True), Learner.reminder_hour > 0,
                        Learner.reminder_hour == now.hour))).all()
                    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=TASHKENT).astimezone(timezone.utc)
                    completion_rows = (await db.execute(select(
                        Completion.telegram_id, func.count(func.distinct(Completion.lesson_key))
                    ).where(Completion.completed_at >= day_start).group_by(Completion.telegram_id))).all()
                completed_by_learner = {user_id: count for user_id, count in completion_rows}
                for learner in learners:
                    marker = f"daily-reminder:{learner.telegram_id}:{now.date().isoformat()}"
                    if not await redis.set(marker, "1", ex=48 * 60 * 60, nx=True):
                        continue
                    completed_today = completed_by_learner.get(learner.telegram_id, 0)
                    goal = max(1, learner.daily_goal_lessons or 1)
                    if completed_today >= goal:
                        continue
                    try:
                        await bot.send_message(learner.telegram_id,
                            f"🌱 Bugungi kichik maqsadingiz: {goal} dars. Hozircha {completed_today} ta bajarildi. "
                            "Davom etishga tayyor bo‘lsangiz /start ni bosing. Eslatma siz tanlagan vaqtda yuborildi.")
                    except Exception:
                        await redis.delete(marker)
                        log.warning("Could not send optional daily reminder to learner %s", learner.telegram_id)
            except Exception:
                log.exception("Learner reminder job failed")
        await asyncio.sleep(300)


async def start_bot_polling() -> None:
    # FastAPI lifespan creates/migrates the DB before Telegram handlers query it.
    await DATABASE_READY.wait()
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def main() -> None:
    global bot
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required. Copy .env.example to .env and configure it.")
    if not ADMIN_IDS:
        raise RuntimeError("Set at least one numeric Telegram ID in ADMIN_IDS.")
    api_token = os.getenv("ADMIN_API_TOKEN", "")
    if len(api_token) < 32 or api_token.isdigit() or api_token == "replace-with-long-random-hex":
        raise RuntimeError("Set ADMIN_API_TOKEN to a private random value at least 32 characters long; do not reuse a Telegram ID.")
    if not EXPECTED_BOT_USERNAME or EXPECTED_BOT_USERNAME == "your_english_bot_username":
        raise RuntimeError("Set EXPECTED_BOT_USERNAME to the username of the English course bot (without @).")
    bot = Bot(BOT_TOKEN)
    try:
        bot_identity = await bot.get_me()
    except Exception as exc:
        await bot.session.close()
        raise RuntimeError("Telegram bot token could not be verified. Check BOT_TOKEN in .env.") from exc
    actual_username = (bot_identity.username or "").lower()
    if actual_username != EXPECTED_BOT_USERNAME:
        await bot.session.close()
        raise RuntimeError(f"BOT_TOKEN belongs to @{actual_username}, not the configured English bot. Check BOT_TOKEN and EXPECTED_BOT_USERNAME.")
    log.info("Verified English course bot @%s", actual_username)
    host = os.getenv("APP_HOST", "127.0.0.1")
    port = int(os.getenv("APP_PORT", "8000"))
    try:
        http_socket = socket.create_server((host, port), backlog=128)
        http_socket.setblocking(False)
    except OSError as exc:
        await bot.session.close()
        raise RuntimeError(
            f"Admin API {host}:{port} portiga ulanmadi ({exc}). "
            "Port band bo‘lishi mumkin; .env dagi APP_PORT qiymatini, masalan 8001 qilib o‘zgartiring."
        ) from exc
    config = uvicorn.Config(api, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    reminder = asyncio.create_task(teacher_reminders())
    learner_reminder = asyncio.create_task(learner_daily_reminders())
    services = [
        asyncio.create_task(start_bot_polling()),
        asyncio.create_task(server.serve(sockets=[http_socket])),
    ]
    try:
        done, _ = await asyncio.wait(services, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in services:
            if not task.done():
                task.cancel()
        await asyncio.gather(*services, return_exceptions=True)
        reminder.cancel()
        learner_reminder.cancel()
        await asyncio.gather(reminder, learner_reminder, return_exceptions=True)
        if not http_socket._closed:
            http_socket.close()
        await bot.session.close()
        await redis.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
