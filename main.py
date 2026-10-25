"""
Telegram Book Studio
=====================

Telegram interface for an AI-powered book-writing agent.

Features:
- Create and manage book projects.
- Generate detailed book outlines.
- Write chapters using saved story context.
- Review and revise chapters.
- Preserve chapter revision history.
- Export manuscripts as TXT, DOCX, EPUB, and PDF.
- Keep each user's books isolated.

Start:
    python main.py
"""

import asyncio
import logging
import re
from pathlib import Path

from telegram import Update, InputFile
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import config
import storage
import exporter

from engine import (
    WritingEngine,
    WritingEngineError,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("telegram_book_studio")

# Avoid unnecessarily verbose HTTP logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


# ============================================================
# CONSTANTS
# ============================================================

MAX_TELEGRAM_MESSAGE = 3900

SUPPORTED_EXPORT_FORMATS = {
    "txt",
    "docx",
    "epub",
    "pdf",
}


# ============================================================
# GENERAL HELPERS
# ============================================================

def get_user_id(update: Update) -> int:
    """Return the Telegram ID of the requesting user."""
    if update.effective_user is None:
        raise ValueError("Could not identify the Telegram user.")

    return update.effective_user.id


def get_engine() -> WritingEngine:
    """Create an AI engine for the current operation."""
    return WritingEngine()


def parse_pipe_arguments(
    text: str,
    expected_parts: int,
) -> list[str]:
    """
    Parse arguments separated by vertical bars.

    Example:
        Title | Genre | Audience | Premise
    """
    parts = [
        part.strip()
        for part in text.split("|", expected_parts - 1)
    ]

    if len(parts) != expected_parts:
        raise ValueError(
            f"Expected {expected_parts} fields separated by |."
        )

    if any(not part for part in parts):
        raise ValueError(
            "All required fields must contain a value."
        )

    return parts


def get_book_or_raise(
    book_id: int,
    user_id: int,
) -> dict:
    """Retrieve a book belonging to the requesting user."""
    book = storage.get_book(book_id, user_id)

    if book is None:
        raise ValueError(
            "Book not found. Use /books to see your projects."
        )

    return book


def get_outline_or_raise(
    book_id: int,
    user_id: int,
) -> dict:
    """Retrieve a saved outline."""
    outline = storage.get_outline(book_id, user_id)

    if not outline or not outline.get("chapters"):
        raise ValueError(
            "This book has no saved outline. "
            f"Generate one with /outline {book_id} | 12"
        )

    return outline


def get_chapter_or_raise(
    book_id: int,
    user_id: int,
    chapter_number: int,
) -> dict:
    """Retrieve a saved chapter."""
    chapter = storage.get_chapter(
        book_id,
        user_id,
        chapter_number,
    )

    if chapter is None:
        raise ValueError(
            "Chapter not found. Check the book ID and chapter number."
        )

    return chapter


def truncate_text(
    text: str,
    limit: int = MAX_TELEGRAM_MESSAGE,
) -> str:
    """Keep Telegram messages below a safe length."""
    if len(text) <= limit:
        return text

    return text[:limit - 100] + "\n\n[Message shortened.]"


def split_message(
    text: str,
    limit: int = MAX_TELEGRAM_MESSAGE,
) -> list[str]:
    """Split long text into Telegram-safe messages."""
    chunks = []

    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)

        if split_at < limit // 2:
            split_at = limit

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()

    if text:
        chunks.append(text)

    return chunks


async def send_long_message(
    update: Update,
    text: str,
) -> None:
    """Send long content as multiple Telegram messages."""
    if update.effective_message is None:
        return

    for chunk in split_message(text):
        await update.effective_message.reply_text(chunk)


async def send_typing_action(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
) -> None:
    """Show that the bot is processing a request."""
    try:
        await context.bot.send_chat_action(
            chat_id=chat_id,
            action=ChatAction.TYPING,
        )
    except Exception:
        logger.debug(
            "Could not send typing indicator.",
            exc_info=True,
        )


def parse_positive_integer(
    value: str,
    field_name: str,
) -> int:
    """Parse a positive integer."""
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be a whole number."
        ) from exc

    if result < 1:
        raise ValueError(
            f"{field_name} must be at least 1."
        )

    return result


