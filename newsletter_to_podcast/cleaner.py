from __future__ import annotations

import html
import logging
import re
from typing import List, Tuple

from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


EMOJI_PATTERN = re.compile(
    "[\U0001F600-\U0001F64F]"  # emoticons
    "|[\U0001F300-\U0001F5FF]"  # symbols & pictographs
    "|[\U0001F680-\U0001F6FF]"  # transport & map
    "|[\U0001F1E0-\U0001F1FF]"  # flags
    "|[\U00002700-\U000027BF]"  # dingbats
    "|[\U0001F900-\U0001F9FF]"  # supplemental symbols and pictographs
    "|[\U00002600-\U000026FF]",  # misc symbols
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    return EMOJI_PATTERN.sub("", text)


def clean_html_text(content_html: str, remove_emoji: bool, remove_ads: bool, ad_keywords: List[str]) -> Tuple[str, str]:
    soup = BeautifulSoup(content_html or "", "html.parser")

    # Remove images/figures/scripts/styles
    for tag in soup.find_all(["img", "figure", "figcaption", "script", "style", "noscript"]):
        tag.decompose()

    # Drop ad/sponsor paragraphs by keyword
    if remove_ads and ad_keywords:
        for p in soup.find_all(["p", "div", "section"]):
            txt = p.get_text(" ", strip=True).lower()
            if any(k.lower() in txt for k in ad_keywords):
                p.decompose()

    # Unwrap links, keep text
    for a in soup.find_all("a"):
        a.replace_with(a.get_text(" ", strip=True))

    # Normalize into paragraph list while preserving breaks
    raw_parts: List[str] = []
    for el in soup.find_all(["p", "h1", "h2", "h3", "li", "blockquote"]):
        t = el.get_text(" ", strip=True)
        if t:
            raw_parts.append(t)

    if not raw_parts:
        # Fallback: get whole text but try to keep some breaks
        whole = soup.get_text("\n", strip=True)
        raw_parts = [x.strip() for x in re.split(r"\n{2,}", whole) if x.strip()]

    def strip_markdown(in_text: str) -> str:
        # Basic Markdown cleanup so TTS won't read symbols
        t = in_text
        # Links: [text](url) -> text
        t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", t)
        # Images: ![alt](url) -> alt
        t = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1", t)
        # Emphasis/strong/code markers
        t = t.replace("**", "").replace("__", "")
        t = t.replace("`", "")
        # Single underscore italics: _text_ -> text
        t = re.sub(r"_(.*?)_", r"\1", t)
        # Headings: remove leading # tokens
        t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t)
        # Blockquotes: leading >
        t = re.sub(r"^\s*>\s?", "", t)
        return t

    def strip_noise_tokens(in_text: str) -> str:
        # Remove boilerplate tokens that come from reader proxies
        t = in_text
        # Whole-line removals
        patterns_line = [
            r"^\s*(?:URL\s*Source|Markdown\s*Content)\b.*$",
            r"^\s*!?\s*Image\b.*$",
            r"^\s*Illustration\b.*$",
            r"^\s*(?:Publish|Published)\s*Time:\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?.*$",
            r"^\s*Title\s*:\s*.*$",
            r"^\s*Share this story\.?\s*$",
            r"^\s*Thanks to\b.*$",
        ]
        for pat in patterns_line:
            if re.match(pat, t, flags=re.IGNORECASE):
                return ""  # drop the line entirely

        # Inline removals for specific labels
        t = re.sub(r"\b(?:URL\s*Source|Markdown\s*Content)\b\s*:?\s*\S+", "", t, flags=re.IGNORECASE)
        return t.strip()

    # Unescape and clean spaces within each paragraph, keep paragraph boundaries
    cleaned_parts: List[str] = []
    for p in raw_parts:
        cp = html.unescape(p)
        cp = re.sub(r"[ \t]+", " ", cp).strip()
        # Fix single-letter bracketed tokens anywhere: "[R]ecent" -> "Recent", "[T]he" -> "The"
        cp = re.sub(r"\[([A-Za-z])\]", r"\1", cp)
        # Drop Markdown markers
        cp = strip_markdown(cp)
        # Drop reader-proxy noise tokens
        cp = strip_noise_tokens(cp)
        # Remove leading bullet symbols
        cp = re.sub(r"^\s*[\-*•]\s+", "", cp)
        # Punctuation spacing normalization: ensure space after colon
        cp = re.sub(r":(?=\S)", ": ", cp)
        if remove_emoji:
            cp = strip_emoji(cp)
        if cp:
            cleaned_parts.append(cp)

    text = "\n\n".join(cleaned_parts)

    # Also produce a simple HTML description for RSS (preserve paragraphs)
    desc_html = "".join(f"<p>{html.escape(p)}</p>" for p in cleaned_parts)

    logger.debug("Cleaned text length", extra={"len": len(text)})
    return text, desc_html
