"""Настройки из переменных окружения (см. .env.example)."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "sqlite+aiosqlite:///./bg-agent.db"))
    # Без REDIS_URL проверки выполняются прямо в процессе веб-сервера (удобно локально).
    redis_url: str = field(default_factory=lambda: os.environ.get("REDIS_URL", ""))
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", "dev-secret"))
    # Cookie сессии только по HTTPS; на сервере за Caddy включить.
    cookie_secure: bool = field(default_factory=lambda: os.environ.get("COOKIE_SECURE") == "1")
    # Общий секрет, с которым Telegram-бот ходит в API от имени своих пользователей.
    bot_api_token: str = field(default_factory=lambda: os.environ.get("BOT_API_TOKEN", ""))
    captcha_timeout: float = field(default_factory=lambda: float(os.environ.get("CAPTCHA_TIMEOUT", "180")))
    # Токен сервиса отдачи документов ЕИС (zakupki.gov.ru/pmd/auth/welcome) для подбора закупок.
    eis_token: str = field(default_factory=lambda: os.environ.get("EIS_TOKEN", ""))
    # Подбор по смыслу: локальная модель векторов (tenders/semantic.py). Пусто — выключено.
    embed_model: str = field(default_factory=lambda: os.environ.get("EMBED_MODEL", ""))
    embed_threshold: float = field(default_factory=lambda: float(os.environ.get("EMBED_THRESHOLD", "0.6")))
    embed_cache_dir: str = field(default_factory=lambda: os.environ.get("EMBED_CACHE_DIR", ""))
    # Email сотрудников-администраторов через запятую: при входе им открывается раздел CRM.
    # Менеджеров назначает администратор (или python -m app.roles email manager).
    admin_emails: tuple[str, ...] = field(default_factory=lambda: tuple(
        e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()))


settings = Settings()
