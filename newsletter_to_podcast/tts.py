from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import List

from google.cloud import texttospeech


logger = logging.getLogger(__name__)


def _ensure_credentials_from_inline_json() -> None:
    # Allow credentials via GCP_TTS_SERVICE_ACCOUNT_JSON secret
    inline_json = os.environ.get("GCP_TTS_SERVICE_ACCOUNT_JSON")
    if inline_json and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        path = os.path.join(".secrets", "gcp_tts_sa.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(inline_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path


def split_into_chunks(text: str, max_len: int) -> List[str]:
    # Simple sentence-aware chunking
    sentences = []
    start = 0
    for i, ch in enumerate(text):
        if ch in ".!?。！？\n":
            sentences.append(text[start : i + 1].strip())
            start = i + 1
    if start < len(text):
        sentences.append(text[start:].strip())

    chunks: List[str] = []
    buf = ""
    for s in sentences:
        if not s:
            continue
        if len(buf) + 1 + len(s) <= max_len:
            buf = (buf + " " + s).strip()
        else:
            if buf:
                chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    if not chunks:
        chunks = [text[:max_len]]
    return chunks


def synthesize_mp3(
    text: str,
    language_code: str,
    voice_name: str,
    speaking_rate: float,
    pitch: float,
    volume_gain_db: float,
    max_chars_per_chunk: int,
    max_retries: int,
    initial_retry_delay: float,
) -> bytes:
    _ensure_credentials_from_inline_json()
    client = texttospeech.TextToSpeechClient()

    chunks = split_into_chunks(text, max_chars_per_chunk)
    logger.info("Synthesizing chunks", extra={"chunks": len(chunks)})
    audio_parts: List[bytes] = []

    for idx, chunk in enumerate(chunks):
        attempt = 0
        delay = initial_retry_delay
        while True:
            attempt += 1
            try:
                input_text = texttospeech.SynthesisInput(text=chunk)
                voice_params = texttospeech.VoiceSelectionParams(
                    language_code=language_code,
                    name=voice_name,
                )
                audio_config = texttospeech.AudioConfig(
                    audio_encoding=texttospeech.AudioEncoding.MP3,
                    speaking_rate=speaking_rate,
                    pitch=pitch,
                    volume_gain_db=volume_gain_db,
                )
                response = client.synthesize_speech(
                    request={
                        "input": input_text,
                        "voice": voice_params,
                        "audio_config": audio_config,
                    }
                )
                audio_parts.append(response.audio_content)
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "TTS chunk failed",
                    extra={"chunk_index": idx, "attempt": attempt, "error": str(e)},
                )
                if attempt >= max_retries:
                    raise
                time.sleep(delay)
                delay *= 2

    # Concatenate MP3 frames (Google returns raw frames without ID3 tags)
    mp3_bytes = b"".join(audio_parts)
    return mp3_bytes

