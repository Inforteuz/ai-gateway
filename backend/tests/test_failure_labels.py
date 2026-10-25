"""Every failure label the server sends to the panel can be translated.

The panel looks the Uzbek label up in its dictionary; a missing entry is shown
raw. "stream_upstream_error" had no label at all and reached the Russian
interface as the bare code (2026-10-25).
"""
import json
from pathlib import Path

from app.failures import CATEGORY_LABELS, ERROR_LABELS, error_label

I18N = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n"


def _has_key(text: str, key: str) -> bool:
    return f"{json.dumps(key, ensure_ascii=False)}:" in text or f"'{key}':" in text


def test_every_label_has_a_dictionary_entry():
    text = (I18N / "uz.js").read_text() + (I18N / "uz-extra.js").read_text()
    missing = [v for v in {**ERROR_LABELS, **CATEGORY_LABELS}.values() if not _has_key(text, v)]
    assert missing == []


def test_stream_errors_get_a_label_not_the_raw_code():
    assert error_label("stream_upstream_error:upstream_failed", 502) == ERROR_LABELS["stream_upstream_error"]