# ============================================================
# START AND HELP
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Welcome the user and introduce the available commands."""
    message = """
📚 TELEGRAM BOOK STUDIO

Your AI-assisted book development workspace.

I can help you plan books, write chapters, review manuscripts,
revise weak writing, and export your work.

GET STARTED

1. Create a book:
/newbook Title | Genre | Audience | Premise

Example:
/newbook The Last Kingdom | Fantasy | Adult readers | A disgraced healer discovers that the king is hiding a dangerous secret.

2. List your books:
/books

3. Generate an outline:
/outline BOOK_ID | CHAPTER_COUNT

Example:
/outline 1 | 12

4. Write a chapter:
/write BOOK_ID CHAPTER_NUMBER

Example:
/write 1 1

5. Review a chapter:
/review BOOK_ID CHAPTER_NUMBER

6. Revise a chapter:
/revise BOOK_ID CHAPTER_NUMBER | Your instructions

7. View a saved chapter:
/chapter BOOK_ID CHAPTER_NUMBER

8. View book statistics:
/status BOOK_ID

9. Export a manuscript:
/export BOOK_ID docx

Available formats: txt, docx, epub, pdf

10. View this help:
/help

IMPORTANT

Your book content is saved in a database. For persistence across
Railway deployments, configure a persistent volume.

Always review your manuscript before publishing it.
"""

    if update.effective_message:
        await update.effective_message.reply_text(
            message.strip()
        )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Display the command reference."""
    await start_command(update, context)


# ============================================================
# CREATE AND LIST BOOKS
# ============================================================

async def newbook_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Create a new book project."""
    if update.effective_message is None:
        return

    raw = " ".join(context.args).strip()

    if not raw:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/newbook Title | Genre | Audience | Premise\n\n"
            "Example:\n"
            "/newbook The Silent City | Mystery | Adult readers | "
            "A journalist discovers that every missing person "
            "in her city received the same anonymous letter."
        )
        return

    try:
        title, genre, audience, premise = parse_pipe_arguments(
            raw,
            4,
        )

        book = storage.create_book(
            user_id=get_user_id(update),
            title=title,
            genre=genre,
            audience=audience,
            premise=premise,
        )

    except ValueError as exc:
        await update.effective_message.reply_text(
            f"Could not create book: {exc}"
        )
        return

    await update.effective_message.reply_text(
        f"✅ Book created successfully!\n\n"
        f"ID: {book['id']}\n"
        f"Title: {book['title']}\n"
        f"Genre: {book['genre']}\n"
        f"Audience: {book['audience']}\n\n"
        f"Next step:\n"
        f"/outline {book['id']} | 12"
    )


async def books_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """List the user's books."""
    if update.effective_message is None:
        return

    books = storage.list_books(get_user_id(update))

    if not books:
        await update.effective_message.reply_text(
            "You have no books yet.\n\n"
            "Create one with:\n"
            "/newbook Title | Genre | Audience | Premise"
        )
        return

    lines = ["📚 YOUR BOOKS", ""]

    for book in books:
        stats = storage.get_book_statistics(
            book["id"],
            get_user_id(update),
        )

        if stats is None:
            continue

        lines.extend(
            [
                f"ID: {book['id']}",
                f"Title: {book['title']}",
                f"Genre: {book['genre']}",
                f"Status: {book['status']}",
                f"Chapters saved: {stats['chapter_count']}",
                f"Words: {stats['total_words']:,}",
                "",
            ]
        )

    await send_long_message(
        update,
        "\n".join(lines),
    )


# ============================================================
# OUTLINE GENERATION
# ============================================================

