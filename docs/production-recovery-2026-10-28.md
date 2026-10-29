# Production yangilanishi va #1 instansiya — 2026-10-28

## 2026-10-29: internet tiklangandan keyingi tekshiruv

Foydalanuvchi oldingi uzilish vaqtida serverning interneti o‘chganini aniqladi. Internet tiklangach SSH, bevosita va ommaviy `/portal/health` yana ishladi (HTTP 200). Avvalgi relay kuzatuvlari internet uzilishining o‘rniga tasdiqlangan asosiy sabab sifatida talqin qilinmasligi kerak. Mac’dagi vaqtinchalik DERP afzalligi avvalroq standart holatga qaytarilgan; serverga shu sozlama qo‘llangan deb hisoblanmaydi.

### Tezlikdagi yangi o‘lchov va tuzatish — 2026-10-29

Production’dagi 13 Chrome worker bekor turganida 10 soniyalik namuna Chrome CPU’sini **976%** deb o‘lchadi. Buning **914.6%** qismi GPU process’laridan edi; Chrome `--use-angle=swiftshader` bilan sun’iy 3D ishlatayotgan edi. CPU esa oldinroq 91°C bo‘lib, `intel_powerclamp cur_state=49` bilan cheklangan.

`SF_CHROME_GRAPHICS=basic` qo‘shildi: chat matni, odatiy rasm, upload va VNC uchun GPU’siz 2D ko‘rinish ishlaydi; WebGL talab qilinsa `software-gl` hali ham tanlanadi. Bir Gemini tabini yolg‘iz o‘lchaganda, dekorativ fon CSS animatsiyalari o‘chirilishidan oldin Chrome jami **83.6% CPU** (`GPU 7.8%, renderer 69.8%`), keyin jami **6.3%** (`GPU 1.0%, renderer 2.4%`) bo‘ldi. Bu bir tabning 10 soniyalik namunasidagi farq, umumiy server yoki model tezligining foizi emas.

Gemini yangi chat sahifasidagi animatsion rangli fon javob/progress animatsiyasi emas; uning `.nl-blob` va `.gradient-strip` qatlamlari statik qilindi. Boshqa javob animatsiyalari saqlanadi. 13 tabni qayta yoqqandan keyingi 10 soniyalik barcha Chrome namunasida GPU process **132.2%**, renderer **494.2%** va jami **788.1%** edi. Shu davrdagi load average oldingi 92 atrofidan taxminan **26.5** bo‘ldi; test sharoiti bir xil emas va CPU harorati/powerclamp bilan tezlik hanuz cheklanishi mumkin.

Ruscha Gemini reasoning menyusida `Низкий / Средний / Высокий` yozilganini ko‘rdim; adapter esa `Низкая / Высокая` qidirayotgan edi. Shu sabab menyuni qayta-qayta tekshirib, so‘ralgan darajani qo‘llamay qolishi mumkin edi. Hozirgi va avvalgi ruscha shakllar hamda inglizcha nomlar qo‘shildi; so‘ralgan daraja allaqachon tanlangan bo‘lsa, qayta bosilmaydi. Ilgari yuborish ikki marta tasdiqlanmagan, shuning uchun Gemini yo‘li alohida jonli sinovdan o‘tishi kerak.

Бүрэн ачаалалтай өмнөх Gemini test (`17 + 26`, ижил stream API хэлбэр) **202.22 s**-д алдаа өгсөн. Нэг tab тусгаарлаж, график/дэвсгэрийн хөнгөн горимтой хийсэн ижил төрлийн test **38.75 s**-д `43` зөв текст болон бүрэн SSE төгсгөл өгсөн (**first real text 38.74 s**). Нэг удаагийн үр дүн тул бүх хэрэглэгч, дараалсан урт chat, өндөр parallelism-д амлалт болохгүй. Шууд adapter-ийн тусгаарласан test 19.26 s байсан нь ижил API зам биш. Gemini-д дахин 3 дараалсан API давталт, одоо бусад бүх worker ажиллаж байх үеийн p50/p95, бусад model/media/file/tool зэрэг бодит speed аудит хараахан батлагдаагүй.

