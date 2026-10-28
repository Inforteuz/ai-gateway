# AI Gateway: tezlik, agent, media va fayllar auditi — 2026-10-27

**2026-10-28 yangilanishi:** quyidagi audit tuzatishlari production’ga chiqarildi. #1 instansiya, startup kechikishi, jonli sinovdagi qolgan timeout’lar va server harorati bo‘yicha natijalar [keyingi hisobotda](production-recovery-2026-10-28.md). Quyidagi “joylanmadi” qaydlari 27-oktabrdagi holatni ifodalaydi.

**Hukm: hali “hamma modelda muammo yo‘q” deb tasdiqlab bo‘lmaydi.** Quyidagi xatolar mahalliy kodda tuzatildi va sinovlardan o‘tdi. Production serverga ulanish tiklanmagani sababli bu audit davomida yangilanish joylanmadi, haqiqiy modellar bilan yangi o‘lchov olinmadi.

Boshlang‘ich kod: `a636674`. Boshlang‘ich tekshiruv: **802 test / 97.04 s**. Yakuniy kod: **845 test / 93.85 s**. Bu soniyalar test to‘plamining ishlash vaqti; modellarning javob tezligi yaxshilanganining o‘lchovi emas. Frontend production build va Python sintaksis tekshiruvi ham muvaffaqiyatli.

## Topilgan va tuzatilgan holatlar

| Daraja | Holat | Tuzatish va dalil |
| --- | --- | --- |
| P1 | Yangi chat ochishdagi xato yutilib, so‘rov oldingi chatga yuborilishi mumkin edi. ChatGPT resetidagi xato ham muvaffaqiyatdek qaytarilardi. | Reset xatolari yuqoriga qaytariladi. Qwen, Gemini, ChatGPT va brauzerdagi DeepSeek uchun yuborishdan oldin yangi chat bo‘shligi tekshiriladi. Tekshiruv muvaffaqiyatsiz bo‘lsa prompt yuborilmasligi to‘rtta kanal uchun sinovdan o‘tdi. Bu haqiqiy foydalanuvchi ma’lumoti sizib chiqqanini tasdiqlovchi dalil emas; xavfli kod yo‘li topildi. |
| P1 | Fayl URL’ini yuklash chat endpointining asosiy async oqimida bajarilib, bir sekin fayl boshqa so‘rovlarni ham kutdirishi mumkin edi. | Fayl tayyorlash alohida oqimga o‘tkazildi. Sun’iy yuklash ushlab turilganda ikkinchi haqiqiy ASGI chat so‘rovi birinchisi tugashidan oldin javob berdi. Bekor qilinganda vaqtinchalik fayllar yuklash tugagach o‘chiriladi; keyin yana paydo bo‘lib qolmaydi. Image-edit yuklash yo‘li ham shu boshqaruvdan foydalanadi. |
| P1 | Umumiy Gemini adapteridagi `_effort_set` barcha akkaunt va sahifalar uchun bitta edi. Bir sahifada Low tanlangani boshqasida ham Low deb hisoblanardi. | Umumiy Python keshi olib tashlandi. Har bir sahifada tanlov bajariladi; uch sahifa/akkaunt ketma-ketligi tekshirildi. Bu menyuga kirish vaqtini qo‘shishi mumkin, ammo boshqa akkauntdagi High darajasini adashib saqlab qolmaydi. Real vaqt farqi hali o‘lchanmadi. |
| P1 | Qisqa, lekin murakkab topshiriqlar, “Davom et” va ochiq High talabi tezlik qoidasi sababli boshqa, o‘ylashi o‘chirilgan modelga o‘tardi. | Faqat aniq bitta o‘qish/ochish/ro‘yxat amali mos keladi. Boshlangan tool vazifasi va ochiq medium/high/xhigh talabi tezlik uchun almashtirilmaydi. Kuzatuvda down bo‘lgan va o‘ylashi yoqilgan nomzodlar bu yo‘ldan chiqarildi. Kengroq kontekst talab qiluvchi mavjud size-route alohida ishlashda davom etadi. |
| P1 | Tahrirdan keyin faylni yana o‘qish loop deb bloklanardi. Yozish ketma-ketligi orasida tekshiruv bo‘lsa ham tarixdagi eski maksimal streak keyingi tahrirni bloklardi. | Muvaffaqiyatli o‘zgartirishdan keyingi o‘qish ruxsat etiladi. Mutatsiya chegarasi faqat joriy uzluksiz ketma-ketlikka qaraydi. Takroriy savol va browser-open himoyalari saqlandi. |
| P1 | Local modeldan `[DONE]` kelishi, javob yoki tool argumentlari tugamagan bo‘lsa ham, JSON chaqiruv uchun yetarli hisoblanardi. | Har bir choice’da yakunlash sababi bo‘lishi shart. Bo‘sh DONE, yarim matn va yarim tool chaqiruvi rad etilishi tekshirildi. Tugagan javobda faqat transportning oxirgi belgisi yetishmasa qabul qilinadi. |
| P1 | Bir xil fayl nomini raqamlash mavjud boshqa nom bilan to‘qnashib, fayl mazmunini ustidan yozishi mumkin edi. | Bo‘sh nom topilmaguncha raqam oshiriladi. To‘rtta to‘qnashadigan nom uchun to‘rtta alohida fayl va to‘g‘ri mazmun tekshirildi. |
| P1 | Ayrim `input_file` shakllari yoki `file_id` sessiz tashlab yuborilardi. Matnli local model esa faylni tushungan kabi javob berishi mumkin edi. | Yuqori darajadagi `input_file.file_data` qo‘llandi. Mavjud bo‘lmagan ma’lumot va qo‘llanmaydigan `file_id` aniq 400 beradi. Hozirgi text-only local modellar fayl/rasmni qabul qilmaydi va pul yechilmaydi. Buzilgan base64 rad etiladi. |
| P1 | DeepSeek Direct’da mijoz uzilgach producer fon oqimi davom etib, cheksiz navbatga yozishi va ayni HTTP klient keyingi so‘rovga qayta ishlatilishi mumkin edi. Generator HTTP javobini yopmasdi. | Navbat 64 bo‘lak bilan chegaralandi. Bekor qilish belgisi producer’ni keyingi bo‘lakda to‘xtatadi. Generator yopilishi va sessiya tozalanishi tugamaguncha worker BUSY bo‘ladi. Bloklangan tarmoq o‘qishi darhol uzilishi kafolatlanmaydi: worker u tugaguncha yangi so‘rov olmaydi. Bekor qilish va HTTP close alohida tekshirildi. |
| P2 | Loop himoyasi bir amalni to‘xtatganda butun topshiriq bajarilgan deb aytardi. | Xabar endi faqat o‘sha amal takrorlanmaganini aytadi, butun vazifa tugaganini da’vo qilmaydi. O‘zbek/rus/ingliz tili tekshirildi; modelning tayyor final matni bo‘lsa u ustun. Eski keshlangan javoblar uchun protokol versiyasi yangilandi. |
| P2 | Qwen image edit yangi muvaffaqiyatli sinovsiz yoqilgan edi. | Generation yoqilgan, edit esa qayta tasdiqlanguncha o‘chirildi. Oldingi sinovda attachment yuklangan, ammo 90 soniyada tahrirlangan yakuniy rasm chiqmagan. Qwen saytining o‘zi edit qila olishi ushbu integratsiya ishlayotganining dalili emas. |

