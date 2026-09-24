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
from certificate import generate_certificate

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("repetitor_bot")

# Optional Sentry
SENTRY_DSN = os.getenv("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.1)
        logger.info("Sentry ulandi")
    except ImportError:
        logger.warning("sentry-sdk o'rnatilmagan — pip install sentry-sdk")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi. .env faylida yoki muhit o'zgaruvchisida "
        "BOT_TOKEN='...' ko'rsating (README.md'ga qarang)."
    )

# Marketing / kanal
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/your_kun_sozi_channel")
CHANNEL_NAME = os.getenv("CHANNEL_NAME", "📢 Kun so'zi kanali")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")  # masalan: MyRepetitorBot (deep link uchun)
FREE_PRACTICE_LIMIT = 3  # kuniga bepul mashq (premiumda cheksiz)


def get_admin_ids() -> set:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def is_admin(user_id: int) -> bool:
    ids = get_admin_ids()
    return bool(ids) and user_id in ids


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
    rows = [
        [InlineKeyboardButton(text="📖 Kursim / Darslar", callback_data="menu:course")],
        [InlineKeyboardButton(text="🎯 Bepul mashq", callback_data="menu:practice")],
        [InlineKeyboardButton(text="📊 Statistika", callback_data="menu:stats")],
        [
            InlineKeyboardButton(text="🏅 Nishonlar", callback_data="menu:badges"),
            InlineKeyboardButton(text="🏆 Reyting", callback_data="menu:top"),
        ],
        [InlineKeyboardButton(text="👥 Do'stni taklif qilish", callback_data="menu:referral")],
        [InlineKeyboardButton(text="📜 Sertifikat", callback_data="menu:cert")],
        [InlineKeyboardButton(text="⭐ Premium", callback_data="menu:premium")],
        [InlineKeyboardButton(text="🔄 Tilni almashtirish", callback_data="menu:switch")],
    ]
    if CHANNEL_URL and "your_kun" not in CHANNEL_URL:
        rows.insert(-1, [InlineKeyboardButton(text=CHANNEL_NAME, url=CHANNEL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    # Deep link: /start REFCODE
    ref_code = None
    if message.text and len(message.text.split()) > 1:
        ref_code = message.text.split(maxsplit=1)[1].strip().upper()

    user_id = db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        referral_code_used=ref_code,
    )
    # Referral bonus (agar yangi va kod to'g'ri)
    if ref_code:
        u = db.get_user_by_telegram(message.from_user.id)
        if u and u.get("referred_by"):
            db.apply_referral_bonus(u["referred_by"], user_id)

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

    # Rasm / audio (media/ papkasidan yoki URL)
    media_dir = BASE_DIR / "media"
    if q.get("image_file"):
        img_path = media_dir / "images" / q["image_file"]
        if img_path.exists():
            from aiogram.types import FSInputFile
            await message.answer_photo(FSInputFile(str(img_path)), caption=header)
        elif q.get("image_url"):
            await message.answer_photo(q["image_url"], caption=header)
    elif q.get("image_url"):
        try:
            await message.answer_photo(q["image_url"], caption=header)
        except Exception:
            pass
    if q.get("audio_file"):
        aud_path = media_dir / "audio" / q["audio_file"]
        if aud_path.exists():
            from aiogram.types import FSInputFile
            await message.answer_audio(FSInputFile(str(aud_path)), caption="🔊 Talaffuz")
    elif q.get("audio_url"):
        try:
            await message.answer_audio(q["audio_url"], caption="🔊 Talaffuz")
        except Exception:
            pass

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



# ---------- Referral / Premium / Certificate ----------

@router.callback_query(F.data == "menu:referral")
async def menu_referral(callback: CallbackQuery, state: FSMContext):
    user_id = db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    code = db.get_referral_code(user_id)
    stats = db.get_user_stats(user_id)
    link = f"https://t.me/{BOT_USERNAME}?start={code}" if BOT_USERNAME else f"Kod: {code}"
    text = (
        "👥 <b>Do'stni taklif qilish</b>\n\n"
        f"Sizning kodngiz: <code>{code}</code>\n"
        f"Taklif havolasi: {link}\n\n"
        f"Taklif qilganlar: <b>{stats['referral_count']}</b> kishi\n\n"
        "Do'stingiz shu havola orqali kirsа:\n"
        "• Sizga +50 XP va nishon\n"
        "• Unga +30 XP va maxsus nishon\n\n"
        "Kodni do'stlaringizga ulashing!"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:premium")
async def menu_premium(callback: CallbackQuery, state: FSMContext):
    user_id = db.get_or_create_user(callback.from_user.id, None, None)
    prem = db.is_premium(user_id)
    if prem:
        msg = (
            "⭐ <b>Premium faol!</b>\n\n"
            "Sizda:\n"
            "• Cheksiz bepul mashq\n"
            "• Sertifikat yuklab olish\n"
            "• Ustuvor yordam\n\n"
            "Rahmat! 💙"
        )
    else:
        msg = (
            "⭐ <b>Premium reja</b>\n\n"
            "Nima beradi:\n"
            "• Kuniga cheklovsiz mashq\n"
            "• Rasmiy PDF sertifikat\n"
            "• Qo'shimcha bonuslar\n\n"
            "Demo: /premium buyrug'i bilan o'zingizga yoqing.\n"
            "To'lov (Stars/Payme) keyingi versiyada."
        )
    await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "menu:cert")
async def menu_cert(callback: CallbackQuery, state: FSMContext):
    user_id = db.get_or_create_user(
        callback.from_user.id, callback.from_user.username, callback.from_user.full_name
    )
    data = await state.get_data()
    subject = data.get("subject", "en")
    stats = db.get_user_stats(user_id)
    level = stats["current_level"]
    mods = get_modules(subject, level)
    progress = db.get_all_module_progress(user_id, subject)
    completed = sum(1 for m in mods if progress.get(m["id"], {}).get("status") == "completed")
    need = len(mods)

    if need == 0 or completed < need:
        await callback.message.edit_text(
            f"📜 Sertifikat uchun joriy darajadagi barcha modullarni tugating.\n\n"
            f"Holat: {completed}/{need} modul\n"
            f"Daraja: {LEVEL_NAMES.get(level, level)}",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    name = callback.from_user.full_name or "Student"
    try:
        pdf_path = generate_certificate(name, subject, level, user_id)
        db.save_certificate(user_id, subject, level, str(pdf_path))
        from aiogram.types import FSInputFile
        await callback.message.answer_document(
            document=FSInputFile(str(pdf_path)),
            caption=f"🎓 Tabriklaymiz! {LEVEL_NAMES.get(level, level)} sertifikatingiz.",
        )
        await callback.message.answer("Asosiy menyu:", reply_markup=main_menu_kb())
    except Exception as e:
        logger.exception("Sertifikat xatosi: %s", e)
        await callback.message.edit_text(
            "Sertifikat yaratishda xato. Keyinroq urinib ko'ring.",
            reply_markup=main_menu_kb(),
        )
    await callback.answer()


@router.message(Command("premium"))
async def cmd_premium_admin(message: Message):
    parts = (message.text or "").split()
    if not is_admin(message.from_user.id):
        if len(parts) >= 2:
            await message.answer("Ruxsat yo'q.")
            return
    if len(parts) < 2:
        uid = db.get_or_create_user(message.from_user.id, None, None)
        db.set_premium(uid, 30)
        await message.answer("⭐ Sizga 30 kunlik Premium yoqildi (demo).")
        return
    try:
        tg_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else 30
    except ValueError:
        await message.answer("Format: /premium <telegram_id> [days]")
        return
    uid = db.get_or_create_user(tg_id, None, None)
    db.set_premium(uid, days)
    await message.answer(f"Premium {days} kun (user_id={uid}).")




# ==================== ADMIN PANEL ====================

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Umumiy statistika", callback_data="adm:stats")],
        [InlineKeyboardButton(text="👥 O'quvchilar", callback_data="adm:users:0")],
        [InlineKeyboardButton(text="📚 Darsliklar (modullar)", callback_data="adm:curr:en")],
        [InlineKeyboardButton(text="🔍 O'quvchi qidirish", callback_data="adm:search")],
        [InlineKeyboardButton(text="⭐ Premium berish", callback_data="adm:prem_help")],
        [InlineKeyboardButton(text="📢 Broadcast (yordam)", callback_data="adm:bc_help")],
        [InlineKeyboardButton(text="◀️ Foydalanuvchi menyusi", callback_data="menu:back")],
    ])


def admin_users_kb(offset: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:users:{max(0, offset-20)}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:users:{offset+20}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ Admin menyu", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_user_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Premium 30 kun", callback_data=f"adm:setprem:{uid}:30")],
        [InlineKeyboardButton(text="📍 Level: beginner", callback_data=f"adm:setlvl:{uid}:beginner")],
        [InlineKeyboardButton(text="📍 Level: intermediate", callback_data=f"adm:setlvl:{uid}:intermediate")],
        [InlineKeyboardButton(text="📍 Level: advanced", callback_data=f"adm:setlvl:{uid}:advanced")],
        [InlineKeyboardButton(text="🔓 Barcha beginner ochish (EN)", callback_data=f"adm:unlockall:{uid}:en:beginner")],
        [InlineKeyboardButton(text="🔓 Barcha beginner ochish (RU)", callback_data=f"adm:unlockall:{uid}:ru:beginner")],
        [InlineKeyboardButton(text="◀️ O'quvchilar", callback_data="adm:users:0")],
        [InlineKeyboardButton(text="🏠 Admin", callback_data="adm:home")],
    ])


