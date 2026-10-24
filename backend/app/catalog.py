"""Model catalog: the client-facing description (does not reveal the real model name or size)."""
from __future__ import annotations

from . import billing

REASONING_LEVELS = ["none", "low", "medium", "high"]


def _cluster_caps(upstream: str) -> dict:
    """Capabilities of a cluster model (vision, file input, agent mode, image generation).

    The source is the measured `caps` of the cluster adapters, so the list is not
    kept in two places that drift apart.

    The API image does not include the cluster package. Direct DeepSeek's
    known capabilities are also available there.
    """
    if (upstream or "").lower() in ("deepseek", "deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-r1"):
        return {"vision": True, "file_input": True, "files": True,
                "image_generation": False, "tool_calling": True}
    try:
        from cluster.adapters import get_adapter
    except Exception:
        return {}
    adapter = get_adapter((upstream or "").lower())
    if adapter is None:
        return {}
    c = adapter.caps
    return {
        "vision": c.vision,            # analyse attached images
        "file_input": c.files,         # pdf/txt/csv attachments
        "image_generation": c.image_gen,
        "tool_calling": c.tool_calling,  # agent / subagent mode
    }


# WHEN answer verification (refine) runs — matches `app/refine.py::request_eligible`
# and `eligible`. The catalog does not promise that "every answer is verified":
# verification is conditional, and whether it actually ran is shown in each
# response's `gateway.verified` field.
VERIFICATION_POLICY = {
    "mode": "conditional",
    "applies_to": "faqat matnli so'rovlar: vositasiz (tools), JSON rejimisiz, n=1, "
                  "rasm/fayl biriktirmasiz; qoralama to'liq va bo'sh bo'lmasa",
    "skipped_when": "shartlar bajarilmasa yoki tekshiruvchi vaqtida javob bermasa — "
                    "javob tekshirilmagan holda qaytadi",
    "response_field": "gateway.verified",
}


def public_model(r, hard_max: int, maintenance: frozenset = frozenset(),
                 health: dict | None = None) -> dict:
    """Public description. The internal name (upstream_name), provider and parameter count are NOT included.

    `maintenance` — maintenance flagged manually by an administrator (planned).
    `health` — current state from monitoring (`up`/`down`/`unknown`). The two are
    INTENTIONALLY separate: a model can be down without announced maintenance,
    and vice versa.
    """
    third_party_provider = r["provider"] in ("external", "cluster")
    checked = bool(r["refine"])
    if r["provider"] == "cluster":
        extra = _cluster_caps(r["upstream_name"])
    elif r["provider"] == "local":
        # Ollama models officially support OpenAI tools (verified with opencode/aider/Qwen Code).
        # Previously this field was missing entirely and local models looked unfit for agents
        # in the catalog/docs. No vision, file input or image generation.
        extra = {"vision": False, "file_input": False, "image_generation": False, "tool_calling": True}
    else:
        extra = {}
    out = {
        # The model works but is temporarily unreliable — let users know in
        # advance instead of waiting for requests to fail.
        "maintenance": r["id"] in maintenance,
        "health": (health or {}).get(r["id"], "unknown"),
        "id": r["id"], "display_name": r["display_name"], "description": r["description"],
        "context_window": r["context_window"], "max_output_tokens": hard_max,
        "capabilities": {
            "streaming": True,
            "reasoning": bool(r["allow_thinking"]),
            # allow_thinking=0: thinking is off, the requested level is ignored
            "reasoning_levels": [] if third_party_provider else (REASONING_LEVELS if r["allow_thinking"] else ["none"]),
            # NOT a guarantee: verification is conditional (VERIFICATION_POLICY). This
            # used to say `true` while responses often returned `verified: false`,
            # which an audit flagged as a contradiction.
            "verified_answers": False,
            "conditional_verification": checked,
            **extra,
        },
        # "third_party": request content is also sent to an external partner model while serving it
        "data_processing": "third_party" if (third_party_provider or checked) else "private",
        "price_in_per_1m": r["price_in_per_1m"], "price_out_per_1m": r["price_out_per_1m"],
        "enabled": bool(r["enabled"]),
    }
    if checked:
        out["verification"] = VERIFICATION_POLICY
    return out


def health_map(db, local_state: str | None) -> dict:
    """{public model ID: up/down/unknown}, from `cluster_watch.public_status`.

    `local_state` — state of the ollama gateway (`app.state.health["state"]`).
    """
    from . import cluster_watch
    try:
        return {x["model"]: x["state"] for x in cluster_watch.public_status(db, local_state)["items"]}
    except Exception:
        return {}


def is_free(r, free_mode: bool) -> bool:
    """Free: free mode is on OR both of the model's prices are 0.

    Previously only the mode/key flag counted: model-1.0 showed `pricing.free=false`
    even with a price of 0.
    """
    return bool(free_mode) or (int(r["price_in_per_1m"] or 0) == 0 and int(r["price_out_per_1m"] or 0) == 0)


def openai_model(r, hard_max: int, free: bool = False,
                 maintenance: frozenset = frozenset(), health: dict | None = None) -> dict:
    m = public_model(r, hard_max, maintenance, health)
    free = is_free(r, free)
    return {
        "id": m["id"], "object": "model", "created": r["created_at"], "owned_by": "gateway",
        "display_name": m["display_name"], "description": m["description"],
        "context_window": m["context_window"], "max_output_tokens": m["max_output_tokens"],
        "capabilities": m["capabilities"], "data_processing": m["data_processing"],
        "maintenance": m["maintenance"], "health": m["health"],
        **({"verification": m["verification"]} if "verification" in m else {}),
        "pricing": {"currency": "UZS", "free": free,
                    "input_per_1m_tokens": 0 if free else m["price_in_per_1m"] / billing.MICRO,
                    "output_per_1m_tokens": 0 if free else m["price_out_per_1m"] / billing.MICRO},
    }
