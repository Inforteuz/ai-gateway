"""PUBLISHED results of external models, each with its source.

WHY THIS FILE IS SEPARATE AND WHY THE NUMBERS ARE NEVER MIXED
-------------------------------------------------------------
The 30 tasks in `benchmark.py` are in Uzbek, auto-graded, and measured through
the gateway itself. The external numbers below come from entirely DIFFERENT
benchmarks (agentic tasks, terminal, scientific reasoning, school subjects).
Putting percentages from two different benchmarks in one column is not a
measurement error, it misleads the customer: 77% and 58% cannot be compared
because they do not measure the same thing.

Therefore:
  * external numbers are shown in SEPARATE blocks and never added to our chart;
  * every block has the benchmark name, version, source link and date;
  * the `comparable` field is always False; the frontend uses it to show a
    warning.

SOURCE RULE (not relaxed)
-------------------------
PRIMARY sources only: a model card, the vendor's official announcement, a
technical report, or the measuring organization's own page. Numbers are NOT
copied from blogs, news sites or "top 10 models" lists. Verified: for the same
model and benchmark different blogs reported both 74.1% and 94.3% (GPQA
Diamond, Gemini 3.1 Pro).

If no reliable primary source exists for a model, it is NOT added. An empty
cell (`None`) is better than a guess.

NUMBERS ARE ENTERED BY HAND
---------------------------
Automatic collection was deliberately not done: leaderboard pages change daily
and model names change too, so nobody would check which benchmark sits behind
an auto-updated number. When updating, open the source and ALWAYS update
`read_date` as well.

BLOCK KINDS (`kind`)
--------------------
  "matrix"  - several models on several benchmarks in one document
              (columns = models, rows = benchmarks, grouped).
  "model"   - one model's benchmarks from its own announcement.
  "ranking" - models listed by a single metric.
"""
from __future__ import annotations

# --- Source 1: Google DeepMind, Gemini 4 Argon evaluation document ----------
# One official document lists four leading models side by side on 19
# benchmarks, which is exactly what users want to see.
#
# IMPORTANT, and also stated on the page: the document's own methodology
# section says "All the results for non-Gemini models are sourced from
# providers' self reported numbers". So even this table was NOT measured under
# identical conditions; each vendor measured with its own setup.
GEMINI_EVALS = {
    "key": "gemini-4-argon-evals",
    "kind": "matrix",
    "name": "Gemini 4 Argon — model baholash hujjati (Google DeepMind)",
    "version": "natijalar 2026-yil oktabr holatiga",
    "source_url": "https://deepmind.google/models/evals-methodology/gemini-4-argon",
    "read_date": "2026-10-20",
    "date_note": "hujjatda: \"results as of October, 2026\"",
    "scale": "to'g'ri bajarilgan topshiriqlar foizi (pass@1)",
    "comparable": False,
    "what_it_measures":
        "To'rtta yetakchi model 19 ta testda: bilim ishi, agentlik kodi, "
        "ML muhandisligi, fan va matematika, uzun kontekst, kompyuterni "
        "boshqarish, multimodal va kiberxavfsizlik.",
    "why_not_comparable":
        "Testlar ingliz tilida va asosan agentlik/ilmiy topshiriqlardan "
        "iborat; bizning 30 ta topshiriq o'zbek tilida va kundalik ishlarga "
        "qaratilgan. Bundan tashqari hujjatning o'zi aytadi: Gemini'dan "
        "boshqa modellarning raqamlari o'sha modellar ishlab chiqaruvchilari "
        "o'zlari e'lon qilgan sonlardan olingan, ya'ni jadval ichida ham "
        "o'lchov sharoiti bir xil emas.",
    "columns": ["Gemini 4 Argon", "GPT-6 Astra", "Claude Fable 5.1", "Claude Opus 5.5"],
    "column_vendors": ["Google", "OpenAI", "Anthropic", "Anthropic"],
    # None = the document shows a dash (not measured or not published).
    "groups": [
        {"group": "Bilim ishi", "rows": [
            {"test": "Vals Index", "scores": [68.9, 63.1, 65.8, 67.0]},
            {"test": "AutomationBench", "note": "Score", "scores": [51.3, 41.4, 31.4, 42.5]},
            {"test": "Vals Finance Agent v2", "scores": [65.4, 53.5, 58.9, 58.6]},
            {"test": "Harvey's Legal Agent Benchmark", "scores": [19.6, 5.4, 6.7, 3.8]},
        ]},
        {"group": "Agentlik kodi", "rows": [
            {"test": "DeepSWE v1.1", "scores": [77.9, 74.1, 67.4, 74.2]},
            {"test": "FrontierSWE v2", "scores": [55.0, 65.5, 56.3, 62.3]},
            {"test": "Vibe Code Bench", "scores": [91.9, 89.6, 90.3, 90.3]},
            {"test": "Terminal-bench 4.0", "scores": [57.4, 58.2, 57.9, 66.4]},
        ]},
        {"group": "ML muhandisligi", "rows": [
            {"test": "PostTrainBench", "scores": [45.3, 44.3, 40.2, 49.3]},
        ]},
        {"group": "Fan va matematika", "rows": [
            {"test": "Terminal-Bench Science 0.1", "scores": [57.6, 68.1, 52.6, 63.3]},
            {"test": "LABBench 2", "scores": [88.8, 85.4, 68.6, 73.1]},
            {"test": "RiemannBench", "scores": [76.0, 72.0, 65.6, 69.6]},
        ]},
        {"group": "Uzun kontekst", "rows": [
            {"test": "GraphWalks", "note": "128k gacha, BFS (F1)",
             "scores": [99.7, 98.7, 91.4, 90.6]},
            {"test": "GraphWalks", "note": "256k–1M, BFS (F1)",
             "scores": [84.2, 71.8, 65.0, 66.8]},
        ]},
        {"group": "Kompyuterni boshqarish", "rows": [
            {"test": "Agent's Last Exam", "note": "Pass rate",
             "scores": [39.5, 34.2, None, 38.2]},
            {"test": "OSWorld-2.0", "note": "offline qism, qisman ball",
             "scores": [69.2, 72.6, None, None]},
        ]},
        {"group": "Multimodal", "rows": [
            {"test": "Chartography", "scores": [71.6, 71.0, 46.2, 66.3]},
            {"test": "LVBench", "scores": [91.7, 87.5, 79.7, 83.7]},
        ]},
        {"group": "Kiberxavfsizlik", "rows": [
            {"test": "CWE-bench v1", "scores": [68.0, 68.0, 58.0, 67.0]},
        ]},
    ],
}


