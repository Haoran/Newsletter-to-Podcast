from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin
import os
import re

import feedparser
import logging
import time
from readability import Document
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import trafilatura
from newspaper import Article
import nltk
from bs4 import BeautifulSoup
try:
    from .diffbot_client import fetch_via_diffbot  # optional
except Exception:  # pragma: no cover
    fetch_via_diffbot = None  # type: ignore


logger = logging.getLogger(__name__)


def fetch_feed(url: str, fetch_original: bool = False) -> List[Dict[str, Any]]:
    # If configured URL is the Axios AI Plus newsletter page, scrape it directly
    try:
        parsed_url = urlparse(url)
        if (
            parsed_url.netloc.endswith("axios.com")
            and "/newsletters/axios-ai-plus" in parsed_url.path
        ):
            items = fetch_from_listing(url)
            logger.info("Fetched items from Axios AI Plus page", extra={"count": len(items)})
            return items
    except Exception:
        # Fallback to RSS flow if URL parsing fails
        pass

    parsed = feedparser.parse(url)
    if parsed.bozo:
        logger.warning("Feed parse warning", extra={"detail": str(parsed.bozo_exception)})
    items: List[Dict[str, Any]] = []
    for e in parsed.entries:
        guid = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", "")
        link = getattr(e, "link", "")
        title = getattr(e, "title", "(no title)")
        author = getattr(e, "author", "")
        published = getattr(e, "published", "") or getattr(e, "updated", "")
        summary = getattr(e, "summary", "")
        content = summary
        if getattr(e, "content", None):
            try:
                content = e.content[0].value or content  # type: ignore[attr-defined]
            except Exception:
                pass
        item: Dict[str, Any] = {
            "guid": guid,
            "link": link,
            "title": title,
            "author": author,
            "published": published,
            "content_html": content,
            "content_source": "rss",
        }

        if fetch_original and link:
            page_html = fetch_article_page(link)
            if page_html:
                extracted, source = extract_main_html(page_html, base_url=link)
                if extracted:
                    item["content_html"] = extracted
                    item["content_source"] = source or "original"

        items.append(item)
    logger.info("Fetched feed entries", extra={"count": len(items)})
    return items


def fetch_from_listing(list_url: str, max_items: int = 20) -> List[Dict[str, Any]]:
    """Build a single issue from the Axios AI Plus hub page itself.

    Only use https://www.axios.com/newsletters/axios-ai-plus content; do not follow
    deeper article links. Extract the top date and compose HTML from the hub page.
    """
    page_html = fetch_article_page(list_url)
    if not page_html:
        logger.warning("Failed to load listing page", extra={"url": list_url})
        return []

    # If the listing page is plaintext (via reader), try to parse it as a single issue directly
    if page_html and "<" not in page_html[:1000]:
        issue = extract_issue_from_listing_plaintext(page_html)
        if issue:
            title, author, published, html_body = issue
            return [
                {
                    # Only append a fragment when a published date was found
                    "guid": list_url + (("#" + published) if published else ""),
                    "link": list_url,
                    "title": title or "Axios AI Plus",
                    "author": author or "Axios",
                    "published": published or "",
                    "content_html": html_body,
                    "content_source": "newsletter_issue_text",
                }
            ]

    # HTML path: extract date and compose content from the hub page itself
    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        soup = BeautifulSoup(page_html, "html.parser")

    published = None
    date_pat = re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        re.IGNORECASE,
    )
    for h in soup.find_all(["h1", "h2", "h3"], limit=8):
        txt = h.get_text(" ", strip=True)
        m = date_pat.search(txt)
        if m:
            published = m.group(0)
            break
    if not published:
        m = date_pat.search(soup.get_text(" ", strip=True)[:2000])
        if m:
            published = m.group(0)

    # Use the listing page itself as the content source
    content_html, source = extract_main_html(page_html, base_url=list_url)
    if not content_html:
        logger.info("Could not extract main content from listing HTML", extra={"url": list_url})
        return []

    title = f"Axios AI Plus — {published}" if published else "Axios AI Plus — Latest"
    item: Dict[str, Any] = {
        "guid": list_url + ("#" + published if published else ""),
        "link": list_url,
        "title": title,
        "author": "Axios",
        "published": published or "",
        "content_html": content_html,
        "content_source": "newsletter_issue",
    }
    return [item]


