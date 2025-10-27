from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, List
import re

from dateutil import parser as dateparser
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, WOAF

from .config import AppConfig, load_config, validate_config
from .fetcher import fetch_feed
from .cleaner import clean_html_text
from .logger import setup_logging, gha_notice
from .rss import render_rss, render_index_html
from .storage import ensure_dirs, load_state, save_state, sha256, slugify
from .tts import synthesize_mp3
from .tts_openai import synthesize_mp3_openai
from .llm_cleaner import maybe_clean_text_with_llm
from .llm_rewriter import maybe_rewrite_for_audio


logger = logging.getLogger(__name__)


def parse_datetime(s: str) -> dt.datetime:
    try:
        v = dateparser.parse(s)
        if v is None:
            return dt.datetime.now(dt.timezone.utc)
        if v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)
        return v
    except Exception:
        return dt.datetime.now(dt.timezone.utc)

def extract_date_from_title(title: str) -> dt.date | None:
    """Try to extract a date from the article title.
    Supports formats like:
    - YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
    - YYYY年M月D日 (Chinese)
    - Month D, YYYY (English month name)
    Returns a dt.date or None if not found.
    """
    if not title:
        return None

    # 1) Explicit numeric formats
    m = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", title)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return dt.date(y, mo, d)
        except ValueError:
            pass

    # 2) Chinese format: 2025年10月15日
    m = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", title)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return dt.date(y, mo, d)
        except ValueError:
            pass

    # 3) English month name: October 15, 2025 (or Oct 15, 2025)
    # Use dateutil to parse if a month name is present
    if re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December)\b", title, re.IGNORECASE):
        try:
            v = dateparser.parse(title, fuzzy=True)
            if v:
                return v.date()
        except Exception:
            pass

    return None


def build_public_url(site_link: str, rel_path: str) -> str:
    return site_link.rstrip("/") + "/" + rel_path.lstrip("/")


def write_audio_with_id3(path: str, data: bytes, title: str, artist: str, date_str: str, link: str) -> None:
    with open(path, "wb") as f:
        f.write(data)
    try:
        tags = EasyID3(path)
    except Exception:
        tags = EasyID3()
        tags.save(path)
        tags = EasyID3(path)
    tags["title"] = title
    if artist:
        tags["artist"] = artist
    if date_str:
        tags["date"] = date_str
    tags.save()
    try:
        id3 = ID3(path)
        if link:
            id3.add(WOAF(url=link))
        id3.save(v2_version=3)
    except Exception:
        pass


