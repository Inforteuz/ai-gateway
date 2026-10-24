# Suhbat tarixi va Qwen tezligi — 2026-10-23

Server: production. Klaster kodi prodga chiqarildi; API va cluster xizmatlari faol, public health HTTP 200.

## O‘zgarishlar

- `cluster/compose.py`: provayderning boshqa chatlaridagi xotira bilan so‘rovdagi `[History]` farqi aniq yozildi. Yuborilgan suhbat tarixidagi faktlardan foydalanish kerakligi tushuntirildi.
- `cluster/server.py`: Qwen startup vaqtida oldindan tayyorlanadi. Tayyorlash 35 soniya bilan cheklangan; muvaffaqiyatsizlik service startini to‘xtatmaydi.
- Monitoring unit testida yarim tun atrofida kun almashishi sababli tasodifiy xato beradigan vaqt barqarorlashtirildi.

## Tekshiruvlar

- Backend: `594 passed in 80.03s`.
- Frontend production build muvaffaqiyatli; katta JS chunk ogohlantirishi mavjud.
- `git diff --check` toza.
- Deploy qilingan ikkala faylning SHA-256 qiymati lokal fayllar bilan bir xil.

## Haqiqiy tekshiruvlar

Patchdan oldin Qwen `[History]` ichida Dilshod va NAVROZ-4821 mavjud bo‘lsa ham, “oldingi suhbatlarni eslamayman” deb javob bergan. Patchdan keyin aynan shu so‘rov to‘g‘ri javob berdi.

| Sinov | Model | Sekund | Javob |
|---|---|---:|---|
| start_on_sable_1 | sable-1.0 | 1.82 | Dilshod,   Loyihamning sinov kodi **NAVROZ-4821**.   Faqat **Qabul qildim** deb yoz.    Mana shu ma'lumotlarga asosan:  **Qabul qildim** |
| switch_to_sable_3_1 | sable-3.1 | 6.69 | Ism: Dilshod Sinov kodi: NAVROZ-4821 |
| fresh_request_isolation | sable-3.1 | 13.15 | Bilmayman. |
| exact_previously_failing_history | sable-3.1 | 5.67 | Ismingiz: Dilshod Sinov kodingiz: NAVROZ-4821 |

Qwen sovuq startdan keyingi birinchi so‘rovda `new_chat:0.01` qayd qilindi; avval loglarda `11.39` soniya kuzatilgan. Ketma-ket so‘rovlarda bitta tab navbati tufayli qo‘shimcha kutish mavjud (yangi so‘rovdagi 13.15 soniya); startup patchi bu navbatni bartaraf qilmaydi.

## Ochiq cheklov

Foydalanuvchi ma’lumotiga ko‘ra DeepSeek akkaunti 26-oktabrgacha vaqtincha bloklangan. Sable 3.7 live tekshiruvi bajarilmadi. Token o‘zgartirilmadi; avtomatik kuzatish yoki keyinroq qayta tekshirish rejalashtirilmadi.

## Rollback nusxalari

- `/var/backups/smartface-api/qwen-startup-20261023-000103/server.py`
- `/var/backups/smartface-api/history-rule-20261023-000447/compose.py`
