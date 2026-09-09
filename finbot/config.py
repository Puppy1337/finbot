"""Загрузка настроек из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Минимальный парсер .env (без сторонних зависимостей)."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Settings:
    telegram_token: str
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    base_currency: str = "USD"
    timezone: str = "Europe/Kyiv"
    db_path: str = "data/finbot.db"
    allowed_user_ids: set[int] = field(default_factory=set)
    # Распознавание голоса: "openai" (OpenAI/Groq-совместимый API) или "local" (faster-whisper)
    stt_provider: str = "openai"
    stt_api_key: str = ""
    stt_base_url: str = "https://api.openai.com/v1"
    stt_model: str = "whisper-1"
    weekly_report_day: int = 0  # 0 = понедельник
    weekly_report_time: str = "09:00"

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(Path(".env"))
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not token:
            raise SystemExit("Не задан TELEGRAM_BOT_TOKEN (см. .env.example)")
        if not api_key:
            raise SystemExit("Не задан ANTHROPIC_API_KEY (см. .env.example)")
        ids_raw = os.environ.get("ALLOWED_USER_IDS", "").strip()
        ids = {int(x) for x in ids_raw.replace(";", ",").split(",") if x.strip().isdigit()}
        stt_key = os.environ.get("STT_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
        return cls(
            telegram_token=token,
            anthropic_api_key=api_key,
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", cls.anthropic_model).strip(),
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", cls.anthropic_base_url).rstrip("/"),
            base_currency=os.environ.get("BASE_CURRENCY", cls.base_currency).strip().upper(),
            timezone=os.environ.get("TIMEZONE", cls.timezone).strip(),
            db_path=os.environ.get("DB_PATH", cls.db_path).strip(),
            allowed_user_ids=ids,
            stt_provider=os.environ.get("STT_PROVIDER", cls.stt_provider).strip().lower(),
            stt_api_key=stt_key,
            stt_base_url=os.environ.get("STT_BASE_URL", cls.stt_base_url).rstrip("/"),
            stt_model=os.environ.get("STT_MODEL", cls.stt_model).strip(),
            weekly_report_day=int(os.environ.get("WEEKLY_REPORT_DAY", cls.weekly_report_day)),
            weekly_report_time=os.environ.get("WEEKLY_REPORT_TIME", cls.weekly_report_time).strip(),
        )
