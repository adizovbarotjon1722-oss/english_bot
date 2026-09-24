"""
bot.py — Repetitorlik Telegram bot (structured o'quv tizimi).
Ingliz va Rus tillari: placement test → darslar → video → nazariya → mashqlar → unlock.
"""

import asyncio
import html
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    TelegramObject,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("repetitor_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi. .env faylida yoki muhit o'zgaruvchisida "
        "BOT_TOKEN='...' ko'rsating (README.md'ga qarang)."
    )

QUESTIONS_PER_QUIZ = 5
PASS_DEFAULT = 70
MIN_SECONDS_BETWEEN_ACTIONS = 0.6
XP_PER_CORRECT = 10
XP_MODULE_COMPLETE = 50
XP_PLACEMENT = 30

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def esc(text: str | None) -> str:
    return html.escape(text or "")


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, min_interval: float = MIN_SECONDS_BETWEEN_ACTIONS):
        self.min_interval = min_interval
        self._last_seen: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            last = self._last_seen.get(user.id, 0.0)
            if now - last < self.min_interval:
                if isinstance(event, CallbackQuery):
                    await event.answer("Biroz asta 🙂", show_alert=False)
                return
            self._last_seen[user.id] = now
        return await handler(event, data)


SUBJECT_NAMES = {"en": "🇬🇧 Ingliz tili", "ru": "🇷🇺 Rus tili"}
LEVEL_NAMES = {
    "beginner": "🟢 Boshlang'ich",
    "intermediate": "🟡 O'rta",
    "advanced": "🔴 Qiyin",
}

TITLE_THRESHOLDS = [
    (0, "🌱 Yangi boshlovchi"),
    (50, "📖 O'quvchi"),
    (150, "🎯 Bilimdon"),
    (300, "🏅 Usta"),
    (500, "👑 Professor"),
]

STREAK_BADGES = {
    3: ("streak_3", "🔥 3 kunlik olov"),
    7: ("streak_7", "🔥🔥 7 kunlik seriya"),
    14: ("streak_14", "🔥🔥🔥 2 haftalik mustahkamlik"),
    30: ("streak_30", "🏆 30 kunlik chempion"),
}

TESTS_TAKEN_BADGES = {
    5: ("tests_5", "📚 5 ta test"),
    15: ("tests_15", "📚📚 15 ta test"),
    50: ("tests_50", "🎓 50 ta test — mehnatkash!"),
}

CORRECT_PHRASES = [
    "✅ To'g'ri! Zo'rsiz! 🎉",
    "✅ Ajoyib, davom eting! 💪",
    "✅ Bingo! Aynan shu! 🎯",
    "✅ Zo'r, miyangiz ishlayapti! 🧠✨",
    "✅ To'g'ri! Hech kim to'xtata olmaydi 🚀",
]
WRONG_PHRASES = [
    "❌ Yo'q, unaqa emas. Xatodan o'rganamiz! 📚",
    "❌ Deyarli! Keyingisida topasiz 💡",
    "❌ Bo'lmadi, lekin harakat muhim! 🙂",
    "❌ Afsuski noto'g'ri. Tushuntirishga qarang 👇",
]


def random_title_for(xp: int) -> str:
    title = TITLE_THRESHOLDS[0][1]
    for threshold, name in TITLE_THRESHOLDS:
        if xp >= threshold:
            title = name
    return title


# ---------- Curriculum yuklash ----------

BASE_DIR = Path(__file__).resolve().parent


def load_curriculum(subject: str) -> dict:
    path = BASE_DIR / f"curriculum_{subject}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CURRICULA = {
    "en": load_curriculum("en"),
    "ru": load_curriculum("ru"),
}


def get_modules(subject: str, level: str) -> List[dict]:
    levels = CURRICULA[subject]["levels"]
    if level not in levels:
        return []
    mods = levels[level]["modules"]
    return sorted(mods, key=lambda m: m["order"])


def get_module(subject: str, module_id: str) -> Optional[dict]:
    for level_data in CURRICULA[subject]["levels"].values():
        for m in level_data["modules"]:
            if m["id"] == module_id:
                return m
    return None


def get_level_of_module(subject: str, module_id: str) -> Optional[str]:
    for level_key, level_data in CURRICULA[subject]["levels"].items():
        for m in level_data["modules"]:
            if m["id"] == module_id:
                return level_key
    return None


def get_all_modules_flat(subject: str) -> List[tuple]:
    """[(level, module), ...] tartiblangan"""
    result = []
    for level_key in ("beginner", "intermediate", "advanced"):
        for m in get_modules(subject, level_key):
            result.append((level_key, m))
    return result


def ensure_first_module_unlocked(user_id: int, subject: str, level: str):
    mods = get_modules(subject, level)
    if mods:
        db.unlock_module(user_id, subject, mods[0]["id"])


# ---------- Placement test savollari (har ikkala tildan aralash) ----------

