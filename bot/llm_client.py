"""Shared OpenAI-compatible chat helper (Arkor / xAI / proxies).

Env (first match):
  LLM_API_KEY | ARKOR_API_KEY | XAI_API_KEY | GROK_API_KEY
  LLM_BASE_URL | ARKOR_BASE_URL  (default Arkor)
  LLM_MODEL                      (default google/gemini-3-5-flash-lite)

Used for auto-generated application blurbs (auto blurbs for new projects).
Never log the API key.
"""

from __future__ import annotations

import json
import os
import re
from typing import Sequence

import requests

from bot.http_client import log

DEFAULT_LLM_BASE_URL = "https://happy-prairie-pmhw.arkor.app/v1"
DEFAULT_LLM_MODEL = "google/gemini-3-5-flash-lite"


def has_llm() -> bool:
    return bool(llm_api_key())


def llm_api_key() -> str:
    return (
        os.getenv("LLM_API_KEY", "").strip()
        or os.getenv("ARKOR_API_KEY", "").strip()
        or os.getenv("XAI_API_KEY", "").strip()
        or os.getenv("GROK_API_KEY", "").strip()
    )


def llm_base_url() -> str:
    raw = (
        os.getenv("LLM_BASE_URL", "").strip()
        or os.getenv("ARKOR_BASE_URL", "").strip()
        or DEFAULT_LLM_BASE_URL
    )
    return raw.rstrip("/")


def llm_chat_url() -> str:
    base = llm_base_url()
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def llm_model_name() -> str:
    return (
        os.getenv("LLM_MODEL", "").strip()
        or os.getenv("KIRDY_LLM_MODEL", "").strip()
        or os.getenv("XAI_MODEL", "").strip()
        or DEFAULT_LLM_MODEL
    )


def chat_completion(
    *,
    system: str,
    user: str,
    temperature: float = 1.0,
    max_tokens: int = 180,
    timeout: float = 60.0,
    model: str = "",
    base_url: str = "",
    api_key: str = "",
) -> str:
    """POST /v1/chat/completions → assistant text. Raises on failure.

    ``model`` overrides the env default for one call. Extraction from a short
    text is not the same job as writing a blurb, and paying reasoning-model
    rates per tweet across every project on a schedule adds up fast.
    """
    key = api_key or llm_api_key()
    if not key:
        raise RuntimeError("no LLM_API_KEY / ARKOR_API_KEY")
    url = f"{base_url.rstrip('/')}/chat/completions" if base_url else llm_chat_url()
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model or llm_model_name(),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=timeout,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        raise RuntimeError(
            f"LLM HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)[:200]}"
        )
    try:
        text = data["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"LLM bad shape: {str(data)[:300]!r}") from e
    if isinstance(text, list):
        parts = []
        for p in text:
            if isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
            else:
                parts.append(str(p))
        text = " ".join(parts)
    text = (text or "").strip().strip('"').strip("'")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise RuntimeError("LLM empty content")
    return text


def generate_blurb(
    *,
    system: str,
    user: str,
    templates: Sequence[str],
    min_len: int = 20,
    max_len: int = 500,
    tag: str = "llm",
    return_source: bool = False,
) -> str | tuple[str, str]:
    """
    Prefer LLM; on any failure pick a random template (+ light spice).
    Always returns text with len in [min_len, max_len] when templates are valid.

    If return_source=True → (text, "llm"|"template").
    """
    import random

    if has_llm():
        try:
            text = chat_completion(system=system, user=user, max_tokens=200)
            if len(text) < min_len:
                raise RuntimeError(f"too short ({len(text)})")
            log(f"{tag} via LLM model={llm_model_name()} ({len(text)} chars)")
            out = text[:max_len]
            return (out, "llm") if return_source else out
        except Exception as e:
            log(f"{tag} LLM failed, using template: {e}")

    base = random.choice(list(templates) or ["I want to join this community and support the project long term."])
    spice = random.choice(("", " Full send.", " No fluff.", " Here for the long run."))
    msg = (base + spice).strip()
    if len(msg) < min_len:
        msg = base + " Please accept this application."
    out = msg[:max_len]
    return (out, "template") if return_source else out
