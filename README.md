# Repetitorlik Telegram Bot — to'liq kurs tizimi

Ingliz va rus tillaridan **strukturaviy kurs**: har bir zamon/mavzu alohida
bo'lim (video darslik + matnli/og'zaki tushuntirish + mashqlar), bo'limlar
**ketma-ket ochiladi** — Duolingo va shunga o'xshash platformalarning asosiy
tamoyillari asosida qurilgan (moslashuvchan joylashtirish testi, mastery-based
unlock, takrorlanmaydigan mashqlar, streak/nishon tizimi).

## Fayllar tuzilishi

```
repetitor_bot/
├── bot.py               # Asosiy bot logikasi (aiogram 3.x)
├── database.py          # SQLite bilan ishlash funksiyalari
├── curriculum_en.json   # Ingliz tili — bo'limlar (modullar) ta'rifi
├── curriculum_ru.json   # Rus tili — bo'limlar (modullar) ta'rifi
├── questions_en.json    # Ingliz tili savollar banki (modulga bog'langan)
├── questions_ru.json    # Rus tili savollar banki (modulga bog'langan)
├── requirements.txt
├── .env.example
└── README.md
```

## O'rnatish

1. **Python 3.10+**
2. ```bash
   pip install -r requirements.txt
   ```
3. [@BotFather](https://t.me/BotFather) dan token oling.
4. ```bash
   cp .env.example .env
   # .env faylini ochib BOT_TOKEN va ADMIN_IDS ni to'ldiring
   ```
   `ADMIN_IDS` — video/audio darslik yuklay oladigan Telegram ID'lar (o'z
   ID'ingizni bilish uchun @userinfobot ga yozing).
5. ```bash
   python bot.py
   ```

## Tizim qanday ishlaydi

### 1. Bo'limlar (modullar) — har bir zamon/mavzu uchun

`curriculum_en.json` / `curriculum_ru.json` da har bir bo'lim: `id`, `order`
(tartib raqami), `title`, `text_explanation` (yozma tushuntirish, HTML bilan),
`pass_threshold` (bo'lim testidan o'tish uchun kerakli foiz, standart 70%).

Boshlang'ich to'plam:
- **Grammatika (EN):** Present Simple → Present Continuous → Past Simple →
  Present Perfect → Modals/Passive → Future Simple → Murakkab grammatika
- **Grammatika (RU):** Asosiy iboralar → Asosiy padejlar → O'tgan zamon →
  Kelasi zamon → Qo'shimcha padejlar → Shart mayli/Passive → Murakkab grammatika
- **Lug'at (EN/RU):** Kundalik so'zlar → Foydali iboralar → Murakkab lug'at

### 2. Ketma-ket ochilish (mastery-based unlock)

Har bir (foydalanuvchi, til, bo'lim-turi) uchun `unlocked_order` saqlanadi.
Faol bo'lim tugallanmaguncha keyingisi **ochilmaydi** — o'quvchi sakrab o'tib
ketolmaydi. Bo'lim testidan `pass_threshold` dan yuqori ball bilan o'tilsa,
keyingi bo'lim avtomatik ochiladi.

### 3. Video-gating

O'qituvchi (admin) `/setvideo en grammar present_simple` buyrug'i bilan videoni
shu bo'limga bog'laydi. Video mavjud bo'lgan bo'limda o'quvchi **"✅ Ko'rdim"**
tugmasini bosmaguncha o'sha bo'limning mashqlari va testi ochilmaydi.

> ⚠️ **Muhim cheklov:** Telegram real vaqtda "video oxirigacha ko'rildimi"
> degan ma'lumotni bermaydi — shuning uchun bu o'z-o'zini tasdiqlash (self-report)
> tugmasi orqali amalga oshiriladi. Bu ko'plab ta'lim botlarida qo'llaniladigan
> standart yechim.

### 4. Joylashtirish testi (placement test)

Yangi o'quvchi bo'lim/til tanlaganda ikkita variant beriladi: joylashtirish
testini ishlash yoki 1-bo'limdan boshlash. Test har bir bo'limdan bittadan
savol oladi; natijaga qarab mos bo'limdan boshlanadi (Duolingo'dagi
soddalashtirilgan versiyasi — to'liq adaptiv IRT algoritmi emas, lekin xuddi
shu tamoyil: tajribali o'quvchi asosdan boshlamaydi).

### 5. Mashqlar takrorlanmaydi

Har bir savol foydalanuvchi bo'yicha (`seen_questions` jadvalida) kuzatiladi.
Mashq/bo'lim testi tanlaganda avval **ko'rilmagan** savollar ustunlik qiladi;
barcha savollar bir marta ko'rilgach, eng kam ko'rilganlari qaytadan
aralashtirilib beriladi — shu bilan mashqlar doim yangilanib turadi.

### 6. Xatolarni qayta mashq qilish — `/review`

Foydalanuvchi ko'proq xato qilgan savollar alohida to'plamda (barcha
bo'limlar bo'yicha) qayta beriladi — progressga ta'sir qilmaydi, faqat
mustahkamlash uchun.

## Bot buyruqlari

- `/start` — kursni boshlash/davom ettirish
- `/progress` — barcha bo'limlar bo'yicha holat (✅ o'tilgan, 🔓 faol, 🔒 qulf)
- `/review` — xato qilingan savollarni qayta mashq qilish
- `/stats` — shaxsiy statistika (unvon, streak, nishonlar soni)
- `/badges` — qo'lga kiritilgan nishonlar
- `/top` — barcha vaqt reytingi
- `/weektop` — so'nggi 7 kunlik reyting

**Admin (o'qituvchi) buyruqlari** (`.env`dagi `ADMIN_IDS`ga kiritilgan bo'lishi kerak):
- `/modules` — barcha til/bo'lim/modul ID'larini ko'rsatadi
- `/setvideo <en|ru> <grammar|vocabulary> <module_id>` — keyingi yuborilgan
  videoni shu modulga bog'laydi
- `/setaudio <en|ru> <grammar|vocabulary> <module_id>` — keyingi yuborilgan
  ovozli xabar/audioni shu modulga bog'laydi

## Savol turlari

`questions_*.json`dagi `"type"` maydoni orqali:

1. **`mcq`** (standart) — variantli test
2. **`fill_blank`** — matnli javob; `accepted_answers` massivida bir nechta
   to'g'ri yozilish shakli bo'lishi mumkin
3. **`matching`** — juftliklarni mos keltirish (`pairs` massivi)

Har bir savol `"module"` maydoni orqali tegishli bo'limga bog'langan bo'lishi
**shart** — aks holda o'sha modulda mashq/test ishlamaydi.

## O'yinlashtirish (gamification)

- **Unvonlar:** 🌱 Yangi boshlovchi → 📖 O'quvchi → 🎯 Bilimdon → 🏅 Usta → 👑 Professor
- **Nishonlar:** birinchi test, 100% natija, streak bosqichlari (3/7/14/30
  kun), test soni bosqichlari (10/50/100), **kursni to'liq tugatish**
  sertifikat-nishoni (har til/bo'lim uchun alohida)
- **Streak "yumshoq qo'nish"** — 2-3 kun tanaffusdan keyin streak butunlay
  emas, yarmiga tushadi (Duolingo'dagi streak freeze tamoyiliga yaqin)
- **Hazil-mutoyibali xabarlar** — har javobdan keyin tasodifiy tanlangan
  rag'batlantiruvchi ibora

## Xavfsizlik

Oldingi versiyada qo'shilgan barcha choralar saqlanib qolgan: `.env`-based
token, HTML-injection himoyasi, faqat shaxsiy chat, sessiya egasini
tekshirish, anti-flood, callback-data validatsiyasi, xatolarni ushlash,
parametrlashtirilgan SQL so'rovlari. Admin buyruqlari faqat `ADMIN_IDS`
ro'yxatidagilar uchun ishlaydi.

## Kengaytirish

### Yangi bo'lim (modul) qo'shish
1. `curriculum_en.json`/`curriculum_ru.json`ga yangi obyekt qo'shing
   (`id`, `order` — ketma-ketlikni buzmang, `title`, `text_explanation`).
2. Shu `module` ID bilan kamida 4-5 ta savol yozing (`questions_*.json`).
3. `/setvideo` va `/setaudio` bilan video/audio biriktiring (ixtiyoriy).

### Savol formati
```json
{"id":"...", "section":"grammar", "module":"present_simple", "type":"mcq",
 "question":"...", "options":["A","B","C","D"], "correct":0, "explanation":"..."}

{"id":"...", "section":"grammar", "module":"...", "type":"fill_blank",
 "question":"...", "accepted_answers":["..."], "explanation":"..."}

{"id":"...", "section":"vocabulary", "module":"...", "type":"matching",
 "question":"...", "pairs":[{"left":"...","right":"..."}], "explanation":"..."}
```

## Keyingi qadamlar (tavsiya)

- [ ] Rasm/audio biriktirilgan savollar (infratuzilma tayyor — `message.answer_photo`
      qo'shish kifoya, kontentni o'zingiz yuklaysiz)
- [ ] Production'da `MemoryStorage` o'rniga Redis-based FSM storage
      (server qayta ishga tushganda sessiyalar yo'qolmasligi uchun)
- [ ] Har bir bo'lim uchun ko'proq savol qo'shish (hozir 4-8 tadan — real
      foydalanishda kamida 15-20 ta tavsiya etiladi)
- [ ] Sentry yoki shunga o'xshash xato kuzatuv tizimi
- [ ] Placement testni to'liq adaptiv (IRT) qilish — hozirgi versiya
      soddalashtirilgan (har bo'limdan bittadan savol)