## Avvalgi kod bilan qayta bajarilgan taqqoslash

Quyidagi sun’iy so‘rovlar avvalgi `a636674` funksiyalari va yangi funksiyalarda aynan bir xil kirish bilan bajarildi. Hech qanday foydalanuvchi fayli yoki browser-open buyrug‘i bajarilmadi.

| Tekshiruv | Avval | Tuzatishdan keyin |
| --- | --- | --- |
| `Davom et` + tools | Boshqa tezlik modeliga o‘tdi | Tezlik uchun almashtirmadi |
| `Sayt yaratib ber` + tools | Boshqa tezlik modeliga o‘tdi | Tezlik uchun almashtirmadi |
| `Read app.py` + ochiq `high` | O‘ylashi o‘chirilgan modelga o‘tdi | Ochiq talab saqlandi |
| `Read app.py`, maxsus effort yo‘q | Oddiy so‘rov sifatida yo‘naltirdi | Shu imkoniyat saqlandi |
| Read → write → ayni faylni read | Qayta o‘qish bloklandi | Tekshiruv uchun ruxsat berildi |
| `report.txt`, `report-3.txt`, `report.txt`, `report.txt` | 3 ta alohida yo‘l; ikkinchi mazmun ustidan yozildi | 4 ta alohida yo‘l; hamma mazmun saqlandi |

## Imkoniyatlar matritsasi

Bu jadval **ushbu koddagi e’lon qilingan imkoniyatlar**. Production’dagi bugungi model/akkaunt holati emas. Ochiq model nomlari va ularning upstream mosligini server qaytgach `/v1/models` va amaldagi katalogdan yangidan olish kerak. DeepSeek aliaslari (`deepseek-chat`, `deepseek-reasoner`, `deepseek-v3`, `deepseek-r1`) bir kanalga moslanadi; borligi ularning production katalogida yoqilganini anglatmaydi.

| Kanal | Tool/agent | Rasmni o‘qish | Hujjat yuborish | Rasm yaratish | Rasm tahrirlash | Resetdan keyin bo‘sh chat tekshiruvi |
| --- | --- | --- | --- | --- | --- | --- |
| Local text modellari | Ha, native | Yo‘q | Yo‘q | Yo‘q | Yo‘q | So‘rovga berilgan tarix; browser chat yo‘q |
| Qwen | Ha, matn protokoli | Ha | Yo‘q | Ha | Hali tasdiqlanmagan, o‘chiq | Ha |
| Gemini | Ha, matn protokoli | Ha | Ha | Ha | Ha | Ha |
| ChatGPT | Ha, matn protokoli | Ha | Ha | Ha | Ha | Ha |
| DeepSeek browser | Ha, matn protokoli | Ha | Ha | Yo‘q | Yo‘q | Ha |
| DeepSeek Direct | Ha, matn protokoli | Ha | Ha | Yo‘q | Yo‘q | Alohida session ID |
| Venice | Ha, matn protokoli | Yo‘q | Yo‘q | Yo‘q | Yo‘q | Maxsus verifier yo‘q |
| ChatGPTAnon, GenSpark, DeepAI, ZAI, Grok, ChatbotApp, ChatbotChatApp | Qo‘shimcha imkoniyatlar e’lon qilinmagan | Yo‘q | Yo‘q | Yo‘q | Yo‘q | Maxsus verifier yo‘q |