def extract_issue_from_listing_plaintext(text: str) -> Optional[Tuple[str, str, str, str]]:
    """Parse a newsletter listing plaintext into a single-issue HTML body.

    Returns (title, author, published, html_body) or None.
    Heuristics target Axios AI Plus format with a date header and 1..5 numbered items.
    """
    lines = [ln.rstrip() for ln in text.splitlines()]
    # Date like "October 14, 2025" — search entire text to be robust
    m_date = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+20\d{2}\b",
        text,
    )
    published = m_date.group(0) if m_date else None

    # Find numbered headings starting with 1..5
    starts: List[Tuple[int, int, str]] = []  # (idx, num, title)
    for idx, ln in enumerate(lines):
        m = re.match(r"^(?P<num>[1-5])(?:\.|\)|\:)?\s+(?P<title>.+)$", ln.strip())
        if m:
            num = int(m.group("num"))
            title = m.group("title").strip()
            # Strip basic Markdown markers from title to avoid TTS reading them
            title = title.replace("**", "").replace("__", "").replace("`", "")
            # Normalize special case like "1 big thing: ..."
            title = re.sub(r"\s*:\s*$", "", title)
            starts.append((idx, num, title))
    # Need at least items 1..4
    have_nums = {n for (_, n, _) in starts}
    if not ({1, 2, 3, 4}.issubset(have_nums)):
        return None
    # Keep up to 5 sections in order of appearance, unique by num
    seen_num: set[int] = set()
    ordered: List[Tuple[int, int, str]] = []
    for tup in starts:
        if tup[1] not in seen_num:
            seen_num.add(tup[1])
            ordered.append(tup)
        if len(ordered) >= 5:
            break
    if not ordered:
        return None

    # Build intro from top until the first numbered header
    intro_html = ""
    first_start = ordered[0][0]
    if first_start > 0:
        intro_block = "\n".join(lines[:first_start]).strip()
        intro_paras = [p.strip() for p in re.split(r"\n\s*\n+", intro_block) if p.strip()]
        if intro_paras:
            intro_html = "".join(f"<p>{re.sub(r'<[^>]+>', '', p)}</p>" for p in intro_paras)

    # Build sections until next header
    html_parts: List[str] = []
    if intro_html:
        html_parts.append(intro_html)
    for i, (start_idx, num, title) in enumerate(ordered):
        end_idx = len(lines)
        if i + 1 < len(ordered):
            end_idx = ordered[i + 1][0]
        body_lines = []
        for ln in lines[start_idx + 1 : end_idx]:
            # stop early at two consecutive blanks (section separator)
            body_lines.append(ln)
        body = "\n".join(body_lines).strip()

        # Use the newsletter's own prose from the listing text; do not replace with linked articles
        paras = [p.strip() for p in re.split(r"\n\s*\n+", body) if p.strip()]
        html_body = "".join(f"<p>{re.sub(r'<[^>]+>', '', p)}</p>" for p in paras) if paras else ""
        html_parts.append(f"<h3>{num}. {title}</h3>" + html_body)

    issue_title = None
    if published:
        issue_title = f"Axios AI Plus — {published}"
    else:
        issue_title = "Axios AI Plus — Latest"
    author = "Axios"
    return issue_title, author, published or "", "".join(html_parts)


def extract_article_links_from_html(page_html: str, base_url: Optional[str] = None) -> List[str]:
    """Extract likely article links from a listing/issue page.

    Heuristics:
    - Prefer links with dated paths like /YYYY/MM/DD/
    - Only keep links on axios.com domain
    - De-duplicate and keep readable URLs (no fragments)
    """
    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        soup = BeautifulSoup(page_html, "html.parser")

    # Collect all anchor hrefs in main content area if available
    container = soup.find("main") or soup
    hrefs: List[str] = []
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        abs_url = urljoin(base_url or "", href)
        try:
            pu = urlparse(abs_url)
        except Exception:
            continue
        if not pu.netloc.endswith("axios.com"):
            continue
        # Heuristic: match dated articles like /2024/10/15/... or /2025/...
        if re.search(r"/20\d{2}/\d{2}/\d{2}/", pu.path):
            hrefs.append(abs_url)

    # Fallback: if nothing matched, consider article-card anchors with data-testid
    if not hrefs:
        for a in container.select("a[data-testid*='card'], a[aria-label], a[rel='bookmark']"):
            href = a.get("href")
            if not href:
                continue
            abs_url = urljoin(base_url or "", href)
            try:
                pu = urlparse(abs_url)
            except Exception:
                continue
            if pu.netloc.endswith("axios.com") and "/newsletters/" not in pu.path:
                hrefs.append(abs_url)

    # If still empty, try regex over plaintext for dated Axios URLs
    if not hrefs and page_html and "<" not in page_html[:1000]:
        for m in re.finditer(r"https?://(?:www\.)?axios\.com/20\d{2}/\d{2}/\d{2}/[\w\-/%]+", page_html):
            hrefs.append(m.group(0))

    # Deduplicate while preserving order
    seen: set[str] = set()
    ordered: List[str] = []
    for u in hrefs:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered


