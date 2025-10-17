from __future__ import annotations

import datetime as dt
import json
import os
from typing import Any, Dict, List

import sys
import yaml

# Ensure repo root is importable
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Import project helpers
from newsletter_to_podcast.storage import load_state, save_state
from newsletter_to_podcast.rss import render_rss


TARGET_DATE = dt.date(2025, 10, 17)


def main() -> None:
    # 1) Load state and filter out the duplicated 2025-10-17 episode
    state: Dict[str, Any] = load_state()
    episodes: List[Dict[str, Any]] = state.get("episodes", [])
    kept: List[Dict[str, Any]] = []
    removed = 0
    for ep in episodes:
        pd = ep.get("pub_date")
        try:
            if isinstance(pd, dt.datetime):
                if pd.date() == TARGET_DATE:
                    removed += 1
                    continue
        except Exception:
            pass
        # also check id safety
        eid = ep.get("id", "")
        if isinstance(eid, str) and eid.startswith("issue::2025-10-17::"):
            removed += 1
            continue
        kept.append(ep)

    # Sort remaining by pub_date desc just in case
    kept.sort(key=lambda x: x.get("pub_date", dt.datetime.now(dt.timezone.utc)), reverse=True)
    state["episodes"] = kept
    save_state(state)

    # 2) Delete audio/transcript files for 2025-10-17 if present
    audio_dir = os.path.join("docs", "audio", "2025", "10")
    for ext in (".mp3", ".txt"):
        p = os.path.join(audio_dir, f"2025-10-17{ext}")
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    # 3) Re-render feed.xml using current state
    with open("config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    site = cfg.get("site", {})
    output = cfg.get("output", {})
    feed_filename = output.get("feed_filename", "feed.xml")
    feed_root = output.get("root_dir", "docs")
    site_link = site.get("link", "/").rstrip("/")
    feed_url = f"{site_link}/{feed_filename}"

    # Load state again to ensure datetime normalization
    state2 = load_state()
    items = state2.get("episodes", [])
    xml = render_rss(
        site={
            "title": site.get("title", "Podcast"),
            "link": site_link + "/",
            "description": site.get("description", ""),
            "language": site.get("language", "en-us"),
            "author": site.get("author", ""),
            "owner_name": (site.get("owner", {}) or {}).get("name", ""),
            "owner_email": (site.get("owner", {}) or {}).get("email", ""),
            "image_url": site.get("image_url", ""),
        },
        items=items,
        feed_url=feed_url,
    )
    os.makedirs(feed_root, exist_ok=True)
    with open(os.path.join(feed_root, feed_filename), "w", encoding="utf-8") as f:
        f.write(xml)

    print(f"Removed episodes: {removed}. Feed regenerated with {len(items)} items.")


if __name__ == "__main__":
    main()