`0a278f8` production cluster image revision-д орсон; өмнөх image `ai-gateway-cluster:rollback-speed-20261029` нэрээр үлдсэн. Browser launcher, Gemini page preparation, graphics горимыг шалгасан **868 passed, 1 skipped** backend tests (98.83 s), өмнөх focused тестүүд, `bash -n`, `compileall`, `git diff --check`, тусдаа Playwright DOM/interaction test давлаа. Эдгээр нь кодын нотолгоо болохоос бүх upstream модель ажиллаж байгааг батлахгүй.

`f250831` revision’da ruscha Gemini effort nomlari va tanlangan darajani qayta bosmaslik tuzatishi qo‘shildi; bu image production’ga chiqdi (`13291aef4559`), oldingi `0a278f8` image `ai-gateway-cluster:rollback-gemini-speed-20261029` sifatida saqlandi. To‘liq qayta ishga tushirgandan keyin `/health` `ok`, 13 worker topildi, Supervisor’dagi 54 xizmat ishga tushdi; public health HTTP 200 berdi.

Yakuniy cache’siz Gemini stream probe (`17 + 26`, `reasoning_effort=none`) **33.65 s**da `43`ni qaytardi, birinchi haqiqiy matn **33.64 s**da keldi, `finish_reason=stop` va `[DONE]` to‘liq keldi. Server log phase’lari: `new_chat 5.6 s`, `page 1.02 s`, `prepare 9.05 s`, `send 9.21 s`, `answer 8.56 s`. Oldingi shu yo‘l **202.22 s**da javobsiz tugagan edi. Bitta muvaffaqiyatli probe p50/p95 yoki barcha provayderlar uchun kafolat emas; yuborish va new-chat phase’lari hali asosiy latency ulushidir.

Yakuniy 10 soniyalik barcha Chrome CPU namunasi jami **337.8%**, GPU process **77.7%**, renderer **232.3%** bo‘ldi. Dastlabki idle namuna jami **976%**, GPU process **914.6%** edi. O‘lchovlar turli ish holatida olingan; natija sezilarli pasayishni ko‘rsatadi, ammo serverning 91°C harorati va qizishdan himoyasi qolmoqda. Testdan keyin `reset_chat failed: TimeoutError` fon prewarm log’da ko‘rindi, lekin foydalanuvchi so‘rovi muvaffaqiyatli yakunlangan; keyingi prewarm/recovery uchun alohida kuzatuv kerak.

Qayta yuklangandan keyingi session probe 15 tabdan 13 tasini sog‘lom ko‘rsatdi; Gemini 9222 va ChatGPT 9226 timeout berdi, boshqa tablar `ok`. Bu instansiyalarni timeout’dan tezroq chiqarish, shuningdek qizishni kamaytirish keyingi ishlar bo‘lib qolmoqda. Tool, fayl, image generation/edit va barcha model bo‘yicha to‘liq jonli tezlik taqqoslash hali bajarilmagan.

Yangi holat avvalgi rolloutdan farq qiladi: `/home/banda/Desktop/aicollections/ai-gateway` va avvalgi rollback katalogi topilmadi. Home ichida compose fayllari qidiruvi ham natija bermadi; foydalanuvchi papkaga tegmaganini aytdi. Yo‘qolish sababi aniqlanmadi. Mavjud bo‘lmagan loyiha katalogi mahalliy Git tarixidan to‘liq bundle orqali qayta yaratildi (`6aefc04` gacha). Mavjud katalog ustidan yozilmadi.

API va database ishlayotgan, lekin `ai-gateway-cluster-1` to‘xtagan edi: `2026-10-29 13:35:35 +05:00` da SIGTERM olgan va exit code 0 bilan chiqqan; OOM belgisi yo‘q. To‘xtatishni kim yoki nima bergani aniqlanmagan. Shu sabab portalning HTTP 200 javobi brauzer modellari ishlayotganini isbotlamaydi. Eski cluster image `5064982` (`e5a74415bc23`) edi. Profillar joylashgan `ai-gateway_cluster-data` volume mavjud; amaldagi container konfiguratsiyasi maxfiy katalogga saqlandi (`~/.ai-gateway-recovery-20261029/`, mode 700).

