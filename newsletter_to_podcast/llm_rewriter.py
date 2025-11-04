from __future__ import annotations

import logging
import os
from typing import List, Optional

from .config import AppConfig

logger = logging.getLogger(__name__)


# Default, used when no external prompt file is provided
REWRITE_SYSTEM_PROMPT = (
    "You are a professional audio editor and voice adaptation expert.\n"
    "Transform the written newsletter into a version that sounds great when read aloud.\n"
    "Goals:\n"
    "- Make it natural, clear, and conversational.\n"
    "- Simplify and shorten sentences for listening comprehension.\n"
    "- Maintain original tone and intent.\n"
    "- Remove or rephrase visual-only references (e.g., 'see chart below').\n"
    "- Ensure good spoken flow with appropriate rhythm and emphasis.\n"
    "Style: Calm, confident, like an NPR host or Morning Brew Daily — conversational but concise.\n"
    "Hard rules:\n"
    "- Do NOT add greetings or generic intros/outros (e.g., 'Welcome to...', 'Let's dive in', 'In today's').\n"
    "- Do NOT fabricate or add content not present in the input.\n"
    "- Return plain text only."
)


def _get_openai_client(api_key: Optional[str]):
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return None, None
    if not api_key:
        return None, None
    try:
        client = OpenAI(api_key=api_key)
        return client, OpenAI
    except Exception:
        return None, None


def _rewrite_chunk_with_openai(client, model: str, chunk: str, system_prompt: str) -> Optional[str]:
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": chunk},
            ],
            temperature=0.4,
        )
        out = resp.choices[0].message.content if resp and resp.choices else None
        return out.strip() if out else None
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM rewrite chunk failed", extra={"error": str(e)})
        return None


def maybe_rewrite_for_audio(text: str, cfg: AppConfig) -> str:
    llm = getattr(cfg, "llm", None)
    if not llm or not getattr(llm, "rewrite_enabled", False):
        return text
    if getattr(llm, "provider", "openai") != "openai":
        return text

    api_key_env = getattr(llm, "api_key_env", "OPENAI_API_KEY")
    api_key = os.environ.get(api_key_env)
    client, _ = _get_openai_client(api_key)
    if client is None:
        logger.info("LLM rewrite skipped: client not available or api key missing")
        return text

    # Resolve system prompt: external file takes precedence if configured
    system_prompt = REWRITE_SYSTEM_PROMPT
    try:
        prompt_path = getattr(getattr(cfg, "llm", None), "rewrite_prompt_file", None)
        if prompt_path and os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as pf:
                content = pf.read().strip()
                if content:
                    system_prompt = content
    except Exception:
        pass

    # If input is too short, skip rewrite to avoid generic filler intros
    try:
        if len(text.strip()) < 200:
            return text
    except Exception:
        pass

    # Soft chunk by paragraphs to ~3000-3500 chars
    paras: List[str] = text.split("\n\n")
    chunks: List[str] = []
    buf: List[str] = []
    total = 0
    for p in paras:
        if total + len(p) + 2 > 3200 and buf:
            chunks.append("\n\n".join(buf))
            buf = [p]
            total = len(p)
        else:
            buf.append(p)
            total += len(p) + 2
    if buf:
        chunks.append("\n\n".join(buf))

    out_parts: List[str] = []
    model = getattr(llm, "rewrite_model", "gpt-4o")
    for ch in chunks:
        out = _rewrite_chunk_with_openai(client, model, ch, system_prompt)
        out_parts.append(out if out else ch)

    result = "\n\n".join(out_parts)
    logger.info(
        "LLM audio rewrite done",
        extra={"chunks": len(chunks), "orig_len": len(text), "rewritten_len": len(result)},
    )
    return result