def extract_latest_issue_link(page_html: str, base_url: Optional[str] = None) -> Optional[str]:
    """Find the latest Axios AI Plus issue URL from hub page (HTML or plaintext)."""
    candidates: List[Tuple[str, str]] = []  # (url, yyyymmdd)
    # Regex that matches either /yyyy/mm/dd or /yyyy-mm-dd after the newsletter path
    pat = re.compile(r"/newsletters/axios-ai-plus/(20\d{2})[-/](\d{2})[-/](\d{2})(?:/|$)")

    # Try HTML
    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        soup = BeautifulSoup(page_html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        abs_url = urljoin(base_url or "", href)
        m = pat.search(abs_url)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            candidates.append((abs_url, f"{y}{mo}{d}"))

    # Plaintext fallback
    if not candidates and page_html and "<" not in page_html[:1000]:
        for m in re.finditer(r"https?://(?:www\.)?axios\.com/newsletters/axios-ai-plus/(20\d{2})[-/](\d{2})[-/](\d{2})(?:/|$)", page_html):
            abs_url = m.group(0)
            y, mo, d = m.group(1), m.group(2), m.group(3)
            candidates.append((abs_url, f"{y}{mo}{d}"))

    if not candidates:
        return None
    # Pick the max yyyymmdd
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def extract_title_author_date(page_html: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract title, author, and published date from an article page's HTML."""
    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        soup = BeautifulSoup(page_html, "html.parser")

    # Title
    title = None
    ogt = soup.find("meta", attrs={"property": "og:title"})
    if ogt and ogt.get("content"):
        title = ogt["content"].strip()
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title and page_html and "<" not in page_html[:1000]:
        for ln in page_html.splitlines():
            ln = ln.strip()
            if ln:
                title = ln[:200]
                break

    # Author
    author = None
    meta_author = soup.find("meta", attrs={"name": "author"})
    if meta_author and meta_author.get("content"):
        author = meta_author["content"].strip()
    # Published time
    published = None
    pub_meta = soup.find("meta", attrs={"property": "article:published_time"})
    if pub_meta and pub_meta.get("content"):
        published = pub_meta["content"].strip()
    if not published:
        t = soup.find("time")
        if t and (t.get("datetime") or t.get_text(strip=True)):
            published = (t.get("datetime") or t.get_text(strip=True)).strip()

    return title, author, published


def parse_date_from_url(href: str) -> Optional[str]:
    m = re.search(r"/(20\d{2})/(\d{2})/(\d{2})/", href)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    return f"{y}-{mo}-{d}"


def fetch_article_page(url: str, max_retries: int = 3, timeout: float = 10.0) -> Optional[str]:
    # Support local file input (useful under restricted network)
    try:
        pu = urlparse(url)
        if pu.scheme == "file":
            local_path = pu.path
            if os.path.exists(local_path):
                with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read()
        if pu.scheme == "" and os.path.exists(url):
            with open(url, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception:
        pass
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Referer": url,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    session = requests.Session()
    retry = Retry(
        total=max_retries,
        backoff_factor=1.0,
        status_forcelist=[403, 404, 408, 429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and resp.text:
            return resp.text
        logger.warning(
            f"Article fetch non-200 ({resp.status_code})",
            extra={"url": url, "status": resp.status_code, "reason": getattr(resp, "reason", "")},
        )
        # Try Diffbot first (if available via env token)
        if fetch_via_diffbot:
            try:
                via = fetch_via_diffbot(url)
                if via and len(via.strip()) > 200:
                    logger.info("Fetched via Diffbot fallback", extra={"url": url})
                    return via
            except Exception:
                pass
        # Then r.jina.ai fallback
        alt = fetch_via_jina(url, timeout=timeout)
        if alt:
            logger.info("Fetched via r.jina.ai fallback", extra={"url": url})
            return alt
    except Exception as e:  # noqa: BLE001
        logger.warning("Article fetch error", extra={"url": url, "error": str(e)})
        # Try Diffbot first (if available)
        if fetch_via_diffbot:
            try:
                via = fetch_via_diffbot(url)
                if via and len(via.strip()) > 200:
                    logger.info("Fetched via Diffbot fallback", extra={"url": url})
                    return via
            except Exception:
                pass
        # Then r.jina.ai fallback
        alt = fetch_via_jina(url, timeout=timeout)
        if alt:
            logger.info("Fetched via r.jina.ai fallback", extra={"url": url})
            return alt
    return None


def fetch_via_jina(url: str, timeout: float = 10.0) -> Optional[str]:
    try:
        session = requests.Session()
        adapter = HTTPAdapter(max_retries=Retry(total=1, backoff_factor=0))
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/plain,*/*;q=0.8",
        }
        pu = urlparse(url)
        # Try both http and https targets
        for scheme in ("https", "http"):
            proxied = f"https://r.jina.ai/{scheme}://{pu.netloc}{pu.path}"
            if pu.query:
                proxied += f"?{pu.query}"
            resp = session.get(proxied, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                return resp.text
    except Exception:
        pass
    return None


def _looks_blocked_or_too_short(text: str) -> bool:
    try:
        if not text:
            return True
        t = text.strip()
        if len(t) < 200:
            return True
        lowered = t.lower()
        signals = (
            "verify you are human",
            "needs to review the security of your connection",
            "access denied",
            "forbidden",
            "captcha",
            "target url returned error",
        )
        return any(s in lowered for s in signals)
    except Exception:
        return True


def _web_text_looks_useful(text: str) -> bool:
    # Deprecated: kept for backward compatibility; we now just check length for Diffbot
    try:
        return bool(text and len(text.strip()) > 200)
    except Exception:
        return False


def extract_main_html(page_html: str, base_url: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    # Plaintext input (e.g., from r.jina.ai) → wrap into paragraphs
    if page_html and "<" not in page_html[:1000]:
        lines = [ln.strip() for ln in page_html.splitlines()]
        paras: List[str] = []
        buf: List[str] = []
        for ln in lines:
            if ln:
                buf.append(ln)
            elif buf:
                paras.append(" ".join(buf))
                buf = []
        if buf:
            paras.append(" ".join(buf))
        if paras:
            html_out = "".join(f"<p>{p}</p>" for p in paras)
            return html_out, "plaintext"
    # 1) Trafilatura (prefer fulltext extraction)
    try:
        txt = trafilatura.extract(
            page_html,
            url=base_url,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if txt and len(txt) > 200:
            paras = [p.strip() for p in txt.split("\n") if p.strip()]
            html_out = "".join(f"<p>{p}</p>" for p in paras)
            return html_out, "trafilatura"
    except Exception:
        pass

    # 2) Readability as secondary
    try:
        doc = Document(page_html)
        summary = doc.summary(html_partial=True)
        if summary and len(BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)) > 200:
            return summary, "readability"
    except Exception:
        pass

    # 3) Newspaper3k fallback
    try:
        try:
            # Ensure tokenizer exists (best effort)
            nltk.data.find("tokenizers/punkt")
        except Exception:
            nltk.download("punkt", quiet=True)
        art = Article(url=base_url or "")
        art.set_html(page_html)
        art.parse()
        if art.text and len(art.text) > 200:
            paras = [p.strip() for p in art.text.split("\n") if p.strip()]
            html_out = "".join(f"<p>{p}</p>" for p in paras)
            return html_out, "newspaper3k"
    except Exception:
        pass

    # Fallback: heuristic with BeautifulSoup
    try:
        soup = BeautifulSoup(page_html, "lxml")
    except Exception:
        soup = BeautifulSoup(page_html, "html.parser")

    # Try typical article containers
    candidates = [
        {"name": "article"},
        {"name": "div", "attrs": {"data-component": "ArticleBody"}},
        {"name": "div", "attrs": {"data-testid": "ArticleContent"}},
        {"name": "section", "attrs": {"role": "main"}},
        {"name": "main"},
    ]
    for sel in candidates:
        node = soup.find(**sel)
        if node and len(node.get_text(strip=True)) > 120:
            return str(node), "heuristic"

    # Last resort: largest text block
    best = None
    best_len = 0
    for div in soup.find_all(["div", "section", "article"]):
        txt_len = len(div.get_text(" ", strip=True))
        if txt_len > best_len:
            best = div
            best_len = txt_len
    if best and best_len > 120:
        return str(best), "heuristic"
    return None, None