async def outline_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Generate and save a book outline."""
    if update.effective_message is None:
        return

    raw = " ".join(context.args).strip()

    if not raw:
        await update.effective_message.reply_text(
            "Usage:\n/outline BOOK_ID | CHAPTER_COUNT\n\n"
            "Example:\n/outline 1 | 12"
        )
        return

    try:
        book_part, count_part = parse_pipe_arguments(
            raw,
            2,
        )

        book_id = parse_positive_integer(
            book_part,
            "Book ID",
        )

        chapter_count = parse_positive_integer(
            count_part,
            "Chapter count",
        )

        user_id = get_user_id(update)

        book = get_book_or_raise(
            book_id,
            user_id,
        )

        await update.effective_message.reply_text(
            "🧠 Developing the book outline...\n\n"
            "I'll design the narrative structure, characters, "
            "chapter progression, and major story turns."
        )

        engine = get_engine()

        outline = await engine.generate_outline(
            title=book["title"],
            genre=book["genre"],
            audience=book["audience"],
            premise=book["premise"],
            chapter_count=chapter_count,
        )

        saved = storage.save_outline(
            book_id,
            user_id,
            outline,
        )

        if not saved:
            raise ValueError(
                "Could not save the outline."
            )

        story_bible = {
            "characters": outline.get("characters", []),
            "worldbuilding": outline.get("worldbuilding", []),
            "themes": outline.get("themes", []),
            "continuity_facts": [],
            "unresolved_threads": [],
        }

        storage.save_story_bible(
            book_id,
            user_id,
            story_bible,
        )

        chapter_lines = []

        for chapter in outline["chapters"]:
            chapter_lines.append(
                f"{chapter['number']}. {chapter['title']}\n"
                f"   {chapter.get('summary', '')}"
            )

        response = (
            f"✅ OUTLINE SAVED\n\n"
            f"Book: {book['title']}\n"
            f"Book ID: {book_id}\n"
            f"Chapters: {len(outline['chapters'])}\n\n"
            f"SYNOPSIS\n{outline.get('synopsis', '')}\n\n"
            f"CHAPTER PLAN\n\n"
            + "\n\n".join(chapter_lines)
            + f"\n\nNext step:\n/write {book_id} 1"
        )

        await send_long_message(
            update,
            response,
        )

    except (
        ValueError,
        WritingEngineError,
    ) as exc:
        await update.effective_message.reply_text(
            f"❌ Outline generation failed:\n{exc}"
        )

    except Exception:
        logger.exception("Outline generation failed.")

        await update.effective_message.reply_text(
            "An unexpected error occurred while creating the outline. "
            "Check the Railway logs before retrying."
        )


# ============================================================
# CHAPTER WRITING
# ============================================================

async def write_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Generate a chapter and save it to the database."""
    if update.effective_message is None:
        return

    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage:\n/write BOOK_ID CHAPTER_NUMBER\n\n"
            "Example:\n/write 1 1"
        )
        return

    try:
        book_id = parse_positive_integer(
            context.args[0],
            "Book ID",
        )

        chapter_number = parse_positive_integer(
            context.args[1],
            "Chapter number",
        )

        user_id = get_user_id(update)

        book = get_book_or_raise(
            book_id,
            user_id,
        )

        outline = get_outline_or_raise(
            book_id,
            user_id,
        )

        chapter_plan = next(
            (
                chapter
                for chapter in outline["chapters"]
                if chapter.get("number") == chapter_number
            ),
            None,
        )

        if chapter_plan is None:
            raise ValueError(
                "That chapter number does not exist in the outline."
            )

        await update.effective_message.reply_text(
            f"✍️ Writing Chapter {chapter_number}...\n\n"
            "I'm checking the saved story context and chapter plan "
            "before generating the draft."
        )

        previous_chapters = storage.list_chapters(
            book_id,
            user_id,
        )

        if previous_chapters is None:
            raise ValueError(
                "Could not retrieve the chapter history."
            )

        previous_chapters = [
            chapter
            for chapter in previous_chapters
            if chapter["chapter_number"] < chapter_number
        ]

        story_bible = storage.get_story_bible(
            book_id,
            user_id,
        ) or {}

        engine = get_engine()

        result = await engine.write_chapter(
            book=book,
            outline=outline,
            chapter_number=chapter_number,
            previous_chapters=previous_chapters,
            story_bible=story_bible,
        )

        saved_chapter = storage.save_chapter(
            book_id=book_id,
            user_id=user_id,
            chapter_number=chapter_number,
            title=result["title"],
            content=result["content"],
            summary=result["summary"],
            status="draft",
            revision_note="AI-generated chapter",
        )

        existing_facts = story_bible.get(
            "continuity_facts",
            [],
        )

        new_facts = result.get(
            "continuity_facts",
            [],
        )

        story_bible["continuity_facts"] = (
            existing_facts + new_facts
        )[-100:]

        storage.save_story_bible(
            book_id,
            user_id,
            story_bible,
        )

        storage.update_book_status(
            book_id,
            user_id,
            "writing",
        )

        response = (
            f"✅ CHAPTER SAVED\n\n"
            f"Book: {book['title']}\n"
            f"Chapter: {chapter_number}\n"
            f"Title: {saved_chapter['title']}\n"
            f"Word count: {saved_chapter['word_count']:,}\n\n"
            f"SUMMARY\n{saved_chapter['summary']}\n\n"
            f"CHAPTER CONTENT\n\n"
            f"{saved_chapter['content']}\n\n"
            f"NEXT OPTIONS\n"
            f"/review {book_id} {chapter_number}\n"
            f"/chapter {book_id} {chapter_number}"
        )

        await send_long_message(
            update,
            response,
        )

    except (
        ValueError,
        WritingEngineError,
    ) as exc:
        await update.effective_message.reply_text(
            f"❌ Chapter generation failed:\n{exc}"
        )

    except Exception:
        logger.exception("Chapter generation failed.")

        await update.effective_message.reply_text(
            "An unexpected error occurred while writing the chapter. "
            "Check the Railway logs. If the database was updated, "
            "use /chapter to verify whether the chapter was saved."
        )


