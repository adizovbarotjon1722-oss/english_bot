"""
bot.py — Repetitorlik Telegram bot: to'liq kurs tizimi.

Duolingo va boshqa yetakchi platformalarning asosiy tamoyillari asosida qurilgan:
  - Har bir zamon/mavzu alohida MODUL (video + matnli/og'zaki tushuntirish + mashqlar)
  - Modullar ketma-ket ochiladi — o'quvchi sakrab o'tib keta olmaydi (mastery-based unlock)
  - Yangi o'quvchi uchun JOYLASHTIRISH TESTI — qaysi modulda boshlashini aniqlaydi
  - Mashqlar TAKRORLANMAYDI — har bir savol foydalanuvchi bo'yicha kuzatiladi
  - Xato qilingan savollarni alohida qayta mashq qilish (/review)
  - Nishonlar, unvonlar, streak, reyting — motivatsiya uchun

Ishga tushirish: README.md ga qarang.
"""

import asyncio
import html
import json
import logging
import os
import random
import time
from typing import Any, Awaitable, Callable, Dict

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

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

PRACTICE_QUESTIONS = 5
TEST_QUESTIONS = 5
REVIEW_QUESTIONS = 5
MIN_SECONDS_BETWEEN_ACTIONS = 0.7

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def esc(text) -> str:
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
                    await event.answer("Biroz asta — juda tez bosyapsiz 🙂", show_alert=False)
                return
            self._last_seen[user.id] = now
        return await handler(event, data)


SUBJECT_NAMES = {"en": "🇬🇧 Ingliz tili", "ru": "🇷🇺 Rus tili"}
SECTION_NAMES = {"grammar": "📘 Grammatika", "vocabulary": "📗 Lug'at (Vocabulary)"}

# ---------- O'yinlashtirish ----------

TITLE_THRESHOLDS = [
    (0, "🌱 Yangi boshlovchi"),
    (20, "📖 O'quvchi"),
    (50, "🎯 Bilimdon"),
    (100, "🏅 Usta"),
    (200, "👑 Professor"),
]

STREAK_BADGES = {
    3: ("streak_3", "🔥 3 kunlik olov"),
    7: ("streak_7", "🔥🔥 7 kunlik seriya"),
    14: ("streak_14", "🔥🔥🔥 2 haftalik mustahkamlik"),
    30: ("streak_30", "🏆 30 kunlik chempion"),
}
TESTS_TAKEN_BADGES = {
    10: ("tests_10", "📚 10 ta test bosqichi"),
    50: ("tests_50", "📚📚 50 ta test — chinakam mehnatkash!"),
    100: ("tests_100", "🎓 100 ta test — haqiqiy bilimdon!"),
}
COURSE_COMPLETE_BADGES = {
    ("en", "grammar"): ("course_complete_en_grammar", "🎓 Ingliz tili grammatikasi — kurs tugallandi!"),
    ("en", "vocabulary"): ("course_complete_en_vocabulary", "🎓 Ingliz tili lug'ati — kurs tugallandi!"),
    ("ru", "grammar"): ("course_complete_ru_grammar", "🎓 Rus tili grammatikasi — kurs tugallandi!"),
    ("ru", "vocabulary"): ("course_complete_ru_vocabulary", "🎓 Rus tili lug'ati — kurs tugallandi!"),
}

ALL_BADGE_LABELS = {
    "first_quiz": "🎉 Birinchi test",
    "perfect_score": "💯 Mukammal natija",
    **{code: label for code, label in STREAK_BADGES.values()},
    **{code: label for code, label in TESTS_TAKEN_BADGES.values()},
    **{code: label for code, label in COURSE_COMPLETE_BADGES.values()},
}

CORRECT_PHRASES = [
    "✅ To'g'ri! Zo'rsiz! 🎉",
    "✅ Ajoyib, xuddi shunday davom eting! 💪",
    "✅ Bingo! Aynan shu javob! 🎯",
    "✅ Zo'r, miyangiz bugun ishlayapti! 🧠✨",
    "✅ To'g'ri javob! Sizni hech kim to'xtata olmaydi 🚀",
]
WRONG_PHRASES = [
    "❌ Yo'q, unaqa emas. Ammo xatodan o'rganamiz! 📚",
    "❌ Deyarli! Keyingisida albatta topasiz 💡",
    "❌ Bo'lmadi, lekin harakat muhim! Davom etamiz 🙂",
    "❌ Afsuski noto'g'ri. Tushuntirishga qarang 👇",
]


