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
    try:
        # Some SDK/accounts support a built-in web tool via responses.create(tools=[{"type":"web_search"}])
        resp = client.responses.create(  # type: ignore[attr-defined]
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Fetch the main article content from the following URL and return plain text only,"
                                " without any greeting/intro/outro, no summary beyond what is present: " + url
                            ),
                        }
                    ],
                }
            ],
            tools=[{"type": "web_search"}],  # if not supported, will raise
        )
        # Many SDK versions expose a convenience accessor
        text = getattr(resp, "output_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        # Fallback – try to navigate the generic structure
        try:
            if resp.output and len(resp.output) > 0:  # type: ignore[attr-defined]
                parts = []
                for seg in resp.output:  # type: ignore[attr-defined]
                    if isinstance(seg, dict):
                        for c in seg.get("content", []):
                            if c.get("type") == "output_text" and c.get("text"):
                                parts.append(c["text"])  # type: ignore[index]
                if parts:
                    return "\n".join(parts).strip()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        logger.info("OpenAI web fetch via Responses tool not available", extra={"error": str(e)})

    # If the tool/path above isn't available, skip (avoid hallucinated content)
    return None

