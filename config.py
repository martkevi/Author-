"""Validated environment configuration for Author Telegram Book Studio.

All secrets must be supplied by the deployment environment (Railway Variables),
never committed to the repository. Existing environment variable names are kept
compatible with the current main.py, engine.py and exporter.py.
"""

from __future__ import annotations

import os
from pathlib import Path


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer; received {raw!r}.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _first_nonempty(*values: str | None) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


TELEGRAM_BOT_TOKEN = _first_nonempty(os.getenv("TELEGRAM_BOT_TOKEN"))
OPENROUTER_API_KEY = _first_nonempty(os.getenv("OPENROUTER_API_KEY"))
OPENROUTER_MODEL = _first_nonempty(
    os.getenv("OPENROUTER_MODEL"),
    "openrouter/auto",
)
OPENROUTER_SITE_URL = _first_nonempty(os.getenv("OPENROUTER_SITE_URL"))
OPENROUTER_APP_NAME = _first_nonempty(
    os.getenv("OPENROUTER_APP_NAME"),
    "Author Telegram Book Studio",
)
OPENROUTER_BASE_URL = _first_nonempty(
    os.getenv("OPENROUTER_BASE_URL"),
    "https://openrouter.ai/api/v1",
).rstrip("/")

# Railway exposes RAILWAY_VOLUME_MOUNT_PATH only when a volume is attached.
# Use that volume by default when available. Otherwise preserve the project's
# existing ./data behavior. DATA_DIR can explicitly override either location.
_default_data_dir = _first_nonempty(
    os.getenv("DATA_DIR"),
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH"),
    "./data",
)
DATA_DIR = Path(_default_data_dir).expanduser()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(DATA_DIR / "book_agent.sqlite3"))).expanduser()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(DATA_DIR / "exports"))).expanduser()

MAX_CHAPTERS = _int_env("MAX_CHAPTERS", 40, 1, 100)
MAX_CHAPTER_WORDS = _int_env("MAX_CHAPTER_WORDS", 2200, 400, 5000)
REQUEST_TIMEOUT_SECONDS = _int_env("REQUEST_TIMEOUT_SECONDS", 180, 30, 300)
OPENROUTER_CONNECT_TIMEOUT_SECONDS = _int_env(
    "OPENROUTER_CONNECT_TIMEOUT_SECONDS", 20, 5, 90
)
OPENROUTER_MAX_RETRIES = _int_env("OPENROUTER_MAX_RETRIES", 1, 0, 3)

# These aliases help diagnostics/tools while preserving existing names.
DATABASE_PATH = DATABASE_PATH.resolve()
DATA_DIR = DATA_DIR.resolve()
OUTPUT_DIR = OUTPUT_DIR.resolve()


def validate_required_settings() -> None:
    """Fail early with a clear message when secrets are absent or malformed."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not OPENROUTER_API_KEY:
        missing.append("OPENROUTER_API_KEY")
    if missing:
        raise RuntimeError(
            "Missing required Railway variable(s): " + ", ".join(missing)
        )
    if "://" not in OPENROUTER_BASE_URL:
        raise RuntimeError("OPENROUTER_BASE_URL must be a valid HTTPS URL.")