Oxirgi qatordagi adapterlar registrda bor; ularning ishlashi, login holati va production’da tanlanishini ushbu auditda tasdiqlash imkoni bo‘lmadi. Qo‘shimcha adapterlarning native imkoniyatini taxmin bilan yoqish tavsiya etilmaydi. Fayl formati allowlist’da bo‘lishi har bir provayder uni muvaffaqiyatli tahlil qilganini anglatmaydi.

## Tezlik bo‘yicha nimani bilamiz

Yangi kod bilan haqiqiy provider TTFT, umumiy vaqt yoki p50/p95 olinmadi. Statik Qwen/Gemini/ChatGPT tartibi real vaqt kechikishi va navbatga qarab eng tez provayderni tanlamaydi; yangi tekshiruv faqat kuzatuvda down bo‘lgan tezlik nomzodini chiqaradi. Hozir “falon foiz tezlashdi” deyishga asos yo‘q.

Oldingi, **2026-10-26** amaliy kuzatuvlar alohida o‘lchovlar edi:

| Amal | Oldingi kuzatuvlar |
| --- | --- |
| Qwen yakuniy generation endpoint | 83.951 s; shundan 71.23 s provider generation |
| ChatGPT generation | 24.832 s va boshqa urinishda 86.067 s |
| ChatGPT edit | 24.123 s; keyingi urinishda tayyorlash bilan taxminan 37.5 s |
| Gemini generation | 50.466 s |

Bu raqamlar yangi kod natijasi ham, barcha foydalanuvchi uchun va’da ham emas. Media vaqtining katta qismi upstream’da o‘tmoqda. Navbat, tayyorlash, birinchi haqiqiy matn va yakuniy natijani alohida o‘lchash zarur; SSE keepalive yoki “thinking” belgisi birinchi javob deb hisoblanmasligi kerak. Uzun chatda tarix hanuz qayta uzatiladi va kontekst chegarasiga yetganda qisqartiriladi.

## Yakunlanmagan amaliy tekshiruv

Serverdagi ulanish yo‘li audit vaqtida ishlamadi. Mac Tailscale backend’i Running, Mac va VPS online, ammo `banda-MS-7D25` / `100.90.116.106` offline bo‘lib, oxirgi ulanish `2026-10-27 08:38:12 +05:00` ko‘rindi. SSH, Tailscale ping va health endpoint timeout bo‘ldi. Foydalanuvchi qayta ulaganidan keyingi urinish ham javob bermadi. Bu model xatosi emas, auditga kirish cheklovi.

Ulanish qaytgach quyidagilar hali kerak:

1. Amaldagi katalog, worker sog‘ligi va versiyani olish; serverdagi o‘zgarishlarni saqlab qolgan holda yangilash. Browser profillari va akkauntlarni almashtirmaslik.
2. Har bir yoqilgan modelda cache’dan tashqari qisqa matn, oddiy task, tool’ning ikki bosqichli aylanishi va stream yakunlanishini sinash. Haqiqiy tanlangan upstream va effort’ni ham qayd etish.
3. Har bir modelda 16+ burilishli suhbat va alohida ikkita kalit bilan tasodifiy canary’lar. Bir suhbat ma’lumoti boshqa kalit/suhbatga chiqmasligi; oldingi tool savoli qaytarilmasligi. Soxta tool natijalaridan foydalanish, model chiqargan OS buyruqlarini ishga tushirmaslik.
4. Fayl qabul qiluvchi kanallarda mazmuni ma’lum TXT/PDF va rasmni haqiqatan o‘qitish. Bir xil nomli fayllar, noto‘g‘ri format va bekor qilingan upload’ni sinash.
5. Qwen/Gemini/ChatGPT generation va Gemini/ChatGPT edit. HTTP 200 bilan cheklanmasdan rasm bytes’ini ochish, to‘liqligini va edit kirish rasmiga amal qilganini ko‘z bilan tekshirish. Qwen edit’ni faqat shunday sinovdan keyin qayta yoqish.
6. Bir va bir nechta parallel so‘rovda navbat, TTFT va umumiy vaqtni alohida solishtirish. Bir martalik muvaffaqiyat bilan “hammasi ishlaydi” xulosasini qilmaslik; yetarli takrorlarsiz p95 deb atamaslik.

Hozirgi kafolat: sanab o‘tilgan regressiyalar mahalliy kod va integratsion testlarda tuzatildi. Production rollout, bugungi tezlik, real rasm sifati va barcha akkauntlarda uzluksiz ishlash hali tasdiqlanmagan.