def admin_curr_kb(subject: str) -> InlineKeyboardMarkup:
    other = "ru" if subject == "en" else "en"
    rows = [
        [InlineKeyboardButton(text=f"🔄 {SUBJECT_NAMES[other]}", callback_data=f"adm:curr:{other}")],
    ]
    for level in ("beginner", "intermediate", "advanced"):
        rows.append([InlineKeyboardButton(
            text=LEVEL_NAMES[level],
            callback_data=f"adm:mods:{subject}:{level}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_mods_kb(subject: str, level: str) -> InlineKeyboardMarkup:
    rows = []
    for m in get_modules(subject, level):
        n = len(m.get("exercises", []))
        rows.append([InlineKeyboardButton(
            text=f"{m.get('emoji','📘')} {m['title'][:35]} ({n})",
            callback_data=f"adm:mod:{subject}:{m['id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Darsliklar", callback_data=f"adm:curr:{subject}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat adminlar uchun.")
        return
    await state.clear()
    overview = db.admin_stats_overview()
    text = (
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{overview['users']}</b>\n"
        f"⭐ Premium: <b>{overview['premium']}</b>\n"
        f"📝 Testlar: <b>{overview['tests']}</b>\n"
        f"✅ Tugatilgan modullar: <b>{overview['completed_modules']}</b>\n"
        f"🟢 Bugun faol: <b>{overview['active_today']}</b>\n\n"
        "Quyidan bo'lim tanlang:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_main_kb())


@router.callback_query(F.data == "adm:home")
async def adm_home(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    overview = db.admin_stats_overview()
    text = (
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 {overview['users']} | ⭐ {overview['premium']} | "
        f"📝 {overview['tests']} | ✅ {overview['completed_modules']}\n"
        f"🟢 Bugun: {overview['active_today']}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=admin_main_kb())
    await callback.answer()


@router.callback_query(F.data == "adm:stats")
async def adm_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    overview = db.admin_stats_overview()
    # Curriculum counts
    lines = ["📊 <b>Batafsil statistika</b>\n"]
    lines.append(f"Foydalanuvchilar: {overview['users']}")
    lines.append(f"Premium: {overview['premium']}")
    lines.append(f"Jami testlar: {overview['tests']}")
    lines.append(f"Tugatilgan modullar: {overview['completed_modules']}")
    lines.append(f"Bugun faol: {overview['active_today']}\n")
    lines.append("<b>Curriculum:</b>")
    for subj, name in SUBJECT_NAMES.items():
        total_m = total_e = 0
        for lv in ("beginner", "intermediate", "advanced"):
            mods = get_modules(subj, lv)
            total_m += len(mods)
            total_e += sum(len(m.get("exercises", [])) for m in mods)
        lines.append(f"  {name}: {total_m} modul, {total_e} mashq")
    await callback.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:users:"))
async def adm_users(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    offset = int(callback.data.split(":")[2])
    users = db.list_users(limit=20, offset=offset)
    has_more = len(users) == 20
    if not users:
        await callback.message.edit_text(
            "O'quvchilar yo'q.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")]
            ]),
        )
        await callback.answer()
        return
    lines = [f"👥 <b>O'quvchilar</b> (offset {offset})\n"]
    rows = []
    for u in users:
        name = esc(u.get("full_name") or u.get("username") or str(u["telegram_id"]))
        prem = "⭐" if u.get("is_premium") else ""
        lines.append(
            f"• {name} {prem}\n"
            f"  id={u['id']} tg={u['telegram_id']} XP={u.get('xp',0)} "
            f"lvl={u.get('current_level','?')}"
        )
        rows.append([InlineKeyboardButton(
            text=f"👤 {name[:28]}",
            callback_data=f"adm:user:{u['id']}",
        )])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm:users:{max(0,offset-20)}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm:users:{offset+20}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")])
    await callback.message.edit_text(
        "\n".join(lines)[:3500],
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:user:"))
async def adm_user_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    uid = int(callback.data.split(":")[2])
    u = db.get_user_detail(uid)
    if not u:
        await callback.answer("Topilmadi", show_alert=True)
        return
    name = esc(u.get("full_name") or u.get("username") or "?")
    lines = [
        f"👤 <b>{name}</b>",
        f"DB id: {u['id']} | TG: <code>{u['telegram_id']}</code>",
        f"Username: @{esc(u.get('username') or '-')}",
        f"XP: {u.get('xp',0)} | Streak: {u.get('streak',0)}",
        f"Level: {u.get('current_level')} | Placement: {bool(u.get('placement_done'))}",
        f"Premium: {bool(u.get('is_premium'))} until {u.get('premium_until') or '-'}",
        f"Referral code: {u.get('referral_code')} | invited: {u.get('referral_count',0)}",
        f"Ro'yxat: {u.get('created_at')}",
        "",
        f"<b>Modullar ({len(u.get('modules',[]))}):</b>",
    ]
    for m in u.get("modules", [])[:15]:
        lines.append(
            f"  • {m['subject']}/{m['module_id']}: {m['status']} "
            f"({m.get('quiz_score',0)}/{m.get('quiz_total',0)})"
        )
    if u.get("recent_results"):
        lines.append("\n<b>So'nggi testlar:</b>")
        for r in u["recent_results"][:5]:
            lines.append(
                f"  • {r['subject']} {r['level']}: {r['score']}/{r['total']} ({r['created_at']})"
            )
    await callback.message.edit_text(
        "\n".join(lines)[:4000],
        parse_mode="HTML",
        reply_markup=admin_user_kb(uid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:setprem:"))
async def adm_set_prem(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    _, _, uid_s, days_s = callback.data.split(":")
    uid, days = int(uid_s), int(days_s)
    db.set_premium(uid, days)
    await callback.answer(f"Premium {days} kun berildi!", show_alert=True)
    # refresh
    callback.data = f"adm:user:{uid}"
    await adm_user_detail(callback)


@router.callback_query(F.data.startswith("adm:setlvl:"))
async def adm_set_lvl(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    uid, level = int(parts[2]), parts[3]
    db.set_user_level(uid, level)
    # unlock first module of that level for both subjects
    for subj in ("en", "ru"):
        mods = get_modules(subj, level)
        if mods:
            db.unlock_module(uid, subj, mods[0]["id"])
    await callback.answer(f"Level → {level}", show_alert=True)
    callback.data = f"adm:user:{uid}"
    await adm_user_detail(callback)


@router.callback_query(F.data.startswith("adm:unlockall:"))
async def adm_unlock_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    parts = callback.data.split(":")
    uid, subject, level = int(parts[2]), parts[3], parts[4]
    for m in get_modules(subject, level):
        db.unlock_module(uid, subject, m["id"])
        db.mark_video_watched(uid, subject, m["id"])
    await callback.answer(f"{subject}/{level} ochildi", show_alert=True)
    callback.data = f"adm:user:{uid}"
    await adm_user_detail(callback)


@router.callback_query(F.data.startswith("adm:curr:"))
async def adm_curriculum(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    subject = callback.data.split(":")[2]
    lines = [f"📚 <b>{SUBJECT_NAMES[subject]}</b> — darsliklar\n"]
    for level in ("beginner", "intermediate", "advanced"):
        mods = get_modules(subject, level)
        ex = sum(len(m.get("exercises", [])) for m in mods)
        lines.append(f"{LEVEL_NAMES[level]}: {len(mods)} modul, {ex} mashq")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_curr_kb(subject),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:mods:"))
async def adm_modules_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    _, _, subject, level = callback.data.split(":")
    mods = get_modules(subject, level)
    lines = [f"📘 <b>{LEVEL_NAMES[level]}</b> modullari\n"]
    for m in mods:
        n = len(m.get("exercises", []))
        lines.append(f"{m.get('emoji','')} {m['title']} — {n} mashq")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=admin_mods_kb(subject, level),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:mod:"))
async def adm_module_detail(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    _, _, subject, module_id = callback.data.split(":", 3)
    mod = get_module(subject, module_id)
    if not mod:
        await callback.answer("Topilmadi", show_alert=True)
        return
    level = get_level_of_module(subject, module_id) or "?"
    exercises = mod.get("exercises", [])
    lines = [
        f"{mod.get('emoji','📘')} <b>{esc(mod['title'])}</b>",
        f"ID: <code>{module_id}</code> | Level: {level}",
        f"Video: {mod.get('video_url', '-')[:60]}",
        f"Pass: {mod.get('pass_threshold', 70)}%",
        f"Mashqlar: <b>{len(exercises)}</b>\n",
    ]
    for i, ex in enumerate(exercises[:20], 1):
        q = esc(ex.get("question", "")[:50])
        t = ex.get("type", "mcq")
        media = ""
        if ex.get("image_file") or ex.get("image_url"):
            media = "🖼"
        if ex.get("audio_file") or ex.get("audio_url"):
            media += "🔊"
        lines.append(f"{i}. [{t}]{media} {q}")
    if len(exercises) > 20:
        lines.append(f"... va yana {len(exercises)-20} ta")
    lines.append("\n💡 Yangi mashq qo'shish: curriculum_*.json ni tahrirlang.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Modullar", callback_data=f"adm:mods:{subject}:{level}")],
        [InlineKeyboardButton(text="🏠 Admin", callback_data="adm:home")],
    ])
    await callback.message.edit_text("\n".join(lines)[:4000], parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "adm:search")
async def adm_search_help(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await state.set_state(None)  # clear
    await callback.message.edit_text(
        "🔍 <b>O'quvchi qidirish</b>\n\n"
        "Yuboring:\n"
        "• <code>/find 123456789</code> — telegram_id\n"
        "• <code>/find @username</code> — username\n"
        "• <code>/find id:5</code> — ichki DB id",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")]
        ]),
    )
    await callback.answer()


@router.message(Command("find"))
async def cmd_find(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Format: /find <telegram_id|@user|id:N>")
        return
    q = parts[1].strip()
    u = None
    if q.startswith("id:"):
        try:
            u = db.get_user_detail(int(q[3:]))
        except ValueError:
            pass
    elif q.isdigit():
        u = db.find_user_by_telegram(int(q))
        if u:
            u = db.get_user_detail(u["id"])
    else:
        u = db.find_user_by_username(q)
        if u:
            u = db.get_user_detail(u["id"])
    if not u:
        await message.answer("Topilmadi.")
        return
    name = esc(u.get("full_name") or u.get("username") or "?")
    text = (
        f"👤 <b>{name}</b>\n"
        f"id={u['id']} tg=<code>{u['telegram_id']}</code>\n"
        f"XP={u.get('xp')} level={u.get('current_level')} premium={bool(u.get('is_premium'))}"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_user_kb(u["id"]))


@router.callback_query(F.data == "adm:prem_help")
async def adm_prem_help(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.message.edit_text(
        "⭐ <b>Premium</b>\n\n"
        "1. O'quvchilar ro'yxatidan foydalanuvchini oching → «Premium 30 kun»\n"
        "2. Yoki: <code>/premium &lt;telegram_id&gt; [days]</code>\n"
        "3. O'zingizga: <code>/premium</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:bc_help")
async def adm_bc_help(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.message.edit_text(
        "📢 <b>Broadcast</b>\n\n"
        "Hozircha: <code>/broadcast Xabar matni</code>\n"
        "Barcha foydalanuvchilarga yuboriladi (sekin, flood limitcha).\n"
        "Faqat admin.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")]
        ]),
    )
    await callback.answer()


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Format: /broadcast Xabar matni")
        return
    body = parts[1]
    users = db.list_users(limit=500, offset=0)
    ok = fail = 0
    status = await message.answer(f"Yuborilmoqda: 0/{len(users)}...")
    for i, u in enumerate(users, 1):
        try:
            await bot.send_message(u["telegram_id"], f"📢 <b>Admin xabari</b>\n\n{esc(body)}", parse_mode="HTML")
            ok += 1
        except Exception:
            fail += 1
        if i % 20 == 0:
            try:
                await status.edit_text(f"Yuborilmoqda: {i}/{len(users)} (ok={ok}, fail={fail})")
            except Exception:
                pass
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Tugadi. Muvaffaqiyat: {ok}, xato: {fail}")



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
