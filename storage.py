"""SQLite persistence for Author Telegram Book Studio.

Public functions and row shapes are intentionally compatible with the current
main.py and exporter.py. Existing databases are preserved: initialization only
creates missing tables/indexes and does not drop or recreate user data.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(config.DATABASE_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def initialize_storage() -> None:
    """Create the database schema safely; safe to call at every startup."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                genre TEXT NOT NULL DEFAULT 'General Fiction',
                audience TEXT NOT NULL DEFAULT 'Adult readers',
                premise TEXT NOT NULL DEFAULT '',
                outline_json TEXT NOT NULL DEFAULT '{}',
                story_bible_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'planning',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_books_user
                ON books(user_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'planned',
                word_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(book_id, chapter_number),
                FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_chapters_book
                ON chapters(book_id, chapter_number);
            CREATE TABLE IF NOT EXISTS chapter_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                revision_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_revisions_chapter
                ON chapter_revisions(chapter_id, created_at DESC);
            """
        )
        # Basic forward-compatible migration for older versions of this schema.
        # ALTER TABLE is performed only when a column is missing.
        _ensure_column(connection, "books", "outline_json", "TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(connection, "books", "story_bible_json", "TEXT NOT NULL DEFAULT '{}'" )
        _ensure_column(connection, "books", "status", "TEXT NOT NULL DEFAULT 'planning'")
        _ensure_column(connection, "chapters", "summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(connection, "chapters", "status", "TEXT NOT NULL DEFAULT 'planned'")
        _ensure_column(connection, "chapters", "word_count", "INTEGER NOT NULL DEFAULT 0")


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def create_book(
    user_id: int,
    title: str,
    genre: str = "General Fiction",
    audience: str = "Adult readers",
    premise: str = "",
) -> dict[str, Any]:
    title = str(title or "").strip()
    if not title:
        raise ValueError("The book title cannot be empty.")
    if not isinstance(user_id, int):
        raise ValueError("A valid Telegram user ID is required.")
    timestamp = _now()
    with _connect() as connection:
        cursor = connection.execute(
            """INSERT INTO books
            (user_id, title, genre, audience, premise, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, title, str(genre or "General Fiction").strip(),
             str(audience or "Adult readers").strip(), str(premise or "").strip(),
             timestamp, timestamp),
        )
        row = connection.execute("SELECT * FROM books WHERE id = ?", (cursor.lastrowid,)).fetchone()
        result = _row_to_dict(row)
        assert result is not None
        return result


def get_book(book_id: int, user_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        return _row_to_dict(connection.execute(
            "SELECT * FROM books WHERE id = ? AND user_id = ?", (book_id, user_id)
        ).fetchone())


def list_books(user_id: int) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM books WHERE user_id = ? ORDER BY updated_at DESC, id DESC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def update_book_status(book_id: int, user_id: int, status: str) -> bool:
    allowed = {"planning", "outlining", "writing", "editing", "completed"}
    if status not in allowed:
        raise ValueError(f"Invalid book status: {status}")
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE books SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (status, _now(), book_id, user_id),
        )
        return cursor.rowcount > 0


def save_outline(book_id: int, user_id: int, outline: dict[str, Any]) -> bool:
    if not isinstance(outline, dict):
        raise ValueError("Outline must be a JSON object.")
    payload = json.dumps(outline, ensure_ascii=False)
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE books SET outline_json = ?, status = 'outlining', updated_at = ? WHERE id = ? AND user_id = ?",
            (payload, _now(), book_id, user_id),
        )
        return cursor.rowcount > 0


