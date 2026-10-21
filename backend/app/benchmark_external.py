"""Runs popular models through their external APIs on our 30 benchmark tasks."""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from benchmark import TASKS


@dataclass
class ExternalModel:
    """An external model to benchmark."""
    name: str           # "GPT-5.6 Terra"
    provider: str       # "openai" | "anthropic" | "google"
    model_id: str       # used in the API
    api_key_var: str    # variable name in .env


MODELS = [
    ExternalModel("GPT-5.6 Terra", "openai", "gpt-5.6-terra", "OPENAI_API_KEY"),
    ExternalModel("GPT-5.6 Luna", "openai", "gpt-5.6-luna", "OPENAI_API_KEY"),
    ExternalModel("GPT-6 Sol", "openai", "gpt-6-sol", "OPENAI_API_KEY"),
    ExternalModel("Claude Haiku 5.5", "anthropic", "claude-haiku-5.5-20260801", "ANTHROPIC_API_KEY"),
    ExternalModel("Claude Sonnet 5", "anthropic", "claude-sonnet-5-20260620", "ANTHROPIC_API_KEY"),
    ExternalModel("Claude Sonnet 5.5", "anthropic", "claude-sonnet-5.5-20261022", "ANTHROPIC_API_KEY"),
    ExternalModel("DeepSeek V4.1 Flash", "openai", "deepseek-v4.1-flash", "DEEPSEEK_API_KEY"),
    ExternalModel("Gemini 3.7 Flash", "google", "gemini-3.7-flash", "GOOGLE_API_KEY"),
]

TIMEOUT = 90.0
REQUEST_PAUSE = 2.5  # pause between requests


async def _openai_chat(client: httpx.AsyncClient, base_url: str, api_key: str,
                       model_id: str, prompt: str) -> str:
    """Request via an OpenAI-compatible API (OpenAI, DeepSeek)."""
    r = await client.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2048,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def _anthropic_chat(client: httpx.AsyncClient, api_key: str,
                          model_id: str, prompt: str) -> str:
    """Request via the Anthropic API."""
    r = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2048,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


async def _google_chat(client: httpx.AsyncClient, api_key: str,
                       model_id: str, prompt: str) -> str:
    """Request via the Google Gemini API."""
    r = await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}",
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048},
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


async def _run_model(model: ExternalModel) -> dict:
    """Runs one model on the 30 tasks."""
    api_key = os.getenv(model.api_key_var)
    if not api_key:
        raise ValueError(f"{model.api_key_var} not found; check .env")

    print(f"[{model.name}] starting... ({len(TASKS)} tasks)")
    results = []
    correct_count = 0
    technical_errors = 0

    async with httpx.AsyncClient() as client:
        for i, t in enumerate(TASKS, 1):
            await asyncio.sleep(REQUEST_PAUSE if i > 1 else 0)
            try:
                if model.provider == "openai":
                    base = "https://api.openai.com/v1"
                    answer = await _openai_chat(client, base, api_key, model.model_id, t.prompt)
                elif model.provider == "anthropic":
                    answer = await _anthropic_chat(client, api_key, model.model_id, t.prompt)
                elif model.provider == "google":
                    answer = await _google_chat(client, api_key, model.model_id, t.prompt)
                else:
                    raise ValueError(f"Unknown provider: {model.provider}")

                correct = t.check(answer)
                if correct:
                    correct_count += 1
                results.append({
                    "prompt": t.prompt[:60],
                    "category": t.category,
                    "answer": answer[:200],
                    "correct": correct,
                })
                print(f"  [{i}/{len(TASKS)}] {'✓' if correct else '✗'} {t.category}")
            except Exception as e:
                technical_errors += 1
                results.append({
                    "prompt": t.prompt[:60],
                    "category": t.category,
                    "answer": f"ERROR: {e}",
                    "correct": False,
                })
                print(f"  [{i}/{len(TASKS)}] ✗ ERROR: {e}")

    percent = round(100 * correct_count / len(TASKS), 1) if TASKS else 0
    print(f"[{model.name}] done: {correct_count}/{len(TASKS)} = {percent}%")

    return {
        "model": model.name,
        "model_id": model.model_id,
        "provider": model.provider,
        "correct": correct_count,
        "total": len(TASKS),
        "percent": percent,
        "technical_errors": technical_errors,
        "results": results,
        "date": time.strftime("%Y-%m-%d"),
    }


async def run_all():
    """Runs all models one after another and writes the results to a file."""
    output = []
    for model in MODELS:
        try:
            output.append(await _run_model(model))
        except Exception as e:
            print(f"[{model.name}] FAILED: {e}")
            output.append({
                "model": model.name,
                "model_id": model.model_id,
                "provider": model.provider,
                "correct": 0,
                "total": len(TASKS),
                "percent": 0.0,
                "technical_errors": len(TASKS),
                "error": str(e),
                "date": time.strftime("%Y-%m-%d"),
            })

    path = Path(__file__).parent / "benchmark_external_results.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nResults saved: {path}")
    print("\n=== SUMMARY ===")
    for n in output:
        print(f"  {n['model']:25s}  {n['percent']:5.1f}%  ({n['correct']}/{n['total']})")


if __name__ == "__main__":
    asyncio.run(run_all())
