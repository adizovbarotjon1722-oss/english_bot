"""
bot.py — Repetitorlik Telegram bot (test/mashq tizimi).
Ingliz va rus tillari bo'yicha grammatika va lug'at testlari.

Ishga tushirish:
    1. pip install -r requirements.txt
    2. .env faylida BOT_TOKEN ni kiriting (yoki quyida TOKEN o'zgaruvchisini to'g'ridan-to'g'ri yozing)
    3. python bot.py
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
    pass  # python-dotenv o'rnatilmagan bo'lsa ham muhit o'zgaruvchisi ishlaydi

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
MIN_SECONDS_BETWEEN_ACTIONS = 0.7  # oddiy anti-flood: har foydalanuvchi uchun

router = Router()
# Guruh/kanallarda emas, faqat shaxsiy chatda ishlaydi — begona odamlar
# boshqa foydalanuvchining testiga aralashib qolmasligi uchun.
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")


def esc(text: str | None) -> str:
    """HTML parse_mode bilan xabar yuborishdan oldin foydalanuvchi kiritgan
    (yoki Telegram profilidan olingan) matnni xavfsiz qilib escape qiladi —
    aks holda ism/familiyaga maxsus belgilar kiritib xabar formatini yoki
    ko'rinishini buzish (HTML-injection) mumkin bo'lardi."""
    return html.escape(text or "")


class ThrottlingMiddleware(BaseMiddleware):
    """Har bir foydalanuvchi uchun juda tez-tez so'rov yuborishning (flood /
    spam / boshqa foydalanuvchilarga xalaqit beradigan yuklama) oldini oladi."""

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
                return  # so'rovni e'tiborsiz qoldiramiz
            self._last_seen[user.id] = now
        return await handler(event, data)

SUBJECT_NAMES = {"en": "🇬🇧 Ingliz tili", "ru": "🇷🇺 Rus tili"}
SECTION_NAMES = {"grammar": "📘 Grammatika", "vocabulary": "📗 Lug'at (Vocabulary)"}
LEVEL_NAMES = {
    "beginner": "🟢 Boshlang'ich",
    "intermediate": "🟡 O'rta",
    "advanced": "🔴 Qiyin",
}

# ---------- O'yinlashtirish: unvonlar, nishonlar, hazil-mutoyiba ----------

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


def load_questions():
    bank = {}
    for code in ("en", "ru"):
        with open(f"questions_{code}.json", encoding="utf-8") as f:
            bank[code] = json.load(f)
    return bank


QUESTIONS = load_questions()


class QuizState(StatesGroup):
    choosing_subject = State()
    choosing_section = State()
    choosing_level = State()
    answering = State()


# ---------- Klaviaturalar ----------

def subjects_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"subj:{code}")]
            for code, name in SUBJECT_NAMES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sections_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"sect:{code}")]
            for code, name in SECTION_NAMES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def levels_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"lvl:{code}")]
            for code, name in LEVEL_NAMES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def answer_kb(options: list[str], q_index: int) -> InlineKeyboardMarkup:
    # Telegram button text max ~64 belgi — juda uzun variantlarni kesib qo'yamiz,
    # aks holda API xato qaytaradi va bot to'xtab qolishi mumkin.
    rows = [[InlineKeyboardButton(text=opt[:64], callback_data=f"ans:{q_index}:{i}")]
            for i, opt in enumerate(options)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Handlerlar ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    await state.clear()
    await message.answer(
        f"Salom, {esc(message.from_user.full_name)}! 👋\n\n"
        "Bu bot orqali Ingliz va Rus tillaridan bilimingizni sinab ko'rishingiz mumkin.\n"
        "Qaysi tildan boshlaymiz?",
        reply_markup=subjects_kb(),
    )
    await state.set_state(QuizState.choosing_subject)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    user_id = db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
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
    )
    if stats["weak_areas"]:
        text += "\n⚠️ Kuchsiz tomonlaringiz:\n"
        for w in stats["weak_areas"]:
            subj = SUBJECT_NAMES.get(w["subject"], w["subject"])
            sect = SECTION_NAMES.get(w["section"], w["section"])
            text += f"  • {subj} — {sect}: {w['pct']}%\n"
    await message.answer(text, parse_mode="HTML")


ALL_BADGE_LABELS = {
    "first_quiz": "🎉 Birinchi test",
    "perfect_score": "💯 Mukammal natija",
    **{code: label for code, label in STREAK_BADGES.values()},
    **{code: label for code, label in TESTS_TAKEN_BADGES.values()},
}


@router.message(Command("badges"))
async def cmd_badges(message: Message):
    user_id = db.get_or_create_user(
        message.from_user.id, message.from_user.username, message.from_user.full_name
    )
    earned = set(db.get_user_badges(user_id))
    if not earned:
        await message.answer(
            "Hali nishonlaringiz yo'q. Birinchi testni yakunlab, birinchi nishoningizni oling! 🎉"
        )
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
    text = "🏆 <b>Reyting jadvali (TOP-10)</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(leaders):
        medal = medals[i] if i < 3 else f"{i+1}."
        name = esc(row["full_name"] or row["username"] or "Foydalanuvchi")
        text += f"{medal} {name} — {row['total_score']} ball ({row['tests_taken']} test)\n"
    await message.answer(text, parse_mode="HTML")


@router.callback_query(QuizState.choosing_subject, F.data.startswith("subj:"))
async def choose_subject(callback: CallbackQuery, state: FSMContext):
    subject = callback.data.split(":")[1]
    await state.update_data(subject=subject)
    await state.set_state(QuizState.choosing_section)
    await callback.message.edit_text(
        f"{SUBJECT_NAMES[subject]} tanlandi.\nQaysi bo'limni mashq qilamiz?",
        reply_markup=sections_kb(),
    )
    await callback.answer()


@router.callback_query(QuizState.choosing_section, F.data.startswith("sect:"))
async def choose_section(callback: CallbackQuery, state: FSMContext):
    section = callback.data.split(":")[1]
    await state.update_data(section=section)
    await state.set_state(QuizState.choosing_level)
    await callback.message.edit_text(
        f"{SECTION_NAMES[section]} tanlandi.\nQaysi darajada?",
        reply_markup=levels_kb(),
    )
    await callback.answer()


@router.callback_query(QuizState.choosing_level, F.data.startswith("lvl:"))
async def choose_level(callback: CallbackQuery, state: FSMContext):
    level = callback.data.split(":")[1]
    data = await state.update_data(level=level)
    subject, section = data["subject"], data["section"]

    pool = [
        q for q in QUESTIONS[subject]
        if q["section"] == section and q["level"] == level
    ]
    if not pool:
        await callback.message.edit_text("Bu bo'limda hozircha savollar yo'q. /start bilan qaytadan urinib ko'ring.")
        await state.clear()
        return

    quiz_questions = random.sample(pool, min(QUESTIONS_PER_QUIZ, len(pool)))
    await state.update_data(
        quiz=quiz_questions,
        current=0,
        score=0,
        owner_id=callback.from_user.id,  # sessiya egasi — boshqa foydalanuvchi aralashmasin
        username=callback.from_user.username,
        full_name=callback.from_user.full_name,
    )
    await state.set_state(QuizState.answering)
    await callback.message.edit_text(f"{LEVEL_NAMES[level]} daraja tanlandi. Boshladik! 🚀")
    await send_question(callback.message, state)
    await callback.answer()


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
            parse_mode="HTML",
            reply_markup=skip_kb,
        )
    elif qtype == "matching":
        pairs = q["pairs"]
        order = list(range(len(pairs)))
        random.shuffle(order)
        await state.update_data(match=dict(order=order, step=0, errors=0))
        await message.answer(f"{header}\n\n<b>{esc(q['question'])}</b>", parse_mode="HTML")
        await send_matching_step(message, state)
    else:  # mcq
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

    # O'ng tomon variantlari — shu savolning barcha juftlaridan, aralashtirilgan
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
        parse_mode="HTML",
        reply_markup=kb,
    )


def check_owner(data: dict, user_id: int) -> bool:
    return data.get("owner_id") == user_id


@router.callback_query(QuizState.answering, F.data.startswith("ans:"))
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

    correct_option = esc(q["options"][q["correct"]])
    feedback = random.choice(CORRECT_PHRASES) if is_correct else (
        f"{random.choice(WRONG_PHRASES)}\nTo'g'ri javob: <b>{correct_option}</b>"
    )
    feedback += f"\n💡 {esc(q['explanation'])}"
    await safe_edit(callback.message, f"❓ {esc(q['question'])}\n\n{feedback}")

    await advance_quiz(callback.message, state, current + 1, score)
    await callback.answer()


@router.callback_query(QuizState.answering, F.data.startswith("skip:"))
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
    accepted = esc(q["accepted_answers"][0])
    await safe_edit(
        callback.message,
        f"⏭ O'tkazib yuborildi.\nTo'g'ri javob: <b>{accepted}</b>\n💡 {esc(q['explanation'])}",
    )
    await advance_quiz(callback.message, state, current + 1, data["score"])
    await callback.answer()


def normalize_answer(text: str) -> str:
    return " ".join(text.strip().lower().split())


@router.message(QuizState.answering, F.text)
async def handle_fill_blank_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if not check_owner(data, message.from_user.id):
        return  # boshqa hodisa — jim o'tkazib yuboramiz

    quiz, current = data["quiz"], data["current"]
    q = quiz[current]
    if q.get("type") != "fill_blank":
        return  # hozir matnli javob kutilmayapti (mcq/matching bosqichi)

    user_answer = normalize_answer(message.text)
    accepted = [normalize_answer(a) for a in q["accepted_answers"]]
    is_correct = user_answer in accepted

    score = data["score"] + (1 if is_correct else 0)
    correct_option = esc(q["accepted_answers"][0])
    feedback = random.choice(CORRECT_PHRASES) if is_correct else (
        f"{random.choice(WRONG_PHRASES)}\nTo'g'ri javob: <b>{correct_option}</b>"
    )
    feedback += f"\n💡 {esc(q['explanation'])}"
    await message.answer(feedback, parse_mode="HTML")

    await advance_quiz(message, state, current + 1, score)


@router.callback_query(QuizState.answering, F.data.startswith("match:"))
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
    user_id = db.get_or_create_user(
        data["owner_id"], data.get("username"), data.get("full_name")
    )

    stats_before = db.get_user_stats(user_id)
    title_before = random_title_for(stats_before["total_score"])
    is_first_quiz = stats_before["tests_taken"] == 0

    db.save_result(user_id, data["subject"], data["section"], data["level"], score, total)
    streak = db.update_streak(user_id)

    stats_after = db.get_user_stats(user_id)
    title_after = random_title_for(stats_after["total_score"])

    pct = round(100 * score / total)
    lines = [
        "🎉 Test tugadi!",
        "",
        f"Natija: <b>{score}/{total}</b> ({pct}%)",
        f"🔥 Streak: {streak} kun",
    ]

    new_badges = []
    if is_first_quiz and db.award_badge(user_id, "first_quiz"):
        new_badges.append("🎉 Birinchi test")
    if pct == 100 and db.award_badge(user_id, "perfect_score"):
        new_badges.append("💯 Mukammal natija")
    if streak in STREAK_BADGES:
        code, label = STREAK_BADGES[streak]
        if db.award_badge(user_id, code):
            new_badges.append(label)
    if stats_after["tests_taken"] in TESTS_TAKEN_BADGES:
        code, label = TESTS_TAKEN_BADGES[stats_after["tests_taken"]]
        if db.award_badge(user_id, code):
            new_badges.append(label)

    if new_badges:
        lines.append("")
        lines.append("🏅 <b>Yangi nishon(lar)!</b>")
        lines.extend(f"  • {b}" for b in new_badges)

    if title_after != title_before:
        lines.append("")
        lines.append(f"🆙 Yangi unvon: <b>{title_after}</b>! Tabriklaymiz! 🎊")

    lines.append("")
    lines.append("Yana mashq qilish uchun /start bosing.")
    lines.append("Statistikangiz uchun /stats, reyting uchun /top yozing.")

    await message.answer("\n".join(lines), parse_mode="HTML")
    await state.clear()


async def on_unhandled_error(event, exception):
    """Kutilmagan xatoliklarni ushlab, botni yiqilishdan saqlaydi
    (masalan, tarmoq uzilishi yoki Telegram API vaqtinchalik xatosi)."""
    logger.exception("Kutilmagan xatolik: %s", exception)
    return True


async def main():
    db.init_db()
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Anti-flood middleware — ham oddiy xabarlarga, ham tugma bosishlarga
    throttle = ThrottlingMiddleware()
    dp.message.middleware(throttle)
    dp.callback_query.middleware(throttle)

    dp.errors.register(on_unhandled_error)
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)  # eski/qolib ketgan so'rovlarni tozalaydi
    logger.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
