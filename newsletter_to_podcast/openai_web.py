from __future__ import annotations

import logging
import os
from typing import Optional

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


def fetch_via_openai_web(
    url: str,
    model: str = "gpt-4o-mini",
    api_key_env: str = "OPENAI_API_KEY",
) -> Optional[str]:
    """Attempt to fetch main article text for a URL via OpenAI Responses API with web search.

    Returns plain text on success, or None on failure/not available.
    """
    api_key = os.environ.get(api_key_env)
    client, _ = _get_openai_client(api_key)
    if client is None:
        return None

    # Prefer Responses API (with web search tool) if available in current SDK/account
    # Attempt with Responses API using the correct typed content 'input_text'
    # Try tool name 'web_search' first; if unsupported, optionally try 'web'.
    for tool_name in ("web_search", "web"):
        try:
            resp = client.responses.create(  # type: ignore[attr-defined]
                model=model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Extract the readable body text from this URL and return VERBATIM paragraphs only. "
                                    "Do NOT summarize, do NOT add intros/outros, do NOT add commentary. "
                                    "Preserve section headings and numbered items if present. URL: " + url
                                ),
                            }
                        ],
                    }
                ],
                tools=[{"type": tool_name}],
            )
            text = getattr(resp, "output_text", None)
            if isinstance(text, str) and text.strip():
                return text.strip()
            # Generic structure fallback
            try:
                out = getattr(resp, "output", None)  # type: ignore[attr-defined]
                if out and isinstance(out, list):
                    parts = []
                    for seg in out:
                        if isinstance(seg, dict):
                            for c in seg.get("content", []):
                                if c.get("type") in ("output_text", "summary_text") and c.get("text"):
                                    parts.append(c["text"])  # type: ignore[index]
                    if parts:
                        return "\n".join(parts).strip()
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            # Try next tool name or give up
            logger.info(
                "OpenAI web fetch via Responses tool not available",
                extra={"error": str(e), "tool": tool_name},
            )

    # If the tool/path above isn't available, skip (avoid hallucinated content)
    return None