# ============================================================
# CHAPTER RETRIEVAL
# ============================================================

async def chapter_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Display a saved chapter."""
    if update.effective_message is None:
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/chapter BOOK_ID CHAPTER_NUMBER\n\n"
            "Example:\n/chapter 1 1"
        )
        return

    try:
        book_id = parse_positive_integer(
            context.args[0],
            "Book ID",
        )

        chapter_number = parse_positive_integer(
            context.args[1],
            "Chapter number",
        )

        user_id = get_user_id(update)

        book = get_book_or_raise(
            book_id,
            user_id,
        )

        chapter = get_chapter_or_raise(
            book_id,
            user_id,
            chapter_number,
        )

        await send_long_message(
            update,
            (
                f"📖 {book['title']}\n"
                f"Chapter {chapter_number}: {chapter['title']}\n"
                f"Status: {chapter['status']}\n"
                f"Words: {chapter['word_count']:,}\n\n"
                f"{chapter['content']}"
            ),
        )

    except ValueError as exc:
        await update.effective_message.reply_text(
            str(exc)
        )


# ============================================================
# CHAPTER REVIEW
# ============================================================

async def review_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Review a saved chapter."""
    if update.effective_message is None:
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/review BOOK_ID CHAPTER_NUMBER\n\n"
            "Example:\n/review 1 1"
        )
        return

    try:
        book_id = parse_positive_integer(
            context.args[0],
            "Book ID",
        )

        chapter_number = parse_positive_integer(
            context.args[1],
            "Chapter number",
        )

        user_id = get_user_id(update)

        book = get_book_or_raise(
            book_id,
            user_id,
        )

        outline = get_outline_or_raise(
            book_id,
            user_id,
        )

        chapter = get_chapter_or_raise(
             book_id,
            user_id,
            chapter_number,
        )

        previous_chapters = storage.list_chapters(
            book_id,
            user_id,
        ) or []

        previous_chapters = [
            item
            for item in previous_chapters
            if item["chapter_number"] < chapter_number
        ]

        await update.effective_message.reply_text(
            "🔎 Reviewing the chapter for narrative weaknesses, "
            "pacing, characterization, prose, and continuity..."
        )

        engine = get_engine()

        report = await engine.review_chapter(
            book=book,
            outline=outline,
            chapter_number=chapter_number,
            chapter_title=chapter["title"],
            content=chapter["content"],
            previous_chapters=previous_chapters,
        )

        lines = [
            f"📝 EDITORIAL REPORT",
            "",
            f"Book: {book['title']}",
            f"Chapter: {chapter_number}",
            "",
            "OVERALL ASSESSMENT",
            str(report.get("overall_assessment", "")),
            "",
            "STRENGTHS",
        ]

        for item in report.get("strengths", []):
            lines.append(f"• {item}")

        lines.extend(
            [
                "",
                "ISSUES TO ADDRESS",
            ]
        )

        for issue in report.get("issues", []):
            if isinstance(issue, dict):
                lines.extend(
                    [
                        f"[{issue.get('severity', 'unspecified').upper()}]",
                        str(issue.get("problem", "")),
                        f"Evidence: {issue.get('evidence', '')}",
                        f"Recommendation: {issue.get('recommendation', '')}",
                        "",
                    ]
                )

        lines.append("CONTINUITY WARNINGS")

        for warning in report.get(
            "continuity_warnings",
            [],
        ):
            lines.append(f"• {warning}")

        lines.extend(
            [
                "",
                "REVISION PRIORITIES",
            ]
        )

        for priority in report.get(
            "revision_priorities",
            [],
        ):
            lines.append(f"• {priority}")

        lines.extend(
            [
                "",
                "To revise this chapter, use:",
                f"/revise {book_id} {chapter_number} | "
                "Apply the editorial feedback.",
            ]
        )

        await send_long_message(
            update,
            "\n".join(lines),
        )

    except (
        ValueError,
        WritingEngineError,
    ) as exc:
        await update.effective_message.reply_text(
            f"Review failed:\n{exc}"
        )

    except Exception:
        logger.exception("Chapter review failed.")

        await update.effective_message.reply_text(
            "An unexpected error occurred during the review. "
            "Check the Railway logs."
        )