`81af006` endi **production’ga chiqarildi**. Yangi cluster image: `15d52a1ddc16`, revision label `81af006`. Avvalgi `e5a74415bc23` image asosida faqat `entrypoint.sh` almashtirildi; Chrome va kutubxonalar yangilanmadi. Eski image `ai-gateway-cluster:rollback-20261029` tag’ida saqlandi. Faqat cluster konteyneri almashtirildi; API, database va search konteynerlarining ID’lari o‘zgarmadi.

Yo‘qolgan `.env` va production’dagi qo‘shimcha source fayllarining aynan asl nusxalari topilmadi. Shu sabab eski compose faylini taxminiy sozlamalar bilan ishga tushirish o‘rniga, saqlangan `docker inspect` ma’lumotidan **faqat cluster** uchun konfiguratsiya tiklandi. Kalit qiymatlari chiqarilmadi. Yangi konteynerning environment’i, hostname, command, entrypoint, profile volume, portlar, network, restart policy, capability/security sozlamalari va resurs cheklovlari eski konteyner bilan dasturiy solishtirildi: farq topilmadi. API’ning amaldagi Gunicorn va Telnyx environment’i shu konteyner ichida saqlandi; hostdagi yo‘qolgan barcha custom source fayllar tiklandi degan kafolat berilmaydi.

Operatsion buyruq: `scripts/run-recovered-cluster.sh up -d --no-deps --no-build cluster`. U maxfiy `~/.ai-gateway-recovery-20261029/cluster.compose.json` faylidan foydalanadi (mode 600), asl named volume va network’ni external sifatida ulaydi. Wrapper Git’dan tashqarida. Hozirgi tiklash faqat cluster xizmati uchun; oddiy to‘liq `docker compose up` qilishdan oldin boshqa yo‘qolgan production konfiguratsiyasini ham tiklash kerak. `profile-check` yordamchi nusxasi tekshirilmagan deb belgilandi: uni original volume o‘rniga avtomatik tiklash mumkin emas.

