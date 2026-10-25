"""Environment-backed configuration for the Telegram book-writing agent."""

import os
from pathlib import Path


def _int_env(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.getenv(name, str(default))

    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be an integer"
        ) from exc

    if not minimum <= value <= maximum:
        raise ValueError(
            f"{name} must be between {minimum} and {maximum}"
        )

    return value


TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", ""
).strip()

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY", ""
).strip()

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "openrouter/auto"
).strip()

OPENROUTER_SITE_URL = os.getenv(
    "OPENROUTER_SITE_URL", ""
).strip()

OPENROUTER_APP_NAME = os.getenv(
    "OPENROUTER_APP_NAME", "Telegram Book Studio"
).strip()

DATA_DIR = Path(
    os.getenv("DATA_DIR", "./data")
).expanduser()

DATABASE_PATH = DATA_DIR / "book_agent.sqlite3"

OUTPUT_DIR = DATA_DIR / "exports"

MAX_CHAPTERS = _int_env(
    "MAX_CHAPTERS", 40, 1, 100
)

MAX_CHAPTER_WORDS = _int_env(
    "MAX_CHAPTER_WORDS", 2200, 400, 5000
)

REQUEST_TIMEOUT_SECONDS = _int_env(
    "REQUEST_TIMEOUT_SECONDS", 180, 30, 300
)


def validate_required_settings() -> None:
    missing = []

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not OPENROUTER_API_KEY:
        missing.append("OPENROUTER_API_KEY")

    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
        )
