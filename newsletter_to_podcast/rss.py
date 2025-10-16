from __future__ import annotations

import datetime as dt
import email.utils
import html
import logging
import os
from typing import Any, Dict, List, Optional
from dateutil import parser as dateparser


logger = logging.getLogger(__name__)


def rfc2822(dt_obj: dt.datetime) -> str:
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=dt.timezone.utc)
    return email.utils.format_datetime(dt_obj)


def render_rss(
    site: Dict[str, str],
    items: List[Dict[str, Any]],
    feed_url: str,
) -> str:
    # Namespaces: itunes + podcast (Podcast 2.0)
    rss_head = (
        f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<rss version=\"2.0\"\n"
        "     xmlns:itunes=\"http://www.itunes.com/dtds/podcast-1.0.dtd\"\n"
        "     xmlns:atom=\"http://www.w3.org/2005/Atom\"\n"
        "     xmlns:podcast=\"https://podcastindex.org/namespace/1.0\">\n"
        "  <channel>\n"
        f"    <title>{html.escape(site['title'])}</title>\n"
        f"    <link>{html.escape(site['link'])}</link>\n"
        f"    <description>{html.escape(site['description'])}</description>\n"
        f"    <language>{html.escape(site['language'])}</language>\n"
        f"    <atom:link href=\"{html.escape(feed_url)}\" rel=\"self\" type=\"application/rss+xml\" />\n"
        f"    <itunes:author>{html.escape(site.get('author',''))}</itunes:author>\n"
        "    <itunes:owner>\n"
        f"      <itunes:name>{html.escape(site.get('owner_name',''))}</itunes:name>\n"
        f"      <itunes:email>{html.escape(site.get('owner_email',''))}</itunes:email>\n"
        "    </itunes:owner>\n"
    )
    if site.get("image_url"):
        img = html.escape(site['image_url'])
        rss_head += f"    <itunes:image href=\"{img}\" />\n"
        # Standard RSS image block for broader compatibility
        rss_head += (
            "    <image>\n"
            f"      <url>{img}</url>\n"
            f"      <title>{html.escape(site['title'])}</title>\n"
            f"      <link>{html.escape(site['link'])}</link>\n"
            "    </image>\n"
        )

    rss_items = []
    for it in items:
        title = html.escape(it["title"])
        link = html.escape(it.get("link", ""))
        guid = html.escape(it.get("id", link or title))
        pub = it.get("pub_date")
        if isinstance(pub, dt.datetime):
            pub_str = rfc2822(pub)
        else:
            try:
                parsed = dateparser.parse(str(pub))
                pub_str = rfc2822(parsed or dt.datetime.now(dt.timezone.utc))
            except Exception:
                pub_str = rfc2822(dt.datetime.now(dt.timezone.utc))
        desc_html = it.get("description_html", "")
        enclosure = ""
        if it.get("audio_url"):
            enclosure = (
                f"<enclosure url=\"{html.escape(it['audio_url'])}\" length=\"{int(it.get('audio_bytes',0))}\" type=\"audio/mpeg\" />\n"
            )
        transcript_xml = ""
        if it.get("transcript_url"):
            transcript_xml = f"<podcast:transcript url=\"{html.escape(it['transcript_url'])}\" type=\"text/plain\" />\n"

        item_xml = f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="false">{guid}</guid>
      <pubDate>{pub_str}</pubDate>
      {enclosure}      {transcript_xml}      <description><![CDATA[{desc_html}]]></description>
    </item>
"""
        rss_items.append(item_xml)

    rss_tail = "  </channel>\n</rss>\n"
    xml = rss_head + "".join(rss_items) + rss_tail
    return xml


def render_index_html(title: str, feed_url: str) -> str:
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Helvetica, Arial, sans-serif; margin: 2rem; }}
    code {{ background: #f4f4f4; padding: .2rem .4rem; border-radius: 4px; }}
  </style>
  <link rel="alternate" type="application/rss+xml" title="{html.escape(title)}" href="{html.escape(feed_url)}" />
  <meta property="og:type" content="website" />
  <meta property="og:title" content="{html.escape(title)}" />
  <meta property="og:description" content="Podcast feed generated from Axios newsletter." />
  <meta property="og:url" content="{html.escape(feed_url)}" />
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="{html.escape(title)}" />
  <meta name="twitter:description" content="Podcast feed generated from Axios newsletter." />
  <link rel="icon" href="data:," />
  <meta name="robots" content="index,follow" />
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p>RSS: <a href="{html.escape(feed_url)}"><code>{html.escape(feed_url)}</code></a></p>
  <p>Submit this feed to your podcast app (e.g., Spotify for Podcasters).</p>
</body>
</html>
"""