Rolloutdan keyin barcha **54 ta Supervisor xizmati RUNNING**: 13 tadan Chrome, Xvfb, openbox va VNC, cluster va heal-watch. `/health` `ok` berdi; dastlabki snapshot 0–12 instansiyalarni `ok` ko‘rsatdi. Keyingi haqiqiy session probe 15 tabdan 12 tasini sog‘lom deb topdi; Gemini 9223 va Qwen 9229/9234 tablari timeout berdi. 9223 (#1) alohida qayta probe’da `ok` bo‘ldi. Demak ekran/startup nosozligi tuzatilgan, ammo yuk ostidagi session timeout’lar hanuz mavjud. Bular modeldan yakuniy javob olish sinovi emas.

Yangi, cache’dan tashqari Gemini generation sinovi (`17 + 26`, `reasoning_effort=none`, streaming) **202.22 s**da `upstream_failed` bilan tugadi. Birinchi haqiqiy matn ham, yakuniy javob ham yo‘q; stream xato va `[DONE]` bilan yopildi. Log’da 9222 da Quill orqali kiritilgan so‘rov yuborilishi tasdiqlanmagani va `could not type into the input field`, 9223 da temporary chat yoqilishi tasdiqlanmagani ko‘rindi. Bu latency yaxshilanganining dalili emas. Qizish yagona sabab deb olinmaydi: adapterdagi yuborish/reset yo‘lini ham tuzatish va qayta sinash kerak. Issiqlik cheklovi ostida keng parallel/media sinovlari bajarilmadi; barcha model, tool, fayl va media bo‘yicha yakuniy e2e hukm ochiq qoladi.

Brauzer xizmati to‘xtagan holda ham `sensors` CPU package haroratini **91°C** (high 80°C, critical 100°C) va powerclamp `cur_state=49` deb ko‘rsatdi. Load avvalgi 125–128 dan taxminan 4.44 ga pasaygan bo‘lsa ham, issiqlik cheklovi qolgan. Brauzerlar qayta yoqilgach load **116.86**, cluster CPU **1111.41%** kuzatildi. Sovitish tizimini jismonan tekshirish kerak; termal himoya o‘zgartirilmadi. Foydalanuvchi kompyuter institutda ekanini va hozir sovitgichni tekshira olmasligini aytdi.

## 2026-10-28 holati: chiqarilgan o‘zgarishlar

- `9b4ee10`: avvalgi end-to-end audit tuzatishlari serverga chiqarildi.
- `26c70e4`: eski X ekran qulflari va ishlamay qolgan ekran xizmatlarini tiklash chiqarildi.
- `5064982`: Qwen tayyorlashini fon vazifasiga o‘tkazish va X socket katalogini root tomonidan oldindan yaratish chiqarildi. Shutdown fon vazifalarining bekor qilinishini kutadi.

Production Git shu o‘zgarishlarni o‘z ichiga oladi. API image revision: `26c70e4`; cluster image revision: `5064982`. Oxirgi commit API kodini o‘zgartirmaydi. Avvalgi Docker image’lar asos qilib olindi: bu rollout Chrome yoki kutubxona versiyalarini yangilamadi. Serverning mavjud lokal konfiguratsiyasi, Gunicorn sozlamalari, profillar va akkauntlar saqlandi.

## Rasmdagi nosozlik sababi

#1 uchun `chrome-1`, `xvfb-1`, `openbox-1` va `x11vnc-1` Supervisor’da FATAL bo‘lgan. `/tmp/.X100-lock` va `/tmp/.X11-unix/X100` eski ekran jarayonidan qolgan; lock ichidagi PID 71 yangi ishga tushishda boshqa ekranga tegishli bo‘lib qolgan. Docker stop/start `/tmp`ni saqlaydi. Shu sababli oddiy PID tekshiruvi eski qulfni haqiqiy faol ekran deb qabul qiladi.

Avvalgi avtomatik tiklash faqat Chrome’ni qayta ishga tushirar edi. X ekrani ishlamagani uchun bu yordam bermagan va holat “tiklashdan keyin ham javob yo‘q” deb qolgan.

Yangi launcher avval shu ekranning Unix socket’larini tekshiradi. Haqiqatan ishlayotgan socket yoki noaniq xato bo‘lsa fayllarni o‘chirmaydi. Eski socket va lock’ni faqat faol listener yo‘qligida tozalaydi. Linux abstract socket ham tekshiriladi. Tiklash yordamchisi ishlamay qolgan ekran va unga bog‘liq xizmatlarni Chrome’dan oldin ishga tushiradi; boshqa instansiyalarni qayta ishga tushirmaydi.

Yordamchidagi eski 0–9 chegarasi ham olib tashlandi: konfiguratsiyadagi `SF_MAX_INSTANCES=13` bo‘yicha 10, 11 va 12-instansiya ham tiklana oladi. Ruxsat etilmagan va noto‘g‘ri raqamlar rad etiladi.

## Tekshiruvlar

- Yakuniy backend: **859 passed, 1 skipped / 134.85 s**. Bu testlar vaqti, model tezligi emas.
- Bash sintaksisi, Python compile va `git diff --check`: o‘tdi.
- Mac’da o‘tkazilmagan Linux abstract socket tekshiruvi serverdagi alohida konteynerda bajarildi.
- Shu izolyatsiyalangan konteynerda haqiqiy Xvfb eski, tirik PID’ga ishora qiladigan qulf bilan ishga tushmadi. Yangi launcher aynan shu holatda haqiqiy Xvfb’ni ishga tushirdi. Ishlayotgan oddiy va abstract socket’lar saqlandi. Production profillari bu sinovga ulanmagan.
- #1 Gemini sessiyasi rolloutlar orasida alohida probe bilan `ok` deb tasdiqlandi.
- `5064982` ishga tushirilgach #1 sessiyasi yana `ok` bo‘ldi; `/cluster/instances` 0–12 instansiyalarni `ok` deb ko‘rsatdi. Bu generation testi emas. X socket katalogining egasi root, rejimi 1777 ekani ham tekshirildi.
- Shu cold start’da 6 ta VNC yordamchi xizmati standart qisqa qayta urinish chegarasi sababli FATAL bo‘ldi; ularni alohida start qilish muvaffaqiyatli bo‘ldi. Lokal `81af006` commit qayta urinishlar sonini 20 ga oshiradi. 15:50 da ulanish yana uzilgani sababli bu oxirgi operatsion sozlama hali serverga chiqarilmadi.
- Ommaviy va ichki portal health endpointlari HTTP 200 berdi. Restart paytida olingan ayrim so‘rovlar startup tugaguncha javob bermadi.

## Ochiq qolgan jonli kuzatuvlar

Bu ish barcha modellarda to‘liq e2e audit tugaganini bildirmaydi.

`26c70e4` bilan 15 ta worker sessiyasi tekshirildi: 13 ta sog‘lom, Qwen’ning 9229 va 9234 portlaridagi bittadan tabi `probe_failed:TimeoutError` berdi. #8 Venice va #9 ChatGPT dastlab yuklanish davrida nosoz ko‘ringan, keyingi alohida probe’da ikkalasi ham `ok` bo‘lgan.

Gemini’ga cache’dan tashqari qisqa arifmetik so‘rov yakuniy javob bermadi: o‘lchov klienti **113.75 s**da timeout oldi. Log’da birinchi worker reset timeout’i, keyingi worker’larda temporary chat yoqilmagani va yana boshqa worker’ga urinish ko‘rindi. Ushbu bitta sinovdan tezlik foizi yoki p95 chiqarib bo‘lmaydi. Bu muvaffaqiyatli generation testi emas. Oxirgi startup tuzatishi Qwen tayyorlashining boshqa provayderlar uchun port ochilishini bloklashini olib tashlaydi; o‘zi Gemini generation timeout’ini hal qildi degan dalil yo‘q.

Haqiqiy image generation/edit, fayllar, uzun agent suhbatlari va barcha katalog modellari bo‘yicha yakuniy jonli sinov hali tugamagan.

## Serverning hisoblash cheklovi

Tekshiruv paytidagi faktlar:

| Ko‘rsatkich | Kuzatuv |
| --- | --- |
| CPU | Intel i7-12700K, 20 mantiqiy CPU |
| CPU harorati | 93°C (`x86_pkg_temp`) |
| Intel powerclamp | `cur_state=49` |
| Load average | 125.30; keyingi kuzatuvda 128.16 |
| CPU pressure | `some avg10=87.43%` |
| Bo‘sh/foydalanish mumkin RAM | taxminan 21 GB available, swap 0 |
| Disk | 38% band |
| Cluster CPU | boshqa kuzatuvda taxminan 840% (8.4 mantiqiy CPU) |

Qizishdan himoya faol bo‘lib, CPU imkoniyatining katta qismini vaqtincha cheklayapti. Bu kechikishlarga hissa qo‘shishi mumkin; barcha timeout’larning yagona sababi ekani isbotlanmagan. Sovitish tizimi, ventilyator/pompa va havo aylanishini tekshirish kerak. Harorat himoyasi o‘zgartirilmadi.

15:30 atrofida Tailscale ulanishi yana uzildi; foydalanuvchi qayta ulaganidan keyin tiklandi. 15:50 da yana offline bo‘ldi. Taxminan 20 daqiqalik oraliq avtomatik uyqu sozlamasini ham tekshirishga asos bo‘ladi, lekin hozircha bu sabab tasdiqlanmagan. Uzilishlarning qizish bilan bog‘liqligi ham isbotlanmagan.

Keyingi ulanish tekshiruvida server “Connected” deb ko‘ringaniga qaramay SSH, IPv4/IPv6 va health ulanmagan. Mac’dan VPS’ga bevosita Tailscale UDP ping ishladi. `tailscale debug derp waw` Varshavadagi uch relay’ning IPv4 TCP 443 ulanishlari rad etilganini ko‘rsatdi; Frankfurt tekshiruvida uch relay bilan DERP ulanishi muvaffaqiyatli bo‘ldi. Bu kuzatuv “server uxlagan” xulosasini tasdiqlamaydi: tarmoq/relay yo‘li ham ishlamayapti. Frankfurt’ni vaqtinchalik afzal ko‘rish orqali ulanishni tiklash urinilmoqda. Serverning uyqu sozlamalari hali o‘qilmadi va o‘zgartirilmadi.

## Qaytarish uchun saqlangan holat

Serverda boshlang‘ich lokal diff va konfiguratsiya: `~/ai-gateway-rollbacks/audit-2026-10-28-9b4ee10/`. Oldingi image’lar `ai-gateway-api:rollback-20261028`, `ai-gateway-cluster:rollback-20261028` va oraliq `ai-gateway-cluster:display-recovery-26c70e4` tag’larida saqlandi. Database yoki akkaunt profillari almashtirilmadi.
