# Repetitorlik Telegram Bot — Full Structured Course

Ingliz va Rus tillari: placement → video → nazariya → mashq → unlock.
Savollar ko'paytirilgan, rasm/audio, referral, premium, sertifikat, Sentry, backup.

## So'nggi yangilanish (24.09.2026)

### Xavfsizlik
- `/premium` endi **faqat admin** uchun — avval har kim o'ziga premium yoqa olardi
- Deep-link referral kodi validatsiyasi (faqat A–Z, 0–9, 4–16 belgi)
- Jamoa kodi validatsiyasi (4–12 belgi, harf/raqam)
- Speaking uchun vaqtinchalik ovoz fayllari avtomatik o'chiriladi
- Throttling xotirasi cheklangan (server uzoq ishlaganda RAM o'smaydi)
- Foydalanuvchi nomi/ismini `None` bilan o'chirib tashlash muammosi tuzatildi (COALESCE)
- `reportlab` o'rnatilmagan bo'lsa ham bot ishlaydi (sertifikat graceful disable)

### Buglar
- Jamoaga kod bilan qo'shilish ishlamasdi (matnli handler bloklab qo'yardi) — tuzatildi
- Tilni almashtirganda qayta placement test majburlanardi — tuzatildi
- "Juftlik poygasi" o'yini noto'g'ri "bepul mashq" sifatida saqlanardi — tuzatildi
- `/broadcast` faqat 500 kishiga yuborardi — endi barcha foydalanuvchilarga

### UX / uslub
- Bosh menyu ixcham va guruhlangan (8 qator)
- Matnlar professional ohangda qayta yozildi
- Progress indikatori (▰▰▰▱▱) — daraja, statistika va modul yakunida
- Kunlik challenge endi haqiqatan kunlik (bir kunda bitta, hamma uchun bir xil)

## Tezkor start

```bash
pip install -r requirements.txt
cp .env.example .env   # BOT_TOKEN=...
python bot.py
```

## Yangi imkoniyatlar (shu versiya)

### Kontent
| Til | Modullar | Mashqlar (taxminan) |
|-----|----------|---------------------|
| EN  | 12       | 150+                |
| RU  | 10       | 100+                |

- Har modulda **12–18** ta mashq (takrorlanmaslik uchun yetarli)
- **Rasm savollar**: `image_file` yoki `image_url` maydoni
- **Audio talaffuz**: `audio_file` yoki `audio_url`
  - Fayllar: `media/images/`, `media/audio/`

### O'sish
- **Referral**: `/start REFCODE` yoki menyu → Do'stni taklif
  - Referrer +50 XP, yangi user +30 XP + nishonlar
- **Kanal**: `CHANNEL_URL` — menyuda tugma
- **BOT_USERNAME** — deep-link havola uchun

### Monetizatsiya
- **Premium**: cheksiz mashq, sertifikat
  - Faqat admin: `/premium` (o'ziga 30 kun) yoki `/premium <telegram_id> [kunlar]` (`ADMIN_IDS`)
- **Sertifikat**: darajadagi barcha modullar tugagach PDF (reportlab)

### Texnik
- **Sentry**: `SENTRY_DSN` bo'lsa avtomatik ulanadi
- **Backup**: `./backup.sh` — `backups/` ga nusxa (cron: `0 3 * * *`)
- SQLite: referral, premium, certificates jadvallari

## Fayllar

```
repetitor_bot/
├── bot.py
├── database.py
├── certificate.py
├── curriculum_en.json / curriculum_ru.json
├── backup.sh
├── media/images/  media/audio/
├── certificates/   (avtomatik)
├── requirements.txt
├── .env.example
└── README.md
```

## Curriculumga rasm/audio qo'shish

```json
{
  "id": "en_b_05_img1",
  "type": "mcq",
  "question": "Bu nima?",
  "image_file": "apple.jpg",
  "options": ["Apple", "Banana"],
  "correct": 0,
  "explanation": "..."
}
```

`media/images/apple.jpg` qo'ying. URL uchun `image_url` ishlating.


## Admin panel

`.env` da `ADMIN_IDS=123456789` (Telegram ID, vergul bilan bir nechta).

| Buyruq / menyu | Vazifa |
|----------------|--------|
| `/admin` | Admin bosh menyu |
| Umumiy statistika | Users, premium, testlar, faollik |
| O'quvchilar | Ro'yxat, sahifalash, batafsil kartochka |
| `/find <id\|@user\|id:N>` | Qidiruv |
| Darsliklar | EN/RU modullar + mashqlar soni/ko'rinishi |
| Premium berish | Kartochkadan yoki `/premium <tg_id> [days]` |
| `/broadcast matn` | Barcha userlarga xabar |
| Level o'zgartirish | beginner/intermediate/advanced |
| Modul ochish | Foydalanuvchi uchun butun darajani unlock |

O'quvchi kartochkasida: XP, streak, premium, referral, modul progress, so'nggi testlar.


## Production

1. VPS (Ubuntu) yoki Railway/Render
2. `.env` da token, SENTRY_DSN, BOT_USERNAME, CHANNEL_URL
3. systemd yoki `screen`/`tmux`:
   ```bash
   python bot.py
   ```
4. Cron backup:
   ```
   0 3 * * * /path/to/repetitor_bot/backup.sh
   ```
5. MemoryStorage o'rniga Redis (ko'p foydalanuvchi uchun)

## Pedagogika

- Placement → to'g'ri daraja
- Video majburiy → nazariya → test (mastery)
- Progressive unlock (sakrash yo'q)
- Varied practice (seen exercises)
- Gamification: XP, streak, badges, referral

## Keyingi qadamlar

- [ ] Haqiqiy YouTube videolarini o'zingizniki bilan almashtirish
- [ ] media/ ga rasm va ovoz fayllari
- [ ] Telegram Stars / to'lov
- [ ] TTS og'zaki nazariya
- [ ] Kunlik eslatma (reminder job)


## Ko'nikmalar (yangi)

- **Reading** — qiziqarli matnlar + tushunish savollari
- **Listening** — dialog/matn; o'qituvchi audio biriktirishi mumkin
- **Speaking** — ovozli javob → AI tahlil → admin/o'qituvchiga xabar
- **Kunlik challenge** — tasodifiy speaking/topshiriq

### O'qituvchi

- `/admin` → Speaking navbati (tinglash, qabul/qayta)
- `/setvideo en en_b_01 https://...` — modulga video
- Video/audio yuborib caption: `video:en:en_b_01`

### AI kalit (ixtiyoriy)

`.env`: `OPENAI_API_KEY` yoki `GROQ_API_KEY` — speaking uchun boyroq feedback.

## Qo'shimcha modullar (to'liq)

### 🧠 SRS takrorlash
Spaced repetition — zaif so'zlar tez-tez chiqadi. Menyu: **SRS takrorlash**.

### 🎮 Mini-o'yinlar
- So'z topish (hint bo'yicha)
- Juftlik poygasi

### 👥 Jamoa challenge
Haftalik XP reytingi. Jamoa yaratish / kod bilan qo'shilish.

### 🎤 Whisper
`OPENAI_API_KEY` yoki `GROQ_API_KEY` bo'lsa speaking matnga aylanadi va AI chuqurroq tahlil qiladi.

### 📖 Hikoya rejimi
Tanlovli sarguzasht (A/B) — o'qib tanlang, oxirida XP.