def random_title_for(score: int) -> str:
    title = TITLE_THRESHOLDS[0][1]
    for threshold, name in TITLE_THRESHOLDS:
        if score >= threshold:
            title = name
    return title


# ---------- Kontent yuklash ----------

def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CURRICULUM = {"en": load_json("curriculum_en.json"), "ru": load_json("curriculum_ru.json")}
QUESTIONS = {"en": load_json("questions_en.json"), "ru": load_json("questions_ru.json")}

QUESTIONS_BY_MODULE = {}
ALL_QUESTIONS_BY_ID = {}
for subj, qs in QUESTIONS.items():
    QUESTIONS_BY_MODULE[subj] = {}
    for q in qs:
        QUESTIONS_BY_MODULE[subj].setdefault(q["module"], []).append(q)
        ALL_QUESTIONS_BY_ID[q["id"]] = q

for subj in CURRICULUM:
    for section in CURRICULUM[subj]:
        CURRICULUM[subj][section].sort(key=lambda m: m["order"])


def get_modules(subject: str, section: str):
    return CURRICULUM[subject][section]


def get_module_by_id(subject: str, section: str, module_id: str):
    for m in get_modules(subject, section):
        if m["id"] == module_id:
            return m
    return None


# ---------- Holatlar ----------

class Nav(StatesGroup):
    choosing_subject = State()
    choosing_section = State()
    placement_confirm = State()
    hub = State()
    quiz = State()


class AdminUpload(StatesGroup):
    waiting_video = State()
    waiting_audio = State()


# ---------- Klaviaturalar ----------

def subjects_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"subj:{code}")]
            for code, name in SUBJECT_NAMES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sections_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"sect:{code}")]
            for code, name in SECTION_NAMES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def placement_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Joylashtirish testi", callback_data="placement:yes")],
        [InlineKeyboardButton(text="▶️ 1-bo'limdan boshlash", callback_data="placement:no")],
    ])


def answer_kb(options, q_index: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=opt[:64], callback_data=f"ans:{q_index}:{i}")]
            for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_db_user(data: dict) -> int:
    return db.get_or_create_user(data["owner_id"], data.get("username"), data.get("full_name"))


def check_owner(data: dict, user_id: int) -> bool:
    return data.get("owner_id") == user_id