def build_placement_pool(subject: str) -> List[dict]:
    """Har darajadan bir nechta savol olib placement test yasaydi."""
    pool = []
    for level_key in ("beginner", "intermediate", "advanced"):
        for m in get_modules(subject, level_key):
            for ex in m.get("exercises", []):
                if ex.get("type") in ("mcq", "fill_blank"):
                    pool.append({**ex, "level": level_key, "module_id": m["id"]})
    random.shuffle(pool)
    # Har darajadan kamida 2 tadan olishga harakat
    selected = []
    for lv in ("beginner", "intermediate", "advanced"):
        lv_items = [p for p in pool if p["level"] == lv]
        selected.extend(lv_items[:3])
    random.shuffle(selected)
    return selected[:9] if selected else pool[:9]


class QuizState(StatesGroup):
    choosing_subject = State()
    placement = State()
    course_menu = State()
    module_view = State()
    theory = State()
    answering = State()
    free_practice = State()


# ---------- Klaviaturalar ----------

def subjects_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"subj:{code}")]
        for code, name in SUBJECT_NAMES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Kursim / Darslar", callback_data="menu:course")],
        [InlineKeyboardButton(text="🎯 Bepul mashq (Free practice)", callback_data="menu:practice")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="menu:stats")],
        [InlineKeyboardButton(text="🏅 Nishonlar", callback_data="menu:badges")],
        [InlineKeyboardButton(text="🏆 Reyting", callback_data="menu:top")],
        [InlineKeyboardButton(text="🔄 Tilni almashtirish", callback_data="menu:switch")],
    ])