# --- Source 2: Anthropic, Claude Opus 5.5 announcement -----------------------
# The vendor's OWN page. It has benchmarks missing from the Google table
# above, so it stands separately.
#
# NOTE: some numbers differ from the Google table for the SAME model and the
# SAME benchmark (AutomationBench 40.0 vs 42.5; Terminal-Bench Science 58.7 vs
# 63.3; Chartography 89.0 "with tools" vs 66.3 "without tools"). This is not a
# mistake; the setups differ. The page shows this example on purpose, because
# it best explains that "official number" does not mean "measured under the
# same conditions".
OPUS_55 = {
    "key": "anthropic-opus-5-5",
    "kind": "model",
    "name": "Claude Opus 5.5 — Anthropic'ning o'z e'loni",
    "version": "e'lon sahifasi, 2026-09-22",
    "source_url": "https://www.anthropic.com/claude-opus-5-5",
    "read_date": "2026-10-20",
    "date_note": "sahifadagi sana: 2026-09-22",
    "scale": "foiz (GDPval-AA — Elo ball)",
    "comparable": False,
    "what_it_measures":
        "Anthropic o'z modeli uchun e'lon qilgan testlar: terminal "
        "topshiriqlari, kod yozish, kompyuterni boshqarish, ilmiy mulohaza "
        "va grafiklarni o'qish.",
    "why_not_comparable":
        "Bu bitta modelning o'z e'loni — boshqa modellar bilan bir xil "
        "sharoitda o'lchangani tekshirilmagan. Bizning o'zbek tilidagi "
        "sinovimiz bilan esa umuman boshqa narsani o'lchaydi.",
    "model": "Claude Opus 5.5",
    "rows": [
        {"test": "Terminal-Bench 4.0", "score": 66.4},
        {"test": "FrontierCode v1.1 (Main)", "score": 54.4},
        {"test": "CursorBench 4.0", "score": 57.8},
        {"test": "AutomationBench", "score": 40.0},
        {"test": "Humanity's Last Exam", "note": "asbob bilan", "score": 67.7},
        {"test": "Terminal-Bench-Science 0.1", "score": 58.7},
        {"test": "OSWorld 2.1", "note": "qisman ball", "score": 81.8},
        {"test": "Chartography", "note": "asbob bilan", "score": 89.0},
        {"test": "GDPval-AA v2.1", "note": "Elo ball, foiz emas", "score": 1846, "unit": "ball"},
    ],
}