# ============================================================
# CHAPTER REVISION
# ============================================================
async def revise_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Revise a saved chapter using editorial feedback or instructions."""
    if update.effective_message is None:
        return

    raw = " ".join(context.args).strip()

    if "|" not in raw:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/revise BOOK_ID CHAPTER_NUMBER | INSTRUCTIONS\n\n"
            "Example:\n"
            "/revise 1 1 | Improve the dialogue, remove repetition, "
            "and strengthen the ending without changing the plot."
        )
        return

    try:
        command_part, instructions = raw.split("|", 1)

        identifiers = command_part.strip().split()

        if len(identifiers) != 2:
            raise ValueError(
                "Provide a book ID and chapter number before |."
            )

        book_id = parse_positive_integer(
            identifiers[0],
            "Book ID",
        )

        chapter_number = parse_positive_integer(
            identifiers[1],
            "Chapter number",
        )

        instructions = instructions.strip()

        if not instructions:
            raise ValueError(
                "Provide instructions for the revision."
            )

        user_id = get_user_id(update)

        book = get_book_or_raise(
            book_id,
            user_id,
        )

        outline = get_outline_or_raise(
            book_id,
            user_id,
        )

        chapter = get_chapter_or_raise(
            book_id,
            user_id,
            chapter_number,
        )

        previous_chapters = storage.list_chapters(
            book_id,
            user_id,
        ) or []

        previous_chapters = [
            item
            for item in previous_chapters
            if item["chapter_number"] < chapter_number
        ]

        await update.effective_message.reply_text(
            "✍️ Revising the chapter. "
            "The previous saved version will be preserved."
        )

        engine = get_engine()

        result = await engine.revise_chapter(
            book=book,
            outline=outline,
            chapter_number=chapter_number,
            chapter_title=chapter["title"],
            content=chapter["content"],
            revision_instructions=instructions,
        )

        revised = storage.save_chapter(
            book_id=book_id,
            user_id=user_id,
            chapter_number=chapter_number,
            title=result["title"],
            content=result["content"],
            summary=result["summary"] or chapter["summary"],
            status="draft",
            revision_note=instructions,
        )

        await send_long_message(
            update,
            (
                f"✅ CHAPTER REVISED\n\n"
                f"Book: {book['title']}\n"
                f"Chapter: {chapter_number}\n"
                f"Title: {revised['title']}\n"
                f"Words: {revised['word_count']:,}\n\n"
                f"{revised['content']}\n\n"
                f"Previous version preserved in revision history."
            ),
        )

    except (
        ValueError,
        WritingEngineError,
    ) as exc:
        await update.effective_message.reply_text(
            f"Revision failed:\n{exc}"
        )

    except Exception:
        logger.exception("Chapter revision failed.")

        await update.effective_message.reply_text(
            "An unexpected error occurred during revision. "
            "Check the Railway logs."
        )


# ============================================================
# BOOK STATISTICS
# ============================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Show a book's current statistics."""
    if update.effective_message is None:
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Usage:\n/status BOOK_ID\n\n"
            "Example:\n/status 1"
        )
        return

    try:
        book_id = parse_positive_integer(
            context.args[0],
            "Book ID",
        )

        user_id = get_user_id(update)

        stats = storage.get_book_statistics(
            book_id,
            user_id,
        )

        if stats is None:
            raise ValueError(
                "Book not found. Use /books to list your projects."
            )

        chapters = storage.list_chapters(
            book_id,
            user_id,
        ) or []

        approved_words = sum(
            chapter["word_count"]
            for chapter in chapters
            if chapter["status"] == "approved"
        )

        await update.effective_message.reply_text(
            f"📊 BOOK STATUS\n\n"
            f"Title: {stats['title']}\n"
            f"Book ID: {book_id}\n"
            f"Workflow: {stats['status']}\n"
            f"Chapters saved: {stats['chapter_count']}\n"
            f"Approved chapters: {stats['approved_chapters']}\n"
            f"Total words: {stats['total_words']:,}\n"
            f"Words in approved chapters: {approved_words:,}"
        )

    except ValueError as exc:
        await update.effective_message.reply_text(
            str(exc)
        )


