from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


def _request_diffbot(api: str, url: str, token: str, timeout: float = 10.0) -> Optional[dict]:
    try:
        ep = f"https://api.diffbot.com/v3/{api}"
        params = {"token": token, "url": url}
        resp = requests.get(ep, params=params, timeout=timeout)
        if resp.status_code != 200:
            logger.info(
                "Diffbot non-200",
                extra={"status": resp.status_code, "reason": getattr(resp, "reason", ""), "api": api},
            )
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:  # noqa: BLE001
        logger.info("Diffbot request failed", extra={"error": str(e), "api": api})
        return None


def _build_plaintext_from_list_payload(payload: dict, max_items: int = 10) -> Optional[str]:
    try:
        objs = payload.get("objects") or []
        if not objs:
            return None
        items = (objs[0] or {}).get("items") or []
        if not items:
            return None
        parts = []
        for idx, it in enumerate(items[:max_items], start=1):
            title = (it.get("title") or "").strip()
            summary = (it.get("summary") or "").strip()
            if title:
                parts.append(f"{idx}. {title}")
            if summary:
                parts.append(summary)
        text = "\n\n".join(p for p in parts if p)
        return text or None
    except Exception:
        return None


def _extract_article_text(payload: dict) -> Optional[str]:
    try:
        objs = payload.get("objects") or []
        if not objs:
            return None
        obj = objs[0] or {}
        # Prefer text field; fallback to html stripped by caller later
        txt = (obj.get("text") or "").strip()
        if txt:
            return txt
        html = (obj.get("html") or "").strip()
        if html:
            return html
        return None
    except Exception:
        return None


def fetch_via_diffbot(url: str, token_env: str = "DIFFBOT_TOKEN", timeout: float = 10.0) -> Optional[str]:
    """Fetch readable content using Diffbot. Returns plaintext or HTML string, or None.

    Strategy:
    - For newsletter hub/listing-like URLs, try /v3/list first and build plaintext from items.
    - Otherwise try /v3/article to extract main text/html.
    - If the first attempt yields nothing, try the other API as a fallback.
    """
    token = os.environ.get(token_env)
    if not token:
        logger.info("Diffbot token missing; skipping Diffbot fallback", extra={"env": token_env})
        return None

    url_lc = url.lower()
    looks_listing = any(s in url_lc for s in ("/newsletters/", "/category/", "/tag/"))

    if looks_listing:
        # 1) list → 2) article
        data = _request_diffbot("list", url, token, timeout=timeout)
        if data:
            text = _build_plaintext_from_list_payload(data)
            if text and len(text) > 200:
                return text
        data = _request_diffbot("article", url, token, timeout=timeout)
        if data:
            text = _extract_article_text(data)
            if text and len(text.strip()) > 200:
                return text
        return None

    # Non-listing: 1) article → 2) list
    data = _request_diffbot("article", url, token, timeout=timeout)
    if data:
        text = _extract_article_text(data)
        if text and len(text.strip()) > 200:
            return text
    data = _request_diffbot("list", url, token, timeout=timeout)
    if data:
        text = _build_plaintext_from_list_payload(data)
        if text and len(text) > 200:
            return text
    return None