def course_levels_kb(subject: str) -> InlineKeyboardMarkup:
    rows = []
    for code, name in LEVEL_NAMES.items():
        rows.append([InlineKeyboardButton(text=name, callback_data=f"lvl:{subject}:{code}")])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def modules_kb(user_id: int, subject: str, level: str) -> InlineKeyboardMarkup:
    mods = get_modules(subject, level)
    progress = db.get_all_module_progress(user_id, subject)
    rows = []
    for m in mods:
        st = progress.get(m["id"], {}).get("status", "locked")
        if st == "completed":
            prefix = "✅ "
        elif st in ("unlocked", "video_done"):
            prefix = "🔓 "
        else:
            prefix = "🔒 "
        rows.append([
            InlineKeyboardButton(
                text=f"{prefix}{m.get('emoji', '📘')} {m['title'][:40]}",
                callback_data=f"mod:{subject}:{m['id']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"menu:course")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def module_actions_kb(subject: str, module_id: str, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status in ("unlocked", "video_done", "completed"):
        rows.append([InlineKeyboardButton(text="🎬 Videoni ko'rish", callback_data=f"vid:{subject}:{module_id}")])
    if status in ("video_done", "completed"):
        rows.append([InlineKeyboardButton(text="📝 Nazariyani o'qish", callback_data=f"th:{subject}:{module_id}")])
        rows.append([InlineKeyboardButton(text="✍️ Mashg'ulot / Test", callback_data=f"ex:{subject}:{module_id}")])
    if status == "completed":
        rows.append([InlineKeyboardButton(text="🔁 Qayta ishlash", callback_data=f"ex:{subject}:{module_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Modullar ro'yxati", callback_data=f"backmods:{subject}:{module_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def answer_kb(options: list, q_index: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=opt[:64], callback_data=f"ans:{q_index}:{i}")]
        for i, opt in enumerate(options)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_kb(q_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data=f"skip:{q_index}")]
    ])


# ---------- Handlerlar ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    await state.clear()
    user = db.get_user_by_telegram(message.from_user.id)
    if user and user.get("placement_done"):
        await message.answer(
            f"Yana salom, {esc(message.from_user.full_name)}! 👋\n\n"
            f"Unvon: {random_title_for(user.get('xp', 0))}\n"
            f"XP: {user.get('xp', 0)} | Streak: {user.get('streak', 0)} kun\n\n"
            "Nima qilamiz?",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        await state.set_state(QuizState.course_menu)
    else:
        await message.answer(
            f"Salom, {esc(message.from_user.full_name)}! 👋\n\n"
            "Bu bot orqali <b>Ingliz</b> va <b>Rus</b> tillarini "
            "structured (bosqichma-bosqich) o'rganasiz.\n\n"
            "🎬 Video darslar → 📖 Nazariya → ✍️ Mashqlar → ✅ Test\n"
            "Oldingi modulni tugatmasangiz keyingisi ochilmaydi.\n\n"
            "Avval <b>placement test</b> ishlaymiz — qaysi darajadan boshlashni aniqlaymiz.\n"
            "Qaysi tilni tanlaysiz?",
            reply_markup=subjects_kb(),
            parse_mode="HTML",
        )
        await state.set_state(QuizState.choosing_subject)


@router.callback_query(QuizState.choosing_subject, F.data.startswith("subj:"))
async def choose_subject_placement(callback: CallbackQuery, state: FSMContext):
    subject = callback.data.split(":")[1]
    await state.update_data(subject=subject, owner_id=callback.from_user.id)
    pool = build_placement_pool(subject)
    if len(pool) < 3:
        # Fallback: darhol beginner
        user_id = db.get_or_create_user(
            callback.from_user.id, callback.from_user.username, callback.from_user.full_name
        )
        db.set_placement_done(user_id, "beginner")
        ensure_first_module_unlocked(user_id, subject, "beginner")
        await callback.message.edit_text(
            f"{SUBJECT_NAMES[subject]} tanlandi.\n"
            "Hozircha savollar kam — boshlang'ich darajadan boshlaymiz.\n\n"
            "Asosiy menyu:",
            reply_markup=main_menu_kb(),
        )
        await state.set_state(QuizState.course_menu)
        await callback.answer()
        return

    quiz = pool[: min(9, len(pool))]
    await state.update_data(
        quiz=quiz,
        current=0,
        score=0,
        is_placement=True,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    await state.set_state(QuizState.placement)
    await callback.message.edit_text(
        f"{SUBJECT_NAMES[subject]} — Placement test\n\n"
        f"Jami {len(quiz)} ta savol. Natijaga qarab darajangiz aniqlanadi.\n"
        "Tayyormisiz? Boshladik! 🚀"
    )
    await send_question(callback.message, state)
    await callback.answer()


async def send_question(message: Message, state: FSMContext):
    data = await state.get_data()
    quiz = data["quiz"]
    idx = data["current"]
    q = quiz[idx]
    qtype = q.get("type", "mcq")
    header = f"❓ Savol {idx + 1}/{len(quiz)}"

    if qtype == "fill_blank":
        await message.answer(
            f"{header}\n\n<b>{esc(q['question'])}</b>\n\n✍️ Javobingizni yozing:",
            parse_mode="HTML",
            reply_markup=skip_kb(idx),
        )
    elif qtype == "matching":
        pairs = q["pairs"]
        order = list(range(len(pairs)))
        random.shuffle(order)
        await state.update_data(match=dict(order=order, step=0, errors=0))
        await message.answer(f"{header}\n\n<b>{esc(q['question'])}</b>", parse_mode="HTML")
        await send_matching_step(message, state)
    else:
        await message.answer(
            f"{header}\n\n<b>{esc(q['question'])}</b>",
            parse_mode="HTML",
            reply_markup=answer_kb(q["options"], idx),
        )


async def send_matching_step(message: Message, state: FSMContext):
    data = await state.get_data()
    quiz, idx = data["quiz"], data["current"]
    q = quiz[idx]
    pairs = q["pairs"]
    m = data["match"]
    step = m["step"]
    left_pos = m["order"][step]
    left_item = pairs[left_pos]["left"]
    right_options = [p["right"] for p in pairs]
    shuffled = list(enumerate(right_options))
    random.shuffle(shuffled)
    rows = [
        [InlineKeyboardButton(text=text[:64], callback_data=f"match:{idx}:{step}:{orig_pos}")]
        for orig_pos, text in shuffled
    ]
    await message.answer(
        f"🔗 Mos keltiring ({step + 1}/{len(pairs)}):\n\n<b>{esc(left_item)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


def check_owner(data: dict, user_id: int) -> bool:
    return data.get("owner_id") == user_id


@router.callback_query(F.data.startswith("ans:"))
async def handle_mcq_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not check_owner(data, callback.from_user.id):
        await callback.answer("Bu sizning testingiz emas.", show_alert=True)
        return
    try:
        _, q_index_str, choice_str = callback.data.split(":")
        q_index, choice = int(q_index_str), int(choice_str)
    except (ValueError, AttributeError):
        await callback.answer("Noto'g'ri so'rov.", show_alert=False)
        return

    quiz, current = data.get("quiz"), data.get("current")
    if quiz is None or q_index != current:
        await callback.answer("Bu savol allaqachon javoblandi.", show_alert=False)
        return

    q = quiz[current]
    if not (0 <= choice < len(q.get("options", []))):
        await callback.answer("Noto'g'ri variant.", show_alert=False)
        return

    is_correct = choice == q["correct"]
    score = data["score"] + (1 if is_correct else 0)
    correct_option = esc(q["options"][q["correct"]])
    feedback = random.choice(CORRECT_PHRASES) if is_correct else (
        f"{random.choice(WRONG_PHRASES)}\nTo'g'ri javob: <b>{correct_option}</b>"
    )
    feedback += f"\n💡 {esc(q.get('explanation', ''))}"
    await safe_edit(callback.message, f"❓ {esc(q['question'])}\n\n{feedback}")

    # Seen mark
    if q.get("id"):
        uid = db.get_or_create_user(callback.from_user.id, None, None)
        db.mark_exercise_seen(uid, data.get("subject", "en"), q["id"])

    await advance_quiz(callback.message, state, current + 1, score)
    await callback.answer()


@router.callback_query(F.data.startswith("skip:"))
async def handle_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not check_owner(data, callback.from_user.id):
        await callback.answer("Bu sizning testingiz emas.", show_alert=True)
        return
    try:
        q_index = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer()
        return
    quiz, current = data.get("quiz"), data.get("current")
    if quiz is None or q_index != current:
        await callback.answer()
        return
    q = quiz[current]
    accepted = esc(q.get("accepted_answers", ["—"])[0])
    await safe_edit(
        callback.message,
        f"⏭ O'tkazib yuborildi.\nTo'g'ri javob: <b>{accepted}</b>\n💡 {esc(q.get('explanation', ''))}",
    )
    await advance_quiz(callback.message, state, current + 1, data["score"])
    await callback.answer()


def normalize_answer(text: str) -> str:
    return " ".join(text.strip().lower().split())


@router.message(F.text)
async def handle_text_answer(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state not in (
        QuizState.placement.state,
        QuizState.answering.state,
        QuizState.free_practice.state,
    ):
        return

    data = await state.get_data()
    if not check_owner(data, message.from_user.id):
        return

    quiz, current = data.get("quiz"), data.get("current")
    if quiz is None or current is None or current >= len(quiz):
        return
    q = quiz[current]
    if q.get("type") != "fill_blank":
        return

    user_answer = normalize_answer(message.text)
    accepted = [normalize_answer(a) for a in q.get("accepted_answers", [])]
    is_correct = user_answer in accepted
    score = data["score"] + (1 if is_correct else 0)
    correct_option = esc(q["accepted_answers"][0])
    feedback = random.choice(CORRECT_PHRASES) if is_correct else (
        f"{random.choice(WRONG_PHRASES)}\nTo'g'ri javob: <b>{correct_option}</b>"
    )
    feedback += f"\n💡 {esc(q.get('explanation', ''))}"
    await message.answer(feedback, parse_mode="HTML")

    if q.get("id"):
        uid = db.get_or_create_user(message.from_user.id, None, None)
        db.mark_exercise_seen(uid, data.get("subject", "en"), q["id"])

    await advance_quiz(message, state, current + 1, score)


@router.callback_query(F.data.startswith("match:"))
async def handle_matching_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not check_owner(data, callback.from_user.id):
        await callback.answer("Bu sizning testingiz emas.", show_alert=True)
        return
    try:
        _, q_index_str, step_str, choice_str = callback.data.split(":")
        q_index, step, choice_pos = int(q_index_str), int(step_str), int(choice_str)
    except (ValueError, AttributeError):
        await callback.answer("Noto'g'ri so'rov.", show_alert=False)
        return

    quiz, current = data.get("quiz"), data.get("current")
    m = data.get("match")
    if quiz is None or q_index != current or not m or step != m["step"]:
        await callback.answer("Bu qadam o'tib ketgan.", show_alert=False)
        return

    q = quiz[current]
    pairs = q["pairs"]
    left_pos = m["order"][step]
    is_correct = choice_pos == left_pos
    result_line = "✅ To'g'ri juftlik!" if is_correct else (
        f"❌ Noto'g'ri. To'g'ri: <b>{esc(pairs[left_pos]['left'])} — {esc(pairs[left_pos]['right'])}</b>"
    )
    await safe_edit(
        callback.message,
        f"🔗 {esc(pairs[left_pos]['left'])} — {esc(pairs[choice_pos]['right'])}\n\n{result_line}",
    )
    m["step"] += 1
    m["errors"] += 0 if is_correct else 1
    await state.update_data(match=m)

    if m["step"] < len(pairs):
        await send_matching_step(callback.message, state)
    else:
        all_correct = m["errors"] == 0
        score = data["score"] + (1 if all_correct else 0)
        summary = "🎉 Barcha juftliklar to'g'ri!" if all_correct else f"Xatolar: {m['errors']}"
        await callback.message.answer(summary)
        if q.get("id"):
            uid = db.get_or_create_user(callback.from_user.id, None, None)
            db.mark_exercise_seen(uid, data.get("subject", "en"), q["id"])
        await advance_quiz(callback.message, state, current + 1, score)
    await callback.answer()


async def safe_edit(message: Message, text: str):
    try:
        await message.edit_text(text, parse_mode="HTML")
    except TelegramBadRequest:
        logger.warning("edit_text muvaffaqiyatsiz")


async def advance_quiz(message: Message, state: FSMContext, next_index: int, score: int):
    data = await state.get_data()
    quiz = data["quiz"]
    await state.update_data(current=next_index, score=score, match=None)
    if next_index < len(quiz):
        await send_question(message, state)
    else:
        if data.get("is_placement"):
            await finish_placement(message, state, score, len(quiz))
        elif data.get("is_module_quiz"):
            await finish_module_quiz(message, state, score, len(quiz))
        else:
            await finish_free_practice(message, state, score, len(quiz))


async def finish_placement(message: Message, state: FSMContext, score: int, total: int):
    data = await state.get_data()
    subject = data["subject"]
    user_id = db.get_or_create_user(
        data["owner_id"], data.get("username"), data.get("full_name")
    )
    pct = round(100 * score / total) if total else 0

    if pct >= 75:
        level = "advanced"
    elif pct >= 45:
        level = "intermediate"
    else:
        level = "beginner"

    db.set_placement_done(user_id, level)
    db.add_xp(user_id, XP_PLACEMENT)
    ensure_first_module_unlocked(user_id, subject, level)
    # Intermediate/advanced uchun oldingi level modullarini ham ochib qo'yamiz (ixtiyoriy)
    if level in ("intermediate", "advanced"):
        for m in get_modules(subject, "beginner"):
            db.unlock_module(user_id, subject, m["id"])
            db.complete_module(user_id, subject, m["id"], 0, 0)  # skip qilingan deb belgilash
        if level == "advanced":
            for m in get_modules(subject, "intermediate"):
                db.unlock_module(user_id, subject, m["id"])
                db.complete_module(user_id, subject, m["id"], 0, 0)

    level_title = LEVEL_NAMES[level]
    await message.answer(
        f"🎉 Placement test tugadi!\n\n"
        f"Natija: <b>{score}/{total}</b> ({pct}%)\n"
        f"Sizning darajangiz: <b>{level_title}</b>\n\n"
        f"+{XP_PLACEMENT} XP\n\n"
        "Endi kurs bo'yicha darslarni boshlashingiz mumkin.\n"
        "Har bir modulda: video → nazariya → mashq/test.\n"
        "Testni muvaffaqiyatli topsangiz keyingi modul ochiladi.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    await state.clear()
    await state.set_state(QuizState.course_menu)
    await state.update_data(subject=subject, owner_id=data["owner_id"])


async def finish_module_quiz(message: Message, state: FSMContext, score: int, total: int):
    data = await state.get_data()
    subject = data["subject"]
    module_id = data["module_id"]
    user_id = db.get_or_create_user(
        data["owner_id"], data.get("username"), data.get("full_name")
    )
    mod = get_module(subject, module_id)
    threshold = mod.get("pass_threshold", PASS_DEFAULT) if mod else PASS_DEFAULT
    pct = round(100 * score / total) if total else 0
    level = get_level_of_module(subject, module_id) or "beginner"

    db.save_result(user_id, subject, mod["title"] if mod else module_id, level, score, total)
    streak = db.update_streak(user_id)
    db.add_xp(user_id, score * XP_PER_CORRECT)

    lines = [
        "🎉 Modul testi tugadi!",
        "",
        f"Natija: <b>{score}/{total}</b> ({pct}%)",
        f"Kerakli minimum: {threshold}%",
        f"🔥 Streak: {streak} kun",
    ]

    if pct >= threshold:
        db.complete_module(user_id, subject, module_id, score, total)
        db.add_xp(user_id, XP_MODULE_COMPLETE)
        db.increment_lessons_done(user_id)
        lines.append("")
        lines.append(f"✅ Modul muvaffaqiyatli yakunlandi! +{XP_MODULE_COMPLETE} XP")
        # Keyingi modulni ochish
        next_mod = find_next_module(subject, module_id)
        if next_mod:
            db.unlock_module(user_id, subject, next_mod["id"])
            lines.append(f"🔓 Yangi modul ochildi: <b>{esc(next_mod['title'])}</b>")
        else:
            # Level tugadi — keyingi level birinchi modulini och
            next_level = {"beginner": "intermediate", "intermediate": "advanced"}.get(level)
            if next_level:
                next_mods = get_modules(subject, next_level)
                if next_mods:
                    db.unlock_module(user_id, subject, next_mods[0]["id"])
                    lines.append(f"🆙 Keyingi daraja ochildi: {LEVEL_NAMES[next_level]}!")
    else:
        lines.append("")
        lines.append(
            f"❌ Hali yetarli emas. Qayta urinib ko'ring yoki nazariyani qayta o'qing.\n"
            f"Kamida {threshold}% kerak."
        )

    # Badges
    stats = db.get_user_stats(user_id)
    new_badges = []
    if stats["tests_taken"] == 1 and db.award_badge(user_id, "first_quiz"):
        new_badges.append("🎉 Birinchi test")
    if pct == 100 and db.award_badge(user_id, "perfect_score"):
        new_badges.append("💯 Mukammal natija")
    if streak in STREAK_BADGES:
        code, label = STREAK_BADGES[streak]
        if db.award_badge(user_id, code):
            new_badges.append(label)
    if stats["tests_taken"] in TESTS_TAKEN_BADGES:
        code, label = TESTS_TAKEN_BADGES[stats["tests_taken"]]
        if db.award_badge(user_id, code):
            new_badges.append(label)

    if new_badges:
        lines.append("")
        lines.append("🏅 <b>Yangi nishon(lar)!</b>")
        lines.extend(f"  • {b}" for b in new_badges)

    lines.append("")
    lines.append("Asosiy menyuga qaytish uchun tugmani bosing.")
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_menu_kb())
    await state.clear()
    await state.set_state(QuizState.course_menu)
    await state.update_data(subject=subject, owner_id=data["owner_id"])


def find_next_module(subject: str, current_id: str) -> Optional[dict]:
    flat = get_all_modules_flat(subject)
    for i, (_, m) in enumerate(flat):
        if m["id"] == current_id and i + 1 < len(flat):
            return flat[i + 1][1]
    return None


async def finish_free_practice(message: Message, state: FSMContext, score: int, total: int):
    data = await state.get_data()
    subject = data.get("subject", "en")
    user_id = db.get_or_create_user(
        data["owner_id"], data.get("username"), data.get("full_name")
    )
    pct = round(100 * score / total) if total else 0
    db.save_result(user_id, subject, "free_practice", data.get("level", "beginner"), score, total)
    db.add_xp(user_id, score * XP_PER_CORRECT)
    streak = db.update_streak(user_id)
    await message.answer(
        f"🎉 Mashq tugadi!\n\nNatija: <b>{score}/{total}</b> ({pct}%)\n"
        f"🔥 Streak: {streak} kun\n+{score * XP_PER_CORRECT} XP",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    await state.clear()
    await state.set_state(QuizState.course_menu)
    await state.update_data(subject=subject, owner_id=data["owner_id"])


# ---------- Menyu handlerlari ----------

@router.callback_query(F.data == "menu:back")
@router.callback_query(F.data == "menu:course")
async def menu_course(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject")
    if not subject:
        await callback.message.edit_text("Avval tilni tanlang:", reply_markup=subjects_kb())
        await state.set_state(QuizState.choosing_subject)
        await callback.answer()
        return
    await callback.message.edit_text(
        f"{SUBJECT_NAMES[subject]}\nQaysi darajani ko'ramiz?",
        reply_markup=course_levels_kb(subject),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lvl:"))
async def choose_level_view(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    subject, level = parts[1], parts[2]
    user_id = db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    # Agar hech qanday modul ochilmagan bo'lsa — birinchi modulni och
    progress = db.get_all_module_progress(user_id, subject)
    if not progress:
        ensure_first_module_unlocked(user_id, subject, level)

    await state.update_data(subject=subject, level=level, owner_id=callback.from_user.id)
    await callback.message.edit_text(
        f"{LEVEL_NAMES[level]}\n\n"
        "🔒 — hali yopiq\n🔓 — ochiq\n✅ — tugatilgan\n\n"
        "Modulni tanlang:",
        reply_markup=modules_kb(user_id, subject, level),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:"))
async def open_module(callback: CallbackQuery, state: FSMContext):
    _, subject, module_id = callback.data.split(":", 2)
    user_id = db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    status = db.get_module_status(user_id, subject, module_id)
    mod = get_module(subject, module_id)
    if not mod:
        await callback.answer("Modul topilmadi.", show_alert=True)
        return

    if status == "locked":
        await callback.answer(
            "Bu modul hali yopiq. Oldingi modulni muvaffaqiyatli yakunlang!",
            show_alert=True,
        )
        return

    await state.update_data(subject=subject, module_id=module_id, owner_id=callback.from_user.id)
    text = (
        f"{mod.get('emoji', '📘')} <b>{esc(mod['title'])}</b>\n\n"
        f"Holat: <b>{status}</b>\n\n"
        "Tartib:\n"
        "1️⃣ Videoni ko'ring va «Ko'rdim» bosing\n"
        "2️⃣ Nazariyani o'qing (yozma + og'zaki)\n"
        "3️⃣ Mashg'ulot/testni ishlang\n"
        "4️⃣ Minimal foizni topsangiz keyingi modul ochiladi"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=module_actions_kb(subject, module_id, status)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vid:"))
async def show_video(callback: CallbackQuery, state: FSMContext):
    _, subject, module_id = callback.data.split(":", 2)
    mod = get_module(subject, module_id)
    if not mod:
        await callback.answer("Modul topilmadi.", show_alert=True)
        return
    user_id = db.get_or_create_user(callback.from_user.id, None, None)
    video_url = mod.get("video_url", "")
    video_title = mod.get("video_title", "Video dars")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Videoni ko'rdim — ochish", callback_data=f"vdone:{subject}:{module_id}")],
        [InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"mod:{subject}:{module_id}")],
    ])
    await callback.message.edit_text(
        f"🎬 <b>{esc(video_title)}</b>\n\n"
        f"Havola: {video_url}\n\n"
        "Videoni tomosha qiling (yoki o'zingiz bilgan manbadan shu mavzuni o'rganing), "
        "keyin pastdagi tugmani bosing — mashg'ulotlar ochiladi.",
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=False,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vdone:"))
async def video_done(callback: CallbackQuery, state: FSMContext):
    _, subject, module_id = callback.data.split(":", 2)
    user_id = db.get_or_create_user(callback.from_user.id, None, None)
    db.mark_video_watched(user_id, subject, module_id)
    status = db.get_module_status(user_id, subject, module_id)
    await callback.message.edit_text(
        "✅ Video ko'rilgan deb belgilandi!\nEndi nazariya va mashg'ulotlarga o'tishingiz mumkin.",
        reply_markup=module_actions_kb(subject, module_id, status),
    )
    await callback.answer("Video qayd etildi!")


@router.callback_query(F.data.startswith("th:"))
async def show_theory(callback: CallbackQuery, state: FSMContext):
    _, subject, module_id = callback.data.split(":", 2)
    mod = get_module(subject, module_id)
    if not mod:
        await callback.answer("Modul topilmadi.", show_alert=True)
        return
    user_id = db.get_or_create_user(callback.from_user.id, None, None)
    status = db.get_module_status(user_id, subject, module_id)
    if status not in ("video_done", "completed"):
        await callback.answer("Avval videoni ko'ring!", show_alert=True)
        return

    theory = mod.get("theory", "Nazariya mavjud emas.")
    oral = mod.get("theory_oral", "")
    text = f"📖 <b>{esc(mod['title'])}</b>\n\n{theory}"
    if oral:
        text += f"\n\n🔊 <b>Og'zaki tushuntirish:</b>\n<i>{esc(oral)}</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Mashg'ulotga o'tish", callback_data=f"ex:{subject}:{module_id}")],
        [InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"mod:{subject}:{module_id}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("ex:"))
async def start_module_exercises(callback: CallbackQuery, state: FSMContext):
    _, subject, module_id = callback.data.split(":", 2)
    mod = get_module(subject, module_id)
    if not mod:
        await callback.answer("Modul topilmadi.", show_alert=True)
        return
    user_id = db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    status = db.get_module_status(user_id, subject, module_id)
    if status not in ("video_done", "completed"):
        await callback.answer("Avval videoni ko'ring va nazariyani o'qing!", show_alert=True)
        return

    exercises = mod.get("exercises", [])
    if not exercises:
        await callback.answer("Bu modulda hali mashqlar yo'q.", show_alert=True)
        return

    # Takrorlanmaslik: ko'rilmaganlarni afzal ko'r
    seen = db.get_seen_exercises(user_id, subject)
    unseen = [e for e in exercises if e.get("id") not in seen]
    if len(unseen) < min(QUESTIONS_PER_QUIZ, len(exercises)):
        # Hammasini ko'rgan — tozalab qayta
        db.clear_seen_for_module(user_id, subject, [e["id"] for e in exercises if e.get("id")])
        pool = exercises[:]
    else:
        pool = unseen if unseen else exercises[:]

    random.shuffle(pool)
    quiz = pool[: min(QUESTIONS_PER_QUIZ, len(pool))]
    await state.update_data(
        subject=subject,
        module_id=module_id,
        quiz=quiz,
        current=0,
        score=0,
        is_module_quiz=True,
        is_placement=False,
        owner_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    await state.set_state(QuizState.answering)
    await callback.message.edit_text(
        f"✍️ <b>{esc(mod['title'])}</b> — mashg'ulot\n"
        f"{len(quiz)} ta savol. Omad! 🚀",
        parse_mode="HTML",
    )
    await send_question(callback.message, state)
    await callback.answer()


@router.callback_query(F.data.startswith("backmods:"))
async def back_to_modules(callback: CallbackQuery, state: FSMContext):
    _, subject, module_id = callback.data.split(":", 2)
    level = get_level_of_module(subject, module_id) or "beginner"
    user_id = db.get_or_create_user(callback.from_user.id, None, None)
    await callback.message.edit_text(
        f"{LEVEL_NAMES[level]}\nModulni tanlang:",
        reply_markup=modules_kb(user_id, subject, level),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:practice")
async def free_practice_menu(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject", "en")
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"fp:{subject}:{code}")]
        for code, name in LEVEL_NAMES.items()
    ]
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="menu:back")])
    await callback.message.edit_text(
        "🎯 Bepul mashq — istalgan darajadan random savollar.\n"
        "Bu yerda progress bloklanmaydi, shunchaki mashq qilasiz.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fp:"))
async def start_free_practice(callback: CallbackQuery, state: FSMContext):
    _, subject, level = callback.data.split(":")
    pool = []
    for m in get_modules(subject, level):
        pool.extend(m.get("exercises", []))
    if not pool:
        await callback.answer("Bu darajada savollar yo'q.", show_alert=True)
        return
    user_id = db.get_or_create_user(callback.from_user.id, None, None)
    seen = db.get_seen_exercises(user_id, subject)
    unseen = [e for e in pool if e.get("id") not in seen]
    use = unseen if len(unseen) >= 3 else pool
    random.shuffle(use)
    quiz = use[: min(QUESTIONS_PER_QUIZ, len(use))]
    await state.update_data(
        subject=subject,
        level=level,
        quiz=quiz,
        current=0,
        score=0,
        is_module_quiz=False,
        is_placement=False,
        owner_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    await state.set_state(QuizState.free_practice)
    await callback.message.edit_text(f"🎯 Bepul mashq ({LEVEL_NAMES[level]})\nBoshladik!")
    await send_question(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "menu:stats")
async def menu_stats(callback: CallbackQuery, state: FSMContext):
    user_id = db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    stats = db.get_user_stats(user_id)
    title = random_title_for(stats["xp"])
    badge_count = len(db.get_user_badges(user_id))
    text = (
        f"📊 <b>Sizning statistikangiz</b>\n\n"
        f"{title}\n"
        f"⭐ XP: {stats['xp']}\n"
        f"🔥 Streak: {stats['streak']} kun\n"
        f"📝 Ishlangan testlar: {stats['tests_taken']}\n"
        f"✅ To'g'ri javoblar: {stats['total_score']}/{stats['total_questions']}\n"
        f"📚 Tugatilgan modullar: {stats['completed_modules']}\n"
        f"🏅 Nishonlar: {badge_count} ta\n"
        f"📍 Joriy daraja: {LEVEL_NAMES.get(stats['current_level'], stats['current_level'])}\n"
    )
    if stats["weak_areas"]:
        text += "\n⚠️ Kuchsiz tomonlar:\n"
        for w in stats["weak_areas"]:
            text += f"  • {w['subject']} — {w['section']}: {w['pct']}%\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:badges")
async def menu_badges(callback: CallbackQuery, state: FSMContext):
    user_id = db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    earned = set(db.get_user_badges(user_id))
    ALL = {
        "first_quiz": "🎉 Birinchi test",
        "perfect_score": "💯 Mukammal natija",
        **{c: l for c, l in STREAK_BADGES.values()},
        **{c: l for c, l in TESTS_TAKEN_BADGES.values()},
    }
    if not earned:
        await callback.message.edit_text(
            "Hali nishonlaringiz yo'q. Birinchi testni yakunlang! 🎉",
            reply_markup=main_menu_kb(),
        )
    else:
        lines = ["🏅 <b>Sizning nishonlaringiz:</b>", ""]
        for code, label in ALL.items():
            if code in earned:
                lines.append(f"✅ {label}")
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:top")
async def menu_top(callback: CallbackQuery, state: FSMContext):
    leaders = db.get_leaderboard()
    if not leaders:
        await callback.message.edit_text(
            "Hali hech kim test ishlamagan. Birinchi bo'ling! 🏆",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return
    text = "🏆 <b>Reyting (TOP-10, XP bo'yicha)</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(leaders):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = esc(row["full_name"] or row["username"] or "Foydalanuvchi")
        text += f"{medal} {name} — {row.get('xp', 0)} XP ({row['tests_taken']} test)\n"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:switch")
async def menu_switch(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Qaysi tilga o'tamiz?",
        reply_markup=subjects_kb(),
    )
    await state.set_state(QuizState.choosing_subject)
    await callback.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    stats = db.get_user_stats(user_id)
    title = random_title_for(stats["xp"])
    text = (
        f"📊 <b>Statistika</b>\n\n{title}\n"
        f"⭐ XP: {stats['xp']} | 🔥 {stats['streak']} kun\n"
        f"📝 Testlar: {stats['tests_taken']}\n"
        f"✅ {stats['total_score']}/{stats['total_questions']}\n"
        f"📚 Modullar: {stats['completed_modules']}"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("top"))
async def cmd_top(message: Message):
    leaders = db.get_leaderboard()
    if not leaders:
        await message.answer("Hali reyting bo'sh.")
        return
    text = "🏆 <b>TOP-10</b>\n\n"
    for i, row in enumerate(leaders):
        name = esc(row["full_name"] or row["username"] or "?")
        text += f"{i+1}. {name} — {row.get('xp', 0)} XP\n"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Buyruqlar:</b>\n"
        "/start — bosh menyu / placement\n"
        "/stats — statistika\n"
        "/top — reyting\n"
        "/help — yordam\n\n"
        "<b>Qanday ishlaydi:</b>\n"
        "1. Placement test → daraja aniqlanadi\n"
        "2. Modul ochiladi → video ko'rasiz\n"
        "3. Nazariya (yozma + og'zaki)\n"
        "4. Mashq/test → o'tsangiz keyingi modul ochiladi\n"
        "5. Takrorlanmaslik: savollar aralashib boradi",
        parse_mode="HTML",
    )


async def on_unhandled_error(event, exception):
    logger.exception("Kutilmagan xatolik: %s", exception)
    return True


async def main():
    db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    throttle = ThrottlingMiddleware()
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)
    dp.errors.register(on_unhandled_error)
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot ishga tushdi (structured curriculum)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