# ============================================================
# MANUSCRIPT EXPORT
# ============================================================

async def export_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Export a manuscript and send the file through Telegram."""
    if update.effective_message is None:
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Usage:\n/export BOOK_ID FORMAT\n\n"
            "Formats: txt, docx, epub, pdf\n\n"
            "Example:\n/export 1 docx"
        )
        return

    try:
        book_id = parse_positive_integer(
            context.args[0],
            "Book ID",
        )

        file_format = context.args[1].lower().strip()

        if file_format not in SUPPORTED_EXPORT_FORMATS:
            raise ValueError(
                "Unsupported format. Choose txt, docx, epub, or pdf."
            )

        user_id = get_user_id(update)

        book = get_book_or_raise(
            book_id,
            user_id,
        )

        await update.effective_message.reply_text(
            f"📦 Preparing {file_format.upper()} manuscript..."
        )

        output_path = await asyncio.to_thread(
            exporter.export_book,
            book_id,
            user_id,
            file_format,
        )

        if not output_path.exists():
            raise ValueError(
                "The export file was not created."
            )

        with output_path.open("rb") as file_handle:
            await update.effective_message.reply_document(
                document=InputFile(
                    file_handle,
                    filename=output_path.name,
                ),
                caption=(
                    f"📚 {book['title']}\n"
                    f"Format: {file_format.upper()}\n\n"
                    "Please review the manuscript before publication."
                ),
            )

    except (
        ValueError,
        exporter.ExportError,
    ) as exc:
        await update.effective_message.reply_text(
            f"Export failed:\n{exc}"
        )

    except Exception:
        logger.exception("Manuscript export failed.")

        await update.effective_message.reply_text(
            "An unexpected error occurred during export. "
            "Check the Railway logs."
        )


# ============================================================
# RAILWAY AND APPLICATION STARTUP
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Log unexpected errors from Telegram handlers."""
    logger.error(
        "Unhandled Telegram application error.",
        exc_info=(
            type(context.error),
            context.error,
            context.error.__traceback__,
        ) if context.error else None,
    )


def main() -> None:
    """Initialize the database and start Telegram polling."""
    config.validate_required_settings()

    storage.initialize_storage()

    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("newbook", newbook_command)
    )

    application.add_handler(
        CommandHandler("books", books_command)
    )

    application.add_handler(
        CommandHandler("outline", outline_command)
    )

    application.add_handler(
        CommandHandler("write", write_command)
    )

    application.add_handler(
        CommandHandler("chapter", chapter_command)
    )

    application.add_handler(
        CommandHandler("review", review_command)
    )

    application.add_handler(
        CommandHandler("revise", revise_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    application.add_handler(
        CommandHandler("export", export_command)
    )

    application.add_error_handler(error_handler)

    logger.info("Telegram Book Studio is starting.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
