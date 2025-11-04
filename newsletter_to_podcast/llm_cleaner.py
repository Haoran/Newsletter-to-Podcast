from __future__ import annotations

import logging
import os
from typing import Optional, List

from .config import AppConfig
from .cleaner import strip_emoji

logger = logging.getLogger(__name__)


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


# Default, used when no external prompt file is provided
SYSTEM_PROMPT = (
    "You clean newsletter text for text-to-speech. Return plain text only.\n"
    "Rules:\n"
    "- Remove all Markdown syntax: **, __, `code`, _italics_, [text](url), images.\n"
    "- Drop boilerplate/meta lines: 'Title:', 'Published Time:'/'Publish Time:', 'URL Source', 'Markdown Content', 'Illustration:', 'Image N: ...'.\n"
    "- Remove image captions and photo credits: lines/clauses like 'Photo: ...', 'Photograph:', 'Credit:', 'Courtesy:', '(Photo: ...)', 'via Getty Images', 'AP Photo', 'Reuters', 'Bloomberg', 'AFP'.\n"
    "- Drop editorial footers like 'Share this story.' and lines starting with 'Thanks to'.\n"
    "- Remove standalone bylines and credit-only bullets (e.g., lines that are just an author name).\n"
    "- Normalize bullets into normal sentences; remove leading bullet symbols (*, -, •).\n"
    "- Keep the meaningful prose and numbered sections in original order, but without symbols.\n"
    "- Normalize spacing and punctuation (ensure a space after colons).\n"
    "Do not summarize or add commentary. Do not fabricate content."
)


def _clean_chunk_with_openai(client, model: str, chunk: str, system_prompt: str) -> Optional[str]:
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Clean this newsletter excerpt for TTS. Return plain text only.\n\n" + chunk
                    ),
                },
            ],
            temperature=0.2,
        )
        out = resp.choices[0].message.content if resp and resp.choices else None
        return out.strip() if out else None
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM clean chunk failed", extra={"error": str(e)})
        return None


def maybe_clean_text_with_llm(text: str, cfg: AppConfig) -> str:
    llm = getattr(cfg, "llm", None)
    if not llm or not getattr(llm, "enabled", False):
        return text
    if getattr(llm, "provider", "openai") != "openai":
        return text

    api_key = os.environ.get(getattr(llm, "api_key_env", "OPENAI_API_KEY"))
    client, _ = _get_openai_client(api_key)
    if client is None:
        logger.info("LLM cleaning skipped: client not available or api key missing")
        return text

    # Resolve system prompt: external file takes precedence if configured
    system_prompt = SYSTEM_PROMPT
    try:
        prompt_path = getattr(getattr(cfg, "llm", None), "clean_prompt_file", None)
        if prompt_path and os.path.exists(prompt_path):
            with open(prompt_path, "r", encoding="utf-8") as pf:
                content = pf.read().strip()
                if content:
                    system_prompt = content
    except Exception:
        pass

    # Soft chunking by paragraph to ~3500 chars per chunk
    paras: List[str] = text.split("\n\n")
    chunks: List[str] = []
    buf = []
    total = 0
    for p in paras:
        if total + len(p) + 2 > 3500 and buf:
            chunks.append("\n\n".join(buf))
            buf = [p]
            total = len(p)
        else:
            buf.append(p)
            total += len(p) + 2
    if buf:
        chunks.append("\n\n".join(buf))

    cleaned_parts: List[str] = []
    model = getattr(llm, "model", "gpt-4o-mini")
    for idx, ch in enumerate(chunks):
        out = _clean_chunk_with_openai(client, model, ch, system_prompt)
        cleaned_parts.append(out if out else ch)

    result = "\n\n".join(cleaned_parts)
    # Ensure emojis are removed if configured
    try:
        if getattr(cfg, "clean", None) and getattr(cfg.clean, "remove_emoji", False):
            result = strip_emoji(result)
    except Exception:
        pass
    logger.info("LLM cleaned text", extra={"chunks": len(chunks), "orig_len": len(text), "clean_len": len(result)})
    return result
