# English Path · CEFR va IELTS maqsad yo‘nalishlari

Telegram orqali boshqariladigan, yopiq Beginner A1 kursining boshlang‘ich loyihasi. O‘quvchi darsni ketma-ket bajaradi; keyingi dars test muvaffaqiyatli tugagandan keyin ochiladi. Har bir darsda mos darslik va mashqlar daftari sahifalari rasm-albom bo‘lib yuboriladi; o‘quvchi rasmni kattalashtirib, sahifadagi vazifani ko‘radi. Xabarning o‘zida shu mavzuga xos sodda izoh, tarjimali misollar, 6 ta tegishli lug‘at va kichik amaliy topshiriq beriladi. Manba kitob nomi o‘quvchi interfeysida chiqmaydi.

## Ishga tushirish

1. Docker Compose o‘rnating va `.env.example` dan `.env` yarating.
2. `BOT_TOKEN`, `EXPECTED_BOT_USERNAME`, `ADMIN_IDS`, `ADMIN_API_TOKEN`, `POSTGRES_PASSWORD` va ixtiyoriy `OPENAI_API_KEY` qiymatlarini to‘ldiring. Bot username’ini `@` belgisiz kiriting. Admin tokeni va DB parolini o‘zaro takrorlamaydigan kuchli qiymatlarga almashtiring.
3. `docker compose up --build`.
4. Docker Compose’da o‘qituvchi paneli: `http://127.0.0.1:8000`.

Docker’siz Windows’da ishlatganda `.env` da `DATABASE_URL=sqlite+aiosqlite:///./english_path.db` va `REDIS_URL=memory://` bo‘lsa, alohida PostgreSQL/Redis shart emas. `.env.example` dagi lokal admin panel porti `http://127.0.0.1:8001`. Bu usul faqat mahalliy sinov uchun; qayta ishga tushganda vaqtinchalik sessiya holatlari tozalanadi. Ishlab chiqarish va ko‘p foydalanuvchi uchun Docker Compose’dagi PostgreSQL/Redis konfiguratsiyasidan foydalaning.

Windows CMD’da `py bot.py` bilan ishga tushirish uchun avval loyiha papkasida:

```bat
copy .env.example .env
notepad .env
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
py bot.py
```

`.env` ichida `BOT_TOKEN`, `EXPECTED_BOT_USERNAME`, `ADMIN_IDS`, `ADMIN_API_TOKEN` to‘ldiring. Startup Telegram tokeni mos bot username’iga tegishli ekanini tekshiradi. Lokal usulda `.env` dagi `DATABASE_URL` va `REDIS_URL` ni SQLite/xotira qiymatlarida qoldiring. `APP_PORT=8001` kabi boshqa portni tanlang, agar 8000 band bo‘lsa.

Bot Telegram long polling bilan ishlaydi; admin API `http://127.0.0.1:8000` da faqat loopback manzilga bog‘lanadi. Production’da HTTPS reverse proxy, maxfiy qiymatlar menejeri, backup, monitoring, webhook secret va migratsiyalar bilan deploy qiling. Bir nechta bot ishchi jarayonini ko‘paytirishdan oldin pollingni bitta scheduler/leader bilan ajrating.

## Admin buyruqlari

- `/admin` – faqat `ADMIN_IDS` dagi adminlarga.
- `/invite [foydalanish_soni]` – 3 kunlik, bir yoki bir nechta o‘quvchiga mo‘ljallangan taklif kodi yaratish; o‘quvchi `/join KOD` bilan kursga kiradi. Bevosita qo‘shish uchun `/grant <telegram_id>`, kirishni yopish uchun `/revoke <telegram_id>`.
- `/video <unit.lesson>` – keyin yuborilgan Telegram videosini darsga biriktirish; yoki `/video <unit.lesson> <https://...>`.
- `/stats` va `/report` – ro‘yxatdan o‘tgan o‘quvchilar, quiz natijalari, sust o‘zlashtirilgan darslar va e'tibor talab qiladigan holatlar.
- `/homeworks` va `/feedback <id> <matn>` – yuborilgan uy vazifasini tekshirish va fikr bildirish.
- `/learners` va `/questions` – o‘quvchi faolligi va savollar.
- `/ask <savol>` – AI yordamchiga savol. AI faqat `OPENAI_API_KEY` sozlanganda yoqiladi.

