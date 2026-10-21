"""Evaluating models with our own benchmark.

WHY OUR OWN BENCHMARK and not numbers from the internet:
  1) Published figures are measured for the original models. Here a system
     prompt, brand masking and browser automation are layered on top, so the
     result a user gets is different (in one measurement a model answered only
     half of 30 requests).
  2) Publishing the original models' numbers would reveal which model belongs
     to which provider.

The questions are DELIBERATELY auto-gradable: no human needs to grade the
answers, so the benchmark can be re-run at any time to refresh the results.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Task:
    category: str
    prompt: str
    check: Callable[[str], bool]
    note: str = ""


def _first_number(answer: str) -> Optional[float]:
    """Extracts the first number from the answer (even when mixed with text)."""
    m = re.search(r"-?\d+(?:[.,]\d+)?", (answer or "").replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _equals(expected: float):
    return lambda j: (_first_number(j) is not None and abs(_first_number(j) - expected) < 1e-6)


def _has_all(*keywords: str):
    """The answer must contain ALL of the given keywords."""
    lowered = [k.lower() for k in keywords]
    return lambda j: all(k in (j or "").lower() for k in lowered)


def _has_any(*keywords: str):
    lowered = [k.lower() for k in keywords]
    return lambda j: any(k in (j or "").lower() for k in lowered)


# ON TASK DIFFICULTY
#
# The first set turned out too easy: every model scored almost the same and
# the difference came down to one or two questions, which is statistical
# noise, not a signal. Real differences between models show up EXACTLY on hard
# tasks: multi-step arithmetic, satisfying several constraints at once, subtle
# logic and bugs hidden in code.
#
# So every category has at least 5 tasks and most of them cannot be solved
# in a single step.

TASKS: list[Task] = [
    # --- Math: multi-step ---------------------------------------------------
    Task("Matematika",
         "Do'kon 15% chegirma e'lon qildi. Chegirmadan keyin mahsulot "
         "68 000 so'm turadi. Chegirmagacha narxi qancha edi? Faqat raqam yoz.",
         _equals(80000)),
    Task("Matematika",
         "Ikki ishchi birgalikda ishni 6 soatda bajaradi. Birinchisi yolg'iz "
         "10 soatda bajaradi. Ikkinchisi yolg'iz necha soatda bajaradi? "
         "Faqat raqam yoz.", _equals(15)),
    Task("Matematika",
         "Bir sonning 3 barobari va 7 ning yig'indisi 40 ga teng. "
         "Shu sonning kvadrati nechchi? Faqat raqam yoz.", _equals(121)),
    Task("Matematika",
         "Mashina 120 km ni 1.5 soatda bosdi, keyin 80 km ni 2 soatda. "
         "Butun yo'ldagi o'rtacha tezligi necha km/soat? Faqat raqam yoz.",
         _equals(57.14285714285714) if False
         else (lambda j: _first_number(j) is not None and abs(_first_number(j) - 57.14) < 0.6)),
    Task("Matematika",
         "Qutida 5 ta qizil va 3 ta ko'k shar bor. Ketma-ket 2 ta shar "
         "olinadi (qaytarilmaydi). Ikkalasi ham qizil bo'lish ehtimoli "
         "qancha? Javobni oddiy kasr ko'rinishida yoz, masalan 3/7.",
         lambda j: "5/14" in (j or "").replace(" ", "")),

    # --- Logic: several inferences ------------------------------------------
    Task("Mantiq",
         "Uch do'st: Aziz, Bobur, Dilshod. Aziz shifokor emas. Bobur "
         "o'qituvchi ham, shifokor ham emas. Dilshod muhandis emas. "
         "Kasblar: shifokor, o'qituvchi, muhandis. Shifokor kim? "
         "Faqat ismni yoz.", _has_any("dilshod")),
    Task("Mantiq",
         "Bugun payshanba bo'lsa, 100 kundan keyin hafta kuni qaysi bo'ladi? "
         "Faqat kun nomini yoz.", _has_any("shanba", "saturday")),
    Task("Mantiq",
         "Agar 'hamma qushlar ucha oladi' noto'g'ri bo'lsa, quyidagilardan "
         "qaysi biri ALBATTA to'g'ri? A) hech bir qush ucholmaydi "
         "B) kamida bitta qush ucholmaydi C) hamma qushlar ucholmaydi. "
         "Faqat harfni yoz.", lambda j: (j or "").strip().upper().startswith("B")),
    Task("Mantiq",
         "Otasining o'g'li menman, lekin men uning o'g'li emasman. "
         "Men kimman? Bir so'z bilan javob ber.",
         _has_any("qiz", "ayol", "o'g'il emas", "daughter")),
    Task("Mantiq",
         "5 ta mashina 5 ta detalni 5 daqiqada yasaydi. 100 ta mashina "
         "100 ta detalni necha daqiqada yasaydi? Faqat raqam yoz.", _equals(5)),

    # --- Code: hidden bug and algorithm ------------------------------------
    Task("Kod",
         "Bu funksiyada yashirin xato bor, uni top va TUZATILGAN kodni yoz:\n"
         "def o_rtacha(sonlar):\n"
         "    return sum(sonlar) / len(sonlar)",
         # A correct fix must handle the empty list
         lambda j: ("len(" in (j or "") and
                    any(k in (j or "").lower()
                        for k in ("if not", "== 0", "len(sonlar) == 0",
                                  "raise", "return 0", "none")))),
    Task("Kod",
         "Python funksiya yoz: matnda eng ko'p uchragan so'zni qaytarsin. "
         "Katta-kichik harf farqlanmasin. Faqat kod.",
         lambda j: ("def" in (j or "").lower()
                    and "lower()" in (j or "")
                    and any(k in (j or "") for k in ("split", "Counter", "count"))) ),
    Task("Kod",
         "Bu SQL so'rovda xato bor, tuzatilganini yoz:\n"
         "SELECT user_id, COUNT(*) FROM orders WHERE total > 100 "
         "GROUP BY user_id HAVING COUNT(*) > 5 ORDER BY total DESC",
         # `total` is not in GROUP BY, so ORDER BY cannot use it
         lambda j: any(k in (j or "").lower()
                       for k in ("order by count", "order by 2",
                                 "sum(total)", "max(total)", "count(*) desc"))),
    Task("Kod",
         "JavaScript: obyektlar massivini `age` maydoni bo'yicha o'sish "
         "tartibida saralaydigan kod yoz. Faqat kod.",
         lambda j: "sort" in (j or "").lower()
                   and ("a.age" in (j or "") or "age -" in (j or ""))),
    Task("Kod",
         "Python: rekursiv funksiya yoz, n-chi Fibonachchi sonini "
         "MEMOIZATSIYA bilan hisoblasin. Faqat kod.",
         lambda j: "def" in (j or "").lower()
                   and any(k in (j or "").lower()
                           for k in ("lru_cache", "memo", "cache", "dict()", "{}"))),

    # --- Uzbek language: subtle points -------------------------------------
    Task("O'zbek tili",
         "Quyidagi jumlada xato bor, tuzatib yoz: "
         "'Men kitobni o'qishni yaxshi ko'raman va u menga yoqdi.'",
         lambda j: len((j or "").strip()) > 15),
    Task("O'zbek tili",
         "'Kitob stolning ustida turibdi' jumlasida 'stolning' so'zi "
         "qaysi kelishikda? Faqat kelishik nomini yoz.",
         _has_any("qaratqich")),
    Task("O'zbek tili",
         "'It costs an arm and a leg' iborasini o'zbekchaga MA'NOSIGA ko'ra "
         "tarjima qil (so'zma-so'z emas). Bir jumla.",
         lambda j: any(k in (j or "").lower()
                       for k in ("qimmat", "baland narx", "juda qimmat", "qimmatga"))),
    Task("O'zbek tili",
         "'yoz' so'zi ikki xil ma'noga ega. Ikkalasini ham bitta jumlada "
         "tushuntir.",
         lambda j: ("fasl" in (j or "").lower() or "yil fasli" in (j or "").lower())
                   and any(k in (j or "").lower() for k in ("yozmoq", "yozish", "fe'l"))),
    Task("O'zbek tili",
         "Quyidagi gapni o'tgan zamonga o'tkaz: "
         "'Bolalar maktabga boradilar.'",
         lambda j: any(k in (j or "").lower()
                       for k in ("bordilar", "borishdi", "bordi"))),

    # --- Instruction following: several constraints ------------------------
    Task("Ko'rsatmaga amal qilish",
         "Uchta meva nomini VERGUL bilan ajratib yoz. Boshqa hech narsa "
         "yozma, nuqta ham qo'yma.",
         lambda j: (lambda t: t.count(",") == 2 and "." not in t and len(t) < 60)
                   ((j or "").strip())),
    Task("Ko'rsatmaga amal qilish",
         "Quyidagi sonlarni KAMAYISH tartibida, probel bilan ajratib yoz: "
         "7 2 9 4 1",
         lambda j: "9 7 4 2 1" in (j or "").replace("\n", " ")),
    Task("Ko'rsatmaga amal qilish",
         "Javobingni AYNAN 5 ta so'zdan iborat qilib yoz: O'zbekiston "
         "haqida bir jumla.",
         lambda j: len([w for w in (j or "").strip().replace(",", " ").split() if w]) == 5),
    Task("Ko'rsatmaga amal qilish",
         "'GATEWAY' so'zidagi harflarni teskari tartibda, har birini "
         "chiziqcha bilan ajratib yoz.",
         lambda j: "Y-A-W-E-T-A-G" in (j or "").upper().replace(" ", "")),
    Task("Ko'rsatmaga amal qilish",
         "Quyidagi savolga JAVOB BERMA, faqat 'TUSHUNDIM' deb yoz: "
         "Yer qancha yoshda?",
         lambda j: (j or "").strip().upper().strip(".!") == "TUSHUNDIM"),

    # --- Working with text --------------------------------------------------
    Task("Matn",
         "Quyidagi matndan FAQAT sanalarni ajratib, vergul bilan yoz: "
         "'Loyiha 2021-yil mart oyida boshlandi, 2023-yil dekabrda "
         "yakunlandi, hisobot esa 2024-yil yanvarda chop etildi.'",
         lambda j: all(y in (j or "") for y in ("2021", "2023", "2024"))),
    Task("Matn",
         "Quyidagi matnni AYNAN bitta jumlada xulosala: 'Shahar hokimiyati "
         "yangi avtobus yo'nalishini ochdi. Yo'nalish markazni chekka "
         "tumanlar bilan bog'laydi. Kuniga 40 ta reys qatnaydi. Chiptalar "
         "narxi o'zgarmadi.'",
         lambda j: (lambda t: 20 < len(t) < 300 and t.count(".") <= 1)
                   ((j or "").strip())),
    Task("Matn",
         "Quyidagi ro'yxatdan faqat SABZAVOTLARNI vergul bilan yoz: "
         "olma, sabzi, nok, kartoshka, uzum, piyoz, anor.",
         lambda j: all(x in (j or "").lower() for x in ("sabzi", "kartoshka", "piyoz"))
                   and not any(x in (j or "").lower() for x in ("olma", "nok", "uzum", "anor"))),
    Task("Matn",
         "Bu matnda nechta jumla bor? Faqat raqam yoz: 'Bugun havo ochiq. "
         "Biz bog'ga bordik. U yerda ko'p odam bor edi, lekin joy yetarli "
         "edi. Kechqurun uyga qaytdik.'", _equals(4)),
    Task("Matn",
         "Quyidagi jumlani INKOR shaklga o'tkaz: "
         "'Hamma talabalar imtihondan o'tdi.'",
         lambda j: any(k in (j or "").lower()
                       for k in ("o'tmadi", "otmadi", "hamma emas", "barchasi emas"))),
]


CATEGORIES = sorted({t.category for t in TASKS})
