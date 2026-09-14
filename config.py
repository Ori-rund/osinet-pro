"""הגדרות — הכל מגיע ממשתני סביבה. שום סוד לא נכנס לקוד."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"חסר משתנה סביבה {name}. העתק את .env.example ל-.env ומלא אותו."
        )
    return value


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_key: str

    telegram_api_id: int | None
    telegram_api_hash: str | None
    telegram_session: str | None

    rss_interval_sec: int
    dedup_window_hours: int
    max_items_per_fetch: int
    backfill_limit: int
    anthropic_api_key: str | None
    filter_model: str
    dry_run: bool
    log_level: str

    @classmethod
    def load(cls) -> "Settings":
        api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        return cls(
            supabase_url=_require("SUPABASE_URL"),
            # service_role — עוקף RLS. לעולם לא בצד לקוח.
            supabase_service_key=_require("SUPABASE_SERVICE_KEY"),
            telegram_api_id=int(api_id) if api_id else None,
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip() or None,
            telegram_session=os.getenv("TELEGRAM_SESSION", "").strip() or None,
            rss_interval_sec=int(os.getenv("RSS_INTERVAL_SEC", "300")),
            dedup_window_hours=int(os.getenv("DEDUP_WINDOW_HOURS", "6")),
            max_items_per_fetch=int(os.getenv("MAX_ITEMS_PER_FETCH", "25")),
            backfill_limit=int(os.getenv("BACKFILL_LIMIT", "10")),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip() or None,
            filter_model=os.getenv("FILTER_MODEL", "claude-haiku-4-5-20251001"),
            dry_run=_flag("DRY_RUN"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def telegram_ready(self) -> bool:
        return all([self.telegram_api_id, self.telegram_api_hash, self.telegram_session])

    @property
    def ai_filter_ready(self) -> bool:
        return bool(self.anthropic_api_key)


settings = Settings.load()