Admin Telegram ID’larini bot orqali o‘quvchilar ko‘ra olmaydi. Admin API bearer kaliti hamda Telegram admin allowlist tekshiruvi bilan himoyalangan. Bot tokeni va AI kalitini repoga yozmang.

## Kontent va navbat

`lesson_guides.json` 40 darsga mos sodda izoh, tarjimali misol, tezkor mashq va mavzuga xos lug‘atni saqlaydi. `assets/lesson_pages/` ichidagi rasm-albomlar darslik va mashqlar daftaridan mos sahifalarni ko‘rsatadi; asl PDF fayllari bot konteyneriga qo‘shilmaydi.

`course.json` da hozirgi A1 kursining 10 bo‘limi va 40 darsi bor. Har bir dars avval qisqa kartada ko‘rinadi; o‘quvchi qo‘llanmani, tarjimali misollarni va oltita so‘zni kerak bo‘lganda alohida ochadi. So‘z kartochkalarida tarjima avval yashirin turadi, o‘quvchi eslab ko‘rgach tugma bilan ko‘radi va keyingisiga o‘tadi. `assets/lesson_pages/` botda ko‘rsatiladigan sahifa rasmlarini saqlaydi. O‘quvchi kuniga 1, 2, 3 darslik maqsad tanlashi hamda Toshkent vaqti bilan 18:00, 19:00 yoki 20:00 eslatmasini yoqib/o‘chirishi mumkin. Eslatma sukut bo‘yicha o‘chiq.

`learning_pathways.json` CEFR A1–C2 yo‘lini va IELTS Foundation/Skills/Practice yo‘lini belgilaydi. Hozir faqat A1 kursi faol; yuqori CEFR darajalari va IELTS kursi o‘quv kontenti hali yozilib, o‘qituvchi tomonidan ko‘rib chiqilishi kerak. O‘quvchi Academic yoki General Training va maqsadli bandini profilida tanlaydi; bu rasmiy ball kafolati emas. `question_banks.json` har darsga 25 savol, `activities.json` 25 mashg‘ulot beradi. Har urinishda 5 savol chiqadi; hozir o‘tish chegarasi 4/5 (80%). Savollar bankdagi savollar tugamaguncha takrorlanmaydi. `lesson_links.json` dars-bet mosligini saqlaydi; o‘quvchi botda kitob nomini ko‘rmaydi. Har 3 darsdan so‘ng mavzuga mos lug‘at takrorlanadi.

AI hozir A1 savollariga o‘zbekcha javob beradi va savolni o‘qituvchiga yuboradi; u kurs sozlamalarini yoki o‘quvchi ruxsatini o‘zgartirmaydi. O‘qituvchi har kuni 7 kundan beri faol bo‘lmaganlar haqida eslatma oladi.

Bu — funksional MVP, yakuniy ommaviy mahsulot emas. O‘nta bo‘lim uchun original sarguzasht muqova rasmlari bor; haqiqiy dars videolarini o‘qituvchi `/video 1.1` buyrug‘i bilan yuklaydi. Kitob sahifalari ko‘rsatiladi, PDF bot orqali tarqatilmaydi. Keyingi daraja ochilishi hozir o‘qituvchi tomonidan boshqariladi. Minglab foydalanuvchiga chiqarishdan oldin versiyali migratsiya, audit va ma'lumotni saqlash siyosati, backup/restore amaliyoti, monitoring, ko‘p worker uchun webhook/navbat arxitekturasi hamda yuklama sinovlari kerak. Hozirgi Telegram polling bitta bot jarayonida ishlaydi; bir xil token bilan bir vaqtda bir nechta bot nusxasini ishga tushirmang.