def run(config: AppConfig) -> int:
    # Setup dirs
    out_root = config.output.root_dir
    audio_dir = os.path.join(out_root, config.output.audio_dir)
    ensure_dirs(out_root, audio_dir, "data")

    # Load state
    state = load_state()
    processed: Dict[str, Any] = state.get("processed", {})
    episodes: List[Dict[str, Any]] = state.get("episodes", [])

    # Fetch
    items = fetch_feed(config.feed.url, fetch_original=config.feed.fetch_original)

    # Prepare new items
    new_items: List[Dict[str, Any]] = []
    for it in items:
        cleaned, desc_html = clean_html_text(
            it["content_html"],
            remove_emoji=config.clean.remove_emoji,
            remove_ads=config.clean.remove_ads,
            ad_keywords=config.clean.ad_keywords,
        )
        content_hash = sha256(cleaned)
        key = f"{it['guid']}::{content_hash}"
        if key in processed:
            continue
        enriched = {**it, "clean_text": cleaned, "desc_html": desc_html, "key": key}
        new_items.append(enriched)

    # Log content source per new item and summary counts
    if new_items:
        src_counts: Dict[str, int] = {}
        for it in new_items:
            src = it.get("content_source", "unknown")
            src_counts[src] = src_counts.get(src, 0) + 1
            logger.info(
                "Item content source",
                extra={"title": it.get("title"), "source": src, "link": it.get("link")},
            )
        logger.info("Content sources summary", extra={"sources": src_counts})

    if not new_items and config.mode not in ("forced_compilation", "force_compilation", "newsletter"):
        logger.info("No new items; exiting.")
        return 0

    # By mode, assemble episodes
    created_episodes: List[Dict[str, Any]] = []
    today = dt.datetime.now(dt.timezone.utc).date()
    # Allow rewriting feed/index even when no new episode is created
    force_rewrite_feed = False

    if config.mode == "separate":
        for it in new_items:
            orig_title = it["title"]
            author = it.get("author", config.site.author)
            link = it.get("link", "")
            pub_dt = parse_datetime(it.get("published", ""))
            # Prefer date present in the title when available
            title_date = extract_date_from_title(orig_title)
            effective_date = title_date or pub_dt.date()
            # Episode display title: `<feed.name>: YYYY-MM-DD — <original title>`
            episode_title = f"{config.feed.name}: {effective_date.isoformat()} — {orig_title}"
            # TTS text still uses the original article title for natural narration
            text = (
                f"{orig_title}. By {author}. {it['clean_text']}"
                if author
                else f"{orig_title}. {it['clean_text']}"
            )
            text = maybe_clean_text_with_llm(text, config)
            text = maybe_rewrite_for_audio(text, config)

            audio_rel = None
            audio_bytes_len = 0
            tts_error = None
            transcript_rel = None

            # Plan paths (audio + transcript) upfront
            date_folder = os.path.join(
                config.output.audio_dir,
                str(effective_date.year),
                f"{effective_date.month:02d}",
            )
            out_dir = os.path.join(config.output.root_dir, date_folder)
            ensure_dirs(out_dir)
            slug = slugify(f"{effective_date}-{title}")
            transcript_fname = f"{slug}.txt"
            transcript_path = os.path.join(out_dir, transcript_fname)
            with open(transcript_path, "w", encoding="utf-8") as tf:
                tf.write(text)
            transcript_rel = os.path.join(date_folder, transcript_fname)

            if config.tts.enabled:
                try:
                    if config.tts.provider.lower() == "openai":
                        mp3 = synthesize_mp3_openai(
                            text=text,
                            model=config.tts.openai_model,
                            voice=config.tts.openai_voice,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                            api_key_env=(getattr(config.llm, "api_key_env", "OPENAI_API_KEY") if getattr(config, "llm", None) else "OPENAI_API_KEY"),
                        )
                    else:
                        mp3 = synthesize_mp3(
                            text=text,
                            language_code=config.tts.language_code,
                            voice_name=config.tts.voice_name,
                            speaking_rate=config.tts.speaking_rate,
                            pitch=config.tts.pitch,
                            volume_gain_db=config.tts.volume_gain_db,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                        )
                    # Build audio path
                    fname = f"{slug}.mp3"
                    path = os.path.join(out_dir, fname)
                    write_audio_with_id3(
                        path,
                        mp3,
                        title=episode_title,
                        artist=author or config.site.title,
                        date_str=str(effective_date),
                        link=link,
                    )
                    audio_rel = os.path.join(date_folder, fname)
                    audio_bytes_len = len(mp3)
                except Exception as e:  # noqa: BLE001
                    tts_error = str(e)
                    gha_notice("ERROR", f"TTS failed for item: {title}: {tts_error}")

            # Build description with source footer
            source = it.get("content_source", "unknown")
            desc_html = it["desc_html"] + f"<p><small>Source: {source}</small></p>"

            canonical_text = it.get("clean_text") or it.get("content_html", "")
            canonical_hash = sha256(canonical_text or "")

            episode = {
                "id": it["key"],
                "title": episode_title,
                "link": link,
                "pub_date": pub_dt,
                "description_html": desc_html + (f"<p><em>TTS failed: {tts_error}</em></p>" if tts_error else ""),
                "audio_url": build_public_url(config.site.link, audio_rel) if audio_rel else None,
                "audio_bytes": audio_bytes_len,
                "components": [
                    {"title": orig_title, "link": link, "source": source},
                ],
                "transcript_url": build_public_url(config.site.link, transcript_rel) if transcript_rel else None,
                "content_hash": canonical_hash,
            }
            created_episodes.append(episode)

    elif config.mode in ("forced_compilation", "force_compilation", "newsletter"):
        # Forced single-episode flow: prefer a single newsletter issue item if present,
        # otherwise compile all available items regardless of date.
        base_pool = new_items if new_items else items
        # If we fell back to raw items (because all new items were already processed),
        # enrich them with cleaned text so downstream logic can rely on 'clean_text'.
        if not new_items:
            enriched_pool: List[Dict[str, Any]] = []
            for it in base_pool:
                try:
                    cleaned, desc_html = clean_html_text(
                        it["content_html"],
                        remove_emoji=config.clean.remove_emoji,
                        remove_ads=config.clean.remove_ads,
                        ad_keywords=config.clean.ad_keywords,
                    )
                except Exception:
                    cleaned, desc_html = it.get("content_html", ""), it.get("content_html", "")
                enriched_pool.append({**it, "clean_text": cleaned, "desc_html": desc_html})
            base_pool = enriched_pool
        issue_candidates = [
            it for it in base_pool if it.get("content_source") in ("newsletter_issue", "newsletter_issue_text")
        ]
        issue_item = None
        if issue_candidates:
            # Pick the most recent by published datetime if available
            issue_item = sorted(
                issue_candidates,
                key=lambda x: parse_datetime(x.get("published", "")),
                reverse=True,
            )[0]

        if issue_item is not None:
            orig_title = issue_item.get("title") or f"{config.feed.name} — Latest"
            author = issue_item.get("author", config.site.author)
            link = issue_item.get("link", "")
            pub_dt = parse_datetime(issue_item.get("published", ""))
            # Prefer title date if available, and normalize pub_dt's date to match
            title_date = extract_date_from_title(orig_title)
            effective_date = title_date or pub_dt.date()
            try:
                pub_dt = pub_dt.replace(year=effective_date.year, month=effective_date.month, day=effective_date.day)
            except Exception:
                # Fallback: keep original pub_dt if replace fails (shouldn't happen for valid dates)
                pass
            # Episode display title: `<feed.name>: YYYY-MM-DD`
            episode_title = f"{config.feed.name}: {effective_date.isoformat()}"
            # Compute canonical hash BEFORE any LLM rewrite to avoid false diffs
            canonical_text = issue_item.get('clean_text') or issue_item.get('content_html', '') or ''
            current_hash = sha256(canonical_text)

            # TTS text keeps the newsletter's own title for natural narration, then LLM steps
            text = (
                f"{orig_title}." + (f" By {author}." if author else "") + f" {issue_item.get('clean_text', issue_item.get('content_html',''))}"
            )
            text = maybe_clean_text_with_llm(text, config)
            text = maybe_rewrite_for_audio(text, config)

            def episode_content_hash(ep: Dict[str, Any]) -> str | None:
                try:
                    if ep.get("content_hash"):
                        return str(ep["content_hash"])  # type: ignore[return-value]
                    ep_id = ep.get("id")
                    if isinstance(ep_id, str) and "::" in ep_id:
                        return ep_id.split("::")[-1]
                except Exception:
                    pass
                return None

            duplicate_ep = None
            for ep in episodes:
                try:
                    if episode_content_hash(ep) == current_hash:
                        duplicate_ep = ep
                        break
                except Exception:
                    continue

            # If content hasn't changed, avoid creating a new episode (allow re-synthesis if no audio yet)
            if duplicate_ep:
                try:
                    if duplicate_ep.get("audio_url"):
                        logger.info("No change detected; existing episode has audio. Skipping generation, refreshing feed/index")
                        force_rewrite_feed = True
                        new_desc = issue_item.get("desc_html")
                        if new_desc:
                            duplicate_ep["description_html"] = new_desc
                        issue_item = None  # prevent further generation logic
                except Exception:
                    pass

            # Gating: if an episode for this date already exists AND has audio, skip;
            # otherwise allow re-synthesis to recover from prior TTS failure.
            has_episode_with_audio = False
            for ep in episodes:
                try:
                    ep_dt = ep.get("pub_date")
                    if isinstance(ep_dt, dt.datetime) and ep_dt.date() == effective_date and ep.get("audio_url"):
                        has_episode_with_audio = True
                        break
                except Exception:
                    continue
            if issue_item is None:
                # Already handled as duplicate-no-change case above
                skip_generation = True
            else:
                # Even if an episode exists for the same date, allow regeneration
                # when content changed (we'll replace the older one below).
                skip_generation = False

            audio_rel = None
            audio_bytes_len = 0
            tts_error = None
            transcript_rel = None

            # Pre-create transcript (even if TTS fails)
            date_folder = os.path.join(
                config.output.audio_dir,
                str(effective_date.year),
                f"{effective_date.month:02d}",
            )
            out_dir = os.path.join(config.output.root_dir, date_folder)
            ensure_dirs(out_dir)
            transcript_fname = f"{effective_date.isoformat()}.txt"
            transcript_path = os.path.join(out_dir, transcript_fname)
            with open(transcript_path, "w", encoding="utf-8") as tf:
                tf.write(text)
            transcript_rel = os.path.join(date_folder, transcript_fname)

            if (not skip_generation) and config.tts.enabled:
                try:
                    if config.tts.provider.lower() == "openai":
                        mp3 = synthesize_mp3_openai(
                            text=text,
                            model=config.tts.openai_model,
                            voice=config.tts.openai_voice,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                            api_key_env=(getattr(config.llm, "api_key_env", "OPENAI_API_KEY") if getattr(config, "llm", None) else "OPENAI_API_KEY"),
                        )
                    else:
                        mp3 = synthesize_mp3(
                            text=text,
                            language_code=config.tts.language_code,
                            voice_name=config.tts.voice_name,
                            speaking_rate=config.tts.speaking_rate,
                            pitch=config.tts.pitch,
                            volume_gain_db=config.tts.volume_gain_db,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                        )
                    fname = f"{effective_date.isoformat()}.mp3"
                    path = os.path.join(out_dir, fname)
                    write_audio_with_id3(
                        path,
                        mp3,
                        title=episode_title,
                        artist=config.site.title,
                        date_str=str(effective_date),
                        link=link,
                    )
                    audio_rel = os.path.join(date_folder, fname)
                    audio_bytes_len = len(mp3)
                except Exception as e:  # noqa: BLE001
                    tts_error = str(e)
                    gha_notice("ERROR", f"TTS failed for forced compilation issue: {tts_error}")

            if not skip_generation:
                desc_html = issue_item["desc_html"]
                if tts_error:
                    desc_html += f"<p><em>TTS failed: {tts_error}</em></p>"

                episode = {
                    "id": f"issue::{effective_date.isoformat()}::{current_hash}",
                    "title": episode_title,
                    "link": link,
                    "pub_date": pub_dt,
                    "description_html": desc_html,
                    "audio_url": build_public_url(config.site.link, audio_rel) if audio_rel else None,
                    "audio_bytes": audio_bytes_len,
                    "components": [
                        {"title": issue_item.get("title"), "link": link, "source": issue_item.get("content_source")}
                    ],
                    "transcript_url": build_public_url(config.site.link, transcript_rel) if transcript_rel else None,
                    "content_hash": current_hash,
                }
                # Drop any existing episode for the same date to avoid duplicates
                episodes = [ep for ep in episodes if not (
                    isinstance(ep.get("pub_date"), dt.datetime) and ep.get("pub_date").date() == pub_dt.date()
                )]
                created_episodes.append(episode)

                # Update last_issue record
                state["last_issue"] = {"published": issue_item.get("published", ""), "guid": issue_item.get("guid", "")}

        else:
            # Fallback: compile all new items regardless of date
            if not base_pool:
                logger.info("No items available for forced compilation; exiting")
                return 0
            parts = []
            links = []
            components = []
            latest_dt = None
            for idx, it in enumerate(base_pool, start=1):
                author = it.get("author") or config.site.author
                parts.append(f"Item {idx}: {it['title']}." + (f" By {author}." if author else ""))
                parts.append(it["clean_text"]) 
                links.append(it.get("link", ""))
                components.append({
                    "title": it.get("title"),
                    "link": it.get("link"),
                    "source": it.get("content_source", "unknown"),
                })
                dtv = parse_datetime(it.get("published", ""))
                latest_dt = max(latest_dt, dtv) if latest_dt else dtv
            text = "\n\n".join(parts)
            # Canonical hash (pre-LLM) for today's compilation
            try:
                canonical_concat = "\n\n".join([it.get("clean_text", "") for it in todays])
            except Exception:
                canonical_concat = text
            today_hash = sha256(canonical_concat)
            text = maybe_clean_text_with_llm(text, config)
            text = maybe_rewrite_for_audio(text, config)

            # Episode display title for compilation fallback: `<feed.name>: YYYY-MM-DD`
            link = links[0] if links else config.site.link
            pub_dt = latest_dt or dt.datetime.now(dt.timezone.utc)
            episode_title = f"{config.feed.name}: {pub_dt.date().isoformat()}"

            # Canonical hash from cleaned text only (pre-LLM rewrite)
            try:
                canonical_concat = "\n\n".join([it.get("clean_text", "") for it in base_pool])
            except Exception:
                canonical_concat = text
            comp_hash = sha256(canonical_concat)

            # If identical content already exists with audio, skip generation
            do_generate = True
            for ep in episodes:
                try:
                    h = ep.get("content_hash")
                    if not h:
                        ep_id = ep.get("id")
                        if isinstance(ep_id, str) and "::" in ep_id:
                            h = ep_id.split("::")[-1]
                    if h == comp_hash and ep.get("audio_url"):
                        logger.info("Compilation (forced) unchanged; skipping generation and refreshing feed/index")
                        do_generate = False
                        force_rewrite_feed = True
                        # Refresh description of existing episode
                        try:
                            new_desc = "".join(
                                f"<p><strong>{idx}. {it['title']}</strong><br/>{it['desc_html']}</p>" for idx, it in enumerate(base_pool, start=1)
                            )
                            if new_desc:
                                ep["description_html"] = new_desc
                        except Exception:
                            pass
                        break
                except Exception:
                    continue

            audio_rel = None
            audio_bytes_len = 0
            tts_error = None
            transcript_rel = None

            # Pre-create transcript (even if TTS fails)
            date_folder = os.path.join(
                config.output.audio_dir,
                str(pub_dt.year),
                f"{pub_dt.month:02d}",
            )
            out_dir = os.path.join(config.output.root_dir, date_folder)
            ensure_dirs(out_dir)
            transcript_fname = f"{pub_dt.date().isoformat()}.txt"
            transcript_path = os.path.join(out_dir, transcript_fname)
            if do_generate:
                with open(transcript_path, "w", encoding="utf-8") as tf:
                    tf.write(text)
                transcript_rel = os.path.join(date_folder, transcript_fname)

            if config.tts.enabled and do_generate:
                try:
                    if config.tts.provider.lower() == "openai":
                        mp3 = synthesize_mp3_openai(
                            text=text,
                            model=config.tts.openai_model,
                            voice=config.tts.openai_voice,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                            api_key_env=(getattr(config.llm, "api_key_env", "OPENAI_API_KEY") if getattr(config, "llm", None) else "OPENAI_API_KEY"),
                        )
                    else:
                        mp3 = synthesize_mp3(
                            text=text,
                            language_code=config.tts.language_code,
                            voice_name=config.tts.voice_name,
                            speaking_rate=config.tts.speaking_rate,
                            pitch=config.tts.pitch,
                            volume_gain_db=config.tts.volume_gain_db,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                        )
                    fname = f"{pub_dt.date().isoformat()}.mp3"
                    path = os.path.join(out_dir, fname)
                    write_audio_with_id3(
                        path,
                        mp3,
                        title=episode_title,
                        artist=config.site.title,
                        date_str=str(pub_dt.date()),
                        link=link,
                    )
                    audio_rel = os.path.join(date_folder, fname)
                    audio_bytes_len = len(mp3)
                except Exception as e:  # noqa: BLE001
                    tts_error = str(e)
                    gha_notice("ERROR", f"TTS failed for forced compilation: {tts_error}")

            if do_generate:
                desc_html = "".join(
                    f"<p><strong>{idx}. {it['title']}</strong><br/>{it['desc_html']}</p>" for idx, it in enumerate(base_pool, start=1)
                )
                comp_list = "".join(
                    f"<li><a href=\"{c.get('link','')}\">{c.get('title','')}</a> — source: {c.get('source','unknown')}</li>"
                    for c in components
                )
                desc_html += f"<h4>Included items</h4><ul>{comp_list}</ul>"
                if tts_error:
                    desc_html += f"<p><em>TTS failed: {tts_error}</em></p>"

                episode = {
                    "id": f"forced::{pub_dt.date().isoformat()}::{comp_hash}",
                    "title": episode_title,
                    "link": link,
                    "pub_date": pub_dt,
                    "description_html": desc_html,
                    "audio_url": build_public_url(config.site.link, audio_rel) if audio_rel else None,
                    "audio_bytes": audio_bytes_len,
                    "components": components,
                    "transcript_url": build_public_url(config.site.link, transcript_rel) if transcript_rel else None,
                    "content_hash": comp_hash,
                }
                created_episodes.append(episode)

    else:  # compilation
        # Group items for today only
        todays = [it for it in new_items if parse_datetime(it.get("published", "")).date() == today]
        if todays:
            # Build compiled text
            parts = []
            links = []
            components = []
            for idx, it in enumerate(todays, start=1):
                author = it.get("author") or config.site.author
                parts.append(f"Item {idx}: {it['title']}." + (f" By {author}." if author else ""))
                parts.append(it["clean_text"]) 
                links.append(it.get("link", ""))
                components.append({
                    "title": it.get("title"),
                    "link": it.get("link"),
                    "source": it.get("content_source", "unknown"),
                })
            text = "\n\n".join(parts)

            # Episode display title for standard compilation: `<feed.name>: YYYY-MM-DD`
            episode_title = f"{config.feed.name}: {today.isoformat()}"
            link = links[0] if links else config.site.link
            pub_dt = dt.datetime.combine(today, dt.time(9, 0, tzinfo=dt.timezone.utc))

            # If identical content already exists with audio, skip generation
            do_generate = True
            for ep in episodes:
                try:
                    h = ep.get("content_hash")
                    if not h:
                        ep_id = ep.get("id")
                        if isinstance(ep_id, str) and "::" in ep_id:
                            h = ep_id.split("::")[-1]
                    if h == today_hash and ep.get("audio_url"):
                        logger.info("Today's compilation unchanged; skipping generation and refreshing feed/index")
                        do_generate = False
                        force_rewrite_feed = True
                        # Refresh description
                        try:
                            new_desc = "".join(
                                f"<p><strong>{idx}. {it['title']}</strong><br/>{it['desc_html']}</p>" for idx, it in enumerate(todays, start=1)
                            )
                            if new_desc:
                                ep["description_html"] = new_desc
                        except Exception:
                            pass
                        break
                except Exception:
                    continue

            audio_rel = None
            audio_bytes_len = 0
            tts_error = None
            transcript_rel = None

            # Pre-create transcript (even if TTS fails)
            date_folder = os.path.join(
                config.output.audio_dir,
                str(pub_dt.year),
                f"{pub_dt.month:02d}",
            )
            out_dir = os.path.join(config.output.root_dir, date_folder)
            ensure_dirs(out_dir)
            transcript_fname = f"{today.isoformat()}.txt"
            transcript_path = os.path.join(out_dir, transcript_fname)
            if do_generate:
                with open(transcript_path, "w", encoding="utf-8") as tf:
                    tf.write(text)
                transcript_rel = os.path.join(date_folder, transcript_fname)

            if config.tts.enabled and do_generate:
                try:
                    if config.tts.provider.lower() == "openai":
                        mp3 = synthesize_mp3_openai(
                            text=text,
                            model=config.tts.openai_model,
                            voice=config.tts.openai_voice,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                            api_key_env=(getattr(config.llm, "api_key_env", "OPENAI_API_KEY") if getattr(config, "llm", None) else "OPENAI_API_KEY"),
                        )
                    else:
                        mp3 = synthesize_mp3(
                            text=text,
                            language_code=config.tts.language_code,
                            voice_name=config.tts.voice_name,
                            speaking_rate=config.tts.speaking_rate,
                            pitch=config.tts.pitch,
                            volume_gain_db=config.tts.volume_gain_db,
                            max_chars_per_chunk=config.tts.max_chars_per_chunk,
                            max_retries=config.tts.max_retries,
                            initial_retry_delay=config.tts.initial_retry_delay,
                        )
                    fname = f"{today.isoformat()}.mp3"
                    path = os.path.join(out_dir, fname)
                    write_audio_with_id3(
                        path,
                        mp3,
                        title=episode_title,
                        artist=config.site.title,
                        date_str=str(pub_dt.date()),
                        link=link,
                    )
                    audio_rel = os.path.join(date_folder, fname)
                    audio_bytes_len = len(mp3)
                except Exception as e:  # noqa: BLE001
                    tts_error = str(e)
                    gha_notice("ERROR", f"TTS failed for compiled episode: {tts_error}")

            if do_generate:
                desc_html = "".join(
                    f"<p><strong>{idx}. {it['title']}</strong><br/>{it['desc_html']}</p>" for idx, it in enumerate(todays, start=1)
                )
                # Append component summary with sources
                comp_list = "".join(
                    f"<li><a href=\"{c.get('link','')}\">{c.get('title','')}</a> — source: {c.get('source','unknown')}</li>"
                    for c in components
                )
                desc_html += f"<h4>Included items</h4><ul>{comp_list}</ul>"
                if tts_error:
                    desc_html += f"<p><em>TTS failed: {tts_error}</em></p>"

                episode = {
                    "id": f"compilation::{today.isoformat()}::{today_hash}",
                    "title": episode_title,
                    "link": link,
                    "pub_date": pub_dt,
                    "description_html": desc_html,
                    "audio_url": build_public_url(config.site.link, audio_rel) if audio_rel else None,
                    "audio_bytes": audio_bytes_len,
                    "components": components,
                    "transcript_url": build_public_url(config.site.link, transcript_rel) if transcript_rel else None,
                    "content_hash": today_hash,
                }
                created_episodes.append(episode)

    if not created_episodes and not force_rewrite_feed:
        logger.info("Nothing to publish after filtering; exiting")
        return 0

    # Update state: mark processed keys
    for it in new_items:
        processed[it["key"]] = dt.datetime.now(dt.timezone.utc).isoformat()

    # Append episodes and sort by date desc; keep last 200
    if created_episodes:
        episodes.extend(created_episodes)
        episodes = sorted(episodes, key=lambda x: x.get("pub_date", dt.datetime.now(dt.timezone.utc)), reverse=True)[:200]

    state["processed"] = processed
    state["episodes"] = episodes

    # Write RSS and index
    feed_path = os.path.join(config.output.root_dir, config.output.feed_filename)
    feed_url = build_public_url(config.site.link, config.output.feed_filename)
    xml = render_rss(
        site={
            "title": config.site.title,
            "link": config.site.link,
            "description": config.site.description,
            "language": config.site.language,
            "author": config.site.author,
            "owner_name": config.site.owner_name,
            "owner_email": config.site.owner_email,
            "image_url": config.site.image_url,
        },
        items=episodes,
        feed_url=feed_url,
    )
    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(xml)

    index_path = os.path.join(config.output.root_dir, config.output.index_filename)
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(render_index_html(config.site.title, feed_url))

    # Save state
    save_state(state)

    logger.info("Publish complete", extra={"episodes": len(created_episodes)})
    return 0


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.logging.level)
    # Best-effort config validation with warnings
    try:
        for msg in validate_config(cfg):
            logging.getLogger(__name__).warning("Config warning: %s", msg)
    except Exception:
        pass
    try:
        code = run(cfg)
        raise SystemExit(code)
    except Exception as e:  # noqa: BLE001
        gha_notice("ERROR", f"Run failed: {e}")
        logger.exception("Fatal error during run")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
