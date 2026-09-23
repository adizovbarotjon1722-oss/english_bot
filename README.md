# Repetitorlik Telegram Bot

Ingliz va rus tillaridan test/mashq qildiradigan Telegram bot. Grammatika va
lug'at (vocabulary) bo'yicha 3 ta daraja (boshlang'ich, o'rta, qiyin) mavjud.

## Fayllar tuzilishi

```
repetitor_bot/
├── bot.py              # Asosiy bot logikasi (aiogram 3.x)
├── database.py         # SQLite bilan ishlash funksiyalari
├── questions_en.json   # Ingliz tili savollar banki
├── questions_ru.json   # Rus tili savollar banki
├── requirements.txt    # Kerakli Python kutubxonalari
└── README.md
```

## O'rnatish va ishga tushirish

1. **Python 3.10+** o'rnatilganiga ishonch hosil qiling.

2. Kutubxonalarni o'rnating:
   ```bash
   pip install -r requirements.txt
   ```

3. Telegram bot tokenini oling:
   - Telegramda [@BotFather](https://t.me/BotFather) ga o'ting
   - `/newbot` buyrug'ini yuboring, nom va username bering
   - Sizga beriladigan tokenni saqlab qo'ying

4. Tokenni `.env` fayli orqali bering (tavsiya etiladi):
   ```bash
   cp .env.example .env
   # .env faylini ochib, BOT_TOKEN=... qatoriga haqiqiy tokeningizni yozing
   ```
   `.env` fayli `.gitignore`ga kiritilgan — u hech qachon GitHub yoki boshqa
   ochiq joyga yuklanmaydi. Muhim: tokenni hech kim bilan ulashmang, u orqali
   botingizni to'liq boshqarish mumkin.

5. Botni ishga tushiring:
   ```bash
   python bot.py
   ```

Birinchi ishga tushirishda `repetitor.db` nomli SQLite fayli avtomatik yaratiladi.

## Bot buyruqlari

- `/start` — testni boshlash (til → bo'lim → daraja tanlash)
- `/stats` — shaxsiy statistika (unvon, streak, ishlangan testlar, kuchsiz tomonlar)
- `/badges` — qo'lga kiritilgan nishonlar ro'yxati
- `/top` — reyting jadvali (TOP-10 foydalanuvchi)

## Savol turlari

Bot 3 xil savol turini qo'llab-quvvatlaydi (`questions_*.json`dagi `"type"` maydoni orqali):

1. **`mcq`** (standart, `type` yozilmasa ham shu) — variantli test
2. **`fill_blank`** — foydalanuvchi javobni matn ko'rinishida yozadi;
   `accepted_answers` massivida bir nechta to'g'ri yozilish shakli berilishi mumkin
3. **`matching`** — chap va o'ng ustunlarni mos keltirish (`pairs` massivi);
   bot har bir juftlikni birma-bir so'raydi va oxirida barchasi to'g'ri
   bo'lsagina savol "to'g'ri" hisoblanadi

## O'yinlashtirish (gamification)

Mashg'ulotlarni qiziqarli qilish uchun quyidagilar qo'shilgan:

- **Hazil-mutoyibali javob xabarlari** — har safar tasodifiy tanlangan turli
  xil rag'batlantiruvchi/hazil iboralar (`CORRECT_PHRASES`, `WRONG_PHRASES`
  ro'yxatlarida — xohlasangiz o'zingizniki bilan to'ldiring/almashtiring)
- **Unvonlar** — umumiy to'g'ri javoblar soniga qarab avtomatik ochiladi:
  🌱 Yangi boshlovchi → 📖 O'quvchi → 🎯 Bilimdon → 🏅 Usta → 👑 Professor
- **Nishonlar (badges)** — birinchi test, 100% natija, streak bosqichlari
  (3/7/14/30 kun), umumiy test soni bosqichlari (10/50/100 ta)
- **Streak** — ketma-ket kunlarda test ishlash hisoblanadi va har test
  yakunida ko'rsatiladi

Unvon va nishon ro'yxatini kengaytirish uchun `bot.py`dagi `TITLE_THRESHOLDS`,
`STREAK_BADGES`, `TESTS_TAKEN_BADGES`, `CORRECT_PHRASES`, `WRONG_PHRASES`
o'zgaruvchilarini tahrirlang.

## Savollar bankini kengaytirish

`questions_en.json` va `questions_ru.json` fayllariga yangi obyekt qo'shish
kifoya. Savol turiga qarab format farq qiladi:

**MCQ (variantli):**
```json
{
  "id": "en_g_b_08",
  "section": "grammar",
  "level": "beginner",
  "question": "Savol matni",
  "options": ["A", "B", "C", "D"],
  "correct": 0,
  "explanation": "Nega bu javob to'g'ri ekanligi tushuntirilishi"
}
```

**Fill_blank (bo'shliq to'ldirish):**
```json
{
  "id": "en_fb_b_05",
  "section": "grammar",
  "level": "beginner",
  "type": "fill_blank",
  "question": "I ___ a student.",
  "accepted_answers": ["am"],
  "explanation": "Tushuntirish"
}
```

**Matching (mos keltirish):**
```json
{
  "id": "en_m_b_02",
  "section": "vocabulary",
  "level": "beginner",
  "type": "matching",
  "question": "So'zlarni mos keltiring:",
  "pairs": [{"left": "Cat", "right": "Mushuk"}, {"left": "Dog", "right": "It"}],
  "explanation": "Tushuntirish"
}
```

- `section`: `"grammar"` yoki `"vocabulary"`
- `level`: `"beginner"`, `"intermediate"` yoki `"advanced"`
- `correct` (mcq uchun): to'g'ri javobning `options` massividagi indeksi (0 dan boshlanadi)

Hozircha har bir tilda ~30 tadan savol bor (demo uchun) — botni ishga
tushirishdan oldin buni kamida 100-150 tagacha ko'paytirish tavsiya etiladi,
aks holda foydalanuvchilarga bir xil savollar tez-tez takrorlanadi.

## Xavfsizlik bo'yicha qo'llanilgan choralar

- **Token himoyasi** — token kodga yozilmaydi, `.env` fayldan o'qiladi va
  `.gitignore` orqali repo'ga tushmaydi. Token bo'lmasa bot ishga tushmaydi
  (aniq xato bilan to'xtaydi, noaniq holatda qolmaydi).
- **HTML-injection himoyasi** — foydalanuvchi ismi/username kabi Telegram
  profilidan olinadigan ma'lumotlar xabarga qo'yishdan oldin `html.escape()`
  bilan tozalanadi, aks holda kimdir o'z ismiga maxsus belgilar kiritib
  xabar formatini buzishi yoki soxta tugma/matn ko'rsatishi mumkin edi.
- **Faqat shaxsiy chat** — bot faqat 1-on-1 (private) chatlarda ishlaydi;
  guruhga qo'shilib qolsa ham boshqa a'zolarning buyruqlariga javob
  bermaydi, shu bilan bir foydalanuvchining testiga boshqa a'zo
  aralashib qolish xavfi yo'qoladi.
- **Sessiya egasini tekshirish** — har bir javob tugmasi bosilganda kim
  bosayotgani (sessiya boshlagan foydalanuvchimi) tekshiriladi — himoya
  chuqurligi uchun qo'shimcha qatlam.
- **Anti-flood/spam** — har bir foydalanuvchi uchun so'rovlar oralig'i
  cheklangan (soniyaning bir qismi), shu bilan bot ustidan spam orqali
  yuklama tushirishning oldi olinadi.
- **Callback-data validatsiyasi** — tugmalardan keladigan ma'lumot doim
  tekshiriladi (noto'g'ri format, chegaradan tashqari indeks va h.k.),
  buzilgan/soxta so'rov bot ishini to'xtatmaydi.
- **SQL-injection himoyasi** — barcha bazaga so'rovlar parametrlashtirilgan
  (`?` placeholder), foydalanuvchi kiritgan matn hech qachon SQL so'roviga
  to'g'ridan-to'g'ri qo'shilmaydi.
- **Xatolarni ushlash** — kutilmagan xatoliklar (masalan, eskirgan xabarni
  tahrirlashga urinish) botni yiqitmaydi, faqat log'ga yoziladi.

## Keyingi qadamlar (tavsiya)

- [ ] Savollar bazasini kengaytirish (kamida 150+ savol/til)
- [ ] Admin panel yoki admin-bot orqali yangi savol qo'shish imkoniyati
- [ ] Kunlik eslatma (reminder) — foydalanuvchi uzoq vaqt kirmasa xabar yuborish
- [ ] Do'stni taklif qilish (referral) tizimi
- [ ] Yangi fanlar qo'shish (Matematika, Dasturlash va h.k.)
- [ ] Production serverga deploy qilish (systemd yoki Docker orqali)
- [ ] Production'da MemoryStorage o'rniga Redis-based FSM storage ishlatish
      (server qayta ishga tushganda foydalanuvchi sessiyalari yo'qolmasligi uchun)
- [ ] Sentry yoki shunga o'xshash xato kuzatuv tizimini ulash
