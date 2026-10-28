# Production yangilanishi va #1 instansiya — 2026-10-28

## Chiqarilgan o‘zgarishlar

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
