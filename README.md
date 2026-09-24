# Repetitorlik Telegram Bot — Ingliz tili (Duolingo uslubida)

Hozircha **faqat ingliz tili** kursi: placement → video → nazariya → mashq → unlock.
Duolingo uslubidagi gamification: ❤️ yuraklar, 💎 gemlar, 🎯 kunlik questlar, 🏆 haftalik liga, 📚 kutubxona.

## So'nggi yangilanish (24.09.2026)

### Duolingo dizayni (shu versiya)
- **Rus tili kursi o'chirildi** — faqat ingliz tili, mukammal darajada
- **❤️ Yuraklar**: 5 ta maksimum; noto'g'ri javobda −1 (modul va ko'nikma testlarida);
  har 30 daqiqada 1 ta tiklanadi; 50 💎 evaziga to'liq to'ldirish
- **💎 Gemlar**: modul yakuni (+20), mukammal natija (+10), questlar, kutubxona, hikoyalar
- **🎯 Kunlik questlar**: har kuni har user uchun tasodifiy 3 ta vazifa
  (XP, to'g'ri javoblar, modul, SRS, o'yin, kutubxona) — bajarib gem yutasiz
- **🏆 Haftalik liga**: 🥉 0+ · 🥈 150+ · 🥇 400+ · 💎 800+ haftalik XP, TOP-10 reyting
- **📚 Kutubxona**: 7 ta kitob (31 bob, 62 tushunish savoli) — badiiy, detektiv,
  ilmiy-fantastik, biznes, sayohat, she'riyat; har bob +10 XP, +2 💎, quiz +bonus gem
- **🖼️ Rasmli mashqlar**: yangi `en_b_06` modul (10 ta rasmli mashq), `media/images/` da
  apple/dog/house/car/book/clock/banner rasmlari
- Yangi foydalanuvchini banner + "🚀 Boshlash" kutib oladi; har qadamda status qatori
  (❤️ 💎 🔥 ⭐)
- `/stats` endi yurak, gem, kutubxona va liga ma'lumotini ham ko'rsatadi

### Xavfsizlik
- `/premium` **faqat admin** uchun
- Deep-link referral kodi va jamoa kodi validatsiyasi
- Callback sessiya egasi tekshiruvi (`owner_id`) — begona odam birovning testiga aralasha olmaydi
- Callback data int-parsing himoyasi, HTML escaping (`esc()`), kutubxona idx chegaralari tekshiriladi
- Speaking vaqtinchalik ovoz fayllari avtomatik o'chiriladi
- Throttling xotirasi cheklangan (5000 yozuv)
- `reportlab` bo'lmasa ham bot ishlaydi (sertifikat graceful disable)

### Buglar (tuzatilgan)
- Jamoaga kod bilan qo'shilish ishlamasdi — tuzatildi
- "Juftlik poygasi" noto'g'ri "bepul mashq" sifatida saqlanardi — tuzatildi
- `/broadcast` faqat 500 kishiga yuborardi — endi barchaga
- O'yin XP si haftalik ligaga tushmasdi — `save_game_score` endi `add_xp` orqali beriladi

## Tezkor start

```bash
pip install -r requirements.txt
cp .env.example .env   # BOT_TOKEN=...
python bot.py
```

## Kontent

| Bo'lim | Miqdor |
|--------|--------|
| Modullar (EN) | 13 ta (6 beginner · 4 intermediate · 3 advanced) |
| Mashqlar | 166 ta (mcq, fill_blank, matching, rasmli) |
| Kutubxona | 7 kitob · 31 bob · 62 savol |
| Ko'nikmalar | Reading, Listening, Speaking, kunlik challenge |
| Boshqa | SRS takrorlash, 2 mini-o'yin, hikoya rejimi, jamoalar |

## Duolingo mexanikasi qanday ishlaydi

```
Noto'g'ri javob (modul/ko'nikma testi) → −1 ❤️
❤️ == 0 → test to'xtaydi → kutish (30 min/❤️) yoki 50 💎 to'ldirish
Har bir XP → kunlik quest progresi + haftalik liga XP
Modul yakuni → +50 XP, +20 💎 (o'tsa); 100% → yana +10 💎
Quest bajarildi → 🎁 tugmasi orqali gem olinadi
```

## Fayllar

```
english_bot/
├── bot.py                  # asosiy bot (~3400 qator)
├── database.py             # SQLite: users, XP, yurak/gem, quest, kutubxona, liga
├── speaking_ai.py          # speaking tahlili (+Whisper ixtiyoriy)
├── certificate.py          # PDF sertifikat (reportlab)
├── curriculum_en.json      # 13 modul, 166 mashq
├── skills_en.json          # reading/listening/speaking
├── stories_en.json         # tanlovli hikoyalar
├── questions_en.json       # placement/bepul mashq savollari
├── library_en.json         # 7 kitob, 31 bob
├── backup.sh
├── media/images/           # banner + 6 ta rasmli mashq fayli
├── media/audio/
├── requirements.txt
├── .env.example
└── README.md
```

## Curriculumga rasm/audio qo'shish

```json
{
  "id": "en_b_06_img1",
  "type": "mcq",
  "question": "Bu nima?",
  "image_file": "apple.png",
  "options": ["Apple", "Banana"],
  "correct": 0,
  "explanation": "..."
}
```

Faylni `media/images/` ga qo'ying. URL uchun `image_url`, ovoz uchun
`audio_file`/`audio_url` maydonlari ishlaydi.

## Kutubxonaga kitob qo'shish

`library_en.json` → `books` massiviga:

```json
{
  "id": "mybook",
  "title": "My Book",
  "emoji": "📘",
  "level": "beginner",
  "description": "Qisqacha ta'rif",
  "chapters": [
    {
      "title": "Chapter 1",
      "text": "Matn...",
      "questions": [
        {"question": "...", "options": ["A","B","C"], "correct": 0, "explanation": "..."}
      ]
    }
  ]
}
```

## Admin panel

`.env` da `ADMIN_IDS=123456789` (vergul bilan bir nechta).

| Buyruq / menyu | Vazifa |
|----------------|--------|
| `/admin` | Admin bosh menyu |
| Umumiy statistika | Users, premium, testlar, faollik |
| O'quvchilar | Ro'yxat, sahifalash, batafsil kartochka |
| `/find <id\|@user\|id:N>` | Qidiruv |
| Darsliklar | EN modullar + mashqlar soni/ko'rinishi |
| Premium berish | Kartochkadan yoki `/premium <tg_id> [days]` |
| `/broadcast matn` | Barcha userlarga xabar |
| Level o'zgartirish | beginner/intermediate/advanced |
| Modul ochish | Foydalanuvchi uchun butun darajani unlock |
| Speaking navbati | Ovozli javoblarni tinglash, qabul/qayta |

### O'qituvchi
- `/setvideo en en_b_01 https://...` — modulga video
- Video/audio yuborib caption: `video:en:en_b_01`

### AI kalit (ixtiyoriy)
`.env`: `OPENAI_API_KEY` yoki `GROQ_API_KEY` — speaking uchun Whisper transkript
va boyroq feedback. Kalitsiz ham heuristik tahlil ishlaydi.

## Production

1. VPS (Ubuntu) yoki Railway/Render
2. `.env` da token, SENTRY_DSN, BOT_USERNAME, CHANNEL_URL, ADMIN_IDS
3. systemd yoki `screen`/`tmux`: `python bot.py`
4. Cron backup: `0 3 * * * /path/to/english_bot/backup.sh`
5. Ko'p foydalanuvchi uchun MemoryStorage o'rniga Redis

## Pedagogika

- Placement → to'g'ri daraja
- Video majburiy → nazariya → test (mastery, pass_threshold)
- Progressive unlock (sakrash yo'q)
- Varied practice (ko'rilgan mashqlar yodda saqlanadi)
- Duolingo gamification: yuraklar, gemlar, questlar, liga, streak
- Extensive reading: kutubxona orqali tabiiy til practice

## Keyingi qadamlar

- [ ] Haqiqiy YouTube videolarini o'zingizniki bilan almashtirish
- [ ] Kutubxonaga audio (listening) kitoblar qo'shish
- [ ] Telegram Stars / to'lov (premium sotish)
- [ ] Kunlik eslatma (reminder job) — streak saqlash uchun
- [ ] Rus tili kursini qayta qo'shish (talab bo'lsa)
