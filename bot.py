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
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command, StateFilter
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
import speaking_ai

# certificate.py reportlab'ga bog'liq — u o'rnatilmagan bo'lsa ham bot ishlashi
# uchun importni kechiktiramiz (lazy).
try:
    from certificate import generate_certificate
except ImportError:
    generate_certificate = None
    logging.getLogger("repetitor_bot").warning(
        "reportlab o'rnatilmagan — sertifikat funksiyasi ishlamaydi. "
        "pip install -r requirements.txt"
    )

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

# Deep-link referral kodi faqat harf/raqam bo'lishi kerak (injeksionning oldini oladi)
REF_CODE_RE = re.compile(r"^[A-Z0-9]{4,16}$")


def get_admin_ids() -> set:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}


def is_admin(user_id: int) -> bool:
    ids = get_admin_ids()
    return bool(ids) and user_id in ids


class AdminGuardMiddleware(BaseMiddleware):
    """adm:* callback va /admin buyruqlarini faqat ADMIN_IDS ga ochadi."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        # Callback
        if isinstance(event, CallbackQuery) and event.data and event.data.startswith("adm:"):
            if not is_admin(user.id):
                await event.answer("⛔ Admin emas.", show_alert=True)
                return
        return await handler(event, data)





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
            # Xotira cheksiz o'smasligi uchun eski yozuvlarni tozalab turamiz
            if len(self._last_seen) > 5000:
                cutoff = now - self.min_interval * 2
                self._last_seen = {
                    k: v for k, v in self._last_seen.items() if v >= cutoff
                }
            last = self._last_seen.get(user.id, 0.0)
            if now - last < self.min_interval:
                if isinstance(event, CallbackQuery):
                    await event.answer("Iltimos, biroz kuting.", show_alert=False)
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
    "✅ To'g'ri! Ajoyib natija.",
    "✅ To'g'ri javob. Davom eting.",
    "✅ Zo'r! Xuddi shunday.",
    "✅ To'g'ri. Bilimingiz mustahkamlanmoqda.",
    "✅ Muvaffaqiyatli! Keyingisiga o'tamiz.",
]
WRONG_PHRASES = [
    "❌ Noto'g'ri. Xatodan o'rganamiz.",
    "❌ Deyarli to'g'ri edi. Keyingisida uddalaysiz.",
    "❌ Afsuski, noto'g'ri. Izohga e'tibor bering.",
    "❌ To'g'ri javob quyida — tahlilni o'qing.",
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

def load_skills(subject: str) -> dict:
    path = BASE_DIR / f"skills_{subject}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


SKILLS = {"en": load_skills("en"), "ru": load_skills("ru")}


def get_skill_units(subject: str, skill: str) -> list:
    data = SKILLS.get(subject) or {}
    return (data.get("skills") or {}).get(skill, {}).get("units") or []


def get_skill_unit(subject: str, skill: str, unit_id: str) -> Optional[dict]:
    for u in get_skill_units(subject, skill):
        if u["id"] == unit_id:
            return u
    return None

def load_stories(subject: str) -> dict:
    path = BASE_DIR / f"stories_{subject}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


STORIES = {"en": load_stories("en"), "ru": load_stories("ru")}
SRS_VOCAB = json.loads((BASE_DIR / "srs_vocab.json").read_text(encoding="utf-8")) if (BASE_DIR / "srs_vocab.json").exists() else {"en": [], "ru": []}




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
    skill_quiz = State()
    speaking_wait = State()
    teacher_video = State()
    srs_review = State()
    game_play = State()
    story = State()
    team_create = State()


# ---------- Klaviaturalar ----------

def subjects_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"subj:{code}")]
        for code, name in SUBJECT_NAMES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📚 Kursim / Grammatika", callback_data="menu:course")],
        [InlineKeyboardButton(text="🌟 Ko'nikmalar (Read · Listen · Speak)", callback_data="menu:skills")],
        [
            InlineKeyboardButton(text="🧠 SRS takrorlash", callback_data="menu:srs"),
            InlineKeyboardButton(text="🎯 Bepul mashq", callback_data="menu:practice"),
        ],
        [
            InlineKeyboardButton(text="🔥 Kunlik challenge", callback_data="menu:daily"),
            InlineKeyboardButton(text="🎮 Mini-o'yinlar", callback_data="menu:games"),
        ],
        [
            InlineKeyboardButton(text="📖 Hikoyalar", callback_data="menu:story"),
            InlineKeyboardButton(text="👥 Jamoalar", callback_data="menu:team"),
        ],
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="menu:stats"),
            InlineKeyboardButton(text="🏆 Reyting", callback_data="menu:top"),
        ],
        [
            InlineKeyboardButton(text="🏅 Nishonlar", callback_data="menu:badges"),
            InlineKeyboardButton(text="👥 Do'stni taklif qilish", callback_data="menu:referral"),
        ],
        [
            InlineKeyboardButton(text="📜 Sertifikat", callback_data="menu:cert"),
            InlineKeyboardButton(text="⭐ Premium", callback_data="menu:premium"),
        ],
        [InlineKeyboardButton(text="🔄 Tilni almashtirish", callback_data="menu:switch")],
    ]
    if CHANNEL_URL and "your_kun" not in CHANNEL_URL:
        rows.append([InlineKeyboardButton(text=CHANNEL_NAME, url=CHANNEL_URL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """Modul progressini vizual ko'rsatish: ▰▰▰▱▱..."""
    if total <= 0:
        return ""
    filled = max(0, min(width, round(width * done / total)))
    return "▰" * filled + "▱" * (width - filled)


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
        candidate = message.text.split(maxsplit=1)[1].strip().upper()
        if REF_CODE_RE.match(candidate):
            ref_code = candidate

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
            f"Yana xush kelibsiz, {esc(message.from_user.full_name)}! 👋\n\n"
            f"Unvon: {random_title_for(user.get('xp', 0))}\n"
            f"⭐ XP: {user.get('xp', 0)}  |  🔥 Streak: {user.get('streak', 0)} kun\n\n"
            "Bugun nimadan boshlaymiz?",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
        await state.set_state(QuizState.course_menu)
    else:
        await message.answer(
            f"Assalomu alaykum, {esc(message.from_user.full_name)}! 👋\n\n"
            "<b>Repetitor</b> — ingliz va rus tillarini bosqichma-bosqich "
            "o'rganish uchun o'quv platformasi.\n\n"
            "<b>Sizni nima kutmoqda:</b>\n"
            "🎬 Video darslar va nazariya\n"
            "✍️ Amaliy mashqlar va testlar\n"
            "🌟 Reading · Listening · Speaking ko'nikmalari\n"
            "🧠 SRS takrorlash — so'zlar xotirada mustahkam qoladi\n"
            "📊 Progress, reyting va kurs sertifikati\n\n"
            "Boshlash uchun qisqa <b>placement test</b> ishlaymiz (~3 daqiqa) — "
            "qaysi darajadan boshlashni aniqlaymiz.\n\n"
            "Qaysi tilni o'rganmoqchisiz?",
            reply_markup=subjects_kb(),
            parse_mode="HTML",
        )
        await state.set_state(QuizState.choosing_subject)