# --- Source 3: OpenAI, GPT-6 Astra announcement ------------------------------
# Most numbers on the page exist only inside charts (not in the text), so only
# those stated verbatim in the TEXT were taken. Numbers "read off" a chart were
# not entered: their source could not be verified.
GPT6_ASTRA = {
    "key": "openai-gpt-6-astra",
    "kind": "model",
    "name": "GPT-6 Astra — OpenAI'ning o'z e'loni",
    "version": "e'lon sahifasi",
    "source_url": "https://openai.com/index/gpt-6-astra/",
    "read_date": "2026-10-20",
    "date_note": "sahifada chop etilgan sana yo'q; 2026-09-22 sanali yangilanish izohi bor",
    "scale": "to'g'ri javoblar foizi",
    "comparable": False,
    "what_it_measures":
        "OpenAI matnda so'zma-so'z keltirgan natijalar. Sahifadagi boshqa "
        "raqamlar faqat grafik ichida bo'lgani uchun olinmadi.",
    "why_not_comparable":
        "Ishlab chiqaruvchining o'z e'loni, o'z sozlamasida. GPQA Diamond — "
        "biologiya, kimyo va fizikadan oliy ta'lim darajasidagi ilmiy "
        "savollar; bizning sinovimiz o'zbek tilidagi kundalik topshiriqlar.",
    "model": "GPT-6 Astra",
    "rows": [
        {"test": "GPQA Diamond", "score": 96.0},
        {"test": "GPQA Diamond", "note": "arzonroq sozlamada", "score": 94.9},
        {"test": "FrontierMath Tier 4", "score": 98.0},
        {"test": "ARC-AGI-3", "score": 99.9},
        {"test": "ExploitBench", "score": 100.0},
        {"test": "OSWorld 2.0", "note": "topshiriqqa ~40 daqiqa", "score": 72.6},
    ],
}


# --- Source 4: Artificial Analysis Intelligence Index ------------------------
# An independent measuring organization that tests every model THE SAME way,
# unlike the three sources above. That is why it is kept: it is the only
# "same conditions" list on the page.
AA_INDEX = {
    "key": "aa-intelligence-index",
    "kind": "ranking",
    "name": "Artificial Analysis Intelligence Index",
    "version": "v4.3.2",
    "source_url": "https://artificialanalysis.ai/leaderboards/models",
    "read_date": "2026-10-20",
    "date_note": "manba sahifasida yangilanish sanasi ko'rsatilmagan — bu biz o'qigan sana",
    "scale": "0–100 ball (indeks)",
    "comparable": False,
    "what_it_measures":
        "O'n ta sinovning vaznli o'rtachasi: agentlik topshiriqlari (30%), "
        "kod (20%), umumiy bilim va gallyutsinatsiya (30%), ilmiy mulohaza "
        "(20%). Tarkibi: AA-Briefcase v1.1, GDPval-AA v2.1, "
        "AutomationBench-AA, Terminal-Bench 4.0, SciCode, AA-Omniscience, "
        "GDP.pdf, AA-LCR v1.1, Humanity's Last Exam, CritPt. "
        "Yuqoridagi uchta manbadan farqi: bu yerda hamma model BIR XIL "
        "usulda, bitta tashkilot tomonidan o'lchangan.",
    "why_not_comparable":
        "Sinov ingliz tilida va asosan agentlik/ilmiy topshiriqlardan iborat. "
        "Bizning 30 ta topshiriq o'zbek tilida va kundalik ishlarga "
        "qaratilgan. Ikkala raqam bir narsani o'lchamaydi.",
    # Each vendor's top entry. Row names are verbatim from the source; changing
    # them would make it impossible to check which setup a number came from.
    "rows": [
        {"vendor": "Anthropic", "model": "Claude Opus 5.5 (max with fallback)", "score": 58},
        {"vendor": "Meta", "model": "Muse Wonder Full (max)", "score": 56},
        {"vendor": "OpenAI", "model": "GPT-6 Astra (max)", "score": 53},
        {"vendor": "Google", "model": "Gemini 4 Argon (high)", "score": 53},
        {"vendor": "SpaceXAI", "model": "Grok 4.7 (xhigh)", "score": 46},
        {"vendor": "Xiaomi", "model": "MiMo-V2.6-Pro", "score": 46},
        {"vendor": "DeepSeek", "model": "DeepSeek V4.1 Flash 1017 (max)", "score": 45},
        {"vendor": "Alibaba", "model": "Qwen3.8 Max (0902)", "score": 45},
        {"vendor": "Moonshot", "model": "Kimi K3 (max)", "score": 44},
    ],
}


