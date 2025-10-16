from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class SiteConfig:
    title: str
    link: str
    description: str
    language: str
    image_url: str
    author: str
    owner_name: str
    owner_email: str


@dataclass
class FeedConfig:
    name: str
    url: str
    fetch_original: bool = False


@dataclass
class OutputConfig:
    root_dir: str
    audio_dir: str
    feed_filename: str
    index_filename: str


@dataclass
class TTSConfig:
    enabled: bool
    language_code: str
    voice_name: str
    speaking_rate: float
    pitch: float
    volume_gain_db: float
    audio_encoding: str
    max_chars_per_chunk: int
    max_retries: int
    initial_retry_delay: float


@dataclass
class CleanConfig:
    remove_emoji: bool
    strip_html: bool
    remove_ads: bool
    ad_keywords: List[str]


@dataclass
class LoggingConfig:
    level: str


@dataclass
class LLMConfig:
    enabled: bool
    provider: str
    model: str
    api_key_env: str


@dataclass
class AppConfig:
    site: SiteConfig
    feed: FeedConfig
    output: OutputConfig
    mode: str
    tts: TTSConfig
    clean: CleanConfig
    logging: LoggingConfig
    # Optional LLM cleaning
    llm: 'LLMConfig' | None


def load_config(path: str = "config.yaml") -> AppConfig:
    with open(path, "r", encoding="utf-8") as f:
        data: Dict[str, Any] = yaml.safe_load(f)

    site = data.get("site", {})
    feed = data.get("feed", {})
    output = data.get("output", {})
    tts = data.get("tts", {})
    clean = data.get("clean", {})
    llm_cfg = data.get("llm", {}) or {}
    logging_cfg = data.get("logging", {})

    return AppConfig(
        site=SiteConfig(
            title=site.get("title", "Newsletter Podcast"),
            link=site.get("link", "http://localhost/"),
            description=site.get("description", "Auto podcast feed"),
            language=site.get("language", "en-us"),
            image_url=site.get("image_url", ""),
            author=site.get("author", ""),
            owner_name=site.get("owner", {}).get("name", ""),
            owner_email=site.get("owner", {}).get("email", ""),
        ),
        feed=FeedConfig(
            name=feed.get("name", "Axios"),
            url=feed.get("url", "https://www.axios.com/feeds/feed.rss"),
            fetch_original=bool(feed.get("fetch_original", False)),
        ),
        output=OutputConfig(
            root_dir=output.get("root_dir", "docs"),
            audio_dir=output.get("audio_dir", "audio"),
            feed_filename=output.get("feed_filename", "feed.xml"),
            index_filename=output.get("index_filename", "index.html"),
        ),
        mode=data.get("mode", "compilation"),
        tts=TTSConfig(
            enabled=bool(tts.get("enabled", True)),
            language_code=tts.get("language_code", "en-US"),
            voice_name=tts.get("voice_name", "en-US-Standard-C"),
            speaking_rate=float(tts.get("speaking_rate", 1.0)),
            pitch=float(tts.get("pitch", 0.0)),
            volume_gain_db=float(tts.get("volume_gain_db", 0.0)),
            audio_encoding=tts.get("audio_encoding", "MP3"),
            max_chars_per_chunk=int(tts.get("max_chars_per_chunk", 4500)),
            max_retries=int(tts.get("max_retries", 3)),
            initial_retry_delay=float(tts.get("initial_retry_delay", 2.0)),
        ),
        clean=CleanConfig(
            remove_emoji=bool(clean.get("remove_emoji", True)),
            strip_html=bool(clean.get("strip_html", True)),
            remove_ads=bool(clean.get("remove_ads", True)),
            ad_keywords=list(clean.get("ad_keywords", [])),
        ),
        logging=LoggingConfig(level=logging_cfg.get("level", "INFO")),
        llm=LLMConfig(
            enabled=bool(llm_cfg.get("enabled", False)),
            provider=llm_cfg.get("provider", "openai"),
            model=llm_cfg.get("model", "gpt-4o-mini"),
            api_key_env=llm_cfg.get("api_key_env", "OPENAI_API_KEY"),
        ),
    )
