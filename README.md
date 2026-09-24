# Repetitorlik Telegram Bot — Structured O'quv Tizimi

Ingliz va Rus tillaridan **bosqichma-bosqich** o'rganish boti.
Duolingo / Babbel / Busuu uslubidagi progressiv ochilish, video darslar,
nazariya (yozma + og'zaki) va takrorlanmaydigan mashqlar.

## Asosiy imkoniyatlar

1. **Placement test** — yangi o'quvchi avval test ishlaydi, natijaga qarab
   beginner / intermediate / advanced darajaga joylashadi.
2. **Progressive unlock** — modulni tugatmasangiz (video + test) keyingisi
   ochilmaydi. Sakrab o'tib ketib bo'lmaydi.
3. **Video dars** — har bir modulda o'qituvchi video havolasi; «Ko'rdim»
   bosilgach mashg'ulotlar ochiladi.
4. **Nazariya** — yozma tushuntirish + og'zaki (matn ko'rinishida) tushuntirish.
5. **Mashg'ulotlar** — MCQ, fill-blank, matching; savollar aralashtiriladi va
   ko'rilganlari qayta takrorlanmasligi uchun kuzatiladi.
6. **Ikkala til** — Ingliz (zamonlar markazli) va Rus (padejlar + zamonlar).
7. **Gamification** — XP, unvonlar, streak, nishonlar, reyting.

## Fayllar tuzilishi

```
repetitor_bot/
├── bot.py                 # Asosiy bot (aiogram 3.x)
├── database.py            # SQLite: users, progress, modules, seen exercises
├── curriculum_en.json     # Ingliz tili o'quv dasturi (modullar + nazariya + mashqlar)
├── curriculum_ru.json     # Rus tili o'quv dasturi
├── questions_en.json      # (eski) qo'shimcha savollar banki — ixtiyoriy
├── questions_ru.json
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## O'rnatish

1. Python 3.10+
2. `pip install -r requirements.txt`
3. `@BotFather` dan token oling
4. `.env` yarating:
   ```bash
   cp .env.example .env
   # BOT_TOKEN=... ni to'ldiring
   ```
5. `python bot.py`

## Foydalanuvchi oqimi

```
/start
  → Til tanlash (en/ru)
  → Placement test (9 savol)
  → Daraja aniqlanadi
  → Kurs menyusi
       → Daraja tanlash
       → Modul ro'yxati (🔒/🔓/✅)
       → Modul ichida:
            1. Video ko'rish → «Ko'rdim»
            2. Nazariya (yozma + og'zaki)
            3. Mashg'ulot/test (≥70–80%)
            4. Keyingi modul ochiladi
```

## Buyruqlar

- `/start` — bosh menyu yoki placement
- `/stats` — XP, streak, modullar, kuchsiz tomonlar
- `/top` — XP bo'yicha TOP-10
- `/help` — qisqa yo'riqnoma

## Curriculum qanday kengaytiriladi

`curriculum_en.json` yoki `curriculum_ru.json` ichida yangi modul:

```json
{
  "id": "en_b_06",
  "order": 6,
  "title": "Yangi mavzu",
  "emoji": "🆕",
  "video_url": "https://youtube.com/...",
  "video_title": "Video nomi",
  "theory": "<b>Yozma tushuntirish</b>...",
  "theory_oral": "Og'zaki tushuntirish matni...",
  "pass_threshold": 70,
  "exercises": [
    {
      "id": "en_b_06_e1",
      "type": "mcq",
      "question": "...",
      "options": ["A", "B", "C", "D"],
      "correct": 0,
      "explanation": "..."
    }
  ]
}
```

`type`: `mcq` | `fill_blank` | `matching`

## Xavfsizlik

- Token faqat `.env` dan
- HTML escape (injection himoyasi)
- Faqat private chat
- Sessiya egasi tekshiruvi
- Anti-flood
- Parametrlangan SQL

## Keyingi rivojlantirish g'oyalari

- [ ] Haqiqiy ovozli xabarlar (TTS) og'zaki nazariya uchun
- [ ] Admin panel orqali video/nazariya qo'shish
- [ ] Kunlik eslatma (reminder)
- [ ] Referral tizimi
- [ ] Redis FSM (production)
- [ ] Ko'proq modullar (150+ mashq)
- [ ] Speaking practice (voice message + baholash)

## Pedagogik asos

Tizim CEFR va eng yaxshi amaliyotlarga asoslangan:

- **Scaffolding** — oddiydan murakkabga, oldingi bilimsiz keyingisi yopiq
- **Comprehensible input** — video + yozma + og'zaki
- **Spaced / varied practice** — savollar aralashadi, takrorlanmaydi
- **Mastery learning** — minimal foizni topmaguncha keyingi modul ochilmaydi
- **Gamification** — XP, streak, nishonlar motivatsiyani ushlab turadi
