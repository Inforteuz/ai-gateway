"""Request outcome classification: one rule shared by monitoring and reports."""
from __future__ import annotations

# Problems on our side (model/server): not the client's fault.
PLATFORM_ERRORS = {
    "reasoning_budget_exceeded", "generation_budget_exceeded", "generation_failed",
    "model_offline", "upstream_unavailable", "upstream_timeout", "upstream_error", "server_busy",
    "stream_interrupted", "stream_stalled", "stream_too_long", "internal_error", "abandoned", "empty_stream", "bad_upstream_json",
}

CATEGORY_LABELS = {
    "platform": "Model yoki server muammosi",
    "billing": "Hisob yetarli emas yoki kalit limiti",
    "limits": "Tezlik chegarasi",
    "client": "So'rov noto'g'ri",
    "cancelled": "Mijoz bekor qilgan",
}

ERROR_LABELS = {
    "reasoning_budget_exceeded": "Reasoning time budget exceeded",
    "generation_budget_exceeded": "Generation time budget exceeded",
    "generation_failed": "Generation stopped by loop protection",
    "model_offline": "Model serveri ulanmagan",
    "upstream_unavailable": "Model serveri ulanmagan",
    "upstream_timeout": "Model javob berishga ulgurmadi",
    "upstream_error": "Model serveri xato qaytardi",
    "server_busy": "Model band (navbat to'ldi)",
    "stream_interrupted": "Oqim o'rtasida aloqa uzildi",
    "stream_stalled": "Model oqim o'rtasida jim qoldi",
    "stream_too_long": "Oqim juda uzoq davom etdi",
    "repetition_loop": "Model takrorlanish siklida: javob to'xtatildi",
    "internal_error": "Ichki xato",
    "abandoned": "So'rov yakunlanmay qolgan",
    "empty_stream": "Model bo'sh javob berdi",
    "bad_upstream_json": "Model noto'g'ri javob berdi",
    "insufficient_balance": "Hisobda mablag' yetarli emas",
    "key_limit_reached": "Kalitning oylik limiti tugagan",
    "rate_limit_exceeded": "Daqiqalik so'rov chegarasi oshgan",
    "too_many_concurrent": "Bir vaqtdagi so'rovlar chegarasi oshgan",
    "model_not_found": "Model topilmadi",
    "invalid_json": "JSON noto'g'ri",
    "invalid_messages": "messages noto'g'ri",
    "invalid_prompt": "prompt berilmagan",
    "invalid_max_tokens": "max_tokens noto'g'ri",
    "request_too_large": "So'rov hajmi katta",
    "upstream_rejected": "Model so'rovni qabul qilmadi",
    "cancelled": "Bekor qilingan",
    "client_disconnected": "Mijoz uzgan",
}


def classify(status_code: int, error: str | None) -> str | None:
    """None = success. Otherwise: platform | billing | limits | client | cancelled."""
    e = (error or "").split(":")[0].strip()
    if 200 <= status_code < 300:
        return None
    if status_code == 499 or e in ("cancelled", "client_disconnected"):
        return "cancelled"
    if e in ("insufficient_balance", "key_limit_reached"):
        return "billing"
    if e in ("rate_limit_exceeded", "too_many_concurrent"):
        return "limits"
    if status_code in (500, 502, 503, 504) or e in PLATFORM_ERRORS:
        return "platform"
    return "client"


def error_label(error: str | None, status_code: int) -> str:
    e = (error or "").split(":")[0].strip()
    if e in ERROR_LABELS:
        return ERROR_LABELS[e]
    if e.startswith("upstream_") and e[9:].isdigit():
        return "Model serveri xato qaytardi" if int(e[9:]) >= 500 else "Model so'rovni qabul qilmadi"
    return e or f"HTTP {status_code}"