# --- Source 5: TUMLU-mini, Uzbek subset -------------------------------------
# The ONLY serious open academic benchmark measured with questions in Uzbek.
# The closest external reference for our "Uzbek language" category, but still
# a different benchmark: TUMLU asks school-subject test questions, while we
# test the language itself (grammar, inflection).
#
# The paper was published in June 2025: the listed models are of that
# generation. Today's models may score higher, and we do not hide that.
TUMLU_UZ = {
    "key": "tumlu-uz",
    "kind": "ranking",
    "name": "TUMLU-mini — o'zbek tili qismi",
    "version": "5-shot, arXiv:2502.11020v2",
    "source_url": "https://arxiv.org/abs/2502.11020",
    "read_date": "2025-06-13",
    "date_note": "maqolaning v2 versiyasi sanasi",
    "scale": "to'g'ri javob foizi",
    "comparable": False,
    "what_it_measures":
        "O'zbek tilida, 7 fandan (ona tili va adabiyot, tarix, geografiya, "
        "kimyo, fizika, biologiya, matematika) 100 tadan test savoli — "
        "jami 700 savol. Savollar o'zbek maktab darsliklaridan olingan, "
        "tarjima emas.",
    "why_not_comparable":
        "Bu — maktab fanlari bo'yicha bilim testi (variantli savollar). "
        "Bizning o'zbek tili topshiriqlari esa tilning o'zini tekshiradi: "
        "qo'shimcha qo'yish, zamon o'zgartirish, inkor shakli. Bundan "
        "tashqari ro'yxatdagi modellar 2024–2025 avlodi.",
    "rows": [
        {"vendor": "Anthropic", "model": "Claude 3.5 Sonnet", "score": 69.1},
        {"vendor": "OpenAI", "model": "GPT-4o", "score": 65.4},
        {"vendor": "Google", "model": "Gemini 1.5 Pro", "score": 65.1},
        {"vendor": "Google", "model": "Gemini 1.5 Flash", "score": 52.1},
        {"vendor": "Meta", "model": "Llama 3.1 405B", "score": 50.4},
        {"vendor": "Alibaba", "model": "Qwen2.5 72B", "score": 50.4},
        {"vendor": "Anthropic", "model": "Claude 3.5 Haiku", "score": 50.3},
        {"vendor": "Meta", "model": "Llama 3.1 70B", "score": 45.3},
        {"vendor": "Meta", "model": "Llama 3.3 70B", "score": 44.1},
        {"vendor": "Google", "model": "Gemma 2 27B", "score": 37.6},
        {"vendor": "Google", "model": "Gemma 2 9B", "score": 36.1},
        {"vendor": "Alibaba", "model": "Qwen2.5 7B", "score": 34.6},
        {"vendor": "Meta", "model": "Llama 3.1 8B", "score": 29.9},
    ],
}


SOURCES = [GEMINI_EVALS, OPUS_55, GPT6_ASTRA, AA_INDEX, TUMLU_UZ]


# The most important note on the page: "official number" does not mean
# "measured under the same conditions". The example is not invented; it is
# taken directly from the two sources above.
HARNESS_NOTE = {
    "title": "\"Rasmiy raqam\" — hali \"bir xil sinov\" degani emas",
    "text":
        "Claude Opus 5.5 ning AutomationBench natijasi Anthropic'ning o'z "
        "sahifasida 40,0%, Google'ning hujjatida esa 42,5%. "
        "Terminal-Bench Science'da 58,7% va 63,3%. Chartography'da 89,0% "
        "(asbob bilan) va 66,3% (asbobsiz). Ikkala manba ham birlamchi va "
        "ikkalasi ham to'g'ri — farq sozlamada. Shuning uchun bu sahifada "
        "turli manbalarning raqamlari bir-biriga qo'shilmaydi va o'rtacha "
        "hisoblanmaydi.",
}


def public_data() -> dict:
    """The list of external sources for the frontend.

    Rows keep the source's order: re-sorting would be our own interpretation.
    """
    return {
        "sources": [dict(s) for s in SOURCES],
        "harness_note": dict(HARNESS_NOTE),
        # The page's warning text lives here, next to the numbers, so it
        # cannot be lost by accident.
        "disclaimer":
            "Quyidagi raqamlar bizning sinovimizda emas, modellar "
            "ishlab chiqaruvchilari va mustaqil tashkilotlar e'lon qilgan "
            "BOSHQA sinovlarda olingan. Ular bizning foizimiz bilan bitta "
            "o'qda solishtirilmaydi — shuning uchun alohida jadvalda "
            "turadi. Har bir blokda sinov nomi, manba havolasi va sana bor.",
    }