def get_outline(book_id: int, user_id: int) -> dict[str, Any] | None:
    book = get_book(book_id, user_id)
    if book is None:
        return None
    try:
        value = json.loads(book.get("outline_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def save_story_bible(book_id: int, user_id: int, story_bible: dict[str, Any]) -> bool:
    if not isinstance(story_bible, dict):
        raise ValueError("Story bible must be a JSON object.")
    payload = json.dumps(story_bible, ensure_ascii=False)
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE books SET story_bible_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (payload, _now(), book_id, user_id),
        )
        return cursor.rowcount > 0


def get_story_bible(book_id: int, user_id: int) -> dict[str, Any] | None:
    book = get_book(book_id, user_id)
    if book is None:
        return None
    try:
        value = json.loads(book.get("story_bible_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def save_chapter(
    book_id: int,
    user_id: int,
    chapter_number: int,
    title: str,
    content: str,
    summary: str = "",
    status: str = "draft",
    revision_note: str = "",
) -> dict[str, Any]:
    if chapter_number < 1:
        raise ValueError("Chapter number must be at least 1.")
    content = str(content or "")
    if not content.strip():
        raise ValueError("Chapter content cannot be empty.")
    allowed_statuses = {"planned", "draft", "reviewed", "approved"}
    if status not in allowed_statuses:
        raise ValueError(f"Invalid chapter status: {status}")
    timestamp = _now()
    word_count = len(content.split())
    title = str(title or f"Chapter {chapter_number}").strip() or f"Chapter {chapter_number}"
    summary = str(summary or "")
    with _connect() as connection:
        book = connection.execute(
            "SELECT id FROM books WHERE id = ? AND user_id = ?", (book_id, user_id)
        ).fetchone()
        if book is None:
            raise ValueError("Book not found or access denied.")
        existing = connection.execute(
            "SELECT id, content FROM chapters WHERE book_id = ? AND chapter_number = ?",
            (book_id, chapter_number),
        ).fetchone()
        if existing:
            if existing["content"] != content:
                connection.execute(
                    "INSERT INTO chapter_revisions (chapter_id, content, revision_note, created_at) VALUES (?, ?, ?, ?)",
                    (existing["id"], existing["content"], str(revision_note or "Previous version"), timestamp),
                )
            connection.execute(
                """UPDATE chapters SET title = ?, content = ?, summary = ?, status = ?,
                   word_count = ?, updated_at = ? WHERE id = ?""",
                (title, content, summary, status, word_count, timestamp, existing["id"]),
            )
        else:
            connection.execute(
                """INSERT INTO chapters
                   (book_id, chapter_number, title, content, summary, status, word_count, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (book_id, chapter_number, title, content, summary, status, word_count, timestamp, timestamp),
            )
        connection.execute("UPDATE books SET updated_at = ? WHERE id = ? AND user_id = ?", (timestamp, book_id, user_id))
        row = connection.execute(
            "SELECT * FROM chapters WHERE book_id = ? AND chapter_number = ?",
            (book_id, chapter_number),
        ).fetchone()
        result = _row_to_dict(row)
        assert result is not None
        return result


def get_chapter(book_id: int, user_id: int, chapter_number: int) -> dict[str, Any] | None:
    with _connect() as connection:
        return _row_to_dict(connection.execute(
            """SELECT c.* FROM chapters AS c JOIN books AS b ON b.id = c.book_id
               WHERE c.book_id = ? AND b.user_id = ? AND c.chapter_number = ?""",
            (book_id, user_id, chapter_number),
        ).fetchone())


def list_chapters(book_id: int, user_id: int) -> list[dict[str, Any]] | None:
    if get_book(book_id, user_id) is None:
        return None
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM chapters WHERE book_id = ? ORDER BY chapter_number ASC", (book_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_chapter_revisions(book_id: int, user_id: int, chapter_number: int) -> list[dict[str, Any]] | None:
    chapter = get_chapter(book_id, user_id, chapter_number)
    if chapter is None:
        return None
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM chapter_revisions WHERE chapter_id = ? ORDER BY id DESC",
            (chapter["id"],),
        ).fetchall()
        return [dict(row) for row in rows]


def get_book_statistics(book_id: int, user_id: int) -> dict[str, Any] | None:
    book = get_book(book_id, user_id)
    if book is None:
        return None
    with _connect() as connection:
        row = connection.execute(
            """SELECT COUNT(*) AS chapter_count,
                      COALESCE(SUM(word_count), 0) AS total_words,
                      COALESCE(SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END), 0) AS approved_chapters
               FROM chapters WHERE book_id = ?""",
            (book_id,),
        ).fetchone()
    return {
        "book_id": book_id,
        "title": book["title"],
        "status": book["status"],
        "chapter_count": int(row["chapter_count"]),
        "total_words": int(row["total_words"]),
        "approved_chapters": int(row["approved_chapters"]),
    }