# ---------- /start va asosiy navigatsiya ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    await state.clear()
    await state.update_data(
        owner_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    await message.answer(
        f"Salom, {esc(message.from_user.full_name)}! 👋\n\n"
        "Bu — to'liq tuzilgan repetitorlik kursi: har bir zamon/mavzu video darslik, "
        "tushuntirish va mashqlar bilan, ketma-ket ochiladi.\n\n"
        "Qaysi tildan boshlaymiz?",
        reply_markup=subjects_kb(),
    )
    await state.set_state(Nav.choosing_subject)


@router.callback_query(Nav.choosing_subject, F.data.startswith("subj:"))
async def choose_subject(callback: CallbackQuery, state: FSMContext):
    subject = callback.data.split(":")[1]
    await state.update_data(subject=subject)
    await state.set_state(Nav.choosing_section)
    await callback.message.edit_text(
        f"{SUBJECT_NAMES[subject]} tanlandi.\nQaysi bo'limni o'rganamiz?",
        reply_markup=sections_kb(),
    )
    await callback.answer()


@router.callback_query(Nav.choosing_section, F.data.startswith("sect:"))
async def choose_section(callback: CallbackQuery, state: FSMContext):
    section = callback.data.split(":")[1]
    data = await state.update_data(section=section)
    subject = data["subject"]
    db_user = get_db_user(data)

    progress = db.get_course_progress(db_user, subject, section)
    if progress is None:
        await state.set_state(Nav.placement_confirm)
        num_modules = len(get_modules(subject, section))
        await callback.message.edit_text(
            f"{SECTION_NAMES[section]} tanlandi ({num_modules} ta bo'lim bor).\n\n"
            "Bu tilni/mavzuni sizga tanish darajasini bilamizmi? Joylashtirish testi "
            "orqali qaysi bo'limdan boshlashingiz kerakligini aniqlaymiz — yoki "
            "to'g'ridan-to'g'ri 1-bo'limdan boshlashingiz mumkin.",
            reply_markup=placement_kb(),
        )
    else:
        await state.set_state(Nav.hub)
        await render_hub(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(Nav.placement_confirm, F.data == "placement:no")
async def placement_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    db_user = get_db_user(data)
    db.ensure_course_progress(db_user, data["subject"], data["section"], start_order=0)
    await state.set_state(Nav.hub)
    await render_hub(callback.message, state, edit=True)
    await callback.answer()


@router.callback_query(Nav.placement_confirm, F.data == "placement:yes")
async def placement_start(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject, section = data["subject"], data["section"]
    modules = get_modules(subject, section)
    quiz_questions = []
    for m in modules:
        pool = QUESTIONS_BY_MODULE[subject].get(m["id"], [])
        if pool:
            quiz_questions.append(random.choice(pool))

    await state.update_data(
        quiz=quiz_questions, current=0, score=0, match=None,
        quiz_mode="placement", quiz_module_id=None,
    )
    await state.set_state(Nav.quiz)
    await callback.message.edit_text(
        "📝 Joylashtirish testi boshlandi! Bilganingizcha javob bering — "
        "bilmasangiz ham xavotir olmang, shunchaki taxmin qiling.\n\n"
        f"Jami savollar: {len(quiz_questions)}"
    )
    await send_question(callback.message, state)
    await callback.answer()


# ---------- Kurs markazi (hub) ----------

async def render_hub(message: Message, state: FSMContext, edit: bool = False):
    data = await state.get_data()
    subject, section = data["subject"], data["section"]
    db_user = get_db_user(data)
    progress = db.get_course_progress(db_user, subject, section)
    modules = get_modules(subject, section)
    num_modules = len(modules)
    unlocked_order = progress["unlocked_order"]
    section_complete = unlocked_order >= num_modules

    if section_complete:
        text = (
            f"🏆 <b>Tabriklaymiz!</b>\n\n"
            f"Siz {SUBJECT_NAMES[subject]} — {SECTION_NAMES[section]} bo'yicha "
            f"barcha {num_modules} ta bo'limni muvaffaqiyatli tugatdingiz! 🎓\n\n"
            f"Istalgan bo'limni erkin qayta mashq qilishingiz mumkin."
        )
        rows = [
            [InlineKeyboardButton(text=f"🔁 {m['title']}", callback_data=f"review_mod:{section}:{m['order']}")]
            for m in modules
        ]
        rows.append([InlineKeyboardButton(text="📊 Progress", callback_data="hub:progress")])
        rows.append([InlineKeyboardButton(text="🔁 Xatolarni takrorlash", callback_data="hub:review")])
        rows.append([InlineKeyboardButton(text="🔀 Boshqa til/bo'lim", callback_data="hub:switch")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    else:
        active = modules[min(unlocked_order, num_modules - 1)]
        media = db.get_module_media(subject, section, active["id"])
        video_required = bool(media["video_file_id"])
        watched = (not video_required) or db.is_video_watched(db_user, subject, section, active["id"])

        text = (
            f"{active['title']}\n"
            f"({active['order'] + 1}/{num_modules}-bo'lim)\n\n"
        )
        if video_required and not watched:
            text += "❗ Avval video darslikni ko'ring, keyin mashqlar ochiladi."
        else:
            text += "✅ Mashqlar va bo'lim testi ochiq."

        rows = []
        if video_required:
            label = "✅ Video ko'rildi" if watched else "🎥 Video darslikni ko'rish"
            rows.append([InlineKeyboardButton(text=label, callback_data="hub:video")])
        rows.append([InlineKeyboardButton(text="📖 Matnli tushuntirish", callback_data="hub:text")])
        if media["audio_file_id"]:
            rows.append([InlineKeyboardButton(text="🎧 Og'zaki tushuntirish", callback_data="hub:audio")])
        if watched:
            rows.append([InlineKeyboardButton(text="✏️ Mashq qilish", callback_data="hub:practice")])
            rows.append([InlineKeyboardButton(text="✅ Bo'lim testi", callback_data="hub:test")])
        rows.append([InlineKeyboardButton(text="📊 Progress", callback_data="hub:progress")])
        rows.append([InlineKeyboardButton(text="🔁 Xatolarni takrorlash", callback_data="hub:review")])
        rows.append([InlineKeyboardButton(text="🔀 Boshqa til/bo'lim", callback_data="hub:switch")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)

    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(Nav.hub, F.data == "hub:switch")
async def hub_switch(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(Nav.choosing_subject)
    await callback.message.edit_text("Qaysi tildan boshlaymiz?", reply_markup=subjects_kb())
    await callback.answer()


@router.callback_query(Nav.hub, F.data == "hub:text")
async def hub_text(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject, section = data["subject"], data["section"]
    modules = get_modules(subject, section)
    db_user = get_db_user(data)
    progress = db.get_course_progress(db_user, subject, section)
    active = modules[min(progress["unlocked_order"], len(modules) - 1)]
    await callback.message.answer(active["text_explanation"], parse_mode="HTML")
    await callback.answer()


@router.callback_query(Nav.hub, F.data == "hub:video")
async def hub_video(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject, section = data["subject"], data["section"]
    modules = get_modules(subject, section)
    db_user = get_db_user(data)
    progress = db.get_course_progress(db_user, subject, section)
    active = modules[min(progress["unlocked_order"], len(modules) - 1)]
    media = db.get_module_media(subject, section, active["id"])

    if not media["video_file_id"]:
        await callback.answer("Bu bo'lim uchun video hali qo'shilmagan.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ko'rdim, davom etaman", callback_data=f"video:watched:{active['id']}")]
    ])
    await callback.message.answer_video(media["video_file_id"], caption=active["title"], reply_markup=kb)
    await callback.answer()


@router.callback_query(Nav.hub, F.data.startswith("video:watched:"))
async def hub_video_watched(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_id = callback.data.split(":", 2)[2]
    db_user = get_db_user(data)
    db.mark_video_watched(db_user, data["subject"], data["section"], module_id)
    await callback.answer("Ajoyib! Endi mashqlar ochildi 🎉", show_alert=False)
    await render_hub(callback.message, state, edit=False)


@router.callback_query(Nav.hub, F.data == "hub:audio")
async def hub_audio(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    subject, section = data["subject"], data["section"]
    modules = get_modules(subject, section)
    db_user = get_db_user(data)
    progress = db.get_course_progress(db_user, subject, section)
    active = modules[min(progress["unlocked_order"], len(modules) - 1)]
    media = db.get_module_media(subject, section, active["id"])
    if not media["audio_file_id"]:
        await callback.answer("Bu bo'lim uchun audio hali qo'shilmagan.", show_alert=True)
        return
    await callback.message.answer_voice(media["audio_file_id"])
    await callback.answer()


@router.callback_query(Nav.hub, F.data == "hub:practice")
async def hub_practice(callback: CallbackQuery, state: FSMContext):
    await start_module_quiz(callback, state, mode="practice", count=PRACTICE_QUESTIONS)


@router.callback_query(Nav.hub, F.data == "hub:test")
async def hub_test(callback: CallbackQuery, state: FSMContext):
    await start_module_quiz(callback, state, mode="module_test", count=TEST_QUESTIONS)


@router.callback_query(Nav.hub, F.data.startswith("review_mod:"))
async def hub_review_module(callback: CallbackQuery, state: FSMContext):
    _, section, order_str = callback.data.split(":")
    data = await state.get_data()
    subject = data["subject"]
    module = get_modules(subject, section)[int(order_str)]
    await state.update_data(section=section)
    await start_module_quiz(callback, state, mode="practice", count=PRACTICE_QUESTIONS, module_override=module["id"])


async def start_module_quiz(callback: CallbackQuery, state: FSMContext, mode: str, count: int, module_override=None):
    data = await state.get_data()
    subject, section = data["subject"], data["section"]
    db_user = get_db_user(data)
    modules = get_modules(subject, section)
    progress = db.get_course_progress(db_user, subject, section)
    active = get_module_by_id(subject, section, module_override) if module_override else \
        modules[min(progress["unlocked_order"], len(modules) - 1)]

    pool = QUESTIONS_BY_MODULE[subject].get(active["id"], [])
    if not pool:
        await callback.answer("Bu bo'limda hozircha savol yo'q.", show_alert=True)
        return

    seen = db.get_seen_question_ids(db_user)
    unseen = [q for q in pool if q["id"] not in seen]
    already_seen = [q for q in pool if q["id"] in seen]
    random.shuffle(unseen)
    random.shuffle(already_seen)
    chosen = (unseen + already_seen)[:count]
    random.shuffle(chosen)

    await state.update_data(
        quiz=chosen, current=0, score=0, match=None,
        quiz_mode=mode, quiz_module_id=active["id"],
    )
    await state.set_state(Nav.quiz)
    label = "✏️ Mashq" if mode == "practice" else "✅ Bo'lim testi"
    await callback.message.edit_text(f"{label}: {active['title']}\n\nBoshladik! 🚀")
    await send_question(callback.message, state)
    await callback.answer()


@router.callback_query(Nav.hub, F.data == "hub:progress")
@router.message(Command("progress"))
async def cmd_progress(event, state: FSMContext):
    is_cb = isinstance(event, CallbackQuery)
    message = event.message if is_cb else event
    tg_user = event.from_user

    data = await state.get_data()
    if "owner_id" not in data:
        db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
        await state.update_data(owner_id=tg_user.id, username=tg_user.username, full_name=tg_user.full_name)
        data = await state.get_data()
    db_user = get_db_user(data)

    lines = ["📊 <b>Sizning kurs progressingiz</b>"]
    for subject in ("en", "ru"):
        lines.append(f"\n{SUBJECT_NAMES[subject]}")
        for section in ("grammar", "vocabulary"):
            modules = get_modules(subject, section)
            progress = db.get_course_progress(db_user, subject, section)
            unlocked = progress["unlocked_order"] if progress else -1
            lines.append(f"  <i>{SECTION_NAMES[section]}</i>")
            for m in modules:
                if progress is None:
                    icon = "⚪"
                elif m["order"] < unlocked:
                    icon = "✅"
                elif m["order"] == unlocked and unlocked < len(modules):
                    icon = "🔓"
                else:
                    icon = "🔒"
                lines.append(f"    {icon} {m['title']}")
    text = "\n".join(lines)

    if is_cb:
        await message.answer(text, parse_mode="HTML")
        await event.answer()
    else:
        await message.answer(text, parse_mode="HTML")


@router.callback_query(Nav.hub, F.data == "hub:review")
@router.message(Command("review"))
async def cmd_review(event, state: FSMContext):
    is_cb = isinstance(event, CallbackQuery)
    message = event.message if is_cb else event
    tg_user = event.from_user

    data = await state.get_data()
    if "owner_id" not in data:
        db.get_or_create_user(tg_user.id, tg_user.username, tg_user.full_name)
        await state.update_data(
            owner_id=tg_user.id, username=tg_user.username, full_name=tg_user.full_name,
            subject=data.get("subject", "en"), section=data.get("section", "grammar"),
        )
        data = await state.get_data()
    db_user = get_db_user(data)

    mistaken_ids = db.get_mistaken_question_ids(db_user, limit=REVIEW_QUESTIONS)
    quiz_questions = [ALL_QUESTIONS_BY_ID[qid] for qid in mistaken_ids if qid in ALL_QUESTIONS_BY_ID]

    if not quiz_questions:
        text = "Xato qilingan savollaringiz yo'q — ajoyib natija! 🎉 Davom eting."
        if is_cb:
            await event.answer(text, show_alert=True)
        else:
            await message.answer(text)
        return

    await state.update_data(
        quiz=quiz_questions, current=0, score=0, match=None,
        quiz_mode="review", quiz_module_id=None,
    )
    await state.set_state(Nav.quiz)
    await message.answer(f"🔁 Xatolarni takrorlash: {len(quiz_questions)} ta savol. Boshladik!")
    await send_question(message, state)
    if is_cb:
        await event.answer()


# ---------- Kvíz dvigateli (mcq / fill_blank / matching) ----------

async def send_question(message: Message, state: FSMContext):
    data = await state.get_data()
    quiz = data["quiz"]
    idx = data["current"]
    q = quiz[idx]
    qtype = q.get("type", "mcq")
    header = f"❓ Savol {idx + 1}/{len(quiz)}:"

    if qtype == "fill_blank":
        skip_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data=f"skip:{idx}")]
        ])
        await message.answer(
            f"{header}\n\n<b>{esc(q['question'])}</b>\n\n✍️ Javobingizni yozib yuboring:",
            parse_mode="HTML", reply_markup=skip_kb,
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
            parse_mode="HTML", reply_markup=answer_kb(q["options"], idx),
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
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        f"🔗 Mos keltiring ({step + 1}/{len(pairs)}):\n\n<b>{esc(left_item)}</b>",
        parse_mode="HTML", reply_markup=kb,
    )


@router.callback_query(Nav.quiz, F.data.startswith("ans:"))
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

    quiz, current = data["quiz"], data["current"]
    if q_index != current:
        await callback.answer("Bu savol allaqachon javoblandi.", show_alert=False)
        return
    q = quiz[current]
    if not (0 <= choice < len(q["options"])):
        await callback.answer("Noto'g'ri javob varianti.", show_alert=False)
        return

    is_correct = choice == q["correct"]
    score = data["score"] + (1 if is_correct else 0)
    db.record_seen_question(get_db_user(data), q["id"], is_correct)

    correct_option = esc(q["options"][q["correct"]])
    feedback = random.choice(CORRECT_PHRASES) if is_correct else (
        f"{random.choice(WRONG_PHRASES)}\nTo'g'ri javob: <b>{correct_option}</b>"
    )
    feedback += f"\n💡 {esc(q['explanation'])}"
    await safe_edit(callback.message, f"❓ {esc(q['question'])}\n\n{feedback}")

    await advance_quiz(callback.message, state, current + 1, score)
    await callback.answer()


@router.callback_query(Nav.quiz, F.data.startswith("skip:"))
async def handle_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not check_owner(data, callback.from_user.id):
        await callback.answer("Bu sizning testingiz emas.", show_alert=True)
        return
    try:
        q_index = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Noto'g'ri so'rov.", show_alert=False)
        return
    quiz, current = data["quiz"], data["current"]
    if q_index != current:
        await callback.answer()
        return
    q = quiz[current]
    db.record_seen_question(get_db_user(data), q["id"], False)
    accepted = esc(q["accepted_answers"][0])
    await safe_edit(
        callback.message,
        f"⏭ O'tkazib yuborildi.\nTo'g'ri javob: <b>{accepted}</b>\n💡 {esc(q['explanation'])}",
    )
    await advance_quiz(callback.message, state, current + 1, data["score"])
    await callback.answer()


def normalize_answer(text: str) -> str:
    return " ".join(text.strip().lower().split())


@router.message(Nav.quiz, F.text)
async def handle_fill_blank_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if not check_owner(data, message.from_user.id):
        return
    quiz, current = data["quiz"], data["current"]
    q = quiz[current]
    if q.get("type") != "fill_blank":
        return

    user_answer = normalize_answer(message.text)
    accepted = [normalize_answer(a) for a in q["accepted_answers"]]
    is_correct = user_answer in accepted
    db.record_seen_question(get_db_user(data), q["id"], is_correct)

    score = data["score"] + (1 if is_correct else 0)
    correct_option = esc(q["accepted_answers"][0])
    feedback = random.choice(CORRECT_PHRASES) if is_correct else (
        f"{random.choice(WRONG_PHRASES)}\nTo'g'ri javob: <b>{correct_option}</b>"
    )
    feedback += f"\n💡 {esc(q['explanation'])}"
    await message.answer(feedback, parse_mode="HTML")
    await advance_quiz(message, state, current + 1, score)


@router.callback_query(Nav.quiz, F.data.startswith("match:"))
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

    quiz, current = data["quiz"], data["current"]
    m = data.get("match")
    if q_index != current or not m or step != m["step"]:
        await callback.answer("Bu qadam allaqachon o'tildi.", show_alert=False)
        return

    q = quiz[current]
    pairs = q["pairs"]
    left_pos = m["order"][step]
    is_correct = choice_pos == left_pos

    result_line = "✅ To'g'ri juftlik!" if is_correct else (
        f"❌ Noto'g'ri. To'g'ri juftlik: <b>{esc(pairs[left_pos]['left'])} — "
        f"{esc(pairs[left_pos]['right'])}</b>"
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
        db.record_seen_question(get_db_user(data), q["id"], all_correct)
        score = data["score"] + (1 if all_correct else 0)
        summary = "🎉 Barcha juftliklar to'g'ri!" if all_correct else f"Xatolar soni: {m['errors']}"
        await callback.message.answer(summary)
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
        await finish_quiz(message, state, score, len(quiz))


async def finish_quiz(message: Message, state: FSMContext, score: int, total: int):
    data = await state.get_data()
    subject, section = data["subject"], data["section"]
    mode = data["quiz_mode"]
    module_id = data.get("quiz_module_id")
    db_user = get_db_user(data)

    stats_before = db.get_user_stats(db_user)
    title_before = random_title_for(stats_before["total_score"])
    is_first_quiz = stats_before["tests_taken"] == 0

    db.save_result(db_user, subject, section, module_id or mode, score, total)
    streak = db.update_streak(db_user)

    stats_after = db.get_user_stats(db_user)
    title_after = random_title_for(stats_after["total_score"])
    pct = round(100 * score / total) if total else 0

    lines = ["🎉 Yakunlandi!", "", f"Natija: <b>{score}/{total}</b> ({pct}%)", f"🔥 Streak: {streak} kun"]

    new_badges = []
    if is_first_quiz and db.award_badge(db_user, "first_quiz"):
        new_badges.append("🎉 Birinchi test")
    if pct == 100 and db.award_badge(db_user, "perfect_score"):
        new_badges.append("💯 Mukammal natija")
    if streak in STREAK_BADGES:
        code, label = STREAK_BADGES[streak]
        if db.award_badge(db_user, code):
            new_badges.append(label)
    if stats_after["tests_taken"] in TESTS_TAKEN_BADGES:
        code, label = TESTS_TAKEN_BADGES[stats_after["tests_taken"]]
        if db.award_badge(db_user, code):
            new_badges.append(label)

    if mode == "placement":
        modules = get_modules(subject, section)
        num_modules = len(modules)
        ratio = score / total if total else 0
        target_order = round(ratio * (num_modules - 1))
        target_order = max(0, min(target_order, num_modules - 1))
        db.set_placement_done(db_user, subject, section, target_order)
        lines.append("")
        lines.append(f"📍 Siz <b>{modules[target_order]['title']}</b> dan boshlaysiz!")

    elif mode == "module_test":
        module = get_module_by_id(subject, section, module_id)
        threshold = module.get("pass_threshold", 0.7)
        if total and score / total >= threshold:
            modules = get_modules(subject, section)
            new_order = module["order"] + 1
            db.advance_module(db_user, subject, section, new_order)
            lines.append("")
            if new_order >= len(modules):
                combo_code, combo_label = COURSE_COMPLETE_BADGES[(subject, section)]
                if db.award_badge(db_user, combo_code):
                    new_badges.append(combo_label)
                lines.append("🏆 Bo'lim testidan o'tdingiz — va butun kursni tugatdingiz!! 🎓")
            else:
                lines.append(f"🔓 Tabriklaymiz! Keyingi bo'lim ochildi: <b>{modules[new_order]['title']}</b>")
        else:
            lines.append("")
            lines.append(
                f"Hali yetarli emas (kerak: {round(threshold*100)}%). "
                "Video/matnni qayta ko'rib chiqib, yana urinib ko'ring 💪"
            )

    if new_badges:
        lines.append("")
        lines.append("🏅 <b>Yangi nishon(lar)!</b>")
        lines.extend(f"  • {b}" for b in new_badges)

    if title_after != title_before:
        lines.append("")
        lines.append(f"🆙 Yangi unvon: <b>{title_after}</b>! Tabriklaymiz! 🎊")

    lines.append("")
    lines.append("Davom etish uchun quyidagi menyudan foydalaning 👇")

    await message.answer("\n".join(lines), parse_mode="HTML")
    await state.set_state(Nav.hub)
    await render_hub(message, state, edit=False)


# ---------- Statistika, nishonlar, reyting ----------

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    stats = db.get_user_stats(user_id)
    title = random_title_for(stats["total_score"])
    badge_count = len(db.get_user_badges(user_id))
    text = (
        f"📊 <b>Sizning statistikangiz</b>\n\n"
        f"{title}\n"
        f"🔥 Streak: {stats['streak']} kun\n"
        f"📝 Ishlangan testlar: {stats['tests_taken']}\n"
        f"✅ To'g'ri javoblar: {stats['total_score']}/{stats['total_questions']}\n"
        f"🏅 Nishonlar: {badge_count} ta (/badges bilan ko'ring)\n"
        f"📈 Progress: /progress buyrug'i bilan ko'ring\n"
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("badges"))
async def cmd_badges(message: Message):
    user_id = db.get_or_create_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    earned = set(db.get_user_badges(user_id))
    if not earned:
        await message.answer("Hali nishonlaringiz yo'q. Birinchi testni yakunlab, birinchi nishoningizni oling! 🎉")
        return
    lines = ["🏅 <b>Sizning nishonlaringiz:</b>", ""]
    for code, label in ALL_BADGE_LABELS.items():
        if code in earned:
            lines.append(f"✅ {label}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("top"))
async def cmd_top(message: Message):
    leaders = db.get_leaderboard()
    if not leaders:
        await message.answer("Hali hech kim test ishlamagan. Birinchi bo'ling! 🏆")
        return
    text = "🏆 <b>Reyting jadvali — barcha vaqt (TOP-10)</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(leaders):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = esc(row["full_name"] or row["username"] or "Foydalanuvchi")
        text += f"{medal} {name} — {row['total_score']} ball ({row['tests_taken']} test)\n"
    text += "\nHaftalik reyting uchun /weektop yozing."
    await message.answer(text, parse_mode="HTML")


@router.message(Command("weektop"))
async def cmd_weektop(message: Message):
    leaders = db.get_weekly_leaderboard()
    if not leaders:
        await message.answer("Bu hafta hali hech kim test ishlamagan. Birinchi bo'ling! 🏆")
        return
    text = "📅 <b>Haftalik reyting (so'nggi 7 kun)</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(leaders):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = esc(row["full_name"] or row["username"] or "Foydalanuvchi")
        text += f"{medal} {name} — {row['total_score']} ball ({row['tests_taken']} test)\n"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📚 <b>Buyruqlar</b>\n\n"
        "/start — kursni boshlash/davom ettirish\n"
        "/progress — barcha bo'limlar bo'yicha progress (skill-tree)\n"
        "/review — xato qilingan savollarni qayta mashq qilish\n"
        "/stats — shaxsiy statistika\n"
        "/badges — nishonlaringiz\n"
        "/top — barcha vaqt reytingi\n"
        "/weektop — haftalik reyting",
        parse_mode="HTML",
    )


# ---------- Admin: video/audio yuklash ----------

@router.message(Command("setvideo"))
async def cmd_setvideo(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("Foydalanish: /setvideo <en|ru> <grammar|vocabulary> <module_id>\n/modules bilan ID larni ko'ring.")
        return
    _, subject, section, module_id = parts
    if not get_module_by_id(subject, section, module_id):
        await message.answer("Bunday modul topilmadi. /modules bilan tekshiring.")
        return
    await state.update_data(admin_target=(subject, section, module_id))
    await state.set_state(AdminUpload.waiting_video)
    await message.answer(f"Endi <b>{module_id}</b> uchun VIDEO faylni yuboring.", parse_mode="HTML")


@router.message(Command("setaudio"))
async def cmd_setaudio(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    parts = message.text.split()
    if len(parts) != 4:
        await message.answer("Foydalanish: /setaudio <en|ru> <grammar|vocabulary> <module_id>")
        return
    _, subject, section, module_id = parts
    if not get_module_by_id(subject, section, module_id):
        await message.answer("Bunday modul topilmadi. /modules bilan tekshiring.")
        return
    await state.update_data(admin_target=(subject, section, module_id))
    await state.set_state(AdminUpload.waiting_audio)
    await message.answer(f"Endi <b>{module_id}</b> uchun AUDIO/ovozli xabar yuboring.", parse_mode="HTML")


@router.message(Command("modules"))
async def cmd_modules(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    lines = []
    for subject in ("en", "ru"):
        for section in ("grammar", "vocabulary"):
            lines.append(f"\n<b>{subject}/{section}</b>:")
            for m in get_modules(subject, section):
                lines.append(f"  {m['id']}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(AdminUpload.waiting_video, F.video)
async def receive_video(message: Message, state: FSMContext):
    data = await state.get_data()
    subject, section, module_id = data["admin_target"]
    db.set_module_media(subject, section, module_id, video_file_id=message.video.file_id)
    await message.answer(f"✅ Video saqlandi: {subject}/{section}/{module_id}")
    await state.clear()


@router.message(AdminUpload.waiting_audio, F.voice | F.audio)
async def receive_audio(message: Message, state: FSMContext):
    data = await state.get_data()
    subject, section, module_id = data["admin_target"]
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    db.set_module_media(subject, section, module_id, audio_file_id=file_id)
    await message.answer(f"✅ Audio saqlandi: {subject}/{section}/{module_id}")
    await state.clear()


# ---------- Ishga tushirish ----------

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
    logger.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
