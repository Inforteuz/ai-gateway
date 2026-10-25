"""Stable upstream error codes; safe interpolation without guessing a message's language."""
MESSAGES = {
    "tool_call_required": ("Model majburiy vosita chaqiruvini qaytarmadi. Qayta urinib ko'ring.", "The model did not return the required tool call. Try again."),
    "attachment_too_large": ("Fayl juda katta: {size_kb} KB (chegara {limit_kb} KB)", "The file is too large: {size_kb} KB (limit {limit_kb} KB)"),
    "attachments_total_exceeded": ("Biriktirmalarning jami hajmi chegaradan oshdi", "The attachments exceed the total size limit"),
    "attachments_count_exceeded": ("Bitta so'rovda ko'pi bilan {limit} ta fayl", "At most {limit} files per request"),
    "attachment_type_rejected": ("Bu fayl turi qabul qilinmaydi: {mime}", "This file type is not accepted: {mime}"),
    "attachment_invalid_base64": ("Faylning base64 ma'lumotini o'qib bo'lmadi", "The base64 payload could not be read"),
    "link_scheme_rejected": ("Faqat http/https havolalar qabul qilinadi", "Only http/https links are accepted"),
    "link_host_missing": ("Havolada host yo'q", "The link has no host"),
    "link_host_unresolved": ("Host aniqlanmadi: {host}", "The host could not be resolved: {host}"),
    "link_not_allowed": ("Ichki tarmoq manzillariga ruxsat yo'q", "Internal network addresses are not allowed"),
    "link_redirect_rejected": ("Havola boshqa manzilga yo'naltiryapti; to'g'ridan-to'g'ri havolani yuboring", "The link redirects elsewhere; send the direct link"),
    "attachment_download_http": ("Fayl yuklanmadi: HTTP {status}", "The file could not be downloaded: HTTP {status}"),
    "attachment_download_failed": ("Fayl yuklanmadi: {reason}", "The file could not be downloaded: {reason}"),
    "model_busy": ("Model band. Birozdan keyin qayta urinib ko'ring.", "The model is busy. Try again shortly."),
    "queue_full": ("Model navbati to'la. Birozdan keyin qayta urinib ko'ring.", "The model queue is full. Try again shortly."),
    "model_unavailable": ("Model hozir mavjud emas. Birozdan keyin qayta urinib ko'ring.", "The model is unavailable right now. Try again shortly."),
    "upstream_failed": ("Model javob bera olmadi. Qayta urinib ko'ring.", "The model did not return an answer. Try again."),
    "unsupported_feature": ("Model so'ralgan imkoniyatni qo'llamaydi.", "The selected model does not support the requested feature."),
    "invalid_output": ("Model javobidan foydalanib bo'lmadi. Qayta urinib ko'ring.", "The model's answer could not be used. Try again."),
}


def localize(code, details=None):
    templates = MESSAGES.get(code)
    if not templates:
        return None
    # Only bounded scalar values carried by the structured upstream error.
    values = {str(k): str(v)[:160] for k, v in (details or {}).items() if isinstance(v, (str, int, float))}
    try:
        return tuple(t.format_map(values) for t in templates)
    except (KeyError, ValueError):
        return None
