from __future__ import annotations

import logging
import os
import time
from typing import List

from .tts import split_into_chunks

logger = logging.getLogger(__name__)


def _get_openai_client(api_key: str | None):
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


def synthesize_mp3_openai(
    text: str,
    model: str,
    voice: str,
    max_chars_per_chunk: int,
    max_retries: int,
    initial_retry_delay: float,
    api_key_env: str = "OPENAI_API_KEY",
) -> bytes:
    api_key = os.environ.get(api_key_env)
    client, _ = _get_openai_client(api_key)
    if client is None:
        raise RuntimeError("OpenAI client not available; set OPENAI_API_KEY or configure api_key_env")

    chunks = split_into_chunks(text, max_chars_per_chunk)
    logger.info("Synthesizing (OpenAI) chunks", extra={"chunks": len(chunks)})
    audio_parts: List[bytes] = []

    for idx, chunk in enumerate(chunks):
        attempt = 0
        delay = initial_retry_delay
        while True:
            attempt += 1
            try:
                # Prefer streaming to reliably obtain raw bytes across SDK versions
                with client.audio.speech.with_streaming_response.create(  # type: ignore[attr-defined]
                    model=model,
                    voice=voice,
                    input=chunk,
                ) as response:
                    buf = b"".join(response.iter_bytes())
                if not buf:
                    raise RuntimeError("Empty audio buffer from OpenAI TTS")
                audio_parts.append(buf)
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "OpenAI TTS chunk failed",
                    extra={"chunk_index": idx, "attempt": attempt, "error": str(e)},
                )
                if attempt >= max_retries:
                    raise
                time.sleep(delay)
                delay *= 2

    return b"".join(audio_parts)