@router.callback_query(QuizState.choosing_subject, F.data.startswith("subj:"))
async def choose_subject_placement(callback: CallbackQuery, state: FSMContext):
    subject = callback.data.split(":")[1]
    if subject not in SUBJECT_NAMES:
        await callback.answer("Noto'g'ri tanlov.", show_alert=True)
        return
    await state.update_data(subject=subject, owner_id=callback.from_user.id)

    # Placement allaqachon o'tilgan bo'lsa — qayta test majburiy emas,
    # shunchaki tanlangan tilni saqlaymiz.
    existing = db.get_user_by_telegram(callback.from_user.id)
    if existing and existing.get("placement_done"):
        user_id = db.get_or_create_user(
            callback.from_user.id, callback.from_user.username, callback.from_user.full_name
        )
        level = existing.get("current_level") or "beginner"
        ensure_first_module_unlocked(user_id, subject, level)
        await state.clear()
        await state.set_state(QuizState.course_menu)
        await state.update_data(subject=subject, owner_id=callback.from_user.id)
        await callback.message.edit_text(
            f"{SUBJECT_NAMES[subject]} tanlandi.\n\n"
            f"Sizning darajangiz: {LEVEL_NAMES.get(level, level)}\n\n"
            "Asosiy menyu:",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

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
        f"Jami {len(quiz)} ta savol (~3 daqiqa).\n"
        "Natijaga qarab o'quv darajangiz aniqlanadi.\n"
        "Boshladik:"
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
    """Sessiya egasi tekshiruvi — begona odam boshqa user testiga aralasha olmaydi."""
    owner = data.get("owner_id")
    if owner is None:
        return False
    return int(owner) == int(user_id)


@router.callback_query(F.data.startswith("ans:"))
async def handle_mcq_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("quiz"):
        await callback.answer("Sessiya tugagan. /start bosing.", show_alert=True)
        return
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
    if not data.get("quiz"):
        await callback.answer("Sessiya tugagan. /start bosing.", show_alert=True)
        return
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
    accepted = esc((q.get("accepted_answers") or ["—"])[0])
    await safe_edit(
        callback.message,
        f"⏭ O'tkazib yuborildi.\nTo'g'ri javob: <b>{accepted}</b>\n💡 {esc(q.get('explanation', ''))}",
    )
    await advance_quiz(callback.message, state, current + 1, data["score"])
    await callback.answer()


def normalize_answer(text: str) -> str:
    return " ".join(text.strip().lower().split())


@router.message(
    StateFilter(
        QuizState.placement,
        QuizState.answering,
        QuizState.free_practice,
        QuizState.skill_quiz,
    ),
    F.text,
)
async def handle_text_answer(message: Message, state: FSMContext):
    # MUHIM: StateFilter ishlatilgan — bu handler faqat test holatlarida ishlaydi.
    # Aks holda u boshqa matnli handlerlarni (masalan, jamoa kodi) bloklab qo'yadi.
    data = await state.get_data()
    if not check_owner(data, message.from_user.id):
        return

    if len(message.text) > 500:
        await message.answer("Javob juda uzun. Iltimos, qisqaroq yozing.")
        return

    quiz, current = data.get("quiz"), data.get("current")
    if quiz is None or current is None or current >= len(quiz):
        return
    q = quiz[current]
    if q.get("type") != "fill_blank":
        return

    user_answer = normalize_answer(message.text)
    accepted = [normalize_answer(a) for a in q.get("accepted_answers", [])]
    is_correct = bool(accepted) and user_answer in accepted
    score = data["score"] + (1 if is_correct else 0)
    correct_option = esc((q.get("accepted_answers") or ["—"])[0])
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
    if not data.get("quiz"):
        await callback.answer("Sessiya tugagan. /start bosing.", show_alert=True)
        return
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
        if q.get("id") and not data.get("is_game"):
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
    quiz = data.get("quiz") or []
    await state.update_data(current=next_index, score=score, match=None)
    if next_index < len(quiz):
        await send_question(message, state)
        return
    try:
        if data.get("is_placement"):
            await finish_placement(message, state, score, len(quiz))
        elif data.get("is_module_quiz"):
            await finish_module_quiz(message, state, score, len(quiz))
        elif data.get("is_skill_quiz"):
            await finish_skill_quiz(message, state, score, len(quiz))
        elif data.get("is_game"):
            await finish_game_match(message, state)
        else:
            await finish_free_practice(message, state, score, len(quiz))
    except Exception as e:
        logger.exception("Quiz yakunlash xatosi: %s", e)
        try:
            await message.answer(
                f"⚠️ Test yakunlandi, lekin natijani saqlashda xato.\n"
                f"Ball: <b>{score}/{len(quiz)}</b>\n"
                f"/start bosing yoki admin bilan bog'laning.",
                parse_mode="HTML",
                reply_markup=main_menu_kb(),
            )
        except Exception:
            pass
        await state.clear()


async def finish_placement(message: Message, state: FSMContext, score: int, total: int):
    """Placement test yakuni — natija va daraja HAR DOIM ko'rsatiladi.

    Muhim: callback.message.from_user = BOT (foydalanuvchi emas).
    Shuning uchun owner_id faqat FSM state dan olinadi (javob handlerlarda allaqachon tekshirilgan).
    """
    data = await state.get_data()
    subject = data.get("subject") or "en"
    owner_id = data.get("owner_id")
    if owner_id is None:
        logger.error("Placement: owner_id yo'q — state yo'qolgan")
        await message.answer(
            "⚠️ Sessiya yo'qoldi. Iltimos /start bosing va testni qayta boshlang.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        return

    user_id = db.get_or_create_user(
        int(owner_id),
        data.get("username"),
        data.get("full_name"),
    )
    total = max(int(total or 1), 1)
    score = int(score or 0)
    pct = round(100 * score / total)

    if pct >= 75:
        level = "advanced"
        level_hint = "Yuqori daraja — murakkab mavzular siz uchun ochiq."
    elif pct >= 45:
        level = "intermediate"
        level_hint = "O'rta daraja — asoslarni mustahkamlab, yangi mavzularni o'rganasiz."
    else:
        level = "beginner"
        level_hint = "Boshlang'ich daraja — oddiy va tushunarli asoslardan boshlaymiz."

    level_title = LEVEL_NAMES.get(level, level)

    # AVVAL natijani yuboramiz (DB xatosi bo'lsa ham ko'rinadi)
    result_text = (
        "🎉 <b>Placement test tugadi!</b>\n\n"
        f"📊 Natija: <b>{score}/{total}</b> ({pct}%)\n"
        f"📍 Sizning darajangiz: <b>{level_title}</b>\n"
        f"💡 {level_hint}\n\n"
        f"⭐ +{XP_PLACEMENT} XP\n\n"
        "——————\n"
        "<b>Keyingi qadamlar:</b>\n"
        "1️⃣ «📚 Kursim / Grammatika» bo'limini oching\n"
        "2️⃣ Modul → video → nazariya → mashq\n"
        "3️⃣ Testdan o'tsangiz keyingi modul ochiladi"
    )
    await message.answer(result_text, parse_mode="HTML", reply_markup=main_menu_kb())

    try:
        db.set_placement_done(user_id, level)
        db.add_xp(user_id, XP_PLACEMENT)
        db.save_result(user_id, subject, "placement", level, score, total)
        ensure_first_module_unlocked(user_id, subject, level)
        if level in ("intermediate", "advanced"):
            for m in get_modules(subject, "beginner"):
                db.unlock_module(user_id, subject, m["id"])
                db.complete_module(user_id, subject, m["id"], 0, 0)
            if level == "advanced":
                for m in get_modules(subject, "intermediate"):
                    db.unlock_module(user_id, subject, m["id"])
                    db.complete_module(user_id, subject, m["id"], 0, 0)
        db.update_streak(user_id)
    except Exception as e:
        logger.exception("Placement DB xatosi: %s", e)

    await state.clear()
    await state.set_state(QuizState.course_menu)
    await state.update_data(subject=subject, owner_id=owner_id)



async def finish_skill_quiz(message: Message, state: FSMContext, score: int, total: int):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    skill = data.get("skill") or "reading"
    owner_id = data.get("owner_id")
    if not owner_id:
        await message.answer("Sessiya tugagan. /start", reply_markup=main_menu_kb())
        await state.clear()
        return
    user_id = db.get_or_create_user(int(owner_id), data.get("username"), data.get("full_name"))
    total = max(total, 1)
    pct = round(100 * score / total)
    db.save_result(user_id, subject, f"skill_{skill}", data.get("unit_id") or skill, score, total)
    db.add_xp(user_id, score * XP_PER_CORRECT)
    streak = db.update_streak(user_id)
    stars = "⭐" * min(5, max(1, pct // 20))
    await message.answer(
        f"🎉 Ko'nikma mashqi tugadi!\n\n"
        f"{stars}\n"
        f"Natija: <b>{score}/{total}</b> ({pct}%)\n"
        f"🔥 Streak: {streak}\n"
        f"+{score * XP_PER_CORRECT} XP\n\n"
        "Yana mashq: Ko'nikmalar menyusi.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    await state.clear()
    await state.set_state(QuizState.course_menu)
    await state.update_data(subject=subject, owner_id=owner_id)


async def finish_module_quiz(message: Message, state: FSMContext, score: int, total: int):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    module_id = data.get("module_id")
    owner_id = data.get("owner_id")
    if not owner_id or not module_id:
        logger.error("finish_module_quiz: state incomplete %s", data.keys())
        await message.answer("Sessiya tugagan. /start bosing.", reply_markup=main_menu_kb())
        await state.clear()
        return
    user_id = db.get_or_create_user(
        int(owner_id), data.get("username"), data.get("full_name")
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
        # Daraja progressi — o'quvchi yutuqni ko'rsin
        level_mods = get_modules(subject, level)
        prog = db.get_all_module_progress(user_id, subject)
        done_cnt = sum(
            1 for m in level_mods
            if prog.get(m["id"], {}).get("status") == "completed"
        )
        if level_mods:
            lines.append(
                f"📈 Daraja progressi: {progress_bar(done_cnt, len(level_mods))} "
                f"{done_cnt}/{len(level_mods)}"
            )
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


async def finish_game_match(message: Message, state: FSMContext):
    """Juftlik poygasi o'yinining yakuni — ball o'yin sifatida saqlanadi."""
    data = await state.get_data()
    subject = data.get("subject") or "en"
    owner_id = data.get("owner_id")
    if not owner_id:
        await message.answer("Sessiya tugagan. /start bosing.", reply_markup=main_menu_kb())
        await state.clear()
        return
    m = data.get("match") or {}
    errors = int(m.get("errors") or 0)
    pts = max(10, 50 - errors * 10)
    uid = db.get_or_create_user(int(owner_id), data.get("username"), data.get("full_name"))
    db.save_game_score(uid, "match", pts)
    db.add_team_xp(uid, pts)
    db.update_streak(uid)
    await message.answer(
        f"🏁 <b>Juftlik poygasi tugadi!</b>\n\n"
        f"Xatolar soni: {errors}\n"
        f"Qo'shilgan ball: <b>+{pts} XP</b>\n\n"
        f"Yana o'ynash uchun: 🎮 Mini-o'yinlar bo'limi.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    await state.clear()
    await state.set_state(QuizState.course_menu)
    await state.update_data(subject=subject, owner_id=owner_id)


async def finish_free_practice(message: Message, state: FSMContext, score: int, total: int):
    data = await state.get_data()
    subject = data.get("subject", "en")
    owner_id = data.get("owner_id")
    if not owner_id:
        await message.answer("Sessiya tugagan. /start bosing.", reply_markup=main_menu_kb())
        await state.clear()
        return
    user_id = db.get_or_create_user(
        int(owner_id), data.get("username"), data.get("full_name")
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
    mods = get_modules(subject, level)
    done = sum(1 for m in mods if progress.get(m["id"], {}).get("status") == "completed")
    total = len(mods)
    bar = progress_bar(done, total)
    await callback.message.edit_text(
        f"{LEVEL_NAMES[level]}\n\n"
        + (f"Progress: {bar} {done}/{total}\n\n" if total else "\n")
        + "🔒 — hali yopiq\n🔓 — ochiq\n✅ — tugatilgan\n\n"
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
    video_url = mod.get("video_url", "")
    video_title = mod.get("video_title", "Video dars")
    tv = db.get_teacher_video(subject, module_id)
    if tv:
        if tv.get("video_url"):
            video_url = tv["video_url"]
        if tv.get("title"):
            video_title = tv["title"]
        if tv.get("file_id"):
            try:
                await callback.message.answer_video(tv["file_id"], caption=f"🎬 {video_title}")
            except Exception:
                try:
                    await callback.message.answer_document(tv["file_id"], caption=f"🎬 {video_title}")
                except Exception:
                    pass
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
    data = await state.get_data()
    subject = data.get("subject") or "en"
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
    level_mods = get_modules(subject, stats["current_level"])
    if level_mods:
        progress = db.get_all_module_progress(user_id, subject)
        done = sum(
            1 for m in level_mods
            if progress.get(m["id"], {}).get("status") == "completed"
        )
        text += (
            f"\n📈 Daraja progressi:\n"
            f"{progress_bar(done, len(level_mods))} {done}/{len(level_mods)} modul\n"
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
            "Premium ulanish uchun administratorga murojaat qiling."
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
    if generate_certificate is None:
        await callback.message.edit_text(
            "📜 Sertifikat moduli hozircha mavjud emas "
            "(serverda reportlab o'rnatilmagan). Administratorga xabar bering.",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return
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
    # XAVFSIZLIK: premium faqat admin nazoratida. Oddiy foydalanuvchi
    # o'ziga premium yoqa olmaydi.
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Bu buyruq faqat administratorlar uchun.")
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        # Admin o'ziga premium yoqadi
        uid = db.get_or_create_user(message.from_user.id, None, None)
        db.set_premium(uid, 30)
        await message.answer("⭐ Sizga 30 kunlik Premium yoqildi.")
        return
    try:
        tg_id = int(parts[1])
        days = int(parts[2]) if len(parts) > 2 else 30
    except ValueError:
        await message.answer("Format: /premium <telegram_id> [kunlar]")
        return
    days = max(1, min(days, 3650))  # 1 kun – 10 yil oralig'ida
    uid = db.get_or_create_user(tg_id, None, None)
    db.set_premium(uid, days)
    await message.answer(f"⭐ Premium {days} kunga yoqildi (user_id={uid}).")




# ==================== ADMIN PANEL ====================

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Umumiy statistika", callback_data="adm:stats")],
        [InlineKeyboardButton(text="👥 O'quvchilar", callback_data="adm:users:0")],
        [InlineKeyboardButton(text="📚 Darsliklar (modullar)", callback_data="adm:curr:en")],
        [InlineKeyboardButton(text="🎤 Speaking navbati", callback_data="adm:speak:0")],
        [InlineKeyboardButton(text="🎬 Video dars biriktirish", callback_data="adm:vidhelp")],
        [InlineKeyboardButton(text="🔍 O'quvchi qidirish", callback_data="adm:search")],
        [InlineKeyboardButton(text="⭐ Premium / 📢 Broadcast", callback_data="adm:prem_help")],
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
    await state.clear()
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
    tg_ids = db.all_telegram_ids()
    ok = fail = 0
    total = len(tg_ids)
    status = await message.answer(f"📢 Yuborilmoqda: 0/{total}...")
    for i, tg_id in enumerate(tg_ids, 1):
        try:
            await bot.send_message(
                tg_id, f"📢 <b>Admin xabari</b>\n\n{esc(body)}", parse_mode="HTML"
            )
            ok += 1
        except Exception:
            fail += 1
        if i % 20 == 0:
            try:
                await status.edit_text(
                    f"📢 Yuborilmoqda: {i}/{total} (muvaffaqiyat: {ok}, xato: {fail})"
                )
            except Exception:
                pass
        # Telegram flood-limitidan qochish uchun
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Tugadi. Muvaffaqiyat: {ok}, xato: {fail}")




# ==================== KO'NIKMALAR: Reading / Listening / Speaking ====================

SKILL_KEYS = ("reading", "listening", "speaking")


def skills_menu_kb(subject: str) -> InlineKeyboardMarkup:
    data = SKILLS.get(subject) or {}
    skills = data.get("skills") or {}
    rows = []
    for key in SKILL_KEYS:
        sk = skills.get(key) or {}
        title = sk.get("title") or key
        rows.append([InlineKeyboardButton(
            text=title,
            callback_data=f"skill:{subject}:{key}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skill_units_kb(subject: str, skill: str) -> InlineKeyboardMarkup:
    rows = []
    for u in get_skill_units(subject, skill):
        lvl = u.get("level", "")
        emoji = {"beginner": "🟢", "intermediate": "🟡", "advanced": "🔴"}.get(lvl, "📘")
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {u['title'][:40]}",
            callback_data=f"skunit:{subject}:{skill}:{u['id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Ko'nikmalar", callback_data="menu:skills")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "menu:skills")
async def menu_skills(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject")
    if not subject:
        await callback.message.edit_text("Avval tilni tanlang:", reply_markup=subjects_kb())
        await state.set_state(QuizState.choosing_subject)
        await callback.answer()
        return
    await callback.message.edit_text(
        f"🌟 <b>Ko'nikmalar — {SUBJECT_NAMES[subject]}</b>\n\n"
        "📖 <b>Reading</b> — matn o'qib tushunish\n"
        "🎧 <b>Listening</b> — eshitib tushunish\n"
        "🎤 <b>Speaking</b> — ovozli javob + AI tahlil\n\n"
        "Bo'limni tanlang:",
        parse_mode="HTML",
        reply_markup=skills_menu_kb(subject),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:daily")
async def menu_daily(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    # Kunlik challenge: barcha foydalanuvchilar uchun bir xil (sanaga bog'liq)
    units = get_skill_units(subject, "speaking")
    if not units:
        units = get_skill_units(subject, "reading")
    if not units:
        await callback.answer("Hozircha challenge yo'q", show_alert=True)
        return
    rng = random.Random(f"{date.today().isoformat()}:{subject}")
    u = rng.choice(units)
    today_str = date.today().strftime("%d.%m.%Y")
    if "prompt" in u:
        text = (
            f"🔥 <b>Kunlik challenge — {today_str}</b>\n\n"
            f"🎤 {esc(u['title'])}\n\n{esc(u['prompt'])}\n\n"
            f"💡 {esc(u.get('tips', ''))}\n\n"
            "Ovozli xabar yuboring (🎤) — AI va o'qituvchi baholaydi."
        )
        await state.update_data(
            subject=subject,
            skill="speaking",
            unit_id=u["id"],
            owner_id=callback.from_user.id,
            username=callback.from_user.username,
            full_name=callback.from_user.full_name,
        )
        await state.set_state(QuizState.speaking_wait)
        await callback.message.edit_text(text, parse_mode="HTML")
    else:
        await callback.message.edit_text(
            f"🔥 Bugungi challenge: <b>{esc(u['title'])}</b>\nKo'nikmalar bo'limidan boshlang.",
            parse_mode="HTML",
            reply_markup=skills_menu_kb(subject),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("skill:"))
async def skill_open(callback: CallbackQuery, state: FSMContext):
    _, subject, skill = callback.data.split(":")
    data = SKILLS.get(subject) or {}
    sk = (data.get("skills") or {}).get(skill) or {}
    await state.update_data(subject=subject, skill=skill, owner_id=callback.from_user.id)
    await callback.message.edit_text(
        f"{sk.get('title', skill)}\n\n{sk.get('desc', '')}\n\nMavzu tanlang:",
        reply_markup=skill_units_kb(subject, skill),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("skunit:"))
async def skill_unit_open(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    subject, skill, unit_id = parts[1], parts[2], parts[3]
    unit = get_skill_unit(subject, skill, unit_id)
    if not unit:
        await callback.answer("Topilmadi", show_alert=True)
        return
    await state.update_data(
        subject=subject, skill=skill, unit_id=unit_id,
        owner_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )

    if skill == "speaking":
        text = (
            f"🎤 <b>{esc(unit['title'])}</b>\n\n"
            f"{esc(unit['prompt'])}\n\n"
            f"💡 <i>{esc(unit.get('tips', ''))}</i>\n\n"
            f"⏱ Tavsiya: {unit.get('min_seconds', 15)}–{unit.get('max_seconds', 90)} soniya\n\n"
            "Endi <b>ovozli xabar</b> yuboring (mikrofon tugmasi)."
        )
        await state.set_state(QuizState.speaking_wait)
        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer()
        return

    # Reading / Listening — show passage then quiz
    body = unit.get("passage") or unit.get("script") or ""
    header = "📖 Matn" if skill == "reading" else "🎧 Dialog / audio matn"
    note = ""
    if skill == "listening":
        note = "\n\n🔊 " + esc(unit.get("audio_note") or "Matnni o'qing yoki o'qituvchi audiosini tinglang.")
        # teacher audio override?
        tv = db.get_teacher_video(subject, unit_id)
        if tv and tv.get("file_id"):
            try:
                await callback.message.answer_voice(tv["file_id"], caption="O'qituvchi audiosi")
            except Exception:
                try:
                    await callback.message.answer_audio(tv["file_id"], caption="O'qituvchi audiosi")
                except Exception:
                    pass

    await callback.message.edit_text(
        f"<b>{esc(unit['title'])}</b>\n\n{header}:\n\n{esc(body)}{note}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Savollarga o'tish", callback_data=f"skquiz:{subject}:{skill}:{unit_id}")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"skill:{subject}:{skill}")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("skquiz:"))
async def skill_start_quiz(callback: CallbackQuery, state: FSMContext):
    _, subject, skill, unit_id = callback.data.split(":")
    unit = get_skill_unit(subject, skill, unit_id)
    if not unit or not unit.get("questions"):
        await callback.answer("Savollar yo'q", show_alert=True)
        return
    quiz = list(unit["questions"])
    random.shuffle(quiz)
    quiz = quiz[: min(5, len(quiz))]
    await state.update_data(
        subject=subject, skill=skill, unit_id=unit_id,
        quiz=quiz, current=0, score=0,
        is_placement=False, is_module_quiz=False, is_skill_quiz=True,
        owner_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    await state.set_state(QuizState.skill_quiz)
    await callback.message.edit_text(f"✍️ {esc(unit['title'])} — savollar\nOmad!")
    await send_question(callback.message, state)
    await callback.answer()


@router.message(QuizState.speaking_wait, F.voice)
async def handle_speaking_voice(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not check_owner(data, message.from_user.id):
        return
    subject = data.get("subject") or "en"
    unit_id = data.get("unit_id") or "unknown"
    skill = data.get("skill") or "speaking"
    unit = get_skill_unit(subject, skill, unit_id) or {}
    duration = int(message.voice.duration or 0)
    file_id = message.voice.file_id

    # Whisper (ixtiyoriy API)
    transcript = None
    tmp = BASE_DIR / "media" / "audio" / f"spk_{message.from_user.id}_{int(time.time()*1000)}.ogg"
    try:
        tg_file = await bot.get_file(file_id)
        tmp.parent.mkdir(parents=True, exist_ok=True)
        await bot.download_file(tg_file.file_path, destination=tmp)
        transcript = speaking_ai.transcribe_voice_file(str(tmp), language=subject if subject in ("en", "ru") else "en")
    except Exception as e:
        logger.warning("Voice download/whisper: %s", e)
    finally:
        # Vaqtinchalik faylni tozalaymiz (disk to'lib ketmasligi uchun)
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

    if transcript:
        score, feedback = speaking_ai.analyze_with_transcript(
            prompt=unit.get("prompt") or unit.get("title") or "Speaking task",
            duration_sec=duration,
            transcript=transcript,
            min_seconds=int(unit.get("min_seconds") or 15),
            max_seconds=int(unit.get("max_seconds") or 120),
            language=subject,
        )
    else:
        score, feedback = speaking_ai.analyze_speaking(
            prompt=unit.get("prompt") or unit.get("title") or "Speaking task",
            duration_sec=duration,
            min_seconds=int(unit.get("min_seconds") or 15),
            max_seconds=int(unit.get("max_seconds") or 120),
            language=subject,
        )

    user_id = db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    sid = db.save_speaking(
        user_id, subject, unit_id, file_id, duration, score, feedback
    )
    db.add_xp(user_id, max(5, score // 5))
    db.update_streak(user_id)

    await message.answer(
        f"🎤 Speaking qabul qilindi!\n\n"
        f"AI baho: <b>{score}/100</b>\n\n{esc(feedback)}\n\n"
        f"🆔 Ariza #{sid} — o'qituvchiga yuborildi.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )

    # O'qituvchilarga xabar
    for admin_id in get_admin_ids():
        try:
            name = esc(message.from_user.full_name or message.from_user.username or str(message.from_user.id))
            await bot.send_message(
                admin_id,
                f"🎤 <b>Yangi speaking</b> #{sid}\n"
                f"O'quvchi: {name} (tg <code>{message.from_user.id}</code>)\n"
                f"Til: {subject} | Mavzu: {esc(unit.get('title') or unit_id)}\n"
                f"Davomiylik: {duration}s | AI: {score}/100\n\n{esc(feedback)[:800]}",
                parse_mode="HTML",
            )
            await bot.send_voice(admin_id, file_id, caption=f"Speaking #{sid}")
        except Exception as e:
            logger.warning("Admin speaking notify fail %s: %s", admin_id, e)

    await state.clear()
    await state.set_state(QuizState.course_menu)
    await state.update_data(subject=subject, owner_id=message.from_user.id)


@router.message(QuizState.speaking_wait)
async def handle_speaking_not_voice(message: Message, state: FSMContext):
    await message.answer(
        "🎤 Iltimos, <b>ovozli xabar</b> yuboring (matn emas).\n"
        "Yoki /start bilan menyuga qayting.",
        parse_mode="HTML",
    )


# advance_quiz skill branch - patch needed in advance_quiz
# (handled below)

@router.callback_query(F.data.startswith("adm:speak"))
async def adm_speaking_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    items = db.list_speaking(limit=15, status="pending")
    if not items:
        items = db.list_speaking(limit=10)
    if not items:
        await callback.message.edit_text(
            "🎤 Speaking arizalari yo'q.",
            reply_markup=admin_main_kb(),
        )
        await callback.answer()
        return
    lines = ["🎤 <b>Speaking arizalari</b>\n"]
    rows = []
    for s in items:
        name = esc(s.get("full_name") or s.get("username") or "?")
        lines.append(
            f"#{s['id']} {name} | {s['subject']} | AI {s.get('ai_score')}/100 | {s.get('status')}"
        )
        rows.append([InlineKeyboardButton(
            text=f"#{s['id']} {name[:20]}",
            callback_data=f"adm:spk:{s['id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Admin", callback_data="adm:home")])
    await callback.message.edit_text(
        "\n".join(lines)[:3500],
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:spk:"))
async def adm_speaking_one(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    sid = int(callback.data.split(":")[2])
    s = db.get_speaking(sid)
    if not s:
        await callback.answer("Topilmadi", show_alert=True)
        return
    name = esc(s.get("full_name") or s.get("username") or "?")
    text = (
        f"🎤 Speaking #{sid}\n"
        f"O'quvchi: {name}\n"
        f"{s['subject']} / {esc(s['unit_id'])}\n"
        f"AI: {s.get('ai_score')}/100\n"
        f"Status: {s.get('status')}\n\n"
        f"{esc(s.get('ai_feedback') or '')}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Qabul", callback_data=f"adm:spok:{sid}:approved"),
            InlineKeyboardButton(text="🔄 Qayta", callback_data=f"adm:spok:{sid}:redo"),
        ],
        [InlineKeyboardButton(text="◀️ Ro'yxat", callback_data="adm:speak:0")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    if s.get("file_id"):
        try:
            await bot.send_voice(callback.from_user.id, s["file_id"], caption=f"#{sid}")
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data.startswith("adm:spok:"))
async def adm_speaking_review(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    _, _, sid_s, status = callback.data.split(":")
    sid = int(sid_s)
    db.review_speaking(sid, status, teacher_note=status)
    s = db.get_speaking(sid)
    await callback.answer(f"Status: {status}", show_alert=True)
    if s and s.get("telegram_id"):
        try:
            msg = "✅ Speaking qabul qilindi! Zo'r!" if status == "approved" else "🔄 Speakingni qayta yuboring — yaxshilash mumkin."
            await bot.send_message(s["telegram_id"], msg)
        except Exception:
            pass
    await callback.message.edit_text(
        f"Speaking #{sid} → {status}",
        reply_markup=admin_main_kb(),
    )


@router.callback_query(F.data == "adm:vidhelp")
async def adm_vid_help(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Ruxsat yo'q", show_alert=True)
        return
    await callback.message.edit_text(
        "🎬 <b>Video / audio dars biriktirish</b>\n\n"
        "Format:\n"
        "<code>/setvideo en en_b_01 https://youtube.com/...</code>\n"
        "yoki video/audio faylni yuborib caption:\n"
        "<code>video:en:en_b_01</code>\n\n"
        "Listening unit uchun ham: <code>video:en:en_list_01</code>",
        parse_mode="HTML",
        reply_markup=admin_main_kb(),
    )
    await callback.answer()


@router.message(Command("setvideo"))
async def cmd_setvideo(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 4:
        await message.answer("Format: /setvideo <en|ru> <module_id> <url>")
        return
    _, subject, module_id, url = parts
    db.set_teacher_video(subject, module_id, module_id, url, "", message.from_user.id)
    await message.answer(f"✅ Video biriktirildi: {subject}/{module_id}")


@router.message(F.video | F.audio | F.voice | F.document)
async def teacher_upload_media(message: Message, state: FSMContext):
    """O'qituvchi media yuklashi: caption = video:en:en_b_01"""
    if not is_admin(message.from_user.id):
        return
    cap = (message.caption or "").strip()
    if not cap.startswith("video:"):
        return
    parts = cap.split(":")
    if len(parts) < 3:
        await message.answer("Caption: video:en:module_id")
        return
    subject, module_id = parts[1], parts[2]
    file_id = None
    if message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    elif message.voice:
        file_id = message.voice.file_id
    elif message.document:
        file_id = message.document.file_id
    db.set_teacher_video(subject, module_id, module_id, "", file_id or "", message.from_user.id)
    await message.answer(f"✅ Media saqlandi: {subject}/{module_id}")




# ==================== SRS / Games / Story / Team ====================

@router.callback_query(F.data == "menu:srs")
async def menu_srs(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    uid = db.get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    words = SRS_VOCAB.get(subject) or SRS_VOCAB.get("en") or []
    db.srs_ensure_seed(uid, subject, words)
    due = db.srs_count_due(uid, subject)
    await state.update_data(subject=subject, owner_id=callback.from_user.id)
    await callback.message.edit_text(
        f"🧠 <b>SRS takrorlash</b> ({SUBJECT_NAMES.get(subject, subject)})\n\n"
        f"Bugun takrorlash kerak: <b>{due}</b> ta so'z\n\n"
        "Spaced repetition — zaif so'zlar tez-tez, mustahkamlar sekinroq chiqadi.\n"
        "Har kuni 5–10 daqiqa — uzoq muddatli xotira!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Boshlash", callback_data="srs:go")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="menu:back")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "srs:go")
async def srs_go(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    uid = db.get_or_create_user(callback.from_user.id, None, None)
    cards = db.srs_due_cards(uid, subject, limit=1)
    if not cards:
        await callback.message.edit_text(
            "✅ Bugun barcha kartalar tayyor! Ertaga qayting yoki yangi so'z o'rganing.",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return
    card = cards[0]
    await state.update_data(srs_word=card["word"], subject=subject, owner_id=callback.from_user.id)
    await state.set_state(QuizState.srs_review)
    await callback.message.edit_text(
        f"🧠 So'z:\n\n<b>{esc(card['word'])}</b>\n\n"
        f"Misoli: <i>{esc(card.get('example') or '')}</i>\n\n"
        "Bu so'zni eslaysizmi?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👁 Javobni ko'rish", callback_data="srs:show")],
        ]),
    )
    await callback.answer()


@router.callback_query(QuizState.srs_review, F.data == "srs:show")
async def srs_show(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    word = data.get("srs_word")
    uid = db.get_or_create_user(callback.from_user.id, None, None)
    cards = db.srs_due_cards(uid, subject, limit=20)
    card = next((c for c in cards if c["word"] == word), None)
    if not card:
        # fallback any
        card = {"word": word, "hint": "—", "example": ""}
    await callback.message.edit_text(
        f"<b>{esc(card.get('word', word))}</b>\n"
        f"💡 {esc(card.get('hint') or '')}\n"
        f"📝 {esc(card.get('example') or '')}\n\n"
        "Qanchalik oson esladingiz?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Qiyin", callback_data="srs:q:1"),
                InlineKeyboardButton(text="😐 O'rta", callback_data="srs:q:3"),
                InlineKeyboardButton(text="✅ Oson", callback_data="srs:q:5"),
            ]
        ]),
    )
    await callback.answer()


@router.callback_query(QuizState.srs_review, F.data.startswith("srs:q:"))
async def srs_quality(callback: CallbackQuery, state: FSMContext):
    q = int(callback.data.split(":")[2])
    data = await state.get_data()
    subject = data.get("subject") or "en"
    word = data.get("srs_word")
    uid = db.get_or_create_user(callback.from_user.id, None, None)
    if word:
        db.srs_review(uid, subject, word, q)
        db.add_xp(uid, 3 if q >= 3 else 1)
        db.add_team_xp(uid, 2)
    due = db.srs_count_due(uid, subject)
    if due > 0:
        await callback.answer("Saqlandi!")
        # next card
        callback.data = "srs:go"
        await srs_go(callback, state)
    else:
        await callback.message.edit_text(
            "🎉 Bugungi SRS tugadi! Ajoyib odat.",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        await state.set_state(QuizState.course_menu)
        await state.update_data(subject=subject, owner_id=callback.from_user.id)
        await callback.answer()


# ----- Mini-games -----
@router.callback_query(F.data == "menu:games")
async def menu_games(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    await state.update_data(subject=subject, owner_id=callback.from_user.id)
    await callback.message.edit_text(
        "🎮 <b>Mini-o'yinlar</b>\n\n"
        "O'yin orqali so'z takrorlash — bilimgizni mustahkamlang va XP yig'ing:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔤 So'z topish", callback_data="game:guess")],
            [InlineKeyboardButton(text="🔗 Juftlik poygasi", callback_data="game:match")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="menu:back")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "game:guess")
async def game_guess_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    words = list(SRS_VOCAB.get(subject) or SRS_VOCAB.get("en") or [])
    if len(words) < 4:
        await callback.answer("So'zlar kam", show_alert=True)
        return
    target = random.choice(words)
    opts = [target["word"]]
    while len(opts) < 4:
        w = random.choice(words)["word"]
        if w not in opts:
            opts.append(w)
    random.shuffle(opts)
    await state.update_data(
        game="guess", game_answer=target["word"], game_score=0, game_round=1,
        subject=subject, owner_id=callback.from_user.id,
    )
    await state.set_state(QuizState.game_play)
    rows = [[InlineKeyboardButton(text=o, callback_data=f"gans:{i}")] for i, o in enumerate(opts)]
    await state.update_data(game_opts=opts)
    await callback.message.edit_text(
        f"🔤 <b>So'z topish</b> (1/5)\n\n"
        f"Bu nima? 💡 <b>{esc(target.get('hint') or '?')}</b>\n"
        f"<i>{esc(target.get('example') or '')}</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(QuizState.game_play, F.data.startswith("gans:"))
async def game_guess_answer(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("game") != "guess":
        await callback.answer()
        return
    idx = int(callback.data.split(":")[1])
    opts = data.get("game_opts") or []
    ans = data.get("game_answer")
    score = int(data.get("game_score") or 0)
    rnd = int(data.get("game_round") or 1)
    subject = data.get("subject") or "en"
    choice = opts[idx] if 0 <= idx < len(opts) else ""
    if choice == ans:
        score += 10
        await callback.answer("✅ To'g'ri!")
    else:
        await callback.answer(f"❌ To'g'ri: {ans}", show_alert=True)
    if rnd >= 5:
        uid = db.get_or_create_user(callback.from_user.id, None, None)
        db.save_game_score(uid, "guess", score)
        db.add_team_xp(uid, score)
        await callback.message.edit_text(
            f"🏁 O'yin tugadi!\nBall: <b>{score}</b>",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        await state.set_state(QuizState.course_menu)
        await state.update_data(subject=subject, owner_id=callback.from_user.id)
        return
    # next round
    words = list(SRS_VOCAB.get(subject) or SRS_VOCAB.get("en") or [])
    target = random.choice(words)
    opts = [target["word"]]
    while len(opts) < 4:
        w = random.choice(words)["word"]
        if w not in opts:
            opts.append(w)
    random.shuffle(opts)
    await state.update_data(
        game_answer=target["word"], game_score=score, game_round=rnd + 1, game_opts=opts
    )
    rows = [[InlineKeyboardButton(text=o, callback_data=f"gans:{i}")] for i, o in enumerate(opts)]
    await callback.message.edit_text(
        f"🔤 <b>So'z topish</b> ({rnd+1}/5) | Ball: {score}\n\n"
        f"💡 <b>{esc(target.get('hint') or '?')}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "game:match")
async def game_match_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    words = list(SRS_VOCAB.get(subject) or SRS_VOCAB.get("en") or [])
    if len(words) < 4:
        await callback.answer("So'zlar kam", show_alert=True)
        return
    sample = random.sample(words, 4)
    pairs = [{"left": w["word"], "right": w.get("hint") or w["word"]} for w in sample]
    order = list(range(4))
    random.shuffle(order)
    await state.update_data(
        game="match", match_pairs=pairs, match_order=order, match_step=0,
        match_errors=0, subject=subject, owner_id=callback.from_user.id,
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
        quiz=[{"type": "matching", "question": "Juftlik poygasi", "pairs": pairs, "id": "game_match"}],
        current=0, score=0, is_placement=False, is_module_quiz=False, is_skill_quiz=False,
        is_game=True,
    )
    await state.set_state(QuizState.answering)
    await callback.message.edit_text("🔗 <b>Juftlik poygasi</b> — mos so'zni toping!", parse_mode="HTML")
    # reuse matching UI
    await state.update_data(match=dict(order=order, step=0, errors=0))
    from_msg = callback.message
    # send matching step manually
    left_pos = order[0]
    left_item = pairs[left_pos]["left"]
    right_options = [p["right"] for p in pairs]
    shuffled = list(enumerate(right_options))
    random.shuffle(shuffled)
    rows = [
        [InlineKeyboardButton(text=t[:64], callback_data=f"match:0:0:{op}")]
        for op, t in shuffled
    ]
    await from_msg.answer(
        f"🔗 Mos keltiring (1/4):\n\n<b>{esc(left_item)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


# ----- Story mode -----
@router.callback_query(F.data == "menu:story")
async def menu_story(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject = data.get("subject") or "en"
    stories = (STORIES.get(subject) or {}).get("stories") or []
    if not stories:
        await callback.answer("Hikoyalar yo'q", show_alert=True)
        return
    rows = [[InlineKeyboardButton(
        text=f"📖 {s['title'][:40]}",
        callback_data=f"story:{subject}:{s['id']}",
    )] for s in stories]
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="menu:back")])
    await callback.message.edit_text(
        "📖 <b>Hikoya rejimi</b>\n\nTanlovingiz hikoyani o'zgartiradi (A/B). O'qing va tanlang!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("story:"))
async def story_start(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) == 3:
        _, subject, sid = parts
        node_id = None
    else:
        _, subject, sid, node_id = parts[0], parts[1], parts[2], parts[3]
    stories = (STORIES.get(subject) or {}).get("stories") or []
    story = next((s for s in stories if s["id"] == sid), None)
    if not story:
        await callback.answer("Topilmadi", show_alert=True)
        return
    node_id = node_id or story["start"]
    node = story["nodes"].get(node_id)
    if not node:
        await callback.answer("Tugugun", show_alert=True)
        return
    await state.set_state(QuizState.story)
    await state.update_data(subject=subject, story_id=sid, owner_id=callback.from_user.id)
    choices = node.get("choices") or []
    if not choices:
        uid = db.get_or_create_user(callback.from_user.id, None, None)
        db.add_xp(uid, 15)
        db.add_team_xp(uid, 10)
        await callback.message.edit_text(
            f"{esc(node['text'])}\n\n⭐ +15 XP",
            parse_mode="HTML",
            reply_markup=main_menu_kb(),
        )
        await state.clear()
        await state.set_state(QuizState.course_menu)
        await state.update_data(subject=subject, owner_id=callback.from_user.id)
        await callback.answer()
        return
    rows = [[InlineKeyboardButton(
        text=c["label"][:60],
        callback_data=f"story:{subject}:{sid}:{c['next']}",
    )] for c in choices]
    await callback.message.edit_text(
        f"📖 <b>{esc(story['title'])}</b>\n\n{esc(node['text'])}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


# ----- Team challenge -----
@router.callback_query(F.data == "menu:team")
async def menu_team(callback: CallbackQuery, state: FSMContext):
    uid = db.get_or_create_user(callback.from_user.id, callback.from_user.username, callback.from_user.full_name)
    teams = db.user_teams(uid)
    lines = ["👥 <b>Jamoa challenge</b> (haftalik XP)\n"]
    if teams:
        lines.append("Sizning jamoalaringiz:")
        for t in teams:
            lines.append(f"• {esc(t.get('name') or t['code'])} — kod: <code>{t['code']}</code> | hafta XP: {t.get('weekly_xp',0)}")
    else:
        lines.append("Hali jamoada emassiz. Yaratíng yoki kod bilan qo'shiling!")
    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Jamoa yaratish", callback_data="team:create")],
            [InlineKeyboardButton(text="🔑 Kod bilan qo'shilish", callback_data="team:join")],
            [InlineKeyboardButton(text="🏆 Reyting", callback_data="team:top")],
            [InlineKeyboardButton(text="◀️ Orqaga", callback_data="menu:back")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == "team:create")
async def team_create(callback: CallbackQuery, state: FSMContext):
    uid = db.get_or_create_user(callback.from_user.id, None, None)
    name = (callback.from_user.first_name or "Team") + " jamoasi"
    code = db.create_team(uid, name)
    await callback.message.edit_text(
        f"✅ Jamoa yaratildi!\n\nKod: <code>{code}</code>\n\nDo'stlaringizga yuboring — ular «Kod bilan qo'shilish» orqali kiradi.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "team:join")
async def team_join_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(QuizState.team_create)
    await state.update_data(team_action="join", owner_id=callback.from_user.id)
    await callback.message.edit_text("Jamoa kodini yuboring (masalan <code>A1B2C3</code>):", parse_mode="HTML")
    await callback.answer()


@router.message(QuizState.team_create, F.text)
async def team_join_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("team_action") != "join":
        return
    uid = db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    name = db.join_team(uid, message.text.strip())
    if not name:
        await message.answer("Kod topilmadi. Qayta urinib ko'ring yoki /start.")
        return
    await message.answer(f"✅ «{esc(name)}» jamoasiga qo'shildingiz!", parse_mode="HTML", reply_markup=main_menu_kb())
    await state.clear()
    await state.set_state(QuizState.course_menu)


@router.callback_query(F.data == "team:top")
async def team_top(callback: CallbackQuery):
    rows = db.team_leaderboard(limit=10)
    if not rows:
        await callback.message.edit_text("Hali jamoalar yo'q.", reply_markup=main_menu_kb())
        await callback.answer()
        return
    lines = ["🏆 <b>Jamoalar reytingi (haftalik XP)</b>\n"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {esc(r.get('team_name') or r.get('code'))} — {r.get('weekly_xp', 0)} XP")
    await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=main_menu_kb())
    await callback.answer()



async def on_unhandled_error(event, exception):
    logger.exception(
        "Kutilmagan xatolik [%s]: %s",
        type(exception).__name__,
        exception,
    )
    # Foydalanuvchiga yumshoq xabar (imkon bo'lsa)
    try:
        bot = None
        if hasattr(event, "bot"):
            bot = event.bot
        chat_id = None
        if hasattr(event, "message") and event.message:
            chat_id = event.message.chat.id
        elif hasattr(event, "chat"):
            chat_id = event.chat.id
        if bot and chat_id:
            await bot.send_message(
                chat_id,
                "⚠️ Vaqtinchalik xato yuz berdi. /start bosing.",
            )
    except Exception:
        pass
    return True


async def main():
    db.init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    throttle = ThrottlingMiddleware()
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)
    admin_guard = AdminGuardMiddleware()
    dp.callback_query.middleware(admin_guard)
    dp.errors.register(on_unhandled_error)
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Bot ishga tushdi (structured curriculum)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
